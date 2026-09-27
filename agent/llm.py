"""Thin wrapper over Token Factory's OpenAI-compatible API with per-call usage logging."""
import json
import time

from openai import OpenAI

from . import config
from .store import store

_client = None


def client() -> OpenAI:
    global _client
    if _client is None:
        _client = OpenAI(base_url=config.NEBIUS_BASE_URL, api_key=config.NEBIUS_API_KEY)
    return _client


TIER_BY_MODEL = lambda m: {config.MODEL_NANO: "nano", config.MODEL_SUPER: "super", config.MODEL_ULTRA: "ultra"}.get(m, "other")


def chat(model: str, messages: list, incident_id: str | None = None, tools: list | None = None,
         temperature: float = 0.1, max_tokens: int = 2048, json_mode: bool = False):
    """Call a model and record tier, tokens and latency. Returns the raw message object."""
    kwargs = dict(model=model, messages=messages, temperature=temperature, max_tokens=max_tokens)
    if tools:
        kwargs["tools"] = tools
        kwargs["tool_choice"] = "auto"
    if json_mode:
        kwargs["response_format"] = {"type": "json_object"}
    extra = config.EXTRA_BODY.get(TIER_BY_MODEL(model))
    if extra:
        kwargs["extra_body"] = json.loads(extra)

    t0 = time.time()
    resp = client().chat.completions.create(**kwargs)
    latency_ms = int((time.time() - t0) * 1000)

    usage = resp.usage
    store.log_call(
        incident_id=incident_id,
        tier=TIER_BY_MODEL(model),
        model=model,
        prompt_tokens=getattr(usage, "prompt_tokens", 0) or 0,
        completion_tokens=getattr(usage, "completion_tokens", 0) or 0,
        latency_ms=latency_ms,
    )
    return resp.choices[0].message


def parse_json(text: str) -> dict:
    """Best-effort JSON extraction (models sometimes wrap JSON in prose or code fences)."""
    if not text:
        return {}
    if "</think>" in text:  # reasoning models may prepend their thinking; keep only the answer
        text = text.split("</think>")[-1]
    text = text.strip()
    if text.startswith("```"):
        text = text.strip("`")
        text = text.split("\n", 1)[1] if "\n" in text else text
    start, end = text.find("{"), text.rfind("}")
    if start == -1 or end == -1:
        return {}
    try:
        return json.loads(text[start:end + 1])
    except json.JSONDecodeError:
        return {}
