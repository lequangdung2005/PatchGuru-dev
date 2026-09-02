"""Dual-execution loader header generation and error classification.

Builds the auto-injected header that loads both the pre-PR and post-PR
versions of a package into a single Python process (via
``patchguru.execution.OracleHelpers.load_package``), and provides helpers to
validate a generated test driver and to classify execution failures.

Wired into ``SpecInfer.py``'s ``analyze()``, which builds one header per PR
(via ``Config.PROJECT_CONFIGS``/``Config.RENAME_PROJECTS``) and threads it
through every generation/repair/review stage, replacing the old
single-installed-version, renamed-function-name approach.
"""

import inspect
import re
from typing import Literal

from patchguru.execution.OracleHelpers import (
    CALL_IMPL_SOURCE,
    load_package,
    resolve_package_path,
)

# ---------------------------------------------------------------------------
# Dual-execution loader header
# ---------------------------------------------------------------------------

# Loader source is extracted once at module load time so build_loader_header()
# can embed it into every test driver without requiring filesystem access.
_LOADER_IMPORTS = "\n".join(
    [
        "import builtins",
        "import importlib.util",
        "import sys",
        "from pathlib import Path",
        "from types import ModuleType",
        "from typing import Dict, Optional, Tuple, cast",
    ]
)
_LOADER_SOURCE: str = (
    _LOADER_IMPORTS
    + "\n\n"
    + CALL_IMPL_SOURCE.strip()
    + "\n\n"
    + inspect.getsource(resolve_package_path)
    + "\n\n"
    + inspect.getsource(load_package)
)
_LOADER_START_MARKER = "# === AUTO-INJECTED DUAL EXECUTION LOADER — DO NOT MODIFY ==="
_LOADER_END_MARKER = "# === END LOADER ==="


def build_loader_header(
    pre_version_path: str,
    post_version_path: str,
    pkg_name: str = "pkg",
    use_rename: bool = False,
) -> str:
    """Generate the auto-injected header that loads both package versions and provides call_impl.

    This header is prepended to every LLM-generated test driver before execution.
    The LLM is told NOT to include this part -- it only writes the user code below.

    When *use_rename* is ``True`` the package has already been compiled as
    ``pre_{pkg_name}`` and ``post_{pkg_name}`` (C-extension rename approach) so the
    header emits plain ``import`` statements instead of the ``load_package`` machinery.

    Injects (use_rename=False only):
    - ``call_impl``: helper for dual pre/post execution and result comparison.
    - ``resolve_package_path``: utility to locate packages.
    - ``load_package``: utility to isolate and load package versions.
    - Package loading code: ``pre_{pkg_name}`` and ``post_{pkg_name}`` module references.

    Args:
        pre_version_path:  Container-internal path to the pre-PR installed package
                           (e.g. ``"/pre_version/marshmallow"``).
        post_version_path: Container-internal path to the post-PR installed package
                           (e.g. ``"/post_version/marshmallow"``).
        pkg_name:          Base name for the dual-execution namespace variables.
                           Produces ``pre_{pkg_name}`` and ``post_{pkg_name}``
                           (default ``"pkg"`` -> ``pre_pkg`` / ``post_pkg``).
        use_rename:        When ``True``, emit ``import pre_{pkg_name}`` /
                           ``import post_{pkg_name}`` instead of ``load_package`` calls.

    Returns:
        A string to prepend verbatim to the LLM-generated test driver.
    """
    if use_rename:
        return (
            f"{_LOADER_START_MARKER}\n"
            f"import importlib  # pre-imported so LLM-generated code can use importlib.import_module etc.\n"
            f"{CALL_IMPL_SOURCE}\n"
            f"import pre_{pkg_name}\n"
            f"import post_{pkg_name}\n"
            f"{_LOADER_END_MARKER}\n\n"
        )
    return (
        f"{_LOADER_START_MARKER}\n"
        f"{_LOADER_SOURCE}\n\n"
        f'pre_{pkg_name} = load_package("{pre_version_path}", "pre_{pkg_name}")\n'
        f'post_{pkg_name} = load_package("{post_version_path}", "post_{pkg_name}")\n'
        f"{_LOADER_END_MARKER}\n\n"
    )


def build_module_path_guidance(module_path: str) -> str:
    """Return prompt guidance teaching the LLM how to reach a target in a submodule.

    Given the FUT's real package-module path (e.g. ``"marshmallow.validate"``),
    emits a short instruction block telling the model to access the symbol as
    ``pre_marshmallow.validate`` / ``post_marshmallow.validate`` and to import the
    submodule explicitly first.

    The generic ``pre_<pkg>.<func>(...)`` convention taught elsewhere only covers
    top-level symbols; without this block the model guesses ``pre_<pkg>.<symbol>``
    and fails (e.g. ``ModuleNotFoundError: No module named 'pre_marshmallow.URL'``
    when ``URL`` actually lives in ``marshmallow.validate``).

    Returns ``""`` when *module_path* is empty or is just the package itself (no
    submodule), where the existing top-level guidance already applies.

    Args:
        module_path: Fully-qualified module path of the FUT, e.g. ``"marshmallow.validate"``.

    Returns:
        A prompt fragment to place in the model's input, or ``""`` if not applicable.
    """
    if not module_path or "." not in module_path:
        # No enclosing submodule: the target lives at the package top level,
        # where the generic ``pre_<pkg>.<func>`` guidance already applies.
        return ""
    pre_ref = "pre_" + module_path
    post_ref = "post_" + module_path
    return f"""
## Module Path of Target Function

The target function(s) are defined inside the package module ``{module_path}``
(a submodule of the package under test). They must be reached through the
pre/post namespaces as ``{pre_ref}`` and ``{post_ref}`` respectively -- NOT as
attributes of the top-level ``pre_<pkg>`` / ``post_<pkg>`` namespaces directly.

Import the submodule at the top of the driver (in the imports section):

```python
import {pre_ref}
import {post_ref}
```

Then access the target symbol(s) through these qualified paths, e.g.
``{pre_ref}.<ClassName>(...).<method>(...)`` and
``{post_ref}.<ClassName>(...).<method>(...)``.
"""


def hoist_future_imports(program: str) -> str:
    """Move every ``from __future__ import ...`` statement to the top of the file.

    The dual-execution loader header is prepended to LLM-generated drivers, but
    Python requires ``from __future__`` imports to appear at the very start of a
    module (preceded only by a docstring and comments). Models frequently emit
    ``from __future__ import annotations`` inside their imports section -- i.e.
    *after* the prepended loader header -- which raises
    ``SyntaxError: from __future__ imports must occur at the beginning of the file``.

    This hoists each distinct future import above the loader header (kept in
    first-occurrence order, deduplicated) so the driver is always valid. Safe to
    call on headers built either for the dual loader (``use_rename=False``) or
    the rename path (``use_rename=True``), and idempotent.
    """
    if not program:
        return program
    future_lines: list[str] = []
    kept: list[str] = []
    seen: set[str] = set()
    for line in program.splitlines():
        if re.match(r"^\s*from\s+__future__\s+import\s+", line):
            stripped = line.strip()
            if stripped not in seen:
                seen.add(stripped)
                future_lines.append(stripped)
        else:
            kept.append(line)
    if not future_lines:
        return program
    return "\n".join(future_lines) + "\n\n" + "\n".join(kept).lstrip() + "\n"


def strip_loader_header(comparison_program: str) -> str:
    """Remove the auto-injected dual-execution loader header when present."""
    if (
        _LOADER_START_MARKER not in comparison_program
        or _LOADER_END_MARKER not in comparison_program
    ):
        return comparison_program

    _, remainder = comparison_program.split(_LOADER_START_MARKER, 1)
    _, user_program = remainder.split(_LOADER_END_MARKER, 1)
    return user_program.lstrip()


def check_valid(
    comparison_program: str,
    pkg_name: str = "pkg",
) -> bool:
    """Return ``True`` when *comparison_program* uses the dual namespace pattern.

    Validates that both ``pre_{pkg_name}`` and ``post_{pkg_name}`` are referenced in
    the user-authored test driver, confirming the LLM followed the dual-execution
    instructions. This deliberately does not require a specific function name: the
    target may be a plain module-level function (``pre_pkg.func(...)``), a class or
    instance method (``pre_pkg.Class(...).method(...)``), or invoked together with
    other functions from the same module, so a literal ``pre_pkg.<name>`` regex
    would reject valid drivers.

    Args:
        comparison_program: Generated test driver source to validate.
        pkg_name:           Base name for the dual-execution namespace variables
                            (default ``"pkg"`` -> checks for ``pre_pkg`` / ``post_pkg``).

    Returns:
        ``True`` if both ``pre_{pkg_name}`` and ``post_{pkg_name}`` appear in the program.
    """
    user_program = strip_loader_header(comparison_program)
    return f"pre_{pkg_name}" in user_program and f"post_{pkg_name}" in user_program


# ---------------------------------------------------------------------------
# Error classification
# ---------------------------------------------------------------------------

# Matches the two standard Python exception-chaining separator lines.
_CHAIN_MARKER_RE = re.compile(
    r"During handling of the above exception, another exception occurred:"
    r"|The above exception was the direct cause of the following exception:"
)


def classify_error(
    stdout: str, stderr: str
) -> Literal["syntax", "assertion", "runtime"]:
    """Classify the type of error from test driver execution output.

    SyntaxError/IndentationError always wins regardless of chaining.  For
    chained tracebacks the classification is based on the *root* (first)
    exception, not the surface exception, so that a ``NameError`` that
    subsequently triggers an ``AssertionError`` is correctly reported as a
    runtime error.

    Args:
        stdout: Captured stdout from the failed execution.
        stderr: Captured stderr from the failed execution.

    Returns:
        ``"syntax"`` for SyntaxError/IndentationError, ``"assertion"`` for
        AssertionError, ``"runtime"`` for all other failures.
    """
    combined = stdout + "\n" + stderr
    if "SyntaxError" in combined or "IndentationError" in combined:
        return "syntax"
    # For chained tracebacks classify on the root cause, not the surface exception.
    root_segment = _CHAIN_MARKER_RE.split(combined, maxsplit=1)[0]
    if "AssertionError" in root_segment:
        return "assertion"
    return "runtime"
