"""Thin GitHub REST + GraphQL clients with call/byte/cost accounting.

Both clients take an injectable ``fetch`` so tests drive them without network.
``fetch(method, url, headers, body) -> (status: int, resp_headers: dict, body: bytes)``.

Read-only by construction: RestClient only issues GET; GraphQLClient posts queries and
refuses any document containing a top-level ``mutation``.
"""

from __future__ import annotations

import http.client
import json
import socket
import time
import urllib.error
import urllib.parse
import urllib.request
from dataclasses import dataclass, field
from typing import Callable

from .errors import (
    AuthUnavailable,
    BadCredentials,
    GitHubServerError,
    GraphQLRequestError,
    NetworkError,
    RateLimited,
    RepoNotFound,
)

FetchFn = Callable[[str, str, dict, bytes | None], "tuple[int, dict, bytes]"]

REST_ROOT = "https://api.github.com"
GQL_URL = "https://api.github.com/graphql"
USER_AGENT = "runkeep/0.1.0"
_RETRY_STATUSES = {500, 502, 503, 504}
_HTTP_TIMEOUT = 60
_TRANSPORT_ERRORS = (
    urllib.error.URLError,
    http.client.IncompleteRead,
    http.client.RemoteDisconnected,
    ConnectionError,
    TimeoutError,
    socket.timeout,
)


class RetryableTransport(Exception):
    """A transport-level failure (reset, incomplete read, DNS blip) worth retrying."""


@dataclass
class CostSample:
    label: str
    cost: int
    remaining: int | None
    node_count: int | None


@dataclass
class Meter:
    """Running tally of everything the benchmark report needs."""

    rest_calls: int = 0
    graphql_calls: int = 0
    rest_bytes: int = 0
    graphql_bytes: int = 0
    graphql_cost_total: int = 0
    graphql_cost_samples: list[CostSample] = field(default_factory=list)
    rest_remaining_first: int | None = None
    rest_remaining_last: int | None = None
    graphql_remaining_first: int | None = None
    graphql_remaining_last: int | None = None
    rate_limit_waits: int = 0

    def record_rest(self, body: bytes, headers: dict) -> None:
        self.rest_calls += 1
        self.rest_bytes += len(body)
        rem = headers.get("X-RateLimit-Remaining") or headers.get("x-ratelimit-remaining")
        if rem is not None:
            rem_i = int(rem)
            if self.rest_remaining_first is None:
                self.rest_remaining_first = rem_i
            self.rest_remaining_last = rem_i

    def record_graphql(self, body: bytes, label: str, rate_limit: dict | None) -> None:
        self.graphql_calls += 1
        self.graphql_bytes += len(body)
        if rate_limit:
            cost = int(rate_limit.get("cost", 0) or 0)
            remaining = rate_limit.get("remaining")
            remaining = int(remaining) if remaining is not None else None
            node_count = rate_limit.get("nodeCount")
            node_count = int(node_count) if node_count is not None else None
            self.graphql_cost_total += cost
            self.graphql_cost_samples.append(CostSample(label, cost, remaining, node_count))
            if remaining is not None:
                if self.graphql_remaining_first is None:
                    self.graphql_remaining_first = remaining
                self.graphql_remaining_last = remaining


def _default_fetch(method: str, url: str, headers: dict, body: bytes | None) -> tuple[int, dict, bytes]:
    req = urllib.request.Request(url, data=body, headers=headers, method=method)
    try:
        with urllib.request.urlopen(req, timeout=_HTTP_TIMEOUT) as resp:
            return resp.status, dict(resp.headers), resp.read()
    except urllib.error.HTTPError as exc:  # 4xx/5xx still carry a body we want
        try:
            return exc.code, dict(exc.headers or {}), exc.read()
        except _TRANSPORT_ERRORS as read_exc:
            raise RetryableTransport(f"{method} {url}: {read_exc}") from read_exc
    except _TRANSPORT_ERRORS as exc:
        raise RetryableTransport(f"{method} {url}: {exc}") from exc


def _reset_delay(resp_headers: dict) -> int | None:
    reset = resp_headers.get("X-RateLimit-Reset") or resp_headers.get("x-ratelimit-reset")
    if not reset:
        return None
    try:
        return max(1, int(float(reset) - time.time()))
    except ValueError:
        return None


def classify_http_error(status: int, resp_headers: dict, body_text: str, *, authenticated: bool):
    """Map a non-200 GitHub response to a user-facing error (or GraphQLRequestError)."""
    if status == 401:
        return BadCredentials("GitHub rejected the token (HTTP 401). Check GITHUB_TOKEN.")
    if status == 404:
        if authenticated:
            return RepoNotFound("repository not found, or your token can't see it.")
        return RepoNotFound(
            "repository not found. If it is private, set GITHUB_TOKEN (a fine-grained token "
            "with read-only Actions access, or a classic token with the 'repo' scope)."
        )
    if status in (403, 429):
        retry_after = resp_headers.get("Retry-After") or resp_headers.get("retry-after")
        remaining = resp_headers.get("X-RateLimit-Remaining") or resp_headers.get("x-ratelimit-remaining")
        secondary = "secondary rate limit" in body_text.lower()
        if secondary or remaining == "0" or retry_after:
            return RateLimited(
                retry_after_s=int(retry_after) if retry_after else _reset_delay(resp_headers),
                authenticated=authenticated,
                secondary=secondary,
            )
        return BadCredentials(f"GitHub returned HTTP 403: {body_text[:200]}")
    if status >= 500:
        return GitHubServerError(f"GitHub returned HTTP {status} and did not recover after retries.")
    return GraphQLRequestError(f"HTTP {status}: {body_text[:300]}")


class _Base:
    def __init__(
        self,
        token: str | None,
        *,
        require_token: bool = True,
        fetch: FetchFn | None = None,
        meter: Meter | None = None,
        sleep: Callable[[float], None] = time.sleep,
        max_retries: int = 3,
        notify: Callable[[str], None] | None = None,
    ) -> None:
        if require_token and not token:
            raise AuthUnavailable("no GitHub token provided")
        self._token = token
        self._fetch = fetch or _default_fetch
        self.meter = meter or Meter()
        self._sleep = sleep
        self._max_retries = max_retries
        self._notify = notify

    @property
    def authenticated(self) -> bool:
        return bool(self._token)

    def _auth_headers(self) -> dict:
        h = {"User-Agent": USER_AGENT}
        if self._token:
            h["Authorization"] = f"Bearer {self._token}"
        return h

    def _do_fetch(self, method: str, url: str, headers: dict, body: bytes | None):
        """Fetch with transport-level retry (resets, incomplete reads). HTTP status
        handling stays with the caller."""
        for attempt in range(self._max_retries + 1):
            try:
                return self._fetch(method, url, headers, body)
            except RetryableTransport as exc:
                if attempt >= self._max_retries:
                    raise NetworkError(
                        "network error talking to api.github.com - check your connection "
                        "and try again."
                    ) from exc
                self._sleep(min(2 ** (attempt + 1), 15))


class RestClient(_Base):
    def get_json(self, path: str, params: dict | None = None) -> tuple[dict, dict]:
        url = REST_ROOT + path
        if params:
            url += "?" + urllib.parse.urlencode(params)
        headers = self._auth_headers()
        headers["Accept"] = "application/vnd.github+json"
        headers["X-GitHub-Api-Version"] = "2022-11-28"

        attempt = 0
        while True:
            status, resp_headers, body = self._do_fetch("GET", url, headers, None)
            self.meter.record_rest(body, resp_headers)
            if status == 200:
                return json.loads(body.decode("utf-8")), resp_headers
            if status in _RETRY_STATUSES and attempt < self._max_retries + 1:
                attempt += 1
                delay = min(3 * 2 ** attempt, 30)
                if self._notify:
                    self._notify(f"GitHub returned {status} - retrying in {delay}s ({attempt})")
                self._sleep(delay)
                continue
            body_text = body.decode("utf-8", "replace")
            if status in (403, 429) and attempt < self._max_retries:
                retry_after = resp_headers.get("Retry-After") or resp_headers.get("retry-after")
                secondary = "secondary rate limit" in body_text.lower()
                if retry_after or secondary:
                    attempt += 1
                    self.meter.rate_limit_waits += 1
                    delay = float(retry_after) if retry_after else min(2 ** (attempt + 2), 60)
                    self._on_rate_limit(delay, secondary)
                    self._sleep(min(delay, 60))
                    continue
            raise classify_http_error(
                status, resp_headers, body_text, authenticated=self.authenticated
            )

    def _on_rate_limit(self, delay: float, secondary: bool) -> None:
        """Overridable hook so the CLI can tell the user we're backing off, not hung."""
        if self._notify:
            kind = "secondary rate limit" if secondary else "rate limit"
            self._notify(f"GitHub {kind} - backing off {int(delay)}s")

    def paginate(
        self,
        path: str,
        *,
        list_key: str,
        params: dict | None = None,
        max_items: int | None = None,
        max_pages: int = 1000,
    ) -> tuple[list[dict], int | None]:
        """Page a list endpoint newest-first. Returns (items, total_count-if-exposed)."""
        params = dict(params or {})
        params.setdefault("per_page", 100)
        items: list[dict] = []
        total_count: int | None = None
        page = 1
        while page <= max_pages:
            params["page"] = page
            payload, _ = self.get_json(path, params)
            total_count = payload.get("total_count", total_count)
            batch = payload.get(list_key, [])
            if not batch:
                break
            items.extend(batch)
            if max_items is not None and len(items) >= max_items:
                return items[:max_items], total_count
            if len(batch) < params["per_page"]:
                break
            page += 1
        return items, total_count


class GraphQLClient(_Base):
    def query(self, document: str, variables: dict, *, label: str) -> dict:
        # Read-only guard: the spike never issues mutations.
        stripped = "\n".join(
            ln for ln in document.splitlines() if not ln.strip().startswith("#")
        )
        if "mutation" in stripped:
            raise GraphQLRequestError("refusing to send a document containing 'mutation'")
        headers = self._auth_headers()
        headers["Content-Type"] = "application/json"
        body = json.dumps({"query": document, "variables": variables}).encode("utf-8")

        attempt = 0
        while True:
            status, resp_headers, resp_body = self._do_fetch("POST", GQL_URL, headers, body)
            if status != 200:
                if status in _RETRY_STATUSES and attempt < self._max_retries + 1:
                    attempt += 1
                    delay = min(3 * 2 ** attempt, 30)
                    if self._notify:
                        self._notify(f"GitHub returned {status} - retrying in {delay}s ({attempt})")
                    self._sleep(delay)
                    continue
                body_text = resp_body.decode("utf-8", "replace")
                if status in (403, 429) and attempt < self._max_retries:
                    retry_after = resp_headers.get("Retry-After") or resp_headers.get("retry-after")
                    secondary = "secondary rate limit" in body_text.lower()
                    if retry_after or secondary:
                        attempt += 1
                        self.meter.rate_limit_waits += 1
                        delay = float(retry_after) if retry_after else min(2 ** (attempt + 2), 60)
                        self._on_rate_limit(delay, secondary)
                        self._sleep(min(delay, 60))
                        continue
                self.meter.record_graphql(resp_body, label, None)
                raise classify_http_error(
                    status, resp_headers, body_text, authenticated=self.authenticated
                )
            payload = json.loads(resp_body.decode("utf-8"))
            rate_limit = (payload.get("data") or {}).get("rateLimit")
            self.meter.record_graphql(resp_body, label, rate_limit)
            if payload.get("errors"):
                # Distinguish partial-data (some nodes null) from a hard failure.
                if payload.get("data"):
                    return payload  # caller inspects; partial is a real signal, not a crash
                raise GraphQLRequestError(
                    f"GraphQL[{label}] errors: {json.dumps(payload['errors'])[:600]}"
                )
            return payload
