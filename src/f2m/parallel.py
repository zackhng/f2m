"""Convert many files in several processes, one file per task.

Python runs one thread of Python code at a time per process, so using more
cores means more processes. Each worker gets its own copy of the Pipeline.
Files are independent, so the output is the same as a one-process run.
"""

from __future__ import annotations

import os
from collections.abc import Callable, Iterator, Sequence
from concurrent.futures import ProcessPoolExecutor, as_completed
from pathlib import Path
from typing import TYPE_CHECKING, Any

if TYPE_CHECKING:
    from f2m.pipeline import Pipeline, Result

# (source file, output folder or None, output stem)
Job = tuple[Path, "Path | None", str]

_pipe: Pipeline | None = None  # the worker's own pipeline


def default_workers() -> int:
    return os.cpu_count() or 1


def _init(pipe: Pipeline, warm: list[Path]) -> None:
    global _pipe
    _pipe = pipe
    for f in warm:  # pay one-off library costs before timed work
        pipe.convert_file(f, None)


def _task(fn: Callable[[Result], Any], source: Path, target: Path | None, stem: str) -> Any:
    assert _pipe is not None
    return fn(_pipe.convert_file(source, target, stem=stem))


def convert_many(pipe: Pipeline, jobs: Sequence[Job], workers: int, fn: Callable[[Result], Any],
                 warm: Sequence[Path] = ()) -> Iterator[tuple[int, Any]]:
    """Convert every job and yield ``(job index, fn(result))`` as each file finishes.

    ``fn`` runs in the worker and its return value is sent back, so it must be a
    module-level function (or functools.partial of one) returning picklable data.
    Results arrive in completion order, not job order, when ``workers > 1``.
    ``warm`` files are converted once per worker first, untimed.
    """
    from f2m.pipeline import Result

    if workers <= 1 or len(jobs) <= 1:
        for f in warm:
            pipe.convert_file(f, None)
        for i, (source, target, stem) in enumerate(jobs):
            yield i, fn(pipe.convert_file(source, target, stem=stem))
        return

    n = min(workers, len(jobs))
    with ProcessPoolExecutor(max_workers=n, initializer=_init, initargs=(pipe, list(warm))) as pool:
        futures = {pool.submit(_task, fn, *job): i for i, job in enumerate(jobs)}
        for fut in as_completed(futures):
            i = futures[fut]
            try:
                value = fut.result()
            except Exception as e:  # the worker process died (e.g. a crash inside a C library)
                value = fn(Result(source=jobs[i][0], error=f"worker failed: {type(e).__name__}: {e}"))
            yield i, value
