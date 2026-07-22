from __future__ import annotations

import uuid
from pathlib import Path
from typing import Any, AsyncIterator, Optional

from ...core.sse import format_sse
from ..base import AgentHarness
from ..types import (
    AgentId,
    ConversationRef,
    ModelCatalog,
    ModelSpec,
    RuntimeCapability,
    TerminalTurnResult,
    TranscriptEntry,
    TurnRef,
)
from .supervisor import ClaudeSupervisor, _wrap_event

CLAUDE_CAPABILITIES = frozenset(
    [
        RuntimeCapability("durable_threads"),
        RuntimeCapability("message_turns"),
        RuntimeCapability("interrupt"),
        RuntimeCapability("active_thread_discovery"),
        RuntimeCapability("model_listing"),
        RuntimeCapability("event_streaming"),
    ]
)

# ponytail: `claude --help` has no subcommand that lists installed/available
# models (checked `claude model --help`, `claude config --help`); the CLI's
# own --model docs only advertise these aliases. Static catalog, refresh by
# hand if Anthropic adds a listing command.
_MODEL_CATALOG = ModelCatalog(
    default_model="sonnet",
    models=[
        ModelSpec(
            id="sonnet",
            display_name="Claude Sonnet",
            supports_reasoning=False,
            reasoning_options=[],
        ),
        ModelSpec(
            id="opus",
            display_name="Claude Opus",
            supports_reasoning=False,
            reasoning_options=[],
        ),
        ModelSpec(
            id="haiku",
            display_name="Claude Haiku",
            supports_reasoning=False,
            reasoning_options=[],
        ),
        ModelSpec(
            id="fable",
            display_name="Claude Fable",
            supports_reasoning=False,
            reasoning_options=[],
        ),
    ],
)

_PERMISSION_MODES = {
    "acceptEdits",
    "auto",
    "bypassPermissions",
    "manual",
    "dontAsk",
    "plan",
}

# Best-effort mapping from the approval_mode vocabulary other harnesses/routes
# already pass (e.g. "yolo", "on-request") onto claude's own --permission-mode
# choices. Not a real interactive per-tool-call approval loop (the CLI in
# --print mode picks the mode once at launch) so we don't advertise the
# "approvals" capability, only best-effort honor the static hint.
_PERMISSION_MODE_ALIASES = {
    "yolo": "bypassPermissions",
    "dangerfullaccess": "bypassPermissions",
    "never": "bypassPermissions",  # codex-vocab: ticket-flow's DefaultAgentPool
    # emits autorunner_approval_policy="never" for yolo mode, not "yolo" itself
    "on-request": "manual",
    "workspacewrite": "acceptEdits",
    "accept-edits": "acceptEdits",
    "acceptedits": "acceptEdits",
    "dont-ask": "dontAsk",
    "dontask": "dontAsk",
}


def _map_permission_mode(approval_mode: Optional[str]) -> Optional[str]:
    if not approval_mode:
        return None
    normalized = approval_mode.strip()
    if normalized in _PERMISSION_MODES:
        return normalized
    return _PERMISSION_MODE_ALIASES.get(normalized.lower())


class ClaudeHarness(AgentHarness):
    agent_id: AgentId = AgentId("claude")
    display_name = "Claude"
    capabilities = CLAUDE_CAPABILITIES

    def __init__(self, supervisor: ClaudeSupervisor) -> None:
        self._supervisor = supervisor

    async def ensure_ready(self, workspace_root: Path) -> None:
        _ = workspace_root  # stateless CLI, nothing to warm up

    async def model_catalog(self, workspace_root: Path) -> ModelCatalog:
        _ = workspace_root
        return _MODEL_CATALOG

    async def new_conversation(
        self, workspace_root: Path, title: Optional[str] = None
    ) -> ConversationRef:
        _ = workspace_root, title  # claude's --name flag has no post-launch setter
        return ConversationRef(agent=self.agent_id, id=str(uuid.uuid4()))

    async def list_conversations(self, workspace_root: Path) -> list[ConversationRef]:
        sessions = self._supervisor.list_sessions_on_disk(workspace_root)
        return [
            ConversationRef(agent=self.agent_id, id=session_id)
            for session_id, _mtime in sessions
        ]

    async def resume_conversation(
        self, workspace_root: Path, conversation_id: str
    ) -> ConversationRef:
        _ = workspace_root
        self._supervisor.mark_known(conversation_id)
        return ConversationRef(agent=self.agent_id, id=conversation_id)

    async def start_turn(
        self,
        workspace_root: Path,
        conversation_id: str,
        prompt: str,
        model: Optional[str],
        reasoning: Optional[str],
        *,
        approval_mode: Optional[str],
        sandbox_policy: Optional[Any],
        input_items: Optional[list[dict[str, Any]]] = None,
    ) -> TurnRef:
        # reasoning: no --reasoning-effort equivalent found on the print-mode
        # CLI; sandbox_policy: no CLI flag maps cleanly, --permission-mode
        # already covers most of the same ground via approval_mode.
        _ = reasoning, sandbox_policy, input_items
        turn_id = await self._supervisor.start_turn(
            workspace_root,
            conversation_id,
            prompt,
            model=model,
            permission_mode=_map_permission_mode(approval_mode),
        )
        return TurnRef(conversation_id=conversation_id, turn_id=turn_id)

    async def wait_for_turn(
        self,
        workspace_root: Path,
        conversation_id: str,
        turn_id: Optional[str],
        *,
        timeout: Optional[float] = None,
    ) -> TerminalTurnResult:
        _ = workspace_root, conversation_id
        if not turn_id:
            raise ValueError("Claude wait_for_turn requires a turn id")
        return await self._supervisor.wait_for_turn(turn_id, timeout=timeout)

    async def interrupt(
        self, workspace_root: Path, conversation_id: str, turn_id: Optional[str]
    ) -> None:
        _ = workspace_root, conversation_id
        # Process-level abort per the CLI contract: no separate interrupt RPC.
        if turn_id:
            await self._supervisor.interrupt(turn_id)

    async def transcript_history(
        self,
        workspace_root: Path,
        conversation_id: str,
        *,
        limit: Optional[int] = None,
    ) -> list[TranscriptEntry]:
        entries = self._supervisor.read_transcript(
            workspace_root, conversation_id, limit=limit
        )
        transcript: list[TranscriptEntry] = []
        for entry in entries:
            message = entry.get("message")
            if not isinstance(message, dict):
                continue
            role = str(message.get("role") or "")
            content = message.get("content")
            text = _flatten_content(content)
            if not text:
                continue
            transcript.append(
                TranscriptEntry(
                    role=role,
                    text=text,
                    turn_id=None,
                    created_at=entry.get("timestamp"),
                )
            )
        return transcript

    async def stream_events(
        self, workspace_root: Path, conversation_id: str, turn_id: str
    ) -> AsyncIterator[str]:
        _ = workspace_root, conversation_id
        async for event in self._supervisor.stream_events(turn_id):
            yield format_sse("claude", _wrap_event(event))


def _flatten_content(content: Any) -> str:
    if isinstance(content, str):
        return content
    if isinstance(content, list):
        parts: list[str] = []
        for block in content:
            if isinstance(block, dict) and block.get("type") == "text":
                text = block.get("text")
                if isinstance(text, str):
                    parts.append(text)
        return "".join(parts)
    return ""


__all__ = ["CLAUDE_CAPABILITIES", "ClaudeHarness"]
