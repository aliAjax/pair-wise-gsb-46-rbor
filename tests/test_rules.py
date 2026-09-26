import unittest

from src.domain import Actor, ValidationError
from src.rules import DomainRules


CREATE_DATA = {'patient_priority': 'critical', 'distance_km': 7.5, 'eta_minutes': 9, 'required_capability': 'ALS', 'vehicle_capability': 'ALS', 'hospital_beds': 4, 'destination': 'City Hospital', 'location': 'East Gate'}
FLOW = [('assign', 'dispatcher', {'vehicle_available': True, 'vehicle_id': 'AMB-07'}, 'assigned'), ('enroute', 'paramedic', {'traffic_level': 'medium'}, 'enroute'), ('arrive', 'paramedic', {'on_scene': True}, 'onscene'), ('transport', 'paramedic', {'destination_beds': 2}, 'transporting'), ('handover', 'hospital_coordinator', {'handover_accepted': True}, 'closed')]


class RulesTest(unittest.TestCase):
    def setUp(self):
        self.rules = DomainRules()

    def test_prepare_create(self):
        prepared = self.rules.prepare_create(CREATE_DATA)
        self.assertEqual(prepared["sla_minutes"], 8)
        self.assertTrue(prepared["capability_ok"])
        self.assertGreater(prepared["priority_score"], 100)

    def test_action_calculation(self):
        action, role, data, expected_state = FLOW[0]
        record = {"id": 1, "state": self.rules.INITIAL_STATE, "payload": self.rules.prepare_create(CREATE_DATA)}
        state, payload, summary = self.rules.apply_action(record, action, data)
        self.assertEqual(state, expected_state)
        self.assertEqual(payload["assigned_vehicle_id"], "AMB-07")

    def test_invalid_input(self):
        invalid = dict(CREATE_DATA)
        invalid["patient_priority"] = 'unknown'
        with self.assertRaises(ValidationError):
            self.rules.prepare_create(invalid)
