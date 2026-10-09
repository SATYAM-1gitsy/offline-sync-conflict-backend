
from datetime import datetime, timezone
from typing import Annotated
from uuid import UUID, uuid4

from fastapi import Depends, FastAPI, HTTPException, status
from pydantic import BaseModel, EmailStr
from sqlalchemy import text, select
from sqlalchemy.orm import Session

from database import engine, SessionLocal
from sync_engine import apply_change, resolve_change
from models import ChangeHistory, Student, SyncRecord
from sync_schemas import RecordCreate, ResolveRequest, SyncRequest, SyncResponse

app = FastAPI(
    title="Student Management and Offline Sync API",
    description=("Offline synchronization uses optimistic versions and top-level field conflicts. "
                 "Device IDs identify clients but are not authentication credentials. "
                 "Idempotency keys are retained indefinitely per device."),
)


def get_db():
    db = SessionLocal()
    try:
        yield db
    finally:
        db.close()


DbSession = Annotated[Session, Depends(get_db)]


@app.get("/")
def home():
    return {"message": "Student Management API is running"}


@app.get("/db-check")
def db_check():
    try:
        with engine.connect() as connection:
            connection.execute(text("SELECT 1"))
        return {"database": "connected"}
    except Exception as e:
        raise HTTPException(status_code=503, detail="Database is unavailable") from e


@app.get("/students")
def get_students(db: DbSession):
    statement = select(Student)
    students = db.scalars(statement).all()
    return students


class StudentCreate(BaseModel):
    name: str
    email: EmailStr
    age: int
    course: str


@app.post("/students", status_code=201)
def create_student(student: StudentCreate, db: DbSession):
    new_student = Student(**student.model_dump())

    db.add(new_student)
    db.commit()
    db.refresh(new_student)

    return new_student



@app.get("/students/{student_id}")
def get_student(student_id: int, db: DbSession):
    student = db.get(Student, student_id)

    if student is None:
        raise HTTPException(
            status_code=404,
            detail="Student not found"
        )

    return student


@app.post("/sync/records", status_code=status.HTTP_201_CREATED, tags=["sync"],
          summary="Create a version 1 synchronized JSON record")
def create_sync_record(request: RecordCreate, db: DbSession):
    now = datetime.now(timezone.utc)
    record_id = str(uuid4())
    try:
        record = SyncRecord(id=record_id, data=request.data, version=1, created_at=now, updated_at=now)
        db.add(record)
        # ORM mapper flush order is not guaranteed from independent add() calls;
        # persist the parent first while keeping both writes in one transaction.
        db.flush()
        db.add(ChangeHistory(id=str(uuid4()), record_id=record_id, version=1, base_version=0,
            device_id=request.device_id, idempotency_key=f"create:{record_id}",
            changed_fields=list(request.data.keys()), resulting_data=request.data, accepted_at=now))
        db.commit()
    except Exception:
        db.rollback()
        raise
    return {"record_id": record_id, "version": 1, "data": request.data,
            "created_at": now, "updated_at": now}


@app.get("/sync/records/{record_id}", tags=["sync"], summary="Read current synchronized state")
def get_sync_record(record_id: UUID, db: DbSession):
    record = db.get(SyncRecord, str(record_id))
    if record is None:
        raise HTTPException(404, "Synchronized record not found")
    return {"record_id": record.id, "version": record.version, "data": record.data,
            "created_at": record.created_at, "updated_at": record.updated_at}


@app.post("/sync/records/{record_id}/changes", response_model=SyncResponse, tags=["sync"],
          summary="Apply, merge, or report conflicting field changes",
          responses={409: {"description": "Overlapping fields or idempotency key reused with another payload"}})
def sync_changes(record_id: UUID, request: SyncRequest, db: DbSession):
    """Changes patch top-level keys; {"$delete": true} deletes a key. Nested JSON is atomic."""
    return apply_change(db, str(record_id), request)


@app.get("/sync/records/{record_id}/history", tags=["sync"], summary="List ordered version history")
def get_sync_history(record_id: UUID, db: DbSession):
    record_key = str(record_id)
    if db.get(SyncRecord, record_key) is None:
        raise HTTPException(404, "Synchronized record not found")
    entries = db.scalars(select(ChangeHistory).where(ChangeHistory.record_id == record_key)
                         .order_by(ChangeHistory.version)).all()
    return [{"version": item.version, "base_version": item.base_version,
             "device_id": item.device_id, "changed_fields": item.changed_fields,
             "resulting_data": item.resulting_data,
             "accepted_at": item.accepted_at} for item in entries]


@app.post("/sync/records/{record_id}/resolve", response_model=SyncResponse, tags=["sync"],
          summary="Apply an explicit resolution against the expected server version")
def resolve_sync_conflict(record_id: UUID, request: ResolveRequest, db: DbSession):
    """Resolution changes are authoritative for their supplied top-level fields, if version matches."""
    return resolve_change(db, str(record_id), request)
