"""`runkeep check` — the headline command: counts, the 90-day split, defensible wording."""

from __future__ import annotations

import unittest
from datetime import datetime, timezone

from runkeep.check import display_count, format_check, retention_line, run_check
from runkeep.errors import RepoNotFound
from tests.fakes import FakeGitHub

NOW = datetime(2026, 8, 29, tzinfo=timezone.utc)  # cutoff = 2026-05-31


class CheckTests(unittest.TestCase):
    def test_counts_split_at_the_90_day_cutoff(self) -> None:
        fake = FakeGitHub(
            {"2026-01-10": 50, "2026-08-20": 30}, repo_created_at="2025-12-01T00:00:00Z"
        )
        r = run_check("o", "r", token="t", now=NOW, fetch=fake.fetch)
        self.assertEqual(r.total_runs, 80)
        self.assertEqual(r.older_than_90d, 50)
        self.assertEqual(r.within_90d, 30)
        self.assertEqual(r.cutoff_date, "2026-05-31")
        self.assertFalse(r.counts_approximate)

    def test_oldest_run_is_exact_for_small_repos(self) -> None:
        fake = FakeGitHub(
            {"2026-01-10": 5, "2026-08-20": 3}, repo_created_at="2025-12-01T00:00:00Z"
        )
        r = run_check("o", "r", token="t", now=NOW, fetch=fake.fetch)
        self.assertEqual(r.oldest_run_at[:10], "2026-01-10")
        self.assertFalse(r.oldest_is_estimate)

    def test_oldest_run_exact_via_window_fetch_for_large_repos(self) -> None:
        fake = FakeGitHub(
            {"2024-03-10": 600, "2024-03-11": 600, "2026-08-20": 50},
            repo_created_at="2024-01-01T00:00:00Z",
        )
        r = run_check("o", "r", token="t", now=NOW, fetch=fake.fetch)
        self.assertEqual(r.oldest_run_at[:10], "2024-03-10")
        self.assertFalse(r.oldest_is_estimate)
        self.assertLess(r.rest_calls, 20, "must stay well under 25 calls / 10 seconds")

    def test_oldest_run_day_estimate_when_a_single_day_exceeds_the_page_cap(self) -> None:
        fake = FakeGitHub(
            {"2024-03-15": 1500, "2026-08-20": 20}, repo_created_at="2024-01-01T00:00:00Z"
        )
        r = run_check("o", "r", token="t", now=NOW, fetch=fake.fetch)
        self.assertEqual(r.oldest_run_at[:10], "2024-03-15")
        self.assertTrue(r.oldest_is_estimate)
        self.assertLess(r.rest_calls, 25)

    def test_repo_with_no_runs(self) -> None:
        fake = FakeGitHub({}, repo_created_at="2026-08-01T00:00:00Z")
        r = run_check("o", "r", token="t", now=NOW, fetch=fake.fetch)
        self.assertEqual(r.total_runs, 0)
        self.assertEqual(r.older_than_90d, 0)
        self.assertIsNone(r.oldest_run_at)
        self.assertIn("no workflow runs", format_check(r, color=False))

    def test_repo_not_found_raises_user_facing_error(self) -> None:
        fake = FakeGitHub({}, missing_repo_name="ghost")
        with self.assertRaises(RepoNotFound):
            run_check("o", "ghost", token="t", now=NOW, fetch=fake.fetch)

    def test_works_without_a_token(self) -> None:
        fake = FakeGitHub({"2026-01-10": 4}, repo_created_at="2025-12-01T00:00:00Z")
        r = run_check("o", "r", token=None, now=NOW, fetch=fake.fetch)
        self.assertFalse(r.authenticated)
        self.assertEqual(r.total_runs, 4)

    def test_large_counts_render_approximate_small_counts_exact(self) -> None:
        self.assertEqual(display_count(2_667), ("2,667", False))
        self.assertEqual(display_count(4_999), ("4,999", False))
        self.assertEqual(display_count(104_388), ("~104k", True))
        self.assertEqual(display_count(49_188), ("~49k", True))
        self.assertEqual(display_count(7_234), ("~7k", True))
        self.assertEqual(display_count(1_530_000), ("~1.5M", True))

    def test_wording_is_defensible_not_a_guaranteed_deletion_date(self) -> None:
        line = retention_line(2_667)
        self.assertEqual(line, "2,667 runs are outside the 90-day retention window.")
        self.assertNotIn("deleted", line)
        self.assertNotIn("will be", line)
        self.assertEqual(
            retention_line(104_388), "~104k runs are outside the 90-day retention window."
        )

    def test_formatted_output_shape(self) -> None:
        fake = FakeGitHub(
            {"2026-01-10": 50, "2026-08-20": 30}, repo_created_at="2025-12-01T00:00:00Z"
        )
        text = format_check(run_check("o", "r", token="t", now=NOW, fetch=fake.fetch), color=False)
        self.assertIn("o/r", text)
        self.assertIn("50 runs are outside the 90-day retention window.", text)
        self.assertIn("GitHub starts applying that window to run history on Oct 1, 2026.", text)
        self.assertIn("runkeep rescue o/r", text)
        self.assertNotIn("will be deleted", text)
        self.assertNotIn("\x1b[", text, "color=False must emit no ANSI codes")


if __name__ == "__main__":
    unittest.main()
