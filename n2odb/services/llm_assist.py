"""Shared language-model seam.

A tiny gate the rest of the app checks before doing anything that would call a
model, plus the single structured-extraction call helper. Keeping both here means
there is one place that answers "is the model assist usable?" and one place that
talks to the Anthropic SDK (manuscript-llm-module.md §2).

The manuscript feature calls ``parse_structured`` from
``manuscript_extract.extract_facts``; this module owns the on/off decision and the
raw API boundary, nothing domain-specific.
"""
from __future__ import annotations

import logging
from typing import Any

import anthropic
from decouple import config
from django.conf import settings

logger = logging.getLogger(__name__)

# Per-million-token USD rates (input, output). Cache writes bill at 1.25× input
# for a 5m TTL and 2× for 1h; cache reads at 0.1× input. Used only to annotate
# the usage log — approximate.
_PRICES = {
    "claude-haiku-5-5": (0.10, 0.50),  # prompts <=100K tokens; $0.50/$2.50 above
    "claude-opus-4-8": (15.0, 75.0),
    "claude-sonnet-4-6": (3.0, 15.0),
    "claude-haiku-4-5-20251001": (1.0, 5.0),
}


def _log_usage(model: str, usage: Any) -> None:
    """Emit one line with token counts and an estimated USD cost for a call."""
    if usage is None:
        return
    inp = getattr(usage, "input_tokens", 0) or 0
    out = getattr(usage, "output_tokens", 0) or 0
    cache_write = getattr(usage, "cache_creation_input_tokens", 0) or 0
    cache_read = getattr(usage, "cache_read_input_tokens", 0) or 0
    in_rate, out_rate = _PRICES.get(model, (0.0, 0.0))
    cc = cache_control()
    write_mult = 2.0 if cc and cc.get("ttl") == "1h" else 1.25
    cost = (
        inp * in_rate
        + cache_write * in_rate * write_mult
        + cache_read * in_rate * 0.1
        + out * out_rate
    ) / 1_000_000
    logger.info(
        "manuscript LLM call: model=%s input=%d cache_write=%d cache_read=%d "
        "output=%d est_cost=$%.4f",
        model, inp, cache_write, cache_read, out, cost,
    )


class LLMUnavailable(Exception):
    """A model call could not be completed (API error or no parseable output).

    Callers translate this into a graceful "failed"/"skipped" outcome — the seam
    never lets a model problem surface as an unhandled exception in a request.
    """


def cache_control() -> dict | None:
    """The ``cache_control`` block for prompt segments, or ``None`` for no caching.

    Caching is opt-in via ``WORKBENCH_LLM_CACHE_TTL`` = ``5m`` or ``1h``; any other
    value (the default ``none`` included) disables it. Rationale: cache *writes*
    bill at a premium (1.25x input for 5m, 2x for 1h) that only pays off when the
    same prompt is re-sent within the TTL — and the sha256 dedupe means an
    unchanged manuscript normally makes exactly one call, so the premium would buy
    nothing. Enable a TTL only if force re-extractions become a common pattern.
    """
    ttl = str(getattr(settings, "WORKBENCH_LLM_CACHE_TTL", "none")).lower()
    if ttl in ("5m", "1h"):
        return {"type": "ephemeral", "ttl": ttl}
    return None


def text_block(text: str) -> dict:
    """A text content block, cache-tagged only when a cache TTL is configured."""
    block: dict = {"type": "text", "text": text}
    cc = cache_control()
    if cc:
        block["cache_control"] = cc
    return block


def is_enabled() -> bool:
    """True only if the feature flag is on AND an API key is configured.

    Both must hold: the flag (``WORKBENCH_LLM_ENABLED``) is the operator's switch,
    and a key is what makes a call actually possible. With either missing the
    manuscript pipeline degrades gracefully — text is still captured and the
    deterministic parsers still run.
    """
    if not getattr(settings, "WORKBENCH_LLM_ENABLED", False):
        return False
    api_key = config("ANTHROPIC_API_KEY", default="")
    return bool(api_key)


def _client() -> "anthropic.Anthropic":
    # This app keeps secrets in a .env read by python-decouple (not the process
    # environment the SDK auto-reads), so pass the key through explicitly. Only
    # called after ``is_enabled()`` has confirmed a key is present.
    #
    # Bounded timeout + one retry: extraction runs synchronously inside the
    # request, so a stalled call must fail fast (→ "failed") rather than pin a
    # worker for the SDK's default multi-minute, max_tokens-scaled timeout.
    return anthropic.Anthropic(
        api_key=config("ANTHROPIC_API_KEY", default=""),
        timeout=120.0,
        max_retries=1,
    )


def parse_structured(
    *,
    system: str,
    content: list,
    output_model: type,
    model: str,
    max_tokens: int = 16000,  # thinking counts toward this, so leave room beyond the JSON
    usage_out: list | None = None,
) -> Any:
    """One structured-output call → a validated ``output_model`` instance.

    The ``system`` block (stable instructions + field catalogue) is sent as a
    cacheable text block; ``content`` is the per-request user content (the delimited
    manuscript). If ``usage_out`` is given, the response's token usage is appended to it. Raises ``LLMUnavailable`` on any API failure or if the model
    returns nothing parseable — it never returns ``None``.
    """
    try:
        resp = _client().messages.parse(
            model=model,
            max_tokens=max_tokens,
            system=[text_block(system)],
            messages=[{"role": "user", "content": content}],
            output_format=output_model,
            output_config={"effort": settings.WORKBENCH_LLM_EFFORT},
        )
    except anthropic.APIError as exc:  # network, rate-limit, 4xx/5xx, etc.
        raise LLMUnavailable(str(exc)) from exc

    _log_usage(model, getattr(resp, "usage", None))
    if usage_out is not None and getattr(resp, "usage", None) is not None:
        usage_out.append(resp.usage)
    # Safety classifiers can decline (HTTP 200, no parsed output); say so in the log.
    if getattr(resp, "stop_reason", None) == "refusal":
        raise LLMUnavailable(f"model declined the request: {getattr(resp, 'stop_details', None)}")
    parsed = resp.parsed_output
    if parsed is None:
        raise LLMUnavailable("model returned no parseable structured output")
    return parsed
