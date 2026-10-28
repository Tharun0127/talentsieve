"""Anthropic (Claude) provider, used when ANTHROPIC_API_KEY is set.

Design points:
- The SDK's own retries are OFF (max_retries=0). Our queue already retries
  with backoff, counts attempts, respects the global rate limit and ends in
  the DLQ. Two retry layers would multiply attempts invisibly.
- Structured outputs (output_config.format with a JSON schema) make the API
  return JSON in our exact shape; we still validate it with Pydantic.
- Effort "low": scoring one resume is close to classification, and low
  effort is cheaper and faster. Change ANTHROPIC_EFFORT to trade up.
- Server-side refusal fallbacks are on by default (ANTHROPIC_FALLBACKS):
  if the model declines a request, the API retries it on a fallback model
  inside the same call. A refusal that survives is a PERMANENT error.
- Every SDK error is mapped to our TransientError / PermanentError.
"""
import anthropic

from app.config import Settings
from app.errors import (
    LLMBadOutput, LLMRateLimited, LLMRequestRejected, LLMServerError, LLMTimeout, PermanentError, TransientError,
)

# The same shape as app.llm.base.ScoreResult, as a JSON schema for the API.
SCORE_SCHEMA = {
    "type": "object",
    "properties": {
        "candidate_name": {"type": ["string", "null"]},
        "score": {"type": "integer"},
        "strengths": {"type": "array", "items": {"type": "string"}},
        "gaps": {"type": "array", "items": {"type": "string"}},
        "verdict": {"type": "string"},
    },
    "required": ["candidate_name", "score", "strengths", "gaps", "verdict"],
    "additionalProperties": False,
}

FALLBACK_BETA = "server-side-fallback-2026-07-01"


def classify_status(status: int) -> type[TransientError] | type[PermanentError]:
    """HTTP status -> our error class. The single place this policy lives."""
    if status == 429:
        return LLMRateLimited
    if status >= 500:  # includes 529 "overloaded"
        return LLMServerError
    if status in (408, 409):  # request timeout / conflict: worth another try
        return LLMTimeout if status == 408 else LLMServerError
    return LLMRequestRejected  # 400, 401, 403, 404, 413, 422: retrying cannot help


class LLMRefused(PermanentError):
    pass


class AnthropicLLM:
    name = "anthropic"

    def __init__(self, settings: Settings):
        self.settings = settings
        self.client = anthropic.AsyncAnthropic(
            api_key=settings.anthropic_api_key, timeout=settings.llm_timeout_seconds, max_retries=0,
        )

    async def complete(self, system: str, user: str) -> str:
        try:
            response = await self._create(system, user)
        except anthropic.APITimeoutError as exc:  # subclass of APIConnectionError: check first
            raise LLMTimeout(f"anthropic timeout: {exc}") from exc
        except anthropic.APIConnectionError as exc:
            raise LLMServerError(f"anthropic connection error: {exc}") from exc
        except anthropic.APIStatusError as exc:
            error_class = classify_status(exc.status_code)
            raise error_class(f"anthropic HTTP {exc.status_code}: {exc.message}") from exc

        if response.stop_reason == "refusal":
            category = getattr(response.stop_details, "category", None) if response.stop_details else None
            raise LLMRefused(f"model declined to score this resume (category: {category})")
        if response.stop_reason == "max_tokens":
            raise LLMBadOutput("answer was cut off at max_tokens")
        text = next((block.text for block in response.content if block.type == "text"), None)
        if not text:
            raise LLMBadOutput("no text block in the response")
        return text

    async def _create(self, system: str, user: str):
        settings = self.settings
        request = {
            "model": settings.anthropic_model,
            "max_tokens": 16000,
            "system": system,
            "messages": [{"role": "user", "content": user}],
            "output_config": {
                "effort": settings.anthropic_effort,
                "format": {"type": "json_schema", "schema": SCORE_SCHEMA},
            },
        }
        if settings.anthropic_fallbacks:
            return await self.client.beta.messages.create(**request, betas=[FALLBACK_BETA], fallbacks="default")
        return await self.client.messages.create(**request)
