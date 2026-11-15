"""Versioned cache for public-business API results, never page/AI results.

Rows contain only freshly completed NTS/FTC responses. Reading the latest row
per API lets a partial success be reused without extending its original TTL
when the other public API fails or when only the page wording changes.
"""

from copy import deepcopy
from datetime import timedelta
import logging
from urllib.parse import urlsplit, urlunsplit

from django.utils import timezone

from risk_engine.business_validation import assess_business_number
from .models import ScanCache


# This version describes the persisted public facts, not the scoring policy.
# Keep it stable across score-budget changes: no AI decision or score is cached.
CACHE_VERSION = "public_api_main1cc_60_40_v1"
TTL_HOURS = 24
MAX_CACHE_ROWS = 64
logger = logging.getLogger(__name__)

_NTS_FIELDS = {
    "status", "business_number", "business_status", "business_status_code",
    "business_status_description", "tax_type", "tax_type_code", "closed_date",
}
_FTC_FIELDS = {"status", "search_method", "match_status", "verification", "official_info", "candidates"}
_CANDIDATE_FIELDS = {
    "business_name", "representative_names", "business_number", "corporate_number",
    "business_status", "report_number", "address", "road_address", "verification",
}
_VERIFICATION_FIELDS = {"business_name", "business_number", "representative_name", "business_address"}


def _cache_key(url, business_info):
    try:
        parsed = urlsplit(url)
        if (parsed.scheme.lower() not in ("http", "https") or not parsed.hostname
                or parsed.username is not None or parsed.password is not None):
            return None
        host = parsed.hostname.lower()
        if ":" in host:
            host = f"[{host}]"
        default_port = 443 if parsed.scheme.lower() == "https" else 80
        if parsed.port is not None and parsed.port != default_port:
            host = f"{host}:{parsed.port}"
        canonical_url = urlunsplit((parsed.scheme.lower(), host, parsed.path or "/", "", ""))
        if len(canonical_url) > 2048:
            return None
        info = business_info if isinstance(business_info, dict) else {}
        key = {"url": canonical_url}
        for field, limit in (("business_name", 255), ("representative_name", 255), ("address", 500)):
            value = info.get(field)
            value = value.strip() if isinstance(value, str) else ""
            if len(value) > limit:
                return None
            key[field] = value
        number = assess_business_number(info.get("business_number"))
        key["business_number"] = number["normalized"] if number["status"] == "valid" else ""
        return key
    except (TypeError, ValueError, AttributeError):
        return None


def _verification(value):
    return ({key: deepcopy(item) for key, item in value.items() if key in _VERIFICATION_FIELDS}
            if isinstance(value, dict) else {})


def _candidate(value):
    if not isinstance(value, dict):
        return None
    result = {key: deepcopy(item) for key, item in value.items() if key in _CANDIDATE_FIELDS}
    if "verification" in result:
        result["verification"] = _verification(result["verification"])
    return result


def _completed_public_results(analyses):
    if not isinstance(analyses, dict):
        return {}
    result = {}
    for name, allowed in (("nts", _NTS_FIELDS), ("ftc", _FTC_FIELDS)):
        value = analyses.get(name)
        if not isinstance(value, dict) or value.get("status") != "completed":
            continue
        public = {key: deepcopy(item) for key, item in value.items() if key in allowed}
        if name == "ftc":
            if "verification" in public:
                public["verification"] = _verification(public["verification"])
            if "official_info" in public:
                public["official_info"] = _candidate(public["official_info"])
            if "candidates" in public:
                candidates = public["candidates"]
                public["candidates"] = [
                    clean for item in candidates if (clean := _candidate(item)) is not None
                ] if isinstance(candidates, list) else []
        result[name] = public
    return result


def load_public_api_cache(url, business_info):
    key = _cache_key(url, business_info)
    if key is None:
        return {"status": "unavailable", "analyses": {}}
    try:
        cutoff = timezone.now() - timedelta(hours=TTL_HOURS)
        ScanCache.objects.filter(created_at__lte=cutoff).delete()
        rows = ScanCache.objects.filter(
            **key, result__cache_version=CACHE_VERSION, created_at__gt=cutoff,
        ).order_by("-created_at", "-pk")[:MAX_CACHE_ROWS]
        public = {}
        for row in rows:
            payload = row.result
            if not isinstance(payload, dict) or payload.get("cache_version") != CACHE_VERSION:
                continue
            for name, result in _completed_public_results(payload.get("analyses")).items():
                public.setdefault(name, result)
            if len(public) == 2:
                break
        return {"status": "hit" if public else "miss", "analyses": public}
    except Exception:
        # Database/migration problems must not prevent the current scan. Error
        # strings can contain SQL or user data, so log only this static message.
        logger.warning("Public API cache read unavailable")
        return {"status": "unavailable", "analyses": {}}


def store_public_api_cache(url, business_info, analyses):
    key = _cache_key(url, business_info)
    public = _completed_public_results(analyses)
    if key is None or not public:
        return False
    try:
        ScanCache.objects.create(
            **key, result={"cache_version": CACHE_VERSION, "analyses": public},
        )
        return True
    except Exception:
        logger.warning("Public API cache write unavailable")
        return False
