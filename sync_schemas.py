from typing import Any

from pydantic import BaseModel, ConfigDict, Field, field_validator


def require_object(value: Any) -> dict:
    if not isinstance(value, dict):
        raise ValueError("must be a JSON object")
    return value


class StrictModel(BaseModel):
    model_config = ConfigDict(extra="forbid")


class RecordCreate(StrictModel):
    device_id: str = Field(min_length=1, max_length=200)
    data: dict[str, Any]

    @field_validator("device_id")
    @classmethod
    def nonblank_device(cls, value: str) -> str:
        if not value.strip():
            raise ValueError("device_id cannot be blank")
        return value

    @field_validator("data", mode="before")
    @classmethod
    def object_data(cls, value: Any) -> dict:
        return require_object(value)


class SyncRequest(StrictModel):
    device_id: str = Field(min_length=1, max_length=200)
    base_version: int = Field(ge=1)
    changes: dict[str, Any] = Field(min_length=1)
    idempotency_key: str = Field(min_length=1, max_length=200)

    @field_validator("device_id", "idempotency_key")
    @classmethod
    def nonblank(cls, value: str) -> str:
        if not value.strip():
            raise ValueError("value cannot be blank")
        return value


class ResolveRequest(StrictModel):
    device_id: str = Field(min_length=1, max_length=200)
    expected_version: int = Field(ge=1)
    changes: dict[str, Any] = Field(min_length=1)
    idempotency_key: str = Field(min_length=1, max_length=200)

    @field_validator("device_id", "idempotency_key")
    @classmethod
    def nonblank(cls, value: str) -> str:
        if not value.strip():
            raise ValueError("value cannot be blank")
        return value


class SyncResponse(BaseModel):
    outcome: str
    record_id: str
    current_version: int
    data: dict[str, Any]
    message: str
    conflicting_fields: list[str] = Field(default_factory=list)
