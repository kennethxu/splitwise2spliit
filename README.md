# splitwise2spliit

Utility script to import Splitwise exported CSV file into an existing [Spliit](https://spliit.app)
group.

## Files

| File | Purpose |
|---|---|
| `spliit_api.py` | Shared client -- talks to Spliit's tRPC API directly. Required by the other scripts. |
| `import_splitwise_csv.py` | Bulk-import expenses from a Splitwise CSV export into a new or existing Spliit group. |
| `wipe_spliit_expenses.py` | Delete all expenses in a Spliit group, to start clean. |
| `category_mapping.json` | Splitwise -> Spliit category name mapping, used by the import script. |

## Setup

```
pip install requests
```

`spliit_api.py` must be in the same directory as the scripts below -- they
import it as a local module, not a package. `requests` is the only external
dependency; there's no third-party Spliit client library involved.

---

## import_splitwise_csv.py

Reads a Splitwise CSV export and creates the equivalent expenses in a Spliit
group -- either an existing one or a brand new one created on the fly.

### Usage

```
# Import into an existing group
python import_splitwise_csv.py GROUP_ID path/to/export.csv
python import_splitwise_csv.py GROUP_ID path/to/export.csv --server-url https://spliit.mydomain.com

# Create a new group and import into it
python import_splitwise_csv.py --create-group "Banff Trip" path/to/export.csv
python import_splitwise_csv.py --create-group "Banff Trip" --currency "$" path/to/export.csv
```

- `GROUP_ID` is the last segment of the group's URL, e.g.
  `spliit.app/groups/RrYePXN2GBpSMW1EPjpjH` -> `RrYePXN2GBpSMW1EPjpjH`.
  Pass exactly one of `GROUP_ID` or `--create-group NAME` -- not both, not
  neither.
- `--create-group NAME` creates a new group with that name instead of
  importing into an existing one. Its participants are seeded directly from
  the CSV's header row, in one call, before any expenses are imported.
  `--currency` (default `$`) sets the new group's currency symbol.
- `--server-url` defaults to `https://spliit.app`; set it for a self-hosted
  instance.
- Participant names in the CSV are matched exactly against the group's
  existing participants. A name that doesn't match is **added to the group
  automatically** (see "Participant handling" below) unless
  `--no-create-participants` is passed, in which case that expense is
  skipped instead.

### Expected CSV format

Splitwise's "Export as CSV", unmodified:

```
Date,Description,Category,Cost,Currency,<Person1>,<Person2>,...
<blank line>
2026-07-06,Travel Insurance,Insurance,261.32,USD,146.36,-146.36,0,0
2026-08-26,Car Rental,Car,1231.46,USD,923.595,-307.865,-307.865,-307.865
...
<blank line>
2026-09-13,Total balance, , ,USD,9512.3975,-3410.4225,...
```

Blank separator lines and the trailing "Total balance" row are skipped
automatically.

### How the split is reconstructed

Splitwise's export doesn't give raw payment amounts -- each person's
column is their *net balance change* from that expense: positive means
they fronted money and are owed it back, negative means they owe their
share, and `0`/blank means they weren't part of it. The script works
backward from those deltas to figure out who paid and what each person's
exact share was.

A few rules apply on top of that reconstruction:

- **Single payer only.** Spliit only supports one payer per expense. If a
  row has more than one positive value (a Splitwise expense paid by
  multiple people), it's skipped with a warning rather than guessed at.
- **Zero shares are dropped.** A participant whose computed share comes
  out to (effectively) $0 -- e.g. someone who paid on behalf of others but
  owes nothing themselves -- is left out of the split entirely, rather
  than sent as a $0 entry.
- **Even splits use `SplitMode.EVENLY`.** If every remaining participant's
  share is within a cent of each other, the expense is added as an even
  split instead of exact per-person amounts. A 1-cent spread is normal --
  it's just the leftover cent(s) from dividing a cost by N -- so this is
  compared at the cent (integer) level rather than as dollar-floats, which
  avoids floating-point boundary noise that could otherwise misclassify an
  even split as uneven. Genuinely uneven splits still use
  `SplitMode.BY_AMOUNT` with exact cents.
- **"Payment" rows become settlements, not expenses.** A row whose
  Category is `Payment` is a Splitwise settle-up (one person directly
  repaying another), not a purchase. These are created with
  `isReimbursement=True` so they net out balances correctly instead of
  showing up as a line-item expense.

### Participant handling

A name in the CSV that isn't already a participant in the Spliit group is
added to the group automatically (via the same request the group's
"Edit" screen uses). This is on by default; pass
`--no-create-participants` to skip those expenses instead, with a warning,
if you'd rather add people manually.

A newly added participant's id is cached for the rest of the run, so if
the same new name appears in several rows it's only created once. When
using `--create-group`, every name in the CSV header is seeded into the
group at creation time instead, so this mainly comes up for
`GROUP_ID`-based imports into a group that doesn't yet have everyone.

### Category mapping

Splitwise and Spliit mostly use the same category names, but not always
(casing differs, and Splitwise's ambiguous "Other" subcategories get
disambiguated in exports as `"<Group> - Other"`, e.g. `Entertainment -
Other`). Mappings live in **`category_mapping.json`**, next to the script:

- The included file already covers every standard Splitwise/Spliit
  category.
- If a category shows up that isn't in the file, you'll be prompted
  interactively to pick the matching Spliit category (or leave it blank
  for "General"). Your answer is saved to `category_mapping.json`
  immediately, so you're only ever asked once per category name, ever.
- The script has no built-in fallback mapping anymore -- if you run it
  somewhere without `category_mapping.json` present, every category will
  be prompted for fresh.

### Output

For each expense, a line like:

```
  Added 2025-08-07 'Gas' $39.31 [EVENLY] -> <expense_id>
```

`[EVENLY]` / `[BY_AMOUNT]` / `[PAYMENT]` shows how it was classified. The
amount is included specifically to help distinguish duplicate titles on
the same or different days (e.g. multiple "Gas" stops on a trip).

Rows that can't be imported (unknown payer/participant name when
`--no-create-participants` is set, ambiguous multi-payer rows) are skipped
with a warning and don't stop the run. The run ends with a summary line
including a direct link to the group:

```
Done: 17 expense(s) added, 0 skipped to group: https://spliit.app/groups/llKlrlQLitFxPmKv4TBXU
```

---

## wipe_spliit_expenses.py

Deletes **all** expenses in a Spliit group. Useful for clearing a group
before re-running a corrected import. **This is irreversible** -- Spliit
has no trash or undo for deleted expenses.

### Usage

```
python wipe_spliit_expenses.py GROUP_ID                # list, confirm, delete
python wipe_spliit_expenses.py GROUP_ID --dry-run       # list only, delete nothing
python wipe_spliit_expenses.py GROUP_ID --yes           # skip the confirmation prompt
python wipe_spliit_expenses.py GROUP_ID --server-url https://spliit.mydomain.com
```

### Safety behavior

1. Always fetches and prints every expense in the group first (date,
   amount, payer, title), so you can see exactly what's about to be
   deleted. This follows pagination internally, so it covers the whole
   group regardless of size.
2. Without `--yes`, you must type `DELETE ALL` exactly to proceed --
   anything else aborts with nothing deleted.
3. `--dry-run` lists only and never deletes, regardless of `--yes`.
4. Deletes one at a time, reporting each success/failure individually
   rather than stopping the whole run on one error.

### Typical re-import flow

```
python wipe_spliit_expenses.py GROUP_ID --dry-run
python wipe_spliit_expenses.py GROUP_ID
python import_splitwise_csv.py GROUP_ID corrected_export.csv
```
