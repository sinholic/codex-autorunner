"""Real `claude` CLI harness support."""

from .harness import CLAUDE_CAPABILITIES, ClaudeHarness
from .supervisor import (
    ClaudeSupervisor,
    ClaudeSupervisorError,
    build_claude_supervisor_from_config,
    claude_binary_available,
)

__all__ = [
    "CLAUDE_CAPABILITIES",
    "ClaudeHarness",
    "ClaudeSupervisor",
    "ClaudeSupervisorError",
    "build_claude_supervisor_from_config",
    "claude_binary_available",
]
