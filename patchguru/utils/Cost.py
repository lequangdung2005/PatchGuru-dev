"""LLM cost accounting: per-model pricing and per-call USD cost.

Extracted from ``Config.py`` so that configuration stays configuration
and billing arithmetic lives with its own unit of logic. The pricing
table is data (rates per 1M tokens), the two functions are the only
places that know how to turn token usage into USD.

Consumers:
    * :func:`RunContext.record_llm_usage` — stamps each LLM call with its
      cost as it is appended to ``llm_usage.jsonl``.
    * ``experiments/efficiency.py`` — per-PR cost aggregation, the RQ3
      cost/time table, and the ``audit`` subcommand's per-PR / per-model
      cost breakdown, including back-of-envelope estimates from token counts
      when a usage record predates cost computation.
"""

from __future__ import annotations

# ── Pricing per 1M tokens (USD) ─────────────────────────────────────────────
# Sources:
#   GPT-5.4 mini:  https://developers.openai.com/api/docs/models/gpt-5.4-mini
#   Gemini 3 Flash: https://ai.google.dev/pricing
#   DeepSeek V3:    https://api-docs.deepseek.com/quick_start/pricing
# Thinking tokens are billed at the output rate, but the way they surface
# in usage metadata differs by provider:
#   OpenAI / DeepSeek: completion_tokens INCLUDES reasoning tokens
#                      (usage.total = prompt + completion)
#   Gemini:            candidates_token_count EXCLUDES thoughts; they are
#                      reported separately (usage.total = prompt + completion
#                      + thoughts) and billed on top of completion
# Whether thinking is already inside completion_tokens is detected at
# call time from the usage total (see calculate_llm_cost).
MODEL_PRICING = {
    "gpt-5.4-mini": {
        "input": 0.75,
        "cached_input": 0.075,
        "output": 4.50,
    },
    "gemini-3-flash-preview": {
        "input": 0.50,          # text/image/video (audio: $1.00)
        "cached_input": 0.05,
        "output": 3.00,
    },
    "deepseek/deepseek-v4-flash": {
        "input": 0.14,
        "cached_input": 0.0028,
        "output": 0.28,
    },
    "default": {
        "input": 0.75,
        "output": 4.50,
    },
}


def get_model_pricing(model: str) -> dict:
    """Return (input_rate, output_rate) per 1M tokens for the given model.

    Falls back to MODEL_PRICING['default'] for unknown models.
    """
    if model in MODEL_PRICING:
        return MODEL_PRICING[model]
    # Try prefix match, longest first (e.g. "gemini-2.5-flash-preview-xx"
    # should match "gemini-2.5-flash-preview", not "gemini-2.5-flash").
    for known in sorted(MODEL_PRICING, key=len, reverse=True):
        if known != "default" and model.startswith(known):
            return MODEL_PRICING[known]
    return MODEL_PRICING["default"]


def calculate_llm_cost(model: str, prompt_tokens: int, completion_tokens: int,
                       thinking_tokens: int = 0, cached_tokens: int = 0,
                       total_tokens: int = 0) -> dict:
    """Calculate USD cost for a single LLM call.

    Returns a dict with 'input_cost', 'output_cost', 'thinking_cost',
    'cached_cost', and 'total_cost' – all in USD.
    """
    pricing = get_model_pricing(model)
    input_rate = pricing["input"] / 1_000_000
    output_rate = pricing["output"] / 1_000_000
    cached_rate = pricing.get("cached_input", input_rate) / 1_000_000

    # `prompt_tokens` already includes the cached portion, so only the
    # non-cached remainder is billed at the full input rate.
    uncached_prompt_tokens = max(0, prompt_tokens - cached_tokens)
    input_cost = uncached_prompt_tokens * input_rate

    output_cost = completion_tokens * output_rate

    # OpenAI / DeepSeek count thinking tokens inside completion_tokens
    # (total = prompt + completion), so no extra charge. Gemini reports
    # thoughts separately (total = prompt + completion + thoughts) and
    # bills them at the output rate. Detect the case via the usage total;
    # when the total is unknown, assume thinking is already included.
    thinking_included = (total_tokens <= 0) or (
        prompt_tokens + completion_tokens == total_tokens
    )
    thinking_cost = 0.0 if thinking_included else thinking_tokens * output_rate

    cached_cost = cached_tokens * cached_rate

    return {
        "input_cost": input_cost,
        "output_cost": output_cost,
        "thinking_cost": thinking_cost,
        "cached_cost": cached_cost,
        "total_cost": input_cost + output_cost + thinking_cost + cached_cost,
        "model": model,
        "pricing_used": pricing,
    }
