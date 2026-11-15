"""Real ORM regressions for public-only caching; model and public HTTP are mocked.

The same page can reveal a new withdrawal requirement later. Public business
lookups may be reused, but neither AI evidence nor a complete old response may
replace analysis of the current request.
"""

import copy
import json
import logging
from datetime import timedelta
from unittest.mock import patch

from django.db import DatabaseError
from django.test import TestCase, override_settings
from django.utils import timezone
from rest_framework.test import APIClient

from scan.models import ScanCache


FEE_TEXT = "판매대금에서 수수료를 차감한 뒤 지급합니다."
DIRECT_TEXT = "출금 제한을 풀려면 5만 원을 먼저 입금하세요."


@override_settings(ALLOWED_HOSTS=["testserver"])
class PublicBusinessCacheTests(TestCase):
    def setUp(self):
        previous = logging.root.manager.disable
        logging.disable(logging.CRITICAL)
        self.addCleanup(logging.disable, previous)
        self.client = APIClient()
        self.url = "https://shop.example.com/withdraw"
        self.business = {
            "business_name": "합성상점", "representative_name": "홍길동",
            "business_number": "123-45-67890", "address": "합성시 합성로 1",
        }
        self.nts_result = {"status": "completed", "business_status": "ACTIVE"}
        self.ftc_result = {
            "status": "completed", "search_method": "BUSINESS_NUMBER",
            "match_status": "NO_MATCH", "candidates": [],
        }
        nts = patch("scan.services.lookup_nts", return_value=self.nts_result)
        ftc = patch("scan.services.lookup_ftc", return_value=self.ftc_result)
        analyzer = patch("scan.services.analyze_page_context", side_effect=self.analyze_current)
        self.nts, self.ftc, self.analyzer = nts.start(), ftc.start(), analyzer.start()
        self.addCleanup(nts.stop)
        self.addCleanup(ftc.stop)
        self.addCleanup(analyzer.stop)
        # The cache tests exercise local business/source scoring, not the SDK.
        guard = patch("requests.sessions.Session.request", side_effect=AssertionError("External HTTP disabled"))
        guard.start()
        self.addCleanup(guard.stop)

    @staticmethod
    def analyze_current(context, *, page_observations=None):
        signals = []
        for source in page_observations or []:
            if DIRECT_TEXT in source["text"]:
                signals.append({
                    "type": "ADVANCE_PAYMENT_REQUIRED", "evidence": DIRECT_TEXT,
                    "reason": "현재 출처의 출금 조건에서 별도 입금을 확인했습니다.",
                    "source_id": source["id"], "source_url": source["source_url"],
                    "kind": source["kind"], "observed_at": source["observed_at"],
                    "source": "ai",
                })
        return {"status": "completed", "signals": signals, "assessment": {
            "state": "advance_payment_required" if signals else "unverified",
            "withdrawal_status": "risk_detected" if signals else "unverified",
            "observed_stages": ["ADVANCE_PAYMENT_REQUIRED"] if signals else [],
        }}

    def post(self, text=FEE_TEXT, *, url=None, business=None, source_id="current"):
        payload = {
            "url": self.url if url is None else url,
            "business_info": copy.deepcopy(self.business if business is None else business),
            "page_context": text,
            "page_observations": [{
                "id": source_id, "source_url": "https://shop.example.com/guide",
                "kind": "current_page", "text": text,
                "observed_at": timezone.now().isoformat(),
            }],
        }
        response = self.client.post("/api/v1/scan/", payload, format="json")
        self.assertEqual(response.status_code, 200, response.content)
        result = response.json()
        self.assertEqual(sum(factor["points"] for factor in result["risk_factors"]), result["risk_score"])
        self.assertEqual(result["public_api_cache"]["ttl_hours"], 24)
        return result

    def test_same_url_reuses_public_results_but_new_ai_requirement_is_analyzed(self):
        before = self.post(FEE_TEXT, source_id="before")
        after = self.post(DIRECT_TEXT, source_id="after")
        self.assertEqual(self.nts.call_count, 1)
        self.assertEqual(self.ftc.call_count, 1)
        self.assertEqual(self.analyzer.call_count, 2)
        self.assertEqual(before["analyses"]["ai"]["risk_score"], 0)
        self.assertEqual(after["analyses"]["ai"]["risk_score"], 50)
        self.assertEqual(after["risk_score"], 90)
        self.assertEqual(after["verdict"], "HIGH_RISK")
        self.assertNotEqual(before["scan_id"], after["scan_id"])
        self.assertEqual(before["public_api_cache"]["status"], "miss")
        self.assertEqual(after["public_api_cache"]["status"], "hit")
        self.assertTrue(any(signal["source_id"] == "after" and signal["evidence"] == DIRECT_TEXT
                            for signal in after["analyses"]["ai"]["signals"]))
        self.assertNotIn("before", json.dumps(after["analyses"]["ai"], ensure_ascii=False))

    def test_cache_persists_only_public_results_not_ai_input_or_query_tokens(self):
        url = self.url + "?token=PRIVATE_URL_TOKEN&phone=PRIVATE_PHONE#PRIVATE_FRAGMENT"
        result = self.post(DIRECT_TEXT, url=url)
        row = ScanCache.objects.latest("created_at")
        stored = json.dumps({"url": row.url, "result": row.result}, ensure_ascii=False)
        self.assertEqual(row.url, self.url)
        self.assertNotIn(DIRECT_TEXT, stored)
        self.assertNotIn("PRIVATE_URL_TOKEN", stored)
        self.assertNotIn("PRIVATE_PHONE", stored)
        self.assertNotIn("PRIVATE_FRAGMENT", stored)
        self.assertNotIn("page_context", row.result)
        self.assertNotIn("business_info", row.result)
        self.assertNotIn("ai", row.result.get("analyses", {}))
        self.assertNotIn("risk_score", row.result)
        self.assertEqual(set(row.result["analyses"]), {"nts", "ftc"})
        self.assertEqual(result["url"], url)  # Original request echo is unaffected.
        second = self.post(FEE_TEXT, url=self.url + "?token=OTHER_PRIVATE_TOKEN")
        self.assertEqual(second["public_api_cache"]["status"], "hit")
        self.assertEqual(self.nts.call_count, 1)
        self.assertEqual(self.ftc.call_count, 1)
        self.assertEqual(self.analyzer.call_count, 2)

    def test_expired_public_results_are_removed_and_fetched_again(self):
        self.post()
        old = ScanCache.objects.latest("created_at")
        ScanCache.objects.filter(pk=old.pk).update(created_at=timezone.now() - timedelta(hours=25))
        result = self.post()
        self.assertEqual(result["public_api_cache"]["status"], "miss")
        self.assertEqual(self.nts.call_count, 2)
        self.assertEqual(self.ftc.call_count, 2)
        self.assertFalse(ScanCache.objects.filter(pk=old.pk).exists())

    def test_different_business_identity_misses_the_previous_cache(self):
        self.post()
        other = {**self.business, "business_number": "123-45-67891"}
        result = self.post(business=other)
        self.assertEqual(self.nts.call_count, 2)
        self.assertEqual(self.ftc.call_count, 2)
        self.assertEqual(result["public_api_cache"]["status"], "miss")

    def test_malformed_displayed_number_reuses_name_result_but_never_queries_nts(self):
        malformed = {**self.business, "business_number": "", "displayed_business_number": "125-86-112516"}
        self.ftc.return_value = {
            "status": "completed", "search_method": "BUSINESS_NAME", "match_status": "NO_MATCH", "candidates": [],
        }
        first = self.post(business=malformed)
        second = self.post(business=malformed)
        self.nts.assert_not_called()
        self.assertEqual(self.ftc.call_count, 1)
        self.assertEqual(first["risk_score"], 50)
        self.assertEqual(second["risk_score"], 50)
        self.assertEqual(second["analyses"]["business_input"]["displayed_value"], "125-86-112516")
        self.assertEqual(second["public_api_cache"]["status"], "hit")
        for result in (first, second):
            combination = [factor for factor in result["risk_factors"]
                           if factor["code"] == "MALFORMED_NUMBER_AND_FTC_NAME_NOT_FOUND"]
            self.assertEqual(len(combination), 1)
            self.assertEqual(combination[0]["points"], 5)
            self.assertEqual(result["scoring_policy"], "business50_ai50_v3")
        self.assertNotIn("nts", ScanCache.objects.latest("created_at").result["analyses"])

    def test_changing_from_malformed_to_valid_number_does_not_reuse_name_lookup(self):
        malformed = {**self.business, "business_number": "", "displayed_business_number": "125-86-112516"}
        self.ftc.return_value = {"status": "completed", "search_method": "BUSINESS_NAME", "match_status": "NO_MATCH", "candidates": []}
        self.post(business=malformed)
        self.ftc.return_value = self.ftc_result
        result = self.post(business=self.business)
        self.nts.assert_called_once()
        self.assertEqual(self.ftc.call_count, 2)
        self.assertEqual(result["analyses"]["ftc"]["search_method"], "BUSINESS_NUMBER")
        self.assertEqual(result["risk_score"], 40)

    def test_cached_name_result_recalculates_new_malformed_combination(self):
        no_number = {**self.business, "business_number": ""}
        self.ftc.return_value = {
            "status": "completed", "search_method": "BUSINESS_NAME", "match_status": "NO_MATCH", "candidates": [],
        }
        before = self.post(business=no_number)
        after = self.post(business={**no_number, "displayed_business_number": "125-86-112516"})
        self.assertEqual(before["risk_score"], 25)
        self.assertEqual(after["risk_score"], 50)
        self.assertEqual(after["public_api_cache"]["status"], "hit")
        self.assertEqual(self.ftc.call_count, 1)
        self.nts.assert_not_called()
        codes = [factor["code"] for factor in after["risk_factors"]]
        self.assertEqual(codes.count("MALFORMED_NUMBER_AND_FTC_NAME_NOT_FOUND"), 1)
        self.assertEqual(sum(factor["points"] for factor in after["risk_factors"]), 50)
        stored = json.dumps(ScanCache.objects.latest("created_at").result, ensure_ascii=False)
        self.assertNotIn("MALFORMED_NUMBER_AND_FTC_NAME_NOT_FOUND", stored)
        self.assertNotIn("risk_score", stored)

    def test_legacy_whole_response_cache_is_not_reused(self):
        ScanCache.objects.create(
            url=self.url, business_name=self.business["business_name"],
            representative_name=self.business["representative_name"],
            business_number="1234567890", address=self.business["address"],
            result={"risk_score": 0, "analyses": {"nts": self.nts_result, "ftc": self.ftc_result,
                                                "ai": {"status": "completed", "risk_score": 0}}},
        )
        result = self.post(DIRECT_TEXT)
        self.nts.assert_called_once()
        self.ftc.assert_called_once()
        self.assertEqual(result["analyses"]["ai"]["risk_score"], 50)
        self.assertEqual(result["risk_score"], 90)
        self.assertEqual(result["public_api_cache"]["status"], "miss")

    def test_wrong_cache_version_is_not_reused(self):
        ScanCache.objects.create(
            url=self.url, business_name=self.business["business_name"],
            representative_name=self.business["representative_name"],
            business_number="1234567890", address=self.business["address"],
            result={"cache_version": "old_policy", "analyses": {"nts": self.nts_result, "ftc": self.ftc_result}},
        )
        result = self.post()
        self.nts.assert_called_once()
        self.ftc.assert_called_once()
        self.assertEqual(result["public_api_cache"]["status"], "miss")

    def test_only_successful_ftc_is_cached_and_failed_nts_is_retried(self):
        self.nts.return_value = {"status": "error", "error": "NTS_API_SERVER_ERROR"}
        self.post()
        self.assertNotIn("nts", ScanCache.objects.latest("created_at").result["analyses"])
        self.nts.return_value = self.nts_result
        result = self.post()
        self.assertEqual(self.nts.call_count, 2)
        self.assertEqual(self.ftc.call_count, 1)
        self.assertEqual(result["analyses"]["nts"]["status"], "completed")

    def test_only_successful_nts_is_cached_and_failed_ftc_is_retried(self):
        self.ftc.side_effect = RuntimeError("mock public service failure")
        self.post()
        self.assertNotIn("ftc", ScanCache.objects.latest("created_at").result["analyses"])
        self.ftc.side_effect = None
        result = self.post()
        self.assertEqual(self.nts.call_count, 1)
        self.assertEqual(self.ftc.call_count, 2)
        self.assertEqual(result["analyses"]["ftc"]["status"], "completed")

    def test_partial_success_does_not_refresh_the_older_public_result_ttl(self):
        self.ftc.side_effect = RuntimeError("mock FTC failure")
        self.post()
        nts_row = ScanCache.objects.latest("created_at")
        ScanCache.objects.filter(pk=nts_row.pk).update(created_at=timezone.now() - timedelta(hours=23))
        self.ftc.side_effect = None
        self.post()
        fresh_ftc_row = ScanCache.objects.latest("created_at")
        self.assertNotIn("nts", fresh_ftc_row.result["analyses"])
        self.assertIn("ftc", fresh_ftc_row.result["analyses"])
        ScanCache.objects.filter(pk=nts_row.pk).update(created_at=timezone.now() - timedelta(hours=25))
        result = self.post()
        self.assertEqual(self.nts.call_count, 2)
        self.assertEqual(self.ftc.call_count, 2)
        self.assertEqual(result["public_api_cache"]["status"], "hit")
        self.assertFalse(ScanCache.objects.filter(pk=nts_row.pk).exists())

    def test_cache_read_database_failure_preserves_current_scoring_response(self):
        with patch("scan.cache.ScanCache.objects.filter", side_effect=DatabaseError("mock database failure")):
            result = self.post(DIRECT_TEXT)
        self.assertEqual(result["risk_score"], 90)
        self.assertEqual(result["analyses"]["ai"]["risk_score"], 50)
        self.nts.assert_called_once()
        self.ftc.assert_called_once()
        self.assertEqual(result["public_api_cache"]["status"], "unavailable")

    def test_cache_write_database_failure_preserves_current_scoring_response(self):
        with patch("scan.cache.ScanCache.objects.create", side_effect=DatabaseError("mock database failure")):
            result = self.post(DIRECT_TEXT)
        self.assertEqual(result["risk_score"], 90)
        self.assertEqual(result["analyses"]["ai"]["risk_score"], 50)
        self.assertEqual(ScanCache.objects.count(), 0)
        self.assertNotEqual(result["public_api_cache"]["status"], "hit")
