"""Unified tool-error policy for all comparison architectures.

Tools never raise recoverable failures toward the model. Missing files,
invalid candidate paths, and OS-level write failures are returned as
``TOOL_ERROR: ...`` strings so the agent can read the error and self-correct
within the same trajectory instead of aborting the whole generation.

Every architecture's skill-read and candidate-write tools (T2 today; T1,
T3-T5 when implemented) must funnel failures through :func:`tool_error` so
the policy stays uniform across the comparison. The policy applies from the
v2 baseline on and is recorded in exported result workbooks.
"""

TOOL_ERROR_PREFIX = "TOOL_ERROR:"


def tool_error(exc: BaseException) -> str:
    """Format one recoverable tool failure as a model-readable string."""

    return f"{TOOL_ERROR_PREFIX} {type(exc).__name__}: {exc}"
