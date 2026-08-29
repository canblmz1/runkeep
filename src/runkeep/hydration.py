"""Pass A — GraphQL bulk hydration of discovered workflow runs.

``nodes(ids: [...])`` with 100 WorkflowRun global node IDs per call (GitHub's hard limit),
each resolved down to its CheckSuite + App + up to 100 CheckRuns (``filterBy: {checkType: ALL}``).

The only REST fallback here is the narrow, spec-sanctioned one: a check suite reporting
``checkRuns.pageInfo.hasNextPage`` is re-enumerated via REST ``check-runs?filter=all`` so
runs 101+ and stale rerun rows are not lost. Every run that does not come back as a
``WorkflowRun`` node is written to ``hydration_gap`` — never silently dropped.
"""

from __future__ import annotations

from .http_client import GraphQLClient, RestClient
from .models import RunRow, apply_graphql_run, check_runs_from_rest, suite_from_graphql
from .storage import Store

NODES_QUERY = """
query($ids: [ID!]!) {
  rateLimit { cost remaining nodeCount }
  nodes(ids: $ids) {
    __typename
    ... on WorkflowRun {
      id
      databaseId
      runNumber
      runAttempt
      event
      url
      createdAt
      updatedAt
      displayTitle
      workflow { name databaseId }
      checkSuite {
        id
        databaseId
        status
        conclusion
        createdAt
        updatedAt
        branch { name }
        commit { oid }
        app { id slug name databaseId }
        checkRuns(first: 100, filterBy: {checkType: ALL}) {
          totalCount
          pageInfo { hasNextPage endCursor }
          nodes {
            id
            databaseId
            name
            status
            conclusion
            startedAt
            completedAt
            detailsUrl
          }
        }
      }
    }
  }
}
"""

MAX_IDS_PER_QUERY = 100


def _chunks(seq, size):
    for i in range(0, len(seq), size):
        yield seq[i : i + size]


def hydrate_runs(
    gql: GraphQLClient,
    rest: RestClient,
    owner: str,
    repo: str,
    runs: list[RunRow],
    store: Store,
    *,
    batch_size: int = MAX_IDS_PER_QUERY,
    on_batch=None,
) -> None:
    by_node = {r.node_id: r for r in runs}
    batch_size = min(batch_size, MAX_IDS_PER_QUERY)

    for bi, batch in enumerate(_chunks([r.node_id for r in runs], batch_size), start=1):
        payload = gql.query(NODES_QUERY, {"ids": batch}, label="hydrate")
        nodes = (payload.get("data") or {}).get("nodes") or []

        # GitHub returns nodes positionally aligned with the ids sent (null for unresolvable).
        # If the response is short (partial data), the tail ids get an explicit gap, not silence.
        for node_id in batch[len(nodes):]:
            store.record_gap("run", node_id, "GraphQL response had no node slot for this id")
            store.upsert_run(by_node[node_id])

        for node_id, node in zip(batch, nodes):
            run = by_node[node_id]

            if not node or node.get("__typename") != "WorkflowRun":
                store.record_gap("run", node_id, "GraphQL returned no WorkflowRun node")
                store.upsert_run(run)  # keep the REST-discovered row, unhydrated
                continue

            apply_graphql_run(run, node)
            store.upsert_run(run)

            suite, app, check_rows = suite_from_graphql(node, run)
            if suite.database_id is None:
                store.record_gap("suite", node_id, "WorkflowRun had no checkSuite.databaseId")
                continue

            if app:
                store.upsert_app(app)
            store.upsert_suite(suite)
            store.replace_check_runs(suite.database_id, check_rows)

            cs = node.get("checkSuite") or {}
            cr = cs.get("checkRuns") or {}
            has_next = bool((cr.get("pageInfo") or {}).get("hasNextPage"))
            gql_total = cr.get("totalCount")
            # Belt and suspenders: also fall back if the count says there are more than the
            # page returned, even if hasNextPage was (wrongly) false.
            undercount = gql_total is not None and gql_total > len(check_rows)

            if has_next or undercount:
                _rest_fill_check_runs(rest, owner, repo, suite.database_id, store, gql_total)
            else:
                stored = len(check_rows)
                complete = gql_total is not None and stored == gql_total
                store.set_suite_checkrun_state(
                    suite.database_id,
                    total_count=gql_total,
                    source="graphql",
                    complete=complete,
                )

        store.commit()
        if on_batch:
            on_batch(bi)

    store.commit()


def _rest_fill_check_runs(
    rest: RestClient,
    owner: str,
    repo: str,
    suite_db: int,
    store: Store,
    gql_total: int | None,
) -> None:
    """Narrow, sanctioned fallback: fully enumerate a >100-check suite via REST filter=all."""
    items, total = rest.paginate(
        f"/repos/{owner}/{repo}/check-suites/{suite_db}/check-runs",
        list_key="check_runs",
        params={"per_page": 100, "filter": "all"},
    )
    rows = check_runs_from_rest(suite_db, items)
    store.replace_check_runs(suite_db, rows)

    expected = total if total is not None else gql_total
    complete = expected is not None and len(rows) == expected
    if expected is None:
        store.record_gap(
            "suite_checkruns", str(suite_db),
            "neither GraphQL totalCount nor REST total_count available",
        )
    store.set_suite_checkrun_state(
        suite_db, total_count=expected, source="graphql+rest_all", complete=complete
    )
