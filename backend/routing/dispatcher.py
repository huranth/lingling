"""The two-layer multi-model dispatcher (multi-provider aware).

Selecting ``lingling-auto`` sends the conversation to a small, fast, free
*dispatcher* model (``deepseek-v4-flash-free``). The dispatcher reads the
conversation plus a capability table of every free model -- now annotated with
which providers serve each model -- and returns a JSON routing decision:
``{"model": "<id>", "reason": "<short>"}``. The executor then forwards the
*entire* conversation to the chosen model across the providers that serve it
(the context bridge + cross-provider failover).

Hard rules:
* If the conversation contains an image, candidates are restricted to
  vision-capable models before the dispatcher decides.
* The dispatcher is nudged to prefer models served by multiple providers, since
  those can fail over if one provider is rate-limited.
"""

from __future__ import annotations

import json
import re
from typing import Any, Callable, Dict, List, Optional, Tuple

from core import config
from models.vision_bridge import messages_have_images

# A callable that runs the dispatcher model: (messages, model) -> assistant text.
CallModel = Callable[[List[Dict[str, Any]], str], str]


def _capability_hint(model: Any) -> str:
    # Prefer the curated human-readable description attached by the provider
    # (providers.opencode.FREE_MODEL_CAPS) -- it is empirically verified and
    # tells the dispatcher exactly what each model is good at. Fall back to an
    # inferred hint only when no curated description exists.
    caps = getattr(model, "capabilities", None)
    if isinstance(caps, dict) and caps.get("desc"):
        return str(caps["desc"])
    mid = model.id.lower()
    hints: List[str] = []
    if model.vision:
        hints.append("understands images/audio/video")
    if "code" in mid or "codex" in mid:
        hints.append("strong at coding and engineering tasks")
    if model.reasoning:
        hints.append("deep multi-step reasoning")
    if any(tok in mid for tok in ("flash", "mini", "nano", "lite")):
        hints.append("fast and lightweight")
    if any(tok in mid for tok in ("ultra", "pro", "max", "large")):
        hints.append("high general capability")
    if not hints:
        hints.append("general purpose")
    return "; ".join(hints)


def build_capability_table(candidates: List[Any]) -> str:
    lines = ["Available free models (choose exactly one id):"]
    for m in candidates:
        # Round up, never down: `//` and bare round() both rendered a 500-token
        # window as "0K" (round(0.5) is 0 -- banker's rounding), telling the
        # routing brain a real model had no context at all.
        ctx = f"{max(1, round(m.context_length / 1000))}K" if m.context_length else "?"
        vision = "yes" if m.vision else "no"
        reasoning = "yes" if m.reasoning else "no"
        provider_ids = getattr(m, "provider_ids", []) or []
        providers = ",".join(provider_ids) or "?"
        resilience = f" (resilient: {len(provider_ids)} providers)" if len(provider_ids) > 1 else ""
        lines.append(
            f"- {m.id} | vision={vision} | reasoning={reasoning} | context={ctx} | "
            f"providers={providers}{resilience} | {_capability_hint(m)}"
        )
    return "\n".join(lines)


SYSTEM_PROMPT = """You are the routing brain of Lingling, a free-tier AI gateway. You pick which
model answers the user's request. You never answer the request yourself.

Read the LAST user turn. That is the request being routed. Earlier turns are
context for what the user is working on, not the thing you are routing.

## How to choose

Match the *kind of work* to the model's stated strengths, in this order:

1. IMAGES OVERRIDE EVERYTHING. Any screenshot, photo, diagram, chart, mockup or
   "look at this" -- pick vision=yes. A text-only model cannot see it, so nothing
   else about the request matters.

2. WRITING OR CHANGING CODE. Implementation, refactoring, bug fixing, writing a
   function, a component, a migration, a config file, a test. Prefer a model whose
   description says it is *specialized* for code over one that merely handles it.
   This covers frontend and backend equally -- both are code generation.

3. THINKING BEFORE CODE. Architecture, debugging something whose cause is unknown,
   tracing why behaviour is wrong, designing a schema, algorithm or data model,
   maths, planning a multi-step change, comparing approaches. The work here is
   reasoning, not typing, so prefer a deep-reasoning model even when the subject
   is code.

4. LONG CONTEXT. When the conversation is long or the user pasted a large file or
   log, prefer a bigger context window; an answer that overflows is no answer.

5. SHORT AND SIMPLE. Greetings, one-line factual questions, small clarifications,
   "what does this flag do". Prefer a fast lightweight model -- spending a heavy
   reasoning model on "hi" wastes the free tier and makes the reply slow.

6. EVERYTHING ELSE. Explanation, prose, translation, summarising, general chat:
   the strongest general-purpose model.

Tie-breakers, in order: a model served by more providers (shown as `providers=`
/ `resilient`) survives a rate limit; then the larger context window.

## Hard rules

- Choose exactly one `id`, copied verbatim from the list. An id not in the list
  is discarded and routing falls back, so inventing one wastes the turn.
- The request's *topic* does not decide this -- its *kind of work* does. "Explain
  what a closure is" is explanation, not coding, even though it mentions code.
- Do not pick a heavier model just to be safe. Every model in the list is free
  and capable; the wrong-sized one costs the user latency for nothing.

## Reply format

One JSON object, nothing else -- no prose, no markdown fence:

{"model": "<id-from-the-list>", "reason": "<one sentence, plain English, with spaces>"}

The reason names the kind of work and why that model fits it. Examples:

{"model": "north-mini-code-free", "reason": "Writing a React component is code generation, and this model is specialized for it."}
{"model": "nemotron-3-ultra-free", "reason": "Finding why the deploy fails needs multi-step diagnosis rather than code output."}
{"model": "mimo-v2.5-free", "reason": "The user attached a screenshot, so only a vision-capable model can answer."}
{"model": "ling-3.0-flash-free", "reason": "A one-line factual question is best served by the fastest model."}
"""


# Cheap, deterministic signals for the *kind of work* in a turn. These do not
# replace the dispatcher -- they are the safety net under it, used when the
# dispatcher model is unreachable, returns something unusable, or names a model
# that does not exist. Before this, all three of those cases fell back to
# `DISPATCHER_MODEL` regardless of the request, so an image or a refactor landed
# on whatever model happened to be configured as the router.
_CODE_MARKERS = (
    # Verbs that ask for code to be produced or changed.
    "refactor", "implement", "rewrite", "write a", "add a test", "unit test",
    "build me", "build a", "create a", "make a", "add an", "add a",
    "fix the", "fix this", "port it", "migrate",
    # Failure output, which always accompanies a fix request.
    "stack trace", "traceback", "compile error", "type error", "syntax error",
    "lint", "exception",
    # Concrete artefacts.
    "migration", "endpoint", "component", "css", "html", "sql", "regex",
    "api route", "dockerfile", "makefile", "yaml", "json schema",
    # Named ecosystems -- a request naming one is almost always asking for code.
    "react", "vue", "svelte", "tailwind", "typescript", "javascript", "python",
    "rust", "golang", "java", "kotlin", "swift", "django", "flask", "fastapi",
    "express", "next.js", "nextjs", "node", "postgres", "sqlite", "redis",
    # Literal source in the turn.
    "```", "def ", "class ", "import ", "function ", "const ", "async ",
)
_REASON_MARKERS = (
    "why", "architect", "design", "trade-off", "tradeoff", "compare",
    "explain how", "root cause", "debug", "diagnose", "plan", "strategy",
    "algorithm", "complexity", "prove", "derive", "schema", "should i",
    "best approach", "how should",
)
_TRIVIAL_MARKERS = (
    "hi", "hey", "hello", "thanks", "thank you", "ok", "okay", "yes", "no",
    "cool", "nice", "got it", "sounds good",
)


def _last_user_text(messages: List[Dict[str, Any]]) -> str:
    """The text of the most recent user turn, flattened and lowercased."""
    for message in reversed(messages or []):
        if message.get("role") != "user":
            continue
        content = message.get("content")
        if isinstance(content, str):
            return content.lower()
        if isinstance(content, list):
            parts = [
                str(p.get("text", "")) for p in content
                if isinstance(p, dict) and p.get("text")
            ]
            return "\n".join(parts).lower()
        return ""
    return ""


def classify_work(messages: List[Dict[str, Any]], has_images: bool) -> str:
    """Name the kind of work in the latest turn: the fallback's routing signal.

    Returns one of ``vision``, ``code``, ``reasoning``, ``trivial`` or
    ``general``. Deliberately crude -- it decides only what the *fallback* does
    when the dispatcher cannot answer, and a crude answer aimed at the right kind
    of model beats a precise answer aimed at the wrong one.
    """
    if has_images:
        return "vision"
    text = _last_user_text(messages)
    if not text.strip():
        return "general"
    stripped = text.strip().strip("!.?,")
    if len(stripped) <= 24 and stripped in _TRIVIAL_MARKERS:
        return "trivial"
    reasoning_hit = any(m in text for m in _REASON_MARKERS)
    code_hit = any(m in text for m in _CODE_MARKERS)
    # Reasoning wins a tie: "why does this function crash" is diagnosis, and a
    # code-specialist model asked to diagnose tends to emit a rewrite instead of
    # an explanation.
    if reasoning_hit:
        return "reasoning"
    if code_hit:
        return "code"
    if len(text) > 4000:
        return "reasoning"
    return "general"


def _distill_messages(
    messages: List[Dict[str, Any]], max_messages: int = 16, max_chars: int = 12000
) -> List[Dict[str, Any]]:
    """Bound the conversation handed to the (text-only) dispatcher."""
    flattened: List[Dict[str, Any]] = []
    for message in messages or []:
        role = message.get("role", "user")
        content = message.get("content")
        if isinstance(content, str):
            text = content
        elif isinstance(content, list):
            chunks = []
            for part in content:
                if isinstance(part, dict):
                    ptype = str(part.get("type", "")).lower()
                    if ptype in ("image_url", "image", "input_image"):
                        chunks.append("[image attached]")
                    elif part.get("text"):
                        chunks.append(str(part["text"]))
            text = "\n".join(chunks)
        else:
            text = "" if content is None else str(content)
        flattened.append({"role": role, "content": text})

    system = [m for m in flattened if m["role"] == "system"]
    rest = [m for m in flattened if m["role"] != "system"]
    kept = system + rest[-max_messages:]

    total = sum(len(m["content"]) for m in kept if m["role"] != "system")
    if total > max_chars and rest:
        trimmed = []
        budget = max_chars
        for m in reversed(rest[-max_messages:]):
            if budget <= 0:
                break
            c = m["content"]
            if len(c) > budget:
                c = c[:budget] + " ...[truncated]"
            trimmed.append({"role": m["role"], "content": c})
            budget -= len(c)
        kept = system + list(reversed(trimmed))
    return kept


def build_dispatcher_messages(
    messages: List[Dict[str, Any]], candidates: List[Any], has_images: bool
) -> List[Dict[str, Any]]:
    table = build_capability_table(candidates)
    image_note = (
        "\n\nThe user's message contains an image. You MUST pick a vision-capable "
        "model (vision=yes)."
        if has_images
        else ""
    )
    system = f"{SYSTEM_PROMPT}\n{table}{image_note}"
    return [
        {"role": "system", "content": system},
        {
            "role": "user",
            "content": (
                "Choose the best model for the following conversation. "
                "Reply with only the JSON object."
            ),
        },
        *_distill_messages(messages),
    ]


_JSON_RE = re.compile(r"\{.*?\}", re.DOTALL)


def _clean_reason(reason: str) -> str:
    """Tidy the dispatcher's one-line reason for display.

    Free models occasionally emit a reason with the spaces missing --
    ``"Thisisacodingquestion"`` -- so run-together text is split on the
    lowercase->uppercase boundary.

    That split is applied *only* when the text has no spaces at all. Applying it
    unconditionally corrupted ordinary sentences, because the same boundary
    occurs inside words a routing reason is full of: ``TypeScript`` became
    ``Type Script``, ``GitHub`` became ``Git Hub``, ``iOS`` became ``i OS``.
    A reason that already has spaces is left alone apart from capitalisation and
    a closing period.
    """
    if not reason:
        return "no reason given"
    cleaned = reason.strip()
    if not cleaned:
        return "no reason given"
    if " " not in cleaned:
        result = [cleaned[0]]
        for i in range(1, len(cleaned)):
            if cleaned[i].isupper() and cleaned[i - 1].islower():
                result.append(" ")
            result.append(cleaned[i])
        cleaned = "".join(result).strip()
    if cleaned and cleaned[0].islower():
        cleaned = cleaned[0].upper() + cleaned[1:]
    if not cleaned.endswith((".", "!", "?")):
        cleaned += "."
    return cleaned


def parse_decision(text: str) -> Tuple[Optional[str], str]:
    if not text:
        return None, "empty dispatcher response"
    cleaned = text.strip()
    cleaned = re.sub(r"^```(?:json)?|```$", "", cleaned, flags=re.MULTILINE).strip()
    for blob in [cleaned] + _JSON_RE.findall(cleaned):
        try:
            obj = json.loads(blob)
        except json.JSONDecodeError:
            continue
        # A model that answers `[]` or `[{...}]` parses as JSON but is not a
        # decision object. Reading `.get` off it raised AttributeError, which the
        # caller's blanket except turned into "dispatcher unavailable: 'list'
        # object has no attribute 'get'" -- an outage message for a parse miss.
        if not isinstance(obj, dict):
            continue
        model = obj.get("model") or obj.get("id") or obj.get("target")
        if isinstance(model, str) and model.strip():
            reason = str(obj.get("reason", "")).strip() or "no reason given"
            return model.strip(), _clean_reason(reason)
    return None, f"could not parse dispatcher response: {text[:160]!r}"


def _capability_score(model: Any, kind: str) -> tuple:
    """Rank a model for one kind of work. Higher sorts first.

    Reads the curated ``capabilities.desc`` the provider attaches, because that
    is the only place a free model's actual specialism is recorded -- ``coding``
    and ``reasoning`` are True for every OpenCode free model, so the booleans
    alone cannot tell a code specialist from a general chat model.
    """
    caps = getattr(model, "capabilities", None) or {}
    desc = str(caps.get("desc", "")).lower()
    providers = len(getattr(model, "provider_ids", []) or [])
    ctx = model.context_length or 0

    specialised = "specialized" in desc or "specialised" in desc
    code_focus = specialised and ("code" in desc or "engineering" in desc)
    deep_focus = any(
        w in desc for w in ("deep", "multi-step", "deliberate", "planning", "math")
    )
    fast_focus = any(w in desc for w in ("fast", "lightweight", "flash"))
    general_focus = "general-purpose" in desc or "general purpose" in desc

    if kind == "vision":
        primary = 2 if model.vision else 0
    elif kind == "code":
        primary = (2 if code_focus else 0) + (1 if "cod" in desc else 0)
    elif kind == "reasoning":
        primary = (2 if deep_focus else 0) + (1 if model.reasoning else 0)
    elif kind == "trivial":
        primary = 2 if fast_focus else 0
    else:
        primary = (2 if general_focus else 0) + (1 if fast_focus else 0)

    # A trivial turn wants the smallest adequate model, so context breaks ties
    # downward there and upward everywhere else.
    return (primary, providers, -ctx if kind == "trivial" else ctx)


def fallback_model(
    catalog: Any,
    has_images: bool,
    exclude: Optional[set] = None,
    messages: Optional[List[Dict[str, Any]]] = None,
) -> str:
    """Deterministic fallback when the dispatcher cannot decide.

    ``messages`` lets the fallback route on the *kind of work* instead of
    ignoring the request. Without it this returned ``DISPATCHER_MODEL`` whenever
    that model was in the pool, so every dispatcher outage, unparseable reply and
    hallucinated id sent the turn to the router's own model no matter what was
    asked -- a refactor got a general chat model, and the model-failover path
    retried on the same profile it had just failed with.

    ``exclude`` removes models that already failed, so failover picks a genuinely
    different one.
    """
    exclude = exclude or set()
    pool = catalog.vision_free() if has_images else catalog.free()
    pool = [m for m in pool if m.id not in exclude]
    if not pool:
        # No vision model left for an image request: answering from a text-only
        # model beats refusing, and the caller has already replaced the image
        # with a placeholder for models that cannot see it.
        pool = [m for m in catalog.free() if m.id not in exclude]
    if not pool:
        return config.DISPATCHER_MODEL

    kind = classify_work(messages or [], has_images)
    pool.sort(key=lambda m: _capability_score(m, kind), reverse=True)
    return pool[0].id


def decide(
    messages: List[Dict[str, Any]],
    catalog: Any,
    call_model: CallModel,
    force_images: Optional[bool] = None,
) -> Tuple[str, str, List[str]]:
    """Run the dispatcher; return ``(target_model, reason, candidate_ids)``."""
    has_images = messages_have_images(messages) if force_images is None else force_images
    candidates = catalog.vision_free() if has_images else catalog.free()
    if not candidates:
        return (
            fallback_model(catalog, has_images, messages=messages),
            "no free models available",
            [],
        )

    candidate_ids = [m.id for m in candidates]
    dispatcher_messages = build_dispatcher_messages(messages, candidates, has_images)
    raw = call_model(dispatcher_messages, config.DISPATCHER_MODEL)
    chosen, reason = parse_decision(raw)

    if chosen and chosen in candidate_ids:
        return chosen, reason, candidate_ids

    # Either the reply was unusable or it named a model nobody serves. Both mean
    # the same thing here: decide it ourselves from the request rather than
    # defaulting to the router's own model.
    picked = fallback_model(catalog, has_images, messages=messages)
    if chosen:
        reason = (
            f"dispatcher chose unavailable model {chosen!r}; "
            f"routed to {picked} by request type"
        )
    return picked, reason, candidate_ids
