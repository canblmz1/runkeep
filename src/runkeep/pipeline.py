"""Orchestration: discover -> hydrate (Pass A) -> statuses (Pass B) -> third-party -> metrics.

Every stage is resumable. Discovery persists each fully-collected time slice; hydration reads
``workflow_run WHERE hydrated = 0``; the status pass reads SHAs not yet in
``commit_status_probe``; third-party hydration reads suites still marked ``pending``. Re-running
``rescue`` against an existing ``--db`` (same repo, same range) picks up exactly where it
stopped — no work is redone.
"""

from __future__ import annotations

import os
import sys
import time
from dataclasses import dataclass
from datetime import datetime, timezone

from .discovery import DiscoveryStats, discover_range
from .http_client import FetchFn, GraphQLClient, Meter, RestClient
from .hydration import hydrate_runs
from .metrics import Completeness, compute_completeness
from .models import run_from_rest
from .statuses import collect_statuses
from .storage import Store
from .thirdparty import hydrate_thirdparty_suites


def _log(msg: str) -> None:
    if os.environ.get("RUNKEEP_QUIET"):
        return
    print(f"[runkeep] {time.strftime('%H:%M:%S')} {msg}", file=sys.stderr, flush=True)


@dataclass
class RescueResult:
    owner: str
    repo: str
    completeness: Completeness
    meter: Meter
    elapsed_s: float
    db_bytes: int
    discovered: int
    resumed: bool
    discovery: DiscoveryStats
    store: Store


def run_rescue(
    owner: str,
    repo: str,
    *,
    db_path: str,
    token: str,
    limit: int | None = None,
    since: str | None = None,
    until: str | None = None,
    cap: int = 1000,
    hydrate_batch: int = 100,
    status_batch: int = 60,
    with_thirdparty: bool = True,
    notify=None,
    fetch_rest: FetchFn | None = None,
    fetch_gql: FetchFn | None = None,
) -> RescueResult:
    meter = Meter()
    rest = RestClient(token, fetch=fetch_rest, meter=meter, notify=notify)
    gql = GraphQLClient(token, fetch=fetch_gql, meter=meter, notify=notify)

    resuming = db_path != ":memory:" and os.path.exists(db_path)
    store = Store(db_path)
    store.init_schema()
    try:
        return _rescue(
            store, rest, gql, owner, repo, meter, resuming,
            db_path=db_path, limit=limit, since=since, until=until, cap=cap,
            hydrate_batch=hydrate_batch, status_batch=status_batch,
            with_thirdparty=with_thirdparty,
        )
    except BaseException:
        # An interrupt or crash: flush what we have and release the file so `rescue` can resume.
        try:
            store.commit()
        finally:
            store.close()
        raise


def _rescue(
    store: "Store",
    rest: "RestClient",
    gql: "GraphQLClient",
    owner: str,
    repo: str,
    meter: "Meter",
    resuming: bool,
    *,
    db_path: str,
    limit: int | None,
    since: str | None,
    until: str | None,
    cap: int,
    hydrate_batch: int,
    status_batch: int,
    with_thirdparty: bool,
) -> "RescueResult":
    if resuming:
        _log(
            f"resuming from {db_path}: {store.count('workflow_run')} runs, "
            f"{store.scalar('SELECT count(*) FROM workflow_run WHERE hydrated=1') or 0} hydrated, "
            f"{store.count('discovery_slice')} slices done"
        )

    t0 = time.monotonic()

    # ---- discovery -------------------------------------------------------
    # Freeze the [since, until] range on the first run and reuse it verbatim on every resume,
    # so the deterministic slice tree lines up and completed slices actually match.
    since = store.get_meta("range_since") or since
    until = store.get_meta("range_until") or until
    if not since:
        repo_json, _ = rest.get_json(f"/repos/{owner}/{repo}")  # 404 -> RepoNotFound
        since = repo_json.get("created_at") or "2018-01-01T00:00:00Z"
    if not until:
        until = datetime.now(timezone.utc).replace(microsecond=0).strftime("%Y-%m-%dT%H:%M:%SZ")
    store.set_meta("range_since", since)
    store.set_meta("range_until", until)
    store.commit()
    _log(f"discovery: {owner}/{repo} since={since} until={until} limit={limit or 'none'}")

    def _sink(start_iso: str, end_iso: str, batch: list[dict]) -> None:
        for raw in batch:
            store.upsert_run(run_from_rest(raw))
        store.record_discovery_slice(start_iso, end_iso, len(batch))
        store.commit()

    dstats = discover_range(
        rest, owner, repo,
        since=since, until=until, cap=cap, limit=limit,
        skip_slices=store.completed_slices(),
        sink=_sink,
        on_progress=lambda n: _log(f"discovery: +{n} runs, {rest.meter.rest_calls} REST calls"),
    )
    _log(
        f"discovery done: {dstats.slices_queried} slices queried, {dstats.slices_split} split, "
        f"{dstats.slices_skipped} skipped (resume), depth {dstats.max_depth}, "
        f"{dstats.duplicates_removed} dupes"
    )

    # ---- Pass A: hydrate runs (only the un-hydrated ones) ---------------
    pending_runs = store.runs_needing_hydration()
    n_batches = max(1, (len(pending_runs) + hydrate_batch - 1) // hydrate_batch)
    _log(f"hydrate: {len(pending_runs)} runs need hydration ({n_batches} batches)")
    hydrate_runs(
        gql, rest, owner, repo, pending_runs, store, batch_size=hydrate_batch,
        on_batch=lambda i: _log(
            f"hydrate batch {i}/{n_batches}  gql={gql.meter.graphql_calls} "
            f"cost={gql.meter.graphql_cost_total}"
        ),
    )

    # ---- Pass B: legacy statuses + (optional) check-suite enumeration --
    shas = store.shas_needing_status()
    _log(f"statuses: {len(shas)} commits to probe")
    collect_statuses(
        gql, owner, repo, shas, store,
        batch_size=status_batch,
        include_check_suites=with_thirdparty,
        on_batch=lambda i, m: _log(f"status batch {i}/{m}"),
    )

    # ---- Pass C: third-party suite check runs --------------------------
    if with_thirdparty:
        tp_ids = store.pending_thirdparty_suite_ids()
        if tp_ids:
            _log(f"third-party: hydrating {len(tp_ids)} independent check suites")
            hydrate_thirdparty_suites(
                gql, rest, owner, repo, tp_ids, store,
                on_batch=lambda i, m: _log(f"third-party batch {i}/{m}"),
            )

    elapsed = time.monotonic() - t0
    _persist_meta(store, meter, elapsed, owner, repo)
    if db_path != ":memory:":
        store.finalize()
    else:
        store.commit()

    db_bytes = 0 if db_path == ":memory:" else os.path.getsize(db_path)
    return RescueResult(
        owner=owner,
        repo=repo,
        completeness=compute_completeness(store),
        meter=meter,
        elapsed_s=elapsed,
        db_bytes=db_bytes,
        discovered=store.count("workflow_run"),
        resumed=resuming,
        discovery=dstats,
        store=store,
    )


def _persist_meta(store: Store, meter: Meter, elapsed: float, owner: str, repo: str) -> None:
    store.set_meta("repo", f"{owner}/{repo}")
    store.set_meta("runkeep_version", _version())
    for key in ("rest_calls", "graphql_calls", "rest_bytes", "graphql_bytes",
                "graphql_cost_total", "rate_limit_waits"):
        store.set_meta(key, getattr(meter, key))
    store.set_meta("elapsed_s_last", round(elapsed, 2))


def _version() -> str:
    from . import __version__

    return __version__
