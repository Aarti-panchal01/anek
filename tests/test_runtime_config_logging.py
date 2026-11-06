import logging

from backend.app import log_runtime_configuration
from backend.config import Settings


def test_startup_config_logs_effective_values_and_env_names_only(caplog):
    config = Settings(
        fit_tolerance=1.07,
        max_stretch=1.12,
        gnani_api_key="GNANI_SECRET_VALUE",
        sarvam_api_key="SARVAM_SECRET_VALUE",
        llm_2_api_key="GEMINI_SECRET_VALUE",
    )

    with caplog.at_level(logging.INFO):
        log_runtime_configuration(config, {
            "RAILWAY_ENVIRONMENT": "production",
            "FIT_TOLERANCE": "1.07",
        })

    output = caplog.text
    assert "FIT_TOLERANCE=1.070" in output
    assert "MAX_STRETCH=1.120" in output
    assert "SILENT_GAP_SPILL=enabled" in output
    assert "SPEEDUP_CAP=1.15x" in output
    assert "Enabled providers: Gnani, Sarvam, Groq, Gemini" in output
    assert "Railway timing environment overrides code defaults: FIT_TOLERANCE" in output
    assert "GNANI_SECRET_VALUE" not in output
    assert "SARVAM_SECRET_VALUE" not in output
    assert "GEMINI_SECRET_VALUE" not in output
    assert "production" not in output
    assert "1.07" not in output.replace("FIT_TOLERANCE=1.070", "")
