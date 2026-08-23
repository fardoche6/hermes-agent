# Telegram human-gate alert card — exact format and notifier artifact spec

Status: **canonical / hard requirement**  
Spec owner task: `t_a81e6fba`  
Implementation parent: `t_647ae0e9`  
Scaffold (public surface, format deferred): `~/.hermes/kanban/operator-artifacts/human-gate-notifier/`  
Date frozen: 2026-07-28

This document is the unambiguous contract for the Telegram human-gate /
review-required alert card and for the notifier operator artifact. Downstream
implementers (`t_fb39d63a`, `t_89433965`, `t_647ae0e9`) MUST align to this
spec; do not invent alternate layouts.

---

## 0. Continuity context (3.2 human-gate notifier flow)

### 0.1 Archival lookup note for `t_802d5216`

On the **default** board, `kanban_show t_802d5216` returns **task not found**
(including archived scan of the default board). That lookup failure is the
documented continuity fact carried on parent `t_647ae0e9` (EVIDENCE NOTE) and
is **not** proof that the historical card never existed.

### 0.2 Where `t_802d5216` actually lives

Archived on board **`subsidysmart`**:

| Field | Value |
|---|---|
| id | `t_802d5216` |
| title | Notify Telegram on SubsidySmart human-gate verification blocks |
| status | `archived` |
| tenant | `infra` |
| assignee | `programmer` |
| workspace | hermes-agent worktree |
| result / runs | none (never executed) |

Body intent (preserved):

- Route **human-required** blocked events from `subsidysmart` only.
- Reuse the **existing** notifier/subscription path — **no parallel notifier**.
- Payload must include task ID, title, board, actionable block reason.
- Technical-only blockers must not page the human-gate route.
- Original chat target in body: Telegram `-1004416879179` thread **`4`**.

Worker comment on the card (2026-07-20 pre-creation readback):

> Shelved by **supersession**, not worker failure. No shipped human-gate
> alert/reply implementation or live-fire proof. Current board routing
> supersedes the stale thread-4 target: SubsidySmart notifications must use
> the board’s configured Telegram **topic 2**. New work must use the existing
> recovery gate/path, current topic-2 routing, and the two-card human-gate
> scope.

### 0.3 Coaching origin of the **exact** card layout

Source: Claude session `60184065-c9b9-405a-92ab-10eab42b0cf5`
(journal: `Obsidian-Vault/_raw/journal/Hermes Human-Gate Notifier.md`).

User-approved hard template (verbatim structure):

```text
<alert icon> Task blocked by human decision (bold title)

the task: <XYZ: task title>

need your decision on this (explain, the situation, give what i need to
understand (ex: pro vs con)).

<icon> Suggestions :
1- (recommended)
2-
3-
```

Coach-rendered canonical example and hard-spec lines (same session):

```text
🚨 Task blocked by human decision

Task: "Pipeline: bind cards to real Supabase programs" (subsidysmart · blocked 2 days)

Need your decision: <situation + pro/con when a real trade-off exists>

🔧 Suggestions:
1- <option> (Recommended)
2- <option>
3- <option>
```

Two-card split (same session, still the approved plan):

1. **Card 1 — alert only:** one deduped Telegram message in the format below;
   until reply wiring ships, each suggestion lists the **exact command** to run.
2. **Card 2 — reply-to-action:** numbered replies execute mapped actions
   (`1` approve+unblock, `2 <reason>` stay blocked, `3 <profile>` reassign+unblock).

### 0.4 Relationship to stock Hermes kanban notifier

Stock gateway path `gateway/kanban_watchers.py` already emits generic terminal
pings, including:

```text
⏸ [board] @assignee Kanban <task_id> blocked: <reason[:160]>
```

via `adapter.send(chat_id, msg, metadata={thread_id})`.

That message is **not** the human-gate alert card. The human-gate artifact
produces a **distinct** decision-asking layout. It must not reimplement
subscription polling, cursor claim/advance, or board status transitions.

Closest control-plane interactive precedent (for Card 2 later, not Card 1):
Telegram `send_exec_approval` in `plugins/platforms/telegram/adapter.py`
(HTML + inline keyboard). Card 1 ships without requiring new gateway code or
a service restart unless an implementer proves script-only delivery is
impossible — in that case STOP and ask before restarting the gateway.

### 0.5 Continuity rules derived from the above

1. Do **not** rebuild a second sweeper. Prefer extending
   `kanban_blocked_recovery_gate.py` (already 1-minute cron + fingerprint
   dedupe) or calling this artifact from that path.
2. Default delivery destination for this product lane:
   - `platform`: `telegram`
   - `chat_id`: `-1004416879179`
   - `thread_id` / topic: **`2`** (supersedes `t_802d5216` thread `4`)
3. Human-type blocks page the owner; technical auto-blocks do not.
4. One alert per block **episode**; unblock→reblock is a new episode.
5. No secrets, tokens, credentials, raw PII, or product data dumps in the card.

---

## 1. Trigger contract (when to render/send)

### 1.1 MUST send (human-gate)

A candidate is a human-gate alert when **any** of the following holds:

| # | Condition |
|---|---|
| H1 | `status == blocked` AND `block_kind` ∈ {`needs_input`, `approval`, `human`} |
| H2 | `status == blocked` AND block reason / payload clearly requires owner input (credentials/secrets, payments/quotas, protected-branch/dequeue, genuine product/design choice) even if kind is missing — fail closed toward paging if ambiguous and recovery gate already treats it as human |
| H3 | `status == triage` AND task is **stale > 4 hours** AND (assigned with `block_recurrences < 2` **or** otherwise not auto-woken by the non-human recovery gate) |

`review-required:` coding handoffs are **lifecycle transport**, not owner
input (kanban-lifecycle One-Shot / recovery rules). They MUST NOT use this
human-gate card unless the block has escalated into a true human decision
(e.g. third disagreement / explicit human-gate). Do not conflate
`review-required:` with `needs_input`.

### 1.2 MUST NOT send

- Purely technical blocks the recovery gate auto-handles
  (`crashed`, `timed_out`, `gave_up`, dead pid, non-human `block_kind`).
- `completed` / `done` / `archived` / silent `unblocked`.
- Boards outside the configured allow-list for this route (default product
  lane: `subsidysmart`; multi-board fan-out is an explicit config change).
- Repeat of the same episode fingerprint (see §3).

### 1.3 Selection ownership

Event selection, board scan, subscription rows, and fingerprints are **owned
by Hermes** (recovery gate / notifier watcher / `kanban_notify_subs`). This
artifact receives an already-selected alert DTO and only renders (and
optionally delivers via a thin adapter).

---

## 2. Exact Telegram alert card format (hard requirement)

### 2.1 Wire layout (plain logical lines)

The rendered message MUST contain these sections **in order**, with a single
blank line between sections:

1. **Title line** — alert icon + bold fixed title  
2. **Task line** — readable title + board + age  
3. **Decision body** — “Need your decision:” + situation  
4. **Suggestions footer** — icon + numbered 1..3, exactly one Recommended  

Canonical plain-text shape (logical content; bold applied per §2.2):

```text
🚨 Task blocked by human decision

Task: "<readable_title>" (<board> · blocked <age>)

Need your decision: <situation_and_decision>
[<optional pro/con lines when a real trade-off exists>]

🔧 Suggestions:
1- <action_label_or_command> (Recommended)
2- <action_label_or_command>
3- <action_label_or_command>
```

### 2.2 Formatting rules

| Rule | Requirement |
|---|---|
| Title text | Exactly `Task blocked by human decision` (case-sensitive) |
| Title icon | Exactly `🚨` as the leading token on line 1 |
| Title emphasis | Title phrase MUST be bold on Telegram. Use HTML: `🚨 <b>Task blocked by human decision</b>` with `parse_mode=HTML` |
| Task line prefix | Exactly `Task: ` then a double-quoted readable title |
| Board / age | Parenthetical after title: `(<board> · blocked <age>)` using middle dot `·` with spaces as shown |
| Age format | Human short form: `N minutes` / `N hours` / `N days` (integer, no ISO timestamps in the card body) |
| Body prefix | Exactly `Need your decision:` (capital N) followed by a space and the situation text |
| Pro/con | When a real trade-off exists, include explicit `Pro …` / `Con …` lines inside the body section. Omit fabricated pros/cons |
| Suggestions header | Exactly `🔧 Suggestions:` |
| Suggestion count | Exactly **3** options, numbered `1-`, `2-`, `3-` (ASCII hyphen after the digit) |
| Recommended marker | Exactly one option ends with ` (Recommended)` — that option MUST be `#1` unless product later documents an exception |
| Task IDs in body | **No task-ID jargon in the decision body.** IDs may appear only inside parentheses on the Task line and inside command strings in the suggestions footer |
| Secrets | Never put tokens, credentials, API keys, `.env` contents, raw headers, or personal secrets in any field |
| Length | Entire message ≤ **4096** Telegram characters after HTML. Truncate the situation middle with `…` if needed; never drop title, task line, or suggestions |
| Language | Match the operator’s working language for the board (default English for this fleet) |
| Blank lines | One blank line after title; one blank line after task line; one blank line before suggestions header |

### 2.3 HTML rendering (normative for Telegram send)

```html
🚨 <b>Task blocked by human decision</b>

Task: &quot;{escaped_title}&quot; ({escaped_board} · blocked {escaped_age})

Need your decision: {escaped_situation}

🔧 Suggestions:
1- {escaped_opt1} (Recommended)
2- {escaped_opt2}
3- {escaped_opt3}
```

- Escape **all** interpolated fields with HTML escaping (`&`, `<`, `>`, `"`).
- Do **not** bold anything except the fixed title phrase.
- Do **not** use MarkdownV2 for this card (too fragile with user text).
- Link previews: off / disabled if the adapter exposes the knob.

### 2.4 Card 1 vs Card 2 suggestion semantics

**Card 1 (alert-only — current implementation target for `t_647ae0e9` /
`t_fb39d63a`):**

Each suggestion is a **copy-pastable action description or shell command**,
not a magic reply keyword. Example:

```text
🔧 Suggestions:
1- hermes kanban unblock t_ce96b670 --reason "human approval: scope pinned" (Recommended)
2- hermes kanban comment t_ce96b670 "keep blocked: <reason>"
3- hermes kanban reassign t_ce96b670 <profile>
```

(Exact CLI flags must match live `hermes kanban --help` at implementation
time; the **shape** is fixed, the flag spellings are verified at implement.)

**Card 2 (reply-to-action — later parent/child work):**

| Reply | Action |
|---|---|
| `1` | Post approval comment + unblock |
| `2 <reason>` | Stay blocked; record reason on card |
| `3 <profile>` | Verify profile spawns, reassign, unblock |
| anything else | Usage help only — never guess |

Card 2 must not ship until Card 1 live-fire proof exists.

### 2.5 Worked example (normative)

Logical content:

```text
🚨 Task blocked by human decision

Task: "Pipeline: bind cards to real Supabase programs" (subsidysmart · blocked 2 days)

Need your decision: the last run fixed 5 of 6 review concerns, then ran out of iteration budget with one mobile widget test still failing (retry/error-text assertion). Resuming costs one short run with scope pinned to that single test. Leaving it blocked keeps the two follow-up tasks waiting — they depend on this one.
Pro resume: one small fix from done; unblocks the whole chain.
Con resume: none found — budget was the only limit; the code itself passed review on everything else.

🔧 Suggestions:
1- Reply path deferred — run: hermes kanban unblock <id> --reason "human approval: scope pinned to failing test" (Recommended)
2- Keep blocked — run: hermes kanban comment <id> "keep blocked: <reason>"
3- Reassign — run: hermes kanban reassign <id> <profile>
```

(`<id>` shown only inside command strings in the footer.)

---

## 3. Dedup and episode rules

| Rule | Requirement |
|---|---|
| One alert per episode | At most one successful Telegram delivery per `(board, task_id, episode_key)` |
| Episode key | Changes when the task leaves blocked/triage human-gate state and later re-enters (unblock→reblock = new message) |
| Cron ticks | Subsequent recovery-gate ticks MUST NOT resend while fingerprint matches |
| Failure retry | Delivery failure may retry; must not advance “sent” fingerprint until adapter reports success with a message id when available |
| Acceptance proof | Live proof requires Telegram `message_id` (or gateway log line containing it) — subscription cursor advance alone is **not** receipt |

---

## 4. Notifier operator artifact — location and structure

### 4.1 Canonical location (Hermes operator path)

```text
/home/fardochebot/.hermes/kanban/operator-artifacts/human-gate-notifier/
```

This path is the durable ops home (same pattern as
`operator-artifacts/phase-weekly-scoreboard/`). It is **outside** the
hermes-agent git tree on purpose so operator contracts do not require a
product PR to land.

### 4.2 Required tree

```text
human-gate-notifier/
├── README.md                 # short orientation + how to test
├── SPEC.md                   # THIS document (format + interface contract)
├── pyproject.toml            # package: hermes-human-gate-notifier
├── src/
│   └── human_gate_notifier/
│       ├── __init__.py       # re-exports public surface
│       └── notifier.py       # DTO + render + Notifier protocol
└── tests/
    └── test_notifier.py      # contract + format tests
```

Optional later (not required for Card 1 format slice):

- `references/examples.md` — extra worked examples
- `src/human_gate_notifier/delivery.py` — thin Telegram adapter wrapper

### 4.3 What MUST NOT live in the artifact

- Kanban SQLite access / `kanban_db` imports
- Subscription cursor claim/advance/rewind
- Board status transitions (`block` / `unblock` / `reassign`)
- A second board sweeper or cron definition
- Secrets or live bot tokens
- SubsidySmart product code

---

## 5. Public interface (stable API)

The scaffold already exposes:

```python
from human_gate_notifier import HumanGateAlert, Notifier, render_alert
```

### 5.1 `HumanGateAlert` — required fields for the full card

The scaffold’s initial fields (`task_id`, `task_title`, `situation`,
`decision_needed`) are the minimum seed. **Format-complete rendering** for
this spec requires the following contract (implementers MUST extend the
dataclass rather than packing everything into free text ad hoc):

| Field | Type | Required | Notes |
|---|---|---|---|
| `task_id` | `str` | yes | Canonical id; used in commands/fingerprint, not body jargon |
| `task_title` | `str` | yes | Human-readable title; appears quoted on Task line |
| `board` | `str` | yes | Board slug (e.g. `subsidysmart`) |
| `blocked_age` | `str` | yes | Preformatted short age (`2 days`, `5 hours`) |
| `situation` | `str` | yes | Phone-decidable context; may include pro/con lines |
| `decision_needed` | `str` | yes | One-line statement of the decision; may be merged into the body after `Need your decision:` |
| `suggestions` | `tuple[Suggestion, ...]` length 3 | yes | Ordered options 1..3 |
| `episode_kind` | `str` | recommended | `human_block` \| `stale_triage` |
| `block_kind` | `str \| None` | optional | Echo of kanban `block_kind` for logs/tests only |

```python
@dataclass(frozen=True, slots=True)
class Suggestion:
    label: str                 # text after "N- "
    recommended: bool = False  # exactly one True across the three
    # Card 1: label may be the full command string.
    # Card 2: label is the human text; reply keyword is the index.
```

Validation invariants (enforce in `render_alert` or a `validate_alert`):

1. `len(suggestions) == 3`
2. Exactly one `suggested.recommended is True`
3. Non-empty `task_title`, `board`, `blocked_age`, `situation` / `decision_needed`
4. No raw secret patterns in any string field (minimal scan: `api_key`, `token=`, `BEGIN PRIVATE`, bearer-like blobs)

### 5.2 `render_alert(alert: HumanGateAlert) -> str`

- **Pure function.** No I/O, no network, no DB.
- Returns the full HTML (or pre-escaped HTML string) ready for Telegram
  `parse_mode=HTML`.
- MUST match §2 byte-for-byte on fixed tokens (`🚨`, title text, `Task: `,
  `Need your decision:`, `🔧 Suggestions:`, numbering).
- Raises `ValueError` on invalid alerts; must not silently emit a partial card.

Body composition rule:

```text
Need your decision: {decision_needed.strip()}
[+ " " + situation if situation is not already included in decision_needed]
```

Prefer a single coherent paragraph: if `decision_needed` is the first
sentence and `situation` is the rest, join with a space. Do not print the
label twice.

### 5.3 `Notifier` protocol

```python
class Notifier(Protocol):
    def send(self, message: str) -> None: ...
```

Normative delivery expectations when a concrete notifier is supplied:

| Concern | Requirement |
|---|---|
| Input | Already-rendered message from `render_alert` |
| Transport | Existing Hermes Telegram adapter / gateway send path |
| Destination | Default `chat_id=-1004416879179`, `thread_id=2` unless caller overrides |
| Return / side channel | Prefer surfacing Telegram `message_id` to the caller for acceptance evidence |
| Errors | Propagate send failures; do not mark episode fingerprint sent on failure |
| Kanban | MUST NOT mutate kanban state |

A helper signature (optional, may live beside the protocol) is acceptable:

```python
def deliver_alert(
    alert: HumanGateAlert,
    *,
    notifier: Notifier,
) -> str:
    """render_alert then notifier.send; return rendered message."""
```

### 5.4 Out-of-scope functions (belong in Hermes, not this package)

- `select_human_gate_candidates(boards) -> list[...]`
- `claim_unseen_events_for_sub(...)`
- `advance_notify_cursor` / `rewind_notify_cursor`
- `kanban_block` / `kanban_unblock` / `reassign`
- Fingerprint file I/O for the recovery gate (caller owns it)

---

## 6. Integration sketch (non-normative placement)

Recommended call site for Card 1:

1. `kanban_blocked_recovery_gate.py` (or thin wrapper) identifies H1–H3.
2. Builds `HumanGateAlert` from task title, board, age, block reason, latest
   comments (situation synthesis may be rule-based first; LLM optional later).
3. Calls `render_alert`.
4. Sends via existing gateway Telegram adapter to topic 2.
5. Records fingerprint + message_id in recovery/notify state.
6. Does **not** auto-unblock.

Stock `kanban_watchers` generic `blocked` ping may still fire for subscribers.
If double-notify is observed on the same topic, suppress the **generic**
blocked one-liner for human-gate kinds on that route — do not dilute the alert
card format.

---

## 7. Test contract (what green means)

Minimum tests the format implementation must add/replace in
`tests/test_notifier.py`:

1. **Constructible alert** with full required fields.
2. **Exact fixed tokens** present in `render_alert` output:
   `🚨`, `<b>Task blocked by human decision</b>`, `Task: "`,
   `Need your decision:`, `🔧 Suggestions:`, `1-`, `2-`, `3-`,
   `(Recommended)`.
3. **Ordering**: title before task line before body before suggestions.
4. **Exactly one** `(Recommended)`.
5. **HTML escaping**: titles/situations containing `<`, `>`, `&` are escaped.
6. **No task id** in the decision body when only provided via `task_id` field
   (id may appear inside suggestion command strings).
7. **Length guard**: oversized situation truncated; title/suggestions retained.
8. **Secret-safety**: obvious token-like payloads rejected or redacted per
   chosen policy (prefer reject).
9. **No Kanban imports** in `src/human_gate_notifier` (static test).

Run:

```bash
cd /home/fardochebot/.hermes/kanban/operator-artifacts/human-gate-notifier
python -m pytest
```

Live acceptance (parent DONE WHEN — not this research card):

- Real `needs_input` episode → exactly one Telegram message in topic 2.
- Proof includes `message_id` (or gateway log equivalent).
- Re-tick does not spam; reblock produces a fresh message.

---

## 8. Acceptance checklist for consumers of this spec

- [ ] Format matches §2 fixed tokens and section order.
- [ ] Destination defaults to chat `-1004416879179` topic `2`.
- [ ] Artifact lives under
      `~/.hermes/kanban/operator-artifacts/human-gate-notifier/`.
- [ ] Public API remains `HumanGateAlert` + `render_alert` + `Notifier`.
- [ ] No duplicated Kanban logic inside the package.
- [ ] Continuity of `t_802d5216` (archived subsidysmart; default-board miss;
      topic-2 supersession; no parallel notifier) is respected.
- [ ] Card 1 does not claim reply-to-action done; Card 2 is separate.

---

## 9. Source index

| Source | Role |
|---|---|
| Coaching session `60184065-c9b9-405a-92ab-10eab42b0cf5` | User hard format + two-card plan |
| `Obsidian-Vault/_raw/journal/Hermes Human-Gate Notifier.md` | Journaled format + topic-2 + dedup |
| subsidysmart DB task `t_802d5216` + comment | Historical human-gate notify card continuity |
| Parent `t_647ae0e9` body/comments | Default-board archival miss note; live sub probe |
| `t_e463a7b9` comments | Code-path map for Telegram send / kanban watchers |
| Operator scaffold `human-gate-notifier/` | Package layout + deferred `render_alert` |
| Lifecycle skill notifications section | Blocked notify should carry id, title, blocker, recovery action |
| `gateway/kanban_watchers.py` | Existing generic blocked ping (non-authoritative layout) |
| `plugins/platforms/telegram/adapter.py` `send_exec_approval` | HTML interactive precedent for later Card 2 |

---

## 10. Explicit non-goals

- Redesigning generic completed/crashed/timed_out pings.
- Replacing `kanban_notify_subs` with a new subsystem.
- Auto-executing human decisions without Card 2 + explicit proof.
- Notifying every `review-required:` coding handoff as a human gate.
