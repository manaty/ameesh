# SPDX-License-Identifier: AGPL-3.0-only
"""Tables of study #6427, recomputed from a context corpus.

    ameesh context corpus export --out corpus.jsonl --harness deepseek \
        --since 2026-11-08T01:17:00Z --until 2026-11-09T01:17:00Z
    python3 -I tables.py corpus.jsonl

Model (the stream has no per-step series): the context grows linearly from
C0 to C_peak over the turn, so steps = R_turn / ((C0 + C_peak) / 2) and the
growth per step g = (C_peak - C0) / steps.  A cap closes the turn when the
context reaches it; the k = (C_peak - cap) / g remaining steps then run in a
fresh session started at C0, each rereading (cap - C0) tokens less:
saved = k * (cap - C0).  The resume summary is not counted.
A fresh first turn is session_seq == 1 and R_session_base == 0; a missing
R_session_base is unknown, not 0: such a turn is not counted as fresh.
"""
import json
import statistics as st
import sys

C0 = 30000
CAPS = (300000, 250000, 200000, 150000)


def value(row, key):
    return (row.get(key) or {}).get("value")


def quality(row, key):
    return (row.get(key) or {}).get("quality")


def fresh(row):
    return row["session_seq"] == 1 and value(row, "R_session_base") == 0


def steps(row):
    return value(row, "R_turn") / ((C0 + value(row, "C_peak")) / 2)


def growth(row):
    return (value(row, "C_peak") - C0) / steps(row)


def main(path):
    rows = [json.loads(line) for line in open(path) if line.strip()]
    turns = [r for r in rows if value(r, "C_peak") and value(r, "R_turn")]
    first = [r for r in turns if fresh(r)]
    unknown = sum(r["session_seq"] == 1 and value(r, "R_session_base") is None
                  for r in turns)
    print("turns %d, fresh first turns %d, seq 1 without R_session_base %d" % (
        len(turns), len(first), unknown))

    print("\n1. fresh first turns, C_peak >= 300k")
    for r in sorted((r for r in first if value(r, "C_peak") >= 300000),
                    key=lambda r: -value(r, "C_peak")):
        print("%d %s C_peak=%d (%s) R=%.1fM O=%d %.0fmin steps>=%d ~%d g~%d" % (
            r["source"]["event_id"], r["agent"], value(r, "C_peak"),
            quality(r, "C_peak"), value(r, "R_turn") / 1e6, value(r, "O") or 0,
            r["duration_s"] / 60, value(r, "R_turn") / value(r, "C_peak"),
            steps(r), growth(r)))

    print("\n2. fresh first turns by size")
    for name, group in (("C_peak >= 200k", [r for r in first if value(r, "C_peak") >= 200000]),
                        ("C_peak < 100k", [r for r in first if value(r, "C_peak") < 100000])):
        print("%s: %d, median %.1f min, %.1f steps, %d per step" % (
            name, len(group), st.median(r["duration_s"] for r in group) / 60,
            st.median(steps(r) for r in group),
            st.median(growth(r) for r in group if value(r, "C_peak") > C0)))

    print("\n3. closures")
    closes = [r for r in turns if r["decision"].get("close")]
    kinds = sorted({r["decision"]["close"].get("kind") for r in closes})
    print("kinds %s, count %d" % (kinds, len(closes)))
    ctx = [r["decision"]["close"]["context_tokens"] for r in closes]
    if ctx:
        print("context at closure %d..%d, also rotated %d" % (
            min(ctx), max(ctx), sum(bool(r["decision"].get("rotation")) for r in closes)))

    print("\n4. caps")
    big = [r for r in turns if value(r, "C_peak") >= 300000]
    print(">=300k: %d turns, %d fresh first, %d seq 1 started before the window, %d later" % (
        len(big), sum(fresh(r) for r in big),
        sum(r["session_seq"] == 1 and (value(r, "R_session_base") or 0) > 0 for r in big),
        sum(r["session_seq"] > 1 for r in big)))
    total = sum(value(r, "R_turn") for r in turns)
    print("reread total %.0fM" % (total / 1e6))
    for cap in CAPS:
        hit = [r for r in turns if value(r, "C_peak") >= cap]
        saved = sum((value(r, "C_peak") - cap) / growth(r) * (cap - C0) for r in hit)
        print("%dk: %d turns closed (%.0f%% unchanged), %d fresh first (%.1f%%), saved %.0fM (%.0f%%)" % (
            cap / 1000, len(hit), 100 * (1 - len(hit) / len(turns)),
            sum(fresh(r) for r in hit), 100 * sum(fresh(r) for r in hit) / len(first),
            saved / 1e6, 100 * saved / total))


if __name__ == "__main__":
    main(sys.argv[1])
