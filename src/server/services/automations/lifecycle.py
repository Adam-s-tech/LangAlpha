"""Automation create, update and control: the rules every surface shares.

The REST API, the flash tools and the automations file all write through
here, so a rule lives once. Checks the request models can make alone
(schedule, a known zone) are theirs; the ones that need the stored row, the
user's targets or their models are made here. The guards against an agent's
slips (a past or date-only time, a fixed-offset zone) are the models
module's helpers, which only the agent's surfaces call. A refusal carries the
field it is about, so the automations file can point at the entry's field.
"""

import logging
from collections.abc import Sequence
from datetime import datetime, timezone
from typing import Any, Dict, Optional
from uuid import UUID

from fastapi import HTTPException

from src.config import settings
from src.llms.llm import get_configured_llm_models
from src.llms.preferences import custom_model_names
from src.server.database import automation as auto_db
from src.server.database import automation_executions as exec_db
from src.server.database.workspace import get_workspace
from src.server.models.automation import AutomationCreate, AutomationUpdate, on_clock
from src.server.services.automation_scheduler import AutomationScheduler
from src.server.services.automation_settlement import Outcome, settle
from src.server.services.llm import user_models
from src.server.utils.api import (
    raise_not_found,
    require_thread_owner,
    require_workspace_owner,
)

logger = logging.getLogger(__name__)


class AutomationRefusal(ValueError):
    """A rule refused the write; REST answers it as a conflict."""

    def __init__(self, message: str, *, field: str | None = None) -> None:
        super().__init__(message)
        self.field = field


class TargetRefused(HTTPException):
    """A named workspace or thread the user can't use, answered 404 or 403 as
    REST always has, with the field that named it."""

    def __init__(self, refused: HTTPException, *, field: str) -> None:
        super().__init__(status_code=refused.status_code, detail=refused.detail)
        self.field = field


async def require_target_ownership(
    user_id: str,
    workspace_id: UUID | str | None,
    conversation_thread_id: UUID | str | None,
    *,
    conn=None,
) -> None:
    """Verify caller-supplied automation targets belong to ``user_id``.

    The check mostly holds for the row's life: nothing reassigns
    ``workspaces.user_id`` and nothing moves a thread between workspaces, so a
    workspace owned at write time stays owned. A pinned thread is the gap:
    deleting one hard-deletes the row and frees its id for whoever posts to it
    next, and the column carries no FK to catch that. Raises 404/403, never
    ValueError, which these routes map to 409.
    """
    if workspace_id:
        try:
            require_workspace_owner(
                await get_workspace(str(workspace_id), conn=conn), user_id=user_id
            )
        except HTTPException as exc:
            raise TargetRefused(exc, field="workspace_id") from None
    if conversation_thread_id:
        try:
            await require_thread_owner(str(conversation_thread_id), user_id, conn=conn)
        except HTTPException as exc:
            raise TargetRefused(exc, field="conversation_thread_id") from None


def _require_workspace_for_ptc(mode: str | None, workspace: Any) -> None:
    if mode == "ptc" and not workspace:
        raise AutomationRefusal(
            "workspace_id is required for agent_mode='ptc'", field="workspace_id"
        )


async def _check_model(user_id: str, name: str, pref: dict[str, Any] | None) -> None:
    """Refuse a model the run would reject: an unknown name fails every
    firing until the automation is disabled."""
    if pref is None:
        pref = await user_models.get_model_preference(user_id)
    source, _ = await user_models.classify_model(user_id, name, _pref_cache=pref)
    if source != user_models.ModelSource.UNKNOWN:
        return
    if await user_models.get_custom_provider_config(user_id, name, _pref_cache=pref):
        return
    names = [
        *sorted(custom_model_names(pref)),
        *sorted(n for group in get_configured_llm_models().values() for n in group),
    ]
    raise AutomationRefusal(
        f"unknown model {name!r}; use null for the user's default, or one of: {', '.join(names)}",
        field="llm_model",
    )


def delivery_warning(methods: Sequence[str] | None) -> str | None:
    """What to tell whoever gave an automation a channel the server can't
    post to yet."""
    if not methods or settings.AUTOMATION_WEBHOOK_URL:
        return None
    return (
        "Delivery was saved, but AUTOMATION_WEBHOOK_URL is not configured, so runs "
        "post only in the app until it is set."
    )


async def create_automation(
    user_id: str,
    data: AutomationCreate,
    *,
    conn=None,
    model_pref: dict[str, Any] | None = None,
) -> Dict[str, Any]:
    """Create a new automation. ``conn`` joins the write to a caller's
    transaction, as the automations file applies a whole document in one.
    Such a caller passes the user's ``model_pref``, read before it took its
    locks: read here, it would hold a second pool connection while they wait.

    Raises:
        AutomationRefusal: agent_mode='ptc' without a workspace, or a model
            the user can't run
        TargetRefused: a workspace or thread that isn't the user's
    """
    # A price trigger has no next_run_at: the price monitor fires it.
    next_run_at = (
        AutomationScheduler.calculate_first_run(data.cron_expression, data.timezone)
        if data.trigger_type == "cron"
        else data.next_run_at
    )

    _require_workspace_for_ptc(data.agent_mode, data.workspace_id)

    # Targets are checked whatever the mode: flash ignores workspace_id at run
    # time, but a later PATCH to agent_mode='ptc' activates the stored one.
    await require_target_ownership(
        user_id, data.workspace_id, data.conversation_thread_id, conn=conn
    )
    if data.llm_model:
        await _check_model(user_id, data.llm_model, model_pref)

    automation = await auto_db.create_automation(
        user_id=user_id,
        name=data.name,
        trigger_type=data.trigger_type,
        instruction=data.instruction,
        description=data.description,
        cron_expression=data.cron_expression,
        timezone=data.timezone,
        trigger_config=data.trigger_config,
        next_run_at=next_run_at,
        agent_mode=data.agent_mode,
        workspace_id=str(data.workspace_id) if data.workspace_id else None,
        llm_model=data.llm_model,
        additional_context=data.additional_context,
        thread_strategy=data.thread_strategy,
        conversation_thread_id=(
            str(data.conversation_thread_id) if data.conversation_thread_id else None
        ),
        max_failures=data.max_failures,
        delivery_config=(
            data.delivery_config.model_dump() if data.delivery_config else None
        ),
        metadata=data.metadata,
        conn=conn,
    )

    logger.info(
        f"[AUTOMATION] Created automation {automation['automation_id']} "
        f"for user {user_id} (trigger={data.trigger_type}, next_run={next_run_at})"
    )
    return automation


async def _current(
    automation_id: str, user_id: str, conn, current: Dict[str, Any] | None
) -> Optional[Dict[str, Any]]:
    if current is not None:
        return current
    return await auto_db.get_automation(automation_id, user_id, conn=conn)


async def update_automation(
    automation_id: str,
    user_id: str,
    fields: Dict[str, Any],
    *,
    conn=None,
    model_pref: dict[str, Any] | None = None,
    current: Dict[str, Any] | None = None,
) -> Optional[Dict[str, Any]]:
    """Update an automation with validation and next_run_at recalculation.

    Args:
        automation_id: Automation UUID
        user_id: Owner user ID
        fields: The fields to change, read through AutomationUpdate against
            the stored kind of trigger. REST drops None fields before they
            get here, so a None ``conversation_thread_id`` was named on
            purpose: the agent's thread "new" clearing the pin.
        conn, model_pref: as ``create_automation`` takes them
        current: the row as a caller holding the user's automation locks
            already read it, which spares reading it again.

    Returns:
        Updated automation dict, or None if not found

    Raises:
        ValidationError: A field AutomationUpdate refuses, such as a schedule
            field of another kind than the stored one
        AutomationRefusal: A merged state that leaves agent_mode='ptc' without
            a workspace, or a model the user can't run
        TargetRefused: a workspace or thread that isn't the user's
    """
    current = await _current(automation_id, user_id, conn, current)
    if not current:
        return None

    update = AutomationUpdate.model_validate(
        fields, context={"trigger_type": current["trigger_type"]}
    )
    update_kwargs = update.model_dump(exclude_unset=True, exclude={"trigger_type"})
    for field in ("workspace_id", "conversation_thread_id"):
        if update_kwargs.get(field) is not None:
            update_kwargs[field] = str(update_kwargs[field])

    await require_target_ownership(
        user_id, update.workspace_id, update.conversation_thread_id, conn=conn
    )

    # create enforces this, but a PATCH can flip agent_mode without naming a
    # workspace: check the merged state, or 'ptc' activates on a row that has
    # none and the executor only finds out at run time, burning a failure.
    _require_workspace_for_ptc(
        update.agent_mode or current.get("agent_mode"),
        update.workspace_id or current.get("workspace_id"),
    )
    # Only a new name is checked: a stored one the user can no longer run
    # must not block an edit to anything else.
    if update.llm_model and update.llm_model != current.get("llm_model"):
        await _check_model(user_id, update.llm_model, model_pref)

    # Recalculate next_run_at if cron expression or timezone changed. Only an
    # active row gets one: a paused or disabled cron keeps the none that pause
    # left it, and resume computes it from the edited expression and zone.
    new_cron = update.cron_expression
    new_tz = update.timezone
    cron_expr = new_cron or current["cron_expression"]
    tz_name = new_tz or current["timezone"]

    if (
        (new_cron or new_tz)
        and current["trigger_type"] == "cron"
        and current["status"] == "active"
        and cron_expr
    ):
        update_kwargs["next_run_at"] = AutomationScheduler.calculate_first_run(
            cron_expr, tz_name
        )

    if not update_kwargs:
        return current

    result = await auto_db.update_automation(
        automation_id, user_id, conn=conn, **update_kwargs
    )
    if result:
        logger.info(
            f"[AUTOMATION] Updated automation {automation_id} "
            f"fields={list(update_kwargs.keys())}"
        )
    return result


async def pause_automation(
    automation_id: str,
    user_id: str,
    *,
    conn=None,
    current: Dict[str, Any] | None = None,
) -> Optional[Dict[str, Any]]:
    """Pause an active automation; ``current`` as ``update_automation``
    takes it.

    A one-time automation keeps its next_run_at: that time is its whole
    schedule, and resume has nowhere else to read it from. Keeping it is safe
    because the scheduler claims only 'active' rows. A cron's is recomputed
    on resume, so it is cleared.
    """
    current = await _current(automation_id, user_id, conn, current)
    if not current:
        return None

    if current["status"] != "active":
        raise AutomationRefusal(
            f"Cannot pause automation in '{current['status']}' status "
            f"(must be 'active')",
            field="status",
        )

    update_kwargs: Dict[str, Any] = {"status": "paused"}
    if current["trigger_type"] != "once":
        update_kwargs["next_run_at"] = None
    return await auto_db.update_automation(
        automation_id, user_id, conn=conn, **update_kwargs
    )


async def resume_automation(
    automation_id: str,
    user_id: str,
    *,
    conn=None,
    current: Dict[str, Any] | None = None,
) -> Optional[Dict[str, Any]]:
    """Resume a paused/disabled automation (recalculates next_run_at, resets
    failures); ``current`` as ``update_automation`` takes it."""
    current = await _current(automation_id, user_id, conn, current)
    if not current:
        return None

    if current["status"] not in ("paused", "disabled"):
        raise AutomationRefusal(
            f"Cannot resume automation in '{current['status']}' status "
            f"(must be 'paused' or 'disabled')",
            field="status",
        )

    # The reason goes with the status it explained, in the same write.
    update_kwargs: Dict[str, Any] = {
        "status": "active",
        "failure_count": 0,
        "disable_reason": None,
    }

    if current["trigger_type"] == "cron" and current.get("cron_expression"):
        update_kwargs["next_run_at"] = AutomationScheduler.calculate_first_run(
            current["cron_expression"],
            current.get("timezone", "UTC"),
        )
    elif current["trigger_type"] == "once":
        when = current.get("next_run_at")
        if when and when > datetime.now(timezone.utc):
            update_kwargs["next_run_at"] = when
        elif current["status"] == "disabled":
            # Switched off by the run it already had: back on, it waits for
            # Run now, as a one-time automation whose run failed does.
            update_kwargs["next_run_at"] = None
        else:
            passed = f" ({on_clock(when, current.get('timezone'))})" if when else ""
            raise AutomationRefusal(
                f"Cannot resume a one-time automation whose scheduled time has passed{passed}; "
                "set next_run_at to a future time first",
                field="status",
            )
    # A price trigger has no next_run_at: it is only re-activated.

    return await auto_db.update_automation(
        automation_id, user_id, conn=conn, **update_kwargs
    )


async def trigger_automation(
    automation_id: str,
    user_id: str,
) -> Dict[str, Any]:
    """Manually trigger an automation immediately (doesn't affect next_run_at).

    Returns:
        Dict with execution_id and status.
    """
    current = await auto_db.get_automation(automation_id, user_id)
    if not current:
        raise ValueError("Automation not found")

    if current["status"] not in ("active", "paused", "completed"):
        raise ValueError(
            f"Cannot trigger automation in '{current['status']}' status"
        )

    scheduler = AutomationScheduler.get_instance()
    execution_id = await exec_db.create_execution(
        automation_id=automation_id,
        scheduled_at=datetime.now(timezone.utc),
        server_id=scheduler.server_id,
    )

    scheduler.dispatch(current, execution_id, name=f"manual_exec_{automation_id[:8]}")

    logger.info(
        f"[AUTOMATION] Manual trigger: automation_id={automation_id} "
        f"execution_id={execution_id}"
    )

    return {
        "execution_id": execution_id,
        "automation_id": automation_id,
        "status": "triggered",
    }


async def skip_execution(
    automation_id: str,
    execution_id: str,
    user_id: str,
) -> Optional[Dict[str, Any]]:
    """Skip a firing that is waiting for its thread's turn to end.

    Returns None when the automation is not the user's and raises 404 when
    the firing does not exist; raises ValueError when it already started or
    settled.
    """
    current = await auto_db.get_automation(automation_id, user_id)
    if not current:
        return None
    # settle raises nothing once the row is written, so True is the skip
    # having happened; False needs a read to tell missing from moved on.
    if not await settle(current, execution_id, Outcome.SKIPPED, skip_reason="user"):
        if await exec_db.get_execution_status(
            execution_id, automation_id=automation_id
        ) is None:
            raise_not_found("Execution")
        raise ValueError("This run is no longer waiting")
    return {
        "execution_id": execution_id,
        "automation_id": automation_id,
        "status": "skipped",
    }


async def dismiss_execution(
    automation_id: str,
    execution_id: str,
    user_id: str,
) -> Optional[Dict[str, Any]]:
    """Take a failed run out of Needs attention, answering with the automation.

    Returns None when the automation is not the user's and raises 404 when
    the run does not exist; raises ValueError when the run did not fail.
    """
    if not await auto_db.get_automation(automation_id, user_id):
        return None
    if not await exec_db.dismiss_execution(execution_id, automation_id=automation_id):
        if await exec_db.get_execution_status(
            execution_id, automation_id=automation_id
        ) is None:
            raise_not_found("Execution")
        raise ValueError("Only a failed run can be dismissed")
    return await auto_db.get_automation(automation_id, user_id)
