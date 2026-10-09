from datetime import datetime, timezone

from sqlalchemy import Integer, String, DateTime, ForeignKey, UniqueConstraint
from sqlalchemy.dialects.postgresql import JSONB, UUID
from sqlalchemy.orm import Mapped, mapped_column

from database import Base


class Student(Base):
    __tablename__ = "students"

    id: Mapped[int] = mapped_column(
        Integer, primary_key=True, index=True
    )

    name: Mapped[str] = mapped_column(
        String(100), nullable=False
    )

    email: Mapped[str] = mapped_column(
        String(255), unique=True, nullable=False, index=True
    )

    age: Mapped[int] = mapped_column(
        Integer, nullable=False
    )

    course: Mapped[str] = mapped_column(
        String(100), nullable=False
    )

    created_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True),
        default=lambda: datetime.now(timezone.utc),
        nullable=False
    )

    updated_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True),
        default=lambda: datetime.now(timezone.utc),
        onupdate=lambda: datetime.now(timezone.utc),
        nullable=False
    )


class SyncRecord(Base):
    __tablename__ = "sync_records"

    id: Mapped[str] = mapped_column(UUID(as_uuid=False), primary_key=True)
    data: Mapped[dict] = mapped_column(JSONB, nullable=False)
    version: Mapped[int] = mapped_column(Integer, nullable=False, default=1)
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), nullable=False)
    updated_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), nullable=False)


class ChangeHistory(Base):
    __tablename__ = "change_history"
    __table_args__ = (UniqueConstraint("record_id", "version", name="uq_history_record_version"),)

    id: Mapped[str] = mapped_column(UUID(as_uuid=False), primary_key=True)
    record_id: Mapped[str] = mapped_column(UUID(as_uuid=False), ForeignKey("sync_records.id", ondelete="CASCADE"), nullable=False, index=True)
    version: Mapped[int] = mapped_column(Integer, nullable=False)
    base_version: Mapped[int] = mapped_column(Integer, nullable=False)
    device_id: Mapped[str] = mapped_column(String(200), nullable=False)
    idempotency_key: Mapped[str] = mapped_column(String(200), nullable=False)
    changed_fields: Mapped[list] = mapped_column(JSONB, nullable=False)
    resulting_data: Mapped[dict | None] = mapped_column(JSONB, nullable=True)
    accepted_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), nullable=False)


class IdempotencyResult(Base):
    __tablename__ = "sync_idempotency"
    __table_args__ = (UniqueConstraint("device_id", "idempotency_key", name="uq_sync_idempotency_device_key"),)

    id: Mapped[int] = mapped_column(Integer, primary_key=True)
    device_id: Mapped[str] = mapped_column(String(200), nullable=False)
    idempotency_key: Mapped[str] = mapped_column(String(200), nullable=False)
    request_hash: Mapped[str] = mapped_column(String(64), nullable=False)
    response: Mapped[dict] = mapped_column(JSONB, nullable=False)
