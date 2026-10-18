"""Языковая модель: только саммаризация инцидента."""

from app.adapters.llm.base import (
    IncidentContext,
    IncidentEventLine,
    LlmClient,
    LlmError,
    LlmResult,
    LlmUnavailableError,
)
from app.adapters.llm.masking import Masker
from app.adapters.llm.openai_compatible import LlmSettings, OpenAiCompatibleClient
from app.adapters.llm.template import TemplateSummarizer
from app.config import Settings


def build_llm_client(settings: Settings) -> LlmClient:
    if settings.llm_provider == "template":
        return TemplateSummarizer()
    return OpenAiCompatibleClient(
        LlmSettings(
            base_url=settings.llm_base_url,
            model=settings.llm_model,
            api_key=settings.llm_api_key,
            timeout_s=settings.llm_timeout_s,
            max_attempts=settings.llm_max_attempts,
            verify_tls=settings.llm_verify_tls,
            disable_thinking=settings.llm_disable_thinking,
        )
    )


__all__ = [
    "IncidentContext",
    "IncidentEventLine",
    "LlmClient",
    "LlmError",
    "LlmResult",
    "LlmSettings",
    "LlmUnavailableError",
    "Masker",
    "OpenAiCompatibleClient",
    "TemplateSummarizer",
    "build_llm_client",
]
