# runkeep

**Archive a GitHub repo's CI history to a local SQLite file before GitHub's new 90-day
retention window starts applying to it on Oct 1, 2026.**

[![CI](https://github.com/canblmz1/runkeep/actions/workflows/ci.yml/badge.svg)](https://github.com/canblmz1/runkeep/actions/workflows/ci.yml)
[![PyPI](https://img.shields.io/pypi/v/runkeep.svg)](https://pypi.org/project/runkeep/)

![demo](demo.gif)

<!-- demo.gif is generated from demo.tape by the `demo` workflow (charmbracelet/vhs). -->

<details>
<summary>the same session as text</summary>

```console
$ runkeep check pallets/click

  pallets/click

  total workflow runs   3,661
  older than 90 days    2,680
  oldest run            2025-07-13  (14 months ago)

  2,680 runs are outside the 90-day retention window.
  GitHub starts applying that window to run history on Oct 1, 2026.

  archive it:  runkeep rescue pallets/click

$ runkeep rescue pallets/click --since 2026-08-27 --db click.db

  pallets/click  ->  click.db

  workflow runs     45   (45 hydrated)
  check suites      49
  check runs        276
  legacy statuses   15
  third-party       4 suites, 0 checks
  missing           0

  236 KB written in 7s

  Core CI history      complete
  Third-party checks   complete

$ runkeep verify click.db pallets/click --sample 8

  verify pallets/click  (click.db)

  archive invariant   49/49 suites consistent
  suites vs GitHub    8/8 match  (spot-check)
  runs vs GitHub      4/4 match  (spot-check)
  core archive        complete
  third-party checks  complete

  OK  (15.7s)
```
</details>

*(All output in this README is from real runs. Counts are live — yours will differ.)*

## Why

On [2026-08-27 GitHub announced](https://github.blog/changelog/2026-08-27-actions-retention-will-cover-checks-workflow-runs-and-statuses/)
that **starting October 1, 2026, checks, workflow runs, and statuses will be governed by the
same Actions retention setting** — a maximum of 90 days for public repositories, which cannot
be raised. Today that history is kept well beyond 90 days, so a lot of it is about to age out.
`runkeep` copies it into a local SQLite file you keep.

## Install

```bash
uvx runkeep check astral-sh/ruff
```

That's the zero-install path — [`uv`](https://docs.astral.sh/uv/) downloads `runkeep`, runs it,
and throws it away. To keep it around:

```bash
pipx install runkeep      # isolated
pip install runkeep       # or plain pip
```

Python 3.10+. No dependencies.

## Commands

### `runkeep check OWNER/REPO`

How much CI history the repo is about to lose. Read-only, usually a few seconds, **no token
needed for public repos**.

```
$ runkeep check astral-sh/ruff

  astral-sh/ruff

  total workflow runs   ~150k
  older than 90 days    ~104k
  oldest run            2025-07-12  (14 months ago)

  ~104k runs are outside the 90-day retention window.
  GitHub starts applying that window to run history on Oct 1, 2026.

  archive it:  runkeep rescue astral-sh/ruff

  counts >= 5,000 are GitHub's live search estimates (they drift a few %).
```

GitHub's unfiltered run count is capped at 40,000, so `check` reconstructs the real total from
two `created=` date-window counts. Those search counts wobble a few percent on large ranges,
so counts at or above 5,000 are shown rounded, with a `~`.

### `runkeep rescue OWNER/REPO [--since DATE] [--until DATE] [--db FILE] [--no-thirdparty]`

Archive the history into SQLite (`<repo>.db` by default). It discovers every workflow run by
recursively slicing `created=` time windows, hydrates each run's check suite and check runs
via GraphQL, collects legacy commit statuses, and enumerates independent third-party check
suites.

- **Resumable.** Interrupt it (Ctrl-C, dropped connection, laptop lid) and run the same
  command again — it reads its own progress out of the SQLite file and continues. Completed
  time slices, hydrated runs, probed commits, and hydrated third-party suites are never redone.
- `--since 2026-01-01` bounds the backfill. A bare `runkeep rescue OWNER/REPO` walks the
  repo's entire surviving history (this can be slow and page-heavy for very large repos —
  bound it with `--since`).
- **Two independent verdicts.** *Core CI history* — workflow runs, their check suites/runs,
  and legacy statuses — is what "the archive is usable" means. *Third-party checks* (CodSpeed,
  Codecov, Renovate, ...) are captured by default but treated as optional: if GitHub times out
  enumerating them on a huge repo, `runkeep` shrinks its batch size, records the commits it
  still couldn't reach, and finishes the core archive anyway. Re-run `rescue` to retry those
  gaps. `--no-thirdparty` skips them entirely.

```
$ runkeep rescue elastic/elasticsearch --since 2026-08-27 --until 2026-08-27 --db es.db
...
  Core CI history      complete
  Third-party checks   incomplete (2 commits could not be queried)

  The archive is usable, but third-party check coverage is incomplete.
  Re-run rescue to retry those gaps.
```

`rescue` needs a token (see [Is this safe?](#is-this-safe)).

### `runkeep verify FILE.db OWNER/REPO`

A trust check. It:

1. re-checks the archive's internal invariant on **every** suite — stored check-run count
   equals the recorded expected count;
2. re-queries a random sample of suites **and** runs against live GitHub through a *separate*
   code path (direct REST `filter=all` pagination), so a bug in `rescue` can't hide behind a
   matching bug in its own verifier; and
3. reports the archive's own `core` / `third-party` completeness flags.

It is a **spot-check plus an invariant**, not a mathematical re-verification of every remote
record. A run that GitHub has since deleted is reported as "deleted at source", not a failure.

## What it saves / what it does not

| Saved (full fidelity) | Not saved |
|---|---|
| Workflow runs — number, attempt, event, status, conclusion, SHA, branch, actor, timestamps, URL | Job **step** details |
| Check suites — status, conclusion, timestamps, commit, branch | Check run **annotations** |
| Check runs — **all attempts** (`filter=all`, never `latest`) | Build **logs** |
| App identity — which GitHub App produced each suite | **Artifacts** |
| Legacy commit status contexts — context, state, description, target URL | Per-attempt job timing |
| Independent third-party check suites + their check runs (best-effort on very large repos) | |
| The relationships between all of the above | |

**Logs and artifacts are deliberately out of scope.** For history old enough to matter here
they are almost always **already deleted at source** by GitHub's existing 90-day artifact/log
window. If you need those, download them separately while they still exist.

`runkeep` is **not** a complete GitHub or GitHub Actions backup, it does **not** preserve logs
or artifacts, and `verify` does **not** re-verify every archived record against GitHub. Full
detail and how each guarantee is checked: [docs/fidelity.md](docs/fidelity.md).

## Is this safe?

Yes, by construction:

- **Read-only.** Every request is a `GET` or a read-only GraphQL query. `runkeep` never issues
  a mutation and refuses to send any GraphQL document containing `mutation`.
- **Your token is never printed, logged, or written to disk** — not into the SQLite file, not
  into `--json` output, nowhere. It is read from `$GITHUB_TOKEN` (or `$GH_TOKEN`) and used only
  as an `Authorization` header.
- **The only host contacted is `api.github.com`.** No telemetry, no analytics, no other
  network egress.
- **The archive is a plain SQLite file you own.** Open it with any SQLite tool.

**Token scopes.** `check` needs no token for public repos. `rescue` and `verify` do:

| Token type | What to grant |
|---|---|
| Classic PAT | `public_repo` for public repos, or `repo` for private ones |
| Fine-grained PAT | Repository access to the target repo, then **Read-only** for *Actions*, *Checks*, *Commit statuses*, and *Metadata*. If a call fails with a permission error, it names the one to add. |

## Do I even need this?

Maybe not.

- **Private repositories:** you can raise the Actions retention period to **up to 400 days**
  in *Settings → Actions → General → Artifact and log retention*
  ([GitHub docs](https://docs.github.com/en/github/administering-a-repository/configuring-the-retention-period-for-github-actions-artifacts-and-logs-in-your-repository)).
  If 400 days is enough, do that instead — nothing to install or maintain.
- **Public repositories:** the limit is **90 days and cannot be raised**
  ([GitHub docs](https://docs.github.com/en/organizations/managing-organization-settings/configuring-the-retention-period-for-github-actions-artifacts-and-logs-in-your-organization)).
  A local archive is the only option.
- Changing the retention setting only affects **new** runs — it does not bring back history
  that has already aged past the limit.

`runkeep check` tells you how much is actually at stake for a given repo before you decide.

## Schema

One SQLite file (WAL mode). The tables you'll query:

| Table | One row per | Key columns |
|---|---|---|
| `workflow_run` | workflow run | `database_id`, `node_id`, `run_number`, `run_attempt`, `event`, `status`, `conclusion`, `head_sha`, `head_branch`, `actor_login`, `created_at`, `hydrated` |
| `check_suite` | check suite | `database_id`, `workflow_run_id` (NULL = independent third-party), `app_id`, `conclusion`, `head_sha`, `checkrun_total_count`, `checkrun_complete` |
| `check_run` | check run | `database_id`, `check_suite_id`, `name`, `status`, `conclusion`, `started_at`, `completed_at`, `app_slug` |
| `app` | GitHub App | `database_id`, `slug`, `name` |
| `status_context` | (commit, legacy status context) | `commit_sha`, `context`, `state`, `description`, `target_url` |
| `commit_status_probe` | commit checked for legacy statuses | `commit_sha`, `has_status`, `context_count` |
| `hydration_gap` | anything not fully archived | `kind`, `ref`, `detail` — empty for a complete archive |
| `discovery_slice`, `thirdparty_probe`, `archive_meta` | resume bookkeeping / metadata | — |

```sql
-- failure rate by workflow, last 90 days in the archive
SELECT workflow_name,
       count(*)                                                  AS runs,
       round(100.0 * sum(conclusion = 'failure') / count(*), 1)  AS pct_failed
FROM workflow_run
WHERE created_at > strftime('%Y-%m-%dT%H:%M:%SZ', 'now', '-90 days')
GROUP BY workflow_name
ORDER BY runs DESC;
```

```sql
-- slowest check runs ever recorded
SELECT cr.name,
       r.run_number,
       round((julianday(cr.completed_at) - julianday(cr.started_at)) * 1440, 1) AS minutes
FROM check_run cr
JOIN check_suite s  ON s.database_id = cr.check_suite_id
JOIN workflow_run r ON r.database_id = s.workflow_run_id
WHERE cr.completed_at IS NOT NULL AND cr.started_at IS NOT NULL
ORDER BY minutes DESC
LIMIT 20;
```

```sql
-- which non-Actions apps post checks on this repo, and how often
SELECT a.slug, a.name, count(*) AS suites
FROM check_suite s
JOIN app a ON a.database_id = s.app_id
WHERE a.slug <> 'github-actions'
GROUP BY a.slug
ORDER BY suites DESC;
```

## License

MIT — see [LICENSE](LICENSE).
