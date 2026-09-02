"""
Batch runner for SpecInfer — processes multiple PRs from a project's PR list file.

Reads PR numbers (one per line) from data/pr_data/prs/<project>.txt and spawns
each PR as a separate subprocess, up to ``--max-parallel`` at a time (default:
the clone pool size, so every clone slot can stay busy). Tracking
(llm_usage.jsonl + results.json) lands in each PR's cache dir under
.cache/oracles/<project>/<pr_nb>/ — one subdirectory per modified function
(see utils/ResultsLayout.py), aggregated by a top-level manifest.json.
"""

from __future__ import annotations

import argparse
import os
import subprocess
import sys
import threading
import time
from concurrent.futures import ThreadPoolExecutor, as_completed

from patchguru.utils.ClonedRepoManager import ClonedRepoManager
from patchguru.utils.ResultsLayout import iter_function_dirs

# Default cap on concurrent PR subprocesses. Matches the clone pool size so
# every leased clone slot can stay busy; going higher just means more PRs
# blocked waiting on acquire_clone_lease.
DEFAULT_MAX_PARALLEL = ClonedRepoManager.nb_clones


def _produced_output(project: str, pr_nb: int) -> bool:
    """Whether SpecInfer wrote any per-function results for this PR."""
    pr_dir = os.path.join(".cache", "oracles", project, str(pr_nb))
    return any(True for _ in iter_function_dirs(pr_dir))


def _run_one(project: str, pr_nb: int, force: bool) -> tuple[int, bool, int, str]:
    """Run SpecInfer for a single PR. Returns (pr_nb, ok, returncode, stderr_tail)."""
    cmd = ["poetry", "run", "python3", "-m", "patchguru.SpecInfer", "--project", project, "--pr_nb", str(pr_nb)]
    if force:
        cmd.append("--force")

    result = subprocess.run(cmd, capture_output=True, text=True)
    ok = result.returncode == 0 and _produced_output(project, pr_nb)
    stderr_tail = "\n".join(result.stderr.strip().split("\n")[-5:]) if not ok else ""
    return pr_nb, ok, result.returncode, stderr_tail


def batch_analyze(
    project: str,
    pr_list_file: str | None = None,
    force: bool = False,
    max_parallel: int = DEFAULT_MAX_PARALLEL,
) -> None:
    if pr_list_file is None:
        pr_list_file = f"data/pr_data/prs/{project}.txt"

    if not os.path.exists(pr_list_file):
        print(f"ERROR: PR list file not found: {pr_list_file}", file=sys.stderr)
        raise FileNotFoundError(f"PR list file not found: {pr_list_file}")

    with open(pr_list_file) as f:
        pr_numbers = [int(line.strip()) for line in f if line.strip()]

    total = len(pr_numbers)
    start_time = time.strftime("%Y%m%d-%H%M%S")
    print(f"[{start_time}] Starting batch SpecInfer for project: {project}")
    print(f"  PR list: {pr_list_file}")
    print(f"  Total PRs: {total}")
    print(f"  Max parallel: {max_parallel}")

    completed = 0
    print_lock = threading.Lock()
    with ThreadPoolExecutor(max_workers=max_parallel) as executor:
        futures = {executor.submit(_run_one, project, pr_nb, force): pr_nb for pr_nb in pr_numbers}
        for future in as_completed(futures):
            pr_nb, ok, returncode, stderr_tail = future.result()
            with print_lock:
                completed += 1
                ts = time.strftime("%H:%M:%S")
                if ok:
                    print(f"[{ts}] [{completed}/{total}] PR #{pr_nb}... OK")
                else:
                    print(f"[{ts}] [{completed}/{total}] PR #{pr_nb}... FAILED (exit={returncode})")
                    if stderr_tail:
                        print(f"  stderr: {stderr_tail}")

    ts = time.strftime("%H:%M:%S")
    print(f"[{ts}] Batch complete! {total} PRs processed.")


if __name__ == "__main__":
    parser = argparse.ArgumentParser(description="Batch SpecInfer runner for multiple PRs")
    parser.add_argument("--project", type=str, required=True, help="Project name (e.g., marshmallow, pandas)")
    parser.add_argument("--pr_list_file", type=str, default=None,
                        help="Path to PR list file (default: data/pr_data/prs/<project>.txt)")
    parser.add_argument("--force", action="store_true", help="Force re-analysis even if results are cached")
    parser.add_argument("--max-parallel", type=int, default=DEFAULT_MAX_PARALLEL,
                        help=f"Max concurrent PR subprocesses (default: clone pool size = {DEFAULT_MAX_PARALLEL})")
    args = parser.parse_args()

    batch_analyze(args.project, args.pr_list_file, args.force, args.max_parallel)
