"""
Delete ALL expenses in an existing Spliit group -- for starting clean.

Setup:
    pip install requests

Usage:
    python wipe_spliit_expenses.py GROUP_ID                # lists, asks for
                                                              # typed confirmation, then deletes
    python wipe_spliit_expenses.py GROUP_ID --dry-run        # only lists, deletes nothing
    python wipe_spliit_expenses.py GROUP_ID --yes            # skips the prompt (for scripts/cron)
    python wipe_spliit_expenses.py GROUP_ID --server-url URL # self-hosted instance

This is IRREVERSIBLE -- Spliit has no trash/undo for deleted expenses.
"""

import argparse
import sys

from spliit_api import Spliit

DEFAULT_SERVER_URL = "https://spliit.app"

CONFIRM_PHRASE = "DELETE ALL"


def list_expenses(client: Spliit) -> list:
    expenses = client.get_expenses()  # follows pagination internally
    # Most recent first, just for readability
    expenses.sort(key=lambda e: e.get("expenseDate", ""), reverse=True)
    return expenses


def print_expenses(expenses: list) -> None:
    if not expenses:
        print("No expenses found -- nothing to do.")
        return
    print(f"{len(expenses)} expense(s) in this group:\n")
    for e in expenses:
        date = (e.get("expenseDate") or "")[:10]
        amount = e.get("amount", 0) / 100
        paid_by = (e.get("paidBy") or {}).get("name", "?")
        print(f"  {date}  {amount:>10.2f}  paid by {paid_by:<10}  {e.get('title', '')}")
    print()


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("group_id", help="The Spliit group ID to wipe expenses from "
                                          "(the last segment of the group's URL)")
    parser.add_argument("--server-url", default=DEFAULT_SERVER_URL,
                         help=f"Spliit server URL (default: {DEFAULT_SERVER_URL})")
    parser.add_argument("--dry-run", action="store_true",
                         help="List expenses that would be deleted, but delete nothing")
    parser.add_argument("--yes", action="store_true",
                         help="Skip the typed confirmation prompt (use with care)")
    args = parser.parse_args()

    client = Spliit(group_id=args.group_id, server_url=args.server_url)
    group = client.get_group()
    print(f"Group: {group['name']} ({group['currency']})  [{args.group_id}]\n")

    expenses = list_expenses(client)
    print_expenses(expenses)

    if not expenses:
        return

    if args.dry_run:
        print("Dry run -- nothing was deleted.")
        return

    if not args.yes:
        print(f"This will PERMANENTLY delete all {len(expenses)} expense(s) above.")
        typed = input(f"Type '{CONFIRM_PHRASE}' to proceed: ").strip()
        if typed != CONFIRM_PHRASE:
            print("Confirmation text didn't match -- aborted, nothing deleted.")
            sys.exit(1)

    deleted, failed = 0, 0
    for e in expenses:
        expense_id = e["id"]
        try:
            client.remove_expense(expense_id)
            deleted += 1
            print(f"  Deleted {(e.get('expenseDate') or '')[:10]} {e.get('title', '')!r}")
        except Exception as exc:
            failed += 1
            print(f"  FAILED to delete {e.get('title', '')!r} ({expense_id}): {exc}")

    print(f"\nDone: {deleted} deleted, {failed} failed.")


if __name__ == "__main__":
    main()
