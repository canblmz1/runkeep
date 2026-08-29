"""End-to-end: REST discovery -> GraphQL bulk hydration -> SQLite, with completeness proven."""

from __future__ import annotations

import unittest

from runkeep.pipeline import run_rescue
from tests.fakes import FakeGitHub


class RoundtripTests(unittest.TestCase):
    def test_run_and_hydration_roundtrip(self) -> None:
        fake = FakeGitHub(
            {"2026-06-01": 6},
            checks_per_suite={20001: 4, 20002: 4, 20003: 4, 20004: 4, 20005: 4, 20006: 4},
        )
        group0_sha = fake.runs[0]["head_sha"]
        fake.statuses_for_sha = {
            group0_sha: [
                {"context": "buildkite/build", "state": "SUCCESS", "description": "passed",
                 "targetUrl": "https://bk/1"}
            ]
        }

        result = run_rescue(
            "o", "r", limit=6, db_path=":memory:", token="t",
            fetch_rest=fake.fetch, fetch_gql=fake.fetch,
        )
        c = result.completeness

        self.assertEqual(c.discovered_runs, 6)
        self.assertEqual(c.hydrated_runs, 6)
        self.assertEqual(c.missing_run_hydrations, 0)
        self.assertEqual(c.check_suites, 6)
        self.assertEqual(c.expected_checks, 24)
        self.assertEqual(c.stored_checks, 24)
        self.assertEqual(c.missing_checks, 0)
        self.assertEqual(c.indeterminate_suites, 0)
        self.assertTrue(c.core_complete)

        # the relationship graph survives into SQLite
        graph = result.store.query(
            "SELECT count(DISTINCT r.database_id) runs, count(DISTINCT s.database_id) suites, "
            "count(c.database_id) checks "
            "FROM workflow_run r "
            "JOIN check_suite s ON s.workflow_run_id = r.database_id "
            "JOIN check_run c ON c.check_suite_id = s.database_id"
        )[0]
        self.assertEqual(graph, {"runs": 6, "suites": 6, "checks": 24})

        # suite app identity captured (from CheckSuite.app, not CheckRun.app)
        apps = result.store.query("SELECT DISTINCT slug FROM app")
        self.assertEqual(apps, [{"slug": "github-actions"}])

        # legacy status context captured and deduped, separate from check runs
        self.assertEqual(c.legacy_status_contexts, 1)
        ctx = result.store.query("SELECT context, state FROM status_context")[0]
        self.assertEqual(ctx["context"], "buildkite/build")
        self.assertEqual(c.commits_status_unprobed, 0)

        result.store.close()

    def test_null_hydration_node_is_a_recorded_gap_not_a_silent_drop(self) -> None:
        fake = FakeGitHub({"2026-06-02": 3})
        drop = fake.runs[1]["node_id"]
        fake.null_run_nodes = {drop}

        result = run_rescue(
            "o", "r", limit=3, db_path=":memory:", token="t",
            fetch_rest=fake.fetch, fetch_gql=fake.fetch,
        )
        c = result.completeness

        self.assertEqual(c.discovered_runs, 3, "the run is still discovered and stored")
        self.assertEqual(c.missing_run_hydrations, 1)
        self.assertFalse(c.core_complete)
        gaps = result.store.query("SELECT kind, ref FROM hydration_gap")
        self.assertIn({"kind": "run", "ref": drop}, gaps)
        result.store.close()

    def test_over_100_checks_triggers_rest_filter_all_fallback(self) -> None:
        fake = FakeGitHub(
            {"2026-06-03": 1},
            over_100_suites={20001},
            checks_per_suite={20001: 105},
        )
        result = run_rescue(
            "o", "r", limit=1, db_path=":memory:", token="t",
            fetch_rest=fake.fetch, fetch_gql=fake.fetch,
        )
        c = result.completeness

        self.assertEqual(c.stored_checks, 105, "must not truncate at the GraphQL first:100 page")
        self.assertEqual(c.missing_checks, 0)
        self.assertTrue(c.core_complete)

        suite = result.store.query(
            "SELECT checkrun_source, checkrun_total_count FROM check_suite"
        )[0]
        self.assertEqual(suite["checkrun_source"], "graphql+rest_all")
        self.assertEqual(suite["checkrun_total_count"], 105)
        self.assertTrue(
            any("filter=all" in call for call in fake.calls),
            "the REST filter=all fallback must actually be exercised",
        )
        result.store.close()


if __name__ == "__main__":
    unittest.main()
