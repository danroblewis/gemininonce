from __future__ import annotations

import argparse
from datetime import date

from .service import TodoList
from .storage import JsonStorage
from .utils import format_todo, parse_due


def main(argv=None, db="todos.json", today: date | None = None, out=print) -> int:
    today = today or date.today()
    p = argparse.ArgumentParser(prog="todo")
    sub = p.add_subparsers(dest="cmd", required=True)
    a = sub.add_parser("add")
    a.add_argument("title")
    a.add_argument("-p", "--priority", type=int, default=2)
    a.add_argument("-d", "--due")
    a.add_argument("-t", "--tag", action="append", default=[])
    sub.add_parser("list")
    for name in ("done", "rm"):
        s = sub.add_parser(name)
        s.add_argument("id")
    args = p.parse_args(argv)

    storage = JsonStorage(db)
    todos = TodoList(storage.load())

    if args.cmd == "add":
        t = todos.add(args.title, args.priority, parse_due(args.due, today), args.tag)
        out(f"added {t.id}")
    elif args.cmd == "list":
        for t in todos.pending():
            out(format_todo(t))
    elif args.cmd in ("done", "rm"):
        try:
            if args.cmd == "done":
                todos.complete(args.id)
            else:
                todos.remove(args.id)
        except KeyError:
            out(f"no todo with id {args.id}")
            return 1
    storage.save(todos.todos)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
