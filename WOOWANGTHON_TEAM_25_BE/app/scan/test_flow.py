"""Observation API validation and isolated partial failures; no outbound calls."""

import json
import logging
import os
from datetime import datetime
from unittest.mock import patch

import httpx
from django.test import SimpleTestCase, override_settings
from openai import OpenAI
from rest_framework.test import APIClient

from ai.test_flow import DIRECT_RULE_TEXT, NOW, TAX_TEXT, observation, sourced_signal
from ai.tests import response_body
from risk_engine.services import calculate_risk
from scan.services import run_scan


@override_settings(ALLOWED_HOSTS=["testserver"])
class ScanObservationFlowTests(SimpleTestCase):
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
            "business_name": "합성 테스트 상점",
            "representative_name": "테스트 대표",
            "business_number": "123-45-67890",
            "address": "테스트시 테스트로 1",
        }
        self.payload = {"url": "https://shop.example.com/products/1", "business_info": self.business}
        self.nts_result = {"status": "completed", "business_status": "폐업"}
        self.ftc_result = {
            "status": "completed", "search_method": "BUSINESS_NUMBER", "match_status": "MATCH",
            "verification": {"business_number": "MATCH", "business_name": "MISMATCH",
                             "representative_name": "MATCH"},
        }
        nts = patch("scan.services.lookup_nts", return_value=self.nts_result)
        ftc = patch("scan.services.lookup_ftc", return_value=self.ftc_result)
        self.nts, self.ftc = nts.start(), ftc.start()
        self.addCleanup(nts.stop)
        self.addCleanup(ftc.stop)
        self.requests, self.sdk_options = [], []
        self.response_payload = response_body()
        self.response_status = 200
        self.response_timeout = False

        def handler(request):
            self.assertEqual(request.url.path, "/v1/responses")
            self.requests.append(json.loads(request.content))
            if self.response_timeout:
                raise httpx.ReadTimeout("mock timeout", request=request)
            return httpx.Response(self.response_status, json=self.response_payload)

        def create_client(**kwargs):
            self.sdk_options.append(kwargs)
            client = OpenAI(
                **kwargs,
                http_client=httpx.Client(transport=httpx.MockTransport(handler)),
            )
            self.addCleanup(client.close)
            return client

        sdk = patch("ai.services.OpenAI", side_effect=create_client)
        self.sdk = sdk.start()
        self.addCleanup(sdk.stop)

    def post_scan(self, **extras):
        return self.client.post("/api/v1/scan/", {**self.payload, **extras}, format="json")

    def assert_preserved(self, response):
        self.assertEqual(response.status_code, 200, response.content)
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

    def assert_invalid_before_calls(self, observations):
        response = self.post_scan(page_observations=observations)
        self.assertEqual(response.status_code, 400, response.content)
        self.assertIn("page_observations", response.json())
        self.nts.assert_not_called()
        self.ftc.assert_not_called()
        self.sdk.assert_not_called()

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

    def test_observations_reach_scan_unchanged_and_optional_context_is_compatible(self):
        observations = [observation("  出金 / 출금 신청\n")]
        with patch("scan.views.run_scan", return_value={"status": "received"}) as scan:
            response = self.post_scan(page_observations=observations, page_context="기존 안내문")
        self.assertEqual(response.status_code, 200)
        self.assertEqual(scan.call_args.kwargs["page_observations"], observations)
        self.assertEqual(scan.call_args.kwargs["page_context"], "기존 안내문")
        self.assertEqual(scan.call_args.kwargs["url"], self.payload["url"])

    def test_verified_ai_source_and_risk_engine_score_are_returned_together(self):
        source = observation(TAX_TEXT, id="linked-tax", kind="linked_policy", path="/policies/withdraw")
        self.response_payload = response_body([sourced_signal(TAX_TEXT, source_id=source["id"])])
        result = self.assert_preserved(self.post_scan(page_observations=[source]))
        ai = result["analyses"]["ai"]
        self.assertEqual(ai["status"], "completed")
        self.assertEqual(ai["assessment"]["state"], "advance_payment_required")
        matching = [s for s in ai["signals"] if s["type"] == "ADVANCE_PAYMENT_REQUIRED"]
        self.assertEqual(len(matching), 1)
        signal = matching[0]
        self.assertEqual(signal["source_id"], source["id"])
        self.assertEqual(signal["source_url"], source["source_url"])
        self.assertEqual(signal["kind"], "linked_policy")
        self.assertEqual(datetime.fromisoformat(signal["observed_at"].replace("Z", "+00:00")),
                         datetime.fromisoformat(NOW.replace("Z", "+00:00")))
        self.assertIn(signal["evidence"], source["text"])
        self.assertEqual(len(self.requests), 1)
        self.assertNotIn(self.business["business_name"], self.requests[0]["input"])
        self.assertNotIn(source["source_url"], self.requests[0]["input"])

    def test_timeout_keeps_capped_business_score_and_rule_warning(self):
        self.response_timeout = True
        source = observation(DIRECT_RULE_TEXT, kind="linked_policy")
        result = self.assert_preserved(self.post_scan(page_observations=[source]))
        ai = result["analyses"]["ai"]
        self.assertEqual(ai["status"], "error")
        self.assertEqual(ai["error"], "OPENAI_TIMEOUT")
        self.assertEqual(ai["assessment"]["state"], "advance_payment_required")
        self.assertTrue(any(s["type"] == "ADVANCE_PAYMENT_REQUIRED" and s["source"] == "rule"
                            and s["source_url"] == source["source_url"] for s in ai["signals"]))
        self.assertEqual(len(self.requests), 1)

    def test_missing_key_keeps_existing_checks_and_unknown_status(self):
        os.environ.pop("OPENAI_API_KEY")
        result = self.assert_preserved(self.post_scan(page_observations=[observation("출금 신청")]))
        ai = result["analyses"]["ai"]
        self.assertEqual(ai["status"], "skipped")
        self.assertEqual(ai["reason"], "MISSING_API_KEY")
        self.assertEqual(ai["assessment"]["withdrawal_status"], "unverified")
        self.assertEqual(ai["assessment"]["state"], "unverified")
        self.sdk.assert_not_called()

    def test_api_error_preserves_existing_checks_without_fabricated_ai_evidence(self):
        self.response_status = 503
        self.response_payload = {"error": {"message": "mock service unavailable", "type": "server_error"}}
        result = self.assert_preserved(self.post_scan(page_observations=[observation("出金 / 출금 신청")]))
        ai = result["analyses"]["ai"]
        self.assertEqual(ai["status"], "error")
        self.assertEqual(ai["error"], "OPENAI_API_ERROR")
        self.assertEqual(ai["assessment"]["withdrawal_status"], "unverified")
        self.assertEqual(len(self.requests), 1)

    def test_hallucinated_evidence_is_not_returned_but_other_checks_remain(self):
        invented = "출금하려면 100만 원을 먼저 송금하세요."
        self.response_payload = response_body([sourced_signal(invented)])
        result = self.assert_preserved(self.post_scan(page_observations=[observation("상품 구매에 사용할 포인트를 충전하세요.")]))
        ai = result["analyses"]["ai"]
        self.assertEqual(ai["status"], "error")
        self.assertEqual(ai["error"], "INVALID_AI_RESPONSE")
        self.assertFalse(any(s["evidence"] == invented for s in ai["signals"]))
        self.assertEqual(ai["assessment"]["withdrawal_status"], "unverified")

    def test_unexpected_analysis_failure_still_returns_unknown_assessment_and_checks(self):
        with patch("scan.services.analyze_page_context", side_effect=RuntimeError("mock unexpected error")):
            result = self.assert_preserved(self.post_scan(page_observations=[observation("출금 신청")]))
        ai = result["analyses"]["ai"]
        self.assertEqual(ai["status"], "error")
        self.assertEqual(ai["error"], "AI_ANALYSIS_ERROR")
        self.assertEqual(ai["assessment"]["state"], "unverified")
        self.assertEqual(ai["assessment"]["withdrawal_status"], "unverified")

    def test_unexpected_analysis_failure_keeps_direct_rule_evidence_and_checks(self):
        source = observation(DIRECT_RULE_TEXT, id="withdrawal-policy", kind="linked_policy")
        with patch("scan.services.analyze_page_context", side_effect=RuntimeError("mock unexpected error")):
            result = self.assert_preserved(self.post_scan(page_observations=[source]))
        ai = result["analyses"]["ai"]
        self.assertEqual(ai["status"], "error")
        self.assertEqual(ai["error"], "AI_ANALYSIS_ERROR")
        self.assertEqual(ai["assessment"]["state"], "advance_payment_required")
        self.assertTrue(any(s["type"] == "ADVANCE_PAYMENT_REQUIRED" and s["source"] == "rule"
                            and s["source_url"] == source["source_url"] for s in ai["signals"]))

    def test_multiple_stage_observations_are_preserved_when_model_fails(self):
        self.response_timeout = True
        sources = [observation("후기 게시물을 작성하면 보상금이 적립됩니다.", id="reward", kind="history"),
                   observation("현재 수익금 잔액은 300,000원입니다.", id="earnings", kind="history"),
                   observation("현재 출금이 제한되어 있습니다.", id="restricted")]
        ai = self.assert_preserved(self.post_scan(page_observations=sources))["analyses"]["ai"]
        self.assertEqual(ai["status"], "error")
        self.assertEqual(set(ai["assessment"]["observed_stages"]),
                         {"REWARD_OFFER", "EARNINGS_DISPLAY", "WITHDRAWAL_RESTRICTED"})
        self.assertEqual(ai["assessment"]["state"], "withdrawal_restricted")
        self.assertEqual(ai["assessment"]["withdrawal_status"], "unverified")
        for signal in ai["signals"]:
            source = next(source for source in sources if source["id"] == signal["source_id"])
            self.assertIn(signal["evidence"], source["text"])
            self.assertEqual(signal["source_url"], source["source_url"])

    def test_empty_observations_keep_legacy_request_shape(self):
        result = self.assert_preserved(self.post_scan(page_observations=[]))
        self.assert_ai_fields(result["analyses"]["ai"],
                         {"status": "skipped", "reason": "NO_PAGE_CONTEXT", "signals": []})
        self.sdk.assert_not_called()

    def test_old_positional_run_scan_call_remains_supported(self):
        result = run_scan(self.payload["url"], self.business)
        self.assertEqual(result["risk_score"], 50)
        self.assertEqual(result["analyses"]["ai"]["status"], "skipped")
        self.sdk.assert_not_called()

    def test_observation_list_type_count_and_required_fields_are_validated(self):
        invalid = [None, {}, "not a list", 1, ["not an object"],
                   [observation("출금 신청", id=f"source-{index}") for index in range(11)]]
        for field in ("id", "source_url", "kind", "text", "observed_at"):
            source = observation("출금 신청")
            del source[field]
            invalid.append([source])
        for sources in invalid:
            with self.subTest(sources=sources):
                self.assert_invalid_before_calls(sources)

    def test_duplicate_and_invalid_ids_are_rejected(self):
        invalid = [[observation("출금 신청"), observation("정산 신청")]]
        for id in ("", " ", "x" * 65, None, 123, False):
            invalid.append([observation("출금 신청", id=id)])
        for sources in invalid:
            with self.subTest(sources=sources):
                self.assert_invalid_before_calls(sources)

    def test_cross_origin_and_credentialed_sources_are_rejected(self):
        urls = (
            "https://other.example.com/withdrawal",
            "https://shop.example.com.evil.example/withdrawal",
            "https://sub.shop.example.com/withdrawal",
            "http://shop.example.com/withdrawal",
            "https://shop.example.com:444/withdrawal",
            "https://shop.example.com:0/withdrawal",
            "https://user:password@shop.example.com/withdrawal",
            "file:///withdrawal",
            "ftp://shop.example.com/withdrawal",
            "/relative-withdrawal",
        )
        for url in urls:
            with self.subTest(url=url):
                self.assert_invalid_before_calls([{**observation("출금 신청"), "source_url": url}])

    def test_same_origin_default_https_port_is_accepted(self):
        source = {**observation("출금 신청"), "source_url": "https://shop.example.com:443/guide"}
        self.assert_preserved(self.post_scan(page_observations=[source]))

    def test_text_kind_and_timestamp_types_and_values_are_validated(self):
        for field, values in (
            ("text", ("", " \n ", None, 123, False, [], {})),
            ("kind", ("external_chat", "", None, 123)),
            ("observed_at", ("not-a-date", "2026-99-99T12:00:00Z", "", None, 123)),
        ):
            for value in values:
                with self.subTest(field=field, value=value):
                    self.assert_invalid_before_calls([{**observation("출금 신청"), field: value}])

    def test_combined_observation_budget_is_2000_characters(self):
        sources = [observation("출금 " + "가" * 997, id="current"),
                   observation("정산 " + "나" * 997, id="policy", kind="linked_policy")]
        self.assertEqual(sum(len(source["text"]) for source in sources), 2000)
        self.assert_preserved(self.post_scan(page_observations=sources))

    def test_combined_observation_budget_excess_is_rejected_before_calls(self):
        sources = [observation("가" * 1000), observation("나" * 1001, id="policy", kind="linked_policy")]
        self.assert_invalid_before_calls(sources)

    def test_ten_distinct_observations_are_supported(self):
        sources = [observation("상품 구매에 사용할 포인트를 충전하세요.", id=f"source-{index}",
                               kind="history" if index < 9 else "current_page")
                   for index in range(10)]
        ai = self.assert_preserved(self.post_scan(page_observations=sources))["analyses"]["ai"]
        self.assertEqual(ai["status"], "completed")
        self.assertEqual(ai["assessment"]["withdrawal_status"], "unverified")
