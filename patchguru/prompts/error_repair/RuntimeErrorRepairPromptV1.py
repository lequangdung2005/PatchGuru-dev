from patchguru.utils.LoaderHeader import strip_loader_header


class RuntimeErrorRepairPrompt:
    def __init__(self):
        pass

    def create_prompt(self, code, error_message):
        template = """
# Role
You are an expert Python software engineer. Your task is to correct runtime errors in the provided Python code based on the accompanying error message.

# Guidelines

1. **Only Debug on Target Sections**: Provided Python code is a test driver with three main sections: "# Neccessary imports", "# Specification", and "# Source Code of target function(s)". You MUST focus all debugging efforts on the "# Neccessary imports" and "# Specification" sections. The "# Source Code of target function(s)" section is guaranteed to be correct and should not be changed.

2.  **Analyze the Full Traceback:** Runtime errors usually provide a full traceback that shows the sequence of function calls leading to the error. Start by reading the error message at the bottom to understand the type of error (e.g., `TypeError`, `ValueError`, `KeyError`). Then, work your way up the traceback to see the exact line of code where the error occurred and the function calls that led to it.

3.  **Check for Common Runtime Errors:** Be on the lookout for frequent issues:
    * **`TypeError`:** This often happens when you perform an operation on an object of the wrong data type (e.g., trying to add a string to an integer).
    * **`NameError`:** The code is trying to use a variable or function that has not been defined or is out of scope.
    * **`IndexError` or `KeyError`:** You are trying to access an item in a list or dictionary with an index or key that does not exist.
    * **`ValueError`:** A function receives an argument of the correct type but an inappropriate value (e.g., `int("abc")`).

4.  **Preserve Dual-Namespace Calls:** The target function(s) are accessed through two pre-loaded module namespaces, `pre_<pkg>` and `post_<pkg>`, using their real, unmodified names (e.g., `pre_<pkg>.calculate_sum(...)` / `post_<pkg>.calculate_sum(...)`). Keep every such call exactly as it appears -- do not rename the function, strip the `pre_<pkg>`/`post_<pkg>` prefix, or merge the two versions into a single bare call.

# Input

You will be provided a test driver that is used to validate the expected relationship between two versions of a function: before and after a pull request. The code is expected to run without errors. However, it is currently faced with RUNTIME errors as follows:

## Test Driver

```python
{code}
```

## Error Message

```
{error_message}
```

# Output Format Instructions

1. Make sure that fixed code is structurally similar to the input code. The fixed code should still contain three main sections: "# Neccessary imports", "# Specification", and "# Source Code of target function(s)".

2. You can assume that the "# Source Code of target function(s)" section is unchanged and do not need to provide it again in your response. But you should still include the section header "# Source Code of target function(s)" between # Neccessary imports and # Specification sections in your response.

3. Provide your response in the following format:

<reasoning>
[Put your reasoning chain here including the analysis of the error message and the test driver code. This should include your thought process on how you arrived at the fix.]
</reasoning>

<fixed_code>
[Put the fixed code here that resolves the runtime error. Ensure that the code is executable and maintains the original semantics of the test driver.]
</fixed_code>
    """
        query = template.format(
            code=code,
            error_message=error_message
        )
        return query

    def parse_answer(self, answer):

        results = {}
        if "<reasoning>" in answer and "</reasoning>" in answer:
            reasoning_start = answer.index("<reasoning>") + len("<reasoning>")
            reasoning_end = answer.index("</reasoning>")
            results["reasoning"] = answer[reasoning_start:reasoning_end].strip()

        if "<fixed_code>" not in answer or "</fixed_code>" not in answer:
            return None

        try:
            fixed_code_start = answer.index("<fixed_code>") + len("<fixed_code>")
            fixed_code_end = answer.index("</fixed_code>")
            fixed_code = answer[fixed_code_start:fixed_code_end].strip()

            if "```" in fixed_code:
                if "```python" in fixed_code:
                    fixed_code = fixed_code.split("```python")[1].split("```")[0].strip()
                else:
                    fixed_code = fixed_code.split("```")[1].split("```")[0].strip()

            results["fixed_code"] = fixed_code
        except ValueError as e:
            return None
        return results

    def insert_code(self, prev_fut_code: str, post_fut_code: str, specification: str, loader_header: str = "") -> str:
            """
            Inserts concrete function code into the specification.
            """
            specification = strip_loader_header(specification)
            if "# Source Code of target function(s)" not in specification or "# Specification" not in specification:
                return None

            import_part = specification.split("# Source Code of target function(s)")[0].strip()
            spec_part = specification.split("# Specification")[1].strip()

            completed_specification = f"""
{loader_header}{import_part}

# Source Code of target function(s)

# Specification
{spec_part}
            """
            return completed_specification
