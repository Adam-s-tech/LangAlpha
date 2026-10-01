"""Post-stream safety net: capture sandbox images referenced in SSE events.

``ImageCaptureMiddleware`` rewrites checkpointed messages at model time; the
streamed sse_events still carry sandbox paths, so this hook rewrites them at
persistence time. Keys are content-addressed (shared helpers), so both passes
converge on identical URLs — re-uploads are idempotent PUTs of the same bytes.

No-op when storage is disabled (storage.provider = "none").
"""

import logging

from ptc_agent.core.project_context import ProjectContext

from ptc_agent.agent.middleware.image_capture import (
    IMAGE_MD_RE,
    capture_sandbox_images,
    is_sandbox_image_path,
    rewrite_image_paths,
)
from src.utils.storage import is_storage_enabled

logger = logging.getLogger(__name__)


async def capture_and_rewrite_images(
    sse_events: list[dict],
    sandbox,
    thread_id: str = "",
    *,
    project: ProjectContext | None = None,
    workspace_id: str | None = None,
) -> int:
    """Scan SSE events for sandbox image paths, upload to storage, rewrite in-place.

    Returns number of images captured. No-op if storage is disabled.
    Non-fatal: logs warnings on failure, never raises.
    """
    if not is_storage_enabled() or not sse_events:
        return 0

    # Collect all unique sandbox image paths from text message_chunks
    image_paths: set[str] = set()
    for evt in sse_events:
        if evt.get("event") != "message_chunk":
            continue
        data = evt.get("data", {})
        if data.get("content_type") != "text":
            continue
        content = data.get("content", "")
        for match in IMAGE_MD_RE.finditer(content):
            if is_sandbox_image_path(match.group(2)):
                image_paths.add(match.group(2))

    if not image_paths:
        return 0

    if project is None and workspace_id:
        path_to_url = await _capture_from_held_folder(
            sandbox, image_paths, thread_id, workspace_id
        )
    else:
        path_to_url = await capture_sandbox_images(
            sandbox, image_paths, thread_id, project=project
        )
    if not path_to_url:
        return 0

    # Persist the path→URL map as a ui record so checkpoint-sourced replay of
    # pre-middleware turns can resolve sandbox image paths without the
    # (long-gone) sandbox. New turns don't need it — the middleware rewrites
    # the checkpointed message itself — but the record is harmless and keeps
    # this hook a complete fallback while it remains in place.
    if thread_id:
        try:
            from src.server.services.history.reader import CheckpointHistoryReader

            await CheckpointHistoryReader.get_instance().append_ui_record(
                thread_id, "image_capture", {"path_to_url": path_to_url}
            )
        except Exception as e:
            logger.warning(f"[IMAGE_CAPTURE] Failed to persist ui record: {e}")

    # Rewrite image paths in SSE events in-place
    for evt in sse_events:
        if evt.get("event") != "message_chunk":
            continue
        data = evt.get("data", {})
        if data.get("content_type") != "text":
            continue
        content = data.get("content", "")
        if content:
            data["content"] = rewrite_image_paths(content, path_to_url)

    return len(path_to_url)


async def _capture_from_held_folder(
    sandbox, image_paths: set[str], thread_id: str, workspace_id: str
) -> dict[str, str]:
    """Late, the run is terminal and a settle may move the folder: the images
    are read from the one the row names under the folder hold, and a folder
    mid-move gives none rather than whatever holds its old name."""
    from src.server.database.workspace_folders import (
        WorkspaceFolderMoving,
        is_top_level,
        workspace_folder_in_use,
    )
    from src.server.services.workspace_layout import resolve_project_placement

    try:
        async with workspace_folder_in_use(workspace_id):
            try:
                placement = await resolve_project_placement(
                    workspace_id, root=sandbox.working_dir
                )
            except Exception:
                logger.warning("[IMAGE_CAPTURE] Workspace placement unavailable", exc_info=True)
                return {}
            if placement.dir_name and not is_top_level(placement.dir_name):
                return {}
            project = ProjectContext(
                workspace_id, placement.dir_name,
                placement.sibling_dir_names, placement.layout_origin,
                placement.previous_dir_names,
            )
            return await capture_sandbox_images(
                sandbox, image_paths, thread_id, project=project
            )
    except WorkspaceFolderMoving:
        logger.info(f"[IMAGE_CAPTURE] Folder of {workspace_id} is moving; images left as paths")
        return {}
