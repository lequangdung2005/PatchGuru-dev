"""Efficiency analysis — RQ3 of the paper (Table 3, Figure 4).

Aggregates per-PR LLM token usage, USD cost, and wall-clock time from the
cached ``llm_usage.jsonl`` and ``results.json`` files, then reports
per-project and overall averages and draws the Figure 4 violin plots.

This file also merges the former ``experiments/calc_cost_per_pr.py`` as an
``audit`` subcommand: a focused per-PR / per-model cost breakdown with
filtering, useful for inspecting where spend goes.

Two entry points:
    python3 -m patchguru.experiments.efficiency                    # RQ3 table + plots
    python3 -m patchguru.experiments.efficiency audit [--model ...]  # per-PR cost audit
            [--project pandas] [--single-func-only] [--exclude-failed]
            [--show-status] [--detail]
"""

from __future__ import annotations

import argparse
import json
import os
import pickle
import re
from collections import defaultdict
from datetime import datetime
from glob import glob

import numpy as np

from patchguru import Config
from patchguru.utils import Cost
from patchguru.utils.ResultsLayout import iter_function_dirs, load_phase_results, summarize_pr_outcome

_TS_FMT = "%Y%m%d-%H%M%S"

# Default projects to analyze (mirrors the original hardcoded RQ1_3 list).
DEFAULT_PROJECTS = ["marshmallow"]

# Display names used in the summary table.
_MAPPING = {
    "pandas": "Pandas",
    "scipy": "SciPy",
    "keras": "Keras",
    "marshmallow": "Marshmallow",
}


# ── Shared parsing helpers ────────────────────────────────────────────────


def _read_usage(usage_path):
    """Read a per-PR llm_usage.jsonl file into a list of usage records."""
    records = []
    if not os.path.exists(usage_path):
        return records
    with open(usage_path, "r") as f:
        for line in f:
            if not line.strip():
                continue
            try:
                entry = json.loads(line)
            except json.JSONDecodeError:
                continue
            if "completion_tokens" in entry or "prompt_tokens" in entry:
                records.append(entry)
    return records


def parse_results(project):
    """Build per-PR, per-function result + usage records from
    `.cache/oracles/<project>/`. A PR now runs one independent oracle per
    modified function (see utils/ResultsLayout.py), so each entry holds a
    "functions" dict keyed by function name (or None for pre-multi-function
    single-function cache trees) instead of a single flat results.json."""
    result_dir = f".cache/oracles/{project}"
    results = {}
    if not os.path.isdir(result_dir):
        return results
    for data_id in os.listdir(result_dir):
        pr_dir = os.path.join(result_dir, data_id)
        functions = {}
        for fct_name, fct_dir in iter_function_dirs(pr_dir):
            data = load_phase_results(fct_dir, "phase1")
            if data is None:
                continue
            functions[fct_name] = {
                "results": data,
                "phase2_results": load_phase_results(fct_dir, "phase2"),
                "llm_usage": _read_usage(os.path.join(fct_dir, "llm_usage.jsonl")),
                "phase2_usage": _read_usage(os.path.join(fct_dir, "phase2", "llm_usage.jsonl")),
                "fct_dir": fct_dir,
            }
        if not functions:
            continue
        results[str(data_id)] = {
            "functions": functions,
            "log_dir": pr_dir,
        }
    return results


def _duration_minutes(data, phase2_data) -> float:
    """Wall-clock minutes from start_time→end_time, summing phase 1 + 2."""
    total = 0.0

    def _span(d):
        st, et = d.get("start_time"), d.get("end_time")
        if not st or not et:
            return 0.0
        try:
            return (datetime.strptime(et, _TS_FMT) - datetime.strptime(st, _TS_FMT)).total_seconds()
        except ValueError:
            return 0.0

    total += _span(data)
    if phase2_data:
        total += _span(phase2_data)
    return total / 60


# ── RQ3 aggregation ───────────────────────────────────────────────────────


def analyze(project):
    """Aggregate per-PR token / cost / time usage for one project.

    Returns ``(input_token_usage, output_token_usage, thinking_token_usage,
    cost_usage, output_cost_usage, time_usage)`` — each a dict keyed by PR
    number. Prints warnings for PRs with no usage log or no timing fields.
    """
    result_dir = f".cache/oracles/{project}"
    log_results = parse_results(project)
    n_no_usage = 0
    n_no_timing = 0

    # Calculate token usage statistics
    input_token_usage = {}
    output_token_usage = {}
    thinking_token_usage = {}
    cost_usage = {}
    output_cost_usage = {}
    time_usage = {}

    for pr_nb, log_data in log_results.items():
        usage_records = []
        duration_minutes = 0.0
        for fdata in log_data["functions"].values():
            usage_records += fdata["llm_usage"] + fdata["phase2_usage"]
            duration_minutes += _duration_minutes(fdata["results"], fdata["phase2_results"])

        input_tokens = 0
        output_tokens = 0
        thinking_tokens = 0
        cached_tokens = 0
        total_tokens = 0
        total_cost = 0.0
        output_cost = 0.0
        for usage in usage_records:
            input_tokens += usage.get('prompt_tokens', 0)
            output_tokens += usage.get('completion_tokens', 0)
            thinking_tokens += usage.get('thinking_tokens', 0)
            cached_tokens += usage.get('cached_tokens', 0)
            total_tokens += usage.get('total_tokens', 0)
            cost_field = usage.get('cost')
            if isinstance(cost_field, dict):
                total_cost += cost_field.get('total_cost', 0)
                output_cost += cost_field.get('output_cost', 0) + cost_field.get('thinking_cost', 0)
        input_token_usage[pr_nb] = input_tokens
        output_token_usage[pr_nb] = output_tokens
        thinking_token_usage[pr_nb] = thinking_tokens
        # If no pre-computed cost, recompute per call so each provider's
        # thinking-token semantics (included-in-completion vs. separate)
        # are handled correctly rather than pricing a cross-provider sum.
        if total_cost == 0.0 and usage_records:
            for usage in usage_records:
                cost_info = Cost.calculate_llm_cost(
                    model=usage.get("model", Config.LLM_MODEL),
                    prompt_tokens=usage.get("prompt_tokens", 0),
                    completion_tokens=usage.get("completion_tokens", 0),
                    thinking_tokens=usage.get("thinking_tokens", 0),
                    cached_tokens=usage.get("cached_tokens", 0),
                    total_tokens=usage.get("total_tokens", 0),
                )
                total_cost += cost_info["total_cost"]
                output_cost += cost_info["output_cost"] + cost_info["thinking_cost"]
        cost_usage[pr_nb] = total_cost
        output_cost_usage[pr_nb] = output_cost
        time_usage[pr_nb] = duration_minutes
        if not usage_records:
            n_no_usage += 1
        if time_usage[pr_nb] == 0.0:
            n_no_timing += 1

    if n_no_usage:
        print(
            f"Warning: {n_no_usage} PR(s) for {project} had no llm_usage.jsonl "
            f"records (pre-refactor cache); their token/cost stats are 0."
        )
    if n_no_timing:
        print(
            f"Warning: {n_no_timing} PR(s) for {project} had no start_time/end_time "
            f"(pre-refactor cache); their time stats are 0."
        )

    return input_token_usage, output_token_usage, thinking_token_usage, cost_usage, output_cost_usage, time_usage


def print_rq3_table(projects, usage_by_project):
    """Print the per-project and overall RQ3 averages table."""
    import pandas as pd

    overall_records = []
    merged_input_tokens = []
    merged_output_tokens = []
    merged_thinking_tokens = []
    merged_time = []
    merged_costs = []
    merged_output_costs = []

    for project in projects:
        input_token_usage, output_token_usage, thinking_token_usage, cost_usage, output_cost_usage, time_usage = usage_by_project[project]
        avg_input_tokens = np.mean(list(input_token_usage.values()))
        avg_output_tokens = np.mean(list(output_token_usage.values()))
        avg_thinking_tokens = np.mean(list(thinking_token_usage.values()))
        avg_time = np.mean(list(time_usage.values()))
        avg_cost = np.mean(list(cost_usage.values()))
        avg_output_cost = np.mean(list(output_cost_usage.values()))
        merged_input_tokens.extend(list(input_token_usage.values()))
        merged_output_tokens.extend(list(output_token_usage.values()))
        merged_thinking_tokens.extend(list(thinking_token_usage.values()))
        merged_time.extend(list(time_usage.values()))
        merged_costs.extend(list(cost_usage.values()))
        merged_output_costs.extend(list(output_cost_usage.values()))
        # Compute per-component cost using current model pricing for display
        pricing = Cost.get_model_pricing(Config.LLM_MODEL)
        input_rate = pricing["input"] / 1_000_000
        overall_records.append({
            "Project": project,
            "Avg Input Tokens": round(avg_input_tokens, 1),
            "Avg Output Tokens": round(avg_output_tokens, 1),
            "Avg Thinking Tokens": round(avg_thinking_tokens, 1),
            "Avg Time (m)": round(avg_time, 1),
            "Cost Input ($)": round(avg_input_tokens * input_rate, 6),
            "Cost Output ($)": round(avg_output_cost, 6),
            "Total Cost ($)": round(avg_cost, 6)
        })

    # Calculate total averages
    pricing = Cost.get_model_pricing(Config.LLM_MODEL)
    input_rate = pricing["input"] / 1_000_000
    overall_records.append({
        "Project": "Overall",
        "Avg Input Tokens": round(np.mean(merged_input_tokens), 1),
        "Avg Output Tokens": round(np.mean(merged_output_tokens), 1),
        "Avg Thinking Tokens": round(np.mean(merged_thinking_tokens), 1),
        "Avg Time (m)": round(np.mean(merged_time), 1),
        "Cost Input ($)": round(np.mean(merged_input_tokens) * input_rate, 6),
        "Cost Output ($)": round(np.mean(merged_output_costs), 6),
        "Total Cost ($)": round(np.mean(merged_costs), 6)
    })

    df_overall = pd.DataFrame.from_records(overall_records)
    print("\n--------------- RQ3: Costs and Time ---------------")
    print(df_overall)


def draw_violin_plots(usage_by_project):
    """Draw Figure 4 violin plots for token and time usage."""
    import matplotlib.pyplot as plt
    import seaborn as sns  # noqa: F401  (styling side-effect)
    from matplotlib.lines import Line2D
    from matplotlib.ticker import FuncFormatter

    print("\nDrawing Violin Plots for Token and Time Usage (Figure 4)\n")

    input_token_usage = {p: u[0] for p, u in usage_by_project.items()}
    output_token_usage = {p: u[1] for p, u in usage_by_project.items()}
    thinking_token_usage = {p: u[2] for p, u in usage_by_project.items()}
    time_usage = {p: u[5] for p, u in usage_by_project.items()}

    metrics = [
        ("input_token_usage", "Input Tokens"),
        ("output_token_usage", "Output Tokens"),
        ("thinking_token_usage", "Thinking Tokens"),
        ("time_usage", "Time (minutes)")
    ]

    # Formatter to display thousands as K on y-axis
    def k_formatter(x, pos):
        if x >= 1000 or x <= -1000:
            s = f"{x/1000:.1f}".rstrip('0').rstrip('.')
            return f"{s}K"
        return f"{x:.0f}"

    # Cost rates per token (USD) — pulled from MODEL_PRICING in utils/Cost.py
    pricing = Cost.get_model_pricing(Config.LLM_MODEL)
    INPUT_COST_PER_TOKEN = pricing["input"] / 1_000_000
    OUTPUT_COST_PER_TOKEN = pricing["output"] / 1_000_000  # also covers thinking tokens

    # Formatter factory to show cost on secondary y-axis
    def make_cost_formatter(rate):
        def _fmt(x, pos):
            dollars = x * rate
            if abs(dollars) >= 1000:
                s = f"{dollars/1000:.1f}".rstrip('0').rstrip('.')
                return f"${s}K"
            # Use more precision for small values
            if abs(dollars) < 1:
                return f"${dollars:.3f}"
            return f"${dollars:.2f}"
        return FuncFormatter(_fmt)

    # Calculate shared y-axis limits for token plots
    all_input_values = [val for proj_data in input_token_usage.values() for val in proj_data.values()]
    all_output_values = [val for proj_data in output_token_usage.values() for val in proj_data.values()]
    all_thinking_values = [val for proj_data in thinking_token_usage.values() for val in proj_data.values()]
    token_y_min = 0
    # Guard against empty token lists (e.g. a project with no analyzed PRs) so
    # max() doesn't raise on an empty sequence and abort the whole run.
    token_y_max = max(
        max(all_input_values, default=0),
        max(all_output_values, default=0),
        max(all_thinking_values, default=0),
    ) * 1.05  # Add 5% padding

    for metric_key, metric_label in metrics:
        metric_data = [input_token_usage, output_token_usage, thinking_token_usage, time_usage]
        data_dict = metric_data[metrics.index((metric_key, metric_label))]

        fig, ax = plt.subplots(figsize=(10, 6))

        # Prepare data for violin plot
        project_names = list(data_dict.keys())
        data_values = [list(data_dict[proj].values()) for proj in project_names]

        # Create violin plot
        parts = ax.violinplot(data_values, positions=range(len(project_names)), widths=0.6,
                              showmeans=False, showextrema=False, showmedians=False)

        # Style violin plot bodies
        for pc in parts['bodies']:
            pc.set_facecolor('skyblue')
            pc.set_edgecolor('black')
            pc.set_alpha(0.7)

        # Add median and mean lines for each project
        medians = [np.median(vals) for vals in data_values]
        means = [np.mean(vals) for vals in data_values]

        for x, (median, mean) in enumerate(zip(medians, means)):
            ax.hlines(median, x - 0.3, x + 0.3, colors='blue', linewidth=2, label='Median' if x == 0 else '')
            ax.hlines(mean, x - 0.3, x + 0.3, colors='red', linestyles='dashed', linewidth=1.5, label='Mean' if x == 0 else '')

            # Format values with K suffix for thousands
            def format_value(val):
                if val >= 1000:
                    return f"{val/1000:.1f}K"
                return f"{val:.1f}"

            # Add text labels for median and mean with smart positioning
            if data_values[x]:
                y_range = max(data_values[x]) - min(data_values[x])
                offset = 0.05 * y_range if y_range != 0 else 1
            else:
                offset = 1

        # Configure plot
        ax.set_xticks(range(len(project_names)))
        ax.set_xticklabels(project_names, fontsize=24)
        ax.set_ylabel(metric_label, fontsize=24)
        ax.grid(axis='y', linestyle='--', alpha=0.7)

        # Set font size of y-ticks
        ax.tick_params(axis='y', labelsize=20)
        # Format y-axis ticks as K for thousands
        ax.yaxis.set_major_formatter(FuncFormatter(k_formatter))

        # Set shared y-axis limits for token plots
        if metric_key in ["input_token_usage", "output_token_usage", "thinking_token_usage"]:
            ax.set_ylim(token_y_min, token_y_max)

        # Add corresponding right-side cost axis for token plots
        rate = None
        if metric_key == "input_token_usage":
            rate = INPUT_COST_PER_TOKEN
        elif metric_key in ("output_token_usage", "thinking_token_usage"):
            rate = OUTPUT_COST_PER_TOKEN  # thinking tokens billed at output rate

        if rate is not None:
            ax2 = ax.twinx()
            ax2.set_ylim(ax.get_ylim())
            ax2.yaxis.set_major_formatter(make_cost_formatter(rate))
            ax2.set_ylabel("Cost ($)", fontsize=24)
            ax2.tick_params(axis='y', labelsize=20)

        # Add legend
        legend_elements = [
            Line2D([0], [0], color='blue', linewidth=2, label='Median'),
            Line2D([0], [0], color='red', linestyle='--', linewidth=1.5, label='Mean')
        ]
        ax.legend(handles=legend_elements, loc='upper right', fontsize=20)

        plt.tight_layout()
        output_file = f".cache/violin_plot/{metric_key}.png"
        plt.savefig(output_file, dpi=300, bbox_inches='tight')
        print(f"Saved figure to {output_file}")
        plt.show()


def run_rq3(projects):
    """Run the RQ3 cost/time experiment: aggregate, print table, draw plots."""
    usage_by_project = {}
    for project in projects:
        project_name = _MAPPING.get(project, project)
        usage_by_project[project_name] = analyze(project)

    print_rq3_table(list(usage_by_project.keys()), usage_by_project)
    draw_violin_plots(usage_by_project)


# ── Cost audit (merged from calc_cost_per_pr.py) ─────────────────────────

PKL_DIRS = [".cache/PullRequestData"]

# Per-PR oracle cache root — llm_usage.jsonl lives alongside results.json in
# `.cache/oracles/<project>/<pr_nb>/` (and `.../<pr_nb>/phase2/`).
ORACLE_ROOT = os.path.join(".cache", "oracles")


def find_multi_function_prs() -> set[int]:
    """Scan pickle files for PRs that modify more than one function."""
    multi: set[int] = set()
    for pkl_dir in PKL_DIRS:
        for pkl_path in glob(os.path.join(pkl_dir, "**", "*.pkl"), recursive=True):
            m = re.match(r"pr_(\d+)_", os.path.basename(pkl_path))
            if not m:
                continue
            pr_nb = int(m.group(1))
            try:
                with open(pkl_path, "rb") as fh:
                    pr_data = pickle.load(fh)
                if isinstance(pr_data, dict) and len(pr_data.get("prev_fut_info", {})) > 1:
                    multi.add(pr_nb)
            except (pickle.UnpicklingError, EOFError, OSError):
                continue
    return multi


def find_failed_prs() -> set[int]:
    """Scan every PR's cache_dir for a 'failed' outcome.

    A PR now runs one independent oracle per modified function (see
    utils/ResultsLayout.py), so a PR counts as failed if any of its
    function runs failed or never reached a terminal stage — the same
    BUG-if-any-else-FAILED-if-any-failed policy ``summarize_pr_outcome``
    uses elsewhere.
    """
    failed: set[int] = set()
    for pr_dir in glob(os.path.join(".cache", "oracles", "*", "*"), recursive=False):
        pr_nb_str = os.path.basename(pr_dir)
        if not pr_nb_str.isdigit():
            continue
        outcome = summarize_pr_outcome(pr_dir)
        if outcome["outcome"] == "FAILED":
            failed.add(int(pr_nb_str))
    return failed


def get_pr_stage(pr_nb: int) -> str:
    """Look up the aggregate pipeline stage for a PR across all its function runs."""
    for pr_dir in glob(os.path.join(".cache", "oracles", "*", str(pr_nb)), recursive=False):
        outcome = summarize_pr_outcome(pr_dir)
        if outcome["outcome"] == "missing":
            continue
        if outcome["outcome"] == "BUG":
            return "completed (BUG)"
        if outcome["outcome"] == "NORMAL":
            return "completed (NORMAL)"
        return f"failed ({outcome['failure_reason'] or 'unknown'})"
    return "unknown"


def aggregate_costs(
    model_filter: str | None = None,
    project_filter: str | None = None,
    single_func_only: bool = False,
    exclude_failed: bool = True,
) -> dict[int, dict]:
    """Aggregate LLM costs per PR from per-PR llm_usage.jsonl files.

    Returns a dict keyed by PR number with per-PR cost breakdowns.
    Falls back to recalculating cost from token counts when the ``cost``
    field is absent from the usage record (e.g. pre-refactor cache).
    """
    multi_func = find_multi_function_prs() if single_func_only else set()
    failed_prs = find_failed_prs() if exclude_failed else set()

    usage_files = glob(os.path.join(ORACLE_ROOT, "**", "llm_usage.jsonl"), recursive=True)
    per_pr: dict[int, dict] = defaultdict(
        lambda: {"project": "", "calls": 0, "input": 0, "cached": 0,
                 "output": 0, "thinking": 0, "cost": 0.0, "stage": "unknown",
                 "models": defaultdict(lambda: {
                     "calls": 0, "input": 0, "cached": 0, "output": 0, "thinking": 0, "cost": 0.0
                 }),
                 "_raw_calls": []}
    )

    for uf in usage_files:
        # Path layout: .cache/oracles/<project>/<pr_nb>[/phase2]/llm_usage.jsonl
        parts = uf.split(os.sep)
        if "oracles" not in parts:
            continue
        idx = parts.index("oracles")
        if idx + 2 >= len(parts):
            continue
        project = parts[idx + 1]
        try:
            pr_nb = int(parts[idx + 2])
        except ValueError:
            continue
        if project_filter and project.lower() != project_filter.lower():
            continue
        if pr_nb in multi_func:
            continue
        if pr_nb in failed_prs:
            continue

        calls = []
        with open(uf) as f:
            for line in f:
                if not line.strip():
                    continue
                try:
                    calls.append(json.loads(line))
                except json.JSONDecodeError:
                    continue

        for call in calls:
            model = call.get("model", "unknown")
            if model_filter and not model.startswith(model_filter):
                continue

            per_pr[pr_nb]["project"] = project
            per_pr[pr_nb]["calls"] += 1
            per_pr[pr_nb]["input"] += call.get("prompt_tokens", 0)
            per_pr[pr_nb]["cached"] += call.get("cached_tokens", 0)
            per_pr[pr_nb]["output"] += call.get("completion_tokens", 0)
            per_pr[pr_nb]["thinking"] += call.get("thinking_tokens", 0)
            cost_field = call.get("cost")
            if isinstance(cost_field, dict):
                per_pr[pr_nb]["cost"] += cost_field.get("total_cost", 0)
                per_pr[pr_nb]["models"][model]["cost"] += cost_field.get("total_cost", 0)
            else:
                # Save raw record for fallback recalculation
                per_pr[pr_nb]["_raw_calls"].append(call)
            per_pr[pr_nb]["models"][model]["calls"] += 1
            per_pr[pr_nb]["models"][model]["input"] += call.get("prompt_tokens", 0)
            per_pr[pr_nb]["models"][model]["cached"] += call.get("cached_tokens", 0)
            per_pr[pr_nb]["models"][model]["output"] += call.get("completion_tokens", 0)
            per_pr[pr_nb]["models"][model]["thinking"] += call.get("thinking_tokens", 0)
            if isinstance(cost_field, dict):
                per_pr[pr_nb]["models"][model]["cost"] += cost_field.get("total_cost", 0)

    # Resolve stage for each PR
    for pr_nb in per_pr:
        per_pr[pr_nb]["stage"] = get_pr_stage(pr_nb)

    # Fallback: recalculate cost from token counts for PRs where the `cost`
    # field was never written to llm_usage.jsonl (pre-refactor cache).
    for pr_nb in per_pr:
        if per_pr[pr_nb]["cost"] == 0.0 and per_pr[pr_nb]["_raw_calls"]:
            total_cost = 0.0
            for call in per_pr[pr_nb]["_raw_calls"]:
                model = call.get("model", "unknown")
                cost_info = Cost.calculate_llm_cost(
                    model=call.get("model", Config.LLM_MODEL),
                    prompt_tokens=call.get("prompt_tokens", 0),
                    completion_tokens=call.get("completion_tokens", 0),
                    thinking_tokens=call.get("thinking_tokens", 0),
                    cached_tokens=call.get("cached_tokens", 0),
                    total_tokens=call.get("total_tokens", 0),
                )
                total_cost += cost_info["total_cost"]
                per_pr[pr_nb]["models"][model]["cost"] += cost_info["total_cost"]
            per_pr[pr_nb]["cost"] = total_cost

    # Remove empty entries and clean up internal field
    result = {}
    for k, v in per_pr.items():
        if v["calls"] > 0:
            del v["_raw_calls"]
            result[k] = v
    return result


def print_per_pr_table(per_pr: dict[int, dict], show_status: bool = False) -> None:
    """Print a per-PR cost table."""
    sorted_prs = sorted(per_pr.items(), key=lambda x: (x[1]["project"], x[0]))

    status_header = "  Status" if show_status else ""
    header = f"{'Project':<12} {'PR#':>6}{status_header} {'Calls':>5} {'Input':>10} {'Cached':>10} {'Cache%':>7} {'Output':>10} {'Think':>10} {'Cost':>10}"
    print(header)
    print("-" * len(header))

    for pr, d in sorted_prs:
        cache_pct = f"{d['cached']/d['input']*100:.1f}%" if d["input"] > 0 else "0%"
        status_str = f"  {d['stage'][:6]:<6}" if show_status else ""
        print(f"{d['project']:<12} {pr:>6}{status_str} {d['calls']:>5} {d['input']:>10,} {d['cached']:>10,} {cache_pct:>7} {d['output']:>10,} {d['thinking']:>10,} ${d['cost']:>9.4f}")

    # Totals row
    total_calls = sum(v["calls"] for v in per_pr.values())
    total_input = sum(v["input"] for v in per_pr.values())
    total_cached = sum(v["cached"] for v in per_pr.values())
    total_output = sum(v["output"] for v in per_pr.values())
    total_thinking = sum(v["thinking"] for v in per_pr.values())
    total_cost = sum(v["cost"] for v in per_pr.values())
    cache_pct = f"{total_cached/total_input*100:.1f}%" if total_input > 0 else "0%"

    print("-" * len(header))
    total_status = f"  {'':<6}" if show_status else ""
    print(f"{'TOTAL':<12} {len(per_pr):>6}{total_status} {total_calls:>5} {total_input:>10,} {total_cached:>10,} {cache_pct:>7} {total_output:>10,} {total_thinking:>10,} ${total_cost:>9.4f}")


def print_project_summary(per_pr: dict[int, dict]) -> None:
    """Print per-project cost summary."""
    proj = defaultdict(lambda: {"prs": set(), "calls": 0, "cost": 0.0, "input": 0, "cached": 0})
    for pr, d in per_pr.items():
        p = d["project"]
        proj[p]["prs"].add(pr)
        proj[p]["calls"] += d["calls"]
        proj[p]["cost"] += d["cost"]
        proj[p]["input"] += d["input"]
        proj[p]["cached"] += d["cached"]

    print(f"\n{'Project':<12} {'PRs':>5} {'Calls':>6} {'Total Cost':>12} {'Avg/PR':>10} {'Avg Cache%':>10}")
    print("-" * 60)
    for p in sorted(proj):
        s = proj[p]
        n = len(s["prs"])
        avg_cache = f"{s['cached']/s['input']*100:.1f}%" if s["input"] > 0 else "0%"
        print(f"{p:<12} {n:>5} {s['calls']:>6} ${s['cost']:>11.4f} ${s['cost']/n:>9.4f} {avg_cache:>10}")

    # Overall average row
    all_prs = sum(len(s["prs"]) for s in proj.values())
    all_calls = sum(s["calls"] for s in proj.values())
    all_cost = sum(s["cost"] for s in proj.values())
    all_input = sum(s["input"] for s in proj.values())
    all_cached = sum(s["cached"] for s in proj.values())
    avg_cache = f"{all_cached/all_input*100:.1f}%" if all_input > 0 else "0%"
    print("-" * 60)
    print(f"{'AVERAGE':<12} {all_prs:>5} {all_calls:>6} ${all_cost:>11.4f} ${all_cost/all_prs:>9.4f} {avg_cache:>10}" if all_prs else "")

    # Status breakdown
    status = defaultdict(lambda: {"prs": set(), "calls": 0, "cost": 0.0})
    for pr, d in per_pr.items():
        st = d.get("stage", "unknown")
        status[st]["prs"].add(pr)
        status[st]["calls"] += d["calls"]
        status[st]["cost"] += d["cost"]
    if len(status) > 1:
        print(f"\n  Status breakdown:")
        for st in sorted(status):
            s = status[st]
            print(f"    {st:<20} {len(s['prs']):>3} PRs  {s['calls']:>4} calls  ${s['cost']:.2f}")


def print_model_summary(per_pr: dict[int, dict]) -> None:
    """Print per-model cost summary across all PRs."""
    models = defaultdict(lambda: {"calls": 0, "input": 0, "cached": 0, "output": 0, "thinking": 0, "cost": 0.0})
    for d in per_pr.values():
        for model, m in d["models"].items():
            for k in m:
                models[model][k] += m[k]

    print(f"\n{'Model':<35} {'Calls':>6} {'Input':>10} {'Cached':>10} {'Output':>10} {'Cost':>10}")
    print("-" * 85)
    for model in sorted(models):
        m = models[model]
        print(f"{model:<35} {m['calls']:>6} {m['input']:>10,} {m['cached']:>10,} {m['output']:>10,} ${m['cost']:>9.4f}")


def run_audit(args) -> None:
    """Per-PR cost audit (merged from calc_cost_per_pr.py)."""
    # Print exclusions
    exclusions = []
    if args.single_func_only:
        multi = find_multi_function_prs()
        exclusions.append(f"{len(multi)} multi-function PRs")
    # Failed PRs are excluded by default; pass --include-failed to include them
    exclude_failed = not args.include_failed
    if exclude_failed:
        failed = find_failed_prs()
        exclusions.append(f"{len(failed)} failed PRs")
    if exclusions:
        print(f"Excluding: {', '.join(exclusions)}\n")

    per_pr = aggregate_costs(
        model_filter=args.model,
        project_filter=args.project,
        single_func_only=args.single_func_only,
        exclude_failed=exclude_failed,
    )

    if not per_pr:
        print("No data found matching the filters.")
        return

    print_per_pr_table(per_pr, show_status=args.show_status)
    print_project_summary(per_pr)
    print_model_summary(per_pr)

    total_cost = sum(v["cost"] for v in per_pr.values())
    total_calls = sum(v["calls"] for v in per_pr.values())
    total_cached = sum(v["cached"] for v in per_pr.values())
    print(f"\nGRAND TOTAL: {len(per_pr)} PRs, {total_calls} calls, ${total_cost:.4f}")

    if args.detail:
        for pr, d in sorted(per_pr.items(), key=lambda x: (x[1]["project"], x[0])):
            print(f"\n--- PR #{pr} ({d['project']}) [{d.get('stage', '?')}] ---")
            for model, m in d["models"].items():
                print(f"  {model}: {m['calls']} calls, {m['input']} input, {m['cached']} cached, ${m['cost']:.4f}")


# ── CLI ───────────────────────────────────────────────────────────────────

def _add_audit_args(sub) -> None:
    sub.add_argument("--model", default=None, help="Filter by model prefix (e.g. 'deepseek', 'gemini', 'gpt')")
    sub.add_argument("--project", default=None, help="Filter by project name (marshmallow, pandas, scipy, keras)")
    sub.add_argument("--single-func-only", action="store_true", help="Exclude PRs that modify multiple functions")
    sub.add_argument("--include-failed", action="store_true", help="Include PRs whose pipeline stage is 'failed' (excluded by default)")
    sub.add_argument("--show-status", action="store_true", help="Show pipeline stage (completed/failed) for each PR")
    sub.add_argument("--detail", action="store_true", help="Show per-model breakdown for each PR")


def main():
    parser = argparse.ArgumentParser(description="Efficiency: RQ3 (costs and time) of PatchGuru")
    subparsers = parser.add_subparsers(dest="command")

    # `RQ3` (default) — RQ3 experiment: table + violin plots
    ex = subparsers.add_parser("experiment", help="Run the RQ3 cost/time analysis (default)")
    ex.add_argument(
        "--projects",
        default=",".join(DEFAULT_PROJECTS),
        help="Comma-separated project names to analyze (default: %(default)s)",
    )

    # `RQ3 audit` — per-PR cost audit (merged from calc_cost_per_pr.py)
    audit = subparsers.add_parser("audit", help="Per-PR / per-model cost audit")
    _add_audit_args(audit)

    args = parser.parse_args()

    if args.command == "audit":
        run_audit(args)
    else:
        projects = [p.strip() for p in args.projects.split(",") if p.strip()]
        run_rq3(projects)


if __name__ == "__main__":
    main()