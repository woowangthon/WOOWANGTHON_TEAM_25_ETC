"""Public-main FTC merge regressions, with the network client replaced in memory."""

from unittest.mock import patch

from django.test import SimpleTestCase

from .services import (
    compare_address, compare_name, compare_representative, lookup_business,
)


def candidate(name="주식회사 테스트상점", *, number="1234567890", representatives="홍길동, 김민수",
              address="서울특별시 강남구 테헤란로 123 4층"):
    return {
        "bzmnNm": name, "brno": number, "rprsvNm": representatives,
        "lctnAddr": address, "rnAddr": address, "operSttusCdNm": "정상영업",
    }


class FtcMainMergeTests(SimpleTestCase):
    def test_registered_name_comparison_ignores_legal_prefix_unicode_spacing_and_commas(self):
        self.assertEqual(compare_name("㈜ 테스트, 상점", "주식회사테스트상점"), "MATCH")
        self.assertEqual(compare_name("（주）테스트 상점", "테스트상점"), "MATCH")
        self.assertEqual(compare_name("테스트상점", "다른상점"), "MISMATCH")
        self.assertEqual(compare_name(None, "테스트상점"), "UNKNOWN")

    def test_name_search_normalizes_query_and_preserves_single_candidate_verification(self):
        with (patch("external_api.ftc.services.get_business_info") as by_number,
              patch("external_api.ftc.services.get_business_info_by_name", return_value=[candidate()]) as by_name):
            result = lookup_business({
                "business_name": "  ㈜ 테스트 상점  ", "representative_name": "홍 길동, 김 민수",
                "address": "서울특별시 강남구 테헤란로 123",
            })
        by_number.assert_not_called()
        by_name.assert_called_once_with("주식회사테스트상점")
        self.assertEqual(result["search_method"], "BUSINESS_NAME")
        self.assertEqual(result["match_status"], "SINGLE_CANDIDATE")
        self.assertEqual(result["verification"]["business_name"], "MATCH")
        self.assertEqual(result["verification"]["representative_name"], "MATCH")
        self.assertEqual(result["verification"]["business_address"], "MATCH")

    def test_multiple_representatives_distinguish_full_partial_masked_and_unknown_matches(self):
        for supplied, official, expected in (
            ("홍 길동, 김민수", ["홍길동", "김 민수"], "MATCH"),
            ("홍길동，김민수", ["홍길동", "김민수"], "MATCH"),
            ("홍길동、김민수", ["홍길동", "김민수"], "MATCH"),
            ("홍길동,이순신", ["홍길동", "김민수"], "PARTIAL_MATCH"),
            ("이순신", ["홍길동", "김민수"], "MISMATCH"),
            ("홍길동", ["홍*동", "김＊수"], "UNKNOWN"),
            (None, ["홍길동"], "UNKNOWN"),
        ):
            with self.subTest(supplied=supplied, official=official):
                self.assertEqual(compare_representative(supplied, official), expected)

    def test_romanized_name_with_parenthesized_korean_name_matches_registered_name(self):
        for supplied, official in (
            ("CHANGSEUNGHWAN(장승환)", ["장승환"]),
            ("CHANGSEUNGHWAN（장승환）", ["장승환"]),
            ("장승환", ["CHANGSEUNGHWAN(장승환)"]),
            ("CHANGSEUNGHWAN(장승환),김민수", ["장승환", "김민수"]),
        ):
            with self.subTest(supplied=supplied, official=official):
                self.assertEqual(compare_representative(supplied, official), "MATCH")

    def test_address_containment_and_regional_difference_remain_distinct(self):
        official = "서울특별시 강남구 테헤란로 123, 4층"
        self.assertEqual(compare_address("서울특별시 강남구 테헤란로 123", official), "MATCH")
        self.assertEqual(compare_address("서울특별시 강남구 봉은사로 9", official), "PARTIAL_MATCH")
        self.assertEqual(compare_address("부산광역시 해운대구 해운대로 5", official), "MISMATCH")
        self.assertEqual(compare_address(None, official), "UNKNOWN")

    def test_business_number_search_takes_priority_without_relying_on_name_query(self):
        with (patch("external_api.ftc.services.get_business_info", return_value=[candidate()]) as by_number,
              patch("external_api.ftc.services.get_business_info_by_name") as by_name):
            result = lookup_business({"business_number": " 123-45-67890 ",
                                      "business_name": "테스트상점", "representative_name": "홍길동"})
        by_number.assert_called_once_with("123-45-67890")
        by_name.assert_not_called()
        self.assertEqual(result["search_method"], "BUSINESS_NUMBER")
        self.assertEqual(result["verification"]["business_number"], "MATCH")
        self.assertEqual(result["verification"]["business_address"], "SKIPPED")

    def test_ambiguous_candidates_are_not_silently_selected(self):
        rows = [candidate(number="1234567890"), candidate(number="1234567891")]
        with patch("external_api.ftc.services.get_business_info_by_name", return_value=rows):
            result = lookup_business({"business_name": "테스트상점", "representative_name": "홍길동"})
        self.assertEqual(result["match_status"], "MULTIPLE_MATCHES")
        self.assertEqual(len(result["candidates"]), 2)
        self.assertNotIn("official_info", result)
        self.assertTrue(all("verification" in item for item in result["candidates"]))

    def test_registered_name_and_address_narrow_candidates_without_guessing_when_input_missing(self):
        rows = [candidate(name="다른상점", number="9999999999"),
                candidate(number="1234567890"),
                candidate(number="1234567891", address="부산광역시 해운대구 해운대로 5")]
        with patch("external_api.ftc.services.get_business_info_by_name", return_value=rows):
            result = lookup_business({"business_name": "테스트상점",
                                      "address": "서울특별시 강남구 테헤란로 123"})
        self.assertEqual(result["match_status"], "SINGLE_CANDIDATE")
        self.assertEqual(result["official_info"]["business_number"], "1234567890")
        with (patch("external_api.ftc.services.get_business_info") as by_number,
              patch("external_api.ftc.services.get_business_info_by_name") as by_name):
            skipped = lookup_business({})
        self.assertEqual(skipped["status"], "skipped")
        self.assertEqual(skipped["match_status"], "UNKNOWN")
        by_number.assert_not_called()
        by_name.assert_not_called()
