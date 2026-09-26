from datetime import date

import pytest

from todo.cli import main
from todo.models import Todo
from todo.service import TodoList
from todo.storage import JsonStorage
from todo.utils import format_todo, normalize_tag, parse_due

TODAY = date(2026, 3, 10)


# --- utils ---
def test_parse_due_keywords():
    assert parse_due("today", TODAY) == TODAY
    assert parse_due("tomorrow", TODAY) == date(2026, 3, 11)
    assert parse_due("+7", TODAY) == date(2026, 3, 17)
    assert parse_due("2026-12-01", TODAY) == date(2026, 12, 1)
    assert parse_due(None, TODAY) is None


def test_normalize_tag_lowercases():
    assert normalize_tag("#Work ") == "work"
    assert normalize_tag("HOME") == "home"


def test_format_todo():
    t = Todo(1, "Buy milk", due=date(2026, 3, 12), tags=["shop"])
    assert format_todo(t) == "  1 [ ] Buy milk (p2) due 2026-03-12 #shop"


# --- models ---
def test_overdue_excludes_due_today():
    assert not Todo(1, "a", due=TODAY).is_overdue(TODAY)
    assert Todo(1, "a", due=date(2026, 3, 9)).is_overdue(TODAY)
    assert not Todo(1, "a", due=date(2026, 3, 9), done=True).is_overdue(TODAY)


# --- service ---
def test_add_validates():
    tl = TodoList()
    with pytest.raises(ValueError):
        tl.add("   ")
    with pytest.raises(ValueError):
        tl.add("x", priority=5)


def test_ids_unique_after_remove():
    tl = TodoList()
    a = tl.add("a")
    tl.add("b")
    tl.remove(a.id)
    c = tl.add("c")
    assert len({t.id for t in tl.todos}) == 2
    assert c.id == 3


def test_pending_sorted_with_undated_last():
    tl = TodoList()
    tl.add("low", priority=3)
    tl.add("high undated", priority=1)
    tl.add("high later", priority=1, due=date(2026, 4, 1))
    tl.add("high soon", priority=1, due=date(2026, 3, 11))
    done = tl.add("finished", priority=1)
    tl.complete(done.id)
    assert [t.title for t in tl.pending()] == ["high soon", "high later", "high undated", "low"]


def test_with_tag_case_insensitive():
    tl = TodoList()
    tl.add("a", tags=["#Work"])
    tl.add("b", tags=["home"])
    assert [t.title for t in tl.with_tag("work")] == ["a"]


# --- storage ---
def test_storage_roundtrip(tmp_path):
    s = JsonStorage(tmp_path / "db.json")
    s.save([Todo(1, "a", due=date(2026, 3, 12), tags=["x"]), Todo(2, "b", done=True)])
    loaded = s.load()
    assert loaded[0].due == date(2026, 3, 12)
    assert loaded[0].tags == ["x"]
    assert loaded[1].done is True


# --- cli ---
def test_cli_flow(tmp_path):
    db = tmp_path / "todos.json"
    lines = []
    assert main(["add", "Write report", "-p", "1", "-d", "tomorrow"], db, TODAY, lines.append) == 0
    assert main(["add", "Call mom"], db, TODAY, lines.append) == 0
    assert main(["done", "2"], db, TODAY, lines.append) == 0
    lines.clear()
    assert main(["list"], db, TODAY, lines.append) == 0
    assert lines == ["  1 [ ] Write report (p1) due 2026-03-11"]
    assert main(["rm", "99"], db, TODAY, lines.append) == 1
