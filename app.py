#!/usr/bin/env python3
"""Pharmacovigilance case intake service using only the Python standard library.

上报对账要点：
- 每次按案例修订生成不可变报文（report_messages），重试沿用同一幂等键；
- 回执只接受与当前报文版本一致的受理或退回，退回必须带原因和补件期限；
- 案例修订后未决上报立即失效（superseded）并进入人工复核；
- 区域负责人只处理本区域，全局管理员可重放失败任务；
- 服务重启后对未决记录继续对账；页面查看当前状态和阻塞原因。

状态取值见 pv_states，判定规则见 pv_rules，页面操作见 pv_actions，
监管网关适配见 pv_gateway，四者分开维护。
"""
from __future__ import annotations

import argparse
import json
import os
import sqlite3
from contextlib import contextmanager
from datetime import date, datetime, timedelta, timezone
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from typing import Any
from urllib.parse import parse_qs, urlparse

import pv_actions
import pv_rules
import pv_states
from pv_gateway import GatewayFailure, GatewayTimeout, RegulatorGateway

PORT = 8201
ROLES = {"reporter", "regional_lead", "medical_reviewer", "global_admin"}
SYSTEM_ACTOR = "system-reconcile"


class ApiError(Exception):
    def __init__(self, status: int, code: str, message: str):
        super().__init__(message)
        self.status = status
        self.code = code
        self.message = message


def utcnow() -> datetime:
    return datetime.now(timezone.utc)


def iso(value: datetime | None = None) -> str:
    current = value or utcnow()
    return current.replace(microsecond=0).isoformat().replace("+00:00", "Z")


def parse_time(value: str | None, default: datetime | None = None) -> datetime:
    if not value:
        if default is None:
            raise ApiError(400, "missing_time", "必须提供 ISO 8601 时间")
        return default
    try:
        parsed = datetime.fromisoformat(value.replace("Z", "+00:00"))
    except ValueError as exc:
        raise ApiError(400, "invalid_time", f"时间格式错误: {value}") from exc
    if parsed.tzinfo is None:
        parsed = parsed.replace(tzinfo=timezone.utc)
    return parsed.astimezone(timezone.utc)


def report_deadline(received_at: datetime, serious: bool, fatal: bool) -> datetime:
    if serious:
        return received_at + timedelta(days=7 if fatal else 15)
    return received_at + timedelta(days=90)


class Repository:
    def __init__(self, db_path: str | Path):
        self.db_path = str(db_path)
        self.conn = sqlite3.connect(self.db_path, check_same_thread=False, isolation_level=None)
        self.conn.row_factory = sqlite3.Row
        self.conn.execute("PRAGMA foreign_keys=ON")
        self.conn.execute("PRAGMA journal_mode=WAL")
        self.init_schema()

    @contextmanager
    def tx(self):
        self.conn.execute("BEGIN IMMEDIATE")
        try:
            yield self.conn
            self.conn.execute("COMMIT")
        except Exception:
            self.conn.execute("ROLLBACK")
            raise

    def init_schema(self) -> None:
        self.conn.executescript(
            """
            CREATE TABLE IF NOT EXISTS cases (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                case_no TEXT NOT NULL UNIQUE,
                patient_ref TEXT NOT NULL,
                region TEXT NOT NULL,
                product TEXT NOT NULL,
                event_term TEXT NOT NULL,
                onset_at TEXT,
                received_at TEXT NOT NULL,
                serious INTEGER NOT NULL DEFAULT 0,
                fatal INTEGER NOT NULL DEFAULT 0,
                causality TEXT,
                report_due_at TEXT NOT NULL,
                status TEXT NOT NULL DEFAULT 'open',
                revision INTEGER NOT NULL DEFAULT 1,
                merged_into INTEGER REFERENCES cases(id),
                created_by TEXT NOT NULL,
                created_at TEXT NOT NULL,
                updated_at TEXT NOT NULL
            );
            CREATE TABLE IF NOT EXISTS intakes (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                case_id INTEGER REFERENCES cases(id),
                source TEXT NOT NULL,
                dedupe_key TEXT NOT NULL UNIQUE,
                payload_json TEXT NOT NULL,
                received_at TEXT NOT NULL,
                created_by TEXT NOT NULL,
                created_at TEXT NOT NULL
            );
            CREATE TABLE IF NOT EXISTS followups (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                case_id INTEGER NOT NULL REFERENCES cases(id),
                content TEXT NOT NULL,
                source TEXT NOT NULL,
                received_at TEXT NOT NULL,
                revision INTEGER NOT NULL,
                created_by TEXT NOT NULL,
                created_at TEXT NOT NULL,
                UNIQUE(case_id, revision)
            );
            CREATE TABLE IF NOT EXISTS reports (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                case_id INTEGER NOT NULL REFERENCES cases(id),
                country TEXT NOT NULL,
                due_at TEXT NOT NULL,
                status TEXT NOT NULL DEFAULT 'pending',
                submitted_at TEXT,
                submitted_by TEXT,
                late INTEGER NOT NULL DEFAULT 0,
                UNIQUE(case_id, country)
            );
            CREATE TABLE IF NOT EXISTS medical_reviews (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                case_id INTEGER NOT NULL REFERENCES cases(id),
                case_revision INTEGER NOT NULL,
                serious INTEGER NOT NULL,
                fatal INTEGER NOT NULL,
                causality TEXT NOT NULL,
                rationale TEXT NOT NULL,
                reviewer TEXT NOT NULL,
                created_at TEXT NOT NULL,
                UNIQUE(case_id, case_revision)
            );
            CREATE TABLE IF NOT EXISTS audit_log (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                case_id INTEGER,
                actor TEXT NOT NULL,
                role TEXT NOT NULL,
                action TEXT NOT NULL,
                detail_json TEXT NOT NULL,
                created_at TEXT NOT NULL
            );
            CREATE TABLE IF NOT EXISTS report_messages (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                report_id INTEGER NOT NULL REFERENCES reports(id),
                case_id INTEGER NOT NULL REFERENCES cases(id),
                case_revision INTEGER NOT NULL,
                country TEXT NOT NULL,
                due_at TEXT NOT NULL,
                payload_json TEXT NOT NULL,
                payload_hash TEXT NOT NULL,
                idempotency_key TEXT NOT NULL UNIQUE,
                status TEXT NOT NULL DEFAULT 'pending',
                return_reason TEXT,
                supplement_due_at TEXT,
                last_error TEXT,
                sim_mode TEXT NOT NULL DEFAULT 'ok',
                regulator_submission_id INTEGER,
                created_by TEXT NOT NULL,
                created_at TEXT NOT NULL,
                updated_at TEXT NOT NULL,
                UNIQUE(report_id, case_revision)
            );
            CREATE TABLE IF NOT EXISTS report_attempts (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                message_id INTEGER NOT NULL REFERENCES report_messages(id),
                result TEXT NOT NULL,
                mode TEXT NOT NULL,
                detail TEXT,
                actor TEXT NOT NULL,
                created_at TEXT NOT NULL
            );
            CREATE TABLE IF NOT EXISTS report_receipts (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                message_id INTEGER REFERENCES report_messages(id),
                idempotency_key TEXT NOT NULL,
                decision TEXT,
                return_reason TEXT,
                supplement_due_at TEXT,
                applied INTEGER NOT NULL DEFAULT 0,
                reject_reason TEXT,
                delivered_by TEXT NOT NULL,
                created_at TEXT NOT NULL
            );
            CREATE TABLE IF NOT EXISTS regulator_submissions (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                idempotency_key TEXT NOT NULL UNIQUE,
                payload_json TEXT NOT NULL,
                first_sent_at TEXT NOT NULL,
                accepted INTEGER NOT NULL DEFAULT 0,
                decision_at TEXT,
                return_reason TEXT,
                supplement_due_at TEXT
            );
            """
        )

    @staticmethod
    def audit(conn: sqlite3.Connection, case_id: int | None, actor: str, role: str, action: str, detail: dict[str, Any]) -> None:
        conn.execute(
            "INSERT INTO audit_log(case_id,actor,role,action,detail_json,created_at) VALUES(?,?,?,?,?,?)",
            (case_id, actor, role, action, json.dumps(detail, ensure_ascii=False, sort_keys=True), iso()),
        )

    @staticmethod
    def row(row: sqlite3.Row | None) -> dict[str, Any] | None:
        return dict(row) if row is not None else None


class PharmacovigilanceService:
    def __init__(self, db_path: str | Path):
        self.repo = Repository(db_path)
        self.gateway = RegulatorGateway()
        # 服务重启：未决（在途）记录继续对账
        self.recover_pending(SYSTEM_ACTOR, "global_admin")

    # ---------- 身份与权限 ----------

    @staticmethod
    def identity(headers: Any) -> tuple[str, str, str]:
        actor = headers.get("X-User-Id", "").strip()
        role = headers.get("X-Role", "").strip()
        region = headers.get("X-Region", "").strip()
        if not actor or role not in ROLES:
            raise ApiError(401, "unauthorized", "需要 X-User-Id 和有效的 X-Role")
        if role in {"reporter", "regional_lead"} and not region:
            raise ApiError(401, "region_required", "该角色必须提供 X-Region")
        return actor, role, region

    @staticmethod
    def can_access(case: dict[str, Any], role: str, region: str) -> bool:
        return role in {"medical_reviewer", "global_admin"} or case["region"] == region

    def _case(self, conn: sqlite3.Connection, case_id: int) -> sqlite3.Row:
        row = conn.execute("SELECT * FROM cases WHERE id=?", (case_id,)).fetchone()
        if not row:
            raise ApiError(404, "case_not_found", "案例不存在")
        return row

    def _report_with_case(self, conn: sqlite3.Connection, report_id: int) -> sqlite3.Row:
        row = conn.execute(
            "SELECT r.*, c.region AS case_region, c.revision AS case_revision, c.case_no AS case_no "
            "FROM reports r JOIN cases c ON c.id=r.case_id WHERE r.id=?",
            (report_id,),
        ).fetchone()
        if not row:
            raise ApiError(404, "report_not_found", "报告不存在")
        return row

    def _require_report_scope(self, conn: sqlite3.Row, role: str, region: str) -> None:
        if role not in {"regional_lead", "global_admin"}:
            raise ApiError(403, "report_forbidden", "只有区域负责人或全局管理员可以处理监管报告")
        if role == "regional_lead" and conn["case_region"] != region:
            raise ApiError(403, "region_forbidden", "区域负责人只能处理本区域报告")

    @staticmethod
    def _latest_message(conn: sqlite3.Connection, report_id: int) -> sqlite3.Row | None:
        return conn.execute(
            "SELECT * FROM report_messages WHERE report_id=? ORDER BY id DESC LIMIT 1", (report_id,)
        ).fetchone()

    # ---------- 案例 ----------

    def create_case(self, actor: str, role: str, region: str, body: dict[str, Any]) -> dict[str, Any]:
        required = ("patient_ref", "region", "product", "event_term", "source", "dedupe_key")
        missing = [key for key in required if not str(body.get(key, "")).strip()]
        if missing:
            raise ApiError(400, "missing_fields", f"缺少字段: {', '.join(missing)}")
        if role == "reporter" and body["region"] != region:
            raise ApiError(403, "region_forbidden", "只能录入本区域案例")
        if role == "medical_reviewer" and body["region"] not in {"", region}:
            raise ApiError(403, "reviewer_region_forbidden", "医学审核员不能代表区域录入案例")
        received = parse_time(body.get("received_at"), utcnow())
        serious = bool(body.get("serious", False))
        fatal = bool(body.get("fatal", False))
        due = report_deadline(received, serious, fatal)
        now = iso()
        with self.repo.tx() as conn:
            duplicate = conn.execute("SELECT * FROM intakes WHERE dedupe_key=?", (body["dedupe_key"],)).fetchone()
            if duplicate:
                case = self._case(conn, duplicate["case_id"])
                Repository.audit(conn, case["id"], actor, role, "intake_deduplicated", {"dedupe_key": body["dedupe_key"], "source": body["source"]})
                return {"deduplicated": True, "case": dict(case), "intake_id": duplicate["id"]}
            count = conn.execute("SELECT COUNT(*) FROM cases").fetchone()[0] + 1
            case_no = body.get("case_no") or f"PV-{received.year}-{count:06d}"
            try:
                cursor = conn.execute(
                    """INSERT INTO cases(case_no,patient_ref,region,product,event_term,onset_at,received_at,
                       serious,fatal,causality,report_due_at,status,revision,created_by,created_at,updated_at)
                       VALUES(?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)""",
                    (case_no, body["patient_ref"], body["region"], body["product"], body["event_term"],
                     body.get("onset_at"), iso(received), int(serious), int(fatal), body.get("causality"),
                     iso(due), "open", 1, actor, now, now),
                )
            except sqlite3.IntegrityError as exc:
                raise ApiError(409, "case_number_conflict", "案例编号已存在") from exc
            case_id = cursor.lastrowid
            conn.execute(
                "INSERT INTO intakes(case_id,source,dedupe_key,payload_json,received_at,created_by,created_at) VALUES(?,?,?,?,?,?,?)",
                (case_id, body["source"], body["dedupe_key"], json.dumps(body, ensure_ascii=False, sort_keys=True), iso(received), actor, now),
            )
            Repository.audit(conn, case_id, actor, role, "case_created", {"case_no": case_no, "source": body["source"]})
            case = self._case(conn, case_id)
            return {"deduplicated": False, "case": dict(case)}

    def _invalidate_open_messages(
        self, conn: sqlite3.Connection, case_id: int, actor: str, role: str, trigger: str
    ) -> int:
        """案例变化（随访/医学审核/合并）后，未决上报立即失效进入人工复核。"""
        rows = conn.execute(
            "SELECT * FROM report_messages WHERE case_id=? AND status IN (?,?,?)",
            (case_id, pv_states.MESSAGE_PENDING, pv_states.MESSAGE_IN_FLIGHT, pv_states.MESSAGE_FAILED),
        ).fetchall()
        for msg in rows:
            pv_states.ensure_message_transition(msg["status"], pv_states.MESSAGE_SUPERSEDED)
            conn.execute(
                "UPDATE report_messages SET status=?,last_error=?,updated_at=? WHERE id=?",
                (pv_states.MESSAGE_SUPERSEDED,
                 f"案例已修订(rev {msg['case_revision']} -> 之后版本)，原报文失效", iso(), msg["id"]),
            )
            self._persist_report_status(conn, msg["report_id"])
            Repository.audit(conn, case_id, actor, role, "message_superseded",
                             {"message_id": msg["id"], "report_id": msg["report_id"],
                              "case_revision": msg["case_revision"], "trigger": trigger})
        return len(rows)

    def get_case(self, case_id: int, role: str, region: str) -> dict[str, Any]:
        case = self._case(self.repo.conn, case_id)
        if not self.can_access(case, role, region):
            raise ApiError(403, "case_forbidden", "无权查看该区域案例")
        conn = self.repo.conn
        reports = [self._report_view(conn, r, role, region)
                   for r in conn.execute("SELECT * FROM reports WHERE case_id=? ORDER BY country", (case_id,))]
        messages = [dict(r) for r in conn.execute(
            """SELECT id,report_id,case_revision,country,idempotency_key,status,payload_hash,
                      return_reason,supplement_due_at,last_error,created_at,updated_at
               FROM report_messages WHERE case_id=? ORDER BY id""", (case_id,))]
        return {
            "case": dict(case),
            "intakes": [dict(r) for r in conn.execute("SELECT id,source,dedupe_key,received_at,created_by,created_at FROM intakes WHERE case_id=? ORDER BY id", (case_id,))],
            "followups": [dict(r) for r in conn.execute("SELECT * FROM followups WHERE case_id=? ORDER BY revision", (case_id,))],
            "reports": reports,
            "messages": messages,
            "reviews": [dict(r) for r in conn.execute("SELECT * FROM medical_reviews WHERE case_id=? ORDER BY id", (case_id,))],
            "audit": [dict(r) for r in conn.execute("SELECT actor,role,action,detail_json,created_at FROM audit_log WHERE case_id=? ORDER BY id", (case_id,))] if role in {"medical_reviewer", "global_admin"} else [],
        }

    def list_cases(self, role: str, region: str, query: dict[str, list[str]]) -> list[dict[str, Any]]:
        sql = "SELECT * FROM cases WHERE status!='merged'"
        args: list[ Any] = []
        if role not in {"medical_reviewer", "global_admin"}:
            sql += " AND region=?"
            args.append(region)
        if query.get("status"):
            sql += " AND status=?"
            args.append(query["status"][0])
        sql += " ORDER BY received_at DESC,id DESC"
        return [dict(r) for r in self.repo.conn.execute(sql, args)]

    def add_followup(self, case_id: int, actor: str, role: str, region: str, body: dict[str, Any]) -> dict[str, Any]:
        content = str(body.get("content", "")).strip()
        source = str(body.get("source", "")).strip()
        if not content or not source:
            raise ApiError(400, "missing_fields", "content 和 source 必填")
        expected = body.get("expected_revision")
        if not isinstance(expected, int):
            raise ApiError(400, "revision_required", "expected_revision 必须是整数")
        with self.repo.tx() as conn:
            case = self._case(conn, case_id)
            if not self.can_access(case, role, region) or role in {"medical_reviewer"}:
                raise ApiError(403, "followup_forbidden", "当前角色不能提交随访")
            if case["status"] == "merged":
                raise ApiError(409, "case_merged", "已合并案例不能再更新")
            if case["revision"] != expected:
                raise ApiError(409, "revision_conflict", "案例已被其他人员更新，请重新读取")
            revision = case["revision"] + 1
            received = parse_time(body.get("received_at"), utcnow())
            due = report_deadline(received, bool(case["serious"]), bool(case["fatal"]))
            conn.execute(
                "INSERT INTO followups(case_id,content,source,received_at,revision,created_by,created_at) VALUES(?,?,?,?,?,?,?)",
                (case_id, content, source, iso(received), revision, actor, iso()),
            )
            conn.execute(
                "UPDATE cases SET revision=?,received_at=?,report_due_at=?,updated_at=? WHERE id=?",
                (revision, iso(received), iso(due), iso(), case_id),
            )
            invalidated = self._invalidate_open_messages(conn, case_id, actor, role, "followup")
            Repository.audit(conn, case_id, actor, role, "followup_added",
                             {"revision": revision, "source": source, "reports_invalidated": invalidated})
            return {"case": dict(self._case(conn, case_id)), "revision": revision, "reports_invalidated": invalidated}

    def medical_review(self, case_id: int, actor: str, role: str, body: dict[str, Any]) -> dict[str, Any]:
        if role != "medical_reviewer":
            raise ApiError(403, "medical_reviewer_required", "只有医学审核员可以裁定严重性")
        expected = body.get("expected_revision")
        if not isinstance(expected, int):
            raise ApiError(400, "revision_required", "expected_revision 必须是整数")
        serious = body.get("serious")
        fatal = body.get("fatal")
        causality = str(body.get("causality", "")).strip()
        rationale = str(body.get("rationale", "")).strip()
        if not isinstance(serious, bool) or not isinstance(fatal, bool) or not causality or not rationale:
            raise ApiError(400, "invalid_review", "serious/fatal 必须是布尔值，causality 和 rationale 必填")
        if fatal and not serious:
            raise ApiError(400, "invalid_severity", "死亡案例必须标记为严重")
        received = parse_time(body.get("received_at"))
        due = report_deadline(received, serious, fatal)
        with self.repo.tx() as conn:
            case = self._case(conn, case_id)
            if case["status"] == "merged":
                raise ApiError(409, "case_merged", "已合并案例不能审核")
            if case["revision"] != expected:
                raise ApiError(409, "revision_conflict", "案例版本已变化")
            revision = expected + 1
            conn.execute(
                """UPDATE cases SET serious=?,fatal=?,causality=?,report_due_at=?,revision=?,updated_at=? WHERE id=?""",
                (int(serious), int(fatal), causality, iso(due), revision, iso(), case_id),
            )
            conn.execute(
                """INSERT INTO medical_reviews(case_id,case_revision,serious,fatal,causality,rationale,reviewer,created_at)
                   VALUES(?,?,?,?,?,?,?,?)""",
                (case_id, expected, int(serious), int(fatal), causality, rationale, actor, iso()),
            )
            invalidated = self._invalidate_open_messages(conn, case_id, actor, role, "medical_review")
            Repository.audit(conn, case_id, actor, role, "medical_reviewed",
                             {"from_revision": expected, "serious": serious, "fatal": fatal,
                              "causality": causality, "reports_invalidated": invalidated})
            return {"case": dict(self._case(conn, case_id)), "reviewed_revision": expected,
                    "reports_invalidated": invalidated}

    def merge_cases(self, source_id: int, actor: str, role: str, body: dict[str, Any]) -> dict[str, Any]:
        if role != "global_admin":
            raise ApiError(403, "merge_forbidden", "只有全局管理员可以合并案例")
        target_id = body.get("target_case_id")
        if not isinstance(target_id, int) or source_id == target_id:
            raise ApiError(400, "invalid_target", "target_case_id 必须指向不同案例")
        with self.repo.tx() as conn:
            source = self._case(conn, source_id)
            target = self._case(conn, target_id)
            if source["status"] == "merged":
                return {"case": dict(source), "idempotent": True}
            if target["status"] == "merged" or source["product"].casefold() != target["product"].casefold():
                raise ApiError(409, "merge_conflict", "目标案例不可用，或产品与来源案例不一致")
            conn.execute("UPDATE cases SET status='merged',merged_into=?,revision=revision+1,updated_at=? WHERE id=?", (target_id, iso(), source_id))
            conn.execute("UPDATE intakes SET case_id=? WHERE case_id=?", (target_id, source_id))
            invalidated = self._invalidate_open_messages(conn, source_id, actor, role, "case_merge")
            Repository.audit(conn, target_id, actor, role, "case_merged_in", {"source_case_id": source_id})
            Repository.audit(conn, source_id, actor, role, "case_merged_into",
                             {"target_case_id": target_id, "reports_invalidated": invalidated})
            return {"case": dict(self._case(conn, source_id)), "idempotent": False}

    # ---------- 报告报文：生成 / 发送 / 重试 / 重放 ----------

    @staticmethod
    def _gateway_mode(role: str, body: dict[str, Any]) -> str:
        mode = str(body.get("gateway_mode", "ok"))
        if mode not in {"ok", "timeout", "fail"}:
            mode = "ok"
        # 故障注入仅用于全局管理员的联调/演练
        if mode != "ok" and role != "global_admin":
            mode = "ok"
        return mode

    def _build_message(
        self, conn: sqlite3.Connection, case: sqlite3.Row, report: sqlite3.Row, actor: str, mode: str
    ) -> sqlite3.Row:
        payload = pv_rules.message_payload(dict(case), dict(report))
        payload_raw = json.dumps(payload, ensure_ascii=False, sort_keys=True)
        digest = pv_rules.payload_hash(payload)
        key = pv_rules.idempotency_key(report["id"], case["revision"])
        now = iso()
        try:
            cur = conn.execute(
                """INSERT INTO report_messages(report_id,case_id,case_revision,country,due_at,payload_json,
                   payload_hash,idempotency_key,status,sim_mode,created_by,created_at,updated_at)
                   VALUES(?,?,?,?,?,?,?,?,?,?,?,?,?)""",
                (report["id"], case["id"], case["revision"], report["country"], report["due_at"],
                 payload_raw, digest, key, pv_states.MESSAGE_PENDING, mode, actor, now, now),
            )
        except sqlite3.IntegrityError as exc:
            raise ApiError(409, "message_exists", "该案例版本的报文已经存在，请勿重复生成") from exc
        Repository.audit(conn, case["id"], actor, "system", "message_generated",
                         {"message_id": cur.lastrowid, "report_id": report["id"],
                          "case_revision": case["revision"], "idempotency_key": key})
        return conn.execute("SELECT * FROM report_messages WHERE id=?", (cur.lastrowid,)).fetchone()

    def _send_message(
        self, conn: sqlite3.Connection, message: sqlite3.Row, actor: str, role: str, mode: str | None = None
    ) -> str:
        """发送（或重试）一条报文，同一幂等键不会被监管侧重复受理。返回尝试结果。"""
        send_mode = mode or message["sim_mode"] or "ok"
        now = iso()
        try:
            result, submission_id = self.gateway.send(
                conn,
                idempotency_key=message["idempotency_key"],
                payload_json=message["payload_json"],
                sent_at=now,
                mode=send_mode,
            )
        except GatewayTimeout as exc:
            # 超时：结果未知，报文保持在途，等待回执或对账
            if message["status"] != pv_states.MESSAGE_IN_FLIGHT:
                pv_states.ensure_message_transition(message["status"], pv_states.MESSAGE_IN_FLIGHT)
            conn.execute(
                "UPDATE report_messages SET status=?,last_error=?,updated_at=? WHERE id=?",
                (pv_states.MESSAGE_IN_FLIGHT, f"网络超时: {exc}", now, message["id"]),
            )
            self._record_attempt(conn, message["id"], pv_states.ATTEMPT_TIMEOUT, send_mode, str(exc), actor)
            return pv_states.ATTEMPT_TIMEOUT
        except GatewayFailure as exc:
            pv_states.ensure_message_transition(message["status"], pv_states.MESSAGE_FAILED)
            conn.execute(
                "UPDATE report_messages SET status=?,last_error=?,updated_at=? WHERE id=?",
                (pv_states.MESSAGE_FAILED, f"发送失败: {exc}", now, message["id"]),
            )
            self._record_attempt(conn, message["id"], pv_states.ATTEMPT_FAILED, send_mode, str(exc), actor)
            return pv_states.ATTEMPT_FAILED

        pv_states.ensure_message_transition(message["status"], pv_states.MESSAGE_IN_FLIGHT)
        conn.execute(
            """UPDATE report_messages SET status=?,last_error=NULL,regulator_submission_id=COALESCE(regulator_submission_id,?),
               updated_at=? WHERE id=?""",
            (pv_states.MESSAGE_IN_FLIGHT, submission_id, now, message["id"]),
        )
        self._record_attempt(conn, message["id"], result, send_mode,
                             "监管已受理请求" if result == pv_states.ATTEMPT_SENT else "幂等重试，监管识别为同一笔",
                             actor)
        return result

    @staticmethod
    def _record_attempt(
        conn: sqlite3.Connection, message_id: int, result: str, mode: str, detail: str, actor: str
    ) -> None:
        conn.execute(
            "INSERT INTO report_attempts(message_id,result,mode,detail,actor,created_at) VALUES(?,?,?,?,?,?)",
            (message_id, result, mode, detail, actor, iso()),
        )

    def _mark_report_sent_once(
        self, conn: sqlite3.Connection, report_id: int, actor: str, at: datetime
    ) -> None:
        row = conn.execute("SELECT submitted_at,due_at FROM reports WHERE id=?", (report_id,)).fetchone()
        if row["submitted_at"] is None:
            late = int(at > parse_time(row["due_at"]))
            conn.execute(
                "UPDATE reports SET submitted_at=?,submitted_by=?,late=? WHERE id=?",
                (iso(at), actor, late, report_id),
            )

    def create_report(self, case_id: int, actor: str, role: str, region: str, body: dict[str, Any]) -> dict[str, Any]:
        if role not in {"regional_lead", "global_admin"}:
            raise ApiError(403, "report_forbidden", "只有区域负责人或全局管理员可以生成报告")
        country = str(body.get("country", "")).strip().upper()
        if not country:
            raise ApiError(400, "country_required", "country 必填")
        with self.repo.tx() as conn:
            case = self._case(conn, case_id)
            if not self.can_access(dict(case), role, region):
                raise ApiError(403, "region_forbidden", "不能为本区域之外案例生成报告")
            due = report_deadline(parse_time(case["received_at"]), bool(case["serious"]), bool(case["fatal"]))
            try:
                cur = conn.execute("INSERT INTO reports(case_id,country,due_at,status) VALUES(?,?,?,?)", (case_id, country, iso(due), "pending"))
            except sqlite3.IntegrityError as exc:
                raise ApiError(409, "report_exists", "该国家报告已经存在") from exc
            Repository.audit(conn, case_id, actor, role, "report_created", {"report_id": cur.lastrowid, "country": country})
            report = conn.execute("SELECT * FROM reports WHERE id=?", (cur.lastrowid,)).fetchone()
            return self._report_view(conn, report, role, region)

    def submit_report(self, report_id: int, actor: str, role: str, region: str, body: dict[str, Any]) -> dict[str, Any]:
        if role not in {"regional_lead", "global_admin"}:
            raise ApiError(403, "submit_forbidden", "当前角色不能提交监管报告")
        mode = self._gateway_mode(role, body)
        sent_at = parse_time(body.get("submitted_at"), utcnow())
        with self.repo.tx() as conn:
            joined = self._report_with_case(conn, report_id)
            self._require_report_scope(joined, role, region)
            case = self._case(conn, joined["case_id"])
            latest = self._latest_message(conn, report_id)

            if latest is not None and latest["case_revision"] == case["revision"]:
                message = latest
                if latest["status"] == pv_states.MESSAGE_ACCEPTED:
                    return {"report": self._report_view(conn, joined, role, region),
                            "idempotent": True, "attempt": "already_accepted"}
                if latest["status"] == pv_states.MESSAGE_RETURNED:
                    raise ApiError(409, "supplement_requires_revision",
                                   "该版本已被退回，请先通过随访补齐资料（产生新案例修订）后再重新上报")
                if latest["status"] == pv_states.MESSAGE_SUPERSEDED:
                    raise ApiError(409, "manual_review_required",
                                   "报文因案例修订失效，需在对账队列执行“按当前版本重新上报”")
                if latest["status"] == pv_states.MESSAGE_FAILED and role != "global_admin":
                    raise ApiError(409, "replay_required",
                                   "发送失败的任务只能由全局管理员重放")
            else:
                # 当前案例修订尚无报文 -> 按当前修订生成不可变报文
                message = self._build_message(conn, case, joined, actor, mode)

            attempt = self._send_message(conn, message, actor, role, mode if message["status"] == pv_states.MESSAGE_PENDING else None)
            self._mark_report_sent_once(conn, report_id, actor, sent_at)
            view = self._persist_report_status(conn, report_id, role, region)
            Repository.audit(conn, case["id"], actor, role, "report_submitted",
                             {"report_id": report_id, "country": joined["country"],
                              "message_id": message["id"], "attempt": attempt,
                              "idempotency_key": message["idempotency_key"]})
            return {"report": view, "idempotent": attempt == pv_states.ATTEMPT_DUPLICATE,
                    "attempt": attempt,
                    "message": {"id": message["id"], "case_revision": message["case_revision"],
                                "idempotency_key": message["idempotency_key"],
                                "status": conn.execute("SELECT status FROM report_messages WHERE id=?", (message["id"],)).fetchone()["status"]}}

    def retry_report(self, report_id: int, actor: str, role: str, region: str) -> dict[str, Any]:
        """网络超时重试：沿用同一幂等键重发在途报文。"""
        if role not in {"regional_lead", "global_admin"}:
            raise ApiError(403, "retry_forbidden", "当前角色不能重试监管报告")
        with self.repo.tx() as conn:
            joined = self._report_with_case(conn, report_id)
            self._require_report_scope(joined, role, region)
            case = self._case(conn, joined["case_id"])
            latest = self._latest_message(conn, report_id)
            if latest is None:
                raise ApiError(409, "not_submitted", "报告尚未发送，不能重试")
            if latest["status"] == pv_states.MESSAGE_FAILED:
                raise ApiError(409, "replay_required", "失败任务需由全局管理员执行重放")
            if latest["status"] != pv_states.MESSAGE_IN_FLIGHT:
                raise ApiError(409, "retry_not_allowed", "只有等待回执（含超时）的报文可以重试")
            if latest["case_revision"] != case["revision"]:
                raise ApiError(409, "manual_review_required", "报文版本已失效，请按当前版本重新上报")
            attempt = self._send_message(conn, latest, actor, role, "ok")
            view = self._persist_report_status(conn, report_id, role, region)
            Repository.audit(conn, case["id"], actor, role, "report_retried",
                             {"report_id": report_id, "message_id": latest["id"], "attempt": attempt,
                              "idempotency_key": latest["idempotency_key"]})
            return {"report": view, "attempt": attempt,
                    "message": {"id": latest["id"], "idempotency_key": latest["idempotency_key"]}}

    def replay_failed(self, actor: str, role: str, body: dict[str, Any]) -> dict[str, Any]:
        """全局管理员重放失败任务（可按 report_id 限定）。"""
        if role != "global_admin":
            raise ApiError(403, "replay_forbidden", "只有全局管理员可以重放失败任务")
        report_id = body.get("report_id")
        if report_id is not None and not isinstance(report_id, int):
            raise ApiError(400, "invalid_report_id", "report_id 必须是整数")
        replayed: list[dict[str, Any]] = []
        with self.repo.tx() as conn:
            sql = ("SELECT m.* FROM report_messages m JOIN reports r ON r.id=m.report_id "
                   "JOIN cases c ON c.id=r.case_id WHERE m.status=?")
            args: list[Any] = [pv_states.MESSAGE_FAILED]
            if report_id is not None:
                sql += " AND m.report_id=?"
                args.append(report_id)
            sql += " ORDER BY m.id"
            for msg in conn.execute(sql, args).fetchall():
                attempt = self._send_message(conn, msg, actor, role, "ok")
                view = self._persist_report_status(conn, msg["report_id"], role, "")
                replayed.append({"report_id": msg["report_id"], "message_id": msg["id"],
                                 "attempt": attempt, "status": view["status"]})
                Repository.audit(conn, msg["case_id"], actor, role, "failed_message_replayed",
                                 {"report_id": msg["report_id"], "message_id": msg["id"], "attempt": attempt})
        return {"replayed": replayed}

    def resubmit_report(self, report_id: int, actor: str, role: str, region: str, body: dict[str, Any]) -> dict[str, Any]:
        """人工复核/退回补件后，按当前案例版本生成新报文重报。"""
        if role not in {"regional_lead", "global_admin"}:
            raise ApiError(403, "resubmit_forbidden", "当前角色不能重新上报")
        mode = self._gateway_mode(role, body)
        with self.repo.tx() as conn:
            joined = self._report_with_case(conn, report_id)
            self._require_report_scope(joined, role, region)
            case = self._case(conn, joined["case_id"])
            latest = self._latest_message(conn, report_id)
            if latest is None:
                raise ApiError(409, "not_submitted", "报告尚未发送过")
            if latest["case_revision"] == case["revision"]:
                if latest["status"] == pv_states.MESSAGE_RETURNED:
                    raise ApiError(409, "supplement_requires_revision",
                                   "退回补件必须先补齐资料并形成新案例修订，然后才能重新上报")
                raise ApiError(409, "revision_required", "案例修订后才能重新上报该报告")
            if latest["status"] == pv_states.MESSAGE_IN_FLIGHT:
                raise ApiError(409, "message_still_pending", "旧报文仍在途，不能重报")
            # 旧报文为已退回/已失效/旧版本受理：按当前修订生成新报文
            message = self._build_message(conn, case, joined, actor, mode)
            attempt = self._send_message(conn, message, actor, role, mode)
            self._mark_report_sent_once(conn, report_id, actor, utcnow())
            view = self._persist_report_status(conn, report_id, role, region)
            Repository.audit(conn, case["id"], actor, role, "report_resubmitted",
                             {"report_id": report_id, "old_message_id": latest["id"],
                              "new_message_id": message["id"], "attempt": attempt})
            return {"report": view, "attempt": attempt,
                    "message": {"id": message["id"], "case_revision": message["case_revision"],
                                "idempotency_key": message["idempotency_key"]}}

    # ---------- 回执与对账 ----------

    def _apply_decision(
        self,
        conn: sqlite3.Connection,
        message: sqlite3.Row,
        case: sqlite3.Row,
        decision: str,
        return_reason: str | None,
        supplement_due_at: str | None,
    ) -> str:
        """按判定规则把监管决定落到报文上。返回 applied / stale / ignored。"""
        joined = conn.execute("SELECT * FROM reports WHERE id=?", (message["report_id"],)).fetchone()
        verdict = pv_rules.validate_receipt(
            dict(message), dict(case), decision,
            return_reason=return_reason, supplement_due_at=supplement_due_at,
        )
        if verdict["action"] != "accept":
            return "stale" if verdict["reason"] == pv_states.BLOCK_RECEIPT_STALE else "ignored"

        target = pv_states.MESSAGE_ACCEPTED if decision == pv_states.DECISION_ACCEPTED else pv_states.MESSAGE_RETURNED
        pv_states.ensure_message_transition(message["status"], target)
        conn.execute(
            """UPDATE report_messages SET status=?,return_reason=?,supplement_due_at=?,last_error=NULL,updated_at=?
               WHERE id=?""",
            (target, verdict.get("return_reason"), verdict.get("supplement_due_at"), iso(), message["id"]),
        )
        return "applied"

    def deliver_receipt(self, actor: str, role: str, body: dict[str, Any]) -> dict[str, Any]:
        """监管回执送达（原型中由全局管理员模拟；生产中替换为网关签名回调）。"""
        if role != "global_admin":
            raise ApiError(403, "receipt_forbidden", "只有全局管理员可以登记监管回执")
        key = str(body.get("idempotency_key", "")).strip()
        decision = str(body.get("decision", "")).strip()
        if not key:
            raise ApiError(400, "key_required", "idempotency_key 必填")
        if decision not in pv_states.RECEIPT_DECISIONS:
            raise ApiError(400, "invalid_decision", "decision 必须是 accepted 或 returned")
        return_reason = str(body.get("return_reason", "")).strip() or None
        supplement_due_at = None
        if decision == pv_states.DECISION_RETURNED:
            if not return_reason:
                raise ApiError(400, "return_reason_required", "退回回执必须带退回原因")
            supplement_due_at = iso(parse_time(body.get("supplement_due_at")))
        with self.repo.tx() as conn:
            message = conn.execute("SELECT * FROM report_messages WHERE idempotency_key=?", (key,)).fetchone()
            if not message:
                raise ApiError(404, "unknown_idempotency_key", "没有与该幂等键匹配的报文")
            case = self._case(conn, message["case_id"])
            try:
                outcome = self._apply_decision(
                    conn, message, case, decision, return_reason, supplement_due_at
                )
            except ValueError as exc:
                raise ApiError(400, "invalid_receipt", str(exc)) from exc
            conn.execute(
                """INSERT INTO report_receipts(message_id,idempotency_key,decision,return_reason,
                   supplement_due_at,applied,reject_reason,delivered_by,created_at)
                   VALUES(?,?,?,?,?,?,?,?,?)""",
                (message["id"], key, decision, return_reason, supplement_due_at,
                 1 if outcome == "applied" else 0, None if outcome == "applied" else outcome,
                 actor, iso()),
            )
            if outcome == "applied":
                self._persist_report_status(conn, message["report_id"])
            Repository.audit(conn, case["id"], actor, role, "receipt_delivered",
                             {"message_id": message["id"], "decision": decision, "outcome": outcome,
                              "return_reason": return_reason})
            return {"applied": outcome == "applied", "outcome": outcome, "decision": decision,
                    "report": self._report_view(conn, self._report_with_case(conn, message["report_id"]), role, "")}

    def _poll_and_apply(self, conn: sqlite3.Connection, message: sqlite3.Row, actor: str, role: str) -> str:
        polled = self.gateway.poll(conn, message["idempotency_key"])
        if not polled or not polled.get("resolved"):
            return "pending"
        case = self._case(conn, message["case_id"])
        try:
            outcome = self._apply_decision(
                conn, message, case, polled["decision"],
                polled.get("return_reason"), polled.get("supplement_due_at"),
            )
        except ValueError:
            outcome = "ignored"
        if outcome == "applied":
            conn.execute(
                """INSERT INTO report_receipts(message_id,idempotency_key,decision,return_reason,
                   supplement_due_at,applied,reject_reason,delivered_by,created_at)
                   VALUES(?,?,?,?,?,?,?,?,?)""",
                (message["id"], message["idempotency_key"], polled["decision"],
                 polled.get("return_reason"), polled.get("supplement_due_at"), 1, None, actor, iso()),
            )
            self._persist_report_status(conn, message["report_id"])
            Repository.audit(conn, case["id"], actor, role, "receipt_reconciled",
                             {"message_id": message["id"], "decision": polled["decision"]})
        return outcome

    def reconcile(self, actor: str, role: str, region: str, report_id: int | None = None) -> dict[str, Any]:
        """对未决（在途）报文主动向监管侧对账，拉回迟到的受理/退回结论。"""
        if role not in {"regional_lead", "global_admin"}:
            raise ApiError(403, "reconcile_forbidden", "当前角色不能执行对账")
        results: list[dict[str, Any]] = []
        with self.repo.tx() as conn:
            sql = ("SELECT m.* FROM report_messages m JOIN reports r ON r.id=m.report_id "
                   "JOIN cases c ON c.id=r.case_id WHERE m.status=?")
            args: list[Any] = [pv_states.MESSAGE_IN_FLIGHT]
            if report_id is not None:
                sql += " AND m.report_id=?"
                args.append(report_id)
            if role == "regional_lead":
                sql += " AND c.region=?"
                args.append(region)
            sql += " ORDER BY m.id"
            for msg in conn.execute(sql, args).fetchall():
                outcome = self._poll_and_apply(conn, msg, actor, role)
                results.append({"report_id": msg["report_id"], "message_id": msg["id"], "outcome": outcome})
        return {"checked": len(results), "results": results}

    def recover_pending(self, actor: str, role: str) -> dict[str, Any]:
        """服务重启后恢复：对所有仍在途的报文继续对账。"""
        return self.reconcile(actor, "global_admin", "", report_id=None)

    # ---------- 状态视图 ----------

    def _persist_report_status(
        self, conn: sqlite3.Connection, report_id: int, role: str | None = None, region: str | None = None
    ) -> dict[str, Any]:
        joined = self._report_with_case(conn, report_id)
        case = self._case(conn, joined["case_id"])
        latest = self._latest_message(conn, report_id)
        view = pv_rules.derive_report_view(dict(joined), dict(latest) if latest else None, dict(case))
        conn.execute("UPDATE reports SET status=? WHERE id=?", (view["status"], report_id))
        view = pv_rules.derive_report_view(
            dict(conn.execute("SELECT * FROM reports WHERE id=?", (report_id,)).fetchone()),
            dict(latest) if latest else None, dict(case))
        if role is not None:
            same_region = (region == case["region"])
            view["actions"] = pv_actions.available_actions(role, same_region, view["status"])
        return view

    def _report_view(
        self, conn: sqlite3.Connection, report: sqlite3.Row, role: str | None = None, region: str | None = None
    ) -> dict[str, Any]:
        if "case_region" in report.keys():
            joined = report
        else:
            joined = self._report_with_case(conn, report["id"])
        case = self._case(conn, joined["case_id"])
        latest = self._latest_message(conn, report["id"])
        view = pv_rules.derive_report_view(dict(joined), dict(latest) if latest else None, dict(case))
        view["case_no"] = case["case_no"]
        view["region"] = case["region"]
        if role is not None:
            view["actions"] = pv_actions.available_actions(role, region == case["region"], view["status"])
        return view

    def reconciliation_queue(self, role: str, region: str) -> list[dict[str, Any]]:
        """对账队列：当前未完成（含阻塞）的报告，附当前状态与阻塞原因。"""
        sql = ("SELECT r.* FROM reports r JOIN cases c ON c.id=r.case_id "
               "WHERE c.status!='merged'")
        args: list[Any] = []
        if role not in {"medical_reviewer", "global_admin"}:
            sql += " AND c.region=?"
            args.append(region)
        sql += " ORDER BY r.due_at, r.id"
        out: list[dict[str, Any]] = []
        with self.repo.tx() as conn:
            for row in conn.execute(sql, args).fetchall():
                view = self._report_view(conn, row, role, region)
                if view["status"] not in {pv_states.REPORT_ACCEPTED, pv_states.REPORT_PENDING, pv_states.REPORT_OVERDUE}:
                    out.append(view)
        return out

    def overdue(self, role: str, region: str) -> list[dict[str, Any]]:
        sql = "SELECT * FROM reports WHERE status IN ('pending','overdue') AND due_at < ?"
        args: list[Any] = [iso()]
        if role not in {"medical_reviewer", "global_admin"}:
            sql += " AND case_id IN (SELECT id FROM cases WHERE region=?)"
            args.append(region)
        return [dict(r) for r in self.repo.conn.execute(sql, args)]

    def escalate_overdue(self, actor: str, role: str, region: str) -> dict[str, Any]:
        if role not in {"regional_lead", "global_admin"}:
            raise ApiError(403, "escalation_forbidden", "当前角色不能执行逾期升级")
        rows = self.overdue(role, region)
        with self.repo.tx() as conn:
            for row in rows:
                conn.execute("UPDATE reports SET status='overdue' WHERE id=? AND status='pending'", (row["id"],))
                Repository.audit(conn, row["case_id"], actor, role, "report_overdue_escalated", {"report_id": row["id"], "country": row["country"]})
        return {"escalated": len(rows)}

    def state(self, role: str, region: str) -> dict[str, Any]:
        cases = self.list_cases(role, region, {})
        return {"cases": cases, "overdue": self.overdue(role, region),
                "reconciliation": self.reconciliation_queue(role, region), "server_time": iso()}


def json_response(handler: BaseHTTPRequestHandler, status: int, payload: Any) -> None:
    raw = json.dumps(payload, ensure_ascii=False, default=str).encode("utf-8")
    handler.send_response(status)
    handler.send_header("Content-Type", "application/json; charset=utf-8")
    handler.send_header("Content-Length", str(len(raw)))
    handler.end_headers()
    handler.wfile.write(raw)


class Handler(BaseHTTPRequestHandler):
    service: PharmacovigilanceService
    web_root: Path

    def log_message(self, fmt: str, *args: Any) -> None:
        print(f"{self.address_string()} - {fmt % args}")

    def _body(self) -> dict[str, Any]:
        length = int(self.headers.get("Content-Length", "0"))
        if not length:
            return {}
        try:
            result = json.loads(self.rfile.read(length))
        except json.JSONDecodeError as exc:
            raise ApiError(400, "invalid_json", "请求体不是有效 JSON") from exc
        if not isinstance(result, dict):
            raise ApiError(400, "invalid_json", "请求体必须是 JSON 对象")
        return result

    def _dispatch_get(self, path: str, query: dict[str, list[str]]) -> Any:
        if path == "/health":
            return 200, {"status": "ok", "service": "pharmacovigilance"}
        actor, role, region = self.service.identity(self.headers)
        if path == "/api/state":
            return 200, self.service.state(role, region)
        if path == "/api/cases":
            return 200, {"cases": self.service.list_cases(role, region, query)}
        if path == "/api/overdue":
            return 200, {"reports": self.service.overdue(role, region)}
        if path == "/api/reconciliation":
            return 200, {"reports": self.service.reconciliation_queue(role, region)}
        parts = [part for part in path.split("/") if part]
        if len(parts) == 3 and parts[:2] == ["api", "cases"] and parts[2].isdigit():
            return 200, self.service.get_case(int(parts[2]), role, region)
        raise ApiError(404, "not_found", "接口不存在")

    def _dispatch_post(self, path: str, body: dict[str, Any]) -> Any:
        actor, role, region = self.service.identity(self.headers)
        if path == "/api/cases":
            return 201, self.service.create_case(actor, role, region, body)
        if path == "/api/escalate-overdue":
            return 200, self.service.escalate_overdue(actor, role, region)
        if path == "/api/reconcile":
            return 200, self.service.reconcile(actor, role, region, body.get("report_id"))
        if path == "/api/admin/replay-failed":
            return 200, self.service.replay_failed(actor, role, body)
        if path == "/api/regulator/receipts":
            return 200, self.service.deliver_receipt(actor, role, body)
        parts = [part for part in path.split("/") if part]
        if len(parts) == 4 and parts[:2] == ["api", "cases"] and parts[2].isdigit():
            case_id, action = int(parts[2]), parts[3]
            if action == "followups":
                return 201, self.service.add_followup(case_id, actor, role, region, body)
            if action == "medical-review":
                return 200, self.service.medical_review(case_id, actor, role, body)
            if action == "reports":
                return 201, self.service.create_report(case_id, actor, role, region, body)
            if action == "merge":
                return 200, self.service.merge_cases(case_id, actor, role, body)
        if len(parts) == 4 and parts[:2] == ["api", "reports"] and parts[2].isdigit():
            report_id, action = int(parts[2]), parts[3]
            if action == "submit":
                return 200, self.service.submit_report(report_id, actor, role, region, body)
            if action == "retry":
                return 200, self.service.retry_report(report_id, actor, role, region)
            if action == "resubmit":
                return 200, self.service.resubmit_report(report_id, actor, role, region, body)
            if action == "reconcile":
                return 200, self.service.reconcile(actor, role, region, report_id)
        raise ApiError(404, "not_found", "接口不存在")

    def _handle(self, method: str) -> None:
        parsed = urlparse(self.path)
        try:
            if method == "GET" and parsed.path == "/":
                page = (self.web_root / "index.html").read_bytes()
                self.send_response(200)
                self.send_header("Content-Type", "text/html; charset=utf-8")
                self.send_header("Content-Length", str(len(page)))
                self.end_headers()
                self.wfile.write(page)
                return
            if method == "GET":
                status, payload = self._dispatch_get(parsed.path, parse_qs(parsed.query))
            else:
                status, payload = self._dispatch_post(parsed.path, self._body())
            json_response(self, status, payload)
        except ApiError as exc:
            json_response(self, exc.status, {"error": exc.code, "message": exc.message})
        except Exception as exc:
            print(f"unhandled error: {exc!r}")
            json_response(self, 500, {"error": "internal_error", "message": str(exc)})

    def do_GET(self) -> None:
        self._handle("GET")

    def do_POST(self) -> None:
        self._handle("POST")


def create_server(db_path: str | Path, host: str = "127.0.0.1", port: int = PORT) -> ThreadingHTTPServer:
    service = PharmacovigilanceService(db_path)
    web_root = Path(__file__).resolve().parent / "static"
    handler = type("PharmacovigilanceHandler", (Handler,), {"service": service, "web_root": web_root})
    return ThreadingHTTPServer((host, port), handler)


def main() -> None:
    parser = argparse.ArgumentParser()
    parser.add_argument("--host", default="127.0.0.1")
    parser.add_argument("--port", type=int, default=PORT)
    parser.add_argument("--db", default=os.environ.get("PV_DB", "pharmacovigilance.db"))
    args = parser.parse_args()
    server = create_server(args.db, args.host, args.port)
    print(f"pharmacovigilance listening on http://{args.host}:{args.port}")
    try:
        server.serve_forever()
    except KeyboardInterrupt:
        pass
    finally:
        server.server_close()


if __name__ == "__main__":
    main()
