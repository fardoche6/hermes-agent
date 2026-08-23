# Hermes human-gate notifier artifact

Canonical operator artifact for the Telegram human-gate / review-required
alert card.

## Canonical contract

**Read `SPEC.md` first.** It freezes:

- exact Telegram alert card format (fixed tokens, section order, HTML bold);
- trigger / non-trigger rules;
- dedup / episode rules;
- package layout under the Hermes operator path;
- public interface (`HumanGateAlert`, `render_alert`, `Notifier`);
- continuity context for archived `t_802d5216` and the 3.2 human-gate flow.

This package must not duplicate Kanban event selection, cursor management, or
status transitions. Hermes owns those; this artifact owns rendering (and a
thin delivery adapter if needed).

## Layout

- `SPEC.md` — **hard format + interface contract** (authoritative)
- `src/human_gate_notifier/` — importable package
- `tests/` — contract / format tests
- `pyproject.toml` — package metadata

Path:

```text
/home/fardochebot/.hermes/kanban/operator-artifacts/human-gate-notifier/
```

## Public surface

```python
from human_gate_notifier import HumanGateAlert, Notifier, render_alert
```

Format-complete field requirements are defined in `SPEC.md` §5. `render_alert`
returns Telegram HTML using `parse_mode=HTML`; it validates the three-option
contract, rejects secret-like values, escapes interpolated fields, and keeps
the message within Telegram's 4096-character limit.

## Local verification

```bash
cd /home/fardochebot/.hermes/kanban/operator-artifacts/human-gate-notifier
python -m pytest
```
