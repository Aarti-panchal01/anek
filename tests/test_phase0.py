import pytest

from backend.clients.gnani import MockGnaniClient, create_gnani_client
from backend.clients.llm import MockLLMClient, create_llm_client
from backend.config import LANGUAGES, Settings


def test_supported_language_table_is_exact():
    assert len(LANGUAGES) == 10
    assert "or-IN" not in LANGUAGES
    assert "as-IN" not in LANGUAGES


def test_settings_enforce_video_hard_cap():
    assert Settings(max_video_seconds=120).max_video_seconds == 120
    with pytest.raises(ValueError):
        Settings(max_video_seconds=121)


def test_mock_clients_are_selectable_and_never_call_live_services():
    gnani = create_gnani_client(mock=True)
    llm = create_llm_client(mock=True)
    assert isinstance(gnani, MockGnaniClient)
    assert isinstance(llm, MockLLMClient)
    transcript = gnani.stt(b"fake", "hi-IN", "prisma-v2.5")
    response = llm.chat_json([])
    assert transcript["text"].startswith("Synthetic")
    assert response["mock"] is True
