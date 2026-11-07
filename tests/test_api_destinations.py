import json
import tempfile
from contextlib import contextmanager
from pathlib import Path

import pytest
from fastapi.testclient import TestClient

from backend import db
from backend import app as app_module
from backend.pipeline import phase3b


@contextmanager
def _sandbox(monkeypatch):
    Path("data").mkdir(exist_ok=True)
    with tempfile.TemporaryDirectory(dir="data", prefix="test-api-destinations-") as folder:
        root = Path(folder)
        database = root / "anek.db"
        monkeypatch.setattr(db, "DB_PATH", database)
        monkeypatch.setattr(app_module, "DATA_ROOT", root / "data")
        db.initialize()
        yield database, root / "data"


def _resolve(coroutine):
    try:
        coroutine.send(None)
    except StopIteration as completed:
        return completed.value
    raise AssertionError("endpoint unexpectedly performed asynchronous I/O")


def _roomy_disk(monkeypatch):
    monkeypatch.setattr(app_module.shutil, "disk_usage", lambda _path: type(
        "Usage", (), {"total": 10**12, "used": 10**6, "free": 10**12 - 10**6},
    )())


def test_settings_and_usage_are_derived_from_config_and_database(monkeypatch):
    with _sandbox(monkeypatch) as (database, _):
        settings = _resolve(app_module.read_settings())
        assert settings["daily_credit_limit"] == app_module.settings.gnani_daily_limit
        assert settings["editable"] is False
        with db.connect() as connection:
            assert connection.execute("SELECT COUNT(*) FROM projects").fetchone()[0] == 0
            connection.execute(
                "INSERT INTO credit_ledger(id,project_id,kind,units,est_credits) VALUES(?,?,?,?,?)",
                ("ledger-test", None, "dub-job-estimate", 1, 17),
            )
        usage = _resolve(app_module.usage_ledger(limit=100, offset=0))
        assert usage["entries"][0]["id"] == "ledger-test"
        assert usage["entries"][0]["credits"] == 17
        assert usage["spent"] == 17


def test_project_list_includes_latest_job_status_stage_and_error(monkeypatch):
    with _sandbox(monkeypatch):
        project = _resolve(app_module.create_project(app_module.ProjectCreate(
            name="Job status", source_lang="en-IN", target_langs=["hi-IN"],
        )))
        with db.connect() as connection:
            connection.execute(
                "INSERT INTO jobs(id,project_id,kind,state,stage,error) VALUES(?,?,?,?,?,?)",
                ("job-status", project["id"], "dub", "failed", "transcribing", "ASR failed"),
            )

        listed = _resolve(app_module.list_projects())
        result = next(item for item in listed if item["id"] == project["id"])

        assert result["job_state"] == "failed"
        assert result["job_stage"] == "transcribing"
        assert result["job_error"] == "ASR failed"


def test_video_upload_keeps_compatible_source_unchanged(monkeypatch):
    with _sandbox(monkeypatch) as (_, data_root):
        _roomy_disk(monkeypatch)
        project = _resolve(app_module.create_project(app_module.ProjectCreate(
            name="Playable video", source_lang="en-IN", target_langs=["hi-IN"],
        )))
        source = data_root / "uploads" / project["id"] / "source.mp4"
        monkeypatch.setattr(app_module, "probe_video", lambda path: (20.0, True))
        monkeypatch.setattr(app_module, "_browser_transcode_reason", lambda path: None)
        conversions = []
        monkeypatch.setattr(app_module, "create_browser_compatible_copy", lambda *args: conversions.append(args))
        class Upload:
            filename = "source.mp4"
            blocks = [b"compatible h264 aac bytes", b""]

            async def read(self, size):
                return self.blocks.pop(0)

        upload = Upload()

        result = _resolve(app_module.upload_video(project["id"], upload))
        project_row = Path(app_module.project_row(project["id"])["video_path"])
        assert conversions == []
        assert project_row == source
        assert project_row.read_bytes() == b"compatible h264 aac bytes"
        assert list(source.parent.iterdir()) == [source]
        assert result["filename"] == "source.mp4"
        assert result["evicted_projects"] == []
        assert result["preview_status"] == "pending"


def test_browser_detector_keeps_h264_mp4_without_video_conversion(monkeypatch, tmp_path):
    source = tmp_path / "original.mp4"
    source.write_bytes(b"h264 mp4")
    calls = []
    def probe(command, **kwargs):
        calls.append(command)
        return type("Completed", (), {"stdout": json.dumps({
            "format": {"format_name": "mov,mp4,m4a,3gp,3g2,mj2"},
            "streams": [{"codec_type": "video", "codec_name": "h264", "pix_fmt": "yuv420p"},
                       {"codec_type": "audio", "codec_name": "aac"}],
        })})()
    monkeypatch.setattr(app_module.subprocess, "run", probe)
    monkeypatch.setattr(app_module, "create_browser_compatible_copy", lambda *_: pytest.fail("compatible video was transcoded"))

    assert app_module._browser_transcode_reason(source) is None
    assert app_module.browser_video_path(source) == source
    assert len(calls) == 2


def test_upload_falls_back_to_original_when_browser_conversion_fails(monkeypatch):
    with _sandbox(monkeypatch) as (_, data_root):
        _roomy_disk(monkeypatch)
        project = _resolve(app_module.create_project(app_module.ProjectCreate(
            name="Failed browser conversion", source_lang="en-IN", target_langs=["hi-IN"],
        )))
        monkeypatch.setattr(app_module, "probe_video", lambda path: (20.0, True))
        monkeypatch.setattr(app_module, "_browser_transcode_reason", lambda path: "HEVC video")
        monkeypatch.setattr(app_module, "create_browser_compatible_copy", lambda *_: (_ for _ in ()).throw(RuntimeError("ffmpeg failed")))
        response = TestClient(app_module.app, raise_server_exceptions=False).put(
            f"/api/projects/{project['id']}/video",
            files={"video": ("source.mov", b"original hevc bytes", "video/quicktime")},
        )
        assert response.status_code == 200
        assert response.json()["preview_status"] == "pending"
        original = data_root / "uploads" / project["id"] / "source.mov"
        assert original.read_bytes() == b"original hevc bytes"
        assert app_module.project_row(project["id"])["video_path"] == str(original)
        assert app_module.project_row(project["id"])["status"] == "uploaded"


def test_upload_storage_failure_returns_507_marks_job_and_cleans_partial(monkeypatch):
    with _sandbox(monkeypatch) as (_, data_root):
        _roomy_disk(monkeypatch)
        project = _resolve(app_module.create_project(app_module.ProjectCreate(
            name="Full disk upload", source_lang="en-IN", target_langs=["hi-IN"],
        )))
        with db.connect() as connection:
            connection.execute(
                "INSERT INTO jobs(id,project_id,kind,state,stage,total) VALUES(?,?,?,?,?,?)",
                ("storage-job", project["id"], "dub", "running", "audio", 1),
            )

        def full_disk(_temporary, _target):
            raise OSError(28, "No space left on device")

        monkeypatch.setattr(app_module, "commit_temporary", full_disk)
        monkeypatch.setattr(app_module, "origins", ["https://studio.example"])
        response = TestClient(app_module.app, raise_server_exceptions=False).put(
            f"/api/projects/{project['id']}/video",
            files={"video": ("source.mp4", b"video bytes", "video/mp4")},
            headers={"Origin": "https://studio.example"},
        )

        assert response.status_code == 507
        assert response.json() == {"error": "storage_full", "message": "Server storage is full"}
        assert response.headers["access-control-allow-origin"] == "https://studio.example"
        with db.connect() as connection:
            job = connection.execute("SELECT state,error FROM jobs WHERE id='storage-job'").fetchone()
            saved_project = connection.execute("SELECT status FROM projects WHERE id=?", (project["id"],)).fetchone()
        assert tuple(job) == ("failed", "Server storage is full")
        assert saved_project["status"] == "failed"
        upload_folder = data_root / "uploads" / project["id"]
        assert list(upload_folder.iterdir()) == []


def test_upload_preflight_reserves_space_for_original_and_optional_conversion(monkeypatch):
    with _sandbox(monkeypatch) as (_, data_root):
        project = _resolve(app_module.create_project(app_module.ProjectCreate(
            name="Preflight", source_lang="en-IN", target_langs=["hi-IN"],
        )))
        monkeypatch.setattr(app_module.shutil, "disk_usage", lambda _path: type(
            "Usage", (), {"total": 1000, "used": 900, "free": 31},
        )())
        response = TestClient(app_module.app, raise_server_exceptions=False).put(
            f"/api/projects/{project['id']}/video",
            files={"video": ("source.mp4", b"0123456789abcdef", "video/mp4")},
        )
        assert response.status_code == 507
        assert response.json()["error"] == "storage_full"
        assert not (data_root / "uploads" / project["id"]).exists()


def test_upload_preflight_rejects_files_over_the_configured_limit(monkeypatch):
    with _sandbox(monkeypatch):
        project = _resolve(app_module.create_project(app_module.ProjectCreate(
            name="Too large", source_lang="en-IN", target_langs=["hi-IN"],
        )))
        monkeypatch.setattr(app_module, "MAX_UPLOAD_BYTES", 4)
        response = TestClient(app_module.app, raise_server_exceptions=False).put(
            f"/api/projects/{project['id']}/video",
            files={"video": ("source.mp4", b"12345", "video/mp4")},
        )
        assert response.status_code == 413
        assert "upload limit" in response.json()["detail"]


def test_storage_eviction_removes_oldest_finished_only(monkeypatch):
    with _sandbox(monkeypatch):
        _roomy_disk(monkeypatch)
        ids = []
        for name in ("old", "new", "running", "incoming"):
            project = _resolve(app_module.create_project(app_module.ProjectCreate(
                name=name, source_lang="en-IN", target_langs=["hi-IN"],
            )))
            ids.append(project["id"])
        with db.connect() as connection:
            connection.execute("UPDATE projects SET status='complete',created_at='2020-01-01' WHERE id=?", (ids[0],))
            connection.execute("UPDATE projects SET status='complete',created_at='2021-01-01' WHERE id=?", (ids[1],))
            connection.execute("UPDATE projects SET status='interrupted' WHERE id=?", (ids[2],))
        removed = app_module.ensure_storage_room(ids[3], 1)
        assert removed == [{"id": ids[0], "name": "old"}]
        with db.connect() as connection:
            remaining = {row["id"] for row in connection.execute("SELECT id FROM projects")}
        assert ids[0] not in remaining
        assert {ids[1], ids[2], ids[3]} <= remaining


def test_storage_endpoint_reports_used_and_free_bytes(monkeypatch):
    with _sandbox(monkeypatch):
        monkeypatch.setattr(app_module, "managed_data_bytes", lambda: 750)
        monkeypatch.setattr(app_module.shutil, "disk_usage", lambda _path: type(
            "Usage", (), {"total": 1000, "used": 750, "free": 250},
        )())
        assert _resolve(app_module.storage_status()) == {
            "total_bytes": 1000, "used_bytes": 750, "managed_used_bytes": 750, "free_bytes": 250,
            "used_percent": 75.0, "free_percent": 25.0,
            "budget_bytes": 1000, "budget_percent": 100.0, "budget_used_percent": 75.0,
            "export_retention_hours": 24,
        }


def test_project_budget_uses_managed_data_not_volume_used_bytes(monkeypatch):
    with _sandbox(monkeypatch):
        _roomy_disk(monkeypatch)
        monkeypatch.setattr(app_module, "managed_data_bytes", lambda: 1024)
        project = _resolve(app_module.create_project(app_module.ProjectCreate(
            name="Volume usage isolation", source_lang="en-IN", target_langs=["hi-IN"],
        )))
        assert app_module.ensure_storage_room(project["id"], 4096) == []


def test_expired_exports_are_removed_after_configured_retention(monkeypatch):
    from datetime import datetime, timezone
    with _sandbox(monkeypatch) as (_, data_root):
        project = _resolve(app_module.create_project(app_module.ProjectCreate(
            name="Expiring export", source_lang="en-IN", target_langs=["hi-IN"],
        )))
        folder = data_root / "exports" / project["id"]
        folder.mkdir(parents=True)
        final = folder / "sample_hi-IN_ai-dubbed.mp4"
        final.write_bytes(b"final")
        with db.connect() as connection:
            connection.execute(
                "INSERT INTO export_events(id,project_id,lang,formats_json,unresolved_red_json,created_at) VALUES(?,?,?,?,?,?)",
                ("old-export", project["id"], "hi-IN", '["mp4"]', '[]', "2020-01-01T00:00:00+00:00"),
            )
        expired = app_module.cleanup_expired_exports(datetime(2020, 1, 2, tzinfo=timezone.utc))
        assert expired == [final]
        assert not final.exists()
        with db.connect() as connection:
            assert connection.execute("SELECT COUNT(*) FROM export_events WHERE id='old-export'").fetchone()[0] == 0
        assert app_module.project_export_expiry(project["id"]) is None


def test_startup_cleanup_removes_orphans_but_preserves_voice_and_database(monkeypatch):
    with _sandbox(monkeypatch) as (database, data_root):
        data_root.mkdir(parents=True, exist_ok=True)
        stale = data_root / "orphan.tmp"
        empty = data_root / "empty.wav"
        voice = data_root / "voices" / "keep.tmp"
        stale.write_bytes(b"partial")
        empty.write_bytes(b"")
        voice.parent.mkdir(parents=True)
        voice.write_bytes(b"voice")
        removed = app_module.cleanup_orphaned_files(data_root)
        assert set(removed) == {stale, empty}
        assert not stale.exists() and not empty.exists()
        assert voice.read_bytes() == b"voice"
        assert database.is_file()

        project = _resolve(app_module.create_project(app_module.ProjectCreate(
            name="Missing source", source_lang="en-IN", target_langs=["hi-IN"],
        )))
        with db.connect() as connection:
            connection.execute("UPDATE projects SET video_path=? WHERE id=?", (str(data_root / "gone.mp4"), project["id"]))
        assert app_module.mark_missing_project_sources() == [project["id"]]
        assert app_module.project_row(project["id"])["status"] == "files_removed"


def test_delete_project_removes_upload_exports_and_database_records(monkeypatch):
    with _sandbox(monkeypatch) as (_, data_root):
        project = _resolve(app_module.create_project(app_module.ProjectCreate(
            name="Delete project", source_lang="en-IN", target_langs=["hi-IN"],
        )))
        project_id = project["id"]
        upload_dir = data_root / "uploads" / project_id
        export_dir = data_root / "exports" / project_id
        upload_dir.mkdir(parents=True)
        export_dir.mkdir(parents=True)
        source = upload_dir / "source.mp4"
        source.write_bytes(b"source")
        (upload_dir / "browser.mp4").write_bytes(b"preview")
        (export_dir / "dub.mp4").write_bytes(b"export")
        with db.connect() as connection:
            connection.execute("UPDATE projects SET video_path=? WHERE id=?", (str(source), project_id))

        assert _resolve(app_module.delete_project(project_id)) == {"deleted": True}
        assert not upload_dir.exists() and not export_dir.exists()
        with db.connect() as connection:
            assert connection.execute("SELECT 1 FROM projects WHERE id=?", (project_id,)).fetchone() is None


def test_export_cleanup_removes_intermediates_but_keeps_source_final_and_resume_files(monkeypatch):
    with _sandbox(monkeypatch) as (_, data_root):
        project = _resolve(app_module.create_project(app_module.ProjectCreate(
            name="Export cleanup", source_lang="en-IN", target_langs=["hi-IN"],
        )))
        project_id = project["id"]
        upload_dir = data_root / "uploads" / project_id
        export_dir = data_root / "exports" / project_id
        clip_audio = export_dir / "clips" / "hi-IN" / "clip_001.wav"
        temp_mix = export_dir / ".anek-tmp" / "stage" / "mix.wav"
        source = upload_dir / "source.mp4"
        preview = upload_dir / "browser.mp4"
        final = export_dir / "dub_hi-IN_ai-dubbed.mp4"
        checkpoint = export_dir / ".job-checkpoint.json"
        for path, content in ((source, b"source"), (preview, b"preview"), (clip_audio, b"clip"),
                              (temp_mix, b"mix"), (final, b"final"), (checkpoint, b"resume")):
            path.parent.mkdir(parents=True, exist_ok=True)
            path.write_bytes(content)
        with db.connect() as connection:
            connection.execute("UPDATE projects SET video_path=? WHERE id=?", (str(source), project_id))
            connection.execute(
                "INSERT INTO clips(id,project_id,idx,start_s,end_s) VALUES(?,?,?,?,?)",
                ("cleanup-clip", project_id, 0, 0, 1),
            )
            connection.execute(
                "INSERT INTO dubs(id,clip_id,lang,audio_path) VALUES(?,?,?,?)",
                ("cleanup-dub", "cleanup-clip", "hi-IN", str(clip_audio)),
            )
            connection.execute(
                "INSERT INTO jobs(id,project_id,kind,state,stage,total) VALUES(?,?,?,?,?,?)",
                ("cleanup-job", project_id, "dub", "interrupted", "interrupted", 1),
            )

        assert app_module.cleanup_project_intermediates(project_id) == []
        assert clip_audio.is_file() and temp_mix.is_file() and preview.is_file()
        with db.connect() as connection:
            connection.execute("UPDATE jobs SET state='complete' WHERE id='cleanup-job'")
        removed = app_module.cleanup_project_intermediates(project_id)
        assert clip_audio in removed and temp_mix in removed and preview in removed
        assert source.is_file() and final.is_file() and checkpoint.is_file()
        assert not clip_audio.exists() and not temp_mix.exists() and not preview.exists()
        with db.connect() as connection:
            assert connection.execute("SELECT audio_path FROM dubs WHERE id='cleanup-dub'").fetchone()[0] is None


def _make_media_project(data_root):
    project = _resolve(app_module.create_project(app_module.ProjectCreate(
        name="Media route test", source_lang="en-IN", target_langs=["hi-IN"],
    )))
    source_dir = data_root / "uploads" / project["id"]
    source_dir.mkdir(parents=True)
    source = source_dir / "source.mp4"
    source.write_bytes(b"source-video")
    with db.connect() as connection:
        connection.execute("UPDATE projects SET video_path=?,original_video_path=?,preview_path=?,preview_status='ready' WHERE id=?",
                           (str(source), str(source), str(source), project["id"]))
    export_dir = data_root / "exports" / project["id"]
    export_dir.mkdir(parents=True)
    dubbed = export_dir / "sample_hi-IN_ai-dubbed.mp4"
    dubbed.write_bytes(b"dubbed-video")
    return project["id"], source, dubbed


def test_project_media_range_request_returns_partial_content_and_headers(monkeypatch):
    with _sandbox(monkeypatch) as (_, data_root):
        project_id, source, dubbed = _make_media_project(data_root)
        monkeypatch.setattr(app_module, "browser_video_path", lambda path: path)
        monkeypatch.setattr(app_module, "browser_dubbed_video_path", lambda path: path)
        monkeypatch.setattr(app_module, "_browser_transcode_reason", lambda path: None)
        monkeypatch.setattr(app_module, "_validate_media_file", lambda path: None)
        response = TestClient(app_module.app).get(
            f"/api/projects/{project_id}/video", headers={"Range": "bytes=2-5"},
        )

        assert response.status_code == 206
        assert response.headers["content-range"] == f"bytes 2-5/{source.stat().st_size}"
        assert response.headers["accept-ranges"] == "bytes"
        assert response.headers["content-type"] == "video/mp4"
        assert response.content == b"urce"


def test_project_video_and_dubbed_media_without_range_return_full_content(monkeypatch):
    with _sandbox(monkeypatch) as (_, data_root):
        project_id, source, dubbed = _make_media_project(data_root)
        monkeypatch.setattr(app_module, "browser_video_path", lambda path: path)
        monkeypatch.setattr(app_module, "browser_dubbed_video_path", lambda path: path)
        monkeypatch.setattr(app_module, "_browser_transcode_reason", lambda path: None)
        monkeypatch.setattr(app_module, "_validate_media_file", lambda path: None)
        client = TestClient(app_module.app)

        source_response = client.get(f"/api/projects/{project_id}/video")
        dubbed_response = client.get(f"/api/projects/{project_id}/media?lang=hi-IN")

        for response in (source_response, dubbed_response):
            assert response.status_code == 200
            assert response.headers["content-type"] == "video/mp4"
            assert response.headers["accept-ranges"] == "bytes"
        assert source_response.content == source.read_bytes()
        assert dubbed_response.content == dubbed.read_bytes()


def test_project_video_waits_for_background_preview_and_retry_is_explicit(monkeypatch):
    with _sandbox(monkeypatch) as (_, data_root):
        project_id, source, _ = _make_media_project(data_root)
        with db.connect() as connection:
            connection.execute("UPDATE projects SET preview_status='pending',preview_path=NULL WHERE id=?", (project_id,))
        monkeypatch.setattr(app_module, "browser_video_path", lambda *_: pytest.fail("GET must not convert media"))
        client = TestClient(app_module.app)
        response = client.get(f"/api/projects/{project_id}/video")
        assert response.status_code == 409
        assert response.json()["error"] == "media_unavailable"

        queued = []
        monkeypatch.setattr(app_module, "queue_project_preview", lambda project: queued.append(project))
        retry = client.post(f"/api/projects/{project_id}/preview/retry")
        assert retry.status_code == 202
        assert retry.json()["status"] == "pending"
        assert queued == [project_id]
        assert app_module.project_row(project_id)["preview_status"] == "pending"


def test_project_video_falls_back_to_original_when_prepared_copy_is_invalid(monkeypatch):
    with _sandbox(monkeypatch) as (_, data_root):
        project_id, source, _ = _make_media_project(data_root)
        broken_copy = source.parent / "source-browser.mp4"
        broken_copy.write_bytes(b"truncated")
        with db.connect() as connection:
            connection.execute("UPDATE projects SET preview_path=? WHERE id=?", (str(broken_copy), project_id))

        def validate(path):
            if Path(path) == broken_copy:
                raise ValueError("ffprobe could not read prepared copy")

        monkeypatch.setattr(app_module, "_validate_media_file", validate)
        response = TestClient(app_module.app).get(f"/api/projects/{project_id}/video")
        assert response.status_code == 200
        assert response.content == source.read_bytes()


def test_audio_track_download_is_nonempty_wav(monkeypatch):
    with _sandbox(monkeypatch) as (_, data_root):
        project_id, _, _ = _make_media_project(data_root)
        folder = data_root / "exports" / project_id
        track = folder / "sample_hi-IN_phase3d_ai-dubbed_dubbed-track.wav"
        track.write_bytes(b"RIFF" + b"\0" * 256)
        response = TestClient(app_module.app).get(
            f"/api/projects/{project_id}/downloads/track-dubbed?lang=hi-IN",
        )
        assert response.status_code == 200
        assert response.headers["content-type"] == "audio/wav"
        assert len(response.content) > 0


@pytest.mark.parametrize("bad_media", [b"", b"truncated mp4"])
def test_dubbed_media_recovers_zero_byte_and_truncated_mp4(monkeypatch, bad_media):
    with _sandbox(monkeypatch) as (_, data_root):
        project_id, source, dubbed = _make_media_project(data_root)
        dubbed.write_bytes(bad_media)

        def fail_probe(path):
            raise app_module.subprocess.CalledProcessError(1, ["ffprobe", str(path)], stderr="invalid MP4")

        monkeypatch.setattr(app_module, "browser_dubbed_video_path", fail_probe)
        monkeypatch.setattr(app_module, "_rebuild_dubbed_preview", lambda *args: (_ for _ in ()).throw(
            app_module.subprocess.CalledProcessError(1, ["ffmpeg"], stderr="saved track unavailable")
        ))
        monkeypatch.setattr(app_module, "browser_video_path", lambda path: source)
        monkeypatch.setattr(app_module, "_validate_media_file", lambda path: None)
        response = TestClient(app_module.app).get(f"/api/projects/{project_id}/media?lang=hi-IN")

        assert response.status_code == 200
        assert response.headers["content-type"] == "video/mp4"
        assert response.headers["accept-ranges"] == "bytes"
        assert response.content == source.read_bytes()


def test_dubbed_media_returns_clear_409_if_all_recovery_options_fail(monkeypatch):
    with _sandbox(monkeypatch) as (_, data_root):
        project_id, _, dubbed = _make_media_project(data_root)
        dubbed.write_bytes(b"truncated")

        def fail(*args):
            raise app_module.subprocess.CalledProcessError(1, ["ffprobe"], stderr="invalid media")

        monkeypatch.setattr(app_module, "_browser_transcode_reason", fail)
        monkeypatch.setattr(app_module, "_rebuild_dubbed_preview", fail)
        monkeypatch.setattr(app_module, "_validate_media_file", fail)
        response = TestClient(app_module.app).get(f"/api/projects/{project_id}/media?lang=hi-IN")

        assert response.status_code == 409
        assert response.json()["error"] == "media_unavailable"
        assert "could not be rebuilt" in response.json()["reason"]


def test_browser_preview_conversion_is_single_threaded_and_does_not_scale(monkeypatch):
    with _sandbox(monkeypatch) as (_, data_root):
        data_root.mkdir(parents=True, exist_ok=True)
        source = data_root / "portrait.mov"
        source.write_bytes(b"video")
        calls = []

        def fake_run(command, **kwargs):
            calls.append((command, kwargs))
            if command[0] == "ffmpeg":
                Path(command[-1]).write_bytes(b"browser preview")
                return type("Completed", (), {"stdout": ""})()
            if "ffprobe" in command[0]:
                if "-show_entries" in command and command[command.index("-show_entries") + 1] == "stream=codec_type":
                    payload = {"streams": [{"codec_type": "video"}]}
                elif str(source) in command:
                    payload = {"format": {"format_name": "mov,mp4"}, "streams": [
                        {"codec_type": "video", "codec_name": "hevc", "pix_fmt": "yuv420p"},
                        {"codec_type": "audio", "codec_name": "aac"},
                    ]}
                else:
                    payload = {"format": {"format_name": "mov,mp4,m4a"}, "streams": [
                        {"codec_type": "video", "codec_name": "h264", "pix_fmt": "yuv420p"},
                        {"codec_type": "audio", "codec_name": "aac"},
                    ]}
                return type("Completed", (), {"stdout": json.dumps(payload)})()
            raise AssertionError(command)

        monkeypatch.setattr(app_module.subprocess, "run", fake_run)

        result = app_module.browser_video_path(source)

        assert result.read_bytes() == b"browser preview"
        ffmpeg_command, options = next(call for call in calls if call[0][0] == "ffmpeg")
        assert "-threads" in ffmpeg_command and ffmpeg_command[ffmpeg_command.index("-threads") + 1] == "1"
        assert "-threads:v" in ffmpeg_command and ffmpeg_command[ffmpeg_command.index("-threads:v") + 1] == "1"
        assert "-preset" in ffmpeg_command and ffmpeg_command[ffmpeg_command.index("-preset") + 1] == "veryfast"
        assert "-pix_fmt" in ffmpeg_command and ffmpeg_command[ffmpeg_command.index("-pix_fmt") + 1] == "yuv420p"
        assert "-movflags" in ffmpeg_command and ffmpeg_command[ffmpeg_command.index("-movflags") + 1] == "+faststart"
        assert "-vf" in ffmpeg_command
        assert "scale=in_range=full:out_range=tv,format=yuv420p" in ffmpeg_command
        assert options["timeout"] == 240


def test_browser_preview_returns_original_when_ffmpeg_conversion_fails(monkeypatch, tmp_path):
    source = tmp_path / "source.mov"
    source.write_bytes(b"original upload")
    monkeypatch.setattr(app_module.subprocess, "run", lambda *args, **kwargs: type(
        "Completed", (), {"stdout": json.dumps({"streams": [
            {"codec_type": "video", "codec_name": "hevc"},
        ]})}
    )() if args[0][0] == "ffprobe" else (_ for _ in ()).throw(
        app_module.subprocess.CalledProcessError(-9, args[0])
    ))

    assert app_module.browser_video_path(source) == source
    assert source.read_bytes() == b"original upload"


def test_final_export_mux_copies_video_and_moves_only_audio(monkeypatch, tmp_path):
    source = tmp_path / "source.mp4"
    clip_audio = tmp_path / "clip.wav"
    mixed_work = tmp_path / "work"
    output_dir = tmp_path / "exports"
    source.write_bytes(b"source")
    clip_audio.write_bytes(b"clip")
    mixed_work.mkdir()
    output_dir.mkdir()
    commands = []
    monkeypatch.setattr(phase3b, "duration_of", lambda _path: 1.0)
    monkeypatch.setattr(phase3b, "assert_timing_invariants", lambda *_args, **_kwargs: None)
    monkeypatch.setattr(phase3b, "run_command", lambda command: commands.append(command))

    phase3b.mux_outputs(
        source, [{"start_s": 0, "audio_path": str(clip_audio)}],
        output_dir / "dub.mp4", 1.0, mixed_work, app_module.settings,
        output_dir=output_dir,
    )

    export_command = commands[-1]
    assert "-c:v" in export_command and export_command[export_command.index("-c:v") + 1] == "copy"
    assert "-c:a" in export_command and export_command[export_command.index("-c:a") + 1] == "aac"
    assert "-movflags" in export_command and export_command[export_command.index("-movflags") + 1] == "+faststart"


def test_global_term_memory_crud_persists_and_validates(monkeypatch):
    with _sandbox(monkeypatch):
        created = _resolve(app_module.create_global_term(app_module.GlobalTermRequest(
            term="Zorvo", type="brand", treatment="keep", targets={"hi-IN": "Zorvo"}, locked=True, note="Product name",
        )))
        term_id = created["id"]
        listed = _resolve(app_module.list_global_terms(q="Zorvo"))
        assert listed["count"] == 1
        assert listed["terms"][0]["locked"] is True
        updated = _resolve(app_module.update_global_term(term_id, app_module.GlobalTermRequest(
            term="Zorvo", type="brand", treatment="transliterate", targets={"hi-IN": "ज़ोर्वो"}, locked=True, note="Approved Hindi form",
        )))
        assert updated["targets"]["hi-IN"] == "ज़ोर्वो"
        assert _resolve(app_module.delete_global_term(term_id))["deleted"] is True
        assert _resolve(app_module.list_global_terms())["count"] == 0


def test_project_summary_export_preflight_and_report_download(monkeypatch):
    with _sandbox(monkeypatch) as (_, data_root):
        created = _resolve(app_module.create_project(app_module.ProjectCreate(
            name="Local test project", source_lang="en-IN", target_langs=["hi-IN"],
        )))
        project_id = created["id"]
        summary = _resolve(app_module.project_summary(project_id))
        assert summary["clips"] == {"total": 0, "dub_rows": 0, "green": 0, "yellow": 0, "red": 0}
        dashboard = _resolve(app_module.projects_summary())
        assert dashboard["project_count"] == 1
        assert dashboard["clip_count"] == 0
        preflight = _resolve(app_module.export_preflight(project_id, app_module.ExportRequest(lang="hi-IN")))
        assert preflight["passed"] is False
        assert any(not check["passed"] for check in preflight["checks"])

        export_dir = data_root / "exports" / project_id
        export_dir.mkdir(parents=True)
        report_path = export_dir / "local_hi-IN_phase3d_ai-dubbed.json"
        report_path.write_text(json.dumps({"language": "hi-IN", "verification": "standard", "clips": []}), encoding="utf-8")
        with db.connect() as connection:
            connection.execute(
                "INSERT INTO export_events(id,project_id,lang,formats_json,unresolved_red_json) VALUES(?,?,?,?,?)",
                ("export-test", project_id, "hi-IN", '["mp4","report"]', '[2]'),
            )
        reports = _resolve(app_module.list_reports())
        assert reports["count"] == 1
        assert reports["reports"][0]["project_id"] == project_id
        assert reports["reports"][0]["exported_with_unresolved_red"] is True
        report_payload = _resolve(app_module.project_report(project_id, "hi-IN"))
        assert report_payload["language"] == "hi-IN"
        assert report_payload["export_history"][0]["unresolved_red_clips"] == [2]
        readable = _resolve(app_module.readable_project_report(project_id, "hi-IN"))
        assert "Local test project" in readable.body.decode()
        download = _resolve(app_module.download_project_artifact(project_id, "report", "hi-IN"))
        assert json.loads(download.body)["export_history"][0]["exported_with_unresolved_red"] is True
        assert json.loads(report_path.read_text(encoding="utf-8"))["language"] == "hi-IN"


def test_clip_api_surfaces_raw_transcript_and_prisma_disagreements(monkeypatch):
    with _sandbox(monkeypatch) as (_, data_root):
        project = _resolve(app_module.create_project(app_module.ProjectCreate(
            name="Transcript review", source_lang="en-IN", target_langs=["hi-IN"],
        )))
        with db.connect() as connection:
            connection.execute(
                "INSERT INTO clips(id,project_id,idx,start_s,end_s,src_text) VALUES(?,?,?,?,?,?)",
                ("clip-review", project["id"], 0, 0, 4, "Cleaned source"),
            )
            connection.execute(
                "INSERT INTO dubs(id,clip_id,lang,translation,draft_translation,status) VALUES(?,?,?,?,?,?)",
                ("dub-review", "clip-review", "hi-IN", "अनुवाद", "अनुवाद", "yellow"),
            )
            connection.execute(
                "UPDATE dubs SET audio_path=?,reasons_json=? WHERE id=?",
                (str(data_root / "exports" / "expired.wav"), json.dumps(["Dub speech still exceeds its safe slot; withheld."]), "dub-review"),
            )
        export_dir = data_root / "exports" / project["id"]
        export_dir.mkdir(parents=True)
        (export_dir / "review_hi-IN_phase3d.json").write_text(json.dumps({"clips": [{
            "clip": 1,
            "source_raw": "Raw Prisma transcript",
            "stt_review": {"disagreements": [{"variant": "prisma_v2_5", "alternative_transcript": "Alternate transcript", "word_differences": [{"left": "Raw", "right": "Alternate"}]}]},
        }]}), encoding="utf-8")

        clips = _resolve(app_module.get_clips(project["id"], "hi-IN"))
        assert clips[0]["src_text"] == "Cleaned source"
        assert clips[0]["source_raw"] == "Raw Prisma transcript"
        assert clips[0]["stt_review"]["disagreements"][0]["alternative_transcript"] == "Alternate transcript"
        assert clips[0]["audio_url"] is None
        assert clips[0]["audio_removed"] is True
        assert clips[0]["reason_code"] == "too_long_for_slot"
        assert clips[0]["reason_text"] == "Speech is too long for its time slot."
        assert clips[0]["fix_action"] == "Shorten the translation"


def test_source_fix_updates_saved_clip_and_report_for_regeneration(monkeypatch):
    with _sandbox(monkeypatch) as (_, data_root):
        project = _resolve(app_module.create_project(app_module.ProjectCreate(
            name="Source correction", source_lang="en-IN", target_langs=["hi-IN"],
        )))
        with db.connect() as connection:
            connection.execute(
                "INSERT INTO clips(id,project_id,idx,start_s,end_s,src_text) VALUES(?,?,?,?,?,?)",
                ("clip-source-edit", project["id"], 0, 0, 4, "a head"),
            )
            connection.execute(
                "INSERT INTO dubs(id,clip_id,lang,translation,draft_translation,status) VALUES(?,?,?,?,?,?)",
                ("dub-source-edit", "clip-source-edit", "hi-IN", "एक सिर", "एक सिर", "green"),
            )
        export_dir = data_root / "exports" / project["id"]
        export_dir.mkdir(parents=True)
        report_path = export_dir / "review_hi-IN_phase3d_ai-dubbed.json"
        report_path.write_text(json.dumps({"clips": [{"clip": 1, "source_cleaned": "a head"}]}), encoding="utf-8")
        response = _resolve(app_module.edit_clip_source("clip-source-edit", app_module.SourceEdit(source_text="Anek")))
        assert response["status"] == "updated"
        with db.connect() as connection:
            clip = connection.execute("SELECT src_text FROM clips WHERE id=?", ("clip-source-edit",)).fetchone()
            dub = connection.execute("SELECT status,reasons_json FROM dubs WHERE id=?", ("dub-source-edit",)).fetchone()
        assert clip["src_text"] == "Anek"
        assert dub["status"] == "yellow"
        assert "re-speak" in dub["reasons_json"]
        assert json.loads(report_path.read_text(encoding="utf-8"))["clips"][0]["source_cleaned"] == "Anek"


def test_audio_tracks_report_when_saved_track_files_are_missing(monkeypatch):
    with _sandbox(monkeypatch) as (_, data_root):
        project = _resolve(app_module.create_project(app_module.ProjectCreate(
            name="Missing track", source_lang="en-IN", target_langs=["hi-IN"],
        )))
        export_dir = data_root / "exports" / project["id"]
        export_dir.mkdir(parents=True)
        (export_dir / "review_hi-IN_phase3d_ai-dubbed.json").write_text(
            json.dumps({"audio_tracks": {"dubbed": "missing.wav", "source": "source.wav"}}), encoding="utf-8",
        )
        tracks = _resolve(app_module.project_audio_tracks(project["id"], "hi-IN"))
        assert tracks["audio_removed"] is True
        assert tracks["tracks"]["dubbed"] is None


def test_voice_list_is_redacted_and_project_audio_delete_preserves_text(monkeypatch):
    with _sandbox(monkeypatch) as (_, data_root):
        project = _resolve(app_module.create_project(app_module.ProjectCreate(
            name="Audio deletion test", source_lang="en-IN", target_langs=["hi-IN"],
        )))
        project_id = project["id"]
        upload_dir = data_root / "uploads" / project_id
        upload_dir.mkdir(parents=True)
        source_video = upload_dir / "source.mp4"
        source_video.write_bytes(b"local test video bytes")
        browser_copy = upload_dir / "browser.mp4"
        browser_copy.write_bytes(b"browser preview bytes")
        export_dir = data_root / "exports" / project_id
        export_dir.mkdir(parents=True)
        muxed = export_dir / "sample_hi-IN_ai-dubbed.mp4"
        wav = export_dir / "sample_hi-IN_ai-dubbed.wav"
        srt = export_dir / "sample_hi-IN_ai-dubbed.srt"
        report = export_dir / "sample_hi-IN_ai-dubbed.json"
        for path in (muxed, wav, srt, report):
            path.write_bytes(b"test content")
        reference = data_root / "voices" / "voice.wav"
        reference.parent.mkdir(parents=True)
        reference.write_bytes(b"voice bytes")
        with db.connect() as connection:
            connection.execute("UPDATE projects SET video_path=?,status='complete' WHERE id=?", (str(source_video), project_id))
            connection.execute(
                "INSERT INTO voices(id,session_id,label,embedding_json,consent_phrase,consent_ok,reference_path) VALUES(?,?,?,?,?,1,?)",
                ("voice-test", "session", "My voice", '{"secret":true}', "consented", str(reference)),
            )
            connection.execute("INSERT INTO clips(id,project_id,idx,start_s,end_s,src_text) VALUES(?,?,?,?,?,?)",
                               ("clip-test", project_id, 0, 0, 1, "Source transcript retained"))
            connection.execute("INSERT INTO dubs(id,clip_id,lang,translation,draft_translation,status,audio_path) VALUES(?,?,?,?,?,?,?)",
                               ("dub-test", "clip-test", "hi-IN", "अनुवाद सुरक्षित", "अनुवाद सुरक्षित", "green", str(wav)))
        voices = _resolve(app_module.list_voices())
        assert voices == [{"id": "voice-test", "label": "My voice", "consent_confirmed": True,
                           "has_reference": True, "created_at": voices[0]["created_at"]}]
        assert "embedding_json" not in voices[0] and "reference_path" not in voices[0]
        result = _resolve(app_module.delete_project_audio(project_id))
        assert result["source_video_removed"] is True
        assert not source_video.exists() and not browser_copy.exists() and not muxed.exists() and not wav.exists()
        assert srt.exists() and report.exists() and reference.exists()
        with db.connect() as connection:
            project_row = connection.execute("SELECT video_path,status FROM projects WHERE id=?", (project_id,)).fetchone()
            clip_row = connection.execute("SELECT src_text,src_audio_path FROM clips WHERE id='clip-test'").fetchone()
            dub_row = connection.execute("SELECT translation,audio_path FROM dubs WHERE id='dub-test'").fetchone()
        assert tuple(project_row) == (None, "audio_deleted")
        assert tuple(clip_row) == ("Source transcript retained", None)
        assert tuple(dub_row) == ("अनुवाद सुरक्षित", None)


def test_delete_all_user_data_clears_files_and_database(monkeypatch):
    with _sandbox(monkeypatch) as (_, data_root):
        project = _resolve(app_module.create_project(app_module.ProjectCreate(
            name="Disposable privacy test", source_lang="en-IN", target_langs=["hi-IN"],
        )))
        for folder in ("uploads", "exports", "voices"):
            path = data_root / folder / "private-test.txt"
            path.parent.mkdir(parents=True, exist_ok=True)
            path.write_text("disposable test data", encoding="utf-8")

        result = _resolve(app_module.delete_all_user_data())

        assert result == {"deleted": True}
        assert all(not (data_root / folder).exists() for folder in ("uploads", "exports", "voices"))
        with db.connect() as connection:
            assert connection.execute("SELECT COUNT(*) FROM projects WHERE id=?", (project["id"],)).fetchone()[0] == 0


def _project_with_review_audio(data_root, *, overlap=False):
    project = _resolve(app_module.create_project(app_module.ProjectCreate(
        name="Review clip", source_lang="en-IN", target_langs=["hi-IN"],
    )))
    project_id = project["id"]
    with db.connect() as connection:
        connection.execute("UPDATE projects SET status='complete',duration_s=10 WHERE id=?", (project_id,))
        connection.execute("INSERT INTO clips(id,project_id,idx,start_s,end_s,src_text) VALUES(?,?,?,?,?,?)",
                           ("review-clip-1", project_id, 0, 0, 2, "source"))
        connection.execute("INSERT INTO dubs(id,clip_id,lang,translation,status,audio_path,duration_s) VALUES(?,?,?,?,?,?,?)",
                           ("review-dub-1", "review-clip-1", "hi-IN", "अनुवाद", "yellow",
                            str(data_root / "exports" / project_id / "clips" / "clip_001.wav"), 1.5))
        if overlap:
            connection.execute("INSERT INTO clips(id,project_id,idx,start_s,end_s,src_text) VALUES(?,?,?,?,?,?)",
                               ("review-clip-2", project_id, 1, 1, 3, "next"))
            connection.execute("INSERT INTO dubs(id,clip_id,lang,translation,status,audio_path,duration_s) VALUES(?,?,?,?,?,?,?)",
                               ("review-dub-2", "review-clip-2", "hi-IN", "अगला", "green",
                                str(data_root / "exports" / project_id / "clips" / "clip_002.wav"), 1.5))
    for index in (1, 2) if overlap else (1,):
        path = data_root / "exports" / project_id / "clips" / f"clip_{index:03d}.wav"
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_bytes(b"test audio")
    return project


def test_user_can_verify_a_saved_audio_take(monkeypatch):
    with _sandbox(monkeypatch) as (_, data_root):
        _project_with_review_audio(data_root)
        result = _resolve(app_module.verify_dub_clip("review-clip-1", "hi-IN", app_module.VerifyDecision(confirm_override=True)))
        assert result["human_verified"] is True
        with db.connect() as connection:
            row = connection.execute("SELECT status,reasons_json FROM dubs WHERE id='review-dub-1'").fetchone()
        assert row["status"] == "yellow"
        assert json.loads(row["reasons_json"]) == []


def test_human_verification_updates_clip_and_project_counts_and_can_be_undone(monkeypatch):
    with _sandbox(monkeypatch) as (_, data_root):
        project = _project_with_review_audio(data_root)
        result = _resolve(app_module.verify_dub_clip("review-clip-1", "hi-IN", app_module.VerifyDecision(confirm_override=True)))
        assert result["human_verified"] is True
        clips = _resolve(app_module.get_clips(project["id"], "hi-IN"))
        assert clips[0]["status"] == "green"
        assert clips[0]["user_verified"] is True
        summary = _resolve(app_module.project_summary(project["id"]))
        assert summary["clips"]["green"] == 1
        undone = _resolve(app_module.unverify_dub_clip("review-clip-1", "hi-IN"))
        assert undone["human_verified"] is False
        clips = _resolve(app_module.get_clips(project["id"], "hi-IN"))
        assert clips[0]["status"] == "yellow"
        assert clips[0]["user_verified"] is False


def test_verification_override_requires_confirmation_and_is_persisted(monkeypatch):
    with _sandbox(monkeypatch) as (_, data_root):
        _project_with_review_audio(data_root)
        with db.connect() as connection:
            connection.execute("UPDATE dubs SET reasons_json=? WHERE id=?", (json.dumps(["name mismatch"]), "review-dub-1"))
        with pytest.raises(app_module.HTTPException) as exc:
            _resolve(app_module.verify_dub_clip("review-clip-1", "hi-IN"))
        assert exc.value.status_code == 409
        assert exc.value.detail["confirm_override"] is True
        result = _resolve(app_module.verify_dub_clip("review-clip-1", "hi-IN", app_module.VerifyDecision(confirm_override=True)))
        assert result["verify_override"] is True
        with db.connect() as connection:
            row = connection.execute("SELECT user_verified,verify_override FROM dubs WHERE id='review-dub-1'").fetchone()
        assert tuple(row) == (1, 1)


def test_balance_endpoint_persists_language_specific_placements(monkeypatch):
    with _sandbox(monkeypatch) as (_, data_root):
        project = _project_with_review_audio(data_root)
        with db.connect() as connection:
            connection.execute("UPDATE dubs SET timing_offset_s=0.2 WHERE id='review-dub-1'")
        rendered = []
        monkeypatch.setattr(app_module, "_render_scheduled_track", lambda project_id, lang, placements, duration: rendered.append((project_id, lang, placements)) or data_root / "track.wav")
        result = _resolve(app_module.balance_project_timing(project["id"], app_module.BalanceRequest(lang="hi-IN")))
        assert result["valid"] is True
        assert result["placements"][0]["start_s"] == pytest.approx(0.2)
        assert rendered and rendered[0][1] == "hi-IN"
        with db.connect() as connection:
            offset = connection.execute("SELECT timing_offset_s FROM dubs WHERE id='review-dub-1'").fetchone()[0]
        assert offset == pytest.approx(0.2)


def test_user_verification_rejects_overlapping_speech(monkeypatch):
    with _sandbox(monkeypatch) as (_, data_root):
        _project_with_review_audio(data_root, overlap=True)
        with pytest.raises(app_module.HTTPException, match="overlaps"):
            _resolve(app_module.verify_dub_clip("review-clip-1", "hi-IN"))


def test_edit_length_is_advisory_until_real_speech_duration_is_measured(monkeypatch):
    with _sandbox(monkeypatch) as (_, data_root):
        project = _project_with_review_audio(data_root)
        text = "कौन सा head cover उन पर better लगेगा? इसलिए मैंने Ahead बनाया।"
        assert len(text) > 32
        result = _resolve(app_module.edit_dub("review-clip-1", "hi-IN", app_module.DubEdit(translation=text)))
        assert result["status"] == "yellow"
        assert not any("over_slot" in reason for reason in result["reasons"])
        with db.connect() as connection:
            row = connection.execute("SELECT translation,audio_path,audio_stale FROM dubs WHERE id='review-dub-1'").fetchone()
        assert row["translation"] == text
        assert row["audio_path"] is not None
        assert row["audio_stale"] == 1
