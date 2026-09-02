#!/usr/bin/env python3
"""List all PRs where PatchGuru found a BUG warning.

Usage:
    python3 -m patchguru.experiments.list_bugs
    python3 -m patchguru.experiments.list_bugs --projects marshmallow,pandas
    python3 -m patchguru.experiments.list_bugs --detail
    python3 -m patchguru.experiments.list_bugs --dump marshmallow:2731
    python3 -m patchguru.experiments.list_bugs --dump pandas:66200 --phase phase2
"""

import argparse
import json
import os

from patchguru.utils.ResultsLayout import iter_function_dirs, load_json, load_phase_results

# Max chars of the execution error message to keep. Build/install logs prefix
# the real AssertionError traceback; we keep only the tail so the verifier
# sees the actual failing assertion and traceback, not pip-install noise.
_ERROR_TAIL_CHARS = 2000


def _load_results(path):
    """Load a results.json file, returning None if missing or invalid."""
    return load_json(path)


def _is_bug(data):
    return (
        data is not None
        and data.get("stage") == "completed"
        and data.get("review_conclusion") == "BUG"
    )


def _last_error_message(data, tail_chars=_ERROR_TAIL_CHARS):
    """Return the tail of the last execution_status error_message (the real traceback)."""
    status = data.get("execution_status") or [{}]
    if not status:
        return ""
    msg = status[-1].get("error_message", "") or ""
    return msg[-tail_chars:]


def dump_bug_context(project, pr_nb, phase="phase1", function=None):
    """Print the full verification context for a single BUG conclusion as JSON.

    ``phase`` is ``phase1`` (results.json) or ``phase2`` (phase2/results.json).
    A PR's oracle runs now live one per modified function (see
    ``utils/ResultsLayout.py``), so when ``function`` is not given, every
    function subdirectory is searched for a BUG conclusion in the requested
    phase and the first match is dumped. Exits non-zero (with a message on
    stderr) if no BUG conclusion is found anywhere for that PR/phase.
    """
    pr_dir = f".cache/oracles/{project}/{pr_nb}"

    candidates = list(iter_function_dirs(pr_dir))
    if function is not None:
        candidates = [(fct_name, fct_dir) for fct_name, fct_dir in candidates if fct_name == function]
        if not candidates:
            raise SystemExit(f"No function '{function}' found in manifest.json under {pr_dir}.")

    for fct_name, fct_dir in candidates:
        data = load_phase_results(fct_dir, phase)
        if _is_bug(data):
            res_path = os.path.join(fct_dir, "results.json" if phase == "phase1" else os.path.join("phase2", "results.json"))
            exec_status = data.get("execution_status") or [{}]
            context = {
                "project": project,
                "pr_nb": str(pr_nb),
                "function": fct_name,
                "phase": phase,
                "cache_dir": fct_dir,
                "results_path": res_path,
                "review_conclusion": data.get("review_conclusion"),
                "review_reasoning": data.get("review_reasoning", ""),
                "specification": data.get("specification", ""),
                "intent_analysis_reasoning": data.get("reasoning", ""),
                "hypothesis": data.get("hypothesis", ""),
                "error_message": _last_error_message(data),
                "execution_exit_code": exec_status[-1].get("exit_code") if exec_status else None,
                "llm_queries": data.get("llm_queries"),
                "review_traces": data.get("review_traces", []),
            }
            print(json.dumps(context, indent=2))
            return

    raise SystemExit(
        f"No BUG conclusion found under {pr_dir} for phase={phase}"
        f"{' function=' + function if function else ''}."
    )


DEFAULT_PROJECTS = ["marshmallow", "pandas", "scipy", "keras"]

_MAPPING = {
    "pandas": "Pandas",
    "scipy": "SciPy",
    "keras": "Keras",
    "marshmallow": "Marshmallow",
}


def _enumerate_bugs(project):
    """Return a list of (pr_nb, function_name, phase, error_message) for every
    BUG conclusion in a project, across every modified function's oracle run."""
    result_dir = f".cache/oracles/{project}"
    bugs = []
    if not os.path.isdir(result_dir):
        return bugs
    for pr_nb in sorted(os.listdir(result_dir), key=_sort_key):
        if not pr_nb.isdigit():
            continue
        pr_dir = os.path.join(result_dir, pr_nb)
        for fct_name, fct_dir in iter_function_dirs(pr_dir):
            data = load_phase_results(fct_dir, "phase1")
            if _is_bug(data):
                bugs.append((pr_nb, fct_name, "phase1", _last_error_message(data)[:120]))
            data = load_phase_results(fct_dir, "phase2")
            if _is_bug(data):
                bugs.append((pr_nb, fct_name, "phase2", _last_error_message(data)[:120]))
    return bugs


def _sort_key(name):
    """Sort PR directory names numerically when possible."""
    return (0, int(name)) if name.isdigit() else (1, name)


def main():
    parser = argparse.ArgumentParser(description="List PRs where PatchGuru found a BUG warning")
    parser.add_argument("--projects", default=",".join(DEFAULT_PROJECTS),
                        help="Comma-separated project names (default: all)")
    parser.add_argument("--detail", action="store_true",
                        help="Show error message for each BUG")
    parser.add_argument("--dump", metavar="PROJECT:PR",
                        help="Print full verification context JSON for a single BUG conclusion")
    parser.add_argument("--phase", default="phase1", choices=["phase1", "phase2"],
                        help="Which phase to dump (only used with --dump; default: phase1)")
    parser.add_argument("--function", default=None,
                        help="Modified function name to dump (only used with --dump; "
                             "default: search all functions in the PR for a BUG)")
    args = parser.parse_args()

    # --dump: emit full context for one BUG conclusion and exit.
    if args.dump:
        if ":" not in args.dump:
            raise SystemExit("Invalid --dump value; expected PROJECT:PR (e.g. marshmallow:2731).")
        project, pr_nb = args.dump.split(":", 1)
        dump_bug_context(project.strip(), pr_nb.strip(), phase=args.phase, function=args.function)
        return

    projects = [p.strip() for p in args.projects.split(",") if p.strip()]

    for project in projects:
        bugs = _enumerate_bugs(project)
        if not bugs and not os.path.isdir(f".cache/oracles/{project}"):
            print(f"[{project}] No cache directory found at .cache/oracles/{project}")
            continue

        if bugs:
            label = _MAPPING.get(project, project)
            print(f"\n=== {label} ({len(bugs)} BUG{'S' if len(bugs) > 1 else ''}) ===")
            for pr_nb, fct_name, phase, msg in bugs:
                if args.detail:
                    print(f"  PR #{pr_nb} ({fct_name or '<single-function>'}, {phase})")
                    print(f"      {msg}")
                else:
                    print(f"  PR #{pr_nb} ({fct_name or '<single-function>'})")
        else:
            label = _MAPPING.get(project, project)
            print(f"\n=== {label} (0 BUGS) ===")


if __name__ == "__main__":
    main()