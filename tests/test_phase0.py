import pytest

from backend.clients.gnani import MockGnaniClient, create_gnani_client
from backend.clients.llm import MockLLMClient, create_llm_client
from backend.config import LANGUAGES, Settings
from scripts.cloning_gate import TEST_SENTENCES, extract_numbers
from backend.brain.autocorrect import combine_audio_verification, translate_with_autocorrect
from backend.brain.checks import run_checks
from backend.brain.numbers import compare_numbers, extract_numbers as brain_numbers
from backend.brain.termsheet import TermSheet
from backend.verify.flags import audio_number_status
from backend.verify.report import build_report


def test_supported_language_table_is_exact():
    assert len(LANGUAGES) == 10
    assert "or-IN" not in LANGUAGES
    assert "as-IN" not in LANGUAGES


def test_settings_enforce_video_hard_cap():
    assert Settings(max_video_seconds=120).max_video_seconds == 120
    with pytest.raises(ValueError):
        Settings(max_video_seconds=121)


def test_cloning_gate_covers_each_supported_language_and_local_numerals():
    assert set(TEST_SENTENCES) == set(LANGUAGES)
    assert extract_numbers("क़ीमत ४७ रुपये, साल ২০২৬") == ["47", "2026"]


def test_mock_clients_are_selectable_and_never_call_live_services():
    gnani = create_gnani_client(mock=True)
    llm = create_llm_client(mock=True)
    assert isinstance(gnani, MockGnaniClient)
    assert isinstance(llm, MockLLMClient)
    transcript = gnani.stt(b"fake", "hi-IN", "prisma-v2.5")
    response = llm.chat_json([])
    assert transcript["text"].startswith("Synthetic")
    assert response["mock"] is True


@pytest.mark.parametrize(
    ("text", "expected"),
    [
        ("47 and 9.8 m/s²", ["47", "9.8"]),
        ("৪৭; ২০২৬", ["47", "2026"]),
        ("૪૭; ૨૦૨૬", ["47", "2026"]),
        ("੪੭; ੨੦੨੬", ["47", "2026"]),
        ("௪௭; ௨௦௨௬", ["47", "2026"]),
        ("౪౭; ౨౦౨౬", ["47", "2026"]),
        ("೪೭; ೨೦೨೬", ["47", "2026"]),
        ("൪൭; ൨൦൨൬", ["47", "2026"]),
        ("3/45 and 1,00,000", ["3/45", "100000"]),
    ],
)
def test_number_normalization_across_scripts_and_formats(text, expected):
    assert brain_numbers(text) == expected


def test_locked_term_and_number_failures_are_authoritative():
    sheet = TermSheet.model_validate(
        {"terms": [{"term": "Zorvo", "type": "brand", "treatment": "keep", "locked": True}]}
    )
    failures = run_checks("Zorvo costs 499", "ज़ोरवो की कीमत 498", "hi-IN", sheet)
    assert {failure["check"] for failure in failures} == {"number_missing", "number_extra", "term_missing"}


def test_term_sheet_accepts_clear_category_synonyms_from_llm():
    sheet = TermSheet.model_validate(
        {"terms": [{"term": "Zorvo", "type": "product", "treatment": "keep"}]}
    )
    assert sheet.terms[0].type == "brand"


def test_autocorrect_logs_failed_draft_then_accepts_corrected_text():
    responses = [
        {"translation": "ज़ोरवो की कीमत 498", "terms_used": [], "notes": ""},
        {"translation": "Zorvo की कीमत 499", "terms_used": ["Zorvo"], "notes": "Corrected price."},
    ]
    sheet = {
        "terms": [{"term": "Zorvo", "type": "brand", "treatment": "keep", "locked": True}]
    }
    result = translate_with_autocorrect(MockLLMClient(responses), "Zorvo costs 499", "en-IN", "hi-IN", sheet)
    assert result["status"] == "yellow"
    assert result["translation"] == "Zorvo की कीमत 499"
    assert result["attempts"][0]["trigger_checks"]
    assert result["attempts"][0]["result"] == "passed"


@pytest.mark.parametrize("language", ["mr-IN", "pa-IN"])
def test_low_trust_audio_number_mismatch_is_yellow(language):
    status = audio_number_status("47 रुपये", "43 रुपये", language, ["mr-IN", "pa-IN"])
    assert status["status"] == "yellow"
    assert "Verification confidence is lower" in status["reasons"][0]
    combined = combine_audio_verification("47 रुपये", "43 रुपये", language, {"status": "green", "reasons": []}, ["mr-IN", "pa-IN"])
    assert combined["status"] == "yellow"


def test_report_discloses_low_trust_languages():
    report = build_report("mr-IN", [], Settings())
    assert report["verification"] == "low trust"
    assert "Marathi and Punjabi" in report["verification_note"]
