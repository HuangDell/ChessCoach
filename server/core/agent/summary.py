"""Deterministic compression for conversation continuity outside the SDK recent window."""
from __future__ import annotations

from collections.abc import Iterable
import json
from typing import Any


SUMMARY_MAX_CHARS = 1_500
SUMMARY_ITEM_MAX_CHARS = 360


def _compact_text(value: str, limit: int = SUMMARY_ITEM_MAX_CHARS) -> str:
    text = " ".join(value.split())
    if len(text) <= limit:
        return text
    return text[: limit - 3].rstrip() + "..."


def _content_text(content: Any) -> str:
    def readable_text(value: str) -> str:
        if not value.lstrip().startswith("{"):
            return value
        try:
            structured = json.loads(value)
        except json.JSONDecodeError:
            return value
        text = structured.get("text") if isinstance(structured, dict) else None
        return text if isinstance(text, str) else value

    if isinstance(content, str):
        return readable_text(content)
    if not isinstance(content, list):
        return ""
    parts: list[str] = []
    for item in content:
        if isinstance(item, str):
            parts.append(item)
        elif isinstance(item, dict):
            value = item.get("text") or item.get("content")
            if isinstance(value, str):
                parts.append(readable_text(value))
    return " ".join(parts)


def _conversation_messages(items: Iterable[Any]) -> list[tuple[str, str]]:
    messages: list[tuple[str, str]] = []
    for item in items:
        if not isinstance(item, dict):
            continue
        role = str(item.get("role") or "").strip().lower()
        if role not in {"user", "assistant"}:
            continue
        text = _compact_text(_content_text(item.get("content")))
        if text:
            messages.append((role, text))
    return messages


class ConversationSummaryBuilder:
    """Build a compact continuity block without treating prose as chess evidence."""

    def summarize(self, items: Iterable[Any], previous_summary: str = "") -> str:
        messages = _conversation_messages(items)
        if not messages:
            return _compact_text(previous_summary, SUMMARY_MAX_CHARS)

        user_messages = [text for role, text in messages if role == "user"]
        assistant_messages = [text for role, text in messages if role == "assistant"]
        sections: list[str] = []
        if user_messages:
            sections.append("Learning goal: " + user_messages[-1])
        if assistant_messages:
            sections.append(
                "Prior coaching note (continuity only, not chess evidence): "
                + assistant_messages[-1]
            )
        unresolved = [
            text
            for role, text in messages[-6:]
            if role == "user" and ("?" in text or "？" in text or text.endswith(("呢", "吗")))
        ]
        if unresolved:
            sections.append("Open question: " + unresolved[-1])
        summary = "\n".join(sections)
        return _compact_text(summary, SUMMARY_MAX_CHARS)
