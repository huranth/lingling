"""Stateless bridge between OpenAI Responses and Chat Completions.

Codex now speaks only ``POST /v1/responses``. Lingling's providers speak the
older chat-completions wire format, so this module translates at the boundary
and leaves routing, failover, WARP egress, and usage logging in the existing
executor path.

The bridge is intentionally stateless: callers must send full context in
``input``. Server-side ``previous_response_id`` would require storing response
items and replaying them, which Lingling's local proxy model does not otherwise
need.
"""

from __future__ import annotations

import json
import time
from json import JSONDecodeError
from typing import Any, Dict, Generator, List, Optional


def _content_from_parts(parts: Any) -> Any:
    """Convert Responses content parts into a chat ``content`` value.

    Returns a plain string when the item is text-only (the common case, and what
    every provider accepts), or an OpenAI-style parts list when an image is
    present. Images must survive this hop: dropping them would silently turn a
    "what is in this screenshot" turn into an unanswerable text question, and
    ``vision_bridge`` would route it to a text-only model.
    """
    if isinstance(parts, str):
        return parts
    if not isinstance(parts, list):
        return ""
    texts: List[str] = []
    multimodal: List[Dict[str, Any]] = []
    saw_image = False
    for part in parts:
        if not isinstance(part, dict):
            continue
        ptype = part.get("type")
        if ptype in ("input_text", "output_text", "text"):
            text = part.get("text")
            if isinstance(text, str) and text:
                texts.append(text)
                multimodal.append({"type": "text", "text": text})
        elif ptype in ("input_image", "image"):
            url = part.get("image_url") or part.get("url")
            # Responses sends a bare string; chat completions wants {"url": ...}.
            if isinstance(url, dict):
                url = url.get("url")
            if isinstance(url, str) and url:
                saw_image = True
                multimodal.append({"type": "image_url", "image_url": {"url": url}})
    if saw_image:
        return multimodal
    return "\n".join(texts)


def request_to_chat(body: Dict[str, Any]) -> tuple[str, List[Dict[str, Any]], Dict[str, Any]]:
    """Return ``(model, messages, params)`` for an OpenAI Responses request."""
    if body.get("previous_response_id"):
        raise ValueError("previous_response_id is not supported; send full input context")
    model = body.get("model")
    if not isinstance(model, str) or not model:
        raise ValueError("Missing 'model' field.")

    messages: List[Dict[str, Any]] = []
    instructions = body.get("instructions")
    if isinstance(instructions, str) and instructions.strip():
        messages.append({"role": "system", "content": instructions})

    input_items = body.get("input")
    if isinstance(input_items, str):
        messages.append({"role": "user", "content": input_items})
    elif isinstance(input_items, list):
        pending_tool_calls: List[Dict[str, Any]] = []
        for item in input_items:
            if not isinstance(item, dict):
                continue
            itype = item.get("type")
            if itype == "message":
                if pending_tool_calls:
                    messages.append({"role": "assistant", "content": "", "tool_calls": pending_tool_calls})
                    pending_tool_calls = []
                role = item.get("role") or "user"
                content = _content_from_parts(item.get("content"))
                if content or role in ("system", "developer"):
                    messages.append({
                        "role": "system" if role == "developer" else role,
                        "content": content,
                    })
            elif itype == "function_call":
                pending_tool_calls.append({
                    "id": item.get("call_id") or item.get("id") or f"call_{len(pending_tool_calls) + 1}",
                    "type": "function",
                    "function": {
                        "name": item.get("name") or "",
                        "arguments": item.get("arguments") or "{}",
                    },
                })
            elif itype == "function_call_output":
                if pending_tool_calls:
                    messages.append({"role": "assistant", "content": "", "tool_calls": pending_tool_calls})
                    pending_tool_calls = []
                output = item.get("output")
                messages.append({
                    "role": "tool",
                    "tool_call_id": item.get("call_id") or item.get("id") or "",
                    "content": output if isinstance(output, str) else json.dumps(output),
                })
        if pending_tool_calls:
            messages.append({"role": "assistant", "content": "", "tool_calls": pending_tool_calls})
    else:
        raise ValueError("Missing or invalid 'input' field.")

    if not messages:
        raise ValueError("Missing or invalid 'input' field.")

    params: Dict[str, Any] = {}
    for key in ("temperature", "top_p", "frequency_penalty", "presence_penalty", "stop"):
        if key in body:
            params[key] = body[key]
    if "max_output_tokens" in body:
        params["max_tokens"] = body["max_output_tokens"]
    if "tool_choice" in body:
        params["tool_choice"] = body["tool_choice"]
    if "parallel_tool_calls" in body:
        params["parallel_tool_calls"] = body["parallel_tool_calls"]
    # Codex nests thinking depth under `reasoning`. Hand the raw label over as
    # chat's `reasoning_effort`; the caller resolves it against the chosen model,
    # which is not known until routing has happened.
    reasoning = body.get("reasoning")
    if isinstance(reasoning, dict) and reasoning.get("effort") is not None:
        params["reasoning_effort"] = reasoning["effort"]
    tools = _responses_tools_to_chat(body.get("tools"))
    if tools:
        params["tools"] = tools
    return model, messages, params


def _responses_tools_to_chat(tools: Any) -> List[Dict[str, Any]]:
    out: List[Dict[str, Any]] = []
    if not isinstance(tools, list):
        return out
    for tool in tools:
        if not isinstance(tool, dict) or tool.get("type") != "function":
            continue
        name = tool.get("name")
        if not isinstance(name, str) or not name:
            continue
        out.append({
            "type": "function",
            "function": {
                "name": name,
                "description": tool.get("description") or "",
                "parameters": tool.get("parameters") or {"type": "object", "properties": {}},
            },
        })
    return out


def _delta_reasoning(delta: Dict[str, Any]) -> str:
    """Pull reasoning text out of a chat delta, whatever the provider calls it.

    OpenCode's models are not consistent: deepseek and ling use
    ``reasoning_content``, nemotron uses ``reasoning`` plus a parallel
    ``reasoning_details`` list. The non-streaming path already coped via
    ``_assistant_text``; the stream did not, so every reasoning token was dropped
    on the floor -- and nemotron, which sends its answer almost entirely as
    reasoning, arrived at Codex as an empty message.
    """
    for key in ("reasoning_content", "reasoning", "thinking"):
        value = delta.get(key)
        if isinstance(value, str) and value:
            return value
    return ""


def _usage(chat_usage: Dict[str, Any]) -> Dict[str, Any]:
    """Map chat-completions usage onto the Responses shape.

    ``reasoning_tokens`` is carried across as ``output_tokens_details``, which is
    where the Responses API puts it. Dropping it meant a Codex client saw a large
    output count with no explanation, while Lingling's own ledger recorded the
    thinking correctly -- the two disagreed about the same request.
    """
    tin = int(chat_usage.get("prompt_tokens", 0) or 0)
    tout = int(chat_usage.get("completion_tokens", 0) or 0)
    out: Dict[str, Any] = {
        "input_tokens": tin, "output_tokens": tout, "total_tokens": tin + tout,
    }
    details = chat_usage.get("completion_tokens_details")
    if isinstance(details, dict) and isinstance(details.get("reasoning_tokens"), int):
        out["output_tokens_details"] = {"reasoning_tokens": details["reasoning_tokens"]}
    return out


def response_object(
    chat: Dict[str, Any], requested_model: str, routed_model: str, provider: str,
) -> Dict[str, Any]:
    choice = (chat.get("choices") or [{}])[0]
    msg = choice.get("message") or {}
    output: List[Dict[str, Any]] = []
    content = _assistant_text(msg)
    if content:
        output.append(_message_item(content, status="completed"))
    for call in msg.get("tool_calls") or []:
        item = _tool_call_item(call)
        if item:
            output.append(item)
    return {
        "id": chat.get("id") or f"resp_{int(time.time() * 1000)}",
        "object": "response",
        "created_at": chat.get("created") or int(time.time()),
        "status": "completed",
        "model": requested_model,
        "output": output,
        "usage": _usage(chat.get("usage") or {}),
        "lingling": {"routed_model": routed_model, "provider": provider},
    }


def _assistant_text(message: Dict[str, Any]) -> str:
    content = message.get("content")
    if isinstance(content, str) and content.strip():
        return content
    for key in ("reasoning_content", "reasoning", "thinking"):
        value = message.get(key)
        if isinstance(value, str) and value.strip():
            return value.strip()
    if isinstance(content, list):
        return "\n".join(str(p.get("text", "")) for p in content if isinstance(p, dict))
    return ""


def _message_item(text: str, status: str = "completed") -> Dict[str, Any]:
    return {
        "id": f"msg_{int(time.time() * 1000)}",
        "type": "message",
        "role": "assistant",
        "status": status,
        "content": [{"type": "output_text", "text": text, "annotations": []}],
    }


def _tool_call_item(call: Dict[str, Any]) -> Optional[Dict[str, Any]]:
    fn = call.get("function") or {}
    name = fn.get("name")
    if not isinstance(name, str) or not name:
        return None
    call_id = call.get("id") or f"call_{int(time.time() * 1000)}"
    return {
        "id": f"fc_{call_id}",
        "type": "function_call",
        "status": "completed",
        "name": name,
        "arguments": fn.get("arguments") or "{}",
        "call_id": call_id,
    }


def sse(event: str, payload: Dict[str, Any]) -> bytes:
    return f"event: {event}\ndata: {json.dumps(payload, separators=(',', ':'))}\n\n".encode("utf-8")


def stream_events(
    chat_stream: Generator[bytes, None, None],
    requested_model: str,
    outcome: Optional[Any] = None,
) -> Generator[bytes, None, None]:
    """Translate OpenAI chat-completion SSE frames into Responses SSE events.

    ``outcome`` is an optional :class:`routing.stream_guard.StreamOutcome`. When
    given, ``completed`` records whether the upstream reported ``finish_reason``,
    so the caller can log a stream that died mid-flight as broken instead of
    filing it as a success.
    """
    rid = f"resp_{int(time.time() * 1000)}"
    resp = {
        "id": rid, "object": "response", "created_at": int(time.time()),
        "status": "in_progress", "model": requested_model, "output": [],
    }
    yield sse("response.created", {"type": "response.created", "response": resp})
    yield sse("response.in_progress", {"type": "response.in_progress", "response": resp})

    text_item_id: Optional[str] = None
    text_parts: List[str] = []
    calls: Dict[int, Dict[str, Any]] = {}
    usage: Dict[str, Any] = {}
    finished = False
    # Responses output_index identifies an item slot, and a client keyed on it
    # collides if two items share one. Indices are therefore handed out in order
    # of first appearance rather than reserved per item type: a model that only
    # answers keeps its message at 0, and one that thinks first puts reasoning at
    # 0 and the message at 1.
    reasoning_item_id: Optional[str] = None
    reasoning_parts: List[str] = []
    reasoning_index: Optional[int] = None
    text_index: Optional[int] = None
    call_indices: Dict[int, int] = {}
    next_index = 0
    broke: Optional[str] = None

    try:
        for raw in chat_stream:
            line = raw if isinstance(raw, bytes) else raw.encode("utf-8")
            if not line.startswith(b"data:"):
                continue
            payload = line[5:].strip()
            if not payload or payload == b"[DONE]":
                continue
            try:
                obj = json.loads(payload.decode("utf-8", "replace"))
            except (JSONDecodeError, UnicodeDecodeError):
                continue
            if isinstance(obj.get("usage"), dict):
                usage = _usage(obj["usage"])
            for choice in obj.get("choices") or []:
                delta = choice.get("delta") or {}
                think = _delta_reasoning(delta)
                if think:
                    if reasoning_item_id is None:
                        reasoning_item_id = f"rs_{int(time.time() * 1000)}"
                        reasoning_index = next_index
                        next_index += 1
                        yield sse("response.output_item.added", {
                            "type": "response.output_item.added", "output_index": reasoning_index,
                            "item": {"id": reasoning_item_id, "type": "reasoning",
                                     "status": "in_progress", "summary": []},
                        })
                    reasoning_parts.append(think)
                    yield sse("response.reasoning_summary_text.delta", {
                        "type": "response.reasoning_summary_text.delta",
                        "item_id": reasoning_item_id, "output_index": reasoning_index,
                        "summary_index": 0, "delta": think,
                    })
                text = delta.get("content")
                if isinstance(text, str) and text:
                    if text_item_id is None:
                        text_item_id = f"msg_{int(time.time() * 1000)}"
                        text_index = next_index
                        next_index += 1
                        yield sse("response.output_item.added", {
                            "type": "response.output_item.added", "output_index": text_index,
                            "item": {"id": text_item_id, "type": "message", "role": "assistant", "status": "in_progress", "content": []},
                        })
                        yield sse("response.content_part.added", {
                            "type": "response.content_part.added", "item_id": text_item_id,
                            "output_index": text_index, "content_index": 0,
                            "part": {"type": "output_text", "text": "", "annotations": []},
                        })
                    text_parts.append(text)
                    yield sse("response.output_text.delta", {
                        "type": "response.output_text.delta", "item_id": text_item_id,
                        "output_index": text_index, "content_index": 0, "delta": text,
                    })
                for call_delta in delta.get("tool_calls") or []:
                    idx = int(call_delta.get("index") or 0)
                    call = calls.setdefault(
                        idx,
                        {"id": call_delta.get("id") or f"call_{idx}", "name": "", "args": [], "announced": False},
                    )
                    if call_delta.get("id"):
                        call["id"] = call_delta["id"]
                    fn = call_delta.get("function") or {}
                    if fn.get("name"):
                        call["name"] = fn["name"]
                    out_idx = call_indices.get(idx)
                    if out_idx is None:
                        out_idx = next_index
                        call_indices[idx] = out_idx
                        next_index += 1
                    if not call["announced"]:
                        call["announced"] = True
                        yield sse("response.output_item.added", {
                            "type": "response.output_item.added", "output_index": out_idx,
                            "item": {
                                "id": f"fc_{call['id']}", "type": "function_call",
                                "status": "in_progress", "name": call.get("name") or "",
                                "arguments": "", "call_id": call["id"],
                            },
                        })
                    if fn.get("arguments"):
                        call["args"].append(fn["arguments"])
                        yield sse("response.function_call_arguments.delta", {
                            "type": "response.function_call_arguments.delta",
                            "item_id": f"fc_{call['id']}", "output_index": out_idx,
                            "delta": fn["arguments"],
                        })
                if choice.get("finish_reason"):
                    finished = True
    except Exception as exc:  # noqa: BLE001 - any transport failure ends the stream
        # A dropped WARP tunnel raises here. Letting it propagate would abort the
        # HTTP response with no terminal event, which Codex sees as a hung turn;
        # the 200 is already sent so it cannot become an error status either. Close
        # out the items we did emit and report `incomplete` instead. GeneratorExit
        # is a BaseException and deliberately not caught, so a client hanging up
        # still cancels normally.
        broke = str(exc)

    output = []
    if reasoning_item_id is not None:
        think = "".join(reasoning_parts)
        item = {
            "id": reasoning_item_id, "type": "reasoning", "status": "completed",
            "summary": [{"type": "summary_text", "text": think}],
        }
        output.append(item)
        yield sse("response.reasoning_summary_text.done", {
            "type": "response.reasoning_summary_text.done", "item_id": reasoning_item_id,
            "output_index": reasoning_index, "summary_index": 0, "text": think,
        })
        yield sse("response.output_item.done", {
            "type": "response.output_item.done", "output_index": reasoning_index, "item": item,
        })
    if text_item_id is None and reasoning_item_id is not None and not finished:
        # Some models stream their whole answer as reasoning and never emit a
        # content delta (nemotron does this). Without a message item the client
        # renders an empty turn, so the thinking becomes the answer rather than
        # being lost.
        text_item_id = f"msg_{int(time.time() * 1000)}"
        text_index = next_index
        next_index += 1
        text_parts = ["".join(reasoning_parts)]
        yield sse("response.output_item.added", {
            "type": "response.output_item.added", "output_index": text_index,
            "item": {"id": text_item_id, "type": "message", "role": "assistant",
                     "status": "in_progress", "content": []},
        })
        yield sse("response.output_text.delta", {
            "type": "response.output_text.delta", "item_id": text_item_id,
            "output_index": text_index, "content_index": 0, "delta": text_parts[0],
        })
    if text_item_id is not None:
        text = "".join(text_parts)
        item = {
            "id": text_item_id, "type": "message", "role": "assistant", "status": "completed",
            "content": [{"type": "output_text", "text": text, "annotations": []}],
        }
        output.append(item)
        yield sse("response.output_text.done", {
            "type": "response.output_text.done", "item_id": text_item_id,
            "output_index": text_index, "content_index": 0, "text": text,
        })
        yield sse("response.content_part.done", {
            "type": "response.content_part.done", "item_id": text_item_id,
            "output_index": text_index, "content_index": 0, "part": item["content"][0],
        })
        yield sse("response.output_item.done", {
            "type": "response.output_item.done", "output_index": text_index, "item": item,
        })
    for idx in sorted(calls):
        call = calls[idx]
        out_idx = call_indices[idx]
        item = {
            "id": f"fc_{call['id']}", "type": "function_call", "status": "completed",
            "name": call.get("name") or "", "arguments": "".join(call.get("args") or []),
            "call_id": call["id"],
        }
        output.append(item)
        yield sse("response.function_call_arguments.done", {
            "type": "response.function_call_arguments.done", "item_id": item["id"],
            "output_index": out_idx, "arguments": item["arguments"],
        })
        yield sse("response.output_item.done", {
            "type": "response.output_item.done", "output_index": out_idx, "item": item,
        })
    status = "completed" if finished else "incomplete"
    if outcome is not None:
        outcome.completed = finished
        outcome.error = broke
    final = dict(
        resp,
        status=status,
        output=output,
        usage=usage or {"input_tokens": 0, "output_tokens": 0, "total_tokens": 0},
    )
    if broke:
        # Surfaced so a client (and the dashboard's ledger) can tell a truncated
        # answer from a complete one. There is no Responses equivalent of the
        # chat path's `lingling_reset`, so this reports rather than retries.
        final["incomplete_details"] = {"reason": broke[:200]}
    yield sse("response.completed", {
        "type": "response.completed",
        "response": final,
    })