"""Oracle execution helpers for dual pre/post execution.

Provides ``resolve_package_path``, ``load_package``, and ``call_impl`` for
robust patch-oracle execution: running both the pre-PR and post-PR versions
of a package inside the same Python process so their behavior can be
directly compared.

Isolation strategy
-------------------
A naive ``spec_from_file_location`` call isolates only the top-level module
object. When a package's ``__init__.py`` performs absolute intra-package
imports, Python resolves those imports through ``sys.path`` and may reuse
modules from whichever version is currently importable.

To prevent that, ``load_package`` wraps the load in three phases:

1. Snapshot and clear any existing ``<real_pkg_name>.*`` entries from
   ``sys.modules``.
2. Temporarily prepend the package's parent directory to ``sys.path`` so
   absolute intra-package imports resolve to the intended version.
3. Rename all loaded ``<real_pkg_name>.*`` modules to ``<runtime_name>.*`` and
   then restore the original ``sys.path`` and any previously-loaded modules.

Used by ``patchguru.utils.LoaderHeader.build_loader_header`` to construct the
auto-injected dual-execution header, replacing the old fake approach (renaming
one function's source text and pasting both copies into a script that shares a
single installed package version).
"""

import builtins
import importlib.util
import sys
from pathlib import Path
from types import ModuleType
from typing import Any, Callable, Dict, Optional, Tuple, cast


def resolve_package_path(repo_root: str, package_name: str) -> str:
    """Locate the importable package directory inside a repository root.

    Resolution order:

    1. src layout:  ``<repo_root>/src/<package_name>/__init__.py``
    2. flat layout: ``<repo_root>/<package_name>/__init__.py``
    3. Scan fallback under ``src/`` for any package directory.
    4. Scan fallback under the repo root for any package directory.

    Args:
        repo_root: Filesystem path to the repository root.
        package_name: Import name derived from the repo or package path. Used
            for the two fast-path checks before the generic scan fallback.

    Returns:
        Absolute path to the directory containing ``__init__.py``.

    Raises:
        RuntimeError: If no package directory can be found.
    """
    root = Path(repo_root)

    candidate = root / "src" / package_name
    if (candidate / "__init__.py").exists():
        return str(candidate)

    candidate = root / package_name
    if (candidate / "__init__.py").exists():
        return str(candidate)

    src_dir = root / "src"
    if src_dir.is_dir():
        for child in sorted(src_dir.iterdir()):
            if child.is_dir() and (child / "__init__.py").exists():
                return str(child)

    for child in sorted(root.iterdir()):
        if child.is_dir() and (child / "__init__.py").exists():
            return str(child)

    raise RuntimeError(
        f"Cannot locate package '{package_name}' inside {repo_root}. "
        f"Tried src/{package_name}/, {package_name}/, and a full directory scan."
    )


def load_package(package_path: str, runtime_name: str) -> ModuleType:
    """Load a Python package into an isolated sys.modules namespace.

    ``package_path`` may be either the exact directory containing
    ``__init__.py`` or a repository root. In the latter case,
    ``resolve_package_path`` is used to locate the importable package first.

    Full sub-module isolation is achieved by snapshotting and clearing any
    existing ``<real_pkg_name>.*`` entries from ``sys.modules``, temporarily
    prepending the package parent to ``sys.path``, and then renaming every
    loaded ``<real_pkg_name>.*`` entry to ``<runtime_name>.*`` before restoring
    the previous interpreter state.

    Args:
        package_path: Path to the package directory or to its repository root.
        runtime_name: Namespace key to register in ``sys.modules`` (for
            example ``"pre_pkg"`` or ``"post_pkg"``).

    Returns:
        The loaded top-level module object.
    """

    runtime_aliases_obj = getattr(load_package, "_runtime_aliases", None)
    if isinstance(runtime_aliases_obj, dict):
        runtime_aliases = cast(Dict[str, str], runtime_aliases_obj)
    else:
        runtime_aliases = {}
        setattr(load_package, "_runtime_aliases", runtime_aliases)

        original_import = builtins.__import__
        original_import_module = importlib.import_module

        def _redirect_absolute_import_name(name: str, caller_name: str) -> str:
            for (
                registered_runtime_name,
                registered_real_name,
            ) in runtime_aliases.items():
                if not (
                    caller_name == registered_runtime_name
                    or caller_name.startswith(registered_runtime_name + ".")
                ):
                    continue
                if name == registered_real_name or name.startswith(
                    registered_real_name + "."
                ):
                    return registered_runtime_name + name[len(registered_real_name):]
            return name

        def _runtime_import(
            name: str,
            globals_dict: Optional[Dict[str, object]] = None,
            locals_dict: Optional[Dict[str, object]] = None,
            fromlist: Tuple[str, ...] = (),
            level: int = 0,
        ) -> object:
            if level == 0 and globals_dict is not None:
                caller_name_obj = globals_dict.get("__name__")
                if isinstance(caller_name_obj, str):
                    redirected = _redirect_absolute_import_name(name, caller_name_obj)
                    if redirected != name:
                        return original_import(
                            redirected,
                            globals_dict,
                            locals_dict,
                            fromlist,
                            level,
                        )
            return original_import(name, globals_dict, locals_dict, fromlist, level)

        def _runtime_import_module(name: str, package: Optional[str] = None) -> ModuleType:
            if not name or name.startswith("."):
                return original_import_module(name, package)

            try:
                caller_name_obj = sys._getframe(1).f_globals.get("__name__")
            except ValueError:
                caller_name_obj = None

            redirected = name
            if isinstance(caller_name_obj, str):
                redirected = _redirect_absolute_import_name(name, caller_name_obj)
            return original_import_module(redirected, package)

        setattr(builtins, "__import__", _runtime_import)
        importlib.import_module = _runtime_import_module

    path = Path(package_path).resolve()
    init_file = path / "__init__.py"

    if not init_file.exists():
        package_name = path.name
        resolved = resolve_package_path(str(path), package_name)
        path = Path(resolved)
        init_file = path / "__init__.py"

    real_pkg_name = path.name
    package_parent = str(path.parent)
    previous_real_name = runtime_aliases.get(runtime_name)
    runtime_aliases[runtime_name] = real_pkg_name

    snapshot: Dict[str, ModuleType] = {
        key: value
        for key, value in sys.modules.items()
        if key == real_pkg_name or key.startswith(real_pkg_name + ".")
    }
    runtime_snapshot: Dict[str, ModuleType] = {
        key: value
        for key, value in sys.modules.items()
        if key == runtime_name or key.startswith(runtime_name + ".")
    }
    for key in snapshot:
        del sys.modules[key]
    for key in runtime_snapshot:
        del sys.modules[key]

    original_path = sys.path.copy()
    sys.path.insert(0, package_parent)

    try:
        spec = importlib.util.spec_from_file_location(
            runtime_name,
            init_file,
            submodule_search_locations=[str(path)],
        )
        assert spec is not None and spec.loader is not None
        module = importlib.util.module_from_spec(spec)
        sys.modules[runtime_name] = module
        spec.loader.exec_module(module)

        loaded_pkg_entries: Dict[str, ModuleType] = {
            key: value
            for key, value in sys.modules.items()
            if key == real_pkg_name or key.startswith(real_pkg_name + ".")
        }
        for key, value in loaded_pkg_entries.items():
            if key == real_pkg_name:
                continue
            suffix = key[len(real_pkg_name):]
            sys.modules[runtime_name + suffix] = value

        for key in loaded_pkg_entries:
            sys.modules.pop(key, None)
    except Exception:
        sys.modules.update(runtime_snapshot)
        if previous_real_name is None:
            runtime_aliases.pop(runtime_name, None)
        else:
            runtime_aliases[runtime_name] = previous_real_name
        raise
    finally:
        sys.path[:] = original_path
        sys.modules.update(snapshot)

    return module


def call_impl(
    pre_impl: Callable[..., Any],
    post_impl: Callable[..., Any],
    *args: Any,
    **kwargs: Any,
) -> Tuple[Optional[Any], Optional[Exception], Optional[Any], Optional[Exception]]:
    """Call pre/post implementations and capture results and exceptions.

    Returns:
        A 4-tuple ``(pre_res, pre_exc, post_res, post_exc)`` where each ``*_exc``
        is either the raised exception instance or ``None`` when the call
        returned normally.
    """

    pre_res = None
    pre_exc = None
    post_res = None
    post_exc = None

    try:
        pre_res = pre_impl(*args, **kwargs)
    except Exception as exc:
        pre_exc = exc

    try:
        post_res = post_impl(*args, **kwargs)
    except Exception as exc:
        post_exc = exc

    return pre_res, pre_exc, post_res, post_exc


# Kept as a literal so oracle driver generation does not depend on inspect.getsource()
# at import time and avoids annotation/import coupling in injected scripts.
CALL_IMPL_SOURCE = """\
def call_impl(pre_impl, post_impl, *args, **kwargs):
    pre_res = None
    pre_exc = None
    post_res = None
    post_exc = None

    try:
        pre_res = pre_impl(*args, **kwargs)
    except Exception as exc:
        pre_exc = exc

    try:
        post_res = post_impl(*args, **kwargs)
    except Exception as exc:
        post_exc = exc

    return pre_res, pre_exc, post_res, post_exc
"""
