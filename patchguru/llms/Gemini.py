import os

import google.genai as genai
from google.genai import types as genai_types

from patchguru.utils.RunContext import record_llm_usage

GEMINI_KEY_EMPTY_MSG = "Gemini API key is empty"

with open(".api_token") as f:
    gemini_key = f.read().strip()
    if not gemini_key:
        raise ValueError(GEMINI_KEY_EMPTY_MSG)

os.environ["GOOGLE_API_KEY"] = gemini_key
client = genai.Client(api_key=gemini_key)


def _query_gemini(prompt, model, temperature, max_tokens=65536):
    """
    Query the Gemini API with the given prompt and parameters.
    Internal — use patchguru.llms.query_llm instead.
    """
    try:
        response = client.models.generate_content(
            model=model,
            contents=prompt,
            config=genai_types.GenerateContentConfig(
                temperature=temperature,
                max_output_tokens=max_tokens,
                thinking_config=genai_types.ThinkingConfig(
                    thinking_level=genai_types.ThinkingLevel.MEDIUM,
                ),
            ),
        )
        response_msg = response.text
        usage = response.usage_metadata

        # Handle None response (blocked content, safety filter, etc.)
        if response_msg is None:
            return None

        # Guard against missing usage metadata
        if usage is None:
            return response_msg

        record_llm_usage({
            "model": model,
            "completion_tokens": usage.candidates_token_count,
            "prompt_tokens": usage.prompt_token_count,
            "thinking_tokens": usage.thoughts_token_count if usage.thoughts_token_count else 0,
            "cached_tokens": usage.cached_content_token_count if usage.cached_content_token_count else 0,
            "total_tokens": usage.total_token_count,
        })
        return response_msg
    except Exception:
        raise
