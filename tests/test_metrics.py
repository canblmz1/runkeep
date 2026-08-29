"""Completeness metrics: proven zeroes only. Unknown expected != zero missing."""

from __future__ import annotations

import unittest

from runkeep.metrics import compute_completeness
from runkeep.models import CheckRunRow, RunRow, SuiteRow
from runkeep.storage import Store


def _run(db: int, *, hydrated: bool, sha: str = "sha") -> RunRow:
    return RunRow(
        database_id=db, node_id=f"WFR_{db}", run_number=db, run_attempt=1, workflow_id=1,
        workflow_name="CI", event="push", status="completed", conclusion="success",
        head_sha=sha, head_branch="main", display_title="t", actor_login="a",
        triggering_actor_login="a", created_at="t", updated_at="t", run_started_at="t",
        html_url="u", check_suite_id=db * 10, hydrated=hydrated,
    )


def _suite(db: int, run_db: int, *, total: int | None, complete: bool) -> SuiteRow:
    return SuiteRow(
        database_id=db, node_id=f"CS_{db}", workflow_run_id=run_db, status="COMPLETED",
        conclusion="SUCCESS", app_id=None, head_sha="sha", branch="main", created_at="t",
        updated_at="t", checkrun_total_count=total, checkrun_source="graphql",
        checkrun_complete=complete,
    )


def _check(db: int, suite_db: int) -> CheckRunRow:
    return CheckRunRow(
        database_id=db, node_id=f"CR_{db}", check_suite_id=suite_db, name="job",
        status="COMPLETED", conclusion="SUCCESS", started_at=None, completed_at=None,
        details_url=None, app_slug="github-actions",
    )


class MetricsTests(unittest.TestCase):
    def setUp(self) -> None:
        self.s = Store(":memory:")
        self.s.init_schema()

    def _seed_ok(self) -> None:
        for rd in (1, 2):
            self.s.upsert_run(_run(rd, hydrated=True))
            self.s.upsert_suite(_suite(rd * 10, rd, total=1, complete=True))
            self.s.replace_check_runs(rd * 10, [_check(rd * 100, rd * 10)])
        self.s.record_status_probe("sha", has_status=False, context_count=0)
        self.s.commit()

    def test_all_green_is_core_complete(self) -> None:
        self._seed_ok()
        c = compute_completeness(self.s)
        self.assertEqual(c.discovered_runs, 2)
        self.assertEqual(c.hydrated_runs, 2)
        self.assertEqual(c.missing_run_hydrations, 0)
        self.assertEqual(c.expected_checks, 2)
        self.assertEqual(c.stored_checks, 2)
        self.assertEqual(c.missing_checks, 0)
        self.assertEqual(c.indeterminate_suites, 0)
        self.assertTrue(c.core_complete)

    def test_unhydrated_run_breaks_completeness(self) -> None:
        self._seed_ok()
        self.s.upsert_run(_run(3, hydrated=False))  # discovered, never came back from GraphQL
        self.s.commit()
        c = compute_completeness(self.s)
        self.assertEqual(c.discovered_runs, 3)
        self.assertEqual(c.missing_run_hydrations, 1)
        self.assertFalse(c.core_complete)

    def test_short_suite_counts_missing_checks(self) -> None:
        self._seed_ok()
        self.s.upsert_run(_run(4, hydrated=True))
        self.s.upsert_suite(_suite(40, 4, total=5, complete=False))
        self.s.replace_check_runs(40, [_check(401, 40), _check(402, 40), _check(403, 40)])
        self.s.commit()
        c = compute_completeness(self.s)
        self.assertEqual(c.missing_checks, 2)  # expected 5, stored 3
        self.assertFalse(c.core_complete)

    def test_unknown_expected_is_indeterminate_not_zero(self) -> None:
        self._seed_ok()
        self.s.upsert_run(_run(5, hydrated=True))
        self.s.upsert_suite(_suite(50, 5, total=None, complete=False))  # totalCount unavailable
        self.s.replace_check_runs(50, [_check(501, 50)])
        self.s.commit()
        c = compute_completeness(self.s)
        self.assertEqual(c.indeterminate_suites, 1)
        self.assertFalse(c.core_complete, "cannot claim complete when an expected total is unknown")


if __name__ == "__main__":
    unittest.main()
