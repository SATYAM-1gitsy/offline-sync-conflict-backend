import os
from concurrent.futures import ThreadPoolExecutor
from uuid import uuid4

import pytest
from fastapi.testclient import TestClient
from sqlalchemy import create_engine, text, delete
from sqlalchemy.orm import sessionmaker

from database import Base
from main import app, get_db
from models import ChangeHistory


TEST_DATABASE_URL = os.getenv("TEST_DATABASE_URL")
APPLICATION_DATABASE_URL = os.getenv("DATABASE_URL", "postgresql+psycopg://gdg:gdgpswd@localhost:5432/myDB")
if TEST_DATABASE_URL and TEST_DATABASE_URL == APPLICATION_DATABASE_URL:
    raise RuntimeError("TEST_DATABASE_URL must not point at DATABASE_URL")
if TEST_DATABASE_URL and not TEST_DATABASE_URL.startswith("postgresql"):
    raise RuntimeError("Sync tests require PostgreSQL (JSONB and advisory locks)")

pytestmark = pytest.mark.skipif(not TEST_DATABASE_URL, reason="Set TEST_DATABASE_URL to an isolated PostgreSQL test database")


@pytest.fixture()
def client():
    base_engine = create_engine(TEST_DATABASE_URL)
    schema = f"test_sync_{uuid4().hex}"
    with base_engine.begin() as connection:
        connection.execute(text(f'CREATE SCHEMA "{schema}"'))
    engine = base_engine.execution_options(schema_translate_map={None: schema})
    Base.metadata.create_all(engine)
    app.state.testing_engine = engine
    app.state.testing_schema = schema
    TestingSession = sessionmaker(bind=engine, autoflush=False, expire_on_commit=False)

    def override_db():
        session = TestingSession()
        try:
            yield session
        finally:
            session.close()

    app.dependency_overrides[get_db] = override_db
    with TestClient(app, raise_server_exceptions=False) as test_client:
        yield test_client
    app.dependency_overrides.clear()
    del app.state.testing_engine
    del app.state.testing_schema
    with base_engine.begin() as connection:
        connection.execute(text(f'DROP SCHEMA "{schema}" CASCADE'))
    base_engine.dispose()


def create(client, data=None):
    if data is None:
        data = {"name": "Alex", "course": "CSE"}
    response = client.post("/sync/records", json={"device_id": "device-a", "data": data})
    assert response.status_code == 201
    return response.json()["record_id"]


def change(client, rid, device, base, changes, key):
    return client.post(f"/sync/records/{rid}/changes", json={"device_id": device, "base_version": base, "changes": changes, "idempotency_key": key})


def test_create_read_accept_merge_conflict_resolution(client):
    rid = create(client)
    assert client.get(f"/sync/records/{rid}").json()["version"] == 1
    a = change(client, rid, "a", 1, {"name": "Alexander"}, "a1").json()
    assert a["outcome"] == "accepted"
    b = change(client, rid, "b", 1, {"course": "AI"}, "b1").json()
    assert b["outcome"] == "merged" and b["current_version"] == 3
    conflict = change(client, rid, "c", 1, {"name": "Alec"}, "c1").json()
    assert conflict["outcome"] == "conflict"
    assert conflict["data"]["name"] == "Alexander"
    resolved = client.post(f"/sync/records/{rid}/resolve", json={"device_id": "c", "expected_version": 3, "changes": {"name": "Alec"}, "idempotency_key": "resolve1"}).json()
    assert resolved["outcome"] == "accepted" and resolved["current_version"] == 4
    stale = client.post(f"/sync/records/{rid}/resolve", json={"device_id": "c", "expected_version": 3, "changes": {"name": "other"}, "idempotency_key": "resolve2"}).json()
    assert stale["outcome"] == "rejected"
    history = client.get(f"/sync/records/{rid}/history").json()
    assert [entry["version"] for entry in history] == [1, 2, 3, 4]
    assert history[1]["resulting_data"] == {"name": "Alexander", "course": "CSE"}
    assert history[2]["resulting_data"] == {"name": "Alexander", "course": "AI"}


def test_idempotency_and_validation(client):
    rid = create(client)
    payload = {"device_id": "a", "base_version": 1, "changes": {"x": 1}, "idempotency_key": "same"}
    first = client.post(f"/sync/records/{rid}/changes", json=payload)
    retry = client.post(f"/sync/records/{rid}/changes", json=payload)
    assert first.json() == retry.json()
    changed = {**payload, "changes": {"x": 2}}
    assert client.post(f"/sync/records/{rid}/changes", json=changed).status_code == 409
    assert client.post("/sync/records", json={"device_id": "a", "data": [1, 2]}).status_code == 422
    assert change(client, rid, "z", 10, {"x": 2}, "future").json()["outcome"] == "rejected"
    assert client.get("/sync/records/00000000-0000-0000-0000-000000000000").status_code == 404


def test_concurrent_same_key_and_same_field_updates_are_serialized(client):
    rid = create(client)
    repeated = {"device_id": "retry", "base_version": 1, "changes": {"v": 1}, "idempotency_key": "retry-key"}
    with ThreadPoolExecutor(max_workers=2) as pool:
        results = list(pool.map(lambda _: client.post(f"/sync/records/{rid}/changes", json=repeated).json(), range(2)))
    assert results[0] == results[1]
    assert client.get(f"/sync/records/{rid}").json()["version"] == 2

    requests = [
        {"device_id": "one", "base_version": 2, "changes": {"v": 2}, "idempotency_key": "one-key"},
        {"device_id": "two", "base_version": 2, "changes": {"v": 3}, "idempotency_key": "two-key"},
    ]
    with ThreadPoolExecutor(max_workers=2) as pool:
        results = list(pool.map(lambda body: client.post(f"/sync/records/{rid}/changes", json=body).json(), requests))
    assert sorted(result["outcome"] for result in results) == ["accepted", "conflict"]
    current = client.get(f"/sync/records/{rid}").json()
    assert current["version"] == 3
    assert current["data"]["v"] in (2, 3)


def test_missing_history_rejects_stale_change_without_mutating_state(client):
    rid = create(client)
    change(client, rid, "a", 1, {"name": "Alexander"}, "history-a")
    change(client, rid, "b", 2, {"course": "AI"}, "history-b")
    engine = app.state.testing_engine
    with engine.begin() as connection:
        connection.execute(delete(ChangeHistory).where(
            ChangeHistory.record_id == rid, ChangeHistory.version == 2
        ))
    response = change(client, rid, "c", 1, {"other": "value"}, "missing-history").json()
    assert response["outcome"] == "rejected"
    assert response["current_version"] == 3
    assert response["data"] == {"name": "Alexander", "course": "AI"}
    assert client.get(f"/sync/records/{rid}").json()["version"] == 3


def test_invalid_sync_bodies_and_unknown_record(client):
    rid = create(client)
    url = f"/sync/records/{rid}/changes"
    valid = {"device_id": "d", "base_version": 1, "changes": {"x": 1}, "idempotency_key": "k"}
    for invalid in (
        {**valid, "device_id": " "},
        {**valid, "base_version": 0},
        {**valid, "changes": {}},
        {**valid, "changes": []},
        {**valid, "idempotency_key": " "},
        {k: v for k, v in valid.items() if k != "device_id"},
    ):
        assert client.post(url, json=invalid).status_code == 422
    unknown = "00000000-0000-0000-0000-000000000000"
    assert change(client, unknown, "d", 1, {"x": 1}, "unknown").status_code == 404
    assert client.post("/sync/records", json={"device_id": "d", "data": [1]}).status_code == 422


def test_failed_history_write_rolls_back_record_version_and_data(client):
    rid = create(client)
    schema = app.state.testing_schema
    with app.state.testing_engine.begin() as connection:
        connection.execute(text(
            f'ALTER TABLE "{schema}".change_history '
            'ADD CONSTRAINT test_reject_v2 CHECK (version < 2)'
        ))
    response = change(client, rid, "a", 1, {"name": "Should Roll Back"}, "rollback").status_code
    assert response == 500
    current = client.get(f"/sync/records/{rid}").json()
    assert current["version"] == 1
    assert current["data"] == {"name": "Alex", "course": "CSE"}
    history = client.get(f"/sync/records/{rid}/history").json()
    assert [entry["version"] for entry in history] == [1]
