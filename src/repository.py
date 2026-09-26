"""SQLite 表结构与事务访问。"""
import json
import sqlite3
from datetime import datetime, timezone
from typing import Any, Dict, List, Optional

from .domain import BedShortage, Conflict, NotFound


# 床位承诺状态
COMMITMENT_HELD = "held"
COMMITMENT_ADMITTED = "admitted"
COMMITMENT_RELEASED = "released"

# 退回原因
RELEASE_CANCEL_PREFIX = "任务取消"
RELEASE_TIMEOUT = "超时未到场，承诺自动退回"


def _now() -> str:
    return datetime.now(timezone.utc).isoformat()


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
                CREATE TABLE IF NOT EXISTS hospitals (
                    id INTEGER PRIMARY KEY AUTOINCREMENT,
                    name TEXT NOT NULL UNIQUE,
                    total_beds INTEGER NOT NULL,
                    capabilities TEXT NOT NULL DEFAULT '[]',
                    note TEXT NOT NULL DEFAULT '',
                    active INTEGER NOT NULL DEFAULT 1,
                    created_by TEXT NOT NULL DEFAULT '',
                    updated_by TEXT NOT NULL DEFAULT '',
                    created_at TEXT NOT NULL,
                    updated_at TEXT NOT NULL
                );
                CREATE TABLE IF NOT EXISTS bed_commitments (
                    id INTEGER PRIMARY KEY AUTOINCREMENT,
                    record_id INTEGER NOT NULL REFERENCES records(id) ON DELETE CASCADE,
                    hospital_name TEXT NOT NULL,
                    beds INTEGER NOT NULL DEFAULT 1,
                    status TEXT NOT NULL,
                    expires_at TEXT,
                    released_at TEXT,
                    release_reason TEXT,
                    arrived_at TEXT,
                    held_by TEXT NOT NULL DEFAULT '',
                    created_at TEXT NOT NULL,
                    updated_at TEXT NOT NULL
                );
                CREATE INDEX IF NOT EXISTS idx_records_state ON records(state);
                CREATE INDEX IF NOT EXISTS idx_audit_record ON audit_events(record_id, id);
                CREATE INDEX IF NOT EXISTS idx_hospitals_name ON hospitals(name);
                CREATE INDEX IF NOT EXISTS idx_commitments_hospital ON bed_commitments(hospital_name, status);
                CREATE INDEX IF NOT EXISTS idx_commitments_record ON bed_commitments(record_id, id);
                CREATE INDEX IF NOT EXISTS idx_commitments_expiry ON bed_commitments(status, expires_at);
                CREATE UNIQUE INDEX IF NOT EXISTS idx_commitment_active
                    ON bed_commitments(record_id) WHERE status='held';
                """
            )

    @staticmethod
    def _row(row: sqlite3.Row) -> Dict[str, Any]:
        item = dict(row)
        item["payload"] = json.loads(item["payload"])
        return item

    @staticmethod
    def _commitment_row(row: sqlite3.Row) -> Dict[str, Any]:
        item = dict(row)
        item["beds"] = int(item["beds"])
        return item

    def _expire_locked(self, connection: sqlite3.Connection, now: str) -> List[Dict[str, Any]]:
        """在当前事务内回收所有已到期且尚未到场的占用，并写审计事件。"""
        due = connection.execute(
            "SELECT * FROM bed_commitments WHERE status=? AND expires_at IS NOT NULL AND expires_at<=?",
            (COMMITMENT_HELD, now),
        ).fetchall()
        expired: List[Dict[str, Any]] = []
        for row in due:
            item = self._commitment_row(row)
            connection.execute(
                "UPDATE bed_commitments SET status=?,released_at=?,release_reason=?,updated_at=? WHERE id=?",
                (COMMITMENT_RELEASED, now, RELEASE_TIMEOUT, now, item["id"]),
            )
            connection.execute(
                "INSERT INTO audit_events(record_id,action,actor_id,version,details,created_at) VALUES(?,?,?,?,?,?)",
                (
                    item["record_id"],
                    "bed_released",
                    "system",
                    0,
                    json.dumps(
                        {
                            "summary": RELEASE_TIMEOUT,
                            "commitment_id": item["id"],
                            "hospital": item["hospital_name"],
                            "expires_at": item["expires_at"],
                            "release_reason": RELEASE_TIMEOUT,
                        },
                        ensure_ascii=False,
                        sort_keys=True,
                    ),
                    now,
                ),
            )
            item["status"] = COMMITMENT_RELEASED
            item["released_at"] = now
            item["release_reason"] = RELEASE_TIMEOUT
            expired.append(item)
        return expired

    def expire_commitments(self, now: str = None) -> List[Dict[str, Any]]:
        """回收全部到期占用（后台清扫与服务重启恢复调用）。"""
        now = now or _now()
        with self._connect() as connection:
            connection.execute("BEGIN IMMEDIATE")
            expired = self._expire_locked(connection, now)
            connection.commit()
        return expired

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

    def upsert_hospital(self, hospital: Dict[str, Any], actor_id: str, now: str = None) -> Dict[str, Any]:
        """登记/更新医院床位总账。返回最新台账行。"""
        now = now or _now()
        capabilities = json.dumps(hospital.get("capabilities", []), ensure_ascii=False, sort_keys=True)
        with self._connect() as connection:
            connection.execute("BEGIN IMMEDIATE")
            existing = connection.execute("SELECT * FROM hospitals WHERE name=?", (hospital["name"],)).fetchone()
            if existing is None:
                connection.execute(
                    "INSERT INTO hospitals(name,total_beds,capabilities,note,active,created_by,updated_by,created_at,updated_at)"
                    " VALUES(?,?,?,?,1,?,?,?,?)",
                    (hospital["name"], int(hospital["total_beds"]), capabilities, hospital.get("note", ""), actor_id, actor_id, now, now),
                )
            else:
                connection.execute(
                    "UPDATE hospitals SET total_beds=?,capabilities=?,note=?,updated_by=?,updated_at=? WHERE name=?",
                    (int(hospital["total_beds"]), capabilities, hospital.get("note", existing["note"]), actor_id, now, hospital["name"]),
                )
            row = connection.execute("SELECT * FROM hospitals WHERE name=?", (hospital["name"],)).fetchone()
            connection.commit()
        item = dict(row)
        item["capabilities"] = json.loads(item["capabilities"])
        return item

    def hospital_by_name(self, name: str) -> Optional[Dict[str, Any]]:
        with self._connect() as connection:
            row = connection.execute("SELECT * FROM hospitals WHERE name=? AND active=1", (name,)).fetchone()
        if row is None:
            return None
        item = dict(row)
        item["capabilities"] = json.loads(item["capabilities"])
        return item

    def _bed_totals_locked(self, connection: sqlite3.Connection, hospital_name: str) -> Dict[str, int]:
        hospital = connection.execute("SELECT total_beds FROM hospitals WHERE name=? AND active=1", (hospital_name,)).fetchone()
        totals = connection.execute(
            "SELECT status, COALESCE(SUM(beds),0) AS total FROM bed_commitments WHERE hospital_name=? GROUP BY status",
            (hospital_name,),
        ).fetchall()
        by_status = {str(row["status"]): int(row["total"]) for row in totals}
        return {
            "total_beds": int(hospital["total_beds"]) if hospital is not None else None,
            "held_beds": by_status.get(COMMITMENT_HELD, 0),
            "admitted_beds": by_status.get(COMMITMENT_ADMITTED, 0),
        }

    def hospital_board(self, now: str = None) -> List[Dict[str, Any]]:
        now = now or _now()
        with self._connect() as connection:
            connection.execute("BEGIN IMMEDIATE")
            self._expire_locked(connection, now)
            rows = connection.execute("SELECT * FROM hospitals WHERE active=1 ORDER BY name").fetchall()
            board: List[Dict[str, Any]] = []
            for row in rows:
                totals = self._bed_totals_locked(connection, row["name"])
                held = totals["held_beds"]
                admitted = totals["admitted_beds"]
                total = int(row["total_beds"])
                item = dict(row)
                item["capabilities"] = json.loads(item["capabilities"])
                item["total_beds"] = total
                item["held_beds"] = held
                item["admitted_beds"] = admitted
                item["available_beds"] = max(0, total - held - admitted)
                board.append(item)
            connection.commit()
        return board

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

    def mutate(
        self,
        record_id: int,
        expected_version: int,
        state: str,
        payload: Dict[str, Any],
        actor_id: str,
        action: str,
        details: Dict[str, Any],
        commitment: Optional[Dict[str, Any]] = None,
        now: str = None,
    ) -> Dict[str, Any]:
        """原子地完成状态变更与床位承诺操作。

        commitment:
          派车   {"op": "hold", "hospital_name": ..., "expires_at": ISO}
          到场   {"op": "arrive"}
          交接   {"op": "admit"}
          取消   {"op": "release", "reason": ...}
        """
        now = now or _now()
        with self._connect() as connection:
            connection.execute("BEGIN IMMEDIATE")
            # 先回收本事务之前已到期的占用，保证余量判断基于最新数据
            self._expire_locked(connection, now)
            row = connection.execute("SELECT * FROM records WHERE id=?", (record_id,)).fetchone()
            if row is None:
                connection.rollback()
                raise NotFound("记录不存在")
            current = self._row(row)
            if int(current["version"]) != int(expected_version):
                connection.rollback()
                raise Conflict("版本冲突，请刷新后重试")

            commitment_detail: Optional[Dict[str, Any]] = None
            op = commitment.get("op") if commitment else None
            if op == "hold":
                hospital_name = commitment["hospital_name"]
                totals = self._bed_totals_locked(connection, hospital_name)
                if totals["total_beds"] is None:
                    connection.rollback()
                    raise BedShortage("医院%s未登记床位台账" % hospital_name, {"hospital": hospital_name, "registered": False})
                available = totals["total_beds"] - totals["held_beds"] - totals["admitted_beds"]
                if available <= 0:
                    connection.rollback()
                    raise BedShortage(
                        "%s暂无余床，任务留在待派区（缺少1张，已占用/预留%s张）"
                        % (hospital_name, totals["held_beds"] + totals["admitted_beds"]),
                        {
                            "hospital": hospital_name,
                            "total_beds": totals["total_beds"],
                            "held_beds": totals["held_beds"],
                            "admitted_beds": totals["admitted_beds"],
                            "available_beds": max(0, available),
                            "shortage_beds": max(1 - available, 0),
                        },
                    )
                cursor = connection.execute(
                    "INSERT INTO bed_commitments(record_id,hospital_name,beds,status,expires_at,held_by,created_at,updated_at)"
                    " VALUES(?,? ,1,?,?,?,?,?)",
                    (record_id, hospital_name, COMMITMENT_HELD, commitment["expires_at"], actor_id, now, now),
                )
                c_row = connection.execute("SELECT * FROM bed_commitments WHERE id=?", (int(cursor.lastrowid),)).fetchone()
                commitment_detail = self._commitment_row(c_row)
            elif op in {"release", "arrive", "admit"}:
                c_row = connection.execute(
                    "SELECT * FROM bed_commitments WHERE record_id=? AND status=? ORDER BY id DESC",
                    (record_id, COMMITMENT_HELD),
                ).fetchone()
                if c_row is None and op == "admit":
                    connection.rollback()
                    raise Conflict("没有有效的床位占用，无法交接收治")
                if c_row is not None:
                    item = self._commitment_row(c_row)
                    if op == "release":
                        reason = commitment.get("reason") or "任务取消"
                        connection.execute(
                            "UPDATE bed_commitments SET status=?,released_at=?,release_reason=?,updated_at=? WHERE id=?",
                            (COMMITMENT_RELEASED, now, reason, now, item["id"]),
                        )
                    elif op == "arrive":
                        # 已到场的占用不再受到期时间限制，等待交接或取消
                        connection.execute(
                            "UPDATE bed_commitments SET arrived_at=?,expires_at=NULL,updated_at=? WHERE id=?",
                            (now, now, item["id"]),
                        )
                    else:  # admit
                        connection.execute(
                            "UPDATE bed_commitments SET status=?,arrived_at=COALESCE(arrived_at,?),updated_at=? WHERE id=?",
                            (COMMITMENT_ADMITTED, now, now, item["id"]),
                        )
                    c_row = connection.execute("SELECT * FROM bed_commitments WHERE id=?", (item["id"],)).fetchone()
                    commitment_detail = self._commitment_row(c_row)

            version = int(expected_version) + 1
            if commitment_detail is not None:
                details = dict(details)
                details["commitment"] = {
                    "id": commitment_detail["id"],
                    "status": commitment_detail["status"],
                    "hospital": commitment_detail["hospital_name"],
                    "expires_at": commitment_detail["expires_at"],
                    "released_at": commitment_detail["released_at"],
                    "release_reason": commitment_detail["release_reason"],
                }
            connection.execute(
                "UPDATE records SET state=?,version=?,payload=?,updated_by=?,updated_at=? WHERE id=?",
                (state, version, json.dumps(payload, ensure_ascii=False, sort_keys=True), actor_id, now, record_id),
            )
            connection.execute(
                "INSERT INTO audit_events(record_id,action,actor_id,version,details,created_at) VALUES(?,?,?,?,?,?)",
                (record_id, action, actor_id, version, json.dumps(details, ensure_ascii=False, sort_keys=True), now),
            )
            result = connection.execute("SELECT * FROM records WHERE id=?", (record_id,)).fetchone()
            connection.commit()
        return self._row(result)

    def active_commitment(self, record_id: int, now: str = None) -> Optional[Dict[str, Any]]:
        with self._connect() as connection:
            row = connection.execute(
                "SELECT * FROM bed_commitments WHERE record_id=? AND status=? ORDER BY id DESC",
                (record_id, COMMITMENT_HELD),
            ).fetchone()
        return self._commitment_row(row) if row is not None else None

    def list_commitments(self, status: Optional[str] = None, hospital_name: Optional[str] = None, limit: int = 200, now: str = None) -> List[Dict[str, Any]]:
        limit = max(1, min(int(limit), 1000))
        now = now or _now()
        with self._connect() as connection:
            connection.execute("BEGIN IMMEDIATE")
            self._expire_locked(connection, now)
            sql = (
                "SELECT c.*, r.reference AS record_reference FROM bed_commitments c"
                " LEFT JOIN records r ON r.id=c.record_id WHERE 1=1"
            )
            params: List[Any] = []
            if status:
                sql += " AND c.status=?"
                params.append(status)
            if hospital_name:
                sql += " AND c.hospital_name=?"
                params.append(hospital_name)
            sql += " ORDER BY c.id DESC LIMIT ?"
            params.append(limit)
            rows = connection.execute(sql, params).fetchall()
            connection.commit()
        return [self._commitment_row(row) for row in rows]

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
