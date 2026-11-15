"""Source-bound, staged analyses using the real SDK over an in-memory transport.

These tests verify parsing and local conservative rules, not live model accuracy.
Every Responses API call is intercepted by httpx.MockTransport.
"""

import json
import logging
import os
from unittest.mock import patch

import httpx
from django.test import SimpleTestCase
from openai import OpenAI

from ai.services import analyze_page_context
from ai.tests import response_body


NOW = "2026-11-14T12:00:00Z"
ORIGIN = "https://shop.example.com"
DIRECT_RULE_TEXT = "계정문제는 출금 동일 금액입금으로 해지 하셔야 됩니다."
TAX_TEXT = "세금 납부가 확인되면 수익금을 받을 수 있습니다."
STAGES = {
    "REWARD_OFFER", "EARNINGS_DISPLAY", "WITHDRAWAL_RESTRICTED",
    "ADVANCE_PAYMENT_REQUIRED",
}


def observation(text, *, id="current", kind="current_page", path="/withdrawal"):
    return {
        "id": id,
        "source_url": ORIGIN + path,
        "kind": kind,
        "text": text,
        "observed_at": NOW,
    }


def sourced_signal(text, *, source_id="current", type="ADVANCE_PAYMENT_REQUIRED"):
    return {
        "type": type,
        "source_id": source_id,
        "evidence": text,
        "reason": "화면에 표시된 지급 조건을 확인했습니다.",
    }


class SourceBoundFlowTests(SimpleTestCase):
    def setUp(self):
        previous_level = logging.root.manager.disable
        logging.disable(logging.CRITICAL)
        self.addCleanup(logging.disable, previous_level)
        env = patch.dict(os.environ, {"OPENAI_API_KEY": "unit-test-key"}, clear=True)
        env.start()
        self.addCleanup(env.stop)

    def analyze_with_response(self, observations, *, signals=None, payload=None,
                              status_code=200, timeout=False, context=""):
        requests, options = [], []

        def handler(request):
            self.assertEqual(request.url.path, "/v1/responses")
            requests.append(json.loads(request.content))
            if timeout:
                raise httpx.ReadTimeout("mock timeout", request=request)
            return httpx.Response(
                status_code,
                json=payload if payload is not None else response_body(signals),
            )

        def create_client(**kwargs):
            options.append(kwargs)
            client = OpenAI(
                **kwargs,
                http_client=httpx.Client(transport=httpx.MockTransport(handler)),
            )
            self.addCleanup(client.close)
            return client

        with patch("ai.services.OpenAI", side_effect=create_client):
            result = analyze_page_context(context, page_observations=observations)
        return result, requests, options

    def assert_no_automatic_advance(self, result):
        self.assertFalse(any(
            signal["type"] == "ADVANCE_PAYMENT_REQUIRED"
            for signal in result["signals"]
        ))
        self.assertNotEqual(result["assessment"]["state"], "advance_payment_required")

    def test_tax_condition_is_sent_and_verified_with_its_original_source(self):
        source = observation(TAX_TEXT, kind="linked_policy", path="/guide/refund")
        result, requests, _ = self.analyze_with_response(
            [source], signals=[sourced_signal(TAX_TEXT)],
        )
        self.assertEqual(result["status"], "completed")
        matching = [signal for signal in result["signals"]
                    if signal["type"] == "ADVANCE_PAYMENT_REQUIRED"]
        self.assertEqual(len(matching), 1)
        signal = matching[0]
        self.assertEqual(signal["evidence"], TAX_TEXT)
        self.assertEqual(signal["source_id"], source["id"])
        self.assertEqual(signal["source_url"], source["source_url"])
        self.assertEqual(signal["kind"], "linked_policy")
        self.assertEqual(signal["observed_at"], NOW)
        self.assertEqual(signal["source"], "ai")
        self.assertEqual(result["assessment"]["state"], "advance_payment_required")
        self.assertEqual(result["assessment"]["withdrawal_status"], "risk_detected")
        self.assertEqual(len(requests), 1)

    def test_direct_demand_does_not_require_reward_or_earnings_history(self):
        text = "보증금 입금이 확인되면 판매대금을 지급합니다."
        result, _, _ = self.analyze_with_response(
            [observation(text)], signals=[sourced_signal(text)],
        )
        self.assertEqual(result["status"], "completed")
        self.assertEqual(result["assessment"]["state"], "advance_payment_required")
        self.assertIn("ADVANCE_PAYMENT_REQUIRED", result["assessment"]["observed_stages"])

    def test_four_stages_from_distinct_pages_retain_each_evidence_source(self):
        texts = [
            "후기 게시물을 올리면 보상금을 드립니다.",
            "적립된 수익금은 300,000원입니다.",
            "현재 출금이 제한되어 있습니다.",
            TAX_TEXT,
        ]
        kinds = ["history", "history", "current_page", "linked_policy"]
        types = ["REWARD_OFFER", "EARNINGS_DISPLAY", "WITHDRAWAL_RESTRICTED",
                 "ADVANCE_PAYMENT_REQUIRED"]
        sources = [observation(text, id=f"stage-{index}", kind=kinds[index],
                               path=f"/stage/{index}")
                   for index, text in enumerate(texts)]
        signals = [sourced_signal(text, source_id=f"stage-{index}", type=types[index])
                   for index, text in enumerate(texts)]
        result, _, _ = self.analyze_with_response(sources, signals=signals)
        self.assertEqual(result["status"], "completed")
        self.assertEqual(set(result["assessment"]["observed_stages"]), STAGES)
        for expected, source in zip(signals, sources):
            with self.subTest(source=source["id"]):
                matching = [s for s in result["signals"] if all(
                    s[key] == value for key, value in expected.items()
                )]
                self.assertEqual(len(matching), 1)
                self.assertEqual(matching[0]["source_url"], source["source_url"])
                self.assertEqual(matching[0]["kind"], source["kind"])

    def test_unknown_source_id_is_rejected(self):
        result, _, _ = self.analyze_with_response(
            [observation(TAX_TEXT)],
            signals=[sourced_signal(TAX_TEXT, source_id="not-observed")],
        )
        self.assertEqual(result["status"], "error")
        self.assertEqual(result["error"], "INVALID_AI_RESPONSE")
        self.assertFalse(any(s["source_id"] == "not-observed" for s in result["signals"]))

    def test_quote_from_another_source_cannot_be_attached_to_current_page(self):
        sources = [observation("출금 신청"), observation(TAX_TEXT, id="policy",
                                                       kind="linked_policy", path="/policy")]
        result, _, _ = self.analyze_with_response(sources, signals=[sourced_signal(TAX_TEXT)])
        self.assertEqual(result["status"], "error")
        self.assertFalse(any(s["evidence"] == TAX_TEXT and s["source_id"] == "current"
                             for s in result["signals"]))

    def test_evidence_cannot_join_words_across_source_boundaries(self):
        sources = [observation("출금하려면"),
                   observation("5만 원을 먼저 입금하세요.", id="policy", kind="linked_policy")]
        invented = "출금하려면 5만 원을 먼저 입금하세요."
        result, _, _ = self.analyze_with_response(sources, signals=[sourced_signal(invented)])
        self.assertEqual(result["status"], "error")
        self.assertFalse(any(s["evidence"] == invented for s in result["signals"]))

    def test_hallucinated_sentence_never_becomes_warning_evidence(self):
        invented = "출금하려면 100만 원을 먼저 송금하세요."
        result, _, _ = self.analyze_with_response(
            [observation("상품 구매에 사용할 포인트를 충전하세요.")], signals=[sourced_signal(invented)],
        )
        self.assertEqual(result["status"], "error")
        self.assert_no_automatic_advance(result)
        self.assertFalse(any(s["evidence"] == invented for s in result["signals"]))

    def test_legacy_context_cannot_be_used_as_observation_evidence(self):
        result, _, _ = self.analyze_with_response(
            [observation("상품 구매에 사용할 포인트를 충전하세요.")], signals=[sourced_signal(TAX_TEXT)], context=TAX_TEXT,
        )
        self.assertEqual(result["status"], "error")
        self.assert_no_automatic_advance(result)

    def test_evidence_must_be_in_source_excerpt_actually_sent_to_model(self):
        excluded = "상품은 파란색입니다."
        source = observation(excluded + "\n\n\n\n\n" + TAX_TEXT)
        result, requests, _ = self.analyze_with_response(
            [source], signals=[sourced_signal(excluded)],
        )
        self.assertNotIn(excluded, requests[0]["input"])
        self.assertEqual(result["status"], "error")
        self.assertFalse(any(s["evidence"] == excluded for s in result["signals"]))

    def test_one_unverified_signal_discards_all_model_claims_in_response(self):
        text = "상품 구매에 사용할 포인트를 충전하세요."
        valid = sourced_signal(text, type="REWARD_OFFER")
        invalid = sourced_signal("원문에 없는 지급 조건", source_id="other")
        result, _, _ = self.analyze_with_response(
            [observation(text)], signals=[valid, invalid],
        )
        self.assertEqual(result["status"], "error")
        self.assertFalse(any(s["source"] == "ai" for s in result["signals"]))
        self.assertEqual(result["assessment"]["state"], "unverified")

    def test_model_cannot_supply_its_own_source_url_or_assessment(self):
        payloads = [
            response_body([{**sourced_signal(TAX_TEXT), "source_url": "https://invented.example/"}]),
            response_body(text=json.dumps({"signals": [], "assessment": {"state": "safe"}})),
            response_body([{**sourced_signal(TAX_TEXT), "type": "SAFE"}]),
            response_body([{"type": "ADVANCE_PAYMENT_REQUIRED", "evidence": TAX_TEXT,
                            "reason": "옛 스키마에는 source_id가 없습니다."}]),
            response_body([{**sourced_signal(TAX_TEXT), "reason": "   "}]),
        ]
        for payload in payloads:
            with self.subTest(payload=payload):
                result, _, _ = self.analyze_with_response([observation(TAX_TEXT)], payload=payload)
                self.assertEqual(result["status"], "error")
                self.assertEqual(result["error"], "INVALID_AI_RESPONSE")
                self.assertFalse(any(s.get("source_url") == "https://invented.example/"
                                     for s in result["signals"]))

    def test_structured_schema_and_model_input_only_contain_source_ids_and_text(self):
        source = observation(TAX_TEXT, path="/private-marker-policy")
        os.environ["OPENAI_MODEL"] = "configured-test-model"
        _, requests, options = self.analyze_with_response([source])
        body = requests[0]
        self.assertEqual(body["model"], "configured-test-model")
        self.assertIs(body["store"], False)
        self.assertEqual(options[0]["max_retries"], 0)
        self.assertGreater(options[0]["timeout"], 0)
        self.assertLessEqual(options[0]["timeout"], 30)
        schema = body["text"]["format"]
        self.assertEqual(schema["type"], "json_schema")
        self.assertIs(schema["strict"], True)
        self.assertIs(schema["schema"]["additionalProperties"], False)
        serialized = json.dumps(body["input"], ensure_ascii=False)
        # Account for JSON-in-JSON input while checking what was really transmitted.
        if isinstance(body["input"], str):
            try:
                serialized = json.dumps(json.loads(body["input"]), ensure_ascii=False)
            except ValueError:
                serialized = body["input"]
        self.assertIn(source["id"], serialized)
        self.assertIn(TAX_TEXT, serialized)
        self.assertNotIn(source["source_url"], serialized)
        self.assertNotIn(NOW, serialized)

    def test_prevention_deduction_points_and_other_ordinary_conditions_do_not_trigger_rule(self):
        texts = (
            "판매대금에서 수수료를 차감한 뒤 지급합니다.",
            "상품 구매에 사용할 포인트를 충전하세요.",
            "출금 신청",
            "출금 전에 수수료를 보내라고 하면 거래를 중단하세요.",
            "세금을 먼저 보내야 출금할 수 있다는 말은 사기 예방 안내의 예시입니다.",
            "계정문제는 출금 동일 금액입금으로 해지 하셔야 됩니다라는 요구는 사기입니다.",
            "사기 예방 사례: '계정문제는 출금 동일 금액입금으로 해지 하셔야 됩니다.' 응하지 마세요.",
            "별도 입금 없이 수익금을 지급합니다.",
            "출금하려면 별도로 먼저 입금하실 필요가 없습니다.",
            "출금하려면 돈을 먼저 보내야 하는 것은 아닙니다.",
            "최소 출금 가능 금액은 5만 원입니다.",
            "출금하려면 신분증으로 본인 확인을 완료하세요.",
            "게시물 작성 보상 포인트는 상품 구매에만 사용할 수 있습니다.",
        )
        os.environ.pop("OPENAI_API_KEY")
        with patch("ai.services.OpenAI") as sdk:
            for text in texts:
                with self.subTest(text=text):
                    result = analyze_page_context(page_observations=[observation(text)])
                    self.assert_no_automatic_advance(result)
                    self.assertEqual(result["assessment"]["withdrawal_status"], "unverified")
        sdk.assert_not_called()

    def test_no_model_signals_does_not_turn_unknown_withdrawal_conditions_into_safe(self):
        result, _, _ = self.analyze_with_response([observation("상품 구매에 사용할 포인트를 충전하세요.")])
        self.assertEqual(result["status"], "completed")
        self.assertEqual(result["assessment"]["state"], "unverified")
        self.assertEqual(result["assessment"]["withdrawal_status"], "unverified")

    def test_exact_redeposit_condition_warns_without_api_key_and_with_source(self):
        os.environ.pop("OPENAI_API_KEY")
        source = observation(DIRECT_RULE_TEXT, id="withdrawal-policy", kind="linked_policy")
        with patch("ai.services.OpenAI") as sdk:
            result = analyze_page_context(page_observations=[source])
        sdk.assert_not_called()
        self.assertEqual(result["status"], "skipped")
        self.assertEqual(result["reason"], "MISSING_API_KEY")
        matches = [s for s in result["signals"] if s["type"] == "ADVANCE_PAYMENT_REQUIRED"]
        self.assertEqual(len(matches), 1)
        self.assertEqual(matches[0]["source"], "rule")
        self.assertIn(matches[0]["evidence"], source["text"])
        self.assertEqual(matches[0]["source_id"], source["id"])
        self.assertEqual(matches[0]["source_url"], source["source_url"])
        self.assertEqual(result["assessment"]["state"], "advance_payment_required")

    def test_rule_evidence_survives_api_failure_and_empty_model_result(self):
        source = observation(DIRECT_RULE_TEXT)
        for options in ({"timeout": True}, {}, {"status_code": 503,
                                               "payload": {"error": {"message": "mock unavailable"}}}):
            with self.subTest(options=options):
                result, requests, _ = self.analyze_with_response([source], **options)
                self.assertEqual(len(requests), 1)
                self.assertEqual(result["assessment"]["state"], "advance_payment_required")
                self.assertTrue(any(s["type"] == "ADVANCE_PAYMENT_REQUIRED"
                                    and s["source"] == "rule" for s in result["signals"]))

    def test_missing_key_retains_observed_history_without_inventing_next_stages(self):
        os.environ.pop("OPENAI_API_KEY")
        sources = [observation("게시물을 작성하면 보상금을 드립니다.", id="reward", kind="history"),
                   observation("현재 적립금 잔액은 300,000원입니다.", id="earnings")]
        with patch("ai.services.OpenAI") as sdk:
            result = analyze_page_context(page_observations=sources)
        sdk.assert_not_called()
        self.assertEqual(result["status"], "skipped")
        self.assertIn("REWARD_OFFER", result["assessment"]["observed_stages"])
        self.assertIn("EARNINGS_DISPLAY", result["assessment"]["observed_stages"])
        self.assertEqual(result["assessment"]["state"], "reward_flow")
        self.assertEqual(result["assessment"]["withdrawal_status"], "unverified")
        self.assert_no_automatic_advance(result)

    def test_timeout_keeps_visible_restriction_but_does_not_invent_deposit_demand(self):
        result, requests, _ = self.analyze_with_response(
            [observation("현재 출금이 제한되어 있습니다.")], timeout=True,
        )
        self.assertEqual(result["status"], "error")
        self.assertEqual(result["error"], "OPENAI_TIMEOUT")
        self.assertEqual(len(requests), 1)
        self.assertEqual(result["assessment"]["state"], "withdrawal_restricted")
        self.assert_no_automatic_advance(result)

    def test_redacted_balance_retains_earnings_stage_without_personal_amount(self):
        os.environ.pop("OPENAI_API_KEY")
        for text in ("현재 잔액: [개인 금액 제외]", "누적 수익금 [개인 금액 제외]"):
            with self.subTest(text=text), patch("ai.services.OpenAI") as sdk:
                result = analyze_page_context(page_observations=[observation(text)])
                self.assertEqual(result["assessment"]["state"], "reward_flow")
                self.assertIn("EARNINGS_DISPLAY", result["assessment"]["observed_stages"])
                self.assertEqual(result["assessment"]["withdrawal_status"], "unverified")
                self.assert_no_automatic_advance(result)
                sdk.assert_not_called()

    def test_invalid_json_incomplete_and_refusal_never_produce_safe_verdict(self):
        payloads = [response_body(text="not json"), response_body(status="incomplete"),
                    response_body(refusal=True)]
        for payload in payloads:
            with self.subTest(payload=payload):
                result, _, _ = self.analyze_with_response([observation("상품 구매에 사용할 포인트를 충전하세요.")], payload=payload)
                self.assertEqual(result["status"], "error")
                self.assertEqual(result["assessment"]["state"], "unverified")
                self.assertEqual(result["assessment"]["withdrawal_status"], "unverified")


    def test_navigation_labels_do_not_create_rule_stages_or_call_the_model(self):
        menus = (
            "보상 | 적립 | 제한 | 세금 조건 | 차감 안내 | 메뉴만",
            "보상\n적립\n제한\n세금 조건\n차감 안내\n메뉴만",
            "보상 ｜ 적립 ｜ 제한 ｜ 세금 조건 ｜ 차감 안내 ｜ 메뉴만",
            "출금 신청",
            "충전 신청 / 출금신청 / 정산",
            "충전 신청 출금신청 정산",
            "보상 적립 제한 세금 조건 차감 안내 메뉴만",
            "보상적립제한세금조건차감안내메뉴만",
        )
        for text in menus:
            with self.subTest(text=text), patch("ai.services.OpenAI") as sdk:
                result = analyze_page_context(page_observations=[observation(text)])
                self.assertEqual(result["status"], "skipped")
                self.assertEqual(result["reason"], "NO_RELEVANT_CONTEXT")
                self.assertEqual(result["signals"], [])
                self.assertEqual(result["assessment"]["observed_stages"], [])
                self.assertEqual(result["assessment"]["withdrawal_status"], "unverified")
                sdk.assert_not_called()

    def test_navigation_labels_do_not_create_fallback_reward_without_key(self):
        os.environ.pop("OPENAI_API_KEY")
        with patch("ai.services.OpenAI") as sdk:
            result = analyze_page_context(page_observations=[observation(
                "보상 | 적립 | 제한 | 세금 조건 | 차감 안내 | 메뉴만",
            )])
        sdk.assert_not_called()
        self.assertEqual(result["signals"], [])
        self.assertEqual(result["assessment"]["observed_stages"], [])
        self.assertEqual(result["assessment"]["withdrawal_status"], "unverified")

    def test_menu_quote_cannot_be_accepted_as_model_stage_evidence(self):
        menus = "보상 | 적립 | 제한 | 세금 조건 | 차감 안내 | 메뉴만"
        text = menus + "\n상품 구매에 사용할 포인트를 충전하세요."
        result, requests, _ = self.analyze_with_response(
            [observation(text)],
            signals=[sourced_signal("보상 | 적립", type="REWARD_OFFER")],
        )
        self.assertEqual(len(requests), 1)
        self.assertNotIn("보상 | 적립", requests[0]["input"])
        self.assertEqual(result["status"], "error")
        self.assertEqual(result["error"], "INVALID_AI_RESPONSE")
        self.assertEqual(result["signals"], [])
        self.assertEqual(result["assessment"]["observed_stages"], [])

    def test_partial_label_quote_from_real_prose_is_not_sufficient_stage_evidence(self):
        text = "리뷰에 대한 보상은 상품 구매에만 사용할 수 있습니다."
        result, _, _ = self.analyze_with_response(
            [observation(text)], signals=[sourced_signal("보상", type="REWARD_OFFER")],
        )
        self.assertEqual(result["status"], "error")
        self.assertFalse(any(signal["source"] == "ai" for signal in result["signals"]))

    def test_mixed_real_history_preserves_tax_warning_but_not_navigation_reward(self):
        menus = "보상 | 적립 | 제한 | 세금 조건 | 차감 안내 | 메뉴만"
        restriction = "계정 확인 전에는 출금이 제한됩니다."
        sources = [observation(menus, id="old-menu", kind="history", path="/stage?stage=menu"),
                   observation(TAX_TEXT, id="earlier-tax", kind="history", path="/stage?stage=tax"),
                   observation(restriction, id="current", kind="current_page", path="/stage?stage=restricted")]
        result, requests, _ = self.analyze_with_response(sources)
        sent = json.loads(requests[0]["input"])
        self.assertEqual({item["id"] for item in sent}, {"earlier-tax", "current"})
        self.assertNotIn("REWARD_OFFER", result["assessment"]["observed_stages"])
        self.assertIn("WITHDRAWAL_RESTRICTED", result["assessment"]["observed_stages"])
        self.assertIn("ADVANCE_PAYMENT_REQUIRED", result["assessment"]["observed_stages"])
        warning = next(signal for signal in result["signals"]
                       if signal["type"] == "ADVANCE_PAYMENT_REQUIRED")
        self.assertEqual(warning["source_id"], "earlier-tax")
        self.assertEqual(warning["evidence"], TAX_TEXT)
        self.assertEqual(warning["source_url"], sources[1]["source_url"])


    def test_same_source_model_and_rule_wrapping_variants_return_one_original_quote(self):
        # This is the public-policy phrase that produced duplicate real UI rows.
        source = observation("(" + DIRECT_RULE_TEXT + ")", id="policy", kind="linked_policy")
        result, requests, _ = self.analyze_with_response(
            [source], signals=[sourced_signal(DIRECT_RULE_TEXT, source_id="policy")],
        )
        self.assertEqual(result["status"], "completed")
        self.assertEqual(len(requests), 1)
        warnings = [signal for signal in result["signals"]
                    if signal["type"] == "ADVANCE_PAYMENT_REQUIRED"]
        self.assertEqual(len(warnings), 1)
        self.assertEqual(warnings[0]["source"], "ai")
        self.assertEqual(warnings[0]["evidence"], DIRECT_RULE_TEXT)
        self.assertIn(warnings[0]["evidence"], source["text"])
        self.assertEqual(warnings[0]["source_id"], source["id"])
        self.assertEqual(warnings[0]["source_url"], source["source_url"])

    def test_same_source_wrappers_and_whitespace_do_not_rewrite_chosen_evidence(self):
        original = "‘출금하려면 5만 원을 먼저 입금하세요.’"
        spaced = "출금하려면  5만 원을 먼저 입금하세요."
        source = observation(original + "\n" + spaced)
        result, _, _ = self.analyze_with_response(
            [source], signals=[sourced_signal(original), sourced_signal(spaced)],
        )
        self.assertEqual(result["status"], "completed")
        warnings = [signal for signal in result["signals"]
                    if signal["type"] == "ADVANCE_PAYMENT_REQUIRED"]
        self.assertEqual(len(warnings), 1)
        self.assertEqual(warnings[0]["evidence"], original)
        self.assertIn(warnings[0]["evidence"], source["text"])

    def test_dedup_preserves_different_amounts_negations_and_internal_punctuation(self):
        # The local deduplicator must not silently turn different exact model
        # quotes into one claim, even when the texts are close in appearance.
        quotes = (
            "출금하려면 5만 원을 먼저 입금하세요.",
            "출금하려면 6만 원을 먼저 입금하세요.",
            "출금하려면 5만 원을 먼저 입금하세요?",
            "출금하려면 5만 원을 먼저 입금하세요!",
            "출금하려면 5만 원을 먼저 입금하지 마세요.",
            "출금하려면 5만 원을 먼저 입금하세요. 입금하지 마세요.",
        )
        source = observation("\n".join(quotes))
        result, _, _ = self.analyze_with_response(
            [source], signals=[sourced_signal(text) for text in quotes],
        )
        self.assertEqual(result["status"], "completed")
        self.assertEqual([signal["evidence"] for signal in result["signals"]
                          if signal["source"] == "ai"], list(quotes))

    def test_same_quote_in_distinct_observations_keeps_each_source_and_time(self):
        old = observation(TAX_TEXT, id="earlier", kind="history", path="/policy")
        old["observed_at"] = "2026-11-14T11:00:00Z"
        current = observation(TAX_TEXT, id="current", kind="current_page", path="/policy")
        other_page = observation(TAX_TEXT, id="other-page", kind="linked_policy", path="/refund")
        sources = [old, current, other_page]
        result, _, _ = self.analyze_with_response(sources, signals=[
            sourced_signal(TAX_TEXT, source_id=source["id"]) for source in sources
        ])
        self.assertEqual(result["status"], "completed")
        warnings = [signal for signal in result["signals"]
                    if signal["type"] == "ADVANCE_PAYMENT_REQUIRED"]
        self.assertEqual(len(warnings), 3)
        self.assertEqual({signal["source_id"] for signal in warnings},
                         {source["id"] for source in sources})
        for signal, source in zip(warnings, sources):
            self.assertEqual(signal["source_url"], source["source_url"])
            self.assertEqual(signal["observed_at"], source["observed_at"])
