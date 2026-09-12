"""V3-only shared tool budget and atomic in-memory field-tool receipts."""

import inspect
import json
from dataclasses import dataclass
from functools import wraps

from .field_assessment_types import FieldToolReceipt
from .tools import CurrentTools, _json


@dataclass(slots=True)
class _V3AttemptBudget:
    attempts: int = 0
    exhausted: bool = False

    def charge(self):
        if self.exhausted or self.attempts >= 16:
            self.exhausted = True
            raise RuntimeError("tool attempt budget exhausted")
        self.attempts += 1


class _V3SourceTools(CurrentTools):
    def __init__(self, context, budget):
        super().__init__(context)
        self._budget = budget
        self.closed = False

    def _charge(self):
        self._budget.charge()
        self._attempts += 1
        if self.closed:
            raise ValueError("source assessment is sealed for the field phase")

    @property
    def attempts(self):
        return self._budget.attempts

    def finish(self):
        if self._budget.exhausted:
            raise RuntimeError("tool attempt budget exhausted")
        return super().finish()


def field_tool(name):
    def decorate(function):
        signature = inspect.signature(function)

        @wraps(function)
        def wrapped(self, *args, **kwargs):
            self._budget.charge()
            state = self._state()
            try:
                if self._decision is not None or len(self._trace) >= 8:
                    raise ValueError("field decision is sealed or its trace is full")
                bound = signature.bind(self, *args, **kwargs)
                payload = {key: value for key, value in bound.arguments.items() if key != "self"}
                encoded = _json(payload)
                FieldToolReceipt(name, encoded, "{}")
                result = function(self, *args, **kwargs)
                output = _json(result)
                self._trace.append(FieldToolReceipt(name, encoded, output))
                return json.loads(output)
            except (KeyError, TypeError, ValueError, RecursionError, UnicodeError):
                self._restore(state)
                raise ValueError("invalid field tool request") from None

        return wrapped

    return decorate
