"""Shared layout helpers for locating per-function oracle results.

`SpecInfer.py::analyze()` runs one independent oracle per modified function
(Part A of the multi-function upgrade): a PR's cache_dir holds a top-level
`manifest.json` listing every modified function, while each function's real
`results.json` (and `phase2/results.json`) lives one level deeper, under
`cache_dir/<safe_function_dirname(fct_name)>/`. Every experiment/reporting
script that walks `.cache/oracles/<project>/<pr_nb>/` needs this same lookup,
so it lives here once instead of being re-derived in each script.

Old, pre-multi-function cache trees (no `manifest.json`, `results.json`
directly under the PR dir) still work: `iter_function_dirs` falls back to a
single `(None, pr_dir)` pair in that case.
"""

from __future__ import annotations

import json
import os
from typing import Any, Dict, Iterator, Optional, Tuple


def safe_function_dirname(fct_name: str) -> str:
    """Mirror SpecInfer.py::safe_function_dirname's dotted-name -> dirname rule."""
    return fct_name.replace(".", "__")


def load_json(path: str) -> Optional[dict]:
    """Load a JSON file, returning None if missing or invalid."""
    if not os.path.exists(path):
        return None
    try:
        with open(path) as f:
            return json.load(f)
    except (json.JSONDecodeError, OSError):
        return None


def iter_function_dirs(pr_dir: str) -> Iterator[Tuple[Optional[str], str]]:
    """Yield (function_name, function_cache_dir) pairs for a PR's cache_dir.

    Reads manifest.json when present (multi-function layout). Falls back to
    a single (None, pr_dir) pair for older cache trees that still have
    results.json directly under pr_dir.
    """
    manifest = load_json(os.path.join(pr_dir, "manifest.json"))
    if manifest is not None:
        for fct_name in manifest:
            yield fct_name, os.path.join(pr_dir, safe_function_dirname(fct_name))
        return

    if os.path.exists(os.path.join(pr_dir, "results.json")):
        yield None, pr_dir


def load_phase_results(fct_dir: str, phase: str = "phase1") -> Optional[dict]:
    """Load results.json (phase1) or phase2/results.json for one function dir."""
    if phase == "phase1":
        path = os.path.join(fct_dir, "results.json")
    else:
        path = os.path.join(fct_dir, "phase2", "results.json")
    return load_json(path)


def iter_pr_function_results(
    pr_dir: str,
) -> Iterator[Tuple[Optional[str], str, str, Optional[Dict[str, Any]]]]:
    """Yield (function_name, function_dir, phase, results_dict) for every
    (function, phase) combination present under a PR's cache_dir.

    phase1 is always yielded per function (results_dict is None if missing);
    phase2 is yielded only when a phase2/results.json actually exists.
    """
    for fct_name, fct_dir in iter_function_dirs(pr_dir):
        yield fct_name, fct_dir, "phase1", load_phase_results(fct_dir, "phase1")
        phase2 = load_phase_results(fct_dir, "phase2")
        if phase2 is not None:
            yield fct_name, fct_dir, "phase2", phase2


def summarize_pr_outcome(pr_dir: str) -> Dict[str, Any]:
    """Aggregate a PR's per-function oracle runs into one PR-level outcome.

    Policy (BUG-if-any, matching how a PR-level "did PatchGuru flag this PR"
    verdict should read when it can now contain several independent function
    runs): a PR is
      - "BUG" if any function run (phase1 or phase2) concluded BUG,
      - else "NORMAL" if every function that produced a results.json
        completed with review_conclusion == NORMAL,
      - else "FAILED" if any function's run failed or never reached a
        terminal 'completed' stage,
      - else "missing" if the PR has no results.json anywhere yet.

    Returns:
        {
            "outcome": "BUG" | "NORMAL" | "FAILED" | "missing",
            "llm_queries": summed llm_queries across every counted phase run,
            "bug_instances": [(fct_name, phase, results_dict), ...],
            "failure_reason": first non-empty failure_reason found, or None,
        }
    """
    bug_instances = []
    llm_queries = 0
    any_failed = False
    any_incomplete = False
    any_result = False
    failure_reason = None

    for fct_name, _fct_dir, phase, data in iter_pr_function_results(pr_dir):
        if data is None:
            if phase == "phase1":
                any_incomplete = True
            continue
        any_result = True
        stage = data.get("stage")
        if stage == "completed":
            llm_queries += data.get("llm_queries") or 0
            if data.get("review_conclusion") == "BUG":
                bug_instances.append((fct_name, phase, data))
        elif stage == "failed":
            any_failed = True
            if failure_reason is None:
                failure_reason = data.get("failure_reason")
        else:
            any_incomplete = True

    if not any_result:
        outcome = "missing"
    elif bug_instances:
        outcome = "BUG"
    elif any_failed or any_incomplete:
        outcome = "FAILED"
    else:
        outcome = "NORMAL"

    return {
        "outcome": outcome,
        "llm_queries": llm_queries,
        "bug_instances": bug_instances,
        "failure_reason": failure_reason,
    }
