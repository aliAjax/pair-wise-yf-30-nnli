# 药物警戒案例处理系统

使用 Python 标准库实现的独立原型，覆盖多渠道案例接入、去重、随访更正、严重性医学裁定、分国家报告、逾期升级、跨区域权限、案例合并审计与上报对账。

## 运行

要求 Python 3.11+。

```bash
python3 app.py --db pharmacovigilance.db
```

默认监听 `127.0.0.1:8201`。首页为 `http://127.0.0.1:8201/`，上报对账台为 `http://127.0.0.1:8201/reconciliation`，健康检查为 `/health`。

所有接口使用请求头 `X-User-Id`、`X-Role` 和区域角色必需的 `X-Region`。角色为 `reporter`、`regional_lead`、`medical_reviewer`、`global_admin`。

## 主要接口

- `POST /api/cases`：录入案例，`dedupe_key` 相同则返回已存在案例。
- `GET /api/cases`、`GET /api/cases/{id}`：按权限查询。
- `POST /api/cases/{id}/followups`：用 `expected_revision` 防止覆盖随访。
- `POST /api/cases/{id}/medical-review`：医学审核员更新严重性、死亡和关联性。
- `POST /api/cases/{id}/reports`、`POST /api/reports/{id}/submit`：生成并提交分国家报告。
- `POST /api/cases/{id}/merge`：全局管理员合并重复案例。
- `POST /api/escalate-overdue`、`GET /api/overdue`：逾期检查与升级。

## 上报对账

报告发送到监管网关后，网络超时会触发重试；为避免同一条报告被重复受理，系统按以下规则对账：

- **不可变报文**：每次提交按当前案例修订生成报文快照（`report_messages`），报文内容不随后续案例更新而改变。
- **幂等重试**：重试沿用同一报文与同一幂等键（`pv-idem-...`），发送次数累加；重新上报才会启用新键。
- **回执版本判定**：监管回执（`report_receipts`）只在报文版本与案例当前修订一致时才受理或退回；旧版本报文的回执记录为 `stale_ignored`，不改变任务状态。
- **未决失效**：案例在未决对账期间被随访、医学审核或合并（修订变化）时，未决任务立即置为 `invalid`，报文置为 `superseded`，进入人工复核。
- **退回补件**：监管退回必须填写原因（`reason`）和补件期限（`supplement_due_at`），任务置为 `rejected` 并在页面展示阻塞原因。
- **权限**：区域负责人只处理本区域的提交、复核与查询；`replay`（重放失败任务）仅全局管理员可调用。
- **重启恢复**：所有对账状态持久化在 SQLite，服务重启后未决任务继续对账（启动时日志提示笔数并登记 `reconciliation_resumed`）。

对账接口：

- `GET /api/reconciliation`：对账台数据（当前状态、报文/案例版本、阻塞原因、最近回执）。
- `POST /api/reports/{id}/submit`：提交上报；请求体 `{"timeout": true}` 可模拟网络超时（任务转 `failed`）。
- `POST /api/reports/{id}/receipt`：模拟监管网关推送回执（`outcome` 为 `accepted`/`rejected`，退回需 `reason` 与 `supplement_due_at`）。
- `POST /api/reports/{id}/replay`：全局管理员重放失败任务（沿用同一报文与幂等键）。
- `POST /api/reports/{id}/reconcile`：人工复核 `invalid` 任务，`decision` 为 `close`（关闭）或 `resubmit`（按当前修订重新生成报文）。
- `GET /api/reports/{id}/messages`、`GET /api/reports/{id}/receipts`：查看不可变报文与回执流水。

状态、判定规则与页面操作分模块维护：

- `reconciliation/status.py`：上报状态常量与页面中文标签；
- `reconciliation/rules.py`：对账判定规则（纯函数，不依赖数据库与 HTTP）；
- `reconciliation/service.py`：对账服务（报文、幂等、回执、失效、复核、重放）；
- `static/reconciliation.js`：对账台页面操作（与状态、规则分离）。

## 测试

```bash
python3 -m unittest discover -s tests -v
```

## 主要局限

该实现使用请求头模拟身份，不含生产级登录、签名和密钥管理；SQLite 与标准库 HTTP 服务适合单机原型。分国家规则采用内置严重 15 天、死亡 7 天、非严重 90 天规则，接入真实监管网关前需按当地法规扩展。

