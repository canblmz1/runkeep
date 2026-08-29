"""Pass B — commit-axis capture, split into two independent passes.

* ``collect_statuses``  — legacy commit status contexts. A tiny GraphQL query
  (``Commit.status.contexts``) that never times out. Part of the **core** archive: every
  probed commit lands in ``commit_status_probe`` so "0 legacy statuses" is measured, not
  assumed. One row per (commit, context) — the current archived representation, not a
  transition log.

* ``enumerate_check_suites`` — independent third-party check suites (``Commit.checkSuites``
  with no ``workflowRun``). This connection **times out (502/503/504) on very large repos**,
  so it runs through :func:`runkeep.adaptive.split_retry`: a failing commit batch is halved
  and retried down to a single commit; a singleton that still fails first tries a bounded
  REST fallback (``GET /commits/{sha}/check-suites``), and only then records a
  ``thirdparty_enum`` gap. Successful commits go in ``thirdparty_probe`` so resume skips them.
  This is **optional** — its failure never touches ``core_complete``.
"""

from __future__ import annotations

from .adaptive import chunks, split_retry
from .errors import GitHubServerError, NetworkError
from .http_client import GraphQLClient, RestClient
from .models import (
    status_contexts_from_graphql,
    thirdparty_suite_from_graphql,
    thirdparty_suite_from_rest,
)
from .storage import Store

_GATEWAY = (GitHubServerError, NetworkError)

_STATUS_FRAGMENT = (
    '{alias}: object(oid: "{oid}") {{ __typename ... on Commit {{ oid '
    "status {{ state contexts {{ context state description targetUrl createdAt }} }} }} }}"
)
_SUITES_FRAGMENT = (
    '{alias}: object(oid: "{oid}") {{ __typename ... on Commit {{ oid '
    "checkSuites(first: 25) {{ nodes {{ id databaseId status conclusion createdAt updatedAt "
    "app {{ id slug name databaseId }} workflowRun {{ databaseId }} }} }} }} }}"
)

STATUS_BATCH = 100
SUITES_BATCH = 50


def _query(frag: str, shas: list[str]) -> tuple[str, dict[str, str]]:
    alias_to_sha = {f"c{i}": s for i, s in enumerate(shas)}
    body = "\n    ".join(frag.format(alias=a, oid=s) for a, s in alias_to_sha.items())
    doc = (
        "query($owner: String!, $name: String!) {\n"
        "  rateLimit { cost remaining nodeCount }\n"
        f"  repository(owner: $owner, name: $name) {{\n    {body}\n  }}\n}}"
    )
    return doc, alias_to_sha


def collect_statuses(
    gql: GraphQLClient, owner: str, repo: str, shas: list[str], store: Store,
    *, batch_size: int = STATUS_BATCH, on_batch=None,
) -> None:
    unique = sorted({s for s in shas if s})
    n = max(1, (len(unique) + batch_size - 1) // batch_size)
    for bi, batch in enumerate(chunks(unique, batch_size), start=1):
        doc, alias_to_sha = _query(_STATUS_FRAGMENT, batch)
        payload = gql.query(doc, {"owner": owner, "name": repo}, label="status")
        repo_obj = (payload.get("data") or {}).get("repository") or {}
        for alias, sha in alias_to_sha.items():
            node = repo_obj.get(alias)
            if node is None:
                store.record_gap("commit_status", sha, "commit oid not resolved")
                store.record_status_probe(sha, has_status=False, context_count=0)
                continue
            contexts = status_contexts_from_graphql(node)
            store.upsert_status_contexts(sha, contexts)
            store.record_status_probe(
                sha, has_status=node.get("status") is not None, context_count=len(contexts)
            )
        store.commit()
        if on_batch:
            on_batch(bi, n)
    store.commit()


def enumerate_check_suites(
    gql: GraphQLClient, rest: RestClient, owner: str, repo: str, shas: list[str], store: Store,
    *, batch_size: int = SUITES_BATCH, notify=None, on_progress=None,
):
    """Adaptive: returns the split_retry SplitStats. Records gaps, never raises on 5xx."""
    todo = [s for s in sorted({s for s in shas if s}) if s not in store.thirdparty_probed()]
    done = [0]

    def run_batch(batch: list[str]) -> None:
        doc, alias_to_sha = _query(_SUITES_FRAGMENT, batch)
        payload = gql.query(doc, {"owner": owner, "name": repo}, label="thirdparty-enum")
        repo_obj = (payload.get("data") or {}).get("repository") or {}
        for alias, sha in alias_to_sha.items():
            node = repo_obj.get(alias)
            n_suites = _store_suites_from_graphql(node, sha, store) if node else 0
            store.record_thirdparty_probe(sha, n_suites)
        store.commit()
        done[0] += len(batch)
        if on_progress:
            on_progress(done[0], len(todo))

    def on_singleton_fail(sha: str) -> None:
        try:
            n = _rest_fallback_enumerate(rest, owner, repo, sha, store)
            store.record_thirdparty_probe(sha, n)
        except _GATEWAY + (Exception,) as exc:  # noqa: BLE001
            store.record_gap("thirdparty_enum", sha, f"graphql + rest both failed: {exc}")
        store.commit()

    return split_retry(
        todo, batch_size, run_batch, on_singleton_fail,
        gateway_errors=_GATEWAY, notify=notify,
    )


def _store_suites_from_graphql(commit_node: dict, sha: str, store: Store) -> int:
    n = 0
    for suite in (commit_node.get("checkSuites") or {}).get("nodes") or []:
        if suite.get("databaseId") is None or suite.get("workflowRun"):
            continue  # owned by Pass A
        row, app = thirdparty_suite_from_graphql(suite, sha)
        if app:
            store.upsert_app(app)
        if not store.scalar("SELECT 1 FROM check_suite WHERE database_id = ?", (row.database_id,)):
            store.upsert_suite(row)
        n += 1
    return n


def _rest_fallback_enumerate(
    rest: RestClient, owner: str, repo: str, sha: str, store: Store
) -> int:
    """One bounded REST call for a commit GraphQL couldn't handle."""
    items, _ = rest.paginate(
        f"/repos/{owner}/{repo}/commits/{sha}/check-suites",
        list_key="check_suites", params={"per_page": 100},
    )
    ours = {
        r[0] for r in store.conn.execute(
            "SELECT check_suite_id FROM workflow_run WHERE check_suite_id IS NOT NULL"
        )
    }
    n = 0
    for suite in items:
        if suite.get("id") in ours:
            continue  # tied to a workflow run we already have
        row, app = thirdparty_suite_from_rest(suite, sha)
        if row.database_id is None:
            continue
        if app:
            store.upsert_app(app)
        if not store.scalar("SELECT 1 FROM check_suite WHERE database_id = ?", (row.database_id,)):
            store.upsert_suite(row)
        n += 1
    return n
