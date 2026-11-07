import tempfile
import uuid
import asyncio
from pathlib import Path
from types import SimpleNamespace

import pytest
from fastapi import HTTPException
from starlette.requests import Request
from starlette.responses import Response

from backend import db
from backend import app as application


def test_access_gate_requires_a_valid_signed_token(monkeypatch):
    monkeypatch.setattr(application.settings, "require_access_auth", True)
    monkeypatch.setattr(application.settings, "anek_access_password", "test-password")
    monkeypatch.setattr(application.settings, "anek_auth_secret", "test-secret")

    def request(headers=None):
        return Request({"type": "http", "method": "GET", "path": "/api/projects", "headers": [
            (key.lower().encode(), value.encode()) for key, value in (headers or {}).items()
        ], "query_string": b"", "server": ("test", 80), "client": ("127.0.0.1", 1234), "scheme": "http"})

    async def next_response(_request):
        return Response("ok")

    denied = asyncio.run(application.require_access_token(request(), next_response))
    assert denied.status_code == 401
    token = application._issue_access_token()
    accepted = asyncio.run(application.require_access_token(request({"authorization": f"Bearer {token}"}), next_response))
    assert accepted.status_code == 200


def test_request_rate_limit_returns_retry_after(monkeypatch):
    application._rate_events.clear()
    monkeypatch.setattr(application.settings, "per_ip_limits", "jobs=2")
    request = SimpleNamespace(client=SimpleNamespace(host="198.51.100.23"))
    application.enforce_ip_limit(request, "jobs")
    application.enforce_ip_limit(request, "jobs")
    with pytest.raises(HTTPException) as error:
        application.enforce_ip_limit(request, "jobs")
    assert error.value.status_code == 429
    assert int(error.value.headers["Retry-After"]) > 0


def test_sqlite_persists_projects_and_pending_jobs(monkeypatch):
    Path("data").mkdir(exist_ok=True)
    with tempfile.TemporaryDirectory(dir="data", prefix="test-db-") as folder:
        monkeypatch.setattr(db, "DB_PATH", Path(folder) / "anek.db")
        db.initialize()
        project_id = uuid.uuid4().hex
        job_id = uuid.uuid4().hex
        with db.connect() as connection:
            connection.execute(
                "INSERT INTO projects(id,name,source_lang,target_langs_json) VALUES(?,?,?,?)",
                (project_id, "Synthetic test", "en-IN", '["hi-IN"]'),
            )
            connection.execute(
                "INSERT INTO jobs(id,project_id,kind,state,stage,total) VALUES(?,?,?,?,?,?)",
                (job_id, project_id, "dub", "pending", "queued", 1),
            )
        with db.connect() as connection:
            row = connection.execute("SELECT state,stage,total FROM jobs WHERE id=?", (job_id,)).fetchone()
        assert tuple(row) == ("pending", "queued", 1)
