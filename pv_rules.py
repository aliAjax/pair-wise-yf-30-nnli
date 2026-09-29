"""判定规则：不可变报文生成、幂等键、回执版本校验、报告当前状态派生。

所有“如何判定”的业务规则集中在本模块，与状态取值（pv_states.py）、
页面操作（pv_actions.py）分离，便于单独审查和维护。
"""
from __future__ import annotations

import hashlib
import json
from typing import Any

import pv_states as st


def message_payload(case: dict[str, Any], report: dict[str, Any]) -> dict[str, Any]:
    """按案例修订快照出本次要发送的报文内容。

    revision 被写入报文；修订后内容改变、版本号改变，旧报文天然不可复用。
    """
    return {
        "schema": "pv-report/1",
        "case_no": case["case_no"],
        "case_revision": case["revision"],
        "patient_ref": case["patient_ref"],
        "product": case["product"],
        "event_term": case["event_term"],
        "serious": bool(case["serious"]),
        "fatal": bool(case["fatal"]),
        "causality": case["causality"],
        "received_at": case["received_at"],
        "country": report["country"],
        "due_at": report["due_at"],
    }


def payload_hash(payload: dict[str, Any]) -> str:
    """规范序列化后的哈希，任何字段变化都会改变指纹。"""
    canonical = json.dumps(payload, ensure_ascii=False, sort_keys=True, separators=(",", ":"))
    return hashlib.sha256(canonical.encode("utf-8")).hexdigest()


def idempotency_key(report_id: int, revision: int) -> str:
    """同一条报告在同一案例修订上的所有重试沿用同一幂等键。"""
    return f"PV-RPT-{report_id:08d}-REV{revision:04d}"


def supersede_on_revision(current_message_status: str) -> bool:
    """案例发生修订（随访/医学审核/合并）时，哪些在途报文立即失效。

    只有未决报文失效；已受理/已退回/已失效保持其历史结论。
    """
    return current_message_status in st.UNRESOLVED_STATUSES


def validate_receipt(
    message: dict[str, Any],
    case: dict[str, Any],
    decision: str,
    *,
    return_reason: str | None = None,
    supplement_due_at: str | None = None,
) -> dict[str, Any]:
    """校验一条回执能否被接受，返回判定结果。

    回执只接受与“当前报文版本一致”的受理或退回；
    退回必须带原因和补件期限。
    """
    if decision not in st.RECEIPT_DECISIONS:
        raise ValueError("回执决定必须是 accepted 或 returned")

    if message["idempotency_key"] is None:
        return {"action": "reject", "reason": "no_outbound_record"}

    # 版本一致性优先：报文必须仍绑定案例当前修订，且指纹未漂移。
    # 即使报文已因案例修订而失效（终态），迟到的旧版本回执仍标记为“版本过期”。
    version_mismatch = message["case_revision"] != case["revision"]
    if not version_mismatch:
        expected_hash = payload_hash(message_payload(case, _report_view(case, message)))
        version_mismatch = expected_hash != message["payload_hash"]
    if version_mismatch:
        return {"action": "reject", "reason": st.BLOCK_RECEIPT_STALE}

    if message["status"] in st.TERMINAL_STATUSES:
        # 同版本终态报文再次收到结论：重复回执，幂等忽略
        return {"action": "ignore", "reason": "terminal_message", "status": message["status"]}

    if decision == st.DECISION_RETURNED:
        reason = (return_reason or "").strip()
        if not reason:
            raise ValueError("退回回执必须提供退回原因")
        if not supplement_due_at:
            raise ValueError("退回回执必须提供补件期限")
        return {"action": "accept", "decision": st.DECISION_RETURNED,
                "return_reason": reason, "supplement_due_at": supplement_due_at}

    return {"action": "accept", "decision": st.DECISION_ACCEPTED}


def _report_view(case: dict[str, Any], message: dict[str, Any]) -> dict[str, Any]:
    # 复算指纹时只需要 country / due_at，due_at 在修订时会被重算并写入新报文
    return {"country": message["country"], "due_at": message["due_at"]}


def derive_report_view(
    report: dict[str, Any],
    latest_message: dict[str, Any] | None,
    case: dict[str, Any],
) -> dict[str, Any]:
    """根据“最新报文 + 案例当前修订”派生报告当前状态与阻塞原因。

    页面和对账接口统一走这里，保证看到的永远是当前真实状态。
    """
    view = dict(report)
    blocked = False
    block_reason_code = None
    block_detail = None

    msg_status = latest_message["status"] if latest_message else None

    if not latest_message:
        status = st.REPORT_OVERDUE if report["status"] == st.REPORT_OVERDUE else st.REPORT_PENDING
    elif msg_status == st.MESSAGE_PENDING:
        status = st.REPORT_PENDING
    elif msg_status == st.MESSAGE_IN_FLIGHT:
        status = st.REPORT_SUBMITTED
        blocked = True
        block_reason_code = st.BLOCK_AWAITING_RECEIPT
    elif msg_status == st.MESSAGE_FAILED:
        status = st.REPORT_FAILED
        blocked = True
        block_reason_code = st.BLOCK_SEND_FAILED
        block_detail = latest_message.get("last_error")
    elif msg_status == st.MESSAGE_RETURNED:
        status = st.REPORT_RETURNED
        blocked = True
        block_reason_code = st.BLOCK_SUPPLEMENT
        block_detail = {
            "reason": latest_message.get("return_reason"),
            "supplement_due_at": latest_message.get("supplement_due_at"),
        }
    elif msg_status == st.MESSAGE_ACCEPTED:
        if latest_message["case_revision"] != case["revision"]:
            # 受理的是旧版本：案例之后又被修订，受理不再覆盖当前版本
            status = st.REPORT_MANUAL_REVIEW
            blocked = True
            block_reason_code = st.BLOCK_REVISION_CHANGED
        else:
            status = st.REPORT_ACCEPTED
    elif msg_status == st.MESSAGE_SUPERSEDED:
        status = st.REPORT_MANUAL_REVIEW
        blocked = True
        block_reason_code = st.BLOCK_REVISION_CHANGED
    else:  # pragma: no cover - 防御性分支
        status = st.REPORT_MANUAL_REVIEW
        blocked = True
        block_reason_code = st.BLOCK_REVISION_CHANGED

    if report["status"] == st.REPORT_OVERDUE and status == st.REPORT_PENDING:
        status = st.REPORT_OVERDUE

    view["status"] = status
    view["status_label"] = st.REPORT_STATUSES[status]
    view["blocked"] = blocked
    view["block_reason_code"] = block_reason_code
    view["block_reason_label"] = st.BLOCK_REASONS[block_reason_code] if block_reason_code else None
    view["block_detail"] = block_detail
    view["current_message_id"] = latest_message["id"] if latest_message else None
    view["current_message_status"] = msg_status
    view["current_case_revision"] = case["revision"]
    view["message_case_revision"] = latest_message["case_revision"] if latest_message else None
    return view
