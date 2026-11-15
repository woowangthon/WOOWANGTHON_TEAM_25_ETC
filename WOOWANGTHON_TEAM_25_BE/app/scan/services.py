
from uuid import uuid4
from urllib.parse import urlsplit
import logging
logger = logging.getLogger(__name__)

from external_api.nts.client import NtsApiError
from external_api.nts.services import lookup_business as lookup_nts
from external_api.ftc.services import lookup_business as lookup_ftc
from risk_engine.services import calculate_risk
from risk_engine.business_validation import assess_business_info, numbers_conflict
from ai.services import analyze_page_context
from ai.flow import observation_fallback
from ai.scoring import AIScoringResult, AI_MAX_SCORE, score_ai_analysis, SCORING_POLICY
from .cache import TTL_HOURS, load_public_api_cache, store_public_api_cache


def run_scan(
    url: str,
    business_info: dict | None = None,
    page_context: str = "",
    page_observations: list | None = None,
    site_checks: dict | None = None,
) -> dict:
    """
    전체 URL 분석 프로세스를 실행한다.
    """
    business_info = dict(business_info) if isinstance(business_info, dict) else {}
    if numbers_conflict(business_info):
        raise ValueError("Conflicting business number values")

    parsed = urlsplit(url)
    domain = (parsed.hostname or "").lower()

    number_assessment = assess_business_info(business_info)
    lookup_info = dict(business_info)
    business_number = None
    if number_assessment["status"] == "valid":
        submitted_number = business_info.get("business_number")
        submitted = submitted_number.strip() if isinstance(submitted_number, str) else ""
        normalized = number_assessment["normalized"]
        standard_hyphenated = f"{normalized[:3]}-{normalized[3:5]}-{normalized[5:]}"
        # Keep existing standard request formatting, but send ASCII digits for
        # a value containing layout spaces or full-width display characters.
        business_number = submitted if submitted in (normalized, standard_hyphenated) else normalized
        lookup_info["business_number"] = business_number
    elif lookup_info.get("business_number"):
        # A malformed numeric value is evidence, not a usable public-API key.
        # A placeholder/non-number remains unknown rather than earning points.
        lookup_info["business_number"] = ""

    # Public API facts can be reused. Page wording and AI decisions cannot:
    # the same URL can show a new withdrawal condition on the next popup.
    cached = load_public_api_cache(url, lookup_info)
    cache_status = cached.get("status", "unavailable")
    cached_analyses = cached.get("analyses", {})
    fresh_public_results = {}
    used_cache = False

    # 1. NTS 조회
    nts_result = {"status": "skipped", "business_status": None}
    if number_assessment["status"] == "malformed":
        nts_result["reason"] = "MALFORMED_BUSINESS_NUMBER"

    cached_nts = cached_analyses.get("nts")
    if business_number and isinstance(cached_nts, dict) and cached_nts.get("status") == "completed":
        nts_result = cached_nts
        used_cache = True
    elif business_number:
        try:
            nts_result = lookup_nts(business_number)
            if isinstance(nts_result, dict) and nts_result.get("status") == "completed":
                fresh_public_results["nts"] = nts_result
        except NtsApiError:
            logger.warning("NTS API lookup failed")
            nts_result = {"status": "error", "error": "NTS_API_SERVER_ERROR"}
        except ValueError:
            nts_result = {"status": "error", "error": "INVALID_BUSINESS_NUMBER"}

    # 2. FTC 조회: malformed numbers are already removed from lookup_info.
    cached_ftc = cached_analyses.get("ftc")
    if isinstance(cached_ftc, dict) and cached_ftc.get("status") == "completed":
        ftc_result = cached_ftc
        used_cache = True
    else:
        try:
            ftc_result = lookup_ftc(lookup_info)
            if isinstance(ftc_result, dict) and ftc_result.get("status") == "completed":
                fresh_public_results["ftc"] = ftc_result
        except Exception:
            logger.warning("FTC lookup failed")
            ftc_result = {"status": "error", "error": "FTC_API_ERROR"}

    if cache_status != "unavailable":
        cache_status = "hit" if used_cache else "miss"
    # Only newly fetched successes create rows; reuse does not refresh a TTL.
    if fresh_public_results and not store_public_api_cache(url, lookup_info, fresh_public_results):
        cache_status = "unavailable"
    public_api_cache = {"status": cache_status, "ttl_hours": TTL_HOURS}

    # AI failures must not discard public API results or the existing risk score.
    try:
        if page_observations:
            ai_result = analyze_page_context(page_context, page_observations=page_observations)
        else:
            ai_result = analyze_page_context(page_context)
    except Exception:
        logger.error("AI analysis failed unexpectedly")
        ai_result = {
            "status": "error",
            "error": "AI_ANALYSIS_ERROR",
            "signals": [],
        }
        if page_observations:
            ai_result = observation_fallback(page_observations)

    try:
        scoring_input = {"page_context": page_context, "page_observations": page_observations, "url": url}
        if site_checks is not None:
            scoring_input["site_checks"] = site_checks
        ai_scoring = score_ai_analysis(ai_result, **scoring_input)
        ai_result = ai_scoring.analysis
    except Exception:
        # Keep business checks if scoring fails; never log source text or keys.
        logger.error("AI evidence scoring failed unexpectedly")
        ai_result = dict(ai_result) if isinstance(ai_result, dict) else {
            "status": "error", "error": "AI_ANALYSIS_ERROR", "signals": [],
        }
        ai_result.update({
            "risk_score": None, "max_score": AI_MAX_SCORE, "scoring_status": "unavailable",
            "scoring_policy": SCORING_POLICY, "risk_factors": [],
            "scoring_error": "AI_SCORING_ERROR",
        })
        ai_scoring = AIScoringResult(ai_result, None, "unavailable", ())

    analyses = {
        "nts": nts_result,
        "ftc": ftc_result,
        "ai": ai_result,
        "business_input": number_assessment,
    }

    risk_result = calculate_risk(analyses, ai_scoring=ai_scoring, business_info=business_info)
    # 3. 최종 응답
    reply = {
        "scan_id": str(uuid4()),
        "url": url,
        "domain": domain,
        "status": "received",

        "business_info": business_info,

    "risk_score": risk_result["risk_score"],
    "verdict": risk_result["verdict"],
    "risk_factors": risk_result["risk_factors"],
    "checked_items": risk_result["checked_items"],
    "score_status": risk_result["score_status"],
    "scoring_policy": risk_result["scoring_policy"],
    "score_breakdown": risk_result["score_breakdown"],
    "public_api_cache": public_api_cache,

    "analyses": analyses,
    }
    return reply
