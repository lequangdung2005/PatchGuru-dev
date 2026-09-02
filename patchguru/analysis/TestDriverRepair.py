from __future__ import annotations
import json
from typing import Any
from patchguru import Config
from patchguru.llms import query_llm
from patchguru.utils.LoaderHeader import strip_loader_header
import re

def filter_logs(log_output: str) -> str:
        traceback_pattern = re.compile(r'Traceback \(most recent call last\):.*', re.DOTALL)

        match = traceback_pattern.search(log_output)

        if match:
            return match.group(0).strip()
        else:
            return log_output.strip()

def load_runtime_error_repair_prompt_template() -> Any:
    """
    Loads the prompt template for runtime error repair.
    """
    if Config.RUNTIME_ERROR_REPAIR_PROMPT == "v1":
        from patchguru.prompts.error_repair.RuntimeErrorRepairPromptV1 import RuntimeErrorRepairPrompt
        return RuntimeErrorRepairPrompt()

    raise ValueError(f"Unknown runtime error repair prompt version: {Config.RUNTIME_ERROR_REPAIR_PROMPT}. Supported versions: v1.")

def load_syntax_error_repair_prompt_template() -> Any:
    """
    Loads the prompt template for syntax error repair.
    """
    if Config.SYNTAX_ERROR_REPAIR_PROMPT == "v1":
        from patchguru.prompts.error_repair.SyntaxErrorRepairPromptV1 import SyntaxErrorRepairPrompt
        return SyntaxErrorRepairPrompt()

    raise ValueError(f"Unknown syntax error repair prompt version: {Config.SYNTAX_ERROR_REPAIR_PROMPT}. Supported versions: v1.")

def load_assertion_error_repair_prompt_template() -> Any:
    """
    Loads the prompt template for assertion error repair.
    """
    if Config.ASSERTION_ERROR_REPAIR_PROMPT == "v1":
        from patchguru.prompts.error_repair.AssertionErrorRepairPromptV1 import AssertionErrorRepairPrompt
        return AssertionErrorRepairPrompt()

    raise ValueError(f"Unknown assertion error repair prompt version: {Config.ASSERTION_ERROR_REPAIR_PROMPT}. Supported versions: v1.")

def repair(code, error_message, prev_fut_code, post_fut_code, loader_header: str = ""):
    error_lines = error_message.split("\n")
    error_lines = [line for line in error_lines if not line.startswith("Warning:") and not line.startswith("WARNING:")]
    error_message = "\n".join(error_lines).strip()
    if "AssertionError" in error_message:
        error_message = filter_logs(error_message)
        return repair_assertion_error(code, error_message, prev_fut_code, post_fut_code, loader_header=loader_header)
    elif "SyntaxError" in error_message:
        return repair_syntax_error(code, error_message, prev_fut_code, post_fut_code, loader_header=loader_header)
    else:
        error_message = filter_logs(error_message)
        return repair_runtime_error(code, error_message, prev_fut_code, post_fut_code, loader_header=loader_header)

def repair_runtime_error(code, error_message, prev_fut_code, post_fut_code, loader_header: str = ""):
    """
    Repairs runtime errors in the provided code using the RuntimeErrorRepairPrompt.
    """

    # Strip the auto-injected loader header before showing the driver code to
    # the LLM -- it never wrote that boilerplate and doesn't need to see it.
    displayed_code = strip_loader_header(code)

    prompt_template = load_runtime_error_repair_prompt_template()
    query = prompt_template.create_prompt(displayed_code, error_message)

    answer = query_llm(query, model=Config.LLM_MODEL)
    parsed_answer = prompt_template.parse_answer(answer)
    if parsed_answer is None:
        return None

    inserted_code = prompt_template.insert_code(
        prev_fut_code=prev_fut_code,
        post_fut_code=post_fut_code,
        specification=parsed_answer["fixed_code"],
        loader_header=loader_header,
    )


    if inserted_code is None:
        return None

    parsed_answer["fixed_code"] = inserted_code

    return parsed_answer["fixed_code"]

def repair_syntax_error(code, error_message, prev_fut_code, post_fut_code, loader_header: str = ""):
    """
    Repairs syntax errors in the provided code using the SyntaxErrorRepairPrompt.
    """
    displayed_code = strip_loader_header(code)

    prompt_template = load_syntax_error_repair_prompt_template()
    query = prompt_template.create_prompt(displayed_code, error_message)

    answer = query_llm(query, model=Config.LLM_MODEL)
    parsed_answer = prompt_template.parse_answer(answer)
    if parsed_answer is None:
        return None

    inserted_code = prompt_template.insert_code(
        prev_fut_code=prev_fut_code,
        post_fut_code=post_fut_code,
        specification=parsed_answer["fixed_code"],
        loader_header=loader_header,
    )

    if inserted_code is None:
        return None

    parsed_answer["fixed_code"] = inserted_code
    return parsed_answer["fixed_code"]

def repair_assertion_error(code, error_message, prev_fut_code, post_fut_code, loader_header: str = ""):
    """
    Repairs assertion errors in the provided code using the AssertionErrorRepairPrompt.
    """
    displayed_code = strip_loader_header(code)

    prompt_template = load_assertion_error_repair_prompt_template()
    query = prompt_template.create_prompt(displayed_code, error_message)

    answer = query_llm(query, model=Config.LLM_MODEL)
    parsed_answer = prompt_template.parse_answer(answer)
    if parsed_answer is None:
        return None

    inserted_code = prompt_template.insert_code(
        prev_fut_code=prev_fut_code,
        post_fut_code=post_fut_code,
        specification=parsed_answer["fixed_code"],
        loader_header=loader_header,
    )

    if inserted_code is None:
        return None

    parsed_answer["fixed_code"] = inserted_code
    return parsed_answer["fixed_code"]
