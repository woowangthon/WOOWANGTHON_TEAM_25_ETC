"""Responses parsing and evidence validation without external network calls."""

import json
import logging
import os
from unittest.mock import patch

import httpx
from django.test import SimpleTestCase
from openai import OpenAI

from .services import analyze_page_context


ADVANCE_TEXT = "출금하려면 5만 원을 먼저 입금하세요"
SIGNAL = {
    "type": "ADVANCE_PAYMENT_REQUIRED",
    "evidence": ADVANCE_TEXT,
    "reason": "출금을 받기 위한 조건으로 별도 선입금을 요구합니다.",
}


def response_body(signals=None, *, status="completed", text=None, refusal=False):
    content = (
        [{"type": "refusal", "refusal": "Cannot analyze this input"}]
        if refusal
        else [{
            "type": "output_text",
            "text": text if text is not None else json.dumps({"signals": signals or []}),
            "annotations": [],
        }]
    )
    return {
        "id": "resp_mock",
        "object": "response",
        "created_at": 1794690000,
        "status": status,
        "error": None,
        "incomplete_details": {"reason": "max_output_tokens"} if status == "incomplete" else None,
        "instructions": None,
        "metadata": {},
        "model": "gpt-6-luna",
        "output": [{
            "id": "msg_mock",
            "type": "message",
            "role": "assistant",
            "status": "completed",
            "content": content,
        }],
        "parallel_tool_calls": False,
        "temperature": 1,
        "tool_choice": "auto",
        "tools": [],
        "top_p": 1,
    }


class PageContextAnalysisTests(SimpleTestCase):
    def setUp(self):
        previous_level = logging.root.manager.disable
        logging.disable(logging.CRITICAL)
        self.addCleanup(logging.disable, previous_level)
        env = patch.dict(os.environ, {"OPENAI_API_KEY": "unit-test-key"}, clear=True)
        env.start()
        self.addCleanup(env.stop)

    def analyze_with_response(self, context, *, payload=None, status_code=200, timeout=False):
        """Use the real SDK and Structured Outputs parser over an in-memory transport."""
        requests = []
        options = []

        def handler(request):
            requests.append(json.loads(request.content))
            self.assertEqual(request.url.path, "/v1/responses")
            if timeout:
                raise httpx.ReadTimeout("mock timeout", request=request)
            return httpx.Response(status_code, json=payload or response_body())

        def create_client(**kwargs):
            options.append(kwargs)
            client = OpenAI(**kwargs, http_client=httpx.Client(transport=httpx.MockTransport(handler)))
            self.addCleanup(client.close)
            return client

        with patch("ai.services.OpenAI", side_effect=create_client):
            result = analyze_page_context(context)
        return result, requests, options

    def test_empty_context_skips_without_constructing_client(self):
        for context in ("", " \n\t "):
            with self.subTest(context=context), patch("ai.services.OpenAI") as client:
                result = analyze_page_context(context)
                self.assertEqual(result["status"], "skipped")
                self.assertEqual(result["reason"], "NO_PAGE_CONTEXT")
                self.assertEqual(result["signals"], [])
                client.assert_not_called()

    def test_missing_key_skips_without_constructing_client(self):
        os.environ.pop("OPENAI_API_KEY")
        with patch("ai.services.OpenAI") as client:
            result = analyze_page_context(ADVANCE_TEXT)
        self.assertEqual(result, {"status": "skipped", "reason": "MISSING_API_KEY", "signals": []})
        client.assert_not_called()

    def test_unrelated_context_skips_without_api(self):
        with patch("ai.services.OpenAI") as client:
            result = analyze_page_context("오늘 배송이 시작됩니다. 상품은 내일 도착합니다.")
        self.assertEqual(result["status"], "skipped")
        self.assertEqual(result["reason"], "NO_RELEVANT_CONTEXT")
        self.assertEqual(result["signals"], [])
        client.assert_not_called()

    def test_menu_label_and_fee_deduction_have_no_mocked_signals(self):
        # This checks handling of a modeled no-signal result, not real model accuracy.
        for context in ("출금 신청", "판매대금에서 수수료를 차감한 뒤 나머지를 지급합니다."):
            with self.subTest(context=context):
                result, requests, _ = self.analyze_with_response(context)
                self.assertEqual(result, {"status": "completed", "signals": []})
                self.assertEqual(len(requests), 1)

    def test_upfront_deposit_returns_original_evidence(self):
        result, requests, _ = self.analyze_with_response(ADVANCE_TEXT, payload=response_body([SIGNAL]))
        self.assertEqual(result, {"status": "completed", "signals": [SIGNAL]})
        self.assertIn(result["signals"][0]["evidence"], requests[0]["input"])

    def test_payment_synonyms_reach_model_without_being_skipped(self):
        # Only the mocked classification is asserted; these are extraction regressions.
        for context in (
            "판매 수익을 받으려면 인증비를 선결제하세요.",
            "고객님의 잔액을 받으려면 본인 확인 비용을 결제하세요.",
        ):
            with self.subTest(context=context):
                signal = {**SIGNAL, "evidence": context}
                result, requests, _ = self.analyze_with_response(context, payload=response_body([signal]))
                self.assertEqual(result, {"status": "completed", "signals": [signal]})
                self.assertEqual(len(requests), 1)
                self.assertIn(context, requests[0]["input"])

    def test_evidence_absent_from_input_invalidates_response(self):
        invented = {**SIGNAL, "evidence": "출금을 위해 10만 원을 입금하세요"}
        result, _, _ = self.analyze_with_response(ADVANCE_TEXT, payload=response_body([invented]))
        self.assertEqual(result, {"status": "error", "error": "INVALID_AI_RESPONSE", "signals": []})

    def test_evidence_must_be_in_excerpt_actually_sent(self):
        excluded = "상품은 파란색입니다."
        context = excluded + "\n\n\n\n" + ADVANCE_TEXT
        invented = {**SIGNAL, "evidence": excluded}
        result, requests, _ = self.analyze_with_response(context, payload=response_body([invented]))
        self.assertNotIn(excluded, requests[0]["input"])
        self.assertEqual(result["status"], "error")
        self.assertEqual(result["signals"], [])

    def test_one_invented_signal_removes_all_warning_evidence(self):
        invented = {**SIGNAL, "evidence": "존재하지 않는 지급 조건"}
        result, _, _ = self.analyze_with_response(ADVANCE_TEXT, payload=response_body([SIGNAL, invented]))
        self.assertEqual(result["status"], "error")
        self.assertEqual(result["signals"], [])

    def test_structured_output_and_request_privacy_options(self):
        context = "사이트 전체 내용이나 업체정보 없이\n" + ADVANCE_TEXT
        _, requests, options = self.analyze_with_response(context)
        self.assertEqual(options[0]["max_retries"], 0)
        self.assertGreater(options[0]["timeout"], 0)
        self.assertLessEqual(options[0]["timeout"], 30)
        body = requests[0]
        self.assertEqual(body["model"], "gpt-6-luna")
        self.assertIs(body["store"], False)
        self.assertEqual(body["text"]["format"]["type"], "json_schema")
        self.assertIs(body["text"]["format"]["strict"], True)
        self.assertIs(body["text"]["format"]["schema"]["additionalProperties"], False)
        self.assertIn(ADVANCE_TEXT, body["input"])
        self.assertNotIn("business_info", body)
        self.assertNotIn("url", body)

    def test_model_override_uses_environment(self):
        os.environ["OPENAI_MODEL"] = "configured-test-model"
        _, requests, _ = self.analyze_with_response(ADVANCE_TEXT)
        self.assertEqual(requests[0]["model"], "configured-test-model")

    def test_timeout_is_error_and_has_no_retries(self):
        result, requests, _ = self.analyze_with_response(ADVANCE_TEXT, timeout=True)
        self.assertEqual(result, {"status": "error", "error": "OPENAI_TIMEOUT", "signals": []})
        self.assertEqual(len(requests), 1)

    def test_api_errors_have_no_sdk_details_or_retries(self):
        for status in (401, 429, 503):
            with self.subTest(status=status):
                result, requests, _ = self.analyze_with_response(
                    ADVANCE_TEXT,
                    status_code=status,
                    payload={"error": {"message": "private API diagnostic", "type": "test_error"}},
                )
                self.assertEqual(result, {"status": "error", "error": "OPENAI_API_ERROR", "signals": []})
                self.assertEqual(len(requests), 1)
                self.assertNotIn("private API diagnostic", json.dumps(result))

    def test_invalid_json_is_error(self):
        result, _, _ = self.analyze_with_response(ADVANCE_TEXT, payload=response_body(text="not JSON"))
        self.assertEqual(result, {"status": "error", "error": "INVALID_AI_RESPONSE", "signals": []})

    def test_invalid_signal_schema_is_error(self):
        invalid_signals = [
            [{**SIGNAL, "type": "SOMETHING_ELSE"}],
            [{**SIGNAL, "evidence": ""}],
            [{**SIGNAL, "evidence": "   "}],
            [{**SIGNAL, "reason": ""}],
            [{**SIGNAL, "reason": "a" * 501}],
            [{**SIGNAL, "unexpected": "field"}],
            [SIGNAL] * 6,
        ]
        for signals in invalid_signals:
            with self.subTest(signals=signals):
                result, _, _ = self.analyze_with_response(ADVANCE_TEXT, payload=response_body(signals))
                self.assertEqual(result, {"status": "error", "error": "INVALID_AI_RESPONSE", "signals": []})

    def test_incomplete_and_refused_responses_are_errors(self):
        for payload in (response_body([SIGNAL], status="incomplete"), response_body(refusal=True)):
            with self.subTest(payload=payload):
                result, _, _ = self.analyze_with_response(ADVANCE_TEXT, payload=payload)
                self.assertEqual(result, {"status": "error", "error": "INVALID_AI_RESPONSE", "signals": []})
