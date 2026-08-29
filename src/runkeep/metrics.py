"""Completeness computed from the archive itself — never from in-memory counters.

``core_complete`` is true only when every discovered run came back from GraphQL hydration,
every check suite's stored check-run count equals its *known* expected total, and no suite has
an unknown expected total. An unknown expected total is reported as ``indeterminate`` — it is
never silently treated as "zero missing".
"""

from __future__ import annotations

from dataclasses import dataclass

from .storage import Store


@dataclass
class Completeness:
    discovered_runs: int
    hydrated_runs: int
    missing_run_hydrations: int

    check_suites: int
    expected_checks: int          # sum over suites with a KNOWN expected total
    stored_checks: int
    missing_checks: int           # sum of per-suite max(0, expected - stored), known suites only
    indeterminate_suites: int     # suites whose expected total could not be established

    unique_commits: int
    legacy_status_contexts: int
    commits_status_probed: int
    commits_status_unprobed: int

    thirdparty_suites: int
    thirdparty_check_runs: int

    core_complete: bool

    def as_dict(self) -> dict:
        return {k: v for k, v in self.__dict__.items()}


def compute_completeness(store: Store) -> Completeness:
    discovered = store.scalar("SELECT count(*) FROM workflow_run") or 0
    hydrated = store.scalar("SELECT count(*) FROM workflow_run WHERE hydrated = 1") or 0
    missing_runs = store.scalar("SELECT count(*) FROM workflow_run WHERE hydrated = 0") or 0

    suites = store.scalar("SELECT count(*) FROM check_suite") or 0
    indeterminate = (
        store.scalar("SELECT count(*) FROM check_suite WHERE checkrun_total_count IS NULL") or 0
    )
    expected = (
        store.scalar(
            "SELECT COALESCE(SUM(checkrun_total_count), 0) FROM check_suite "
            "WHERE checkrun_total_count IS NOT NULL"
        )
        or 0
    )
    stored_checks = store.scalar("SELECT count(*) FROM check_run") or 0

    per_suite = store.query(
        "SELECT s.database_id AS sid, s.checkrun_total_count AS expected, "
        "       (SELECT count(*) FROM check_run c WHERE c.check_suite_id = s.database_id) AS stored "
        "FROM check_suite s WHERE s.checkrun_total_count IS NOT NULL"
    )
    missing_checks = sum(max(0, r["expected"] - r["stored"]) for r in per_suite)

    unique_commits = (
        store.scalar("SELECT count(DISTINCT head_sha) FROM workflow_run WHERE head_sha IS NOT NULL")
        or 0
    )
    legacy_contexts = store.scalar("SELECT count(*) FROM status_context") or 0
    probed = store.scalar("SELECT count(*) FROM commit_status_probe") or 0

    tp_suites = store.scalar("SELECT count(*) FROM check_suite WHERE workflow_run_id IS NULL") or 0
    tp_checks = (
        store.scalar(
            "SELECT count(*) FROM check_run c JOIN check_suite s ON s.database_id = c.check_suite_id "
            "WHERE s.workflow_run_id IS NULL"
        )
        or 0
    )

    core_complete = missing_runs == 0 and missing_checks == 0 and indeterminate == 0

    return Completeness(
        discovered_runs=discovered,
        hydrated_runs=hydrated,
        missing_run_hydrations=missing_runs,
        check_suites=suites,
        expected_checks=expected,
        stored_checks=stored_checks,
        missing_checks=missing_checks,
        indeterminate_suites=indeterminate,
        unique_commits=unique_commits,
        legacy_status_contexts=legacy_contexts,
        commits_status_probed=probed,
        commits_status_unprobed=max(0, unique_commits - probed),
        thirdparty_suites=tp_suites,
        thirdparty_check_runs=tp_checks,
        core_complete=core_complete,
    )
