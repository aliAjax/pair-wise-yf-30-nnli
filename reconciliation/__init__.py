"""药物警戒上报对账模块。

- status.py：上报状态常量（状态维护）
- rules.py：对账判定规则（纯函数，规则维护）
- service.py：对账服务（报文、幂等重试、回执、失效、人工复核、重放）
- errors.py / timeutil.py：共享基础
页面操作在 static/reconciliation.js，与状态、规则分离。
"""
