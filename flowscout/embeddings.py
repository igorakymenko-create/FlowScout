"""Text embeddings, used by semantic_dedup.py and gap_analysis.py to
catch flow duplicates / TCMS matches that exact/structural comparison
misses -- same intent, different wording or action sequence.

Multi-provider abstraction added (Aug 2026) -- originally Gemini-only,
with the choice hardcoded (`_API_KEY_ENV = "GEMINI_API_KEY"`, no
abstraction at all). Widened after a direct question: not every operator
has a Gemini key -- some run on Anthropic (which has no embeddings model
of its own; Voyage AI is Anthropic's own recommended embeddings partner)
or a local model.

**openai and voyage are commented out below, PAUSED, not removed --
Aug 2026.** Both were written from each provider's documented API
contract with no live key to test against (unlike everything else in
this project -- see ROADMAP.md throughout). A real OpenAI key became
available and was used for the first live check: `GET /v1/models`
succeeded (key valid, `text-embedding-3-small` visible), but the actual
embeddings call returned `429 insufficient_quota` -- an account-level
billing/balance problem, not a bug in the request/response handling
here (confirmed: it's a different error code than a rate-limit error
would be). Still genuinely unverified as a result -- the one thing that
actually matters (does the success-path response parsing match a real
200 payload) was never exercised. Commented out at the user's explicit
request rather than left registered-but-broken. To re-enable: uncomment
the OpenAI/Voyage sections below and their two lines in `_PROVIDERS`,
then re-run the smoke test once billing is sorted:
`python -c "from flowscout.embeddings import embed_text; print(len(embed_text('hello world', provider='openai')))"`
should print a plausible vector length (1536 for text-embedding-3-small)
with no exception. Voyage (voyage-3.5, 1024-dim) has had no live check
at all yet, key or no key.

**Threshold calibration does NOT carry over between providers.**
semantic_dedup.py's 0.95 and gap_analysis.py's 0.74 were both measured
empirically against Gemini's own similarity distribution -- real false-
merge bugs were found and fixed while calibrating them (see ROADMAP.md's
M1/M2 entries), not picked once and assumed correct. A different
provider's embedding space won't necessarily cluster the same way at
the same thresholds. Switching provider without redoing that same
live-measurement discipline risks silently reintroducing exactly the
bugs those thresholds exist to prevent -- pass an explicit threshold
(don't trust the Gemini-tuned defaults) until you've measured your own
provider's distribution the same way. Moot while openai/voyage are
paused, kept here for whoever re-enables them.
"""
from __future__ import annotations

import json
import os
import urllib.error
import urllib.request

DEFAULT_PROVIDER = "gemini"


class EmbeddingsUnavailable(Exception):
    """Embeddings can't be computed right now (no key, API error, network,
    unknown provider). Callers catch this and degrade to structural-dedup-
    only / not_found gap status -- a crawl should never fail just because
    semantic comparison couldn't run."""


# ---------------------------------------------------------------------
# Gemini -- the original, live-verified implementation (see module
# docstring). Unchanged in behavior from before the provider split.
# ---------------------------------------------------------------------
# Model choice: there is no Flash/Pro split for Gemini embeddings (that's
# a generative-model concept) -- as of Aug 2026 the available embedding
# models are gemini-embedding-001, gemini-embedding-2, and
# gemini-embedding-2-preview. Verified live against this project's own
# key (GET /v1beta/models) rather than docs, which gave inconsistent
# answers across pages. gemini-embedding-001 has a documented free-tier
# quota problem on batch embedding
# (github.com/RooCodeInc/Roo-Code/issues/5713, HTTP 429 on the "Batch
# Embed Content API requests" metric); gemini-embedding-2 is GA (not
# preview), doubles the input token limit (8192 vs 2048 -- matters once
# flows have many steps), and passed an 8-call back-to-back burst test on
# this key with zero failures, same as -001. No reason found to prefer
# -001 or the -preview variant.
_GEMINI_MODEL = "gemini-embedding-2"
_GEMINI_ENDPOINT = f"https://generativelanguage.googleapis.com/v1beta/models/{_GEMINI_MODEL}:embedContent"
_GEMINI_KEY_ENV = "GEMINI_API_KEY"


def _gemini_configured() -> bool:
    return bool(os.environ.get(_GEMINI_KEY_ENV))


def _gemini_embed(text: str, task_type: str) -> list[float]:
    key = os.environ.get(_GEMINI_KEY_ENV)
    if not key:
        raise EmbeddingsUnavailable(f"{_GEMINI_KEY_ENV} is not set")

    body = json.dumps({
        "content": {"parts": [{"text": text}]},
        "embedContentConfig": {"taskType": task_type},
    }).encode("utf-8")
    req = urllib.request.Request(
        _GEMINI_ENDPOINT, data=body, method="POST",
        headers={"Content-Type": "application/json", "x-goog-api-key": key},
    )
    try:
        with urllib.request.urlopen(req, timeout=20) as resp:
            payload = json.loads(resp.read())
    except urllib.error.HTTPError as exc:
        detail = exc.read().decode("utf-8", errors="replace")[:500]
        raise EmbeddingsUnavailable(f"Gemini embeddings API returned {exc.code}: {detail}") from exc
    except urllib.error.URLError as exc:
        raise EmbeddingsUnavailable(f"Gemini embeddings API unreachable: {exc}") from exc
    try:
        return payload["embedding"]["values"]
    except (KeyError, TypeError) as exc:
        raise EmbeddingsUnavailable(f"Unexpected response shape from Gemini embeddings API: {payload}") from exc


# ---------------------------------------------------------------------
# OpenAI -- PAUSED, see module docstring (key obtained, but blocked on
# account billing/quota before the actual embed call could be verified).
# Commented out rather than left registered with a provider that's known
# not to work yet. Uncomment this block + its line in _PROVIDERS below
# to re-enable.
# ---------------------------------------------------------------------
# text-embedding-3-small over -large: cheaper, and semantic_dedup/
# gap_analysis only ever need relative similarity within one run, not
# maximum absolute embedding quality -- the same "GA, practical default"
# reasoning gemini-embedding-2 was picked for, not a measured comparison
# (no key to measure with). Reconsider once real usage data exists.
# _OPENAI_MODEL = "text-embedding-3-small"
# _OPENAI_ENDPOINT = "https://api.openai.com/v1/embeddings"
# _OPENAI_KEY_ENV = "OPENAI_API_KEY"
#
#
# def _openai_configured() -> bool:
#     return bool(os.environ.get(_OPENAI_KEY_ENV))
#
#
# def _openai_embed(text: str, task_type: str) -> list[float]:
#     # task_type (Gemini's SEMANTIC_SIMILARITY/RETRIEVAL_QUERY/... hint)
#     # has no OpenAI equivalent -- OpenAI's embeddings API takes no
#     # comparable parameter, so it's silently accepted and ignored here
#     # rather than raising, matching how a provider-agnostic caller
#     # shouldn't need to know which providers support the hint.
#     key = os.environ.get(_OPENAI_KEY_ENV)
#     if not key:
#         raise EmbeddingsUnavailable(f"{_OPENAI_KEY_ENV} is not set")
#
#     body = json.dumps({"model": _OPENAI_MODEL, "input": text}).encode("utf-8")
#     req = urllib.request.Request(
#         _OPENAI_ENDPOINT, data=body, method="POST",
#         headers={"Content-Type": "application/json", "Authorization": f"Bearer {key}"},
#     )
#     try:
#         with urllib.request.urlopen(req, timeout=20) as resp:
#             payload = json.loads(resp.read())
#     except urllib.error.HTTPError as exc:
#         detail = exc.read().decode("utf-8", errors="replace")[:500]
#         raise EmbeddingsUnavailable(f"OpenAI embeddings API returned {exc.code}: {detail}") from exc
#     except urllib.error.URLError as exc:
#         raise EmbeddingsUnavailable(f"OpenAI embeddings API unreachable: {exc}") from exc
#     try:
#         return payload["data"][0]["embedding"]
#     except (KeyError, IndexError, TypeError) as exc:
#         raise EmbeddingsUnavailable(f"Unexpected response shape from OpenAI embeddings API: {payload}") from exc


# ---------------------------------------------------------------------
# Voyage AI -- PAUSED, see module docstring. No live check at all yet,
# key or no key. Commented out rather than left registered untested.
# Uncomment this block + its line in _PROVIDERS below to re-enable.
# ---------------------------------------------------------------------
# _VOYAGE_MODEL = "voyage-3.5"
# _VOYAGE_ENDPOINT = "https://api.voyageai.com/v1/embeddings"
# _VOYAGE_KEY_ENV = "VOYAGE_API_KEY"
#
#
# def _voyage_configured() -> bool:
#     return bool(os.environ.get(_VOYAGE_KEY_ENV))
#
#
# def _voyage_embed(text: str, task_type: str) -> list[float]:
#     key = os.environ.get(_VOYAGE_KEY_ENV)
#     if not key:
#         raise EmbeddingsUnavailable(f"{_VOYAGE_KEY_ENV} is not set")
#
#     # Voyage's `input` is a batch (list), even for a single string.
#     body = json.dumps({"model": _VOYAGE_MODEL, "input": [text]}).encode("utf-8")
#     req = urllib.request.Request(
#         _VOYAGE_ENDPOINT, data=body, method="POST",
#         headers={"Content-Type": "application/json", "Authorization": f"Bearer {key}"},
#     )
#     try:
#         with urllib.request.urlopen(req, timeout=20) as resp:
#             payload = json.loads(resp.read())
#     except urllib.error.HTTPError as exc:
#         detail = exc.read().decode("utf-8", errors="replace")[:500]
#         raise EmbeddingsUnavailable(f"Voyage embeddings API returned {exc.code}: {detail}") from exc
#     except urllib.error.URLError as exc:
#         raise EmbeddingsUnavailable(f"Voyage embeddings API unreachable: {exc}") from exc
#     try:
#         return payload["data"][0]["embedding"]
#     except (KeyError, IndexError, TypeError) as exc:
#         raise EmbeddingsUnavailable(f"Unexpected response shape from Voyage embeddings API: {payload}") from exc


# ---------------------------------------------------------------------
# Provider registry + dispatch. Every caller in this project (
# semantic_dedup.py, gap_analysis.py, web/app.py) goes through these
# three functions, never the per-provider helpers above directly.
#
# Only Gemini registered right now -- openai/voyage are commented out
# above, paused, not deleted (see module docstring). Uncomment their two
# lines below alongside their implementation blocks to re-enable.
# ---------------------------------------------------------------------
_PROVIDERS = {
    "gemini": {"configured": _gemini_configured, "embed": _gemini_embed,
               "model_name": _GEMINI_MODEL, "key_env": _GEMINI_KEY_ENV, "verified_live": True},
    # "openai": {"configured": _openai_configured, "embed": _openai_embed,
    #            "model_name": _OPENAI_MODEL, "key_env": _OPENAI_KEY_ENV, "verified_live": False},
    # "voyage": {"configured": _voyage_configured, "embed": _voyage_embed,
    #            "model_name": _VOYAGE_MODEL, "key_env": _VOYAGE_KEY_ENV, "verified_live": False},
}


def _resolve(provider: str | None) -> dict:
    name = (provider or DEFAULT_PROVIDER).lower()
    if name not in _PROVIDERS:
        raise EmbeddingsUnavailable(
            f"Unknown embeddings provider '{name}' (known: {', '.join(_PROVIDERS)})")
    return _PROVIDERS[name]


def provider_status() -> dict[str, dict]:
    """One row per known provider -- {"configured": bool, "key_env": str,
    "model_name": str, "verified_live": bool} -- for the web UI's status
    display and New Run provider picker, so an operator can see at a
    glance which provider(s) they actually have a key for, and which
    implementations have and haven't been exercised against a real call
    yet (see module docstring)."""
    return {
        name: {"configured": p["configured"](), "key_env": p["key_env"],
               "model_name": p["model_name"], "verified_live": p["verified_live"]}
        for name, p in _PROVIDERS.items()
    }


def api_key_configured(provider: str | None = None) -> bool:
    return _resolve(provider)["configured"]()


def model_name(provider: str | None = None) -> str:
    return _resolve(provider)["model_name"]


def embed_text(text: str, task_type: str = "SEMANTIC_SIMILARITY", provider: str | None = None) -> list[float]:
    return _resolve(provider)["embed"](text, task_type)


def cosine_similarity(a: list[float], b: list[float]) -> float:
    dot = sum(x * y for x, y in zip(a, b))
    norm_a = sum(x * x for x in a) ** 0.5
    norm_b = sum(y * y for y in b) ** 0.5
    if norm_a == 0 or norm_b == 0:
        return 0.0
    return dot / (norm_a * norm_b)
