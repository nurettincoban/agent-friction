"""Exception hierarchy for agent-friction."""

from __future__ import annotations


class AgentFrictionError(Exception):
    """Base class for all errors raised deliberately by agent-friction."""


class NoTranscriptsError(AgentFrictionError):
    """Raised when no agent transcripts could be found for a repository."""


class BadTranscriptError(NoTranscriptsError):
    """A transcript path that was given does not exist, or is not a transcript.

    A mistake in the invocation rather than "nothing to analyse": raised even
    where an empty result is allowed.
    """
