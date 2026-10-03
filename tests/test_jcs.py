# SPDX-License-Identifier: AGPL-3.0-only
"""JCS (RFC 8785) : exemples de la RFC, tri UTF-16, refus des nombres non sûrs."""
from __future__ import annotations

import json
import math
import struct
import unittest

from ameesh import jcs


def _double(hex_bits: str) -> float:
    return struct.unpack(">d", bytes.fromhex(hex_bits))[0]


class JcsRfcExamplesTest(unittest.TestCase):
    def test_rfc8785_3_2_2_chaines_et_litteraux(self):
        """§3.2.2 sans la partie « numbers » (doubles non entiers : refusés ici)."""
        source = ('{"string": "\\u20ac$\\u000F\\u000aA\'\\u0042\\u0022\\u005c\\\\\\"\\/",'
                  ' "literals": [null, true, false]}')
        value = jcs.loads(source)
        self.assertEqual(
            jcs.dumps(value),
            '{"literals":[null,true,false],"string":"€$\\u000f\\nA\'B\\"\\\\\\\\\\"/"}')

    def test_rfc8785_3_2_2_nombres_non_entiers_refuses(self):
        source = '{"numbers": [333333333.33333329, 1E30, 4.50, 2e-3, 0.000000000000000000000000001]}'
        with self.assertRaises(jcs.JcsError):
            jcs.canonicalize(jcs.loads(source))

    def test_rfc8785_3_2_3_tri_par_unites_utf16(self):
        source = ('{"\\u20ac": "Euro Sign", "\\r": "Carriage Return",'
                  ' "\\ufb33": "Hebrew Letter Dalet With Dagesh", "1": "One",'
                  ' "\\ud83d\\ude00": "Emoji: Grinning Face", "\\u0080": "Control",'
                  ' "\\u00f6": "Latin Small Letter O With Diaeresis"}')
        out = jcs.dumps(jcs.loads(source))
        expected_order = ["\r", "1", "\u0080", "ö", "€", "\U0001F600", "דּ"]
        self.assertEqual(list(json.loads(out)), expected_order)
        self.assertTrue(out.startswith('{"\\r":"Carriage Return","1":"One","\u0080":"Control"'))
        # l'émoji (D83D…) passe AVANT U+FB33 en UTF-16, alors qu'il passerait
        # après en ordre des points de code
        self.assertLess(out.index("\U0001F600"), out.index("דּ"))

    def test_rfc8785_3_2_4_sortie_utf8_sans_normalisation(self):
        value = jcs.loads('{\n  "Unnormalized Unicode":"A\\u030a"\n}')
        self.assertEqual(
            jcs.canonicalize(value).hex(" "),
            "7b 22 55 6e 6e 6f 72 6d 61 6c 69 7a 65 64 20 55 6e 69 63 6f 64 65 22 3a "
            "22 41 cc 8a 22 7d")

    def test_rfc8785_annexe_b_entiers(self):
        """Annexe B : les valeurs entières dans l'intervalle sûr."""
        self.assertEqual(jcs.dumps(_double("0000000000000000")), "0")
        self.assertEqual(jcs.dumps(_double("8000000000000000")), "0")  # -0
        self.assertEqual(jcs.dumps(9007199254740991), "9007199254740991")
        self.assertEqual(jcs.dumps(-9007199254740991), "-9007199254740991")
        self.assertEqual(jcs.dumps(_double("433fffffffffffff")), "9007199254740991")

    def test_rfc8785_annexe_b_hors_sous_ensemble_refuses(self):
        """Annexe B : ce que ce sous-ensemble refuse au lieu de mal l'écrire."""
        for bits in ("0000000000000001",   # 5e-324
                     "8000000000000001",   # -5e-324
                     "7fefffffffffffff",   # 1.7976931348623157e+308 (entier, hors intervalle)
                     "4340000000000000",   # 9007199254740992 = 2^53 (non sûr)
                     "c340000000000000",   # -2^53
                     "4430000000000000",   # 295147905179352830000
                     "44b52d02c7e14af5",   # 9.999999999999997e+22
                     "3eb0c6f7a0b5ed8d",   # 0.000001
                     "41b3de4355555553",   # 333333333.3333332
                     "7fffffffffffffff",   # NaN
                     "7ff0000000000000"):  # Infinity
            with self.subTest(bits=bits):
                with self.assertRaises(jcs.JcsError):
                    jcs.dumps(_double(bits))


class JcsBehaviourTest(unittest.TestCase):
    def test_entiers_hors_intervalle_sur(self):
        for value in (2 ** 53, -(2 ** 53), 10 ** 30):
            with self.subTest(value=value):
                with self.assertRaises(jcs.JcsError):
                    jcs.dumps(value)

    def test_flottants(self):
        self.assertEqual(jcs.dumps(1.0), "1")
        self.assertEqual(jcs.dumps(-12.0), "-12")
        for value in (0.5, math.nan, math.inf, -math.inf, 1e-7):
            with self.subTest(value=value):
                with self.assertRaises(jcs.JcsError):
                    jcs.dumps(value)

    def test_echappements(self):
        self.assertEqual(jcs.dumps("\x00\x1f\x7f/é"), '"\\u0000\\u001f\x7f/é"')
        self.assertEqual(jcs.dumps("\b\t\n\f\r\"\\"), '"\\b\\t\\n\\f\\r\\"\\\\"')
        self.assertEqual(jcs.dumps("  "), '"  "')

    def test_surrogate_isole_refuse(self):
        for text in ("\ud800", "a\udfffb"):
            with self.subTest(text=repr(text)):
                with self.assertRaises(jcs.JcsError):
                    jcs.dumps(text)
                with self.assertRaises(jcs.JcsError):
                    jcs.dumps({text: 1})

    def test_types_refuses(self):
        for value in ({1: "x"}, {(1, 2): "x"}, b"octets", {1, 2}, object()):
            with self.subTest(value=type(value).__name__):
                with self.assertRaises(jcs.JcsError):
                    jcs.dumps(value)

    def test_structures(self):
        self.assertEqual(jcs.dumps({}), "{}")
        self.assertEqual(jcs.dumps([]), "[]")
        self.assertEqual(jcs.dumps({"b": [1, {"d": None, "c": True}], "a": "x"}),
                         '{"a":"x","b":[1,{"c":true,"d":null}]}')
        self.assertEqual(jcs.dumps((1, 2)), "[1,2]")
        self.assertIsInstance(jcs.canonicalize({"a": 1}), bytes)

    def test_ordre_d_insertion_sans_effet(self):
        a = {"z": 1, "a": 2, "m": {"y": 1, "b": 2}}
        b = {"m": {"b": 2, "y": 1}, "a": 2, "z": 1}
        self.assertEqual(jcs.canonicalize(a), jcs.canonicalize(b))

    def test_accord_avec_json_pour_l_ascii(self):
        value = {"action_id": "act_X", "amount": 1250, "args": {"pr": 42, "repo": "o/r"},
                 "currency": None, "ok": False}
        self.assertEqual(jcs.dumps(value), json.dumps(
            value, sort_keys=True, separators=(",", ":"), ensure_ascii=False))

    def test_profondeur_bornee(self):
        value: list = []
        for _ in range(jcs.MAX_DEPTH + 5):
            value = [value]
        with self.assertRaises(jcs.JcsError):
            jcs.dumps(value)

    def test_loads_strict(self):
        with self.assertRaises(jcs.JcsError):
            jcs.loads('{"a": 1, "a": 2}')
        with self.assertRaises(jcs.JcsError):
            jcs.loads('{"a": {"b": 1, "b": 1}}')
        for text in ('{"a": NaN}', '[Infinity]', '[-Infinity]', "{", b"\xff\xfe"):
            with self.subTest(text=text):
                with self.assertRaises(jcs.JcsError):
                    jcs.loads(text)
        self.assertEqual(jcs.loads(b'{"a": [1, "\\u00e9"]}'), {"a": [1, "é"]})


if __name__ == "__main__":
    unittest.main()
