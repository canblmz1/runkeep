"""``runkeep check OWNER/REPO`` — the headline command.

Read-only. Works unauthenticated for public repos. A handful of tiny ``per_page=1`` requests:
two ``created=`` counts give both the total and the 90-day split (GitHub caps the *unfiltered*
``total_count`` at 40 000, so we never trust it), and the oldest run is found either by a
direct page (small repos) or a seeded ~9-call binary search on ``created=`` date (large repos).

Wording is kept to what GitHub's own announcement supports: runs *outside the retention
window*, which GitHub *starts applying* on 2026-10-01 — not a guaranteed deletion timestamp.
GitHub's ``created=`` search counts drift a few percent on large ranges, so counts at or above
``APPROX_AT`` are shown rounded with a ``~``.
"""

from __future__ import annotations

import time
from dataclasses import asdict, dataclass
from datetime import date, datetime, timedelta, timezone

from .http_client import RestClient

RETENTION_DAYS = 90
POLICY_DATE = "Oct 1, 2026"
DIRECT_PAGE_LIMIT = 1000  # GitHub 422s on deep offsets; stay well under
SEED_DAYS = 430  # GitHub already trims public runs at ~400d, so seed the search there
MAX_BISECT = 12
APPROX_AT = 5_000  # counts >= this are GitHub search estimates; show them rounded

CHANGELOG_URL = (
    "https://github.blog/changelog/"
    "2026-08-27-actions-retention-will-cover-checks-workflow-runs-and-statuses/"
)


@dataclass
class CheckResult:
    owner: str
    repo: str
    total_runs: int
    older_than_90d: int
    within_90d: int
    oldest_run_at: str | None
    oldest_is_estimate: bool
    cutoff_date: str
    counts_approximate: bool
    rest_calls: int
    elapsed_s: float
    authenticated: bool

    def as_dict(self) -> dict:
        return asdict(self)


def display_count(n: int) -> tuple[str, bool]:
    """(display string, is_approximate). Small counts exact; large ones rounded to 2 sig figs."""
    if n < APPROX_AT:
        return f"{n:,}", False
    digits = len(str(n))
    rounded = round(n, -(digits - 2))
    return f"~{rounded:,}", True


def retention_line(n: int) -> str:
    shown, _ = display_count(n)
    noun = "run" if n == 1 else "runs"
    return f"{shown} {noun} are outside the 90-day retention window."


def _runs_path(owner: str, repo: str) -> str:
    return f"/repos/{owner}/{repo}/actions/runs"


def _count(rest: RestClient, path: str, created: str) -> int:
    payload, _ = rest.get_json(path, {"per_page": 1, "created": created})
    return int(payload.get("total_count", 0) or 0)


def _page_to_oldest(rest: RestClient, path: str, params: dict) -> str | None:
    payload, _ = rest.get_json(path, params)
    runs = payload.get("workflow_runs") or []
    return runs[0]["created_at"] if runs else None


def _find_oldest(
    rest: RestClient,
    path: str,
    *,
    repo_created: date,
    today: date,
    total: int,
    older: int,
    cutoff: str,
) -> tuple[str | None, bool]:
    # Small enough to page straight to the last (oldest) row — exact, one call.
    if total <= DIRECT_PAGE_LIMIT:
        got = _page_to_oldest(rest, path, {"per_page": 1, "page": total})
        if got:
            return got, False
    elif 0 < older <= DIRECT_PAGE_LIMIT:
        got = _page_to_oldest(rest, path, {"per_page": 1, "page": older, "created": f"<{cutoff}"})
        if got:
            return got, False

    # Bisect on date. Invariant: (# runs created before `lo`) == before_lo, likewise before_hi.
    hi = today + timedelta(days=1)
    before_hi = total
    lo = max(repo_created, today - timedelta(days=SEED_DAYS))
    before_lo = 0
    if lo > repo_created:
        before_lo = _count(rest, path, f"<{lo.isoformat()}")
        if before_lo > 0:  # genuinely older history exists — widen to the repo's birth
            lo, before_lo = repo_created, 0

    for _ in range(MAX_BISECT):
        if (hi - lo).days <= 1 or (before_hi - before_lo) <= DIRECT_PAGE_LIMIT:
            break
        mid = lo + (hi - lo) // 2
        n = _count(rest, path, f"<{mid.isoformat()}")
        if n > before_lo:
            hi, before_hi = mid, n
        else:
            lo, before_lo = mid, n

    window = before_hi - before_lo
    if 0 < window <= DIRECT_PAGE_LIMIT:
        got = _page_to_oldest(
            rest, path,
            {"per_page": 1, "page": window,
             "created": f"{lo.isoformat()}..{(hi - timedelta(days=1)).isoformat()}"},
        )
        if got:
            return got, False
    return f"{lo.isoformat()}T00:00:00Z", True


def run_check(
    owner: str,
    repo: str,
    *,
    token: str | None = None,
    now: datetime | None = None,
    fetch=None,
    sleep=None,
    notify=None,
) -> CheckResult:
    now = now or datetime.now(timezone.utc)
    today = now.date()
    cutoff = (today - timedelta(days=RETENTION_DAYS)).isoformat()

    kwargs = {"require_token": False, "fetch": fetch, "notify": notify}
    if sleep is not None:
        kwargs["sleep"] = sleep
    rest = RestClient(token, **kwargs)

    t0 = time.monotonic()

    repo_json, _ = rest.get_json(f"/repos/{owner}/{repo}")  # 404 -> RepoNotFound
    repo_created = _parse_day(repo_json.get("created_at")) or date(2015, 1, 1)

    path = _runs_path(owner, repo)
    older = _count(rest, path, f"<{cutoff}")
    within = _count(rest, path, f">={cutoff}")
    total = older + within

    if total == 0:
        oldest_at, oldest_est = None, False
    else:
        oldest_at, oldest_est = _find_oldest(
            rest, path,
            repo_created=repo_created, today=today,
            total=total, older=older, cutoff=cutoff,
        )

    _, total_approx = display_count(total)
    _, older_approx = display_count(older)

    return CheckResult(
        owner=owner,
        repo=repo,
        total_runs=total,
        older_than_90d=older,
        within_90d=within,
        oldest_run_at=oldest_at,
        oldest_is_estimate=oldest_est,
        cutoff_date=cutoff,
        counts_approximate=total_approx or older_approx,
        rest_calls=rest.meter.rest_calls,
        elapsed_s=round(time.monotonic() - t0, 2),
        authenticated=rest.authenticated,
    )


def _parse_day(iso: str | None) -> date | None:
    if not iso:
        return None
    try:
        return datetime.fromisoformat(iso.replace("Z", "+00:00")).date()
    except ValueError:
        return None


# --------------------------------------------------------------------------- formatting

_DIM = "\x1b[2m"
_BOLD = "\x1b[1m"
_RESET = "\x1b[0m"


def _age(oldest_iso: str, today: date) -> str:
    d = _parse_day(oldest_iso)
    if not d:
        return ""
    days = (today - d).days
    if days < 45:
        return f"{days} days ago"
    if days < 550:
        return f"{max(1, round(days / 30))} months ago"
    return f"{days / 365.25:.0f} years ago"


def format_check(result: CheckResult, *, color: bool) -> str:
    dim = _DIM if color else ""
    bold = _BOLD if color else ""
    reset = _RESET if color else ""
    today = date.fromisoformat(result.cutoff_date) + timedelta(days=RETENTION_DAYS)

    if result.oldest_run_at:
        age = _age(result.oldest_run_at, today)
        oldest = result.oldest_run_at[:10] + (f"  {dim}({age}){reset}" if age else "")
    else:
        oldest = f"{dim}none{reset}"

    total_s, _ = display_count(result.total_runs)
    older_s, _ = display_count(result.older_than_90d)
    rows = [
        ("total workflow runs", total_s),
        ("older than 90 days", older_s),
        ("oldest run", oldest),
    ]
    width = max(len(label) for label, _ in rows)
    body = "\n".join(f"  {dim}{label.ljust(width)}{reset}   {value}" for label, value in rows)

    parts = [f"\n  {bold}{result.owner}/{result.repo}{reset}\n", body, ""]

    if result.total_runs == 0:
        parts.append(f"  {dim}no workflow runs to archive.{reset}\n")
        return "\n".join(parts)

    line = retention_line(result.older_than_90d)
    parts.append(f"  {bold}{line}{reset}" if color else f"  {line}")
    parts.append(
        f"  {dim}GitHub starts applying that window to run history on {POLICY_DATE}.{reset}"
    )
    parts.append("")
    parts.append(
        f"  {dim}archive it:{reset}  runkeep rescue {result.owner}/{result.repo}"
    )
    if result.counts_approximate:
        parts.append(
            f"\n  {dim}counts >= {APPROX_AT:,} are GitHub's live search estimates "
            f"(they drift a few %).{reset}"
        )
    parts.append("")
    return "\n".join(parts)
