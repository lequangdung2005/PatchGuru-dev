"""
Unified LLM query interface for PatchGuru.

Auto-detects the LLM provider from the model name and delegates
to the appropriate backend (Gemini or OpenAI).

Usage:
    from patchguru.llms import query_llm
    response = query_llm("What is Python?")
    response = query_llm("...", model="gpt-5", temperature=0.5)
"""

from patchguru import Config


def _detect_provider(model: str) -> str:
    """Determine the LLM provider from the model name.

    Returns 'gemini' for Gemini models, 'openai' for everything else.
    """
    if model.lower().startswith("gemini"):
        return "gemini"
    return "openai"


def query_llm(prompt, model=None, temperature=None, max_tokens=65536):
    """Unified LLM query interface.

    Auto-detects provider from model name and delegates to the
    appropriate backend. Provider-specific default temperatures
    are applied when `temperature` is None.

    Args:
        prompt: The prompt string to send.
        model: Model name (defaults to Config.LLM_MODEL).
        temperature: Sampling temperature (defaults to 0.2 for Gemini, 0.7 for OpenAI).
        max_tokens: Maximum output tokens (default 65536).

    Returns:
        The LLM response text, or None if blocked by content/safety filters.
    """
    if model is None:
        model = Config.LLM_MODEL

    provider = _detect_provider(model)

    if temperature is None:
        temperature = 0.2 if provider == "gemini" else 0.7

    if provider == "gemini":
        from patchguru.llms.Gemini import _query_gemini
        return _query_gemini(prompt, model=model, temperature=temperature, max_tokens=max_tokens)
    else:
        from patchguru.llms.OpenAI import _query_openai
        return _query_openai(prompt, model=model, temperature=temperature, max_tokens=max_tokens)
