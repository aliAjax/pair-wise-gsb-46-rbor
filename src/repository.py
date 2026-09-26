"""SQLite 表结构与事务访问。"""
import json
import sqlite3
from datetime import datetime, timedelta, timezone
from typing import Any, Dict, List, Optional

from .domain import Conflict, NotFound


def _now() -> str:
    return datetime.now(timezone.utc).isoformat()


# 床位承诺状态：held 占用中 / released 已退回 / admitted 已收治
RESERVATION_HELD = "held"
RESERVATION_RELEASED = "released"
RESERVATION_ADMITTED = "admitted"
RELEASE_REASON_EXPIRED = "超时未到场，占用自动退回"
RELEASE_REASON_CANCELLED = "任务取消，占用退回"


class Repository:
    def __init__(self, db_path: str) -> None:
        self.db_path = db_path
        self._init_schema()

    def _connect(self) -> sqlite3.Connection:
        connection = sqlite3.connect(self.db_path, timeout=15)
        connection.row_factory = sqlite3.Row
        connection.execute("PRAGMA foreign_keys = ON")
        connection.execute("PRAGMA busy_timeout = 15000")
        return connection

    def _init_schema(self) -> None:
        with self._connect() as connection:
            connection.executescript(
                """
                CREATE TABLE IF NOT EXISTS records (
                    id INTEGER PRIMARY KEY AUTOINCREMENT,
                    reference TEXT NOT NULL UNIQUE,
                    state TEXT NOT NULL,
                    version INTEGER NOT NULL DEFAULT 1,
                    payload TEXT NOT NULL,
                    created_by TEXT NOT NULL,
                    updated_by TEXT NOT NULL,
                    created_at TEXT NOT NULL,
                    updated_at TEXT NOT NULL
                );
                CREATE TABLE IF NOT EXISTS audit_events (
                    id INTEGER PRIMARY KEY AUTOINCREMENT,
                    record_id INTEGER NOT NULL REFERENCES records(id) ON DELETE CASCADE,
                    action TEXT NOT NULL,
                    actor_id TEXT NOT NULL,
                    version INTEGER NOT NULL,
                    details TEXT NOT NULL,
                    created_at TEXT NOT NULL
                );
                CREATE TABLE IF NOT EXISTS bed_reservations (
                    id INTEGER PRIMARY KEY AUTOINCREMENT,
                    record_id INTEGER NOT NULL REFERENCES records(id) ON DELETE CASCADE,
                    hospital TEXT NOT NULL,
                    beds INTEGER NOT NULL DEFAULT 1,
                    status TEXT NOT NULL,
                    expires_at TEXT,
                    released_reason TEXT,
                    created_at TEXT NOT NULL,
                    updated_at TEXT NOT NULL
                );
                CREATE INDEX IF NOT EXISTS idx_records_state ON records(state);
                CREATE INDEX IF NOT EXISTS idx_audit_record ON audit_events(record_id, id);
                CREATE INDEX IF NOT EXISTS idx_reservations_hospital ON bed_reservations(hospital, status);
                CREATE INDEX IF NOT EXISTS idx_reservations_record ON bed_reservations(record_id);
                """
            )

    @staticmethod
    def _row(row: sqlite3.Row) -> Dict[str, Any]:
        item = dict(row)
        item["payload"] = json.loads(item["payload"])
        return item

    def create(self, reference: str, state: str, payload: Dict[str, Any], actor_id: str) -> Dict[str, Any]:
        now = _now()
        try:
            with self._connect() as connection:
                cursor = connection.execute(
                    "INSERT INTO records(reference,state,version,payload,created_by,updated_by,created_at,updated_at) VALUES(?,?,?,?,?,?,?,?)",
                    (reference, state, 1, json.dumps(payload, ensure_ascii=False, sort_keys=True), actor_id, actor_id, now, now),
                )
                record_id = int(cursor.lastrowid)
                connection.execute(
                    "INSERT INTO audit_events(record_id,action,actor_id,version,details,created_at) VALUES(?,?,?,?,?,?)",
                    (record_id, "created", actor_id, 1, json.dumps({"state": state}, ensure_ascii=False, sort_keys=True), now),
                )
                row = connection.execute("SELECT * FROM records WHERE id=?", (record_id,)).fetchone()
        except sqlite3.IntegrityError as exc:
            raise Conflict("reference已存在") from exc
        return self._row(row)

    def get(self, record_id: int) -> Dict[str, Any]:
        with self._connect() as connection:
            row = connection.execute("SELECT * FROM records WHERE id=?", (record_id,)).fetchone()
        if row is None:
            raise NotFound("记录不存在")
        return self._row(row)

    def list_records(self, state: Optional[str] = None, limit: int = 100) -> List[Dict[str, Any]]:
        limit = max(1, min(int(limit), 500))
        with self._connect() as connection:
            if state:
                rows = connection.execute("SELECT * FROM records WHERE state=? ORDER BY id DESC LIMIT ?", (state, limit)).fetchall()
            else:
                rows = connection.execute("SELECT * FROM records ORDER BY id DESC LIMIT ?", (limit,)).fetchall()
        return [self._row(row) for row in rows]

    def mutate(self, record_id: int, expected_version: int, state: str, payload: Dict[str, Any], actor_id: str, action: str, details: Dict[str, Any], reservation_update: Optional[Dict[str, Any]] = None) -> Dict[str, Any]:
        now = _now()
        with self._connect() as connection:
            connection.execute("BEGIN IMMEDIATE")
            self._sweep_expired(connection, now)
            row = connection.execute("SELECT version FROM records WHERE id=?", (record_id,)).fetchone()
            if row is None:
                connection.rollback()
                raise NotFound("记录不存在")
            if int(row["version"]) != int(expected_version):
                connection.rollback()
                raise Conflict("版本冲突，请刷新后重试")
            version = int(expected_version) + 1
            connection.execute(
                "UPDATE records SET state=?,version=?,payload=?,updated_by=?,updated_at=? WHERE id=?",
                (state, version, json.dumps(payload, ensure_ascii=False, sort_keys=True), actor_id, now, record_id),
            )
            if reservation_update:
                connection.execute(
                    "UPDATE bed_reservations SET status=?, released_reason=?, updated_at=? WHERE record_id=? AND status=?",
                    (reservation_update["to_status"], reservation_update.get("reason"), now, record_id, RESERVATION_HELD),
                )
            connection.execute(
                "INSERT INTO audit_events(record_id,action,actor_id,version,details,created_at) VALUES(?,?,?,?,?,?)",
                (record_id, action, actor_id, version, json.dumps(details, ensure_ascii=False, sort_keys=True), now),
            )
            result = connection.execute("SELECT * FROM records WHERE id=?", (record_id,)).fetchone()
            connection.commit()
        return self._row(result)

    def _sweep_expired(self, connection: sqlite3.Connection, now: str) -> List[sqlite3.Row]:
        """把已到期仍未交接的占用退回，并在对应任务时间线上留痕。"""
        rows = connection.execute(
            "SELECT id, record_id, hospital, beds FROM bed_reservations WHERE status=? AND expires_at IS NOT NULL AND expires_at<=?",
            (RESERVATION_HELD, now),
        ).fetchall()
        for row in rows:
            connection.execute(
                "UPDATE bed_reservations SET status=?, released_reason=?, updated_at=? WHERE id=?",
                (RESERVATION_RELEASED, RELEASE_REASON_EXPIRED, now, row["id"]),
            )
            version_row = connection.execute("SELECT version FROM records WHERE id=?", (row["record_id"],)).fetchone()
            connection.execute(
                "INSERT INTO audit_events(record_id,action,actor_id,version,details,created_at) VALUES(?,?,?,?,?,?)",
                (
                    row["record_id"],
                    "bed_hold_expired",
                    "system",
                    int(version_row["version"]) if version_row else 0,
                    json.dumps({"summary": RELEASE_REASON_EXPIRED, "reservation_id": row["id"], "hospital": row["hospital"], "beds": row["beds"]}, ensure_ascii=False, sort_keys=True),
                    now,
                ),
            )
        return rows

    def assign_with_bed_hold(self, record_id: int, expected_version: int, actor_id: str, hospital: str, beds_needed: int, hold_minutes: int, assign_changes: Dict[str, Any], audit_input: Dict[str, Any]) -> Dict[str, Any]:
        """在同一事务内核对床位并派车：有余床则占用并派车，否则任务进入待派区。"""
        now = _now()
        with self._connect() as connection:
            connection.execute("BEGIN IMMEDIATE")
            self._sweep_expired(connection, now)
            row = connection.execute("SELECT * FROM records WHERE id=?", (record_id,)).fetchone()
            if row is None:
                connection.rollback()
                raise NotFound("记录不存在")
            if int(row["version"]) != int(expected_version):
                connection.rollback()
                raise Conflict("版本冲突，请刷新后重试")
            payload = json.loads(row["payload"])
            from_state = row["state"]
            held_row = connection.execute(
                "SELECT COALESCE(SUM(beds),0) AS held FROM bed_reservations WHERE hospital=? AND status=?",
                (hospital, RESERVATION_HELD),
            ).fetchone()
            total_beds = int(payload.get("hospital_beds", 0))
            available = total_beds - int(held_row["held"])
            version = int(expected_version) + 1
            if available >= beds_needed:
                expires_at = (datetime.now(timezone.utc) + timedelta(minutes=hold_minutes)).isoformat()
                cursor = connection.execute(
                    "INSERT INTO bed_reservations(record_id,hospital,beds,status,expires_at,released_reason,created_at,updated_at) VALUES(?,?,?,?,?,?,?,?)",
                    (record_id, hospital, beds_needed, RESERVATION_HELD, expires_at, None, now, now),
                )
                reservation_id = int(cursor.lastrowid)
                payload.update(assign_changes)
                payload["bed_reservation_id"] = reservation_id
                payload["bed_hold_expires_at"] = expires_at
                payload["awaiting_beds"] = False
                payload["beds_short"] = 0
                new_state = "assigned"
                details = {"summary": "已完成派车并占用%s张床" % beds_needed, "input": audit_input, "from": from_state, "to": new_state, "reservation_id": reservation_id, "expires_at": expires_at}
            else:
                beds_short = beds_needed - max(available, 0)
                payload["awaiting_beds"] = True
                payload["beds_short"] = beds_short
                new_state = "awaiting_beds"
                details = {"summary": "床位不足，任务进入待派区，缺%s张床" % beds_short, "input": audit_input, "from": from_state, "to": new_state, "hospital": hospital, "beds_short": beds_short}
            connection.execute(
                "UPDATE records SET state=?,version=?,payload=?,updated_by=?,updated_at=? WHERE id=?",
                (new_state, version, json.dumps(payload, ensure_ascii=False, sort_keys=True), actor_id, now, record_id),
            )
            connection.execute(
                "INSERT INTO audit_events(record_id,action,actor_id,version,details,created_at) VALUES(?,?,?,?,?,?)",
                (record_id, "assign", actor_id, version, json.dumps(details, ensure_ascii=False, sort_keys=True), now),
            )
            result = connection.execute("SELECT * FROM records WHERE id=?", (record_id,)).fetchone()
            connection.commit()
        return self._row(result)

    def list_reservations(self, status: Optional[str] = None, limit: int = 200) -> List[Dict[str, Any]]:
        limit = max(1, min(int(limit), 500))
        now = _now()
        with self._connect() as connection:
            connection.execute("BEGIN IMMEDIATE")
            self._sweep_expired(connection, now)
            if status:
                rows = connection.execute(
                    "SELECT r.*, rec.reference AS reference FROM bed_reservations r JOIN records rec ON rec.id=r.record_id WHERE r.status=? ORDER BY r.id DESC LIMIT ?",
                    (status, limit),
                ).fetchall()
            else:
                rows = connection.execute(
                    "SELECT r.*, rec.reference AS reference FROM bed_reservations r JOIN records rec ON rec.id=r.record_id ORDER BY r.id DESC LIMIT ?",
                    (limit,),
                ).fetchall()
            connection.commit()
        return [dict(row) for row in rows]

    def add_audit(self, record_id: int, actor_id: str, action: str, details: Dict[str, Any]) -> None:
        with self._connect() as connection:
            row = connection.execute("SELECT version FROM records WHERE id=?", (record_id,)).fetchone()
            if row is None:
                raise NotFound("记录不存在")
            connection.execute(
                "INSERT INTO audit_events(record_id,action,actor_id,version,details,created_at) VALUES(?,?,?,?,?,?)",
                (record_id, action, actor_id, int(row["version"]), json.dumps(details, ensure_ascii=False, sort_keys=True), _now()),
            )

    def audit_timeline(self, record_id: int) -> List[Dict[str, Any]]:
        self.get(record_id)
        with self._connect() as connection:
            rows = connection.execute("SELECT * FROM audit_events WHERE record_id=? ORDER BY id", (record_id,)).fetchall()
        result = []
        for row in rows:
            item = dict(row)
            item["details"] = json.loads(item["details"])
            result.append(item)
        return result

    def stats(self) -> Dict[str, int]:
        with self._connect() as connection:
            rows = connection.execute("SELECT state, COUNT(*) AS total FROM records GROUP BY state").fetchall()
        return {str(row["state"]): int(row["total"]) for row in rows}

    def health(self) -> bool:
        try:
            with self._connect() as connection:
                connection.execute("SELECT 1").fetchone()
            return True
        except sqlite3.Error:
            return False
