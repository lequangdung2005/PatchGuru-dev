import os

DATA_SYNTHESIS_PROMPT = "v1"

LOG_LEVEL = "DEBUG"
# Deprecated: the global event-log mechanism (Tracker.py) was removed.
# Per-PR tracking now lives in .cache/oracles/<project>/<pr_nb>/ (llm_usage.jsonl
# + results.json). Kept only for backward compatibility with old scripts.
LOG_DIR = os.environ.get("PATCHGURU_LOG_DIR", "logs/batch_runs")

LLM_MODEL = "openai/gpt-5.6-luna"  # Default model for LLM queries

# ── LLM provider base URLs ──────────────────────────────────────────────────
# The OpenAI client uses these to determine the API endpoint.
# Leave as None to use each provider's default.
LLM_BASE_URL = None  # Override for all models (takes precedence over MODEL_BASE_URL_MAP)

MODEL_BASE_URL_MAP = {
    "openai/": "https://openrouter.ai/api/v1",
    "deepseek/": "https://openrouter.ai/api/v1",
    "gpt-": None,        # None = OpenAI default (https://api.openai.com/v1)
    "gemini-": None,     # Gemini uses its own SDK, not OpenAI client
}


def get_llm_base_url(model: str) -> str | None:
    """Return the base URL for the given model, or None for provider default."""
    if LLM_BASE_URL is not None:
        return LLM_BASE_URL
    for prefix in sorted(MODEL_BASE_URL_MAP, key=len, reverse=True):
        if model.startswith(prefix):
            return MODEL_BASE_URL_MAP[prefix]
    return None

USE_REFERENCE = True
USE_REFERENCE_SUMMARY = True

USE_PHASE2 = True  # Whether to use phase 2 in the analysis pipeline
INTENT_ANALYSIS_PROMPT = "v1"  # Default prompt version for intent analysis
RUNTIME_ERROR_REPAIR_PROMPT = "v1"  # Default prompt version for runtime error repair
SYNTAX_ERROR_REPAIR_PROMPT = "v1"  # Default prompt version for syntax error repair
ASSERTION_ERROR_REPAIR_PROMPT = "v1"  # Default prompt version for assertion error repair
SELF_REVIEW_PROMPT = "v1"  # Default prompt version for self review
BUG_TRIGGER_PROMPT = "v1"  # Default prompt version for bug trigger generation

REPAIR_ATTEMPTS = 5  # Number of attempts to repair errors in code
ANALYSIS_ATTEMPTS = 5  # Number of attempts to re-run the analysis if output is invalid
GENERALIZED_ATTEMPTS = 3  # Number of attempts to generalize specifications
REVIEW_ATTEMPTS = 3  # Number of attempts to re-run the review if output is invalid

MAX_LLM_QUERIES = 20  # Maximum number of LLM queries to ask during analysis

PL = "python"  # Default programming language for analysis

CACHE_DIR = ".cache"  # Default cache directory for storing results

PR_CUT_OFF = {
    "pandas": 59900,
    "scipy": 21652,
    "keras": 20264,
    "marshmallow": 0,
}

# ── Per-project repository / container metadata ─────────────────────────────
# Ported in spirit from patchguru4py's src/patchguru4py/config.py::PROJECT_CONFIGS.
# Values below match PatchGuru's OWN current conventions, not patchguru4py's:
#   - repo_id: GitHub "org/repo", as passed to ClonedRepoManager by
#     analysis/PRRetriever.py::get_repo().
#   - container_base: the container-name prefix used by .devcontainer/setup_<project>.sh
#     (docker run --name <container_base>1/2/3 ...) and matched today via
#     execution/DockerExecutor.py's `self.container.name.startswith(...)` branches.
#   - module_name / package_name: the importable package name (identical for every
#     project below).
#   - conda_env: the mamba/conda env DockerExecutor.py activates (`mamba
#     activate <env>`) before running code in-container. Set for scipy
#     (`scipy-dev`) and pandas (`pandas-dev`, strict-pinned from
#     environment.yml via .devcontainer/setup_pandas_to_run_in_container.sh).
#   - rename_script (rename projects with rebuild_per_pr): the container-root
#     rename/rebuild script DockerExecutor.rebuild_wheels_if_stale() re-runs
#     after each PR checkout so the installed pre_<pkg>/post_<pkg> wheels match
#     the analyzed commits (set for all RENAME_PROJECTS: pandas, scipy, keras).
#   - rebuild_per_pr (all RENAME_PROJECTS): whether to rebuild the renamed
#     wheels in the container after checking out a PR's pre/post commits (see
#     rename_script). Leases (ClonedRepoManager.acquire_clone_lease) make this
#     race-safe across the parallel batch.
#   - test_extras: the pip extras group installed for test dependencies, mirroring
#     the `pip install -e '.[...]'` invocations in DockerExecutor.py / the
#     corresponding setup_<project>.sh.
PROJECT_CONFIGS: dict = {
    "pandas": {
        "repo_id": "pandas-dev/pandas",
        "container_base": "pandas-dev",
        "module_name": "pandas",
        "package_name": "pandas",
        # Strict/pinned pandas-dev conda env built from pandas's environment.yml
        # (see .devcontainer/setup_pandas_to_run_in_container.sh). Routes docker
        # exec through `mamba activate pandas-dev`, incl. the per-PR wheel
        # rebuild (rebuild_per_pr below).
        "conda_env": "pandas-dev",
        "rename_script": "rename_pandas_in_container.sh",
        "rebuild_per_pr": True,
    },
    "scipy": {
        "repo_id": "scipy/scipy",
        "container_base": "scipy-dev",
        "module_name": "scipy",
        "package_name": "scipy",
        # Routes docker exec through `mamba activate scipy-dev`
        # (see execution/DockerExecutor.py's scipy branch).
        "conda_env": "scipy-dev",
        "rename_script": "rename_scipy_in_container.sh",
        "rebuild_per_pr": True,
    },
    "keras": {
        "repo_id": "keras-team/keras",
        "container_base": "keras",
        "module_name": "keras",
        "package_name": "keras",
        "rename_script": "rename_keras_in_container.sh",
        "rebuild_per_pr": True,
    },
    "marshmallow": {
        "repo_id": "marshmallow-code/marshmallow",
        "container_base": "marshmallow",
        "module_name": "marshmallow",
        "package_name": "marshmallow",
        # `pip install -e '.[dev]'` in execution/DockerExecutor.py's marshmallow branch.
        "test_extras": "dev",
    },
}

# Projects that need the rename mechanism (compiling genuine pre_<pkg>/post_<pkg>
# distributions) instead of sys.modules renaming, because they ship C extensions
# whose singletons can't be isolated by load_package()-style renaming.
RENAME_PROJECTS = ["pandas", "scipy", "keras"]
assert set(RENAME_PROJECTS) <= set(
    PROJECT_CONFIGS
), f"RENAME_PROJECTS contains unknown projects: {set(RENAME_PROJECTS) - set(PROJECT_CONFIGS)}"

# Rename/rebuild step (compiling both pre- and post-commit C extensions from
# scratch) can run long, especially under contention from sibling containers,
# so it gets its own, more generous timeout budget than a single oracle script run.
RENAME_SCRIPT_TIMEOUT_SECONDS = 1800
# Timeout for a single oracle-script execution inside the Docker container.
# Cross-check against execution/DockerExecutor.py::execute_python_code's own
# `timeout: Optional[int] = 900` default parameter — this is the pipeline-level
# budget passed in for a single dual-version comparison run.
ORACLE_DOCKER_TIMEOUT_SECONDS = 300
