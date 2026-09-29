import sys
import tempfile
import unittest
from datetime import timedelta
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
from app import ApiError, PharmacovigilanceService, iso, utcnow
import pv_states


class ReconciliationTest(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.db = Path(self.tmp.name) / "test.db"
        self.svc = PharmacovigilanceService(self.db)

    def tearDown(self):
        self.tmp.cleanup()

    def _case_report(self, serious=True, dedupe="d-1"):
        case = self.svc.create_case(
            "reporter-a", "reporter", "CN",
            {"patient_ref": "P-1", "region": "CN", "product": "DrugA", "event_term": "肝损伤",
             "source": "email", "dedupe_key": dedupe, "received_at": iso(utcnow()),
             "serious": serious},
        )["case"]
        report = self.svc.create_report(case["id"], "lead-cn", "regional_lead", "CN", {"country": "CN"})
        return case, report

    def _submit(self, report_id, role="admin", rrole="global_admin", region="", **body):
        return self.svc.submit_report(report_id, role, rrole, region, body)

    def _key_for(self, report_id, revision):
        row = self.svc.repo.conn.execute(
            "SELECT idempotency_key FROM report_messages WHERE report_id=? AND case_revision=?",
            (report_id, revision),
        ).fetchone()
        return row["idempotency_key"]

    # 1. 不可变报文 + 同一幂等键重试 + 重复提交不二次受理
    def test_immutable_message_and_idempotent_retry(self):
        case, report = self._case_report()
        # 第一次发送模拟网络超时
        r1 = self._submit(report["id"], gateway_mode="timeout")
        self.assertEqual(r1["attempt"], "timeout")
        self.assertEqual(r1["report"]["status"], pv_states.REPORT_SUBMITTED)
        self.assertTrue(r1["report"]["blocked"])
        self.assertEqual(r1["report"]["block_reason_code"], pv_states.BLOCK_AWAITING_RECEIPT)
        key1 = r1["message"]["idempotency_key"]
        self.assertEqual(r1["message"]["case_revision"], 1)

        # 超时重试沿用同一幂等键；监管侧此前可能已受理，本次只登记 duplicate
        r2 = self.svc.retry_report(report["id"], "lead-cn", "regional_lead", "CN")
        self.assertEqual(r2["attempt"], pv_states.ATTEMPT_DUPLICATE)
        self.assertEqual(r2["message"]["idempotency_key"], key1)
        submissions = self.svc.repo.conn.execute(
            "SELECT COUNT(*) AS n FROM regulator_submissions WHERE idempotency_key=?", (key1,)
        ).fetchone()["n"]
        self.assertEqual(submissions, 1)  # 监管侧绝不重复受理

    # 2. 受理回执被接受，状态变已完成
    def test_acceptance_receipt_completes_report(self):
        case, report = self._case_report()
        submitted = self._submit(report["id"], gateway_mode="timeout")
        key = submitted["message"]["idempotency_key"]
        res = self.svc.deliver_receipt("admin", "global_admin",
                                       {"idempotency_key": key, "decision": "accepted"})
        self.assertTrue(res["applied"])
        self.assertEqual(res["report"]["status"], pv_states.REPORT_ACCEPTED)
        self.assertFalse(res["report"]["blocked"])

    # 3. 案例修订后，迟到的旧版本受理回执必须被拒绝（页面仍显示阻塞，不误报完成）
    def test_stale_receipt_after_medical_review_rejected(self):
        case, report = self._case_report()
        submitted = self._submit(report["id"], gateway_mode="timeout")
        key = submitted["message"]["idempotency_key"]

        # 等待回执期间医学审核改判，产生修订；在途报文立即失效
        review = self.svc.medical_review(
            case["id"], "reviewer-1", "medical_reviewer",
            {"expected_revision": 1, "serious": True, "fatal": False,
             "causality": "related", "rationale": "检验结果支持", "received_at": iso(utcnow())},
        )
        self.assertEqual(review["reports_invalidated"], 1)
        detail = self.svc.get_case(case["id"], "global_admin", "")
        report_view = detail["reports"][0]
        self.assertEqual(report_view["status"], pv_states.REPORT_MANUAL_REVIEW)
        self.assertEqual(report_view["block_reason_code"], pv_states.BLOCK_REVISION_CHANGED)

        # 旧版本受理回执此时才回来，不能被接受
        res = self.svc.deliver_receipt("admin", "global_admin",
                                       {"idempotency_key": key, "decision": "accepted"})
        self.assertFalse(res["applied"])
        self.assertEqual(res["outcome"], "stale")
        detail = self.svc.get_case(case["id"], "global_admin", "")
        self.assertEqual(detail["reports"][0]["status"], pv_states.REPORT_MANUAL_REVIEW)

        # 人工复核后按当前版本重新上报，得到新幂等键与新报文
        resub = self.svc.resubmit_report(report["id"], "lead-cn", "regional_lead", "CN", {})
        self.assertNotEqual(resub["message"]["idempotency_key"], key)
        self.assertEqual(resub["message"]["case_revision"], 2)

    # 4. 案例修订使未决上报立即失效（pending/in_flight/failed 都失效）
    def test_unresolved_messages_invalidated_on_revision(self):
        case, report = self._case_report()
        self._submit(report["id"], gateway_mode="fail")
        self.assertEqual(self.svc.get_case(case["id"], "lead-cn", "CN")["reports"][0]["status"],
                         pv_states.REPORT_FAILED)
        follow = self.svc.add_followup(
            case["id"], "reporter-a", "reporter", "CN",
            {"content": "新的随访资料", "source": "email", "expected_revision": 1},
        )
        self.assertEqual(follow["reports_invalidated"], 1)
        msg = self.svc.repo.conn.execute(
            "SELECT status FROM report_messages ORDER BY id DESC LIMIT 1"
        ).fetchone()
        self.assertEqual(msg["status"], pv_states.MESSAGE_SUPERSEDED)

    # 5. 退回补件必须带原因和期限；页面展示原因/期限；未修订不能重报
    def test_returned_receipt_requires_reason_and_deadline(self):
        case, report = self._case_report()
        submitted = self._submit(report["id"])
        key = submitted["message"]["idempotency_key"]

        with self.assertRaises(ApiError) as ctx:
            self.svc.deliver_receipt("admin", "global_admin",
                                     {"idempotency_key": key, "decision": "returned"})
        self.assertEqual(ctx.exception.code, "return_reason_required")

        deadline = iso(utcnow() + timedelta(days=5))
        res = self.svc.deliver_receipt("admin", "global_admin", {
            "idempotency_key": key, "decision": "returned",
            "return_reason": "缺少批号和用药起止时间", "supplement_due_at": deadline,
        })
        self.assertTrue(res["applied"])
        view = res["report"]
        self.assertEqual(view["status"], pv_states.REPORT_RETURNED)
        self.assertEqual(view["block_detail"]["reason"], "缺少批号和用药起止时间")
        self.assertEqual(view["block_detail"]["supplement_due_at"], deadline)

        # 未形成新修订不能直接重报
        with self.assertRaises(ApiError) as ctx:
            self.svc.resubmit_report(report["id"], "lead-cn", "regional_lead", "CN", {})
        self.assertEqual(ctx.exception.code, "supplement_requires_revision")

        # 补齐随访 -> 新修订后可重报
        self.svc.add_followup(case["id"], "reporter-a", "reporter", "CN",
                              {"content": "批号 B123，用药 1/1-1/10", "source": "email",
                               "expected_revision": 1})
        resub = self.svc.resubmit_report(report["id"], "lead-cn", "regional_lead", "CN", {})
        self.assertEqual(resub["message"]["case_revision"], 2)

    # 6. 区域负责人只能处理本区域
    def test_regional_lead_scoped_to_region(self):
        case, report = self._case_report(dedupe="d-cn")
        # 本区域负责人可以发送
        ok = self.svc.submit_report(report["id"], "lead-cn", "regional_lead", "CN", {})
        self.assertEqual(ok["attempt"], "sent")
        # 其他区域负责人无权操作同一条报告
        with self.assertRaises(ApiError) as ctx:
            self.svc.retry_report(report["id"], "lead-us", "regional_lead", "US")
        self.assertEqual(ctx.exception.status, 403)
        # 其他区域队列看不到
        queue = self.svc.reconciliation_queue("regional_lead", "US")
        self.assertEqual(queue, [])
        # 本区域队列可以看到
        queue_cn = self.svc.reconciliation_queue("regional_lead", "CN")
        self.assertEqual([r["id"] for r in queue_cn], [report["id"]])

    # 7. 失败任务只能由全局管理员重放
    def test_failed_replay_global_admin_only(self):
        case, report = self._case_report()
        self._submit(report["id"], role="admin", rrole="global_admin", gateway_mode="fail")
        with self.assertRaises(ApiError) as ctx:
            self.svc.retry_report(report["id"], "lead-cn", "regional_lead", "CN")
        self.assertEqual(ctx.exception.code, "replay_required")
        replay = self.svc.replay_failed("admin", "global_admin", {"report_id": report["id"]})
        self.assertEqual(len(replay["replayed"]), 1)
        self.assertEqual(replay["replayed"][0]["attempt"], pv_states.ATTEMPT_SENT)
        # 区域负责人不能重放
        with self.assertRaises(ApiError) as ctx:
            self.svc.replay_failed("lead-cn", "regional_lead", {})
        self.assertEqual(ctx.exception.status, 403)

    # 8. 服务重启后未决记录继续对账，迟到回执被自动应用
    def test_pending_reconciled_after_restart(self):
        case, report = self._case_report()
        submitted = self._submit(report["id"], gateway_mode="timeout")
        key = submitted["message"]["idempotency_key"]

        # 监管侧在服务离线期间作出受理决定
        with self.svc.repo.tx() as conn:
            self.svc.gateway.issue_decision(
                conn, idempotency_key=key, decision="accepted", decided_at=iso())

        # 重启服务：构造函数自动对账
        svc2 = PharmacovigilanceService(self.db)
        view = svc2.get_case(case["id"], "global_admin", "")["reports"][0]
        self.assertEqual(view["status"], pv_states.REPORT_ACCEPTED)
        msg = svc2.repo.conn.execute("SELECT status FROM report_messages WHERE idempotency_key=?",
                                     (key,)).fetchone()
        self.assertEqual(msg["status"], pv_states.MESSAGE_ACCEPTED)

    # 9. 主动对账接口也可拉回结论
    def test_manual_reconcile_pulls_decision(self):
        case, report = self._case_report()
        submitted = self._submit(report["id"], gateway_mode="timeout")
        key = submitted["message"]["idempotency_key"]
        before = self.svc.reconcile("lead-cn", "regional_lead", "CN", report["id"])
        self.assertEqual(before["results"][0]["outcome"], "pending")
        with self.svc.repo.tx() as conn:
            self.svc.gateway.issue_decision(
                conn, idempotency_key=key, decision="accepted", decided_at=iso())
        after = self.svc.reconcile("lead-cn", "regional_lead", "CN", report["id"])
        self.assertEqual(after["results"][0]["outcome"], "applied")

    # 10. 重复受理回执幂等：终态报文再来一次不改变结论
    def test_duplicate_receipt_is_idempotent(self):
        case, report = self._case_report()
        key = self._submit(report["id"])["message"]["idempotency_key"]
        self.svc.deliver_receipt("admin", "global_admin",
                                 {"idempotency_key": key, "decision": "accepted"})
        again = self.svc.deliver_receipt("admin", "global_admin",
                                         {"idempotency_key": key, "decision": "accepted"})
        self.assertFalse(again["applied"])
        self.assertEqual(again["outcome"], "ignored")
        self.assertEqual(again["report"]["status"], pv_states.REPORT_ACCEPTED)


if __name__ == "__main__":
    unittest.main()
