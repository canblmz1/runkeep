"""SQLite archive: schema round-trips the core graph, upserts are idempotent, gaps recorded."""

from __future__ import annotations

import unittest

from runkeep.models import AppRow, CheckRunRow, RunRow, StatusContextRow, SuiteRow
from runkeep.storage import Store


def _run(**over) -> RunRow:
    base = dict(
        database_id=1,
        node_id="WFR_1",
        run_number=7,
        run_attempt=1,
        workflow_id=42,
        workflow_name="CI",
        event="push",
        status="completed",
        conclusion="success",
        head_sha="abc123",
        head_branch="main",
        display_title="run 7",
        actor_login="octocat",
        triggering_actor_login="octocat",
        created_at="2026-01-01T00:00:00Z",
        updated_at="2026-01-01T00:01:00Z",
        run_started_at="2026-01-01T00:00:05Z",
        html_url="https://x/runs/1",
        check_suite_id=100,
        hydrated=False,
    )
    base.update(over)
    return RunRow(**base)


class StorageTests(unittest.TestCase):
    def setUp(self) -> None:
        self.store = Store(":memory:")
        self.store.init_schema()

    def test_core_graph_round_trips(self) -> None:
        self.store.upsert_app(AppRow(database_id=15368, node_id="A_1", slug="github-actions", name="GitHub Actions"))
        self.store.upsert_run(_run(hydrated=True))
        self.store.upsert_suite(
            SuiteRow(
                database_id=100, node_id="CS_1", workflow_run_id=1, status="COMPLETED",
                conclusion="SUCCESS", app_id=15368, head_sha="abc123", branch="main",
                created_at="2026-01-01T00:00:00Z", updated_at="2026-01-01T00:01:00Z",
                checkrun_total_count=2, checkrun_source="graphql", checkrun_complete=True,
            )
        )
        self.store.replace_check_runs(
            100,
            [
                CheckRunRow(database_id=5001, node_id="CR_1", check_suite_id=100, name="lint",
                            status="COMPLETED", conclusion="SUCCESS", started_at=None,
                            completed_at=None, details_url=None, app_slug="github-actions"),
                CheckRunRow(database_id=5002, node_id="CR_2", check_suite_id=100, name="test",
                            status="COMPLETED", conclusion="SUCCESS", started_at=None,
                            completed_at=None, details_url=None, app_slug="github-actions"),
            ],
        )

        self.assertEqual(self.store.count("workflow_run"), 1)
        self.assertEqual(self.store.count("check_suite"), 1)
        self.assertEqual(self.store.count("check_run"), 2)

        joined = self.store.query(
            "SELECT r.run_number, s.conclusion, count(c.database_id) AS n "
            "FROM workflow_run r JOIN check_suite s ON s.workflow_run_id = r.database_id "
            "JOIN check_run c ON c.check_suite_id = s.database_id GROUP BY r.database_id"
        )
        self.assertEqual(joined, [{"run_number": 7, "conclusion": "SUCCESS", "n": 2}])

    def test_upsert_run_is_idempotent_and_updates(self) -> None:
        self.store.upsert_run(_run(conclusion=None, hydrated=False))
        self.store.upsert_run(_run(conclusion="failure", hydrated=True))
        self.assertEqual(self.store.count("workflow_run"), 1)
        row = self.store.query("SELECT conclusion, hydrated FROM workflow_run")[0]
        self.assertEqual(row["conclusion"], "failure")
        self.assertEqual(row["hydrated"], 1)

    def test_replace_check_runs_removes_stale_rows(self) -> None:
        self.store.upsert_run(_run())
        self.store.upsert_suite(
            SuiteRow(database_id=100, node_id="CS_1", workflow_run_id=1, status=None,
                     conclusion=None, app_id=None, head_sha="abc123", branch="main",
                     created_at=None, updated_at=None, checkrun_total_count=1,
                     checkrun_source="graphql", checkrun_complete=True)
        )
        self.store.replace_check_runs(100, [
            CheckRunRow(database_id=1, node_id="a", check_suite_id=100, name="old",
                        status=None, conclusion=None, started_at=None, completed_at=None,
                        details_url=None, app_slug=None),
        ])
        self.store.replace_check_runs(100, [
            CheckRunRow(database_id=2, node_id="b", check_suite_id=100, name="new",
                        status=None, conclusion=None, started_at=None, completed_at=None,
                        details_url=None, app_slug=None),
        ])
        names = {r["name"] for r in self.store.query("SELECT name FROM check_run WHERE check_suite_id=100")}
        self.assertEqual(names, {"new"})

    def test_status_contexts_dedupe_by_context(self) -> None:
        rows = [
            StatusContextRow(commit_sha="s1", context="ci/circle", state="success",
                             description="ok", target_url="u", created_at="t"),
            StatusContextRow(commit_sha="s1", context="ci/circle", state="failure",
                             description="later", target_url="u", created_at="t2"),
        ]
        self.store.upsert_status_contexts("s1", rows)
        got = self.store.query("SELECT context, state FROM status_context WHERE commit_sha='s1'")
        self.assertEqual(len(got), 1)

    def test_records_hydration_gap(self) -> None:
        self.store.record_gap("run", "WFR_9", "node returned null")
        gaps = self.store.query("SELECT kind, ref, detail FROM hydration_gap")
        self.assertEqual(gaps, [{"kind": "run", "ref": "WFR_9", "detail": "node returned null"}])


if __name__ == "__main__":
    unittest.main()
