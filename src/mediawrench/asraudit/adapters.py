"""Explicit offline format conversion, pinned to whisper.cpp's v1.7.6 CLI."""

from copy import deepcopy
from fractions import Fraction
import re

from .core import AuditError, _json_value, _number


WHISPER_CPP_FORMAT = "whisper.cpp-json-v1.7.6"
UPSTREAM = {
    "version": "v1.7.6",
    "commit": "a8d002cfd879315632a579e73f0148d06959de36",
    "source": "https://github.com/ggml-org/whisper.cpp/blob/a8d002cfd879315632a579e73f0148d06959de36/examples/cli/cli.cpp#L594-L752",
}
_TIMESTAMP = re.compile(r"([0-9]{2,13}):([0-5][0-9]):([0-5][0-9]),([0-9]{3})")
TOKEN_POLICY = {
    "schema": WHISPER_CPP_FORMAT + " -ojf",
    "selection": "flagged segments only; original token order including special tokens",
    "source_paths": "zero-based paths in source_document",
    "values": "raw parsed fields; no token timing conversion, clipping or inference",
    "offsets_unit": "milliseconds",
    "timestamps_format": "HH:MM:SS,mmm",
    "t_dtw": "raw upstream value, including -1; never interpreted as seconds",
    "probability": "p is token probability, never segment confidence or a flag rule",
}


def _required(record, key, path):
    if key not in record:
        raise AuditError(f"{path}.{key}: missing required field for {WHISPER_CPP_FORMAT}")
    return record[key]


def _time_pair(record, key, path):
    value = _required(record, key, path)
    location = f"{path}.{key}"
    if not isinstance(value, dict):
        raise AuditError(f"{location}: expected object with from/to")
    for extra in value.keys() - {"from", "to"}:
        raise AuditError(f"{location}.{extra}: unsupported timing field; units are fixed by the selected format")
    return {endpoint: _required(value, endpoint, location) for endpoint in ("from", "to")}


def _validate_endpoint(offset, timestamp, offset_path, timestamp_path):
    # times_o() writes int64 milliseconds; to_timestamp(t, true) writes comma
    # timestamps from the same clock. Neither representation wins a conflict.
    if isinstance(offset, bool) or not isinstance(offset, int) or not 0 <= offset <= 2**63 - 1:
        raise AuditError(f"{offset_path}: expected nonnegative int64 milliseconds, not seconds or numeric strings")
    match = _TIMESTAMP.fullmatch(timestamp) if isinstance(timestamp, str) else None
    if match is None:
        raise AuditError(f"{timestamp_path}: expected HH:MM:SS,mmm string (comma milliseconds)")
    hours, minutes, seconds, millis = map(int, match.groups())
    timestamp_ms = ((hours * 60 + minutes) * 60 + seconds) * 1000 + millis
    if offset != timestamp_ms:
        raise AuditError(f"{offset_path}: {offset} milliseconds conflicts with {timestamp_path} ({timestamp_ms} milliseconds); no unit guessing")


def _endpoint(offset, timestamp, offset_path, timestamp_path):
    _validate_endpoint(offset, timestamp, offset_path, timestamp_path)
    seconds = offset / 1000
    if Fraction(str(seconds)) != Fraction(offset, 1000):
        raise AuditError(f"{offset_path}: milliseconds cannot be represented as seconds without decimal precision loss")
    return seconds, {"offset_path": offset_path, "timestamp_path": timestamp_path,
                     "milliseconds": offset, "seconds": seconds}


def _validate_tokens(record, path):
    if "tokens" not in record:
        return  # -oj has no token array; absence is unknown, not an empty array.
    path += ".tokens"
    tokens = record["tokens"]
    if not isinstance(tokens, list):
        raise AuditError(f"{path}: expected array for -ojf token evidence")
    for j, token in enumerate(tokens):
        loc = f"{path}[{j}]"
        if not isinstance(token, dict):
            raise AuditError(f"{loc}: expected token object")
        if not isinstance(_required(token, "text", loc), str):
            raise AuditError(f"{loc}.text: expected string")
        identifier = _required(token, "id", loc)
        if isinstance(identifier, bool) or not isinstance(identifier, int) or not 0 <= identifier <= 2**31 - 1:
            raise AuditError(f"{loc}.id: expected nonnegative int32 token ID")
        probability = _number(_required(token, "p", loc), f"{loc}.p")
        if not 0 <= probability <= 1:
            raise AuditError(f"{loc}.p: expected token probability in [0, 1]")
        dtw = _number(_required(token, "t_dtw", loc), f"{loc}.t_dtw")
        if dtw < 0 and dtw != -1:
            raise AuditError(f"{loc}.t_dtw: expected -1 sentinel or nonnegative upstream value")
        # The exporter omits both timing representations for untimed tokens.
        # Validate paired representations without converting to seconds: this
        # view retains even reversed/out-of-segment times and large exact ints.
        if "offsets" in token or "timestamps" in token:
            offsets = _time_pair(token, "offsets", loc)
            timestamps = _time_pair(token, "timestamps", loc)
            for endpoint in ("from", "to"):
                _validate_endpoint(offsets[endpoint], timestamps[endpoint],
                                   f"{loc}.offsets.{endpoint}", f"{loc}.timestamps.{endpoint}")


def token_evidence(record, path):
    """Snapshot already validated native tokens; no aggregate scores or times."""
    if "tokens" not in record:
        return None
    path += ".tokens"
    return {"source_path": path, "tokens": [
        {"source_path": f"{path}[{j}]", "token": deepcopy(token)}
        for j, token in enumerate(record["tokens"])]}


def adapt_whisper_cpp(document):
    """Return (normalized document, conversion evidence); retain every raw field.

    Requires both segment timing representations from -oj/-ojf. Full-output
    tokens and other metadata are retained, never substituted for segment data.
    This validates the timing schema, not the exporter identity or model output.
    """
    _json_value(document)
    if not isinstance(document, dict):
        raise AuditError("input: expected whisper.cpp JSON object")
    for key in ("segments", "time_unit"):
        if key in document:
            raise AuditError(f"input.{key}: unsupported mixed native/normalized variant")
    records = _required(document, "transcription", "input")
    if not isinstance(records, list):
        raise AuditError("input.transcription: expected array")
    normalized = deepcopy(document)
    del normalized["transcription"]
    normalized.update(time_unit="seconds", segments=[])
    conversion = {"adapter": WHISPER_CPP_FORMAT, "upstream": dict(UPSTREAM),
                  "offsets_unit": "milliseconds", "timestamps_format": "HH:MM:SS,mmm",
                  "operation": "offsets / 1000; exact equality with parsed timestamps required",
                  "segments": []}
    for i, record in enumerate(records):
        path = f"transcription[{i}]"
        if not isinstance(record, dict):
            raise AuditError(f"{path}: expected object")
        for key in ("start", "end", "time_unit"):
            if key in record:
                raise AuditError(f"{path}.{key}: unsupported mixed native/normalized timing")
        text = _required(record, "text", path)
        if not isinstance(text, str):
            raise AuditError(f"{path}.text: expected string")
        offsets = _time_pair(record, "offsets", path)
        timestamps = _time_pair(record, "timestamps", path)
        segment = deepcopy(record)
        evidence = {"source_path": path, "normalized_path": f"segments[{i}]"}
        for target, origin in (("start", "from"), ("end", "to")):
            segment[target], evidence[target] = _endpoint(
                offsets[origin], timestamps[origin], f"{path}.offsets.{origin}",
                f"{path}.timestamps.{origin}")
        _validate_tokens(record, path)
        normalized["segments"].append(segment)
        conversion["segments"].append(evidence)
    return normalized, conversion
