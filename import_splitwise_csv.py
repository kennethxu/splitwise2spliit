"""
Bulk-import expenses from a Splitwise CSV export into an existing Spliit group.

Setup:
    pip install spliit_client requests

Usage:
    python import_splitwise_csv.py GROUP_ID path/to/export.csv
    python import_splitwise_csv.py GROUP_ID path/to/export.csv --server-url https://spliit.mydomain.com

Expected CSV format (Splitwise's "Export as CSV"):

    Date,Description,Category,Cost,Currency,<Person1>,<Person2>,...
    <blank line>
    2026-07-06,Travel Insurance,Insurance,261.32,USD,146.36,-146.36,0,0
    2026-08-26,Car Rental,Car,1231.46,USD,923.595,-307.865,-307.865,-307.865
    ...
    <blank line>
    2026-09-13,Total balance, , ,USD,9512.3975,-3410.4225,...

Each person's column is their *net balance change* from that expense
(Splitwise's convention): positive = they fronted money and are owed it
back, negative = they owe their share, 0/blank = not part of that expense.
This script reconstructs who paid and each participant's exact share from
those deltas -- see `parse_expenses()`. Spliit only supports a single payer
per expense, so rows with more than one positive value (split payments) are
skipped with a warning rather than guessed at.

Two refinements on top of the raw reconstruction:
  1. Participants whose computed share comes out to (effectively) zero are
     dropped from the split entirely, rather than being sent as a $0 share.
  2. If every remaining participant's share is equal, the expense is added
     with SplitMode.EVENLY instead of BY_AMOUNT, so it matches what you'd
     get from ticking "split equally" in the Spliit UI (and sidesteps
     rounding drift across cents).

Category mapping: Splitwise category names are mapped to Spliit category
names via CATEGORY_NAME_MAP plus category_mapping.json (created next to
this script). The first time an unrecognized Splitwise category shows up,
you're prompted interactively to pick the matching Spliit category; that
choice is saved to category_mapping.json immediately, so future runs (with
that same Splitwise category) use it automatically without asking again.
"""

from datetime import datetime
import argparse
import csv
import json
import os

import requests
from spliit_client import Spliit, SplitMode

DEFAULT_SERVER_URL = "https://spliit.app"

# Seed mappings for Splitwise category names -> Spliit category names where
# they differ. These are the starting point; anything learned interactively
# (see resolve_category/prompt_for_category) is persisted to
# CATEGORY_MAPPING_FILE and merged on top of these on every run, so you're
# only ever prompted once per new Splitwise category name.
CATEGORY_NAME_MAP = { }
CATEGORY_MAPPING_FILE = "category_mapping.json"

# Shares within this many dollars of each other are treated as "equal"
# (covers float/rounding noise like 307.865 vs 307.8650000000001).
EQUAL_SHARE_TOLERANCE = 0.01
# A computed share smaller than this (in dollars) is treated as zero and
# the participant is dropped from the split.
ZERO_SHARE_TOLERANCE = 0.005


def get_categories(server_url: str = DEFAULT_SERVER_URL) -> dict:
    resp = requests.get(f"{server_url}/api/trpc/categories.list", timeout=10)
    resp.raise_for_status()
    payload = resp.json()["result"]["data"]
    if isinstance(payload, dict) and "json" in payload:
        payload = payload["json"]
    return {c["name"]: c["id"] for c in payload["categories"]}


def load_category_mapping(path: str = CATEGORY_MAPPING_FILE) -> dict:
    """Start from the built-in CATEGORY_NAME_MAP, then layer on anything
    already learned and saved to `path` from a previous run."""
    mapping = dict(CATEGORY_NAME_MAP)
    if os.path.exists(path):
        with open(path, "r", encoding="utf-8") as f:
            mapping.update(json.load(f))
    return mapping


def save_category_mapping(mapping: dict, path: str = CATEGORY_MAPPING_FILE) -> None:
    with open(path, "w", encoding="utf-8") as f:
        json.dump(mapping, f, indent=2, sort_keys=True, ensure_ascii=False)


def prompt_for_category(splitwise_name: str, categories: dict) -> str:
    """Ask the user which Spliit category a new Splitwise category name
    should map to. Returns a Spliit category name (defaults to 'General'
    on a blank answer)."""
    names = sorted(categories)
    print(f"\n  No mapping yet for Splitwise category {splitwise_name!r}.")
    print("  Choose a Spliit category:")
    for i, n in enumerate(names, 1):
        print(f"    {i:>3}. {n}")
    while True:
        choice = input("  Enter a number above, type a category name exactly, "
                        "or leave blank for 'General': ").strip()
        if choice == "":
            return "General"
        if choice.isdigit() and 1 <= int(choice) <= len(names):
            return names[int(choice) - 1]
        if choice in categories:
            return choice
        print(f"  {choice!r} isn't a valid choice, try again.")


def resolve_category(name: str, categories: dict, mapping: dict,
                      mapping_path: str = CATEGORY_MAPPING_FILE) -> int:
    name = (name or "").strip()

    if name in mapping:
        spliit_name = mapping[name]
    elif name in categories:
        spliit_name = name
    else:
        spliit_name = prompt_for_category(name, categories)
        mapping[name] = spliit_name
        save_category_mapping(mapping, mapping_path)
        print(f"  Remembered: {name!r} -> {spliit_name!r} (saved to {mapping_path})")

    if spliit_name not in categories:
        print(f"  Warning: mapped category {spliit_name!r} not found in "
              f"Spliit, using 'General'")
        spliit_name = "General"
    return categories.get(spliit_name, 0)


def parse_expenses(csv_path: str):
    """Yield one dict per real expense row (skips blank lines and the
    trailing 'Total balance' row).

    Each yielded dict has:
        date, description, category, cost_cents, payer_name,
        split_mode ("EVENLY" or "BY_AMOUNT"),
        shares: {participant_name: value}
            - for EVENLY, value is a weight (1 for everyone, ignored by
              Spliit -- it just needs to be present and nonzero)
            - for BY_AMOUNT, value is the participant's share in cents,
              and all values sum exactly to cost_cents
    """
    with open(csv_path, newline="", encoding="utf-8-sig") as f:
        reader = csv.reader(f)
        header = next(reader)
        assert header[:5] == ["Date", "Description", "Category", "Cost", "Currency"], (
            f"Unexpected CSV header: {header}"
        )
        person_columns = [h.strip() for h in header[5:]]

        for row in reader:
            if not row or not any(cell.strip() for cell in row):
                continue  # blank separator line

            date_str, description = row[0].strip(), row[1].strip()
            if not date_str or description.lower().startswith("total"):
                continue  # totals row

            category = row[2].strip()
            try:
                cost = float(row[3])
            except ValueError:
                continue  # not a real expense row

            deltas = {}
            for name, raw in zip(person_columns, row[5:5 + len(person_columns)]):
                raw = raw.strip()
                if raw in ("", "0"):
                    continue
                deltas[name] = float(raw)

            if not deltas:
                continue

            # payer(s): positive delta means they fronted the full cost, so
            #   owed_share = cost - delta
            # non-payers: negative delta means owed_share = -delta
            payers = {n: d for n, d in deltas.items() if d > 0}
            if len(payers) != 1:
                print(f"  Warning: expected exactly one payer for "
                      f"{date_str} {description!r}, got {list(payers)}; skipping")
                continue
            payer_name = next(iter(payers))

            raw_shares = {}
            for name, delta in deltas.items():
                raw_shares[name] = (cost - delta) if name == payer_name else -delta

            # (1) drop anyone whose computed share is effectively zero
            raw_shares = {n: s for n, s in raw_shares.items()
                          if abs(s) >= ZERO_SHARE_TOLERANCE}
            if not raw_shares:
                print(f"  Warning: no nonzero shares for "
                      f"{date_str} {description!r}; skipping")
                continue

            cost_cents = round(cost * 100)
            values = list(raw_shares.values())

            # (2) equal shares -> EVENLY, otherwise exact BY_AMOUNT split
            if max(values) - min(values) < EQUAL_SHARE_TOLERANCE:
                split_mode = "EVENLY"
                shares = {n: 1 for n in raw_shares}
            else:
                split_mode = "BY_AMOUNT"
                names = list(raw_shares)
                shares, running = {}, 0
                for name in names[:-1]:
                    c = round(raw_shares[name] * 100)
                    shares[name] = c
                    running += c
                shares[names[-1]] = cost_cents - running

            yield {
                "date": datetime.strptime(date_str, "%Y-%m-%d"),
                "description": description,
                "category": category,
                "cost_cents": cost_cents,
                "payer_name": payer_name,
                "split_mode": split_mode,
                "shares": shares,
            }


def main():
    parser = argparse.ArgumentParser(description=__doc__,
                                      formatter_class=argparse.RawDescriptionHelpFormatter)
    parser.add_argument("group_id", help="The Spliit group ID to import into "
                                          "(the last segment of the group's URL)")
    parser.add_argument("csv_path", help="Path to the Splitwise CSV export")
    parser.add_argument("--server-url", default=DEFAULT_SERVER_URL,
                         help=f"Spliit server URL (default: {DEFAULT_SERVER_URL})")
    args = parser.parse_args()

    client = Spliit(group_id=args.group_id, server_url=args.server_url)
    group = client.get_group()
    print(f"Connected to group: {group['name']} ({group['currency']})  [{args.group_id}]")

    participants = client.get_participants()  # name -> id
    categories = get_categories(args.server_url)
    category_mapping = load_category_mapping()

    created, skipped = 0, 0
    for expense in parse_expenses(args.csv_path):
        payer_id = participants.get(expense["payer_name"])
        if payer_id is None:
            print(f"  Skipping {expense['description']!r}: unknown payer "
                  f"{expense['payer_name']!r}")
            skipped += 1
            continue

        paid_for, missing = [], False
        for name, value in expense["shares"].items():
            pid = participants.get(name)
            if pid is None:
                print(f"  Skipping {expense['description']!r}: unknown "
                      f"participant {name!r}")
                missing = True
                break
            paid_for.append((pid, value))
        if missing:
            skipped += 1
            continue

        split_mode = (SplitMode.EVENLY if expense["split_mode"] == "EVENLY"
                      else SplitMode.BY_AMOUNT)
        category_id = resolve_category(expense["category"], categories, category_mapping)

        expense_id = client.add_expense(
            title=expense["description"],
            amount=expense["cost_cents"],
            paid_by=payer_id,
            paid_for=paid_for,
            split_mode=split_mode,
            expense_date=expense["date"],
            category=category_id,
        )
        print(f"  Added {expense['date'].date()} {expense['description']!r} "
              f"[{expense['split_mode']}] -> {expense_id}")
        created += 1

    print(f"\nDone: {created} expense(s) added, {skipped} skipped.")


if __name__ == "__main__":
    main()
