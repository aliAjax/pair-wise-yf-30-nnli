// 上报对账台页面操作（与状态常量、判定规则分开维护）
const STATUS_LABELS = {
  not_submitted: "未提交",
  reconciling: "未决对账中",
  accepted: "已受理",
  rejected: "退回补件",
  failed: "发送失败",
  invalid: "已失效·人工复核",
  closed: "已关闭",
};

function headers() {
  return {
    "X-User-Id": document.getElementById("user").value.trim(),
    "X-Role": document.getElementById("role").value.trim(),
    "X-Region": document.getElementById("region").value.trim(),
    "Content-Type": "application/json",
  };
}

async function api(path, opts) {
  const r = await fetch(path, { headers: headers(), ...opts });
  const data = await r.json().catch(() => ({}));
  if (!r.ok) throw new Error(`${data.error || "error"}: ${data.message || r.status}`);
  return data;
}

function esc(s) {
  return String(s ?? "").replace(/[&<>"]/g, (c) => ({ "&": "&amp;", "<": "&lt;", ">": "&gt;", '"': "&quot;" }[c]));
}

async function load() {
  const wrap = document.getElementById("table-wrap");
  wrap.innerHTML = "<p>加载中…</p>";
  try {
    const { tasks } = await api("/api/reconciliation");
    if (!tasks.length) {
      wrap.innerHTML = "<p>暂无对账任务。请先在案例台生成国家报告。</p>";
      return;
    }
    const role = document.getElementById("role").value.trim();
    const rows = tasks.map((t) => {
      const stale = t.message_id && t.message_revision !== t.case_revision;
      const version = t.message_id
        ? `<span class="${stale ? "stale" : "ok"}">报文 v${t.message_revision} / 案例 v${t.case_revision}${stale ? "（版本不一致）" : ""}</span>`
        : "—";
      const receipt = t.receipt_outcome
        ? t.receipt_outcome === "accepted"
          ? '<span class="ok">已受理</span>'
          : `退回：${esc(t.receipt_reason)}<br>补件期限：${esc(t.receipt_supplement_due_at)}`
        : "—";
      return `<tr>
        <td>${esc(t.case_no)}<br><span class="mono">${esc(t.region)} · ${esc(t.country)}</span></td>
        <td><span class="badge ${esc(t.task_status)}">${STATUS_LABELS[t.task_status] || t.task_status}</span></td>
        <td>${version}</td>
        <td>${t.attempts}</td>
        <td class="mono">${esc(t.idempotency_key || "—")}</td>
        <td>${esc(t.blocking_reason || "—")}</td>
        <td>${receipt}</td>
        <td>${actions(t, role)}</td>
      </tr>`;
    });
    wrap.innerHTML = `<table><thead><tr>
      <th>案例/区域</th><th>状态</th><th>报文版本</th><th>尝试</th><th>幂等键</th><th>阻塞原因</th><th>最近回执</th><th>操作</th>
      </tr></thead><tbody>${rows.join("")}</tbody></table>`;
  } catch (e) {
    wrap.innerHTML = `<p style="color:#b91c1c">加载失败：${esc(e.message)}</p>`;
  }
}

function actions(t, role) {
  const btn = (label, fn, cls) => `<button class="${cls || ""}" onclick="${fn}">${label}</button>`;
  let html = "";
  if (t.task_status === "not_submitted" || t.task_status === "rejected") {
    html += btn("提交上报", `submitReport(${t.report_id}, false)`) + " ";
  }
  if (t.task_status === "failed" && role === "global_admin") {
    html += btn("重放失败任务", `replayTask(${t.report_id}, false)`, "warn") + " ";
  }
  if (t.task_status === "invalid") {
    html += btn("关闭", `manualReview(${t.report_id}, 'close')`, "gray") + " ";
    html += btn("重新上报", `manualReview(${t.report_id}, 'resubmit')`);
  }
  if (t.task_status === "reconciling") {
    html += btn("模拟监管回执", `pushReceipt(${t.report_id})`, "gray");
  }
  return html || "—";
}

async function submitReport(reportId, timeout) {
  try {
    await api(`/api/reports/${reportId}/submit`, { method: "POST", body: JSON.stringify({ timeout }) });
    await load();
  } catch (e) {
    alert(e.message);
  }
}

async function replayTask(reportId, timeout) {
  try {
    await api(`/api/reports/${reportId}/replay`, { method: "POST", body: JSON.stringify({ timeout }) });
    await load();
  } catch (e) {
    alert(e.message);
  }
}

async function manualReview(reportId, decision) {
  const tip = decision === "close" ? "确认关闭该上报？关闭后不再上报。" : "按当前案例修订重新生成报文并上报？";
  if (!confirm(tip)) return;
  try {
    await api(`/api/reports/${reportId}/reconcile`, { method: "POST", body: JSON.stringify({ decision }) });
    await load();
  } catch (e) {
    alert(e.message);
  }
}

async function pushReceipt(reportId) {
  const outcome = prompt("监管回执结果：输入 1 = 受理，2 = 退回补件");
  if (!outcome) return;
  const body = { idempotency_key: "", outcome: outcome.trim() === "2" ? "rejected" : "accepted" };
  if (!body.idempotency_key) {
    // 取当前报文幂等键
    const { tasks } = await api("/api/reconciliation");
    const t = tasks.find((x) => x.report_id === reportId);
    body.idempotency_key = t.idempotency_key;
  }
  if (body.outcome === "rejected") {
    body.reason = prompt("退回原因：") || "";
    body.supplement_due_at = prompt("补件期限（ISO 8601，如 2026-10-15T00:00:00Z）：") || "";
  }
  try {
    await api(`/api/reports/${reportId}/receipt`, { method: "POST", body: JSON.stringify(body) });
    await load();
  } catch (e) {
    alert(e.message);
  }
}

load();
