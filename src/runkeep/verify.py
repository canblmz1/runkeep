"""``runkeep verify FILE.db OWNER/REPO`` — re-query GitHub and confirm the archive matches.

A trust feature. It does two things:

1. checks the archive's internal invariant on **every** suite (stored check-run count ==
   recorded expected count), and
2. spot-checks a random sample of suites *and* runs against live GitHub through an
   independent code path (direct REST ``filter=all`` pagination), so a bug in the rescue
   pipeline can't hide a bug in its own verification.

Read-only. Exits non-zero if anything doesn't line up.
"""

from __future__ import annotations

import random
import time
from dataclasses import dataclass, field

from .http_client import RestClient
from .metrics import compute_completeness
from .storage import Store


@dataclass
class VerifyReport:
    repo: str
    db_path: str
    total_suites: int
    invariant_violations: int
    suites_sampled: int
    suites_matched: int
    runs_sampled: int
    runs_matched: int
    runs_deleted_at_source: int
    core_complete: bool = True
    third_party_requested: bool = False
    third_party_complete: bool = True
    third_party_gap_commits: int = 0
    mismatches: list[str] = field(default_factory=list)
    elapsed_s: float = 0.0

    @property
    def ok(self) -> bool:
        """A spot-check + invariant pass on what's in the archive - not a proof of every
        remote record. 'ok' means: everything checked lines up, the invariant holds, and
        the archive's own core_complete flag is true."""
        return (
            self.invariant_violations == 0
            and self.suites_matched == self.suites_sampled
            and self.runs_matched + self.runs_deleted_at_source == self.runs_sampled
            and self.core_complete
        )

    def as_dict(self) -> dict:
        return {**self.__dict__, "ok": self.ok}

    def render(self) -> str:
        tick = "OK" if self.ok else "FAIL"
        lines = [
            "",
            f"  verify {self.repo}  ({self.db_path})",
            "",
            f"  archive invariant   {self.total_suites - self.invariant_violations}/{self.total_suites} suites consistent",
            f"  suites vs GitHub    {self.suites_matched}/{self.suites_sampled} match  (spot-check)",
            f"  runs vs GitHub      {self.runs_matched}/{self.runs_sampled} match  (spot-check)"
            + (f"  +{self.runs_deleted_at_source} deleted at source"
               if self.runs_deleted_at_source else ""),
            f"  core archive        {'complete' if self.core_complete else 'INCOMPLETE'}",
        ]
        if self.third_party_requested:
            tp = "complete" if self.third_party_complete else (
                f"incomplete ({self.third_party_gap_commits} commits could not be queried)")
            lines.append(f"  third-party checks  {tp}")
        for m in self.mismatches[:20]:
            lines.append(f"    - {m}")
        lines += ["", f"  {tick}  ({self.elapsed_s:.1f}s)", ""]
        return "\n".join(lines)


def _rest_check_run_ids(rest: RestClient, owner: str, repo: str, suite_db: int) -> tuple[set[int], int]:
    items, total = rest.paginate(
        f"/repos/{owner}/{repo}/check-suites/{suite_db}/check-runs",
        list_key="check_runs",
        params={"per_page": 100, "filter": "all"},
    )
    return {c["id"] for c in items}, (total if total is not None else len(items))


def run_verify(
    db_path: str,
    owner: str,
    repo: str,
    *,
    token: str | None = None,
    sample: int = 15,
    notify=None,
    fetch=None,
    rng: random.Random | None = None,
) -> VerifyReport:
    rng = rng or random.Random(1_048_583)
    store = Store(db_path)
    rest = RestClient(token, require_token=False, fetch=fetch, notify=notify)
    slug = f"{owner}/{repo}"
    t0 = time.monotonic()

    total_suites = store.count("check_suite")
    invariant = store.scalar(
        "SELECT count(*) FROM check_suite s WHERE s.checkrun_total_count IS NOT NULL "
        "AND s.checkrun_total_count <> "
        "(SELECT count(*) FROM check_run c WHERE c.check_suite_id = s.database_id)"
    ) or 0

    mismatches: list[str] = []

    suites = store.query(
        "SELECT s.database_id AS sid, s.checkrun_total_count AS expected, "
        "(SELECT count(*) FROM check_run c WHERE c.check_suite_id = s.database_id) AS stored "
        "FROM check_suite s WHERE s.checkrun_total_count IS NOT NULL"
    )
    if suites:
        picks = rng.sample(suites, min(sample, len(suites)))
        biggest = max(suites, key=lambda r: r["expected"])
        if biggest not in picks:
            picks.append(biggest)
    else:
        picks = []

    suites_matched = 0
    for i, s in enumerate(picks, 1):
        if notify and i % 10 == 0:
            notify(f"verify: {i}/{len(picks)} suites")
        gh_ids, gh_total = _rest_check_run_ids(rest, owner, repo, s["sid"])
        stored_ids = {
            r["database_id"]
            for r in store.query(
                "SELECT database_id FROM check_run WHERE check_suite_id = ?", (s["sid"],)
            )
        }
        if gh_total == s["expected"] == s["stored"] and stored_ids <= gh_ids:
            suites_matched += 1
        else:
            mismatches.append(
                f"suite {s['sid']}: archive expected={s['expected']} stored={s['stored']}, "
                f"GitHub filter=all={gh_total}, id-subset={stored_ids <= gh_ids}"
            )

    runs = store.query(
        "SELECT database_id AS rid, run_number, head_sha, status, conclusion FROM workflow_run"
    )
    run_picks = rng.sample(runs, min(max(sample // 2, 1), len(runs))) if runs else []
    runs_matched = 0
    runs_deleted = 0
    for i, r in enumerate(run_picks, 1):
        if notify and i % 10 == 0:
            notify(f"verify: {i}/{len(run_picks)} runs")
        try:
            live, _ = rest.get_json(f"/repos/{owner}/{repo}/actions/runs/{r['rid']}")
        except Exception as exc:  # noqa: BLE001  (RepoNotFound et al. -> run gone / inaccessible)
            if "not found" in str(exc).lower():
                runs_deleted += 1
                continue
            mismatches.append(f"run {r['rid']}: {exc}")
            continue
        if (live.get("run_number") == r["run_number"]
                and live.get("head_sha") == r["head_sha"]
                and live.get("conclusion") == r["conclusion"]):
            runs_matched += 1
        else:
            mismatches.append(
                f"run {r['rid']}: archive #{r['run_number']}/{(r['head_sha'] or '')[:8]}/"
                f"{r['conclusion']} vs GitHub #{live.get('run_number')}/"
                f"{(live.get('head_sha') or '')[:8]}/{live.get('conclusion')}"
            )

    c = compute_completeness(store)
    store.close()
    return VerifyReport(
        repo=slug,
        db_path=db_path,
        total_suites=total_suites,
        invariant_violations=invariant,
        suites_sampled=len(picks),
        suites_matched=suites_matched,
        runs_sampled=len(run_picks),
        runs_matched=runs_matched,
        runs_deleted_at_source=runs_deleted,
        core_complete=c.core_complete,
        third_party_requested=c.thirdparty_requested,
        third_party_complete=c.third_party_complete,
        third_party_gap_commits=c.thirdparty_gap_commits,
        mismatches=mismatches,
        elapsed_s=time.monotonic() - t0,
    )
