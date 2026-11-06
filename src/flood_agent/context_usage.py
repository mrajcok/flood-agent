"""Context Usage: the share of the model's context window a Conversation occupies (see GLOSSARY.md).

The number is the provider's token count for the latest LLM call; the window comes from config.
The page shows it as "187k of 1M" with a bar that turns amber at 40% and red at 70%.
"""

AMBER_AT = 0.40
RED_AT = 0.70


def format_tokens(n: int) -> str:
    """187_400 -> "187k", 1_050_000 -> "1.05M"."""
    if n >= 999_500:
        return f"{n / 1_000_000:.2f}".rstrip("0").rstrip(".") + "M"
    if n >= 1000:
        return f"{round(n / 1000)}k"
    return str(n)


def context_usage(tokens: int | None, window: int) -> dict:
    """What the meter shows. `window` 0 means no model (Fake Mode), so no context is used."""
    if not window:
        return {"text": "no context used", "percent": 0, "level": "", "window": 0}
    used = tokens or 0
    share = used / window
    level = "red" if share >= RED_AT else "amber" if share >= AMBER_AT else ""
    return {"text": f"{format_tokens(used)} of {format_tokens(window)}", "percent": min(100, round(share * 100, 1)),
            "level": level, "window": window}
