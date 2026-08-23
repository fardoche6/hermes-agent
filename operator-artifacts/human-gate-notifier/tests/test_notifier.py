import pytest

from human_gate_notifier import HumanGateAlert, Notifier, Suggestion, render_alert


def make_alert(**overrides):
    values = {
        "task_id": "t_example",
        "task_title": "Example task",
        "board": "subsidysmart",
        "blocked_age": "2 days",
        "situation": "The implementation is ready except for one product decision.",
        "decision_needed": "Choose whether to proceed with the current scope.",
        "suggestions": (
            Suggestion('hermes kanban unblock t_example --reason "approved"', True),
            Suggestion('Keep the task blocked'),
            Suggestion("Reassign the task to another profile"),
        ),
    }
    values.update(overrides)
    return HumanGateAlert(**values)


def test_public_alert_contract_is_constructible():
    alert = make_alert()

    assert alert.task_id == "t_example"
    assert alert.board == "subsidysmart"
    assert len(alert.suggestions) == 3


def test_render_matches_fixed_telegram_tokens_and_order():
    rendered = render_alert(make_alert())

    assert rendered.startswith("🚨 <b>Task blocked by human decision</b>\n\n")
    assert 'Task: &quot;Example task&quot; (subsidysmart · blocked 2 days)' in rendered
    assert "Need your decision:" in rendered
    assert "🔧 Suggestions:" in rendered
    assert all(token in rendered for token in ("1-", "2-", "3-", "(Recommended)"))
    assert rendered.index("Task:") > rendered.index("Task blocked")
    assert rendered.index("Need your decision:") > rendered.index("Task:")
    assert rendered.index("🔧 Suggestions:") > rendered.index("Need your decision:")
    assert rendered.count("(Recommended)") == 1


def test_html_escapes_interpolated_fields():
    rendered = render_alert(
        make_alert(
            task_title='Fix <this> & that "now"',
            situation="Use <safe> & sound reasoning.",
            decision_needed="Choose <A> & <B>.",
        )
    )

    assert "&lt;this&gt; &amp; that &quot;now&quot;" in rendered
    assert "Use &lt;safe&gt; &amp; sound reasoning." in rendered
    assert "Choose &lt;A&gt; &amp; &lt;B&gt;." in rendered
    assert "<this>" not in rendered


def test_task_id_is_not_added_to_decision_body():
    rendered = render_alert(make_alert())
    body = rendered.split("Need your decision:", 1)[1].split("🔧 Suggestions:", 1)[0]

    assert "t_example" not in body


def test_oversized_situation_is_middle_truncated_and_card_kept():
    rendered = render_alert(make_alert(situation="prefix " + ("x" * 10000) + " suffix"))

    assert len(rendered) <= 4096
    assert "🚨 <b>Task blocked by human decision</b>" in rendered
    assert 'Task: &quot;Example task&quot;' in rendered
    assert "🔧 Suggestions:" in rendered
    assert "prefix " in rendered and " suffix" in rendered
    assert "…" in rendered


@pytest.mark.parametrize(
    "field,value",
    [
        ("task_title", "contains token=do-not-leak"),
        ("situation", "Bearer abcdefghijklmnopqrstuvwxyz0123456789"),
        ("decision_needed", "-----BEGIN PRIVATE KEY-----"),
        ("suggestions", (Suggestion("safe", True), Suggestion("api_key=secret"), Suggestion("third"))),
    ],
)
def test_secret_like_values_are_rejected(field, value):
    with pytest.raises(ValueError, match="secret"):
        render_alert(make_alert(**{field: value}))


def test_invalid_suggestion_contract_is_rejected():
    with pytest.raises(ValueError, match="exactly three"):
        render_alert(make_alert(suggestions=(Suggestion("only", True),)))

    with pytest.raises(ValueError, match="exactly one"):
        render_alert(make_alert(suggestions=(Suggestion("one"), Suggestion("two"), Suggestion("three"))))

    with pytest.raises(ValueError, match="first suggestion"):
        render_alert(
            make_alert(
                suggestions=(
                    Suggestion("one"),
                    Suggestion("two", True),
                    Suggestion("three"),
                )
            )
        )


def test_blocked_age_uses_the_documented_short_human_form():
    # SPEC.md §2.2: age is an integer number of minutes, hours, or days;
    # arbitrary timestamps/labels must not leak into the Telegram card.
    for invalid_age in ("2026-07-28T12:00:00Z", "about yesterday", "2 weeks"):
        with pytest.raises(ValueError, match="age"):
            render_alert(make_alert(blocked_age=invalid_age))


def test_card_does_not_emit_unsupported_markdown_or_reply_controls():
    # SPEC.md §2.3 and §2.4: Card 1 is HTML-only and alert-only; reply-to-action
    # controls belong to Card 2 and must not be implied by this renderer.
    rendered = render_alert(make_alert())

    assert "**" not in rendered
    assert "__" not in rendered
    assert "[Approve]" not in rendered
    assert "reply" not in rendered.lower()
    assert "parse_mode" not in rendered


def test_notifier_artifact_stays_at_the_integration_boundary():
    # SPEC.md §§1.3, 4.3, 5.2, and 5.4: Hermes owns selection, dedupe, and
    # state transitions; this package only validates/renders a selected DTO.
    from pathlib import Path

    package_root = Path(__file__).parents[1] / "src" / "human_gate_notifier"
    source = "\\n".join(path.read_text() for path in package_root.glob("*.py"))

    assert "kanban_db" not in source
    assert "kanban_db" not in source.lower()
    assert "sqlite3" not in source
    assert "advance_notify_cursor" not in source
    assert "select_human_gate_candidates" not in source
    assert "def kanban_block" not in source
    assert "def kanban_unblock" not in source
    assert "def reassign" not in source


def test_notifier_protocol_remains_public():
    assert Notifier
