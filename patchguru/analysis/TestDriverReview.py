from __future__ import annotations
import json
from typing import Any
from patchguru import Config
from patchguru.llms import query_llm
from patchguru.utils.LoaderHeader import strip_loader_header
from patchguru.utils.PythonCodeUtil import get_docstring_of_function

def load_prompt_template() -> Any:
    """
    Loads the prompt template for test driver review.
    """
    if Config.SELF_REVIEW_PROMPT == "v1":
        from patchguru.prompts.self_review.SelfReviewPromptV1 import SelfReviewPrompt
        return SelfReviewPrompt()

    raise ValueError(f"Unknown test driver review prompt version: {Config.SELF_REVIEW_PROMPT}. Supported versions: v1.")

def review_test_driver(
    pull_request_details: str,
    prev_fut_code: str,
    prev_fut_names: str,
    post_fut_signatures: str,
    post_fut_code: str = "",
    available_import: str = "",
    enclosing_class: str = "",
    test_driver: str = "",
    error_message: str = "",
    code_changes: str = "",
    pkg_name: str = "pkg",
    loader_header: str = "",
) -> dict[str, Any] | None:
    """
    Analyzes the intent of a pull request and generates formal specifications.
    """

    PromptTemplate = load_prompt_template()

    # Hidden post-PR code in test driver repair
    # test_driver = PromptTemplate.hidden_post_pr_code(test_driver)

    # Strip the auto-injected dual-execution loader header before showing the
    # test driver back to the LLM -- it is boilerplate the model never wrote
    # and never needs to see/repeat; a fresh header is re-prepended by
    # insert_code() when a MISMATCH is found below.
    displayed_test_driver = strip_loader_header(test_driver)

    prompt = PromptTemplate.create_prompt(
        pull_request_details=pull_request_details,
        prev_fut_code=prev_fut_code,
        post_fut_signatures=post_fut_signatures,
        enclosing_class=enclosing_class,
        test_driver=displayed_test_driver,
        error_message=error_message,
        code_changes=code_changes,
    )

    assert "," not in prev_fut_names, "Currently only support analyzing one function at a time."

    is_valid = False
    llm_queries = 0
    max_retries = Config.REVIEW_ATTEMPTS
    while not is_valid and llm_queries < max_retries:
        response = query_llm(prompt)

        parsed_response = PromptTemplate.parse_answer(response)

        if parsed_response is None:
            return None
        is_valid = PromptTemplate.check_valid(parsed_response, pkg_name=pkg_name)
        llm_queries += 1

    if not is_valid:
        return None

    if parsed_response["conclusion"] == "MISMATCH":
        inserted_spec = PromptTemplate.insert_code(
            prev_fut_code=prev_fut_code,
            post_fut_code=post_fut_code,
            specification=parsed_response["specification"],
            loader_header=loader_header,
        )

        if inserted_spec is None:
            return None

        parsed_response["specification"] = inserted_spec
    else:
        parsed_response["specification"] = test_driver
    parsed_response["review_queries"] = llm_queries

    return parsed_response
