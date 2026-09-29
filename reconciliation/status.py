"""上报对账状态常量（与判定规则、页面操作分开维护）。

状态分三组：
- TaskStatus：上报对账任务状态（页面展示的"当前状态"）
- MessageStatus：不可变报文状态
- ReceiptOutcome / ReceiptDisposition：监管回执结果与处理结论
"""
from __future__ import annotations


class TaskStatus:
    NOT_SUBMITTED = "not_submitted"  # 报告已生成，尚未发送报文
    RECONCILING = "reconciling"      # 报文已发送，未决，等待监管回执对账
    ACCEPTED = "accepted"            # 回执受理，且报文版本与当前案例修订一致
    REJECTED = "rejected"            # 回执退回补件（带原因与期限）
    FAILED = "failed"                # 发送失败（如网络超时），可由全局管理员重放
    INVALID = "invalid"              # 案例在未决期间变化，报文失效，进入人工复核
    CLOSED = "closed"                # 人工复核确认关闭，不再上报


class MessageStatus:
    ACTIVE = "active"      # 当前有效报文
    SUPERSEDED = "superseded"  # 已被新报文取代（案例变化后失效）


class ReceiptOutcome:
    ACCEPTED = "accepted"
    REJECTED = "rejected"


class ReceiptDisposition:
    APPLIED = "applied"                # 回执与当前报文版本一致，已生效
    STALE_IGNORED = "stale_ignored"    # 回执针对旧报文，已记录但不生效
    DUPLICATE = "duplicate"            # 同一报文的重复回执，幂等忽略


TASK_STATUS_LABELS = {
    TaskStatus.NOT_SUBMITTED: "未提交",
    TaskStatus.RECONCILING: "未决对账中",
    TaskStatus.ACCEPTED: "已受理",
    TaskStatus.REJECTED: "退回补件",
    TaskStatus.FAILED: "发送失败",
    TaskStatus.INVALID: "已失效·人工复核",
    TaskStatus.CLOSED: "已关闭",
}
