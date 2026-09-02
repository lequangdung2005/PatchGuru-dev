import os

from openai import OpenAI

from patchguru import Config
from patchguru.utils.RunContext import record_llm_usage

OPENAI_KEY_EMPTY_MSG = "OpenAI API key is empty"

with open(".api_token") as f:
    openai_key = f.read().strip()
    if not openai_key:
        raise ValueError(OPENAI_KEY_EMPTY_MSG)

os.environ["OPENAI_API_KEY"] = openai_key

# Reasoning effort for OpenAI reasoning models (gpt-5, o-series).
# Valid values: "low", "medium", "high". Analogous to the thinking level
# configured in Gemini.py.
OPENAI_THINKING_LEVEL = "medium"

_clients: dict[str, OpenAI] = {}


def _get_client(model: str) -> OpenAI:
    """Return a cached OpenAI client for the given model's base URL."""
    base_url = Config.get_llm_base_url(model) or "default"
    if base_url not in _clients:
        if base_url == "default":
            _clients[base_url] = OpenAI(api_key=openai_key)
        else:
            _clients[base_url] = OpenAI(api_key=openai_key, base_url=base_url)
    return _clients[base_url]


def _query_openai(prompt, model, temperature, max_tokens=65536):
    """
    Query the OpenAI API with the given prompt and parameters.
    Internal — use patchguru.llms.query_llm instead.
    """
    client = _get_client(model)
    base_url = Config.get_llm_base_url(model)

    # Reasoning-effort control, applied for every model routed through the
    # OpenAI client. OpenAI reasoning models (gpt-5, o-series) accept it as a
    # first-class parameter; OpenRouter exposes the same knob as an extra
    # `reasoning: {effort}` field for the models it serves (e.g. deepseek).
    request_kwargs: dict = {
        "model": model,
        "messages": [{"role": "user", "content": prompt}],
    }
    if model.startswith("gpt-5"):
        request_kwargs["reasoning_effort"] = OPENAI_THINKING_LEVEL
        request_kwargs["max_completion_tokens"] = max_tokens
    else:
        request_kwargs["temperature"] = temperature
        request_kwargs["max_tokens"] = max_tokens
        if base_url and "openrouter" in base_url:
            request_kwargs["extra_body"] = {"reasoning": {"effort": OPENAI_THINKING_LEVEL}}

    try:
        response = client.chat.completions.create(**request_kwargs)
        # Bind usage before any branch that references it: the empty-choices
        # path below records tokens and used to reference `usage` before
        # assignment (UnboundLocalError), crashing the whole per-PR subprocess.
        usage = response.usage
        # An empty choices list means the API returned no completion
        # (transient error / safety refusal at the request level).
        if not response.choices:
            record_llm_usage({
                "model": model,
                "completion_tokens": 0,
                "prompt_tokens": getattr(usage, "prompt_tokens", 0) or 0,
                "thinking_tokens": 0,
                "cached_tokens": 0,
                "total_tokens": getattr(usage, "total_tokens", 0) or 0,
                "finish_reason": "no_choices",
            })
            return None

        choice = response.choices[0]
        response_msg = choice.message.content
        finish_reason = getattr(choice, "finish_reason", None)

        # Extract reasoning + cached tokens from newer OpenAI models (gpt-5, o-series)
        reasoning_tokens = 0
        cached_tokens = 0
        if usage is not None:
            if hasattr(usage, 'completion_tokens_details') and usage.completion_tokens_details:
                reasoning_tokens = getattr(usage.completion_tokens_details, 'reasoning_tokens', 0) or 0
            if hasattr(usage, 'prompt_tokens_details') and usage.prompt_tokens_details:
                cached_tokens = getattr(usage.prompt_tokens_details, 'cached_tokens', 0) or 0
            if not cached_tokens:
                # DeepSeek-compatible endpoints report cache hits at the top
                # level of usage rather than inside prompt_tokens_details.
                cached_tokens = getattr(usage, 'prompt_cache_hit_tokens', 0) or 0

        # Handle None response: reasoning model exhausted its output budget on
        # thinking (finish_reason="length"), a content filter, or a refusal.
        # Record the diagnostic so the failure is traceable instead of silent.
        if response_msg is None:
            record_llm_usage({
                "model": model,
                "completion_tokens": getattr(usage, "completion_tokens", 0) or 0,
                "prompt_tokens": getattr(usage, "prompt_tokens", 0) or 0,
                "thinking_tokens": reasoning_tokens,
                "cached_tokens": cached_tokens,
                "total_tokens": getattr(usage, "total_tokens", 0) or 0,
                "finish_reason": finish_reason,
            })
            return None

        # Guard against missing usage metadata
        if usage is None:
            return response_msg

        record_llm_usage({
            "model": model,
            "completion_tokens": usage.completion_tokens,
            "prompt_tokens": usage.prompt_tokens,
            "thinking_tokens": reasoning_tokens,
            "cached_tokens": cached_tokens,
            "total_tokens": usage.total_tokens,
            "finish_reason": finish_reason,
        })
        return response_msg
    except Exception:
        raise
