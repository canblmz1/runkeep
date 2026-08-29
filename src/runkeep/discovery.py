"""Workflow-run discovery — recursive ``created=`` time-interval slicing.

GitHub's filtered ``/actions/runs?created=`` listing can only be paged so far, so a window
holding more than ``cap`` runs is bisected. Bisection goes all the way down to a **single UTC
second** (day -> hour -> minute -> second); only when one 1-second window alone exceeds the
cap does it fail, via :class:`MinIntervalCapExceeded`. No silent truncation, ever.

Slices are half-open by construction: a window ``[s, e]`` splits at ``mid`` into ``[s, mid-1s]``
and ``[mid, e]``. GitHub's ``A..B`` is inclusive on both ends, and ``created_at`` is
second-precision, so every second belongs to exactly one slice — adjacent slices neither lose
nor double-count a run. A final dedupe pass is a belt-and-suspenders check: it should remove 0.
"""

from __future__ import annotations

from dataclasses import dataclass
from datetime import datetime, timedelta, timezone

from .errors import MinIntervalCapExceeded
from .http_client import RestClient

_RUNS_KEY = "workflow_runs"
_ONE_SEC = timedelta(seconds=1)
_FMT = "%Y-%m-%dT%H:%M:%SZ"


@dataclass
class DiscoveryStats:
    slices_queried: int = 0
    slices_split: int = 0
    slices_skipped: int = 0  # already completed on a previous run
    max_depth: int = 0
    kept_runs: int = 0
    raw_runs: int = 0
    duplicates_removed: int = 0


def _runs_path(owner: str, repo: str) -> str:
    return f"/repos/{owner}/{repo}/actions/runs"


def _iso(dt: datetime) -> str:
    return dt.strftime(_FMT)


def _parse_bound(value: str, *, is_end: bool) -> datetime:
    """A bare date means the whole UTC day; a full timestamp is taken as-is (second precision)."""
    value = value.strip()
    if "T" in value:
        dt = datetime.fromisoformat(value.replace("Z", "+00:00"))
    else:
        d = datetime.fromisoformat(value)
        dt = d.replace(hour=23, minute=59, second=59) if is_end else d
    if dt.tzinfo is None:
        dt = dt.replace(tzinfo=timezone.utc)
    return dt.astimezone(timezone.utc).replace(microsecond=0)


def _collect_window(
    client: RestClient, path: str, *, created: str, cap: int, max_items: int | None
) -> tuple[list[dict] | None, int]:
    """Page one ``created=`` window fully. Returns (runs, total); runs is None iff total > cap."""
    params = {"per_page": 100, "created": created, "page": 1}
    first, _ = client.get_json(path, params)
    total = int(first.get("total_count", 0) or 0)
    if total > cap:
        return None, total
    runs = list(first.get(_RUNS_KEY, []))
    page = 2
    while len(runs) < total:
        if max_items is not None and len(runs) >= max_items:
            break
        params["page"] = page
        nxt, _ = client.get_json(path, params)
        batch = nxt.get(_RUNS_KEY, [])
        if not batch:
            break
        runs.extend(batch)
        page += 1
    return (runs[:max_items] if max_items is not None else runs), total


def discover_range(
    client: RestClient,
    owner: str,
    repo: str,
    *,
    since: str,
    until: str | None = None,
    cap: int = 1000,
    limit: int | None = None,
    skip_slices: set[tuple[str, str]] | None = None,
    sink=None,
    on_progress=None,
) -> DiscoveryStats:
    """Walk ``[since, until]`` newest-first, bisecting any window over ``cap``.

    ``sink(start_iso, end_iso, runs)`` is called once per fully-collected leaf slice — the
    caller persists those runs and records the slice, so an interrupted run resumes by passing
    the completed ``(start_iso, end_iso)`` pairs back in ``skip_slices``.
    """
    path = _runs_path(owner, repo)
    start = _parse_bound(since, is_end=False)
    end = (
        _parse_bound(until, is_end=True)
        if until
        else datetime.now(timezone.utc).replace(microsecond=0)
    )
    skip = skip_slices or set()

    stats = DiscoveryStats()
    seen_ids: set[int] = set()
    # Stack of (start, end, depth). Push older then newer so newer is processed first.
    stack: list[tuple[datetime, datetime, int]] = [(start, end, 0)]

    while stack:
        s, e, depth = stack.pop()
        if s > e:
            continue
        if limit is not None and stats.kept_runs >= limit:
            break

        key = (_iso(s), _iso(e))
        if key in skip:
            stats.slices_skipped += 1
            continue

        stats.slices_queried += 1
        stats.max_depth = max(stats.max_depth, depth)
        remaining = None if limit is None else max(0, limit - stats.kept_runs)
        runs, total = _collect_window(
            client, path, created=f"{key[0]}..{key[1]}", cap=cap, max_items=remaining
        )

        if runs is None:  # over cap -> split, or fail if we're already at 1 second
            if s == e:
                raise MinIntervalCapExceeded(_iso(s), total, cap)
            stats.slices_split += 1
            span = int((e - s).total_seconds()) + 1  # inclusive seconds in [s, e]
            mid = s + timedelta(seconds=span // 2)
            stack.append((s, mid - _ONE_SEC, depth + 1))  # older half
            stack.append((mid, e, depth + 1))  # newer half (processed next)
            continue

        stats.raw_runs += len(runs)
        fresh = []
        for r in runs:
            if r["id"] in seen_ids:
                stats.duplicates_removed += 1
            else:
                seen_ids.add(r["id"])
                fresh.append(r)
        stats.kept_runs += len(fresh)
        if sink:
            sink(key[0], key[1], fresh)
        if on_progress:
            on_progress(stats.kept_runs)

    return stats
