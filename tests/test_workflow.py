import tempfile
import unittest
from pathlib import Path

from app import build_service
from src.domain import Actor, Conflict


CREATE_DATA = {'patient_priority': 'critical', 'distance_km': 7.5, 'eta_minutes': 9, 'required_capability': 'ALS', 'vehicle_capability': 'ALS', 'hospital_beds': 4, 'destination': 'City Hospital', 'location': 'East Gate'}
FLOW = [('assign', 'dispatcher', {'vehicle_available': True, 'vehicle_id': 'AMB-07'}, 'assigned'), ('enroute', 'paramedic', {'traffic_level': 'medium'}, 'enroute'), ('arrive', 'paramedic', {'on_scene': True}, 'onscene'), ('transport', 'paramedic', {'destination_beds': 2}, 'transporting'), ('handover', 'hospital_coordinator', {'handover_accepted': True}, 'closed')]


class WorkflowTest(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.service = build_service(str(Path(self.temp.name) / "test.db"))

    def tearDown(self):
        self.temp.cleanup()

    def test_complete_workflow_and_audit(self):
        record = self.service.create(Actor("creator", "dispatcher"), "EMG-22001", CREATE_DATA)
        self.assertEqual(record["state"], "received")
        for action, role, data, expected_state in FLOW:
            record = self.service.act(Actor("operator", role), record["id"], record["version"], action, data)
            self.assertEqual(record["state"], expected_state)
        timeline = self.service.timeline(Actor("creator", "dispatcher"), record["id"])
        self.assertEqual(len(timeline), len(FLOW) + 1)
        self.assertEqual(timeline[-1]["action"], FLOW[-1][0])
