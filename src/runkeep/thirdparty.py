"""Optional Pass C — hydrate check runs for independent third-party check suites.

These suites (Codecov, CodSpeed, Renovate, ...) have no ``workflowRun``, so they are not on
the workflow-run discovery axis. Given their node IDs (from the Pass B enumeration) we hydrate
check runs with the same ``nodes(ids:)`` batching + ``>100`` REST ``filter=all`` fallback used
for Actions suites — but adaptively: a batch that hits a gateway timeout is halved and retried
down to one suite; a singleton that still fails records a ``thirdparty_suite`` gap instead of
crashing. A suite whose check runs are stored flips ``checkrun_source`` off ``pending``, so
resume only revisits the unfinished ones.
"""

from __future__ import annotations

from .adaptive import split_retry
from .errors import GitHubServerError, NetworkError
from .http_client import GraphQLClient, RestClient
from .models import CheckRunRow, check_runs_from_rest
from .storage import Store

_GATEWAY = (GitHubServerError, NetworkError)
MAX_IDS_PER_QUERY = 100

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


def hydrate_thirdparty_suites(
    gql: GraphQLClient, rest: RestClient, owner: str, repo: str,
    suite_node_ids: list[str], store: Store, *, notify=None, on_progress=None,
):
    todo = list(dict.fromkeys(suite_node_ids))
    done = [0]

    def run_batch(batch: list[str]) -> None:
        payload = gql.query(SUITE_NODES_QUERY, {"ids": batch}, label="thirdparty")
        nodes = (payload.get("data") or {}).get("nodes") or []
        by_id = {n.get("id"): n for n in nodes if n}
        for node_id in batch:
            _store_one(rest, owner, repo, node_id, by_id.get(node_id), store)
        store.commit()
        done[0] += len(batch)
        if on_progress:
            on_progress(done[0], len(todo))

    def on_singleton_fail(node_id: str) -> None:
        store.record_gap("thirdparty_suite", node_id, "GraphQL hydration failed after retries")
        store.commit()

    return split_retry(
        todo, min(MAX_IDS_PER_QUERY, 100), run_batch, on_singleton_fail,
        gateway_errors=_GATEWAY, notify=notify,
    )


def _store_one(rest, owner, repo, node_id, node, store: Store) -> None:
    if not node or node.get("__typename") != "CheckSuite":
        store.record_gap("thirdparty_suite", node_id, "did not resolve to a CheckSuite")
        return
    suite_db = node.get("databaseId")
    cr = node.get("checkRuns") or {}
    total = cr.get("totalCount")
    has_next = bool((cr.get("pageInfo") or {}).get("hasNextPage"))
    app_slug = _suite_app_slug(store, suite_db)

    rows = [
        CheckRunRow(
            database_id=x["databaseId"], node_id=x.get("id"), check_suite_id=suite_db,
            name=x.get("name"), status=x.get("status"), conclusion=x.get("conclusion"),
            started_at=x.get("startedAt"), completed_at=x.get("completedAt"),
            details_url=x.get("detailsUrl"), app_slug=app_slug,
        )
        for x in (cr.get("nodes") or [])
        if x.get("databaseId") is not None
    ]
    store.replace_check_runs(suite_db, rows)

    if has_next or (total is not None and total > len(rows)):
        items, rest_total = rest.paginate(
            f"/repos/{owner}/{repo}/check-suites/{suite_db}/check-runs",
            list_key="check_runs", params={"per_page": 100, "filter": "all"},
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
    # clear any prior gap for this suite now that it's hydrated
    store.conn.execute(
        "DELETE FROM hydration_gap WHERE kind='thirdparty_suite' AND ref=?", (node_id,)
    )


def _suite_app_slug(store: Store, suite_db: int) -> str | None:
    return store.scalar(
        "SELECT a.slug FROM check_suite s JOIN app a ON a.database_id = s.app_id "
        "WHERE s.database_id = ?",
        (suite_db,),
    )
