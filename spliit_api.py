"""
Minimal, dependency-light client for the Spliit API (https://spliit.app).

Spliit doesn't publish a REST API -- the app talks to its backend via tRPC
(https://trpc.io) with the superjson transformer. This module speaks that
protocol directly with `requests`, covering just what these scripts need:
reading a group and its participants, listing categories, creating expenses
(including reimbursements/settlements), listing expenses (with pagination),
and deleting expenses.

This replaces the third-party `spliit_client` PyPI package, which was
dropped for a few concrete reasons:
  - it has no way to create a settlement/reimbursement expense
    (isReimbursement is hardcoded to False)
  - its get_expenses() doesn't follow pagination, so it silently returns
    only the first page (~10) of a group's expenses
  - each of its methods is a thin wrapper around one HTTP call -- build a
    small JSON payload, GET/POST it to {base_url}/<procedure>, and unwrap
    result.data.json -- so reimplementing the handful of calls actually
    used here removes an external dependency for not much code.

Only `requests` is required: pip install requests
"""

from dataclasses import dataclass
from datetime import datetime, timezone
from enum import Enum
from typing import Dict, List, Optional, Tuple
from urllib.parse import urljoin
import json

import requests

DEFAULT_SERVER_URL = "https://spliit.app"


class SplitMode(str, Enum):
    EVENLY = "EVENLY"
    BY_SHARES = "BY_SHARES"
    BY_PERCENTAGE = "BY_PERCENTAGE"
    BY_AMOUNT = "BY_AMOUNT"


def get_categories(server_url: str = DEFAULT_SERVER_URL) -> Dict[str, int]:
    """Fetch the server's category list as {name: id}. Not group-scoped --
    categories are shared across all groups on a given Spliit instance."""
    resp = requests.get(f"{server_url}/api/trpc/categories.list", timeout=10)
    resp.raise_for_status()
    payload = resp.json()["result"]["data"]
    if isinstance(payload, dict) and "json" in payload:
        payload = payload["json"]
    return {c["name"]: c["id"] for c in payload["categories"]}


@dataclass
class Spliit:
    """Client for one Spliit group."""

    group_id: str
    server_url: str = DEFAULT_SERVER_URL

    @property
    def base_url(self) -> str:
        return urljoin(self.server_url, "/api/trpc")

    def get_group(self) -> Dict:
        """Fetch group details: name, currency, participants, etc."""
        params_input = {
            "0": {"json": {"groupId": self.group_id}},
            "1": {"json": {"groupId": self.group_id}},
        }
        resp = requests.get(
            f"{self.base_url}/groups.get,groups.getDetails",
            params={"batch": "1", "input": json.dumps(params_input)},
            timeout=10,
        )
        resp.raise_for_status()
        return resp.json()[0]["result"]["data"]["json"]["group"]

    def get_participants(self) -> Dict[str, str]:
        """Return {participant_name: participant_id} for the group."""
        group = self.get_group()
        return {p["name"]: p["id"] for p in group["participants"]}

    def get_expenses(self) -> List[Dict]:
        """Fetch every expense in the group, following pagination (the
        server paginates groups.expenses.list at ~10 per page)."""
        all_expenses = []
        cursor = None
        while True:
            query = {"groupId": self.group_id}
            if cursor is not None:
                query["cursor"] = cursor
            params = {"batch": "1", "input": json.dumps({"0": {"json": query}})}
            resp = requests.get(f"{self.base_url}/groups.expenses.list",
                                 params=params, timeout=10)
            resp.raise_for_status()
            data = resp.json()[0]["result"]["data"]["json"]
            all_expenses.extend(data["expenses"])
            if not data.get("hasMore"):
                break
            cursor = data["nextCursor"]
        return all_expenses

    def add_expense(self, title: str, amount: int, paid_by: str,
                     paid_for: List[Tuple[str, int]],
                     split_mode: SplitMode = SplitMode.EVENLY,
                     expense_date: Optional[datetime] = None,
                     notes: str = "", category: int = 0,
                     is_reimbursement: bool = False) -> str:
        """
        Add an expense to the group, or a settlement payment when
        is_reimbursement=True (used e.g. for Splitwise's "Payment" rows).

        amount and each paid_for share are in cents. paid_for is a list of
        (participant_id, shares) tuples; for EVENLY, shares is just a
        nonzero weight (1 works), for BY_AMOUNT it's the exact cents owed.
        """
        if expense_date is None:
            expense_date = datetime.now(timezone.utc)

        formatted_paid_for = [{"participant": pid, "shares": shares}
                               for pid, shares in paid_for]
        formatted_date = (expense_date.strftime("%Y-%m-%dT%H:%M:%S.")
                          + f"{expense_date.microsecond // 10000:03d}Z")

        expense_form_values = {
            "expenseDate": formatted_date,
            "title": title,
            "category": category,
            "amount": amount,
            "paidBy": paid_by,
            "paidFor": formatted_paid_for,
            "splitMode": split_mode.value,
            "saveDefaultSplittingOptions": False,
            "isReimbursement": is_reimbursement,
            "documents": [],
            "notes": notes,
        }
        json_data = {
            "0": {
                "json": {
                    "groupId": self.group_id,
                    "expenseFormValues": expense_form_values,
                    "participantId": "None",
                },
                "meta": {"values": {"expenseFormValues.expenseDate": ["Date"]}},
            }
        }
        resp = requests.post(f"{self.base_url}/groups.expenses.create",
                              params={"batch": "1"}, json=json_data, timeout=10)
        resp.raise_for_status()
        return resp.content.decode()

    def remove_expense(self, expense_id: str) -> Dict:
        """Permanently delete an expense. No undo."""
        json_data = {"0": {"json": {"groupId": self.group_id, "expenseId": expense_id}}}
        resp = requests.post(f"{self.base_url}/groups.expenses.delete",
                              params={"batch": "1"}, json=json_data, timeout=10)
        resp.raise_for_status()
        return resp.json()[0]["result"]["data"]["json"]
