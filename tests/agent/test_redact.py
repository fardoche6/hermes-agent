"""Tests for agent.redact -- secret masking in logs and output."""

import json
import logging

import pytest

import agent.redact as redact_module
from agent.redact import (
    RedactingFormatter,
    mask_secret,
    redact_cdp_url,
    redact_persisted_diagnostic,
    redact_sensitive_text,
)


def _r8_percent_encode_once(value):
    if "%" not in value:
        return "".join(f"%{byte:02X}" for byte in value.encode("utf-8"))
    return value.replace("%", "%25")


def _r8_percent_encode(value, depth):
    for _ in range(depth):
        value = _r8_percent_encode_once(value)
    return value


@pytest.fixture(autouse=True)
def _ensure_redaction_enabled(monkeypatch):
    """Ensure HERMES_REDACT_SECRETS is not disabled by prior test imports."""
    monkeypatch.delenv("HERMES_REDACT_SECRETS", raising=False)
    # Also patch the module-level snapshot so it reflects the cleared env var
    monkeypatch.setattr("agent.redact._REDACT_ENABLED", True)


class TestKnownPrefixes:




    def test_gitlab_token_prefixes(self):
        """GitLab token families redact via their literal prefixes.

        Ported from openclaw/openclaw#112954; follow-up invited in #4541.
        """
        tokens = [
            # NOTE: every token is prefix + suffix CONCATENATION so no
            # contiguous token literal exists in this file — GitHub push
            # protection blocks realistic GitLab-token-shaped literals.
            "glpat-" + "Zx9AbCdEfGhIjKlMnOpQ",       # personal access token
            "gloas-" + "a" * 64,                     # OAuth application secret
            "gldt-" + "AbCdEfGhIjKlMnOpQrSt",        # deploy token
            "glrt-" + "t1_AbCdEfGhIjKlMnOpQrSt",     # runner auth token
            "glrt-" + "A" * 27 + ".01." + "a" * 9,   # routable (dotted) runner token
            "glrtr-" + "B" * 27 + ".01." + "b" * 9,  # routable runner registration
            "glcbt-" + "a1B2_AbCdEfGhIjKlMnOpQ",     # CI/CD job token
            "glptt-" + "c" * 40,                     # pipeline trigger token
            "glft-" + "AbCdEfGhIjKlMnOp",            # feed token
            "glimt-" + "AbCdEfGhIjKlMnOpQrStUvWxY",  # incoming mail token
            "glagent-" + "d" * 50,                   # agent (KAS) token
            "glsoat-" + "AbCdEfGhIjKlMnOpQrSt",      # service-account token
            "glffct-" + "AbCdEfGhIjKlMnOpQrSt",      # feature-flags client token
            "glwt-" + "AbCdEfGhIjKlMnOpQrSt",        # workspace token
            "GR1348941" + "E" * 20,                  # legacy runner registration
        ]
        for token in tokens:
            result = redact_sensitive_text(f"leaked {token} in output")
            secret_body = token.split("-", 1)[-1] if "-" in token else token[9:]
            assert secret_body not in result, f"{token!r} survived redaction: {result!r}"

    def test_gitlab_prefix_requires_word_boundary_and_length(self):
        """Prose and embedded identifiers must not false-positive."""
        for benign in [
            "the glossary explains gitlab tokens",   # no prefix at all
            "glpat-short",                            # suffix under 10 chars
            "myglpat-AbCdEfGhIjKlMnOpQrSt",           # embedded — lookbehind blocks
        ]:
            assert redact_sensitive_text(benign) == benign

    def test_slack_token(self):
        token = "xoxb-" + "0" * 12 + "-" + "a" * 14
        result = redact_sensitive_text(token)
        assert "a" * 14 not in result





    def test_fireworks_keys(self):
        samples = [
            "fw-" + "A" * 40,
            "fw_" + "B" * 40,
            "fpk_" + "C" * 40,
        ]

        for token in samples:
            result = redact_sensitive_text(f"provider error {token}")
            assert token not in result
            assert "..." in result

    def test_short_fireworks_like_words_unchanged(self):
        text = "fw-tooshort fw_tooshort fpk_tooshort"
        assert redact_sensitive_text(text) == text




class TestEnvAssignments:
    def test_export_api_key(self):
        text = "export OPENAI_API_KEY=sk-proj-abc123def456ghi789jkl012"
        result = redact_sensitive_text(text)
        assert "OPENAI_API_KEY=" in result
        assert "abc123def456" not in result


    def test_non_secret_env_unchanged(self):
        text = "HOME=/home/user"
        result = redact_sensitive_text(text)
        assert result == text

    @pytest.mark.parametrize(
        "text",
        [
            'IDENTITY_TOKEN="bailu"',
            "--override-tensor per_layer_token_embd.weight=CPU",
            'runtime.token="local"',
            '{"token": "CPU"}',
            "token: CPU",
        ],
    )
    def test_ambiguous_key_preserves_obviously_noncredential_value(self, text):
        assert redact_sensitive_text(text, force=True) == text

    @pytest.mark.parametrize(
        "text, cleartext",
        [
            ("PASSWORD=hunter2", "hunter2"),
            ("SECRET_TOKEN=bailu", "bailu"),
            ("id_token=local", "local"),
            ("CUSTOM_TOKEN=opaqueValue123456789", "opaqueValue123456789"),
            ('{"token": "opaqueValue123456789"}', "opaqueValue123456789"),
            ('{"key_material": "CPU"}', "CPU"),
            ('{"bearer": "local"}', "local"),
            ("TOKEN=" + "sk-" + "a" * 30, "a" * 20),
        ],
    )
    def test_strong_key_or_credential_shaped_value_still_redacts(
        self, text, cleartext
    ):
        assert cleartext not in redact_sensitive_text(text, force=True)






    def test_export_whitespace_preserved(self):
        # Regression: #4367 — whitespace before uppercase env var must be preserved
        text = "export SECRET_TOKEN=mypassword"
        result = redact_sensitive_text(text)
        assert result.startswith("export ")
        assert "SECRET_TOKEN=" in result
        assert "mypassword" not in result


class TestBareSecretEnvSuffixes:
    """Bare *_KEY / *_PASS / *_PW env suffixes mask, incl. lowercase — #77484."""

    def test_upper_suffix_keys_mask(self):
        for text in ("FAL_KEY=sk-abc123def456", "OPENAI_KEY=sk-abc123def456",
                     "MYSQL_PASS=ghi789", "DB_PW=jkl012"):
            result = redact_sensitive_text(text, force=True)
            assert "=" in result and result.split("=", 1)[1] != text.split("=", 1)[1]

    def test_lowercase_env_name_masks(self):
        # Opaque value with NO known prefix: only the env-name regex can
        # catch it — a sk-/ghp_ value would be masked by _PREFIX_RE on old
        # code too, making this test false-pass (review finding).
        result = redact_sensitive_text("openai_key=xyzzyplugh1234567890abcd", force=True)
        assert "xyzzyplugh1234567890abcd" not in result

    def test_prose_words_with_keyword_unchanged(self):
        # KEYBOARD / PASSAGE embed the bare keyword but are prose, not creds
        for text in ("KEYBOARD=notsecret", "PASSAGE=notsecret"):
            result = redact_sensitive_text(text, force=True)
            assert result == text

    def test_form_body_not_swallowed(self):
        # A bare `password=`/`token=` in a form body must not be eaten greedily
        text = "password=mysecret&username=bob&token=opaqueValue"
        result = redact_sensitive_text(text, force=True)
        assert "mysecret" not in result
        assert "opaqueValue" not in result
        assert "username=bob" in result


class TestControlCharSplitTokens:
    """Tokens split by control/zero-width chars must still mask — #77484."""

    def _assert_split_masked(self, text, tok):
        # Bare token (no KEY= context). Assert the LONGEST FRAGMENT is gone
        # from the result: the token is split in the input, so `tok` (whole,
        # contiguous) is absent from ANY output — even one that leaks every
        # fragment verbatim (review finding: whole-token assertion is a false
        # negative; the tail fragment appears contiguously in the leak).
        result = redact_sensitive_text(text, force=True)
        longest = max(tok[10:], tok[:10], key=len)
        assert longest not in result

    def test_newline_split_token_masks(self):
        tok = "ghp_abcdef1234567890ABCDEF1234567890abcdef"
        self._assert_split_masked(f"{tok[:10]}\n{tok[10:]}", tok)

    def test_esc_split_token_masks(self):
        tok = "ghp_abcdef1234567890ABCDEF1234567890abcdef"
        self._assert_split_masked(f"{tok[:10]}\x1b{tok[10:]}", tok)

    def test_zero_width_split_token_masks(self):
        tok = "ghp_abcdef1234567890ABCDEF1234567890abcdef"
        self._assert_split_masked(f"{tok[:10]}\u200b{tok[10:]}", tok)

    def test_complete_token_does_not_swallow_next_line(self):
        # A COMPLETE token at end-of-line followed by ordinary text must not
        # be joined across the newline — the ordinary prefix pass masks the
        # token; joining would swallow the adjacent line (browser
        # accessibility annotations regressed this way: "button [ref=e3]"
        # disappeared into the mask).
        tok = "ghp_" + "F" * 29
        text = f"text: Token: {tok}\nbutton [ref=e3]: Copy\n"
        result = redact_sensitive_text(text, force=True)
        assert "F" * 20 not in result
        assert "button" in result
        assert "ref=e3" in result

    def test_selfmatching_head_esc_split_tail_masked(self):
        # A split where the HEAD fragment alone already matches _PREFIX_RE
        # (>= 10 body chars) but the tail doesn't: the join must still run
        # for non-newline controls, or the tail leaks in cleartext. Only
        # LINE-crossing spans skip the join (see the annotation test).
        head = "sk-" + "a" * 15
        tail = "b" * 25
        result = redact_sensitive_text(head + "\x1b" + tail, force=True)
        assert tail not in result
        assert "a" * 12 not in result

    def test_env_dump_lines_not_joined(self):
        # Control-stripping must not join unrelated env lines into one match
        env_dump = (
            "HOME=/home/user\n"
            "ELEVENLABS_API_KEY=sk_abc123def456ghi789jkl\n"
            "EXA_API_KEY=exa_XY789abcdef01234\n"
            "SHELL=/bin/bash\n"
        )
        result = redact_sensitive_text(env_dump, force=True)
        assert "SHELL=/bin/bash" in result
        assert "HOME=/home/user" in result


class TestEnvLookupPreserved:
    """Programmatic env var lookups must not be corrupted (issue #2852)."""

    def test_os_getenv_single_quote_uppercase_key(self):
        text = "MY_API_KEY=os.getenv('OPENAI_API_KEY')"
        assert redact_sensitive_text(text, force=True) == text






    def test_real_env_value_still_redacted(self):
        text = "HOMEASSISTANT_TOKEN=eyJhbGciOiJIUzI1NiJ9.abc123.xyz"
        result = redact_sensitive_text(text, force=True)
        assert "eyJhbGciOiJIUzI1NiJ9" not in result


    def test_multiline_prose_with_code_snippet(self):
        text = """Set it up like this:
    HA_TOKEN=os.getenv('HOMEASSISTANT_TOKEN')
    if not HA_TOKEN:
        raise ValueError('Missing credentials')"""
        result = redact_sensitive_text(text, force=True)
        assert "os.getenv('HOMEASSISTANT_TOKEN')" in result







class TestJsonFields:
    def test_json_api_key(self):
        text = '{"apiKey": "sk-proj-abc123def456ghi789jkl012"}'
        result = redact_sensitive_text(text)
        assert "abc123def456" not in result


    def test_json_non_secret_unchanged(self):
        text = '{"name": "John", "model": "gpt-4"}'
        result = redact_sensitive_text(text)
        assert result == text


class TestAuthHeaders:





    def test_authorization_prose_unchanged(self):
        # "authorization" without a colon-delimited value is plain prose.
        text = "the authorization model is fully open"
        assert redact_sensitive_text(text) == text

    def test_token_flush_against_double_quote_preserves_quote(self):
        # Regression for #43083: a token sitting flush against a closing
        # double quote must NOT pull that quote into the mask. Greedy \S+
        # used to eat it, turning value corruption into syntax corruption
        # (unterminated quote → shell EOF).
        text = 'curl -H "Authorization: Bearer sk-abcdef1234567890"'
        result = redact_sensitive_text(text)
        assert "sk-abcdef1234567890" not in result
        assert result.count('"') == 2, result  # both quotes survive
        assert result.endswith('"'), result



class TestApiKeyHeaders:
    def test_x_api_key_header_masked(self):
        text = "x-api-key: opaque-provider-key-1234567890"
        result = redact_sensitive_text(text)
        assert "x-api-key:" in result
        assert "opaque-provider-key" not in result

    def test_x_api_key_in_curl_command_masked(self):
        text = 'curl -H "x-api-key: sk-local-VERYsecret-999888" https://api.example.com'
        result = redact_sensitive_text(text)
        assert "VERYsecret" not in result
        assert "https://api.example.com" in result

    def test_api_key_header_masked(self):
        text = "api-key: anotherOpaqueSecret1234567"
        result = redact_sensitive_text(text)
        assert "anotherOpaqueSecret" not in result


class TestTelegramTokens:
    def test_bot_token(self):
        text = "bot123456789:ABCDEfghij-KLMNopqrst_UVWXyz12345"
        result = redact_sensitive_text(text)
        assert "ABCDEfghij" not in result
        assert "123456789:***" in result

    def test_raw_token(self):
        text = "12345678901:ABCDEfghijKLMNopqrstUVWXyz1234567890"
        result = redact_sensitive_text(text)
        assert "ABCDEfghij" not in result


class TestPassthrough:
    def test_empty_string(self):
        assert redact_sensitive_text("") == ""



    def test_non_string_input_dict_coerced_and_redacted(self):
        result = redact_sensitive_text({"token": "sk-proj-abc123def456ghi789jkl012"})
        assert "abc123def456" not in result





class TestRedactingFormatter:
    def test_formats_and_redacts(self):
        formatter = RedactingFormatter("%(message)s")
        record = logging.LogRecord(
            name="test",
            level=logging.INFO,
            pathname="",
            lineno=0,
            msg="Key is sk-proj-abc123def456ghi789jkl012",
            args=(),
            exc_info=None,
        )
        result = formatter.format(record)
        assert "abc123def456" not in result
        assert "sk-pro" in result

    def test_formats_strict_persisted_diagnostic(self):
        formatter = RedactingFormatter("%(message)s")
        record = logging.LogRecord(
            name="test",
            level=logging.INFO,
            pathname="",
            lineno=0,
            msg="GET https://api.example.test/x?token=SYN_FORMAT_QUERY&view=public",
            args=(),
            exc_info=None,
        )

        result = formatter.format(record)

        assert "SYN_FORMAT_QUERY" not in result
        assert "https://api.example.test/x?token=***&view=public" in result

    @pytest.mark.parametrize(
        ("text", "expected"),
        [
            (
                "Set-Cookie: sid=SYN_FORMAT_COOKIE; SameSite=bogus; Path=/",
                "Set-Cookie: ***",
            ),
            (
                '{"Authorization": "Bearer SYN_FORMAT_AUTH" suffix SYN_FORMAT_TAIL}',
                "[redaction-unavailable]",
            ),
            (
                '{"X-Api-Key": true, "safe": "public"}',
                '{"X-Api-Key": "***", "safe": "public"}',
            ),
        ],
    )
    def test_formats_r5_persisted_boundaries(self, text, expected):
        formatter = RedactingFormatter("%(message)s")
        record = logging.makeLogRecord({"msg": text})

        assert formatter.format(record) == expected


class TestPrintenvSimulation:
    """Simulate what happens when the agent runs `env` or `printenv`."""

    def test_full_env_dump(self):
        env_dump = """HOME=/home/user
PATH=/usr/local/bin:/usr/bin
OPENAI_API_KEY=sk-proj-abc123def456ghi789jkl012mno345
OPENROUTER_API_KEY=sk-or-v1-reallyLongSecretKeyValue12345678
FIRECRAWL_API_KEY=fc-shortkey123456789012
TELEGRAM_BOT_TOKEN=bot987654321:ABCDEfghij-KLMNopqrst_UVWXyz12345
SHELL=/bin/bash
USER=teknium"""
        result = redact_sensitive_text(env_dump)
        # Secrets should be masked
        assert "abc123def456" not in result
        assert "reallyLongSecretKey" not in result
        assert "ABCDEfghij" not in result
        # Non-secrets should survive
        assert "HOME=/home/user" in result
        assert "SHELL=/bin/bash" in result
        assert "USER=teknium" in result


class TestSecretCapturePayloadRedaction:
    def test_secret_value_field_redacted(self):
        text = '{"success": true, "secret_value": "sk-test-secret-1234567890"}'
        result = redact_sensitive_text(text)
        assert "sk-test-secret-1234567890" not in result



class TestElevenLabsTavilyExaKeys:
    """Regression tests for ElevenLabs (sk_), Tavily (tvly-), and Exa (exa_) keys."""

    def test_elevenlabs_key_redacted(self):
        text = "ELEVENLABS_API_KEY=sk_abc123def456ghi789jklmnopqrstu"
        result = redact_sensitive_text(text)
        assert "abc123def456ghi" not in result






    def test_all_three_in_env_dump(self):
        env_dump = (
            "HOME=/home/user\n"
            "ELEVENLABS_API_KEY=sk_abc123def456ghi789jklmnopqrstu\n"
            "TAVILY_API_KEY=tvly-ABCdef123456789GHIJKL0000\n"
            "EXA_API_KEY=exa_XYZ789abcdef000000000000000\n"
            "SHELL=/bin/bash\n"
        )
        result = redact_sensitive_text(env_dump)
        assert "abc123def456ghi" not in result
        assert "ABCdef123456789" not in result
        assert "XYZ789abcdef" not in result
        assert "HOME=/home/user" in result
        assert "SHELL=/bin/bash" in result


class TestJWTTokens:
    """JWT tokens start with eyJ (base64 for '{') and have dot-separated parts."""


    def test_2part_jwt(self):
        text = "eyJhbGciOiJIUzI1NiJ9.eyJzdWIiOiIxMjM0NTY3ODkwIn0"
        result = redact_sensitive_text(text)
        assert "eyJzdWIi" not in result




    def test_jwt_preserves_surrounding_text(self):
        text = "before eyJhbGciOiJIUzI1NiJ9.eyJzdWIiOiIxMjM0NTY3ODkwIn0 after"
        result = redact_sensitive_text(text)
        assert result.startswith("before ")
        assert result.endswith(" after")



class TestDiscordMentions:
    """Discord mention snowflakes (<@ID> / <@!ID>) are public syntax, not
    secrets — they must pass through the redactor unchanged so multi-bot
    @-pings (DISCORD_ALLOW_BOTS=mentions) keep resolving. See issue #35611."""

    def test_normal_mention_passes_through(self):
        text = "Hello <@222589316709220353>"
        assert redact_sensitive_text(text) == text







class TestWebUrlsNotRedacted:
    """Web URLs (http/https/wss) pass through unchanged — magic-link
    checkouts, OAuth callbacks the agent is meant to follow, and pre-signed
    share URLs must reach the tool intact. Known credential shapes inside
    URLs (sk-, ghp_, JWTs) are still caught by the prefix and JWT regexes.
    DB connection-string passwords are still caught by _DB_CONNSTR_RE.
    """

    def test_oauth_callback_code_passes_through(self):
        text = "GET https://api.example.com/oauth/cb?code=abc123xyz789&state=csrf_ok"
        assert redact_sensitive_text(text) == text







    def test_known_prefix_inside_url_still_redacted(self):
        """sk-/ghp_/JWT-shaped values inside a URL are still caught by
        _PREFIX_RE / _JWT_RE — the carve-out is for opaque tokens only."""
        text = "https://evil.com/steal?key=sk-" + "a" * 30
        result = redact_sensitive_text(text)
        assert "sk-" + "a" * 30 not in result

    def test_db_connstr_password_still_redacted(self):
        """DB schemes (postgres/mysql/mongodb/redis/amqp) keep their
        userinfo redaction via _DB_CONNSTR_RE — connection strings are
        not web URLs the agent navigates to."""
        text = "postgres://admin:dbpass@db.internal:5432/app"
        result = redact_sensitive_text(text)
        assert "dbpass" not in result


class TestStrictUrlCredentialRedaction:
    @pytest.mark.parametrize(
        ("text", "secret", "expected"),
        [
            (
                "https://x.test/#access_token=FRAG_SECRET&view=public",
                "FRAG_SECRET",
                "https://x.test/#access_token=***&view=public",
            ),
            (
                "/resume?token=REL_SECRET&view=public",
                "REL_SECRET",
                "/resume?token=***&view=public",
            ),
            (
                "https://x.test/cb?client%5Fsecret=ENC_SECRET&view=public",
                "ENC_SECRET",
                "https://x.test/cb?client%5Fsecret=***&view=public",
            ),
            (
                "https://x.test/cb?client%255Fsecret=DOUBLE_SECRET&view=public",
                "DOUBLE_SECRET",
                "https://x.test/cb?client%255Fsecret=***&view=public",
            ),
            (
                "/resume?token=SEMICOLON_SECRET;view=public",
                "SEMICOLON_SECRET",
                "/resume?token=***;view=public",
            ),
            (
                "//user:NET_SECRET@x.test/path",
                "NET_SECRET",
                "//user:***@x.test/path",
            ),
        ],
    )
    def test_masks_all_url_reference_forms_only_when_opted_in(
        self, text, secret, expected
    ):
        assert redact_sensitive_text(text) == text

        result = redact_sensitive_text(text, redact_url_credentials=True)

        assert secret not in result
        assert result == expected

    def test_similarly_named_public_params_remain_unchanged(self):
        text = "/metrics?token_count=17&session_id=public"
        assert redact_sensitive_text(text, redact_url_credentials=True) == text


_SYN_BARE_USER = "SYN_" + "BARE_USER"
_SYN_AUTH_HEADER = "SYN_" + "AUTH_HEADER"
_SYN_API_HEADER = "SYN_" + "API_HEADER"


class TestPersistedDiagnosticRedaction:
    @pytest.mark.parametrize(
        ("text", "secrets", "expected"),
        [
            (
                "https://api.example.test/x?token=SYN_QUERY_A&token=SYN_QUERY_B&view=public",
                ("SYN_QUERY_A", "SYN_QUERY_B"),
                "https://api.example.test/x?token=***&token=***&view=public",
            ),
            (
                "/x?key=SYN_SEMICOLON_A;key=SYN_SEMICOLON_B&state=ok",
                ("SYN_SEMICOLON_A", "SYN_SEMICOLON_B"),
                "/x?key=***;key=***&state=ok",
            ),
            (
                "https://api.example.test/cb?client%5Fsecret=SYN_PERCENT&view=public",
                ("SYN_PERCENT",),
                "https://api.example.test/cb?client%5Fsecret=***&view=public",
            ),
            (
                "https://api.example.test/cb?client%255Fsecret=SYN_DOUBLE_PERCENT",
                ("SYN_DOUBLE_PERCENT",),
                "https://api.example.test/cb?client%255Fsecret=***",
            ),
            (
                "https://api.example.test/cb?token=SYN%5FENCODED_VALUE&view=public",
                ("SYN%5FENCODED_VALUE",),
                "https://api.example.test/cb?token=***&view=public",
            ),
            (
                "https://user:SYN_USERINFO@api.example.test/path?view=public",
                ("SYN_USERINFO",),
                "https://user:***@api.example.test/path?view=public",
            ),
            (
                "https://" + _SYN_BARE_USER + "@api.example.test/path?view=public",
                (_SYN_BARE_USER,),
                "https://***@api.example.test/path?view=public",
            ),
            (
                "GET /x Authorization: Bearer " + _SYN_AUTH_HEADER,
                (_SYN_AUTH_HEADER,),
                "GET /x Authorization: Bearer ***",
            ),
            (
                "GET /x X-Api-Key: " + _SYN_API_HEADER,
                (_SYN_API_HEADER,),
                "GET /x X-Api-Key: ***",
            ),
            (
                "Cookie: session=SYN_COOKIE_SESSION; csrf=SYN_COOKIE_CSRF",
                ("SYN_COOKIE_SESSION", "SYN_COOKIE_CSRF"),
                "Cookie: ***",
            ),
            (
                "Set-Cookie: sid=SYN_SET_COOKIE; Path=/; HttpOnly; Secure; SameSite=Lax",
                ("SYN_SET_COOKIE",),
                "Set-Cookie: ***",
            ),
        ],
    )
    def test_redacts_http_credentials_and_preserves_safe_context(self, text, secrets, expected):
        result = redact_persisted_diagnostic(text)

        assert result == expected
        for secret in secrets:
            assert secret not in result

    def test_preserves_ordinary_web_url_query_semantics(self):
        text = "https://api.example.test/callback?token=SYN_ORDINARY_QUERY&view=public"
        assert redact_sensitive_text(text) == text
        assert "SYN_ORDINARY_QUERY" not in redact_persisted_diagnostic(text)

    def test_redacts_before_truncating_and_enforces_bound(self):
        text = (
            "status=429 https://api.example.test/v1/items?token=SYN_BOUND&limit=20 "
            + ("safe-context " * 30)
            + "tail-context"
        )

        result = redact_persisted_diagnostic(text, limit=96)

        assert len(result) <= 96
        assert "SYN_BOUND" not in result
        assert "api.example.test" in result
        assert "tail-context" in result

    def test_redaction_failure_returns_fixed_safe_marker(self, monkeypatch):
        def _fail(*_args, **_kwargs):
            raise RuntimeError("SYN_REDACTOR_ERROR")

        monkeypatch.setattr("agent.redact.redact_sensitive_text", _fail)
        raw = "https://api.example.test/x?token=SYN_FAILURE"

        result = redact_persisted_diagnostic(raw)

        assert result == "[redaction-unavailable]"
        assert "SYN_FAILURE" not in result
        assert "SYN_REDACTOR_ERROR" not in result


class TestBareTokenUserinfoRedaction:
    """Regression tests for #6396 — a bare credential in URL userinfo
    (``scheme://TOKEN@host``, no ``user:pass`` colon) is redacted. This is the
    git-remote-with-embedded-password shape. The colon form ``user:pass@`` and
    query-string tokens are deliberately left to pass through (#34029) so
    magic-link / OAuth round-trip skills keep working — see
    TestWebUrlsNotRedacted for those invariants.
    """

    def test_git_remote_bare_password_redacted(self):
        """Exact bug scenario: password in a git remote URL."""
        text = (
            "git remote set-url origin "
            "https://MYPASSWORDWASDISLAYEDHERE@github.com/unclehowell/FCUK.git"
        )
        result = redact_sensitive_text(text)
        assert "MYPASSWORDWASDISLAYEDHERE" not in result
        assert "@github.com" in result
        assert "unclehowell/FCUK.git" in result

    def test_ssh_bare_token_redacted(self):
        text = "ssh://longtoken1234567@gitlab.com/project.git"
        result = redact_sensitive_text(text)
        assert "longtoken1234567" not in result
        assert "@gitlab.com" in result

    def test_ftp_bare_token_redacted(self):
        text = "ftp://ftptoken123456@ftp.example.com/files"
        result = redact_sensitive_text(text)
        assert "ftptoken123456" not in result


    def test_user_pass_form_still_passes_through(self):
        """The ``user:pass@`` colon form must NOT be redacted (#34029)."""
        text = "URL: https://user:supersecretpw@host.example.com/path"
        assert redact_sensitive_text(text) == text

    def test_short_username_not_redacted(self):
        """Short userinfo (git, admin, deploy) below the 8-char floor passes."""
        for text in (
            "https://git@github.com/user/repo.git",
            "https://admin@example.com/x",
            "https://deploy@host.com/y",
        ):
            assert redact_sensitive_text(text) == text

    def test_email_in_path_not_redacted(self):
        """An ``@`` in a path/query is not userinfo — the token class stops at
        ``/``, so emails after the first slash are never treated as a credential."""
        for text in (
            "https://example.com/search?q=user@example.com",
            "https://example.com/users/john@doe.com/profile",
        ):
            assert redact_sensitive_text(text) == text




class TestFormBodyRedaction:
    """Form-urlencoded body redaction (k=v&k=v with no other text)."""

    def test_pure_form_body(self):
        text = "password=mysecret&username=bob&token=opaqueValue"
        result = redact_sensitive_text(text)
        assert "mysecret" not in result
        assert "opaqueValue" not in result
        assert "username=bob" in result


    def test_non_form_text_unchanged(self):
        """Sentences with `&` should NOT trigger form redaction."""
        text = "I have password=foo and other things"  # contains spaces
        result = redact_sensitive_text(text)
        # The space breaks the form regex; passthrough expected.
        assert "I have" in result

    def test_multiline_text_not_form(self):
        """Multi-line text is never treated as form body."""
        text = "first=1\nsecond=2"
        # Should pass through (still subject to other redactors)
        assert "first=1" in redact_sensitive_text(text)


class TestLowercaseDottedConfigKeys:
    """Issue #16413 — config-file passwords in lowercase/dotted/colon keys
    must be redacted. The uppercase _ENV_ASSIGN_RE missed these, leaking
    `spring.datasource.password=...` and `password: ...` from `cat`'d config
    files. Carve-outs: prose, code (#4367), and web URLs are left untouched.
    """







    def test_properties_file_dump(self):
        text = (
            "server.port=8080\n"
            "spring.datasource.username=admin\n"
            "spring.datasource.password=Sup3rS3cret!\n"
            "logging.level.root=INFO"
        )
        result = redact_sensitive_text(text)
        assert "Sup3rS3cret!" not in result
        assert "server.port=8080" in result  # non-secret keys preserved
        assert "username=admin" in result

    # --- carve-outs: must NOT redact ---

    def test_prose_mid_sentence_password_unchanged(self):
        # Not line-anchored, not dotted → conversational text, leave alone.
        text = "I have password=foo and other things"
        assert redact_sensitive_text(text) == text





class TestConfigKeyRedosResistance:
    """The dotted-key patterns must not backtrack exponentially (ReDoS).

    Before the possessive-quantifier rewrite, a non-matching run of ~40
    dotted segments took ~30ms and doubled every ~4 segments; 100 segments
    would effectively hang the redactor (it runs on every log line).
    """

    def test_long_dotted_run_completes_fast(self):
        import time

        # 100 dotted segments with no '=' — worst case for the old pattern.
        text = ".".join(["segment"] * 100) + " end"
        t0 = time.perf_counter()
        assert redact_sensitive_text(text) == text
        assert time.perf_counter() - t0 < 2.0

    def test_long_dotted_run_with_keyword_completes_fast(self):
        """Exercise _CFG_DOTTED_RE directly (bypasses the keyword pre-gate).

        The pre-gate skips the regex when no secret keyword is present, so
        test_long_dotted_run_completes_fast only guards the pre-gate.  This
        test includes a keyword but no '=' so the regex runs and must still
        complete quickly thanks to the possessive quantifiers.
        """
        import time

        text = ".".join(["segment"] * 100) + ".token end"
        t0 = time.perf_counter()
        assert redact_sensitive_text(text) == text
        assert time.perf_counter() - t0 < 2.0

    def test_long_dotted_secret_still_redacted(self):
        # Possessive quantifiers must not change matching behavior.
        text = ".".join(["seg"] * 50) + ".password=Sup3rS3cret!"
        result = redact_sensitive_text(text)
        assert "Sup3rS3cret!" not in result
        assert ".password=" in result

    def test_long_opaque_assignment_run_completes_fast(self):
        """Lowercase env scanning stays linear on compaction payload blobs."""
        import time

        # A serialized tool payload can contain a long opaque alphanumeric
        # value followed by '=' without containing a secret-key suffix.  The
        # old unanchored lowercase-env pattern retried its greedy prefix from
        # every byte, making this quadratic while holding the GIL.
        text = "a" * 20_000 + "=value"
        t0 = time.perf_counter()
        assert redact_sensitive_text(text, force=True) == text
        assert time.perf_counter() - t0 < 2.0

    def test_dotted_cfg_scan_stays_linear_with_keyword_elsewhere(self):
        """_CFG_DOTTED_RE must stay linear once the pre-gate passes.

        The ``_CFG_SECRET_WORD_RE`` pre-gate only skips secret-FREE text, so a
        payload that contains a real secret assignment AND a long opaque
        dotted run still reaches the backtrackable ``*`` prefix. Without the
        run-start lookbehind the sub retries that prefix from every byte of
        the run (quadratic while holding the GIL).
        """
        import time

        text = "password=hunter2\n" + "a." * 15_000 + "=value"
        t0 = time.perf_counter()
        result = redact_sensitive_text(text, force=True)
        assert "hunter2" not in result
        assert time.perf_counter() - t0 < 2.0

    def test_yaml_assign_redos_resistance(self):
        """_YAML_ASSIGN_RE must not backtrack excessively on long inputs."""
        import time

        # 100 lines of a long dotted key with a secret keyword but no
        # matching colon-value form — stresses the regex without matching.
        line = "a." * 50 + "token not_an_assignment"
        text = "\n".join([line] * 100)
        t0 = time.perf_counter()
        redact_sensitive_text(text)
        assert time.perf_counter() - t0 < 2.0

    def test_yaml_assign_secret_still_redacted(self):
        # Possessive quantifiers must not change YAML matching behavior.
        text = "spring.datasource.password: hunter2"
        result = redact_sensitive_text(text)
        assert "hunter2" not in result
        assert "password:" in result


class TestXaiToken:
    KEY = "xai-ABCDEFGHIJKLMNOPQRSTUVWXYZ0123456789abcdefghijklmnopqrstu"

    def test_bare_token_masked(self):
        result = redact_sensitive_text(f"using key {self.KEY}", force=True)
        assert self.KEY not in result
        assert "xai-AB" in result


    def test_too_short_not_masked(self):
        short = "xai-tooshort"
        result = redact_sensitive_text(f"text {short} here", force=True)
        assert short in result




class TestDbConnstrCodeOutput:
    """Regression tests for issue #33801 — _DB_CONNSTR_RE corrupting code output.

    Two distinct flaws, both confined to displayed tool OUTPUT (read_file /
    terminal / execute_code), never the on-disk content:

    1. The password group ``[^@]+`` was greedy across newlines, so on a
       multi-line block it scanned past the DSN line to the next stray ``@``
       (e.g. a Python ``@decorator``), replacing everything in between with
       ``***`` — dropping lines and concatenating the next one.
    2. An f-string DSN template (``f"postgresql://{user}:{pass}@{host}"``) is
       not a live credential, but was redacted anyway. Under ``code_file=True``
       a pure ``{...}`` brace password is now preserved.
    """

    MULTILINE = (
        '            return f"postgresql://{auth}@{self.pg_host}:'
        '{self.pg_port}/{self.pg_database}"\n'
        "\n"
        '    @model_validator(mode="after")\n'
        '    def _validate_critical_settings(self) -> "Settings":'
    )





    def test_literal_connstr_still_redacted_with_code_file(self):
        """A real password in a literal DSN is still masked under code_file."""
        text = "postgresql://admin:realpassword@db.internal:5432/app"
        result = redact_sensitive_text(text, code_file=True, force=True)
        assert "realpassword" not in result
        assert "***" in result

    def test_literal_connstr_redacted_all_schemes(self):
        for scheme, secret in [
            ("postgres", "pgsecret1234"),
            ("mysql", "mysqlsecret99"),
            ("redis", "redissecret77"),
            ("mongodb+srv", "mongosecret55"),
            ("amqp", "amqpsecret33"),
        ]:
            text = f"{scheme}://user:{secret}@host:1234/db"
            result = redact_sensitive_text(text, code_file=True, force=True)
            assert secret not in result, scheme

    def test_literal_connstr_in_log_line_redacted(self):
        text = "connected via postgres://user:s3cr3tpw@host:5432/db ok"
        result = redact_sensitive_text(text, force=True)
        assert "s3cr3tpw" not in result


class TestTerminalOutputRedaction:
    """is_env_dump_command + redact_terminal_output — issue #43025.

    Terminal/process stdout must be redacted on every surface (foreground
    `terminal` AND background `process(poll/log/wait)`). Env-dump commands
    and commands that read ``.env`` files get the ENV-assignment pass so
    opaque tokens (no vendor prefix) are masked; other commands stay on
    the code_file path to avoid false positives.
    """

    def test_is_env_dump_command_detection(self):
        from agent.redact import is_env_dump_command
        assert is_env_dump_command("printenv")
        assert is_env_dump_command("env")
        assert is_env_dump_command("env | grep API")
        assert is_env_dump_command("set")
        assert is_env_dump_command("export")
        assert is_env_dump_command("declare -x")
        assert is_env_dump_command("cat /tmp/x && printenv")
        assert not is_env_dump_command("python app.py")
        assert not is_env_dump_command("cat config.py")
        assert not is_env_dump_command("printf 'TOKEN=x'")
        assert not is_env_dump_command("")
        assert not is_env_dump_command(None)

    # ── .env file detection (issue #61352 v2) ──

    def test_command_reads_env_file_detection(self):
        from agent.redact import _command_reads_env_file
        # Basic detection
        assert _command_reads_env_file("cat .env")
        assert _command_reads_env_file("cat .env.local")
        assert _command_reads_env_file("cat .env.production")
        assert _command_reads_env_file("cat .envrc")
        assert _command_reads_env_file("head .env")
        assert _command_reads_env_file("tail .env")
        assert _command_reads_env_file("type .env")
        assert _command_reads_env_file("nl .env")
        assert _command_reads_env_file("bat .env")
        # With flags
        assert _command_reads_env_file("cat -n .env")
        assert _command_reads_env_file("cat -A .env")
        # With paths
        assert _command_reads_env_file("cat ~/.hermes/.env")
        assert _command_reads_env_file("cat /home/user/project/.env")
        assert _command_reads_env_file("cat ./config/.env.local")
        # In a pipeline / sequence
        assert _command_reads_env_file("cat .env | grep KEY")
        assert _command_reads_env_file("echo '---' && cat .env")
        # Windows-style backslash paths
        assert _command_reads_env_file("cat C:\\Users\\test\\.env")
        # Quoted paths (plain split leaves the quotes attached)
        assert _command_reads_env_file('cat ".env"')
        assert _command_reads_env_file("cat '.env'")
        # Case-insensitive basename (macOS/Windows filesystems)
        assert _command_reads_env_file("cat .ENV")

    def test_command_reads_env_file_excludes_templates(self):
        from agent.redact import _command_reads_env_file
        # Templates/examples should NOT trigger
        assert not _command_reads_env_file("cat .env.example")
        assert not _command_reads_env_file("cat .env.sample")
        assert not _command_reads_env_file("cat .env.template")
        assert not _command_reads_env_file("cat .env.dist")

    def test_command_reads_env_file_rejects_non_env_files(self):
        from agent.redact import _command_reads_env_file
        assert not _command_reads_env_file("cat config.py")
        assert not _command_reads_env_file("cat README.md")
        assert not _command_reads_env_file("cat .envrc.bak")  # .bak not in list
        assert not _command_reads_env_file("python app.py")
        assert not _command_reads_env_file("echo .env")  # echo is not a file-read cmd
        assert not _command_reads_env_file("")
        assert not _command_reads_env_file(None)

    def test_cat_env_file_masks_opaque_token(self):
        """cat .env → code_file=False → generic ENV pass redacts opaque keys."""
        from agent.redact import redact_terminal_output
        out = (
            "MISTRAL_API_KEY=abc123opaqueSecretValue\n"
            "NOUS_API_KEY=xyz789opaqueKey\n"
            "DEBUG=true\n"
        )
        red = redact_terminal_output(out, "cat .env")
        assert "abc123opaqueSecretValue" not in red
        assert "xyz789opaqueKey" not in red
        assert "DEBUG=true" in red  # non-secret key preserved

    def test_cat_env_file_with_flags_masks_opaque_token(self):
        """cat -n .env → still detected as .env read."""
        from agent.redact import redact_terminal_output
        out = "     1\tMISTRAL_API_KEY=abc123opaqueSecretValue\n"
        red = redact_terminal_output(out, "cat -n .env")
        assert "abc123opaqueSecretValue" not in red

    def test_cat_env_file_in_pipeline_masks_opaque_token(self):
        """cat .env | grep KEY → still detected as .env read."""
        from agent.redact import redact_terminal_output
        out = "MISTRAL_API_KEY=abc123opaqueSecretValue"
        red = redact_terminal_output(out, "cat .env | grep MISTRAL")
        assert "abc123opaqueSecretValue" not in red

    def test_cat_env_example_not_redacted_as_env(self):
        """cat .env.example → NOT treated as .env read (template file)."""
        from agent.redact import redact_terminal_output
        out = "MISTRAL_API_KEY=placeholder_value_here"
        red = redact_terminal_output(out, "cat .env.example")
        # Should NOT be redacted by the ENV-assignment pass (code_file=True).
        # The placeholder value should survive since it has no vendor prefix.
        assert "placeholder_value_here" in red

    def test_cat_env_local_masks_opaque_token(self):
        """cat .env.local → detected as .env read."""
        from agent.redact import redact_terminal_output
        out = "CUSTOM_API_KEY=opaquecustomkey123456"
        red = redact_terminal_output(out, "cat .env.local")
        assert "opaquecustomkey123456" not in red

    def test_cat_env_inline_comment_preserved(self):
        """Inline comments after env values are preserved (issue #61352 review)."""
        from agent.redact import redact_terminal_output
        out = "MISTRAL_API_KEY=abc123secret # used for tests"
        red = redact_terminal_output(out, "cat .env")
        assert "abc123secret" not in red
        assert "MISTRAL_API_KEY=*** # used for tests" in red

    def test_cat_env_export_inline_comment_preserved(self):
        """export KEY=VALUE # comment — comment preserved."""
        from agent.redact import redact_terminal_output
        out = "export MISTRAL_API_KEY=abc123secret # prod key"
        red = redact_terminal_output(out, "cat .env")
        assert "abc123secret" not in red
        assert "export MISTRAL_API_KEY=*** # prod key" in red



    def test_disabled_passes_through(self, monkeypatch):
        from agent.redact import redact_terminal_output
        monkeypatch.setattr("agent.redact._REDACT_ENABLED", False)
        out = "CUSTOM_TOKEN=zzzopaque1234567890abcdef"
        red = redact_terminal_output(out, "printenv")
        assert "zzzopaque1234567890abcdef" in red


class TestFileReadNonReusableRedaction:
    """#35519: prefix-matched credentials in FILE CONTENT (read_file /
    search_files / cat) must be redacted to a NON-REUSABLE sentinel — not a
    head/tail mask that looks like a real-but-truncated key and gets written
    back to config (corrupting the credential -> 401)."""

    GHP = "ghp_S1abcdefghijklmnopqrstuvwxyz0Pn2T"  # realistic GitHub PAT shape
    SK = "sk-proj-abcdefghijklmnopqrstuvwxyz0123456789"


    def test_file_read_does_not_leak_secret_body(self):
        """Crucial: file_read must NOT expose the real key (no un-redact)."""
        out = redact_sensitive_text(f"token: {self.GHP}", force=True, file_read=True)
        # No run of the secret body survives.
        assert "S1abcdefghij" not in out
        assert self.GHP not in out
        assert "Pn2T" not in out  # not even the tail (the old mask kept it)

    def test_file_read_sentinel_is_not_a_plausible_key(self):
        """The sentinel can't be mistaken for / written back as a usable key:
        the old mask was a 13-char `ghp_S1...Pn2T` that broke GitHub auth when
        an agent re-saved it. The sentinel is syntactically invalid as a token
        (contains « » … and ':'), so it can't round-trip into a dead key."""
        out = redact_sensitive_text(f"GITHUB_PERSONAL_ACCESS_TOKEN: {self.GHP}",
                                    force=True, file_read=True)
        masked = out.split(": ", 1)[1].strip()
        # Not a bare token: contains the sentinel delimiters.
        assert masked.startswith("«") and masked.endswith("»")
        assert "…" in masked





class TestFireworksToken:
    KEY = "fw_" + "A" * 40

    def test_bare_token_masked(self):
        result = redact_sensitive_text(f"fireworks error: key {self.KEY}", force=True)
        assert self.KEY not in result
        assert "fw_AA" in result


    def test_too_short_not_masked(self):
        short = "fw_tooshort"
        result = redact_sensitive_text(f"text {short} here", force=True)
        assert short in result



class TestRedactCdpUrl:
    """redact_cdp_url() is the single chokepoint for CDP endpoint log redaction.

    Unlike the global pass (which deliberately lets web-URL query params and
    userinfo through for OAuth/magic-link workflows), CDP endpoint credentials
    are pure secrets and must always be masked. Both the browser tool's
    session/discovery logs and the supervisor's attach-timeout error route
    through this helper.
    """


    def test_masks_multiple_query_credentials(self):
        url = "wss://provider.example/session?token=aaa-secret&apikey=bbb-secret"
        out = redact_cdp_url(url)
        assert "aaa-secret" not in out
        assert "bbb-secret" not in out




    def test_none_returns_empty(self):
        assert redact_cdp_url(None) == ""


class TestKeywordWordBoundary:
    """Ported from nearai/ironclaw#6129 — a secret keyword embedded inside a
    larger prose word (``Secretary`` ⊃ ``secret``, ``tokenizer`` ⊃ ``token``,
    ``authored`` ⊃ ``auth``) must NOT trigger the lowercase/dotted/YAML config
    passes. Real key shapes (separators, camelCase, acronyms, plurals, common
    concatenated compounds, all-caps env style) must keep redacting.
    """

    # ── prose words embedding a keyword are preserved ──────────────────

    def test_secretary_yaml_value_preserved(self):
        text = "Secretary: JanetYellen1234567890"
        assert redact_sensitive_text(text) == text








    # ── real key shapes still redact ────────────────────────────────────

    def test_separator_keys_still_redacted(self):
        for text in (
            "client_secret: abc123def456ghi789jkl",
            "auth_token: xyz789xyz789xyz789xyz",
            "my_secret: topvalue123456789012345",
            "db.password=hunter2verylongpassword",
        ):
            result = redact_sensitive_text(text)
            assert result != text, text

    def test_camelcase_keys_still_redacted(self):
        for text in (
            "clientSecret: abc123def456ghi789jkl",
            "secretKey: abc123def456ghi789jklmno",
            "APIToken: abc123def456ghi789jklmn",
        ):
            result = redact_sensitive_text(text)
            assert result != text, text


    def test_plural_keys_still_redacted(self):
        text = "secrets: hunter2hunter2hunter2hh"
        result = redact_sensitive_text(text)
        assert "hunter2hunter2hunter2hh" not in result


class TestMaskSecretControlStripping:
    """Issue #55319/#55321: mask_secret() must not emit control bytes
    (newline, NUL, DEL, C1) in the visible head/tail of a masked secret —
    they corrupt config/status/dump display output."""

    def test_newline_stripped_from_mask(self):
        # The #55319 probe: a newline inside the preserved head.
        assert mask_secret("ab\ncd0123456789zzzz") == "abcd...zzzz"

    def test_c1_control_stripped_from_mask(self):
        assert mask_secret("abcd0123456789zz\x85q") == "abcd...9zzq"

    def test_printable_mask_unchanged(self):
        assert mask_secret("abcdef0123456789zzzz") == "abcd...zzzz"

    def test_all_control_value_returns_empty_fallback(self):
        assert mask_secret("\n\x85\u200b") == ""
        assert mask_secret("\n\x85\u200b", empty="(not set)") == "(not set)"


class TestValueAwareGatingCorpus:
    """Issue #96607: corpus-level before/after for value-aware gating.

    Redaction must mask a keyword-named assignment ONLY when the value has
    credential shape (vendor prefix, hex/base64/high-entropy, or a strong
    credential-specific key name). Bare technical vocabulary — ``token``,
    ``key``, ``cpu`` — in ordinary technical prose/config must pass through
    byte-for-byte, on every assignment family (ENV, dotted config, JSON,
    YAML).
    """

    # Realistic technical prose. On pre-fix main every line was corrupted
    # to ``***`` despite containing no secret.
    TECHNICAL_CORPUS = [
        'IDENTITY_TOKEN="bailu"',
        "--override-tensor per_layer_token_embd.weight=CPU",
        "MAX_TOKENS=4096",
        "runtime.token=local",
        "The tokenizer splits on whitespace; set max_new_tokens=256.",
        "num_key_value_heads=8",
        "token: CPU",
        "llm_load_tensors: per_layer_token_embd.weight=CPU buffer",
    ]

    # Obviously-fake but shape-realistic secrets: every one of these must
    # STAY masked after the gating change (fail-closed on credential shape
    # or strong key names).
    FAKE_SECRET_CORPUS = [
        ("API_KEY=sk-fakefakefakefakefake1234567890abcd", "fakefake"),
        ("GITHUB_TOKEN=ghp_FAKEfakeFAKEfake1234567890fake", "FAKEfake"),
        ("MY_SERVICE_TOKEN=A9f3kZq7Lm2Xw8Rt4Yv6", "A9f3kZq7"),
        ("TOKEN=6f1d2a9c8b3e4f5a6d7c8b9a0e1f2d3c", "6f1d2a9c"),
        ("password=hunter2", "hunter2"),
        ("db_password: hunter2", "hunter2"),
        ("auth_token: 9f8e7d6c5b4a39281706f5e4d3c2b1a0", "9f8e7d6c"),
        ('"token": "Zx9Qw8Er7Ty6Ui5Op4As3"', "Zx9Qw8Er"),
        ("SESSION_TOKEN=shrt", "shrt"),
        ("client_secret=abc", "abc"),
        ("spring.datasource.password=fakePass123", "fakePass123"),
    ]

    def test_technical_prose_survives_intact(self):
        for line in self.TECHNICAL_CORPUS:
            assert redact_sensitive_text(line, force=True) == line, line

    def test_technical_corpus_as_one_block_survives_intact(self):
        # The multi-line shape a model actually reads from tool output.
        block = "\n".join(self.TECHNICAL_CORPUS)
        assert redact_sensitive_text(block, force=True) == block

    def test_shape_realistic_fake_secrets_still_masked(self):
        for line, cleartext in self.FAKE_SECRET_CORPUS:
            result = redact_sensitive_text(line, force=True)
            assert result != line, line
            assert cleartext not in result, line

    def test_mixed_block_masks_only_the_secret_lines(self):
        # Precondition guard: both halves must actually exercise the gate.
        secret_line = "MY_SERVICE_TOKEN=A9f3kZq7Lm2Xw8Rt4Yv6"
        prose_line = 'IDENTITY_TOKEN="bailu"'
        block = f"{prose_line}\n{secret_line}"
        result = redact_sensitive_text(block, force=True)
        assert prose_line in result
        assert "A9f3kZq7Lm2Xw8Rt4Yv6" not in result


class TestPersistedHttpCredentialCorrection:
    @pytest.mark.parametrize(
        ("text", "expected"),
        [
            (
                "Authorization: Bearer " + "SYN_STRUCT_AUTH_PLAIN",
                "Authorization: Bearer ***",
            ),
            (
                '{"Authorization": "Bearer ' + "SYN_STRUCT_AUTH_JSON" + '"}',
                '{"Authorization": "Bearer ***"}',
            ),
            (
                "{'authorization': 'Bearer " + "SYN_STRUCT_AUTH_SINGLE" + "'}",
                "{'authorization': 'Bearer ***'}",
            ),
            (
                '{\\"Authorization\\": \\"Bearer '
                + "SYN_STRUCT_AUTH_ESCAPED"
                + '\\"}',
                '{\\"Authorization\\": \\"Bearer ***\\"}',
            ),
            (
                '{\\\\\\"x-api-key\\\\\\": \\\\\\"'
                + "SYN_STRUCT_API_ESCAPED"
                + '\\\\\\"}',
                '{\\\\\\"x-api-key\\\\\\": \\\\\\"***\\\\\\"}',
            ),
            (
                '"X-Api-Key": "' + "SYN_STRUCT_API_JSON" + '"',
                '"X-Api-Key": "***"',
            ),
        ],
    )
    def test_persisted_structured_authorization_and_api_keys(self, text, expected):
        result = redact_persisted_diagnostic(text)
        assert result == expected

    def test_ordinary_authorization_and_api_key_keep_parent_backslash_semantics(self):
        text = (
            "Authorization: Bearer ***\\SYN_ORDINARY_AUTH_SUFFIX "
            "X-Api-Key: ***\\SYN_ORDINARY_API_SUFFIX"
        )
        assert redact_sensitive_text(text, force=True, code_file=True) == (
            "Authorization: Bearer ***\\SY...FFIX X-Api-Key: ***\\SY...FFIX"
        )

    @pytest.mark.parametrize(
        "text",
        [
            "Cookie: session=SYN_COOKIE_ONE; csrf=SYN_COOKIE_TWO; refresh=SYN_COOKIE_THREE",
            "Cookie: session=\"SYN_COOKIE_QUOTED\"; csrf=SYN_COOKIE_QUOTED_TWO",
            "Set-Cookie: sid=SYN_SET_ONE; refresh=SYN_SET_TWO; Path=/; Domain=example.test; "
            "Expires=Wed, 21 Oct 2015 07:28:00 GMT; Max-Age=3600; SameSite=Lax; Secure; HttpOnly",
            "Set-Cookie: sid=SYN_SET_COMBINED_ONE; Path=/, refresh=SYN_SET_COMBINED_TWO; Secure",
            "Set-Cookie: sid=SYN_SET_UNSAFE_ONE; Priority=SYN_SET_UNSAFE_ATTRIBUTE; Path=/",
            '{"Cookie": ["a=SYN_COOKIE_ARRAY_ONE; b=SYN_COOKIE_ARRAY_TWO", '
            '"c=SYN_COOKIE_ARRAY_THREE"], "Set-Cookie": ["sid=SYN_SET_ARRAY"]}',
            "{\\\"Cookie\\\": \\\"session=SYN_COOKIE_ESCAPED; csrf=SYN_CSRF_ESCAPED\\\"}",
        ],
    )
    def test_persisted_cookie_values_are_all_redacted(self, text):
        result = redact_persisted_diagnostic(text)
        for marker in (
            "SYN_COOKIE_ONE", "SYN_COOKIE_TWO", "SYN_COOKIE_THREE",
            "SYN_COOKIE_QUOTED", "SYN_COOKIE_QUOTED_TWO",
            "SYN_SET_ONE", "SYN_SET_TWO", "SYN_SET_COMBINED_ONE", "SYN_SET_COMBINED_TWO",
            "SYN_SET_UNSAFE_ONE", "SYN_SET_UNSAFE_ATTRIBUTE",
            "SYN_COOKIE_ARRAY_ONE", "SYN_COOKIE_ARRAY_TWO", "SYN_COOKIE_ARRAY_THREE",
            "SYN_SET_ARRAY", "SYN_COOKIE_ESCAPED", "SYN_CSRF_ESCAPED",
        ):
            assert marker not in result
    def test_strict_userinfo_masks_through_last_authority_at(self):
        text = "https://user:SYN_USERINFO_LEFT@SYN_USERINFO_RIGHT@api.example.test/path?view=public"
        result = redact_sensitive_text(text, force=True, redact_url_credentials=True)
        assert "SYN_USERINFO_LEFT" not in result
        assert "SYN_USERINFO_RIGHT" not in result
        assert result == "https://user:***@api.example.test/path?view=public"

    def test_persisted_redaction_is_idempotent(self):
        text = (
            '{"Authorization": "Bearer SYN_IDEMPOTENT_AUTH", '
            '"Cookie": ["sid=SYN_IDEMPOTENT_COOKIE; csrf=SYN_IDEMPOTENT_CSRF"], '
            '"url": "https://api.example.test/x?token=SYN_IDEMPOTENT_QUERY"}'
        )
        first = redact_persisted_diagnostic(text)
        assert redact_persisted_diagnostic(first) == first

    def test_non_ascii_prefix_keeps_structured_spans_aligned_on_helper_and_formatter(self):
        text = (
            "synthetic prefix İ before fields "
            + json.dumps({
                "Authorization": "Bearer SYN_NONASCII_AUTH",
                "X-Api-Key": "SYN_NONASCII_API",
                "Cookie": "sid=SYN_NONASCII_COOKIE",
                "Set-Cookie": "sid=SYN_NONASCII_SET; Path=/; Secure; HttpOnly",
                "safe": "public",
            }, ensure_ascii=False)
        )

        result = redact_persisted_diagnostic(text, limit=None)
        formatted = RedactingFormatter().format(logging.makeLogRecord({"msg": text}))

        for marker in (
            "SYN_NONASCII_AUTH", "SYN_NONASCII_API", "SYN_NONASCII_COOKIE", "SYN_NONASCII_SET",
        ):
            assert marker not in result
            assert marker not in formatted
        assert '"safe": "public"' in result
        assert '"Set-Cookie": "***"' in result

    @pytest.mark.parametrize(
        ("text", "expected"),
        [
            ("X-Api-Key:   SYN_KIND_API   ", "X-Api-Key:   ***"),
            ("Authorization:\tBearer SYN_KIND_AUTH   ", "Authorization:\tBearer ***"),
            ("Authorization:\tSYN_KIND_BARE   ", "Authorization:\t***"),
        ],
    )
    def test_structured_field_kind_controls_masking_and_scheme_preservation(self, text, expected):
        assert redact_persisted_diagnostic(text, limit=None) == expected

    @pytest.mark.parametrize(
        ("field_name", "value", "marker"),
        [
            ("Authorization", ["Bearer SYN_ESCAPED_AUTH\\"], "SYN_ESCAPED_AUTH"),
            ("X-Api-Key", ["SYN_ESCAPED_API\\"], "SYN_ESCAPED_API"),
            ("Cookie", ["sid=SYN_ESCAPED_COOKIE\\"], "SYN_ESCAPED_COOKIE"),
            ("Set-Cookie", ["sid=SYN_ESCAPED_SET\\"], "SYN_ESCAPED_SET"),
        ],
    )
    def test_structured_lists_ending_in_escaped_backslashes_are_redacted(self, field_name, value, marker):
        text = json.dumps({field_name: value}, ensure_ascii=False)
        result = redact_persisted_diagnostic(text, limit=None)
        assert marker not in result
        assert "***" in result

    def test_uncertain_structured_boundaries_fail_closed_for_the_whole_diagnostic(self):
        text = '{"Authorization": ["Bearer SYN_MALFORMED_AUTH", "tail": "SYN_MALFORMED_TAIL"}'

        result = redact_persisted_diagnostic(text, limit=None)

        assert result == "[redaction-unavailable]"
        assert "SYN_MALFORMED_AUTH" not in result
        assert "SYN_MALFORMED_TAIL" not in result

    def test_repeated_structured_fields_have_a_deterministic_bound_without_timing_assumptions(self):
        text = "\n".join(
            "Authorization: Bearer SYN_REPEAT_%04d" % index for index in range(600)
        )

        result = redact_persisted_diagnostic(text, limit=128)

        assert len(result) <= 128
        assert "SYN_REPEAT_0000" not in result
        assert "SYN_REPEAT_0599" not in result
        assert redact_persisted_diagnostic(result, limit=128) == result

    def test_truncated_structured_output_is_stable_on_a_second_pass(self):
        text = (
            '{"Authorization": "Bearer SYN_TRUNCATED_AUTH", '
            '"Cookie": ["sid=SYN_TRUNCATED_COOKIE"], '
            '"padding": "' + ("x" * 100) + '"}'
        )

        first = redact_persisted_diagnostic(text, limit=46)

        assert len(first) <= 46
        assert "SYN_TRUNCATED_AUTH" not in first
        assert "SYN_TRUNCATED_COOKIE" not in first
        assert redact_persisted_diagnostic(first, limit=46) == first

    @pytest.mark.parametrize(
        "field_name",
        [
            "authorization", "Proxy-Authorization", "x-api-key", "X-Goog-Api-Key",
            "api-key", "Apikey", "X-Api-Token", "X-Auth-Token", "X-Access-Token",
        ],
    )
    def test_all_persisted_credential_aliases_mask_scalar_and_non_scalar_values(self, field_name):
        scalar_marker = "SYN_R4_ALIAS_SCALAR_" + field_name.replace("-", "_")
        object_marker = "SYN_R4_ALIAS_OBJECT_" + field_name.replace("-", "_")

        scalar_result = redact_persisted_diagnostic(
            json.dumps({field_name: scalar_marker}), limit=None
        )
        non_scalar_result = redact_persisted_diagnostic(
            json.dumps({field_name: {"nested": object_marker}}), limit=None
        )
        numeric_result = redact_persisted_diagnostic(
            json.dumps({field_name: [8462057319]}), limit=None
        )

        assert scalar_marker not in scalar_result
        assert object_marker not in non_scalar_result
        assert "8462057319" not in numeric_result
        assert redact_persisted_diagnostic(non_scalar_result, limit=None) == non_scalar_result

    def test_digest_quoted_parameters_are_scanned_at_each_encoding_depth(self):
        value = {"Authorization": 'Digest realm="SYN_R4_REALM", token=SYN_R4_DIGEST'}
        for _depth in range(5):
            serialized = json.dumps(value, ensure_ascii=False)
            result = redact_persisted_diagnostic(serialized, limit=None)
            assert "SYN_R4_REALM" not in result
            assert "SYN_R4_DIGEST" not in result
            value = serialized

    @pytest.mark.parametrize(
        ("text", "markers"),
        [
            ('{"Authorization": [SYN_R4_RAW_AUTH]}', ("SYN_R4_RAW_AUTH",)),
            ('{"X-Api-Key": {"nested": SYN_R4_RAW_API}}', ("SYN_R4_RAW_API",)),
            (
                '{"Cookie": ["sid=SYN_R4_OUTER_COOKIE", '
                '"Authorization": "Bearer SYN_R4_HIDDEN_AUTH"}',
                ("SYN_R4_OUTER_COOKIE", "SYN_R4_HIDDEN_AUTH"),
            ),
            (
                'Set-Cookie: ["sid=SYN_R4_OUTER_SET"; '
                '"X-Api-Key": "SYN_R4_HIDDEN_API"]',
                ("SYN_R4_OUTER_SET", "SYN_R4_HIDDEN_API"),
            ),
        ],
    )
    def test_balanced_invalid_and_malformed_structured_fields_fail_closed(self, text, markers):
        result = redact_persisted_diagnostic(text, limit=None)
        assert all(marker not in result for marker in markers)
        if "Cookie" in text and "[" in text and "}" in text:
            assert result == "[redaction-unavailable]"

    @pytest.mark.parametrize(
        "text",
        [
            "Set-Cookie: sid=SYN_R5_COOKIE; SameSite=not-a-mode; Max-Age=not-a-number; Path=/",
            "Set-Cookie: sid=SYN_R5_OUTER; Path=/\\\"Authorization\\\":\\\"Bearer SYN_R5_NESTED\\\"; Secure",
        ],
    )
    def test_cookie_values_are_atomic_even_when_attributes_look_structured(self, text):
        result = redact_persisted_diagnostic(text, limit=None)
        assert result == "Set-Cookie: ***"
        assert redact_persisted_diagnostic(result, limit=None) == result

    def test_quoted_plain_cookie_value_masks_the_complete_header_line(self):
        text = 'Cookie: "sid=SYN_R5_QUOTED_COOKIE"; Path=/; HttpOnly'
        assert redact_persisted_diagnostic(text, limit=None) == "Cookie: ***"

    def test_quoted_plain_auth_value_with_suffix_fails_closed(self):
        text = 'Authorization: "Bearer SYN_R5_QUOTED_AUTH" suffix SYN_R5_TAIL'
        assert redact_persisted_diagnostic(text, limit=None) == "[redaction-unavailable]"

    @pytest.mark.parametrize("token", ["8462057319", "true", "false", "null"])
    def test_bare_json_scalars_are_masked_as_valid_strings_with_safe_sibling(self, token):
        result = redact_persisted_diagnostic(
            '{"X-Api-Key": ' + token + ', "safe": "public"}', limit=None
        )
        assert json.loads(result) == {"X-Api-Key": "***", "safe": "public"}
        assert token not in result

    def test_invalid_bare_json_scalar_fails_closed(self):
        result = redact_persisted_diagnostic(
            '{"X-Api-Key": opaque-token, "safe": "public"}', limit=None
        )
        assert result == "[redaction-unavailable]"

    @pytest.mark.parametrize(
        "text",
        [
            '{"Authorization": "Bearer SYN_R5_INCOMPLETE}',
            '{"Authorization": \\"Bearer SYN_R5_MISMATCH\\"suffix SYN_R5_TAIL}',
            '{\\\\"Authorization\\\\": \\\\"Bearer SYN_R5_UNSUPPORTED\\\\"}',
        ],
    )
    def test_ambiguous_quoted_depth_fails_closed_for_the_diagnostic(self, text):
        result = redact_persisted_diagnostic(text, limit=None)
        assert result == "[redaction-unavailable]"
        assert all(marker not in result for marker in ("SYN_R5_INCOMPLETE", "SYN_R5_MISMATCH", "SYN_R5_TAIL", "SYN_R5_UNSUPPORTED"))

    def test_backslash_run_work_is_linear_under_a_deterministic_budget(self, monkeypatch):
        import agent.redact as redact_module

        original = redact_module._backslash_run_end
        scanned = []

        def counted(text, start):
            end = original(text, start)
            scanned.append(end - start)
            return end

        monkeypatch.setattr(redact_module, "_backslash_run_end", counted)
        work = []
        for length in (32, 64, 128, 256):
            before = len(scanned)
            text = 'Authorization: "' + ("\\" * length) + "SYN_R4_UNCLOSED"
            result = redact_persisted_diagnostic(text, limit=None)
            assert "SYN_R4_UNCLOSED" not in result
            work.append(sum(scanned[before:]))

        assert all(value > 0 for value in work)
        assert work[1] <= work[0] * 3 + 64
        assert work[2] <= work[1] * 3 + 128
        assert work[3] <= work[2] * 3 + 256
        assert work[3] <= work[0] * 12 + 512

    def test_persisted_output_is_valid_utf8_for_lone_surrogates(self):
        marker = "SYN_R4_" + "SURROGATE"
        result = redact_persisted_diagnostic(
            "synthetic prefix \ud800 Authorization: Bearer " + marker,
            limit=None,
        )

        assert result.encode("utf-8").decode("utf-8") == result
        assert marker not in result

    def test_plain_record_scanner_requires_a_proven_serialization_layer(self):
        import agent.redact as redact_module

        physical = "sid=SYN_R6_PHYSICAL\nnext=SYN_R6_TAIL"
        assert redact_module._plain_field_value_end(physical, 0) == physical.index("\n")
        assert redact_module._plain_field_value_end(
            r"sid=SYN_R6_LITERAL\nnext=SYN_R6_TAIL", 0
        ) is None

        for layer in (0, 1, 3, 7):
            closing = "\\" * layer + '"'
            literal_width = 2 * (layer + 1)
            for line_char in ("n", "r"):
                literal = (
                    "sid=SYN_R6_LITERAL"
                    + "\\" * literal_width
                    + line_char
                    + "continued=SYN_R6_TAIL"
                    + closing
                )
                assert redact_module._plain_field_value_end(
                    literal, 0, serialization_layer=layer
                ) == literal.index(closing)

                boundary = (
                    "sid=SYN_R6_BOUNDARY"
                    + "\\" * (layer + 1)
                    + line_char
                    + "continued=SYN_R6_TAIL"
                    + closing
                )
                assert redact_module._plain_field_value_end(
                    boundary, 0, serialization_layer=layer
                ) == boundary.index("\\" * (layer + 1) + line_char)

        for layer in (1, 3, 7):
            mismatched = (
                "sid=SYN_R6_MISMATCH"
                + "\\"
                + "ncontinued=SYN_R6_TAIL"
                + "\\" * layer
                + '"'
            )
            assert redact_module._plain_field_value_end(
                mismatched, 0, serialization_layer=layer
            ) is None

    @pytest.mark.parametrize(
        ("field_name", "line_char", "prefix"),
        [
            ("Cookie", "n", "SYN_R6_COOKIE_N"),
            ("Cookie", "r", "SYN_R6_COOKIE_R"),
            ("Set-Cookie", "n", "SYN_R6_SET_N"),
            ("Set-Cookie", "r", "SYN_R6_SET_R"),
        ],
    )
    def test_literal_slash_records_are_atomic_at_each_serialization_depth(
        self, field_name, line_char, prefix
    ):
        value = (
            f"{field_name}: sid={prefix}_HEAD"
            + "\\"
            + line_char
            + f"continued={prefix}_TAIL; Path=/{prefix}_PATH"
        )
        raw = json.dumps({"content": value, "safe": "public"}, ensure_ascii=False)

        for depth in range(5):
            result = redact_persisted_diagnostic(raw, limit=None)
            assert all(marker not in result for marker in (
                f"{prefix}_HEAD", f"{prefix}_TAIL", f"{prefix}_PATH",
            ))
            decoded = result
            for _ in range(depth + 1):
                decoded = json.loads(decoded)
            assert isinstance(decoded, dict)
            assert decoded["content"] == f"{field_name}: ***"
            assert decoded["safe"] == "public"
            assert redact_persisted_diagnostic(result, limit=None) == result
            raw = json.dumps(raw, ensure_ascii=False)

    def test_actual_physical_records_end_without_swallowing_the_next_sensitive_field(self):
        text = (
            "Cookie: sid=SYN_R6_PHYSICAL_COOKIE\n"
            "Authorization: Bearer SYN_R6_PHYSICAL_AUTH\r\n"
            "Set-Cookie: sid=SYN_R6_PHYSICAL_SET"
        )
        assert redact_persisted_diagnostic(text, limit=None) == (
            "Cookie: ***\n"
            "Authorization: Bearer ***\r\n"
            "Set-Cookie: ***"
        )

    def test_json_whitespace_predicate_is_exactly_rfc_8259(self):
        import agent.redact as redact_module

        assert all(redact_module._is_json_whitespace(char) for char in " \t\n\r")
        assert not any(
            redact_module._is_json_whitespace(char)
            for char in ("\x0b", "\x0c", "\u0085", "\u00a0", "\u2003", "\u2028")
        )

    @pytest.mark.parametrize(
        "text",
        [
            '{"Authorization"\x0b: "Bearer SYN_R6_KEY_VT", "safe": "SYN_R6_TAIL"}',
            '{"Authorization":\x0c"Bearer SYN_R6_COLON_FF", "safe": "SYN_R6_TAIL"}',
            '{"Authorization": "Bearer SYN_R6_QUOTED_NBSP"\u00a0, "safe": "SYN_R6_TAIL"}',
            '{"X-Api-Key": true\u2003, "safe": "SYN_R6_BARE_EM"}',
            '{"X-Api-Key": [1]\u2028, "safe": "SYN_R6_CONTAINER_LINE"}',
        ],
    )
    def test_non_json_whitespace_at_structured_boundaries_fails_closed(self, text):
        result = redact_persisted_diagnostic(text, limit=None)
        assert result == "[redaction-unavailable]"
        assert all(marker not in result for marker in (
            "SYN_R6_KEY_VT", "SYN_R6_COLON_FF", "SYN_R6_QUOTED_NBSP",
            "SYN_R6_TAIL", "SYN_R6_BARE_EM", "SYN_R6_CONTAINER_LINE",
        ))

    def test_r7_json_escape_key_names_are_canonicalized_at_nested_depths(self):
        auth_marker = "SYN_R7_ESCAPED_AUTH"
        cookie_marker = "SYN_R7_ESCAPED_COOKIE"
        raw = (
            '{"Authoriz\\u0061tion": "Bearer ' + auth_marker + '", '
            '"Coo\\u006bie": "sid=' + cookie_marker + '", "safe": "public"}'
        )

        for depth in range(5):
            result = redact_persisted_diagnostic(raw, limit=None)
            assert auth_marker not in result
            assert cookie_marker not in result
            decoded = result
            for _ in range(depth + 1):
                decoded = json.loads(decoded)
            assert isinstance(decoded, dict)
            assert decoded["Authorization"] == "Bearer ***"
            assert decoded["Cookie"] == "***"
            assert decoded["safe"] == "public"
            assert redact_persisted_diagnostic(result, limit=None) == result
            raw = json.dumps(raw, ensure_ascii=False)

    @pytest.mark.parametrize(
        "text",
        [
            "https://api.example.test/x?token=&safe=ok",
            'https://api.example.test/x?token="SYN_R7_QUOTED_QUERY"&safe=ok',
            f"https://api.example.test/x?{_r8_percent_encode('client_secret', 11)}=SYN_R7_QUERY_CAP&safe=ok",
        ],
    )
    def test_r7_strict_query_ambiguity_fails_closed(self, text):
        result = redact_persisted_diagnostic(text, limit=None)
        assert result == "[redaction-unavailable]"
        assert "SYN_R7_" not in result

    def test_r7_strict_query_preserves_json_escape_framing(self):
        marker = "SYN_R7_JSON_QUERY"
        raw = json.dumps(
            {
                "url": "https://api.example.test/x?token=" + marker + "&safe=ok",
                "safe": "public",
            },
            ensure_ascii=False,
        )

        result = redact_persisted_diagnostic(raw, limit=None)

        parsed = json.loads(result)
        assert parsed["url"] == "https://api.example.test/x?token=***&safe=ok"
        assert parsed["safe"] == "public"
        assert marker not in result
        assert redact_persisted_diagnostic(result, limit=None) == result

    @pytest.mark.parametrize(
        "text",
        [
            "https://alice:SYN_R7_TRUNCATED_USERINFO@",
            "Authorization\x0b: Bearer SYN_R7_VT_HEADER",
            "X-Api-Key\x0c: SYN_R7_FF_HEADER",
            "Cookie\u00a0: sid=SYN_R7_NBSP_HEADER",
        ],
    )
    def test_r7_truncated_userinfo_and_non_ows_headers_fail_closed(self, text):
        result = redact_persisted_diagnostic(text, limit=None)
        if text.startswith("https://"):
            assert result == "https://alice:***@"
        else:
            assert result == "[redaction-unavailable]"
        assert "SYN_R7_" not in result

    def test_r7_utf8_bound_is_byte_bounded_and_idempotent(self):
        text = "é" * 80 + " safe tail context"

        result = redact_persisted_diagnostic(text, limit=64)

        assert len(result.encode("utf-8")) <= 64
        assert redact_persisted_diagnostic(result, limit=64) == result

    def test_r7_formatter_fails_closed_for_ambiguous_strict_query(self):
        marker = "SYN_R7_FORMATTER_QUERY"
        text = "provider https://api.example.test/x?token=\"" + marker + "\""

        result = RedactingFormatter("%(message)s").format(
            logging.makeLogRecord({"msg": text})
        )

        assert result == "[redaction-unavailable]"
        assert marker not in result


_R8_POLICY_NAMES = tuple(sorted(
    redact_module._PERSISTED_STRUCTURED_POLICY_NAMES
))


@pytest.mark.parametrize("field_name", _R8_POLICY_NAMES)
def test_r8_policy_names_are_safe_in_structured_shapes_and_formatter(field_name):
    marker = "SYN_R8_STRUCTURED_" + field_name.replace("-", "_")
    structured = json.dumps({field_name: marker, "safe": "public"})
    list_value = json.dumps([{"safe": "public"}, {field_name: marker}])
    malformed = '{"' + field_name + '\": "' + marker + '"'

    for candidate in (structured, list_value, structured.encode("utf-8"), malformed):
        result = redact_persisted_diagnostic(candidate, limit=None)
        assert marker not in result
    formatted = RedactingFormatter("%(message)s").format(
        logging.makeLogRecord({"msg": "provider detail " + structured})
    )
    assert marker not in formatted


def test_r8_canonicalization_reaches_fully_percent_encoded_names_through_depth_ten():
    name = "token"
    for depth in range(1, 11):
        name = _r8_percent_encode_once(name)
        marker = f"SYN_R8_DEPTH_{depth}"
        result = redact_persisted_diagnostic(
            f"https://api.example.test/x?{name}={marker}&safe=ok", limit=None
        )
        assert marker not in result
        assert result != "[redaction-unavailable]"

    name = _r8_percent_encode_once(name)
    result = redact_persisted_diagnostic(
        f"https://api.example.test/x?{name}=SYN_R8_DEPTH_CAP&safe=ok", limit=None
    )
    assert result == "[redaction-unavailable]"


def test_r8_mixed_percent_and_rfc8259_query_name_is_safe():
    marker = "SYN_R8_MIXED_QUERY"
    text = "https://api.example.test/x?%5Cu0074oken=" + marker + "&safe=ok"

    result = redact_persisted_diagnostic(text, limit=None)
    formatted = RedactingFormatter("%(message)s").format(
        logging.makeLogRecord({"msg": text})
    )

    assert marker not in result
    assert marker not in formatted


@pytest.mark.parametrize(
    "field_name",
    tuple(redact_module._PERSISTED_AUTH_HEADER_NAMES) + ("cookie", "set-cookie"),
)
def test_r8_repeated_non_ows_header_run_fails_closed(field_name):
    marker = "SYN_R8_NBSP_" + field_name.replace("-", "_")
    value = "Bearer " + marker if "authorization" in field_name else marker
    if field_name in {"cookie", "set-cookie"}:
        value = "sid=" + marker
    text = field_name + ("\u00a0" * 5) + ": " + value

    result = redact_persisted_diagnostic(text, limit=None)

    assert result == "[redaction-unavailable]"
    assert marker not in result
