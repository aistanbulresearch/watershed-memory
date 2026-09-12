"""A bounded, immutable agent capability loaded from one trusted case snapshot.

Records are internal capabilities, not an external deserialization or write API.
The loader never reads private human-action receipts or the whole event history.
"""

from __future__ import annotations

import json
import sqlite3
from dataclasses import asdict, dataclass
from datetime import datetime
from typing import TYPE_CHECKING

from ..watch.store import WatchEvent, WatchStore
from . import case_records as rows
from .case_store import CaseStore, _expected
from .case_types import CaseConfig, ReviewRecord, _identifier
from .fact_types import CoveragePolicy, IntervalFacts
from .fact_validation import event_id as validate_event_id
from .fact_validation import timestamp, utc
from .facts import compare_intervals, inspect_interval
from .registry import SourceRegistry, gallinas_registry

if TYPE_CHECKING:
    from .source_health import SourceHealth


@dataclass(frozen=True, slots=True)
class CurrentContext:
    case_id: str
    case_revision: int
    policy_digest: str
    simulated: bool
    evaluated_at: datetime
    current: IntervalFacts
    prior: tuple[IntervalFacts, ...]
    reviews: tuple[ReviewRecord, ...]
    review_evidence: tuple[tuple[str, tuple[str, ...]], ...]
    registry: SourceRegistry
    source_health: SourceHealth | None = None

    def __post_init__(self):
        _identifier(self.case_id, "case_id")
        _expected(self.case_revision)
        validate_event_id(self.policy_digest)
        evaluated = utc(self.evaluated_at)
        object.__setattr__(self, "evaluated_at", evaluated)
        if type(self.simulated) is not bool or type(self.current) is not IntervalFacts:
            raise ValueError("invalid current context")
        if type(self.registry) is not SourceRegistry:
            raise ValueError("invalid source registry")
        registered = self.registry.get(self.current.source_id)
        if self.source_health is not None:
            from .source_health import SourceHealth

            health = self.source_health
            if type(health) is not SourceHealth or (
                health.case_id, health.monitor_id, health.source_id,
                health.station_id, health.evaluated_at,
            ) != (
                self.case_id, self.current.monitor_id, self.current.source_id,
                self.current.station_id, evaluated,
            ) or rows.digest(rows.encode(asdict(health.policy))) != self.policy_digest:
                raise ValueError("source health differs from the reserved case context")
            if tuple((s.series_id, s.parameter_code, s.unit) for s in health.series) != tuple(
                (s.series_id, s.parameter_code, s.unit) for s in registered.series
            ):
                raise ValueError("source health differs from registered series")
        if (
            registered.station_id != self.current.station_id
            or self.current.interval_end > evaluated
        ):
            raise ValueError("source or evaluation time differs from context")
        if (
            type(self.prior) is not tuple
            or len(self.prior) > 3
            or any(
                type(item) is not IntervalFacts
                or not compare_intervals(self.current, item).comparable
                for item in self.prior
            )
        ):
            raise ValueError("invalid prior evidence")
        ids = {self.current.event_id, *(item.event_id for item in self.prior)}
        if len(ids) != 1 + len(self.prior):
            raise ValueError("duplicate prior evidence")
        if (
            type(self.reviews) is not tuple
            or len(self.reviews) > 3
            or any(
                type(item) is not ReviewRecord
                or item.case_id != self.case_id
                or item.simulated != self.simulated
                or item.status not in ("PROPOSED", "APPROVED", "DEFERRED")
                or item.updated_at > evaluated
                for item in self.reviews
            )
        ):
            raise ValueError("invalid active work snapshot")
        if len({item.kind for item in self.reviews}) != len(self.reviews) or len(
            {item.task_id for item in self.reviews}
        ) != len(self.reviews):
            raise ValueError("duplicate active work")
        if type(self.review_evidence) is not tuple or len(self.review_evidence) != len(
            self.reviews
        ):
            raise ValueError("invalid work evidence mapping")
        for pair, review in zip(self.review_evidence, self.reviews, strict=True):
            if (
                type(pair) is not tuple
                or len(pair) != 2
                or pair[0] != review.task_id
                or type(pair[1]) is not tuple
                or len(pair[1]) > 3
                or any(type(item) is not str or item not in ids for item in pair[1])
                or len(set(pair[1])) != len(pair[1])
            ):
                raise ValueError("work evidence is outside this context")


def _case_config(case: sqlite3.Row) -> CaseConfig:
    try:
        raw = json.loads(case["config_json"])
        policy = raw["coverage_policy"]
        policy["required_parameters"] = tuple(policy["required_parameters"])
        raw["coverage_policy"] = CoveragePolicy(**policy)
        config = CaseConfig(**raw)
        encoded_policy = rows.encode(asdict(config.coverage_policy))
        if (
            rows.encode(asdict(config)) != case["config_json"]
            or encoded_policy != case["policy_json"]
            or rows.digest(encoded_policy) != case["policy_digest"]
            or (config.case_id, config.monitor_id, config.policy_id, int(config.simulated))
            != (case["case_id"], case["monitor_id"], case["policy_id"], case["simulated"])
        ):
            raise ValueError("stored case and policy differ")
        return config
    except (KeyError, TypeError, AttributeError, json.JSONDecodeError) as error:
        raise ValueError("invalid stored case configuration") from error


def load_context(
    store: CaseStore, case_id: str, event_id: str, *, evaluated_at: datetime,
    include_source_health: bool = False,
) -> CurrentContext:
    """Load exact registered evidence and at most three relevant prior intervals."""
    with store._connect() as db:
        db.execute("BEGIN")
        return _load_context(db, case_id, event_id, evaluated_at=evaluated_at,
            include_source_health=include_source_health)


def _load_context(
    db: sqlite3.Connection,
    case_id: str,
    event_id: str,
    *,
    evaluated_at: datetime,
    prior_event_ids: tuple[str, ...] | None = None,
    review_refs: tuple[tuple[str, int, tuple[str, ...]], ...] | None = None,
    reserved_revision: int | None = None,
    include_source_health: bool = False,
    source_version_ids: tuple[int | None, ...] | None = None,
) -> CurrentContext:
    """Reconstruct a trusted snapshot inside the caller's transaction."""
    if not db.in_transaction:
        raise ValueError("context loading requires a caller transaction")
    if type(include_source_health) is not bool or (
        source_version_ids is not None and not include_source_health
    ):
        raise ValueError("invalid source health inclusion")
    if reserved_revision is not None and include_source_health and source_version_ids is None:
        raise ValueError("reserved source health requires exact version references")
    _identifier(case_id, "case_id")
    validate_event_id(event_id)
    evaluated = utc(evaluated_at)
    registry = gallinas_registry()
    if (review_refs is None) != (reserved_revision is None):
        raise ValueError("reserved work and case revision must be supplied together")
    if reserved_revision is not None:
        _expected(reserved_revision)
        if prior_event_ids is None:
            raise ValueError("reserved context requires pinned prior evidence")
    if prior_event_ids is not None:
        if type(prior_event_ids) is not tuple or len(prior_event_ids) > 3:
            raise ValueError("invalid pinned prior identities")
        for identity in prior_event_ids:
            validate_event_id(identity)
        if event_id in prior_event_ids or len(set(prior_event_ids)) != len(prior_event_ids):
            raise ValueError("duplicate pinned evidence")
    case = rows.case_row(db, case_id)
    config = _case_config(case)
    if reserved_revision is None:
        from .field_presence import require_legacy_context

        require_legacy_context(db, case_id)
        rows.check_time(case, evaluated)
    elif reserved_revision > case["revision"] or timestamp(case["created_at"]) > evaluated:
        raise ValueError("reserved context predates its case or exceeds the current revision")
    monitor_row = WatchStore._monitor(db, config.monitor_id)
    monitor = WatchStore._config(monitor_row)
    if (
        (monitor.monitor_id, monitor.case_id, monitor.station_id)
        != (
            monitor_row["monitor_id"],
            monitor_row["case_id"],
            monitor_row["station_id"],
        )
        or monitor.start_at.isoformat() != monitor_row["start_at"]
        or (not config.simulated and case_id != monitor.case_id)
    ):
        raise ValueError("stored monitor differs from case configuration")

    cache: dict[str, IntervalFacts] = {}

    def facts(identity: str) -> IntervalFacts:
        if identity not in cache:
            raw = db.execute(
                "SELECT * FROM watch_events WHERE event_id=? AND monitor_id=?",
                (identity, config.monitor_id),
            ).fetchone()
            if raw is None:
                raise KeyError("source event is not in this case monitor")
            item = WatchEvent(
                raw["event_id"],
                raw["monitor_id"],
                raw["case_id"],
                timestamp(raw["interval_start"]),
                timestamp(raw["interval_end"]),
                raw["revision"],
                raw["supersedes_event_id"],
                json.loads(raw["payload_json"]),
            )
            cache[identity] = inspect_interval(
                item, monitor, registry, config.coverage_policy, evaluated_at=evaluated
            )
        return cache[identity]

    current = facts(event_id)
    if review_refs is not None:
        active, links = _reserved_work(db, case_id, review_refs)
    else:
        active = tuple(
            rows.record(row)
            for row in db.execute(
                "SELECT r.*,c.simulated FROM current_reviews r JOIN current_cases c "
                "ON c.case_id=r.case_id WHERE r.case_id=? AND r.status IN "
                + rows.ACTIVE
                + " ORDER BY r.kind LIMIT 3",
                (case_id,),
            ).fetchall()
        )
        links = {
            review.task_id: tuple(
                row[0]
                for row in db.execute(
                    "SELECT event_id FROM current_review_evidence WHERE case_id=? AND task_id=? "
                    "ORDER BY link_id DESC LIMIT 3",
                    (case_id, review.task_id),
                ).fetchall()
            )
            for review in active
        }
    ancestor = current.supersedes_event_id
    if ancestor is not None and review_refs is None:
        for review in active:
            supported = db.execute(
                "SELECT 1 FROM current_review_evidence WHERE case_id=? AND task_id=? "
                "AND event_id=? LIMIT 1",
                (case_id, review.task_id, ancestor),
            ).fetchone()
            if supported:
                links[review.task_id] = tuple(dict.fromkeys((ancestor, *links[review.task_id])))[:3]

    if prior_event_ids is not None:
        if (
            current.supersedes_event_id is not None
            and current.supersedes_event_id not in prior_event_ids
        ):
            raise ValueError("pinned correction snapshot lacks its direct ancestor")
        selected = {identity: facts(identity) for identity in prior_event_ids}
        if any(not compare_intervals(current, item).comparable for item in selected.values()):
            raise ValueError("pinned evidence is not comparable")
    else:
        # At most nine work references: recent links plus an exact correction-basis lookup.
        linked = []
        for identity in dict.fromkeys(identity for ids in links.values() for identity in ids):
            if identity == event_id:
                continue
            item = facts(identity)
            if compare_intervals(current, item).comparable:
                linked.append(item)
        linked.sort(
            key=lambda item: (item.interval_start, item.revision, item.event_id), reverse=True
        )
        latest = db.execute(
            "SELECT event_id FROM watch_events WHERE monitor_id=? AND interval_start<? "
            "ORDER BY interval_start DESC,revision DESC LIMIT 1",
            (config.monitor_id, current.interval_start.isoformat()),
        ).fetchone()
        candidates = []
        if current.supersedes_event_id is not None:
            candidates.append(facts(current.supersedes_event_id))
        if linked:
            candidates.append(linked[0])
        if latest is not None:
            candidates.append(facts(latest[0]))
        candidates.extend(linked[1:])
        selected: dict[str, IntervalFacts] = {}
        for item in candidates:
            if compare_intervals(current, item).comparable:
                selected.setdefault(item.event_id, item)
            if len(selected) == 3:
                break
    allowlist = {event_id, *selected}
    if review_refs is not None and any(
        identity not in allowlist for ids in links.values() for identity in ids
    ):
        raise ValueError("reserved work evidence is outside the original context")
    health = None
    if include_source_health:
        from .source_health import _load_source_health

        health = _load_source_health(db, case_id, evaluated_at=evaluated,
            version_ids=source_version_ids)
    return CurrentContext(
        case_id,
        case["revision"] if reserved_revision is None else reserved_revision,
        case["policy_digest"],
        config.simulated,
        evaluated,
        current,
        tuple(selected.values()),
        active,
        tuple(
            (task_id, tuple(identity for identity in ids if identity in allowlist))
            for task_id, ids in links.items()
        ),
        registry,
        health,
    )


def _reserved_work(db: sqlite3.Connection, case_id: str, references: tuple):
    """Retrieve exact immutable review revisions and the originally allowed links."""
    if type(references) is not tuple or len(references) > 3:
        raise ValueError("invalid reserved work references")
    active, links = [], {}
    for reference in references:
        if type(reference) is not tuple or len(reference) != 3:
            raise ValueError("invalid reserved work reference")
        task_id, revision, evidence = reference
        _identifier(task_id, "task_id")
        _expected(revision)
        if revision == 0 or type(evidence) is not tuple or len(evidence) > 3:
            raise ValueError("invalid reserved work revision or evidence")
        for identity in evidence:
            validate_event_id(identity)
        if task_id in links or len(set(evidence)) != len(evidence):
            raise ValueError("duplicate reserved work or evidence")
        raw = db.execute(
            "SELECT record_json FROM current_review_revisions WHERE case_id=? AND task_id=? AND revision=?",
            (case_id, task_id, revision),
        ).fetchone()
        if raw is None:
            raise KeyError("reserved work revision does not exist in this case")
        review = rows.record(json.loads(raw["record_json"]))
        if (review.task_id, review.case_id, review.revision) != (task_id, case_id, revision):
            raise ValueError("reserved work record differs from its revision identity")
        active.append(review)
        links[task_id] = evidence
    return tuple(active), links
