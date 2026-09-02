import argparse
import os
import json

from patchguru.execution.DockerExecutor import DockerExecutor
from patchguru.utils.PullRequest import PullRequest
from patchguru.analysis.PRRetriever import get_repo
from patchguru.utils.ResultsLayout import iter_function_dirs, load_phase_results
import time

# Matches SpecInfer.py's real cache_dir (Config.CACHE_DIR + "oracles"); the
# old ".cache/RQ2/" constant here pointed at a tree nothing writes to.
CACHE_DIR = ".cache/oracles/"


def _find_bug_result_path(repo_name: str, pr_number: int, function: str = None):
    """Locate a BUG-concluded results.json for a PR.

    A PR now runs one independent oracle per modified function (see
    utils/ResultsLayout.py), so every function subdirectory is searched
    (phase2 preferred over phase1, matching the prior single-function lookup
    order) unless `function` narrows the search to one. Returns
    (result_path, result_data) for the first BUG match, or (None, None).
    """
    pr_dir = os.path.join(CACHE_DIR, repo_name, str(pr_number))
    candidates = list(iter_function_dirs(pr_dir))
    if function is not None:
        candidates = [(fct_name, fct_dir) for fct_name, fct_dir in candidates if fct_name == function]

    for _fct_name, fct_dir in candidates:
        for phase in ("phase2", "phase1"):
            data = load_phase_results(fct_dir, phase)
            if data is not None and data.get("review_conclusion") == "BUG":
                result_path = (
                    os.path.join(fct_dir, "phase2", "results.json")
                    if phase == "phase2"
                    else os.path.join(fct_dir, "results.json")
                )
                return result_path, data
    return None, None


def replay(repo_name: str, pr_number: int, timeout: int, function: str = None):
    result_path, result_data = _find_bug_result_path(repo_name, pr_number, function)
    assert result_path is not None, (
        f"No BUG-concluded result found for PR {pr_number} in repo {repo_name}"
        f"{' function=' + function if function else ''}"
    )
    print(result_path)

    review_conclusion = result_data.get("review_conclusion", None)
    assert review_conclusion == "BUG"
    review_reasoning = result_data.get("review_reasoning", "")
    with open("viewer/viewer.txt", "w") as f:
        f.write(f"Review Conclusion: {review_conclusion}\n")
        f.write(f"Review Reasoning: \n{review_reasoning}\n")

    print(f"Replaying bug triggering process for PR {pr_number} in repo {repo_name}")
    print(f"You can check bug explaination in viewer/viewer.txt")

    github_repo, cloned_repo_manager = get_repo(args.repo)
    github_pr = github_repo.get_pull(args.pr)
    pr = PullRequest(github_pr, github_repo, cloned_repo_manager)
    commit = pr.pre_commit
    print(f"Using pre-change commit: {commit}")
    print(f"post-change commit: {pr.post_commit}")
    cloned_repo = cloned_repo_manager.get_cloned_repo(pr.pre_commit, pr.post_commit)
    container_name = cloned_repo.container_name
    docker_executor = DockerExecutor(container_name)

    spec_path = result_path.replace("results.json", "specification.py")
    print(f"Replaying bug triggering code from specification: {spec_path}")

    while True:
        start_time = time.time()
        with open(spec_path, "r") as f:
            code = f.read()
        exit_code, output = docker_executor.execute_python_code(code, timeout=timeout)
        print(output)
        print(f"Time taken for import: {time.time() - start_time} seconds")
        input("Press Enter to re-run the code...")

if __name__ == "__main__":
    parser = argparse.ArgumentParser(description="Replay bug triggering process in a Docker container for a given PR.")
    parser.add_argument("--project", required=True, help="Repository name (e.g., keras)")
    parser.add_argument("--pr", type=int, required=True, help="Pull request number")
    parser.add_argument("--timeout", type=int, default=600, help="Timeout for code execution (seconds)")
    parser.add_argument("--function", default=None,
                        help="Modified function name to replay (default: search all functions in the PR for a BUG)")
    args = parser.parse_args()
    replay(args.project, args.pr, args.timeout, args.function)
