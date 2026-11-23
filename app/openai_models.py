"""OpenAI models supported by the application and their chat request settings."""

DEFAULT_MODEL = "gpt-6.1-terra"
REASONING_MODEL = "gpt-6.2-sol"
VOICE_MODEL = "gpt-transcribe"
IMAGE_MODEL = "gpt-image-2.6-flare"

AGENT_MODELS = {
    DEFAULT_MODEL: {
        "label": "GPT-6.1 Terra",
        "description": "Default for project conversations, design and photo inspection.",
    },
    REASONING_MODEL: {
        "label": "GPT-6.2 Sol",
        "description": "Optional reasoning model for complex design decisions.",
    },
}


def chat_parameters(model: str, output_tokens: int) -> dict:
    """Leave room for Sol's reasoning tokens within a bounded completion."""
    reasoning = model == REASONING_MODEL
    return {
        "model": model,
        "reasoning_effort": "low" if reasoning else "none",
        "max_completion_tokens": output_tokens + (1024 if reasoning else 0),
        "store": False,
    }
