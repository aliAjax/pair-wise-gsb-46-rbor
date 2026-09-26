"""业务用例编排、权限检查与审计。"""
from datetime import datetime, timedelta, timezone
from typing import Any, Dict, List, Optional

from .audit import AuditRecorder
from .domain import Actor, BedShortage, PermissionDenied, text
from .repository import RELEASE_CANCEL_PREFIX, Repository
from .rules import DomainRules
from .sweeper import CommitmentSweeper


def _utcnow() -> datetime:
    return datetime.now(timezone.utc)


def _iso(dt: datetime) -> str:
    return dt.isoformat()


class Service:
    def __init__(self, repository: Repository, rules: DomainRules, audit: AuditRecorder = None, sweep_interval: float = 30.0) -> None:
        self.repository = repository
        self.rules = rules
        self.audit = audit or AuditRecorder(repository)
        self.sweeper = CommitmentSweeper(repository.expire_commitments, interval_seconds=sweep_interval)

    def start_sweeper(self) -> None:
        self.sweeper.start()

    def close(self) -> None:
        self.sweeper.stop()

    @staticmethod
    def _actor(actor: Actor) -> Actor:
        if actor is None or not actor.user_id.strip() or not actor.role.strip():
            raise PermissionDenied("缺少调用身份")
        return actor

    def _ensure_known_role(self, actor: Actor) -> None:
        if not self.rules.known_role(actor.role):
            raise PermissionDenied("角色无权访问该服务")

    def create(self, actor: Actor, reference: str, payload: Dict[str, Any]) -> Dict[str, Any]:
        actor = self._actor(actor)
        self._ensure_known_role(actor)
        if not self.rules.role_can_create(actor.role):
            raise PermissionDenied("角色无权创建记录")
        reference = text({"reference": reference}, "reference")
        prepared = self.rules.prepare_create(payload or {})
        self.rules.check_create_conflicts(prepared, self.repository.list_records(limit=500))
        # 未登记的医院按申报床位数自动建立台账，保持创建接口向后兼容
        hospital_name = prepared["destination"]
        if self.repository.hospital_by_name(hospital_name) is None:
            self.repository.upsert_hospital(
                {"name": hospital_name, "total_beds": int(prepared["hospital_beds"]), "capabilities": [], "note": "任务创建时自动登记"},
                actor_id=actor.user_id,
            )
        return self.repository.create(reference, self.rules.INITIAL_STATE, prepared, actor.user_id)

    def register_hospital(self, actor: Actor, payload: Dict[str, Any]) -> Dict[str, Any]:
        actor = self._actor(actor)
        self._ensure_known_role(actor)
        if actor.role != "admin" and actor.role not in {"dispatcher", "hospital_coordinator"}:
            raise PermissionDenied("角色无权登记医院")
        hospital = self.rules.validate_hospital(payload or {})
        return self.repository.upsert_hospital(hospital, actor_id=actor.user_id)

    def hospital_board(self, actor: Actor) -> List[Dict[str, Any]]:
        actor = self._actor(actor)
        self._ensure_known_role(actor)
        return self.repository.hospital_board()

    def list_records(self, actor: Actor, state: Optional[str] = None, limit: int = 100) -> List[Dict[str, Any]]:
        actor = self._actor(actor)
        self._ensure_known_role(actor)
        return self.repository.list_records(state=state, limit=limit)

    def get_record(self, actor: Actor, record_id: int) -> Dict[str, Any]:
        actor = self._actor(actor)
        self._ensure_known_role(actor)
        return self.repository.get(record_id)

    def pending(self, actor: Actor, limit: int = 100) -> Dict[str, Any]:
        """待派区：尚未派车的任务，并标注各医院余床与缺少床位数。"""
        actor = self._actor(actor)
        self._ensure_known_role(actor)
        records = self.repository.list_records(state=self.rules.INITIAL_STATE, limit=limit)
        board = {item["name"]: item for item in self.repository.hospital_board()}
        items: List[Dict[str, Any]] = []
        for record in records:
            hospital_name = record["payload"].get("destination", "")
            hospital = board.get(hospital_name)
            if hospital is None:
                shortage = 1
                bed_info = {"hospital": hospital_name, "registered": False, "total_beds": None, "available_beds": 0, "shortage_beds": shortage}
            else:
                available = int(hospital["available_beds"])
                bed_info = {
                    "hospital": hospital_name,
                    "registered": True,
                    "total_beds": int(hospital["total_beds"]),
                    "held_beds": int(hospital["held_beds"]),
                    "admitted_beds": int(hospital["admitted_beds"]),
                    "available_beds": available,
                    "shortage_beds": max(1 - available, 0),
                }
            items.append({"record": record, "beds": bed_info, "dispatchable": bed_info["shortage_beds"] == 0})
        return {"items": items, "count": len(items)}

    def act(self, actor: Actor, record_id: int, expected_version: int, action: str, data: Dict[str, Any]) -> Dict[str, Any]:
        actor = self._actor(actor)
        self._ensure_known_role(actor)
        action = text({"action": action}, "action")
        if not self.rules.role_can_action(actor.role, action):
            raise PermissionDenied("角色无权执行该操作")
        record = self.repository.get(record_id)
        self.rules.require_transition(record, action)
        new_state, new_payload, summary = self.rules.apply_action(record, action, data or {})
        data = data or {}
        commitment: Optional[Dict[str, Any]] = None
        if action == "assign":
            hold_minutes = self.rules.validate_hold_minutes(data)
            expires_at = _iso(_utcnow() + timedelta(minutes=hold_minutes))
            commitment = {"op": "hold", "hospital_name": record["payload"]["destination"], "expires_at": expires_at}
        elif action == "arrive":
            commitment = {"op": "arrive"}
        elif action == "transport":
            commitment = None
        elif action == "handover":
            commitment = {"op": "admit"}
        elif action == "cancel":
            commitment = {"op": "release", "reason": RELEASE_CANCEL_PREFIX + "：" + text(data, "cancel_reason")}
        try:
            return self.repository.mutate(
                record_id=record_id,
                expected_version=int(expected_version),
                state=new_state,
                payload=new_payload,
                actor_id=actor.user_id,
                action=action,
                details={"summary": summary, "input": data, "from": record["state"], "to": new_state},
                commitment=commitment,
            )
        except BedShortage as exc:
            # 床位不足：任务不发生任何状态变化，继续留在待派区
            exc.details["summary"] = str(exc)
            exc.details["record_id"] = record_id
            exc.details["state"] = record["state"]
            raise

    def commitments(self, actor: Actor, status: Optional[str] = None, hospital_name: Optional[str] = None, limit: int = 200) -> Dict[str, Any]:
        actor = self._actor(actor)
        self._ensure_known_role(actor)
        items = self.repository.list_commitments(status=status, hospital_name=hospital_name, limit=limit)
        return {"items": items, "count": len(items)}

    def sweep_expired(self, actor: Actor) -> Dict[str, Any]:
        actor = self._actor(actor)
        self._ensure_known_role(actor)
        expired = self.repository.expire_commitments()
        return {"expired": len(expired), "items": expired}

    def timeline(self, actor: Actor, record_id: int) -> List[Dict[str, Any]]:
        actor = self._actor(actor)
        self._ensure_known_role(actor)
        return self.audit.timeline(record_id)

    def stats(self, actor: Actor) -> Dict[str, int]:
        actor = self._actor(actor)
        self._ensure_known_role(actor)
        return self.repository.stats()
