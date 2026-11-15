"""Suspicion scoring over source-validated observations, without network calls."""

from copy import deepcopy

from django.test import SimpleTestCase

from ai.scoring import score_ai_analysis
from ai.test_flow import observation


URL = "https://shop.example.com/withdrawal"
DIRECT = "세금 납부가 확인되면 수익금을 받을 수 있습니다."
FEE = "판매대금에서 수수료를 차감한 뒤 지급합니다."
REWARD = "리뷰를 작성하면 보상 포인트를 지급합니다."
EARNINGS = "현재 잔액: [개인 금액 제외]"
RESTRICTED = "현재 출금이 제한되어 있습니다."


def signal(source, type="ADVANCE_PAYMENT_REQUIRED", *, method="ai", evidence=None):
    return {
        "type": type, "source_id": source["id"], "source_url": source["source_url"],
        "kind": source["kind"], "observed_at": source["observed_at"],
        "evidence": source["text"] if evidence is None else evidence,
        "reason": "이 출처의 안내 문장을 확인했습니다.", "source": method,
    }


def scored(sources, signals=(), *, status="completed", **extras):
    analysis = {"status": status, "signals": list(signals), "assessment": {"state": "unverified", "withdrawal_status": "unverified", "observed_stages": []}, **extras}
    return score_ai_analysis(analysis, page_observations=sources, url=URL).analysis


class SuspicionScoringTests(SimpleTestCase):
    def assert_scored(self, result, points, status):
        self.assertEqual(result["risk_score"], points)
        self.assertEqual(result["max_score"], 50)
        self.assertEqual(result["scoring_status"], status)
        self.assertEqual(result["scoring_policy"], "business50_ai50_v3")
        self.assertEqual(sum(item["points"] for item in result["risk_factors"]), points or 0)

    def test_direct_deposit_scores_fifty_without_prior_stages(self):
        source = observation(DIRECT)
        result = scored([source], [signal(source)])
        self.assert_scored(result, 50, "completed")
        self.assertEqual(result["risk_factors"][0]["code"], "AI_ADVANCE_PAYMENT_REQUIRED")
        self.assertEqual(result["risk_factors"][0]["evidence"][0]["evidence"], DIRECT)
        self.assertEqual(result["risk_factors"][0]["evidence"][0]["source_url"], source["source_url"])

    def test_complete_three_stage_combination_scores_ten(self):
        sources = [observation(REWARD, id="reward", kind="history"),
                   observation(EARNINGS, id="earning", kind="history"),
                   observation(RESTRICTED, id="restriction")]
        signals = [signal(source, type) for source, type in zip(sources, (
            "REWARD_OFFER", "EARNINGS_DISPLAY", "WITHDRAWAL_RESTRICTED"))]
        result = scored(sources, signals)
        self.assert_scored(result, 10, "completed")
        self.assertEqual(result["risk_factors"][0]["code"], "AI_STAGED_FLOW")
        self.assertEqual({item["type"] for item in result["risk_factors"][0]["evidence"]},
                         {"REWARD_OFFER", "EARNINGS_DISPLAY", "WITHDRAWAL_RESTRICTED"})
        self.assertEqual(result["assessment"]["withdrawal_status"], "unverified")

    def test_individual_and_incomplete_stage_combinations_get_no_points(self):
        sources = [observation(REWARD, id="reward"), observation(EARNINGS, id="earning"),
                   observation(RESTRICTED, id="restriction")]
        signals = [signal(source, type) for source, type in zip(sources, (
            "REWARD_OFFER", "EARNINGS_DISPLAY", "WITHDRAWAL_RESTRICTED"))]
        for selected in ([signals[0]], [signals[1]], [signals[2]], signals[:2], signals[1:]):
            with self.subTest(types=[item["type"] for item in selected]):
                self.assert_scored(scored(sources, selected), 0, "completed")

    def test_direct_condition_replaces_combination_and_duplicate_points(self):
        sources = [observation(REWARD, id="reward"), observation(EARNINGS, id="earning"),
                   observation(RESTRICTED, id="restriction"), observation(DIRECT, id="direct")]
        signals = [signal(source, type) for source, type in zip(sources, (
            "REWARD_OFFER", "EARNINGS_DISPLAY", "WITHDRAWAL_RESTRICTED", "ADVANCE_PAYMENT_REQUIRED"))]
        result = scored(sources, signals + [signals[-1], signals[-1]])
        self.assert_scored(result, 50, "completed")
        self.assertEqual(len(result["risk_factors"]), 1)
        self.assertEqual(result["risk_factors"][0]["code"], "AI_ADVANCE_PAYMENT_REQUIRED")

    def test_same_stage_repeated_across_sources_does_not_replace_missing_stage(self):
        sources = [observation(REWARD, id=f"reward-{index}", path=f"/reward-{index}") for index in range(3)]
        result = scored(sources, [signal(source, "REWARD_OFFER") for source in sources])
        self.assert_scored(result, 0, "completed")

    def test_completed_real_guidance_has_zero_but_missing_input_remains_unavailable(self):
        for text in (FEE, "상품 구매에 사용할 포인트를 충전하세요."):
            with self.subTest(text=text):
                result = scored([observation(text)], [])
                self.assert_scored(result, 0, "completed")
                self.assertEqual(result["assessment"]["withdrawal_status"], "unverified")
        for analysis, sources, context in (
            ({"status": "completed"}, [observation(FEE)], ""),
            ({"status": "completed", "signals": []}, None, ""),
            ({"status": "completed", "signals": []}, [observation("출금 신청")], ""),
            ({"status": "completed", "signals": []}, None, "일반 상품 설명입니다."),
        ):
            with self.subTest(analysis=analysis, sources=sources, context=context):
                result = score_ai_analysis(analysis, page_observations=sources, page_context=context, url=URL).analysis
                self.assert_scored(result, None, "unavailable")

    def test_skipped_and_error_without_rule_evidence_remain_unknown(self):
        for status, reason in (("skipped", "MISSING_API_KEY"), ("error", "OPENAI_TIMEOUT")):
            with self.subTest(status=status):
                result = scored([observation(FEE)], [], status=status, reason=reason)
                self.assert_scored(result, None, "unavailable")
                self.assertEqual(result["status"], status)

    def test_verified_rule_fifty_survives_model_error_and_missing_key(self):
        source = observation(DIRECT)
        for status in ("skipped", "error"):
            with self.subTest(status=status):
                result = scored([source], [signal(source, method="rule")], status=status)
                self.assert_scored(result, 50, "rule_only")
                self.assertEqual(result["status"], status)
                self.assertEqual(result["risk_factors"][0]["source"], "rule")

    def test_fabricated_rule_provenance_cannot_score(self):
        source = observation(FEE)
        result = scored([source], [signal(source, method="rule")], status="error")
        self.assert_scored(result, None, "unavailable")

    def test_hallucinated_and_cross_source_evidence_cannot_score(self):
        first = observation(FEE, id="fee")
        other = observation("상품 구매에 사용할 포인트를 충전하세요.", id="points")
        bad = [
            {**signal(first), "evidence": "입력에 없는 선입금 요구입니다."},
            {**signal(first), "evidence": other["text"]},
            {**signal(first), "source_id": "invented"},
            {**signal(first), "source_url": "https://different.example.com/withdrawal"},
            {**signal(first), "kind": "history"},
            {**signal(first), "observed_at": "2026-01-01T00:00:00Z"},
            {**signal(first), "reason": " "},
        ]
        for item in bad:
            with self.subTest(item=item):
                result = scored([first, other], [item])
                self.assert_scored(result, None, "unavailable")
                self.assertEqual(result.get("scoring_error"), "INVALID_SCORING_EVIDENCE")

    def test_model_numeric_score_and_assessment_cannot_create_points(self):
        source = observation(FEE)
        result = scored([source], [], risk_score=999, max_score=999,
                        assessment={"state": "advance_payment_required", "observed_stages": [
                            "REWARD_OFFER", "EARNINGS_DISPLAY", "WITHDRAWAL_RESTRICTED",
                            "ADVANCE_PAYMENT_REQUIRED"]})
        self.assert_scored(result, 0, "completed")
        self.assertEqual(result["assessment"]["withdrawal_status"], "unverified")

    def test_legacy_context_retains_exact_grounded_score(self):
        analysis = {"status": "completed", "signals": [{
            "type": "ADVANCE_PAYMENT_REQUIRED", "evidence": DIRECT,
            "reason": "수익금 수령 조건으로 세금 납부를 요구합니다."}]}
        original = deepcopy(analysis)
        result = score_ai_analysis(analysis, page_context=DIRECT, url=URL).analysis
        self.assert_scored(result, 50, "completed")
        self.assertEqual(analysis, original)
        self.assertEqual(result["signals"][0]["evidence"], DIRECT)

    def test_maximum_valid_source_bundle_keeps_rule_score_above_thirty_two_signals(self):
        texts = (REWARD, EARNINGS, RESTRICTED, DIRECT)
        types = ("REWARD_OFFER", "EARNINGS_DISPLAY", "WITHDRAWAL_RESTRICTED", "ADVANCE_PAYMENT_REQUIRED")
        sources = [observation("\n".join(texts), id=f"page-{index}", path=f"/page-{index}")
                   for index in range(10)]
        self.assertLessEqual(sum(len(source["text"]) for source in sources), 2000)
        signals = [signal(source, type, method="rule", evidence=text)
                   for source in sources for text, type in zip(texts, types)]
        self.assertEqual(len(signals), 40)
        result = scored(sources, signals, status="error", error="OPENAI_TIMEOUT")
        self.assert_scored(result, 50, "rule_only")
        self.assertEqual(len(result["risk_factors"]), 1)

    def test_unrelated_existing_context_cannot_ground_legacy_evidence(self):
        result = score_ai_analysis({"status": "completed", "signals": [{
            "type": "ADVANCE_PAYMENT_REQUIRED", "evidence": DIRECT, "reason": "조건 확인"}]},
            page_context=FEE, url=URL).analysis
        self.assert_scored(result, None, "unavailable")
