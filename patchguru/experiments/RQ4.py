import os
import json

from patchguru.utils.ResultsLayout import iter_function_dirs


def first_review_conclusion(results) -> str | None:
    """Return the conclusion of the first assertion review, or None if no
    review ran. Reads the first entry of results["review_traces"].

    Falls back to the top-level ``review_conclusion`` for pre-refactor
    cached results that predate ``review_traces``; a ``NORMAL`` conclusion
    (no assertion review ran) yields ``None`` so it is not counted as a
    warning.
    """
    traces = results.get("review_traces")
    if isinstance(traces, list) and traces:
        return traces[0].get("conclusion")
    conclusion = results.get("review_conclusion")
    if conclusion in ("BUG", "MISMATCH"):
        return conclusion
    return None


def _pr_review_conclusion(pr_dir) -> str | None:
    """Aggregate a PR's per-function review conclusions into one PR-level
    conclusion (a PR now runs one independent oracle per modified function,
    see utils/ResultsLayout.py). Mirrors the BUG-if-any policy used by
    effectiveness.py/efficiency.py's ``summarize_pr_outcome``: the PR counts
    as a BUG warning if any function's review concluded BUG, else a MISMATCH
    warning if any function's review concluded MISMATCH, else no warning.
    """
    saw_mismatch = False
    for _fct_name, fct_dir in iter_function_dirs(pr_dir):
        # Phase 1 review happens first, so it determines the conclusion when
        # present; otherwise fall back to the Phase 2 review.
        conclusion = None
        results_path = os.path.join(fct_dir, "results.json")
        if os.path.exists(results_path):
            with open(results_path, "r") as f:
                conclusion = first_review_conclusion(json.load(f))

        if conclusion is None:
            phase2_path = os.path.join(fct_dir, "phase2", "results.json")
            if os.path.exists(phase2_path):
                with open(phase2_path, "r") as f:
                    conclusion = first_review_conclusion(json.load(f))

        if conclusion == "BUG":
            return "BUG"
        if conclusion == "MISMATCH":
            saw_mismatch = True

    return "MISMATCH" if saw_mismatch else None


def analyze(project):
    result_dir = f".cache/oracles/{project}"
    data_id_path = f".cache/pr_ids/{project}.txt"
    with open(data_id_path, "r") as f:
        data_ids = [line.strip() for line in f]

    n_warnings = 0
    n_bug = 0
    n_mismatch = 0

    for data_id in data_ids:
        pr_dir = os.path.join(result_dir, data_id)
        conclusion = _pr_review_conclusion(pr_dir)

        if conclusion is None:
            continue  # No assertion review ran for this PR

        n_warnings += 1
        if conclusion == "BUG":
            n_bug += 1
        elif conclusion == "MISMATCH":
            n_mismatch += 1
        else:
            raise ValueError(
                f"Unexpected review conclusion for Data ID {data_id}: {conclusion}"
            )

    return n_warnings, n_bug, n_mismatch


if __name__ == "__main__":
    _MAPPING = {
        "pandas": "Pandas",
        "scipy": "SciPy",
        "keras": "Keras",
        "marshmallow": "Marshmallow"
    }

    n_warnings = 0
    n_bugs = 0
    n_mismatches = 0
    n_tp_in_bug = 0
    n_fp_in_bug = 0
    n_tp_in_mismatch = 0
    n_fp_in_mismatch = 0
    for project in ["pandas", "scipy", "keras", "marshmallow"]:
        project_name = _MAPPING[project]
        project_warnings, project_bugs, project_mismatches = analyze(project)
        n_warnings += project_warnings
        n_bugs += project_bugs
        n_mismatches += project_mismatches
        with open(f".cache/manual_annotation/RQ4/{project}/bug_cases.txt", "r") as f:
            for line in f:
                splitted_line = line.strip().split()
                label = splitted_line[1]
                if label == "TP":
                    n_tp_in_bug += 1
                elif label == "FP":
                    n_fp_in_bug += 1
                else:
                    assert False, f"Invalid label in bug_cases.txt for project {project}: {line}"
        with open(f".cache/manual_annotation/RQ4/{project}/mismatch_cases.txt", "r") as f:
            for line in f:
                splitted_line = line.strip().split()
                label = splitted_line[1]
                if label == "TP":
                    n_tp_in_mismatch += 1
                elif label == "FP":
                    n_fp_in_mismatch += 1
                else:
                    assert False, f"Invalid label in mismatch_cases.txt for project {project}: {line}"

    print(f"Total number of warnings across all projects: {n_warnings}")
    print(f"Total number of bugs identified across all projects: {n_bugs}")
    print(f"Total number of mismatches identified across all projects: {n_mismatches}")
    print(f"Number of true positives in bug cases: {n_tp_in_bug}")
    print(f"Number of false positives in bug cases: {n_fp_in_bug}")
    print(f"Number of true positives in mismatch cases: {n_tp_in_mismatch}")
    print(f"Number of false positives in mismatch cases: {n_fp_in_mismatch}")
    estimated_tp = n_tp_in_bug + n_tp_in_mismatch/20 * (n_mismatches)
    estimated_fp = n_fp_in_bug + n_fp_in_mismatch/20 * (n_mismatches)
    print(f"Estimated true positives across all warnings: {estimated_tp}")
    print(f"Estimated false positives across all warnings: {estimated_fp}")
    print(f"Estimated precision: {estimated_tp / (estimated_tp + estimated_fp):.2f}")
