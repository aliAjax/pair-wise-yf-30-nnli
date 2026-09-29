"""上报对账服务：不可变报文、幂等重试、回执版本判定、失效与人工复核、失败重放。

状态、规则、页面操作分离：
- 状态常量见 status.py；
- 判定规则见 rules.py（纯函数）；
- 页面脚本见 static/reconciliation.js。

所有对账状态持久化在 SQLite 中，服务重启后未决任务继续对账。
"""
from __future__ import annotations

import json
import sqlite3
from contextlib import contextmanager
from datetime import timedelta
from typing import Any

from . import rules
from .errors import ApiError
from .status import MessageStatus, ReceiptDisposition, ReceiptOutcome, TaskStatus
from .timeutil import iso, parse_time, utcnow


class ReconciliationService:
    def __init__(self, conn: sqlite3.Connection):
        self.conn = conn

    @contextmanager
    def tx(self):
        """显式事务：保证多语句写入的原子性；事务内抛出的异常会回滚。"""
        self.conn.execute("BEGIN IMMEDIATE")
        try:
            yield self.conn
            self.conn.execute("COMMIT")
        except Exception:
            self.conn.execute("ROLLBACK")
            raise

    # ---------- 基础查询 ----------

    def _case(self, conn: sqlite3.Connection, case_id: int) -> sqlite3.Row:
        row = conn.execute("SELECT * FROM cases WHERE id=?", (case_id,)).fetchone()
        if not row:
            raise ApiError(404, "case_not_found", "案例不存在")
        return row

    def _report(self, conn: sqlite3.Connection, report_id: int) -> sqlite3.Row:
        row = conn.execute(
            "SELECT r.*, c.region AS case_region, c.revision AS case_revision "
            "FROM reports r JOIN cases c ON c.id=r.case_id WHERE r.id=?",
            (report_id,),
        ).fetchone()
        if not row:
            raise ApiError(404, "report_not_found", "报告不存在")
        return row

    def _task(self, conn: sqlite3.Connection, report_id: int) -> sqlite3.Row | None:
        return conn.execute("SELECT * FROM reconciliation_tasks WHERE report_id=?", (report_id,)).fetchone()

    def _message(self, conn: sqlite3.Connection, message_id: int) -> sqlite3.Row | None:
        return conn.execute("SELECT * FROM report_messages WHERE id=?", (message_id,)).fetchone()

    @staticmethod
    def _audit(conn: sqlite3.Connection, case_id: int | None, actor: str, role: str,
                action: str, detail: dict[str, Any]) -> None:
        conn.execute(
            "INSERT INTO audit_log(case_id,actor,role,action,detail_json,created_at) VALUES(?,?,?,?,?,?)",
            (case_id, actor, role, action, json.dumps(detail, ensure_ascii=False, sort_keys=True), iso()),
        )

    def ensure_task(self, conn: sqlite3.Connection, report_id: int, case_id: int) -> sqlite3.Row:
        task = self._task(conn, report_id)
        if task:
            return task
        now = iso()
        cur = conn.execute(
            "INSERT INTO reconciliation_tasks(report_id,case_id,status,attempts,created_at,updated_at) "
            "VALUES(?,?,?,?,?,?)",
            (report_id, case_id, TaskStatus.NOT_SUBMITTED, 0, now, now),
        )
        return conn.execute("SELECT * FROM reconciliation_tasks WHERE id=?", (cur.lastrowid,)).fetchone()

    # ---------- 报文生成与提交 ----------

    def _build_message(self, conn: sqlite3.Connection, report: sqlite3.Row, actor: str) -> sqlite3.Row:
        """按案例当前修订生成不可变报文，启用新的幂等键。"""
        case = self._case(conn, report["case_id"])
        payload = rules.build_payload(dict(case), dict(report))
        conn.execute(
            "UPDATE report_messages SET status=? WHERE report_id=? AND status=?",
            (MessageStatus.SUPERSEDED, report["id"], MessageStatus.ACTIVE),
        )
        cur = conn.execute(
            "INSERT INTO report_messages(report_id,case_id,case_revision,payload_json,"
            "idempotency_key,attempt,status,created_by,created_at) VALUES(?,?,?,?,?,?,?,?,?)",
            (report["id"], report["case_id"], case["revision"], rules.payload_json(payload),
             rules.new_idempotency_key(), 1, MessageStatus.ACTIVE, actor, iso()),
        )
        return self._message(conn, cur.lastrowid)

    def submit(self, report_id: int, actor: str, role: str, region: str, body: dict[str, Any]) -> dict[str, Any]:
        """提交上报：按当前案例修订生成不可变报文并发送。

        body.timeout=true 时模拟网络超时：任务转 failed，报文与幂等键保留，等待重放。
        """
        if role not in {"regional_lead", "global_admin"}:
            raise ApiError(403, "submit_forbidden", "当前角色不能提交监管报告")
        timeout = bool(body.get("timeout", False))
        conn = self.conn
        with self.tx() as conn:
            report = self._report(conn, report_id)
            if role == "regional_lead" and not rules.can_manage_region(report["case_region"], role, region):
                raise ApiError(403, "region_forbidden", "不能提交其他区域的报告")
            task = self.ensure_task(conn, report_id, report["case_id"])
            if task["status"] == TaskStatus.RECONCILING:
                return {"report": dict(report), "task": dict(task), "idempotent": True,
                        "message": dict(self._message(conn, task["message_id"])) if task["message_id"] else None}
            if task["status"] == TaskStatus.ACCEPTED:
                return {"report": dict(report), "task": dict(task), "idempotent": True,
                        "message": dict(self._message(conn, task["message_id"])) if task["message_id"] else None}
            if task["status"] == TaskStatus.INVALID:
                raise ApiError(409, "invalid_needs_manual_review",
                               "上报已失效并进入人工复核，请先完成人工复核再重新上报")
            if task["status"] == TaskStatus.CLOSED:
                raise ApiError(409, "task_closed", "该上报已经人工复核关闭，不能重新提交")
            if not rules.can_submit(dict(task)):
                raise ApiError(409, "submit_not_allowed", f"当前状态 {task['status']} 不允许提交")
            message = self._build_message(conn, report, actor)
            now = iso()
            if timeout:
                conn.execute(
                    "UPDATE reconciliation_tasks SET message_id=?,status=?,attempts=?,last_error=?,"
                    "next_retry_at=?,blocking_reason=?,updated_at=? WHERE id=?",
                    (message["id"], TaskStatus.FAILED, 1, "network_timeout",
                     iso(parse_time(now) + timedelta(minutes=5)),
                     rules.timeout_reason(), now, task["id"]),
                )
                self._audit(conn, report["case_id"], actor, role, "report_send_failed",
                            {"report_id": report_id, "country": report["country"],
                             "idempotency_key": message["idempotency_key"], "error": "network_timeout"})
            else:
                conn.execute(
                    "UPDATE reconciliation_tasks SET message_id=?,status=?,attempts=1,last_error=NULL,"
                    "next_retry_at=NULL,blocking_reason=?,updated_at=? WHERE id=?",
                    (message["id"], TaskStatus.RECONCILING,
                     "报文已发送至监管网关，等待回执对账（幂等键不变，重试不会重复受理）", now, task["id"]),
                )
                self._audit(conn, report["case_id"], actor, role, "report_submitted",
                            {"report_id": report_id, "country": report["country"],
                             "case_revision": message["case_revision"],
                             "idempotency_key": message["idempotency_key"]})
            late = int(parse_time(now) > parse_time(report["due_at"]))
            conn.execute(
                "UPDATE reports SET status='submitted',submitted_at=?,submitted_by=?,late=? WHERE id=?",
                (now, actor, late, report_id),
            )
            return {
                "report": dict(self._report(conn, report_id)),
                "task": dict(self._task(conn, report_id)),
                "message": dict(message),
                "idempotent": False,
            }

    # ---------- 监管回执 ----------

    def receive_receipt(self, report_id: int, actor: str, role: str, body: dict[str, Any]) -> dict[str, Any]:
        """监管回执回调（原型中由页面模拟监管网关推送）。

        只受理与当前报文版本一致的回执：
        - 受理 → 任务 accepted；
        - 退回 → 任务 rejected，必须带原因与补件期限；
        - 旧版本报文的回执记录为 stale_ignored，不改变任务状态。
        """
        outcome = str(body.get("outcome", "")).strip()
        if outcome not in (ReceiptOutcome.ACCEPTED, ReceiptOutcome.REJECTED):
            raise ApiError(400, "invalid_outcome", "outcome 必须是 accepted 或 rejected")
        reason = str(body.get("reason", "")).strip()
        supplement_due = str(body.get("supplement_due_at", "")).strip()
        if outcome == ReceiptOutcome.REJECTED and (not reason or not supplement_due):
            raise ApiError(400, "reason_and_deadline_required",
                           "退回补件必须填写原因和补件期限（reason、supplement_due_at）")
        received = parse_time(body.get("received_at"), utcnow())
        key = str(body.get("idempotency_key", "")).strip()
        message_id = body.get("message_id")
        conn = self.conn
        with self.tx() as conn:
            report = self._report(conn, report_id)
            task = self.ensure_task(conn, report_id, report["case_id"])
            message = None
            if key:
                message = conn.execute(
                    "SELECT * FROM report_messages WHERE idempotency_key=?", (key,)).fetchone()
            elif isinstance(message_id, int):
                message = self._message(conn, message_id)
            if not message:
                raise ApiError(404, "unknown_message", "回执中的报文不存在")
            if message["report_id"] != report_id:
                raise ApiError(400, "message_mismatch", "回执报文与报告不匹配")
            existing = conn.execute(
                "SELECT * FROM report_receipts WHERE message_id=? AND disposition=? ORDER BY id LIMIT 1",
                (message["id"], ReceiptDisposition.APPLIED),
            ).fetchone()
            if existing:
                return {"receipt": dict(existing), "idempotent": True,
                        "task": dict(task), "message": dict(message)}
            case = self._case(conn, message["case_id"])
            current = rules.is_receipt_current(dict(message), dict(case))
            disposition = ReceiptDisposition.APPLIED if current else ReceiptDisposition.STALE_IGNORED
            cur = conn.execute(
                "INSERT INTO report_receipts(report_id,message_id,idempotency_key,outcome,reason,"
                "supplement_due_at,disposition,received_at,created_at) VALUES(?,?,?,?,?,?,?,?,?)",
                (report_id, message["id"], message["idempotency_key"], outcome,
                 reason or None, supplement_due or None, disposition, iso(received), iso()),
            )
            receipt = conn.execute("SELECT * FROM report_receipts WHERE id=?", (cur.lastrowid,)).fetchone()
            stale_error: ApiError | None = None
            if not current:
                if task["status"] == TaskStatus.RECONCILING:
                    conn.execute(
                        "UPDATE report_messages SET status=? WHERE id=?",
                        (MessageStatus.SUPERSEDED, message["id"]),
                    )
                    conn.execute(
                        "UPDATE reconciliation_tasks SET status=?,blocking_reason=?,updated_at=? WHERE id=?",
                        (TaskStatus.INVALID,
                         f"回执报文版本 {message['case_revision']} 与案例当前修订 {case['revision']} 不一致，"
                         "报文已失效，请人工复核", iso(), task["id"]),
                    )
                self._audit(conn, report["case_id"], actor, role, "receipt_stale_ignored",
                            {"report_id": report_id, "message_id": message["id"],
                             "message_revision": message["case_revision"],
                             "case_revision": case["revision"], "outcome": outcome})
                stale_error = ApiError(409, "stale_receipt",
                                       f"回执针对修订 {message['case_revision']} 的报文，案例当前已更新至修订 "
                                       f"{case['revision']}，回执不生效，任务转人工复核")
            elif outcome == ReceiptOutcome.ACCEPTED:
                conn.execute(
                    "UPDATE reconciliation_tasks SET status=?,blocking_reason=NULL,last_error=NULL,"
                    "next_retry_at=NULL,updated_at=? WHERE id=?",
                    (TaskStatus.ACCEPTED, iso(), task["id"]),
                )
                self._audit(conn, report["case_id"], actor, role, "report_accepted",
                            {"report_id": report_id, "message_id": message["id"],
                             "case_revision": message["case_revision"]})
            else:
                conn.execute(
                    "UPDATE reconciliation_tasks SET status=?,blocking_reason=?,updated_at=? WHERE id=?",
                    (TaskStatus.REJECTED, rules.rejected_reason(reason, supplement_due), iso(), task["id"]),
                )
                self._audit(conn, report["case_id"], actor, role, "report_rejected",
                            {"report_id": report_id, "message_id": message["id"], "reason": reason,
                             "supplement_due_at": supplement_due})
            result = {"receipt": dict(receipt), "idempotent": False,
                      "task": dict(self._task(conn, report_id)), "message": dict(message)}
        if stale_error is not None:
            raise stale_error
        return result

    # ---------- 失败重放（全局管理员） ----------

    def replay(self, report_id: int, actor: str, role: str, body: dict[str, Any]) -> dict[str, Any]:
        """重放失败任务：沿用同一报文与幂等键，发送次数 +1。

        若案例已在失败期间更新，则旧报文失效，任务转人工复核，不允许重放。
        """
        if role != "global_admin":
            raise ApiError(403, "replay_forbidden", "只有全局管理员可以重放失败任务")
        timeout = bool(body.get("timeout", False))
        conn = self.conn
        with self.tx() as conn:
            report = self._report(conn, report_id)
            task = self.ensure_task(conn, report_id, report["case_id"])
            if not rules.can_replay(dict(task)):
                raise ApiError(409, "replay_not_allowed",
                               f"只有发送失败的任务可以重放，当前状态：{task['status']}")
            message = self._message(conn, task["message_id"]) if task["message_id"] else None
            if not message:
                raise ApiError(409, "replay_not_allowed", "任务没有可重放的报文")
            case = self._case(conn, report["case_id"])
            case_changed_error: ApiError | None = None
            if message["case_revision"] != case["revision"]:
                conn.execute(
                    "UPDATE report_messages SET status=? WHERE id=?",
                    (MessageStatus.SUPERSEDED, message["id"]),
                )
                conn.execute(
                    "UPDATE reconciliation_tasks SET status=?,blocking_reason=?,updated_at=? WHERE id=?",
                    (TaskStatus.INVALID,
                     f"失败期间案例由修订 {message['case_revision']} 更新为 {case['revision']}，"
                     "旧报文已失效，请人工复核后重新上报", iso(), task["id"]),
                )
                self._audit(conn, report["case_id"], actor, role, "report_invalidated",
                            {"report_id": report_id, "reason": "replay 时发现案例已更新"})
                case_changed_error = ApiError(409, "case_changed_manual_review",
                                              "案例已更新，旧报文失效，请人工复核后重新上报")
            else:
                attempts = task["attempts"] + 1
                now = iso()
                if timeout:
                    conn.execute(
                        "UPDATE reconciliation_tasks SET status=?,attempts=?,last_error=?,"
                        "next_retry_at=?,blocking_reason=?,updated_at=? WHERE id=?",
                        (TaskStatus.FAILED, attempts, "network_timeout",
                         iso(parse_time(now) + timedelta(minutes=5)),
                         rules.timeout_reason(), now, task["id"]),
                    )
                else:
                    conn.execute(
                        "UPDATE reconciliation_tasks SET status=?,attempts=?,last_error=NULL,"
                        "next_retry_at=NULL,blocking_reason=?,updated_at=? WHERE id=?",
                        (TaskStatus.RECONCILING, attempts,
                         f"第 {attempts} 次发送，等待回执对账（幂等键不变，重试不会重复受理）", now, task["id"]),
                    )
                self._audit(conn, report["case_id"], actor, role, "report_replayed",
                            {"report_id": report_id, "attempt": attempts,
                             "idempotency_key": message["idempotency_key"], "timeout": timeout})
                result = {"task": dict(self._task(conn, report_id)), "message": dict(message),
                          "idempotent": False}
        if case_changed_error is not None:
            raise case_changed_error
        return result

    # ---------- 人工复核（失效任务） ----------

    def manual_review(self, report_id: int, actor: str, role: str, region: str, body: dict[str, Any]) -> dict[str, Any]:
        """对已失效任务做人工复核：close 关闭，或 resubmit 按当前修订重新生成报文。"""
        if role not in {"regional_lead", "global_admin"}:
            raise ApiError(403, "manual_review_forbidden", "当前角色不能处理人工复核")
        decision = str(body.get("decision", "")).strip()
        if decision not in {"close", "resubmit"}:
            raise ApiError(400, "invalid_decision", "decision 必须是 close 或 resubmit")
        conn = self.conn
        with self.tx() as conn:
            report = self._report(conn, report_id)
            if role == "regional_lead" and not rules.can_manage_region(report["case_region"], role, region):
                raise ApiError(403, "region_forbidden", "不能复核其他区域的报告")
            task = self.ensure_task(conn, report_id, report["case_id"])
            if not rules.can_manual_review(dict(task)):
                raise ApiError(409, "manual_review_not_allowed",
                               f"只有已失效、进入人工复核的任务需要处理，当前状态：{task['status']}")
            now = iso()
            if decision == "close":
                conn.execute(
                    "UPDATE reconciliation_tasks SET status=?,blocking_reason=?,updated_at=? WHERE id=?",
                    (TaskStatus.CLOSED, f"人工复核确认关闭（{actor}）", now, task["id"]),
                )
                self._audit(conn, report["case_id"], actor, role, "report_manual_closed",
                            {"report_id": report_id})
            else:
                message = self._build_message(conn, report, actor)
                conn.execute(
                    "UPDATE reconciliation_tasks SET message_id=?,status=?,attempts=1,last_error=NULL,"
                    "next_retry_at=NULL,blocking_reason=?,updated_at=? WHERE id=?",
                    (message["id"], TaskStatus.RECONCILING,
                     "人工复核后按当前修订重新上报，等待回执对账", now, task["id"]),
                )
                conn.execute(
                    "UPDATE reports SET status='submitted',submitted_at=?,submitted_by=? WHERE id=?",
                    (now, actor, report_id),
                )
                self._audit(conn, report["case_id"], actor, role, "report_manual_resubmitted",
                            {"report_id": report_id, "case_revision": message["case_revision"],
                             "idempotency_key": message["idempotency_key"]})
                return {"task": dict(self._task(conn, report_id)), "message": dict(message),
                        "idempotent": False}
            return {"task": dict(self._task(conn, report_id)), "idempotent": False}

    # ---------- 案例变化钩子 ----------

    def invalidate_for_case(self, conn: sqlite3.Connection, case_id: int, actor: str, role: str,
                            reason: str) -> int:
        """案例修订变化后，所有未决（对账中）任务立即失效并进入人工复核。"""
        rows = conn.execute(
            "SELECT * FROM reconciliation_tasks WHERE case_id=? AND status=?",
            (case_id, TaskStatus.RECONCILING),
        ).fetchall()
        for task in rows:
            if task["message_id"]:
                conn.execute(
                    "UPDATE report_messages SET status=? WHERE id=?",
                    (MessageStatus.SUPERSEDED, task["message_id"]),
                )
            conn.execute(
                "UPDATE reconciliation_tasks SET status=?,blocking_reason=?,updated_at=? WHERE id=?",
                (TaskStatus.INVALID, reason, iso(), task["id"]),
            )
            self._audit(conn, case_id, actor, role, "report_invalidated",
                        {"report_id": task["report_id"], "reason": reason})
        return len(rows)

    def resume_pending(self) -> list[dict[str, Any]]:
        """服务重启后继续未决任务的对账：持久化状态不丢失，逐笔登记续对账日志。"""
        conn = self.conn
        rows = conn.execute(
            "SELECT * FROM reconciliation_tasks WHERE status=? ORDER BY id",
            (TaskStatus.RECONCILING,),
        ).fetchall()
        for task in rows:
            self._audit(conn, task["case_id"], "system", "global_admin",
                        "reconciliation_resumed", {"report_id": task["report_id"]})
        return [dict(r) for r in rows]

    # ---------- 查询（对账台） ----------

    _TASK_SELECT = """
        SELECT t.id AS task_id, t.report_id, t.case_id, t.message_id, t.status AS task_status,
               t.attempts, t.last_error, t.next_retry_at, t.blocking_reason,
               t.created_at AS task_created_at, t.updated_at AS task_updated_at,
               r.country, r.status AS report_status, r.due_at,
               c.case_no, c.region, c.revision AS case_revision,
               m.case_revision AS message_revision, m.idempotency_key,
               m.attempt AS message_attempt, m.status AS message_status,
               (SELECT r2.outcome FROM report_receipts r2 WHERE r2.message_id=m.id
                ORDER BY r2.id DESC LIMIT 1) AS receipt_outcome,
               (SELECT r2.reason FROM report_receipts r2 WHERE r2.message_id=m.id
                ORDER BY r2.id DESC LIMIT 1) AS receipt_reason,
               (SELECT r2.supplement_due_at FROM report_receipts r2 WHERE r2.message_id=m.id
                ORDER BY r2.id DESC LIMIT 1) AS receipt_supplement_due_at,
               (SELECT r2.disposition FROM report_receipts r2 WHERE r2.message_id=m.id
                ORDER BY r2.id DESC LIMIT 1) AS receipt_disposition
        FROM reconciliation_tasks t
        JOIN reports r ON r.id=t.report_id
        JOIN cases c ON c.id=t.case_id
        LEFT JOIN report_messages m ON m.id=t.message_id
    """

    def list_tasks(self, role: str, region: str) -> list[dict[str, Any]]:
        if role == "reporter":
            raise ApiError(403, "reconciliation_forbidden", "上报对账不对报告员开放")
        sql = self._TASK_SELECT
        args: list[Any] = []
        if role == "regional_lead":
            sql += " WHERE c.region=?"
            args.append(region)
        sql += " ORDER BY t.updated_at DESC, t.id DESC"
        rows = [dict(r) for r in self.conn.execute(sql, args)]
        for row in rows:
            task = {"status": row["task_status"], "blocking_reason": row["blocking_reason"]}
            message = {"case_revision": row["message_revision"]} if row["message_id"] else None
            case = {"revision": row["case_revision"]}
            receipt = (
                {"reason": row["receipt_reason"], "supplement_due_at": row["receipt_supplement_due_at"]}
                if row["receipt_outcome"] == ReceiptOutcome.REJECTED else None
            )
            row["blocking_reason"] = rules.blocking_reason(task, message, case, receipt)
        return rows

    def list_messages(self, report_id: int, role: str, region: str) -> list[dict[str, Any]]:
        report = self._report(self.conn, report_id)
        self._check_view(report, role, region)
        return [dict(r) for r in self.conn.execute(
            "SELECT * FROM report_messages WHERE report_id=? ORDER BY id DESC", (report_id,))]

    def list_receipts(self, report_id: int, role: str, region: str) -> list[dict[str, Any]]:
        report = self._report(self.conn, report_id)
        self._check_view(report, role, region)
        return [dict(r) for r in self.conn.execute(
            "SELECT * FROM report_receipts WHERE report_id=? ORDER BY id DESC", (report_id,))]

    @staticmethod
    def _check_view(report: sqlite3.Row, role: str, region: str) -> None:
        if role == "reporter":
            raise ApiError(403, "reconciliation_forbidden", "上报对账不对报告员开放")
        if role == "regional_lead" and not rules.can_manage_region(report["case_region"], role, region):
            raise ApiError(403, "region_forbidden", "无权查看其他区域的对账记录")
