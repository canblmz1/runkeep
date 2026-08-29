"""``runkeep`` command-line entry point.

Exactly three subcommands: ``check`` (headline, read-only, no token needed for public repos),
``rescue`` (the archiver), ``verify`` (re-query GitHub and confirm an archive matches).

Tokens come from ``$GITHUB_TOKEN`` or ``$GH_TOKEN`` and are never printed, logged, or written
to disk. ``UserFacingError`` subclasses print as a plain one-line message with no traceback;
anything else is a bug and is allowed to crash.
"""

from __future__ import annotations

import argparse
import json
import os
import sys

from .check import format_check, run_check
from .errors import AuthUnavailable, UserFacingError
from .pipeline import run_rescue
from .report import rescue_summary, summary_dict
from .verify import run_verify

BLOCKED_BANNER = "BLOCKED: GitHub authentication unavailable"
_TOKEN_ENV = ("GITHUB_TOKEN", "GH_TOKEN")


def _token() -> str | None:
    for name in _TOKEN_ENV:
        if os.environ.get(name):
            return os.environ[name]
    return None


def _parse_repo(value: str) -> tuple[str, str]:
    v = value.strip()
    for prefix in ("https://github.com/", "http://github.com/", "github.com/"):
        if v.startswith(prefix):
            v = v[len(prefix):]
    v = v.removesuffix(".git")
    parts = v.strip("/").split("/")
    if len(parts) != 2 or not all(parts):
        raise argparse.ArgumentTypeError("repository must be OWNER/REPO, e.g. astral-sh/ruff")
    return parts[0], parts[1]


def _stderr(msg: str) -> None:
    print(msg, file=sys.stderr, flush=True)


def build_parser() -> argparse.ArgumentParser:
    p = argparse.ArgumentParser(
        prog="runkeep",
        description="Archive a GitHub repo's CI history to local SQLite before the Oct 1, 2026 "
        "retention change starts applying to run history.",
    )
    sub = p.add_subparsers(dest="command", required=True)

    c = sub.add_parser("check", help="how much CI history this repo is about to lose (read-only)")
    c.add_argument("repo", type=_parse_repo, metavar="OWNER/REPO")

    r = sub.add_parser("rescue", help="archive a repo's CI history into a local SQLite file")
    r.add_argument("repo", type=_parse_repo, metavar="OWNER/REPO")
    r.add_argument("--db", help="SQLite output path (default: <repo>.db)")
    r.add_argument("--since", metavar="DATE", help="ISO date; archive runs created on/after it")
    r.add_argument("--until", metavar="DATE", help="ISO date; defaults to today")
    r.add_argument("--limit", type=int, help="stop after N runs (default: no limit)")
    r.add_argument("--no-thirdparty", action="store_true",
                   help="skip independent third-party check suites")
    r.add_argument("--json", action="store_true", help="also print a machine-readable summary")

    v = sub.add_parser("verify", help="re-query GitHub and confirm an archive is complete")
    v.add_argument("db", metavar="FILE.db")
    v.add_argument("repo", type=_parse_repo, metavar="OWNER/REPO")
    v.add_argument("--sample", type=int, default=15, help="suites/runs to spot-check (default 15)")
    v.add_argument("--json", action="store_true")

    return p


def _cmd_check(args) -> int:
    owner, repo = args.repo
    result = run_check(owner, repo, token=_token(), notify=_stderr)
    sys.stdout.write(format_check(result, color=sys.stdout.isatty()))
    sys.stdout.flush()
    if not result.authenticated:
        _stderr("unauthenticated (60 req/hour) - set GITHUB_TOKEN for 5,000/hour\n")
    return 0


def _cmd_rescue(args) -> int:
    token = _token()
    if not token:
        raise AuthUnavailable(
            "rescue needs a token: set GITHUB_TOKEN (fine-grained with read-only Actions, "
            "or classic with the 'repo' scope). `runkeep check` works without one."
        )
    owner, repo = args.repo
    db = args.db or f"{repo}.db"
    result = run_rescue(
        owner, repo,
        limit=args.limit,
        db_path=db,
        token=token,
        since=args.since,
        until=args.until,
        with_thirdparty=not args.no_thirdparty,
        notify=_stderr,
    )
    sys.stdout.write(rescue_summary(result, color=sys.stdout.isatty()))
    if args.json:
        sys.stdout.write("\n" + json.dumps(summary_dict(result), indent=2) + "\n")
    sys.stdout.flush()
    complete = result.completeness.core_complete
    result.store.close()
    return 0 if complete else 2


def _cmd_verify(args) -> int:
    owner, repo = args.repo
    report = run_verify(args.db, owner, repo, token=_token(), sample=args.sample, notify=_stderr)
    sys.stdout.write(report.render())
    if args.json:
        sys.stdout.write("\n" + json.dumps(report.as_dict(), indent=2) + "\n")
    sys.stdout.flush()
    return 0 if report.ok else 2


_DISPATCH = {"check": _cmd_check, "rescue": _cmd_rescue, "verify": _cmd_verify}


def main(argv: list[str] | None = None) -> int:
    args = build_parser().parse_args(argv)
    try:
        return _DISPATCH[args.command](args)
    except UserFacingError as exc:
        _stderr(f"error: {exc}")
        return getattr(exc, "exit_code", 1)
    except KeyboardInterrupt:
        _stderr("\ninterrupted.")
        return 130


if __name__ == "__main__":
    sys.exit(main())
