"""Vision bridge: detect images in a conversation and pick vision models.

When a request contains an image, a text-only model cannot answer it. This
module provides the two primitives the dispatcher relies on:

* ``messages_have_images`` -- scan an OpenAI-format message list for image
  content (``image_url`` / ``image`` parts, or base64 data URLs).
* ``select_vision_model`` -- choose the best free vision-capable model from the
  unified catalog, used both as a hard constraint (image present -> vision
  candidates only) and as a deterministic fallback.

It is duck-typed over the catalog: any object exposing ``vision_free()`` whose
items have ``id``/``modalities``/``context_length``/``max_output`` works
(the unified catalog's ``LogicalModel``).
"""

from __future__ import annotations

from typing import Any, Dict, List, Optional


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


def _vision_score(model: Any) -> tuple:
    """Rank vision models: richer input modalities, then bigger context/output."""
    modality_bonus = len(set(model.modalities) & {"image", "audio", "video"})
    return (
        modality_bonus,
        len(getattr(model, "provider_ids", [])),  # prefer multi-provider (resilient)
        model.context_length or 0,
        model.max_output or 0,
    )


def select_vision_model(catalog: Any, exclude: Optional[set] = None) -> Optional[str]:
    """Pick the best free vision-capable model id, or None if there is none."""
    exclude = exclude or set()
    candidates = [m for m in catalog.vision_free() if m.id not in exclude]
    if not candidates:
        return None
    candidates.sort(key=_vision_score, reverse=True)
    return candidates[0].id
