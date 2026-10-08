"""One place that calls Claude for the inbox agent (the assistant chat has its own loop)."""
FALLBACK_MODELS = ("claude-opus-5-5", "claude-opus-5", "claude-sonnet-5-5", "claude-fable-5-1")


def create(client, model, **kw):
    """messages.create with the server-side refusal fallback on the models that support it."""
    if model in FALLBACK_MODELS:
        return client.beta.messages.create(model=model, betas=["server-side-fallback-2026-07-01"], fallbacks="default", **kw)
    return client.messages.create(model=model, **kw)


def text_of(resp):
    return "".join(b.text for b in resp.content if b.type == "text").strip()
