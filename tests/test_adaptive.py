"""split_retry: on a gateway-class failure, halve the batch and retry; never lose siblings."""

from __future__ import annotations

import unittest

from runkeep.adaptive import split_retry


class Gateway(Exception):
    pass


class SplitRetryTests(unittest.TestCase):
    def test_happy_path_is_one_call_per_initial_batch(self) -> None:
        calls: list[list[int]] = []
        stats = split_retry(
            list(range(10)), 5, lambda c: calls.append(list(c)),
            lambda x: None, gateway_errors=(Gateway,),
        )
        self.assertEqual(calls, [[0, 1, 2, 3, 4], [5, 6, 7, 8, 9]])
        self.assertEqual(stats.splits, 0)
        self.assertEqual(stats.singleton_failures, 0)

    def test_a_failing_batch_is_split_in_half_and_retried(self) -> None:
        ok: list[list[int]] = []
        fail_once_at_size = {4}

        def run(chunk):
            if len(chunk) in fail_once_at_size:
                fail_once_at_size.discard(len(chunk))
                raise Gateway("504")
            ok.append(list(chunk))

        stats = split_retry([0, 1, 2, 3], 4, run, lambda x: None, gateway_errors=(Gateway,))
        self.assertEqual(sorted(ok), [[0, 1], [2, 3]])
        self.assertEqual(stats.splits, 1)
        self.assertEqual(stats.min_batch, 2)

    def test_recurses_to_singletons_then_reports_each_failure(self) -> None:
        failed: list[int] = []
        stats = split_retry(
            [0, 1, 2, 3], 4,
            lambda c: (_ for _ in ()).throw(Gateway("504")),
            lambda x: failed.append(x),
            gateway_errors=(Gateway,),
        )
        self.assertEqual(sorted(failed), [0, 1, 2, 3])
        self.assertEqual(stats.singleton_failures, 4)
        self.assertEqual(stats.min_batch, 1)

    def test_a_non_gateway_error_propagates_immediately(self) -> None:
        with self.assertRaises(ValueError):
            split_retry(
                [0, 1], 2,
                lambda c: (_ for _ in ()).throw(ValueError("bug")),
                lambda x: None, gateway_errors=(Gateway,),
            )

    def test_a_failed_half_never_discards_a_successful_sibling(self) -> None:
        ok: list[list[int]] = []
        failed: list[int] = []

        def run(chunk):
            if chunk == [3] or (3 in chunk and len(chunk) > 1):
                raise Gateway("504")
            ok.append(list(chunk))

        split_retry([0, 1, 2, 3], 4, run, lambda x: failed.append(x), gateway_errors=(Gateway,))
        self.assertIn([0, 1], ok)
        self.assertIn([2], ok)
        self.assertEqual(failed, [3])


if __name__ == "__main__":
    unittest.main()
