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
        self.assertTrue(c.third_party_complete)
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
        self.assertFalse(result.completeness.thirdparty_requested)
        self.assertTrue(result.completeness.third_party_complete)
        result.store.close()


class ThirdPartyResilienceTests(unittest.TestCase):
    def _fake(self, **kw) -> FakeGitHub:
        fake = FakeGitHub({"2026-07-01": 30}, runs_per_sha=1, **kw)  # 30 distinct commits
        for r in fake.runs:
            fake.thirdparty_suites_for_sha[r["head_sha"]] = [
                {"database_id": 90000 + r["id"], "node_id": f"TPS_{90000 + r['id']}",
                 "app_slug": "codspeed-hq", "checks": 1}
            ]
        return fake

    def _run(self, fake, *, db_path=":memory:", **kw):
        return run_rescue("o", "r", limit=30, db_path=db_path, token="t",
                          sleep=lambda *_: None, fetch_rest=fake.fetch, fetch_gql=fake.fetch, **kw)

    def test_a_slow_enum_batch_splits_down_and_still_completes(self) -> None:
        fake = self._fake()
        bad = {fake.runs[3]["head_sha"], fake.runs[17]["head_sha"]}
        fake.fail_tp_enum_shas = bad
        fake.tp_enum_ok_below = 2  # 504 only for batches >= 2; singletons succeed via GraphQL

        result = self._run(fake)
        c = result.completeness
        self.assertTrue(c.core_complete)
        self.assertTrue(c.third_party_complete, "singleton retries should have recovered")
        self.assertEqual(c.thirdparty_commits_unprobed, 0)
        self.assertEqual(c.thirdparty_gap_commits, 0)
        result.store.close()

    def test_enum_that_fails_even_as_a_singleton_uses_the_rest_fallback(self) -> None:
        fake = self._fake()
        bad = {fake.runs[5]["head_sha"]}
        fake.fail_tp_enum_shas = bad          # GraphQL 504s at every size
        # not in fail_tp_rest_shas -> the REST fallback works

        result = self._run(fake)
        c = result.completeness
        self.assertTrue(c.core_complete)
        self.assertTrue(c.third_party_complete)
        self.assertEqual(c.thirdparty_gap_commits, 0)
        self.assertEqual(c.thirdparty_suites, 30)
        result.store.close()

    def test_graphql_and_rest_both_fail_records_a_gap_and_keeps_core_complete(self) -> None:
        fake = self._fake()
        bad = {fake.runs[8]["head_sha"], fake.runs[9]["head_sha"], fake.runs[10]["head_sha"]}
        fake.fail_tp_enum_shas = set(bad)
        fake.fail_tp_rest_shas = set(bad)

        result = self._run(fake)
        c = result.completeness
        self.assertTrue(c.core_complete, "an optional third-party failure must not touch core")
        self.assertFalse(c.third_party_complete)
        self.assertEqual(c.thirdparty_gap_commits, 3)
        gaps = {r["ref"] for r in result.store.query(
            "SELECT ref FROM hydration_gap WHERE kind='thirdparty_enum'")}
        self.assertEqual(gaps, bad)
        result.store.close()

    def test_resume_retries_a_recorded_thirdparty_gap(self) -> None:
        import tempfile
        from pathlib import Path
        tmp = tempfile.TemporaryDirectory(ignore_cleanup_errors=True)
        db = str(Path(tmp.name) / "r.db")

        fake = self._fake()
        bad = {fake.runs[8]["head_sha"]}
        fake.fail_tp_enum_shas = set(bad)
        fake.fail_tp_rest_shas = set(bad)
        r1 = self._run(fake, db_path=db)
        self.assertEqual(r1.completeness.thirdparty_gap_commits, 1)
        self.assertFalse(r1.completeness.third_party_complete)
        r1.store.close()

        healthy = self._fake()  # same 30 commits, nothing fails now
        r2 = run_rescue("o", "r", db_path=db, token="t", sleep=lambda *_: None,
                        fetch_rest=healthy.fetch, fetch_gql=healthy.fetch)
        self.assertTrue(r2.resumed)
        self.assertEqual(r2.completeness.thirdparty_gap_commits, 0)
        self.assertTrue(r2.completeness.third_party_complete)
        self.assertTrue(r2.completeness.core_complete)
        r2.store.close()
        tmp.cleanup()


if __name__ == "__main__":
    unittest.main()
