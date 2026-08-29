"""Pass B — legacy commit status contexts (and, optionally, check-suite enumeration).

Unique commit SHAs from discovery, deduplicated, hydrated in batches via GraphQL
``repository { cN: object(oid:) { ... on Commit { status { contexts } } } }``. No per-run REST.

We preserve the *current archived status representation* — one row per (commit, context) —
not every historical status transition event. Every probed commit is recorded in
``commit_status_probe`` so "0 legacy statuses" is a measured fact, never an assumed default.

When ``include_check_suites`` is set, the same query also pulls ``Commit.checkSuites`` so that
independent third-party suites (no ``workflowRun``) are identified and stored. Their check runs
are hydrated separately (see :mod:`runkeep.thirdparty`). This costs ~0 extra API calls (it
rides the status batches) and a few GraphQL points.
"""

from __future__ import annotations

from .http_client import GraphQLClient
from .models import status_contexts_from_graphql, thirdparty_suite_from_graphql
from .storage import Store

_COMMIT_FRAGMENT = (
    '{alias}: object(oid: "{oid}") {{ __typename ... on Commit {{ oid '
    "status {{ state contexts {{ context state description targetUrl createdAt }} }} }} }}"
)

_COMMIT_FRAGMENT_WITH_SUITES = (
    '{alias}: object(oid: "{oid}") {{ __typename ... on Commit {{ oid '
    "status {{ state contexts {{ context state description targetUrl createdAt }} }} "
    "checkSuites(first: 20) {{ totalCount nodes {{ id databaseId status conclusion "
    "createdAt updatedAt app {{ id slug name databaseId }} workflowRun {{ databaseId }} }} }} }} }}"
)

DEFAULT_BATCH = 60  # smaller batches: the checkSuites enumeration 504s on very large repos


def _chunks(seq, size):
    for i in range(0, len(seq), size):
        yield seq[i : i + size]


def _build_query(shas: list[str], with_suites: bool) -> tuple[str, dict[str, str]]:
    frag = _COMMIT_FRAGMENT_WITH_SUITES if with_suites else _COMMIT_FRAGMENT
    alias_to_sha = {f"c{i}": sha for i, sha in enumerate(shas)}
    body = "\n    ".join(frag.format(alias=a, oid=s) for a, s in alias_to_sha.items())
    doc = (
        "query($owner: String!, $name: String!) {\n"
        "  rateLimit { cost remaining nodeCount }\n"
        "  repository(owner: $owner, name: $name) {\n"
        f"    {body}\n"
        "  }\n"
        "}"
    )
    return doc, alias_to_sha


def collect_statuses(
    gql: GraphQLClient,
    owner: str,
    repo: str,
    shas: list[str],
    store: Store,
    *,
    batch_size: int = DEFAULT_BATCH,
    include_check_suites: bool = False,
    on_batch=None,
) -> list[str]:
    """Returns the node IDs of independent third-party check suites to hydrate later."""
    unique = sorted({s for s in shas if s})
    n_batches = (len(unique) + batch_size - 1) // max(batch_size, 1)
    thirdparty_suite_ids: list[str] = []

    for bi, batch in enumerate(_chunks(unique, batch_size), start=1):
        doc, alias_to_sha = _build_query(batch, include_check_suites)
        payload = gql.query(doc, {"owner": owner, "name": repo}, label="status")
        repo_obj = (payload.get("data") or {}).get("repository") or {}

        for alias, sha in alias_to_sha.items():
            node = repo_obj.get(alias)
            if node is None:
                store.record_gap("commit_status", sha, "commit oid not resolved by GraphQL")
                store.record_status_probe(sha, has_status=False, context_count=0)
                continue

            contexts = status_contexts_from_graphql(node)
            store.upsert_status_contexts(sha, contexts)
            store.record_status_probe(
                sha, has_status=node.get("status") is not None, context_count=len(contexts)
            )

            if include_check_suites:
                thirdparty_suite_ids += _store_check_suites(node, sha, store)

        store.commit()
        if on_batch:
            on_batch(bi, n_batches)

    store.commit()
    return thirdparty_suite_ids


def _store_check_suites(commit_node: dict, sha: str, store: Store) -> list[str]:
    ids: list[str] = []
    conn = commit_node.get("checkSuites") or {}
    for suite in conn.get("nodes") or []:
        if suite.get("databaseId") is None:
            continue
        # suites tied to a workflow run are owned by Pass A; don't shadow them here
        if suite.get("workflowRun"):
            continue
        row, app = thirdparty_suite_from_graphql(suite, sha)
        if app:
            store.upsert_app(app)
        # only create the row if Pass A hasn't already stored this suite id
        existing = store.scalar(
            "SELECT 1 FROM check_suite WHERE database_id = ?", (row.database_id,)
        )
        if not existing:
            store.upsert_suite(row)
        if suite.get("id"):
            ids.append(suite["id"])
    return ids
