"""Opt-in third-party capture: independent check suites (no workflow run) + their check runs."""

from __future__ import annotations

import unittest

from runkeep.pipeline import run_rescue
from tests.fakes import FakeGitHub


class ThirdPartyTests(unittest.TestCase):
    def test_thirdparty_suites_and_checks_captured_by_default(self) -> None:
        fake = FakeGitHub({"2026-07-01": 2}, checks_per_suite={20001: 3, 20002: 3})
        sha0 = fake.runs[0]["head_sha"]
        fake.thirdparty_suites_for_sha = {
            sha0: [
                {"database_id": 77001, "node_id": "TPS_77001", "app_slug": "codspeed-hq", "checks": 1},
                {"database_id": 77002, "node_id": "TPS_77002", "app_slug": "renovate", "checks": 0},
            ]
        }

        result = run_rescue(
            "o", "r", limit=2, db_path=":memory:", token="t",
            fetch_rest=fake.fetch, fetch_gql=fake.fetch,
        )
        st = result.store

        tp = st.query(
            "SELECT database_id, workflow_run_id FROM check_suite "
            "WHERE workflow_run_id IS NULL ORDER BY database_id"
        )
        self.assertEqual([r["database_id"] for r in tp], [77001, 77002])

        apps = {r["slug"] for r in st.query("SELECT slug FROM app")}
        self.assertIn("codspeed-hq", apps)
        self.assertIn("renovate", apps)

        self.assertEqual(
            st.scalar("SELECT count(*) FROM check_run WHERE check_suite_id = 77001"), 1
        )
        self.assertEqual(
            st.query("SELECT app_slug FROM check_run WHERE check_suite_id = 77001")[0]["app_slug"],
            "codspeed-hq",
        )

        c = result.completeness
        self.assertTrue(c.core_complete)
        self.assertEqual(c.missing_checks, 0)
        self.assertEqual(c.thirdparty_suites, 2)
        self.assertEqual(c.thirdparty_check_runs, 1)
        result.store.close()

    def test_thirdparty_skipped_when_disabled(self) -> None:
        fake = FakeGitHub({"2026-07-02": 2})
        fake.thirdparty_suites_for_sha = {
            fake.runs[0]["head_sha"]: [
                {"database_id": 77003, "node_id": "TPS_77003", "app_slug": "codspeed-hq", "checks": 1}
            ]
        }
        result = run_rescue(
            "o", "r", limit=2, db_path=":memory:", token="t", with_thirdparty=False,
            fetch_rest=fake.fetch, fetch_gql=fake.fetch,
        )
        self.assertEqual(
            result.store.scalar("SELECT count(*) FROM check_suite WHERE workflow_run_id IS NULL"), 0
        )
        self.assertEqual(result.completeness.thirdparty_suites, 0)
        result.store.close()


if __name__ == "__main__":
    unittest.main()
