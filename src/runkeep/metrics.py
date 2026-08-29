"""Completeness, computed from the archive file itself — never from in-memory counters.

Two independent verdicts:

* ``core_complete`` — every discovered run hydrated, every **workflow-run** check suite's
  stored check-run count equals its known expected total, no such suite has an unknown
  expected total, and every commit was probed for legacy statuses. This is what "the archive
  is usable" means. It does **not** depend on third-party capture.

* ``third_party_complete`` — only meaningful when third-party capture was requested: every
  unique commit was enumerated for independent check suites, no ``thirdparty_enum`` gap
  remains, and every third-party suite's check runs were hydrated.
"""

from __future__ import annotations

from dataclasses import asdict, dataclass

from .storage import Store


@dataclass
class Completeness:
    discovered_runs: int
    hydrated_runs: int
    missing_run_hydrations: int

    check_suites: int             # all suites (Actions + third-party)
    expected_checks: int          # core (workflow-run) suites, known expected totals
    stored_checks: int            # core suites
    missing_checks: int           # core suites
    indeterminate_suites: int     # core suites whose expected total is unknown

    unique_commits: int
    legacy_status_contexts: int
    commits_status_probed: int
    commits_status_unprobed: int

    thirdparty_requested: bool
    thirdparty_suites: int
    thirdparty_check_runs: int
    thirdparty_commits_probed: int
    thirdparty_commits_unprobed: int
    thirdparty_gap_commits: int
    thirdparty_pending_suites: int

    core_complete: bool
    third_party_complete: bool

    def as_dict(self) -> dict:
        return asdict(self)


def compute_completeness(store: Store) -> Completeness:
    s = store.scalar

    discovered = s("SELECT count(*) FROM workflow_run") or 0
    hydrated = s("SELECT count(*) FROM workflow_run WHERE hydrated = 1") or 0
    missing_runs = s("SELECT count(*) FROM workflow_run WHERE hydrated = 0") or 0

    all_suites = s("SELECT count(*) FROM check_suite") or 0

    # --- core: only suites tied to a workflow run ---
    core_where = "check_suite WHERE workflow_run_id IS NOT NULL"
    indeterminate = s(f"SELECT count(*) FROM {core_where} AND checkrun_total_count IS NULL") or 0
    expected = s(
        f"SELECT COALESCE(SUM(checkrun_total_count), 0) FROM {core_where} "
        "AND checkrun_total_count IS NOT NULL"
    ) or 0
    core_per_suite = store.query(
        "SELECT s.checkrun_total_count AS expected, "
        "(SELECT count(*) FROM check_run c WHERE c.check_suite_id = s.database_id) AS stored "
        "FROM check_suite s WHERE s.workflow_run_id IS NOT NULL AND s.checkrun_total_count IS NOT NULL"
    )
    missing_checks = sum(max(0, r["expected"] - r["stored"]) for r in core_per_suite)
    stored_core_checks = s(
        "SELECT count(*) FROM check_run c JOIN check_suite s ON s.database_id = c.check_suite_id "
        "WHERE s.workflow_run_id IS NOT NULL"
    ) or 0

    unique_commits = s(
        "SELECT count(DISTINCT head_sha) FROM workflow_run WHERE head_sha IS NOT NULL"
    ) or 0
    legacy_contexts = s("SELECT count(*) FROM status_context") or 0
    status_probed = s("SELECT count(*) FROM commit_status_probe") or 0
    status_unprobed = max(0, unique_commits - status_probed)

    # --- third-party ---
    requested = (store.get_meta("thirdparty_requested") or "false") == "true"
    tp_suites = s("SELECT count(*) FROM check_suite WHERE workflow_run_id IS NULL") or 0
    tp_checks = s(
        "SELECT count(*) FROM check_run c JOIN check_suite s ON s.database_id = c.check_suite_id "
        "WHERE s.workflow_run_id IS NULL"
    ) or 0
    tp_probed = s("SELECT count(*) FROM thirdparty_probe") or 0
    tp_unprobed = max(0, unique_commits - tp_probed) if requested else 0
    tp_gap_commits = s("SELECT count(*) FROM hydration_gap WHERE kind = 'thirdparty_enum'") or 0
    tp_pending = s(
        "SELECT count(*) FROM check_suite WHERE workflow_run_id IS NULL "
        "AND checkrun_source = 'pending'"
    ) or 0
    tp_suite_gaps = s("SELECT count(*) FROM hydration_gap WHERE kind = 'thirdparty_suite'") or 0

    core_complete = (
        missing_runs == 0
        and missing_checks == 0
        and indeterminate == 0
        and status_unprobed == 0
    )
    third_party_complete = (not requested) or (
        tp_unprobed == 0 and tp_gap_commits == 0 and tp_pending == 0 and tp_suite_gaps == 0
    )

    return Completeness(
        discovered_runs=discovered,
        hydrated_runs=hydrated,
        missing_run_hydrations=missing_runs,
        check_suites=all_suites,
        expected_checks=expected,
        stored_checks=stored_core_checks,
        missing_checks=missing_checks,
        indeterminate_suites=indeterminate,
        unique_commits=unique_commits,
        legacy_status_contexts=legacy_contexts,
        commits_status_probed=status_probed,
        commits_status_unprobed=status_unprobed,
        thirdparty_requested=requested,
        thirdparty_suites=tp_suites,
        thirdparty_check_runs=tp_checks,
        thirdparty_commits_probed=tp_probed,
        thirdparty_commits_unprobed=tp_unprobed,
        thirdparty_gap_commits=tp_gap_commits,
        thirdparty_pending_suites=tp_pending,
        core_complete=core_complete,
        third_party_complete=third_party_complete,
    )
