"""Row types for the SQLite archive + parsers from REST / GraphQL payloads.

Core fidelity contract: Workflow Runs, Check Suites, Check Runs, Legacy Status Contexts,
their relationships, timestamps, conclusions, commit association, third-party check identity.
Logs / artifacts / steps / annotations are explicitly out of scope.
"""

from __future__ import annotations

from dataclasses import dataclass


@dataclass
class AppRow:
    database_id: int
    node_id: str | None
    slug: str | None
    name: str | None


@dataclass
class RunRow:
    database_id: int
    node_id: str
    run_number: int | None
    run_attempt: int | None
    workflow_id: int | None
    workflow_name: str | None
    event: str | None
    status: str | None
    conclusion: str | None
    head_sha: str | None
    head_branch: str | None
    display_title: str | None
    actor_login: str | None
    triggering_actor_login: str | None
    created_at: str | None
    updated_at: str | None
    run_started_at: str | None
    html_url: str | None
    check_suite_id: int | None
    hydrated: bool = False


@dataclass
class SuiteRow:
    database_id: int
    node_id: str | None
    workflow_run_id: int | None
    status: str | None
    conclusion: str | None
    app_id: int | None
    head_sha: str | None
    branch: str | None
    created_at: str | None
    updated_at: str | None
    checkrun_total_count: int | None
    checkrun_source: str
    checkrun_complete: bool


@dataclass
class CheckRunRow:
    database_id: int
    node_id: str | None
    check_suite_id: int
    name: str | None
    status: str | None
    conclusion: str | None
    started_at: str | None
    completed_at: str | None
    details_url: str | None
    app_slug: str | None


@dataclass
class StatusContextRow:
    commit_sha: str
    context: str
    state: str | None
    description: str | None
    target_url: str | None
    created_at: str | None


# --------------------------------------------------------------------------- parsers


def run_from_rest(d: dict) -> RunRow:
    actor = d.get("actor") or {}
    trig = d.get("triggering_actor") or {}
    return RunRow(
        database_id=d["id"],
        node_id=d["node_id"],
        run_number=d.get("run_number"),
        run_attempt=d.get("run_attempt"),
        workflow_id=d.get("workflow_id"),
        workflow_name=d.get("name"),
        event=d.get("event"),
        status=d.get("status"),
        conclusion=d.get("conclusion"),
        head_sha=d.get("head_sha"),
        head_branch=d.get("head_branch"),
        display_title=d.get("display_title") or d.get("name"),
        actor_login=actor.get("login"),
        triggering_actor_login=trig.get("login"),
        created_at=d.get("created_at"),
        updated_at=d.get("updated_at"),
        run_started_at=d.get("run_started_at"),
        html_url=d.get("html_url"),
        check_suite_id=d.get("check_suite_id"),
        hydrated=False,
    )


def apply_graphql_run(run: RunRow, node: dict) -> None:
    """Fold GraphQL WorkflowRun fields into a run parsed from REST; mark it hydrated."""
    run.hydrated = True
    wf = node.get("workflow") or {}
    if wf.get("name"):
        run.workflow_name = wf["name"]
    if wf.get("databaseId") is not None:
        run.workflow_id = wf["databaseId"]
    if node.get("displayTitle"):
        run.display_title = node["displayTitle"]
    if node.get("event"):
        run.event = node["event"]
    if node.get("runAttempt") is not None:
        run.run_attempt = node["runAttempt"]


def app_from_graphql(app: dict | None) -> AppRow | None:
    if not app:
        return None
    db = app.get("databaseId")
    if db is None:
        return None
    return AppRow(database_id=db, node_id=app.get("id"), slug=app.get("slug"), name=app.get("name"))


def suite_from_graphql(node: dict, run: RunRow) -> tuple[SuiteRow, AppRow | None, list[CheckRunRow]]:
    cs = node.get("checkSuite") or {}
    app = app_from_graphql(cs.get("app"))
    cr = cs.get("checkRuns") or {}
    total = cr.get("totalCount")
    has_next = bool((cr.get("pageInfo") or {}).get("hasNextPage"))
    nodes = cr.get("nodes") or []
    suite_db = cs.get("databaseId")

    check_rows = [
        CheckRunRow(
            database_id=n["databaseId"],
            node_id=n.get("id"),
            check_suite_id=suite_db,
            name=n.get("name"),
            status=n.get("status"),
            conclusion=n.get("conclusion"),
            started_at=n.get("startedAt"),
            completed_at=n.get("completedAt"),
            details_url=n.get("detailsUrl"),
            app_slug=(app.slug if app else None),
        )
        for n in nodes
        if n.get("databaseId") is not None
    ]

    complete = (total is not None) and (not has_next) and (len(check_rows) == total)
    suite = SuiteRow(
        database_id=suite_db,
        node_id=cs.get("id"),
        workflow_run_id=run.database_id,
        status=cs.get("status"),
        conclusion=cs.get("conclusion"),
        app_id=(app.database_id if app else None),
        head_sha=(cs.get("commit") or {}).get("oid") or run.head_sha,
        branch=(cs.get("branch") or {}).get("name") or run.head_branch,
        created_at=cs.get("createdAt"),
        updated_at=cs.get("updatedAt"),
        checkrun_total_count=total,
        checkrun_source="graphql",
        checkrun_complete=complete,
    )
    return suite, app, check_rows


def check_runs_from_rest(suite_db: int, rest_runs: list[dict]) -> list[CheckRunRow]:
    out = []
    for n in rest_runs:
        app = n.get("app") or {}
        out.append(
            CheckRunRow(
                database_id=n["id"],
                node_id=n.get("node_id"),
                check_suite_id=suite_db,
                name=n.get("name"),
                status=n.get("status"),
                conclusion=n.get("conclusion"),
                started_at=n.get("started_at"),
                completed_at=n.get("completed_at"),
                details_url=n.get("html_url") or n.get("details_url"),
                app_slug=app.get("slug"),
            )
        )
    return out


def thirdparty_suite_from_graphql(suite: dict, sha: str) -> tuple[SuiteRow, AppRow | None]:
    """A CheckSuite enumerated from a Commit that has no associated WorkflowRun."""
    app = app_from_graphql(suite.get("app"))
    row = SuiteRow(
        database_id=suite.get("databaseId"),
        node_id=suite.get("id"),
        workflow_run_id=None,
        status=suite.get("status"),
        conclusion=suite.get("conclusion"),
        app_id=(app.database_id if app else None),
        head_sha=sha,
        branch=None,
        created_at=suite.get("createdAt"),
        updated_at=suite.get("updatedAt"),
        checkrun_total_count=None,  # filled by the dedicated hydration pass
        checkrun_source="pending",
        checkrun_complete=False,
    )
    return row, app


def status_contexts_from_graphql(commit_node: dict | None) -> list[StatusContextRow]:
    if not commit_node:
        return []
    status = commit_node.get("status")
    if not status:
        return []
    sha = commit_node.get("oid")
    out = []
    for c in status.get("contexts") or []:
        out.append(
            StatusContextRow(
                commit_sha=sha,
                context=c.get("context"),
                state=c.get("state"),
                description=c.get("description"),
                target_url=c.get("targetUrl"),
                created_at=c.get("createdAt"),
            )
        )
    return out
