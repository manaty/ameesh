# SPDX-License-Identifier: AGPL-3.0-only
"""Caller attribution of turns outside a lot (manaty/ameesh#4592).

A turn belongs to a lot when its `work_item_id` is set. Role turns started by
an external caller (a run of another orchestrator, for instance) belong to no
lot: the caller supplies a small **attribution** instead, recorded beside
`work_item_id` on the `turn_costs` row. It is data only — never admission,
budget or authorization: nothing here decides that a turn may start, be paid
for or act.

The attribution is a JSON object with a closed key set. Text values are
bounded, `issue_number`/`pr_number` are positive integers, and the whole
object is bounded so a caller cannot turn the ledger into a document store.

The carrier is the message that opens the turn: `mail send --attribution …`
stores it in `payload.attribution`, and the runner copies it to the accounting
marker and then to the ledger. Turns inside a lot keep `work_item_id` as the
authority; an attribution may accompany it and never replaces it.

    ameesh cost attribution --since 24h [--by source,run_type] [--json]

groups the ledger by those keys over a period and always shows the
unattributed remainder.
"""
from __future__ import annotations

import json
import re
from typing import Any, Iterable, Mapping, Sequence

#: the closed key set of a caller attribution
KEYS = ("source", "environment", "run_type", "run_id", "repo",
        "issue_number", "pr_number", "run_kind")

#: keys whose value is a bounded string
TEXT_KEYS = ("source", "environment", "run_type", "run_id", "repo", "run_kind")

#: keys whose value is a positive integer
NUMBER_KEYS = ("issue_number", "pr_number")

#: a single text value never exceeds this many characters
MAX_TEXT = 128

#: the serialized object never exceeds this many bytes
MAX_BYTES = 1024

#: `--by` default: everything useful to split a caller's spend, except the
#: run identifier (unique per run: too fine for a default report).
DEFAULT_KEYS = ("source", "environment", "run_type", "run_kind", "repo",
                "issue_number", "pr_number")

#: the payload key of a message carrying the attribution of the turn it opens
PAYLOAD_KEY = "attribution"

#: report schema
SCHEMA = "ameesh-cost-attribution/1"

_CONTROL_RE = re.compile(r"[\x00-\x1f\x7f]")
_DIGITS_RE = re.compile(r"^[0-9]+\Z")
_MAX_NUMBER = 2 ** 31 - 1


class AttributionError(ValueError):
    """The attribution is not a bounded, allow-listed JSON object."""


def _text(value: Any, key: str) -> str:
    if not isinstance(value, str):
        raise AttributionError("%s must be a string" % key)
    value = value.strip()
    if not value:
        raise AttributionError("%s is empty" % key)
    if len(value) > MAX_TEXT:
        raise AttributionError("%s is longer than %d characters" % (key, MAX_TEXT))
    if _CONTROL_RE.search(value):
        raise AttributionError("%s contains a control character" % key)
    return value


def _number(value: Any, key: str) -> int:
    if isinstance(value, bool):
        raise AttributionError("%s must be an integer" % key)
    if isinstance(value, str) and _DIGITS_RE.match(value.strip()):
        value = int(value.strip())
    if not isinstance(value, int):
        raise AttributionError("%s must be an integer" % key)
    if value <= 0 or value > _MAX_NUMBER:
        raise AttributionError("%s must be a positive integer" % key)
    return value


def normalize(raw: Any) -> dict | None:
    """Validate and normalize a caller attribution; None means "none given".

    Unknown keys are refused (a closed set: a caller cannot smuggle free-form
    data into the ledger). Text values are stripped and bounded, number values
    are positive integers (an all-digit string is accepted and normalized).
    A `None`/empty value is absent. The serialized object is bounded too.
    """
    if raw is None:
        return None
    if not isinstance(raw, Mapping):
        raise AttributionError("attribution must be a JSON object")
    out: dict = {}
    for key, value in raw.items():
        if not isinstance(key, str) or key not in KEYS:
            raise AttributionError("unknown attribution key: %r (allowed: %s)"
                                   % (key, ", ".join(KEYS)))
        if value is None:
            continue
        if isinstance(value, str) and not value.strip():
            continue
        if key in NUMBER_KEYS:
            out[key] = _number(value, key)
        else:
            out[key] = _text(value, key)
    if not out:
        return None
    text = json.dumps(out, ensure_ascii=False, sort_keys=True,
                      separators=(",", ":"))
    if len(text.encode("utf-8")) > MAX_BYTES:
        raise AttributionError("attribution is larger than %d bytes" % MAX_BYTES)
    return {key: out[key] for key in KEYS if key in out}


def parse(text: str) -> dict | None:
    """Normalize a JSON text (CLI value); invalid JSON is an AttributionError."""
    try:
        raw = json.loads(text)
    except ValueError as exc:
        raise AttributionError("attribution is not valid JSON: %s" % exc) from exc
    return normalize(raw)


def canonical(attribution: Mapping | None) -> str:
    """A stable identity for comparisons ("" for no attribution)."""
    if not attribution:
        return ""
    return json.dumps(dict(attribution), ensure_ascii=False, sort_keys=True,
                      separators=(",", ":"))


def from_messages(rows: Iterable[Mapping]) -> dict | None:
    """The single attribution carried by the messages served by a turn.

    Several messages served by one turn must agree: the first distinct valid
    attribution wins only when it is the sole one. Two different attributions
    mean the turn served more than one caller (or one caller sent inconsistent
    metadata): the turn stays **unattributed** — never guessed. An invalid
    attribution is ignored, never fatal (it is data, not admission).
    """
    from . import mail

    found: dict[str, dict] = {}
    for row in rows or ():
        payload = mail.message_payload(row)
        if not isinstance(payload, Mapping):
            continue
        try:
            attribution = normalize(payload.get(PAYLOAD_KEY))
        except AttributionError:
            continue
        if attribution is not None:
            found[canonical(attribution)] = attribution
    if len(found) == 1:
        return next(iter(found.values()))
    return None


def from_spec(spec: Mapping | None) -> dict | None:
    """The attribution of a turn spec, or None if absent or invalid."""
    if not isinstance(spec, Mapping):
        return None
    try:
        return normalize(spec.get(PAYLOAD_KEY))
    except AttributionError:
        return None


def render(attribution: Mapping | None, keys: Sequence[str] | None = None) -> str:
    """A compact `key=value` rendering ("" for no attribution)."""
    attribution = _clean_attribution(attribution)
    if not attribution:
        return ""
    keys = tuple(keys) if keys else KEYS
    return " ".join("%s=%s" % (key, attribution[key])
                    for key in keys if attribution.get(key) not in (None, ""))


def _clean_attribution(value: Any) -> dict | None:
    """Read a jsonb attribution from a driver row (dict, JSON text or None)."""
    if isinstance(value, str):
        try:
            value = json.loads(value)
        except ValueError:
            return None
    if not isinstance(value, Mapping) or not value:
        return None
    return dict(value)


def report(rows: Iterable[Mapping], *, keys: Sequence[str] = DEFAULT_KEYS,
           until_ts: float | None = None, agent: str | None = None) -> dict:
    """Group ledger rows by attribution keys, keeping the unattributed part.

    `rows` are `turn_costs.ledger()` rows (or the same shape). Cost lines that
    are voided or declared by a mediated device are excluded, as in
    `turn_costs.spent`: they are not spend. A row without attribution — or
    with an attribution that does not have any of the grouping keys — is
    counted in the unattributed remainder; the remainder is never hidden.
    """
    keys = tuple(keys)
    unknown = [key for key in keys if key not in KEYS]
    if unknown:
        raise AttributionError("unknown group key: %s" % ", ".join(unknown))
    groups: dict[tuple, dict] = {}
    unattributed = {"turns": 0, "usd": 0.0}
    total = {"turns": 0, "usd": 0.0}
    for row in rows or ():
        if row.get("void_reason"):
            continue
        if row.get("source") == "device":
            continue
        if agent is not None and row.get("agent") != agent:
            continue
        recorded = row.get("recorded_ts")
        if until_ts is not None and recorded is not None and float(recorded) >= until_ts:
            continue
        try:
            usd = float(row.get("usd") or 0.0)
        except (TypeError, ValueError):
            continue
        total["turns"] += 1
        total["usd"] += usd
        attribution = _clean_attribution(row.get("attribution"))
        values = tuple(attribution.get(key) if attribution else None for key in keys)
        if not attribution or all(value in (None, "") for value in values):
            unattributed["turns"] += 1
            unattributed["usd"] += usd
            continue
        entry = groups.setdefault(values, {"values": values, "turns": 0, "usd": 0.0})
        entry["turns"] += 1
        entry["usd"] += usd
    ordered = sorted(groups.values(), key=lambda item: (-item["usd"], item["turns"],
                                                        item["values"]))
    return {
        "schema": SCHEMA,
        "keys": list(keys),
        "groups": [{"values": dict(zip(keys, item["values"])),
                    "turns": item["turns"], "usd": item["usd"]} for item in ordered],
        "unattributed": unattributed,
        "total": total,
    }


def _share(usd: float, total: float) -> str:
    if total <= 0:
        return "—"
    return "%.1f%%" % (usd * 100.0 / total)


def format_report(data: Mapping) -> str:
    """A fixed-width table of an attribution report (unattributed last)."""
    keys = list(data.get("keys") or ())
    groups = list(data.get("groups") or ())
    unattributed = data.get("unattributed") or {"turns": 0, "usd": 0.0}
    total = data.get("total") or {"turns": 0, "usd": 0.0}
    labels = [key.upper() for key in keys]
    widths = [max(len(label), 10) for label in labels]
    rows: list[list[str]] = []
    for entry in groups:
        values = entry.get("values") or {}
        rows.append([str(values.get(key) if values.get(key) not in (None, "") else "—")
                     for key in keys])
    for line in rows:
        for index, value in enumerate(line):
            widths[index] = max(widths[index], min(len(value), 28))
    usd_width = 12
    turns_width = 8
    header = "  ".join(label.ljust(width) for label, width in zip(labels, widths))
    header = (header + "  " if header else "") \
        + "TURNS".rjust(turns_width) + "  " + "USD".rjust(usd_width) + "  SHARE"
    out = [header]

    def line(values, turns, usd, share, label=None):
        cells = []
        for index, value in enumerate(values):
            text = str(value) if value not in (None, "") else "—"
            if len(text) > widths[index]:
                text = text[:max(1, widths[index] - 1)] + "…"
            cells.append(text.ljust(widths[index]))
        prefix = "  ".join(cells)
        if label is not None:
            prefix = label
        return (prefix + "  " if prefix else "") + str(turns).rjust(turns_width) \
            + "  " + ("%.6f" % usd).rjust(usd_width) + "  " + share

    total_usd = float(total.get("usd") or 0.0)
    for entry, values in zip(groups, rows):
        out.append(line(values, entry.get("turns", 0), float(entry.get("usd") or 0.0),
                        _share(float(entry.get("usd") or 0.0), total_usd)))
    out.append(line(["—"] * len(keys), unattributed.get("turns", 0),
                    float(unattributed.get("usd") or 0.0),
                    _share(float(unattributed.get("usd") or 0.0), total_usd),
                    label="unattributed"))
    out.append(line(["total"] + [""] * (len(keys) - 1) if keys else [],
                    total.get("turns", 0), total_usd, "100.0%" if total_usd else "—"))
    return "\n".join(out)
