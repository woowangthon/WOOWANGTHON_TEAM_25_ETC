"""Observed site criteria and the shared 50-point budget, without network I/O."""

from copy import deepcopy
from datetime import datetime, timezone as dt_timezone
from unittest.mock import patch

from django.test import SimpleTestCase

from ai.scoring import score_ai_analysis
from ai.test_flow import observation
from ai.test_scoring import DIRECT, FEE, URL, signal
from risk_engine.services import calculate_risk


NOW = datetime(2026, 11, 15, 3, 0, tzinfo=dt_timezone.utc)


def record(status, evidence, *, path="/", scope="current_page", **extra):
    return {
        "status": status,
        "source_url": "https://shop.example.com" + path,
        "observed_at": "2026-11-15T03:00:00Z",
        "scope": scope,
        "evidence": evidence,
        **extra,
    }


def notices(*dates, confirmed=True):
    return record("present", "공지사항\n" + "\n".join(dates), path="/notices",
                  scope="linked_board", dates=list(dates), latest_date_confirmed=confirmed)


def checked(checks, *, status="skipped", source=None, signals=()):
    kwargs = {"site_checks": checks, "url": URL}
    if source is not None:
        kwargs["page_observations"] = [source]
    return score_ai_analysis({"status": status, "signals": list(signals)}, **kwargs)


class SiteSuspicionScoringTests(SimpleTestCase):
    def setUp(self):
        clock = patch("ai.site_checks.timezone.now", return_value=NOW)
        clock.start()
        self.addCleanup(clock.stop)

    def assert_budget(self, scored, points, raw):
        data = scored.analysis
        self.assertEqual(scored.score, points)
        self.assertEqual(data["risk_score"], points)
        self.assertEqual(data["raw_score"], raw)
        self.assertEqual(data["max_score"], 50)
        self.assertEqual(data["scoring_policy"], "business50_ai50_v3")
        self.assertEqual(data["capped"], raw is not None and raw > 50)
        self.assertEqual(sum(f["points"] for f in data["risk_factors"]), points or 0)
        return data

    def test_menu_awards_forty_even_when_linked_boards_require_login(self):
        checks = {
            "payment_menus": record("present", "출금 신청 · 충전신청"),
            "notices": record("unverified", "로그인 후 확인할 수 있습니다.", reason="login_required"),
            "reviews": record("unverified", "로그인이 필요합니다.", reason="login_required"),
        }
        result = checked(checks)
        data = self.assert_budget(result, 40, 40)
        self.assertEqual(data["scoring_status"], "rule_only")
        self.assertEqual([f["code"] for f in data["risk_factors"]], ["AI_PAYMENT_MENU"])
        self.assertEqual(data["risk_factors"][0]["source"], "site_rule")
        self.assertEqual(data["signals"], [])

    def test_either_menu_and_both_menus_award_once_not_twice(self):
        for text in ("출금신청", "충전 신청", "출금 신청 · 충전 신청"):
            with self.subTest(menu=text):
                data = self.assert_budget(checked({"payment_menus": record("present", text)}), 40, 40)
                self.assertEqual(len(data["risk_factors"]), 1)

    def test_settlement_only_is_not_the_requested_charge_withdraw_menu_rule(self):
        self.assertIsNone(checked({"payment_menus": record("present", "정산")}).score)

    def test_menu_absent_or_unverified_does_not_create_points(self):
        for status in ("absent", "unverified"):
            with self.subTest(status=status):
                data = self.assert_budget(checked({"payment_menus": record(status, "메뉴 미확인")}), None, None)
                self.assertEqual(data["scoring_status"], "unavailable")

    def test_missing_or_empty_notices_score_fifty(self):
        for status, evidence in (("missing", "확인한 공개 화면에서 공지사항을 찾지 못했습니다."),
                                 ("empty", "등록된 공지사항이 없습니다.")):
            with self.subTest(status=status):
                data = self.assert_budget(checked({"notices": record(status, evidence)}), 50, 50)
                self.assertEqual(data["risk_factors"][0]["code"], "AI_NOTICE_MISSING")

    def test_missing_or_empty_reviews_score_forty(self):
        for status, evidence in (("missing", "확인한 공개 화면에서 구매후기를 찾지 못했습니다."),
                                 ("empty", "등록된 구매후기가 없습니다.")):
            with self.subTest(status=status):
                data = self.assert_budget(checked({"reviews": record(status, evidence)}), 40, 40)
                self.assertEqual(data["risk_factors"][0]["code"], "AI_REVIEW_MISSING")

    def test_missing_reviews_and_notices_raw_ninety_are_capped_to_fifty(self):
        result = checked({"notices": record("empty", "공지사항이 없습니다."),
                          "reviews": record("empty", "구매후기가 없습니다.")})
        data = self.assert_budget(result, 50, 90)
        factors = {f["code"]: f for f in data["risk_factors"]}
        self.assertEqual(factors["AI_NOTICE_MISSING"]["raw_points"], 50)
        self.assertEqual(factors["AI_REVIEW_MISSING"]["raw_points"], 40)
        self.assertEqual(factors["AI_NOTICE_MISSING"]["points"], 28)
        self.assertEqual(factors["AI_REVIEW_MISSING"]["points"], 22)

    def test_all_three_site_rules_raw_one_thirty_are_capped(self):
        checks = {"payment_menus": record("present", "출금신청"),
                  "notices": record("missing", "공개 화면에서 공지사항을 찾지 못했습니다."),
                  "reviews": record("empty", "구매후기가 없습니다.")}
        data = self.assert_budget(checked(checks), 50, 130)
        self.assertEqual(sorted(f["raw_points"] for f in data["risk_factors"]), [40, 40, 50])
        self.assertEqual(data["signals"], [])

    def test_stale_notice_uses_most_recent_date_in_the_list(self):
        checks = {"notices": notices("2026-10-01", "2026.10.14", "2026/09/30")}
        data = self.assert_budget(checked(checks), 50, 50)
        notice = data["site_checks"]["notices"]
        self.assertEqual(notice["freshness"], "stale")
        self.assertEqual(notice["latest_date"], "2026-10-14")
        self.assertEqual(notice["cutoff_date"], "2026-10-15")
        self.assertEqual(data["risk_factors"][0]["code"], "AI_NOTICE_STALE")

    def test_notice_exactly_on_calendar_month_cutoff_is_recent(self):
        for date in ("2026-10-15", "2026-11-01", "2026-11-15", "11-01"):
            with self.subTest(date=date):
                result = checked({"notices": notices(date)}, status="completed", source=observation(FEE))
                data = self.assert_budget(result, 0, 0)
                self.assertEqual(data["site_checks"]["notices"]["freshness"], "recent")

    def test_date_separator_whitespace_is_parsed_without_rewriting_source_evidence(self):
        cases = (("2026 - 08 - 01", "stale", 50), ("2026. 08. 01", "stale", 50),
                 ("2026 / 10 / 15", "recent", 0), ("11 - 01", "recent", 0),
                 ("2026 - 11 - 16", "unverified", 0), ("2026 - 02 - 30", "unverified", 0))
        for raw, freshness, points in cases:
            with self.subTest(raw=raw):
                result = checked({"notices": notices(raw)}, status="completed", source=observation(FEE))
                data = self.assert_budget(result, points, points)
                notice = data["site_checks"]["notices"]
                self.assertEqual(notice["dates"], [raw])
                self.assertIn(raw, notice["evidence"])
                self.assertEqual(notice["freshness"], freshness)
                if points:
                    self.assertIn(raw, data["risk_factors"][0]["evidence"][0]["evidence"])

    def test_recent_notice_is_not_overridden_by_old_pinned_notice(self):
        result = checked({"notices": notices("2024-01-01", "2026-11-14")},
                         status="completed", source=observation(FEE))
        self.assert_budget(result, 0, 0)
        self.assertEqual(result.analysis["site_checks"]["notices"]["latest_date"], "2026-11-14")

    def test_undated_unconfirmed_invalid_future_or_unquoted_dates_are_unverified(self):
        records = [notices(), notices("2026-09-01", confirmed=False), notices("2026-02-30"),
                   notices("2026-11-16"), notices("2026-09-01", "2026-11-16"),
                   {**notices("2026-09-01"), "evidence": "공지사항 날짜를 읽지 못했습니다."}]
        for item in records:
            with self.subTest(record=item):
                result = checked({"notices": item}, status="completed", source=observation(FEE))
                data = self.assert_budget(result, 0, 0)
                self.assertEqual(data["site_checks"]["notices"]["freshness"], "unverified")

    def test_calendar_month_end_and_korean_local_day_are_used(self):
        # UTC March30 16:00 is March31 in Korea; a calendar month ago is Feb28,
        # not a fixed 30-day window and not UTC's March30 date.
        with patch("ai.site_checks.timezone.now", return_value=datetime(2027, 3, 30, 16, tzinfo=dt_timezone.utc)):
            recent = checked({"notices": notices("2027-02-28")}, status="completed", source=observation(FEE))
            self.assertEqual(recent.score, 0)
            self.assertEqual(recent.analysis["site_checks"]["notices"]["cutoff_date"], "2027-02-28")
            stale = checked({"notices": notices("2027-02-27")})
            self.assertEqual(stale.score, 50)

    def test_model_failure_keeps_site_facts_but_not_a_completed_model_claim(self):
        for status in ("skipped", "error"):
            with self.subTest(status=status):
                data = self.assert_budget(checked({"reviews": record("empty", "구매후기가 없습니다.")}, status=status), 40, 40)
                self.assertEqual(data["status"], status)
                self.assertEqual(data["scoring_status"], "rule_only")

    def test_site_claims_that_reveal_login_or_fetch_failure_are_not_absence(self):
        for reason in ("login_required", "timeout", "unavailable"):
            checks = {"notices": record("missing", "로그인 후 확인할 수 있습니다.", reason=reason),
                      "reviews": record("empty", "로그인 후 확인할 수 있습니다.", reason=reason)}
            with self.subTest(reason=reason):
                self.assert_budget(checked(checks), None, None)

    def test_direct_payout_signal_survives_proportional_cap_with_site_factors(self):
        source = observation(DIRECT)
        result = checked({"payment_menus": record("present", "출금 신청"),
                          "notices": record("empty", "공지사항이 없습니다."),
                          "reviews": record("empty", "구매후기가 없습니다.")},
                         status="completed", source=source, signals=[signal(source)])
        data = self.assert_budget(result, 50, 180)
        direct = next(f for f in data["risk_factors"] if f["code"] == "AI_ADVANCE_PAYMENT_REQUIRED")
        self.assertLess(direct["points"], 50)
        self.assertEqual(direct["raw_points"], 50)
        risk = calculate_risk({"ai": data}, ai_scoring=result)
        self.assertEqual(risk["verdict"], "HIGH_RISK")

    def test_full_site_score_without_direct_quote_is_not_direct_high_risk(self):
        result = checked({"notices": record("empty", "공지사항이 없습니다.")})
        risk = calculate_risk({"ai": result.analysis}, ai_scoring=result)
        self.assertEqual(risk["risk_score"], 50)
        self.assertEqual(risk["verdict"], "CAUTION")
        self.assertFalse(any(f["code"] == "AI_ADVANCE_PAYMENT_REQUIRED" for f in risk["risk_factors"]))

    def test_model_numeric_fields_cannot_invent_site_points(self):
        original = {"status": "completed", "signals": [], "risk_score": 999,
                    "raw_score": 999, "risk_factors": [{"code": "AI_NOTICE_MISSING", "points": 50}]}
        result = score_ai_analysis(original, page_observations=[observation(FEE)], url=URL)
        self.assert_budget(result, 0, 0)

    def test_inputs_are_not_mutated_when_normalizing_and_allocating(self):
        checks = {"notices": notices("2026.09.01"), "reviews": record("empty", "구매후기가 없습니다.")}
        before = deepcopy(checks)
        self.assert_budget(checked(checks), 50, 90)
        self.assertEqual(checks, before)
