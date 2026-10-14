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

    def test_busy_tasks_is_read_and_needs_busy_units(self):
        # #1256 : le motif de tâche est ce qui distingue un job d'un écouteur
        # persistant ; il ne se lit que sur des unités (`busy_units`)
        base = {"cpu_max": 1, "memory_max": "1GiB", "disk_max": "1GiB"}
        offer, problems, unknown = capacity_guard.parse_lend(dict(base, reclaim={
            "busy_units": ["actions.runner.*"], "busy_tasks": ["Runner.Worker"],
            "busy_max": 2}))
        self.assertEqual(problems, [])
        self.assertEqual(unknown, [])
        self.assertEqual(offer["reclaim"]["busy_tasks"], ["Runner.Worker"])
        self.assertEqual(offer["reclaim"]["busy_max"], 2)
        # une chaîne seule vaut une liste d'un motif (comme `busy_units`)
        offer, problems, _ = capacity_guard.parse_lend(dict(base, reclaim={
            "busy_units": ["actions.runner.*"], "busy_tasks": "Runner.Worker"}))
        self.assertEqual(problems, [])
        self.assertEqual(offer["reclaim"]["busy_tasks"], ["Runner.Worker"])
        # sans unités, ou avec une entrée vide : refusé, l'offre tombe
        for reclaim in ({"busy_tasks": ["Runner.Worker"]},
                        {"busy_units": ["actions.runner.*"], "busy_tasks": []},
                        {"busy_units": ["actions.runner.*"], "busy_tasks": [""]},
                        {"busy_units": ["actions.runner.*"], "busy_tasks": [3]},
                        {"busy_units": ["actions.runner.*"], "busy_tasks": ["a\nb"]}):
            with self.subTest(reclaim=reclaim):
                offer, problems, _ = capacity_guard.parse_lend(dict(base, reclaim=reclaim))
                self.assertIsNone(offer, reclaim)
                self.assertTrue(any("busy_tasks" in p for p in problems), problems)

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
                                          "memory_pressure_max", "io_pressure_max",
                                          "disk_free_min", "busy_max")]
        for path in keys:
            for value in (10 ** 400, float("inf"), float("nan")):
                with self.subTest(path=path, value=str(value)[:6]):
                    lend = json.loads(json.dumps(base))
                    (lend if len(path) == 1 else lend["reclaim"])[path[-1]] = value
                    offer, problems, _ = capacity_guard.parse_lend(lend)
                    self.assertIsNone(offer)
                    self.assertTrue(any(path[-1] in p for p in problems), problems)

    def test_guard_reclaim_keys_are_known(self):
        # L479/#450: the guard's three reclaim keys are read by the contract
        # too: a fiche using them is valid (no unknown key), and they are
        # validated the way the guard does
        base = {"cpu_max": 1, "memory_max": "1GiB", "disk_max": "1GiB"}
        lend = dict(base, reclaim={"io_pressure_max": "20%", "disk_free_min": "10GiB",
                                   "disk_paths": ["/", "/srv/ameesh-guest"]})
        offer, problems, unknown = capacity_guard.parse_lend(lend)
        self.assertEqual(problems, [])
        self.assertEqual(unknown, [])
        self.assertEqual(offer["reclaim"], {"io_pressure_max": 20.0, "disk_free_min": 10 * GIB,
                                            "disk_paths": ["/", "/srv/ameesh-guest"]})
        self.assertTrue(set(capacity_guard.GUARD_RECLAIM_KEYS)
                        <= set(capacity_guard.RECLAIM_KEYS))
        for key, value in (("io_pressure_max", "120%"), ("io_pressure_max", -1),
                           ("disk_free_min", "lots"), ("disk_free_min", -1),
                           ("disk_paths", "/"),
                           ("disk_paths", [""]), ("disk_paths", ["/", 3])):
            with self.subTest(key=key, value=value):
                offer, problems, unknown = capacity_guard.parse_lend(
                    dict(base, reclaim={key: value}))
                self.assertIsNone(offer)
                self.assertTrue(any(key in p for p in problems), problems)
                self.assertEqual(unknown, [])

    def test_disk_paths_are_absolute_normalized_and_unique(self):
        # codex #459 r1: a relative or traversing path reached the disk probe
        # unchanged; each watched filesystem is named once, by an absolute
        # normalized path (surrounding blanks are stripped, as for the other
        # keys)
        base = {"cpu_max": 1, "memory_max": "1GiB", "disk_max": "1GiB"}
        for paths in (["../../outside"], ["srv/ameesh"], ["/srv/../etc"],
                      ["/srv/./../etc"]):
            with self.subTest(paths=paths):
                offer, problems, unknown = capacity_guard.parse_lend(
                    dict(base, reclaim={"disk_paths": paths}))
                self.assertIsNone(offer)
                self.assertTrue(any("disk_paths" in p for p in problems), problems)
                self.assertEqual(unknown, [])
        offer, problems, _ = capacity_guard.parse_lend(
            dict(base, reclaim={"disk_paths": [" / ", "/srv/ameesh-guest"]}))
        self.assertEqual(problems, [])
        self.assertEqual(offer["reclaim"]["disk_paths"], ["/", "/srv/ameesh-guest"])

    def test_disk_paths_legacy_spellings_are_normalized_with_a_warning(self):
        # codex #459 r3: main accepted `//srv`, `/srv/.`, `//` inside, a
        # duplicate and an empty list; refusing them would stop the guard
        # from starting after the update. They name the same directories and
        # are normalized — the warning, not a refusal, asks for a clean fiche
        base = {"cpu_max": 1, "memory_max": "1GiB", "disk_max": "1GiB"}
        for paths, expected in ((["//srv"], ["/srv"]),
                                (["/srv/."], ["/srv"]),
                                (["/srv//guest"], ["/srv/guest"]),
                                (["/."], ["/"]),
                                (["/srv", "/srv/"], ["/srv"]),
                                ([], ["/"])):
            with self.subTest(paths=paths):
                warnings: list[str] = []
                offer, problems, _ = capacity_guard.parse_lend(
                    dict(base, reclaim={"disk_paths": paths}), warnings)
                self.assertEqual(problems, [])
                self.assertEqual(offer["reclaim"]["disk_paths"], expected)
                self.assertTrue(warnings, "la forme héritée est signalée")
                self.assertTrue(all("disk_paths" in w for w in warnings), warnings)
        warnings = []
        offer, problems, _ = capacity_guard.parse_lend(
            dict(base, reclaim={"disk_paths": ["/", "/srv"]}), warnings)
        self.assertEqual(problems, [])
        self.assertEqual(warnings, [], "une fiche propre ne dit rien")

    def test_disk_paths_refuse_nul(self):
        # codex #459 r1: `disk_paths: ["/\u0000"]` was accepted, then
        # `resources.disk_free` raised an uncaught ValueError ("embedded null
        # byte") while measuring; the reader refuses it
        base = {"cpu_max": 1, "memory_max": "1GiB", "disk_max": "1GiB"}
        for path in ("/\x00", "/srv\x00/guest"):
            with self.subTest(path=repr(path)):
                offer, problems, _ = capacity_guard.parse_lend(
                    dict(base, reclaim={"disk_paths": [path]}))
                self.assertIsNone(offer)
                self.assertTrue(any("disk_paths" in p for p in problems), problems)

    def test_reclaim_sizes_are_positive_whole_bytes(self):
        # codex #459 r1: `disk_free_min: 0` and `"0.5B"` were accepted as 0,
        # and 0 free bytes were still above it: disk reclaim was disabled; a
        # threshold is a size > 0. r4b: "1.5B" (main truncated it to 1) is
        # accepted truncated, with a warning
        base = {"cpu_max": 1, "memory_max": "1GiB", "disk_max": "1GiB"}
        for key in ("disk_free_min", "memory_available_min"):
            for value in (0, 0.0, "0B", "0.5B", "0.0001KiB", -1):
                with self.subTest(key=key, value=value):
                    offer, problems, _ = capacity_guard.parse_lend(
                        dict(base, reclaim={key: value}))
                    self.assertIsNone(offer)
                    self.assertTrue(any(key in p for p in problems), problems)
            offer, problems, _ = capacity_guard.parse_lend(
                dict(base, reclaim={key: "4.5GiB"}))
            self.assertEqual(problems, [])
            self.assertEqual(offer["reclaim"][key], int(4.5 * GIB))
            warnings: list[str] = []
            offer, problems, _ = capacity_guard.parse_lend(
                dict(base, reclaim={key: "1.5B"}), warnings)
            self.assertEqual(problems, [])
            self.assertEqual(offer["reclaim"][key], 1)
            self.assertTrue(any(key in w and "truncated" in w for w in warnings), warnings)

    def test_disk_paths_accept_a_trailing_slash(self):
        # codex #459 r2: a trailing slash names the same filesystem; it is
        # normalized before the duplicate check, so an offer valid at r1
        # (`["/srv/"]`) is accepted as `["/srv"]` instead of exiting 1 at
        # startup
        base = {"cpu_max": 1, "memory_max": "1GiB", "disk_max": "1GiB"}
        offer, problems, _ = capacity_guard.parse_lend(
            dict(base, reclaim={"disk_paths": ["/srv/", "/var/lib//"]}))
        self.assertEqual(problems, [])
        self.assertEqual(offer["reclaim"]["disk_paths"], ["/srv", "/var/lib"])
        # r3: the same path named twice is collapsed, not refused
        for paths in (["/srv", "/srv/"], ["/srv/", "/srv//"]):
            with self.subTest(paths=paths):
                warnings: list[str] = []
                offer, problems, _ = capacity_guard.parse_lend(
                    dict(base, reclaim={"disk_paths": paths}), warnings)
                self.assertEqual(problems, [])
                self.assertEqual(offer["reclaim"]["disk_paths"], ["/srv"])
                self.assertTrue(any("duplicate" in w for w in warnings), warnings)

    def test_disk_paths_trailing_slash_is_normalized_with_a_warning(self):
        # codex #459 r4 (P3): "/srv/" and "/srv//" were normalized without the
        # warning the doc announces (`host-lend-legacy`): the comparison
        # stripped the trailing slash before comparing
        base = {"cpu_max": 1, "memory_max": "1GiB", "disk_max": "1GiB"}
        for paths, expected in ((["/srv/"], ["/srv"]),
                                (["/srv//"], ["/srv"]),
                                (["/srv//guest/"], ["/srv/guest"]),
                                (["/."], ["/"])):
            with self.subTest(paths=paths):
                warnings: list[str] = []
                offer, problems, _ = capacity_guard.parse_lend(
                    dict(base, reclaim={"disk_paths": paths}), warnings)
                self.assertEqual(problems, [])
                self.assertEqual(offer["reclaim"]["disk_paths"], expected)
                self.assertTrue(any("normalized" in w for w in warnings), warnings)
        warnings = []
        offer, problems, _ = capacity_guard.parse_lend(
            dict(base, reclaim={"disk_paths": ["/", "/srv"]}), warnings)
        self.assertEqual(problems, [])
        self.assertEqual(warnings, [], "a clean fiche says nothing")

    def test_les_codes_classent_sans_lire_le_message(self):
        # mesh-design #459 r4c: the tolerated family ("main accepted it") is
        # read from `LendProblem.code`, never from the message — a message
        # quotes the value the operator wrote, which can copy the wording
        base = {"cpu_max": 1, "memory_max": "1GiB", "disk_max": "1GiB"}
        for reclaim, code in (({"disk_paths": ["../etc"]},
                               capacity_guard.PROBLEM_DISK_PATHS),
                              ({"disk_paths": ["/\x00"]},
                               capacity_guard.PROBLEM_DISK_PATHS),
                              ({"disk_paths": ["rel/x", "/srv"]},
                               capacity_guard.PROBLEM_DISK_PATHS),
                              ({"disk_free_min": 0}, capacity_guard.PROBLEM_SIZE_ZERO),
                              ({"disk_free_min": -0.0}, capacity_guard.PROBLEM_SIZE_ZERO),
                              ({"disk_free_min": "0.5B"},
                               capacity_guard.PROBLEM_SIZE_ZERO),
                              ({"memory_available_min": "0.0001KiB"},
                               capacity_guard.PROBLEM_SIZE_ZERO)):
            with self.subTest(reclaim=reclaim):
                offer, problems, _ = capacity_guard.parse_lend(dict(base, reclaim=reclaim))
                self.assertIsNone(offer)
                self.assertTrue(problems)
                self.assertEqual([p.code for p in problems], [code])
                self.assertTrue(capacity_guard.tolerated_only(problems))
        # several tolerated problems together: still the family
        offer, problems, _ = capacity_guard.parse_lend(dict(
            base, reclaim={"disk_paths": ["../etc"], "disk_free_min": 0}))
        self.assertEqual({p.code for p in problems},
                         {capacity_guard.PROBLEM_DISK_PATHS,
                          capacity_guard.PROBLEM_SIZE_ZERO})
        self.assertTrue(capacity_guard.tolerated_only(problems))
        # anything else is refused, even next to a tolerated problem
        for lend in ({"cpu_max": 1, "memory_max": "1GiB",
                      "reclaim": {"disk_paths": ["../etc"]}},
                     dict(base, reclaim={"disk_paths": "/"}),
                     dict(base, reclaim={"disk_free_min": -1}),
                     dict(base, reclaim={"disk_free_min": "lots"}),
                     dict(base, reclaim={"io_pressure_max": None}),
                     dict(base, cpu_max="three")):
            with self.subTest(lend=lend):
                offer, problems, _ = capacity_guard.parse_lend(lend)
                self.assertTrue(problems)
                self.assertFalse(capacity_guard.tolerated_only(problems))
        # the value that copied the wording of a refused path is not one: the
        # old substring test ("disk_paths`" + "refused path") tolerated it
        _, problems, _ = capacity_guard.parse_lend(
            dict(base, windows=["mon 10:00-11:00 disk_paths` refused path"]))
        self.assertIn("disk_paths`", problems[0])
        self.assertIn("refused path", problems[0])
        self.assertEqual([p.code for p in problems], [capacity_guard.PROBLEM_INVALID])
        self.assertFalse(capacity_guard.tolerated_only(problems))

    def test_une_taille_au_dela_des_chiffres_de_python_est_refusee(self):
        # codex #459 r4 (P3): `disk_free_min: "1." + "0"*4300 + "GiB"` raised
        # an uncaught ValueError (Python's integer conversion limit) in the
        # exact reader; it is refused, with a problem naming the key
        if not resources.bytes_digit_limit():
            self.skipTest("this interpreter converts any number of digits")
        huge = "1." + "0" * 4300 + "GiB"
        base = {"cpu_max": 1, "memory_max": "1GiB", "disk_max": "1GiB"}
        self.assertIsNone(resources.parse_bytes(huge, decimal_ok=False))
        # the caps go through the truncating reader only: no exact path, so no
        # ValueError, and main's reading (1 GiB) is kept
        self.assertEqual(resources.parse_bytes(huge), 1 * GIB)
        for key in ("memory_max", "agent_memory_max", "disk_max"):
            with self.subTest(key=key):
                offer, problems, _ = capacity_guard.parse_lend(dict(base, **{key: huge}))
                self.assertEqual(problems, [])
                self.assertEqual(offer[key], 1 * GIB)
        for key in ("disk_free_min", "memory_available_min"):
            with self.subTest(key=key):
                offer, problems, _ = capacity_guard.parse_lend(
                    dict(base, reclaim={key: huge}))
                self.assertIsNone(offer)
                self.assertEqual([p.code for p in problems],
                                 [capacity_guard.PROBLEM_INVALID])
                self.assertTrue(any(key in p for p in problems), problems)
                self.assertFalse(capacity_guard.tolerated_only(problems))

    def test_reclaim_sizes_accept_integral_numbers(self):
        # codex #459 r2: `memory_available_min: 6442450944.0` (exactly 6 GiB)
        # was valid at r1 and refused although it is a whole number of bytes.
        # r4b: `6442450944.5` is main's truncated value, with a warning
        base = {"cpu_max": 1, "memory_max": "1GiB", "disk_max": "1GiB"}
        for key in ("disk_free_min", "memory_available_min"):
            for value, expected in ((6442450944.0, 6442450944),
                                    (4.5 * GIB, int(4.5 * GIB))):
                with self.subTest(key=key, value=value):
                    warnings: list[str] = []
                    offer, problems, _ = capacity_guard.parse_lend(
                        dict(base, reclaim={key: value}), warnings)
                    self.assertEqual(problems, [])
                    self.assertEqual(offer["reclaim"][key], expected)
                    self.assertEqual(warnings, [])
            warnings = []
            offer, problems, _ = capacity_guard.parse_lend(
                dict(base, reclaim={key: 6442450944.5}), warnings)
            self.assertEqual(problems, [])
            self.assertEqual(offer["reclaim"][key], 6442450944)
            self.assertTrue(any("truncated" in w for w in warnings), warnings)
            for value in (-1.0, float("nan"), float("inf")):
                with self.subTest(key=key, value=value):
                    offer, problems, _ = capacity_guard.parse_lend(
                        dict(base, reclaim={key: value}))
                    self.assertIsNone(offer)
                    self.assertTrue(any(key in p for p in problems), problems)

    def test_reclaim_sizes_truncate_like_main_with_a_warning(self):
        # codex #459 r2 refused a fractional byte count ("0.001KiB" -> 1);
        # mesh-design #459 r4b: main truncated it, an update must not
        # invalidate the offer — truncate the same way, warn, and let the
        # canon report it as host-lend-legacy
        base = {"cpu_max": 1, "memory_max": "1GiB", "disk_max": "1GiB"}
        for key in ("disk_free_min", "memory_available_min"):
            for value, expected in (("0.001KiB", 1), ("0.1KiB", 102),
                                    ("1.5B", 1), ("1.0000000000000001GiB", GIB)):
                with self.subTest(key=key, value=value):
                    warnings: list[str] = []
                    offer, problems, _ = capacity_guard.parse_lend(
                        dict(base, reclaim={key: value}), warnings)
                    self.assertEqual(problems, [])
                    self.assertEqual(offer["reclaim"][key], expected)
                    self.assertTrue(any(key in w and "truncated" in w for w in warnings),
                                    warnings)
            for value, expected in (("4.5GiB", int(4.5 * GIB)), ("1.5KiB", 1536)):
                with self.subTest(key=key, value=value):
                    warnings = []
                    offer, problems, _ = capacity_guard.parse_lend(
                        dict(base, reclaim={key: value}), warnings)
                    self.assertEqual(problems, [])
                    self.assertEqual(offer["reclaim"][key], expected)
                    self.assertEqual(warnings, [], "exact byte count, nothing truncated")

    def test_reclaim_sizes_keep_every_input_digit(self):
        # codex #459 r3: the whole-byte check went through `Decimal` under the
        # default 28-digit context, which rounds. r4b: such a value is
        # truncated to main's value, with a warning — every input digit counts
        base = {"cpu_max": 1, "memory_max": "1GiB", "disk_max": "1GiB"}
        for key in ("disk_free_min", "memory_available_min"):
            for value, expected in (("1.0000000000000000000000000001GiB", GIB),
                                    ("1.0000000000000000000000000001KiB", 1024)):
                with self.subTest(key=key, value=value):
                    warnings: list[str] = []
                    offer, problems, _ = capacity_guard.parse_lend(
                        dict(base, reclaim={key: value}), warnings)
                    self.assertEqual(problems, [])
                    self.assertEqual(offer["reclaim"][key], expected)
                    self.assertTrue(any("truncated" in w for w in warnings), warnings)
            # a value that truncates to 0 bytes is still refused
            for value in ("0." + "0" * 27 + "1GiB",):
                with self.subTest(key=key, value=value):
                    offer, problems, _ = capacity_guard.parse_lend(
                        dict(base, reclaim={key: value}))
                    self.assertIsNone(offer)
                    self.assertTrue(any(key in p for p in problems), problems)
            # the same values padded with zeros after the point are exact and
            # must keep passing, without a warning
            for value, expected in (("1." + "0" * 28 + "GiB", GIB),
                                    ("4.5" + "0" * 26 + "GiB", int(4.5 * GIB))):
                with self.subTest(key=key, value=value):
                    warnings = []
                    offer, problems, _ = capacity_guard.parse_lend(
                        dict(base, reclaim={key: value}), warnings)
                    self.assertEqual(problems, [])
                    self.assertEqual(offer["reclaim"][key], expected)
                    self.assertEqual(warnings, [])

    def test_reclaim_explicit_nulls_are_refused(self):
        # codex #459 r1: an explicit null is not an absent key: a null
        # `io_pressure_max` disabled the threshold and null disk keys fell
        # back to the defaults, silently
        base = {"cpu_max": 1, "memory_max": "1GiB", "disk_max": "1GiB"}
        for key in ("io_pressure_max", "disk_free_min", "disk_paths",
                    "memory_available_min", "cpu_pressure_max", "memory_pressure_max",
                    "busy_units", "busy_tasks", "busy_max"):
            with self.subTest(key=key):
                offer, problems, unknown = capacity_guard.parse_lend(
                    dict(base, reclaim={key: None}))
                self.assertIsNone(offer)
                self.assertTrue(any(key in p for p in problems), problems)
                self.assertEqual(unknown, [])

    def test_percent_is_a_single_sign(self):
        # codex #459 r1: '20%%' parsed as 20% (rstrip removed every sign); the
        # sign is single, and only at the end
        self.assertEqual(resources.parse_percent("20%"), 20.0)
        self.assertEqual(resources.parse_percent(" 20 % "), 20.0)
        self.assertEqual(resources.parse_percent("20"), 20.0)
        for text in ("20%%", "20 % %", "%%", "%20", "2%0", "20%%%"):
            with self.subTest(text=text):
                self.assertIsNone(resources.parse_percent(text))

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

    def test_canon_check_accepts_the_guard_reclaim_keys(self):
        # L479/#450: the guard's three reclaim keys are part of the offer the
        # canon reads: a valid fiche using them is no host-lend-unknown
        text = host("bob", harnesses=("codex",), resources_yaml=(
            "  lend:\n    cpu_max: 3\n    memory_max: 14GiB\n    disk_max: 80GiB\n"
            "    reclaim:\n      io_pressure_max: 20%\n      disk_free_min: 10GiB\n"
            "      disk_paths: [\"/\", \"/srv/ameesh-guest\"]\n"))
        loaded = canon.load(self.acme(host_text=text, name="l479"), default=False)
        found = canon.validate(loaded)
        self.assertNotIn("host-lend-unknown", codes(found, canon.WARNING))
        self.assertNotIn("host-policy-invalid", codes(found, canon.ERROR))
        self.assertEqual(loaded.host(HOST).policy.lend["reclaim"],
                         {"io_pressure_max": 20.0, "disk_free_min": 10 * GIB,
                          "disk_paths": ["/", "/srv/ameesh-guest"]})

    def test_canon_check_normalizes_legacy_disk_paths_with_a_warning(self):
        # codex #459 r3: an offer main accepted (`//srv`, `/srv/.`, a duplicate)
        # must not become a startup failure: the canon reads it normalized and
        # warns (`host-lend-legacy`), it does not refuse it
        text = host("bob", harnesses=("codex",), resources_yaml=(
            "  lend:\n    cpu_max: 3\n    memory_max: 14GiB\n    disk_max: 80GiB\n"
            "    reclaim:\n      disk_paths: [\"//srv\", \"/srv/.\"]\n"))
        loaded = canon.load(self.acme(host_text=text, name="l479-legacy"), default=False)
        found = canon.validate(loaded)
        self.assertNotIn("host-policy-invalid", codes(found, canon.ERROR))
        self.assertIn("host-lend-legacy", codes(found, canon.WARNING))
        self.assertEqual(loaded.host(HOST).policy.lend["reclaim"]["disk_paths"],
                         ["/srv"])

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
                                                   "memory_pressure_max", "io_pressure_max",
                                                   "disk_free_min", "busy_max")]
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
