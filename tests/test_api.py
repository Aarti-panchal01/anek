import tempfile
import uuid
from pathlib import Path

from backend import db


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
