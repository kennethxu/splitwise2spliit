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

    @classmethod
    def create_group(cls, name: str, currency: str = "$",
                      participants: Optional[List[str]] = None,
                      currency_code: Optional[str] = None,
                      information: Optional[str] = None,
                      server_url: str = DEFAULT_SERVER_URL) -> "Spliit":
        """
        Create a new Spliit group and return a client bound to it.

        Mirrors groups.create -- the same groupFormValues shape as
        groups.update, minus the id-based update/delete diffing since
        there's nothing existing to preserve. Confirmed against Spliit's
        actual server-side createGroup() (src/lib/api.ts): it destructures
        only `name` from each submitted participant and always assigns
        its own id, so unlike add_participant this one never risks the
        "id present but doesn't match anything" pitfall -- but participant
        ids are still never sent here, to stay consistent with what the
        server actually reads.

        The Zod schema backing this endpoint requires at least one
        participant, so `participants` can't be left empty.
        """
        if not participants:
            raise ValueError("create_group requires at least one participant name")

        group_form_values = {
            "name": name,
            "currency": currency,
            "participants": [{"name": p} for p in participants],
        }
        if information:
            group_form_values["information"] = information
        if currency_code:
            group_form_values["currencyCode"] = currency_code

        json_data = {"0": {"json": {"groupFormValues": group_form_values}}}
        base_url = urljoin(server_url, "/api/trpc")
        resp = requests.post(f"{base_url}/groups.create",
                              params={"batch": "1"}, json=json_data, timeout=10)
        resp.raise_for_status()

        try:
            body = resp.json()
            if isinstance(body, list) and body and "error" in body[0]:
                raise RuntimeError(f"groups.create returned an error: {body[0]['error']}")
            group_id = body[0]["result"]["data"]["json"]["groupId"]
        except (ValueError, KeyError, IndexError, TypeError) as exc:
            raise RuntimeError(
                f"groups.create returned {resp.status_code} but the response "
                f"didn't contain a groupId as expected. Response body: "
                f"{resp.text[:1000]!r}"
            ) from exc

        client = cls(group_id=group_id, server_url=server_url)

        # Verify the write actually took effect and every requested
        # participant is really there, rather than trusting the response
        # alone (see add_participant for why this matters here).
        created = client.get_participants()
        missing = [p for p in participants if p not in created]
        if missing:
            raise RuntimeError(
                f"groups.create reported success (group {group_id}) but "
                f"these participants are missing from it afterward: {missing}"
            )
        return client

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

    def add_participant(self, name: str) -> str:
        """
        Add a new participant to the group, returning their new id.

        Spliit has no dedicated "add participant" endpoint -- the group
        settings form edits the whole group (name, currency, participant
        list) in one groups.update mutation. So this fetches the group's
        current participant list, appends the new person to it (existing
        participants kept as-is, ids included), and sends the full form
        back.

        Confirmed against Spliit's actual server-side updateGroup()
        (src/lib/api.ts): it decides "new" purely by whether a submitted
        participant's id is undefined -- anyone WITH an id is routed to
        updateMany (matched against an existing row; a client-made-up id
        that matches nothing just silently updates zero rows and reports
        success), and only entries with id truly absent go through
        createMany, where the server assigns its own id via randomId().
        So the new participant here is sent with id omitted entirely
        (never a client-generated one), letting the server assign the
        real id -- which this method then discovers by re-fetching the
        group and looking the new name up by name.

        A 200 response from this endpoint doesn't guarantee the write
        actually took effect (tRPC can report success with the mutation
        still a no-op, as above), so this re-fetches the group afterward
        and confirms the new participant is really there before
        returning -- raising with the server's response body if not.
        """
        group = self.get_group()

        participants = [{"id": p["id"], "name": p["name"]} for p in group["participants"]]
        participants.append({"name": name})  # no "id" -- server assigns one

        group_form_values = {
            "name": group["name"],
            "currency": group["currency"],
            "participants": participants,
        }
        if group.get("information"):
            group_form_values["information"] = group["information"]
        if group.get("currencyCode"):
            group_form_values["currencyCode"] = group["currencyCode"]

        json_data = {
            "0": {
                "json": {
                    "groupId": self.group_id,
                    "groupFormValues": group_form_values,
                }
            }
        }
        resp = requests.post(f"{self.base_url}/groups.update",
                              params={"batch": "1"}, json=json_data, timeout=10)
        resp.raise_for_status()

        # tRPC can return HTTP 200 with a per-call error embedded in the
        # batch response body, so a clean status code alone isn't proof of
        # success -- check the body for an explicit error too.
        try:
            body = resp.json()
            if isinstance(body, list) and body and "error" in body[0]:
                raise RuntimeError(
                    f"groups.update for participant {name!r} returned an "
                    f"error: {body[0]['error']}"
                )
        except ValueError:
            pass  # non-JSON response body; nothing more to check here

        # Verify the write actually took effect, rather than trusting the
        # response alone.
        updated = self.get_participants()
        if name not in updated:
            raise RuntimeError(
                f"groups.update for participant {name!r} returned {resp.status_code} "
                f"but the participant isn't in the group afterward. "
                f"Response body: {resp.text[:1000]!r}"
            )
        return updated[name]

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
