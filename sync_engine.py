import hashlib
import json
from datetime import datetime, timezone
from uuid import uuid4

from fastapi import HTTPException
from sqlalchemy import select, text
from sqlalchemy.orm import Session

from models import ChangeHistory, IdempotencyResult, SyncRecord
from sync_schemas import ResolveRequest, SyncRequest

DELETE_MARKER = {"$delete": True}


def _now():
    return datetime.now(timezone.utc)


def _hash(payload: dict) -> str:
    raw = json.dumps(payload, sort_keys=True, separators=(",", ":"), ensure_ascii=False)
    return hashlib.sha256(raw.encode()).hexdigest()


def _idempotent_result(db: Session, device_id: str, key: str, payload: dict):
    prior = db.scalar(select(IdempotencyResult).where(
        IdempotencyResult.device_id == device_id,
        IdempotencyResult.idempotency_key == key,
    ))
    if prior is None:
        return None
    if prior.request_hash != _hash(payload):
        raise HTTPException(409, "Idempotency key was already used with a different payload")
    return prior.response


def _save_result(db: Session, device_id: str, key: str, payload: dict, response: dict):
    db.add(IdempotencyResult(device_id=device_id, idempotency_key=key,
                             request_hash=_hash(payload), response=response))


def _lock_idempotency_key(db: Session, device_id: str, key: str):
    # PostgreSQL transaction advisory lock serializes the same client key even
    # when simultaneous retries target different records.
    db.execute(text("SELECT pg_advisory_xact_lock(hashtext(:key))"),
               {"key": f"sync:{device_id}:{key}"})


def _record_response(record: SyncRecord, outcome: str, message: str, conflicts=None):
    return {"outcome": outcome, "record_id": record.id,
            "current_version": record.version, "data": record.data,
            "message": message, "conflicting_fields": conflicts or []}


def apply_change(db: Session, record_id: str, request: SyncRequest):
    payload = request.model_dump(mode="json")
    try:
        with db.begin():
            _lock_idempotency_key(db, request.device_id, request.idempotency_key)
            duplicate = _idempotent_result(db, request.device_id, request.idempotency_key, payload)
            if duplicate is not None:
                return duplicate
            record = db.scalar(select(SyncRecord).where(SyncRecord.id == record_id).with_for_update())
            if record is None:
                raise HTTPException(404, "Synchronized record not found")
            if request.base_version > record.version:
                response = _record_response(record, "rejected", "base_version is ahead of the server")
                _save_result(db, request.device_id, request.idempotency_key, payload, response)
                return response
            if request.base_version < 1:
                raise HTTPException(422, "base_version must be at least 1")
            if request.base_version < record.version:
                history = db.scalars(select(ChangeHistory).where(
                    ChangeHistory.record_id == record_id,
                    ChangeHistory.version > request.base_version,
                )).all()
                versions = {h.version for h in history}
                if versions != set(range(request.base_version + 1, record.version + 1)):
                    response = _record_response(record, "rejected", "Required change history is unavailable")
                    _save_result(db, request.device_id, request.idempotency_key, payload, response)
                    return response
                server_fields = {field for entry in history for field in entry.changed_fields}
                conflicts = sorted(server_fields.intersection(request.changes.keys()))
                if conflicts:
                    response = _record_response(record, "conflict", "Changes overlap with newer server changes", conflicts)
                    _save_result(db, request.device_id, request.idempotency_key, payload, response)
                    return response
                outcome = "merged"
            else:
                outcome = "accepted"
            updated = dict(record.data)
            for field, value in request.changes.items():
                if isinstance(value, dict) and value == DELETE_MARKER:
                    updated.pop(field, None)
                else:
                    updated[field] = value
            timestamp = _now()
            record.data = updated
            record.version += 1
            record.updated_at = timestamp
            db.add(ChangeHistory(id=str(uuid4()), record_id=record.id, version=record.version,
                base_version=request.base_version, device_id=request.device_id,
                idempotency_key=request.idempotency_key, changed_fields=list(request.changes),
                resulting_data=updated, accepted_at=timestamp))
            response = _record_response(record, outcome, "Changes applied" if outcome == "accepted" else "Non-overlapping changes merged")
            _save_result(db, request.device_id, request.idempotency_key, payload, response)
        return response
    except HTTPException:
        db.rollback()
        raise


def resolve_change(db: Session, record_id: str, request: ResolveRequest):
    payload = request.model_dump(mode="json")
    try:
        with db.begin():
            _lock_idempotency_key(db, request.device_id, request.idempotency_key)
            duplicate = _idempotent_result(db, request.device_id, request.idempotency_key, payload)
            if duplicate is not None:
                return duplicate
            record = db.scalar(select(SyncRecord).where(SyncRecord.id == record_id).with_for_update())
            if record is None:
                raise HTTPException(404, "Synchronized record not found")
            if request.expected_version != record.version:
                response = _record_response(record, "rejected", "Server version changed; fetch the record and review again")
            else:
                updated = dict(record.data)
                for field, value in request.changes.items():
                    if isinstance(value, dict) and value == DELETE_MARKER:
                        updated.pop(field, None)
                    else:
                        updated[field] = value
                timestamp = _now()
                record.data = updated
                old_version = record.version
                record.version += 1
                record.updated_at = timestamp
                db.add(ChangeHistory(id=str(uuid4()), record_id=record.id, version=record.version,
                    base_version=old_version, device_id=request.device_id,
                    idempotency_key=request.idempotency_key, changed_fields=list(request.changes),
                    resulting_data=updated, accepted_at=timestamp))
                response = _record_response(record, "accepted", "Explicit resolution applied")
            _save_result(db, request.device_id, request.idempotency_key, payload, response)
        return response
    except HTTPException:
        db.rollback()
        raise
