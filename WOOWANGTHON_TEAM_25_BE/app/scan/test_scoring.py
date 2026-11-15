"""End-to-end scan scoring using the SDK parser with all HTTP services mocked."""

import json
import logging
import os
from unittest.mock import patch

import httpx
from django.test import SimpleTestCase, override_settings
from openai import OpenAI
from rest_framework.test import APIClient

from ai.test_flow import TAX_TEXT, observation, sourced_signal
from ai.tests import response_body
from external_api.nts.client import NtsApiError


@override_settings(ALLOWED_HOSTS=["testserver"])
class ScanScoringContractTests(SimpleTestCase):
    def setUp(self):
        # These source/scoring tests intentionally avoid ORM access. Real cache
        # behavior and migrations are exercised by scan.test_cache.TestCase.
        cache_read = patch("scan.services.load_public_api_cache", side_effect=lambda *args, **kwargs: {"status": "miss", "analyses": {}})
        cache_write = patch("scan.services.store_public_api_cache", return_value=True)
        cache_read.start()
        cache_write.start()
        self.addCleanup(cache_read.stop)
        self.addCleanup(cache_write.stop)
        previous = logging.root.manager.disable
        logging.disable(logging.CRITICAL)
        self.addCleanup(logging.disable, previous)
        environment = patch.dict(os.environ, {"OPENAI_API_KEY": "unit-test-key"}, clear=True)
        environment.start()
        self.addCleanup(environment.stop)
        self.client = APIClient()
        self.url = "https://shop.example.com/home"
        self.business = {"business_number": "123-45-67890", "business_name": "합성 테스트 상점"}
        self.nts_result = {"status": "completed", "business_status": "ACTIVE"}
        self.ftc_result = {"status": "completed", "search_method": "BUSINESS_NUMBER", "match_status": "NO_MATCH", "candidates": []}
        nts = patch("scan.services.lookup_nts", return_value=self.nts_result)
        ftc = patch("scan.services.lookup_ftc", return_value=self.ftc_result)
        self.nts, self.ftc = nts.start(), ftc.start()
        self.addCleanup(nts.stop)
        self.addCleanup(ftc.stop)
        self.sdk_requests = []
        self.sdk_response = response_body()
        self.timeout = False

        def handler(request):
            self.sdk_requests.append(json.loads(request.content))
            self.assertEqual(request.url.path, "/v1/responses")
            if self.timeout:
                raise httpx.ReadTimeout("mock timeout", request=request)
            return httpx.Response(200, json=self.sdk_response)

        def client_factory(**kwargs):
            client = OpenAI(**kwargs, http_client=httpx.Client(transport=httpx.MockTransport(handler)))
            self.addCleanup(client.close)
            return client

        sdk = patch("ai.services.OpenAI", side_effect=client_factory)
        self.sdk = sdk.start()
        self.addCleanup(sdk.stop)
        guard = patch("requests.sessions.Session.request", side_effect=AssertionError("External HTTP is disabled in scoring tests"))
        guard.start()
        self.addCleanup(guard.stop)

    def post(self, *, sources=None, context=None, business=None):
        payload = {"url": self.url, "business_info": self.business if business is None else business}
        if sources is not None:
            payload["page_observations"] = sources
        if context is not None:
            payload["page_context"] = context
        response = self.client.post("/api/v1/scan/", payload, format="json")
        self.assertEqual(response.status_code, 200, response.content)
        result = response.json()
        self.assertEqual(sum(item["points"] for item in result["risk_factors"]), result["risk_score"])
        self.assertNotIn("unit-test-key", response.content.decode())
        self.assertLessEqual(result["risk_score"], 100)
        return result

    def direct_source(self):
        source = observation(TAX_TEXT, id="withdrawal-rule", kind="linked_policy", path="/withdrawal-policy")
        self.sdk_response = response_body([sourced_signal(TAX_TEXT, source_id=source["id"])])
        return source

    def test_source_bound_ai_fifty_combines_with_business_forty(self):
        source = self.direct_source()
        result = self.post(sources=[source])
        self.assertEqual(result["risk_score"], 90)
        self.assertEqual(result["verdict"], "HIGH_RISK")
        self.assertEqual(result["score_status"], "completed")
        self.assertEqual(result["score_breakdown"]["business"]["score"], 40)
        self.assertEqual(result["score_breakdown"]["business"]["max_score"], 50)
        self.assertEqual(result["score_breakdown"]["ai"]["score"], 50)
        ai = result["analyses"]["ai"]
        self.assertEqual(ai["risk_score"], 50)
        self.assertEqual(ai["scoring_status"], "completed")
        self.assertEqual(ai["risk_factors"][0]["evidence"][0]["source_url"], source["source_url"])
        self.assertIn(ai["risk_factors"][0]["evidence"][0]["evidence"], source["text"])
        self.assertEqual(len(self.sdk_requests), 1)
        self.assertIs(self.sdk_requests[0]["store"], False)
        self.assertNotIn(self.business["business_name"], self.sdk_requests[0]["input"])
        self.assertNotIn(source["source_url"], self.sdk_requests[0]["input"])

    def test_public_api_failures_do_not_remove_direct_fifty(self):
        self.nts.side_effect = NtsApiError("mock public API failure")
        self.ftc.side_effect = RuntimeError("mock FTC failure")
        result = self.post(sources=[self.direct_source()])
        self.assertEqual(result["analyses"]["nts"]["status"], "error")
        self.assertEqual(result["analyses"]["ftc"]["status"], "error")
        self.assertEqual(result["risk_score"], 50)
        self.assertEqual(result["verdict"], "HIGH_RISK")
        self.assertEqual(result["score_status"], "partial")

    def test_model_timeout_retains_rule_fifty_and_distinguishes_provenance(self):
        self.timeout = True
        result = self.post(sources=[self.direct_source()])
        ai = result["analyses"]["ai"]
        self.assertEqual(ai["status"], "error")
        self.assertEqual(ai["error"], "OPENAI_TIMEOUT")
        self.assertEqual(ai["risk_score"], 50)
        self.assertEqual(ai["scoring_status"], "rule_only")
        self.assertEqual(ai["risk_factors"][0]["source"], "rule")
        self.assertEqual(result["risk_score"], 90)
        self.assertEqual(result["score_status"], "partial")

    def test_missing_key_retains_explicit_rule_score_without_constructing_sdk(self):
        os.environ.pop("OPENAI_API_KEY")
        result = self.post(sources=[self.direct_source()])
        ai = result["analyses"]["ai"]
        self.assertEqual(ai["status"], "skipped")
        self.assertEqual(ai["scoring_status"], "rule_only")
        self.assertEqual(ai["risk_score"], 50)
        self.assertEqual(result["risk_score"], 90)
        self.sdk.assert_not_called()

    def test_omitted_context_keeps_old_request_valid_with_unknown_ai_score(self):
        result = self.post()
        ai = result["analyses"]["ai"]
        self.assertEqual(ai["status"], "skipped")
        self.assertEqual(ai["reason"], "NO_PAGE_CONTEXT")
        self.assertIsNone(ai["risk_score"])
        self.assertEqual(ai["scoring_status"], "unavailable")
        self.assertEqual(result["risk_score"], 40)
        self.assertEqual(result["verdict"], "CAUTION")
        self.assertEqual(result["score_status"], "partial")
        self.sdk.assert_not_called()

    def test_fee_deduction_zero_points_is_not_a_withdrawal_safety_finding(self):
        source = observation("판매대금에서 수수료를 차감한 뒤 지급합니다.")
        result = self.post(sources=[source])
        ai = result["analyses"]["ai"]
        self.assertEqual(ai["risk_score"], 0)
        self.assertEqual(ai["scoring_status"], "completed")
        self.assertEqual(ai["assessment"]["withdrawal_status"], "unverified")
        self.assertEqual(result["risk_score"], 40)
        self.assertEqual(result["score_status"], "completed")

    def test_hallucinated_quote_cannot_supply_ai_points(self):
        source = observation("상품 구매에 사용할 포인트를 충전하세요.")
        self.sdk_response = response_body([sourced_signal("입력에 없는 100만원 선입금 요구", source_id=source["id"])])
        result = self.post(sources=[source])
        ai = result["analyses"]["ai"]
        self.assertEqual(ai["status"], "error")
        self.assertEqual(ai["error"], "INVALID_AI_RESPONSE")
        self.assertIsNone(ai["risk_score"])
        self.assertEqual(result["risk_score"], 40)
        self.assertEqual(result["score_status"], "partial")

    def test_complete_weak_stages_get_ten_without_advance_payment_claim(self):
        sources = [observation("리뷰를 작성하면 보상 포인트를 지급합니다.", id="reward", kind="history"),
                   observation("현재 잔액: [개인 금액 제외]", id="earnings", kind="history"),
                   observation("현재 출금이 제한되어 있습니다.", id="restriction")]
        types = ("REWARD_OFFER", "EARNINGS_DISPLAY", "WITHDRAWAL_RESTRICTED")
        self.sdk_response = response_body([sourced_signal(source["text"], source_id=source["id"], type=type)
                                           for source, type in zip(sources, types)])
        result = self.post(sources=sources)
        ai = result["analyses"]["ai"]
        self.assertEqual(ai["risk_score"], 10)
        self.assertEqual(ai["risk_factors"][0]["code"], "AI_STAGED_FLOW")
        self.assertEqual(ai["assessment"]["withdrawal_status"], "unverified")
        self.assertFalse(any(signal["type"] == "ADVANCE_PAYMENT_REQUIRED" for signal in ai["signals"]))
        self.assertEqual(result["risk_score"], 50)
        self.assertEqual(result["verdict"], "CAUTION")

    def test_repeated_direct_signal_gets_one_ai_factor(self):
        source = self.direct_source()
        item = sourced_signal(TAX_TEXT, source_id=source["id"])
        self.sdk_response = response_body([item, item, item])
        result = self.post(sources=[source])
        self.assertEqual(result["risk_score"], 90)
        self.assertEqual(len(result["analyses"]["ai"]["risk_factors"]), 1)
        self.assertEqual(len([factor for factor in result["risk_factors"] if factor["code"] == "AI_ADVANCE_PAYMENT_REQUIRED"]), 1)

    def test_completed_zero_without_business_information_remains_insufficient(self):
        self.ftc.return_value = {"status": "skipped", "match_status": "UNKNOWN"}
        result = self.post(sources=[observation("판매대금에서 수수료를 차감한 뒤 지급합니다.")], business={})
        self.assertEqual(result["risk_score"], 0)
        self.assertEqual(result["analyses"]["ai"]["risk_score"], 0)
        self.assertEqual(result["verdict"], "INSUFFICIENT_DATA")
        self.assertEqual(result["score_status"], "partial")
        self.nts.assert_not_called()

    def test_legacy_single_context_also_returns_grounded_ai_score(self):
        text = "출금하려면 5만 원을 먼저 입금하세요."
        self.sdk_response = response_body([{"type": "ADVANCE_PAYMENT_REQUIRED", "evidence": text,
                                            "reason": "출금 전 별도 입금 조건입니다."}])
        result = self.post(context=text)
        self.assertEqual(result["risk_score"], 90)
        self.assertEqual(result["analyses"]["ai"]["risk_score"], 50)
        self.assertEqual(result["analyses"]["ai"]["signals"][0]["evidence"], text)

    def test_displayed_malformed_number_and_name_lookup_failure_score_fifty(self):
        raw = "125-86-112516"
        business = {"business_number": "", "business_name": "합성 테스트 상점", "displayed_business_number": raw}
        self.ftc.return_value = {"status": "completed", "search_method": "BUSINESS_NAME", "match_status": "NO_MATCH", "candidates": []}
        result = self.post(sources=[observation("판매대금에서 수수료를 차감한 뒤 지급합니다.")], business=business)
        self.assertEqual(result["risk_score"], 50)
        self.assertEqual(result["score_breakdown"]["business"]["score"], 50)
        self.assertEqual(result["score_breakdown"]["business"]["max_score"], 50)
        self.assertEqual(result["analyses"]["ai"]["risk_score"], 0)
        self.assertEqual(result["score_breakdown"]["business"]["raw_score"], 50)
        self.assertFalse(result["score_breakdown"]["business"]["capped"])
        combination = [factor for factor in result["risk_factors"]
                       if factor["code"] == "MALFORMED_NUMBER_AND_FTC_NAME_NOT_FOUND"]
        self.assertEqual(len(combination), 1)
        self.assertEqual(combination[0]["points"], 5)
        self.assertIn("사업자번호 형식 오류", combination[0]["reason"])
        self.assertEqual(sum(factor["points"] for factor in result["risk_factors"]), 50)
        self.assertEqual(result["scoring_policy"], "business50_ai50_v3")
        self.assertEqual(result["verdict"], "CAUTION")
        self.assertEqual(result["score_status"], "partial")
        self.assertEqual(result["business_info"], business)
        self.assertEqual(result["analyses"]["business_input"]["status"], "malformed")
        self.assertEqual(result["analyses"]["business_input"]["displayed_value"], raw)
        self.assertEqual(result["analyses"]["business_input"]["digit_count"], 11)
        format_factor = next(item for item in result["risk_factors"] if item["code"] == "MALFORMED_BUSINESS_NUMBER")
        self.assertEqual(format_factor["points"], 20)
        self.assertEqual(format_factor["evidence"], raw)
        self.assertEqual(format_factor["digit_count"], 11)
        self.nts.assert_not_called()
        self.ftc.assert_called_once_with(business)
        self.assertEqual(len(self.sdk_requests), 1)

    def test_legacy_malformed_business_number_is_preserved_and_never_truncated_for_lookup(self):
        raw = "125-86-112516"
        business = {"business_number": raw, "business_name": "합성 테스트 상점"}
        self.ftc.return_value = {"status": "completed", "search_method": "BUSINESS_NAME", "match_status": "NO_MATCH", "candidates": []}
        result = self.post(business=business)
        self.assertEqual(result["risk_score"], 50)
        self.assertEqual(result["business_info"], business)
        self.assertEqual(result["analyses"]["business_input"]["normalized"], "12586112516")
        self.assertEqual(result["analyses"]["nts"]["reason"], "MALFORMED_BUSINESS_NUMBER")
        self.assertIsNone(result["analyses"]["ai"]["risk_score"])
        self.nts.assert_not_called()
        self.ftc.assert_called_once_with({**business, "business_number": ""})
        self.sdk.assert_not_called()

    def test_compound_business_fifty_and_grounded_ai_fifty_total_one_hundred(self):
        business = {"business_number": "", "business_name": "합성 테스트 상점",
                    "displayed_business_number": "125-86-112516"}
        self.ftc.return_value = {"status": "completed", "search_method": "BUSINESS_NAME",
                                 "match_status": "NO_MATCH", "candidates": []}
        result = self.post(sources=[self.direct_source()], business=business)
        self.assertEqual(result["risk_score"], 100)
        self.assertEqual(result["score_breakdown"]["business"]["score"], 50)
        self.assertEqual(result["score_breakdown"]["business"]["raw_score"], 50)
        self.assertEqual(result["analyses"]["ai"]["risk_score"], 50)
        self.assertEqual(result["verdict"], "HIGH_RISK")
        self.assertEqual(result["score_status"], "partial")
        combination = [factor for factor in result["risk_factors"]
                       if factor["code"] == "MALFORMED_NUMBER_AND_FTC_NAME_NOT_FOUND"]
        self.assertEqual(len(combination), 1)
        self.assertEqual(combination[0]["points"], 5)
        self.nts.assert_not_called()

    def test_compound_business_fifty_survives_model_timeout_without_safe_zero(self):
        business = {"business_number": "", "business_name": "합성 테스트 상점",
                    "displayed_business_number": "125-86-112516"}
        self.ftc.return_value = {"status": "completed", "search_method": "BUSINESS_NAME",
                                 "match_status": "NO_MATCH", "candidates": []}
        self.timeout = True
        result = self.post(sources=[observation("판매대금에서 수수료를 차감한 뒤 지급합니다.")],
                           business=business)
        self.assertEqual(result["risk_score"], 50)
        self.assertEqual(result["score_breakdown"]["business"]["score"], 50)
        self.assertEqual(result["analyses"]["ai"]["status"], "error")
        self.assertIsNone(result["analyses"]["ai"]["risk_score"])
        self.assertEqual(result["score_status"], "partial")
        self.assertEqual(result["verdict"], "CAUTION")
        self.assertEqual(sum(factor["points"] for factor in result["risk_factors"]), 50)
        self.nts.assert_not_called()

    def test_missing_and_placeholder_numbers_remain_unknown_without_format_points(self):
        self.ftc.return_value = {"status": "skipped", "match_status": "UNKNOWN"}
        for raw in (None, "", " ", "찾지 못함", "확인 불가"):
            with self.subTest(raw=raw):
                self.nts.reset_mock()
                self.ftc.reset_mock()
                result = self.post(business={"business_number": raw})
                self.assertEqual(result["risk_score"], 0)
                self.assertEqual(result["risk_factors"], [])
                self.assertEqual(result["analyses"]["business_input"]["status"], "unavailable")
                self.assertEqual(result["verdict"], "INSUFFICIENT_DATA")
                self.nts.assert_not_called()
        flagged = self.post(business={"invalid_business_number": True})
        self.assertEqual(flagged["risk_score"], 0)
        self.assertEqual(flagged["risk_factors"], [])
        self.assertEqual(flagged["analyses"]["business_input"]["status"], "unavailable")
        self.sdk.assert_not_called()

    def test_displayed_number_is_optional_strict_string_and_bounded_before_calls(self):
        for invalid in (None, 1234567890, False, [], {}, "1" * 41):
            with self.subTest(invalid=invalid):
                response = self.client.post("/api/v1/scan/", {
                    "url": self.url,
                    "business_info": {"displayed_business_number": invalid},
                }, format="json")
                self.assertEqual(response.status_code, 400, response.content)
                self.assertIn("displayed_business_number", response.json()["business_info"])
        self.nts.assert_not_called()
        self.ftc.assert_not_called()
        self.sdk.assert_not_called()

    def test_conflicting_declared_and_displayed_numbers_are_rejected_before_calls(self):
        for declared, displayed in (
            ("123-45-67890", "987-65-43210"),
            ("123-45-67890", "125-86-112516"),
            ("123-45-67890", "12-345-67890"),
        ):
            with self.subTest(declared=declared, displayed=displayed):
                response = self.client.post("/api/v1/scan/", {
                    "url": self.url, "business_info": {
                        "business_number": declared, "displayed_business_number": displayed,
                    },
                }, format="json")
                self.assertEqual(response.status_code, 400, response.content)
                self.assertIn("displayed_business_number", response.json()["business_info"])
        self.nts.assert_not_called()
        self.ftc.assert_not_called()
        self.sdk.assert_not_called()

    def test_equivalent_declared_and_displayed_number_formats_are_accepted(self):
        for declared, displayed, expected_lookup in (
            ("123-45-67890", " １２３ - ４５ - ６７８９０ ", "123-45-67890"),
            ("１２３ - ４５ - ６７８９０", "123-45-67890", "1234567890"),
        ):
            with self.subTest(declared=declared, displayed=displayed):
                self.nts.reset_mock()
                self.ftc.reset_mock()
                business = {**self.business, "business_number": declared, "displayed_business_number": displayed}
                result = self.post(business=business)
                self.assertEqual(result["risk_score"], 40)
                self.assertEqual(result["business_info"], business)
                self.assertEqual(result["analyses"]["business_input"]["status"], "valid")
                self.nts.assert_called_once_with(expected_lookup)
                self.ftc.assert_called_once_with({**business, "business_number": expected_lookup})
                self.assertFalse(any(item["code"] == "MALFORMED_BUSINESS_NUMBER" for item in result["risk_factors"]))

    def test_confirmed_unregistered_business_cap_fifty_plus_ai_fifty_is_one_hundred(self):
        self.nts.return_value = {"status": "completed", "business_status": "UNKNOWN",
                                 "tax_type": "국세청에 등록되지 않은 사업자등록번호입니다."}
        result = self.post(sources=[self.direct_source()])
        self.assertEqual(result["risk_score"], 100)
        self.assertEqual(result["score_breakdown"]["business"]["raw_score"], 70)
        self.assertEqual(result["score_breakdown"]["business"]["score"], 50)
        self.assertEqual(result["analyses"]["ai"]["risk_score"], 50)
        self.assertEqual(result["verdict"], "HIGH_RISK")
        self.assertEqual(result["score_status"], "completed")
