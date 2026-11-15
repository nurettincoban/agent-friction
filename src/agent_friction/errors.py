"""Exception hierarchy for agent-friction."""

from __future__ import annotations


class AgentFrictionError(Exception):
    """Base class for all errors raised deliberately by agent-friction."""


class NoTranscriptsError(AgentFrictionError):
    """Raised when no agent transcripts could be found for a repository."""
