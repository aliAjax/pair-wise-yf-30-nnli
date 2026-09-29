"""上报状态机：报文状态、报告状态、阻塞原因与允许的流转。

本模块只维护“状态”本身（取值、中文标签、合法迁移），
不包含任何判定规则（见 pv_rules.py）和页面操作（见 pv_actions.py）。
"""
from __future__ import annotations

# 报文（按案例修订生成的不可变报文）生命周期
MESSAGE_PENDING = "pending"          # 报文已生成，尚未发出
MESSAGE_IN_FLIGHT = "in_flight"      # 已发送，等待监管回执（未决）
MESSAGE_ACCEPTED = "accepted"        # 监管受理（终态）
MESSAGE_RETURNED = "returned"        # 监管退回补件（终态，带原因与期限）
MESSAGE_FAILED = "failed"            # 发送失败，等待全局管理员重放
MESSAGE_SUPERSEDED = "superseded"    # 案例已修订，旧报文失效

MESSAGE_STATUSES: dict[str, str] = {
    MESSAGE_PENDING: "待发送",
    MESSAGE_IN_FLIGHT: "等待回执",
    MESSAGE_ACCEPTED: "已受理",
    MESSAGE_RETURNED: "退回补件",
    MESSAGE_FAILED: "发送失败",
    MESSAGE_SUPERSEDED: "已失效",
}

# 未决：监管尚未给出结论（pending 也算未生成发送结论；失败等待人工重放）
UNRESOLVED_STATUSES = {MESSAGE_PENDING, MESSAGE_IN_FLIGHT, MESSAGE_FAILED}
# 终态报文不可再迁移
TERMINAL_STATUSES = {MESSAGE_ACCEPTED, MESSAGE_RETURNED, MESSAGE_SUPERSEDED}

# 允许的状态迁移，作为服务层落库前的护栏
MESSAGE_TRANSITIONS: dict[str, set[str]] = {
    MESSAGE_PENDING: {MESSAGE_IN_FLIGHT, MESSAGE_FAILED, MESSAGE_SUPERSEDED},
    MESSAGE_IN_FLIGHT: {MESSAGE_ACCEPTED, MESSAGE_RETURNED, MESSAGE_FAILED, MESSAGE_SUPERSEDED},
    MESSAGE_FAILED: {MESSAGE_IN_FLIGHT, MESSAGE_SUPERSEDED},
    MESSAGE_ACCEPTED: set(),
    MESSAGE_RETURNED: set(),
    MESSAGE_SUPERSEDED: set(),
}

# 报告（分国家报告）聚合状态
REPORT_PENDING = "pending"
REPORT_OVERDUE = "overdue"
REPORT_SUBMITTED = "submitted"            # 当前报文在途（兼容既有状态名）
REPORT_ACCEPTED = "accepted"
REPORT_RETURNED = "returned"
REPORT_FAILED = "failed"
REPORT_MANUAL_REVIEW = "manual_review"

REPORT_STATUSES: dict[str, str] = {
    REPORT_PENDING: "待上报",
    REPORT_OVERDUE: "已逾期",
    REPORT_SUBMITTED: "已发送待回执",
    REPORT_ACCEPTED: "已完成受理",
    REPORT_RETURNED: "退回待补件",
    REPORT_FAILED: "发送失败待重放",
    REPORT_MANUAL_REVIEW: "人工复核",
}

# 机器可读的阻塞原因（页面据此展示“阻塞原因”，标签集中维护）
BLOCK_AWAITING_RECEIPT = "awaiting_receipt"
BLOCK_SEND_FAILED = "send_failed"
BLOCK_SUPPLEMENT = "supplement_requested"
BLOCK_REVISION_CHANGED = "case_revision_changed"
BLOCK_RECEIPT_STALE = "receipt_version_stale"

BLOCK_REASONS: dict[str, str] = {
    BLOCK_AWAITING_RECEIPT: "等待监管机构回执，到点自动对账",
    BLOCK_SEND_FAILED: "监管网关发送失败，需全局管理员重放",
    BLOCK_SUPPLEMENT: "监管退回补件，请按原因和期限补充后重新上报",
    BLOCK_REVISION_CHANGED: "案例已被修订，原报文失效，需人工复核后按新版本重报",
    BLOCK_RECEIPT_STALE: "回执对应旧版本报文，不能作为当前受理结论",
}

# 发送尝试结果
ATTEMPT_SENT = "sent"          # 网关确认收到
ATTEMPT_TIMEOUT = "timeout"    # 超时（网关可能已收到，结果未知）
ATTEMPT_DUPLICATE = "duplicate"  # 幂等重试，网关识别为同一笔
ATTEMPT_FAILED = "failed"      # 明确发送失败

# 回执决定
DECISION_ACCEPTED = "accepted"
DECISION_RETURNED = "returned"
RECEIPT_DECISIONS = {DECISION_ACCEPTED, DECISION_RETURNED}


def ensure_message_transition(current: str, target: str) -> None:
    if target != current and target not in MESSAGE_TRANSITIONS.get(current, set()):
        raise ValueError(f"非法报文状态迁移: {current} -> {target}")
