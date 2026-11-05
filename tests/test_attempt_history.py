import json
from pathlib import Path

from backend import app as app_module
from backend import db
from tests.test_api_destinations import _resolve, _sandbox


def test_attempt_history_returns_previous_and_updated_audio(monkeypatch):
    with _sandbox(monkeypatch) as (_, data_root):
        project = _resolve(app_module.create_project(app_module.ProjectCreate(
            name="Attempt history", source_lang="en-IN", target_langs=["hi-IN"],
        )))
        clip_id = "clip-attempt-history"
        export_dir = data_root / "exports" / project["id"]
        (export_dir / "clips" / "hi-IN" / "attempts").mkdir(parents=True)
        previous = "clips/hi-IN/attempts/previous.wav"
        updated = "clips/hi-IN/attempts/updated.wav"
        (export_dir / previous).write_bytes(b"previous audio")
        (export_dir / updated).write_bytes(b"updated audio")
        attempts = [{
            "trigger": "shorter translation retry",
            "old_text": "Long previous wording",
            "new_text": "Shorter wording",
            "old_audio_key": previous,
            "new_audio_key": updated,
            "change": "Translation and audio updated",
        }]
        with db.connect() as connection:
            connection.execute(
                "INSERT INTO clips(id,project_id,idx,start_s,end_s,src_text) VALUES(?,?,?,?,?,?)",
                (clip_id, project["id"], 0, 0, 2, "Source words."),
            )
            connection.execute(
                "INSERT INTO dubs(id,clip_id,lang,translation,attempts_json) VALUES(?,?,?,?,?)",
                ("dub-attempt-history", clip_id, "hi-IN", "Shorter wording", json.dumps(attempts)),
            )

        clips = _resolve(app_module.get_clips(project["id"], "hi-IN"))
        attempt = clips[0]["attempts"][0]
        assert attempt["old_text"] == "Long previous wording"
        assert attempt["new_text"] == "Shorter wording"
        assert attempt["old_audio_url"].endswith("/0/previous")
        assert attempt["new_audio_url"].endswith("/0/updated")
        assert "old_audio_key" not in attempt and "new_audio_key" not in attempt
        assert Path(_resolve(app_module.clip_attempt_audio(clip_id, "hi-IN", 0, "previous")).path).read_bytes() == b"previous audio"
        assert Path(_resolve(app_module.clip_attempt_audio(clip_id, "hi-IN", 0, "updated")).path).read_bytes() == b"updated audio"
