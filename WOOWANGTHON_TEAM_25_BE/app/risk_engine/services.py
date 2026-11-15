"""Versioned business (50) plus evidence-grounded AI (50) suspicion points."""

from copy import deepcopy
import re

from ai.scoring import AIScoringResult, AI_MAX_SCORE, AI_FACTOR_POINTS, SCORING_POLICY, cap_ai_factors
from .business_validation import assess_business_info

BUSINESS_MAX_SCORE = 50
TOTAL_MAX_SCORE = BUSINESS_MAX_SCORE + AI_MAX_SCORE


# risk_engine/services.py

def _add_factor(factors, checked_items, name, result, points, reason):
    """
    검증 결과에 따라 위험 점수를 추가한다.
    """
    if result not in ("MATCH", "MISMATCH", "PARTIAL_MATCH"):
        return 0

    checked_items.append(name)

    if result == "MISMATCH":
        factors.append({
            "code": name.upper() + "_MISMATCH",
            "name": name,
            "result": result,
            "points": points,
            "reason": reason,
        })
        return points

    return 0


def _check_nts(nts_result, factors, checked_items):
    """
    국세청 사업자 상태 확인
    """
    if not isinstance(nts_result, dict):
        return 0

    if nts_result.get("status") != "completed":
        return 0

    business_status = nts_result.get("business_status")

    if not _nts_determinate(nts_result):
        return 0

    checked_items.append("nts_business_status")
    if _nts_unregistered(nts_result):
        factors.append({
            "code": "NTS_UNREGISTERED", "name": "국세청 사업자 상태",
            "result": "UNREGISTERED", "points": 30,
            "reason": "국세청에서 등록되지 않은 사업자번호라고 응답했습니다.",
            "evidence": nts_result["tax_type"],
        })
        return 30

    status_text = str(business_status).strip()

    # 비정상 사업자 상태
    abnormal_keywords = ("폐업", "휴업", "말소", "등록취소")

    if (any(keyword in status_text for keyword in abnormal_keywords)
            or status_text.upper() in {"CLOSED", "SUSPENDED", "DEREGISTERED", "CANCELLED"}):
        factors.append({
            "code": "ABNORMAL_BUSINESS_STATUS",
            "name": "사업자 상태",
            "result": "MISMATCH",
            "points": 30,
            "reason": f"사업자 상태가 정상적이지 않습니다: {status_text}",
        })
        return 30

    return 0


def _check_ftc(ftc_result, factors, checked_items):
    if not isinstance(ftc_result, dict):
        return 0

    if ftc_result.get("status") != "completed":
        return 0

    search_method = ftc_result.get("search_method")
    match_status = ftc_result.get("match_status")

    # 검색 결과가 없는 경우
    if match_status == "NO_MATCH":
        # 사업자번호 검색 실패는 강한 위험 신호
        if search_method == "BUSINESS_NUMBER":
            checked_items.append("ftc_business_lookup")

            factors.append({
                "code": "FTC_NO_MATCH",
                "name": "공정위 업체 조회",
                "result": "NO_MATCH",
                "points": 40,
                "reason": "입력한 사업자등록번호와 일치하는 업체를 찾지 못했습니다.",
            })
            return 40
        if search_method == "BUSINESS_NAME":
            checked_items.append("ftc_business_name_lookup")
            factors.append({
                "code": "FTC_BUSINESS_NAME_NOT_FOUND",
                "message": "공정거래위원회에서 해당 상호명을 찾을 수 없습니다.",
                "points": 25,
            })
            return 25
        return 0

    # 여러 후보가 있으면 특정 업체를 판단하지 않음
    if match_status == "MULTIPLE_MATCHES":
        return 0

    official_info = ftc_result.get("official_info") or {}
    verification = ftc_result.get("verification")

    if not isinstance(verification, dict):
        verification = official_info.get("verification")

    if not isinstance(verification, dict):
        return 0

    # 법인명 검색은 추가적인 신원 확인이 필요함
    if search_method == "BUSINESS_NAME":
        representative = verification.get("representative_name")
        address = verification.get("business_address")

        identity_confirmed = (
            representative == "MATCH"
            or address in ("MATCH", "PARTIAL_MATCH")
        )

        if not identity_confirmed:
            return 0

    score = 0

    score += _add_factor(
        factors,
        checked_items,
        "business_number",
        verification.get("business_number"),
        40,
        "사업자등록번호가 공식 정보와 일치하지 않습니다.",
    )

    score += _add_factor(
        factors,
        checked_items,
        "business_name",
        verification.get("business_name"),
        25,
        "상호명이 공식 정보와 일치하지 않습니다.",
    )

    score += _add_factor(
        factors,
        checked_items,
        "representative_name",
        verification.get("representative_name"),
        15,
        "대표자 이름이 공식 정보와 일치하지 않습니다.",
    )

    return score



def _nts_unregistered(result):
    if not isinstance(result, dict) or result.get("status") != "completed":
        return False
    status = result.get("business_status")
    if status not in (None, "", "UNKNOWN"):
        return False
    tax_type = result.get("tax_type")
    return isinstance(tax_type, str) and bool(re.fullmatch(
        r"국세청에\s*등록되지\s*않은\s*사업자등록번호입니다[.!]?", tax_type.strip(),
    ))


def _check_number_format(assessment, factors, checked_items):
    if assessment["status"] == "unavailable":
        return 0
    checked_items.append("displayed_business_number_format")
    if assessment["status"] != "malformed":
        return 0
    count = assessment["digit_count"]
    reason = (f"페이지에 표시된 사업자번호는 숫자 {count}자리로 10자리 형식과 다릅니다."
              if count != 10 else "페이지에 표시된 사업자번호의 구분자 형식이 올바르지 않습니다.")
    factors.append({
        "code": "MALFORMED_BUSINESS_NUMBER", "name": "표시 사업자번호 형식",
        "result": "MALFORMED", "points": 20, "reason": reason,
        "evidence": assessment["displayed_value"], "digit_count": count,
    })
    return 20


def _check_business_identity_combination(factors, checked_items):
    """Raise the verified malformed-number/name-not-found pair to 50 points."""
    contributed = {factor["code"] for factor in factors}
    if not {"MALFORMED_BUSINESS_NUMBER", "FTC_BUSINESS_NAME_NOT_FOUND"} <= contributed:
        return 0
    checked_items.append("business_identity_combination")
    factors.append({
        "code": "MALFORMED_NUMBER_AND_FTC_NAME_NOT_FOUND",
        "name": "사업자번호 형식 오류·상호명 미조회 복합 근거",
        "result": "COMBINED",
        "points": 5,
        "reason": "사업자번호 형식 오류와 공정위 상호명 검색 결과 없음이 함께 확인되어 복합 위험 5점을 추가했습니다.",
    })
    return 5


def _nts_determinate(result):
    if not isinstance(result, dict) or result.get("status") != "completed":
        return False
    if _nts_unregistered(result):
        return True
    value = result.get("business_status")
    if not isinstance(value, str) or not value.strip():
        return False
    value = value.strip()
    return (value.upper() in {"ACTIVE", "CLOSED", "SUSPENDED", "DEREGISTERED", "CANCELLED"}
            or any(word in value for word in ("계속", "정상", "폐업", "휴업", "말소", "등록취소")))


def _ftc_determinate(result):
    if not isinstance(result, dict) or result.get("status") != "completed":
        return False
    match_status = result.get("match_status")
    if match_status in ("UNKNOWN", "MULTIPLE_MATCHES"):
        return False
    if match_status == "NO_MATCH":
        return result.get("search_method") in ("BUSINESS_NUMBER", "BUSINESS_NAME")
    verification = result.get("verification")
    if not isinstance(verification, dict):
        official = result.get("official_info")
        verification = official.get("verification") if isinstance(official, dict) else None
    return isinstance(verification, dict) and any(
        value in ("MATCH", "MISMATCH", "PARTIAL_MATCH") for value in verification.values()
    )


def _applied_business_factors(factors, raw_score):
    """Allocate the 50-point budget without changing the original raw weights."""
    result = deepcopy(factors)
    if raw_score <= BUSINESS_MAX_SCORE:
        return result
    numerators = [factor["points"] * BUSINESS_MAX_SCORE for factor in result]
    applied = [value // raw_score for value in numerators]
    remaining = BUSINESS_MAX_SCORE - sum(applied)
    order = sorted(range(len(result)), key=lambda index: (-(numerators[index] % raw_score), index))
    for index in order[:remaining]:
        applied[index] += 1
    for factor, points in zip(result, applied):
        factor["raw_points"] = factor["points"]
        factor["points"] = points
    return result


def _trusted_ai_component(ai_scoring):
    """Only accept an explicit server-scored value, never a model's JSON score."""
    if not isinstance(ai_scoring, AIScoringResult):
        return None, "unavailable", []
    score, status, factors = ai_scoring.score, ai_scoring.status, ai_scoring.factors
    if score is None and status == "unavailable" and not factors:
        return None, status, []
    if (type(score) is not int or not 0 <= score <= AI_MAX_SCORE
            or status not in ("completed", "rule_only")
            or score == 0 and status != "completed"
            or not isinstance(factors, tuple)
            or any(not isinstance(factor, dict) or type(factor.get("points")) is not int
                   or factor["points"] < 0 for factor in factors)
            or sum(factor["points"] for factor in factors) != score):
        return None, "unavailable", []
    if not score and factors:
        return None, "unavailable", []
    if score:
        codes = [item.get("code") for item in factors]
        if (len(codes) != len(set(codes)) or any(code not in AI_FACTOR_POINTS for code in codes)
                or {"AI_NOTICE_MISSING", "AI_NOTICE_STALE"} <= set(codes)
                or {"AI_ADVANCE_PAYMENT_REQUIRED", "AI_STAGED_FLOW"} <= set(codes)
                or any(type(item.get("raw_points", item["points"])) is not int
                       or item.get("raw_points", item["points"]) != AI_FACTOR_POINTS[item["code"]]
                       for item in factors)):
            return None, "unavailable", []
        raw_score, expected = cap_ai_factors(factors)
        if (score != min(raw_score, AI_MAX_SCORE)
                or any(actual["points"] != canonical["points"] for actual, canonical in zip(factors, expected))
                or ai_scoring.analysis.get("raw_score") != raw_score):
            return None, "unavailable", []
    return score, status, deepcopy(list(factors))


def calculate_risk(analyses: dict, *, ai_scoring: AIScoringResult | None = None,
                   business_info: dict | None = None) -> dict:
    """Combine known points; unknown AI data remains null in the breakdown.

    analyses.ai.risk_score is ignored. The scan service supplies an explicit
    result from score_ai_analysis, which validated this request's evidence first.
    """
    analyses = analyses if isinstance(analyses, dict) else {}
    raw_factors, checked_items = [], []
    nts_result = analyses.get("nts", {})
    ftc_result = analyses.get("ftc", {})
    # The displayed format and NTS registration status describe the same
    # identifier. Keep only the strongest identity factor, never stack them.
    number_assessment = assess_business_info(business_info)
    number_factors, nts_factors = [], []
    number_score = _check_number_format(number_assessment, number_factors, checked_items)
    nts_score = _check_nts(nts_result, nts_factors, checked_items)
    raw_factors.extend(nts_factors if nts_score >= number_score else number_factors)
    raw_score = max(number_score, nts_score)
    raw_score += _check_ftc(ftc_result, raw_factors, checked_items)
    raw_score += _check_business_identity_combination(raw_factors, checked_items)
    business_score = min(raw_score, BUSINESS_MAX_SCORE)
    business_factors = _applied_business_factors(raw_factors, raw_score)
    nts_checked, ftc_checked = _nts_determinate(nts_result), _ftc_determinate(ftc_result)
    business_status = ("completed" if nts_checked and ftc_checked else
                       "partial" if nts_checked or ftc_checked or raw_factors else "unavailable")
    ai_score, ai_status, ai_factors = _trusted_ai_component(ai_scoring)
    ai_raw_score = (ai_scoring.analysis.get("raw_score")
                    if ai_score is not None and isinstance(ai_scoring, AIScoringResult) else None)
    total_score = min(business_score + (ai_score if ai_score is not None else 0), TOTAL_MAX_SCORE)
    score_status = ("completed" if business_status == "completed" and ai_status == "completed" else
                    "unavailable" if business_status == "unavailable" and ai_status == "unavailable" else "partial")
    if (isinstance(ai_scoring, AIScoringResult)
            and "site_checks_status" in ai_scoring.analysis
            and ai_scoring.analysis["site_checks_status"] != "completed"
            and score_status != "unavailable"):
        score_status = "partial"
    if ai_status == "completed":
        checked_items.append("ai_payment_context")
    elif ai_status == "rule_only":
        checked_items.append("ai_independent_rule_evidence")
    direct_payment = any(item.get("code") == "AI_ADVANCE_PAYMENT_REQUIRED" for item in ai_factors)
    if direct_payment or total_score >= 60:
        # A verified payment condition remains a direct warning even when an
        # otherwise legitimate business identity contributes no points.
        verdict = "HIGH_RISK"
    elif total_score >= 20:
        verdict = "CAUTION"
    elif score_status != "completed":
        verdict = "INSUFFICIENT_DATA"
    else:
        verdict = "LOW_RISK"
    return {
        "risk_score": total_score,
        "verdict": verdict,
        "risk_factors": [*business_factors, *ai_factors],
        "checked_items": list(dict.fromkeys(checked_items)),
        "score_status": score_status,
        "scoring_policy": SCORING_POLICY,
        "score_breakdown": {
            "business": {"score": business_score, "max_score": BUSINESS_MAX_SCORE,
                         "raw_score": raw_score, "status": business_status,
                         "capped": raw_score > BUSINESS_MAX_SCORE},
            "ai": {"score": ai_score, "max_score": AI_MAX_SCORE, "status": ai_status,
                   "raw_score": ai_raw_score,
                   "capped": ai_raw_score is not None and ai_raw_score > AI_MAX_SCORE},
        },
    }
