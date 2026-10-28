"""Provider selection and the Anthropic error policy (no network calls)."""
import pytest

from app.errors import LLMRateLimited, LLMRequestRejected, LLMServerError, LLMTimeout, PermanentError, TransientError
from app.llm import build_provider
from app.llm.anthropic import AnthropicLLM, classify_status
from tests.conftest import make_settings


def test_mock_is_the_default_without_a_key():
    assert build_provider(make_settings(llm_provider="auto", anthropic_api_key="")).name == "mock"


def test_anthropic_is_used_when_a_key_is_set():
    provider = build_provider(make_settings(llm_provider="auto", anthropic_api_key="sk-ant-test"))
    assert isinstance(provider, AnthropicLLM)


@pytest.mark.parametrize("status, expected", [
    (429, LLMRateLimited), (500, LLMServerError), (503, LLMServerError), (529, LLMServerError),
    (408, LLMTimeout), (400, LLMRequestRejected), (401, LLMRequestRejected),
    (403, LLMRequestRejected), (404, LLMRequestRejected),
])
def test_http_status_maps_to_the_right_error_kind(status, expected):
    assert classify_status(status) is expected


def test_retryable_statuses_are_transient_and_others_permanent():
    assert all(issubclass(classify_status(s), TransientError) for s in (408, 429, 500, 502, 503, 529))
    assert all(issubclass(classify_status(s), PermanentError) for s in (400, 401, 403, 404, 422))
