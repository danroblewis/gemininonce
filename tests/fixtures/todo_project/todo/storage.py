from __future__ import annotations

import json
from pathlib import Path

from .models import Todo


class JsonStorage:
    def __init__(self, path: str | Path):
        self.path = Path(path)

    def load(self) -> list[Todo]:
        if not self.path.exists():
            return []
        data = json.loads(self.path.read_text())
        return [Todo.from_dict(d) for d in data]

    def save(self, todos: list[Todo]) -> None:
        self.path.parent.mkdir(parents=True, exist_ok=True)
        self.path.write_text(json.dumps([t.to_dict() for t in todos], indent=2))
