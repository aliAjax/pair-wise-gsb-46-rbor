import threading
import tempfile
import unittest
from datetime import datetime, timedelta, timezone
from pathlib import Path

from app import build_service
from src.domain import Actor, BedShortage, Conflict
from src.repository import COMMITMENT_ADMITTED, COMMITMENT_HELD, COMMITMENT_RELEASED


DISPATCHER = Actor("dispatcher-1", "dispatcher")
DISPATCHER_2 = Actor("dispatcher-2", "dispatcher")
PARAMEDIC = Actor("medic-1", "paramedic")
COORDINATOR = Actor("coord-1", "hospital_coordinator")


def make_data(beds=1, destination="One Bed Hospital", **overrides):
    data = {
        "patient_priority": "critical",
        "distance_km": 3.0,
        "eta_minutes": 8,
        "required_capability": "ALS",
        "vehicle_capability": "ALS",
        "hospital_beds": beds,
        "destination": destination,
        "location": "Station " + destination,
    }
    data.update(overrides)
    return data


def iso_future(minutes):
    return (datetime.now(timezone.utc) + timedelta(minutes=minutes)).isoformat()


class BedCommitmentTest(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.db_path = str(Path(self.temp.name) / "test.db")
        self.service = build_service(self.db_path)

    def tearDown(self):
        self.service.close()
        self.temp.cleanup()

    def _create(self, reference, beds=1, destination="One Bed Hospital"):
        return self.service.create(DISPATCHER, reference, make_data(beds=beds, destination=destination))

    def _assign(self, record, vehicle="AMB-07", actor=DISPATCHER, hold_minutes=30):
        return self.service.act(
            actor,
            record["id"],
            record["version"],
            "assign",
            {"vehicle_available": True, "vehicle_id": vehicle, "hold_minutes": hold_minutes},
        )

    def test_concurrent_assign_sells_bed_once(self):
        first = self._create("EMG-BED-1")
        second = self._create("EMG-BED-2")
        results = {}

        def worker(key, record, actor, vehicle):
            try:
                results[key] = ("ok", self._assign(record, vehicle=vehicle, actor=actor))
            except Exception as exc:  # noqa: BLE001 - 需要在主线程断言
                results[key] = ("error", exc)

        t1 = threading.Thread(target=worker, args=("a", first, DISPATCHER, "AMB-01"))
        t2 = threading.Thread(target=worker, args=("b", second, DISPATCHER_2, "AMB-02"))
        t1.start()
        t2.start()
        t1.join(15)
        t2.join(15)

        statuses = {key: value[0] for key, value in results.items()}
        self.assertEqual(sorted(statuses.values()), ["error", "ok"])
        error = next(value[1] for value in results.values() if value[0] == "error")
        self.assertIsInstance(error, BedShortage)
        self.assertEqual(error.details["shortage_beds"], 1)
        self.assertEqual(error.details["available_beds"], 0)
        self.assertEqual(error.details["total_beds"], 1)

        hospital = {h["name"]: h for h in self.service.hospital_board(DISPATCHER)}["One Bed Hospital"]
        self.assertEqual(hospital["held_beds"], 1)
        self.assertEqual(hospital["admitted_beds"], 0)
        self.assertEqual(hospital["available_beds"], 0)

        held = [c for c in self.service.commitments(DISPATCHER)["items"] if c["status"] == COMMITMENT_HELD]
        self.assertEqual(len(held), 1)
        self.assertIsNotNone(held[0]["expires_at"])

        # 失败的任务仍停留在待派区，并写明缺少1张床
        pending_items = {item["record"]["reference"]: item for item in self.service.pending(DISPATCHER)["items"]}
        failed_ref = "EMG-BED-2" if statuses["b"] == "error" else "EMG-BED-1"
        succeeded_ref = "EMG-BED-1" if failed_ref == "EMG-BED-2" else "EMG-BED-2"
        self.assertNotIn(succeeded_ref, pending_items)
        self.assertIn(failed_ref, pending_items)
        self.assertEqual(pending_items[failed_ref]["beds"]["shortage_beds"], 1)
        self.assertFalse(pending_items[failed_ref]["dispatchable"])

    def test_cancel_releases_hold_with_reason(self):
        record = self._create("EMG-CAN-1")
        record = self._assign(record, vehicle="AMB-09")
        self.assertEqual(record["state"], "assigned")
        record = self.service.act(
            DISPATCHER, record["id"], record["version"], "cancel", {"cancel_reason": "患者改道其他医院"}
        )
        self.assertEqual(record["state"], "cancelled")
        commitment = self.service.commitments(DISPATCHER)["items"][0]
        self.assertEqual(commitment["status"], COMMITMENT_RELEASED)
        self.assertTrue(commitment["release_reason"].startswith("任务取消"))
        self.assertIn("患者改道其他医院", commitment["release_reason"])
        self.assertIsNotNone(commitment["released_at"])
        hospital = {h["name"]: h for h in self.service.hospital_board(DISPATCHER)}["One Bed Hospital"]
        self.assertEqual(hospital["available_beds"], 1)

        # 床退回后，待派区可以再次派车
        other = self._create("EMG-CAN-2")
        other = self._assign(other, vehicle="AMB-10")
        self.assertEqual(other["state"], "assigned")

    def test_timeout_releases_after_no_arrival_and_restart_persists(self):
        record = self._create("EMG-EXP-1")
        record = self._assign(record, vehicle="AMB-11", hold_minutes=1)
        commitment = self.service.repository.active_commitment(record["id"])
        self.assertIsNotNone(commitment)
        expires_at = commitment["expires_at"]

        # 服务重启：占用仍在数据库中，没有丢失
        self.service.close()
        restarted = build_service(self.db_path)
        try:
            still_held = [c for c in restarted.commitments(DISPATCHER)["items"] if c["status"] == COMMITMENT_HELD]
            self.assertEqual(len(still_held), 1)
            hospital = {h["name"]: h for h in restarted.hospital_board(DISPATCHER)}["One Bed Hospital"]
            self.assertEqual(hospital["available_beds"], 0)

            # 到期后由系统结算退回
            expired = restarted.repository.expire_commitments(now=iso_future(2))
            self.assertEqual(len(expired), 1)
            self.assertEqual(expired[0]["release_reason"], "超时未到场，承诺自动退回")
            commitment_after = restarted.commitments(DISPATCHER)["items"][0]
            self.assertEqual(commitment_after["status"], COMMITMENT_RELEASED)
            self.assertIsNotNone(commitment_after["released_at"])
            self.assertEqual(commitment_after["expires_at"], expires_at)
            hospital = {h["name"]: h for h in restarted.hospital_board(DISPATCHER)}["One Bed Hospital"]
            self.assertEqual(hospital["available_beds"], 1)

            timeline = restarted.timeline(DISPATCHER, record["id"])
            self.assertTrue(any(event["action"] == "bed_released" for event in timeline))

            # 占用已退回：继续走流程可以到场/转运，但交接时不能吞掉别人的床
            record = restarted.repository.get(record["id"])
            record = restarted.act(PARAMEDIC, record["id"], record["version"], "enroute", {"traffic_level": "low"})
            record = restarted.act(PARAMEDIC, record["id"], record["version"], "arrive", {"on_scene": True})
            record = restarted.act(PARAMEDIC, record["id"], record["version"], "transport", {"destination_beds": 1})
            with self.assertRaises(Conflict) as ctx:
                restarted.act(COORDINATOR, record["id"], record["version"], "handover", {"handover_accepted": True})
            self.assertIn("没有有效的床位占用", str(ctx.exception))
        finally:
            restarted.close()

    def test_arrival_immunes_expiry_and_handover_admits(self):
        record = self._create("EMG-ARR-1")
        record = self._assign(record, vehicle="AMB-12", hold_minutes=1)

        record = self.service.act(PARAMEDIC, record["id"], record["version"], "enroute", {"traffic_level": "low"})
        record = self.service.act(PARAMEDIC, record["id"], record["version"], "arrive", {"on_scene": True})

        # 已到场：即使超过保留时长，占用也不退回
        expired = self.service.repository.expire_commitments(now=iso_future(30))
        self.assertEqual(expired, [])
        commitment = self.service.commitments(DISPATCHER)["items"][0]
        self.assertEqual(commitment["status"], COMMITMENT_HELD)
        self.assertIsNone(commitment["expires_at"])
        self.assertIsNotNone(commitment["arrived_at"])

        record = self.service.act(PARAMEDIC, record["id"], record["version"], "transport", {"destination_beds": 1})
        record = self.service.act(COORDINATOR, record["id"], record["version"], "handover", {"handover_accepted": True})
        self.assertEqual(record["state"], "closed")

        commitment = self.service.commitments(DISPATCHER)["items"][0]
        self.assertEqual(commitment["status"], COMMITMENT_ADMITTED)
        self.assertIsNone(commitment["release_reason"])
        hospital = {h["name"]: h for h in self.service.hospital_board(DISPATCHER)}["One Bed Hospital"]
        self.assertEqual(hospital["admitted_beds"], 1)
        self.assertEqual(hospital["held_beds"], 0)
        self.assertEqual(hospital["available_beds"], 0)

    def test_bed_shortage_leaves_task_in_pending(self):
        record = self._create("EMG-SHORT-1")
        self._assign(record, vehicle="AMB-13")
        other = self._create("EMG-SHORT-2")
        with self.assertRaises(BedShortage) as ctx:
            self._assign(other, vehicle="AMB-14")
        self.assertIn("暂无余床", str(ctx.exception))

        # 缺床不改变状态与版本，任务仍在待派区
        other = self.service.repository.get(other["id"])
        self.assertEqual(other["state"], "received")
        self.assertEqual(other["version"], 1)

    def test_hold_minutes_validation(self):
        record = self._create("EMG-HOLD-1")
        with self.assertRaises(Exception):
            self.service.act(
                DISPATCHER, record["id"], record["version"], "assign",
                {"vehicle_available": True, "vehicle_id": "AMB-15", "hold_minutes": 0},
            )


if __name__ == "__main__":
    unittest.main()
