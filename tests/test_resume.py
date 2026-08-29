"""Resume + idempotency: an interrupted `rescue` picks up where it stopped, redoing nothing."""

from __future__ import annotations

import tempfile
import unittest
from pathlib import Path

from runkeep.pipeline import run_rescue
from runkeep.storage import Store
from tests.fakes import FakeGitHub


class _Boom(Exception):
    """Stands in for an abrupt kill (SIGKILL / power loss) - not a catchable network error."""


class _Flaky:
    """Wraps a fake fetch; raises _Boom on the Nth matching call, then behaves normally."""

    def __init__(self, inner_fetch, *, boom_on_call: int) -> None:
        self._inner = inner_fetch
        self._boom_on = boom_on_call
        self.calls = 0

    def fetch(self, method, url, headers, body):
        self.calls += 1
        if self.calls == self._boom_on:
            raise _Boom(f"killed on call {self.calls}")
        return self._inner(method, url, headers, body)


def _fixture() -> FakeGitHub:
    return FakeGitHub(
        {"2026-03-01": 120, "2026-03-02": 120, "2026-03-03": 60},
        checks_per_suite={},
    )


def _db_snapshot(path: str) -> dict:
    s = Store(path)
    tables = ("workflow_run", "check_suite", "check_run", "status_context",
              "commit_status_probe", "discovery_slice", "hydration_gap")
    snap = {t: s.count(t) for t in tables}
    snap["hydrated"] = s.scalar("SELECT count(*) FROM workflow_run WHERE hydrated=1")
    snap["run_ids"] = sorted(r["database_id"] for r in s.query("SELECT database_id FROM workflow_run"))
    snap["check_ids"] = sorted(r["database_id"] for r in s.query("SELECT database_id FROM check_run"))
    s.close()
    return snap


class ResumeTests(unittest.TestCase):
    def setUp(self) -> None:
        self._tmp = tempfile.TemporaryDirectory(ignore_cleanup_errors=True)
        self.db = str(Path(self._tmp.name) / "r.db")

    def tearDown(self) -> None:
        self._tmp.cleanup()

    def _run(self, fake: FakeGitHub, *, fetch_gql=None):
        return run_rescue(
            "o", "r", db_path=self.db, token="t", cap=100,
            hydrate_batch=100, status_batch=100,
            fetch_rest=fake.fetch, fetch_gql=fetch_gql or fake.fetch,
        )

    def test_resume_after_a_kill_during_hydration(self) -> None:
        fake = _fixture()
        gql_flaky = _Flaky(fake.fetch, boom_on_call=2)  # discovery (REST) ok; die in hydration

        with self.assertRaises(_Boom):
            self._run(fake, fetch_gql=gql_flaky.fetch)

        mid = _db_snapshot(self.db)
        self.assertEqual(mid["workflow_run"], 300, "all runs discovered before the kill")
        self.assertLess(mid["hydrated"], 300, "hydration did not finish")
        self.assertGreater(mid["discovery_slice"], 0)

        # resume with a healthy fake
        fresh = _fixture()
        result = self._run(fresh)

        self.assertTrue(result.resumed)
        self.assertEqual(result.completeness.discovered_runs, 300)
        self.assertEqual(result.completeness.missing_run_hydrations, 0)
        self.assertTrue(result.completeness.core_complete)

        # the resumed run re-walks the (cheap) split tree but re-collects no leaf: every
        # slice it actually queried was an over-cap split, and it discovered nothing new.
        d = result.discovery
        self.assertGreater(d.slices_skipped, 0)
        self.assertEqual(d.slices_queried, d.slices_split, "resumed run re-collected a leaf slice")
        self.assertEqual(d.kept_runs, 0, "resumed run discovered new runs it should have skipped")
        self.assertEqual(
            fresh.calls_to("page=2"), 0, "resumed run paginated deeper into a completed slice"
        )
        result.store.close()

    def test_resume_after_a_kill_during_discovery(self) -> None:
        fake = _fixture()
        rest_flaky = _Flaky(fake.fetch, boom_on_call=4)

        with self.assertRaises(_Boom):
            run_rescue("o", "r", db_path=self.db, token="t", cap=100,
                       fetch_rest=rest_flaky.fetch, fetch_gql=fake.fetch)

        mid = _db_snapshot(self.db)
        self.assertLess(mid["workflow_run"], 300, "discovery was interrupted")

        fresh = _fixture()
        result = self._run(fresh)
        self.assertTrue(result.resumed)
        self.assertEqual(result.completeness.discovered_runs, 300)
        self.assertTrue(result.completeness.core_complete)
        result.store.close()

    def test_running_to_completion_twice_is_idempotent(self) -> None:
        r1 = self._run(_fixture())
        r1.store.close()
        snap1 = _db_snapshot(self.db)

        r2 = self._run(_fixture())
        self.assertTrue(r2.resumed)
        r2.store.close()
        snap2 = _db_snapshot(self.db)

        self.assertEqual(snap1, snap2, "a second full run changed the archive")
        self.assertEqual(len(snap1["run_ids"]), len(set(snap1["run_ids"])), "duplicate run rows")
        self.assertEqual(len(snap1["check_ids"]), len(set(snap1["check_ids"])), "duplicate check rows")


if __name__ == "__main__":
    unittest.main()
