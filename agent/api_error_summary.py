"""Provider API error summarising for ``AIAgent``.

Entitlement-failure detection, xAI subscription decoration, structured-detail coercion, and log-safe
redaction of provider error payloads.
Extracted from ``run_agent.py``; every method resolves through ``AIAgent``'s MRO unchanged.
"""
import json
import re
from typing import Any, Dict, Optional

from agent.redact import redact_persisted_diagnostic

# Offline DNS failures are wrapped in a generic "Connection error" by SDKs — inspect the chain.
_NETWORK_RESOLUTION_MARKERS = (
    "temporary failure in name resolution",
    "name or service not known",
    "nodename nor servname provided, or not known",
    "getaddrinfo failed",
    "no address associated with hostname",
    "network is unreachable",
)
_XAI_ENTITLEMENT_HINT_MARKER = "X Premium+ does NOT include"
_XAI_ENTITLEMENT_HINT = (
    " — xAI rejected this OAuth account. NOTE: X Premium+ does NOT "
    "include xAI API access — only standalone SuperGrok subscribers "
    "can use this provider. Other possible causes: no Grok "
    "subscription, your tier doesn't include this model, or your "
    "quota is exhausted. Check https://grok.com/?_s=usage to see "
    "which, or run `/model` to switch providers."
)
_ERROR_DETAIL_KEYS = ("message", "detail", "error", "code", "type")
_SAFE_API_ERROR_SUMMARY = "Provider returned an error."
_MAX_DETAIL_DEPTH = 8
_MAX_DETAIL_NODES = 128


def _is_xai_entitlement_text(lower: str) -> bool:
    """xAI's permission-denied body text for an unsubscribed / under-tiered / exhausted account."""
    return (
        "do not have an active grok subscription" in lower
        or ("out of available resources" in lower and "grok" in lower)
        or ("does not have permission" in lower and "grok" in lower)
    )


def _http_prefix(error: Exception) -> str:
    try:
        status_code = getattr(error, "status_code", None)
    except Exception:
        return ""
    if type(status_code) is not int or not 100 <= status_code <= 599:
        return ""
    return f"HTTP {status_code}: "


def _safe_api_error_summary(error: Exception) -> str:
    """Return a fixed safe result when summary extraction itself fails."""
    return f"{_http_prefix(error)}{_SAFE_API_ERROR_SUMMARY}"


class ApiErrorSummaryMixin:
    """Provider error -> user/log-safe summary (see module docstring)."""

    @staticmethod
    def _is_entitlement_failure(
        error_context: Optional[Dict[str, Any]], status_code: Optional[int]
    ) -> bool:
        """Detect subscription/entitlement 401/403s that masquerade as auth failures.

        Refreshing a token cannot fix an unsubscribed account, so callers surface the error instead of looping
        the pool. xAI returns the same permission-denied text for BOTH cases; a ``[WKE=unauthenticated:...]``
        suffix (or "access token could not be validated") means stale token → return False so the refresh path
        runs.

        Disambiguator for xAI (#29344): the same ``code`` text ("The caller does not have permission to
        execute the specified operation") is returned for BOTH an unsubscribed account AND a stale OAuth
        access token.  xAI ships an explicit signal in the ``error`` field that tells the two apart: a
        ``[WKE=unauthenticated:...]`` suffix (and/or the ``OAuth2 access token could not be validated``
        phrasing) means the credentials failed validation — that's recoverable by refreshing the token, NOT
        by surfacing an entitlement message. When either signal is present we return False eagerly so the
        credential-pool refresh path runs, letting long-running TUI sessions recover from stale tokens
        without an exit/reopen cycle.
        """
        if status_code not in {401, 403, None}:
            return False
        if not isinstance(error_context, dict):
            return False
        # Single lowercase haystack over every field shape (message/reason and raw code/error).
        haystack = " ".join(
            str(error_context.get(k) or "").lower() for k in ("message", "reason", "code", "error")
        )
        if not haystack.strip():
            return False
        if "[wke=unauthenticated:" in haystack or "oauth2 access token could not be validated" in haystack:
            return False
        return _is_xai_entitlement_text(haystack)

    @staticmethod
    def _decorate_xai_entitlement_error(detail: str) -> str:
        """Append a neutral hint when xAI's OAuth surface returns the permission-denied 403.

        xAI's ``/v1/responses`` uses one body for several causes (no subscription, tier lacks the model, quota
        exhausted). The least obvious: X Premium+ does NOT include API access — only SuperGrok does. Lead with
        that, keep the raw text, point at https://grok.com/?_s=usage. Idempotent: a substring unique to the
        hint marks prior decoration.
        """
        if not detail or not _is_xai_entitlement_text(detail.lower()):
            return detail
        if _XAI_ENTITLEMENT_HINT_MARKER in detail:
            return detail
        return f"{detail}{_XAI_ENTITLEMENT_HINT}"

    @staticmethod
    def _redact_summary_detail(detail: str, *, limit: int) -> str:
        """Bound untrusted detail before appending the complete trusted entitlement hint."""
        if _is_xai_entitlement_text(detail.lower()):
            marker_index = detail.find(_XAI_ENTITLEMENT_HINT_MARKER)
            untrusted_detail = detail if marker_index < 0 else detail[:marker_index]
            safe_detail = redact_persisted_diagnostic(untrusted_detail, limit=limit)
            safe_hint = redact_persisted_diagnostic(_XAI_ENTITLEMENT_HINT, limit=None)
            return f"{safe_detail}{safe_hint}"
        return redact_persisted_diagnostic(detail, limit=limit)

    @staticmethod
    def _coerce_api_error_detail(
        value: Any, *, _depth: int = 0, _seen: Optional[set[int]] = None,
        _nodes: Optional[list[int]] = None,
    ) -> str:
        """Return a display-safe string for structured provider error fields."""
        if _seen is None:
            _seen = set()
        if _nodes is None:
            _nodes = [0]
        if _depth > _MAX_DETAIL_DEPTH or _nodes[0] >= _MAX_DETAIL_NODES:
            return "[structured error omitted]"
        if isinstance(value, (bytes, bytearray, memoryview)):
            try:
                return bytes(value).decode("utf-8", errors="strict")
            except (TypeError, UnicodeError):
                return "[redaction-unavailable]"
        if isinstance(value, str):
            return value
        if isinstance(value, dict):
            value_id = id(value)
            if value_id in _seen:
                return "[structured error omitted]"
            _seen.add(value_id)
            _nodes[0] += 1
            try:
                for key in _ERROR_DETAIL_KEYS:
                    nested = value.get(key)
                    if isinstance(nested, str) and nested.strip():
                        return nested
                for key in _ERROR_DETAIL_KEYS:
                    if key in value:
                        nested_detail = ApiErrorSummaryMixin._coerce_api_error_detail(
                            value[key], _depth=_depth + 1, _seen=_seen, _nodes=_nodes
                        )
                        if nested_detail:
                            return nested_detail
                try:
                    return json.dumps(value, ensure_ascii=False, sort_keys=True)
                except Exception:
                    return "[structured error omitted]"
            except Exception:
                return "[structured error omitted]"
            finally:
                _seen.discard(value_id)
        if isinstance(value, (list, tuple)):
            value_id = id(value)
            if value_id in _seen:
                return "[structured error omitted]"
            _seen.add(value_id)
            _nodes[0] += 1
            try:
                parts = [
                    ApiErrorSummaryMixin._coerce_api_error_detail(
                        item, _depth=_depth + 1, _seen=_seen, _nodes=_nodes
                    )
                    for item in value[:_MAX_DETAIL_NODES]
                ]
                return "; ".join(part for part in parts if part)
            except Exception:
                return "[structured error omitted]"
            finally:
                _seen.discard(value_id)
        if value is None:
            return ""
        try:
            return str(value)
        except Exception:
            return "[structured error omitted]"

    @staticmethod
    def _summarize_api_error(error: Exception) -> str:
        try:
            status_code = getattr(error, "status_code", None)
        except Exception:
            return _SAFE_API_ERROR_SUMMARY
        if status_code is not None and (type(status_code) is not int or not 100 <= status_code <= 599):
            return _SAFE_API_ERROR_SUMMARY
        try:
            return ApiErrorSummaryMixin._summarize_api_error_unsafe(error)
        except Exception:
            return _safe_api_error_summary(error)

    @staticmethod
    def _summarize_api_error_unsafe(error: Exception) -> str:
        """Extract a human-readable one-liner from an API error.

        Cloudflare HTML pages → ``<title>``; network/DNS failures (even SDK-wrapped) → offline hint; else
        truncated str(error).
        """
        raw = str(error)

        current: Optional[BaseException] = error
        seen: set[int] = set()
        while current is not None and id(current) not in seen:
            seen.add(id(current))
            if any(marker in str(current).lower() for marker in _NETWORK_RESOLUTION_MARKERS):
                return redact_persisted_diagnostic(
                    "Hermes can't reach the model provider. You may be offline. "
                    "Check your internet connection and try again."
                )
            current = current.__cause__ or current.__context__

        if isinstance(error, ValueError) and "expected ident at line" in raw.lower():
            return redact_persisted_diagnostic(
                f"Malformed provider streaming response: {raw}", limit=300
            )

        prefix = _http_prefix(error)
        # Cloudflare / proxy HTML pages: grab the <title> (and Ray ID) for a clean summary
        if "<!DOCTYPE" in raw or "<html" in raw:
            m = re.search(r"<title[^>]*>([^<]+)</title>", raw, re.IGNORECASE)
            title = m.group(1).strip() if m else "HTML error page (title not found)"
            ray = re.search(r"Cloudflare Ray ID:\s*<strong[^>]*>([^<]+)</strong>", raw)
            parts = [prefix[:-2]] if prefix else []
            parts.append(title)
            if ray:
                parts.append(f"Ray {ray.group(1).strip()}")
            return redact_persisted_diagnostic(" — ".join(parts), limit=300)

        # GeminiAPIError already composes a clean one-liner with guidance; don't re-extract the raw body.
        if type(error).__name__ == "GeminiAPIError":
            return redact_persisted_diagnostic(raw, limit=1000)

        # JSON body errors from OpenAI/Anthropic SDKs
        body = getattr(error, "body", None)
        if isinstance(body, dict):
            msg = body.get("error", {}).get("message") if isinstance(body.get("error"), dict) else body.get("message")
            if msg:
                msg = ApiErrorSummaryMixin._coerce_api_error_detail(msg)
                detail = f"{prefix}{msg}"
                return ApiErrorSummaryMixin._redact_summary_detail(detail, limit=300)

        # SDK may leave body empty while httpx has the payload. Redact: the body is attacker-influenced
        # and may echo Authorization / x-api-key / request JSON.
        # Redact before returning: the raw provider/proxy error body is attacker-influenced and may echo
        # Authorization / x-api-key / request JSON, which would otherwise leak into final_response + logs
        # (this path widens exposure vs the old empty-body "HTTP 400" string). See #36109.
        response = getattr(error, "response", None)
        if response is not None:
            try:
                snippet = (getattr(response, "text", None) or "").strip()
            except Exception:
                snippet = ""
            if snippet:
                try:
                    payload = json.loads(snippet)
                except Exception:
                    payload = None
                if isinstance(payload, dict):
                    err = payload.get("error")
                    if isinstance(err, dict) and err.get("message"):
                        return ApiErrorSummaryMixin._redact_summary_detail(
                            f"{prefix}{ApiErrorSummaryMixin._coerce_api_error_detail(err['message'])}", limit=300
                        )
                    if payload.get("message"):
                        return ApiErrorSummaryMixin._redact_summary_detail(
                            f"{prefix}{ApiErrorSummaryMixin._coerce_api_error_detail(payload['message'])}", limit=300
                        )
                return ApiErrorSummaryMixin._redact_summary_detail(
                    f"{prefix}{snippet}", limit=300
                )

        # Fallback: redact the complete raw exception before applying the prior 500-character bound.
        return ApiErrorSummaryMixin._redact_summary_detail(f"{prefix}{raw}", limit=500)

    def _mask_api_key_for_logs(self, key: Any) -> Optional[str]:
        # Azure Foundry Entra ID bearer providers are callables — never invoke them in log
        # paths; identify the auth surface instead.
        if callable(key) and not isinstance(key, str):
            return "<entra-id-bearer>"
        if not key:
            return None
        if len(key) <= 12:
            return "***"
        return f"{key[:8]}...{key[-4:]}"

    def _clean_error_message(self, error_msg: str) -> str:
        """Clean up error messages for user display, removing HTML content and truncating."""
        if not error_msg:
            return redact_persisted_diagnostic("Unknown error")
        # HTML content is common with CloudFlare and gateway error pages
        if error_msg.strip().startswith('<!DOCTYPE html') or '<html' in error_msg:
            return redact_persisted_diagnostic("Service temporarily unavailable (HTML error page returned)")
        cleaned = redact_persisted_diagnostic(' '.join(error_msg.split()))
        if len(cleaned) > 150:
            cleaned = cleaned[:150] + "..."
        return cleaned
