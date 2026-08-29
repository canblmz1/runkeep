"""Opt-in Pass C — hydrate check runs for independent third-party check suites.

These suites (Codecov, CodSpeed, Renovate, ...) have no ``workflowRun`` so they are not on
the workflow-run discovery axis. Given their GraphQL node IDs (from the Pass B check-suite
enumeration) we hydrate their check runs with the same ``nodes(ids:)`` batching + the same
``>100`` REST ``filter=all`` fallback used for Actions suites. Bounded and cheap: third-party
suites are few and most carry 0-1 check runs.
"""

from __future__ import annotations

from .http_client import GraphQLClient, RestClient
from .models import CheckRunRow, check_runs_from_rest
from .storage import Store

SUITE_NODES_QUERY = """
query($ids: [ID!]!) {
  rateLimit { cost remaining nodeCount }
  nodes(ids: $ids) {
    __typename
    ... on CheckSuite {
      id
      databaseId
      checkRuns(first: 100, filterBy: {checkType: ALL}) {
        totalCount
        pageInfo { hasNextPage endCursor }
        nodes { id databaseId name status conclusion startedAt completedAt detailsUrl }
      }
    }
  }
}
"""

MAX_IDS_PER_QUERY = 100


def _chunks(seq, size):
    for i in range(0, len(seq), size):
        yield seq[i : i + size]


def hydrate_thirdparty_suites(
    gql: GraphQLClient,
    rest: RestClient,
    owner: str,
    repo: str,
    suite_node_ids: list[str],
    store: Store,
    *,
    on_batch=None,
) -> None:
    unique = list(dict.fromkeys(suite_node_ids))
    n = (len(unique) + MAX_IDS_PER_QUERY - 1) // MAX_IDS_PER_QUERY

    for bi, batch in enumerate(_chunks(unique, MAX_IDS_PER_QUERY), start=1):
        payload = gql.query(SUITE_NODES_QUERY, {"ids": batch}, label="thirdparty")
        nodes = (payload.get("data") or {}).get("nodes") or []

        for node_id, node in zip(batch, nodes):
            if not node or node.get("__typename") != "CheckSuite":
                store.record_gap("thirdparty_suite", node_id, "node did not resolve to a CheckSuite")
                continue
            suite_db = node.get("databaseId")
            cr = node.get("checkRuns") or {}
            total = cr.get("totalCount")
            has_next = bool((cr.get("pageInfo") or {}).get("hasNextPage"))

            rows = [
                CheckRunRow(
                    database_id=x["databaseId"],
                    node_id=x.get("id"),
                    check_suite_id=suite_db,
                    name=x.get("name"),
                    status=x.get("status"),
                    conclusion=x.get("conclusion"),
                    started_at=x.get("startedAt"),
                    completed_at=x.get("completedAt"),
                    details_url=x.get("detailsUrl"),
                    app_slug=_suite_app_slug(store, suite_db),
                )
                for x in (cr.get("nodes") or [])
                if x.get("databaseId") is not None
            ]
            store.replace_check_runs(suite_db, rows)

            if has_next or (total is not None and total > len(rows)):
                items, rest_total = rest.paginate(
                    f"/repos/{owner}/{repo}/check-suites/{suite_db}/check-runs",
                    list_key="check_runs",
                    params={"per_page": 100, "filter": "all"},
                )
                rows = check_runs_from_rest(suite_db, items)
                store.replace_check_runs(suite_db, rows)
                expected = rest_total if rest_total is not None else total
                store.set_suite_checkrun_state(
                    suite_db, total_count=expected, source="thirdparty+rest_all",
                    complete=expected is not None and len(rows) == expected,
                )
            else:
                store.set_suite_checkrun_state(
                    suite_db, total_count=total, source="thirdparty_graphql",
                    complete=total is not None and len(rows) == total,
                )

        store.commit()
        if on_batch:
            on_batch(bi, n)

    store.commit()


def _suite_app_slug(store: Store, suite_db: int) -> str | None:
    return store.scalar(
        "SELECT a.slug FROM check_suite s JOIN app a ON a.database_id = s.app_id "
        "WHERE s.database_id = ?",
        (suite_db,),
    )
