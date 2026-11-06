import asyncio
import json
from pathlib import Path
from starlette.background import BackgroundTasks
from starlette.requests import Request

from backend import db
from backend import app as app_module
from tests.test_api_destinations import _sandbox


def _request():
    return Request({"type": "http", "method": "POST", "path": "/", "headers": [], "client": ("127.0.0.1", 1234)})


def _resolve(coroutine):
    try:
        coroutine.send(None)
    except StopIteration as completed:
        return completed.value
    raise AssertionError("endpoint unexpectedly performed asynchronous I/O")


def test_running_job_is_marked_interrupted_and_can_be_resumed(monkeypatch):
    async def scenario():
        with _sandbox(monkeypatch) as (_, data_root):
            source = data_root / "uploads" / "job-test" / "source.mp4"
            source.parent.mkdir(parents=True)
            source.write_bytes(b"mock video")
            project = _resolve(app_module.create_project(app_module.ProjectCreate(
                name="resume", source_lang="en-IN", target_langs=["hi-IN"],
            )))
            with db.connect() as connection:
                connection.execute("UPDATE projects SET video_path=?,duration_s=20,status='processing' WHERE id=?", (str(source), project["id"]))
                connection.execute(
                    "INSERT INTO jobs(id,project_id,kind,voice_mode,stock_voice,state,stage,total) VALUES(?,?,?,?,?,?,?,?)",
                    ("resume-job", project["id"], "dub", "stock", "Shivani", "running", "transcribing", 1),
                )

            await app_module.startup()
            with db.connect() as connection:
                row = connection.execute("SELECT state FROM jobs WHERE id='resume-job'").fetchone()
                project_row = connection.execute("SELECT status FROM projects WHERE id=?", (project["id"],)).fetchone()
            assert row["state"] == "interrupted"
            assert project_row["status"] == "interrupted"

            queued = await app_module.resume_job(_request(), project["id"], BackgroundTasks())
            assert queued["state"] == "pending"
            with db.connect() as connection:
                row = connection.execute("SELECT state FROM jobs WHERE id='resume-job'").fetchone()
            assert row["state"] == "pending"

    asyncio.run(scenario())


def test_failed_language_can_be_retried_independently(monkeypatch):
    async def scenario():
        with _sandbox(monkeypatch) as (_, data_root):
            source = data_root / "uploads" / "language-retry" / "source.mp4"
            source.parent.mkdir(parents=True)
            source.write_bytes(b"mock video")
            project = _resolve(app_module.create_project(app_module.ProjectCreate(
                name="language retry", source_lang="en-IN", target_langs=["hi-IN", "kn-IN"],
            )))
            with db.connect() as connection:
                connection.execute("UPDATE projects SET video_path=?,duration_s=20,status='failed' WHERE id=?", (str(source), project["id"]))
                connection.execute("INSERT INTO jobs(id,project_id,kind,lang,state,stage) VALUES(?,?,?,?,?,?)",
                                   ("language-retry-job", project["id"], "dub", None, "failed", "failed"))
            tasks = BackgroundTasks()
            queued = await app_module.retry_language(_request(), project["id"], "kn-IN", tasks)
            assert queued["lang"] == "kn-IN"
            assert len(tasks.tasks) == 1
            with db.connect() as connection:
                row = connection.execute("SELECT kind,lang,state FROM jobs WHERE id='language-retry-job'").fetchone()
            assert tuple(row) == ("retry_language", "kn-IN", "pending")
    asyncio.run(scenario())


def test_job_resume_reuses_transcript_and_completed_clip_checkpoint(monkeypatch):
    async def scenario():
        paid_transcriptions = 0
        with _sandbox(monkeypatch) as (_, data_root):
            source = data_root / "uploads" / "job-test" / "source.mp4"
            source.parent.mkdir(parents=True)
            source.write_bytes(b"mock video")
            project = _resolve(app_module.create_project(app_module.ProjectCreate(
                name="resume", source_lang="en-IN", target_langs=["hi-IN"],
            )))
            with db.connect() as connection:
                connection.execute("UPDATE projects SET video_path=?,duration_s=20,status='processing' WHERE id=?", (str(source), project["id"]))
                connection.execute(
                    "INSERT INTO jobs(id,project_id,kind,voice_mode,stock_voice,state,stage,total) VALUES(?,?,?,?,?,?,?,?)",
                    ("resume-job", project["id"], "dub", "stock", "Shivani", "pending", "queued", 1),
                )
            audio = data_root / "exports" / project["id"] / "clips" / "hi-IN" / "clip_001.wav"
            audio.parent.mkdir(parents=True)
            audio.write_bytes(b"completed paid take")
            app_module._shutdown_requested.set()

            def fake_quality_pass(*args, resume_state, checkpoint_callback, continue_check, **kwargs):
                nonlocal paid_transcriptions
                if not resume_state.get("transcript"):
                    paid_transcriptions += 1
                    resume_state["transcript"] = {"raw": "source transcript"}
                resume_state["current_language"] = "hi-IN"
                resume_state["last_clip_index"] = 0
                resume_state.setdefault("clips", {}).setdefault("hi-IN", {})["0"] = {
                    "audio_key": audio.relative_to(data_root / "exports" / project["id"]).as_posix(),
                    "clip": {"clip": 1, "start_s": 0.0, "end_s": 2.0, "source_cleaned": "hello", "translation": "नमस्ते", "status": "green", "reasons": [], "final_audio_duration_s": 1.0, "translation_synthesis_attempts": []},
                }
                checkpoint_callback("clip", resume_state)
                continue_check()
                return [{"language": "hi-IN", "clips": [], "term_sheet": {"terms": []}}]

            monkeypatch.setattr(app_module, "run_quality_pass", fake_quality_pass)
            await app_module.run_job("resume-job")
            with db.connect() as connection:
                job = connection.execute("SELECT state,checkpoint_json FROM jobs WHERE id='resume-job'").fetchone()
            assert job["state"] == "interrupted"
            assert json.loads(job["checkpoint_json"])["transcript"]["raw"] == "source transcript"
            with db.connect() as connection:
                connection.execute("UPDATE jobs SET state='pending' WHERE id='resume-job'")
            app_module._shutdown_requested.clear()
            await app_module.run_job("resume-job")

            with db.connect() as connection:
                job = connection.execute("SELECT state FROM jobs WHERE id='resume-job'").fetchone()
                dub_count = connection.execute("SELECT COUNT(*) FROM dubs").fetchone()[0]
            assert job["state"] == "complete"
            assert dub_count == 1
            assert paid_transcriptions == 1
            app_module._shutdown_requested.clear()

    asyncio.run(scenario())


def test_job_with_zero_clips_is_failed_instead_of_completed(monkeypatch):
    async def scenario():
        with _sandbox(monkeypatch) as (_, data_root):
            source = data_root / "uploads" / "empty" / "source.mp4"
            source.parent.mkdir(parents=True)
            source.write_bytes(b"mock video")
            project = _resolve(app_module.create_project(app_module.ProjectCreate(
                name="empty output", source_lang="en-IN", target_langs=["hi-IN"],
            )))
            with db.connect() as connection:
                connection.execute("UPDATE projects SET video_path=?,duration_s=20 WHERE id=?", (str(source), project["id"]))
                connection.execute(
                    "INSERT INTO jobs(id,project_id,kind,voice_mode,stock_voice,state,stage,total) VALUES(?,?,?,?,?,?,?,?)",
                    ("empty-job", project["id"], "dub", "stock", "Shivani", "pending", "queued", 1),
                )

            monkeypatch.setattr(app_module, "run_quality_pass", lambda *args, **kwargs: [])
            await app_module.run_job("empty-job")

            with db.connect() as connection:
                job = connection.execute("SELECT state,error FROM jobs WHERE id='empty-job'").fetchone()
                project_state = connection.execute("SELECT status FROM projects WHERE id=?", (project["id"],)).fetchone()
            assert job["state"] == "failed"
            assert "without creating any clips" in job["error"]
            assert project_state["status"] == "failed"

    asyncio.run(scenario())
