"""页面操作：前端可执行动作的注册表。

“页面上能点什么”与上报状态（pv_states.py）、判定规则（pv_rules.py）
分开维护：调整按钮显隐或权限只改这里，不动状态机和对账逻辑。
服务端在执行前仍会再次鉴权，前端注册表只用于渲染。
"""
from __future__ import annotations

from dataclasses import dataclass

import pv_states as st


@dataclass(frozen=True)
class PageAction:
    key: str
    label: str
    endpoint: str
    method: str
    # 哪些角色可用；regional_lead 仅在本区域报告上出现（调用方按区域过滤）
    roles: frozenset[str]
    # 该动作适用的报告派生状态
    statuses: frozenset[str]
    danger: bool = False


ACTIONS: tuple[PageAction, ...] = (
    PageAction(
        key="submit",
        label="提交监管上报",
        endpoint="/api/reports/{id}/submit",
        method="POST",
        roles=frozenset({"regional_lead", "global_admin"}),
        statuses=frozenset({st.REPORT_PENDING, st.REPORT_OVERDUE}),
    ),
    PageAction(
        key="retry",
        label="超时重试（沿用同一幂等键）",
        endpoint="/api/reports/{id}/retry",
        method="POST",
        roles=frozenset({"regional_lead", "global_admin"}),
        statuses=frozenset({st.REPORT_SUBMITTED}),
    ),
    PageAction(
        key="reconcile",
        label="立即对账",
        endpoint="/api/reports/{id}/reconcile",
        method="POST",
        roles=frozenset({"regional_lead", "global_admin"}),
        statuses=frozenset({st.REPORT_SUBMITTED, st.REPORT_FAILED, st.REPORT_MANUAL_REVIEW}),
    ),
    PageAction(
        key="resubmit",
        label="按当前案例版本重新上报",
        endpoint="/api/reports/{id}/resubmit",
        method="POST",
        roles=frozenset({"regional_lead", "global_admin"}),
        statuses=frozenset({st.REPORT_RETURNED, st.REPORT_MANUAL_REVIEW}),
    ),
    PageAction(
        key="replay",
        label="全局重放失败任务",
        endpoint="/api/admin/replay-failed",
        method="POST",
        roles=frozenset({"global_admin"}),
        statuses=frozenset({st.REPORT_FAILED}),
        danger=True,
    ),
)

_ACTION_BY_KEY = {action.key: action for action in ACTIONS}


def available_actions(role: str, same_region: bool, report_status: str) -> list[dict[str, str | bool]]:
    """返回某角色在某条报告上当前可用的页面动作。"""
    result: list[dict[str, str | bool]] = []
    for action in ACTIONS:
        if role not in action.roles:
            continue
        if role == "regional_lead" and not same_region:
            # 区域负责人只处理本区域
            continue
        if report_status not in action.statuses:
            continue
        result.append({
            "key": action.key,
            "label": action.label,
            "endpoint": action.endpoint,
            "method": action.method,
            "danger": action.danger,
        })
    return result


def action(key: str) -> PageAction:
    return _ACTION_BY_KEY[key]
