"""Pure current-evidence facts and explicitly configured source registry."""

from .facts import (
    CoveragePolicy,
    IntervalComparison,
    IntervalFacts,
    compare_intervals,
    inspect_interval,
)
from .registry import Compatibility, SourceRegistration, SourceRegistry, gallinas_registry

__all__ = [
    "Compatibility",
    "CoveragePolicy",
    "IntervalComparison",
    "IntervalFacts",
    "SourceRegistration",
    "SourceRegistry",
    "compare_intervals",
    "gallinas_registry",
    "inspect_interval",
]
