"""Application configuration loaded from environment variables and .env."""

from functools import lru_cache

from dotenv import load_dotenv
from pydantic import Field, field_validator, model_validator
from pydantic_settings import BaseSettings, NoDecode, SettingsConfigDict
from typing import Annotated

LANGUAGES = {
    "bn-IN": "Bengali",
    "en-IN": "English",
    "gu-IN": "Gujarati",
    "hi-IN": "Hindi",
    "kn-IN": "Kannada",
    "ml-IN": "Malayalam",
    "mr-IN": "Marathi",
    "pa-IN": "Punjabi",
    "ta-IN": "Tamil",
    "te-IN": "Telugu",
}


class Settings(BaseSettings):
    model_config = SettingsConfigDict(env_file=".env", extra="ignore")

    app_name: str = "Anek"
    gnani_api_key: str = ""
    gnani_base_url: str = "https://api.vachana.ai"
    sarvam_api_key: str = ""
    sarvam_api_key_1: str = ""
    sarvam_api_key_2: str = ""
    sarvam_api_key_3: str = ""
    sarvam_api_key_4: str = ""
    sarvam_api_key_5: str = ""
    sarvam_translation_model: str = "mayura:v1"
    sarvam_rewrite_model: str = "sarvam-105b-conversations"
    numeral_style: str = "ascii"
    max_video_seconds: int = Field(default=120, le=120, gt=0)
    export_retention_hours: int = Field(default=24, ge=1, le=8760)
    max_credits_per_job: int = 0
    gnani_daily_limit: int = 2500
    enabled_langs: Annotated[list[str], NoDecode] = Field(default_factory=lambda: list(LANGUAGES))
    low_trust_verify_langs: Annotated[list[str], NoDecode] = Field(
        default_factory=lambda: ["mr-IN", "pa-IN"]
    )
    per_ip_limits: str = "voice_clones=3,previews=30,jobs=5"
    gnani_mock: bool = False
    llm_mock: bool = False
    fit_tolerance: float = 1.15
    max_stretch: float = 1.15
    similarity_threshold: float = 0.7
    consent_match_threshold: int = 85
    anek_data_dir: str = "data"
    require_access_auth: bool = False
    anek_access_password: str = ""
    anek_auth_secret: str = ""
    cors_origins: str = ""
    cors_origin_regex: str = ""

    @property
    def resolved_sarvam_api_keys(self) -> tuple[str, ...]:
        numbered = tuple(
            key for key in (
                self.sarvam_api_key_1, self.sarvam_api_key_2, self.sarvam_api_key_3,
                self.sarvam_api_key_4, self.sarvam_api_key_5,
            ) if key and key.strip()
        )
        if numbered:
            return tuple(dict.fromkeys(key.strip() for key in numbered))
        return (self.sarvam_api_key.strip(),) if self.sarvam_api_key.strip() else ()

    @model_validator(mode="after")
    def set_legacy_sarvam_key(self) -> "Settings":
        if not self.sarvam_api_key:
            keys = self.resolved_sarvam_api_keys
            if keys:
                self.sarvam_api_key = keys[0]
        return self

    @field_validator("enabled_langs", mode="before")
    @classmethod
    def parse_enabled_langs(cls, value: list[str] | str) -> list[str]:
        if isinstance(value, str):
            return [language.strip() for language in value.split(",") if language.strip()]
        return value

    @field_validator("low_trust_verify_langs", mode="before")
    @classmethod
    def parse_low_trust_langs(cls, value: list[str] | str) -> list[str]:
        if isinstance(value, str):
            return [language.strip() for language in value.split(",") if language.strip()]
        return value


@lru_cache
def get_settings() -> Settings:
    load_dotenv()
    return Settings()
