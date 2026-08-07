"""Vision bridge: detect images in a conversation and pick vision models.

When a request contains an image, a text-only model cannot answer it. This
module provides the two primitives callers rely on:

* ``messages_have_images`` -- scan an OpenAI-format message list for image
  content (``image_url`` / ``image`` parts, or base64 data URLs).
* ``strip_images_for_text_model`` -- replace image parts with short placeholders
  so a text-only model can still read a conversation with attachments.
"""

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
    returns HTTP 400 "Upstream request failed"). When a user switches from a
    vision model to a text-only model mid-conversation, the prior image turns
    would otherwise poison every subsequent request.

    Strategy -- preserve context, drop only the image bytes:
    * The text of every turn is kept verbatim.
    * Each removed image is replaced with a compact one-line placeholder
      (``[image was attached here]``) rather than dropping the turn. This keeps
      the assistant's reply that *described* the image anchored to the right
      place in the conversation, so the text-only model reads the description
      as the image's context -- exactly what the vision model already extracted.
    * A user turn that was *only* an image still becomes a short placeholder
      line so the following assistant reply is not orphaned.
    """
    cleaned: List[Dict[str, Any]] = []
    for message in messages or []:
        content = message.get("content")
        if not isinstance(content, list):
            # A bare string that is itself a data URL would otherwise slip
            # through to a text-only model as raw base64. `messages_have_images`
            # detects these, so the stripper must too -- or detection and
            # stripping disagree about what an image is.
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
            # Keep the user's own words; note where the image sat so the model
            # knows the assistant reply below was describing a real attachment.
            placeholder = "[image was attached here]"
            new["content"] = f"{joined}\n{placeholder}".strip() if joined else placeholder
        else:
            new["content"] = joined
        cleaned.append(new)
    return cleaned
