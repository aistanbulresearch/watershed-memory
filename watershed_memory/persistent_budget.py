"""Durable allowance for live planner turns."""

from __future__ import annotations

import re
import sqlite3
from contextlib import closing
from pathlib import Path

from .budget import DemoBudgetExhausted
from .planning import Plan, Planner


class PersistentBudgetPlanner:
    def __init__(
        self, planner: Planner, path: Path, scope: str = "local-live-demo-v1", limit: int = 6
    ):
        if not isinstance(scope, str) or not re.fullmatch(r"[A-Za-z0-9][A-Za-z0-9_-]{0,63}", scope):
            raise ValueError("scope must be ASCII alphanumeric, underscore or hyphen")
        if type(limit) is not int or not 1 <= limit <= 12:
            raise ValueError("limit must be an integer from 1 through 12")
        self.planner, self.path, self.scope, self.limit = planner, Path(path), scope, limit
        self.path.parent.mkdir(parents=True, exist_ok=True)
        self._ensure()
        self.mode = dict(planner.mode)

    def _connect(self) -> sqlite3.Connection:
        return sqlite3.connect(self.path, timeout=10, isolation_level=None)

    def _ensure(self) -> None:
        with closing(self._connect()) as db:
            db.execute("BEGIN IMMEDIATE")
            try:
                db.execute(
                    "CREATE TABLE IF NOT EXISTS live_budgets (scope TEXT PRIMARY KEY, configured_limit INTEGER NOT NULL, attempts INTEGER NOT NULL)"
                )
                row = db.execute(
                    "SELECT configured_limit, attempts FROM live_budgets WHERE scope=?",
                    (self.scope,),
                ).fetchone()
                if row is None:
                    db.execute(
                        "INSERT INTO live_budgets VALUES (?, ?, 0)", (self.scope, self.limit)
                    )
                else:
                    self._validate_row(row)
                db.commit()
            except BaseException:
                db.rollback()
                raise

    def _validate_row(self, row: tuple) -> None:
        if (
            type(row[0]) is not int
            or type(row[1]) is not int
            or row[0] != self.limit
            or not 0 <= row[1] <= row[0]
        ):
            raise ValueError("persisted live budget is incompatible or corrupt")

    @property
    def attempts(self) -> int:
        with closing(self._connect()) as db:
            row = db.execute(
                "SELECT configured_limit, attempts FROM live_budgets WHERE scope=?", (self.scope,)
            ).fetchone()
        if row is None:
            raise ValueError("persisted live budget is missing or corrupt")
        self._validate_row(row)
        return row[1]

    def plan(self, state: dict, released: list[dict]) -> Plan:
        with closing(self._connect()) as db:
            db.execute("BEGIN IMMEDIATE")
            row = db.execute(
                "SELECT configured_limit, attempts FROM live_budgets WHERE scope=?", (self.scope,)
            ).fetchone()
            if row is None:
                db.rollback()
                raise ValueError("persisted live budget is missing or corrupt")
            self._validate_row(row)
            if row[1] >= row[0]:
                db.rollback()
                raise DemoBudgetExhausted("This live demo has reached its durable turn allowance.")
            db.execute("UPDATE live_budgets SET attempts=attempts+1 WHERE scope=?", (self.scope,))
            db.commit()
        return self.planner.plan(state, released)
