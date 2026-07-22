"""GitLab merge request review client: fetch unresolved discussion threads, load creds from hub .env."""

from __future__ import annotations

import json
import os
import re
import urllib.error
import urllib.parse
import urllib.request
from dataclasses import dataclass
from pathlib import Path
from typing import Any, Optional

DEFAULT_BASE_URL = "https://gitlab.sprout.co.id"


class ScmConfigError(RuntimeError):
    pass


class ScmApiError(RuntimeError):
    def __init__(self, status: int, detail: str) -> None:
        super().__init__(f"GitLab API error {status}: {detail}")
        self.status = status
        self.detail = detail


@dataclass(frozen=True)
class ScmCredentials:
    base_url: str
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


def load_gitlab_credentials(hub_root: Path) -> ScmCredentials:
    dotenv = _load_dotenv(hub_root)
    token = os.environ.get("GITLAB_API_TOKEN") or dotenv.get("GITLAB_API_TOKEN")
    base_url = (
        os.environ.get("GITLAB_BASE_URL")
        or dotenv.get("GITLAB_BASE_URL")
        or DEFAULT_BASE_URL
    )
    if not token:
        raise ScmConfigError(
            "GitLab not configured: set GITLAB_API_TOKEN in hub's .env"
        )
    return ScmCredentials(base_url=base_url, token=token)


def _request(creds: ScmCredentials, path: str) -> Any:
    url = f"{creds.base_url.rstrip('/')}/api/v4{path}"
    req = urllib.request.Request(url)
    req.add_header("PRIVATE-TOKEN", creds.token)
    req.add_header("Accept", "application/json")
    try:
        with urllib.request.urlopen(req) as resp:
            return json.loads(resp.read())
    except urllib.error.HTTPError as exc:
        raise ScmApiError(exc.code, exc.read().decode("utf-8", "replace")) from exc
    except urllib.error.URLError as exc:
        raise ScmApiError(0, str(exc.reason)) from exc


_MR_URL_RE = re.compile(
    r"^https?://[^/]+/(?P<project>.+)/-/merge_requests/(?P<iid>\d+)/?"
)


def parse_mr_url(raw: str) -> tuple[str, int]:
    """Accept a full MR URL, return (project_path, mr_iid)."""
    match = _MR_URL_RE.match(raw.strip())
    if not match:
        raise ScmConfigError(f"Not a recognizable merge request URL: {raw}")
    return match.group("project"), int(match.group("iid"))


@dataclass(frozen=True)
class ReviewThread:
    discussion_id: str
    author: str
    body: str
    file_path: Optional[str]
    line: Optional[int]
    url: str


def fetch_unresolved_threads(
    creds: ScmCredentials, project_path: str, mr_iid: int
) -> list[ReviewThread]:
    encoded_project = urllib.parse.quote(project_path, safe="")
    discussions = _request(
        creds, f"/projects/{encoded_project}/merge_requests/{mr_iid}/discussions"
    )
    mr_url = (
        f"{creds.base_url.rstrip('/')}/{project_path}/-/merge_requests/{mr_iid}"
    )
    threads: list[ReviewThread] = []
    for discussion in discussions or []:
        notes = discussion.get("notes") or []
        first = next((n for n in notes if not n.get("system")), None)
        if first is None:
            continue
        if not first.get("resolvable") or first.get("resolved"):
            continue
        position = first.get("position") or {}
        threads.append(
            ReviewThread(
                discussion_id=discussion["id"],
                author=(first.get("author") or {}).get("name", "unknown"),
                body=(first.get("body") or "").strip(),
                file_path=position.get("new_path") or position.get("old_path"),
                line=position.get("new_line") or position.get("old_line"),
                url=f"{mr_url}#note_{first.get('id')}",
            )
        )
    return threads
