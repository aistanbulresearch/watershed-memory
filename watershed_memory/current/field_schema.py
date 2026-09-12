"""Atomic versioned field-work extension beside the accepted current ledger."""

import sqlite3

from .case_schema import ensure_schema as ensure_cases

VERSION = 1
SCHEMA = {
    "field_schema_version": """CREATE TABLE field_schema_version (
  singleton INTEGER PRIMARY KEY CHECK(singleton=1),
  version INTEGER NOT NULL
)""",
    "field_plans": """CREATE TABLE field_plans (
  plan_id TEXT PRIMARY KEY,
  case_id TEXT NOT NULL,
  task_id TEXT NOT NULL,
  review_revision INTEGER NOT NULL CHECK(review_revision>0),
  current_revision INTEGER NOT NULL CHECK(current_revision>0),
  status TEXT NOT NULL CHECK(status IN
    ('PROPOSED','APPROVED','DEFERRED','CANCELLED','REPORTED')),
  simulated INTEGER NOT NULL CHECK(simulated IN (0,1)),
  created_at TEXT NOT NULL,
  updated_at TEXT NOT NULL,
  last_activity_revision INTEGER NOT NULL CHECK(last_activity_revision>0),
  UNIQUE(case_id,plan_id),
  UNIQUE(case_id,plan_id,task_id,review_revision),
  FOREIGN KEY(case_id,task_id) REFERENCES current_reviews(case_id,task_id),
  FOREIGN KEY(case_id,task_id,review_revision)
    REFERENCES current_review_revisions(case_id,task_id,revision),
  FOREIGN KEY(case_id,plan_id,current_revision)
    REFERENCES field_plan_revisions(case_id,plan_id,revision)
    DEFERRABLE INITIALLY DEFERRED,
  FOREIGN KEY(case_id,plan_id,last_activity_revision)
    REFERENCES field_receipts(case_id,plan_id,case_revision)
    DEFERRABLE INITIALLY DEFERRED
)""",
    "field_plan_revisions": """CREATE TABLE field_plan_revisions (
  case_id TEXT NOT NULL,
  plan_id TEXT NOT NULL,
  revision INTEGER NOT NULL CHECK(revision>0),
  task_id TEXT NOT NULL,
  review_revision INTEGER NOT NULL CHECK(review_revision>0),
  record_json TEXT NOT NULL CHECK(length(record_json)<=16384),
  recorded_at TEXT NOT NULL,
  PRIMARY KEY(case_id,plan_id,revision),
  FOREIGN KEY(case_id,plan_id,task_id,review_revision)
    REFERENCES field_plans(case_id,plan_id,task_id,review_revision),
  FOREIGN KEY(case_id,task_id,review_revision)
    REFERENCES current_review_revisions(case_id,task_id,revision)
)""",
    "field_reports": """CREATE TABLE field_reports (
  report_id TEXT PRIMARY KEY,
  case_id TEXT NOT NULL,
  plan_id TEXT NOT NULL,
  performed_plan_revision INTEGER NOT NULL CHECK(performed_plan_revision>0),
  current_revision INTEGER NOT NULL CHECK(current_revision>0),
  outcome TEXT NOT NULL CHECK(outcome IN ('COMPLETE','PARTIAL','NOT_DONE')),
  simulated INTEGER NOT NULL CHECK(simulated IN (0,1)),
  created_at TEXT NOT NULL,
  updated_at TEXT NOT NULL,
  UNIQUE(case_id,report_id),
  UNIQUE(case_id,report_id,plan_id,performed_plan_revision),
  UNIQUE(case_id,plan_id),
  FOREIGN KEY(case_id,plan_id,performed_plan_revision)
    REFERENCES field_plan_revisions(case_id,plan_id,revision),
  FOREIGN KEY(case_id,report_id,current_revision)
    REFERENCES field_report_revisions(case_id,report_id,revision)
    DEFERRABLE INITIALLY DEFERRED
)""",
    "field_report_revisions": """CREATE TABLE field_report_revisions (
  case_id TEXT NOT NULL,
  report_id TEXT NOT NULL,
  revision INTEGER NOT NULL CHECK(revision>0),
  plan_id TEXT NOT NULL,
  performed_plan_revision INTEGER NOT NULL CHECK(performed_plan_revision>0),
  record_json TEXT NOT NULL CHECK(length(record_json)<=8192),
  recorded_at TEXT NOT NULL,
  PRIMARY KEY(case_id,report_id,revision),
  FOREIGN KEY(case_id,report_id,plan_id,performed_plan_revision)
    REFERENCES field_reports(case_id,report_id,plan_id,performed_plan_revision),
  FOREIGN KEY(case_id,plan_id,performed_plan_revision)
    REFERENCES field_plan_revisions(case_id,plan_id,revision)
)""",
    "field_evidence": """CREATE TABLE field_evidence (
  evidence_id TEXT PRIMARY KEY,
  case_id TEXT NOT NULL,
  report_id TEXT NOT NULL,
  report_revision INTEGER NOT NULL CHECK(report_revision>0),
  evidence_digest TEXT NOT NULL CHECK(length(evidence_digest)=64),
  record_json TEXT NOT NULL CHECK(length(record_json)<=4096),
  recorded_at TEXT NOT NULL,
  UNIQUE(case_id,evidence_id),
  UNIQUE(case_id,evidence_id,report_id,report_revision),
  UNIQUE(case_id,report_id,report_revision,evidence_digest),
  FOREIGN KEY(case_id,report_id,report_revision)
    REFERENCES field_report_revisions(case_id,report_id,revision)
)""",
    "field_verifications": """CREATE TABLE field_verifications (
  sequence INTEGER PRIMARY KEY,
  verification_id TEXT NOT NULL UNIQUE,
  case_id TEXT NOT NULL,
  report_id TEXT NOT NULL,
  report_revision INTEGER NOT NULL CHECK(report_revision>0),
  evidence_ids_json TEXT NOT NULL CHECK(length(evidence_ids_json)<=2048),
  record_json TEXT NOT NULL CHECK(length(record_json)<=4096),
  recorded_at TEXT NOT NULL,
  UNIQUE(case_id,verification_id),
  UNIQUE(case_id,verification_id,report_id,report_revision),
  FOREIGN KEY(case_id,report_id,report_revision)
    REFERENCES field_report_revisions(case_id,report_id,revision)
)""",
    "field_receipts": """CREATE TABLE field_receipts (
  case_id TEXT NOT NULL,
  request_id TEXT NOT NULL,
  operation TEXT NOT NULL CHECK(operation IN
    ('PROPOSE','DECIDE','MODIFY','REPORT','CORRECT','ATTACH','VERIFY')),
  input_hash TEXT NOT NULL CHECK(length(input_hash)=64),
  input_json TEXT NOT NULL CHECK(length(input_json)<=16384),
  result_json TEXT NOT NULL CHECK(length(result_json)<=32768),
  result_hash TEXT NOT NULL CHECK(length(result_hash)=64),
  case_revision INTEGER NOT NULL CHECK(case_revision>0),
  plan_id TEXT NOT NULL,
  plan_revision INTEGER NOT NULL CHECK(plan_revision>0),
  recorded_at TEXT NOT NULL,
  PRIMARY KEY(case_id,request_id),
  UNIQUE(case_id,case_revision),
  UNIQUE(case_id,plan_id,case_revision),
  FOREIGN KEY(case_id,plan_id,plan_revision)
    REFERENCES field_plan_revisions(case_id,plan_id,revision)
)""",
    "field_receipt_results": """CREATE TABLE field_receipt_results (
  case_id TEXT NOT NULL, request_id TEXT NOT NULL,
  report_id TEXT NOT NULL, report_revision INTEGER NOT NULL CHECK(report_revision>0),
  verification_id TEXT, evidence_id TEXT,
  PRIMARY KEY(case_id,request_id),
  UNIQUE(case_id,request_id,report_id,report_revision),
  FOREIGN KEY(case_id,request_id) REFERENCES field_receipts(case_id,request_id),
  FOREIGN KEY(case_id,report_id,report_revision)
    REFERENCES field_report_revisions(case_id,report_id,revision),
  FOREIGN KEY(case_id,verification_id,report_id,report_revision)
    REFERENCES field_verifications(case_id,verification_id,report_id,report_revision),
  FOREIGN KEY(case_id,evidence_id,report_id,report_revision)
    REFERENCES field_evidence(case_id,evidence_id,report_id,report_revision)
)""",
    "field_receipt_evidence": """CREATE TABLE field_receipt_evidence (
  case_id TEXT NOT NULL, request_id TEXT NOT NULL,
  report_id TEXT NOT NULL, report_revision INTEGER NOT NULL CHECK(report_revision>0),
  evidence_id TEXT NOT NULL,
  PRIMARY KEY(case_id,request_id,evidence_id),
  FOREIGN KEY(case_id,request_id,report_id,report_revision)
    REFERENCES field_receipt_results(case_id,request_id,report_id,report_revision),
  FOREIGN KEY(case_id,evidence_id,report_id,report_revision)
    REFERENCES field_evidence(case_id,evidence_id,report_id,report_revision)
)""",
    "field_active_review": """CREATE UNIQUE INDEX field_active_review
  ON field_plans(case_id,task_id)
  WHERE status IN ('PROPOSED','APPROVED','DEFERRED')""",
    "field_activity_monotonic": """CREATE TRIGGER field_activity_monotonic
  BEFORE UPDATE OF last_activity_revision ON field_plans
  WHEN NEW.last_activity_revision < OLD.last_activity_revision
  BEGIN SELECT RAISE(ABORT, 'field activity revision cannot move backwards'); END""",
    "field_plan_review_recent": """CREATE INDEX field_plan_review_recent
  ON field_plans(case_id,task_id,last_activity_revision DESC)""",
    "field_plan_case_recent": """CREATE INDEX field_plan_case_recent
  ON field_plans(case_id,last_activity_revision DESC)""",
    "field_report_recent": """CREATE INDEX field_report_recent
  ON field_reports(case_id,updated_at DESC,report_id)""",
    "field_evidence_for_revision": """CREATE INDEX field_evidence_for_revision
  ON field_evidence(case_id,report_id,report_revision,evidence_id)""",
    "field_verification_for_revision": """CREATE INDEX field_verification_for_revision
  ON field_verifications(case_id,report_id,report_revision,sequence DESC)""",
}


def ensure_schema(db: sqlite3.Connection) -> None:
    """Create or verify the full extension inside the caller's transaction."""
    if not db.in_transaction:
        raise ValueError("field schema requires an explicit transaction")
    ensure_cases(db)
    present = dict(db.execute("SELECT name,sql FROM sqlite_master WHERE name GLOB 'field_*'"))
    if present:
        if set(present) != set(SCHEMA):
            raise ValueError("partial or unknown field-work schema")
        for name, expected in SCHEMA.items():
            if " ".join((present[name] or "").split()) != " ".join(expected.split()):
                raise ValueError("field-work schema definition differs from this version")
        rows = db.execute("SELECT singleton,version FROM field_schema_version").fetchall()
        if [tuple(row) for row in rows] != [(1, VERSION)]:
            raise ValueError("unsupported field-work schema version")
        return
    for statement in SCHEMA.values():
        db.execute(statement)
    db.execute("INSERT INTO field_schema_version VALUES(1,?)", (VERSION,))
