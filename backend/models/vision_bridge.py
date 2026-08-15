"""Vision bridge: detect images in a conversation and adapt for text-only models."""

from __future__ import annotations

from typing import Any, Dict, List


def _iter_content_parts(content: Any) -> List[Any]:
    if content is None:
        return []
    if isinstance(content, str):
        return [{"type": "text", "text": content}]
    if isinstance(content, list):
        return content
    return []


def messages_have_images(messages: List[Dict[str, Any]]) -> bool:
    """Return True if any message carries image content."""
    for message in messages or []:
        for part in _iter_content_parts(message.get("content")):
            if not isinstance(part, dict):
                continue
            ptype = str(part.get("type", "")).lower()
            if ptype in ("image_url", "image", "input_image"):
                return True
            if "image_url" in part:
                return True
            text = str(part.get("text", ""))
            if text.strip().startswith("data:image"):
                return True
    return False


def strip_images_for_text_model(messages: List[Dict[str, Any]]) -> List[Dict[str, Any]]:
    """Return a copy of ``messages`` safe for a text-only model.

    Text-only models reject requests whose history carries image parts (OpenCode
    returns HTTP 400). Text is kept verbatim; each image becomes a compact
    ``[image was attached here]`` placeholder so the assistant reply that
    described it stays anchored in place. Idempotent; a no-op for a
    vision-capable model or a text-only request.
    """
    cleaned: List[Dict[str, Any]] = []
    for message in messages or []:
        content = message.get("content")
        if not isinstance(content, list):
            # A bare data-URL string must be caught here too, or detection
            # (messages_have_images) and stripping would disagree on what an
            # image is.
            if isinstance(content, str) and content.strip().startswith("data:image"):
                new = dict(message)
                new["content"] = "[image was attached here]"
                cleaned.append(new)
                continue
            cleaned.append(message)
            continue
        text_parts: List[str] = []
        had_image = False
        for part in content:
            if not isinstance(part, dict):
                continue
            ptype = str(part.get("type", "")).lower()
            if ptype in ("image_url", "image", "input_image") or "image_url" in part:
                had_image = True
                continue
            text = part.get("text")
            if isinstance(text, str) and not text.strip().startswith("data:image"):
                text_parts.append(text)
        new = dict(message)
        joined = "\n".join(p for p in text_parts if p).strip()
        if had_image:
            placeholder = "[image was attached here]"
            new["content"] = f"{joined}\n{placeholder}".strip() if joined else placeholder
        else:
            new["content"] = joined
        cleaned.append(new)
    return cleaned

