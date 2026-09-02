"""Effectiveness analysis — RQ1 of the paper (Table 1).

Classifies each analyzed PR into an outcome — BUG warning, NORMAL, or
pipeline failure — from its cached ``results.json`` (and the nested
``phase2/`` results when Phase 2 ran). It then reports, per project:

* ``#Warnings`` — PRs where the review concluded ``BUG`` (potential bugs)
* ``#Normal`` — PRs where the review concluded ``NORMAL``
* ``#Oracles``  = warnings + normal
* ``#Failures`` — PRs whose pipeline failed
* ``Precision``  — TP / (TP + FP) from the human-annotated
  ``.cache/WarningAnnotation.xlsx`` when present
* a tally of failure reasons

This is split from the former ``RQ1_3.py``; cost/time analysis now lives
in ``efficiency.py`` (RQ3).

Usage:
    python3 -m patchguru.experiments.effectiveness                          # default projects
    python3 -m patchguru.experiments.effectiveness --projects pandas,scipy
"""

from __future__ import annotations

import argparse
import json
import os

import pandas as pd

from patchguru.utils.ResultsLayout import (
    iter_function_dirs,
    load_phase_results,
    safe_function_dirname,
    summarize_pr_outcome,
)

# Default projects to analyze (mirrors the original hardcoded RQ1_3 list).
DEFAULT_PROJECTS = ["marshmallow"]

# Display names used in the summary table.
_MAPPING = {
    "pandas": "Pandas",
    "scipy": "SciPy",
    "keras": "Keras",
    "marshmallow": "Marshmallow",
}


def _read_usage(usage_path):
    """Read a per-PR llm_usage.jsonl file into a list of usage records."""
    records = []
    if not os.path.exists(usage_path):
        return records
    with open(usage_path, "r") as f:
        for line in f:
            if not line.strip():
                continue
            try:
                entry = json.loads(line)
            except json.JSONDecodeError:
                continue
            if "completion_tokens" in entry or "prompt_tokens" in entry:
                records.append(entry)
    return records


def parse_results(project):
    """Build per-PR, per-function result + usage records from
    `.cache/oracles/<project>/`. A PR now runs one independent oracle per
    modified function (see utils/ResultsLayout.py), so each entry holds a
    "functions" dict keyed by function name (or None for pre-multi-function
    single-function cache trees) instead of a single flat results.json."""
    result_dir = f".cache/oracles/{project}"
    results = {}
    if not os.path.isdir(result_dir):
        return results
    for data_id in os.listdir(result_dir):
        pr_dir = os.path.join(result_dir, data_id)
        functions = {}
        for fct_name, fct_dir in iter_function_dirs(pr_dir):
            data = load_phase_results(fct_dir, "phase1")
            if data is None:
                continue
            functions[fct_name] = {
                "results": data,
                "phase2_results": load_phase_results(fct_dir, "phase2"),
                "llm_usage": _read_usage(os.path.join(fct_dir, "llm_usage.jsonl")),
                "phase2_usage": _read_usage(os.path.join(fct_dir, "phase2", "llm_usage.jsonl")),
                "fct_dir": fct_dir,
            }
        if not functions:
            continue
        results[str(data_id)] = {
            "functions": functions,
            "log_dir": pr_dir,
        }
    return results


def analyze(project):
    """Classify every PR's outcome and tally warnings / normals / failures.

    A PR now runs one independent oracle per modified function
    (utils/ResultsLayout.py), so its outcome is aggregated across every
    function's phase1/phase2 run via ``summarize_pr_outcome``'s BUG-if-any
    policy: a PR counts as a Warning if ANY function found a BUG, else
    Normal if every function that ran completed NORMAL, else a Failure if
    any function's run failed or never reached a terminal stage.

    Returns ``(summary, failure_reasons, incompleted_prs, bug_found_prs)``.
    """
    result_dir = f".cache/oracles/{project}"
    data_id_path = f"data/pr_data/prs/{project}.txt"
    with open(data_id_path, "r") as f:
        data_ids = [line.strip() for line in f]
    n_warnings = 0
    n_normal_cases = 0
    n_failures = 0
    incompleted_prs = []
    bug_found_prs = []

    log_results = parse_results(project)
    failure_reasons = {}
    for data_id in data_ids:
        pr_dir = os.path.join(result_dir, data_id)
        outcome = summarize_pr_outcome(pr_dir)

        if outcome["outcome"] == "missing":
            incompleted_prs.append(data_id)
            continue

        if outcome["outcome"] == "BUG":
            n_warnings += 1
            for fct_name, phase, data in outcome["bug_instances"]:
                bug_dir = pr_dir if fct_name is None else os.path.join(pr_dir, safe_function_dirname(fct_name))
                bug_found_prs.append((
                    bug_dir if phase == "phase1" else os.path.join(bug_dir, "phase2"),
                    data["execution_status"][-1]["error_message"],
                ))
        elif outcome["outcome"] == "NORMAL":
            n_normal_cases += 1
        else:  # "FAILED"
            n_failures += 1
            reason = outcome["failure_reason"] or "query_error"
            failure_reasons[reason] = failure_reasons.get(reason, 0) + 1

        if outcome["outcome"] in ("BUG", "NORMAL") and data_id in log_results:
            total_usage = sum(
                len(fdata["llm_usage"]) + len(fdata["phase2_usage"])
                for fdata in log_results[data_id]["functions"].values()
            )
            if total_usage != outcome["llm_queries"]:
                print(f"Warning: LLM queries mismatch for PR {data_id}: log {total_usage}, result {outcome['llm_queries']}")

    summary = {
        "Total PRs": len(data_ids),
        "#Warnings": n_warnings,
        "#Normal": n_normal_cases,
        "#Oracles": n_warnings + n_normal_cases,
        "#Failures": n_failures,
    }

    return summary, failure_reasons, incompleted_prs, bug_found_prs


def apply_annotations(summary: dict) -> dict:
    """Fold human TP/FP annotations from WarningAnnotation.xlsx into summary.

    Adds ``#TP``, ``#FP``, and ``Precision`` per project when the
    annotation file exists. Returns the summary unchanged otherwise.
    """
    if not os.path.exists(".cache/WarningAnnotation.xlsx"):
        return summary
    for project in summary.keys():
        # Load sheet for the project
        project_df = pd.read_excel(".cache/WarningAnnotation.xlsx", sheet_name=project.capitalize())
        n_fp = 0
        n_tp = 0
        for idx, row in project_df.iterrows():
            if pd.isna(row["PR"]):
                continue
            if row["FP"] == 1:
                n_fp += 1
            if row["TP"] == 1:
                n_tp += 1
        assert n_tp + n_fp == summary[project]["#Warnings"], f"Mismatch in warnings for project {project}, summary {summary[project]['#Warnings']}, annotated {n_tp + n_fp}"
        summary[project]["#TP"] = n_tp
        summary[project]["#FP"] = n_fp
        summary[project]["Precision"] = round(n_tp / (n_tp + n_fp), 2) if (n_tp + n_fp) > 0 else 0.0
    return summary


def main():
    parser = argparse.ArgumentParser(description="Effectiveness: RQ1 of PatchGuru (Table 1)")
    parser.add_argument(
        "--projects",
        default=",".join(DEFAULT_PROJECTS),
        help="Comma-separated project names to analyze (default: %(default)s)",
    )
    args = parser.parse_args()

    projects = [p.strip() for p in args.projects.split(",") if p.strip()]

    summary = {}
    failure_reasons = {}
    for project in projects:
        project_name = _MAPPING.get(project, project)
        project_summary, project_failure_reasons, _, _ = analyze(project)
        summary[project_name] = project_summary
        for reason, count in project_failure_reasons.items():
            failure_reasons[reason] = failure_reasons.get(reason, 0) + count

    summary = apply_annotations(summary)

    # Print table summary
    print("--------------- RQ1: Effectiveness of PatchGuru (Table 1) ---------------")
    df_summary = pd.DataFrame.from_dict(summary, orient="index")
    print(df_summary)

    print("\nFailure Reasons Summary:")
    for reason, count in failure_reasons.items():
        print(f"{reason}: {count}")


if __name__ == "__main__":
    main()