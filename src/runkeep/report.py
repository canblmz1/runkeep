"""The ``rescue`` completion summary — built to be screenshotted."""

from __future__ import annotations

from .pipeline import RescueResult

_DIM = "\x1b[2m"
_BOLD = "\x1b[1m"
_RESET = "\x1b[0m"


def _fmt_bytes(n: int) -> str:
    if n >= 1024 * 1024:
        return f"{n / 1024 / 1024:.1f} MB"
    if n >= 1024:
        return f"{n / 1024:.0f} KB"
    return f"{n} B"


def _fmt_elapsed(s: float) -> str:
    if s < 1:
        return "<1s"
    s = int(s)
    if s < 60:
        return f"{s}s"
    if s < 3600:
        return f"{s // 60}m {s % 60}s"
    return f"{s // 3600}h {s % 3600 // 60}m"


def rescue_summary(result: RescueResult, *, color: bool = False) -> str:
    dim = _DIM if color else ""
    bold = _BOLD if color else ""
    reset = _RESET if color else ""
    c = result.completeness

    rows = [
        ("workflow runs", f"{c.discovered_runs:,}   {dim}({c.hydrated_runs:,} hydrated){reset}"),
        ("check suites", f"{c.check_suites:,}"),
        ("check runs", f"{c.stored_checks:,}"),
        ("legacy statuses", f"{c.legacy_status_contexts:,}"),
    ]
    if c.thirdparty_suites:
        rows.append(("third-party", f"{c.thirdparty_suites:,} suites, {c.thirdparty_check_runs:,} checks"))

    missing = c.missing_run_hydrations + c.missing_checks
    miss_val = f"{missing}" if missing == 0 else f"{bold}{missing}{reset}"
    if c.indeterminate_suites:
        miss_val += f"  {dim}(+{c.indeterminate_suites} suites: expected total unknown){reset}"
    rows.append(("missing", miss_val))

    width = max(len(k) for k, _ in rows)
    body = "\n".join(f"  {dim}{k.ljust(width)}{reset}   {v}" for k, v in rows)

    header = f"{result.owner}/{result.repo}  {dim}->{reset}  {result.store.path}"
    footer = f"{_fmt_bytes(result.db_bytes)} written in {_fmt_elapsed(result.elapsed_s)}"

    out = ["", f"  {bold}{header}{reset}"]
    if result.resumed:
        out.append(f"  {dim}(resumed an earlier run){reset}")
    out += ["", body, "", f"  {footer}", ""]

    core = "complete" if c.core_complete else f"{bold}incomplete - re-run rescue to resume{reset}"
    out.append(f"  {dim}Core CI history      {reset}{core}")
    if c.thirdparty_requested:
        if c.third_party_complete:
            tp = "complete"
        else:
            bits = []
            unresolved = c.thirdparty_gap_commits + c.thirdparty_commits_unprobed
            if unresolved:
                bits.append(f"{unresolved} commits could not be queried")
            if c.thirdparty_pending_suites:
                bits.append(f"{c.thirdparty_pending_suites} suites not hydrated")
            tp = f"{bold}incomplete{reset} {dim}({'; '.join(bits) or 'gaps recorded'}){reset}"
        out.append(f"  {dim}Third-party checks   {reset}{tp}")
        if not c.third_party_complete:
            out += ["", "  The archive is usable, but third-party check coverage is incomplete.",
                    "  Re-run rescue to retry those gaps."]
    out.append("")
    return "\n".join(out)


def summary_dict(result: RescueResult) -> dict:
    c = result.completeness
    return {
        "repo": f"{result.owner}/{result.repo}",
        "db": result.store.path,
        "resumed": result.resumed,
        "workflow_runs": c.discovered_runs,
        "runs_hydrated": c.hydrated_runs,
        "check_suites": c.check_suites,
        "check_runs": c.stored_checks,
        "legacy_status_contexts": c.legacy_status_contexts,
        "thirdparty_suites": c.thirdparty_suites,
        "thirdparty_check_runs": c.thirdparty_check_runs,
        "missing_run_hydrations": c.missing_run_hydrations,
        "missing_checks": c.missing_checks,
        "indeterminate_suites": c.indeterminate_suites,
        "gaps_recorded": result.store.count("hydration_gap"),
        "core_complete": c.core_complete,
        "third_party_requested": c.thirdparty_requested,
        "third_party_complete": c.third_party_complete,
        "third_party_gap_commits": c.thirdparty_gap_commits,
        "third_party_commits_unprobed": c.thirdparty_commits_unprobed,
        "db_bytes": result.db_bytes,
        "elapsed_s": round(result.elapsed_s, 1),
        "api_calls": {
            "rest": result.meter.rest_calls,
            "graphql": result.meter.graphql_calls,
            "graphql_cost": result.meter.graphql_cost_total,
            "rate_limit_waits": result.meter.rate_limit_waits,
        },
        "discovery": {
            "slices_queried": result.discovery.slices_queried,
            "slices_split": result.discovery.slices_split,
            "slices_skipped": result.discovery.slices_skipped,
            "max_depth": result.discovery.max_depth,
            "duplicates_removed": result.discovery.duplicates_removed,
        },
    }
