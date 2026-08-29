# What runkeep preserves — the fidelity contract

runkeep is honest about being an **archive of GitHub's expiring CI *history***, not a complete
GitHub Actions backup. This page is the exact contract, and how each guarantee was verified.

## Architecture

```
REST discovery                 recursive created= time-interval slicing, day -> hour ->
                               minute -> second; a window over the page cap is bisected,
                               never truncated
   |
   v
GraphQL bulk hydration         nodes(ids: [<=100 WorkflowRun global node ids]) -> CheckSuite
                               + App + CheckRuns(filterBy: {checkType: ALL}); ~1 GraphQL
                               cost point per 100 runs
   |
   v
narrow REST fallback           check-suites/{id}/check-runs?filter=all, only for suites with
                               >100 check runs (GraphQL can't page that connection fully)
   |
   v
SQLite archive                 WAL, foreign keys enforced, every write an idempotent upsert
```

Everything is resumable: re-running `rescue` against an existing `--db` reads the
`discovery_slice` table, the `workflow_run.hydrated` flag, `commit_status_probe`, and
`check_suite.checkrun_source` and continues from exactly where it stopped.

## Preserved — full fidelity

| Item | Source | Notes |
|---|---|---|
| Workflow runs | REST discovery | id, node id, run number, **run attempt**, event, status, conclusion, head SHA, head branch, actor + triggering actor, timestamps (`created_at`, `updated_at`, `run_started_at`), URL |
| Check suites | GraphQL `WorkflowRun.checkSuite` | id, node id, status, conclusion, timestamps, branch, commit SHA |
| Check runs | GraphQL `checkRuns(filterBy: {checkType: ALL})`, REST `filter=all` for suites over 100 | id, node id, name, status, conclusion, `started_at`, `completed_at`, details URL |
| App identity | `CheckSuite.app` (never `CheckRun.app`) | slug, name, database id, node id — so a third-party check keeps its `codecov` / `codspeed-hq` identity |
| Legacy commit status contexts | GraphQL `Commit.status.contexts` | context, state, description, target URL, timestamp. **One row per (commit, context)** — the current archived representation, *not* every historical status transition event |
| Relationships | foreign keys | `app -> workflow_run -> check_suite -> check_run`; `status_context` and third-party suites keyed by commit SHA |
| Independent third-party check suites | GraphQL `Commit.checkSuites` per unique commit (**on by default**; `--no-thirdparty` to skip) | Suites with no associated workflow run — CodSpeed, Codecov, Renovate, etc. — plus their check runs |

### Re-run divergence is preserved

When a workflow is re-run, GitHub keeps the earlier attempt's check runs. `filter=latest` /
the GraphQL default would silently drop them. runkeep uses `checkType: ALL` / `filter=all`
everywhere. This is checked on live data: on `astral-sh/ruff`, suite `89630904990` has 84
check runs under `filter=all` versus 42 under `filter=latest` — runkeep stores all 84.

## Not preserved — out of scope

| Item | Why |
|---|---|
| **Job step details** | Not exposed usefully for historical runs; large; low value once the run is old |
| **Check run annotations** | Same |
| **Build logs** | GitHub already deletes these on the *existing* artifact/log retention window (default 90 days). For history old enough to matter here, the logs are usually **already gone at source** |
| **Artifacts** | Same — already deleted at source in almost all cases |
| **Previous-attempt job timing / per-attempt job breakdown** | Partial and inconsistent for old runs |

runkeep will not claim to preserve logs or artifacts. If you need those, download them
separately while they still exist (they won't, for anything old).

## Completeness — how "missing = 0" is proven

runkeep never reports success just because the API answered.

- **Missing runs.** Every discovered run is written to `workflow_run` with `hydrated = 0`
  *before* hydration. The flag flips to `1` only when GraphQL returns a `WorkflowRun` node
  for that exact node id. `missing` counts `WHERE hydrated = 0`. Anything that never came
  back is also written to the `hydration_gap` table — never dropped silently.
- **Missing checks.** For every suite, the expected count is GraphQL
  `checkRuns(filterBy: {checkType: ALL}).totalCount`. `missing` sums, per suite,
  `max(0, expected - stored_rows)`. A suite whose expected total can't be established is
  reported as **indeterminate**, not silently treated as zero.
- **`verify`.** `runkeep verify FILE.db OWNER/REPO` re-checks the whole-archive invariant
  (stored rows == expected, for every suite) and spot-checks a random sample of suites *and*
  runs against live GitHub through a completely separate code path (direct REST `filter=all`
  pagination). A bug in the rescue pipeline can't hide a matching bug in its own check.

## Known quirks (faithfully preserved)

- **`check` counts wobble.** GitHub's `created=` search counts drift a few percent on large
  date ranges (observed: `astral-sh/ruff` reported 100,208 → 104,388 for the same immutable
  query across repeated calls, ~4%). `check` shows counts at or above 5,000 rounded, with a
  `~`, and says so.
- **`SKIPPED` check runs.** GitHub itself returns `completed_at` about one second *before*
  `started_at` for skipped checks. runkeep stores GitHub's values verbatim.
- **`rescue` on a very large repo is slow, not wrong.** GitHub's filtered run search is
  ~0.5–4 s per call depending on repo size, and a 40k-run backfill is thousands of pages. Use
  `--since` to bound it; the resume support means an interrupted backfill costs nothing to
  restart.
