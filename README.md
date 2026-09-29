# 药物警戒案例处理系统

使用 Python 标准库实现的独立原型，覆盖多渠道案例接入、去重、随访更正、严重性医学裁定、分国家报告、逾期升级、跨区域权限和案例合并审计。

## 运行

要求 Python 3.11+。

```bash
python3 app.py --db pharmacovigilance.db
```

默认监听 `127.0.0.1:8201`。首页为 `http://127.0.0.1:8201/`，健康检查为 `/health`。

所有接口使用请求头 `X-User-Id`、`X-Role` 和区域角色必需的 `X-Region`。角色为 `reporter`、`regional_lead`、`medical_reviewer`、`global_admin`。

## 主要接口

- `POST /api/cases`：录入案例，`dedupe_key` 相同则返回已存在案例。
- `GET /api/cases`、`GET /api/cases/{id}`：按权限查询。
- `POST /api/cases/{id}/followups`：用 `expected_revision` 防止覆盖随访。
- `POST /api/cases/{id}/medical-review`：医学审核员更新严重性、死亡和关联性。
- `POST /api/cases/{id}/reports`、`POST /api/reports/{id}/submit`：生成并提交分国家报告。
- `POST /api/reports/{id}/retry`：网络超时重试，沿用同一幂等键重发同一不可变报文。
- `POST /api/reports/{id}/resubmit`：退回补件/人工复核后，按当前案例修订生成新报文重报。
- `POST /api/reports/{id}/reconcile`、`POST /api/reconcile`：对在途报文主动对账拉回回执。
- `POST /api/admin/replay-failed`：全局管理员重放失败任务（可带 `report_id`）。
- `POST /api/regulator/receipts`：监管回执送达（原型由全局管理员模拟）；退回必须带 `return_reason` 和 `supplement_due_at`。
- `GET /api/reconciliation`：上报对账队列，含当前状态、阻塞原因和可用页面操作。
- `POST /api/cases/{id}/merge`：全局管理员合并重复案例。
- `POST /api/escalate-overdue`、`GET /api/overdue`：逾期检查与升级。

## 上报对账模型

- 每次按案例修订生成不可变报文（`report_messages`，含规范哈希与版本号），重试沿用同一幂等键 `PV-RPT-{report}-REV{revision}`；监管侧按幂等键去重，超时重试不会重复受理。
- 回执只接受与**当前报文版本**一致的受理或退回：旧版本迟到回执记为 `receipt_version_stale`，页面不会误显示“已完成”，而是进入人工复核。
- 案例变化（随访、医学审核、合并）后，所有未决报文（待发送/在途/失败）立即置为 `superseded` 并进入人工复核。
- 服务重启时自动对全部在途报文继续对账；页面统一显示当前状态与阻塞原因（等待回执/发送失败/退回补件/版本失效）。

模块按关注点分开维护：

- `pv_states.py`：报文与报告状态、阻塞原因、合法状态迁移。
- `pv_rules.py`：不可变报文生成、幂等键、回执版本判定、报告派生状态。
- `pv_actions.py`：页面操作注册表（按钮标签、接口、角色与适用状态）。
- `pv_gateway.py`：监管网关适配器（本地 SQLite 模拟，支持超时/故障注入），真实接入时替换此文件即可。

`gateway_mode=timeout|fail` 故障注入仅全局管理员可用，用于联调“超时重复受理”场景。

## 测试

```bash
python3 -m unittest discover -s tests -v
```

## 主要局限

该实现使用请求头模拟身份，不含生产级登录、签名和密钥管理；SQLite 与标准库 HTTP 服务适合单机原型。分国家规则采用内置严重 15 天、死亡 7 天、非严重 90 天规则，接入真实监管网关前需按当地法规扩展。
