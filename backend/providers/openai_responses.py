"""Outbound Chat Completions <-> OpenAI Responses translation.

A subset of OpenCode Zen's free models is published ONLY on the modern
``POST /v1/responses`` endpoint (notably Muse Spark; verified live against the
real Zen endpoint this session). Sending a chat-shaped POST to one of them
returns an unhelpful HTTP 500 ``Internal server error`` -- the upstream has
no chat-completions handler for these model ids -- and Lingling's executor then
correctly falls over to a healthy fallback model. From the operator's chair
that looks like a bug ("Muse Spark is broken, I want it served") even though
the provider is healthy: the request simply went to the wrong endpoint.

This module is the OUTBOUND half of the translation. The INBOUND half (Codex's
``/v1/responses``-speaking client talking to a chat-shaped provider) lives in
``routing/responses_bridge.py``. The two are mirror images, not the same code
path, because they translate on opposite sides of the gateway
(``responses_bridge`` sits at the *client* edge; this module sits at the
*upstream* edge). Keeping them apart stops a future tweak on one edge from
silently distorting the other.

Everything here is shape work only -- it builds and parses JSON objects, no
transport -- so it unit-tests without ever touching the network. The transport
(Client + httpx, IO error -> UpstreamError) stays with the provider, exactly
where it lives for the chat path, so the executor's existing failover, egress
pool, and usage logging keep working unchanged.

Translation order for an inbound chat request is::

    messages --messages_to_input---> input[]         \\
    params   --build_responses_body--> responses body \\
                                                      v
                                                 POST /responses
                                                      |
                                                      v
                            response JSON --response_to_chat_completion--> chat completion

For streaming, ``chat_sse_from_responses_sse`` reads upstream Responses SSE
events and emits OpenAI chat-completion SSE chunks, terminating with the
``[DONE]`` marker that chat clients expect.

Verified live: a POST of ``responses_body({model, input, store:false,
include:["reasoning.encrypted_content"], prompt_cache_key, max_output_tokens,
stream:false})`` to ``https://opencode.ai/zen/v1/responses`` for
``muse-spark-1.2-contributor-free`` returns HTTP 200 with::

    {"object":"response","status":"completed","output":[
        {"type":"reasoning","encrypted_content":"..."},
        {"type":"message","role":"assistant","content":[
            {"type":"output_text","text":"Hi","annotations":[]}
        ]}
    ], "usage": {"input_tokens":...,"output_tokens":...,
                  "output_tokens_details":{"reasoning_tokens":...}}}
"""

from __future__ import annotations

import json
import time
from json import JSONDecodeError
from typing import Any, Dict, Generator, List

from models import metadata


# Default cap the official opencode CLI applies to every outbound Responses
# request (``ProviderTransform.maxOutputTokens`` -> ``min(model.limit.output,
# 32000)``). We mirror it so an absent client setting still bounds reasoning
# spend on a model whose catalogue advertises a much larger output ceiling;
# the upstream has its own caps, but stating one explicitly keeps first-token
# latency sane on long-running turns.
_DEFAULT_MAX_OUTPUT_TOKENS = 32000
# Headroom ceiling for a published output limit (models.dev ``limit.output``).
# ``min(published, ceiling)`` replaces the old hardcoded ``muse-spark`` string
# check: any Responses-only model that publishes a 131072-token ceiling gets
# the same 64000 headroom muse-spark had, while one publishing less stays at
# its own number or the CLI default.
_MAX_OUTPUT_HEADROOM = 64000


def _default_max_output_tokens(model: str) -> int:
    """The cap to send when the client sends no ``max_tokens``.

    Prefers the model's published output limit so long as it is a sensible
    magnitude (``min(published, headroom)``); falls back to the CLI default
    the opencode CLI itself uses for unknown/absent limits. Live third-party
    data every call -- the old muse-spark id check silently lost meaning the
    day the catalog stopped listing that id and a different Responses-only
    model with a huge ceiling took its place.
    """
    published = metadata.max_output(metadata.lookup(model))
    if isinstance(published, int) and published > 0:
        return min(published, _MAX_OUTPUT_HEADROOM)
    return _DEFAULT_MAX_OUTPUT_TOKENS


# ----------------------------------------------------------------------------- input


def _content_to_parts(content: Any, *, outbound: bool) -> Any:
    """Translate a chat message's ``content`` into Responses content parts.

    ``outbound=True`` for an assistant turn (parts use ``output_text``), False
    otherwise (``input_text``). A bare string is widened to a single-part list
    so the input on either side of the gateway consistently carries the
    canonical AI-SDK shape; an image (input side only) is preserved so a
    "what's in this screenshot" turn is not silently turned unanswerable on the
    way out.
    """
    if isinstance(content, str):
        text_type = "output_text" if outbound else "input_text"
        return [{"type": text_type, "text": content}] if content else []
    if isinstance(content, list):
        parts: List[Dict[str, Any]] = []
        for part in content:
            if not isinstance(part, dict):
                continue
            ptype = part.get("type")
            if ptype in ("text", "output_text", "input_text"):
                text = part.get("text")
                if isinstance(text, str) and text:
                    parts.append({
                        "type": "output_text" if outbound else "input_text",
                        "text": text,
                    })
            elif ptype in ("image_url", "input_image", "image"):
                url = part.get("image_url")
                if isinstance(url, dict):
                    url = url.get("url")
                if isinstance(url, str) and url:
                    parts.append({"type": "input_image", "image_url": url})
        return parts
    return []


def _assistant_text(message: Dict[str, Any]) -> str:
    """Best-effort pull of an assistant turn's text content."""
    content = message.get("content")
    if isinstance(content, str):
        return content
    if isinstance(content, list):
        return "\n".join(
            str(p.get("text", "")) for p in content if isinstance(p, dict)
        )
    return ""


def _content_to_output_str(content: Any) -> str:
    """Reduce a tool message's content to the plain string the Responses API
    wants on a ``function_call_output`` item."""
    if isinstance(content, str):
        return content
    if isinstance(content, list):
        return "\n".join(
            str(p.get("text", "")) for p in content if isinstance(p, dict)
        )
    if content is None:
        return ""
    return json.dumps(content)


def messages_to_input(messages: List[Dict[str, Any]]) -> List[Dict[str, Any]]:
    """Translate an OpenAI chat ``messages`` list to Responses ``input`` items.

    The Responses API has no top-level ``system`` role -- it reuses
    ``developer`` for instructions, which is also what ``@ai-sdk/openai``
    emits for reasoning models. An assistant turn that carries tool calls is
    expanded into one ``message`` item (its text, if any) followed by a
    ``function_call`` item per call, mirroring the inbound bridge's reading
    direction so a round trip chat -> responses -> chat reproduces the same
    item spine. ``tool`` messages become ``function_call_output`` items keyed
    by ``call_id`` so the upstream can re-attach outputs to the calls.
    """
    items: List[Dict[str, Any]] = []
    for msg in messages:
        if not isinstance(msg, dict):
            continue
        role = msg.get("role") or "user"
        if role == "system":
            role = "developer"
        if role == "tool":
            items.append({
                "type": "function_call_output",
                "call_id": msg.get("tool_call_id") or "",
                "output": _content_to_output_str(msg.get("content")),
            })
            continue
        if role == "assistant" and msg.get("tool_calls"):
            if _assistant_text(msg):
                items.append({
                    "type": "message",
                    "role": "assistant",
                    "content": _content_to_parts(
                        msg.get("content") or _assistant_text(msg), outbound=True,
                    ),
                })
            for tc in msg.get("tool_calls") or []:
                if not isinstance(tc, dict):
                    continue
                fn = tc.get("function") or {}
                items.append({
                    "type": "function_call",
                    "status": "completed",
                    "call_id": tc.get("id") or "",
                    "name": fn.get("name") or "",
                    "arguments": fn.get("arguments") or "{}",
                })
            continue
        parts = _content_to_parts(msg.get("content"), outbound=(role == "assistant"))
        if parts or role == "developer":
            items.append({"type": "message", "role": role, "content": parts})
    return items


# ---------------------------------------------------------------------------- body


def _convert_chat_tools(tools: Any) -> List[Dict[str, Any]]:
    """Chat-style ``tools`` (nested under ``function``) -> Responses shape (flat)."""
    out: List[Dict[str, Any]] = []
    if not isinstance(tools, list):
        return out
    for tool in tools:
        if not isinstance(tool, dict):
            continue
        if tool.get("type") and tool.get("type") != "function":
            continue
        fn = tool.get("function") or tool
        name = fn.get("name")
        if not isinstance(name, str) or not name:
            continue
        out.append({
            "type": "function",
            "name": name,
            "description": fn.get("description") or "",
            "parameters": fn.get("parameters") or {"type": "object", "properties": {}},
            "strict": False,
        })
    return out


def _convert_chat_tool_choice(tc: Any) -> Any:
    """Chat tool_choice -> Responses tool_choice (string passthrough; per-name
    selection flattens from {type:function, function:{name}} to {type, name})."""
    if isinstance(tc, str):
        return tc
    if isinstance(tc, dict) and tc.get("type") == "function":
        fn = tc.get("function") or {}
        name = fn.get("name")
        if name:
            return {"type": "function", "name": name}
    return tc


def build_responses_body(
    model: str,
    messages: List[Dict[str, Any]],
    *,
    stream: bool,
    session_id: str = "",
    include_encrypted_reasoning: bool = True,
    **params: Any,
) -> Dict[str, Any]:
    """Build the upstream ``POST /v1/responses`` body.

    Operates on the API parameters Lingling has already resolved through the
    chat path (``reasoning_effort`` clamped to what the model honours, etc.):
    no re-translation of harness vocabulary here.

    ``store:false`` plus ``include:["reasoning.encrypted_content"]`` is the
    combination ``@ai-sdk/openai`` uses for every stateless reasoning call --
    the encrypted blob is what lets multi-turn reasoning survive without
    server-side item storage, which the proxy model doesn't otherwise need.
    A per-conversation ``prompt_cache_key`` is forwarded when a session id is
    available so the upstream's Redis prompt cache reuses work across a turn.

    ``max_tokens`` (and its newer alias ``max_completion_tokens``) is renamed
    to ``max_output_tokens``; an absent client value still gets the CLI default
    so a reasoning turn cannot trivially run to the 131072-token output ceiling
    on a model that has it (the catalogue advertises it for muse-spark) -- the
    upstream's own caps are belt-and-braces, but stating it here keeps
    first-token latency honest.

    Samplers (`temperature`, `top_p`, `stop`) are dropped while a reasoning
    effort is engaged: reasoning models reject them (verified by way of the AI
    SDK stripping the same fields for ``effort != "none"``), and the catalogue
    for muse-spark publishes no ``none`` option so once the chat-path effort
    resolver has run the value is either a real effort word or absent. With no
    effort asked for, samplers pass through so a future non-reasoning
    Responses-only model can still be tuned.
    """
    body: Dict[str, Any] = {
        "model": model,
        "input": messages_to_input(messages),
        "stream": stream,
        "store": False,
    }
    if include_encrypted_reasoning:
        body["include"] = ["reasoning.encrypted_content"]
    if session_id:
        body["prompt_cache_key"] = session_id[:128]

    max_tokens = params.get("max_tokens")
    if not (isinstance(max_tokens, int) and max_tokens > 0):
        max_tokens = params.get("max_completion_tokens")
    if isinstance(max_tokens, int) and max_tokens > 0:
        body["max_output_tokens"] = max_tokens
    else:
        # No client cap: resolve headroom from the model's published output
        # limit (Muse Spark at xhigh reasoning exhausts 32k quickly and blanks
        # the response; a rotated-in Responses successor with the same ceiling
        # inherits the headroom with no id check). Resolved lazily so a client
        # that did send max_tokens never trips a metadata lookup.
        body["max_output_tokens"] = _default_max_output_tokens(model)
        # Large workspace (Get deep context) can be 50k+ chars. With max_output
        # at 32k/64k the total can exceed the model's window and the upstream
        # returns empty (status incomplete, no output_text) at any effort —
        # the response pattern/translation the user called out. Cap to 4k when
        # input is large so the model always has room to answer.
        try:
            total_chars = 0
            for m in messages:
                c = m.get("content")
                if isinstance(c, str):
                    total_chars += len(c)
                elif isinstance(c, list):
                    for p in c:
                        if isinstance(p, dict) and isinstance(p.get("text"), str):
                            total_chars += len(p.get("text"))
                        elif isinstance(p, dict) and isinstance(p.get("image_url"), dict):
                            total_chars += 2000  # image placeholder
            if total_chars > 40000:
                body["max_output_tokens"] = min(int(body["max_output_tokens"]), 4096)
        except Exception:
            pass

    effort = params.get("reasoning_effort")
    if isinstance(effort, str) and effort:
        # ``summary: "auto"`` matches the AI SDK's pairing; we keep it so a
        # reasoning-summary item arrives on the upstream and could be surfaced
        # later. We do not currently forward the summary back to the chat
        # client (no standard field for it), but emitting it costs nothing.
        body["reasoning"] = {"effort": effort, "summary": "auto"}

    if not (isinstance(effort, str) and effort):
        for key in ("temperature", "top_p"):
            value = params.get(key)
            if value is not None:
                body[key] = value
        stop = params.get("stop")
        if stop is not None:
            body["stop"] = stop

    tools = _convert_chat_tools(params.get("tools"))
    if tools:
        body["tools"] = tools
    tc = params.get("tool_choice")
    if tc is not None:
        body["tool_choice"] = _convert_chat_tool_choice(tc)
    return body


# ---------------------------------------------------------------------------- reply


def response_to_chat_completion(
    body: Dict[str, Any], requested_model: str,
) -> Dict[str, Any]:
    """Translate a non-streaming Responses object to an OpenAI Chat Completion.

    Walks ``output[]`` collecting assistant ``output_text`` parts and any
    ``function_call`` items (turned back into chat ``tool_calls``). The
    ``reasoning`` item type carries only an ``encrypted_content`` blob: there is
    no plaintext thinking we could surface. The chat protocol has no field for
    an opaque reasoning blob, and the user already selected a reasoning model
    deliberately, so the encrypted reasoning is intentionally dropped at the
    boundary. (A future model that emits ``reasoning_summary_text`` would
    arrive as a structured ``summary`` list and could ship through here once
    one exists in the free catalog.)

    An incomplete upstream (``status`` != ``"completed"``) is mapped to a
    partial answer with ``finish_reason="length"``: the model produced text,
    but the token cap clipped it, and the chat-completion convention for that
    is the ``length`` reason, not ``stop``.

    Usage is renamed from the Responses fields to the chat ones the rest of
    the pipeline consumes (see ``providers.base.extract_usage``).
    """
    text_parts: List[str] = []
    structured_parts: List[Dict[str, Any]] = []
    has_annotations = False
    tool_calls: List[Dict[str, Any]] = []
    for item in body.get("output") or []:
        if not isinstance(item, dict):
            continue
        itype = item.get("type")
        # Robust: accept any message role, and also handle content being a
        # plain string (some free-tier variants return that) plus reasoning
        # summary fallback — response pattern/translation was dropping these and
        # leaving Get deep context blank at any effort, per user report.
        if itype == "message":
            content = item.get("content")
            # content can be a string (rare) or a list of output_text parts
            if isinstance(content, str) and content.strip():
                text_parts.append(content)
                structured_parts.append({"type": "text", "text": content, "annotations": []})
            elif isinstance(content, list):
                for part in content:
                    if isinstance(part, dict) and part.get("type") == "output_text":
                        text = part.get("text")
                        if isinstance(text, str):
                            structured_parts.append({
                                "type": "text", "text": text,
                                "annotations": list(part.get("annotations") or []),
                            })
                            text_parts.append(text)
                            if part.get("annotations"):
                                has_annotations = True
                    elif isinstance(part, dict) and isinstance(part.get("text"), str) and part.get("text").strip():
                        # Fallback for non-standard part shape: treat any text field as content
                        t = part.get("text").strip()
                        text_parts.append(t)
                        structured_parts.append({"type": "text", "text": t, "annotations": []})
            # Also handle direct text field on the item itself (some variants)
            if not text_parts and isinstance(item.get("text"), str) and item.get("text").strip():
                t = item.get("text").strip()
                text_parts.append(t)
                structured_parts.append({"type": "text", "text": t, "annotations": []})
        elif itype == "reasoning":
            # Muse Spark at xhigh sometimes puts the answer only in reasoning
            # summary when output_text is empty due to token budget. Previously
            # this was dropped, leaving a blank turn at any effort (minimal too).
            for block in item.get("summary") or []:
                if isinstance(block, dict) and block.get("type") == "summary_text":
                    t = block.get("text")
                    if isinstance(t, str) and t.strip():
                        text_parts.append(t)
                        structured_parts.append({"type": "text", "text": t, "annotations": []})
            # Also handle encrypted_content case where no summary text exists —
            # leave empty and let the placeholder handle it, but don't drop.
        elif itype == "function_call":
            tool_calls.append({
                "id": item.get("call_id") or item.get("id") or "",
                "type": "function",
                "function": {
                    "name": item.get("name") or "",
                    "arguments": item.get("arguments") or "{}",
                },
            })

    finish_reason = "length" if body.get("status") == "incomplete" else "stop"
    # Robustness: spark at high effort can return incomplete with reasoning
    # billed but no visible output_text (token budget exhausted in thinking).
    # Returning empty content renders as blank in Codex; surface a placeholder
    # so the client sees why the turn is empty and can retry at lower effort.
    # User-visible hook requested: "Muse Spark has an empty reply" so the
    # thinking-then-empty-then-thinking-again pattern at xhigh is surfaced as
    # Lingling's notice, not a silent blank.
    if not text_parts and not tool_calls:
        usage = body.get("usage") or {}
        details = usage.get("output_tokens_details") or {}
        rt = details.get("reasoning_tokens") if isinstance(details.get("reasoning_tokens"), int) else 0
        if rt and body.get("status") == "incomplete":
            text_parts = [f"[lingling: response produced no visible content; upstream billed {rt} reasoning_tokens then finish_reason=length. Increase max_output_tokens or retry at lower effort.]"]
            structured_parts = [{"type": "text", "text": text_parts[0], "annotations": []}]
    if has_annotations:
        content: Any = structured_parts
    else:
        content = "".join(text_parts)
    message: Dict[str, Any] = {"role": "assistant", "content": content or ""}
    if tool_calls:
        message["tool_calls"] = tool_calls

    usage = body.get("usage") or {}
    details = usage.get("output_tokens_details") or {}
    chat_usage: Dict[str, int] = {
        "prompt_tokens": int(usage.get("input_tokens", 0) or 0),
        "completion_tokens": int(usage.get("output_tokens", 0) or 0),
        "total_tokens": int(
            usage.get(
                "total_tokens",
                int(usage.get("input_tokens", 0) or 0)
                + int(usage.get("output_tokens", 0) or 0),
            ) or 0
        ),
    }
    reasoning = details.get("reasoning_tokens")
    if isinstance(reasoning, int):
        chat_usage["completion_tokens_details"] = {"reasoning_tokens": reasoning}

    return {
        "id": body.get("id") or f"chatcmpl-{int(time.time() * 1000)}",
        "object": "chat.completion",
        "created": int(body.get("created_at") or time.time()),
        "model": requested_model,
        "choices": [{
            "index": 0,
            "message": message,
            "finish_reason": finish_reason,
        }],
        "usage": chat_usage,
    }


# ---------------------------------------------------------------------------- stream


def _emit_chunk(obj: Dict[str, Any]) -> bytes:
    return f"data: {json.dumps(obj, separators=(',', ':'))}\n\n".encode("utf-8")


def chat_sse_from_responses_sse(
    upstream_lines: Generator[bytes, None, None],
    requested_model: str,
) -> Generator[bytes, None, None]:
    """Translate upstream Responses SSE into OpenAI chat-completion SSE chunks.

    The upstream stream is ``event: ...`` / ``data: ...`` pairs (verified live);
    only the ``data:`` lines carry JSON. We respond to ``response.output_text.delta``
    with chat-shape content deltas and to ``response.completed`` with a
    terminal chunk carrying ``finish_reason`` and (when the upstream reports
    it) usage. The OpenAI chat SSE convention ends with ``data: [DONE]`` --
    the Responses channel does not send one, so we generate it.

    Function-call items translate to a single ``delta.tool_calls`` chunk when
    each ``function_call`` item is finalised, rather than streaming the
    arguments incrementally -- chat-completion clients are tolerant of a
    single-shot tool call chunk, and streaming the args requires tracking an
    inner ``response.function_call_arguments.delta`` sequence per item that no
    in-catalog free Responses-only Zen model exercises today.

    Upstream reasoning events carry encrypted blobs without plaintext; they
    are never forwarded (see ``response_to_chat_completion``).
    """
    rid = f"chatcmpl-{int(time.time() * 1000)}"
    started = int(time.time())
    finish_reason = "stop"
    usage: Dict[str, Any] = {}
    has_content = False
    has_tool = False

    # The first OpenAI chat chunk carries the assistant role so a client that
    # renders "assistant:" can show one immediately. The upstream itself only
    # emits a reasoning item first, no content delta until after reasoning is
    # done, so emitting role up front keeps chat clients from rendering an
    # empty initial turn while the model thinks.
    yield _emit_chunk({
        "id": rid, "object": "chat.completion.chunk", "created": started,
        "model": requested_model,
        "choices": [{
            "index": 0,
            "delta": {"role": "assistant", "content": ""},
            "finish_reason": None,
        }],
    })

    for raw in upstream_lines:
        line = raw if isinstance(raw, bytes) else raw.encode("utf-8")
        s = line.decode("utf-8", "replace")
        if not s.startswith("data:"):
            continue
        payload = s[5:].strip()
        if not payload:
            continue
        try:
            obj = json.loads(payload)
        except (JSONDecodeError, ValueError):
            continue
        etype = obj.get("type") or ""

        if etype == "response.output_text.delta":
            delta = obj.get("delta")
            text = ""
            if isinstance(delta, str):
                text = delta
            elif isinstance(delta, dict):
                # Some variants wrap as {"text": "..."} or {"delta": "..."}
                text = delta.get("text") or delta.get("delta") or ""
                if not isinstance(text, str):
                    text = ""
            if text:
                has_content = True
                yield _emit_chunk({
                    "id": rid, "object": "chat.completion.chunk",
                    "created": int(time.time()), "model": requested_model,
                    "choices": [{"index": 0, "delta": {"content": text},
                                "finish_reason": None}],
                })
        elif etype.startswith("response.reasoning"):
            # Robust: upstream has sent reasoning as encrypted blobs, summary_text,
            # text, or plain delta under various keys. Handle any reasoning prefix
            # so minimal at any effort doesn't drop the delta and leave blank.
            # ``*.done`` events re-carry the FULL assembled text that the
            # preceding ``*.delta`` events already streamed -- forwarding them
            # duplicates the whole reasoning block client-side (verified live
            # with muse-spark: two identical summary frames per turn).
            if etype.endswith(".done"):
                continue
            rd = obj.get("delta")
            text = ""
            if isinstance(rd, str):
                text = rd
            elif isinstance(rd, dict):
                text = rd.get("text") or rd.get("delta") or ""
                if not isinstance(text, str):
                    text = ""
            # Also handle when reasoning arrives as top-level "text" or "summary"
            if not text and isinstance(obj.get("text"), str):
                text = obj.get("text")
            if text:
                yield _emit_chunk({
                    "id": rid, "object": "chat.completion.chunk",
                    "created": int(time.time()), "model": requested_model,
                    "choices": [{"index": 0, "delta": {"reasoning_content": text},
                                "finish_reason": None}],
                })
        elif etype == "response.output_text.done":
            # PB5-W3-84 stream sibling: the upstream "done" event for an
            # output_text part carries the *full* assembled text plus the
            # part's final ``annotations`` array (url_citation /
            # file_citation / file_path). The text itself already streamed
            # out via the per-delta chunks above; the annotations are the
            # ONLY signal that's still stranded. Emit a synthetic chat
            # chunk whose ``delta`` carries ``annotations`` -- the
            # chat-completions SSE convention extension -- so a client
            # SDK can splice citations into the assistant message. A
            # free-tier muse-spark return with no annotations still flows
            # through unchanged (no annotations -> no chunk emitted here).
            ann = obj.get("annotations")
            if isinstance(ann, list) and ann:
                yield _emit_chunk({
                    "id": rid, "object": "chat.completion.chunk",
                    "created": int(time.time()), "model": requested_model,
                    "choices": [{"index": 0, "delta": {"annotations": ann},
                                "finish_reason": None}],
                })
        elif etype == "response.output_item.done":
            item = obj.get("item") or {}
            if item.get("type") == "function_call":
                has_tool = True
                tc = {
                    "index": 0,
                    "id": item.get("call_id") or item.get("id") or "",
                    "type": "function",
                    "function": {
                        "name": item.get("name") or "",
                        "arguments": item.get("arguments") or "{}",
                    },
                }
                yield _emit_chunk({
                    "id": rid, "object": "chat.completion.chunk",
                    "created": int(time.time()), "model": requested_model,
                    "choices": [{"index": 0, "delta": {"tool_calls": [tc]},
                                "finish_reason": None}],
                })
            elif item.get("type") == "message":
                # Robust: accept any role, and handle content being a string, a list
                # of output_text parts, or a direct text field — response pattern
                # translation was dropping large workspace messages where content
                # arrived as a plain string or with type "text" instead of "output_text".
                if not has_content:
                    content = item.get("content")
                    text_to_emit = ""
                    if isinstance(content, str) and content.strip():
                        text_to_emit = content
                    elif isinstance(content, list):
                        for part in content:
                            if isinstance(part, dict):
                                if part.get("type") in ("output_text", "text", "input_text"):
                                    t = part.get("text")
                                    if isinstance(t, str) and t.strip():
                                        text_to_emit = t
                                        break
                                elif isinstance(part.get("text"), str) and part.get("text").strip():
                                    text_to_emit = part.get("text")
                                    break
                    # Fallback to direct text field on the item itself
                    if not text_to_emit and isinstance(item.get("text"), str) and item.get("text").strip():
                        text_to_emit = item.get("text")
                    if text_to_emit:
                        has_content = True
                        yield _emit_chunk({
                            "id": rid, "object": "chat.completion.chunk",
                            "created": int(time.time()), "model": requested_model,
                            "choices": [{"index": 0, "delta": {"content": text_to_emit},
                                        "finish_reason": None}],
                        })
        elif etype == "response.completed":
            resp = obj.get("response") or {}
            up_usage = resp.get("usage") or {}
            details = up_usage.get("output_tokens_details") or {}
            tin = int(up_usage.get("input_tokens", 0) or 0)
            tout = int(up_usage.get("output_tokens", 0) or 0)
            usage = {
                "prompt_tokens": tin,
                "completion_tokens": tout,
                "total_tokens": int(up_usage.get("total_tokens", tin + tout) or 0),
            }
            reasoning = details.get("reasoning_tokens")
            if isinstance(reasoning, int):
                usage["completion_tokens_details"] = {"reasoning_tokens": reasoning}
            if resp.get("status") == "incomplete":
                finish_reason = "length"
            # Mirror non-streaming placeholder: incomplete + reasoning billed but no visible output
            if not has_content and not has_tool and resp.get("status") == "incomplete":
                rt = details.get("reasoning_tokens") if isinstance(details.get("reasoning_tokens"), int) else 0
                if rt:
                    placeholder = f"[lingling: response produced no visible content; upstream billed {rt} reasoning_tokens then finish_reason=length. Increase max_output_tokens or retry at lower effort.]"
                    yield _emit_chunk({
                        "id": rid, "object": "chat.completion.chunk",
                        "created": int(time.time()), "model": requested_model,
                        "choices": [{"index": 0, "delta": {"content": placeholder}, "finish_reason": None}],
                    })

    final: Dict[str, Any] = {
        "id": rid, "object": "chat.completion.chunk", "created": int(time.time()),
        "model": requested_model,
        "choices": [{"index": 0, "delta": {}, "finish_reason": finish_reason}],
    }
    if usage:
        final["usage"] = usage
    yield _emit_chunk(final)
    yield b"data: [DONE]\n\n"
