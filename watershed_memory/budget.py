"""A shared invocation allowance for an explicitly started local live demonstration."""

from threading import Lock

from .planning import Plan, Planner


class DemoBudgetExhausted(RuntimeError):
    """The local server has used its configured live-turn allowance."""


class BoundedPlanner:
    def __init__(self, planner: Planner, limit: int = 6):
        if type(limit) is not int or not 1 <= limit <= 12:
            raise ValueError("Choose a live-turn allowance from 1 to 12.")
        self.planner, self.limit = planner, limit
        self.mode = dict(planner.mode)
        self._attempts = 0
        self._lock = Lock()

    @property
    def attempts(self) -> int:
        with self._lock:
            return self._attempts

    def plan(self, state: dict, released: list[dict]) -> Plan:
        with self._lock:
            if self._attempts >= self.limit:
                raise DemoBudgetExhausted("This demo has reached its live review allowance.")
            self._attempts += 1
        # Count failed/uncertain calls as attempts too. Receipt replay is handled
        # by Service before this method; new browser sessions share the allowance.
        return self.planner.plan(state, released)
