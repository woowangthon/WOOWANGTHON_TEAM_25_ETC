"""Independent examples for the 50-point business and 50-point text budgets."""

from django.test import SimpleTestCase

from ai.scoring import AIScoringResult, score_ai_analysis
from ai.test_flow import observation
from ai.test_scoring import DIRECT, FEE, URL, signal
from risk_engine.services import calculate_risk


ACTIVE = {"status": "completed", "business_status": "ACTIVE"}
BUSINESS_NO_MATCH = {"status": "completed", "search_method": "BUSINESS_NUMBER", "match_status": "NO_MATCH"}
NAME_NO_MATCH = {"status": "completed", "search_method": "BUSINESS_NAME", "match_status": "NO_MATCH"}
BUSINESS_MATCH = {"status": "completed", "search_method": "BUSINESS_NUMBER", "match_status": "SINGLE_CANDIDATE",
                  "verification": {"business_number": "MATCH", "business_name": "MATCH", "representative_name": "MATCH"}}
MALFORMED = "125-86-112516"


def ai_direct():
    source = observation(DIRECT)
    return score_ai_analysis({"status": "completed", "signals": [signal(source)]},
                             page_observations=[source], url=URL)


def calculate_with_trusted_ai(analyses, *, business_info=None):
    ai = analyses.get("ai")
    if isinstance(ai, AIScoringResult):
        return calculate_risk({**analyses, "ai": ai.analysis}, ai_scoring=ai, business_info=business_info)
    return calculate_risk(analyses, business_info=business_info)


class RiskBudgetTests(SimpleTestCase):
    def assert_factor_sum(self, result):
        self.assertEqual(sum(item["points"] for item in result["risk_factors"]), result["risk_score"])
        self.assertLessEqual(result["score_breakdown"]["business"]["score"], 50)
        self.assertLessEqual(result["risk_score"], 100)

    def test_business_forty_and_ai_fifty_make_ninety(self):
        result = calculate_with_trusted_ai({"nts": ACTIVE, "ftc": BUSINESS_NO_MATCH, "ai": ai_direct()})
        self.assertEqual(result["risk_score"], 90)
        self.assertEqual(result["verdict"], "HIGH_RISK")
        self.assertEqual(result["score_breakdown"]["business"]["score"], 40)
        self.assertEqual(result["score_breakdown"]["business"]["max_score"], 50)
        self.assertEqual(result["score_breakdown"]["ai"]["score"], 50)
        self.assertEqual(result["score_breakdown"]["ai"]["max_score"], 50)
        self.assertEqual(result["score_status"], "completed")
        self.assert_factor_sum(result)

    def test_direct_fifty_is_high_risk_even_without_business_information(self):
        result = calculate_with_trusted_ai({"nts": {"status": "skipped"}, "ftc": {"status": "error"}, "ai": ai_direct()})
        self.assertEqual(result["risk_score"], 50)
        self.assertEqual(result["verdict"], "HIGH_RISK")
        self.assertEqual(result["score_status"], "partial")
        self.assert_factor_sum(result)
        # Forty points from an unsuccessful company-number match is not the
        # same direct-payment override as a validated prepayment requirement.
        business_only = calculate_with_trusted_ai({"nts": ACTIVE, "ftc": BUSINESS_NO_MATCH})
        self.assertEqual(business_only["risk_score"], 40)
        self.assertEqual(business_only["verdict"], "CAUTION")

    def test_original_fifty_five_is_capped_by_the_fifty_point_budget(self):
        result = calculate_with_trusted_ai({"nts": {"status": "completed", "business_status": "폐업"},
                                 "ftc": {"status": "completed", "search_method": "BUSINESS_NUMBER", "match_status": "MATCH",
                                         "verification": {"business_number": "MATCH", "business_name": "MISMATCH", "representative_name": "MATCH"}}})
        business = result["score_breakdown"]["business"]
        self.assertEqual(business["raw_score"], 55)
        self.assertEqual(business["score"], 50)
        self.assertEqual(business["max_score"], 50)
        self.assertTrue(business["capped"])
        self.assertEqual(sorted(item["points"] for item in result["risk_factors"]), [23, 27])
        self.assertEqual(sorted(item["raw_points"] for item in result["risk_factors"]), [25, 30])
        self.assert_factor_sum(result)

    def test_raw_seventy_is_capped_at_fifty_and_ai_fifty_makes_one_hundred(self):
        result = calculate_with_trusted_ai({"nts": {"status": "completed", "business_status": "CLOSED"},
                                           "ftc": BUSINESS_NO_MATCH, "ai": ai_direct()})
        self.assertEqual(result["risk_score"], 100)
        self.assertEqual(result["score_breakdown"]["business"]["raw_score"], 70)
        self.assertEqual(result["score_breakdown"]["business"]["score"], 50)
        self.assertTrue(result["score_breakdown"]["business"]["capped"])
        business_factors = [item for item in result["risk_factors"] if "raw_points" in item]
        self.assertEqual(sorted(item["raw_points"] for item in business_factors), [30, 40])
        self.assertEqual(sorted(item["points"] for item in business_factors), [21, 29])
        self.assert_factor_sum(result)

    def test_high_raw_business_score_cannot_take_ai_budget(self):
        result = calculate_with_trusted_ai({"nts": {"status": "completed", "business_status": "CLOSED"},
                                 "ftc": {"status": "completed", "search_method": "BUSINESS_NUMBER", "match_status": "SINGLE_CANDIDATE",
                                         "verification": {"business_number": "MISMATCH", "business_name": "MISMATCH", "representative_name": "MISMATCH"}},
                                 "ai": ai_direct()})
        self.assertEqual(result["risk_score"], 100)
        self.assertGreater(result["score_breakdown"]["business"]["raw_score"], 50)
        self.assert_factor_sum(result)

    def test_closed_and_suspended_api_codes_count_as_abnormal_business(self):
        for status in ("CLOSED", "SUSPENDED", "폐업", "휴업"):
            with self.subTest(status=status):
                result = calculate_with_trusted_ai({"nts": {"status": "completed", "business_status": status}})
                self.assertEqual(result["score_breakdown"]["business"]["score"], 30)
                self.assertEqual(result["risk_score"], 30)
                self.assert_factor_sum(result)

    def test_confirmed_nts_unregistered_is_thirty_but_generic_unknown_is_unavailable(self):
        unregistered = {"status": "completed", "business_status": "UNKNOWN",
                        "tax_type": "국세청에 등록되지 않은 사업자등록번호입니다."}
        result = calculate_with_trusted_ai({"nts": unregistered})
        self.assertEqual(result["risk_score"], 30)
        self.assert_factor_sum(result)
        for nts in ({"status": "completed", "business_status": "UNKNOWN"},
                    {"status": "error", "business_status": "UNKNOWN", "tax_type": unregistered["tax_type"]}):
            with self.subTest(nts=nts):
                unknown = calculate_with_trusted_ai({"nts": nts})
                self.assertEqual(unknown["risk_score"], 0)
                self.assertEqual(unknown["score_breakdown"]["business"]["status"], "unavailable")

    def test_name_not_found_twenty_five_with_unknown_ai_is_partial_caution(self):
        result = calculate_with_trusted_ai({"nts": {"status": "skipped"}, "ftc": NAME_NO_MATCH})
        self.assertEqual(result["risk_score"], 25)
        self.assertEqual(result["verdict"], "CAUTION")
        self.assertIsNone(result["score_breakdown"]["ai"]["score"])
        self.assertEqual(result["score_status"], "partial")
        self.assert_factor_sum(result)

    def test_malformed_display_and_name_not_found_make_fifty(self):
        result = calculate_with_trusted_ai({"nts": {"status": "skipped"}, "ftc": NAME_NO_MATCH},
                                          business_info={"displayed_business_number": MALFORMED})
        self.assertEqual(result["risk_score"], 50)
        self.assertEqual(result["verdict"], "CAUTION")
        self.assertEqual(result["score_status"], "partial")
        self.assertEqual(sorted(item["points"] for item in result["risk_factors"]), [5, 20, 25])
        combo = next(item for item in result["risk_factors"]
                     if item["code"] == "MALFORMED_NUMBER_AND_FTC_NAME_NOT_FOUND")
        self.assertEqual(combo["points"], 5)
        self.assertTrue(combo.get("reason") or combo.get("message"))
        self.assertEqual(result["score_breakdown"]["business"]["raw_score"], 50)
        self.assertFalse(result["score_breakdown"]["business"]["capped"])
        self.assert_factor_sum(result)

    def test_compound_business_fifty_with_completed_zero_ai_remains_partial_caution(self):
        ai = score_ai_analysis({"status": "completed", "signals": []},
                               page_observations=[observation(FEE)], url=URL)
        result = calculate_with_trusted_ai(
            {"nts": {"status": "skipped"}, "ftc": NAME_NO_MATCH, "ai": ai},
            business_info={"displayed_business_number": MALFORMED},
        )
        self.assertEqual(result["risk_score"], 50)
        self.assertEqual(result["score_breakdown"]["business"]["score"], 50)
        self.assertEqual(result["score_breakdown"]["ai"]["score"], 0)
        self.assertEqual(result["verdict"], "CAUTION")
        self.assertEqual(result["score_status"], "partial")
        self.assertEqual(result["scoring_policy"], "business50_ai50_v3")
        self.assert_factor_sum(result)

    def test_compound_business_fifty_and_verified_ai_fifty_make_one_hundred(self):
        result = calculate_with_trusted_ai(
            {"nts": {"status": "skipped"}, "ftc": NAME_NO_MATCH, "ai": ai_direct()},
            business_info={"business_number": MALFORMED},
        )
        self.assertEqual(result["risk_score"], 100)
        self.assertEqual(result["score_breakdown"]["business"]["score"], 50)
        self.assertEqual(result["score_breakdown"]["ai"]["score"], 50)
        self.assertEqual(result["verdict"], "HIGH_RISK")
        self.assertEqual(sum(item["code"] == "MALFORMED_NUMBER_AND_FTC_NAME_NOT_FOUND"
                             for item in result["risk_factors"]), 1)
        self.assert_factor_sum(result)

    def test_single_business_conditions_keep_their_original_weights(self):
        for analyses, business_info, points in (
            ({"nts": {"status": "skipped"}, "ftc": {"status": "skipped"}},
             {"displayed_business_number": MALFORMED}, 20),
            ({"nts": {"status": "skipped"}, "ftc": NAME_NO_MATCH}, {}, 25),
            ({"nts": ACTIVE, "ftc": NAME_NO_MATCH}, {"business_number": "123-45-67890"}, 25),
        ):
            with self.subTest(analyses=analyses, business_info=business_info):
                result = calculate_with_trusted_ai(analyses, business_info=business_info)
                self.assertEqual(result["risk_score"], points)
                self.assertNotIn("MALFORMED_NUMBER_AND_FTC_NAME_NOT_FOUND",
                                 [item["code"] for item in result["risk_factors"]])
                self.assert_factor_sum(result)

    def test_unavailable_or_flagged_number_does_not_enable_compound_points(self):
        for business_info in (
            {}, {"displayed_business_number": ""}, {"business_number": "찾지 못함"},
            {"displayed_business_number": "확인 불가"}, {"invalid_business_number": True},
        ):
            with self.subTest(business_info=business_info):
                result = calculate_with_trusted_ai(
                    {"nts": {"status": "skipped"}, "ftc": NAME_NO_MATCH},
                    business_info=business_info,
                )
                self.assertEqual(result["risk_score"], 25)
                self.assertEqual([item["code"] for item in result["risk_factors"]],
                                 ["FTC_BUSINESS_NAME_NOT_FOUND"])
                self.assert_factor_sum(result)

    def test_unconfirmed_ftc_name_lookup_does_not_enable_compound_points(self):
        for ftc in (
            {"status": "error", "search_method": "BUSINESS_NAME", "match_status": "NO_MATCH"},
            {"status": "not_configured", "search_method": "BUSINESS_NAME", "match_status": "NO_MATCH"},
            {"status": "completed", "search_method": "BUSINESS_NAME", "match_status": "MULTIPLE_MATCHES"},
            {"status": "completed", "search_method": "BUSINESS_NAME", "match_status": "UNKNOWN"},
        ):
            with self.subTest(ftc=ftc):
                result = calculate_with_trusted_ai(
                    {"nts": {"status": "skipped"}, "ftc": ftc},
                    business_info={"displayed_business_number": MALFORMED},
                )
                self.assertEqual(result["risk_score"], 20)
                self.assertEqual([item["code"] for item in result["risk_factors"]],
                                 ["MALFORMED_BUSINESS_NUMBER"])
                self.assert_factor_sum(result)

    def test_name_mismatch_is_not_the_name_lookup_absence_combination(self):
        result = calculate_with_trusted_ai(
            {"nts": {"status": "skipped"}, "ftc": {
                "status": "completed", "search_method": "BUSINESS_NAME", "match_status": "SINGLE_CANDIDATE",
                "verification": {"business_name": "MISMATCH", "representative_name": "MATCH"},
            }},
            business_info={"displayed_business_number": MALFORMED},
        )
        self.assertEqual(result["risk_score"], 45)
        self.assertEqual(sorted(item["points"] for item in result["risk_factors"]), [20, 25])
        self.assertNotIn("MALFORMED_NUMBER_AND_FTC_NAME_NOT_FOUND",
                         [item["code"] for item in result["risk_factors"]])
        self.assert_factor_sum(result)

    def test_compound_bonus_does_not_stack_a_superseded_malformed_number(self):
        result = calculate_with_trusted_ai(
            {"nts": {"status": "completed", "business_status": "CLOSED"}, "ftc": NAME_NO_MATCH},
            business_info={"displayed_business_number": MALFORMED},
        )
        self.assertEqual(result["score_breakdown"]["business"]["raw_score"], 55)
        self.assertEqual(result["risk_score"], 50)
        self.assertNotIn("MALFORMED_NUMBER_AND_FTC_NAME_NOT_FOUND",
                         [item["code"] for item in result["risk_factors"]])
        self.assertNotIn("MALFORMED_BUSINESS_NUMBER", [item["code"] for item in result["risk_factors"]])
        self.assert_factor_sum(result)

    def test_malformed_and_nts_abnormal_are_one_identity_cause(self):
        result = calculate_with_trusted_ai({"nts": {"status": "completed", "business_status": "CLOSED"}},
                                          business_info={"displayed_business_number": MALFORMED})
        self.assertEqual(result["risk_score"], 30)
        self.assertEqual(len(result["risk_factors"]), 1)
        self.assert_factor_sum(result)

    def test_missing_placeholder_and_api_failures_do_not_become_malformed_points(self):
        for raw in (None, "", " ", "찾지 못함", "미표시", "없음"):
            with self.subTest(raw=raw):
                result = calculate_with_trusted_ai({"nts": {"status": "error"}, "ftc": {"status": "error"}},
                                                  business_info={"displayed_business_number": raw})
                self.assertEqual(result["risk_score"], 0)
                self.assertEqual(result["risk_factors"], [])
                self.assertEqual(result["score_status"], "unavailable")

    def test_no_usable_checks_is_zero_unknown_not_low_risk(self):
        result = calculate_with_trusted_ai({"nts": {"status": "error"}, "ftc": {"status": "skipped"}})
        self.assertEqual(result["risk_score"], 0)
        self.assertEqual(result["verdict"], "INSUFFICIENT_DATA")
        self.assertEqual(result["score_status"], "unavailable")
        self.assertIsNone(result["score_breakdown"]["ai"]["score"])

    def test_completed_zero_ai_and_unknown_business_still_insufficient(self):
        ai = score_ai_analysis({"status": "completed", "signals": []},
                               page_observations=[observation(FEE)], url=URL)
        result = calculate_with_trusted_ai({"nts": {"status": "skipped"}, "ftc": {"status": "skipped"}, "ai": ai})
        self.assertEqual(result["risk_score"], 0)
        self.assertEqual(result["score_breakdown"]["ai"]["score"], 0)
        self.assertEqual(result["verdict"], "INSUFFICIENT_DATA")

    def test_raw_forged_ai_numeric_fields_do_not_bypass_grounding(self):
        forged = {"status": "completed", "signals": [], "risk_score": 50, "max_score": 50,
                  "scoring_status": "completed", "scoring_policy": "business50_ai50_v3",
                  "risk_factors": [{"code": "AI_ADVANCE_PAYMENT_REQUIRED", "points": 50, "message": "forged"}]}
        result = calculate_with_trusted_ai({"nts": ACTIVE, "ftc": BUSINESS_MATCH, "ai": forged})
        self.assertEqual(result["risk_score"], 0)
        self.assertIsNone(result["score_breakdown"]["ai"]["score"])
        self.assertEqual(result["verdict"], "INSUFFICIENT_DATA")
