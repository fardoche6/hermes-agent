#!/usr/bin/env python3
"""Look up Kanban tasks by ID across all Hermes boards.

This script is read-only and does not modify the Kanban database or state.
"""

import argparse
import json
import os
import sys
from pathlib import Path
from typing import Any, Dict, List, Optional

# Remove environment variable overrides that might pin the connection
# to a single board/db, ensuring board switching behaves correctly.
os.environ.pop("HERMES_KANBAN_DB", None)
os.environ.pop("HERMES_KANBAN_BOARD", None)

# Add repo root to import path
REPO_ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(REPO_ROOT))

try:
    import hermes_cli.kanban_db as kb
except ImportError as e:
    print(f"Error: Could not import hermes_cli.kanban_db: {e}", file=sys.stderr)
    sys.exit(2)


def get_task_details(board_slug: str, board_name: str, task_id: str) -> Optional[Dict[str, Any]]:
    """Connect to a board's DB (if it exists) and retrieve the task details."""
    db_path = kb.kanban_db_path(board=board_slug)
    if not db_path.exists():
        return None

    try:
        # Using connect_closing to ensure connection is properly closed
        with kb.connect_closing(board=board_slug) as conn:
            task = kb.get_task(conn, task_id)
            if not task:
                return None

            parents = kb.parent_ids(conn, task_id)
            children = kb.child_ids(conn, task_id)

            return {
                "board": {
                    "slug": board_slug,
                    "name": board_name,
                    "db_path": str(db_path),
                },
                "task": {
                    "id": task.id,
                    "title": task.title,
                    "status": task.status,
                    "assignee": task.assignee,
                    "parents": parents,
                    "children": children,
                    "tenant": task.tenant,
                    "priority": task.priority if hasattr(task, "priority") else None,
                }
            }
    except Exception as e:
        # Log to stderr but don't crash
        print(f"Warning: Failed to query board '{board_slug}': {e}", file=sys.stderr)
        return None


def main() -> int:
    parser = argparse.ArgumentParser(
        description="Search for a Kanban task ID across all non-archived Hermes boards."
    )
    parser.add_argument(
        "task_id_pos",
        nargs="?",
        metavar="TASK_ID",
        help="The Kanban task ID to search for (positional, alternative to --task-id).",
    )
    parser.add_argument(
        "--task-id",
        help="The Kanban task ID to search for (e.g., t_ba150fd9).",
    )
    parser.add_argument(
        "--board",
        help="Optional slug of a specific board to search in.",
    )
    parser.add_argument(
        "--json",
        action="store_true",
        help="Output the result in JSON format.",
    )
    args = parser.parse_args()

    task_id = args.task_id or args.task_id_pos

    # Retrieve all non-archived boards
    try:
        boards = kb.list_boards(include_archived=False)
    except Exception as e:
        print(f"Error listing boards: {e}", file=sys.stderr)
        return 2

    # 1) If task-id is not provided, list all active boards and exit
    if not task_id:
        if args.json:
            print(json.dumps(boards, indent=2))
        else:
            print("Active Kanban Boards:")
            for b in boards:
                print(f"  - slug: {b['slug']}")
                print(f"    name: {b['name']}")
                print(f"    db_path: {b['db_path']}")
                if b.get("default_workdir"):
                    print(f"    workdir: {b['default_workdir']}")
                print()
        return 0

    # 2) Filter boards if a specific one was requested
    if args.board:
        boards = [b for b in boards if b["slug"] == args.board]
        if not boards:
            print(f"Error: Board '{args.board}' is archived or does not exist.", file=sys.stderr)
            return 2

    # Search each board DB for the task ID
    results = []
    for b in boards:
        detail = get_task_details(b["slug"], b["name"], task_id)
        if detail:
            results.append(detail)

    # 3) Report results or explicit not-found
    if not results:
        if args.json:
            print(json.dumps({"found": False, "task_id": task_id, "message": "not_found"}, indent=2))
        else:
            print("not_found")
        return 1

    if args.json:
        print(json.dumps({"found": True, "results": results}, indent=2))
    else:
        for r in results:
            b = r["board"]
            t = r["task"]
            print(f"Found task '{t['id']}' on board '{b['slug']}' ({b['name']}):")
            print(f"  Board Slug: {b['slug']}")
            print(f"  Board Name: {b['name']}")
            print(f"  Task ID: {t['id']}")
            print(f"  Title: {t['title']}")
            print(f"  Status: {t['status']}")
            print(f"  Assignee: {t['assignee'] or 'None'}")
            print(f"  Parents: {', '.join(t['parents']) if t['parents'] else 'None'}")
            print(f"  Children: {', '.join(t['children']) if t['children'] else 'None'}")
            print(f"  Tenant: {t['tenant'] or 'None'}")
            print(f"  Priority: {t['priority'] if t['priority'] is not None else 'None'}")
            print()

    return 0


if __name__ == "__main__":
    sys.exit(main())
