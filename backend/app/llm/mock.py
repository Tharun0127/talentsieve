"""MockLLM: behaves like a real LLM API, without a key or a bill.

- Random latency between min_delay and max_delay seconds.
- Fails at `failure_rate` with the same TRANSIENT errors a real API gives
  (timeout, 429 rate limit, 503 overloaded), so retries and the DLQ can be
  exercised and tested.
- Answers in the same strict JSON format a real model must use, so the
  parse-and-validate path runs for real.
- The score is a crude keyword overlap between resume and job description,
  so rankings are meaningful in the demo (not random noise).
"""
import asyncio
import json
import random
import re

from app.errors import LLMRateLimited, LLMServerError, LLMTimeout

STOPWORDS = {"and", "the", "with", "for", "you", "our", "are", "will", "years", "year", "experience",
             "senior", "junior", "engineer", "developer", "work", "team", "using", "have", "this", "that"}

# Recognisable skills. When a job description mentions some of these, only
# they count as requirements; otherwise prose words like "hiring" or
# "mentor" would make every score tiny.
SKILLS = {"python", "fastapi", "django", "flask", "postgresql", "mysql", "redis", "docker", "kubernetes", "aws",
          "gcp", "azure", "celery", "kafka", "sqlalchemy", "graphql", "terraform", "react", "typescript",
          "javascript", "java", "spring", "go", "rust", "swift", "asyncio", "ci/cd", "rest", "apis", "sql",
          "figma", "photoshop", "tableau", "excel", "salesforce", "seo"}


def keywords(text: str) -> set[str]:
    words = {w.strip(".,;:()") for w in re.findall(r"[a-z][a-z0-9+#./]{1,}", text.lower())}
    return {w for w in words if w not in STOPWORDS and len(w) > 1}


def requirements(job: str) -> set[str]:
    wanted = keywords(job)
    skills = wanted & SKILLS
    return skills or wanted


class MockLLM:
    name = "mock"

    def __init__(self, min_delay: float, max_delay: float, failure_rate: float, rng: random.Random | None = None):
        self.min_delay = min_delay
        self.max_delay = max(max_delay, min_delay)
        self.failure_rate = failure_rate
        self.rng = rng or random.Random()
        self.calls = 0  # tests use this to prove the cache skips the LLM

    async def complete(self, system: str, user: str) -> str:
        self.calls += 1
        await asyncio.sleep(self.rng.uniform(self.min_delay, self.max_delay))
        if self.rng.random() < self.failure_rate:
            raise self.rng.choice([
                LLMTimeout("mock: request timed out"),
                LLMRateLimited("mock: 429 rate limited"),
                LLMServerError("mock: 503 overloaded"),
            ])
        job, resume = self._split(user)
        return json.dumps(self._answer(job, resume))

    @staticmethod
    def _split(user: str) -> tuple[str, str]:
        job = re.search(r"<job_description>(.*?)</job_description>", user, re.DOTALL)
        resume = re.search(r"<resume>(.*?)</resume>", user, re.DOTALL)
        return (job.group(1) if job else ""), (resume.group(1) if resume else "")

    def _answer(self, job: str, resume: str) -> dict:
        wanted, have = requirements(job), keywords(resume)
        matched = sorted(wanted & have)
        missing = sorted(wanted - have)
        score = round(100 * len(matched) / len(wanted)) if wanted else 50
        # Sample resumes start with "First Last"; PDF text has no line breaks
        # left after normalisation, so take the first two words.
        first_line = " ".join(resume.split()[:2])[:60] or None
        strengths = [f"Has {m}" for m in matched[:3]]
        strengths += ["Relevant engineering background", "Clear CV structure", "Hands-on project work"]
        gaps = [f"No evidence of {m}" for m in missing[:3]]
        gaps += ["Limited leadership evidence", "No metrics on impact", "Domain experience unclear"]
        verdict = ("Strong match, interview." if score >= 70 else
                   "Partial match, consider for a screen." if score >= 50 else "Weak match for this role.")
        return {"candidate_name": first_line, "score": min(100, score), "strengths": strengths[:3],
                "gaps": gaps[:3], "verdict": verdict}
