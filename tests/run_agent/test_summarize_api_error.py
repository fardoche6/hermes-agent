"""Regression: empty-body HTTP 4xx errors must still surface a real provider message.

Reported on Windows (#36109): an LLM API call returned HTTP 400 with an *empty*
parsed SDK ``body`` ({}), so ``_summarize_api_error`` fell through to the bare
``str(error)`` path and the user saw only "HTTP 400" with no provider detail.
The SDK leaves ``body`` empty in this case, but the underlying httpx
``response`` still carries the real payload in ``.text``. These tests lock the
contract: when ``body`` is empty, fall back to ``response.text`` (parsing a JSON
``error.message`` / ``message`` when present) so logs and CLI show the real
provider error. This is a diagnostic improvement and is platform-agnostic.
"""

import json
from types import SimpleNamespace
from typing import Any

import httpx
import pytest

from run_agent import AIAgent
from agent.api_error_summary import ApiErrorSummaryMixin


def _make_empty_body_error(response_text: str, status_code: int = 400) -> Exception:
    """Mimic an OpenAI-SDK error whose parsed body is empty but whose httpx
    response still holds the payload text."""
    err = Exception("")  # str(error) is empty/uninformative on this path
    err.status_code = status_code
    err.body = {}  # empty dict — the #36109 trigger
    err.response = SimpleNamespace(text=response_text)
    return err


def test_empty_body_falls_back_to_response_json_error_message():
    """A JSON payload with error.message is surfaced (not a bare HTTP 400)."""
    err = _make_empty_body_error(
        '{"error": {"message": "model `foo` does not exist", "type": "invalid_request_error"}}'
    )
    summary = AIAgent._summarize_api_error(err)
    assert "HTTP 400" in summary
    assert "model `foo` does not exist" in summary

@pytest.mark.parametrize(
    "technical_message",
    [
        "Temporary failure in name resolution",
        "Name or service not known",
        "nodename nor servname provided, or not known",
        "getaddrinfo failed",
        "No address associated with hostname",
        "Network is unreachable",
    ],
)
def test_network_resolution_failure_explains_that_the_user_may_be_offline(
    technical_message,
):
    error = OSError(-3, technical_message)

    summary = AIAgent._summarize_api_error(error)

    assert summary == (
        "Hermes can't reach the model provider. You may be offline. "
        "Check your internet connection and try again."
    )
    assert "name resolution" not in summary.lower()


def test_wrapped_dns_resolution_failure_gets_the_same_friendly_message():
    try:
        try:
            raise OSError(-3, "Temporary failure in name resolution")
        except OSError as cause:
            raise RuntimeError("Connection error.") from cause
    except RuntimeError as error:
        summary = AIAgent._summarize_api_error(error)

    assert "You may be offline" in summary
    assert "Connection error" not in summary


def test_unread_streaming_response_does_not_crash_and_falls_back_to_exception_message():
    """Unread streaming responses must not replace the real provider error."""

    class _StreamingError(Exception):
        def __init__(self):
            super().__init__("Gemini HTTP 429: quota exceeded")
            self.status_code = 429
            self.response: Any = None

    err = _StreamingError()

    class _UnreadStreamingResponse:
        @property
        def text(self):
            raise httpx.ResponseNotRead()

    err.response = _UnreadStreamingResponse()
    summary = AIAgent._summarize_api_error(err)
    assert "HTTP 429" in summary
    assert "Gemini HTTP 429: quota exceeded" in summary


def _summary_error_cases():
    body_error = Exception("body error")
    body_error.status_code = 400
    body_error.body = {
        "error": {
            "message": (
                "GET https://api.example.test/v1?token=SYN_SUMMARY_BODY&view=public "
                "status=400"
            ),
            "type": "invalid_request_error",
        }
    }

    response_error = Exception("response error")
    response_error.status_code = 502
    response_error.body = {}
    response_error.response = SimpleNamespace(
        text="proxy echoed https://api.example.test/v1?key=SYN_SUMMARY_RESPONSE&safe=ok"
    )

    fallback_error = Exception(
        "provider failed at https://api.example.test/v1?token=SYN_SUMMARY_FALLBACK&safe=ok"
    )
    fallback_error.status_code = 500

    return (
        (body_error, "SYN_SUMMARY_BODY"),
        (response_error, "SYN_SUMMARY_RESPONSE"),
        (fallback_error, "SYN_SUMMARY_FALLBACK"),
    )


@pytest.mark.parametrize("error, synthetic_secret", _summary_error_cases())
def test_api_error_summary_redacts_query_credentials_on_all_summary_paths(error, synthetic_secret):
    summary = AIAgent._summarize_api_error(error)

    assert synthetic_secret not in summary
    assert "api.example.test" in summary


def test_clean_error_message_redacts_query_credentials_for_display():
    message = "provider returned https://api.example.test/v1?token=SYN_CLEAN_MESSAGE&safe=ok"

    cleaned = ApiErrorSummaryMixin()._clean_error_message(message)

    assert "SYN_CLEAN_MESSAGE" not in cleaned
    assert "api.example.test" in cleaned


def test_deep_json_parser_failure_is_contained_and_bounded():
    marker = "SYN_DEEP_JSON_QUERY"
    error = Exception("provider failed at https://api.example.test/?token=" + marker)
    error.status_code = 502
    error.body = {}
    error.response = SimpleNamespace(
        text="[" * 1200 + '"' + marker + '"' + "]" * 1200,
    )

    summary = AIAgent._summarize_api_error(error)

    assert marker not in summary
    assert len(summary) <= 500
    assert summary.startswith("HTTP 502:")


def test_cyclic_structured_detail_fails_closed():
    marker = "SYN_CYCLIC_DETAIL"
    cyclic = {}
    cyclic["detail"] = cyclic
    error = Exception("provider failed " + marker)
    error.status_code = 400
    error.body = {"error": {"message": cyclic}}

    summary = AIAgent._summarize_api_error(error)

    assert marker not in summary
    assert len(summary) <= 300


def test_deep_structured_detail_is_bounded_by_depth():
    marker = "SYN_DEEP_STRUCTURED_DETAIL"
    nested = marker
    for _ in range(32):
        nested = {"detail": nested}
    error = Exception("provider returned structured detail")
    error.status_code = 400
    error.body = {"error": {"message": nested}}

    summary = AIAgent._summarize_api_error(error)

    assert marker not in summary
    assert len(summary) <= 300


def test_fallback_preserves_the_prior_500_character_limit():
    marker = "SYN_FALLBACK_BOUND_QUERY"
    error = Exception(
        "provider failed at https://api.example.test/?token=" + marker + " " + ("safe " * 300)
    )
    error.status_code = 500

    summary = AIAgent._summarize_api_error(error)

    assert marker not in summary
    assert len(summary) <= 500


def test_exception_string_failure_never_escapes_raw_text():
    class _UnstringableError(Exception):
        def __str__(self):
            raise RuntimeError("SYN_STRING_COERCION_FAILURE")

    summary = AIAgent._summarize_api_error(_UnstringableError())

    assert "SYN_STRING_COERCION_FAILURE" not in summary
    assert len(summary) <= 500


def test_auxiliary_warning_does_not_emit_raw_parser_input(monkeypatch):
    marker = "SYN_WARNING_QUERY"
    error = Exception("provider failed at https://api.example.test/?token=" + marker)
    error.status_code = 502
    error.body = {}
    error.response = SimpleNamespace(text="[" * 1200 + '"' + marker + '"' + "]" * 1200)
    warnings = []
    agent = object.__new__(AIAgent)
    monkeypatch.setattr(agent, "_emit_warning", warnings.append)

    AIAgent._emit_auxiliary_failure(agent, "summary", error)

    assert warnings
    assert marker not in warnings[0]


def test_api_error_summary_redacts_non_ascii_prefixed_structured_body():
    message = (
        "synthetic prefix İ before fields "
        '{"Authorization": "Bearer SYN_NONASCII_SUMMARY_AUTH", '
        '"X-Api-Key": "SYN_NONASCII_SUMMARY_API", '
        '"Cookie": "sid=SYN_NONASCII_SUMMARY_COOKIE", '
        '"Set-Cookie": "sid=SYN_NONASCII_SUMMARY_SET; Path=/; Secure; HttpOnly", '
        '"safe": "public"}'
    )
    error = Exception("synthetic provider failure")
    error.status_code = 500
    error.body = {"error": {"message": message}}

    summary = AIAgent._summarize_api_error(error)

    for marker in (
        "SYN_NONASCII_SUMMARY_AUTH", "SYN_NONASCII_SUMMARY_API",
        "SYN_NONASCII_SUMMARY_COOKIE", "SYN_NONASCII_SUMMARY_SET",
    ):
        assert marker not in summary
    assert "synthetic prefix İ" in summary
    assert '"safe": "public"' in summary


def test_pathological_status_metadata_uses_total_safe_summary_and_warning():
    marker = "SYN_PATHOLOGICAL_STATUS"

    class _PathologicalStatus(int):
        def __format__(self, _spec):
            raise ValueError(marker)

    error = Exception(marker)
    error.status_code = _PathologicalStatus(500)

    summary = AIAgent._summarize_api_error(error)
    warnings = []
    agent = object.__new__(AIAgent)
    agent._emit_warning = warnings.append
    AIAgent._emit_auxiliary_failure(agent, "summary", error)

    assert summary == "Provider returned an error."
    assert warnings == ["⚠ Auxiliary summary failed: Provider returned an error."]
    assert marker not in summary
    assert marker not in warnings[0]


def test_summary_and_auxiliary_warning_redact_non_scalar_aliases():
    marker = "SYN_R4_SUMMARY_API"
    error = Exception("synthetic provider failure")
    error.status_code = 502
    error.body = {
        "error": {
            "message": 'provider detail {"X-Api-Key": [8462057319, "SYN_R4_SUMMARY_API"], "safe": "public"}',
        },
    }

    summary = AIAgent._summarize_api_error(error)
    warnings = []
    agent = object.__new__(AIAgent)
    agent._emit_warning = warnings.append
    AIAgent._emit_auxiliary_failure(agent, "summary", error)

    assert marker not in summary
    assert "8462057319" not in summary
    assert '"safe": "public"' in summary
    assert warnings and marker not in warnings[0]
    assert "8462057319" not in warnings[0]


def test_summary_fails_closed_for_malformed_outer_cookie_with_nested_credentials():
    markers = ("SYN_R4_SUMMARY_COOKIE", "SYN_R4_SUMMARY_HIDDEN")
    error = Exception("synthetic provider failure")
    error.status_code = 500
    error.body = {
        "error": {
            "message": (
                '{"Cookie": ["sid=' + markers[0] + '", '
                '"Authorization": "Bearer ' + markers[1] + '"}'
            ),
        },
    }

    summary = AIAgent._summarize_api_error(error)
    warnings = []
    agent = object.__new__(AIAgent)
    agent._emit_warning = warnings.append
    AIAgent._emit_auxiliary_failure(agent, "summary", error)

    assert all(marker not in summary for marker in markers)
    assert summary == "[redaction-unavailable]"
    assert warnings == ["⚠ Auxiliary summary failed: [redaction-unavailable]"]
    assert all(marker not in warnings[0] for marker in markers)


def test_summary_and_auxiliary_warning_mask_complete_set_cookie_value():
    marker = "SYN_R5_SUMMARY_SET_COOKIE"
    error = Exception("synthetic provider failure")
    error.status_code = 502
    error.body = {
        "error": {
            "message": (
                "provider detail Set-Cookie: sid=" + marker
                + "; SameSite=bogus; Max-Age=opaque; Path=/"
            ),
        },
    }

    summary = AIAgent._summarize_api_error(error)
    warnings = []
    agent = object.__new__(AIAgent)
    agent._emit_warning = warnings.append
    AIAgent._emit_auxiliary_failure(agent, "summary", error)

    assert marker not in summary
    assert "Set-Cookie: ***" in summary
    assert all(detail not in summary for detail in ("SameSite=bogus", "Max-Age=opaque", "Path=/"))
    assert warnings and marker not in warnings[0]
    assert "Set-Cookie: ***" in warnings[0]


def test_entitlement_hint_is_complete_after_detail_redaction_and_second_pass():
    from agent.redact import redact_persisted_diagnostic

    error = Exception("synthetic provider failure")
    error.status_code = 403
    error.body = {
        "error": {
            "message": (
                'You have either run out of available resources or do not have an active '
                'Grok subscription. Echo {"X-Api-Key": "SYN_R4_ENTITLEMENT_API"}'
            ),
        },
    }

    summary = AIAgent._summarize_api_error(error)

    assert "SYN_R4_ENTITLEMENT_API" not in summary
    assert summary.count("X Premium+ does NOT include") == 1
    assert "https://grok.com/?_s=usage" in summary
    assert "/model" in summary
    assert redact_persisted_diagnostic(summary, limit=None) == summary


@pytest.mark.parametrize(
    "detail",
    [
        r"provider Set-Cookie: sid=SYN_R6_SUMMARY_HEAD\ncontinued=SYN_R6_SUMMARY_TAIL; Path=/SYN_R6_SUMMARY_PATH",
        '{"X-Api-Key": true\x0b, "safe": "SYN_R6_SUMMARY_WHITESPACE_TAIL"}',
        '{"Authorization": "Bearer SYN_R6_SUMMARY_AUTH"\u00a0, "safe": "SYN_R6_SUMMARY_NBSP_TAIL"}',
    ],
)
def test_summary_and_auxiliary_warning_fail_closed_for_ambiguous_http_framing(
    detail, monkeypatch
):
    error = Exception("synthetic provider failure")
    error.status_code = 502
    error.body = {"error": {"message": detail}}
    summary = AIAgent._summarize_api_error(error)
    warnings = []
    agent = object.__new__(AIAgent)
    monkeypatch.setattr(agent, "_emit_warning", warnings.append)

    AIAgent._emit_auxiliary_failure(agent, "summary", error)

    assert summary == "[redaction-unavailable]"
    assert warnings == ["⚠ Auxiliary summary failed: [redaction-unavailable]"]
    assert "SYN_R6_" not in summary
    assert all("SYN_R6_" not in warning for warning in warnings)


def test_summary_and_auxiliary_warning_keep_serialized_literal_cookie_atomic(monkeypatch):
    detail = json.dumps({
        "detail": r"Set-Cookie: sid=SYN_R6_SUMMARY_COOKIE\ncontinued=SYN_R6_SUMMARY_SUFFIX; Path=/SYN_R6_SUMMARY_PATH",
        "safe": "public",
    }, ensure_ascii=False)
    error = Exception("synthetic provider failure")
    error.status_code = 502
    error.body = {"error": {"message": detail}}
    summary = AIAgent._summarize_api_error(error)
    warnings = []
    agent = object.__new__(AIAgent)
    monkeypatch.setattr(agent, "_emit_warning", warnings.append)

    AIAgent._emit_auxiliary_failure(agent, "summary", error)

    prefix = "HTTP 502: "
    assert summary.startswith(prefix)
    parsed = json.loads(summary[len(prefix):])
    assert parsed == {"detail": "Set-Cookie: ***", "safe": "public"}
    assert warnings == [f"⚠ Auxiliary summary failed: {summary}"]
    assert "SYN_R6_SUMMARY_" not in summary
    assert "SYN_R6_SUMMARY_" not in warnings[0]
