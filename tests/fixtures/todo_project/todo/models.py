from __future__ import annotations

from dataclasses import dataclass, field
from datetime import date


@dataclass
class Todo:
    id: int
    title: str
    done: bool = False
    priority: int = 2  # 1 = high, 3 = low
    due: date | None = None
    tags: list[str] = field(default_factory=list)

    def is_overdue(self, today: date) -> bool:
        if self.done or self.due is None:
            return False
        return self.due <= today

    def to_dict(self) -> dict:
        return {
            "id": self.id,
            "title": self.title,
            "done": self.done,
            "priority": self.priority,
            "due": self.due.isoformat() if self.due else None,
            "tags": list(self.tags),
        }

    @classmethod
    def from_dict(cls, d: dict) -> Todo:
        return cls(
            id=d["id"],
            title=d["title"],
            done=d.get("done", False),
            priority=d.get("priority", 2),
            due=d.get("due"),
            tags=d.get("tags", []),
        )
