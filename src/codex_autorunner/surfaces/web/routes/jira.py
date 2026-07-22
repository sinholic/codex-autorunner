from __future__ import annotations

import uuid
from pathlib import Path
from typing import Optional

from fastapi import APIRouter, HTTPException, Request

from ....agents.registry import validate_agent_id
from ....core.config import ConfigError, load_hub_config
from ....integrations.jira import (
    JiraApiError,
    JiraConfigError,
    JiraIssue,
    fetch_epic_children,
    fetch_issue,
    load_jira_credentials,
)
from ....tickets.files import safe_relpath
from ..schemas import (
    JiraApplyRequest,
    JiraApplyResponse,
    JiraApplyResult,
    JiraIssueSummary,
    JiraPreviewRequest,
    JiraPreviewResponse,
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


def _issue_to_schema(issue: JiraIssue) -> JiraIssueSummary:
    return JiraIssueSummary(
        key=issue.key,
        summary=issue.summary,
        is_epic=issue.is_epic,
        description_text=issue.description_text,
        url=issue.url,
    )


def _yaml_scalar(value: str) -> str:
    escaped = value.replace("\\", "\\\\").replace('"', '\\"').replace("\n", "\\n")
    return f'"{escaped}"'


def _ticket_content(issue: JiraIssue, agent: str) -> str:
    ticket_id = f"tkt_{uuid.uuid4().hex}"
    title = f"{issue.key}: {issue.summary}"
    description = issue.description_text or "_(no description)_"
    return (
        "---\n"
        f"title: {_yaml_scalar(title)}\n"
        f"agent: {_yaml_scalar(agent)}\n"
        "done: false\n"
        f"ticket_id: {_yaml_scalar(ticket_id)}\n"
        f"jira_key: {_yaml_scalar(issue.key)}\n"
        f"jira_url: {_yaml_scalar(issue.url)}\n"
        "---\n\n"
        f"## From Jira ({issue.key})\n\n"
        f"{description}\n\n"
        f"[{issue.key} on Jira]({issue.url})\n"
    )


def build_jira_routes() -> APIRouter:
    router = APIRouter(prefix="/api/jira", tags=["jira"])

    @router.post("/preview", response_model=JiraPreviewResponse)
    def preview(request: Request, payload: JiraPreviewRequest):
        hub_root = _resolve_hub_root(request.app.state.engine.repo_root)
        try:
            creds = load_jira_credentials(hub_root)
        except JiraConfigError as exc:
            raise HTTPException(
                status_code=503, detail=_error_detail("jira_not_configured", str(exc))
            ) from exc

        key = payload.key.strip().upper()
        if not key:
            raise HTTPException(
                status_code=400,
                detail=_error_detail("validation_error", "key is required"),
            )
        try:
            issue = fetch_issue(creds, key)
            children = fetch_epic_children(creds, key) if issue.is_epic else []
        except JiraApiError as exc:
            status = 404 if exc.status == 404 else 502
            raise HTTPException(
                status_code=status, detail=_error_detail("jira_api_error", str(exc))
            ) from exc

        return JiraPreviewResponse(
            issue=_issue_to_schema(issue),
            children=[_issue_to_schema(child) for child in children],
        )

    @router.post("/apply", response_model=JiraApplyResponse)
    def apply(request: Request, payload: JiraApplyRequest):
        hub_root = _resolve_hub_root(request.app.state.engine.repo_root)
        try:
            creds = load_jira_credentials(hub_root)
        except JiraConfigError as exc:
            raise HTTPException(
                status_code=503, detail=_error_detail("jira_not_configured", str(exc))
            ) from exc

        keys = [k.strip().upper() for k in payload.keys if k.strip()]
        if not keys:
            raise HTTPException(
                status_code=400,
                detail=_error_detail("validation_error", "keys is required"),
            )

        agent = payload.agent or "claude"
        if agent != "user":
            try:
                validate_agent_id(agent)
            except ValueError as exc:
                raise HTTPException(
                    status_code=400, detail=_error_detail("agent_invalid", str(exc))
                ) from exc

        ticket_dir = request.app.state.engine.repo_root / ".codex-autorunner" / "tickets"
        try:
            ticket_dir.mkdir(parents=True, exist_ok=True)
        except OSError as exc:
            raise HTTPException(
                status_code=500, detail=_error_detail("ticket_dir_error", str(exc))
            ) from exc

        results: list[JiraApplyResult] = []
        for key in keys:
            try:
                issue = fetch_issue(creds, key)
            except JiraApiError as exc:
                status = 404 if exc.status == 404 else 502
                raise HTTPException(
                    status_code=status,
                    detail=_error_detail("jira_api_error", str(exc)),
                ) from exc

            existing_indices = _collect_ticket_indices(ticket_dir)
            index = _next_available_ticket_index(existing_indices)
            suffix = _normalize_ticket_suffix(key.lower())
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
                path.write_text(_ticket_content(issue, agent), encoding="utf-8")
            except OSError as exc:
                raise HTTPException(
                    status_code=500,
                    detail=_error_detail("ticket_write_failed", str(exc)),
                ) from exc

            results.append(
                JiraApplyResult(
                    key=key,
                    created_path=safe_relpath(path, request.app.state.engine.repo_root),
                    filename=filename,
                    index=index,
                )
            )

        return JiraApplyResponse(results=results)

    return router
