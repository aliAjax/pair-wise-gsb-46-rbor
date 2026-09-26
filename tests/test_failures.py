import tempfile
import unittest
from pathlib import Path

from app import build_service
from src.domain import Actor, Conflict, PermissionDenied


CREATE_DATA = {'patient_priority': 'critical', 'distance_km': 7.5, 'eta_minutes': 9, 'required_capability': 'ALS', 'vehicle_capability': 'ALS', 'hospital_beds': 4, 'destination': 'City Hospital', 'location': 'East Gate'}
FLOW = [('assign', 'dispatcher', {'vehicle_available': True, 'vehicle_id': 'AMB-07'}, 'assigned'), ('enroute', 'paramedic', {'traffic_level': 'medium'}, 'enroute'), ('arrive', 'paramedic', {'on_scene': True}, 'onscene'), ('transport', 'paramedic', {'destination_beds': 2}, 'transporting'), ('handover', 'hospital_coordinator', {'handover_accepted': True}, 'closed')]


class FailureTest(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.service = build_service(str(Path(self.temp.name) / "test.db"))

    def tearDown(self):
        self.temp.cleanup()

    def test_permission_and_duplicate(self):
        with self.assertRaises(PermissionDenied):
            self.service.create(Actor("outsider", "outsider"), "EMG-22001", CREATE_DATA)
        self.service.create(Actor("creator", "dispatcher"), "EMG-22001", CREATE_DATA)
        with self.assertRaises(Conflict):
            self.service.create(Actor("creator", "dispatcher"), "EMG-22001", CREATE_DATA)

    def test_stale_version_is_rejected(self):
        record = self.service.create(Actor("creator", "dispatcher"), "EMG-22001", CREATE_DATA)
        first = FLOW[0]
        record = self.service.act(Actor("operator", first[1]), record["id"], record["version"], first[0], first[2])
        second = FLOW[1]
        with self.assertRaises(Conflict):
            self.service.act(Actor("operator", second[1]), record["id"], record["version"] - 1, second[0], second[2])
