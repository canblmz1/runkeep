"""SQLite archive for the rescued CI history.

One connection, WAL mode, explicit upserts, insert in dependency order (app -> run -> suite ->
check run). ``replace_check_runs`` deletes a suite's rows before reinserting so a rerun /
filter=all reconciliation never leaves stale rows. Foreign keys are enforced.

Every write is an idempotent upsert and progress lives in the file itself (``discovery_slice``
rows, the ``workflow_run.hydrated`` flag, ``commit_status_probe``, ``check_suite.checkrun_source``),
so re-running ``rescue`` against an existing ``--db`` resumes instead of redoing work.
"""

from __future__ import annotations

import sqlite3
from dataclasses import astuple, fields

from .models import AppRow, CheckRunRow, RunRow, StatusContextRow, SuiteRow

_SCHEMA = """
CREATE TABLE IF NOT EXISTS app (
    database_id INTEGER PRIMARY KEY,
    node_id     TEXT,
    slug        TEXT,
    name        TEXT
);
CREATE TABLE IF NOT EXISTS workflow_run (
    database_id    INTEGER PRIMARY KEY,
    node_id        TEXT UNIQUE NOT NULL,
    run_number     INTEGER,
    run_attempt    INTEGER,
    workflow_id    INTEGER,
    workflow_name  TEXT,
    event          TEXT,
    status         TEXT,
    conclusion     TEXT,
    head_sha       TEXT,
    head_branch    TEXT,
    display_title  TEXT,
    actor_login    TEXT,
    triggering_actor_login TEXT,
    created_at     TEXT,
    updated_at     TEXT,
    run_started_at TEXT,
    html_url       TEXT,
    check_suite_id INTEGER,
    hydrated       INTEGER NOT NULL DEFAULT 0
);
CREATE TABLE IF NOT EXISTS check_suite (
    database_id     INTEGER PRIMARY KEY,
    node_id         TEXT,
    workflow_run_id INTEGER REFERENCES workflow_run(database_id),
    status          TEXT,
    conclusion      TEXT,
    app_id          INTEGER REFERENCES app(database_id),
    head_sha        TEXT,
    branch          TEXT,
    created_at      TEXT,
    updated_at      TEXT,
    checkrun_total_count INTEGER,
    checkrun_source      TEXT,
    checkrun_complete    INTEGER NOT NULL DEFAULT 0
);
CREATE TABLE IF NOT EXISTS check_run (
    database_id    INTEGER PRIMARY KEY,
    node_id        TEXT,
    check_suite_id INTEGER NOT NULL REFERENCES check_suite(database_id),
    name           TEXT,
    status         TEXT,
    conclusion     TEXT,
    started_at     TEXT,
    completed_at   TEXT,
    details_url    TEXT,
    app_slug       TEXT
);
CREATE TABLE IF NOT EXISTS status_context (
    commit_sha  TEXT NOT NULL,
    context     TEXT NOT NULL,
    state       TEXT,
    description TEXT,
    target_url  TEXT,
    created_at  TEXT,
    PRIMARY KEY (commit_sha, context)
);
CREATE TABLE IF NOT EXISTS commit_status_probe (
    commit_sha    TEXT PRIMARY KEY,
    has_status    INTEGER NOT NULL,
    context_count INTEGER NOT NULL
);
CREATE TABLE IF NOT EXISTS hydration_gap (
    kind   TEXT NOT NULL,
    ref    TEXT NOT NULL,
    detail TEXT
);
CREATE TABLE IF NOT EXISTS discovery_slice (
    start_iso    TEXT NOT NULL,
    end_iso      TEXT NOT NULL,
    run_count    INTEGER NOT NULL,
    completed_at TEXT NOT NULL DEFAULT (datetime('now')),
    PRIMARY KEY (start_iso, end_iso)
);
CREATE TABLE IF NOT EXISTS archive_meta (
    key   TEXT PRIMARY KEY,
    value TEXT
);
CREATE INDEX IF NOT EXISTS ix_check_run_suite ON check_run(check_suite_id);
CREATE INDEX IF NOT EXISTS ix_suite_run ON check_suite(workflow_run_id);
CREATE INDEX IF NOT EXISTS ix_run_sha ON workflow_run(head_sha);
CREATE INDEX IF NOT EXISTS ix_run_hydrated ON workflow_run(hydrated);
CREATE INDEX IF NOT EXISTS ix_suite_source ON check_suite(checkrun_source);
"""


def _cols(row_type) -> list[str]:
    return [f.name for f in fields(row_type)]


def _upsert_sql(table: str, row_type, pk: str | tuple[str, ...]) -> str:
    cols = _cols(row_type)
    pks = (pk,) if isinstance(pk, str) else pk
    updates = ", ".join(f"{c}=excluded.{c}" for c in cols if c not in pks)
    return (
        f"INSERT INTO {table} ({', '.join(cols)}) VALUES ({', '.join('?' for _ in cols)}) "
        f"ON CONFLICT({', '.join(pks)}) DO UPDATE SET {updates}"
    )


class Store:
    def __init__(self, path: str) -> None:
        self.path = path
        self.conn = sqlite3.connect(path)
        self.conn.row_factory = sqlite3.Row
        self.conn.execute("PRAGMA foreign_keys = ON")
        if path != ":memory:":
            self.conn.execute("PRAGMA journal_mode = WAL")
            self.conn.execute("PRAGMA synchronous = NORMAL")

    # ---------------------------------------------------------------- lifecycle
    def init_schema(self) -> None:
        self.conn.executescript(_SCHEMA)
        self.conn.commit()

    def close(self) -> None:
        self.conn.close()

    def __enter__(self) -> "Store":
        return self

    def __exit__(self, *exc) -> None:
        self.close()

    # ---------------------------------------------------------------- writers
    def upsert_app(self, app: AppRow) -> None:
        self.conn.execute(_upsert_sql("app", AppRow, "database_id"), _row_values(app))

    def upsert_run(self, run: RunRow) -> None:
        self.conn.execute(_upsert_sql("workflow_run", RunRow, "database_id"), _row_values(run))

    def upsert_suite(self, suite: SuiteRow) -> None:
        self.conn.execute(_upsert_sql("check_suite", SuiteRow, "database_id"), _row_values(suite))

    def replace_check_runs(self, suite_db: int, rows: list[CheckRunRow]) -> None:
        self.conn.execute("DELETE FROM check_run WHERE check_suite_id = ?", (suite_db,))
        if rows:
            self.conn.executemany(
                _upsert_sql("check_run", CheckRunRow, "database_id"),
                [_row_values(r) for r in rows],
            )

    def set_suite_checkrun_state(
        self, suite_db: int, *, total_count: int | None, source: str, complete: bool
    ) -> None:
        self.conn.execute(
            "UPDATE check_suite SET checkrun_total_count=?, checkrun_source=?, checkrun_complete=? "
            "WHERE database_id=?",
            (total_count, source, int(complete), suite_db),
        )

    def upsert_status_contexts(self, sha: str, rows: list[StatusContextRow]) -> None:
        for r in rows:
            self.conn.execute(
                _upsert_sql("status_context", StatusContextRow, ("commit_sha", "context")),
                _row_values(r),
            )

    def record_status_probe(self, sha: str, has_status: bool, context_count: int) -> None:
        self.conn.execute(
            "INSERT INTO commit_status_probe (commit_sha, has_status, context_count) VALUES (?, ?, ?) "
            "ON CONFLICT(commit_sha) DO UPDATE SET has_status=excluded.has_status, "
            "context_count=excluded.context_count",
            (sha, int(has_status), context_count),
        )

    def record_gap(self, kind: str, ref: str, detail: str = "") -> None:
        self.conn.execute(
            "INSERT INTO hydration_gap (kind, ref, detail) VALUES (?, ?, ?)", (kind, ref, detail)
        )

    def record_discovery_slice(self, start_iso: str, end_iso: str, run_count: int) -> None:
        self.conn.execute(
            "INSERT INTO discovery_slice (start_iso, end_iso, run_count) VALUES (?, ?, ?) "
            "ON CONFLICT(start_iso, end_iso) DO UPDATE SET run_count=excluded.run_count",
            (start_iso, end_iso, run_count),
        )

    def set_meta(self, key: str, value) -> None:
        self.conn.execute(
            "INSERT INTO archive_meta (key, value) VALUES (?, ?) "
            "ON CONFLICT(key) DO UPDATE SET value=excluded.value",
            (key, str(value)),
        )

    def commit(self) -> None:
        self.conn.commit()

    def finalize(self) -> None:
        """Compact the file — many small writes leave SQLite over-allocated."""
        self.conn.commit()
        self.conn.execute("PRAGMA optimize")
        self.conn.execute("PRAGMA wal_checkpoint(TRUNCATE)")
        self.conn.execute("VACUUM")

    # ---------------------------------------------------------------- readers
    def count(self, table: str) -> int:
        return self.conn.execute(f"SELECT count(*) FROM {table}").fetchone()[0]

    def get_meta(self, key: str) -> str | None:
        row = self.conn.execute("SELECT value FROM archive_meta WHERE key=?", (key,)).fetchone()
        return row[0] if row else None

    # ---- resume: what still needs doing --------------------------------------
    def completed_slices(self) -> set[tuple[str, str]]:
        return {
            (r["start_iso"], r["end_iso"])
            for r in self.conn.execute("SELECT start_iso, end_iso FROM discovery_slice")
        }

    def runs_needing_hydration(self) -> list[RunRow]:
        rows = self.conn.execute(
            "SELECT * FROM workflow_run WHERE hydrated = 0 ORDER BY created_at DESC"
        ).fetchall()
        return [row_to_runrow(r) for r in rows]

    def shas_needing_status(self) -> list[str]:
        return [
            r[0]
            for r in self.conn.execute(
                "SELECT DISTINCT head_sha FROM workflow_run "
                "WHERE head_sha IS NOT NULL "
                "AND head_sha NOT IN (SELECT commit_sha FROM commit_status_probe)"
            )
        ]

    def pending_thirdparty_suite_ids(self) -> list[str]:
        return [
            r[0]
            for r in self.conn.execute(
                "SELECT node_id FROM check_suite "
                "WHERE workflow_run_id IS NULL AND node_id IS NOT NULL "
                "AND checkrun_source = 'pending'"
            )
        ]

    def query(self, sql: str, params: tuple = ()) -> list[dict]:
        return [dict(r) for r in self.conn.execute(sql, params).fetchall()]

    def scalar(self, sql: str, params: tuple = ()):
        row = self.conn.execute(sql, params).fetchone()
        return row[0] if row else None


def _row_values(row) -> tuple:
    vals = list(astuple(row))
    return tuple(int(v) if isinstance(v, bool) else v for v in vals)


def row_to_runrow(r) -> RunRow:
    names = {f.name for f in fields(RunRow)}
    data = {k: r[k] for k in r.keys() if k in names}
    data["hydrated"] = bool(data.get("hydrated"))
    return RunRow(**data)
