import sqlite3
import tempfile
import unittest
from datetime import datetime, timedelta, timezone
from pathlib import Path

from app import build_service
from src.domain import Actor


CREATE_DATA = {'patient_priority': 'critical', 'distance_km': 7.5, 'eta_minutes': 9, 'required_capability': 'ALS', 'vehicle_capability': 'ALS', 'hospital_beds': 1, 'destination': 'City Hospital', 'location': 'East Gate'}
DISPATCHER = Actor("dispatcher-1", "dispatcher")
VIEWER = Actor("viewer", "admin")


class BedReservationTest(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.db_path = str(Path(self.temp.name) / "test.db")
        self.service = build_service(self.db_path)

    def tearDown(self):
        self.temp.cleanup()

    def _create(self, reference, **overrides):
        data = dict(CREATE_DATA)
        data.update(overrides)
        return self.service.create(DISPATCHER, reference, data)

    def _assign(self, record, vehicle="AMB-07", **extra):
        data = {'vehicle_available': True, 'vehicle_id': vehicle}
        data.update(extra)
        return self.service.act(DISPATCHER, record["id"], record["version"], "assign", data)

    def _reservations(self):
        return self.service.list_reservations(VIEWER)

    def test_assign_holds_bed_with_expiry(self):
        record = self._create("EMG-1")
        record = self._assign(record)
        self.assertEqual(record["state"], "assigned")
        self.assertTrue(record["payload"]["bed_reservation_id"])
        self.assertFalse(record["payload"]["awaiting_beds"])
        reservations = self._reservations()
        self.assertEqual(len(reservations), 1)
        self.assertEqual(reservations[0]["status"], "held")
        self.assertEqual(reservations[0]["hospital"], "City Hospital")
        self.assertIsNotNone(reservations[0]["expires_at"])
        self.assertIsNone(reservations[0]["released_reason"])

    def test_second_task_waits_when_no_bed_left(self):
        first = self._assign(self._create("EMG-1"))
        self.assertEqual(first["state"], "assigned")
        second = self._assign(self._create("EMG-2"), vehicle="AMB-08")
        self.assertEqual(second["state"], "awaiting_beds")
        self.assertEqual(second["payload"]["beds_short"], 1)
        self.assertTrue(second["payload"]["awaiting_beds"])
        self.assertEqual(len(self._reservations()), 1)

    def test_cancel_releases_bed_and_waiting_task_can_assign(self):
        first = self._assign(self._create("EMG-1"))
        second = self._assign(self._create("EMG-2"), vehicle="AMB-08")
        self.assertEqual(second["state"], "awaiting_beds")
        first = self.service.act(DISPATCHER, first["id"], first["version"], "cancel", {"cancel_reason": "家属拒绝送医"})
        self.assertEqual(first["state"], "cancelled")
        reservations = self._reservations()
        self.assertEqual(reservations[0]["status"], "released")
        self.assertIn("取消", reservations[0]["released_reason"])
        second = self._assign(second, vehicle="AMB-08")
        self.assertEqual(second["state"], "assigned")
        self.assertEqual(second["payload"]["beds_short"], 0)

    def test_handover_marks_reservation_admitted(self):
        record = self._assign(self._create("EMG-1"))
        flow = [
            ("enroute", "paramedic", {'traffic_level': 'low'}),
            ("arrive", "paramedic", {'on_scene': True}),
            ("transport", "paramedic", {'destination_beds': 1}),
            ("handover", "hospital_coordinator", {'handover_accepted': True}),
        ]
        for action, role, data in flow:
            record = self.service.act(Actor("op", role), record["id"], record["version"], action, data)
        self.assertEqual(record["state"], "closed")
        reservations = self._reservations()
        self.assertEqual(reservations[0]["status"], "admitted")
        self.assertIsNone(reservations[0]["released_reason"])

    def test_expired_hold_is_auto_released(self):
        record = self._assign(self._create("EMG-1"), hold_minutes=1)
        self.assertEqual(record["state"], "assigned")
        past = (datetime.now(timezone.utc) - timedelta(minutes=5)).isoformat()
        with sqlite3.connect(self.db_path) as connection:
            connection.execute("UPDATE bed_reservations SET expires_at=?", (past,))
        waiting = self._assign(self._create("EMG-2"), vehicle="AMB-08")
        self.assertEqual(waiting["state"], "assigned")
        reservations = self._reservations()
        by_id = {item["record_id"]: item for item in reservations}
        self.assertEqual(by_id[record["id"]]["status"], "released")
        self.assertIn("超时", by_id[record["id"]]["released_reason"])
        timeline = self.service.timeline(VIEWER, record["id"])
        self.assertEqual(timeline[-1]["action"], "bed_hold_expired")

    def test_reservations_survive_restart(self):
        record = self._assign(self._create("EMG-1"))
        self.assertEqual(record["state"], "assigned")
        restarted = build_service(self.db_path)
        reservations = restarted.list_reservations(VIEWER)
        self.assertEqual(len(reservations), 1)
        self.assertEqual(reservations[0]["status"], "held")


if __name__ == "__main__":
    unittest.main()
