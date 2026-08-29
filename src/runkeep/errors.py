"""Error types. Loud failures only — no silent truncation anywhere.

``UserFacingError`` and its subclasses carry a message that is safe and useful to print to
the user verbatim, with no traceback. The CLI catches them, prints ``message``, and exits
with ``exit_code``. Everything else is a bug and is allowed to crash loudly in dev.
"""

from __future__ import annotations


class RunKeepError(Exception):
    """Base for all runkeep errors."""


class UserFacingError(RunKeepError):
    """An error whose str() is meant to be shown to the user as-is (no stack trace)."""

    exit_code = 1


class AuthUnavailable(UserFacingError):
    """No usable GitHub token where one is required."""

    exit_code = 3


class RepoNotFound(UserFacingError):
    """The repository does not exist, or a token is needed to see it (private)."""

    exit_code = 4


class BadCredentials(UserFacingError):
    """GitHub rejected the token (401)."""

    exit_code = 5


class RateLimited(UserFacingError):
    """GitHub primary or secondary rate limit hit."""

    exit_code = 6

    def __init__(self, *, retry_after_s: int | None, authenticated: bool, secondary: bool = False) -> None:
        wait = (
            f"retry in {retry_after_s}s"
            if retry_after_s
            else "wait a bit"
        )
        hint = "" if authenticated else " Set GITHUB_TOKEN for a 5,000/hour limit."
        kind = "secondary rate limit" if secondary else "rate limit"
        super().__init__(f"GitHub {kind} hit - {wait}.{hint}")
        self.retry_after_s = retry_after_s
        self.authenticated = authenticated
        self.secondary = secondary


class NetworkError(UserFacingError):
    """The network dropped and retries were exhausted."""

    exit_code = 7


class GitHubServerError(UserFacingError):
    """GitHub returned 5xx and retries were exhausted."""

    exit_code = 8


class MinIntervalCapExceeded(UserFacingError):
    """The finest slice the API supports — a single UTC second — still exceeds the page cap.

    The recursive discovery slicer subdivides day -> hour -> minute -> second. This is raised
    only when one 1-second window alone holds more runs than can be paged safely; there is no
    finer slice to fall back to, so it fails loudly rather than truncate.
    """

    exit_code = 9

    def __init__(self, second_iso: str, total: int, cap: int) -> None:
        super().__init__(
            f"the 1-second window {second_iso} has {total} runs, over the {cap}-result page "
            f"cap - GitHub's created= filter has no finer granularity to slice on"
        )
        self.second_iso = second_iso
        self.total = total
        self.cap = cap


class HydrationError(RunKeepError):
    """A GraphQL hydration response was structurally unusable (not just incomplete)."""


class GraphQLRequestError(RunKeepError):
    """GraphQL endpoint returned transport-level or top-level ``errors`` we cannot proceed past."""
