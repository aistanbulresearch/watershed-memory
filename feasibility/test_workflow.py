"""Safety and persistence tests; all inputs in this file are synthetic fixtures."""

import sqlite3
import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch

from feasibility.workflow import CaseStore


def fixture(event_id="synthetic-july", at="2022-07-11T06:00:00+00:00", missing=False):
    return {
        "event_id": event_id,
        "evidence_class": "SYNTHETIC_TEST_FIXTURE",
        "available_at": at,
        "availability_basis": "test fixture clock",
        "observations_end": at,
        "p2_turbidity_count": 100,
        "p1_turbidity_count": 0 if missing else 100,
        "source_ids": ["synthetic-test-source"],
    }


class WorkflowTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.db = Path(self.temp.name) / "case.sqlite"

    def tearDown(self):
        self.temp.cleanup()

    def test_restart_preserves_one_task_and_operator_response(self):
        with CaseStore(self.db) as store:
            store.ingest(fixture(), now="2022-07-12T00:00:00+00:00")
            task = store.snapshot()["tasks"][0]
            store.respond(task["task_id"], "acknowledge", "Demo operator", "Needs review", simulated=True)
        with CaseStore(self.db) as store:
            snap = store.snapshot()
            self.assertEqual(len(snap["tasks"]), 1)
            self.assertEqual(snap["tasks"][0]["status"], "ACKNOWLEDGED")
            self.assertEqual(snap["responses"][0]["simulated"], 1)

    def test_next_event_updates_unfinished_task_instead_of_duplicating(self):
        with CaseStore(self.db) as store:
            store.ingest(fixture(), now="2022-07-12T00:00:00+00:00")
            store.ingest(fixture("synthetic-august", "2022-08-05T06:00:00+00:00"), now="2022-08-06T00:00:00+00:00")
            snap = store.snapshot()
            self.assertEqual(len(snap["tasks"]), 1)
            self.assertEqual(len(snap["task_evidence"]), 2)

    def test_duplicate_delivery_does_not_reopen_completed_task(self):
        with CaseStore(self.db) as store:
            event = fixture()
            store.ingest(event, now="2022-07-12T00:00:00+00:00")
            task = store.snapshot()["tasks"][0]
            store.respond(task["task_id"], "complete_review", "Demo operator", "Reviewed evidence only", simulated=True)
            self.assertEqual(store.ingest(event, now="2022-07-12T00:00:00+00:00"), "DUPLICATE_IGNORED")
            self.assertEqual(store.snapshot()["tasks"][0]["status"], "COMPLETED")
            self.assertEqual(store.snapshot()["case"]["status"], "OPEN")

    def test_new_event_after_completion_creates_new_review(self):
        with CaseStore(self.db) as store:
            store.ingest(fixture(), now="2022-07-12T00:00:00+00:00")
            task = store.snapshot()["tasks"][0]
            store.respond(task["task_id"], "complete_review", "Demo operator", "Reviewed", simulated=True)
            store.ingest(fixture("synthetic-august", "2022-08-05T06:00:00+00:00"), now="2022-08-06T00:00:00+00:00")
            self.assertEqual(len(store.snapshot()["tasks"]), 2)

    def test_missing_monitor_creates_gap_work_without_safety_claim(self):
        with CaseStore(self.db) as store:
            store.ingest(fixture(missing=True), now="2022-07-12T00:00:00+00:00")
            snap = store.snapshot()
            self.assertEqual({t["kind"] for t in snap["tasks"]}, {"MONITORING_REVIEW", "EVIDENCE_GAP_REVIEW"})
            self.assertEqual(snap["case"]["monitoring_confidence"], "DEGRADED")
            self.assertEqual(snap["case"]["water_safety"], "NOT_ASSESSED")

    def test_future_evidence_rejected_without_partial_write(self):
        with CaseStore(self.db) as store:
            with self.assertRaises(ValueError):
                store.ingest(fixture(), now="2022-07-09T00:00:00+00:00")
            self.assertEqual(store.snapshot()["events"], [])

    def test_claimed_availability_before_observation_end_rejected(self):
        event = fixture()
        event["observations_end"] = "2022-07-13T00:00:00+00:00"
        with CaseStore(self.db) as store:
            with self.assertRaises(ValueError):
                store.ingest(event, now="2022-07-14T00:00:00+00:00")

    def test_same_event_id_with_changed_payload_rejected(self):
        with CaseStore(self.db) as store:
            store.ingest(fixture(), now="2022-07-12T00:00:00+00:00")
            with self.assertRaises(ValueError):
                store.ingest(fixture(missing=True), now="2022-07-12T00:00:00+00:00")

    def test_unsafe_action_and_unlabelled_operator_rejected(self):
        with CaseStore(self.db) as store:
            store.ingest(fixture(), now="2022-07-12T00:00:00+00:00")
            task = store.snapshot()["tasks"][0]
            with self.assertRaises(ValueError):
                store.respond(task["task_id"], "declare_safe", "Demo operator", "", simulated=True)
            with self.assertRaises(ValueError):
                store.respond(task["task_id"], "acknowledge", "Demo operator", "x", simulated=False)

    def test_late_event_cannot_restore_monitoring_confidence(self):
        with CaseStore(self.db) as store:
            store.ingest(fixture("synthetic-september", "2022-09-12T06:00:00+00:00", missing=True), now="2022-09-13T00:00:00+00:00")
            store.ingest(fixture(), now="2022-09-13T00:00:00+00:00")
            self.assertEqual(store.snapshot()["case"]["monitoring_confidence"], "DEGRADED")

    def test_naive_timestamps_rejected(self):
        event = fixture(at="2022-07-11T06:00:00")
        with CaseStore(self.db) as store:
            with self.assertRaises(ValueError):
                store.ingest(event, now="2022-07-12T00:00:00+00:00")

    def test_unknown_task_cannot_receive_response(self):
        with CaseStore(self.db) as store:
            with self.assertRaises(ValueError):
                store.respond("not-a-task", "acknowledge", "Demo operator", "x", simulated=True)

    def test_old_observations_published_later_do_not_restore_confidence(self):
        with CaseStore(self.db) as store:
            store.ingest(fixture("september", "2022-09-12T06:00:00+00:00", missing=True), now="2022-09-13T00:00:00+00:00")
            delayed = fixture()
            delayed["available_at"] = "2022-09-14T00:00:00+00:00"
            store.ingest(delayed, now="2022-09-15T00:00:00+00:00")
            self.assertEqual(store.snapshot()["case"]["monitoring_confidence"], "DEGRADED")

    def test_equal_window_conflict_does_not_overwrite_case(self):
        with CaseStore(self.db) as store:
            store.ingest(fixture(missing=True), now="2022-07-12T00:00:00+00:00")
            before = store.snapshot()
            with self.assertRaises(ValueError):
                store.ingest(fixture(event_id="conflicting-same-window"), now="2022-07-12T00:00:00+00:00")
            self.assertEqual(before, store.snapshot())

    def test_operator_retries_are_idempotent_and_collisions_rejected(self):
        with CaseStore(self.db) as store:
            store.ingest(fixture(), now="2022-07-12T00:00:00+00:00")
            task = store.snapshot()["tasks"][0]["task_id"]
            store.respond(task, "complete_review", "Demo", "Reviewed", simulated=True, response_id="response-1")
            self.assertEqual(store.respond(task, "complete_review", "Demo", "Reviewed", simulated=True, response_id="response-1"), "DUPLICATE_RESPONSE_IGNORED")
            with self.assertRaises(ValueError):
                store.respond(task, "complete_review", "Demo", "Changed", simulated=True, response_id="response-1")
            self.assertEqual(len(store.snapshot()["responses"]), 1)

    def test_failure_during_task_creation_rolls_back_event_and_case(self):
        with CaseStore(self.db) as store:
            before = store.snapshot()
            with patch.object(store, "_attach_task", side_effect=RuntimeError("injected failure")):
                with self.assertRaises(RuntimeError):
                    store.ingest(fixture(), now="2022-07-12T00:00:00+00:00")
            self.assertEqual(before, store.snapshot())

    def test_failure_during_response_insert_rolls_back_task_update(self):
        with CaseStore(self.db) as store:
            store.ingest(fixture(), now="2022-07-12T00:00:00+00:00")
            task = store.snapshot()["tasks"][0]["task_id"]
            before = store.snapshot()
            store.db.execute("CREATE TRIGGER fail_response BEFORE INSERT ON responses BEGIN SELECT RAISE(ABORT, 'injected'); END")
            with self.assertRaises(sqlite3.IntegrityError):
                store.respond(task, "complete_review", "Demo", "Reviewed", simulated=True)
            self.assertEqual(before, store.snapshot())


if __name__ == "__main__":
    unittest.main()
