"""Admission guard: Hermes/Kanban repair work never becomes a Kanban card.

Standing invariant of this fleet:

* SubsidySmart (product/application) work goes through Kanban.
* Hermes / Kanban / profile / gateway / dispatcher / lifecycle fixes — and the
  reviews of those fixes — run DIRECTLY through isolated external workers, in
  their own worktree, never as a Kanban card.

The second half of that rule used to be pure convention, so any creation
surface (CLI, ``hermes kanban create``, ``kanban_create`` tool, dashboard API, swarm/decompose/specify)
could quietly enqueue a self-repair card. This module makes it mechanical: a
pure predicate plus a raiser, called from the single choke point
``hermes_cli.kanban_db.create_task`` before any durable write.

The module is deliberately dependency-light (stdlib only, no import of
``kanban_db``) so it can be unit-tested in isolation and imported from the
creation path without a cycle. Board normalization is the caller's job — pass
the already-resolved board slug.
"""

from __future__ import annotations

import os
from pathlib import Path
from typing import Optional

__all__ = [
    "HERMES_SOURCE_ROOT",
    "CONTROL_BOARDS",
    "REPAIR_ASSIGNEE_PREFIXES",
    "is_direct_route_repair",
    "guard_direct_route_repair",
]


# Root of the Hermes source tree this process is actually running from
# (.../hermes_cli/kanban_direct_route_guard.py -> ...). Derived from the
# module's own location rather than a hardcoded path so worktrees, checkouts and
# site-packages installs all self-identify correctly.
HERMES_SOURCE_ROOT = Path(__file__).resolve().parent.parent

# Boards that carry Hermes control work. ``default`` is the fleet's own board;
# a slug that names the hermes/kanban scope counts too.
CONTROL_BOARDS = frozenset({"default", ""})
_CONTROL_BOARD_TOKENS = ("hermes", "kanban")

# Implementation-or-review profiles. Prefix match, so ``programmer-claudecode``
# and ``code-reviewer-x`` are covered without enumerating variants.
REPAIR_ASSIGNEE_PREFIXES = ("programmer", "code-reviewer")

# Tokens in a project id / tenant that identify the Hermes/Kanban repo itself.
_HERMES_PROJECT_TOKENS = ("hermes", "kanban")


def _norm(value: Optional[str]) -> str:
    return (value or "").strip().casefold()


def _is_control_board(board: Optional[str]) -> bool:
    slug = _norm(board)
    if not slug or slug in CONTROL_BOARDS:
        return True
    return any(token in slug for token in _CONTROL_BOARD_TOKENS)


def _is_repair_assignee(assignee: Optional[str]) -> bool:
    name = _norm(assignee)
    if not name:
        return False
    return name.startswith(REPAIR_ASSIGNEE_PREFIXES)


def _under_hermes_source_root(path: Optional[str]) -> bool:
    raw = (path or "").strip()
    if not raw:
        return False
    try:
        # Purely lexical: the path need not exist yet (worktrees are created
        # later, and the guard must stay side-effect free).
        candidate = Path(os.path.abspath(os.path.expanduser(raw)))
    except Exception:
        return False
    try:
        candidate.relative_to(HERMES_SOURCE_ROOT)
    except ValueError:
        return False
    return True


def _identifies_hermes_repo(value: Optional[str]) -> bool:
    token = _norm(value)
    if not token:
        return False
    return any(t in token for t in _HERMES_PROJECT_TOKENS)


def _is_explicit_non_hermes_target(
    *,
    workspace_path: Optional[str] = None,
    branch_name: Optional[str] = None,
    project_path: Optional[str] = None,
    project_id: Optional[str] = None,
    tenant: Optional[str] = None,
) -> bool:
    t = _norm(tenant)
    if t and t not in ("default", "") and not any(tok in t for tok in _HERMES_PROJECT_TOKENS):
        return True

    pid = _norm(project_id)
    if pid and pid not in ("default", "") and not any(tok in pid for tok in _HERMES_PROJECT_TOKENS):
        return True

    for p in (workspace_path, project_path):
        raw = (p or "").strip()
        if raw and not _under_hermes_source_root(raw):
            norm_p = raw.casefold()
            if "subsidysmart" in norm_p or "projects/" in norm_p or "projects\\" in norm_p:
                return True
            if not ("scratch" in norm_p or ".hermes" in norm_p or "/tmp/t_" in norm_p):
                return True

    return False


def is_direct_route_repair(
    *,
    board: Optional[str],
    assignee: Optional[str],
    workspace_path: Optional[str] = None,
    branch_name: Optional[str] = None,
    project_path: Optional[str] = None,
    project_id: Optional[str] = None,
    tenant: Optional[str] = None,
) -> bool:
    """True when this card is Hermes/Kanban repair work that must not be a card.

    All three conditions must hold:

    1. the resolved board is the Hermes control board;
    2. the (already canonicalised) assignee is an implementation-or-review
       profile — ``programmer*`` or ``code-reviewer*``;
    3. the target is Hermes/Kanban tenant/scope or Hermes-local workspace/project,
       and NOT an explicit external (e.g. SubsidySmart product) target.

    Condition 3 is what keeps SubsidySmart product cards and ordinary
    default-board work legal.
    """
    if not _is_control_board(board):
        return False
    if not _is_repair_assignee(assignee):
        return False
    if _is_explicit_non_hermes_target(
        workspace_path=workspace_path,
        branch_name=branch_name,
        project_path=project_path,
        project_id=project_id,
        tenant=tenant,
    ):
        return False
    return True


def guard_direct_route_repair(
    *,
    board: Optional[str],
    assignee: Optional[str],
    workspace_path: Optional[str] = None,
    branch_name: Optional[str] = None,
    project_path: Optional[str] = None,
    project_id: Optional[str] = None,
    tenant: Optional[str] = None,
) -> None:
    """Raise ``ValueError`` when the card is direct-route repair work."""
    if not is_direct_route_repair(
        board=board,
        assignee=assignee,
        workspace_path=workspace_path,
        branch_name=branch_name,
        project_path=project_path,
        project_id=project_id,
        tenant=tenant,
    ):
        return
    raise ValueError(
        "hermes/kanban repair work must run directly through an isolated "
        "external worker, not as a kanban card "
        f"(assignee={assignee!r}, board={board!r}): create a dedicated git "
        "worktree off the hermes source tree and run the fix (and its review) "
        "there directly"
    )
