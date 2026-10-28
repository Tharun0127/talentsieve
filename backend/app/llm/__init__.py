"""Pick the LLM provider from settings: MockLLM unless a real key is set."""
from app.config import Settings
from app.llm.base import LLMProvider
from app.llm.mock import MockLLM


def build_provider(settings: Settings) -> LLMProvider:
    choice = settings.llm_provider
    if choice == "auto":
        choice = "anthropic" if settings.anthropic_api_key else "mock"
    if choice == "anthropic":
        from app.llm.anthropic import AnthropicLLM  # phase 6

        return AnthropicLLM(settings)
    return MockLLM(settings.mock_llm_min_delay, settings.mock_llm_max_delay, settings.mock_llm_failure_rate)
