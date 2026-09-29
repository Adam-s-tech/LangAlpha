"""The user's automations as one JSON document, and a save of it as row changes.

Serves ``AutomationsBackend`` (`.agents/user/automations/automations.json`). The
document is ``{"automations": [...]}``, oldest first. Each entry is the
automation's definition, which the agent may edit, plus a read-only ``state``
block the server keeps (next cron run, newest run, strikes).

A save is planned against the rows as they stand: an entry without
``automation_id`` is created, a changed entry is updated (``status`` pauses and
resumes), and an automation missing from the document is deleted. The
document's own shape is checked first (``AutomationsFile.parse``), before
the save reads or locks anything. The plan then checks the guards against an
agent's slips (a past or date-only time, a fixed-offset zone) and what only
the file can see, such as a completed automation keeping its schedule; every
rule an automation obeys anywhere is ``lifecycle``'s, which the commit runs
for each change. The report names each change, because the agent cannot see
the rows it touched any other way.

The version is a hash of the definitions only. ``state`` moves with every
firing and every scheduler poll, and hashing it would refuse most writes.
"""

from __future__ import annotations

import hashlib
import json
from collections.abc import Awaitable, Callable
from dataclasses import dataclass, field
from datetime import UTC, datetime
from typing import Any, Literal
from uuid import UUID

from fastapi import HTTPException
from pydantic import BaseModel, ConfigDict, ValidationError, ValidationInfo, field_validator

from ptc_agent.agent.backends.db_json_route import DbJsonFile, ErrorType, Plan, UserDataValidationError
from ptc_agent.core.paths import USER_DATA_FILES, SandboxLayout
from ptc_agent.core.sandbox.livefs_mount import CallContext
from src.server.database import automation as auto_db
from src.server.models.automation import (
    AutomationCreate,
    AutomationUpdate,
    MarketType,
    PriceTriggerConfig,
    future_run,
    on_clock,
    parse_delivery,
    region_zone,
    run_time,
    thread_fields,
)
from src.server.services.automations import lifecycle
from src.server.services.llm import user_models
from src.utils.timezone_utils import zone_or_none

(FILE_NAME,) = USER_DATA_FILES[SandboxLayout.AUTOMATIONS_DIR]

# The fields an entry may carry, in the order they are written.
_EDITABLE = (
    "name",
    "description",
    "status",
    "trigger_type",
    "cron_expression",
    "next_run_at",
    "trigger_config",
    "timezone",
    "instruction",
    "agent_mode",
    "workspace_id",
    "thread",
    "llm_model",
    "delivery",
    "max_failures",
)

# The schedule field each kind reads; an entry shows only its own kind's.
_SCHEDULE_FIELD = {"cron": "cron_expression", "once": "next_run_at", "price": "trigger_config"}

# Model field → the file field a refusal of it should point at.
_FILE_FIELD = {
    "thread_strategy": "thread",
    "conversation_thread_id": "thread",
    "delivery_config": "delivery",
}

_ERROR_EXCERPT = 300
_MAX_PROBLEMS = 10


# =============================================================================
# Errors
# =============================================================================


@dataclass
class AutomationFileError(UserDataValidationError):
    """A refused write. Nothing was saved; ``message`` goes to the agent verbatim."""

    problems: list[tuple[str, str]] = field(default_factory=list)

    def _text(self) -> str:
        if not self.problems:
            return super()._text()
        count = len(self.problems)
        lines = [f"{self.error_type}:{self.file}: {count} problem{'s' if count > 1 else ''}, nothing was saved."]
        lines += [f"- {path}: {msg}" for path, msg in self.problems]
        return "\n".join(lines)


def _refuse(hint: str, error_type: ErrorType = "schema_error") -> AutomationFileError:
    return AutomationFileError(error_type=error_type, file=FILE_NAME, field_path="", hint=hint)


# =============================================================================
# Serialization
# =============================================================================


def _zone(name: str | None):
    return zone_or_none(name or "UTC") or UTC


def _iso(value: datetime | str | None, tz_name: str | None) -> str | None:
    if value is None:
        return None
    if isinstance(value, str):
        value = datetime.fromisoformat(value)
    return on_clock(value, tz_name)


def _thread_value(row: dict[str, Any]) -> str:
    # A persistent automation pins the thread its first run creates, and
    # still reads "persistent" after that; only a thread it was pointed at
    # shows as an id.
    if row.get("thread_strategy") != "continue":
        return "new"
    pinned = row.get("conversation_thread_id")
    return str(pinned) if pinned and not row.get("owns_thread") else "persistent"


def _definition(row: dict[str, Any]) -> dict[str, Any]:
    """The editable part of a row, as the file shows it."""
    kind = row["trigger_type"]
    tz = row.get("timezone") or "UTC"
    entry: dict[str, Any] = {
        "automation_id": str(row["automation_id"]),
        "name": row["name"],
        "description": row.get("description"),
        "status": row["status"],
        "trigger_type": kind,
    }
    if kind == "cron":
        entry["cron_expression"] = row.get("cron_expression")
    elif kind == "once":
        entry["next_run_at"] = _iso(row.get("next_run_at"), tz)
    else:
        entry["trigger_config"] = row.get("trigger_config") or {}
    workspace_id = row.get("workspace_id")
    entry.update(
        timezone=tz,
        instruction=row["instruction"],
        agent_mode=row["agent_mode"],
        workspace_id=str(workspace_id) if workspace_id else None,
        thread=_thread_value(row),
        llm_model=row.get("llm_model"),
        delivery=list((row.get("delivery_config") or {}).get("methods") or []),
        max_failures=row["max_failures"],
    )
    return entry


def _state(row: dict[str, Any]) -> dict[str, Any]:
    """What the server keeps about the automation; read-only in the file."""
    tz = row.get("timezone")
    state: dict[str, Any] = {}
    if row["trigger_type"] == "cron" and row.get("next_run_at"):
        state["next_run_at"] = _iso(row["next_run_at"], tz)
    last = row.get("last_execution")
    if last:
        run: dict[str, Any] = {
            "status": last.get("status"),
            "at": _iso(
                last.get("completed_at") or last.get("started_at") or last.get("scheduled_at"),
                tz,
            ),
        }
        if last.get("conversation_thread_id"):
            run["thread_id"] = str(last["conversation_thread_id"])
        if last.get("excerpt"):
            run["excerpt"] = last["excerpt"]
        for key in ("failure_reason", "skip_reason"):
            if last.get(key):
                run[key] = last[key]
        error = last.get("error_message")
        if error:
            run["error"] = error if len(error) <= _ERROR_EXCERPT else f"{error[:_ERROR_EXCERPT]}…"
        if last.get("dismissed_at"):
            run["dismissed"] = True
        state["last_run"] = run
    if row.get("failure_count"):
        state["failure_count"] = row["failure_count"]
    if row.get("disable_reason"):
        state["disable_reason"] = row["disable_reason"]
    return state


def _version(rows: list[dict[str, Any]]) -> str:
    definitions = sorted((_definition(r) for r in rows), key=lambda d: d["automation_id"])
    blob = json.dumps(definitions, sort_keys=True, ensure_ascii=False, default=str)
    return "sha256:" + hashlib.sha256(blob.encode("utf-8")).hexdigest()


# =============================================================================
# Planning a write
# =============================================================================


def _not_null(value: Any) -> Any:
    if value is None:
        raise ValueError("can't be null")
    return value


class _Entry(BaseModel):
    """One entry as written. Only the document's own shape is checked here:
    the automation's rules run where every surface's do, in ``lifecycle``."""

    model_config = ConfigDict(extra="forbid")

    automation_id: UUID | None = None
    name: str | None = None
    description: str | None = None
    status: str | None = None
    trigger_type: Literal["cron", "once", "price"] | None = None
    cron_expression: str | None = None
    next_run_at: datetime | None = None
    trigger_config: dict[str, Any] | None = None
    timezone: str | None = None
    instruction: str | None = None
    agent_mode: Literal["ptc", "flash"] | None = None
    workspace_id: UUID | None = None
    thread: dict[str, Any] | None = None
    llm_model: str | None = None
    delivery: dict[str, list[str]] | None = None
    max_failures: int | None = None
    state: Any = None

    # Fields every automation has: null is refused rather than read as
    # "clear". A schedule field's null depends on the kind, so the plan reads it.
    @field_validator("name", "status", "trigger_type", "timezone", "instruction", "agent_mode", "max_failures", mode="before")
    @classmethod
    def _required_value(cls, value: Any) -> Any:
        return _not_null(value)

    @field_validator("timezone")
    @classmethod
    def _written_zone(cls, value: str | None, info: ValidationInfo) -> str | None:
        # Only a zone the agent writes is held to region zones; the one a row
        # already has passes, whichever surface stored it.
        stored = (info.context or {}).get("zones", {}).get(str(info.data.get("automation_id")))
        return value if value is None or value == stored else region_zone(value)

    @field_validator("thread", mode="before")
    @classmethod
    def _thread(cls, value: Any, info: ValidationInfo) -> Any:
        return thread_fields(_not_null(value), (info.context or {}).get("thread_id"))

    @field_validator("delivery", mode="before")
    @classmethod
    def _delivery(cls, value: Any) -> Any:
        return parse_delivery(_not_null(value))

    @field_validator("next_run_at", mode="before")
    @classmethod
    def _time(cls, value: Any) -> Any:
        # Naive when it names no offset: the plan reads it on the automation's clock.
        if value is None:
            return None
        if not isinstance(value, str):
            raise ValueError("must be an ISO datetime string, e.g. 2026-10-01T09:00:00")
        return run_time(value)


_ALLOWED = ", ".join(_Entry.model_fields)


@dataclass
class _Create:
    path: str
    data: AutomationCreate
    pause: bool


@dataclass
class _Update:
    path: str
    automation_id: str
    name: str
    fields: dict[str, Any]
    changed: list[str]
    action: str | None  # "pause" | "resume"
    # The row as the save's check read it, which the lifecycle writes over
    # rather than reading it again.
    row: dict[str, Any]


@dataclass
class _Delete:
    automation_id: str
    name: str

    @property
    def label(self) -> str:
        return f'"{self.name}" ({self.automation_id})'


@dataclass
class FilePlan:
    deletes: list[_Delete] = field(default_factory=list)
    updates: list[_Update] = field(default_factory=list)
    creates: list[_Create] = field(default_factory=list)
    notes: list[str] = field(default_factory=list)
    # What the lifecycle checks a named model against; None reads it there.
    model_pref: dict[str, Any] | None = None

    def __bool__(self) -> bool:
        return bool(self.deletes or self.updates or self.creates)


class _Problems:
    """Refusals gathered across the whole document, so one retry can fix all."""

    def __init__(self) -> None:
        self.items: list[tuple[str, str]] = []

    def add(self, path: str, msg: str) -> None:
        if len(self.items) < _MAX_PROBLEMS:
            self.items.append((path, msg))

    def add_validation(self, path: str, exc: ValidationError) -> None:
        for err in exc.errors(include_url=False):
            loc = [str(p) for p in err["loc"]]
            if loc:
                loc[0] = _FILE_FIELD.get(loc[0], loc[0])
            where = ".".join([path, *loc]) if loc else path
            if err["type"] == "extra_forbidden":
                msg = f"unknown field; allowed: {_ALLOWED}"
            else:
                msg = err["msg"].removeprefix("Value error, ")
            self.add(where, msg)

    def raise_if_any(self) -> None:
        if self.items:
            first_path, first_msg = self.items[0]
            raise AutomationFileError(
                error_type="schema_error",
                file=FILE_NAME,
                field_path=first_path,
                hint=first_msg,
                problems=list(self.items),
            )


class _DuplicateKey(ValueError):
    pass


def _unique_keys(pairs: list[tuple[str, Any]]) -> dict[str, Any]:
    # json keeps the last of a repeated key, so an Edit that adds "status"
    # above the one already there would be dropped without a word.
    seen: set[str] = set()
    for key, _ in pairs:
        if key in seen:
            name = dict(pairs).get("name")
            where = f' in "{name}"' if isinstance(name, str) else ""
            raise _DuplicateKey(f'"{key}" appears twice{where}; keep one')
        seen.add(key)
    return dict(pairs)


def _parse_document(content: str) -> list[Any]:
    try:
        doc = json.loads(content, object_pairs_hook=_unique_keys)
    except json.JSONDecodeError as exc:
        raise _refuse(f"invalid JSON at line {exc.lineno}, column {exc.colno}: {exc.msg}", "parse_error")
    except _DuplicateKey as exc:
        raise _refuse(str(exc), "parse_error")
    if not isinstance(doc, dict) or set(doc) != {"automations"} or not isinstance(doc["automations"], list):
        raise _refuse('the file must be one object, {"automations": [...]}, and nothing else')
    return doc["automations"]


def _served(served_content: str) -> dict[str, dict[str, Any]]:
    """Each automation as the agent was shown it, by id."""
    try:
        entries = json.loads(served_content)["automations"]
    except (ValueError, KeyError, TypeError):
        return {}
    return {e["automation_id"]: e for e in entries if isinstance(e, dict)}


def _label(path: str, entry: dict[str, Any]) -> str:
    name = entry.get("name")
    return f'{path} ("{name}")' if isinstance(name, str) and name else path


def _same(written: Any, stored: Any) -> bool:
    """Whether a written value is the stored one in another form ("3" for 3)."""
    if written is None or stored is None:
        return written is None and stored is None
    if isinstance(written, dict) or isinstance(stored, dict):
        return json.dumps(written, sort_keys=True, default=str) == json.dumps(stored, sort_keys=True, default=str)
    return str(written) == str(stored)


_RAN_ONCE = (
    "already ran and is completed; it won't run on its own again. Add a new entry "
    "(without automation_id) for another run."
)


# The status changes a writer may ask for, as the lifecycle call that makes each.
_STATUS_ACTIONS = {
    ("active", "paused"): "pause",
    ("paused", "active"): "resume",
    ("disabled", "active"): "resume",
}


def _status_problem(current: str, value: Any) -> str:
    if current == "completed":
        return f"this automation {_RAN_ONCE}"
    return (
        f'can\'t go from "{current}" to {json.dumps(value)}: set "paused" to pause an active '
        'automation, "active" to resume a paused or disabled one. Other statuses are set by the server.'
    )


@dataclass
class Document:
    """A written document, its entries not yet checked (the plan checks each
    once, against the rows), and what the save read about the user for it."""

    entries: list[Any]
    # Each automation's ``state`` as the writer was shown it, by id; None
    # when it saw the live file, whose rows the plan reads it from.
    states: dict[str, Any] | None
    # A new automation's clock when its entry names none.
    timezone: str
    # What the lifecycle checks a named model against; None when no entry
    # names one it would check.
    model_pref: dict[str, Any] | None


def _names_model(raw: dict[str, Any], shown: dict[str, dict[str, Any]] | None) -> bool:
    model = raw.get("llm_model")
    if not model:
        return False
    automation_id = raw.get("automation_id")
    if automation_id is None or shown is None:
        return True
    # The model the writer was shown is the stored one, or the version check
    # refuses the save; resent as it was, it isn't checked.
    return (shown.get(str(automation_id)) or {}).get("llm_model") != model


def _plan_create(
    entry: _Entry, path: str, call: CallContext, zone: str, problems: _Problems
) -> _Create | None:
    written = entry.model_fields_set
    kind = entry.trigger_type or next(
        (k for k, f in _SCHEDULE_FIELD.items() if getattr(entry, f) is not None), None
    )
    status = entry.status or "active"
    if status not in ("active", "paused"):
        problems.add(f"{path}.status", 'a new automation starts "active" or "paused"')
    if kind is None:
        problems.add(
            f"{path}.trigger_type",
            'required: "cron" (with cron_expression), "once" (with next_run_at) or "price" (with trigger_config)',
        )
        return None

    # The defaults of the old automation tool, so an automation made through
    # the file matches one it made: this workspace, the user's clock, a PTC run.
    tz = entry.timezone or zone
    data: dict[str, Any] = {
        "trigger_type": kind,
        "timezone": tz,
        "agent_mode": entry.agent_mode or "ptc",
        "workspace_id": entry.workspace_id if "workspace_id" in written else call.workspace_id,
        **(entry.thread or {}),
    }
    for key in ("name", "description", "instruction", "llm_model", "max_failures"):
        if key in written:
            data[key] = getattr(entry, key)
    if "delivery" in written:
        data["delivery_config"] = entry.delivery
    for other_kind, kind_field in _SCHEDULE_FIELD.items():
        value = getattr(entry, kind_field)
        # Another kind's field left null reads as absent, as on an update.
        if kind_field in written and (other_kind == kind or value is not None):
            data[kind_field] = value
    when = data.get("next_run_at")
    if when is not None and when.tzinfo is None:
        data["next_run_at"] = when.replace(tzinfo=_zone(tz))

    try:
        create = AutomationCreate.model_validate(data)
        if create.next_run_at is not None:
            future_run(create.next_run_at, tz)
    except ValidationError as exc:
        problems.add_validation(path, exc)
        return None
    except ValueError as exc:
        problems.add(f"{path}.next_run_at", str(exc))
        return None
    return _Create(path=path, data=create, pause=status == "paused")


def _plan_update(
    entry: _Entry, raw: dict[str, Any], row: dict[str, Any], path: str, problems: _Problems
) -> _Update | None:
    """The fields that differ from the row, and the pause or resume a status
    asks for; what the row allows beyond that is the lifecycle's to say."""
    current = _definition(row)
    kind = row["trigger_type"]
    tz = entry.timezone or current["timezone"]
    fields: dict[str, Any] = {}
    changed: list[str] = []
    action: str | None = None

    for key in _EDITABLE:
        if key not in entry.model_fields_set:
            continue  # an omitted field keeps its value
        value = getattr(entry, key)
        where = f"{path}.{key}"
        if value is None and key in _SCHEDULE_FIELD.values():
            if key != _SCHEDULE_FIELD[kind]:
                continue  # another kind's schedule field, left null, is absent
            problems.add(
                where,
                'a one-time automation needs a time; set status "paused" to hold it instead'
                if kind == "once"
                else "can't be null",
            )
            continue
        if key == "status":
            if value == current["status"]:
                continue
            action = _STATUS_ACTIONS.get((current["status"], value))
            if action is None:
                problems.add(where, _status_problem(current["status"], value))
            continue
        if key == "next_run_at":
            wanted = value if value.tzinfo else value.replace(tzinfo=_zone(tz))
            # Compared with the time as the file shows it, to the second.
            if current.get("next_run_at") and wanted == datetime.fromisoformat(current["next_run_at"]):
                continue
            if current["status"] == "completed":
                problems.add(where, f"this automation {_RAN_ONCE}")
                continue
            try:
                fields[key] = future_run(wanted, tz)
            except ValueError as exc:
                problems.add(where, str(exc))
                continue
            changed.append(key)
            continue
        if key == "thread":
            if raw[key] == current["thread"]:
                continue
            pinned = row.get("conversation_thread_id")
            stored = (row.get("thread_strategy"), str(pinned) if pinned else None)
            if (value["thread_strategy"], value["conversation_thread_id"]) != stored:
                fields.update(value)
                changed.append(key)
            continue
        if key == "delivery":
            if value["methods"] != current["delivery"]:
                fields["delivery_config"] = value
                changed.append(key)
            continue
        if _same(value, current.get(key)):
            continue
        if key == "trigger_config" and current["status"] == "completed":
            problems.add(where, f"this automation {_RAN_ONCE}")
            continue
        if key == "workspace_id" and value is None:
            problems.add(where, "can't be cleared; point it at another workspace instead")
            continue
        fields[key] = value
        changed.append(key)

    if fields:
        # Checked again by the lifecycle under the lock; checked here too so
        # one refusal lists every entry's field problems, as a create's are.
        try:
            AutomationUpdate.model_validate(fields, context={"trigger_type": kind})
        except ValidationError as exc:
            problems.add_validation(path, exc)
            return None
    if not fields and action is None:
        return None
    return _Update(
        path=path,
        automation_id=current["automation_id"],
        name=entry.name or current["name"],
        fields=fields,
        changed=changed,
        action=action,
        row=row,
    )


# =============================================================================
# Committing a write
# =============================================================================


def _next_run(row: dict[str, Any] | None) -> str:
    # Only an active automation runs; a paused one keeps its next_run_at.
    if not row or row.get("status") != "active":
        return ""
    if row["trigger_type"] == "price":
        # Named with the feed it is watched on, so a symbol read as a US
        # stock doesn't pass for the crypto or foreign listing meant.
        try:
            config = PriceTriggerConfig(**(row.get("trigger_config") or {}))
        except (ValidationError, TypeError):
            return ""
        feed = "an index" if config.market == MarketType.INDEX else "a US stock"
        return f"; watching {config.symbol} as {feed}"
    when = _iso(row.get("next_run_at"), row.get("timezone"))
    return f"; next run {when}" if when else ""


def _at(path: str, model_field: str | None) -> str:
    return f"{path}.{_FILE_FIELD.get(model_field, model_field)}" if model_field else path


_REFUSED = object()


def _no_changes(notes: list[str]) -> str:
    """The report of a save that changed nothing."""
    return "\n".join(
        [
            f"No changes: {FILE_NAME} already matches the saved automations.",
            *notes,
            f"Read {FILE_NAME} again before your next edit.",
        ]
    )


# =============================================================================
# The file
# =============================================================================


class AutomationsFile(DbJsonFile[list[dict[str, Any]], Document, FilePlan]):
    """Every automation of the user, oldest first, as one document."""

    unchanged = _no_changes([])

    async def fetch(self, user_id: str, conn: Any = None) -> list[dict[str, Any]]:
        return await auto_db.list_all_automations(user_id, conn=conn)

    def render(self, rows: list[dict[str, Any]]) -> tuple[str, str]:
        # An empty state is left out: shown as {}, it was copied into new entries.
        entries = [{**_definition(r), **({"state": state} if (state := _state(r)) else {})} for r in rows]
        content = json.dumps({"automations": entries}, indent=2, ensure_ascii=False) + "\n"
        return content, _version(rows)

    async def lock(self, user_id: str, conn: Any) -> None:
        await auto_db.lock_user_automations(user_id, conn=conn)

    async def parse(self, user_id: str, call: CallContext, content: str, served: str | None) -> Document:
        """The document ``content`` holds. Content that is no document is
        refused here, before the save reads or locks anything; each entry is
        checked by the plan, which lists its problems with the ones the rows
        show, so one retry can fix all. ``served`` is the document the writer
        read, whose ``state`` blocks the write may repeat but not change;
        None compares them with the live rows.

        What the save reads about the user, it reads only when an entry as
        written needs it, which may ask for more than the checked entries
        would: most saves change existing automations and name no model.
        """
        entries = _parse_document(content)
        shown = _served(served) if served is not None else None
        written = [e for e in entries if isinstance(e, dict)]
        # The conversation's clock first, else the user's stored zone, else UTC.
        zone = call.timezone or "UTC"
        if not call.timezone and any(e.get("automation_id") is None and e.get("timezone") is None for e in written):
            zone = await auto_db.get_user_timezone(user_id) or "UTC"
        names_model = any(_names_model(e, shown) for e in written)
        return Document(
            entries=entries,
            states=None if shown is None else {i: e.get("state") for i, e in shown.items()},
            timezone=zone,
            model_pref=await user_models.get_model_preference(user_id) if names_model else None,
        )

    def plan(self, call: CallContext, document: Document, rows: list[dict[str, Any]]) -> Plan[FilePlan]:
        """The changes a save of ``document`` over ``rows`` makes. Raises
        ``AutomationFileError`` with every problem it finds."""
        by_id = {str(r["automation_id"]): r for r in rows}
        states = (
            document.states
            if document.states is not None
            else {automation_id: _state(row) for automation_id, row in by_id.items()}
        )
        zones = {automation_id: row.get("timezone") or "UTC" for automation_id, row in by_id.items()}
        problems = _Problems()
        result = FilePlan(model_pref=document.model_pref)
        seen: set[str] = set()

        for index, raw in enumerate(document.entries):
            label = f"automations[{index}]"
            if not isinstance(raw, dict):
                problems.add(label, "each automation must be a JSON object")
                continue
            label = _label(label, raw)
            try:
                entry = _Entry.model_validate(raw, context={"thread_id": call.thread_id, "zones": zones})
            except ValidationError as exc:
                problems.add_validation(label, exc)
                continue
            automation_id = str(entry.automation_id) if entry.automation_id else None
            if "state" in raw and (raw["state"] or {}) != (states.get(automation_id) or {}):
                # Ignored, not refused: models "fix" state.next_run_at alongside a
                # schedule change, and a refusal cost a whole-document retry.
                result.notes.append(
                    f"- note: {label}: state is kept by the server, so what you wrote in it was ignored"
                )
            if automation_id is None:
                create = _plan_create(entry, label, call, document.timezone, problems)
                if create:
                    result.creates.append(create)
                continue
            if automation_id in seen:
                problems.add(f"{label}.automation_id", "appears twice; each automation is listed once")
                continue
            seen.add(automation_id)
            row = by_id.get(automation_id)
            if row is None:
                problems.add(
                    f"{label}.automation_id",
                    "no automation has this id. Leave automation_id out to create a new one.",
                )
                continue
            update = _plan_update(entry, raw, row, label, problems)
            if update:
                result.updates.append(update)

        problems.raise_if_any()
        for automation_id, row in by_id.items():
            if automation_id not in seen:
                result.deletes.append(_Delete(automation_id=automation_id, name=row["name"]))
        return Plan(result, [d.label for d in result.deletes])

    async def hold(self, user_id: str, plan: FilePlan, rows: list[dict[str, Any]], conn: Any) -> str | None:
        """Lock the rows before a save with changes writes over them: the
        version of the rows it was planned from, as they stand locked. The
        caller holds that to the version it checked, since an edit elsewhere
        may have landed after its read. None for a save with no changes,
        which locks no row.

        A row created since is left out of the version: the plan never touches it.
        """
        if not plan:
            return None
        planned = {str(r["automation_id"]) for r in rows}
        locked = await auto_db.list_all_automations(user_id, conn=conn, lock=True)
        return _version([r for r in locked if str(r["automation_id"]) in planned])

    async def commit(self, user_id: str, plan: FilePlan, conn: Any) -> str:
        """Apply ``plan`` in ``conn``'s transaction and report what changed.

        Each change runs through the lifecycle under its own savepoint, so one
        refused change leaves the rest to be checked and the report names each
        entry's first problem. Any refusal raises ``AutomationFileError`` at the
        end, which rolls the whole save back. The lifecycle writes over the rows
        the plan read, which ``hold`` locked unchanged, rather than reading each
        again.
        """
        if not plan:
            return _no_changes(plan.notes)

        report: list[str] = []
        problems = _Problems()
        warning: str | None = None

        async def step(path: str, change: Callable[..., Awaitable[Any]], *args: Any, **kwargs: Any) -> Any:
            try:
                async with conn.transaction():
                    return await change(*args, conn=conn, **kwargs)
            except ValidationError as exc:
                problems.add_validation(path, exc)
            except lifecycle.AutomationRefusal as exc:
                problems.add(_at(path, exc.field), str(exc))
            except HTTPException as exc:
                detail = {404: "not found", 403: "belongs to another user"}.get(exc.status_code, str(exc.detail))
                problems.add(_at(path, getattr(exc, "field", None)), detail)
            except ValueError as exc:
                problems.add(path, str(exc))
            return _REFUSED

        if plan.deletes:
            await auto_db.delete_automations([d.automation_id for d in plan.deletes], user_id, conn=conn)
        for delete in plan.deletes:
            report.append(f"- deleted {delete.label}, with its run history")

        for update in plan.updates:
            row = update.row
            if update.fields:
                row = await step(
                    update.path,
                    lifecycle.update_automation,
                    update.automation_id,
                    user_id,
                    update.fields,
                    model_pref=plan.model_pref,
                    current=row,
                )
                if row is _REFUSED:
                    continue
            if update.action:
                control = lifecycle.pause_automation if update.action == "pause" else lifecycle.resume_automation
                row = await step(update.path, control, update.automation_id, user_id, current=row)
                if row is _REFUSED:
                    continue
            parts = []
            if update.changed:
                parts.append(", ".join(update.changed))
            if update.action:
                parts.append("paused" if update.action == "pause" else "resumed")
            report.append(f'- updated "{update.name}" ({update.automation_id}): {"; ".join(parts)}{_next_run(row)}')
            warning = warning or lifecycle.delivery_warning(
                (update.fields.get("delivery_config") or {}).get("methods")
            )

        for create in plan.creates:
            row = await step(
                create.path, lifecycle.create_automation, user_id, create.data, model_pref=plan.model_pref
            )
            if row is _REFUSED:
                continue
            automation_id = str(row["automation_id"])
            if create.pause:
                row = await step(create.path, lifecycle.pause_automation, automation_id, user_id, current=row)
                if row is _REFUSED:
                    continue
                report.append(f'- created "{row["name"]}" ({automation_id}), paused')
            else:
                report.append(f'- created "{row["name"]}" ({automation_id}){_next_run(row)}')
            delivery = create.data.delivery_config
            warning = warning or lifecycle.delivery_warning(delivery.methods if delivery else None)

        problems.raise_if_any()

        counts = [
            f"{len(items)} {verb}"
            for items, verb in ((plan.creates, "created"), (plan.updates, "updated"), (plan.deletes, "deleted"))
            if items
        ]
        lines = [f"Saved {FILE_NAME}: {', '.join(counts)}.", *report]
        if warning:
            lines.append(f"Note: {warning}")
        return "\n".join([*lines, *plan.notes, f"Read {FILE_NAME} again before your next edit."])
