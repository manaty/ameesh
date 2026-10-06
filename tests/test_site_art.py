# SPDX-License-Identifier: AGPL-3.0-only
"""Public site art (L32): generated files up to date, and the rules of the
"journey" draws (realistic pairs except Hermes, no model or harness twice on
the road at once, never the same start twice in a row, never orange; the
horses' winks follow the model's family)."""
import colorsys
import importlib.util
import json
import pathlib
import re
import shutil
import subprocess
import sys
import unittest

ROOT = pathlib.Path(__file__).resolve().parent.parent
INDEX = ROOT / "site" / "landing" / "index.html"
SPEC = importlib.util.spec_from_file_location("site_art", ROOT / "scripts" / "site_art.py")
art = importlib.util.module_from_spec(SPEC)
SPEC.loader.exec_module(art)

DRAWS = 1000


def family_of(model):
    return next(f for f, models in art.FAMILY.items() if model in models)


def is_orange(hex_colour):
    r, g, b = (int(hex_colour[i:i + 2], 16) / 255 for i in (1, 3, 5))
    h, l, s = colorsys.rgb_to_hls(r, g, b)
    return 15 <= h * 360 <= 45 and s >= .6 and .3 <= l <= .75


class SiteArtTest(unittest.TestCase):
    def check_pairs(self, pairs):
        """pairs: {slot: (harness, model)}"""
        for slot, (h, m) in pairs.items():
            self.assertIn(h, art.HARNESS, slot)
            self.assertIn(m, art.COAT, slot)
            accepted = art.HARNESS[h][1]
            if accepted is not None:   # Claude Code, Codex, dsh: their own family only
                self.assertEqual(family_of(m), accepted, "%s: %s + %s" % (slot, h, m))
        # on the road together: the team and the relay newcomer at each relay
        for group in (("W1", "L1", "W2"), ("W2", "L1", "L2")):
            hs = [pairs[s][0] for s in group]
            ms = [pairs[s][1] for s in group]
            self.assertEqual(len(set(hs)), 3, "harness twice in %s: %s" % (group, pairs))
            self.assertEqual(len(set(ms)), 3, "model twice in %s: %s" % (group, pairs))

    def test_generated_files_are_up_to_date(self):
        out = subprocess.run([sys.executable, str(ROOT / "scripts" / "site_art.py"), "--check"],
                             capture_output=True, text=True)
        self.assertEqual(out.returncode, 0, out.stdout + out.stderr)

    def test_default_sequence_follows_the_rules(self):
        self.check_pairs(art.DEFAULT_PAIRS)
        html = INDEX.read_text()
        for slot, (h, m) in art.DEFAULT_PAIRS.items():   # without script: the right wink per family
            self.assertIn('class="hz hz-%s a f-%s" data-fam="%s"' % (slot, family_of(m), slot), html)
        self.assertIn('data-sign="L1">%s<' % art.NEXT_VERSION["GPT-6"], html)
        self.assertEqual(art.NEXT_VERSION, {"GPT-6": "GPT-7", "GPT-5.5": "GPT-6"})
        self.assertTrue(any(h == "he" for h, _ in art.DEFAULT_PAIRS.values()))
        for gait in art.DEFAULT_GAITS.values():
            self.assertIn(gait, art.EFFORT)

    def test_never_orange_but_the_driver(self):
        names = [c + "-" + k for c in set(art.COAT.values()) for k in "sf"]
        names += [h for h in art.HARNESS] + [h + "-soft" for h in art.HARNESS] + ["buggy-f", "buggy-s", "oat"]
        for name in names:
            for colour in art.COLOURS[name]:
                self.assertFalse(is_orange(colour), "%s %s looks orange" % (name, colour))
        self.assertTrue(all(is_orange(c) for c in art.DRIVER))
        css = INDEX.read_text()
        for name in names:   # the page uses the generator's colours
            self.assertIn("--%s: %s;" % (name, art.COLOURS[name][0]), css)

    def test_page_draws(self):
        """Runs the page's own draw function 1000 times (needs node)."""
        node = shutil.which("node")
        if not node:
            self.skipTest("node not installed")
        html = INDEX.read_text()
        code = re.search(r"// journey:draw:begin(.*?)// journey:draw:end", html, re.S).group(1)
        prog = code + """
var out = [], last = '';
for (var i = 0; i < %d; i++) {
  var d = roadDraw(Math.random, last);
  if (d) { d.looks = {}; Object.keys(d.pairs).forEach(function (s) { d.looks[s] = roadLook(d.pairs[s].m); }); }
  out.push(d); last = d ? d.start : last;
}
process.stdout.write(JSON.stringify(out));
""" % DRAWS
        res = subprocess.run([node, "-e", prog], capture_output=True, text=True, timeout=60)
        self.assertEqual(res.returncode, 0, res.stderr)
        draws = json.loads(res.stdout)
        self.assertEqual(len(draws), DRAWS)
        previous, starts, hermes_cross, glances = None, set(), 0, 0
        for d in draws:
            self.assertIsNotNone(d)
            pairs = {s: (p["h"], p["m"]) for s, p in d["pairs"].items()}
            self.check_pairs(pairs)
            self.assertTrue(any(h == "he" for h, _ in pairs.values()), pairs)
            self.assertNotEqual(d["start"], previous, "same start twice in a row")
            previous = d["start"]
            starts.add(d["start"])
            self.assertEqual(sorted(d["gaits"]), ["A", "B", "C"])
            for gait in d["gaits"].values():
                self.assertIn(gait, art.EFFORT)
            hermes_cross += any(h == "he" and family_of(m) != "claude" for h, m in pairs.values())
            # what a horse wears follows its model's family, Hermes included
            for slot, look in d["looks"].items():
                m = pairs[slot][1]
                fam = family_of(m)
                self.assertEqual(look["fam"], fam, (slot, m))
                self.assertEqual(look["helmet"], fam == "claude", (slot, m))
                self.assertEqual(look["oats"], 1 if fam == "deepseek" else "full", (slot, m))
                if fam == "gpt":   # the sign always announces the version above the horse's own
                    self.assertEqual(look["sign"], "GPT-%d" % (int(float(m.split("-")[1])) + 1), m)
                else:
                    self.assertIsNone(look["sign"])
            for seg, team in art.TEAMS.items():
                fams = sorted(family_of(pairs[s][1]) for s in team)
                self.assertEqual(d["glance"][seg], fams == ["claude", "gpt"], (seg, pairs))
                glances += d["glance"][seg]
        self.assertGreater(len(starts), 20)       # really random
        self.assertGreater(hermes_cross, 0)       # Hermes does carry other families
        self.assertGreater(glances, 0)


if __name__ == "__main__":
    unittest.main()
