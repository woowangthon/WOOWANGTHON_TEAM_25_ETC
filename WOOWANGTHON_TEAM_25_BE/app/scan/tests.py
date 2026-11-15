"""Scan API compatibility and partial-result behavior with every external call mocked."""

import os
import logging
from types import SimpleNamespace
from unittest.mock import MagicMock, patch

import httpx
from django.test import SimpleTestCase, override_settings
from openai import APITimeoutError, InternalServerError
from rest_framework.test import APIClient

from ai.services import PageContextAnalysis
from ai.tests import ADVANCE_TEXT, SIGNAL
from risk_engine.services import calculate_risk
from scan.services import run_scan


@override_settings(ALLOWED_HOSTS=["testserver"])
class ScanAPIPageContextTests(SimpleTestCase):
    def setUp(self):
        # These source/scoring tests intentionally avoid ORM access. Real cache
        # behavior and migrations are exercised by scan.test_cache.TestCase.
        cache_read = patch("scan.services.load_public_api_cache", side_effect=lambda *args, **kwargs: {"status": "miss", "analyses": {}})
        cache_write = patch("scan.services.store_public_api_cache", return_value=True)
        cache_read.start()
        cache_write.start()
        self.addCleanup(cache_read.stop)
        self.addCleanup(cache_write.stop)
        previous_level = logging.root.manager.disable
        logging.disable(logging.CRITICAL)
        self.addCleanup(logging.disable, previous_level)
        env = patch.dict(os.environ, {"OPENAI_API_KEY": "unit-test-key"}, clear=True)
        env.start()
        self.addCleanup(env.stop)
        self.client = APIClient()
        self.business = {
            "business_name": "테스트 상점",
            "representative_name": "홍길동",
            "business_number": "123-45-67890",
            "address": "서울 테스트로 1",
        }
        self.payload = {"url": "https://shop.example.com/products/1", "business_info": self.business}
        self.nts_result = {"status": "completed", "business_status": "폐업"}
        self.ftc_result = {
            "status": "completed",
            "search_method": "BUSINESS_NUMBER",
            "match_status": "MATCH",
            "verification": {
                "business_number": "MATCH",
                "business_name": "MISMATCH",
                "representative_name": "MATCH",
            },
        }
        nts = patch("scan.services.lookup_nts", return_value=self.nts_result)
        ftc = patch("scan.services.lookup_ftc", return_value=self.ftc_result)
        self.nts = nts.start()
        self.ftc = ftc.start()
        self.addCleanup(nts.stop)
        self.addCleanup(ftc.stop)
        sdk = patch("ai.services.OpenAI")
        self.sdk = sdk.start()
        self.addCleanup(sdk.stop)
        self.ai_client = MagicMock()
        self.sdk.return_value = self.ai_client
        self.ai_client.__enter__.return_value = self.ai_client
        self.set_ai_signals([])

    def set_ai_signals(self, signals):
        self.ai_client.responses.parse.return_value = SimpleNamespace(
            status="completed", output_parsed=PageContextAnalysis(signals=signals), output=[]
        )

    def post_scan(self, **extras):
        return self.client.post("/api/v1/scan/", {**self.payload, **extras}, format="json")

    def assert_existing_checks_preserved(self, response):
        self.assertEqual(response.status_code, 200)
        result = response.json()
        self.assertEqual(result["analyses"]["nts"], self.nts_result)
        self.assertEqual(result["analyses"]["ftc"], self.ftc_result)
        expected = calculate_risk({"nts": self.nts_result, "ftc": self.ftc_result})
        self.assertEqual(expected["risk_score"], 50)
        self.assertEqual(expected["score_breakdown"]["business"]["raw_score"], 55)
        self.assertEqual(result["score_breakdown"]["business"], expected["score_breakdown"]["business"])
        # Public checks retain their original 55 points as raw points, capped by the 50-point budget.
        self.assertEqual(result["risk_factors"][:len(expected["risk_factors"])], expected["risk_factors"])
        self.assertTrue(set(expected["checked_items"]).issubset(result["checked_items"]))
        ai_score = result["analyses"]["ai"]["risk_score"]
        self.assertEqual(result["risk_score"], 50 + (ai_score or 0))
        self.assertEqual(sum(factor["points"] for factor in result["risk_factors"]), result["risk_score"])
        self.assertEqual(result["verdict"], "HIGH_RISK" if result["risk_score"] >= 60 else "CAUTION")
        self.assertEqual(result["business_info"], self.business)
        self.assertEqual(result["domain"], "shop.example.com")
        self.assertIn("scan_id", result)
        return result

    def assert_ai_fields(self, actual, expected):
        for key, value in expected.items():
            if key == "signals":
                self.assertEqual(len(actual[key]), len(value))
                for signal, expected_signal in zip(actual[key], value):
                    for field, expected_value in expected_signal.items():
                        self.assertEqual(signal[field], expected_value)
            else:
                self.assertEqual(actual[key], value)
        self.assertEqual(actual["max_score"], 50)
        self.assertEqual(actual["scoring_policy"], "business50_ai50_v3")
        self.assertIn(actual["scoring_status"], ("completed", "rule_only", "unavailable"))

    def test_old_url_and_business_info_request_still_returns_checks(self):
        result = self.assert_existing_checks_preserved(self.post_scan())
        self.assert_ai_fields(result["analyses"]["ai"], {
            "status": "skipped", "reason": "NO_PAGE_CONTEXT", "signals": [],
        })
        self.sdk.assert_not_called()
        self.nts.assert_called_once_with(self.business["business_number"])
        self.ftc.assert_called_once_with(self.business)

    def test_url_only_request_still_supported(self):
        response = self.client.post("/api/v1/scan/", {"url": self.payload["url"]}, format="json")
        self.assertEqual(response.status_code, 200)
        result = response.json()
        self.assertEqual(result["business_info"], {})
        self.assertEqual(result["analyses"]["nts"]["status"], "skipped")
        self.assertEqual(result["analyses"]["ai"]["status"], "skipped")
        self.nts.assert_not_called()
        self.sdk.assert_not_called()

    def test_context_reaches_run_scan_unchanged(self):
        context = "  출금하려면 5만 원을 먼저 입금하세요\n"
        with patch("scan.views.run_scan", return_value={"status": "received"}) as scan:
            response = self.post_scan(page_context=context)
        self.assertEqual(response.status_code, 200)
        scan.assert_called_once_with(url=self.payload["url"], business_info=self.business, page_context=context)

    def test_optional_context_is_passed_as_empty_string(self):
        with patch("scan.views.run_scan", return_value={"status": "received"}) as scan:
            self.post_scan()
        self.assertEqual(scan.call_args.kwargs["page_context"], "")

    def test_maximum_2000_characters_is_accepted(self):
        response = self.post_scan(page_context="출금 신청" + "가" * 1995)
        self.assert_existing_checks_preserved(response)

    def test_context_longer_than_2000_characters_is_rejected_before_calls(self):
        response = self.post_scan(page_context="가" * 2001)
        self.assertEqual(response.status_code, 400)
        self.assertIn("page_context", response.json())
        self.nts.assert_not_called()
        self.ftc.assert_not_called()
        self.sdk.assert_not_called()

    def test_non_string_context_is_rejected(self):
        for context in (None, 123, 0.5, False, [], {}):
            with self.subTest(context=context):
                response = self.post_scan(page_context=context)
                self.assertEqual(response.status_code, 400)
                self.assertIn("page_context", response.json())
        self.sdk.assert_not_called()
        self.nts.assert_not_called()
        self.ftc.assert_not_called()

    def test_blank_context_skips_ai_and_returns_existing_checks(self):
        result = self.assert_existing_checks_preserved(self.post_scan(page_context=" \n "))
        self.assertEqual(result["analyses"]["ai"]["status"], "skipped")
        self.sdk.assert_not_called()

    def test_no_api_key_skips_ai_but_returns_existing_checks(self):
        os.environ.pop("OPENAI_API_KEY")
        result = self.assert_existing_checks_preserved(self.post_scan(page_context=ADVANCE_TEXT))
        self.assert_ai_fields(result["analyses"]["ai"], {
            "status": "skipped", "reason": "MISSING_API_KEY", "signals": [],
        })
        self.sdk.assert_not_called()

    def test_upfront_signal_is_recorded_with_ai_score_and_total(self):
        self.set_ai_signals([SIGNAL])
        result = self.assert_existing_checks_preserved(self.post_scan(page_context=ADVANCE_TEXT))
        self.assert_ai_fields(result["analyses"]["ai"], {"status": "completed", "signals": [SIGNAL]})
        self.assertNotIn("page_context", result)
        options = self.ai_client.responses.parse.call_args.kwargs
        self.assertIn(ADVANCE_TEXT, options["input"])
        self.assertNotIn(self.payload["url"], options["input"])
        self.assertNotIn(self.business["business_name"], options["input"])

    def test_menu_label_and_fee_deduction_return_no_mocked_signals(self):
        for context in ("출금 신청", "판매대금에서 수수료를 차감하고 지급합니다."):
            with self.subTest(context=context):
                result = self.assert_existing_checks_preserved(self.post_scan(page_context=context))
                self.assert_ai_fields(result["analyses"]["ai"], {"status": "completed", "signals": []})

    def test_hallucinated_evidence_is_not_warning_evidence(self):
        self.set_ai_signals([{**SIGNAL, "evidence": "입력에 없는 선입금 안내문"}])
        result = self.assert_existing_checks_preserved(self.post_scan(page_context=ADVANCE_TEXT))
        self.assert_ai_fields(result["analyses"]["ai"], {
            "status": "error", "error": "INVALID_AI_RESPONSE", "signals": [],
        })

    def test_openai_timeout_preserves_all_existing_results(self):
        self.ai_client.responses.parse.side_effect = APITimeoutError(request=httpx.Request("POST", "https://api.openai.com/v1/responses"))
        result = self.assert_existing_checks_preserved(self.post_scan(page_context=ADVANCE_TEXT))
        self.assert_ai_fields(result["analyses"]["ai"], {
            "status": "error", "error": "OPENAI_TIMEOUT", "signals": [],
        })

    def test_openai_api_error_preserves_all_existing_results(self):
        self.ai_client.responses.parse.side_effect = InternalServerError(
            "mock unavailable",
            response=httpx.Response(503, request=httpx.Request("POST", "https://api.openai.com/v1/responses")),
            body={"error": "unavailable"},
        )
        result = self.assert_existing_checks_preserved(self.post_scan(page_context=ADVANCE_TEXT))
        self.assert_ai_fields(result["analyses"]["ai"], {
            "status": "error", "error": "OPENAI_API_ERROR", "signals": [],
        })

    def test_invalid_ai_output_preserves_all_existing_results(self):
        self.ai_client.responses.parse.return_value = SimpleNamespace(status="completed", output_parsed=None)
        result = self.assert_existing_checks_preserved(self.post_scan(page_context=ADVANCE_TEXT))
        self.assert_ai_fields(result["analyses"]["ai"], {
            "status": "error", "error": "INVALID_AI_RESPONSE", "signals": [],
        })

    def test_unexpected_ai_failure_is_isolated_from_existing_checks(self):
        with patch("scan.services.analyze_page_context", side_effect=RuntimeError("mock unexpected failure")):
            result = self.assert_existing_checks_preserved(self.post_scan(page_context=ADVANCE_TEXT))
        self.assert_ai_fields(result["analyses"]["ai"], {
            "status": "error", "error": "AI_ANALYSIS_ERROR", "signals": [],
        })

    def test_legacy_direct_run_scan_signature_remains_supported(self):
        result = run_scan(self.payload["url"], self.business)
        self.assertEqual(result["analyses"]["ai"]["status"], "skipped")
        self.assertEqual(result["risk_score"], 50)
        self.sdk.assert_not_called()
