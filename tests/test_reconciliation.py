"""上报对账测试：不可变报文、幂等重试、回执版本判定、失效人工复核、权限与重启恢复。"""
import sys
import tempfile
import unittest
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
from app import ApiError, PharmacovigilanceService, iso, utcnow


class ReconciliationTest(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.db = Path(self.tmp.name) / "test.db"
        self.svc = PharmacovigilanceService(self.db)

    def tearDown(self):
        self.tmp.cleanup()

    def make_case(self, dedupe="intake-1", region="CN"):
        return self.svc.create_case(
            "reporter-a", "reporter", region,
            {"patient_ref": "P-1", "region": region, "product": "DrugA", "event_term": "肝损伤",
             "source": "email", "dedupe_key": dedupe, "received_at": iso(utcnow()), "serious": False},
        )["case"]

    def make_report(self, case_id, region="CN", country="CN"):
        return self.svc.create_report(case_id, "lead-cn", "regional_lead", region, {"country": country})

    def followup(self, case, content="随访补充"):
        return self.svc.add_followup(
            case["id"], "reporter-a", "reporter", "CN",
            {"content": content, "source": "phone", "expected_revision": case["revision"],
             "received_at": iso(utcnow())},
        )["case"]

    def review(self, case):
        return self.svc.medical_review(
            case["id"], "reviewer-1", "medical_reviewer",
            {"expected_revision": case["revision"], "serious": True, "fatal": False,
             "causality": "possibly_related", "rationale": "医学审核修订", "received_at": iso(utcnow())},
        )["case"]

    def submit(self, report, timeout=False, actor="lead-cn", role="regional_lead", region="CN"):
        return self.svc.submit_report(report["id"], actor, role, region, {"timeout": timeout})

    def receipt(self, report, key, outcome="accepted", **extra):
        body = {"idempotency_key": key, "outcome": outcome, "received_at": iso(utcnow())}
        body.update(extra)
        return self.svc.recon.receive_receipt(report["id"], "gateway", "global_admin", body)

    # ---------- 报文与幂等重试 ----------

    def test_message_built_per_revision_and_retry_keeps_key(self):
        case = self.make_case()
        report = self.make_report(case["id"])
        failed = self.submit(report, timeout=True)
        self.assertEqual(failed["task"]["status"], "failed")
        self.assertEqual(failed["task"]["attempts"], 1)
        key = failed["message"]["idempotency_key"]
        self.assertTrue(key.startswith("pv-idem-"))

        replayed = self.svc.recon.replay(report["id"], "admin", "global_admin", {})
        self.assertEqual(replayed["task"]["status"], "reconciling")
        self.assertEqual(replayed["task"]["attempts"], 2)
        self.assertEqual(replayed["message"]["idempotency_key"], key, "重试必须沿用同一幂等键")
        self.assertEqual(replayed["message"]["id"], failed["message"]["id"], "重试不生成新报文")

        messages = self.svc.recon.list_messages(report["id"], "global_admin", "")
        self.assertEqual(len(messages), 1)
        self.assertEqual(messages[0]["case_revision"], 1)
        self.assertEqual(messages[0]["attempt"], 1)

    def test_payload_is_immutable_snapshot(self):
        case = self.make_case()
        report = self.make_report(case["id"])
        submitted = self.submit(report)
        payload = submitted["message"]["payload_json"]
        self.assertIn('"case_revision": 1', payload)
        self.followup(case)
        messages = self.svc.recon.list_messages(report["id"], "global_admin", "")
        self.assertEqual(len(messages), 1)
        self.assertIn('"case_revision": 1', messages[0]["payload_json"], "报文内容不可随案例改变")

    # ---------- 回执版本判定 ----------

    def test_accepted_receipt_matching_version_completes(self):
        case = self.make_case()
        report = self.make_report(case["id"])
        submitted = self.submit(report)
        key = submitted["message"]["idempotency_key"]
        result = self.receipt(report, key)
        self.assertEqual(result["receipt"]["outcome"], "accepted")
        self.assertEqual(result["receipt"]["disposition"], "applied")
        self.assertEqual(result["task"]["status"], "accepted")

        again = self.receipt(report, key)
        self.assertTrue(again["idempotent"], "重复回执幂等忽略")
        tasks = self.svc.recon.list_tasks("global_admin", "")
        self.assertEqual(len(tasks), 1)
        self.assertEqual(tasks[0]["task_status"], "accepted")

    def test_rejected_receipt_requires_reason_and_deadline(self):
        case = self.make_case()
        report = self.make_report(case["id"])
        submitted = self.submit(report)
        key = submitted["message"]["idempotency_key"]
        with self.assertRaises(ApiError) as ctx:
            self.receipt(report, key, outcome="rejected")
        self.assertEqual(ctx.exception.code, "reason_and_deadline_required")

        result = self.receipt(
            report, key, outcome="rejected",
            reason="缺少用药起止时间", supplement_due_at="2026-10-15T00:00:00Z",
        )
        self.assertEqual(result["task"]["status"], "rejected")
        tasks = self.svc.recon.list_tasks("global_admin", "")
        self.assertIn("缺少用药起止时间", tasks[0]["blocking_reason"])
        self.assertIn("2026-10-15", tasks[0]["blocking_reason"])

    def test_stale_receipt_after_followup_is_ignored_and_invalidates(self):
        case = self.make_case()
        report = self.make_report(case["id"])
        submitted = self.submit(report)
        key = submitted["message"]["idempotency_key"]
        self.followup(case)

        tasks = self.svc.recon.list_tasks("global_admin", "")
        self.assertEqual(tasks[0]["task_status"], "invalid", "案例变化后未决上报立即失效")
        self.assertEqual(tasks[0]["message_revision"], 1)
        self.assertEqual(tasks[0]["case_revision"], 2)
        self.assertIn("人工复核", tasks[0]["blocking_reason"])

        with self.assertRaises(ApiError) as ctx:
            self.receipt(report, key)
        self.assertEqual(ctx.exception.code, "stale_receipt")
        receipts = self.svc.recon.list_receipts(report["id"], "global_admin", "")
        self.assertEqual(receipts[0]["disposition"], "stale_ignored", "旧报文回执只记录不生效")

    def test_stale_receipt_after_medical_review(self):
        case = self.make_case()
        report = self.make_report(case["id"])
        submitted = self.submit(report)
        self.review(case)
        tasks = self.svc.recon.list_tasks("global_admin", "")
        self.assertEqual(tasks[0]["task_status"], "invalid")
        self.assertEqual(tasks[0]["case_revision"], 2)

    # ---------- 人工复核 ----------

    def test_manual_review_close_and_resubmit(self):
        case = self.make_case()
        report = self.make_report(case["id"])
        self.submit(report)
        self.followup(case)

        closed = self.svc.recon.manual_review(report["id"], "lead-cn", "regional_lead", "CN", {"decision": "close"})
        self.assertEqual(closed["task"]["status"], "closed")

        case2 = self.make_case("intake-2")
        report2 = self.make_report(case2["id"])
        submitted2 = self.submit(report2)
        key2_old = submitted2["message"]["idempotency_key"]
        self.followup(case2)
        resubmitted = self.svc.recon.manual_review(
            report2["id"], "lead-cn", "regional_lead", "CN", {"decision": "resubmit"})
        self.assertEqual(resubmitted["task"]["status"], "reconciling")
        self.assertNotEqual(resubmitted["message"]["idempotency_key"], key2_old, "重新上报启用新幂等键")
        self.assertEqual(resubmitted["message"]["case_revision"], 2)

    def test_submit_blocked_when_invalid(self):
        case = self.make_case()
        report = self.make_report(case["id"])
        self.submit(report)
        self.followup(case)
        with self.assertRaises(ApiError) as ctx:
            self.submit(report)
        self.assertEqual(ctx.exception.code, "invalid_needs_manual_review")

    # ---------- 权限 ----------

    def test_replay_requires_global_admin(self):
        case = self.make_case()
        report = self.make_report(case["id"])
        self.submit(report, timeout=True)
        with self.assertRaises(ApiError) as ctx:
            self.svc.recon.replay(report["id"], "lead-cn", "regional_lead", {})
        self.assertEqual(ctx.exception.status, 403)
        replayed = self.svc.recon.replay(report["id"], "admin", "global_admin", {})
        self.assertEqual(replayed["task"]["status"], "reconciling")

    def test_regional_lead_only_sees_own_region(self):
        case_cn = self.make_case("intake-cn", "CN")
        report_cn = self.make_report(case_cn["id"], "CN", "CN")
        self.submit(report_cn)
        case_us = self.make_case("intake-us", "US")
        report_us = self.svc.create_report(case_us["id"], "lead-us", "regional_lead", "US", {"country": "US"})
        self.svc.submit_report(report_us["id"], "lead-us", "regional_lead", "US", {})

        cn_tasks = self.svc.recon.list_tasks("regional_lead", "CN")
        self.assertEqual(len(cn_tasks), 1)
        self.assertEqual(cn_tasks[0]["region"], "CN")
        all_tasks = self.svc.recon.list_tasks("global_admin", "")
        self.assertEqual(len(all_tasks), 2)
        with self.assertRaises(ApiError):
            self.svc.recon.list_messages(report_us["id"], "regional_lead", "CN")

    def test_replay_after_case_change_goes_to_manual_review(self):
        case = self.make_case()
        report = self.make_report(case["id"])
        self.submit(report, timeout=True)
        self.followup(case)
        with self.assertRaises(ApiError) as ctx:
            self.svc.recon.replay(report["id"], "admin", "global_admin", {})
        self.assertEqual(ctx.exception.code, "case_changed_manual_review")
        tasks = self.svc.recon.list_tasks("global_admin", "")
        self.assertEqual(tasks[0]["task_status"], "invalid")

    # ---------- 重启恢复 ----------

    def test_pending_reconciliation_continues_after_restart(self):
        case = self.make_case()
        report = self.make_report(case["id"])
        submitted = self.submit(report)
        self.assertEqual(submitted["task"]["status"], "reconciling")

        restarted = PharmacovigilanceService(self.db)
        resumed = restarted.recon.resume_pending()
        self.assertEqual(len(resumed), 1, "重启后未决记录继续对账")
        tasks = restarted.recon.list_tasks("global_admin", "")
        self.assertEqual(tasks[0]["task_status"], "reconciling")
        self.assertEqual(tasks[0]["idempotency_key"], submitted["message"]["idempotency_key"])

        result = restarted.recon.receive_receipt(
            report["id"], "gateway", "global_admin",
            {"idempotency_key": submitted["message"]["idempotency_key"],
             "outcome": "accepted", "received_at": iso(utcnow())},
        )
        self.assertEqual(result["task"]["status"], "accepted")


if __name__ == "__main__":
    unittest.main()
