import json

import pytest

from backend.brain.checks import latin_letter_share
from backend.brain.numbers import compare_numbers
from backend.clients.llm import (ProviderError, SarvamTranslationClient, SharedSarvamLimiter,
    BadInputError, _provider_call, _split_at_character_limit)
from backend.config import LANGUAGES
from backend.translation_style import eligible_style_switch_rows, style_switch_eligible


def _client(model="mayura:v1"):
    client = SarvamTranslationClient.__new__(SarvamTranslationClient)
    client.translation_model = model
    client.rewrite_model = "sarvam-105b-conversations"
    client.model = model
    client.models_used = []
    return client


@pytest.mark.parametrize("target", list(LANGUAGES))
@pytest.mark.parametrize(("style", "mode", "script"), [
    ("natural", "code-mixed", None),
    ("pure", "formal", "fully-native"),
])
def test_translation_style_parameters_for_all_ten_targets(monkeypatch, target, style, mode, script):
    calls = []

    class Text:
        def translate(self, **kwargs):
            calls.append(kwargs)
            return type("Result", (), {"translated_text": "ಅನುವಾದ"})()

    monkeypatch.setattr(SharedSarvamLimiter, "call", classmethod(lambda cls, operation, **kwargs: operation()))
    instance = _client()
    instance.client = type("Sarvam", (), {"text": Text()})()
    instance.chat_json([
        {"role": "system", "content": "Translate the full source paragraph"},
        {"role": "user", "content": json.dumps({
            "source_language": "auto", "target_language": target, "translation_style": style,
            "timed_source_spans": [{"source_text": "We studied 2 things. Asia is a large continent."}],
        })},
    ])
    assert calls[0]["model"] == "mayura:v1"
    assert calls[0]["mode"] == mode
    assert calls[0]["input"] == "We studied 2 things. Asia is a large continent."
    assert "ZXQ" not in calls[0]["input"]
    if target == "en-IN" or style == "natural":
        assert "output_script" not in calls[0]
    else:
        assert calls[0]["output_script"] == script


def test_long_clip_input_is_split_at_one_thousand_characters(monkeypatch):
    calls = []

    class Text:
        def translate(self, **kwargs):
            calls.append(kwargs)
            return type("Result", (), {"translated_text": "क"})()

    monkeypatch.setattr(SharedSarvamLimiter, "call", classmethod(lambda cls, operation, **kwargs: operation()))
    instance = _client()
    instance.client = type("Sarvam", (), {"text": Text()})()
    text = ("word " * 250).strip()
    instance.chat_json([
        {"role": "system", "content": "Translate the full source paragraph"},
        {"role": "user", "content": json.dumps({"source_language": "en-IN", "target_language": "hi-IN", "timed_source_spans": [{"source_text": text}]})},
    ])
    assert len(calls) > 1
    assert all(len(call["input"]) <= 1000 for call in calls)
    assert "".join(call["input"].replace(" ", "") for call in calls) == text.replace(" ", "")


def test_mayura_failure_uses_formal_sarvam_translate_fallback(monkeypatch):
    calls = []

    class Text:
        def translate(self, **kwargs):
            calls.append(kwargs)
            if kwargs["model"] == "mayura:v1":
                raise RuntimeError("provider unavailable")
            return type("Result", (), {"translated_text": "अनुवाद"})()

    monkeypatch.setattr(SharedSarvamLimiter, "call", classmethod(lambda cls, operation, **kwargs: operation()))
    instance = _client()
    instance.client = type("Sarvam", (), {"text": Text()})()
    instance.chat_json([
        {"role": "system", "content": "Translate the full source paragraph"},
        {"role": "user", "content": json.dumps({"source_language": "en-IN", "target_language": "hi-IN", "timed_source_spans": [{"source_text": "Hello"}]})},
    ])
    assert calls[0]["model"] == "mayura:v1"
    assert calls[1]["model"] == "sarvam-translate:v1"
    assert calls[1]["mode"] == "formal"
    assert "output_script" not in calls[1]
    assert instance.models_used == ["sarvam-translate:v1"]


def test_natural_latin_share_ignores_numbers_and_protected_terms():
    sheet = {"terms": [{"term": "Anek", "type": "brand", "treatment": "keep", "targets": {}, "spoken_forms": {}, "locked": True}]}
    assert latin_letter_share("अनुवाद Anek १२३", "hi-IN", sheet) == 0
    assert latin_letter_share("अनुवाद location", "hi-IN") > 0.05
    assert latin_letter_share("English", "en-IN") == 0


@pytest.mark.parametrize("translation", ["हमने दो चीज़ें पढ़ीं।", "ನಾವು ಎರಡು ವಿಷಯಗಳನ್ನು ಅಧ್ಯಯನ ಮಾಡಿದ್ದೇವೆ."])
def test_clip_two_number_words_are_valid_in_target_scripts(translation):
    source = "So we have studied two things. India is located on the northeastern hemisphere, along with it,"
    assert compare_numbers(source, translation) == []


@pytest.mark.parametrize("translation", ["हमने २ चीज़ें पढ़ीं।", "ನಾವು ೨ ವಿಷಯಗಳನ್ನು ಅಧ್ಯಯನ ಮಾಡಿದ್ದೇವೆ."])
def test_number_check_accepts_native_numerals(translation):
    assert compare_numbers("We studied 2 things.", translation) == []


def test_style_switch_lock_predicate_preserves_edited_locked_and_verified_rows():
    assert style_switch_eligible({"human_edited": 0, "timing_locked": 0, "user_verified": 0})
    assert not style_switch_eligible({"human_edited": 1, "timing_locked": 0, "user_verified": 0})
    assert not style_switch_eligible({"human_edited": 0, "timing_locked": 1, "user_verified": 0})
    assert not style_switch_eligible({"human_edited": 0, "timing_locked": 0, "user_verified": 1})


def test_style_switch_selection_never_includes_verified_clips():
    rows = [
        {"clip": 1, "human_edited": 0, "timing_locked": 0, "user_verified": 0},
        {"clip": 2, "human_edited": 0, "timing_locked": 0, "user_verified": 1},
        {"clip": 3, "human_edited": 1, "timing_locked": 0, "user_verified": 0},
    ]
    assert [row["clip"] for row in eligible_style_switch_rows(rows)] == [1]


def test_clip_splitter_keeps_parts_within_limit_and_preserves_content():
    source = "x" * 1001
    parts = _split_at_character_limit(source, 1000)
    assert all(len(part) <= 1000 for part in parts)
    assert "".join(parts) == source


def test_sarvam_http_400_keeps_redacted_provider_reason_in_log_and_error(monkeypatch, caplog):
    class Response:
        status_code = 400
        text = '{"error":"invalid target_language_code","api_key":"do-not-log"}'

    class ProviderFailure(Exception):
        status_code = 400
        response = Response()

    monkeypatch.setattr(
        SharedSarvamLimiter, "call",
        classmethod(lambda cls, operation, **kwargs: operation()),
    )
    with pytest.raises(BadInputError, match="invalid target_language_code"):
        _provider_call(
            lambda: (_ for _ in ()).throw(ProviderFailure()), attempts=1,
            context={"model": "mayura:v1", "source_language_code": "en-IN",
                     "target_language_code": "hi-IN", "mode": "code-mixed"},
        )
    assert "path=/text/translate" in caplog.text
    assert "target_language=hi-IN" in caplog.text
    assert "do-not-log" not in caplog.text


def test_numeric_values_are_sent_as_words_not_pronounceable_placeholders(monkeypatch):
    calls = []

    class Text:
        def translate(self, **kwargs):
            calls.append(kwargs["input"])
            return type("Result", (), {"translated_text": "ನಾವು ಎರಡು ವಿಷಯಗಳನ್ನು ಅಧ್ಯಯನ ಮಾಡಿದ್ದೇವೆ."})()

    monkeypatch.setattr(SharedSarvamLimiter, "call", classmethod(lambda cls, operation, **kwargs: operation()))
    instance = _client()
    instance.client = type("Sarvam", (), {"text": Text()})()
    source = "We studied 2 things."
    result = instance.chat_json([
        {"role": "system", "content": "Translate the full source paragraph"},
        {"role": "user", "content": json.dumps({"source_language": "en-IN", "target_language": "hi-IN", "timed_source_spans": [{"source_text": source}]})},
    ])
    translated = result["candidates"][0]["translations"][0]
    assert len(calls) == 1
    assert calls == [source]
    assert all(token not in translated.casefold() for token in ("zxq", "x cube"))
    assert compare_numbers(source, translated) == []
