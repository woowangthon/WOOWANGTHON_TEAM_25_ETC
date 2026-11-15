"""Assess an explicitly displayed business-number value without inventing one.

A missing number and a failed extraction are unknown. A numeric value with the
wrong length or hyphen grouping is an observed format error. This check does not
assert that a syntactically valid number is registered or belongs to a site.
"""

import re
import unicodedata


def assess_business_number(value):
    original = value if isinstance(value, str) else ""
    compact = re.sub(r"\s+", "", unicodedata.normalize("NFKC", original))
    unavailable = {
        "status": "unavailable", "displayed_value": original,
        "digit_count": 0, "normalized": "",
    }
    # Do not turn text such as "확인 불가", a missing field, or a client flag
    # into evidence that a displayed numeric identifier is malformed.
    if not compact or len(original) > 40 or not re.fullmatch(r"[0-9-]+", compact):
        return unavailable
    digits = compact.replace("-", "")
    if not digits:
        return unavailable
    valid = bool(re.fullmatch(r"(?:[0-9]{10}|[0-9]{3}-[0-9]{2}-[0-9]{5})", compact))
    return {
        "status": "valid" if valid else "malformed",
        "displayed_value": original, "digit_count": len(digits),
        "normalized": digits,
    }


def assess_business_info(business_info):
    info = business_info if isinstance(business_info, dict) else {}
    displayed = assess_business_number(info.get("displayed_business_number"))
    if displayed["status"] != "unavailable":
        return displayed
    return assess_business_number(info.get("business_number"))


def numbers_conflict(business_info):
    info = business_info if isinstance(business_info, dict) else {}
    declared = assess_business_number(info.get("business_number"))
    displayed = assess_business_number(info.get("displayed_business_number"))
    if not declared["normalized"] or not displayed["normalized"]:
        return False
    return (declared["normalized"] != displayed["normalized"]
            or declared["status"] != displayed["status"])
