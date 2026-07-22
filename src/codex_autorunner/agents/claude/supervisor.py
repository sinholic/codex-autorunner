from __future__ import annotations

import asyncio
import json
import logging
import re
import shutil
import uuid
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any, AsyncIterator, Optional

from ...core.config import HubConfig, RepoConfig
from ..types import TerminalTurnResult

_NON_ALNUM_RE = re.compile(r"[^A-Za-z0-9]")


class ClaudeSupervisorError(RuntimeError):
    """Raised when the claude CLI supervisor cannot satisfy a request."""


def _encode_project_dir(workspace_root: Path) -> str:
    # ponytail: matches the `claude` CLI's own ~/.claude/projects/<encoded-cwd>
    # encoding (every non-alnum char -> "-"), reverse-engineered by inspecting
    # real project dirs on disk. If Anthropic changes the scheme, list/history
    # reads just come back empty (best-effort, not load-bearing for turns).
    return _NON_ALNUM_RE.sub("-", str(workspace_root.resolve()))


def _claude_projects_root() -> Path:
    return Path.home() / ".claude" / "projects"


@dataclass
class _TurnState:
    process: "asyncio.subprocess.Process"
    events: list[dict[str, Any]] = field(default_factory=list)
    subscribers: list["asyncio.Queue[Optional[dict[str, Any]]]"] = field(
        default_factory=list
    )
    stderr_text: str = ""
    task: Optional[asyncio.Task[None]] = None


def _publish(state: _TurnState, event: dict[str, Any]) -> None:
    state.events.append(event)
    for queue in state.subscribers:
        queue.put_nowait(event)


def _close_subscribers(state: _TurnState) -> None:
    for queue in state.subscribers:
        queue.put_nowait(None)


class ClaudeSupervisor:
    """Spawns the real `claude` CLI per turn (stateless subprocess, durable

    session state lives on disk owned entirely by the CLI itself, unlike the
    zeroclaw session-state-file contract this replaces).
    """

    def __init__(self, binary: str, *, logger: Optional[logging.Logger] = None) -> None:
        self._binary = binary
        self._logger = logger or logging.getLogger(__name__)
        # ponytail: process-local memory of which session ids we've already
        # launched once (decides --session-id vs --resume). Lost on restart;
        # resume_conversation() re-marks known ids explicitly so restarts
        # recover as long as the caller still has the id. Upgrade to a disk
        # check (session jsonl exists) if cross-restart accuracy matters.
        self._known_sessions: set[str] = set()
        self._turns: dict[str, _TurnState] = {}

    def mark_known(self, conversation_id: str) -> None:
        self._known_sessions.add(conversation_id)

    async def start_turn(
        self,
        workspace_root: Path,
        conversation_id: str,
        prompt: str,
        *,
        model: Optional[str],
        permission_mode: Optional[str],
    ) -> str:
        args = [
            self._binary,
            "-p",
            "--verbose",
            "--output-format",
            "stream-json",
            "--include-partial-messages",
        ]
        if conversation_id in self._known_sessions:
            args += ["--resume", conversation_id]
        else:
            args += ["--session-id", conversation_id]
            self._known_sessions.add(conversation_id)
        if model:
            args += ["--model", model]
        if permission_mode:
            args += ["--permission-mode", permission_mode]
        args.append(prompt)

        try:
            process = await asyncio.create_subprocess_exec(
                *args,
                cwd=str(workspace_root),
                stdin=asyncio.subprocess.DEVNULL,
                # `claude -p` treats a non-tty stdin as extra prompt context
                # and appends whatever it reads to the positional prompt; a
                # server process must never let it inherit our fd 0.
                stdout=asyncio.subprocess.PIPE,
                stderr=asyncio.subprocess.PIPE,
            )
        except FileNotFoundError as exc:
            raise ClaudeSupervisorError(
                f"claude binary not found: {self._binary}"
            ) from exc

        turn_id = f"claude-turn-{uuid.uuid4()}"
        state = _TurnState(process=process)
        self._turns[turn_id] = state
        state.task = asyncio.create_task(self._pump(state))
        return turn_id

    async def _pump(self, state: _TurnState) -> None:
        assert state.process.stdout is not None
        try:
            async for raw_line in state.process.stdout:
                line = raw_line.decode("utf-8", "replace").strip()
                if not line:
                    continue
                try:
                    event = json.loads(line)
                except json.JSONDecodeError:
                    continue
                if isinstance(event, dict):
                    _publish(state, event)
        finally:
            if state.process.stderr is not None:
                stderr_bytes = await state.process.stderr.read()
                state.stderr_text = stderr_bytes.decode("utf-8", "replace").strip()
            await state.process.wait()
            _close_subscribers(state)

    async def wait_for_turn(
        self, turn_id: str, *, timeout: Optional[float] = None
    ) -> TerminalTurnResult:
        state = self._turns.get(turn_id)
        if state is None:
            raise ClaudeSupervisorError(f"Unknown claude turn '{turn_id}'")
        assert state.task is not None
        if timeout is None:
            await state.task
        else:
            await asyncio.wait_for(state.task, timeout=timeout)
        return self._terminal_result(state)

    def _terminal_result(self, state: _TurnState) -> TerminalTurnResult:
        wrapped_events = [_wrap_event(event) for event in state.events]
        result_event = next(
            (e for e in reversed(state.events) if e.get("type") == "result"), None
        )
        if result_event is not None:
            is_error = bool(result_event.get("is_error"))
            errors = [str(e) for e in result_event.get("errors") or []]
            text = str(result_event.get("result") or "")
            if is_error and not errors:
                errors = [text or "claude turn failed"]
            return TerminalTurnResult(
                status="error" if is_error else "ok",
                assistant_text="" if is_error else text,
                errors=errors,
                raw_events=wrapped_events,
            )
        returncode = state.process.returncode
        error_detail = state.stderr_text or f"claude exited with code {returncode}"
        return TerminalTurnResult(
            status="error",
            assistant_text="",
            errors=[error_detail],
            raw_events=wrapped_events,
        )

    async def interrupt(self, turn_id: str) -> None:
        state = self._turns.get(turn_id)
        if state is None:
            return
        if state.process.returncode is not None:
            return
        state.process.terminate()
        try:
            await asyncio.wait_for(state.process.wait(), timeout=5)
        except asyncio.TimeoutError:
            state.process.kill()

    async def stream_events(self, turn_id: str) -> AsyncIterator[dict[str, Any]]:
        state = self._turns.get(turn_id)
        if state is None:
            return
        queue: "asyncio.Queue[Optional[dict[str, Any]]]" = asyncio.Queue()
        for event in state.events:
            queue.put_nowait(event)
        if state.task is not None and state.task.done():
            queue.put_nowait(None)
        else:
            state.subscribers.append(queue)
        try:
            while True:
                item = await queue.get()
                if item is None:
                    break
                yield item
        finally:
            try:
                state.subscribers.remove(queue)
            except ValueError:
                pass

    def list_sessions_on_disk(self, workspace_root: Path) -> list[tuple[str, float]]:
        project_dir = _claude_projects_root() / _encode_project_dir(workspace_root)
        if not project_dir.is_dir():
            return []
        sessions: list[tuple[str, float]] = []
        for path in project_dir.glob("*.jsonl"):
            try:
                mtime = path.stat().st_mtime
            except OSError:
                continue
            sessions.append((path.stem, mtime))
        sessions.sort(key=lambda item: item[1], reverse=True)
        return sessions

    def read_transcript(
        self, workspace_root: Path, conversation_id: str, *, limit: Optional[int] = None
    ) -> list[dict[str, Any]]:
        project_dir = _claude_projects_root() / _encode_project_dir(workspace_root)
        session_file = project_dir / f"{conversation_id}.jsonl"
        if not session_file.is_file():
            return []
        entries: list[dict[str, Any]] = []
        with session_file.open("r", encoding="utf-8") as handle:
            for line in handle:
                line = line.strip()
                if not line:
                    continue
                try:
                    entry = json.loads(line)
                except json.JSONDecodeError:
                    continue
                if isinstance(entry, dict) and entry.get("type") in {"user", "assistant"}:
                    entries.append(entry)
        if limit is not None and limit >= 0:
            entries = entries[-limit:]
        return entries


def _wrap_event(event: dict[str, Any]) -> dict[str, Any]:
    event_type = str(event.get("type") or "")
    if event_type == "stream_event":
        inner = event.get("event")
        inner_type = inner.get("type") if isinstance(inner, dict) else None
        if inner_type == "content_block_delta":
            delta = inner.get("delta") if isinstance(inner, dict) else None
            text = delta.get("text") if isinstance(delta, dict) else None
            if isinstance(text, str) and text:
                return {"message": {"method": "prompt/delta", "params": {"text": text}}}
        return {"message": {"method": "claude.stream_event", "params": event}}
    if event_type == "result":
        is_error = bool(event.get("is_error"))
        if is_error:
            errors = event.get("errors") or [event.get("result") or "claude turn failed"]
            return {
                "message": {
                    "method": "prompt/failed",
                    "params": {"error": "; ".join(str(e) for e in errors)},
                }
            }
        return {
            "message": {
                "method": "prompt/completed",
                "params": {"message": str(event.get("result") or ""), "status": "completed"},
            }
        }
    return {"message": {"method": f"claude.{event_type or 'event'}", "params": event}}


def claude_binary_available(config: Optional[RepoConfig | HubConfig]) -> bool:
    if config is None:
        return False
    try:
        binary = config.agent_binary("claude").strip()
    except Exception:
        return False
    if not binary:
        return False
    resolved = binary if Path(binary).is_absolute() else shutil.which(binary)
    return bool(resolved)


def build_claude_supervisor_from_config(
    config: RepoConfig | HubConfig,
    *,
    logger: Optional[logging.Logger] = None,
) -> Optional[ClaudeSupervisor]:
    try:
        binary = config.agent_binary("claude")
    except Exception:
        return None
    if not binary:
        return None
    return ClaudeSupervisor(binary, logger=logger)


__all__ = [
    "ClaudeSupervisor",
    "ClaudeSupervisorError",
    "build_claude_supervisor_from_config",
    "claude_binary_available",
]
