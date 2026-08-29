"""Adaptive batching: shrink under gateway-class failure, never lose a sibling batch.

Used for the commit-axis GraphQL passes (third-party check-suite enumeration and hydration),
which time out (502/503/504) on very large repositories at a normal batch size. On such a
failure the batch is halved and each half retried independently, recursively down to a single
commit; a singleton that still fails is handed to ``on_singleton_fail`` (which records a gap)
rather than crashing the archive.
"""

from __future__ import annotations

from dataclasses import dataclass
from typing import Callable, Iterable, Sequence


@dataclass
class SplitStats:
    batches_ok: int = 0
    splits: int = 0
    singleton_failures: int = 0
    min_batch: int | None = None

    def _record_ok(self, n: int) -> None:
        self.batches_ok += 1
        self.min_batch = n if self.min_batch is None else min(self.min_batch, n)


def split_retry(
    items: Sequence,
    size: int,
    run_batch: Callable[[list], None],
    on_singleton_fail: Callable[[object], None],
    *,
    gateway_errors: tuple[type[BaseException], ...],
    notify: Callable[[str], None] | None = None,
) -> SplitStats:
    stats = SplitStats()
    work: list[list] = [
        list(items[i : i + size]) for i in range(0, len(items), max(size, 1))
    ]
    work.reverse()  # pop() takes the last -> preserves input order

    while work:
        chunk = work.pop()
        if not chunk:
            continue
        try:
            run_batch(chunk)
            stats._record_ok(len(chunk))
        except gateway_errors as exc:
            if len(chunk) == 1:
                stats.singleton_failures += 1
                stats.min_batch = 1
                on_singleton_fail(chunk[0])
                continue
            mid = len(chunk) // 2
            stats.splits += 1
            if notify:
                notify(
                    f"batch of {len(chunk)} failed ({type(exc).__name__}); "
                    f"splitting into {mid} + {len(chunk) - mid}"
                )
            work.append(chunk[mid:])   # older half
            work.append(chunk[:mid])   # newer half, processed next
    return stats


def chunks(seq: Iterable, size: int):
    seq = list(seq)
    for i in range(0, len(seq), max(size, 1)):
        yield seq[i : i + size]
