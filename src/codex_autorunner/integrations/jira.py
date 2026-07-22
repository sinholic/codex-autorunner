"""Jira Cloud REST v3 client: fetch issues/epic children, load creds from hub .env."""

from __future__ import annotations

import base64
import json
import os
import urllib.error
import urllib.request
from dataclasses import dataclass
from pathlib import Path
from typing import Any, Optional

DEFAULT_BASE_URL = "https://sprout-id.atlassian.net"


class JiraConfigError(RuntimeError):
    pass


class JiraApiError(RuntimeError):
    def __init__(self, status: int, detail: str) -> None:
        super().__init__(f"Jira API error {status}: {detail}")
        self.status = status
        self.detail = detail


@dataclass(frozen=True)
class JiraCredentials:
    base_url: str
    email: str
    token: str


def _load_dotenv(hub_root: Path) -> dict[str, str]:
    env_path = hub_root / ".env"
    values: dict[str, str] = {}
    if not env_path.exists():
        return values
    for line in env_path.read_text(encoding="utf-8").splitlines():
        line = line.strip()
        if not line or line.startswith("#") or "=" not in line:
            continue
        key, _, value = line.partition("=")
        key = key.strip()
        if key:
            values[key] = value.strip()
    return values


def load_jira_credentials(hub_root: Path) -> JiraCredentials:
    dotenv = _load_dotenv(hub_root)
    email = os.environ.get("JIRA_EMAIL") or dotenv.get("JIRA_EMAIL")
    token = os.environ.get("JIRA_API_TOKEN") or dotenv.get("JIRA_API_TOKEN")
    base_url = (
        os.environ.get("JIRA_BASE_URL") or dotenv.get("JIRA_BASE_URL") or DEFAULT_BASE_URL
    )
    if not email or not token:
        raise JiraConfigError(
            "Jira is not configured: set JIRA_EMAIL and JIRA_API_TOKEN in the hub's .env"
        )
    return JiraCredentials(base_url=base_url, email=email, token=token)


def _auth_header(creds: JiraCredentials) -> str:
    raw = f"{creds.email}:{creds.token}".encode("utf-8")
    return "Basic " + base64.b64encode(raw).decode("ascii")


def _request(
    creds: JiraCredentials, method: str, path: str, body: Optional[dict] = None
) -> Any:
    url = f"{creds.base_url.rstrip('/')}{path}"
    data = json.dumps(body).encode("utf-8") if body is not None else None
    req = urllib.request.Request(url, data=data, method=method)
    req.add_header("Authorization", _auth_header(creds))
    req.add_header("Accept", "application/json")
    if data is not None:
        req.add_header("Content-Type", "application/json")
    try:
        with urllib.request.urlopen(req) as resp:
            raw = resp.read()
            return json.loads(raw) if raw else None
    except urllib.error.HTTPError as exc:
        detail = exc.read().decode("utf-8", errors="replace")
        raise JiraApiError(exc.code, detail) from exc
    except urllib.error.URLError as exc:
        raise JiraApiError(0, str(exc.reason)) from exc


def adf_to_text(node: Any) -> str:
    if node is None:
        return ""
    if isinstance(node, str):
        return node
    if not isinstance(node, dict):
        return ""

    node_type = node.get("type")
    content = node.get("content") or []
    pieces = [adf_to_text(child) for child in content]

    if node_type == "text":
        return node.get("text", "")
    if node_type == "paragraph":
        return "".join(pieces) + "\n\n"
    if node_type == "heading":
        level = node.get("attrs", {}).get("level", 1)
        return ("#" * level) + " " + "".join(pieces) + "\n\n"
    if node_type == "listItem":
        return "- " + "".join(pieces).strip() + "\n"
    if node_type in ("bulletList", "orderedList"):
        return "".join(pieces) + "\n"
    if node_type == "tableRow":
        cells = [" ".join(adf_to_text(c).split()) for c in content]
        return "| " + " | ".join(cells) + " |\n"
    if node_type == "table":
        rows = "".join(pieces)
        row_lines = [line for line in rows.splitlines() if line.strip()]
        if row_lines:
            width = row_lines[0].count("|") - 1
            row_lines.insert(1, "|" + " --- |" * width)
        return "\n".join(row_lines) + "\n\n"
    if node_type == "codeBlock":
        return "```\n" + "".join(pieces) + "\n```\n\n"
    if node_type == "hardBreak":
        return "\n"
    return "".join(pieces)


@dataclass(frozen=True)
class JiraIssue:
    key: str
    summary: str
    description_text: str
    is_epic: bool
    url: str


def _issue_from_payload(creds: JiraCredentials, data: dict) -> JiraIssue:
    fields = data.get("fields", {}) or {}
    issuetype = (fields.get("issuetype") or {}).get("name", "")
    return JiraIssue(
        key=data["key"],
        summary=fields.get("summary", data["key"]),
        description_text=adf_to_text(fields.get("description")).strip(),
        is_epic=issuetype.strip().lower() == "epic",
        url=f"{creds.base_url.rstrip('/')}/browse/{data['key']}",
    )


def normalize_issue_key(raw: str) -> str:
    """Accept a bare key ("ZEL-968") or a full/partial issue URL and return the key."""
    value = raw.strip()
    if "/" in value:
        value = value.rstrip("/").rsplit("/", 1)[-1]
    return value.strip().upper()


def fetch_issue(creds: JiraCredentials, key: str) -> JiraIssue:
    key = normalize_issue_key(key)
    fields = "summary,description,status,issuetype"
    try:
        data = _request(creds, "GET", f"/rest/api/3/issue/{key}?fields={fields}")
    except JiraApiError as exc:
        if exc.status == 404:
            raise JiraApiError(404, f"Issue not found: {key}") from exc
        raise
    return _issue_from_payload(creds, data)


def fetch_epic_children(creds: JiraCredentials, epic_key: str) -> list[JiraIssue]:
    fields = "summary,description,status,issuetype"
    for jql in (
        f'parent = "{epic_key}" ORDER BY created ASC',
        f'"Epic Link" = "{epic_key}" ORDER BY created ASC',
    ):
        data = _request(
            creds,
            "POST",
            "/rest/api/3/search/jql",
            {"jql": jql, "fields": fields.split(","), "maxResults": 100},
        )
        issues = data.get("issues", []) if data else []
        if issues:
            return [_issue_from_payload(creds, issue) for issue in issues]
    return []
