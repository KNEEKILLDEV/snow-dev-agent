import json
import re


PROVIDER_LABELS = {
    "openai": "OpenAI",
    "gemini": "Gemini",
    "claude": "Claude",
}

PROVIDER_ENV_KEYS = {
    "openai": "OPENAI_API_KEYS",
    "gemini": "GEMINI_API_KEYS",
    "claude": "CLAUDE_API_KEYS",
}

TIMEOUT_DETAIL_PATTERN = re.compile(
    r"(?:read timeout=|timeout after |timed out after )(?P<seconds>\d+(?:\.\d+)?)",
    re.IGNORECASE,
)


def provider_label(provider):
    return PROVIDER_LABELS.get(str(provider or "").lower(), str(provider or "LLM").strip() or "LLM")


def provider_env_key(provider):
    return PROVIDER_ENV_KEYS.get(str(provider or "").lower(), "API_KEYS")


def normalize_error_text(text, limit=260):
    if not text:
        return ""

    cleaned = re.sub(r"\s+", " ", str(text)).strip()

    if len(cleaned) <= limit:
        return cleaned

    return cleaned[:limit].rstrip() + "...[truncated]"


def extract_status_code(exc):
    if exc is None:
        return None

    for attr in ("status_code", "code"):
        value = getattr(exc, attr, None)
        if isinstance(value, int):
            return value

    response = getattr(exc, "response", None)
    if response is not None:
        value = getattr(response, "status_code", None)
        if isinstance(value, int):
            return value

    return None


def extract_response_text(exc):
    if exc is None:
        return ""

    response = getattr(exc, "response", None)
    if response is None:
        return ""

    pieces = []

    text = getattr(response, "text", "")
    if text:
        pieces.append(str(text))

    try:
        payload = response.json()
    except Exception:
        payload = None

    if isinstance(payload, dict):
        error_obj = payload.get("error")
        if isinstance(error_obj, dict):
            message = error_obj.get("message")
            if message:
                pieces.append(str(message))
            status = error_obj.get("status")
            if status:
                pieces.append(str(status))
        elif payload:
            pieces.append(json.dumps(payload, ensure_ascii=True, default=str))
    elif payload is not None:
        pieces.append(str(payload))

    return " | ".join(pieces)


def extract_timeout_seconds(detail):
    if not detail:
        return None

    match = TIMEOUT_DETAIL_PATTERN.search(str(detail))
    if not match:
        return None

    seconds = match.group("seconds").rstrip(".")
    return seconds or None


def classify_error_kind(exc):
    status_code = extract_status_code(exc)
    response_text = extract_response_text(exc)
    combined = f"{exc} {response_text}".lower()

    if status_code == 429 or any(
        marker in combined
        for marker in (
            "quota exceeded",
            "resource_exhausted",
            "rate limit",
            "too many requests",
            "insufficient quota",
        )
    ):
        return "quota_exceeded"

    if status_code in {401, 403} or any(
        marker in combined
        for marker in (
            "unauthorized",
            "forbidden",
            "api key",
            "authentication",
            "invalid api key",
        )
    ):
        return "authentication_failed"

    if "timeout" in combined or "timed out" in combined or "deadline exceeded" in combined:
        return "timeout"

    if status_code in {500, 502, 503, 504} or any(
        marker in combined
        for marker in (
            "service unavailable",
            "internal server error",
            "backend error",
            "temporarily unavailable",
        )
    ):
        return "provider_unavailable"

    if "invalid" in combined and "request" in combined:
        return "invalid_request"

    return "request_failed"


def build_provider_message(provider, error_kind, status_code=None, detail=""):
    label = provider_label(provider)
    suffix = f" (HTTP {status_code})" if status_code else ""
    env_key = provider_env_key(provider)

    if error_kind == "no_keys":
        if label == "AI provider":
            return "AI provider is not configured. Add the appropriate API_KEYS value to .env."
        return f"{label} is not configured. Add {env_key} to .env."

    if error_kind == "quota_exceeded":
        if label == "AI provider":
            return (
                f"AI provider quota exceeded{suffix}. "
                "The configured key hit its limit. Please wait for quota reset or use a different configured key."
            )
        return (
            f"{label} quota exceeded{suffix}. "
            f"The configured key hit its limit. Please wait for quota reset or add another {label} key."
        )

    if error_kind == "authentication_failed":
        return f"{label} authentication failed{suffix}. Check the API key in .env."

    if error_kind == "timeout":
        timeout_seconds = extract_timeout_seconds(detail)
        if timeout_seconds:
            return f"{label} timed out after {timeout_seconds}s while generating the script. Please try again."
        if detail:
            return f"{label} timed out while generating the script ({normalize_error_text(detail)}). Please try again."
        return f"{label} timed out while generating the script. Please try again."

    if error_kind == "provider_unavailable":
        return f"{label} is temporarily unavailable{suffix}. Please try again later."

    if error_kind == "invalid_request":
        if detail:
            return f"{label} request was invalid{suffix}: {normalize_error_text(detail)}"
        return f"{label} request was invalid{suffix}."

    if detail:
        return f"{label} request failed{suffix}: {normalize_error_text(detail)}"

    return f"{label} request failed{suffix}."


class LLMProviderError(RuntimeError):
    def __init__(
        self,
        provider,
        message,
        *,
        error_kind="request_failed",
        status_code=None,
        raw_error="",
    ):
        super().__init__(message)
        self.provider = provider
        self.error_kind = error_kind
        self.status_code = status_code
        self.raw_error = raw_error or ""

    @classmethod
    def from_exception(cls, provider, exc):
        status_code = extract_status_code(exc)
        response_text = extract_response_text(exc)
        detail = response_text or str(exc) or ""
        error_kind = classify_error_kind(exc)
        message = build_provider_message(
            provider=provider,
            error_kind=error_kind,
            status_code=status_code,
            detail=detail,
        )

        return cls(
            provider=provider,
            message=message,
            error_kind=error_kind,
            status_code=status_code,
            raw_error=detail,
        )


def format_generation_error(exc):
    if isinstance(exc, LLMProviderError):
        return str(exc)

    status_code = extract_status_code(exc)
    response_text = extract_response_text(exc)
    detail = response_text or str(exc) or ""
    provider = getattr(exc, "provider", None)

    if not provider and not status_code and not response_text:
        detail = normalize_error_text(str(exc))
        return detail or "Script generation failed."

    provider = provider or "AI provider"
    error_kind = classify_error_kind(exc)

    if error_kind == "request_failed" and not detail:
        return f"{provider_label(provider)} request failed."

    return build_provider_message(
        provider=provider,
        error_kind=error_kind,
        status_code=status_code,
        detail=detail,
    )
