
from urllib.parse import urlsplit

from django.utils.dateparse import parse_datetime
from django.utils.timezone import is_aware
from rest_framework import serializers

from risk_engine.business_validation import numbers_conflict
from ai.site_checks import normalize_site_checks


class StrictStringField(serializers.CharField):
    def to_internal_value(self, data):
        if not isinstance(data, str):
            raise serializers.ValidationError("문자열만 허용됩니다.")
        return super().to_internal_value(data)


class BusinessInfoSerializer(serializers.Serializer):
    business_name = serializers.CharField(
        required=False,
        allow_null=True,
        allow_blank=True,
        max_length=255,
    )
    representative_name = serializers.CharField(
        required=False,
        allow_null=True,
        allow_blank=True,
        max_length=100,
    )
    business_number = serializers.CharField(
        required=False,
        allow_null=True,
        allow_blank=True,
        max_length=20,
    )
    displayed_business_number = StrictStringField(
        required=False,
        allow_blank=True,
        max_length=40,
        trim_whitespace=False,
    )
    address = serializers.CharField(
        required=False,
        allow_null=True,
        allow_blank=True,
        max_length=500,
    )

    def validate(self, attrs):
        if numbers_conflict(attrs):
            raise serializers.ValidationError({
                "displayed_business_number": "정규 사업자번호와 표시된 사업자번호가 서로 다릅니다.",
            })
        return attrs


def _http_origin(value):
    parsed = urlsplit(value)
    if parsed.scheme.lower() not in ("http", "https") or not parsed.hostname:
        raise ValueError("Invalid HTTP URL")
    if parsed.username is not None or parsed.password is not None:
        raise ValueError("URL credentials are not permitted")
    return (
        parsed.scheme.lower(), parsed.hostname.lower(),
        parsed.port if parsed.port is not None else (443 if parsed.scheme.lower() == "https" else 80),
    )


class PageObservationSerializer(serializers.Serializer):
    id = StrictStringField(max_length=64, trim_whitespace=False)
    source_url = serializers.URLField(max_length=2048)
    kind = serializers.ChoiceField(choices=("current_page", "linked_policy", "history"))
    text = StrictStringField(max_length=2000, trim_whitespace=False)
    observed_at = StrictStringField(max_length=64, trim_whitespace=False)

    def validate_id(self, value):
        if not value.strip():
            raise serializers.ValidationError("빈 출처 식별자는 허용되지 않습니다.")
        return value

    def validate_text(self, value):
        if not value.strip():
            raise serializers.ValidationError("빈 관찰 문구는 허용되지 않습니다.")
        return value

    def validate_source_url(self, value):
        try:
            _http_origin(value)
        except (ValueError, TypeError):
            raise serializers.ValidationError("인증 정보가 없는 HTTP(S) 출처 URL이 필요합니다.")
        return value

    def validate_observed_at(self, value):
        try:
            parsed = parse_datetime(value)
        except (ValueError, TypeError):
            parsed = None
        if parsed is None or not is_aware(parsed):
            raise serializers.ValidationError("시간대가 포함된 ISO 8601 관찰 시각이 필요합니다.")
        # Preserve the observed timestamp, rather than substituting scan time.
        return value


class ScanRequestSerializer(serializers.Serializer):
    url = serializers.URLField(
        required=True,
        max_length=2048,
    )
    business_info = BusinessInfoSerializer(
        required=False,
        default=dict,
    )
    page_context = serializers.CharField(
        required=False,
        default="",
        allow_blank=True,
        max_length=2000,
        trim_whitespace=False,
    )
    page_observations = PageObservationSerializer(
        many=True, required=False, default=list, allow_empty=True, max_length=10,
    )
    site_checks = serializers.JSONField(required=False)

    def validate_page_context(self, value):
        # CharField also accepts numbers; this input must be a string.
        if not isinstance(self.initial_data.get("page_context", ""), str):
            raise serializers.ValidationError("문자열만 허용됩니다.")
        return value

    def validate_url(self, value):
        parsed = urlsplit(value)

        if parsed.scheme.lower() not in ("http", "https"):
            raise serializers.ValidationError(
                "HTTP 또는 HTTPS URL만 허용됩니다."
            )

        if not parsed.hostname:
            raise serializers.ValidationError(
                "유효한 도메인이 필요합니다."
            )

        if parsed.username is not None or parsed.password is not None:
            raise serializers.ValidationError(
                "인증 정보가 포함된 URL은 허용하지 않습니다."
            )

        return value

    def validate(self, attrs):
        if "site_checks" in attrs:
            try:
                attrs["site_checks"] = normalize_site_checks(attrs["site_checks"], attrs["url"])
            except (ValueError, TypeError, AttributeError, OverflowError) as error:
                raise serializers.ValidationError({"site_checks": str(error)})
        observations = attrs.get("page_observations", [])
        if not observations:
            return attrs
        ids = [observation["id"] for observation in observations]
        if len(ids) != len(set(ids)):
            raise serializers.ValidationError({"page_observations": "출처 식별자는 중복될 수 없습니다."})
        if sum(len(observation["text"]) for observation in observations) > 2000:
            raise serializers.ValidationError({"page_observations": "관찰 문구 전체는 2,000자 이하여야 합니다."})
        try:
            origin = _http_origin(attrs["url"])
            if any(_http_origin(observation["source_url"]) != origin for observation in observations):
                raise ValueError("Cross-origin observation")
        except (ValueError, TypeError):
            raise serializers.ValidationError({"page_observations": "현재 페이지와 같은 HTTP(S) 출처만 허용됩니다."})
        return attrs
