"""监管网关适配器（原型用本地 SQLite 模拟真实监管受理系统）。

关键行为用于复现并解决“超时重复受理”问题：
- 同一幂等键重复提交：网关识别为同一笔，不会二次受理，返回 duplicate；
- timeout：网络超时，网关是否已受理未知，必须靠后续回执/对账确认；
- failed：明确的发送失败（非超时），任务进入失败队列等待全局重放。

真实环境下把本类替换为 HTTP 网关客户端即可，服务层契约保持不变。
"""
from __future__ import annotations

import sqlite3
from datetime import datetime

import pv_states as st


class GatewayTimeout(Exception):
    """网络超时：结果未知，报文保持在途。"""


class GatewayFailure(Exception):
    """明确发送失败。"""


class RegulatorGateway:
    def send(
        self,
        conn: sqlite3.Connection,
        *,
        idempotency_key: str,
        payload_json: str,
        sent_at: str,
        mode: str = "ok",
    ) -> tuple[str, int | None]:
        """发送报文。返回 (attempt结果, 网关受理序号)。

        mode:
          ok       —— 正常进入监管受理队列
          timeout  —— 模拟网络超时（可能已到达，结果未知）
          fail     —— 模拟网关明确故障
        """
        existing = conn.execute(
            "SELECT id, accepted FROM regulator_submissions WHERE idempotency_key=?",
            (idempotency_key,),
        ).fetchone()
        if existing:
            # 重试沿用同一幂等键：监管侧绝不重复受理
            return st.ATTEMPT_DUPLICATE, existing["id"]

        if mode == "fail":
            # 明确故障：请求未到达监管侧，没有受理记录
            raise GatewayFailure("监管网关不可用")

        # 请求到达监管侧并登记（ok / timeout 都会落库）；
        # timeout 表示响应丢失，监管侧是否受理要靠后续回执/对账确认。
        cur = conn.execute(
            """INSERT INTO regulator_submissions(idempotency_key,payload_json,first_sent_at,accepted,decision_at)
               VALUES(?,?,?,0,NULL)""",
            (idempotency_key, payload_json, sent_at),
        )
        if mode == "timeout":
            raise GatewayTimeout("监管网关网络超时（请求可能已受理）")
        return st.ATTEMPT_SENT, cur.lastrowid

    def issue_decision(
        self,
        conn: sqlite3.Connection,
        *,
        idempotency_key: str,
        decision: str,
        decided_at: str,
        return_reason: str | None = None,
        supplement_due_at: str | None = None,
    ) -> bool:
        """监管侧作出受理/退回决定。若幂等键不存在（超时未达）返回 False。"""
        row = conn.execute(
            "SELECT id FROM regulator_submissions WHERE idempotency_key=?",
            (idempotency_key,),
        ).fetchone()
        if not row:
            return False
        conn.execute(
            """UPDATE regulator_submissions SET accepted=?,decision_at=?,return_reason=?,supplement_due_at=?
               WHERE id=?""",
            (1 if decision == st.DECISION_ACCEPTED else 0, decided_at, return_reason, supplement_due_at, row["id"]),
        )
        return True

    def poll(self, conn: sqlite3.Connection, idempotency_key: str) -> dict | None:
        row = conn.execute(
            "SELECT * FROM regulator_submissions WHERE idempotency_key=?",
            (idempotency_key,),
        ).fetchone()
        if not row:
            return None
        if row["decision_at"] is None:
            return {"resolved": False}
        return {
            "resolved": True,
            "decision": st.DECISION_ACCEPTED if row["accepted"] else st.DECISION_RETURNED,
            "return_reason": row["return_reason"],
            "supplement_due_at": row["supplement_due_at"],
            "decided_at": row["decision_at"],
        }
