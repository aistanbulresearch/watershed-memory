import sqlite3
import subprocess
import sys
import threading
from concurrent.futures import ThreadPoolExecutor

import pytest

from watershed_memory.budget import DemoBudgetExhausted
from watershed_memory.persistent_budget import PersistentBudgetPlanner
from watershed_memory.planning import Plan, ReplayPlanner
from watershed_memory.service import Service


class Failing:
    mode = ReplayPlanner.mode

    def plan(self, state, released):
        raise RuntimeError("planner failed")


class Good:
    mode = ReplayPlanner.mode

    def plan(self, state, released):
        return Plan([], [])


def test_reopen_and_fresh_process_keep_allowance(tmp_path):
    path = tmp_path / "budget.sqlite"
    first = PersistentBudgetPlanner(Good(), path, limit=2)
    first.plan({}, [])
    assert PersistentBudgetPlanner(Good(), path, limit=2).attempts == 1
    code = "import sys;from pathlib import Path;from watershed_memory.persistent_budget import PersistentBudgetPlanner;from watershed_memory.planning import Plan;P=type('P',(),{'mode':{},'plan':lambda s,r:Plan([],[])});print(PersistentBudgetPlanner(P(),Path(sys.argv[1]),limit=2).attempts)"
    assert (
        subprocess.check_output([sys.executable, "-c", code, str(path)], text=True).strip() == "1"
    )


def test_concurrent_instances_never_exceed_cap(tmp_path):
    path = tmp_path / "budget.sqlite"
    wrappers = [PersistentBudgetPlanner(Good(), path, limit=6) for _ in range(24)]

    def call(wrapper):
        try:
            wrapper.plan({}, [])
            return True
        except DemoBudgetExhausted:
            return False

    with ThreadPoolExecutor(max_workers=24) as pool:
        results = list(pool.map(call, wrappers))
    assert sum(results) == 6 and wrappers[0].attempts == 6


@pytest.mark.parametrize("limit", [0, 13, True, 1.0])
def test_invalid_limit_rejected(tmp_path, limit):
    with pytest.raises(ValueError):
        PersistentBudgetPlanner(ReplayPlanner(), tmp_path / "x.sqlite", limit=limit)


@pytest.mark.parametrize("scope", ["", "bad scope", "é", "-bad", "x" * 65])
def test_invalid_scope_rejected(tmp_path, scope):
    with pytest.raises(ValueError):
        PersistentBudgetPlanner(ReplayPlanner(), tmp_path / "x.sqlite", scope=scope)


def test_failure_and_baseexception_consume_allowance_and_limit_mismatch_fails(tmp_path):
    path = tmp_path / "budget.sqlite"
    wrapper = PersistentBudgetPlanner(Failing(), path, limit=2)
    with pytest.raises(RuntimeError):
        wrapper.plan({}, [])
    with pytest.raises(RuntimeError):
        wrapper.plan({}, [])
    assert wrapper.attempts == 2
    with pytest.raises(DemoBudgetExhausted):
        wrapper.plan({}, [])
    with pytest.raises(ValueError):
        PersistentBudgetPlanner(ReplayPlanner(), path, limit=3)


def test_service_duplicate_receipt_does_not_consume_budget(tmp_path):
    path = tmp_path / "case.sqlite"
    planner = PersistentBudgetPlanner(ReplayPlanner(), path, limit=1)
    service = Service(path, planner)
    session = service.create_session()["session_id"]
    first = service.advance(session, "same")
    assert service.advance(session, "same") == first and planner.attempts == 1
    second = service.create_session()
    with pytest.raises(DemoBudgetExhausted):
        service.advance(second["session_id"], "new")
    assert service.snapshot(second["session_id"])["progress"]["processed"] == 0


def test_keyboard_interrupt_and_real_process_death_consume_allowance(tmp_path):
    class Interrupt:
        mode = {}

        def plan(self, state, released):
            raise KeyboardInterrupt()

    path = tmp_path / "interrupt.sqlite"
    with pytest.raises(KeyboardInterrupt):
        PersistentBudgetPlanner(Interrupt(), path, limit=2).plan({}, [])
    assert PersistentBudgetPlanner(Good(), path, limit=2).attempts == 1
    code = "import os,sys;from pathlib import Path;from watershed_memory.persistent_budget import PersistentBudgetPlanner;P=type('P',(),{'mode':{},'plan':lambda self,s,r:os._exit(23)});PersistentBudgetPlanner(P(),Path(sys.argv[1]),limit=2).plan({},[])"
    assert subprocess.run([sys.executable, "-c", code, str(path)]).returncode == 23
    assert PersistentBudgetPlanner(Good(), path, limit=2).attempts == 2


def test_missing_and_corrupt_rows_fail_closed(tmp_path):
    path = tmp_path / "corrupt.sqlite"
    wrapper = PersistentBudgetPlanner(Good(), path, limit=2)
    with sqlite3.connect(path) as db:
        db.execute("DELETE FROM live_budgets")
    with pytest.raises(ValueError):
        wrapper.plan({}, [])
    for value in (-1, 3, "bad"):
        with sqlite3.connect(path) as db:
            db.execute(
                "INSERT OR REPLACE INTO live_budgets VALUES ('local-live-demo-v1', 2, ?)", (value,)
            )
        with pytest.raises(ValueError):
            wrapper.attempts
        with sqlite3.connect(path) as db:
            db.execute("DELETE FROM live_budgets")
    with sqlite3.connect(path) as db:
        db.execute("INSERT INTO live_budgets VALUES ('local-live-demo-v1', 'drift', 0)")
    with pytest.raises(ValueError):
        PersistentBudgetPlanner(Good(), path, limit=6)


def test_constructor_race_initializes_one_row(tmp_path):
    path = tmp_path / "race.sqlite"
    barrier = threading.Barrier(8)

    def construct():
        barrier.wait()
        return PersistentBudgetPlanner(Good(), path, limit=4)

    with ThreadPoolExecutor(max_workers=8) as pool:
        wrappers = list(pool.map(lambda _: construct(), range(8)))
    assert all(wrapper.attempts == 0 for wrapper in wrappers)
