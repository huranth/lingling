"""Anthropic Messages request -> OpenAI chat-completions request."""

from __future__ import annotations

import json
from typing import Any, Dict, List, Optional, Tuple

from claudecode import effort as cc_effort


def _text_from_blocks(blocks: Any) -> str:
    """Join the text of a block list, ignoring everything else."""
    if isinstance(blocks, str):
        return blocks
    if not isinstance(blocks, list):
        return ""
    parts: List[str] = []
    for block in blocks:
        if isinstance(block, dict) and block.get("type") == "text":
            text = block.get("text")
            if isinstance(text, str) and text:
                parts.append(text)
        elif isinstance(block, str) and block:
            parts.append(block)
    return "\n".join(parts)


def _system_prompt(value: Any) -> str:
    """Anthropic's ``system`` is ``string | TextBlockParam[]``; flatten either."""
    if isinstance(value, str):
        return value
    return _text_from_blocks(value)


def _image_url_from_source(source: Any) -> Optional[str]:
    """Turn an Anthropic image source into a chat-completions image URL.

    Anthropic uses ``{"type": "base64", "media_type": ..., "data": ...}`` or
    ``{"type": "url", "url": ...}``; chat completions wants one URL string, with
    base64 expressed as a data URI. Dropping images here would silently turn "fix
    what this screenshot shows" into an unanswerable question, and vision_bridge
    would then route it to a text-only model.
    """
    if not isinstance(source, dict):
        return None
    stype = source.get("type")
    if stype == "url":
        url = source.get("url")
        return url if isinstance(url, str) and url else None
    if stype == "base64":
        data = source.get("data")
        media = source.get("media_type") or "image/png"
        if isinstance(data, str) and data:
            return f"data:{media};base64,{data}"
    return None


def _user_content(blocks: Any) -> Tuple[Any, List[Dict[str, Any]]]:
    """Split a user turn into ``(content, tool_results)``.

    A single Anthropic user turn can carry both tool results and new user text.
    Chat completions cannot express that in one message, so the caller emits the
    ``tool`` messages first and then the user message, which is the order the
    conversation actually happened in.
    """
    if isinstance(blocks, str):
        return blocks, []
    if not isinstance(blocks, list):
        return "", []

    texts: List[str] = []
    multimodal: List[Dict[str, Any]] = []
    tool_results: List[Dict[str, Any]] = []
    saw_image = False

    for block in blocks:
        if not isinstance(block, dict):
            continue
        btype = block.get("type")
        if btype == "text":
            text = block.get("text")
            if isinstance(text, str) and text:
                texts.append(text)
                multimodal.append({"type": "text", "text": text})
        elif btype == "image":
            url = _image_url_from_source(block.get("source"))
            if url:
                saw_image = True
                multimodal.append({"type": "image_url", "image_url": {"url": url}})
        elif btype == "tool_result":
            content = block.get("content")
            if not isinstance(content, str):
                # Tool results may themselves be block lists (text, images).
                # Only the text survives: a tool result is data for the model, and
                # no free model accepts an image nested inside one.
                content = _text_from_blocks(content)
            tool_results.append({
                "role": "tool",
                "tool_call_id": block.get("tool_use_id") or "",
                "content": content,
            })

    if saw_image:
        return multimodal, tool_results
    return "\n".join(texts), tool_results


def _assistant_message(blocks: Any) -> Optional[Dict[str, Any]]:
    """Rebuild one assistant turn, promoting ``tool_use`` blocks to tool_calls.

    Incoming ``thinking`` blocks are **replayed** as ``reasoning_content``, not
    dropped. Measured against the free tier: an assistant turn that carries tool
    calls but no ``reasoning_content`` is rejected outright --

        400 The `reasoning_content` in the thinking mode must be passed back
            to the API.

    -- which killed every agentic session at the second turn. The first tool call
    succeeded, then replaying it to get the model's next step failed, so the loop
    died with an empty answer. Anthropic has the same requirement from its own
    side: thinking blocks must be preserved across tool-use turns.

    The key is therefore always present on a turn with tool calls, empty when the
    client replayed nothing (verified: an empty string satisfies the upstream). A
    plain assistant turn without tool calls does not need it and does not get it.

    Returns None for a turn carrying nothing chat completions can represent.
    """
    if isinstance(blocks, str):
        return {"role": "assistant", "content": blocks} if blocks else None
    if not isinstance(blocks, list):
        return None

    texts: List[str] = []
    thinking: List[str] = []
    tool_calls: List[Dict[str, Any]] = []
    for block in blocks:
        if not isinstance(block, dict):
            continue
        btype = block.get("type")
        if btype == "text":
            text = block.get("text")
            if isinstance(text, str) and text:
                texts.append(text)
        elif btype == "thinking":
            thought = block.get("thinking")
            if isinstance(thought, str) and thought:
                thinking.append(thought)
        elif btype == "tool_use":
            tool_calls.append({
                "id": block.get("id") or f"call_{len(tool_calls) + 1}",
                "type": "function",
                "function": {
                    "name": block.get("name") or "",
                    # Anthropic sends parsed JSON; chat completions wants the
                    # arguments as a JSON *string*.
                    "arguments": json.dumps(block.get("input") or {}),
                },
            })
        # `redacted_thinking` is skipped: its payload is Anthropic-encrypted, so
        # there is nothing another provider could read.

    content = "\n".join(texts)
    if not content and not tool_calls and not thinking:
        return None
    message: Dict[str, Any] = {"role": "assistant", "content": content}
    if tool_calls:
        message["tool_calls"] = tool_calls
        # Mandatory on a tool-calling turn, as measured above.
        message["reasoning_content"] = "\n".join(thinking)
    elif thinking:
        message["reasoning_content"] = "\n".join(thinking)
    return message


def _tools_to_chat(tools: Any) -> List[Dict[str, Any]]:
    """Anthropic tool definitions -> chat-completions function definitions.

    Anthropic puts the JSON Schema in ``input_schema``; chat completions calls it
    ``parameters``. Server tools (web_search, bash, text_editor and friends)
    declare a ``type`` and no ``input_schema`` -- they are executed by Anthropic's
    own infrastructure, so there is nothing to forward and they are skipped rather
    than sent as broken function definitions.
    """
    out: List[Dict[str, Any]] = []
    if not isinstance(tools, list):
        return out
    for tool in tools:
        if not isinstance(tool, dict):
            continue
        name = tool.get("name")
        schema = tool.get("input_schema")
        if not isinstance(name, str) or not name or not isinstance(schema, dict):
            continue
        fn: Dict[str, Any] = {"name": name, "parameters": schema}
        description = tool.get("description")
        if isinstance(description, str) and description:
            fn["description"] = description
        out.append({"type": "function", "function": fn})
    return out


def _tool_choice_to_chat(choice: Any) -> Optional[Any]:
    """Map Anthropic's four tool_choice variants onto chat completions.

    ``any`` and ``tool`` -- Anthropic's two forms of *forced* tool use -- are
    deliberately downgraded to ``auto``. Measured against the free tier: both
    ``"required"`` and ``{"type": "function", "function": {...}}`` are rejected
    with ``400 Thinking mode does not support this tool_choice``, and every free
    model here reasons. Forwarding them turned a working request into a hard
    failure, which for Claude Code ends the turn.

    Anthropic documents the same incompatibility from the other side: "Forced tool
    use is incompatible with manual extended thinking". So the downgrade matches
    what a real endpoint does rather than papering over a quirk -- and in practice
    a model given tools and an instruction to use them calls them anyway.
    """
    if not isinstance(choice, dict):
        return None
    ctype = choice.get("type")
    if ctype in ("auto", "any", "tool"):
        return "auto"
    if ctype == "none":
        return "none"
    return None


def request_to_chat(body: Dict[str, Any]) -> Tuple[str, List[Dict[str, Any]], Dict[str, Any]]:
    """Return ``(model, messages, params)`` for an Anthropic Messages request.

    Raises ``ValueError`` for a body that is not a usable request, which the
    caller turns into a 400. Anthropic requires ``model``, ``messages`` and
    ``max_tokens``; a missing ``max_tokens`` is tolerated rather than rejected,
    because the upstream has its own default and enforcing it here would make
    Lingling stricter than the client it is serving.
    """
    model = body.get("model")
    if not isinstance(model, str) or not model:
        raise ValueError("Missing 'model' field.")

    raw_messages = body.get("messages")
    if not isinstance(raw_messages, list) or not raw_messages:
        raise ValueError("Missing or invalid 'messages' field.")

    messages: List[Dict[str, Any]] = []
    system = _system_prompt(body.get("system"))
    if system.strip():
        messages.append({"role": "system", "content": system})

    for turn in raw_messages:
        if not isinstance(turn, dict):
            raise ValueError("Each entry in 'messages' must be an object.")
        role = turn.get("role")
        content = turn.get("content")
        if role == "assistant":
            built = _assistant_message(content)
            if built is not None:
                messages.append(built)
        elif role == "user":
            text, tool_results = _user_content(content)
            # Tool results answer the previous assistant turn, so they precede
            # whatever the user said next.
            messages.extend(tool_results)
            if text:
                messages.append({"role": "user", "content": text})
        elif role == "system":
            # Claude Code v2.1.x sends mid-conversation system turns (Anthropic's
            # "Mid-conversation system messages" feature), so `system` inside
            # `messages` is real traffic and not a malformed request. Rejecting it
            # failed the very first turn of a session.
            text = _text_from_blocks(content) if not isinstance(content, str) else content
            if text.strip():
                messages.append({"role": "system", "content": text})
        else:
            # An unfamiliar role is forwarded as a user turn rather than failing
            # the request. A future Anthropic role would otherwise take a whole
            # session down, and the model can still read the content.
            text = _text_from_blocks(content) if not isinstance(content, str) else content
            if text.strip():
                messages.append({"role": "user", "content": text})

    if not messages:
        raise ValueError("Missing or invalid 'messages' field.")

    params: Dict[str, Any] = {}
    for key in ("temperature", "top_p"):
        if key in body:
            params[key] = body[key]
    if isinstance(body.get("max_tokens"), int):
        # Claude Code sends a conservative max_tokens default.  Reasoning
        # models (deepseek etc.) spend tokens on thinking first, so a low
        # budget means zero content after the reasoning exhausts it.  OpenCode
        # natively defaults higher; match that floor so the same model that
        # works in OpenCode's own CLI doesn't fail through Lingling.
        params["max_tokens"] = max(body["max_tokens"], 16384)
    if isinstance(body.get("stop_sequences"), list):
        params["stop"] = body["stop_sequences"]

    tools = _tools_to_chat(body.get("tools"))
    if tools:
        params["tools"] = tools
        choice = _tool_choice_to_chat(body.get("tool_choice"))
        if choice is not None:
            params["tool_choice"] = choice

    # Depth crosses as a raw label. It cannot be clamped yet: which values are
    # legal depends on the model routing has not chosen at this point.
    depth = cc_effort.requested_effort(body)
    if depth is not None:
        params["reasoning_effort"] = depth

    return model, messages, params
