"""Deterministic suspicion points from source-verified observations.

A score measures evidence in the supplied excerpts. It is not a calibrated fraud
probability and does not establish that unobserved withdrawal steps are safe.
The model extracts signals; it never chooses numeric points.
"""

from dataclasses import dataclass
from copy import deepcopy
from datetime import datetime
from urllib.parse import urlsplit

from .flow import (
    MAX_TEXT_LENGTH, STAGES, _DEDUCTION, _PREVENTION, _deduplicate_signals, _is_menu_label_only,
    _prepared_sources, _relevant_text, assessment_for, observe_sources,
)
from .site_checks import SITE_POINTS, evaluate_site_checks, site_checks_status

SCORING_POLICY = "business50_ai50_v3"
AI_MAX_SCORE = 50
COMBINATION_SCORE = 10
COMBINATION_STAGES = frozenset({
    "REWARD_OFFER", "EARNINGS_DISPLAY", "WITHDRAWAL_RESTRICTED",
})
AI_FACTOR_POINTS = {
    "AI_ADVANCE_PAYMENT_REQUIRED": AI_MAX_SCORE,
    "AI_STAGED_FLOW": COMBINATION_SCORE,
    **SITE_POINTS,
}


@dataclass(frozen=True)
class AIScoringResult:
    """Explicit server-computed value passed to the risk engine, not model JSON."""

    analysis: dict
    score: int | None
    status: str
    factors: tuple[dict, ...]


def _origin(value):
    parsed = urlsplit(value)
    if parsed.scheme.lower() not in ("http", "https") or not parsed.hostname:
        raise ValueError("Invalid source URL")
    if parsed.username is not None or parsed.password is not None:
        raise ValueError("Source URL credentials are not allowed")
    return (parsed.scheme.lower(), parsed.hostname.lower(),
            parsed.port if parsed.port is not None else (443 if parsed.scheme.lower() == "https" else 80))


def _sources(observations, request_url):
    sources = _prepared_sources(observations)
    expected_origin = _origin(request_url) if request_url is not None else None
    for item in sources.values():
        origin = _origin(item["source_url"])
        if expected_origin is None:
            expected_origin = origin
        if origin != expected_origin:
            raise ValueError("Cross-origin scoring evidence")
        observed = datetime.fromisoformat(item["observed_at"].replace("Z", "+00:00"))
        if observed.utcoffset() is None:
            raise ValueError("Observation timestamp needs a time zone")
    return sources


def _canonical_evidence(evidence):
    return " ".join(evidence.strip(" \t\r\n()（）\"'‘’“”「」『』").split())


def _source_signal(signal, sources, rule_keys, analysis_status):
    source = sources.get(signal.get("source_id"))
    if source is None or signal.get("source_url") != source["source_url"]:
        raise ValueError("Unbound scoring evidence")
    if signal.get("kind", source["kind"]) != source["kind"]:
        raise ValueError("Wrong observation kind")
    if signal.get("observed_at", source["observed_at"]) != source["observed_at"]:
        raise ValueError("Wrong observation time")
    evidence = signal["evidence"]
    if evidence not in source["text"]:
        raise ValueError("Evidence absent from submitted source")
    if signal["type"] == "ADVANCE_PAYMENT_REQUIRED" and (
            _PREVENTION.search(evidence) or _DEDUCTION.search(evidence)
            or _PREVENTION.search(source["text"])):
        raise ValueError("Prevention, negation or deduction is not a prepayment requirement")
    method = signal.get("source", "ai")
    if method == "rule":
        key = (source["id"], source["source_url"], signal["type"], _canonical_evidence(evidence))
        if key not in rule_keys:
            raise ValueError("Unverified rule claim")
    elif method != "ai" or analysis_status != "completed" or evidence not in _relevant_text(source["text"]):
        raise ValueError("Evidence not from a completed model analysis")
    return {
        "type": signal["type"], "evidence": evidence, "reason": signal["reason"],
        "source_id": source["id"], "source_url": source["source_url"],
        "kind": source["kind"], "observed_at": source["observed_at"], "source": method,
    }


def _factor_source(signals):
    methods = {item["source"] for item in signals}
    return next(iter(methods)) if len(methods) == 1 else "mixed"


def cap_ai_factors(factors):
    """Preserve raw weights and make displayed factor points sum to the cap."""
    result = deepcopy(list(factors))
    raw_score = sum(item.get("raw_points", item["points"]) for item in result)
    if raw_score <= AI_MAX_SCORE:
        return raw_score, result
    numerators = [item.get("raw_points", item["points"]) * AI_MAX_SCORE for item in result]
    applied = [value // raw_score for value in numerators]
    remaining = AI_MAX_SCORE - sum(applied)
    order = sorted(range(len(result)), key=lambda index: (-(numerators[index] % raw_score), index))
    for index in order[:remaining]:
        applied[index] += 1
    for item, points in zip(result, applied):
        item["raw_points"] = item.get("raw_points", item["points"])
        item["points"] = points
    return raw_score, result


def score_ai_analysis(analysis, *, page_context="", page_observations=None, url=None, site_checks=None):
    """Validate against this request before assigning any AI-area points.

    Invalid evidence is removed. If the model's signal list cannot be verified,
    a completed model status alone cannot turn that result into a zero-point
    assessment. Independently recomputed rule evidence can still establish risk.
    """
    original = analysis if isinstance(analysis, dict) else {}
    result = dict(original)
    invalid = not isinstance(analysis, dict)
    status = original.get("status")
    if status not in ("completed", "skipped", "error"):
        invalid = True
    if status == "completed" and "signals" not in original:
        invalid = True
    signals = original.get("signals", [])
    # A valid response may contain 12 model signals plus four rule stages for
    # each of 10 sources. Keep that bounded union intact before deduplicating.
    if not isinstance(signals, list) or len(signals) > 64:
        signals = []
        invalid = True
    sources = {}
    rule_keys = set()
    has_observations = page_observations is not None and page_observations != []
    try:
        if has_observations:
            sources = _sources(page_observations, url)
            rule_keys = {
                (item["source_id"], item["source_url"], item["type"], _canonical_evidence(item["evidence"]))
                for item in observe_sources(sources)
            }
        elif not isinstance(page_context, str) or len(page_context) > MAX_TEXT_LENGTH:
            raise ValueError("Invalid page context")
    except (ValueError, TypeError, AttributeError, OverflowError):
        invalid = True
        sources = {}
    if status == "completed":
        from .services import _payment_context
        if has_observations:
            if not any(_relevant_text(source["text"]) for source in sources.values()):
                invalid = True
        elif not isinstance(page_context, str) or not _payment_context(page_context):
            invalid = True
    verified = []
    for signal in signals:
        try:
            if not isinstance(signal, dict) or signal.get("type") not in STAGES:
                raise ValueError("Unknown scoring signal")
            if (not isinstance(signal.get("evidence"), str) or not signal["evidence"].strip()
                    or len(signal["evidence"]) > MAX_TEXT_LENGTH
                    or _is_menu_label_only(signal["evidence"])
                    or not isinstance(signal.get("reason"), str) or not signal["reason"].strip()
                    or len(signal["reason"]) > 500):
                raise ValueError("Invalid scoring signal")
            if has_observations:
                verified.append(_source_signal(signal, sources, rule_keys, status))
            else:
                # The old single-context schema contains only an advance-payment
                # signal, with no invented source URL or independent rule source.
                from .services import _payment_context
                if (status != "completed" or signal["type"] != "ADVANCE_PAYMENT_REQUIRED"
                        or _PREVENTION.search(page_context) or _DEDUCTION.search(signal["evidence"])
                        or signal.get("source", "ai") != "ai"
                        or any(key in signal for key in ("source_id", "source_url", "kind", "observed_at"))
                        or signal["evidence"] not in page_context
                        or signal["evidence"] not in _payment_context(page_context)):
                    raise ValueError("Invalid legacy evidence")
                verified.append({
                    "type": signal["type"], "evidence": signal["evidence"],
                    "reason": signal["reason"], "source": "ai",
                })
        except (ValueError, TypeError, AttributeError):
            invalid = True
    if has_observations:
        verified = _deduplicate_signals(verified)
    else:
        unique = {}
        for signal in verified:
            unique.setdefault((signal["type"], _canonical_evidence(signal["evidence"])), signal)
        verified = list(unique.values())
    # An invalid/incomplete model result contributes no model points. Valid,
    # independently recomputed rule signals remain usable on that failure path.
    eligible = verified if status == "completed" and not invalid else [
        signal for signal in verified if signal["source"] == "rule"
    ]
    direct = [signal for signal in eligible if signal["type"] == "ADVANCE_PAYMENT_REQUIRED"]
    observed = {signal["type"] for signal in eligible}
    factors = []
    score = None
    scoring_status = "unavailable"
    if direct:
        score = AI_MAX_SCORE
        evidence = direct
        factor = {
            "code": "AI_ADVANCE_PAYMENT_REQUIRED", "points": score,
            "message": "대금 수령이나 출금·계정 해제를 조건으로 별도 선입금을 요구하는 원문이 확인됐습니다.",
        }
    elif COMBINATION_STAGES <= observed:
        score = COMBINATION_SCORE
        # One complete supporting quote per stage is sufficient. Repeated quotes
        # or pages never add more points to the same observed combination.
        evidence = [next(signal for signal in eligible if signal["type"] == stage)
                    for stage in STAGES if stage in COMBINATION_STAGES]
        factor = {
            "code": "AI_STAGED_FLOW", "points": score,
            "message": "보상 안내·수익 표시·출금 제한을 함께 관찰해 추가 확인이 필요합니다. 선입금 요구가 확인된 것은 아닙니다.",
        }
    else:
        evidence = []
        factor = None
        if status == "completed" and not invalid:
            score = 0
    if score is not None:
        scoring_status = "completed" if status == "completed" and not invalid else "rule_only"
    if factor is not None:
        factor["source"] = _factor_source(evidence)
        factor["evidence"] = evidence
        factors.append(factor)
    # Site facts are collected separately from model excerpts. A visible main
    # menu remains valid if its destination requires login. Board read failures
    # remain unverified and cannot contribute absence points.
    for key in ("site_checks", "site_checks_status", "site_checks_error"):
        result.pop(key, None)
    if site_checks is not None:
        try:
            checked_sites, site_factors = evaluate_site_checks(site_checks, url)
            result["site_checks"] = checked_sites
            result["site_checks_status"] = site_checks_status(checked_sites)
            factors.extend(site_factors)
        except (ValueError, TypeError, AttributeError, OverflowError):
            result.update({"site_checks": {}, "site_checks_status": "unavailable",
                           "site_checks_error": "INVALID_SITE_CHECKS"})
    raw_score = None
    if factors:
        raw_score, factors = cap_ai_factors(factors)
        score = min(raw_score, AI_MAX_SCORE)
        scoring_status = "completed" if status == "completed" and not invalid else "rule_only"
    elif score is not None:
        raw_score = score
    visible_signals = verified if not invalid else [item for item in verified if item["source"] == "rule"]
    result["signals"] = visible_signals if has_observations else [
        {key: value for key, value in item.items() if key != "source"} for item in visible_signals
    ]
    if has_observations or "assessment" in result:
        result["assessment"] = assessment_for(visible_signals)
    if invalid:
        result["scoring_error"] = "INVALID_SCORING_EVIDENCE"
    else:
        result.pop("scoring_error", None)
    result.update({
        "risk_score": score, "max_score": AI_MAX_SCORE,
        "raw_score": raw_score, "capped": raw_score is not None and raw_score > AI_MAX_SCORE,
        "scoring_status": scoring_status, "scoring_policy": SCORING_POLICY,
        "risk_factors": factors,
    })
    return AIScoringResult(result, score, scoring_status, tuple(factors))
