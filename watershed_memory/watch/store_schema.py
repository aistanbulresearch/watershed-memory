"""Relational provenance and indexed work queues for the current watch."""

APPLICATION_ID = 0x574D4357
SCHEMA_VERSION = 1

SCHEMA = """
CREATE TABLE monitors (
    monitor_id TEXT PRIMARY KEY, config_json TEXT NOT NULL,
    station_id TEXT NOT NULL, case_id TEXT NOT NULL, start_at TEXT NOT NULL,
    fetched_through TEXT NOT NULL, next_poll_at TEXT NOT NULL,
    consecutive_failures INTEGER NOT NULL DEFAULT 0 CHECK(consecutive_failures BETWEEN 0 AND 1000000),
    last_error_code TEXT, status TEXT NOT NULL DEFAULT 'READY',
    observation_count INTEGER NOT NULL DEFAULT 0 CHECK(observation_count >= 0),
    version_count INTEGER NOT NULL DEFAULT 0 CHECK(version_count >= 0),
    event_count INTEGER NOT NULL DEFAULT 0 CHECK(event_count >= 0),
    pending_count INTEGER NOT NULL DEFAULT 0 CHECK(pending_count >= 0)
);
CREATE TABLE series_cursors (
    monitor_id TEXT NOT NULL REFERENCES monitors(monitor_id),
    series_id TEXT NOT NULL, cursor TEXT NOT NULL,
    PRIMARY KEY(monitor_id, series_id)
);
CREATE TABLE leases (
    monitor_id TEXT PRIMARY KEY REFERENCES monitors(monitor_id),
    token TEXT NOT NULL, start TEXT NOT NULL, end TEXT NOT NULL,
    acquired_at TEXT NOT NULL, expires_at TEXT NOT NULL
);
CREATE TABLE poll_receipts (
    poll_id INTEGER PRIMARY KEY, monitor_id TEXT NOT NULL REFERENCES monitors(monitor_id),
    retrieved_at TEXT NOT NULL, start TEXT NOT NULL, end TEXT NOT NULL, pages_json TEXT NOT NULL,
    UNIQUE(poll_id, monitor_id)
);
CREATE TABLE observation_versions (
    version_id INTEGER PRIMARY KEY, monitor_id TEXT NOT NULL REFERENCES monitors(monitor_id),
    series_id TEXT NOT NULL, observed_at TEXT NOT NULL, semantic_hash TEXT NOT NULL,
    value TEXT, unit TEXT NOT NULL, approval_status TEXT NOT NULL, qualifier TEXT,
    source_modified_at TEXT NOT NULL, provider_id TEXT NOT NULL, poll_id INTEGER NOT NULL,
    UNIQUE(version_id, monitor_id, series_id, observed_at),
    FOREIGN KEY(poll_id, monitor_id) REFERENCES poll_receipts(poll_id, monitor_id),
    FOREIGN KEY(monitor_id, series_id) REFERENCES series_cursors(monitor_id, series_id)
);
CREATE TABLE observations_current (
    monitor_id TEXT NOT NULL REFERENCES monitors(monitor_id),
    series_id TEXT NOT NULL, observed_at TEXT NOT NULL, version_id INTEGER NOT NULL,
    last_seen_modified_at TEXT NOT NULL, last_seen_provider_id TEXT NOT NULL,
    last_seen_poll_id INTEGER NOT NULL,
    PRIMARY KEY(monitor_id, series_id, observed_at),
    FOREIGN KEY(version_id, monitor_id, series_id, observed_at)
      REFERENCES observation_versions(version_id, monitor_id, series_id, observed_at),
    FOREIGN KEY(last_seen_poll_id, monitor_id) REFERENCES poll_receipts(poll_id, monitor_id)
);
CREATE TABLE dirty_intervals (
    monitor_id TEXT NOT NULL REFERENCES monitors(monitor_id),
    interval_start TEXT NOT NULL, interval_end TEXT NOT NULL,
    PRIMARY KEY(monitor_id, interval_start)
);
CREATE TABLE watch_events (
    event_id TEXT PRIMARY KEY, monitor_id TEXT NOT NULL REFERENCES monitors(monitor_id),
    case_id TEXT NOT NULL, interval_start TEXT NOT NULL, interval_end TEXT NOT NULL,
    revision INTEGER NOT NULL CHECK(revision > 0), supersedes_event_id TEXT,
    payload_json TEXT NOT NULL CHECK(length(payload_json) <= 16384), member_digest TEXT NOT NULL,
    UNIQUE(monitor_id, interval_start, revision), UNIQUE(event_id, monitor_id),
    UNIQUE(event_id, monitor_id, interval_start),
    UNIQUE(event_id, monitor_id, interval_start, revision),
    FOREIGN KEY(supersedes_event_id, monitor_id, interval_start)
      REFERENCES watch_events(event_id, monitor_id, interval_start)
);
CREATE TABLE event_members (
    event_id TEXT NOT NULL, monitor_id TEXT NOT NULL, version_id INTEGER NOT NULL,
    series_id TEXT NOT NULL, observed_at TEXT NOT NULL,
    PRIMARY KEY(event_id, version_id),
    FOREIGN KEY(event_id, monitor_id) REFERENCES watch_events(event_id, monitor_id),
    FOREIGN KEY(version_id, monitor_id, series_id, observed_at)
      REFERENCES observation_versions(version_id, monitor_id, series_id, observed_at)
);
CREATE TABLE watch_outbox (
    event_id TEXT PRIMARY KEY, monitor_id TEXT NOT NULL REFERENCES monitors(monitor_id),
    interval_start TEXT NOT NULL, revision INTEGER NOT NULL,
    status TEXT NOT NULL DEFAULT 'PENDING',
    FOREIGN KEY(event_id, monitor_id, interval_start, revision)
      REFERENCES watch_events(event_id, monitor_id, interval_start, revision)
);
CREATE INDEX versions_by_monitor ON observation_versions(monitor_id);
CREATE INDEX observations_by_interval ON observations_current(monitor_id, observed_at);
CREATE INDEX dirty_by_end ON dirty_intervals(monitor_id, interval_end);
CREATE INDEX outbox_by_status ON watch_outbox(monitor_id, status, interval_start, revision);
"""
