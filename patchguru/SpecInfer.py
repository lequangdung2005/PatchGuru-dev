import argparse
import json
import os
import re
import time

import github

from patchguru import Config
from patchguru.analysis.BugTrigger import generalize_spec
from patchguru.analysis.IntentAnalysis import analyze_intent
from patchguru.analysis.PRRetriever import retrieve_pr
from patchguru.analysis.TestDriverRepair import repair
from patchguru.analysis.TestDriverReview import review_test_driver
from patchguru.execution.DockerExecutor import DockerExecutor
from patchguru.llms import query_llm
from patchguru.utils.LoaderHeader import build_loader_header, hoist_future_imports
from patchguru.utils.PythonCodeUtil import get_function_signature
from patchguru.utils.RunContext import current as current_run
from patchguru.utils.RunContext import run_scope


def derive_module_path(file_path: str, pkg_name: str) -> str:
    """Convert a FUT source file path into its importable module path.

    E.g. ``"src/marshmallow/validate.py"`` with ``pkg_name="marshmallow"`` ->
    ``"marshmallow.validate"`` (and ``"marshmallow/__init__.py"`` -> ``"marshmallow"``).
    This is robust for both module-level functions and class methods (the FUT's
    qualified *name* can't be used directly, since for a method it includes the
    enclosing class, e.g. ``marshmallow.fields.Field.deserialize``).

    Args:
        file_path: ``prev_fut_info``/``post_fut_info`` ``file_path`` value.
        pkg_name:  Name of the package under test.

    Returns:
        Fully-qualified module path containing the FUT, or ``""`` if unknown.
    """
    if not file_path:
        return ""
    p = str(file_path).replace("\\", "/")
    if p.endswith(".py"):
        p = p[:-3]
    for prefix in ("src/", ""):
        base = prefix + pkg_name
        if p == base or p == base + "/__init__":
            return pkg_name
        if p.startswith(base + "/"):
            return (pkg_name + "/" + p[len(base) + 1:]).replace("/", ".")
    # Unrecognized layout: fall back to the first path segment before the package.
    return p.replace("/", ".")


def extract_pr_reference(pr):
    comments = ""
    comments += f"Comment by {pr.user.login}:\n"
    comments += f"{pr.body}\n\n"
    for comment in pr.get_issue_comments():
        new_comment = f"Comment by {comment.user.login}:\n"
        new_comment += f"{comment.body}\n\n"
        if len(comments) + len(new_comment) > 1000:
            comments += "(...truncated...)\n\n"
            break
        comments += new_comment

    review_comments = ""
    for comment in pr.get_comments():
        new_review_comment = f"Comment by {comment.user.login}:\n"
        new_review_comment += f"{comment.body}\n\n"
        if len(review_comments) + len(new_review_comment) > 1000:
            review_comments += "(...truncated...)\n\n"
            break
        review_comments += new_review_comment

    commit_messages = ""
    for commit in pr.get_commits():
        new_commit_message = f"{commit.commit.message}\n\n"
        if len(commit_messages) + len(new_commit_message) > 1000:
            commit_messages += "(...truncated...)\n\n"
            break
        commit_messages += new_commit_message

    result = "#### Reference PR #" + str(pr.number) + "\n\n"
    result += "##### Title"
    result += "#" + str(pr.number) + ": " + pr.title + "\n"
    result += "\n##### Comments\n"
    result += comments
    result += "\n##### Review comments\n"
    result += review_comments
    result += "\n##### Commit messages\n"
    result += commit_messages

    return result

def extract_issue_reference(issue):
    comments = ""
    comments += f"Comment by {issue.user.login}:\n"
    comments += f"{issue.body}\n\n"
    for comment in issue.get_comments():
        new_comment = f"Comment by {comment.user.login}:\n"
        new_comment += f"{comment.body}\n\n"
        if len(comments) + len(new_comment) > 1000:
            comments += "(...truncated...)\n\n"
            break
        comments += new_comment

    result = "#### Reference Issue #" + str(issue.number) + "\n\n"
    result += "##### Title\n"
    result += "#" + str(issue.number) + ": " + issue.title + "\n"
    result += "\n##### Comments\n"
    result += comments

    return result

def extract_references(github_repo, comments):
    # Find issue/PR references in the comments and review comments
    pattern = r"(#\d+)"
    matched_refs = re.findall(pattern, comments)
    unique_refs = list(set(matched_refs))
    unique_refs = [int(ref[1:]) for ref in unique_refs]

    result = ""
    for ref in unique_refs:
        try:
            ref_pr = github_repo.get_pull(ref)
            ref_details = extract_pr_reference(ref_pr)
            result += ref_details + "\n\n"
            continue
        except github.GithubException:
            pass

        try:
            ref_issue = github_repo.get_issue(ref)
            ref_details = extract_issue_reference(ref_issue)
            result += ref_details + "\n\n"
        except github.GithubException:
            pass
    return result

def extract_pr_details(pr, use_reference=False, github_repo= None) -> str:
    comments = ""
    comments += f"Comment by {pr.github_pr.user.login}:\n"
    comments += f"{pr.github_pr.body}\n\n"
    for comment in pr.github_pr.get_issue_comments():
        new_comment = f"Comment by {comment.user.login}:\n"
        new_comment += f"{comment.body}\n\n"
        if len(comments) + len(new_comment) > 2000:
            comments += "(...truncated...)\n\n"
            break
        comments += new_comment

    review_comments = ""
    for comment in pr.github_pr.get_comments():
        new_review_comment = f"Comment by {comment.user.login}:\n"
        new_review_comment += f"{comment.body}\n\n"
        if len(review_comments) + len(new_review_comment) > 2000:
            review_comments += "(...truncated...)\n\n"
            break
        review_comments += new_review_comment

    commit_messages = ""
    for commit in pr.github_pr.get_commits():
        new_commit_message = f"{commit.commit.message}\n\n"
        if len(commit_messages) + len(new_commit_message) > 2000:
            commit_messages += "(...truncated...)\n\n"
            break
        commit_messages += new_commit_message

    result = ""
    result += "### Title\n"
    result += "#" + str(pr.github_pr.number) + ": " + pr.github_pr.title + "\n"
    result += "\n### Comments\n"
    result += comments
    result += "\n### Review comments\n"
    result += review_comments
    result += "\n### Commit messages\n"
    result += commit_messages

    has_reference = False
    n_queries = 0
    if use_reference:
        assert github_repo is not None, "github_repo must be provided when use_reference is True"
        reference_details = extract_references(github_repo, comments)
        if Config.USE_REFERENCE_SUMMARY:
            # Create prompt to summarize references
            from patchguru.prompts.reference_summary.ReferenceSummaryPromptV1 import ReferenceSummaryPrompt
            reference_prompt = ReferenceSummaryPrompt()
            prompt = reference_prompt.create_prompt(
                pull_request_details=result,
                references=reference_details
            )
            response = query_llm(prompt, model=Config.LLM_MODEL)
            summary = reference_prompt.parse_answer(response)

            n_queries = 1
            while summary is None and n_queries < 3:
                response = query_llm(prompt, model=Config.LLM_MODEL)
                summary = reference_prompt.parse_answer(response)
                n_queries += 1

            assert summary is not None, "Failed to parse LLM response for reference summary generation after 3 attempts."
            reference_details = summary["summary"]

        if len(reference_details) > 0:
            result += "\n### References\n"
            result += reference_details
            has_reference = True

    return result, has_reference, n_queries

def extract_fut_code(fut_info):
    message = ""
    for fct_name, fct_info in fut_info.items():
        message += f"### {fct_name}#{fct_info['start_line']}-{fct_info['end_line']}\n"
        code = fct_info['code']
        message += f"{code}\n\n"
    return message

def extract_enclosing_class(fut_info):
    enclosing_class = ""
    for fct_name, fct_info in fut_info.items():
        if fct_info["context_class"] and fct_info["context_code"]:
            enclosing_class += fct_info["context_class"] + "\n"
            enclosing_class += fct_info["context_code"] + "\n\n"
    return enclosing_class

def extract_fut_signatures(fut_info):
    message = ""
    for fct_name, fct_info in fut_info.items():
        code = fct_info['code']
        signature = get_function_signature(code)
        message += f"## {fct_name}#{fct_info['start_line']}-{fct_info['end_line']}\n"
        message += f"{signature}\n\n"
    return message

def safe_function_dirname(fct_name: str) -> str:
    """Turn a dotted qualified function name (e.g. ``module.Class.method``)
    into a filesystem-safe subdirectory name, so each modified function in a
    multi-function PR gets its own cache subtree under the PR's cache_dir."""
    return fct_name.replace(".", "__")

def prepare_information(pr, github_repo, pr_nb):
    pull_request_details, has_reference, summary_queries = extract_pr_details(pr, use_reference= Config.USE_REFERENCE, github_repo= github_repo)
    prev_fut_code = extract_fut_code(pr.prev_fut_info)
    post_fut_code = extract_fut_code(pr.post_fut_info)
    prev_fut_names = ", ".join(list(pr.prev_fut_info.keys()))
    post_fut_signatures = extract_fut_signatures(pr.post_fut_info)
    enclosing_class = extract_enclosing_class(pr.prev_fut_info)
    code_changes = str(pr.patch)

    return pull_request_details, prev_fut_code, post_fut_code, prev_fut_names, post_fut_signatures, enclosing_class, code_changes, has_reference, summary_queries

def load_from_cache(cache_dir, pr_nb):
    with open(os.path.join(cache_dir, "results.json")) as f:
        states = json.load(f)

    if states["stage"] == "failed":
        return True, states

    if states["stage"] == "completed":
        assert "review_conclusion" in states, "If analysis is completed, review_conclusion should be in states."

        if states["review_conclusion"] == "BUG":
            assert "error_message" in states["execution_status"][-1], "If analysis is completed, error_message should be in the last execution status."
        else:
            assert states["review_conclusion"] == "NORMAL", "Unknown review conclusion in cached results."

        return True, states

    return False, states

def intent_analysis(
        states,
        pull_request_details,
        prev_fut_code,
        post_fut_code,
        prev_fut_names,
        post_fut_signatures,
        enclosing_class,
        pr_nb, cache_dir,
        available_import,
        pkg_name: str = "pkg",
        loader_header: str = "",
        module_path: str = "",
    ):
    states["stage"] = "intent_analysis"

    analysis_results = analyze_intent(
            pull_request_details=pull_request_details,
            prev_fut_code=prev_fut_code,
            prev_fut_names=prev_fut_names,
            post_fut_signatures=post_fut_signatures,
            post_fut_code=post_fut_code,
            available_import=available_import,
            enclosing_class=enclosing_class,
            pkg_name=pkg_name,
            loader_header=loader_header,
            module_path=module_path,
        )

    if analysis_results is None:
        states["intent_analysis"] = False
        states["llm_queries"] = Config.ANALYSIS_ATTEMPTS
        states["failure_reason"] = "query_error"
        save_results_to_cache(cache_dir, states)
        return states

    states.update(analysis_results)

    assert "specification" in states, "Intent analysis did not return a specification."

    states["llm_queries"] = analysis_results["analysis_queries"]
    states["intent_analysis"] = True
    save_results_to_cache(cache_dir, states)
    return states

def error_repair(
        states,
        pr_nb,
        cache_dir,
        pr,
        cloned_repo_manager,
        prev_fut_code,
        post_fut_code,
        fut_name,
        max_attempts= 5,
        loader_header: str = "",
        pkg_name: str = "pkg",
    ):
    execution_status = states.get("execution_status", None)
    # Pin a clone slot for the whole repair session (checkout + every repair
    # attempt's Docker execution) rather than re-resolving (and potentially
    # racing another PR run for) a slot on every single execution.
    lease_token = cloned_repo_manager.acquire_clone_lease(pr.pre_commit, pr.post_commit)
    try:
        cloned_repo = cloned_repo_manager.get_cloned_repo(pr.pre_commit, pr.post_commit, lease_token=lease_token)
        executor = DockerExecutor(container_name=cloned_repo.container_name)
        # Rename projects configured with rebuild_per_pr (pandas, scipy, keras)
        # build their pre_<pkg>/post_<pkg> wheels from the clone slot's
        # /pre_version and /post_version checkouts. Rebuild them now that this
        # slot is pinned to this PR's commits (marker-deduped, ccache-warm) so
        # every executed pre_<pkg>/post_<pkg> reflects the analyzed commits, not
        # the container-build-time snapshot.
        executor.rebuild_wheels_if_stale(pr.pre_commit, pr.post_commit)
        specification = states["specification"]
        if states["stage"] != "error_repair":
            states["stage"] = "error_repair"
            states["specification_traces"] = [specification]
            # The LLM may emit `from __future__ import annotations` inside its
            # imports section, which lands AFTER the prepended loader header and
            # is a SyntaxError. Hoist future imports above the header before run.
            exit_code, stdout = executor.execute_python_code(hoist_future_imports(specification))
            repair_attempts = 0
            if "execution_status" not in states:
                states["execution_status"] = []
            states["execution_status"].append({
                "exit_code": exit_code,
                "error_message": stdout,
                "repair_attempts": repair_attempts
            })
            save_results_to_cache(cache_dir, states)
        else:
            exit_code = execution_status[-1]["exit_code"]
            stdout = execution_status[-1]["error_message"]
            repair_attempts = execution_status[-1]["repair_attempts"]

        # reset llm_queries for repair attempts
        states["llm_queries"] -= repair_attempts

        is_assertion_error = False
        # Repair loop until success or max attempts reached
        while (exit_code != 0 and repair_attempts < max_attempts):

            # If assertion error happens in post-PR functions, we stop the repair attempts.
            # Under the module-qualified loader convention (pre_<pkg>.foo(...) /
            # post_<pkg>.foo(...), or call_impl's pre_res/pre_exc/post_res/post_exc
            # locals), a one-sided artifact no longer reliably embeds the literal
            # "pre_" + pkg_name substring on the failing line (results are usually
            # bound to a local variable first) -- match any \bpre_<token> instead of
            # only the exact package-qualified name.
            stdout_lower = stdout.lower()
            one_sided = ("pre-pr" in stdout_lower and "post-pr" not in stdout_lower) or (
                re.search(r"\bpre_\w+", stdout_lower) is not None
                and re.search(r"\bpost_\w+", stdout_lower) is None
            )
            if "AssertionError" in stdout and not one_sided:
                is_assertion_error = True
                break

            # Call to LLM-Fixer to repair the specification
            fixed_specification = repair(
                code=specification,
                error_message=stdout,
                prev_fut_code=prev_fut_code,
                post_fut_code=post_fut_code,
                loader_header=loader_header,
            )

            if fixed_specification is None:
                repair_attempts += 1
                continue

            specification = fixed_specification
            exit_code, stdout = executor.execute_python_code(hoist_future_imports(specification))
            repair_attempts += 1

            # Update states and save to cache
            states["specification"] = specification
            states["specification_traces"].append(specification)
            states["execution_status"].append({
                "exit_code": exit_code,
                "error_message": stdout,
                "repair_attempts": repair_attempts
            })
            save_results_to_cache(cache_dir, states)

        if exit_code != 0 and not is_assertion_error:
            assert repair_attempts >= max_attempts, "If exit_code is not 0, repair_attempts should reach max_attempts."

            states["llm_queries"] += repair_attempts
            states["error_repair"] = False
            states["failure_reason"] = "error_repair"
            save_results_to_cache(cache_dir, states)
            return states

        states["llm_queries"] += repair_attempts
        states["error_repair"] = True
        save_results_to_cache(cache_dir, states)
        return states
    finally:
        cloned_repo_manager.release_clone_lease(lease_token)

def assertion_errors_review(
        states,
        pr_nb,
        cache_dir,
        pull_request_details,
        prev_fut_code,
        post_fut_code,
        prev_fut_names,
        post_fut_signatures,
        available_import,
        enclosing_class,
        code_changes,
        pkg_name: str = "pkg",
        loader_header: str = "",
    ):
    error_message = states["execution_status"][-1]["error_message"]
    exit_code = states["execution_status"][-1]["exit_code"]
    specification = states["specification"]
    if exit_code != 0:
        assert "AssertionError" in error_message, "Only AssertionError should reach the review stage."
        states["stage"] = "assert_review"

        review_results = review_test_driver(
            pull_request_details = pull_request_details,
            prev_fut_code= prev_fut_code,
            prev_fut_names= prev_fut_names,
            post_fut_signatures= post_fut_signatures,
            post_fut_code= post_fut_code,
            available_import= available_import,
            enclosing_class= enclosing_class,
            test_driver= specification,
            error_message= error_message,
            code_changes= code_changes,
            pkg_name= pkg_name,
            loader_header= loader_header,
        )

        if review_results is None:
            states["llm_queries"] += Config.REVIEW_ATTEMPTS
            states["assert_review"] = False
            states["failure_reason"] = "query_error"
            save_results_to_cache(cache_dir, states)
            return states

        states["review_conclusion"] = review_results["conclusion"]
        states["review_reasoning"] = review_results.get("reasoning", "")
        states["review_queries"] = review_results["review_queries"]

        if "review_traces" not in states:
            states["review_traces"] = []

        states["review_traces"].append({
            "conclusion": review_results["conclusion"],
            "reasoning": review_results.get("reasoning", ""),
            "revised_specification": review_results.get("specification", specification),
            "review_queries": review_results["review_queries"]
        })

        if review_results["conclusion"] == "MISMATCH":
            states["specification"] = review_results["specification"]
            states["specification_traces"].append(states["specification"])

        elif review_results["conclusion"] == "BUG":
            states["stage"] = "completed"

        else:
            raise ValueError(f"Unknown conclusion from test driver review: {review_results['conclusion']}")

        states["llm_queries"] += review_results["review_queries"]
        states["assert_review"] = True

    else:
        states["review_conclusion"] = "NORMAL"
        states["stage"] = "completed"

    states["assert_review"] = True
    save_results_to_cache(cache_dir, states)
    return states

def bug_trigger_generation(
        states,
        original_specification,
        pull_request_details,
        prev_fut_code,
        post_fut_code,
        prev_fut_names,
        post_fut_signatures,
        enclosing_class,
        pr_nb, cache_dir,
        available_import,
        pkg_name: str = "pkg",
        loader_header: str = "",
        module_path: str = "",
    ):
    states["stage"] = "bug_trigger_generation"

    analysis_results = generalize_spec(
            specification=original_specification,
            pull_request_details=pull_request_details,
            prev_fut_code=prev_fut_code,
            prev_fut_names=prev_fut_names,
            post_fut_code=post_fut_code,
            available_import=available_import,
            enclosing_class=enclosing_class,
            pkg_name=pkg_name,
            loader_header=loader_header,
            module_path=module_path,
        )

    if analysis_results is None:
        states["bug_trigger_generation"] = False
        states["llm_queries"] = Config.ANALYSIS_ATTEMPTS
        states["failure_reason"] = "query_error"
        save_results_to_cache(cache_dir, states)
        return states

    states.update(analysis_results)

    assert "specification" in states, "Bug trigger generation did not return a specification."

    states["llm_queries"] += analysis_results["bug_trigger_queries"]
    states["bug_trigger_generation"] = True
    save_results_to_cache(cache_dir, states)
    return states

def spec_infer(
        pr_nb: int,
        force: bool = False,
        cache_dir: str = None,
        pull_request_details: str = None,
        prev_fut_code: str = None,
        post_fut_code: str = None,
        prev_fut_names: str = None,
        post_fut_signatures: str = None,
        enclosing_class: str = None,
        pr = None,
        cloned_repo_manager = None,
        fut_name: str = None,
        code_changes: str = None,
        summary_queries: int = 0,
        pkg_name: str = "pkg",
        loader_header: str = "",
        module_path: str = "",
    ) -> int:

    with run_scope(cache_dir, pr_nb, reset_usage=force):
        ### Check and load from cache if available
        states = {
            "stage": "init",
            "llm_queries": 0,
        }
        if os.path.exists(os.path.join(cache_dir, "results.json")) and not force:
            is_complete, states = load_from_cache(cache_dir, pr_nb)
            if is_complete:
                return states

        # Stage 1: Intent Analysis
        if states["stage"] == "init":
            states = intent_analysis(states, pull_request_details, prev_fut_code, post_fut_code, prev_fut_names, post_fut_signatures, enclosing_class, pr_nb, cache_dir, pr.import_string, pkg_name=pkg_name, loader_header=loader_header, module_path=module_path)
            states["llm_queries"] += summary_queries
            if not states["intent_analysis"]:
                states["stage"] = "failed"
                save_results_to_cache(cache_dir, states)
                return states

        # Stage 2: Error Repair and Bug Review
        while states["stage"] not in ["completed", "failed"] and states["llm_queries"] < Config.MAX_LLM_QUERIES:
            states = error_repair(states, pr_nb, cache_dir, pr, cloned_repo_manager, prev_fut_code, post_fut_code, fut_name, Config.REPAIR_ATTEMPTS, loader_header=loader_header, pkg_name=pkg_name)

            if not states["error_repair"]:
                states["stage"] = "failed"
                break

            states = assertion_errors_review(states, pr_nb, cache_dir, pull_request_details, prev_fut_code, post_fut_code, prev_fut_names, post_fut_signatures, pr.import_string, enclosing_class, code_changes, pkg_name=pkg_name, loader_header=loader_header)

            if not states["assert_review"]:
                states["stage"] = "failed"
                break

        if states["stage"] != "completed":
            states["stage"] = "failed"
            states.setdefault("failure_reason", "query_error")
            save_results_to_cache(cache_dir, states)
            return states

        if states["review_conclusion"] not in ("BUG", "NORMAL"):
            raise ValueError(f"Unknown review conclusion: {states['review_conclusion']}")

        save_results_to_cache(cache_dir, states)
        return states

def spec_generalization(
        pr_nb: int,
        force: bool = False,
        cache_dir: str = None,
        original_specification: str = None,
        pull_request_details: str = None,
        prev_fut_code: str = None,
        post_fut_code: str = None,
        prev_fut_names: str = None,
        post_fut_signatures: str = None,
        enclosing_class: str = None,
        pr = None,
        cloned_repo_manager = None,
        fut_name: str = None,
        code_changes: str = None,
        pkg_name: str = "pkg",
        loader_header: str = "",
        module_path: str = "",
    ) -> int:

    with run_scope(cache_dir, pr_nb, reset_usage=force):
        ### Check and load from cache if available
        states = {
            "stage": "init",
            "llm_queries": 0,
        }
        if os.path.exists(os.path.join(cache_dir, "results.json")) and not force:
            is_complete, states = load_from_cache(cache_dir, pr_nb)
            if is_complete:
                return states

        # Stage 1: Bug Trigger Generation
        if states["stage"] == "init":
            states = bug_trigger_generation(states, original_specification, pull_request_details, prev_fut_code, post_fut_code, prev_fut_names, post_fut_signatures, enclosing_class, pr_nb, cache_dir, pr.import_string, pkg_name=pkg_name, loader_header=loader_header, module_path=module_path)
            if not states["bug_trigger_generation"]:
                states["stage"] = "failed"
                save_results_to_cache(cache_dir, states)
                return states

        # Stage 2: Error Repair and Bug Review
        while states["stage"] not in ["completed", "failed"] and states["llm_queries"] < Config.MAX_LLM_QUERIES:
            states = error_repair(states, pr_nb, cache_dir, pr, cloned_repo_manager, prev_fut_code, post_fut_code, fut_name, Config.REPAIR_ATTEMPTS, loader_header=loader_header, pkg_name=pkg_name)

            if not states["error_repair"]:
                states["stage"] = "failed"
                break

            states = assertion_errors_review(states, pr_nb, cache_dir, pull_request_details, prev_fut_code, post_fut_code, prev_fut_names, post_fut_signatures, pr.import_string, enclosing_class, code_changes, pkg_name=pkg_name, loader_header=loader_header)

            if not states["assert_review"]:
                states["stage"] = "failed"
                break

        if states["stage"] != "completed":
            states["stage"] = "failed"
            states.setdefault("failure_reason", "query_error")
            save_results_to_cache(cache_dir, states)
            return states

        if states["review_conclusion"] not in ("BUG", "NORMAL"):
            raise ValueError(f"Unknown review conclusion: {states['review_conclusion']}")

        save_results_to_cache(cache_dir, states)
        return states

def analyze(project: str, pr_nb: int, force: bool = False) -> None:
    cache_dir = os.path.join(Config.CACHE_DIR, "oracles", project, str(pr_nb))
    os.makedirs(cache_dir, exist_ok=True)

    with run_scope(cache_dir, pr_nb, reset_usage=force):
        config_dict = {
            "INTENT_ANALYSIS_PROMPT": Config.INTENT_ANALYSIS_PROMPT,
            "SELF_REVIEW_PROMPT": Config.SELF_REVIEW_PROMPT,
            "RUNTIME_ERROR_REPAIR_PROMPT": Config.RUNTIME_ERROR_REPAIR_PROMPT,
            "ASSERTION_ERROR_REPAIR_PROMPT": Config.ASSERTION_ERROR_REPAIR_PROMPT,
            "SYNTAX_ERROR_REPAIR_PROMPT": Config.SYNTAX_ERROR_REPAIR_PROMPT,
            "MAX_LLM_QUERIES": Config.MAX_LLM_QUERIES,
            "ANALYSIS_ATTEMPTS": Config.ANALYSIS_ATTEMPTS,
            "REVIEW_ATTEMPTS": Config.REVIEW_ATTEMPTS,
            "REPAIR_ATTEMPTS": Config.REPAIR_ATTEMPTS,
            "USE_REFERENCE": Config.USE_REFERENCE,
            "LLM_MODEL": Config.LLM_MODEL,
            "USE_REFERENCE_SUMMARY": Config.USE_REFERENCE_SUMMARY,
        }

        with open(os.path.join(cache_dir, "config.json"), "w") as f:
            json.dump(config_dict, f, indent=4)

        ## Retrieve and do lightweight analysis of the target PR
        pr, cloned_repo_manager, github_repo = retrieve_pr(project, pr_nb)
        if pr is None:
            # Retrieval failed (retrieve_pr already reported the cause to
            # stderr). Record a failed state so BatchSpecInfer and the
            # experiment scripts see this PR as analyzed-but-failed rather
            # than silently skipped.
            states = {
                "stage": "failed",
                "llm_queries": 0,
                "failure_reason": "retrieval_error",
            }
            save_results_to_cache(cache_dir, states)
            return

        # PR-level context is expensive (GitHub API calls + optional LLM
        # reference-summary) and is shared across every modified function in
        # this PR, so it is computed exactly once here rather than once per
        # function.
        pull_request_details, has_reference, summary_queries = extract_pr_details(
            pr, use_reference=Config.USE_REFERENCE, github_repo=github_repo
        )
        code_changes = str(pr.patch)

        # Real dual-version execution: both the pre-PR and post-PR checkouts
        # are loaded as importable `pre_<pkg_name>`/`post_<pkg_name>` modules
        # inside the same script, via the auto-injected loader header. This
        # replaces the old fake mechanism (renaming one function's source
        # text to pre_foo/post_foo and pasting both copies into a script that
        # shared a single installed package version). Container-internal
        # paths mirror the ClonedRepoManager pool layout
        # (clone{i}/pre_version/<pkg>/, clone{i}/post_version/<pkg>/), doubled
        # from the single-version path convention DockerExecutor already uses
        # (`/home/<pkg>`) into a dedicated pre/post pair.
        project_config = Config.PROJECT_CONFIGS[project]
        pkg_name = project_config["package_name"]
        use_rename = project in Config.RENAME_PROJECTS
        pre_version_path = f"/pre_version/{pkg_name}"
        post_version_path = f"/post_version/{pkg_name}"
        loader_header = build_loader_header(
            pre_version_path,
            post_version_path,
            pkg_name=pkg_name,
            use_rename=use_rename,
        )

        # Only functions present on both sides have a meaningful pre/post
        # comparison; pure additions/deletions are skipped.
        modified_functions = [name for name in pr.prev_fut_info if name in pr.post_fut_info]

        manifest = {}
        for i, fct_name in enumerate(modified_functions):
            safe_name = safe_function_dirname(fct_name)
            fct_cache_dir = os.path.join(cache_dir, safe_name)
            os.makedirs(fct_cache_dir, exist_ok=True)

            single_prev = {fct_name: pr.prev_fut_info[fct_name]}
            single_post = {fct_name: pr.post_fut_info[fct_name]}

            prev_fut_code = extract_fut_code(single_prev)
            post_fut_code = extract_fut_code(single_post)
            prev_fut_names = fct_name
            post_fut_signatures = extract_fut_signatures(single_post)
            enclosing_class = extract_enclosing_class(single_prev)
            fut_name = fct_name.split(".")[-1]
            module_path = derive_module_path(
                pr.prev_fut_info[fct_name].get("file_path", ""), pkg_name
            )

            phase1_ending_stages = spec_infer(
                pr_nb= pr_nb,
                force= force,
                cache_dir= fct_cache_dir,
                pull_request_details= pull_request_details,
                prev_fut_code= prev_fut_code,
                post_fut_code= post_fut_code,
                prev_fut_names= prev_fut_names,
                post_fut_signatures= post_fut_signatures,
                enclosing_class= enclosing_class,
                pr = pr,
                cloned_repo_manager = cloned_repo_manager,
                fut_name= fut_name,
                code_changes= code_changes,
                pkg_name= pkg_name,
                loader_header= loader_header,
                module_path= module_path,
                # summary_queries is a one-time PR-level LLM cost (the
                # reference-summary query above); attribute it only to the
                # first function's llm_queries budget so it isn't
                # double-counted across every modified function.
                summary_queries = summary_queries if i == 0 else 0,
            )

            manifest_entry = {
                "stage": phase1_ending_stages.get("stage"),
                "review_conclusion": phase1_ending_stages.get("review_conclusion"),
            }

            if (
                Config.USE_PHASE2
                and phase1_ending_stages["stage"] == "completed"
                and phase1_ending_stages["review_conclusion"] != "BUG"
            ):
                phase1_specification = phase1_ending_stages["specification"]

                cache_dir_phase2 = os.path.join(fct_cache_dir, "phase2")
                os.makedirs(cache_dir_phase2, exist_ok=True)
                phase2_ending_stages = spec_generalization(
                    pr_nb= pr_nb,
                    force= force,
                    cache_dir= cache_dir_phase2,
                    original_specification= phase1_specification,
                    pull_request_details= pull_request_details,
                    prev_fut_code= prev_fut_code,
                    post_fut_code= post_fut_code,
                    prev_fut_names= prev_fut_names,
                    post_fut_signatures= post_fut_signatures,
                    enclosing_class= enclosing_class,
                    pr = pr,
                    cloned_repo_manager = cloned_repo_manager,
                    fut_name= fut_name,
                    code_changes= code_changes,
                    pkg_name= pkg_name,
                    loader_header= loader_header,
                    module_path= module_path,
                )
                manifest_entry["phase2_stage"] = phase2_ending_stages.get("stage")
                manifest_entry["phase2_review_conclusion"] = phase2_ending_stages.get("review_conclusion")

            manifest[fct_name] = manifest_entry

        with open(os.path.join(cache_dir, "manifest.json"), "w") as f:
            json.dump(manifest, f, indent=4)



def save_results_to_cache(cache_dir, results):
    if not os.path.exists(cache_dir):
        os.makedirs(cache_dir)

    # Stamp timing: start_time is captured once at run-scope entry (and
    # preserved across resumes); end_time is refreshed on every checkpoint
    # so the terminal save's stamp reflects the run's end.
    run = current_run()
    if run is not None:
        results.setdefault("start_time", run.start_time)
        results["end_time"] = time.strftime("%Y%m%d-%H%M%S")

    with open(os.path.join(cache_dir, "results.json"), "w") as f:
        json.dump(results, f, indent=4)

    with open(os.path.join(cache_dir, "specification.py"), "w") as f:
        if "specification" in results:
            f.write(results["specification"])
        else:
            f.write("# No specification generated.")

if __name__ == "__main__":
    parser = argparse.ArgumentParser(description="Patch Reviewer CLI")
    parser.add_argument("--project", type=str, required=True, help="Project name (e.g., pandas, scikit-learn)")
    parser.add_argument("--pr_nb", type=int, required=True, help="Pull Request number to review")
    parser.add_argument("--force", action="store_true", help="Force re-analysis even if results are cached")
    args = parser.parse_args()
    analyze(args.project, args.pr_nb, args.force)
