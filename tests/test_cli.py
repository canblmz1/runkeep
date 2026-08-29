"""CLI surface: three subcommands, clean token errors, strict OWNER/REPO, no tracebacks."""

from __future__ import annotations

import io
import unittest
from contextlib import redirect_stderr, redirect_stdout
from unittest import mock

from runkeep.cli import build_parser, main


class CliTests(unittest.TestCase):
    def test_exactly_three_subcommands(self) -> None:
        parser = build_parser()
        sub = next(a for a in parser._actions if a.dest == "command")
        self.assertEqual(set(sub.choices), {"check", "rescue", "verify"})

    def test_rescue_without_token_is_a_clean_error_exit_3(self) -> None:
        out, err = io.StringIO(), io.StringIO()
        with mock.patch.dict("os.environ", {}, clear=True), \
                redirect_stdout(out), redirect_stderr(err):
            code = main(["rescue", "astral-sh/ruff"])
        self.assertEqual(code, 3)
        self.assertEqual(out.getvalue(), "", "nothing should go to stdout on this error")
        self.assertIn("token", err.getvalue().lower())
        self.assertNotIn("Traceback", err.getvalue())

    def test_repo_must_be_owner_slash_repo(self) -> None:
        with self.assertRaises(SystemExit):
            build_parser().parse_args(["check", "notaslug"])

    def test_repo_parsed_into_owner_repo(self) -> None:
        p = build_parser()
        self.assertEqual(p.parse_args(["check", "astral-sh/ruff"]).repo, ("astral-sh", "ruff"))
        self.assertEqual(
            p.parse_args(["check", "https://github.com/astral-sh/ruff"]).repo,
            ("astral-sh", "ruff"),
        )
        self.assertEqual(p.parse_args(["check", "astral-sh/ruff.git"]).repo, ("astral-sh", "ruff"))

    def test_no_command_is_a_clean_error_not_a_traceback(self) -> None:
        with self.assertRaises(SystemExit) as ctx:
            build_parser().parse_args([])
        self.assertEqual(ctx.exception.code, 2)


if __name__ == "__main__":
    unittest.main()
