# SPDX-License-Identifier: AGPL-3.0-only
"""L159a — the capacity guard contract, shared with Nexlink Compute
(docs/GARDE-DE-CAPACITE.md).

The golden sets of tests/dore/capacity_guard/ are checked here against the
JSON schemas of docs/schemas/ (by a validator of the subset of JSON Schema
they use: a keyword outside it fails the test, so schema and validator do not
drift), against `capacity_guard.parse_lend` (the offer, `policy.lend`), and
against the lendable formula of the study (section 1.4). Nexlink Compute runs
the same files in its own tests.
"""
from __future__ import annotations

import json
import pathlib
import unittest

from ameesh import canon, capacity_guard, resources

from .test_l43_perimetre_hote import HOST, _Canons, codes, host

ROOT = pathlib.Path(__file__).resolve().parent.parent
GOLDEN = ROOT / "tests" / "dore" / "capacity_guard"
SCHEMAS = ROOT / "docs" / "schemas"
GIB = 1024 ** 3

#: the keywords the validator below implements; annotations are ignored
KEYWORDS = {"type", "const", "enum", "required", "properties", "additionalProperties",
            "items", "minimum", "maximum", "exclusiveMinimum", "uniqueItems",
            "dependentRequired"}
ANNOTATIONS = {"$schema", "$id", "title", "description"}
TYPES = {
    "object": lambda v: isinstance(v, dict),
    "array": lambda v: isinstance(v, list),
    "string": lambda v: isinstance(v, str),
    "boolean": lambda v: isinstance(v, bool),
    "null": lambda v: v is None,
    "integer": lambda v: isinstance(v, int) and not isinstance(v, bool),
    "number": lambda v: isinstance(v, (int, float)) and not isinstance(v, bool),
}


def keywords(schema: dict, where: str = "#") -> list[str]:
    """The keywords of `schema` (recursively) the validator does not know."""
    out = ["%s/%s" % (where, k) for k in schema if k not in KEYWORDS | ANNOTATIONS]
    for name, sub in (schema.get("properties") or {}).items():
        out += keywords(sub, "%s/properties/%s" % (where, name))
    if isinstance(schema.get("items"), dict):
        out += keywords(schema["items"], where + "/items")
    return out


def errors(schema: dict, value, where: str = "$") -> list[str]:
    """Why `value` does not match `schema` (empty: it matches)."""
    found: list[str] = []
    kinds = schema.get("type")
    if kinds is not None:
        kinds = [kinds] if isinstance(kinds, str) else kinds
        if not any(TYPES[k](value) for k in kinds):
            return ["%s: %r is not %s" % (where, value, " or ".join(kinds))]
    if "const" in schema and value != schema["const"]:
        found.append("%s: %r is not %r" % (where, value, schema["const"]))
    if "enum" in schema and value not in schema["enum"]:
        found.append("%s: %r not in %s" % (where, value, schema["enum"]))
    if TYPES["number"](value):
        if "minimum" in schema and value < schema["minimum"]:
            found.append("%s: %r < %r" % (where, value, schema["minimum"]))
        if "maximum" in schema and value > schema["maximum"]:
            found.append("%s: %r > %r" % (where, value, schema["maximum"]))
        if "exclusiveMinimum" in schema and value <= schema["exclusiveMinimum"]:
            found.append("%s: %r <= %r" % (where, value, schema["exclusiveMinimum"]))
    if isinstance(value, dict):
        props = schema.get("properties") or {}
        for key in schema.get("required") or ():
            if key not in value:
                found.append("%s: %s required" % (where, key))
        for key, needed in (schema.get("dependentRequired") or {}).items():
            if key in value:
                found += ["%s: %s required with %s" % (where, n, key)
                          for n in needed if n not in value]
        for key, item in value.items():
            if key in props:
                found += errors(props[key], item, "%s.%s" % (where, key))
            elif schema.get("additionalProperties") is False:
                found.append("%s: %s not admitted" % (where, key))
    if isinstance(value, list):
        if isinstance(schema.get("items"), dict):
            for index, item in enumerate(value):
                found += errors(schema["items"], item, "%s[%d]" % (where, index))
        if schema.get("uniqueItems") and len({json.dumps(v, sort_keys=True)
                                              for v in value}) != len(value):
            found.append("%s: duplicate items" % where)
    return found


def golden(name: str) -> dict:
    data = json.loads((GOLDEN / name).read_text(encoding="utf-8"))
    assert data["schema"] == "ameesh-capacity-golden/1" and data["family"] + ".json" == name
    return data


def schema(name: str) -> dict:
    return json.loads((SCHEMAS / name).read_text(encoding="utf-8"))


class SchemaTest(unittest.TestCase):
    def check(self, schema_name: str, family: str):
        doc = schema(schema_name)
        self.assertEqual(keywords(doc), [], "keywords outside the validator's subset")
        data = golden(family)
        for case in data["valid"]:
            with self.subTest(case["why"]):
                self.assertEqual(errors(doc, case["doc"]), [])
        for case in data["invalid"]:
            with self.subTest(case["why"]):
                self.assertNotEqual(errors(doc, case["doc"]), [])

    def test_state(self):
        self.check("ameesh-host-state-1.schema.json", "state.json")

    def test_ack(self):
        self.check("ameesh-host-ack-1.schema.json", "ack.json")

    def test_names_agree(self):
        state = schema("ameesh-host-state-1.schema.json")
        ack = schema("ameesh-host-ack-1.schema.json")
        self.assertEqual(state["properties"]["schema"]["const"], capacity_guard.SCHEMA_HOST_STATE)
        self.assertEqual(ack["properties"]["schema"]["const"], capacity_guard.SCHEMA_HOST_ACK)
        self.assertEqual(state["properties"]["state"]["enum"], list(capacity_guard.STATES))
        self.assertEqual(ack["properties"]["state"]["enum"], list(capacity_guard.STATES))
        # the ack stays closed (the device reader admits a fixed list of keys)
        self.assertIs(ack["additionalProperties"], False)
        self.assertEqual(sorted(ack["required"]), sorted(ack["properties"]))
        # the state stays open: a reader ignores what it does not know
        self.assertIs(state["additionalProperties"], True)
        self.assertEqual(state["required"], ["schema", "state", "seq"])

    def test_measure_keys_are_those_of_the_common_measure(self):
        """The `measure` object of the state lists the keys `resources.measure`
        publishes (L159d), nothing more and nothing less."""
        props = schema("ameesh-host-state-1.schema.json")["properties"]["measure"]["properties"]
        full = golden("state.json")["valid"][4]["doc"]["measure"]
        self.assertEqual(set(props), set(full))
        measure = getattr(resources, "measure", None)
        if measure is None:
            self.skipTest("resources.measure comes with L159d")
        self.assertEqual(set(measure(None)), set(props))


class FreshnessTest(unittest.TestCase):
    """Section 5.3: the state a guest obeys, given what it read and when
    (codex #212 r2: an `available` state must not outlive its dead guard)."""

    def test_golden_freshness(self):
        data = golden("freshness.json")
        self.assertEqual(data["max_state_ttl_s"], capacity_guard.MAX_STATE_TTL_S)
        for case in data["cases"]:
            with self.subTest(case["why"]):
                self.assertEqual(capacity_guard.gate_state(case["doc"], case["now"],
                                                           lending=case["lending"]),
                                 case["expect"])

    def test_non_finite_expiry(self):
        doc = {"schema": "ameesh-host-state/1", "state": "available", "seq": 1}
        # codex #212 r3: JSON digits too large for a float (10**400)
        for value in (float("inf"), float("nan"), True, 10 ** 400):
            with self.subTest(value):
                self.assertEqual(capacity_guard.gate_state(dict(doc, expires_ts=value), 0.0,
                                                           lending=True), "stopped")


class OfferTest(unittest.TestCase):
    def test_golden_offers(self):
        for case in golden("offer.json")["cases"]:
            with self.subTest(case["why"]):
                offer, problems, _ = capacity_guard.parse_lend(case["lend"])
                if "offer" in case:
                    self.assertEqual((offer, problems), (case["offer"], []))
                else:
                    self.assertIsNone(offer)
                    for expected in case["problems"]:
                        self.assertTrue(any(expected in p for p in problems),
                                        (expected, problems))

    def test_windows(self):
        self.assertEqual(capacity_guard.parse_window("mon-fri 18:00-14:00 UTC"),
                         {"days": ["mon", "tue", "wed", "thu", "fri"], "start": 1080,
                          "end": 840, "tz": "UTC"})
        self.assertEqual(capacity_guard.parse_window("sat-sun")["days"], ["sat", "sun"])
        self.assertEqual(capacity_guard.parse_window("fri-mon,wed")["days"],
                         ["fri", "sat", "sun", "mon", "wed"])
        for text in ("weekdays", "mon 25:00-26:00", "mon 10:00-10:00", "mon 10:00-11:00 Mars/Base",
                     "", None, 3):
            with self.subTest(text):
                self.assertIsNone(capacity_guard.parse_window(text))

    def test_every_number_too_large_for_a_float(self):
        # codex #212 r4: every number of the offer, reclaim included, goes
        # through one guarded conversion: 1 followed by 400 zeros, inf or nan
        # drop the offer with a problem on its key, never an OverflowError
        base = {"cpu_max": 1, "memory_max": "1GiB", "disk_max": "1GiB",
                "reclaim": {"busy_units": ["actions.runner.*"]}}
        keys = [(k,) for k in ("cpu_max", "memory_max", "agent_memory_max", "disk_max",
                                 "cpu_weight", "drain_seconds", "calm_seconds")]
        keys += [("reclaim", k) for k in ("memory_available_min", "cpu_pressure_max",
                                          "memory_pressure_max", "busy_max")]
        for path in keys:
            for value in (10 ** 400, float("inf"), float("nan")):
                with self.subTest(path=path, value=str(value)[:6]):
                    lend = json.loads(json.dumps(base))
                    (lend if len(path) == 1 else lend["reclaim"])[path[-1]] = value
                    offer, problems, _ = capacity_guard.parse_lend(lend)
                    self.assertIsNone(offer)
                    self.assertTrue(any(path[-1] in p for p in problems), problems)

    def test_parse_helpers_never_overflow(self):
        # codex #212 r4: the resources helpers the contract uses
        for value in (10 ** 400, "1" + "0" * 400, float("inf"), float("nan"), "inf", "nan"):
            with self.subTest(value=str(value)[:6]):
                self.assertIsNone(resources.finite_float(value))
                self.assertIsNone(resources.parse_percent(value))
                self.assertIsNone(resources.parse_fraction(value))
                if not isinstance(value, str) or value[0].isdigit():
                    self.assertIsNone(resources.parse_bytes(value))
        self.assertEqual(resources.finite_float("2.5"), 2.5)
        self.assertEqual(resources.parse_bytes(10 ** 18), 10 ** 18)

    def test_unknown_keys_are_reported(self):
        lend = {"cpu_max": 1, "memory_max": "1GiB", "disk_max": "1GiB", "gpu_max": 1,
                "reclaim": {"swap_max": "1GiB"}}
        offer, problems, unknown = capacity_guard.parse_lend(lend)
        self.assertEqual(problems, [])
        self.assertIsNotNone(offer)
        self.assertEqual(unknown, ["gpu_max", "reclaim.swap_max"])


class CanonTest(_Canons, unittest.TestCase):
    def test_canon_check_reads_policy_lend(self):
        text = host("bob", harnesses=("codex",), resources_yaml=(
            "  lend:\n    cpu_max: 3\n    memory_max: 14GiB\n    disk_max: 80GiB\n"
            "    windows: [\"mon-fri 18:00-14:00 UTC\", \"sat-sun\"]\n"
            "    reclaim:\n      cpu_pressure_max: 20%\n    gpu_max: 1\n"))
        loaded = canon.load(self.acme(host_text=text), default=False)
        found = canon.validate(loaded)
        lend = loaded.host(HOST).policy.lend
        self.assertEqual((lend["cpu_max"], lend["memory_max"], lend["disk_max"]),
                         (3.0, 14 * GIB, 80 * GIB))
        self.assertEqual(lend["reclaim"], {"cpu_pressure_max": 20.0})
        self.assertNotIn("host-policy-invalid", codes(found, canon.ERROR))
        self.assertIn("host-lend-unknown", codes(found, canon.WARNING))

    def test_unreadable_offer_lends_nothing(self):
        text = host("bob", harnesses=("codex",), resources_yaml=(
            "  lend:\n    cpu_max: 3\n    memory_max: 14GiB\n"))
        loaded = canon.load(self.acme(host_text=text), default=False)
        found = canon.validate(loaded)
        self.assertIsNone(loaded.host(HOST).policy.lend)
        self.assertIn("host-policy-invalid", codes(found, canon.ERROR))

    def test_non_finite_offer_lends_nothing(self):
        # codex #212 r2: YAML .inf / .nan, and a size too large for a float,
        # drop the offer through host-policy-invalid (never an OverflowError,
        # never an unbounded cap)
        # codex #212 r3: an integer too large for a float (1 and 400 zeros)
        for n, (key, value) in enumerate((("cpu_max", ".inf"), ("cpu_max", ".nan"),
                                          ("cpu_max", "1" + "0" * 400),
                                          ("memory_max", ".inf"), ("disk_max", ".nan"),
                                          ("memory_max", "9" * 400 + "GiB"))):
            with self.subTest(key=key, value=value[:6]):
                caps = {"cpu_max": "3", "memory_max": "14GiB", "disk_max": "80GiB"}
                caps[key] = value
                text = host("bob", harnesses=("codex",), resources_yaml=(
                    "  lend:\n" + "".join("    %s: %s\n" % kv for kv in caps.items())))
                loaded = canon.load(self.acme(host_text=text, name="t%d" % n), default=False)
                found = canon.validate(loaded)
                self.assertIsNone(loaded.host(HOST).policy.lend)
                self.assertIn("host-policy-invalid", codes(found, canon.ERROR))

    def test_numbers_too_large_for_a_float_in_the_fiche(self):
        # codex #212 r4: reclaim.cpu_pressure_max of 1 and 400 zeros crashed
        # the host builder; every number of policy.lend and policy.resources
        huge = "1" + "0" * 400
        lend = {"cpu_max": "3", "memory_max": "14GiB", "disk_max": "80GiB"}
        cases = [("lend", k, None) for k in ("agent_memory_max", "cpu_weight",
                                             "drain_seconds", "calm_seconds")]
        cases += [("lend", "reclaim", k) for k in ("memory_available_min", "cpu_pressure_max",
                                                   "memory_pressure_max", "busy_max")]
        cases += [("resources", k, None) for k in resources.THRESHOLD_KEYS
                  + ("agent_memory_max",)]
        for n, (section, key, sub) in enumerate(cases):
            with self.subTest(section=section, key=key, sub=sub):
                if section == "lend":
                    lines = dict(lend)
                    if sub is None:
                        lines[key] = huge
                    else:
                        lines["reclaim"] = "{busy_units: [x.service], %s: %s}" % (sub, huge)
                    yaml = "  lend:\n" + "".join("    %s: %s\n" % kv for kv in lines.items())
                else:
                    yaml = "  resources:\n    %s: %s\n" % (key, huge)
                text = host("bob", harnesses=("codex",), resources_yaml=yaml)
                loaded = canon.load(self.acme(host_text=text, name="h%d" % n), default=False)
                found = canon.validate(loaded)
                self.assertIn("host-policy-invalid", codes(found, canon.ERROR))
                if section == "lend":
                    self.assertIsNone(loaded.host(HOST).policy.lend)

    def test_no_offer(self):
        loaded = canon.load(self.acme(host_text=host("bob", harnesses=("codex",))),
                            default=False)
        self.assertIsNone(loaded.host(HOST).policy.lend)


def reference_lendable(reading: dict, history: dict, offer: dict, k: dict) -> dict | None:
    """The formula of the study (section 1.4), written from the contract,
    independently of `resources`."""
    free, guest = reading["mem_available_bytes"], reading["guest_mem_bytes"]
    # guest memory unreadable (None, unlike 0: no guest): free memory unknown
    free = None if free is None or guest is None else free + guest
    lows = [v for v in (history["min_free_mem_bytes"], free) if v is not None]
    memory = int(k["margin"] * min(lows)) if lows else None
    a7 = history["app_cpu_p95"] if history["app_cpu_p95"] is not None else reading["app_cpu"]
    cpu = k["margin"] * (reading["cpu_count"] - a7) \
        if reading["cpu_count"] and a7 is not None else None
    if history["span_s"] < k["warmup_s"]:
        if memory is not None and reading["mem_total_bytes"]:
            memory = min(memory, int(k["warmup_memory_fraction"] * reading["mem_total_bytes"]))
        if cpu is not None:
            cpu = min(cpu, k["warmup_cpu"])
    disk = reading["disk_free_bytes"]
    disk = None if disk is None else max(0, disk - k["disk_reserve_bytes"])
    cpu = None if cpu is None else round(max(0.0, cpu), k["cpu_decimals"])
    memory = None if memory is None else max(0, memory)
    if cpu is None and memory is None and disk is None:
        return None
    if offer is None:  # no `policy.lend`: the host lends nothing
        return {"cpu": 0.0, "memory_bytes": 0, "disk_bytes": 0}
    return {"cpu": None if cpu is None else min(cpu, offer["cpu_max"]),
            "memory_bytes": None if memory is None else min(memory, offer["memory_max"]),
            "disk_bytes": None if disk is None else min(disk, offer["disk_max"])}


class LendableTest(unittest.TestCase):
    def test_golden_formula(self):
        data = golden("lendable.json")
        for case in data["cases"]:
            with self.subTest(case["why"]):
                self.assertEqual(reference_lendable(case["reading"], case["history"],
                                                    case["offer"], data["constants"]),
                                 case["lendable"])

    def test_golden_against_resources(self):
        """Once the common measure is in (L159d), ameesh's own computation
        gives the golden values too."""
        lendable = getattr(resources, "lendable", None)
        offered = getattr(resources, "offered", None)
        if lendable is None or offered is None:
            self.skipTest("resources.lendable comes with L159d")
        for case in golden("lendable.json")["cases"]:
            with self.subTest(case["why"]):
                self.assertEqual(offered(lendable(case["reading"], case["history"]),
                                         case["offer"]), case["lendable"])


if __name__ == "__main__":
    unittest.main()
