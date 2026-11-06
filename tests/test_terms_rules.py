from backend import app as app_module
from backend import db
from backend.brain.termsheet import (
    TermSheet,
    apply_mishearing_maps,
    apply_spoken_forms,
    mask_protected_terms,
    restore_protected_terms,
)
from tests.test_api_destinations import _resolve, _sandbox


def test_mishearing_map_rewrites_heard_variants_to_canonical_term():
    sheet = TermSheet.model_validate({"terms": [{
        "term": "Anek", "type": "brand", "treatment": "keep",
        "rule_type": "mishearing", "heard_variants": ["a head", "one head", "ek sir"],
    }]})
    assert apply_mishearing_maps("I heard a head and ek sir", sheet) == "I heard Anek and Anek"


def test_fix_everywhere_rule_can_correct_a_phrase_and_protect_the_canonical_term():
    sheet = TermSheet.model_validate({"terms": [{
        "term": "Anek", "type": "brand", "treatment": "keep", "locked": True,
        "rule_type": "mishearing", "heard_variants": ["built a head", "built ahead", "a head"],
    }]})
    corrected = apply_mishearing_maps("I built ahead and saw a head", sheet)
    masked, placeholders = mask_protected_terms(corrected, sheet)
    restored, missing = restore_protected_terms(masked, placeholders)
    assert corrected == "I Anek and saw Anek"
    assert "Anek" not in masked
    assert restored == corrected
    assert missing == []


def test_protected_placeholder_round_trip_and_missing_term_detection():
    sheet = TermSheet.model_validate({"terms": [{
        "term": "Anek", "type": "brand", "treatment": "keep", "locked": True,
        "rule_type": "protected",
    }]})
    masked, placeholders = mask_protected_terms("Welcome to Anek", sheet)
    restored, missing = restore_protected_terms(masked, placeholders)
    assert "Anek" not in masked
    assert restored == "Welcome to Anek"
    assert missing == []
    _, missing = restore_protected_terms("Welcome", placeholders)
    assert missing == ["Anek"]


def test_per_language_spoken_spelling_is_used_for_synthesis():
    sheet = TermSheet.model_validate({"terms": [{
        "term": "Anek", "type": "brand", "treatment": "keep", "locked": True,
        "rule_type": "protected", "spoken_forms": {"hi-IN": "अनेक"},
    }]})
    assert apply_spoken_forms("Welcome to Anek", sheet, "hi-IN") == "Welcome to अनेक"


def test_edited_translation_is_locked_against_generated_overwrite(monkeypatch):
    with _sandbox(monkeypatch):
        project = _resolve(app_module.create_project(app_module.ProjectCreate(
            name="Locked edit", source_lang="en-IN", target_langs=["hi-IN"],
        )))
        clip_id = "clip-locked-edit"
        with db.connect() as connection:
            connection.execute(
                "INSERT INTO clips(id,project_id,idx,start_s,end_s,src_text) VALUES(?,?,?,?,?,?)",
                (clip_id, project["id"], 0, 0, 2, "A source sentence."),
            )
            connection.execute(
                "INSERT INTO dubs(id,clip_id,lang,translation,draft_translation,status) VALUES(?,?,?,?,?,?)",
                ("dub-locked-edit", clip_id, "hi-IN", "AI draft", "AI draft", "yellow"),
            )
        _resolve(app_module.edit_dub(clip_id, "hi-IN", app_module.DubEdit(translation="Human-edited text")))
        with db.connect() as connection:
            app_module.persist_generated_dub(connection, clip_id, "hi-IN", {
                "translation": "New machine text", "status": "green", "reasons": [],
                "translation_synthesis_attempts": [], "audio_path": "new.wav", "final_audio_duration_s": 1.5,
            })
            saved = connection.execute("SELECT translation,draft_translation,human_edited FROM dubs WHERE clip_id=?", (clip_id,)).fetchone()
        assert saved["translation"] == "Human-edited text"
        assert saved["draft_translation"] == "AI draft"
        assert saved["human_edited"] == 1


def test_project_term_rule_is_scoped_until_explicitly_saved_globally(monkeypatch):
    with _sandbox(monkeypatch):
        project = _resolve(app_module.create_project(app_module.ProjectCreate(
            name="Scoped terms", source_lang="en-IN", target_langs=["hi-IN"],
        )))
        rule = {
            "term": "Anek", "type": "brand", "treatment": "keep", "locked": True,
            "rule_type": "mishearing", "heard_variants": ["a head"],
        }
        _resolve(app_module.approve_terms(project["id"], app_module.TermsUpdate(terms=[rule])))
        assert _resolve(app_module.get_termsheet(project["id"]))["terms"][0]["heard_variants"] == ["a head"]
        assert _resolve(app_module.list_global_terms())["count"] == 0
        rule["save_global"] = True
        _resolve(app_module.approve_terms(project["id"], app_module.TermsUpdate(terms=[rule])))
        assert _resolve(app_module.list_global_terms())["terms"][0]["rule_type"] == "mishearing"
