"""Optional site observations through POST /scan/, with external I/O blocked."""

import logging
from unittest.mock import patch

from django.test import SimpleTestCase, override_settings
from rest_framework.test import APIClient

from ai.test_site_scoring import NOW, notices, record
from ai.test_scoring import FEE


@override_settings(ALLOWED_HOSTS=["testserver"])
class ScanSiteChecksTests(SimpleTestCase):
    def setUp(self):
        previous = logging.root.manager.disable
        logging.disable(logging.CRITICAL)
        self.addCleanup(logging.disable, previous)
        self.client = APIClient()
        self.url = "https://shop.example.com/"
        self.business = {"business_number": "123-45-67890", "business_name": "합성상점"}
        patches = {
            "cache_read": patch("scan.services.load_public_api_cache", return_value={"status": "miss", "analyses": {}}),
            "cache_write": patch("scan.services.store_public_api_cache", return_value=True),
            "nts": patch("scan.services.lookup_nts", return_value={"status": "completed", "business_status": "ACTIVE"}),
            "ftc": patch("scan.services.lookup_ftc", return_value={"status": "completed", "search_method": "BUSINESS_NUMBER", "match_status": "SINGLE_CANDIDATE", "verification": {"business_number": "MATCH", "business_name": "MATCH", "representative_name": "MATCH"}}),
            "analyze": patch("scan.services.analyze_page_context", return_value={"status": "skipped", "reason": "NO_PAGE_CONTEXT", "signals": []}),
            "clock": patch("ai.site_checks.timezone.now", return_value=NOW),
            "requests": patch("requests.sessions.Session.request", side_effect=AssertionError("External HTTP disabled")),
            "httpx": patch("httpx.HTTPTransport.handle_request", side_effect=AssertionError("External HTTP disabled")),
        }
        for name, patcher in patches.items():
            setattr(self, name, patcher.start())
            self.addCleanup(patcher.stop)

    def post(self, checks=None, **extra):
        payload = {"url": self.url, "business_info": self.business, **extra}
        if checks is not None:
            payload["site_checks"] = checks
        return self.client.post("/api/v1/scan/", payload, format="json")

    def assert_success(self, response, total):
        self.assertEqual(response.status_code, 200, response.content)
        data = response.json()
        self.assertEqual(data["risk_score"], total)
        self.assertEqual(sum(f["points"] for f in data["risk_factors"]), total)
        self.assertEqual(data["score_breakdown"]["ai"]["max_score"], 50)
        self.assertEqual(data["scoring_policy"], "business50_ai50_v3")
        return data

    def test_menu_on_home_scores_forty_even_if_both_boards_are_login_only(self):
        checks = {"payment_menus": record("present", "출금신청 · 충전 신청"),
                  "notices": record("unverified", "로그인이 필요합니다.", reason="login_required"),
                  "reviews": record("unverified", "로그인이 필요합니다.", reason="login_required")}
        data = self.assert_success(self.post(checks), 40)
        self.assertEqual(data["analyses"]["ai"]["status"], "skipped")
        self.assertEqual(data["analyses"]["ai"]["scoring_status"], "rule_only")
        self.assertEqual(data["analyses"]["ai"]["signals"], [])
        self.assertEqual(data["analyses"]["ai"]["site_checks"]["payment_menus"]["status"], "present")
        self.assertEqual(data["verdict"], "CAUTION")

    def test_missing_notice_and_reviews_raw_ninety_only_add_ai_fifty(self):
        data = self.assert_success(self.post({"notices": record("missing", "공개 화면에서 공지사항을 찾지 못했습니다."),
                                              "reviews": record("empty", "구매후기가 없습니다.")}), 50)
        ai = data["analyses"]["ai"]
        self.assertEqual(ai["raw_score"], 90)
        self.assertIs(ai["capped"], True)
        self.assertEqual(data["score_breakdown"]["ai"]["raw_score"], 90)
        self.assertIs(data["score_breakdown"]["ai"]["capped"], True)
        self.assertEqual(data["score_breakdown"]["business"]["score"], 0)
        self.assertEqual(data["verdict"], "CAUTION")

    def test_existing_business_pair_fifty_plus_site_ai_fifty_make_one_hundred(self):
        self.ftc.return_value = {"status": "completed", "search_method": "BUSINESS_NAME", "match_status": "NO_MATCH"}
        data = self.assert_success(self.post({"notices": record("empty", "공지사항이 없습니다.")},
                                             business_info={"business_name": "합성상점", "displayed_business_number": "125-86-112516"}), 100)
        self.assertEqual(data["score_breakdown"]["business"]["score"], 50)
        self.assertEqual(data["score_breakdown"]["ai"]["score"], 50)
        self.assertEqual(data["verdict"], "HIGH_RISK")
        self.nts.assert_not_called()

    def test_model_error_does_not_discard_site_facts_or_public_results(self):
        self.analyze.return_value = {"status": "error", "error": "OPENAI_TIMEOUT", "signals": []}
        data = self.assert_success(self.post({"payment_menus": record("present", "출금신청")}), 40)
        self.assertEqual(data["analyses"]["ai"]["error"], "OPENAI_TIMEOUT")
        self.assertEqual(data["analyses"]["ai"]["scoring_status"], "rule_only")
        self.assertEqual(data["analyses"]["nts"]["status"], "completed")
        self.assertEqual(data["analyses"]["ftc"]["status"], "completed")

    def test_omitting_site_checks_preserves_existing_request_and_unknown_ai(self):
        data = self.assert_success(self.post(), 0)
        self.assertIsNone(data["analyses"]["ai"]["risk_score"])
        self.assertEqual(data["analyses"]["ai"]["scoring_status"], "unavailable")
        self.assertFalse(data["analyses"]["ai"]["risk_factors"])
        self.assertIsNone(data["score_breakdown"]["ai"]["raw_score"])
        self.assertIs(data["score_breakdown"]["ai"]["capped"], False)

    def test_unknown_site_criteria_keep_global_status_partial_at_zero_and_positive_scores(self):
        self.analyze.return_value = {"status": "completed", "signals": []}
        variants = [
            ({"payment_menus": record("absent", "출금·충전 신청 메뉴를 찾지 못했습니다."),
              "notices": record("unverified", "공지 날짜를 확인하지 못했습니다."),
              "reviews": record("present", "구매후기 1건")}, 0),
            ({"payment_menus": record("present", "출금신청"),
              "notices": record("empty", "공지사항이 없습니다."),
              "reviews": record("unverified", "로그인이 필요합니다.", reason="login_required")}, 50),
        ]
        for checks, total in variants:
            with self.subTest(total=total):
                data = self.assert_success(self.post(checks, page_context=FEE), total)
                self.assertEqual(data["analyses"]["ai"]["scoring_status"], "completed")
                self.assertEqual(data["analyses"]["ai"]["site_checks_status"], "partial")
                self.assertEqual(data["score_status"], "partial")
                self.assertNotEqual(data["verdict"], "LOW_RISK")

    def test_all_checked_site_criteria_and_completed_model_have_completed_status(self):
        self.analyze.return_value = {"status": "completed", "signals": []}
        checks = {"payment_menus": record("absent", "출금·충전 신청 메뉴를 찾지 못했습니다."),
                  "notices": notices("2026-11-14"), "reviews": record("present", "구매후기 1건")}
        data = self.assert_success(self.post(checks, page_context=FEE), 0)
        self.assertEqual(data["analyses"]["ai"]["site_checks_status"], "completed")
        self.assertEqual(data["score_status"], "completed")

    def test_current_calendar_month_boundary_and_old_notice_through_api(self):
        old = self.assert_success(self.post({"notices": notices("2026-10-14")}), 50)
        self.assertEqual(old["analyses"]["ai"]["site_checks"]["notices"]["freshness"], "stale")
        recent = self.assert_success(self.post({"notices": notices("2026-10-15")}), 0)
        self.assertEqual(recent["analyses"]["ai"]["site_checks"]["notices"]["freshness"], "recent")
        self.assertFalse(recent["analyses"]["ai"]["risk_factors"])

    def test_cross_origin_credentials_or_invalid_timestamp_rejected_before_any_lookup(self):
        for changes in ({"source_url": "https://other.example.com/notices"},
                        {"source_url": "https://user:secret@shop.example.com/notices"},
                        {"source_url": "http://shop.example.com/notices"},
                        {"observed_at": "2026-11-15T03:00:00"},
                        {"observed_at": "not-a-time"}):
            with self.subTest(changes=changes):
                self.nts.reset_mock(); self.ftc.reset_mock(); self.analyze.reset_mock()
                response = self.post({"notices": {**record("empty", "공지사항이 없습니다."), **changes}})
                self.assertEqual(response.status_code, 400, response.content)
                self.nts.assert_not_called(); self.ftc.assert_not_called(); self.analyze.assert_not_called()

    def test_strict_record_shapes_types_and_bounds(self):
        good = record("empty", "공지사항이 없습니다.")
        variants = [None, [], "missing", 50,
                    {**good, "status": "absent"}, {**good, "scope": "history"},
                    {**good, "evidence": "x" * 1001}, {**good, "evidence": 123},
                    {**good, "reason": "x" * 81}, {**good, "latest_date_confirmed": "yes"},
                    {**good, "dates": ["2026-01-01"] * 51}, {**good, "dates": ["x" * 33]},
                    {**good, "dates": [123]}, {**good, "invented": True}]
        for value in variants:
            with self.subTest(value=str(value)[:120]):
                response = self.post({"notices": value})
                self.assertEqual(response.status_code, 400, response.content)

    def test_required_record_fields_cannot_be_omitted(self):
        for key in ("status", "source_url", "observed_at", "scope"):
            value = record("empty", "공지사항이 없습니다.")
            value.pop(key)
            with self.subTest(key=key):
                response = self.post({"notices": value})
                self.assertEqual(response.status_code, 400, response.content)

    def test_site_checks_must_be_an_object_and_empty_object_remains_unknown(self):
        for value in (None, [], "missing", 50):
            with self.subTest(value=value):
                response = self.client.post("/api/v1/scan/", {
                    "url": self.url, "site_checks": value,
                }, format="json")
                self.assertEqual(response.status_code, 400, response.content)
        data = self.assert_success(self.post({}), 0)
        self.assertIsNone(data["analyses"]["ai"]["risk_score"])

    def test_unknown_top_level_site_key_and_dates_on_other_criteria_are_rejected(self):
        cases = [{"extra": record("empty", "없음")},
                 {"reviews": {**record("empty", "구매후기가 없습니다."), "dates": ["2026-01-01"]}},
                 {"payment_menus": {**record("present", "출금신청"), "dates": []}}]
        for checks in cases:
            with self.subTest(checks=checks):
                response = self.post(checks)
                self.assertEqual(response.status_code, 400, response.content)

    def test_request_snapshot_is_not_persisted_in_public_cache_payload(self):
        checks = {"reviews": record("empty", "구매후기가 없습니다.")}
        self.assert_success(self.post(checks), 40)
        payload = self.cache_write.call_args.args[2]
        self.assertNotIn("ai", payload)
        self.assertNotIn("site_checks", payload)
        self.assertNotIn("reviews", str(payload))
