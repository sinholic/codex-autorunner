from __future__ import annotations

import uuid
from pathlib import Path

from fastapi import APIRouter, HTTPException, Request

from ....agents.registry import validate_agent_id
from ....core.config import ConfigError, load_hub_config
from ....integrations.scm_review import (
    ReviewThread,
    ScmApiError,
    ScmConfigError,
    fetch_unresolved_threads,
    load_gitlab_credentials,
    parse_mr_url,
)
from ....tickets.files import safe_relpath
from ..schemas import (
    ScmReviewApplyRequest,
    ScmReviewApplyResponse,
    ScmReviewApplyResult,
    ScmReviewPreviewRequest,
    ScmReviewPreviewResponse,
    ScmReviewThreadSummary,
)
from ..services.responses import error_detail
from .templates import (
    _collect_ticket_indices,
    _next_available_ticket_index,
    _normalize_ticket_suffix,
    _ticket_filename,
)


def _error_detail(code: str, message: str) -> dict[str, object]:
    return error_detail(code, message)


def _resolve_hub_root(repo_root: Path) -> Path:
    try:
        hub_config = load_hub_config(repo_root)
    except ConfigError as exc:
        raise HTTPException(
            status_code=500,
            detail=_error_detail("hub_config_error", str(exc)),
        ) from exc
    return hub_config.root


def _thread_to_schema(thread: ReviewThread) -> ScmReviewThreadSummary:
    return ScmReviewThreadSummary(
        discussion_id=thread.discussion_id,
        author=thread.author,
        body=thread.body,
        file_path=thread.file_path,
        line=thread.line,
        url=thread.url,
    )


def _yaml_scalar(value: str) -> str:
    escaped = value.replace("\\", "\\\\").replace('"', '\\"').replace("\n", "\\n")
    return f'"{escaped}"'


def _ticket_content(thread: ReviewThread, mr_url: str, agent: str) -> str:
    ticket_id = f"tkt_{uuid.uuid4().hex}"
    location = f"{thread.file_path}:{thread.line}" if thread.file_path else "general"
    title = f"Review comment ({location})"
    return (
        "---\n"
        f"title: {_yaml_scalar(title)}\n"
        f"agent: {_yaml_scalar(agent)}\n"
        "done: false\n"
        f"ticket_id: {_yaml_scalar(ticket_id)}\n"
        f"scm_discussion_id: {_yaml_scalar(thread.discussion_id)}\n"
        f"scm_mr_url: {_yaml_scalar(mr_url)}\n"
        "---\n\n"
        f"## Reviewer comment from {thread.author}\n\n"
        f"{thread.body or '_(no comment body)_'}\n\n"
        f"Location: {location}\n\n"
        f"[Thread on GitLab]({thread.url})\n"
    )


def build_scm_review_routes() -> APIRouter:
    router = APIRouter(prefix="/api/scm-review", tags=["scm-review"])

    @router.post("/preview", response_model=ScmReviewPreviewResponse)
    def preview(request: Request, payload: ScmReviewPreviewRequest):
        hub_root = _resolve_hub_root(request.app.state.engine.repo_root)
        try:
            creds = load_gitlab_credentials(hub_root)
        except ScmConfigError as exc:
            raise HTTPException(
                status_code=503, detail=_error_detail("scm_not_configured", str(exc))
            ) from exc

        try:
            project_path, mr_iid = parse_mr_url(payload.mr_url)
        except ScmConfigError as exc:
            raise HTTPException(
                status_code=400, detail=_error_detail("validation_error", str(exc))
            ) from exc

        try:
            threads = fetch_unresolved_threads(creds, project_path, mr_iid)
        except ScmApiError as exc:
            status = 404 if exc.status == 404 else 502
            raise HTTPException(
                status_code=status, detail=_error_detail("scm_api_error", str(exc))
            ) from exc

        return ScmReviewPreviewResponse(
            threads=[_thread_to_schema(t) for t in threads]
        )

    @router.post("/apply", response_model=ScmReviewApplyResponse)
    def apply(request: Request, payload: ScmReviewApplyRequest):
        hub_root = _resolve_hub_root(request.app.state.engine.repo_root)
        try:
            creds = load_gitlab_credentials(hub_root)
        except ScmConfigError as exc:
            raise HTTPException(
                status_code=503, detail=_error_detail("scm_not_configured", str(exc))
            ) from exc

        try:
            project_path, mr_iid = parse_mr_url(payload.mr_url)
        except ScmConfigError as exc:
            raise HTTPException(
                status_code=400, detail=_error_detail("validation_error", str(exc))
            ) from exc

        discussion_ids = {d.strip() for d in payload.discussion_ids if d.strip()}
        if not discussion_ids:
            raise HTTPException(
                status_code=400,
                detail=_error_detail("validation_error", "discussion_ids is required"),
            )

        agent = payload.agent or "claude"
        if agent != "user":
            try:
                validate_agent_id(agent)
            except ValueError as exc:
                raise HTTPException(
                    status_code=400, detail=_error_detail("agent_invalid", str(exc))
                ) from exc

        try:
            threads = fetch_unresolved_threads(creds, project_path, mr_iid)
        except ScmApiError as exc:
            status = 404 if exc.status == 404 else 502
            raise HTTPException(
                status_code=status, detail=_error_detail("scm_api_error", str(exc))
            ) from exc

        selected = [t for t in threads if t.discussion_id in discussion_ids]
        missing = discussion_ids - {t.discussion_id for t in selected}
        if missing:
            raise HTTPException(
                status_code=404,
                detail=_error_detail(
                    "discussion_not_found",
                    f"Unresolved discussion(s) not found: {', '.join(sorted(missing))}",
                ),
            )

        ticket_dir = request.app.state.engine.repo_root / ".codex-autorunner" / "tickets"
        try:
            ticket_dir.mkdir(parents=True, exist_ok=True)
        except OSError as exc:
            raise HTTPException(
                status_code=500, detail=_error_detail("ticket_dir_error", str(exc))
            ) from exc

        results: list[ScmReviewApplyResult] = []
        for thread in selected:
            existing_indices = _collect_ticket_indices(ticket_dir)
            index = _next_available_ticket_index(existing_indices)
            suffix = _normalize_ticket_suffix(
                f"mr{mr_iid}-{thread.discussion_id[:8]}"
            )
            width = max(3, max([len(str(i)) for i in existing_indices + [index]]))
            filename = _ticket_filename(index, suffix=suffix, width=width)
            path = ticket_dir / filename
            if path.exists():
                raise HTTPException(
                    status_code=409,
                    detail=_error_detail(
                        "ticket_exists", f"Ticket already exists: {path}"
                    ),
                )

            try:
                path.write_text(
                    _ticket_content(thread, payload.mr_url, agent), encoding="utf-8"
                )
            except OSError as exc:
                raise HTTPException(
                    status_code=500,
                    detail=_error_detail("ticket_write_failed", str(exc)),
                ) from exc

            results.append(
                ScmReviewApplyResult(
                    discussion_id=thread.discussion_id,
                    created_path=safe_relpath(path, request.app.state.engine.repo_root),
                    filename=filename,
                    index=index,
                )
            )

        return ScmReviewApplyResponse(results=results)

    return router
