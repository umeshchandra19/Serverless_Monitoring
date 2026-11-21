"""Turns raw log lines (JSON or plain text) into normalised records. Never raises: bad input becomes an INFO record."""
import hashlib
import json
import re
from dataclasses import dataclass, field
from typing import Optional

SEVERITY_ORDER = {"DEBUG": 0, "INFO": 1, "WARN": 2, "ERROR": 3, "CRITICAL": 4}
_ALIASES = {"trace": "DEBUG", "debug": "DEBUG", "info": "INFO", "notice": "INFO", "warn": "WARN", "warning": "WARN",
            "err": "ERROR", "error": "ERROR", "exception": "ERROR", "crit": "CRITICAL", "critical": "CRITICAL",
            "fatal": "CRITICAL", "alert": "CRITICAL", "emerg": "CRITICAL", "emergency": "CRITICAL"}

LEVEL_KEYS = ("level", "severity", "levelname", "log_level", "loglevel", "lvl")
MESSAGE_KEYS = ("message", "msg", "event", "error", "exception")
ERROR_TYPE_KEYS = ("error_type", "exception_type", "errorType", "exc_type")
LATENCY_KEYS = ("latency_ms", "duration_ms", "elapsed_ms", "response_time_ms", "took_ms")
REQUEST_KEYS = ("request_id", "requestId", "aws_request_id", "req_id", "correlation_id")
TRACE_KEYS = ("trace_id", "traceId", "xray_trace_id", "x_amzn_trace_id")
OWN_TYPES = {"error_signature", "api_exemplar", "ingest_summary"}  # our own output: never re-process it

_PLAIN_LEVEL = re.compile(r"\b(CRITICAL|FATAL|ERROR|WARN(?:ING)?|INFO|DEBUG)\b")
_REPORT = re.compile(r"^REPORT RequestId:")
_DURATION = re.compile(r"\bDuration: ([\d.]+) ms")
_INIT = re.compile(r"Init Duration: ([\d.]+) ms")
_MEMORY = re.compile(r"Max Memory Used: (\d+) MB")
_TIMEOUT = re.compile(r"Task timed out after ([\d.]+) seconds")
_OOM = re.compile(r"(Runtime exited with error: signal: killed|OutOfMemory|MemoryError|Out of memory)", re.I)
_TRACEBACK = re.compile(r"^Traceback \(most recent call last\)")

_NORMALISERS = [
    (re.compile(r"[0-9a-f]{8}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{12}", re.I), "<uuid>"),
    (re.compile(r"\b\d{4}-\d{2}-\d{2}[T ]\d{2}:\d{2}:\d{2}(?:[.,]\d+)?Z?\b"), "<ts>"),
    (re.compile(r"\b(?:\d{1,3}\.){3}\d{1,3}\b"), "<ip>"),
    (re.compile(r"\b0x[0-9a-f]+\b", re.I), "<hex>"),
    (re.compile(r"\b[0-9a-f]{12,}\b", re.I), "<hex>"),
    (re.compile(r"\d+"), "<n>"),
]


@dataclass
class LogRecord:
    severity: str = "INFO"
    message: str = ""
    latency_ms: Optional[float] = None
    request_id: Optional[str] = None
    trace_id: Optional[str] = None
    lambda_duration_ms: Optional[float] = None
    lambda_memory_mb: Optional[float] = None
    lambda_init_ms: Optional[float] = None
    critical_reason: Optional[str] = None
    is_json: bool = False
    parse_failed: bool = False
    own_output: bool = False
    fields: dict = field(default_factory=dict)


def normalise_severity(value):
    if value is None:
        return None
    return _ALIASES.get(str(value).strip().lower())


def _first(d, keys):
    for k in keys:
        if k in d and d[k] not in (None, ""):
            return d[k]
    return None


def _number(value):
    try:
        number = float(value)
        return number if number == number and number not in (float("inf"), float("-inf")) else None
    except (TypeError, ValueError):
        return None


def parse_line(line):
    text = (line or "").strip()
    if not text:
        return LogRecord(message="")
    try:
        if text.startswith("{"):
            return _parse_json(text)
        return _parse_plain(text)
    except Exception:  # a log line must never break the pipeline
        return LogRecord(message=text[:500], parse_failed=True)


def _parse_json(text):
    try:
        data = json.loads(text)
    except ValueError:
        rec = _parse_plain(text)
        rec.parse_failed = True  # looked like JSON but was not
        return rec
    if not isinstance(data, dict):
        return LogRecord(message=text[:500], parse_failed=True)
    if "_aws" in data or data.get("type") in OWN_TYPES:
        return LogRecord(own_output=True, is_json=True)
    severity = normalise_severity(_first(data, LEVEL_KEYS))
    message = _first(data, MESSAGE_KEYS)
    if isinstance(message, (dict, list)):
        message = json.dumps(message)[:500]
    message = str(message) if message is not None else ""
    error_type = _first(data, ERROR_TYPE_KEYS)
    if error_type and error_type not in message:
        message = f"{error_type}: {message}" if message else str(error_type)
    if severity is None:
        severity = "ERROR" if any(k in data for k in ("exception", "stack_trace", "stacktrace", "error_type")) else "INFO"
    rec = LogRecord(severity=severity, message=message[:1000], is_json=True,
                    latency_ms=_number(_first(data, LATENCY_KEYS)),
                    request_id=_str_or_none(_first(data, REQUEST_KEYS)),
                    trace_id=_str_or_none(_first(data, TRACE_KEYS)), fields=data)
    _apply_critical_patterns(rec)
    return rec


def _str_or_none(value):
    return str(value)[:200] if value is not None else None


def _parse_plain(text):
    rec = LogRecord(message=text[:1000])
    if _REPORT.match(text):  # Lambda's own end-of-invocation line
        rec.lambda_duration_ms = _number((_DURATION.search(text) or [None, None])[1])
        rec.lambda_memory_mb = _number((_MEMORY.search(text) or [None, None])[1])
        rec.lambda_init_ms = _number((_INIT.search(text) or [None, None])[1])
        return rec
    if text.startswith(("START RequestId:", "END RequestId:")):
        return rec
    match = _PLAIN_LEVEL.search(text[:200])
    if match:
        rec.severity = normalise_severity(match.group(1)) or "INFO"
    elif _TRACEBACK.match(text):
        rec.severity = "ERROR"
    _apply_critical_patterns(rec)
    return rec


def _apply_critical_patterns(rec):
    timeout = _TIMEOUT.search(rec.message)
    if timeout:
        rec.severity, rec.critical_reason = "CRITICAL", f"timeout after {timeout.group(1)}s"
    elif _OOM.search(rec.message):
        rec.severity, rec.critical_reason = "CRITICAL", "out of memory"
    elif rec.severity == "CRITICAL":
        rec.critical_reason = "critical severity"


def fingerprint(message):
    """Stable short id for 'the same error': ids, numbers, timestamps and addresses are masked first."""
    text = (message or "").lower()
    for pattern, replacement in _NORMALISERS:
        text = pattern.sub(replacement, text)
    text = re.sub(r"\s+", " ", text).strip()[:200]
    return hashlib.sha1(text.encode()).hexdigest()[:12]
