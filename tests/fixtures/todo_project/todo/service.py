from __future__ import annotations

from datetime import date

from .models import Todo
from .utils import normalize_tag


class TodoList:
    def __init__(self, todos: list[Todo] | None = None):
        self.todos = list(todos or [])

    def _next_id(self) -> int:
        return len(self.todos) + 1

    def add(self, title: str, priority: int = 2, due: date | None = None, tags=()) -> Todo:
        title = title.strip()
        if not title:
            raise ValueError("title must not be empty")
        if priority not in (1, 2, 3):
            raise ValueError("priority must be 1, 2 or 3")
        todo = Todo(self._next_id(), title, priority=priority, due=due,
                    tags=[normalize_tag(t) for t in tags])
        self.todos.append(todo)
        return todo

    def get(self, todo_id: int) -> Todo:
        for t in self.todos:
            if t.id == todo_id:
                return t
        raise KeyError(todo_id)

    def complete(self, todo_id: int) -> Todo:
        todo = self.get(todo_id)
        todo.done = True
        return todo

    def remove(self, todo_id: int) -> None:
        self.todos.remove(self.get(todo_id))

    def pending(self) -> list[Todo]:
        """Open todos, most urgent first: by priority, then due date (undated last)."""
        open_todos = [t for t in self.todos if not t.done]
        return sorted(open_todos, key=lambda t: (t.priority, t.due))

    def with_tag(self, tag: str) -> list[Todo]:
        tag = normalize_tag(tag)
        return [t for t in self.todos if tag in t.tags]

    def overdue(self, today: date) -> list[Todo]:
        return [t for t in self.todos if t.is_overdue(today)]
