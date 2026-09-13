"""Explicit planner boundary for current field assessments."""

from __future__ import annotations

import json
from abc import ABC, abstractmethod
from dataclasses import asdict

from .case_records import encode
from .context_v3_types import CurrentContextV3
from .delivery_types import InvocationProfile
from .field_delivery_types import CurrentExecutionV3, CurrentFailureV3


class FieldPlannerV3(ABC):
    """Typed boundary for a planner that produces one uncommitted execution."""

    model_id: str
    scripted_test: bool

    @property
    @abstractmethod
    def profile(self) -> InvocationProfile:
        """Return the exact immutable execution profile for this planner."""
        raise NotImplementedError

    @abstractmethod
    def plan(self, context: CurrentContextV3) -> CurrentExecutionV3:
        """Plan from trusted context without committing state."""
        raise NotImplementedError


class FieldPlannerErrorV3(RuntimeError):
    """Typed, redacted planner failure that can be durably recorded."""

    def __init__(self, failure: CurrentFailureV3):
        if type(failure) is not CurrentFailureV3:
            raise ValueError("expected an exact current planner failure")
        super().__init__("Field-aware Strands turn did not complete a valid staged assessment.")
        self._failure = failure

    @property
    def failure(self) -> CurrentFailureV3:
        return self._failure

    @property
    def evidence(self) -> dict:
        return json.loads(encode(asdict(self._failure)))
