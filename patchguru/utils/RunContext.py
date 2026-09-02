"""Per-PR run context and LLM usage recording.

Replaces the former global event-log mechanism (``utils/Tracker.py``).
Instead of one cross-PR log directory per process, tracking now lives
inside each PR's cache directory (``.cache/oracles/<project>/<pr_nb>/``):

* ``llm_usage.jsonl`` — one JSON record per LLM call, appended at call
  time (crash-safe — survives a hard kill mid-stage).
* ``results.json`` — structured fields ``start_time`` / ``end_time`` /
  ``failure_reason`` (stamped by :func:`SpecInfer.save_results_to_cache`
  and the failure sites).

The LLM backends are PR-agnostic; they discover the active PR via a
``contextvars`` token pushed by :func:`run_scope` in
``SpecInfer.analyze`` / ``spec_infer`` / ``spec_generalization``.
"""

from __future__ import annotations

import contextvars
import json
import os
import time
from contextlib import contextmanager
from dataclasses import dataclass
from typing import Iterator, Optional

from patchguru import Config

_TS_FMT = "%Y%m%d-%H%M%S"


@dataclass(frozen=True)
class Run:
    cache_dir: str
    pr_nb: int
    start_time: str


_current: contextvars.ContextVar[Optional[Run]] = contextvars.ContextVar(
    "patchguru_run", default=None
)


def current() -> Optional[Run]:
    """The active run, or ``None`` when called outside a ``run_scope``."""
    return _current.get()

@contextmanager
def run_scope(cache_dir: str, pr_nb: int, reset_usage: bool = False) -> Iterator[Run]:
    """Push a run record for ``cache_dir`` / ``pr_nb``.

    Captures ``start_time`` at entry so it survives across stage
    checkpoints and resumes. Nesting-safe: when a ``run_scope`` is opened
    inside another one that targets the *same* ``cache_dir`` (e.g.
    ``spec_infer`` running inside ``analyze``), the outer ``Run`` is reused
    as-is — ``start_time`` is not re-stamped and usage tracking continues
    against the same record. A genuinely different ``cache_dir`` (e.g.
    Phase 2's ``.../phase2``) opens a fresh, independent run.

    ``reset_usage`` truncates any existing ``llm_usage.jsonl`` at entry so
    a ``--force`` re-analysis (or a retry after a crash) does not append
    duplicate records on top of a prior run's log. It is a no-op when the
    scope reuses an outer run (the outer scope already handled it).
    """
    existing = _current.get()
    if existing is not None and os.path.abspath(existing.cache_dir) == os.path.abspath(cache_dir):
        # Nested scope over the same cache dir — reuse the outer run so
        # start_time and the usage log stay consistent.
        yield existing
        return

    run = Run(cache_dir=cache_dir, pr_nb=pr_nb, start_time=time.strftime(_TS_FMT))
    if reset_usage:
        # Truncate stale usage log so --force / retries don't accumulate.
        try:
            open(os.path.join(cache_dir, "llm_usage.jsonl"), "w").close()
        except OSError:
            pass
    token = _current.set(run)
    try:
        yield run
    finally:
        _current.reset(token)


def record_llm_usage(usage: dict) -> None:
    """Append a per-call usage record to the active run's ``llm_usage.jsonl``.

    ``usage`` carries the token counts + model from the LLM backend. Only the
    model name and token counts are recorded — cost is not computed here; any
    cost analysis is derived downstream from the token counts (e.g. by the
    experiment scripts). No-op when no run is active.
    """
    run = _current.get()
    if run is None:
        return

    model = usage.get("model", Config.LLM_MODEL)
    prompt_tokens = usage.get("prompt_tokens", 0)
    completion_tokens = usage.get("completion_tokens", 0)
    thinking_tokens = usage.get("thinking_tokens", 0)
    cached_tokens = usage.get("cached_tokens", 0)
    total_tokens = usage.get("total_tokens", 0)

    record = {
        "timestamp": time.strftime(_TS_FMT),
        "model": model,
        "prompt_tokens": prompt_tokens,
        "completion_tokens": completion_tokens,
        "thinking_tokens": thinking_tokens,
        "cached_tokens": cached_tokens,
        "total_tokens": total_tokens,
    }
    usage_path = os.path.join(run.cache_dir, "llm_usage.jsonl")
    os.makedirs(run.cache_dir, exist_ok=True)
    with open(usage_path, "a") as f:
        f.write(json.dumps(record) + "\n")
