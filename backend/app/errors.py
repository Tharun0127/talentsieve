"""The two kinds of failure a job can have. Every error a job raises must be
one of these (or is treated as transient, see app/worker.py).

TransientError: might work if we try again later.
    LLM timeout, HTTP 429 rate limit, HTTP 5xx, connection reset, an LLM
    answer that was not valid JSON this time.
    -> retry with exponential backoff + jitter, up to MAX_ATTEMPTS.

PermanentError: will fail the same way every time; retrying wastes money.
    Corrupt or encrypted PDF, a PDF with no extractable text (a scan),
    resume or batch no longer in the database, HTTP 400/401 from the LLM.
    -> straight to the dead-letter queue, no retry.
"""


class JobError(Exception):
    """Base for errors raised on purpose by job code."""


class TransientError(JobError):
    pass


class PermanentError(JobError):
    pass


# --- concrete transient errors (mostly from the LLM) --------------------------
class LLMTimeout(TransientError):
    pass


class LLMRateLimited(TransientError):
    pass


class LLMServerError(TransientError):
    pass


class LLMBadOutput(TransientError):
    """The model answered, but not in the required JSON shape. Models are
    non-deterministic, so asking again usually works."""


# --- concrete permanent errors -------------------------------------------------
class UnreadablePDF(PermanentError):
    pass


class NoTextInPDF(PermanentError):
    pass


class LLMRequestRejected(PermanentError):
    """HTTP 400/401/403 from the provider: our request or key is wrong."""


class MissingRecord(PermanentError):
    pass
