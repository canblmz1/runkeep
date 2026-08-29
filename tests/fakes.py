"""In-memory GitHub fakes for the spike's unit tests.

``FakeGitHub`` serves the exact REST + GraphQL shapes the pipeline consumes, driven by a
small declarative fixture (runs, check runs per suite, legacy statuses). It is deliberately
strict: unknown routes raise, so a test can't silently pass against an un-stubbed call.
"""

from __future__ import annotations

import json
import re
import urllib.parse
from datetime import date, timedelta


def _days(start: str, end: str):
    d0 = date.fromisoformat(start)
    d1 = date.fromisoformat(end)
    cur = d0
    while cur <= d1:
        yield cur.isoformat()
        cur += timedelta(days=1)


def _day_before(iso: str) -> str:
    return (date.fromisoformat(iso) - timedelta(days=1)).isoformat()


def _day_after(iso: str) -> str:
    return (date.fromisoformat(iso) + timedelta(days=1)).isoformat()


class FakeGitHub:
    """Configurable fake for REST list endpoints + GraphQL hydration/status queries.

    Parameters
    ----------
    per_day:
        ``{"2026-01-15": 20}`` — how many workflow runs were created that day. Runs are
        synthesised newest-first across the whole configured span.
    checks_per_suite:
        ``{suite_db: n}`` override for how many check runs a suite has (default 3).
    over_100_suites:
        set of suite_db that should report ``pageInfo.hasNextPage=True`` from GraphQL and
        require the REST ``filter=all`` fallback to enumerate fully.
    statuses_for_sha:
        ``{sha: [context dicts]}`` legacy commit status contexts.
    filtered_cap:
        the value the fake pretends GitHub's filtered-search cap is (for ``created=`` queries
        the fake still returns the true count so the slicer can react).
    """

    def __init__(
        self,
        per_day: dict[str, int] | None = None,
        *,
        runs_at: list[str] | None = None,
        checks_per_suite: dict[int, int] | None = None,
        over_100_suites: set[int] | None = None,
        statuses_for_sha: dict[str, list[dict]] | None = None,
        null_run_nodes: set[str] | None = None,
        thirdparty_suites_for_sha: dict[str, list[dict]] | None = None,
        fail_tp_enum_shas: set[str] | None = None,
        fail_tp_rest_shas: set[str] | None = None,
        tp_enum_ok_below: int = 1,
        runs_per_sha: int = 3,
        repo_created_at: str = "2020-01-01T00:00:00Z",
        missing_repo_name: str | None = None,
    ) -> None:
        self.fail_tp_enum_shas = fail_tp_enum_shas or set()
        self.fail_tp_rest_shas = fail_tp_rest_shas or set()
        # a fail_tp_enum sha 504s only in batches of >= tp_enum_ok_below shas
        self.tp_enum_ok_below = tp_enum_ok_below
        self.checks_per_suite = checks_per_suite or {}
        self.over_100_suites = over_100_suites or set()
        self.statuses_for_sha = statuses_for_sha or {}
        self.null_run_nodes = null_run_nodes or set()
        # {sha: [{"database_id": int, "node_id": str, "app_slug": str, "checks": int}]}
        self.thirdparty_suites_for_sha = thirdparty_suites_for_sha or {}
        self.repo_created_at = repo_created_at
        self.missing_repo_name = missing_repo_name
        self.calls: list[str] = []

        # Build the run timestamps: either explicit (`runs_at`) or `n` per day spread evenly
        # across the day at second precision.
        timestamps: list[str] = list(runs_at or [])
        for day in sorted(per_day or {}, reverse=True):
            n = per_day[day]
            for k in range(n):
                sec = (k * 86_400) // max(n, 1)
                timestamps.append(f"{day}T{sec // 3600:02d}:{sec % 3600 // 60:02d}:{sec % 60:02d}Z")

        self.runs: list[dict] = []
        for idx, ts in enumerate(timestamps, start=1):
            rid = 10_000 + idx
            self.runs.append(
                {
                    "id": rid,
                    "node_id": f"WFR_fake{rid}",
                    "run_number": idx,
                    "run_attempt": 1,
                    "event": "push",
                    "status": "completed",
                    "conclusion": "success",
                    "workflow_id": 42,
                    "name": "CI",
                    "head_branch": "main",
                    "head_sha": f"{(idx - 1) // runs_per_sha:040x}",
                    "created_at": ts,
                    "updated_at": ts,
                    "html_url": f"https://github.com/o/r/actions/runs/{rid}",
                    "check_suite_id": 20_000 + idx,
                    "check_suite_node_id": f"CS_fake{20_000 + idx}",
                    "actor": {"login": "octocat"},
                    "triggering_actor": {"login": "octocat"},
                }
            )
        # GitHub returns runs strictly newest-first
        self.runs.sort(key=lambda r: (r["created_at"], r["id"]), reverse=True)
        self._by_node = {r["node_id"]: r for r in self.runs}

    def calls_to(self, needle: str) -> int:
        return sum(1 for c in self.calls if needle in c)

    # ---- transport entry point injected as RestClient/GraphQLClient fetch ----
    def fetch(self, method: str, url: str, headers: dict, body: bytes | None):
        parsed = urllib.parse.urlparse(url)
        qs = urllib.parse.parse_qs(parsed.query)
        self.calls.append(f"{method} {parsed.path}?{parsed.query}")
        if parsed.path == "/graphql":
            return self._graphql(json.loads(body.decode()))
        m = re.match(r"^/repos/([^/]+)/([^/]+)/actions/runs$", parsed.path)
        if m:
            if m.group(2) == self.missing_repo_name:
                return (404, {}, json.dumps({"message": "Not Found"}).encode())
            return self._list_runs(qs)
        m = re.match(r"^/repos/([^/]+)/([^/]+)/check-suites/(\d+)/check-runs$", parsed.path)
        if m:
            return self._suite_check_runs(int(m.group(3)), qs)
        m = re.match(r"^/repos/([^/]+)/([^/]+)/actions/runs/(\d+)$", parsed.path)
        if m:
            r = next((x for x in self.runs if x["id"] == int(m.group(3))), None)
            if r is None:
                return (404, {}, json.dumps({"message": "Not Found"}).encode())
            return (200, {}, json.dumps(r).encode())
        m = re.match(r"^/repos/([^/]+)/([^/]+)/commits/([0-9a-fx]+)/check-suites$", parsed.path)
        if m:
            return self._commit_check_suites(m.group(3))
        m = re.match(r"^/repos/([^/]+)/([^/]+)$", parsed.path)
        if m:
            return self._repo(m.group(1), m.group(2))
        return (404, {}, json.dumps({"message": f"fake has no route for {parsed.path}"}).encode())

    # ---- REST ----
    def _commit_check_suites(self, sha: str):
        if sha in self.fail_tp_rest_shas:
            return (504, {}, json.dumps({"message": "server timeout"}).encode())
        suites = self.thirdparty_suites_for_sha.get(sha, [])
        payload = {
            "total_count": len(suites),
            "check_suites": [
                {
                    "id": s["database_id"],
                    "node_id": s.get("node_id", f"TPS_{s['database_id']}"),
                    "head_sha": sha,
                    "status": "completed",
                    "conclusion": "success",
                    "app": {"id": 900 + s["database_id"] % 100, "slug": s["app_slug"],
                            "name": s["app_slug"], "node_id": f"A_{s['app_slug']}"},
                }
                for s in suites
            ],
        }
        return (200, {}, json.dumps(payload).encode())

    def _repo(self, owner: str, name: str):
        if name == self.missing_repo_name:
            return (404, {}, json.dumps({"message": "Not Found"}).encode())
        payload = {
            "full_name": f"{owner}/{name}",
            "private": False,
            "created_at": self.repo_created_at,
        }
        return (200, {"X-RateLimit-Remaining": "59"}, json.dumps(payload).encode())

    @staticmethod
    def _norm(bound: str | None, *, is_end: bool) -> str | None:
        """Normalise a `created=` bound to a full ISO timestamp (bare date -> day edge)."""
        if bound in (None, "", "*"):
            return None
        if "T" in bound:
            return bound if bound.endswith("Z") else bound + "Z"
        return f"{bound}T23:59:59Z" if is_end else f"{bound}T00:00:00Z"

    def _match_created(self, created: str):
        """Return the runs matching a `created=` filter. ISO-8601 sorts chronologically, so
        lexical string comparison of full timestamps is correct. `A..B` is inclusive both ends."""
        if ".." in created:
            lo_raw, hi_raw = created.split("..", 1)
            lo = self._norm(lo_raw, is_end=False)
            hi = self._norm(hi_raw, is_end=True)
        elif created.startswith(">="):
            lo, hi = self._norm(created[2:], is_end=False), None
        elif created.startswith("<="):
            lo, hi = None, self._norm(created[2:], is_end=True)
        elif created.startswith(">"):
            lo, hi = self._norm(_day_after(created[1:]), is_end=False), None
        elif created.startswith("<"):
            lo, hi = None, self._norm(_day_before(created[1:]), is_end=True)
        else:  # exact day
            lo = self._norm(created, is_end=False)
            hi = self._norm(created, is_end=True)
        out = []
        for r in self.runs:
            ts = r["created_at"]
            if lo and ts < lo:
                continue
            if hi and ts > hi:
                continue
            out.append(r)
        return out

    def _list_runs(self, qs: dict):
        per_page = int(qs.get("per_page", ["100"])[0])
        page = int(qs.get("page", ["1"])[0])
        created = qs.get("created", [None])[0]
        pool = self._match_created(created) if created else self.runs
        total = len(pool)
        start = (page - 1) * per_page
        chunk = pool[start : start + per_page]
        payload = {"total_count": total, "workflow_runs": chunk}
        return (200, {"X-RateLimit-Remaining": "4999"}, json.dumps(payload).encode())

    def _suite_check_runs(self, suite_db: int, qs: dict):
        filt = qs.get("filter", ["latest"])[0]
        per_page = int(qs.get("per_page", ["100"])[0])
        page = int(qs.get("page", ["1"])[0])
        total = self.checks_per_suite.get(suite_db, 3)
        # filter=all yields one extra "stale" run per suite when it's a rerun-heavy fixture
        if suite_db in self.over_100_suites and filt == "all":
            total = max(total, 105)
        runs = [
            {
                "id": suite_db * 1000 + i,
                "node_id": f"CR_{suite_db}_{i}",
                "name": f"job-{i}",
                "status": "completed",
                "conclusion": "success",
                "started_at": "2026-01-01T00:00:00Z",
                "completed_at": "2026-01-01T00:05:00Z",
                "html_url": f"https://github.com/o/r/runs/{suite_db*1000+i}",
                "app": {"slug": "github-actions", "id": 15368, "name": "GitHub Actions"},
            }
            for i in range(total)
        ]
        start = (page - 1) * per_page
        payload = {"total_count": total, "check_runs": runs[start : start + per_page]}
        return (200, {"X-RateLimit-Remaining": "4998"}, json.dumps(payload).encode())

    # ---- GraphQL ----
    def _graphql(self, req: dict):
        q = req["query"]
        variables = req.get("variables", {})
        if "$ids" in q or "nodes(ids:" in q or "nodes(ids :" in q:
            return self._graphql_nodes(variables.get("ids", []), want_suites="on CheckSuite" in q)
        if "object(oid:" in q or "object(oid :" in q:
            return self._graphql_statuses(q, want_suites="checkSuites(" in q)
        return (200, {}, json.dumps({"data": {"rateLimit": {"cost": 1, "remaining": 4990, "nodeCount": 0}}}).encode())

    def _graphql_nodes(self, ids: list[str], *, want_suites: bool = False):
        out = []
        for nid in ids:
            if want_suites and nid.startswith("TPS_"):
                out.append(self._thirdparty_suite_node(nid))
                continue
            if nid in self.null_run_nodes:
                out.append(None)
                continue
            r = self._by_node.get(nid)
            if r is None:
                out.append(None)
                continue
            suite_db = r["check_suite_id"]
            total_checks = self.checks_per_suite.get(suite_db, 3)
            has_next = suite_db in self.over_100_suites
            visible = 100 if has_next else total_checks
            check_nodes = [
                {
                    "databaseId": suite_db * 1000 + i,
                    "name": f"job-{i}",
                    "status": "COMPLETED",
                    "conclusion": "SUCCESS",
                    "startedAt": "2026-01-01T00:00:00Z",
                    "completedAt": "2026-01-01T00:05:00Z",
                    "detailsUrl": f"https://github.com/o/r/runs/{suite_db*1000+i}",
                }
                for i in range(visible)
            ]
            out.append(
                {
                    "__typename": "WorkflowRun",
                    "id": nid,
                    "databaseId": r["id"],
                    "runNumber": r["run_number"],
                    "runAttempt": r["run_attempt"],
                    "event": r["event"],
                    "url": r["html_url"],
                    "createdAt": r["created_at"],
                    "updatedAt": r["updated_at"],
                    "displayTitle": f"run {r['run_number']}",
                    "workflow": {"name": "CI", "databaseId": 42},
                    "checkSuite": {
                        "id": r["check_suite_node_id"],
                        "databaseId": suite_db,
                        "status": "COMPLETED",
                        "conclusion": "SUCCESS",
                        "createdAt": r["created_at"],
                        "updatedAt": r["updated_at"],
                        "branch": {"name": "main"},
                        "commit": {"oid": r["head_sha"]},
                        "app": {"slug": "github-actions", "name": "GitHub Actions", "databaseId": 15368},
                        "checkRuns": {
                            "totalCount": total_checks,
                            "pageInfo": {"hasNextPage": has_next, "endCursor": "CUR"},
                            "nodes": check_nodes,
                        },
                    },
                }
            )
        data = {"rateLimit": {"cost": 1, "remaining": 4990, "nodeCount": len(ids) * 101}, "nodes": out}
        return (200, {}, json.dumps({"data": data}).encode())

    def _thirdparty_suite_node(self, node_id: str):
        db = int(node_id.split("_")[1])
        for suites in self.thirdparty_suites_for_sha.values():
            for s in suites:
                if s["database_id"] == db:
                    n = s.get("checks", 0)
                    return {
                        "__typename": "CheckSuite",
                        "id": node_id,
                        "databaseId": db,
                        "checkRuns": {
                            "totalCount": n,
                            "pageInfo": {"hasNextPage": False, "endCursor": "C"},
                            "nodes": [
                                {
                                    "databaseId": db * 1000 + i,
                                    "id": f"CR_{db}_{i}",
                                    "name": f"{s['app_slug']}-check-{i}",
                                    "status": "COMPLETED",
                                    "conclusion": "SUCCESS",
                                    "startedAt": "2026-01-01T00:00:00Z",
                                    "completedAt": "2026-01-01T00:02:00Z",
                                    "detailsUrl": "https://x/tp",
                                }
                                for i in range(n)
                            ],
                        },
                    }
        return None

    def _graphql_statuses(self, q: str, *, want_suites: bool = False):
        shas_in_q = [m[1] for m in re.findall(r'(c\d+):\s*object\(oid:\s*"([0-9a-fx]+)"\)', q)]
        if (want_suites and self.fail_tp_enum_shas.intersection(shas_in_q)
                and len(shas_in_q) >= self.tp_enum_ok_below):
            return (504, {}, json.dumps({"message": "server timeout"}).encode())
        repo: dict = {}
        for alias, sha in re.findall(r'(c\d+):\s*object\(oid:\s*"([0-9a-fx]+)"\)', q):
            ctxs = self.statuses_for_sha.get(sha)
            node: dict = {"__typename": "Commit", "oid": sha, "status": None}
            if ctxs:
                node["status"] = {
                    "state": "SUCCESS",
                    "contexts": [
                        {
                            "context": c["context"],
                            "state": c.get("state", "SUCCESS"),
                            "description": c.get("description", ""),
                            "targetUrl": c.get("targetUrl", ""),
                            "createdAt": c.get("createdAt", "2026-01-01T00:00:00Z"),
                        }
                        for c in ctxs
                    ],
                }
            if want_suites:
                ga = [
                    {
                        "id": r["check_suite_node_id"],
                        "databaseId": r["check_suite_id"],
                        "app": {"slug": "github-actions", "name": "GitHub Actions",
                                "databaseId": 15368, "id": "A_ga"},
                        "workflowRun": {"databaseId": r["id"]},
                        "conclusion": "SUCCESS",
                        "status": "COMPLETED",
                        "createdAt": r["created_at"],
                        "updatedAt": r["updated_at"],
                        "url": "https://x/cs",
                    }
                    for r in self.runs
                    if r["head_sha"] == sha
                ]
                tp = [
                    {
                        "id": s.get("node_id", f"TPS_{s['database_id']}"),
                        "databaseId": s["database_id"],
                        "app": {"slug": s["app_slug"], "name": s["app_slug"],
                                "databaseId": 900 + s["database_id"] % 100, "id": f"A_{s['app_slug']}"},
                        "workflowRun": None,
                        "conclusion": "SUCCESS",
                        "status": "COMPLETED",
                        "createdAt": "2026-01-01T00:00:00Z",
                        "updatedAt": "2026-01-01T00:03:00Z",
                        "url": "https://x/tp",
                    }
                    for s in self.thirdparty_suites_for_sha.get(sha, [])
                ]
                node["checkSuites"] = {"totalCount": len(ga) + len(tp), "nodes": ga + tp}
            repo[alias] = node
        data = {"rateLimit": {"cost": 2, "remaining": 4989, "nodeCount": 0}, "repository": repo}
        return (200, {}, json.dumps({"data": data}).encode())
