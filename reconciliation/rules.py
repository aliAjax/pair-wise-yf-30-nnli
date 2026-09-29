"""对账判定规则：纯函数，不访问数据库、不依赖 HTTP（与状态、页面操作分开维护）。

核心规则：
1. 报文按案例修订不可变生成，一次修订一份快照；
2. 重试沿用同一幂等键，重复发送不会被监管重复受理；
3. 回执只在报文版本与案例当前修订一致（且报文有效）时才受理或退回；
4. 案例变化后未决报文立即失效，任务转人工复核；
5. 退回补件必须带原因和补件期限；
6. 区域负责人只处理本区域，全局管理员可重放失败任务。
"""
from __future__ import annotations

import json
import uuid
from typing import Any

from .status import MessageStatus, TaskStatus


def new_idempotency_key() -> str:
    """生成幂等键。同一报文的多次重试共用一个键，重新上报才会换新键。"""
    return "pv-idem-" + uuid.uuid4().hex


def build_payload(case: dict[str, Any], report: dict[str, Any]) -> dict[str, Any]:
    """按当前案例修订生成不可变报文快照。

    报文一经生成不再随案例更新而改变；案例变化后只能按新修订重新生成。
    """
    return {
        "message_version": 1,
        "report_id": report["id"],
        "case_id": case["id"],
        "case_no": case["case_no"],
        "country": report["country"],
        "case_revision": case["revision"],
        "patient_ref": case["patient_ref"],
        "region": case["region"],
        "product": case["product"],
        "event_term": case["event_term"],
        "onset_at": case["onset_at"],
        "serious": bool(case["serious"]),
        "fatal": bool(case["fatal"]),
        "causality": case["causality"],
        "received_at": case["received_at"],
        "report_due_at": case["report_due_at"],
        "submitted_at": report.get("submitted_at"),
    }


def payload_json(payload: dict[str, Any]) -> str:
    return json.dumps(payload, ensure_ascii=False, sort_keys=True)


def is_receipt_current(message: dict[str, Any], case: dict[str, Any]) -> bool:
    """回执只接受与当前报文版本一致的受理或退回。"""
    return message["status"] == MessageStatus.ACTIVE and message["case_revision"] == case["revision"]


def can_submit(task: dict[str, Any] | None) -> bool:
    """未提交或被退回补件后可以重新生成报文；其余状态不允许直接提交。"""
    if task is None:
        return True
    return task["status"] in {TaskStatus.NOT_SUBMITTED, TaskStatus.REJECTED}


def can_replay(task: dict[str, Any] | None) -> bool:
    """只有发送失败的任务可以重放（重试沿用同一报文与幂等键）。"""
    return task is not None and task["status"] == TaskStatus.FAILED


def can_manual_review(task: dict[str, Any] | None) -> bool:
    """只有已失效、进入人工复核的任务需要人工判定。"""
    return task is not None and task["status"] == TaskStatus.INVALID


def can_manage_region(case_region: str, role: str, user_region: str) -> bool:
    """区域负责人只处理本区域；全局管理员不受区域限制。"""
    return role == "global_admin" or (role == "regional_lead" and case_region == user_region)


def invalidation_reason(old_revision: int, new_revision: int) -> str:
    """案例在未决对账期间变化时的阻塞原因。"""
    return (
        f"案例在未决对账期间由修订 {old_revision} 更新为 {new_revision}，"
        "原报文已失效，请人工复核后重新上报"
    )


def timeout_reason() -> str:
    return "发送网络超时，监管受理结果未知；重试沿用同一幂等键，可由全局管理员重放"


def rejected_reason(reason: str, supplement_due_at: str) -> str:
    return f"监管退回补件：{reason}；补件期限：{supplement_due_at}"


def blocking_reason(task: dict[str, Any], message: dict[str, Any] | None, case: dict[str, Any] | None,
                    receipt: dict[str, Any] | None) -> str | None:
    """汇总页面展示的阻塞原因。"""
    status = task["status"]
    if status == TaskStatus.RECONCILING:
        if message and case and message["case_revision"] != case["revision"]:
            return (
                f"报文基于案例修订 {message['case_revision']}，案例已更新至修订 {case['revision']}，"
                "回执版本不一致，请人工复核"
            )
        return "报文已发送至监管网关，等待回执对账（幂等键不变，重试不会重复受理）"
    if status == TaskStatus.FAILED:
        return f"{task.get('blocking_reason') or '发送失败'}；可由全局管理员重放"
    if status == TaskStatus.INVALID:
        return task.get("blocking_reason") or "案例在未决对账期间发生变化，报文已失效，请人工复核"
    if status == TaskStatus.REJECTED and receipt:
        return rejected_reason(receipt["reason"], receipt["supplement_due_at"])
    if status == TaskStatus.CLOSED:
        return task.get("blocking_reason") or "人工复核确认关闭，不再上报"
    return None
