from __future__ import annotations

from datetime import date, timedelta


def parse_due(text: str | None, today: date) -> date | None:
    """Accepts 'today', 'tomorrow', '+N' (days from today) or YYYY-MM-DD."""
    if not text:
        return None
    text = text.strip().lower()
    if text == "today":
        return today
    if text == "tomorrow":
        return today + timedelta(days=1)
    if text.startswith("+"):
        return today + timedelta(days=int(text[1:]))
    return date.fromisoformat(text)


def normalize_tag(tag: str) -> str:
    """'#Work ' -> 'work'"""
    return tag.strip().lstrip("#")


def format_todo(todo) -> str:
    box = "[x]" if todo.done else "[ ]"
    parts = [f"{todo.id:>3} {box} {todo.title}", f"(p{todo.priority})"]
    if todo.due:
        parts.append(f"due {todo.due.isoformat()}")
    if todo.tags:
        parts.append(" ".join("#" + t for t in todo.tags))
    return " ".join(parts)
