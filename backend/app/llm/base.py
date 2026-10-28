"""The LLM interface every provider implements, and the strict output format.

Providers return the model's RAW TEXT. We parse and validate it ourselves
with Pydantic, so a model that rambles, wraps JSON in prose, returns 4
strengths or a score of 140 is caught here, not stored in the database.
"""
import json
import re
from typing import Protocol

from pydantic import BaseModel, Field, ValidationError, field_validator

from app.errors import LLMBadOutput

SYSTEM_PROMPT = """You are an expert technical recruiter. Score how well a resume matches a job description.
Reply with ONLY a JSON object, no prose, no markdown fences, exactly in this shape:
{"candidate_name": string or null, "score": integer 0-100, "strengths": [3 short strings],
 "gaps": [3 short strings], "verdict": "one sentence"}
Scoring guide: 90-100 exceptional fit, 70-89 strong, 50-69 partial, below 50 weak.
Base everything only on evidence in the resume."""


def build_user_prompt(resume_text: str, job_description: str) -> str:
    return f"<job_description>\n{job_description}\n</job_description>\n\n<resume>\n{resume_text}\n</resume>"


class ScoreResult(BaseModel):
    candidate_name: str | None = Field(default=None, max_length=200)
    score: int = Field(ge=0, le=100)
    strengths: list[str] = Field(min_length=3, max_length=3)
    gaps: list[str] = Field(min_length=3, max_length=3)
    verdict: str = Field(min_length=1, max_length=500)

    @field_validator("strengths", "gaps")
    @classmethod
    def non_empty_items(cls, items: list[str]) -> list[str]:
        cleaned = [item.strip() for item in items]
        if any(not item for item in cleaned):
            raise ValueError("items must not be empty")
        return cleaned


class LLMProvider(Protocol):
    name: str

    async def complete(self, system: str, user: str) -> str:
        """Return the model's raw text answer or raise a TransientError /
        PermanentError from app.errors."""
        ...


def parse_score(raw: str) -> ScoreResult:
    """Strict JSON -> validated ScoreResult, or LLMBadOutput (transient)."""
    text = raw.strip()
    # Tolerate a ```json fence, the one deviation models commonly make.
    fenced = re.match(r"^```(?:json)?\s*(.*?)\s*```$", text, re.DOTALL)
    if fenced:
        text = fenced.group(1)
    try:
        return ScoreResult.model_validate(json.loads(text))
    except (json.JSONDecodeError, ValidationError) as exc:
        raise LLMBadOutput(f"LLM output failed validation: {str(exc)[:200]}") from exc


async def score_resume(provider: LLMProvider, resume_text: str, job_description: str) -> ScoreResult:
    raw = await provider.complete(SYSTEM_PROMPT, build_user_prompt(resume_text, job_description))
    return parse_score(raw)
