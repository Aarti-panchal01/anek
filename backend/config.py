"""Application configuration loaded from environment variables and .env."""

from functools import lru_cache

from dotenv import load_dotenv
from pydantic import Field, field_validator
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
    stt_model: str = "prisma-v2.5"
    verify_stt_model: str = "prisma-v3.0"
    llm_base_url: str = ""
    llm_model: str = ""
    llm_api_key: str = ""
    numeral_style: str = "ascii"
    max_video_seconds: int = Field(default=120, le=120, gt=0)
    max_credits_per_job: int = 2500
    gnani_daily_limit: int = 2500
    enabled_langs: Annotated[list[str], NoDecode] = Field(default_factory=lambda: list(LANGUAGES))
    low_trust_verify_langs: Annotated[list[str], NoDecode] = Field(
        default_factory=lambda: ["mr-IN", "pa-IN"]
    )
    gnani_mock: bool = False
    llm_mock: bool = False
    fit_tolerance: float = 1.15
    max_stretch: float = 1.15
    similarity_threshold: float = 0.7
    consent_match_threshold: int = 85

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
