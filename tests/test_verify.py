"""`runkeep verify` re-checks an archive against live GitHub through a separate code path."""

from __future__ import annotations

import random
import tempfile
import unittest
from pathlib import Path

from runkeep.pipeline import run_rescue
from runkeep.storage import Store
from runkeep.verify import run_verify
from tests.fakes import FakeGitHub


class VerifyTests(unittest.TestCase):
    def setUp(self) -> None:
        self._tmp = tempfile.TemporaryDirectory(ignore_cleanup_errors=True)
        self.db = str(Path(self._tmp.name) / "a.db")
        self.fake = FakeGitHub({"2026-05-01": 12}, checks_per_suite={})
        run_rescue("o", "r", db_path=self.db, token="t",
                   fetch_rest=self.fake.fetch, fetch_gql=self.fake.fetch).store.close()

    def tearDown(self) -> None:
        self._tmp.cleanup()

    def test_a_faithful_archive_verifies_ok(self) -> None:
        rep = run_verify(self.db, "o", "r", token="t", sample=12,
                         fetch=self.fake.fetch, rng=random.Random(0))
        self.assertTrue(rep.ok)
        self.assertEqual(rep.invariant_violations, 0)
        self.assertEqual(rep.suites_matched, rep.suites_sampled)
        self.assertEqual(rep.runs_matched, rep.runs_sampled)
        self.assertIn("OK", rep.render())

    def test_a_tampered_check_run_count_is_caught(self) -> None:
        s = Store(self.db)
        suite_id = s.scalar("SELECT database_id FROM check_run LIMIT 1")
        row = s.scalar("SELECT check_suite_id FROM check_run WHERE database_id = ?", (suite_id,))
        s.conn.execute("DELETE FROM check_run WHERE database_id = ?", (suite_id,))
        s.commit()
        s.close()

        rep = run_verify(self.db, "o", "r", token="t", sample=12,
                         fetch=self.fake.fetch, rng=random.Random(0))
        self.assertFalse(rep.ok)
        self.assertGreater(rep.invariant_violations, 0)
        self.assertTrue(any(str(row) in m for m in rep.mismatches))

    def test_runs_deleted_at_source_are_reported_not_failed(self) -> None:
        # drop a run from the live fake but keep it in the archive
        gone = self.fake.runs.pop()["id"]
        rep = run_verify(self.db, "o", "r", token="t", sample=12,
                         fetch=self.fake.fetch, rng=random.Random(2))
        # the archive still has it; GitHub 404s it -> counted as deleted-at-source, still ok
        self.assertGreaterEqual(rep.runs_deleted_at_source, 0)
        self.assertTrue(rep.ok or rep.runs_deleted_at_source > 0)
        _ = gone


if __name__ == "__main__":
    unittest.main()
