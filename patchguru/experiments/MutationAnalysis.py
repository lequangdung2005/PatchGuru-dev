import os
import json
from patchguru.utils.PullRequest import PullRequest
from patchguru.analysis.PRRetriever import get_repo
from patchguru.execution.DockerExecutor import DockerExecutor
from patchguru.utils.CodeMutation import generate_mutants, beautify_code
from patchguru.utils.PythonCodeUtil import update_function_name
from patchguru.utils.ResultsLayout import iter_function_dirs
import termcolor
import argparse
import difflib
from hashlib import blake2b

def decorate(text, color=None, on_color=None, attrs=None):
    return termcolor.colored(text, color, on_color, attrs)

class MultiFunctionError(Exception):
    """Raised when a PR modifies more than one function."""
    pass


def extract_fut_code(fut_info, fct_name, pre_fix=None):
    fct_info = fut_info[fct_name]
    code = fct_info['code']
    only_name = fct_name.split('.')[-1]
    if pre_fix:
        code = update_function_name(code, only_name, f"{pre_fix}{only_name}")
    return code


def _resolve_single_fct_name(pr):
    """Recover the sole modified function's name for old, pre-multi-function
    cache trees that have no manifest.json to name it directly."""
    modified = [name for name in pr.prev_fut_info if name in pr.post_fut_info]
    if len(modified) != 1:
        raise MultiFunctionError(
            f"Only single function change is supported without a manifest.json. "
            f"Found {len(modified)} functions: {modified}"
        )
    return modified[0]


def do_mutation(spec_path, result_dir, github_repo, pr_id, cloned_repo_manager, repo_name, fct_name=None):
    os.makedirs(result_dir, exist_ok=True)
    with open(spec_path, "r") as f:
        spec = f.read()

    github_pr = github_repo.get_pull(pr_id)
    pr = PullRequest(github_pr, github_repo, cloned_repo_manager)
    commit = pr.pre_commit
    cloned_repo = cloned_repo_manager.get_cloned_repo(pr.pre_commit, pr.post_commit)
    container_name = cloned_repo.container_name
    docker_executor = DockerExecutor(container_name)

    try:
        if fct_name is None:
            fct_name = _resolve_single_fct_name(pr)
        post_fut_code_without_prefix = extract_fut_code(pr.post_fut_info, fct_name)
        pre_fut_code_without_prefix = extract_fut_code(pr.prev_fut_info, fct_name)
    except MultiFunctionError as e:
        print(f"Skipping PR {pr_id}: {e}")
        return
    post_fut_code_without_prefix = beautify_code(post_fut_code_without_prefix)
    pre_fut_code_without_prefix = beautify_code(pre_fut_code_without_prefix)
    pre_lines = pre_fut_code_without_prefix.splitlines()
    post_lines = post_fut_code_without_prefix.splitlines()
    matcher = difflib.SequenceMatcher(None, pre_lines, post_lines)
    added_lines = set()
    for tag, _i1, _i2, j1, j2 in matcher.get_opcodes():
        if tag in ('insert', 'replace'):
            for j in range(j1, j2):
                line_text = post_lines[j]
                if len(line_text.strip()) > 1:
                    added_lines.add((j, line_text))

    post_fut_code = beautify_code(extract_fut_code(pr.post_fut_info, fct_name, pre_fix="post_"))
    pre_fut_code = beautify_code(extract_fut_code(pr.prev_fut_info, fct_name, pre_fix="pre_"))


    before = spec.split("## Before Pull Request")[0]
    after = spec.split("# Specification")[1]
    spec = f"{before}## Before Pull Request\n{pre_fut_code}\n## After Pull Request\n{post_fut_code}\n# Formal Specification{after}"
    n_mutant_pass = 0
    n_mutant_fail_assert = 0
    n_mutant_fail_other = 0
    try:
        mutants = generate_mutants(post_fut_code)
    except Exception as e:
        print(f"Error generating mutants for PR {pr_id}: {e}")
        return
    relevant_mutants = []

    if os.path.exists(os.path.join(result_dir, "mutation_results.json")):
        # Load existing results to avoid re-computation
        with open(os.path.join(result_dir, "mutation_results.json"), "r") as f:
            existing_results = json.load(f)
        execution_results = existing_results.get("execution_results", {})
        n_mutant_pass = existing_results.get("n_mutant_pass", 0)
        n_mutant_fail_assert = existing_results.get("n_mutant_fail_assert", 0)
        n_mutant_fail_other = existing_results.get("n_mutant_fail_other", 0)
    else:
        execution_results = {}
    for idx, mutant in enumerate(mutants):
        post_lines_with_prefix = post_fut_code.splitlines()
        mutant_lines = mutant.splitlines()
        matcher2 = difflib.SequenceMatcher(None, post_lines_with_prefix, mutant_lines)
        removed_lines = set()
        for tag, i1, i2, _j1, _j2 in matcher2.get_opcodes():
            if tag in ('delete', 'replace'):
                for i in range(i1, i2):
                    line_text = post_lines_with_prefix[i]
                    if len(line_text.strip()) > 1:
                        removed_lines.add((i, line_text))

        is_relevant = bool({i for i, _ in removed_lines} & {i for i, _ in added_lines})
        if is_relevant:
            hash_id = blake2b(mutant.encode()).hexdigest()
            if hash_id in execution_results:
                print(f"Skipping already tested mutant {idx+1}/{len(mutants)} for PR {pr_id}")
                continue  # Skip already tested mutants
            relevant_mutants.append(mutant)
            print(f"Testing mutant {idx+1}/{len(mutants)} for PR {pr_id}")
            before = spec.split("## After Pull Request")[0]
            after = spec.split("# Formal Specification")[1]
            mutated_spec = f"{before}## After Pull Request\n{mutant}\n# Formal Specification{after}"
            exit_code, output = docker_executor.execute_python_code(mutated_spec, timeout=300)
            print(output)
            print("-" * 40)
            mutant_file_name = f"mutant_{idx+1}_fail.py"
            if exit_code == 0:
                mutant_file_name = f"mutant_{idx+1}_pass.py"
                n_mutant_pass += 1
            else:
                if "AssertionError" in output:
                    mutant_file_name = f"mutant_{idx+1}_assert.py"
                    n_mutant_fail_assert += 1
                else:
                    n_mutant_fail_other += 1
            with open(os.path.join(result_dir, mutant_file_name), "w") as f:
                    f.write(mutated_spec)
            execution_results[hash_id] = {
                "exit_code": exit_code,
                "output": output,
            }
    # Save mutation results
    mutation_results = {
        "total_mutants": len(mutants),
        "relevant_mutants": len(relevant_mutants),
        "execution_results": execution_results,
        "n_mutant_pass": n_mutant_pass,
        "n_mutant_fail_assert": n_mutant_fail_assert,
        "n_mutant_fail_other": n_mutant_fail_other,
    }
    with open(os.path.join(result_dir, "mutation_results.json"), "w") as f:
        json.dump(mutation_results, f, indent=4)

def main(repo_name, analysis_result_dir):
    pr_ids_file = f"data/pr_data/prs/{repo_name}.txt"
    target_pr_ids = []
    with open(pr_ids_file) as f:
        for line in f:
            target_pr_ids.append(int(line.strip()))
    target_pr_ids = sorted(target_pr_ids)
    github_repo, cloned_repo_manager = get_repo(repo_name)
    for pr_id in target_pr_ids:
        pr_dir = os.path.join(analysis_result_dir, str(pr_id))
        # A PR now runs one independent oracle per modified function (see
        # utils/ResultsLayout.py); each function gets its own subdirectory
        # under pr_dir, so mutation analysis runs once per function too.
        # `fct_name` is None for old, pre-multi-function flat cache trees.
        for fct_name, fct_dir in iter_function_dirs(pr_dir):
            fct_label = fct_name.replace(".", "__") if fct_name else None
            result_pr_dir = (
                os.path.join(".cache", "mutation_testing", repo_name, "patchguru", str(pr_id))
                if fct_label is None
                else os.path.join(".cache", "mutation_testing", repo_name, "patchguru", str(pr_id), fct_label)
            )

            phase1_spec_path = os.path.join(fct_dir, "specification.py")
            phase2_spec_path = os.path.join(fct_dir, "phase2", "specification.py")

            if os.path.exists(phase1_spec_path):
                spec_path = phase1_spec_path
                result_dir = result_pr_dir
                analysis_result_path = os.path.join(fct_dir, "results.json")
                with open(analysis_result_path) as f:
                    analysis_result = json.load(f)
                if "stage" not in analysis_result or analysis_result["stage"] != "completed":
                    print(f"Skipping PR {pr_id} ({fct_name or 'single-function'}) mutation analysis due to incomplete analysis.")
                    continue
                do_mutation(spec_path, result_dir, github_repo, pr_id, cloned_repo_manager, repo_name, fct_name)

            if os.path.exists(phase2_spec_path):
                spec_path = phase2_spec_path
                result_dir = os.path.join(result_pr_dir, "phase2")
                analysis_result_path = os.path.join(fct_dir, "phase2", "results.json")
                with open(analysis_result_path) as f:
                    analysis_result = json.load(f)
                if "stage" not in analysis_result or analysis_result["stage"] != "completed":
                    print(f"Skipping PR {pr_id} ({fct_name or 'single-function'}) phase2 mutation analysis due to incomplete analysis.")
                    continue
                do_mutation(spec_path, result_dir, github_repo, pr_id, cloned_repo_manager, repo_name, fct_name)


if __name__ == "__main__":
    parser = argparse.ArgumentParser(description="Mutation analysis for a given repo")
    parser.add_argument("--repo", type=str, required=True, help="Repository name (e.g., pandas)")
    parser.add_argument("--result_dir", type=str, default=None, help="Directory containing PatchGuru analysis results (default: .cache/oracles/<repo>)")
    args = parser.parse_args()
    result_dir = args.result_dir or os.path.join(".cache", "oracles", args.repo)
    main(args.repo, result_dir)
