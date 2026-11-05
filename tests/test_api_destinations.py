import json
import sqlite3
import tempfile
from contextlib import closing, contextmanager
from pathlib import Path

from backend import db
from backend import app as app_module


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


def test_video_upload_keeps_original_and_preview_conversion_is_deferred(monkeypatch):
    with _sandbox(monkeypatch) as (_, data_root):
        project = _resolve(app_module.create_project(app_module.ProjectCreate(
            name="Playable video", source_lang="en-IN", target_langs=["hi-IN"],
        )))
        browser_copy = data_root / "uploads" / project["id"] / "browser.mp4"
        browser_copy.parent.mkdir(parents=True, exist_ok=True)
        browser_copy.write_bytes(b"h264 preview")
        monkeypatch.setattr(app_module, "probe_video", lambda path: (20.0, True))
        conversions = []
        monkeypatch.setattr(app_module, "browser_video_path", lambda path: conversions.append(path) or browser_copy)
        class Upload:
            filename = "source.mov"
            blocks = [b"original hevc bytes", b""]

            async def read(self, size):
                return self.blocks.pop(0)

        upload = Upload()

        result = _resolve(app_module.upload_video(project["id"], upload))
        assert conversions == []
        assert Path(project_row := app_module.project_row(project["id"])["video_path"]).read_bytes() == b"original hevc bytes"
        response = _resolve(app_module.project_video(project["id"]))

        assert result["filename"] == "source.mov"
        assert (browser_copy.parent / "source.mov").read_bytes() == b"original hevc bytes"
        assert Path(response.path) == browser_copy
        assert response.media_type == "video/mp4"
        assert "attachment" not in response.headers.get("content-disposition", "")
        assert conversions == [Path(project_row)]


def test_browser_preview_conversion_bounds_portrait_resolution_and_time(monkeypatch):
    with _sandbox(monkeypatch) as (_, data_root):
        data_root.mkdir(parents=True, exist_ok=True)
        source = data_root / "portrait.mov"
        source.write_bytes(b"video")
        calls = []

        class Completed:
            stdout = json.dumps({"streams": [
                {"codec_type": "video", "codec_name": "hevc"},
                {"codec_type": "audio", "codec_name": "aac"},
            ]})

        def fake_run(command, **kwargs):
            calls.append((command, kwargs))
            if command[0] == "ffmpeg":
                Path(command[-1]).write_bytes(b"browser preview")
            return Completed()

        monkeypatch.setattr(app_module.subprocess, "run", fake_run)

        result = app_module.browser_video_path(source)

        assert result.read_bytes() == b"browser preview"
        ffmpeg_command, options = calls[-1]
        scale = ffmpeg_command[ffmpeg_command.index("-vf") + 1]
        assert "min(720,iw)" in scale and "min(720,ih)" in scale
        assert "fps=24" in scale
        assert "force_original_aspect_ratio=decrease" in scale
        assert "-threads:v" in ffmpeg_command
        assert options["timeout"] == 120


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


def test_delete_all_user_data_clears_files_database_and_llm_cache(monkeypatch):
    with _sandbox(monkeypatch) as (_, data_root):
        project = _resolve(app_module.create_project(app_module.ProjectCreate(
            name="Disposable privacy test", source_lang="en-IN", target_langs=["hi-IN"],
        )))
        for folder in ("uploads", "exports", "voices"):
            path = data_root / folder / "private-test.txt"
            path.parent.mkdir(parents=True, exist_ok=True)
            path.write_text("disposable test data", encoding="utf-8")

        cache_path = data_root / "llm_response_cache.sqlite3"
        with closing(sqlite3.connect(cache_path)) as connection, connection:
            connection.execute("CREATE TABLE llm_responses (cache_key TEXT PRIMARY KEY, response_json TEXT NOT NULL, created_at REAL NOT NULL)")
            connection.execute("INSERT INTO llm_responses VALUES(?,?,?)", ("key", '{"translation":"private test phrase"}', 1.0))
        monkeypatch.setattr(app_module, "settings", app_module.settings.model_copy(update={
            "anek_data_dir": str(data_root), "llm_cache_path": str(cache_path),
        }))

        result = _resolve(app_module.delete_all_user_data())

        assert result == {"deleted": True, "llm_response_cache_cleared": True}
        assert all(not (data_root / folder).exists() for folder in ("uploads", "exports", "voices"))
        with db.connect() as connection:
            assert connection.execute("SELECT COUNT(*) FROM projects WHERE id=?", (project["id"],)).fetchone()[0] == 0
        with closing(sqlite3.connect(cache_path)) as connection:
            assert connection.execute("SELECT COUNT(*) FROM llm_responses").fetchone()[0] == 0
