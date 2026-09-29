"""AutomationsBackend: the user's automations as `.agents/user/automations/automations.json`.

The rows live in Postgres; ``services.automations.file`` serializes them and
turns a save into creates, updates and deletes in one transaction. Flash, which
has no filesystem, manages the same rows through the automation tools.
"""

from __future__ import annotations

from ptc_agent.agent.backends.db_json_route import README_FILE, DbJsonRoute
from ptc_agent.core.paths import SandboxLayout
from src.server.services.automations.file import FILE_NAME as AUTOMATIONS_FILE
from src.server.services.automations.file import AutomationsFile

__all__ = ["AUTOMATIONS_FILE", "README_FILE", "AutomationsBackend"]

_README_CONTENT = """\
# Automations

`automations.json` holds every automation the user has: agent runs that start on
a schedule or when a price condition is met, without the user present. It is
the live database, not a copy. Reads are fresh. A write is checked, then saved
all at once, and the Write/Edit result lists every automation it created,
updated or deleted. If anything is refused, nothing is saved.

Code sees the file at the same path when the sandbox has the file mount. A
program saves it whole, deleting any automation it leaves out, and the
command's result lists the changes or why the save was refused. Write it in
place: `sed -i` and write-then-rename helpers need a new file in this folder,
which fails with Permission denied.

**Read the file before you Write it.** A Write is refused if you haven't read
the file in this run, or if it changed since (the user's Automations page, a
run, another turn). The user's answer to a question starts a new run, so Read
again after asking. A Write that leaves automations out deletes them, so it also
needs a Read that showed the whole file, without offset or limit. Edit needs no
Read of its own, since `old_string` must match the file, and is refused the same
way if the file changed since you read it. After any write, Read again before
the next one.

**Confirm with the user first.** Automations run unattended, so before a write
that creates or deletes one, summarize it and get a yes. Pin down:
- the schedule: "every morning" means which days, what time, which timezone?
- the instruction: it runs with no one to ask. Make it self-contained: which
  tickers, which metrics, what format.
- the thread: a fresh one each run, one ongoing thread, or this conversation.
- delivery: in-app only, or also a channel such as Slack.

Change only what the user asked for; suggest any other edit instead of making
it. This file is the record of the user's automations: Read it when you need
them rather than keeping their schedules or statuses in notes or memory, where
they go stale.

## Editing

- **Create:** append an object without `automation_id`. The server assigns one.
- **Update:** change fields in place. A field you leave out keeps its value;
  `null` clears `description` or `llm_model`.
- **Pause / resume:** set `status` to `"paused"` or `"active"`.
- **Delete:** remove the object from the array. Its run history goes with it
  and cannot be restored.
- `state` is kept by the server and moves as runs happen, which never makes a
  write conflict. A write ignores it, and the result notes an edit to it: to
  move a run, change `cron_expression` or `next_run_at`.
- Nothing in the file runs an automation now. The user can, with Run now on
  the Automations page.

With Edit, make `old_string` the whole `{ ... }` object you are changing: every
automation shares the same keys, so a single line such as `"status": "active"`
matches several of them. Use Write to make many changes at once.

## Example

```json
{
  "automations": [
    {
      "automation_id": "6f1c2b1e-5d0a-4a57-9a8e-2f0b7c1d9e10",
      "name": "Morning market brief",
      "description": null,
      "status": "active",
      "trigger_type": "cron",
      "cron_expression": "0 9 * * 1-5",
      "timezone": "America/New_York",
      "instruction": "Summarize overnight moves for my watchlist: top movers, news, one line each.",
      "agent_mode": "flash",
      "workspace_id": null,
      "thread": "new",
      "llm_model": null,
      "delivery": ["slack"],
      "max_failures": 3,
      "state": {
        "next_run_at": "2026-09-29T09:00:00-04:00",
        "last_run": {"status": "completed", "at": "2026-09-28T09:01:12-04:00"}
      }
    },
    {
      "name": "AAPL below 200",
      "trigger_type": "price",
      "trigger_config": {
        "symbol": "AAPL",
        "conditions": [{"type": "price_below", "value": 200}]
      },
      "instruction": "AAPL just fell below $200. Summarize the news and analyst moves behind it.",
      "agent_mode": "flash"
    }
  ]
}
```

The second entry is a new automation: no `automation_id`, no `state`, and the
fields it leaves out take their defaults.

## Fields

| Field | Required on create | Notes |
|-------|--------------------|-------|
| automation_id  | no (server sets it) | Identifies the automation. Never change it. |
| name           | yes | Up to 255 characters. |
| description    | no  | Free text. |
| status         | no  | `active` (default) or `paused`. The server also sets `executing` (a price alert firing now), `completed` (a one-time or one-shot run finished) and `disabled` (switched off, see `state.disable_reason`); set `active` to resume a disabled one. A `completed` one won't run on its own again: add a new entry for another run. |
| trigger_type   | no  | `cron`, `once` or `price`; inferred from the schedule field you give. Fixed once created: to change it, create a new automation and delete the old one. |
| cron_expression| cron | 5-field cron on the automation's clock: `0 9 * * 1-5` weekdays 9:00, `0 */4 * * *` every 4 hours, `30 8 1 * *` the 1st at 8:30. No seconds field; a step can't span fields (no "every 90 minutes"). |
| next_run_at    | once | A future ISO time with a time of day, e.g. `2026-10-01T09:00:00`. Without an offset it is read in `timezone`. |
| trigger_config | price | See Price alerts. |
| timezone       | no  | IANA region name, e.g. `America/New_York` (not `EST`, which ignores daylight saving). Defaults to the user's timezone. Changing it keeps a one-time automation's moment, since `next_run_at` carries its offset. |
| instruction    | yes | The prompt each run executes. |
| agent_mode     | no  | `ptc` (default here): runs as you do, in `workspace_id`, with code execution. `flash`: fast, no sandbox; best for price alerts and quick lookups. |
| workspace_id   | ptc | Defaults to this workspace. |
| thread         | no  | `new` (default): a fresh thread each run. `persistent`: one thread that every run continues, created by the first run (`state.last_run.thread_id` names it). `current`: continue in this conversation. A thread id: continue in that thread. |
| llm_model      | no  | A model the user can run; `null` uses their default. An unknown name is refused with the list. |
| delivery       | no  | Channels to post results to besides the app, e.g. `["slack"]`; `[]` for none. |
| max_failures   | no  | 1–100, default 3. Consecutive failed runs before it is disabled. |

## Price alerts

```json
"trigger_config": {
  "symbol": "TSLA",
  "conditions": [
    {"type": "pct_change_above", "value": 5, "reference": "previous_close"}
  ],
  "retrigger": {"mode": "recurring", "cooldown_seconds": 14400}
}
```

- `symbol`: a bare US stock or index ticker (`AAPL`, `SPX`), no `^` or `I:` prefix.
  There is no crypto, currency or futures feed, so no alert can watch those.
- `conditions`: one or more, all of which must hold. `type` is `price_above`,
  `price_below` (a dollar `value`), `pct_change_above` or `pct_change_below` (a
  percent `value`, always positive). `reference` sets the base for a percent:
  `previous_close` (default) or `day_open`.
- `retrigger.mode`: `one_shot` (default) fires once and completes. `recurring`
  re-arms after `cooldown_seconds` (at least 14400, i.e. 4 hours), or the next
  trading day when it is left out. Default to `one_shot` unless the user wants
  repeated alerts.

Repeat the symbol, condition, value and retrigger mode back to the user before
you save a price alert.

## state (read-only)

- `next_run_at`: when a cron automation runs next.
- `last_run`: its newest run, absent before the first. `status` is `pending`,
  `waiting` (queued behind the turn running on its thread), `running`,
  `completed`, `failed`, `timeout` or `skipped`, as of `at`. `thread_id` is the
  conversation the run posted to, and `excerpt` is the start of its answer once
  it completes. `error` is the start of a failed run's error. `failure_reason`
  `usage_limit` is a usage limit and never counts toward `max_failures`;
  `server_error` and `interrupted` mean the service failed or cut the run off,
  so nothing in the automation needs changing. `skip_reason` says why a run was
  skipped: `user`, `thread_busy` (its thread was running another turn) or
  `interrupted`.
  `dismissed: true` means the user has already seen the failure and set it aside.
- `failure_count`: consecutive failures so far.
- `disable_reason`: why a `disabled` automation was switched off.
  `provider_auth`: the model provider rejected the user's own key; resume it
  once the key is fixed. `max_failures`: it failed `max_failures` times in a row.
"""


class AutomationsBackend(DbJsonRoute):
    """Filesystem surface backed by the `automations` table."""

    directory = SandboxLayout.AUTOMATIONS_DIR
    files = {AUTOMATIONS_FILE: AutomationsFile()}
    readme_content = _README_CONTENT

    source = "automations_backend"
    read_failure = "Failed to read automations data"
    read_only = (
        "The automations JSON file is read-only through the file panel. "
        "Edit via the Automations page or ask the agent to update it."
    )
    undeletable = (
        "The automations JSON file cannot be deleted through the file panel. "
        "Manage automations via the Automations page."
    )
