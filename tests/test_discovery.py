"""Discovery: recursive created= time-interval slicing down to one UTC second.

The slicer must never drop or double-count a run, and must only fail when a single second
alone can't be paged.
"""

from __future__ import annotations

import unittest

from runkeep.discovery import discover_range
from runkeep.errors import MinIntervalCapExceeded
from runkeep.http_client import RestClient
from tests.fakes import FakeGitHub


def _client(fake: FakeGitHub) -> RestClient:
    return RestClient("t", fetch=fake.fetch)


def _run(fake: FakeGitHub, *, skip_slices=None, **kw):
    """Drive discover_range through its sink and return (collected runs, stats)."""
    collected: list[dict] = []
    slices: list[tuple[str, str]] = []

    def sink(s_iso, e_iso, runs):
        collected.extend(runs)
        slices.append((s_iso, e_iso))

    stats = discover_range(
        _client(fake), "o", "r", cap=1000, skip_slices=skip_slices, sink=sink, **kw
    )
    lim = kw.get("limit")
    kept = collected[:lim] if lim else collected
    stats.slices = slices  # type: ignore[attr-defined]
    return kept, stats


class DiscoveryTests(unittest.TestCase):
    def test_1000_plus_runs_in_one_day_split_successfully(self) -> None:
        fake = FakeGitHub({"2026-03-15": 1500})
        runs, stats = _run(fake, since="2026-03-15", until="2026-03-15")
        self.assertEqual(len(runs), 1500)
        self.assertGreaterEqual(stats.slices_split, 1, "the day window must have been bisected")
        self.assertGreater(stats.max_depth, 0)

    def test_adjacent_slices_lose_zero_ids(self) -> None:
        fake = FakeGitHub({"2026-03-13": 40, "2026-03-14": 1400, "2026-03-15": 60})
        runs, _ = _run(fake, since="2026-03-13", until="2026-03-15")
        got = {r["id"] for r in runs}
        expected = {r["id"] for r in fake.runs}
        self.assertEqual(got, expected)
        self.assertEqual(len(runs), 1500)

    def test_adjacent_slices_duplicate_zero_ids_before_final_dedupe(self) -> None:
        fake = FakeGitHub({"2026-03-14": 1400})
        runs, stats = _run(fake, since="2026-03-14", until="2026-03-14")
        self.assertEqual(stats.duplicates_removed, 0, "the -1s boundary scheme must not overlap")
        self.assertEqual(stats.raw_runs, len(runs))

    def test_runs_sharing_the_exact_same_second_all_survive_a_split(self) -> None:
        fake = FakeGitHub({"2026-03-15": 1200}, runs_at=["2026-03-15T12:00:00Z"] * 5)
        noon_ids = {r["id"] for r in fake.runs if r["created_at"] == "2026-03-15T12:00:00Z"}
        self.assertGreaterEqual(len(noon_ids), 5)

        runs, stats = _run(fake, since="2026-03-15", until="2026-03-15")
        got = {r["id"] for r in runs}
        self.assertEqual(len(runs), 1205)
        self.assertEqual(stats.duplicates_removed, 0)
        self.assertTrue(noon_ids <= got, "every run sharing 12:00:00 must be collected exactly once")

    def test_fails_only_when_a_single_second_exceeds_the_cap(self) -> None:
        fake = FakeGitHub(runs_at=["2026-03-15T12:00:00Z"] * 1500)
        with self.assertRaises(MinIntervalCapExceeded) as ctx:
            _run(fake, since="2026-03-15", until="2026-03-15")
        self.assertEqual(ctx.exception.second_iso, "2026-03-15T12:00:00Z")
        self.assertEqual(ctx.exception.total, 1500)

    def test_boundary_seconds_are_partitioned_exactly(self) -> None:
        # two runs one second apart, plus bulk to force a split near them
        fake = FakeGitHub(
            {"2026-03-15": 1400},
            runs_at=["2026-03-15T11:59:59Z", "2026-03-15T12:00:00Z"],
        )
        runs, stats = _run(fake, since="2026-03-15", until="2026-03-15")
        got = {r["id"] for r in runs}
        self.assertEqual(len(runs), 1402)
        self.assertEqual(stats.duplicates_removed, 0)
        for r in fake.runs:
            self.assertIn(r["id"], got)

    def test_respects_limit_newest_first(self) -> None:
        fake = FakeGitHub({"2026-05-01": 300, "2026-05-02": 300})
        runs, _ = _run(fake, since="2026-05-01", until="2026-05-02", limit=100)
        self.assertEqual(len(runs), 100)
        self.assertTrue(all(r["created_at"][:10] == "2026-05-02" for r in runs))

    def test_bare_date_range_covers_the_whole_days(self) -> None:
        fake = FakeGitHub({"2026-06-10": 5, "2026-06-11": 5, "2026-06-12": 5})
        runs, _ = _run(fake, since="2026-06-11", until="2026-06-11")
        self.assertEqual(len(runs), 5)
        self.assertTrue(all(r["created_at"][:10] == "2026-06-11" for r in runs))


if __name__ == "__main__":
    unittest.main()
