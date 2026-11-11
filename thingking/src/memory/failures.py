"""Safe model-failure classification, separate from uncertain Memory RPC writes."""
from __future__ import annotations

import errno
import re
import socket
import ssl

CATEGORIES = frozenset({"model_transient", "model_auth", "model_quota", "model_request", "model_output", "model_unknown"})
_TYPE = re.compile(r"[A-Za-z_][A-Za-z0-9_]{0,79}\Z")
TRANSIENT_DELAYS = (60, 120, 300, 600, 1800, 3600)


def transient_delay(failures):
    return TRANSIENT_DELAYS[min(max(1, failures), len(TRANSIENT_DELAYS)) - 1]


def validate_failure(value):
    """Only bounded categories/types/numbers cross RPC; never exception text or request data."""
    if value is None:
        return None
    if (not isinstance(value, dict) or not isinstance(value.get("category"), str)
            or value["category"] not in CATEGORIES):
        raise ValueError("invalid model failure category")
    causes = value.get("causes")
    if (not isinstance(causes, list) or not 1 <= len(causes) <= 8
            or any(not isinstance(name, str) or not _TYPE.fullmatch(name) for name in causes)):
        raise ValueError("invalid model failure cause types")
    result = {"category": value["category"], "causes": list(causes)}
    if "reason" in value:
        if not isinstance(value["reason"], str) or value["reason"] not in {"empty_text", "truncated_output", "no_choices"}:
            raise ValueError("invalid model failure reason")
        result["reason"] = value["reason"]
    for key, low, high in (("status_code", 100, 599), ("errno", -10000, 10000)):
        if key in value:
            number = value[key]
            if type(number) is not int or not low <= number <= high:
                raise ValueError("invalid model failure numeric detail")
            result[key] = number
    return result


def failure_summary(failure):
    failure = validate_failure(failure)
    suffix = ",".join(f"{key}={failure[key]}" for key in ("status_code", "errno") if key in failure)
    if failure.get("reason"):
        suffix = ",".join(filter(None, (suffix, failure["reason"].replace("_", " "))))
    return f"{failure['category']}: {' > '.join(failure['causes'])}" + (f" ({suffix})" if suffix else "")


def classify_model_failure(exc):
    """A bare APIConnectionError is unknown: SDKs also wrap local programming errors."""
    chain, seen = [], set()
    current = exc
    while current is not None and id(current) not in seen and len(chain) < 8:
        chain.append(current)
        seen.add(id(current))
        current = current.__cause__ or (None if current.__suppress_context__ else current.__context__)
    result = {"category": "model_unknown", "causes": [
        type(item).__name__ if _TYPE.fullmatch(type(item).__name__) else "UnknownError" for item in chain]}
    try:
        import httpx
        network_types = (httpx.NetworkError, httpx.TimeoutException, httpx.RemoteProtocolError)
        request_types = (httpx.InvalidURL, httpx.UnsupportedProtocol, httpx.LocalProtocolError)
        status_types = (httpx.HTTPStatusError,)
    except ImportError:
        network_types = request_types = status_types = ()
    try:
        from openai import APIStatusError, APITimeoutError
        status_types += (APIStatusError,)
        network_types += (APITimeoutError,)
    except ImportError:
        pass
    status = None
    for item in chain:
        if isinstance(item, status_types):
            value = getattr(item, "status_code", None) or getattr(getattr(item, "response", None), "status_code", None)
            if type(value) is int and 100 <= value <= 599:
                status = result["status_code"] = value
        value = getattr(item, "errno", None)
        if type(value) is int and -10000 <= value <= 10000:
            result["errno"] = value
    # A local deterministic cause wins over a misleading outer connection wrapper.
    tls_disconnect = (ssl.SSLEOFError, ssl.SSLZeroReturnError)
    network_errno = {errno.ECONNRESET, errno.ECONNREFUSED, errno.ECONNABORTED,
                     errno.ENETUNREACH, errno.EHOSTUNREACH, errno.ENETDOWN,
                     errno.ENETRESET, errno.ETIMEDOUT, errno.EPIPE}
    if any(isinstance(item, (UnicodeError, ValueError, TypeError, ssl.SSLCertVerificationError, *request_types)) for item in chain):
        result["category"] = "model_request"
    elif any(isinstance(item, (FileNotFoundError, PermissionError, IsADirectoryError, NotADirectoryError))
             or isinstance(item, socket.gaierror) and item.errno != socket.EAI_AGAIN for item in chain):
        result["category"] = "model_request"
    elif any(isinstance(item, ssl.SSLError) and not isinstance(item, tls_disconnect) for item in chain):
        # Unclassified TLS errors can be deterministic configuration failures;
        # an outer ConnectError must not turn them into indefinite retries.
        result["category"] = "model_unknown"
    elif any(isinstance(item, OSError) and not isinstance(item, (ConnectionError, TimeoutError, *tls_disconnect))
             and item.errno not in network_errno
             and not (isinstance(item, socket.gaierror) and item.errno == socket.EAI_AGAIN) for item in chain):
        result["category"] = "model_unknown"
    elif status in {401, 403}:
        result["category"] = "model_auth"
    elif status == 429:
        result["category"] = "model_quota"  # Ambiguous rate-limit/quota bodies stay bounded.
    elif status in {408, 500, 502, 503, 504}:
        result["category"] = "model_transient"
    elif status is not None and status >= 400:
        result["category"] = "model_request"
    elif any(type(item).__name__ == "ModelCompletionError" for item in chain):
        result["category"] = "model_output"
        for item in chain:
            if type(item).__name__ != "ModelCompletionError":
                continue
            # Only fixed reason labels survive; never store the original model
            # identifier, text, request, or arbitrary exception message.
            for phrase, reason in (("empty text", "empty_text"), ("truncated output", "truncated_output"),
                                   ("no choices", "no_choices")):
                if phrase in str(item):
                    result["reason"] = reason
                    break
    else:
        if any(isinstance(item, (ConnectionError, TimeoutError, *tls_disconnect, *network_types))
               or isinstance(item, socket.gaierror) and item.errno == socket.EAI_AGAIN
               or isinstance(item, OSError) and item.errno in network_errno for item in chain):
            result["category"] = "model_transient"
    return validate_failure(result)


class ModelFailure(RuntimeError):
    def __init__(self, original):
        self.failure = classify_model_failure(original)
        super().__init__(failure_summary(self.failure))


class JobReceiptError(RuntimeError):
    """Preserve errors from the model-usage RPC that runs before the provider call."""
    def __init__(self, original):
        self.original = original
        super().__init__(type(original).__name__)
