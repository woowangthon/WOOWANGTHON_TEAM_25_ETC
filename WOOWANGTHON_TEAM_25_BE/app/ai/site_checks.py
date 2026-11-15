"""Bounded client DOM observations used by the deterministic AI-area policy.

The extension observes rendered public pages. These records are not model quotes
and do not establish that the rest of a site or private pages were inspected.
The server validates their shape, source origin and date logic; it does not fetch
the supplied URLs or trust client/model numeric scores.
"""

from calendar import monthrange
from copy import deepcopy
from datetime import date, datetime, timedelta, timezone as datetime_timezone
import re
from urllib.parse import urlsplit

from django.utils import timezone


KST = datetime_timezone(timedelta(hours=9))
CHECK_NAMES = {"payment_menus", "notices", "reviews"}
RECORD_FIELDS = {
    "status", "source_url", "observed_at", "scope", "evidence", "reason",
    "dates", "latest_date_confirmed",
}
SITE_POINTS = {
    "AI_PAYMENT_MENU": 40,
    "AI_NOTICE_MISSING": 50,
    "AI_NOTICE_STALE": 50,
    "AI_REVIEW_MISSING": 40,
}
_BLOCKED_REASON = re.compile(r"login|auth|timeout|fail|error|blocked|unavailable|not_html|too_large", re.I)
_BLOCKED_TEXT = re.compile(r"(?:로그인|회원\s*가입|인증).{0,20}(?:필요|하세요|후에|후\s*이용|해야|페이지)|접근\s*(?:불가|거부)")


def _origin(value):
    parsed = urlsplit(value)
    if (parsed.scheme.lower() not in ("http", "https") or not parsed.hostname
            or parsed.username is not None or parsed.password is not None):
        raise ValueError("인증 정보가 없는 HTTP(S) 출처 URL이 필요합니다.")
    return (parsed.scheme.lower(), parsed.hostname.lower(),
            parsed.port if parsed.port is not None else (443 if parsed.scheme.lower() == "https" else 80))


def _string(record, key, maximum, *, required=False):
    value = record.get(key, "")
    if not isinstance(value, str) or len(value) > maximum or required and not value.strip():
        raise ValueError(f"site_checks.{key} 문자열이 올바르지 않습니다.")
    return value


def normalize_site_checks(checks, url):
    """Validate only supplied checks; omitted legacy inputs incur no penalties."""
    if checks is None:
        return {}
    if not isinstance(checks, dict) or set(checks) - CHECK_NAMES:
        raise ValueError("site_checks는 정해진 항목의 객체여야 합니다.")
    expected_origin = _origin(url)
    normalized = {}
    for name, original in checks.items():
        if not isinstance(original, dict) or set(original) - RECORD_FIELDS:
            raise ValueError("site_checks 관찰 항목의 필드가 올바르지 않습니다.")
        record = deepcopy(original)
        allowed_statuses = ({"present", "absent", "unverified"} if name == "payment_menus"
                            else {"missing", "empty", "present", "unverified"})
        status = record.get("status")
        if status not in allowed_statuses:
            raise ValueError("site_checks 관찰 상태가 올바르지 않습니다.")
        if record.get("scope") not in {"current_page", "linked_board"}:
            raise ValueError("site_checks 관찰 범위가 필요합니다.")
        source_url = _string(record, "source_url", 2048, required=True)
        if _origin(source_url) != expected_origin:
            raise ValueError("site_checks는 현재 페이지와 같은 HTTP(S) 출처만 허용됩니다.")
        observed_at = _string(record, "observed_at", 64, required=True)
        try:
            observed = datetime.fromisoformat(observed_at.replace("Z", "+00:00"))
        except (ValueError, TypeError):
            raise ValueError("site_checks 관찰 시각은 ISO 8601 문자열이어야 합니다.") from None
        if observed.utcoffset() is None:
            raise ValueError("site_checks 관찰 시각에는 시간대가 필요합니다.")
        evidence = _string(record, "evidence", 1000)
        reason = _string(record, "reason", 80)
        needs_evidence = (name == "payment_menus" and status == "present"
                          or name != "payment_menus" and status in {"missing", "empty"})
        if needs_evidence and not evidence.strip():
            raise ValueError("site_checks 가점 근거 설명이 필요합니다.")
        if name != "notices" and ("dates" in record or "latest_date_confirmed" in record):
            raise ValueError("작성일 관찰은 notices 항목에만 허용됩니다.")
        if name == "notices":
            dates = record.get("dates", [])
            if (not isinstance(dates, list) or len(dates) > 50
                    or any(not isinstance(item, str) or not item.strip() or len(item) > 32 for item in dates)):
                raise ValueError("공지 작성일은 최대50개의 짧은 문자열이어야 합니다.")
            if type(record.get("latest_date_confirmed", False)) is not bool:
                raise ValueError("latest_date_confirmed는 불리언이어야 합니다.")
            record["dates"] = list(dict.fromkeys(dates))
            record["latest_date_confirmed"] = record.get("latest_date_confirmed", False)
        # A failed/login response cannot prove a board is empty. Main-page
        # menu presence remains valid even if its target page needs login.
        if name != "payment_menus" and (
                _BLOCKED_REASON.search(reason) or _BLOCKED_TEXT.search(evidence)):
            record["status"] = "unverified"
        record.update({"evidence": evidence, "reason": reason})
        normalized[name] = record
    return normalized


def _previous_month(today):
    year = today.year - (today.month == 1)
    month = today.month - 1 if today.month > 1 else 12
    return date(year, month, min(today.day, monthrange(year, month)[1]))


def _posted_date(raw, today):
    full = re.fullmatch(r"(\d{4})\s*[-./]\s*(\d{1,2})\s*[-./]\s*(\d{1,2})\.?", raw.strip())
    short = re.fullmatch(r"(\d{1,2})\s*[-./]\s*(\d{1,2})\.?", raw.strip())
    try:
        if full:
            parsed = date(*map(int, full.groups()))
        elif short:
            month, day = map(int, short.groups())
            parsed = date(today.year, month, day)
            if parsed > today:
                parsed = date(today.year - 1, month, day)
        else:
            return None
    except ValueError:
        return None
    return parsed if parsed <= today else None


def evaluate_site_checks(checks, url):
    """Return normalized public-scope records and uncapped rule factors."""
    normalized = normalize_site_checks(checks, url)
    factors = []
    today = timezone.now().astimezone(KST).date()
    cutoff = _previous_month(today)

    def add(name, code, message):
        source = normalized[name]
        factors.append({
            "code": code, "points": SITE_POINTS[code], "message": message,
            "source": "site_rule",
            "evidence": [{"type": code, "evidence": source["evidence"],
                          "source_url": source["source_url"], "observed_at": source["observed_at"],
                          "scope": source["scope"], "source": "site_rule"}],
        })

    menus = normalized.get("payment_menus")
    if menus and menus["status"] == "present":
        compact = re.sub(r"\s+", "", menus["evidence"])
        if re.search(r"출금신청|충전신청", compact):
            add("payment_menus", "AI_PAYMENT_MENU", "현재 공개 화면에 출금신청 또는 충전신청 메뉴가 확인됐습니다.")
        else:
            menus["status"] = "unverified"
            menus["reason"] = "NO_PAYMENT_MENU_EVIDENCE"
    notices = normalized.get("notices")
    if notices:
        notices.update({"freshness": "unverified", "latest_date": None, "cutoff_date": cutoff.isoformat()})
        if notices["status"] in {"missing", "empty"}:
            add("notices", "AI_NOTICE_MISSING", "확인한 공개 범위에서 공지사항을 찾지 못했거나 공지 목록이 비어 있습니다.")
        elif notices["status"] == "present" and notices["latest_date_confirmed"]:
            dates = notices["dates"]
            parsed = [_posted_date(raw, today) if raw in notices["evidence"] else None for raw in dates]
            if dates and all(item is not None for item in parsed):
                latest = max(parsed)
                notices["latest_date"] = latest.isoformat()
                notices["freshness"] = "recent" if latest >= cutoff else "stale"
                if latest < cutoff:
                    add("notices", "AI_NOTICE_STALE", "확인한 공지 목록의 최신 작성일이 최근1개월 범위보다 이전입니다.")
    reviews = normalized.get("reviews")
    if reviews and reviews["status"] in {"missing", "empty"}:
        add("reviews", "AI_REVIEW_MISSING", "확인한 공개 범위에서 구매후기를 찾지 못했거나 구매후기 목록이 비어 있습니다.")
    return normalized, factors


def site_checks_status(checks):
    """Coverage describes the three criteria, independently of model success."""
    known = []
    for name in ("payment_menus", "notices", "reviews"):
        record = checks.get(name, {})
        status = record.get("status")
        if name == "payment_menus":
            known.append(status in {"present", "absent"})
        elif name == "notices":
            known.append(status in {"missing", "empty"}
                         or status == "present" and record.get("freshness") in {"recent", "stale"})
        else:
            known.append(status in {"missing", "empty", "present"})
    return "completed" if all(known) else "partial" if any(known) else "unavailable"
