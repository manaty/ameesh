#!/usr/bin/env python3
# SPDX-License-Identifier: AGPL-3.0-only
"""Logo, favicon and "journey" animation of the public site (single source).

    python3 scripts/site_art.py           # (re)writes the generated files
    python3 scripts/site_art.py --check   # exit 1 if a generated file is stale

The picture: ameesh is a horse-drawn buggy. The horses are the models, the
harness ties a horse to the carriage (Claude Code, Codex, dsh, Hermes), the
driver holding the reins is the human who approves, and at a relay a tired
pair is swapped for a fresh one without stopping the journey. The gait of the
team is the effort (walk, trot, gallop).

Generated:
  site/landing/logo.svg, site/landing/favicon.svg
  site/docs/assets/logo.svg (header variant), site/docs/assets/favicon.svg
  site/landing/index.html, between the markers
    <!-- logo:begin --> … <!-- logo:end -->        header logo
    /* journey:css:begin */ … /* journey:css:end */ styles
    <!-- journey:begin --> … <!-- journey:end -->  the animated section

The animation is SVG + CSS. Without script it plays a fixed sequence; with
prefers-reduced-motion it is a still image. A short inline script (between
"journey:draw" markers, tested by tests/test_site_art.py) draws new pairs
(model + harness) and gaits at each loop.
"""
import json
import pathlib
import re
import sys

ROOT = pathlib.Path(__file__).resolve().parent.parent
LANDING = ROOT / "site" / "landing"
DOCS_ASSETS = ROOT / "site" / "docs" / "assets"

# ---------------------------------------------------------------- the cast
# models by family; each model has its own coat
FAMILY = {"claude": ["Opus 5.5", "Sonnet 5"], "gpt": ["GPT-6", "GPT-5.5"],
          "deepseek": ["DeepSeek 4.1", "DeepSeek 4"]}
COAT = {"Opus 5.5": "bay", "Sonnet 5": "chest", "GPT-6": "grey", "GPT-5.5": "roan",
        "DeepSeek 4.1": "black", "DeepSeek 4": "dun"}
# harness: (label, family it accepts; None = any model)
HARNESS = {"cc": ["Claude Code", "claude"], "cx": ["Codex", "gpt"], "ds": ["dsh", "deepseek"],
           "he": ["Hermes", None]}
EFFORT = {"walk": "low", "trot": "medium", "gallop": "high"}
# fixed sequence (no script): wheel and lead horses, then the pairs brought by relays 1 and 2
DEFAULT_PAIRS = {"W1": ("cc", "Opus 5.5"), "L1": ("cx", "GPT-6"),
                 "W2": ("ds", "DeepSeek 4.1"), "L2": ("he", "Sonnet 5")}
# a wink per family, on the horses only: Claude wears a riding helmet, a GPT horse
# carries a sign announcing the version above its own, a DeepSeek horse pulls as
# much on a single oat (nosebags at the relays); a Claude and a GPT horse in the
# same team exchange a sideways glance once per stretch of road.
NEXT_VERSION = {"GPT-6": "GPT-7", "GPT-5.5": "GPT-6"}
TEAMS = {"A": ("W1", "L1"), "B": ("W2", "L1"), "C": ("W2", "L2")}   # who pulls together, per stretch
GLANCE = {"A": 12.6, "B": 19.3, "C": 26.2}                            # start of each glance
DEFAULT_GAITS = {"A": "trot", "B": "gallop", "C": "trot"}

# colours: (light, dark). Orange is kept for the driver and the reins only.
COLOURS = {
    "buggy-f": ("#23262d", "#090a0d"), "buggy-s": ("#15171c", "#a3abba"),
    "bay-s": ("#4a2c1a", "#e6c9ae"), "bay-f": ("#b98560", "#7a4d31"),
    "chest-s": ("#5e2a1b", "#f0c2ad"), "chest-f": ("#c9917a", "#8a3d29"),
    "grey-s": ("#3f4652", "#eef0f3"), "grey-f": ("#d3d7de", "#6b7383"),
    "black-s": ("#111318", "#c9ced8"), "black-f": ("#474d58", "#07080b"),
    "roan-s": ("#4b3a3c", "#ecd6d3"), "roan-f": ("#b9a2a0", "#6c5452"),
    "dun-s": ("#4d4130", "#eee2c4"), "dun-f": ("#cdbb92", "#6b5b3a"),
    "cc": ("#0f766e", "#2dd4bf"), "cc-soft": ("#e3f3f1", "#10332f"),
    "cx": ("#1d4ed8", "#7aa7ff"), "cx-soft": ("#e3ebff", "#172447"),
    "ds": ("#6d28d9", "#b9a3ff"), "ds-soft": ("#efe9fe", "#2a1957"),
    "he": ("#a21caf", "#f09bf6"), "he-soft": ("#fbe8ff", "#431645"),
    "oat": ("#b8a46a", "#d9c78f"),
}
INK = ("#1c1f24", "#e7e9ee")
HARNESS_TEAL = ("#0f766e", "#2dd4bf")
DRIVER = ("#c2410c", "#fb923c")

# ---------------------------------------------------------------- geometry (logo units, 160 x 80)
HORSE_BODY = ("M92 32c10 3 20 3 28-2 4-8 8-14 14-21l0-6 4 5c4 4 10 12 15 19 2 3-1 6-4 5-4-1-8-3-12-6"
              "-3 6-6 13-9 20-2 5-6 7-12 7h-16c-6 0-11-2-13-6-3-6-2-12 5-15z")
HORSE_TAIL = "M88 35c-8 2-11 13-9 25"
LEGS = {
    "ls": "M90 49l-2 13 1 10 3 4M97 52l0 11 1 9 3 4M120 52l0 12 0 8 3 4M126 50l1 12 0 10 3 4",
    "logo": "M90 49l-4 13 1 10 3 4M97 52l-2 11 3 9 3 4M120 52l1 12-1 8 3 4M126 50l3 12v10l3 4",
    "wa": "M90 49l-2 13 1 10 3 4M97 52l-3 11 2 9 4 3M120 52l0 12 0 8 3 4M126 50l4 11 1 7 4 2",
    "wb": "M90 49l1 13 0 10 3 4M97 52l0 11 1 9 3 4M120 52l3 11 0 8 3 3M126 50l0 12 0 10 3 4",
    "ta": "M90 49l-4 13 1 10 3 4M97 52l-2 11 3 9 3 4M120 52l1 12-1 8 3 4M126 50l3 12v10l3 4",
    "tb": "M90 49l1 13 0 10 3 4M97 52l-6 10-2 9 2 4M120 52l-3 12-1 8 3 4M126 50l6 10 3 6 4 1",
    "ga": "M90 49l-8 10-7 7-3 1M97 52l-6 10-5 7-2 1M120 52l9 8 8 4 3-1M126 50l10 6 8 2 2-2",
    "gb": "M90 49l4 12 4 8 3 2M97 52l5 10 3 8 3 1M120 52l-5 11-3 8 2 2M126 50l-4 12-2 8 3 2",
}
HARNESS_PATH = "M124 25c-6 7-5 17 1 23M104 33v20M89 40c3 5 7 7 13 7M135 9l11 20"
SHAFT = "M62 54l62-10"
TRACES = "M126 44H204"
BOX = "M6 54V22c0-7 6-11 14-11h30c8 0 14 4 14 11v32z"
WINDOW = "M42 18h14a2 2 0 0 1 2 2v18a2 2 0 0 1-2 2H42a2 2 0 0 1-2-2V20a2 2 0 0 1 2-2z"
DRIVER_FILL = ("M44.5 40c0-4.5 2.5-7 6.5-7s6.5 2.5 6.5 7zM47 24.5v-4.2c0-1.2.8-2 2-2h4c1.2 0 2 .8 2 2v4.2z"
               "M47.9 29.3c.4 4.5 1.7 7.2 3.1 7.2s2.7-2.7 3.1-7.2c-.6 1.6-1.6 2.6-3.1 2.6s-2.5-1-3.1-2.6z")
DRIVER_LINES = "M43.5 24.6h15M54 36.5l5 .8"
REINS = "M59 37.3c30 6 64 0 87-8"


def spokes(cx):
    return "M{0} 46.5v29M{1} 53.75l25.2 14.5M{1} 68.25l25.2-14.5".format(cx, round(cx - 12.6, 1))


# ---------------------------------------------------------------- logo
def logo_body(cls):
    """The logo's shapes; colours come from the classes cls[...] (ink, box, harness, driver)."""
    return (
        '<g class="{ink}" fill="none" stroke="currentColor" stroke-width="2.2" stroke-linecap="round" stroke-linejoin="round">'
        '<path d="{body}"/><path d="{tail}{legs}"/>'
        '<circle cx="20" cy="61" r="14.5" stroke-width="2"/><circle cx="52" cy="61" r="14.5" stroke-width="2"/>'
        '<path d="{sp}" stroke-width="1.1"/></g>'
        '<circle class="{ink}" cx="141" cy="15" r="1.2" fill="currentColor"/>'
        '<path class="{box}" fill-rule="evenodd" stroke-width="1.6" stroke-linejoin="round" d="{boxd}{win}"/>'
        '<g class="{drv}"><path fill="currentColor" d="{df}"/>'
        '<g fill="none" stroke="currentColor" stroke-linecap="round"><path stroke-width="1.6" d="{dl}"/>'
        '<circle cx="51" cy="28.6" r="3.2" stroke-width="1.2"/><path stroke-width="1.2" d="{reins}"/></g></g>'
        '<path class="{har}" fill="none" stroke="currentColor" stroke-width="2.2" stroke-linecap="round" d="{h}{shaft}"/>'
    ).format(ink=cls["ink"], box=cls["box"], drv=cls["drv"], har=cls["har"], body=HORSE_BODY, tail=HORSE_TAIL,
             legs=LEGS["logo"], sp=spokes(20) + spokes(52), boxd=BOX, win=WINDOW, df=DRIVER_FILL,
             dl=DRIVER_LINES, reins=REINS, h=HARNESS_PATH, shaft=SHAFT)


def logo_file(light, dark):
    """A standalone SVG; light/dark: dicts of colours (ink, box-f, box-s, har, drv)."""
    def rules(c):
        return (".i{color:%(ink)s}.b{fill:%(box-f)s;stroke:%(box-s)s}.h{color:%(har)s}.d{color:%(drv)s}" % c)
    style = rules(light) + ("@media (prefers-color-scheme:dark){%s}" % rules(dark) if dark else "")
    return ('<svg xmlns="http://www.w3.org/2000/svg" viewBox="0 0 160 80" role="img" aria-label="ameesh">'
            '<style>%s</style>%s</svg>\n' % (style, logo_body({"ink": "i", "box": "b", "drv": "d", "har": "h"})))


def favicon_file():
    """Simplified for 16 px: the dark buggy box, its two wheels, the shaft and a horse's head."""
    light = {"ink": INK[0], "box-f": COLOURS["buggy-f"][0], "box-s": COLOURS["buggy-s"][0],
             "har": HARNESS_TEAL[0], "drv": DRIVER[0]}
    dark = {"ink": INK[1], "box-f": COLOURS["buggy-f"][1], "box-s": COLOURS["buggy-s"][1],
            "har": HARNESS_TEAL[1], "drv": DRIVER[1]}

    def rules(c):
        return ".i{stroke:%(ink)s}.f{fill:%(ink)s}.b{fill:%(box-f)s;stroke:%(box-s)s}.h{stroke:%(har)s}.d{fill:%(drv)s}" % c
    return ('<svg xmlns="http://www.w3.org/2000/svg" viewBox="0 0 32 32">'
            '<style>%s@media (prefers-color-scheme:dark){%s}</style>'
            '<path class="b" stroke-width="1" fill-rule="evenodd" d="M1.5 21V8.5C1.5 5.5 3.5 4 6.5 4h7c3 0 5 1.5 5 4.5V21z'
            'M12 7.5h3.5v6.5H12z"/>'
            '<path class="d" d="M12.6 9.3h2.3v1h-2.3zM13.75 10.6a1 1 0 1 0 .01 0zM12.5 14h2.5l-.5-1.6h-1.5z"/>'
            '<g fill="none" stroke-linecap="round" stroke-linejoin="round">'
            '<circle class="i" cx="6" cy="25.5" r="4.6" stroke-width="1.6"/>'
            '<circle class="i" cx="15" cy="25.5" r="4.6" stroke-width="1.6"/>'
            '<path class="h" stroke-width="1.8" d="M18 21l6-3"/></g>'
            '<path class="f" d="M21.5 23.5l1.2-8.5c.6-3.5 2.4-6.5 4.6-9.2l.2-2.3 1.4 2.1c1.4 2.2 2.4 4.8 2.7 7.2'
            '.1.9-.6 1.4-1.5 1.1l-2.2-1-1.6 4.1-.6 6.5z"/>'
            '<path class="h" fill="none" stroke-width="1.3" stroke-linecap="round" d="M23.4 14.5c-.9 2.2-.8 4.4.2 6.3"/>'
            '</svg>\n' % (rules(light), rules(dark)))


def header_logo():
    """Inline logo for the landing page header (colours from the page's CSS variables)."""
    return ('<svg class="logo" viewBox="0 0 160 80" aria-hidden="true">%s</svg>'
            % logo_body({"ink": "lo-i", "box": "lo-b", "drv": "lo-d", "har": "lo-h"}))


# ---------------------------------------------------------------- the journey: timeline
D = 30.0                                     # one loop, seconds
SPEED = {"walk": 22, "trot": 60, "gallop": 110}
GAITS = ("walk", "trot", "gallop")
# segments: S0 hitching; A, B, C on the road (drawn gait); R1, R2 relays (walk)
SEG = {"S0": (0, 8), "A": (8, 14.5), "R1": (14.5, 18.5), "B": (18.5, 21), "R2": (21, 25), "C": (25, D)}
NEST = ["A", "R1", "B", "R2", "C"]          # the scroll is the sum of the segments
WR = 29                                     # wheel radius, scene units
SLOT_W, SLOT_L = 304, 464                   # bottom centre of each horse, scene x
X1, T1 = SLOT_W - 40, 16.3                  # relay 1 (wheel pair): door position and time
X2, T2 = SLOT_L - 40, 22.8                  # relay 2 (lead pair)


def pct(t):
    v = t / D * 100
    return ("%.2f" % v).rstrip("0").rstrip(".") + "%"


def kf(name, frames):
    out = ["@keyframes %s {" % name]
    for ts, css in frames:
        ts = ts if isinstance(ts, (list, tuple)) else [ts]
        out.append("  %s { %s }" % (", ".join(pct(t) for t in ts), css))
    return "\n".join(out + ["}"])


def dist(seg, gait="walk"):
    a, b = SEG[seg]
    return SPEED[gait] * (b - a)


def window(name, a, b, fade=0.6):
    return kf(name, [([0, a], "opacity: 0;"), (a + fade, "opacity: 1;"), (b, "opacity: 1;"),
                     ([b + fade, D], "opacity: 0;")])


def draw_kf(name, t0, t1, hide=None):
    fr = [([0, t0], "stroke-dashoffset: 1; opacity: 0;"),
          (t0 + .05, "stroke-dashoffset: 1; opacity: 1;"), (t1, "stroke-dashoffset: 0; opacity: 1;")]
    if hide:
        fr += [(hide[0], "stroke-dashoffset: 0; opacity: 1;"), ([hide[1], D], "stroke-dashoffset: 0; opacity: 0;")]
    else:
        fr.append((D, "stroke-dashoffset: 0; opacity: 1;"))
    return kf(name, fr)


def door_dx(x_door, t_door, t, slot):
    return round((x_door - SPEED["walk"] * (t - t_door) - slot) / 2, 1)


def leave(t0, t1, xd, td, slot):
    return [(t0, "opacity: .65; transform: none;"),
            (t1 - .4, "opacity: .5; transform: translateX(%spx) scale(.32);" % door_dx(xd, td, t1 - .4, slot)),
            ([t1, D], "opacity: 0; transform: translateX(%spx) scale(.25);" % door_dx(xd, td, t1, slot))]


def come(t0, t1, xd, td, slot):
    return [([0, t0], "opacity: 0; transform: translateX(%spx) scale(.25);" % door_dx(xd, td, t0, slot)),
            (t0 + .4, "opacity: 1; transform: translateX(%spx) scale(.32);" % door_dx(xd, td, t0 + .4, slot)),
            ([t1, D], "opacity: 1; transform: none;")]


# labels: (key, row, x, slot or effort segment, t_in, t_out, in the still image)
#   rows: m = model (above the heads), h = harness (below them), e = effort (above the buggy)
LABELS = [("mW1", "m", 340, "W1", .8, 4.4, True), ("hW1", "h", 270, "W1", 3.2, 6.6, True),
          ("mL1", "m", 556, "L1", 10.0, 13.0, True), ("hL1", "h", 460, "L1", 10.6, 13.6, True),
          ("mW2", "m", 340, "W2", 17.8, 21.0, False), ("hW2", "h", 270, "W2", 18.4, 21.4, False),
          ("mL2", "m", 556, "L2", 24.0, 27.4, False), ("hL2", "h", 460, "L2", 24.6, 28.0, False),
          ("e0", "e", 120, "A", 8.2, 10.4, False), ("e1", "e", 120, "low", 14.5, 16.6, False),
          ("e2", "e", 120, "B", 18.5, 20.6, False), ("e3", "e", 120, "low", 21.0, 23.0, False),
          ("e4", "e", 120, "C", 25.0, 27.2, False)]
CAPTIONS = [(0, 2.4), (2.4, 4.8), (4.8, 8.0), (8.0, 9.5), (9.5, 14.5), (14.5, 29.0)]


def timeline():
    """(keyframes, animated rules, static rules)"""
    css, rules, static = [], [], []
    for s in NEST:                                       # scroll and wheels, per segment and gait
        a, b = SEG[s]
        for g in (GAITS if s in DEFAULT_GAITS else ("walk",)):
            d, key = dist(s, g), "%s-%s" % (s, g)
            css.append(kf("mv" + key, [([0, a], "transform: translateX(0);"), ([b, D], "transform: translateX(-%gpx);" % d)]))
            css.append(kf("sp" + key, [([0, a], "transform: rotate(0deg);"),
                                       ([b, D], "transform: rotate(%ddeg);" % round(d / WR * 57.2958))]))
            sel = ".%s-%s " % (s, g) if s in DEFAULT_GAITS else ""
            rules.append("%s.mv%s { animation-name: mv%s; }" % (sel, s, key))
            rules.append("%s.sp%s { animation-name: sp%s; }" % (sel, s, key))
    for s in ("A", "B"):                                 # relay 2 sits after A and B, whatever their gaits
        for g in GAITS:
            static.append(".%s-%s .off%s { transform: translateX(%gpx); }" % (s, g, s, dist(s, g)))
    css.append(kf("winS0", [([0, 8], "opacity: 1;"), ([8.05, D], "opacity: 0;")]))
    for s in ("A", "B", "C"):                            # legs: one gait shown per segment
        a, b = SEG[s]
        css.append(kf("win" + s, [([0, a], "opacity: 0;"), ([a + .05, b], "opacity: 1;"), ([b + .05, D], "opacity: 0;")]
                      if b < D else [([0, a], "opacity: 0;"), ([a + .05, D], "opacity: 1;")]))
        rules.append(", ".join(".%s-%s .lg-%s.%s" % (s, g, s, g) for g in GAITS) + " { animation-name: win%s; }" % s)
    css.append(kf("winR", [([0, 14.5], "opacity: 0;"), ([14.55, 18.5], "opacity: 1;"), ([18.55, 21], "opacity: 0;"),
                           ([21.05, 25], "opacity: 1;"), ([25.05, D], "opacity: 0;")]))
    rules += [".lg-S0 { animation-name: winS0; }", ".lg-R { animation-name: winR; }", ".nb { animation-name: winR; }",
              ".relay { animation-name: relayIn; }"]
    # relay stations come into sight once on the road (and stay out of the still image)
    css.append(kf("relayIn", [([0, 8.0], "opacity: 0;"), ([9.5, D], "opacity: 1;")]))
    # glances: the lead horse looks back, the wheel horse looks forward, ~1.6 s, soft
    look = {"W": "translate(.9px, -.45px)", "L": "translate(-1.1px, .1px)"}
    for slot in ("W1", "L1", "W2", "L2"):
        segs = [s for s, team in TEAMS.items() if slot in team]
        for n in range(1 << len(segs)):
            on = [seg for i, seg in enumerate(segs) if n >> i & 1]
            fr = [(0, "transform: none;")]
            for seg in on:
                t = GLANCE[seg]
                fr += [(t, "transform: none;"), (t + .6, "transform: %s;" % look[slot[0]]),
                       (t + 1.4, "transform: %s;" % look[slot[0]]), (t + 2.0, "transform: none;")]
            fr.append((D, "transform: none;"))
            name = "eye%s-%s" % (slot, "".join(on) or "0")
            css.append(kf(name, fr))
            sel = "".join(".g" + seg for seg in on)
            neg = "".join(":not(.g%s)" % seg for seg in segs if seg not in on)
            rules.append("%s%s .pu-%s { animation-name: %s; }" % (sel, neg, slot, name))
    for tired in ("g", "n"):                             # a galloping team tires sooner
        pale = 11.0 if tired == "g" else 13.4
        css.append(kf("hzW1-" + tired, [(0, "opacity: 0; transform: translateX(24px);"),
                                        ([1.8, pale], "opacity: 1; transform: none;"),
                                        (14.5, "opacity: .65; transform: none;")] + leave(15.6, 17.0, X1, T1, SLOT_W)))
        pale = 18.6 if tired == "g" else 20.2
        css.append(kf("hzL1-" + tired, [([0, 9.5], "opacity: 0; transform: translateX(60px);"),
                                        ([11.0, pale], "opacity: 1; transform: none;"),
                                        (21.0, "opacity: .65; transform: none;")] + leave(21.6, 23.0, X2, T2, SLOT_L)))
    css.append(kf("hzW2", come(17.0, 18.0, X1, T1, SLOT_W)))
    css.append(kf("hzL2", come(23.0, 24.0, X2, T2, SLOT_L)))
    rules += [".hz-W1 { animation-name: hzW1-n; }", ".A-gallop .hz-W1 { animation-name: hzW1-g; }",
              ".hz-L1 { animation-name: hzL1-n; }", ".B-gallop .hz-L1 { animation-name: hzL1-g; }",
              ".hz-L2 { animation-name: hzL2; }", ".hz-W2 { animation-name: hzW2; }"]
    css += [draw_kf("drawW1", 2.4, 4.6), draw_kf("trL1", 11.0, 11.6, (21.0, 21.6)), draw_kf("trL2", 24.0, 24.6),
            draw_kf("drawC", 6.6, 7.9),
            kf("cart", [([0, 4.8], "opacity: 0; transform: translateX(-50px);"), ([6.8, D], "opacity: 1; transform: none;")]),
            kf("driver", [([0, 6.8], "opacity: 0;"), ([7.6, D], "opacity: 1;")]),
            kf("scene", [([0, 29.0], "opacity: 1;"), (D, "opacity: 0;")])]
    rules += [".draw-W1 { animation-name: drawW1; }", ".tr-L1 { animation-name: trL1; }",
              ".tr-L2 { animation-name: trL2; }", ".draw-c { animation-name: drawC; }",
              ".cart { animation-name: cart; }", ".driver { animation-name: driver; }",
              ".scene { animation-name: scene; }"]
    for key, row, x, ref, a, b, st in LABELS:
        css.append(window("t" + key, a, b))
        rules.append(".t-%s { animation-name: t%s; }" % (key, key))
    # a "low" label only when the gait really changes on arriving at a relay
    static.append(".A-walk .t-e1, .B-walk .t-e3 { visibility: hidden; }")
    for i, (a, b) in enumerate(CAPTIONS):
        c = "warm" if i == 2 else "accent"
        on = "border-color: var(--%s); background: var(--%s-soft);" % (c, c)
        off = "border-color: var(--border); background: var(--card);"
        fr = ([(0, on)] if a == 0 else [([0, a], off), (a + .15, on)]) + [(b, on), ([b + .15, D], off)]
        css.append(kf("cap%d" % (i + 1), fr))
        rules.append("ol.steps li:nth-child(%d) { animation-name: cap%d; }" % (i + 1, i + 1))
    return css, rules, static


# ---------------------------------------------------------------- the journey: markup
def pair_info(slot):
    h, m = DEFAULT_PAIRS[slot]
    return m, COAT[m], HARNESS[h][0], h


def legs_markup():
    out = ['<use href="#rd-ls" class="lg lg-S0 a"/>', '<use href="#rd-gw" class="lg lg-R a"/>']
    for s in ("A", "B", "C"):
        for g, sym in (("walk", "gw"), ("trot", "gt"), ("gallop", "gg")):
            out.append('<use href="#rd-%s" class="lg lg-%s %s a"/>' % (sym, s, g))
    return "".join(out)


def family_of(model):
    return next(f for f, models in FAMILY.items() if model in models)


OATS_FULL = "".join('<ellipse cx="%s" cy="%s" rx="1.4" ry=".8" transform="rotate(-20 %s %s)"/>' % (x, y, x, y)
                    for x, y in ((148.6, 33.6), (150.8, 33.4), (153, 32.4), (149.8, 31.6), (152.1, 30.8), (154.2, 30.2)))
ACCESSORIES = (
    # riding helmet over the poll, peak forward, chin strap down the cheek
    '<g class="acc acc-claude"><path class="acc-fill" d="M130.6 10c-.6-5.5 3-9.2 7.8-9 4.2.2 6.3 3.6 5.2 8.2z"/>'
    '<path class="acc-line" d="M143.4 8.9l3.9 1M132.4 10.4l4.6 8.6"/></g>'
    # a sign on the chest, hung from the neck
    '<g class="acc acc-gpt"><path class="acc-line" d="M133 21l-6 13M135 22l13 12"/>'
    '<rect class="acc-fill" x="125" y="33.5" width="26" height="12" rx="1.5"/>'
    '<text class="sign-v" x="138" y="39.6" data-sign="@@SLOT@@">@@NEXT@@</text>'
    '<text class="sign-s" x="138" y="43.9">coming soon</text></g>'
    # nosebag, shown at the relays and on the still image
    '<g class="nb a"><path class="acc-line" d="M145 24.5l-9-16"/>'
    '<path class="acc-fill" d="M145 24.5l8.5-3 3.6 9.6c-1.6 5-7.4 6.6-10.2 3z"/>'
    '<g class="oat"><g class="oats-full">' + OATS_FULL + '</g>'
    '<ellipse class="oats-one" cx="151.4" cy="33.2" rx="1.4" ry=".8" transform="rotate(-20 151.4 33.2)"/></g></g>'
)


def horse(slot, x, extra=""):
    model, coat, har, h = pair_info(slot)
    acc = ACCESSORIES.replace("@@SLOT@@", slot).replace("@@NEXT@@", NEXT_VERSION.get(model, "GPT-7"))
    return ('<g transform="translate(%d 0)"><g class="hz hz-%s a f-%s" data-fam="%s">'
            '<g class="k-%s" data-coat="%s"><use href="#rd-hb"/>%s'
            '<ellipse class="eye-w" cx="141" cy="15" rx="2.3" ry="1.7"/>'
            '<circle class="pu pu-%s a" cx="141" cy="15" r="1.15"/>%s</g>'
            '<g class="h-%s" data-har="%s"><path class="s-ln s-h%s" pathLength="1" d="%s"/></g>'
            '</g></g>' % (x, slot, family_of(model), slot, coat, slot, legs_markup(), slot, acc,
                          h, slot, extra, HARNESS_PATH))


def label(key, row, x, ref, still):
    y = {"m": -18, "h": 30, "e": -18}[row]
    later = "" if still else " later"
    if row == "e":
        txt = "effort: " + ("low" if ref == "low" else EFFORT[DEFAULT_GAITS[ref]])
        data = ' data-eff="%s"' % ref if ref in DEFAULT_GAITS else ""
        return ('<g transform="translate(%d %d)"><g class="tg t-eff t-%s a%s"%s>'
                '<rect x="-58" width="116" height="20" rx="10"/><text y="14">%s</text></g></g>'
                % (x, y, key, later, data, txt))
    model, coat, har, h = pair_info(ref)
    if row == "m":
        w = round(len(model) * 6.7 + 32)
        return ('<g transform="translate(%d %d)"><g class="tg t-coat t-%s k-%s a%s" data-coat="%s" data-model="%s">'
                '<rect x="%d" width="%d" height="20" rx="10"/><circle class="sw" cx="%d" cy="10" r="4"/>'
                '<text x="6" y="14">%s</text></g></g>'
                % (x, y, key, coat, later, ref, ref, -w // 2, w, -w // 2 + 11, model))
    w = round(len(har) * 6.7 + 20)
    return ('<g transform="translate(%d %d)"><g class="tg t-har t-%s h-%s a%s" data-har="%s" data-harness="%s">'
            '<rect x="%d" width="%d" height="20" rx="10"/><text y="14">%s</text></g></g>'
            % (x, y, key, h, later, ref, ref, -w // 2, w, har))


def nest(inner, prefix="mv"):
    for s in reversed(NEST):
        inner = '<g class="%s%s a lin">%s</g>' % (prefix, s, inner)
    return inner


def scene():
    base1 = X1 - 60 + SPEED["walk"] * (T1 - SEG["R1"][0])
    base2 = X2 - 60 + SPEED["walk"] * (T2 - SEG["R2"][0]) + dist("R1")
    trees = "".join('<use href="#rd-tree" transform="translate(%d %g) scale(%s)"/>' % (x, 192 - 60 * sc, sc)
                    for x, sc in ((40, 1), (470, .85), (760, 1), (1000, .85), (1380, 1), (1640, .85),
                                  (1900, 1), (2150, .85), (2420, 1)))
    relay = ('<g transform="translate(%g 0)"><g class="offA">%s<g class="relay a">'
             '<path class="s-ln s-relay" d="M0 192V122L60 92 120 122V192M-10 128 60 84l70 44M44 192v-36a16 16 0 0 1 32 0v36"/>'
             '<text class="s-relay-t" x="60" y="132">RELAY</text></g>%s</g></g>')
    near = (trees
            + '<path class="s-ln s-post s-butt" d="M0 185H2700" stroke-width="22" stroke-dasharray="3 57" opacity=".7"/>'
            '<path class="s-ln s-post" d="M0 180H2700" stroke-width="2"/>'
            + relay % (base1, "", "") + relay % (base2, '<g class="offB">', "</g>")
            + '<path class="s-ln s-post s-butt" d="M0 203H2700" stroke-width="1.5" stroke-dasharray="10 50"/>')
    far = ('<g transform="scale(.2 1)">%s</g>' % nest(
        '<g transform="scale(5 1)"><path class="s-ln s-far" d="M0 150c60-40 120-40 180-10s110 20 170-12 130-30 200 0 '
        '120 34 190 4 120-30 190 0 110 20 180-8 100-20 160 4 120 30 180 0 130-26 190-4 110 22 170 0 120-24 190 0"/></g>'))

    def wheel(cx):
        sp = '<path class="s-ln s-fg" stroke-width=".9" d="%s"/>' % spokes(cx)
        return '<circle class="s-ln s-fg" cx="%d" cy="61" r="14.5" stroke-width="1.6"/>%s' % (cx, nest(sp, "sp"))

    team = ('<g transform="translate(70 40) scale(2)">'
            + horse("W2", 0) + horse("L2", 80)
            + '<g class="h-%s" data-har="L2"><path class="s-ln s-h draw tr-L2 a later" pathLength="1" d="%s"/></g>'
            % (DEFAULT_PAIRS["L2"][0], TRACES)
            + horse("L1", 80)
            + '<g class="h-%s" data-har="L1"><path class="s-ln s-h draw tr-L1 a" pathLength="1" d="%s"/></g>'
            % (DEFAULT_PAIRS["L1"][0], TRACES)
            + horse("W1", 0, " draw draw-W1 a")
            + '<g class="cart a"><path class="s-ln s-fg draw draw-c a" pathLength="1" d="%s"/>' % SHAFT
            + '<path class="box" d="%s"/><rect class="win" x="40" y="18" width="18" height="22" rx="2"/>' % BOX
            + wheel(20) + wheel(52) + '</g>'
            + '<g class="driver a"><path class="drv-cloth" d="M44.5 40c0-4.5 2.5-7 6.5-7s6.5 2.5 6.5 7z"/>'
              '<path class="drv-cloth" d="M47 24.5v-4.2c0-1.2.8-2 2-2h4c1.2 0 2 .8 2 2v4.2z"/>'
              '<path class="drv-line" d="M43.5 24.6h15"/><circle class="drv-face" cx="51" cy="28.6" r="3.2"/>'
              '<path class="drv-beard" d="M47.9 29.3c.4 4.5 1.7 7.2 3.1 7.2s2.7-2.7 3.1-7.2c-.6 1.6-1.6 2.6-3.1 2.6s-2.5-1-3.1-2.6z"/>'
              '<path class="drv-line" d="M54 36.5l5 .8"/></g>'
            + '<path class="s-ln s-d draw draw-c a" stroke-width="1.2" pathLength="1" d="%s"/>' % REINS
            + '</g>')
    # content reused through <use> lives in a shadow tree: unscoped "rd-" classes only
    defs = "".join('<path id="rd-%s" class="rd-line" d="%s"/>' % (k, v) for k, v in LEGS.items() if k != "logo")
    defs += ('<g id="rd-hb"><path class="rd-body" d="%s"/><path class="rd-line" d="%s"/>'
             '</g>' % (HORSE_BODY, HORSE_TAIL))
    for sym, a, b, c in (("gw", "wa", "wb", "w"), ("gt", "ta", "tb", "t"), ("gg", "ga", "gb", "g")):
        defs += ('<g id="rd-%s"><use href="#rd-%s" class="rd-pa-%s"/><use href="#rd-%s" class="rd-pb-%s"/></g>'
                 % (sym, a, c, b, c))
    defs += '<path id="rd-tree" class="rd-tree" d="M20 60V34M20 4c10 0 16 9 16 17s-7 14-16 14S4 29 4 21 10 4 20 4z"/>'
    return ('<defs>%s</defs><g class="scene a"><g class="far">%s</g><g class="near">%s</g>'
            '<path class="s-ln s-post" d="M0 192H720" stroke-width="2"/>%s<g>%s</g></g>'
            % (defs, far, nest(near), team, "".join(label(k, r, x, ref, st) for k, r, x, ref, a, b, st in LABELS)))


# ---------------------------------------------------------------- the journey: CSS, section, script
def colour_vars(i):
    return " ".join("--%s: %s;" % (k, v[i]) for k, v in COLOURS.items())


STYLE = """
/* journey: colours of the buggy, coats (one per model) and harnesses (one hue each) */
:root { @@LIGHT@@ }
@media (prefers-color-scheme: dark) { :root { @@DARK@@ } }
/* header logo */
.lo-i { color: var(--fg); } .lo-b { fill: var(--buggy-f); stroke: var(--buggy-s); }
.lo-h { color: var(--accent); } .lo-d { color: var(--warm); }
/* the journey */
.ride { margin: 0; background: var(--card); border: 1px solid var(--border); border-radius: var(--radius); padding: 8px; box-shadow: var(--shadow); }
.ride > svg { display: block; width: 100%; height: auto; }
.ride .s-ln { fill: none; stroke-linecap: round; stroke-linejoin: round; }
.ride .s-butt { stroke-linecap: butt; }
.ride .s-fg { stroke: currentColor; stroke-width: 2.2; }
.ride .s-h { stroke: currentColor; stroke-width: 2.4; }
.ride .s-d { stroke: var(--warm); stroke-width: 2.2; }
.ride .s-far, .ride .s-post { stroke: var(--border); stroke-width: 2; }
.ride .s-relay { stroke: var(--muted); stroke-width: 2; }
.ride .s-relay-t { fill: var(--muted); font: 700 13px var(--font); letter-spacing: .12em; text-anchor: middle; }
.rd-line, .rd-tree { fill: none; stroke: currentColor; stroke-width: 2.2; stroke-linecap: round; stroke-linejoin: round; }
.rd-tree { stroke: var(--border); stroke-width: 2; }
.rd-body { stroke: currentColor; stroke-width: 2.2; stroke-linejoin: round; }
.rd-eye { fill: currentColor; stroke: none; }
.rd-pb-w, .rd-pb-t, .rd-pb-g { opacity: 0; }
.ride .cart { color: var(--fg); }
.ride .box { fill: var(--buggy-f); stroke: var(--buggy-s); stroke-width: 1.6; stroke-linejoin: round; }
.ride .win { fill: var(--card); stroke: var(--buggy-s); stroke-width: 1; }
.ride .drv-cloth { fill: var(--buggy-f); stroke: var(--warm); stroke-width: 1.2; stroke-linejoin: round; }
.ride .drv-line { fill: none; stroke: var(--warm); stroke-width: 1.6; stroke-linecap: round; }
.ride .drv-face { fill: var(--card); stroke: var(--warm); stroke-width: 1.2; }
.ride .drv-beard { fill: var(--warm); }
@@COATS@@
/* the horses' winks: helmet (Claude), sign (GPT), one oat (DeepSeek), glances */
.ride .eye-w { fill: var(--card); stroke: currentColor; stroke-width: .6; }
.ride .pu { fill: currentColor; stroke: none; }
.ride .acc, .ride .oats-one { display: none; }
.ride .f-claude .acc-claude, .ride .f-gpt .acc-gpt, .ride .f-deepseek .oats-one { display: inline; }
.ride .f-deepseek .oats-full { display: none; }
.ride .acc-fill { fill: var(--card); stroke: currentColor; stroke-width: 1.1; stroke-linejoin: round; }
.ride .acc-line { fill: none; stroke: currentColor; stroke-width: 1.1; stroke-linecap: round; }
.ride .oat { fill: var(--oat); stroke: none; }
.ride .sign-v { font: 700 6px var(--font); text-anchor: middle; fill: currentColor; }
.ride .sign-s { font: 600 3.7px var(--font); text-anchor: middle; fill: currentColor; }
.ride .acc-gpt { transform-box: fill-box; transform-origin: 50% 0; }
@media (max-width: 600px) { .ride .acc-gpt { transform: scale(1.5); } }
/* still image (no animation): the first two pairs, standing, one label each */
.ride .lg:not(.lg-S0), .ride .hz-L2, .ride .hz-W2, .ride .later, .ride .relay { opacity: 0; }
.ride .draw { stroke-dasharray: 1 1; }
.ride .spA, .ride .spR1, .ride .spB, .ride .spR2, .ride .spC { transform-box: fill-box; transform-origin: center; }
.ride .hz { transform-box: fill-box; transform-origin: 50% 100%; }
.ride .tg { transform-box: fill-box; transform-origin: 50% 0; }
.ride .tg text { font: 600 11px var(--font); text-anchor: middle; fill: currentColor; }
.ride .tg rect { stroke: currentColor; stroke-width: 1.2; }
.ride .t-coat rect { fill: var(--card); }
.ride .t-coat .sw { stroke: currentColor; stroke-width: 1; }
.ride .t-har rect { fill: var(--hs); }
.ride .t-eff { color: var(--fg); }
.ride .t-eff rect { fill: var(--card); stroke: var(--muted); stroke-dasharray: 3 2; }
@media (max-width: 600px) { .ride .tg { transform: scale(1.9); } }
@@STATIC@@
.ride ol.steps { list-style: none; counter-reset: st; margin: 12px 0 0; padding: 0; display: grid; gap: 8px; grid-template-columns: 1fr; }
@media (min-width: 760px) { .ride ol.steps { grid-template-columns: repeat(3, 1fr); } }
.ride ol.steps li { counter-increment: st; border: 1px solid var(--border); border-radius: 10px; padding: 8px 10px 8px 42px; position: relative; font-size: .92rem; color: var(--muted); background: var(--card); }
.ride ol.steps li::before { content: counter(st); position: absolute; left: 10px; top: 9px; width: 22px; height: 22px; border-radius: 50%; background: var(--accent-soft); color: var(--accent); font-weight: 700; font-size: .8rem; display: grid; place-items: center; }
.ride ol.steps b { color: var(--fg); }
@media (prefers-reduced-motion: no-preference) {
  .ride.go .a, .ride.go ol.steps li { animation-duration: @@DUR@@; animation-iteration-count: infinite; animation-timing-function: ease-in-out; animation-fill-mode: both; }
  .ride.go .lin { animation-timing-function: linear; }
  .rd-pa-w, .rd-pb-w { animation: rd-flip 1s steps(1, end) infinite; }
  .rd-pa-t, .rd-pb-t { animation: rd-flip .5s steps(1, end) infinite; }
  .rd-pa-g, .rd-pb-g { animation: rd-flip .32s steps(1, end) infinite; }
  .rd-pb-w, .rd-pb-t, .rd-pb-g { animation-name: rd-flop; }
@@RULES@@
}
@keyframes rd-flip { 0% { opacity: 1; } 50% { opacity: 0; } }
@keyframes rd-flop { 0% { opacity: 0; } 50% { opacity: 1; } }
/* one loop of @@DUR@@ */
@@KEYFRAMES@@
"""

SECTION = """
<section id="journey" aria-labelledby="journey-h">
  <div class="wrap">
    <h2 id="journey-h">The journey</h2>
    <p class="section-lead">Models are the horses, harnesses tie them to the work, ameesh is the carriage, and a human holds the reins. Horses can be changed at a relay without stopping the journey.</p>
    <figure class="ride go @@CLASSES@@">
      <svg viewBox="0 -20 720 240" role="img" aria-labelledby="journey-t journey-d">
        <title id="journey-t">ameesh as a horse-drawn buggy</title>
        <desc id="journey-d">A horse appears with its model name, then its harness with the harness name, then the carriage, a closed dark buggy with tall thin wheels, whose driver holds the reins. The buggy sets off; its gait shows the effort, walk, trot or gallop, the whole team together. A second harnessed horse is hitched in front. At two relay stations a tired horse is unhitched with its harness and walks into the stable, and a fresh horse comes out of the door with its own harness and is hitched, without the buggy stopping.</desc>
        @@SCENE@@
      </svg>
      <ol class="steps">
        <li><b>Models</b> are the horses: the pulling power, interchangeable.</li>
        <li><b>Harnesses</b> tie a model to the work, as a pair: Claude Code with a Claude model, Codex with a GPT model, dsh with a DeepSeek model; Hermes with any model.</li>
        <li><b>ameesh</b> is the carriage: it carries the work and the team. A human holds the reins and approves.</li>
        <li><b>On the road</b>, only the gait changes: it is the effort, walk low, trot medium, gallop high, for the whole team at once.</li>
        <li><b>More pulling power</b>: another harnessed horse is hitched in front.</li>
        <li><b>At each relay</b>, accounts and models switch: a tired horse is unhitched with its harness and a fresh pair is hitched on the spot. The journey continues.</li>
      </ol>
    </figure>
    <script>
    // Draws new pairs (model + harness) and gaits at each loop; without it the fixed sequence plays.
    (function () {
      // journey:draw:begin
      var ROAD = @@ROAD@@;
      function roadLook(model) {   // what a horse wears follows its model's family, whatever the harness
        var fam = Object.keys(ROAD.family).filter(function (f) { return ROAD.family[f].indexOf(model) >= 0; })[0];
        return { fam: fam, helmet: fam === 'claude', sign: fam === 'gpt' ? ROAD.next[model] : null,
                 oats: fam === 'deepseek' ? 1 : 'full' };
      }
      function roadDraw(rand, last) {
        function pick(list) { return list[Math.floor(rand() * list.length)]; }
        function pair() {
          var h = pick(Object.keys(ROAD.harness)), fam = ROAD.harness[h][1];
          return { h: h, m: pick(fam ? ROAD.family[fam] : ROAD.models) };
        }
        function apart(list) {   // pairs seen together share neither model nor harness
          for (var i = 0; i < list.length; i++)
            for (var j = i + 1; j < list.length; j++)
              if (list[i].h === list[j].h || list[i].m === list[j].m) return false;
          return true;
        }
        for (var n = 0; n < 1000; n++) {
          var p = { W1: pair(), L1: pair(), W2: pair(), L2: pair() };
          var start = p.W1.h + ':' + p.W1.m + '|' + p.L1.h + ':' + p.L1.m;
          if (start === last) continue;                                   // a new start each loop
          if (!apart([p.W1, p.L1, p.W2]) || !apart([p.W2, p.L1, p.L2])) continue;
          if (!['W1', 'L1', 'W2', 'L2'].some(function (s) { return p[s].h === 'he'; })) continue;
          var gaits = {};
          ['A', 'B', 'C'].forEach(function (s) { gaits[s] = pick(Object.keys(ROAD.effort)); });
          var glance = {};   // a Claude and a GPT horse pulling together glance at each other
          Object.keys(ROAD.teams).forEach(function (seg) {
            var f = ROAD.teams[seg].map(function (slot) { return roadLook(p[slot].m).fam; }).sort().join();
            glance[seg] = f === 'claude,gpt';
          });
          return { pairs: p, gaits: gaits, start: start, glance: glance };
        }
        return null;
      }
      // journey:draw:end
      var fig = document.querySelector('figure.ride'), last = '';
      if (!fig || !fig.classList || !document.querySelectorAll) return;
      function swap(el, prefix, value) {
        [].slice.call(el.classList).forEach(function (c) { if (c.indexOf(prefix) === 0) el.classList.remove(c); });
        el.classList.add(prefix + value);
      }
      function pill(g, text, pad) {
        var w = Math.round(text.length * 6.7 + pad), r = g.querySelector('rect'), s = g.querySelector('.sw');
        g.querySelector('text').textContent = text;
        r.setAttribute('x', -w / 2); r.setAttribute('width', w);
        if (s) s.setAttribute('cx', -w / 2 + 11);
      }
      function each(sel, fn) { [].forEach.call(fig.querySelectorAll(sel), fn); }
      function loop() {
        var d = roadDraw(Math.random, last);
        if (!d) return;
        last = d.start;
        Object.keys(d.pairs).forEach(function (slot) {
          var p = d.pairs[slot];
          each('[data-coat="' + slot + '"]', function (el) { swap(el, 'k-', ROAD.coat[p.m]); });
          each('[data-har="' + slot + '"]', function (el) { swap(el, 'h-', p.h); });
          var look = roadLook(p.m);
          each('[data-fam="' + slot + '"]', function (el) { swap(el, 'f-', look.fam); });
          if (look.sign) fig.querySelector('[data-sign="' + slot + '"]').textContent = look.sign;
          pill(fig.querySelector('[data-model="' + slot + '"]'), p.m, 32);
          pill(fig.querySelector('[data-harness="' + slot + '"]'), ROAD.harness[p.h][0], 20);
        });
        Object.keys(d.gaits).forEach(function (s) {
          ['walk', 'trot', 'gallop'].forEach(function (g) { fig.classList.remove(s + '-' + g); });
          fig.classList.add(s + '-' + d.gaits[s]);
          fig.querySelector('[data-eff="' + s + '"] text').textContent = 'effort: ' + ROAD.effort[d.gaits[s]];
        });
        Object.keys(d.glance).forEach(function (seg) { fig.classList.toggle('g' + seg, d.glance[seg]); });
        fig.classList.remove('go');      // restart every animation together
        void fig.getBoundingClientRect();
        fig.classList.add('go');
      }
      fig.addEventListener('animationiteration', function (e) { if (e.animationName === 'scene') loop(); });
      loop();
    })();
    </script>
  </div>
</section>
"""


def road_json():
    return json.dumps({"family": FAMILY, "models": [m for f in FAMILY.values() for m in f], "coat": COAT,
                       "harness": HARNESS, "effort": EFFORT,
                       "next": NEXT_VERSION, "teams": TEAMS}, separators=(",", ":"))


# classes carried by the figure itself: gaits (".A-trot") and glances (".gA", ":not(.gB)")
GAIT_CLASS = re.compile(r"^(\.[A-C]-(walk|trot|gallop)|(\.g[A-C]|:not\(\.g[A-C]\))+) ")


def scope(rule, prefix):
    """Scopes a rule to the figure; gait classes (".A-trot …") sit on the figure itself."""
    sels, body = rule.split(" {", 1)
    out = []
    for sel in sels.split(", "):
        out.append(prefix + sel if GAIT_CLASS.match(sel) else prefix + " " + sel)
    return ", ".join(out) + " {" + body


def journey_css():
    css, rules, static = timeline()
    coats = "\n".join(".ride .k-%s { color: var(--%s-s); fill: var(--%s-f); }" % (c, c, c) for c in sorted(set(COAT.values())))
    coats += "\n" + "\n".join(".ride .h-%s { color: var(--%s); --hs: var(--%s-soft); }" % (h, h, h) for h in HARNESS)
    return (STYLE.replace("@@LIGHT@@", colour_vars(0)).replace("@@DARK@@", colour_vars(1))
            .replace("@@COATS@@", coats).replace("@@STATIC@@", "\n".join(scope(r, ".ride") for r in static))
            .replace("@@RULES@@", "\n".join("  " + scope(r, ".ride.go") for r in rules))
            .replace("@@KEYFRAMES@@", "\n".join(css)).replace("@@DUR@@", "%gs" % D).strip("\n"))


def journey_section():
    fams = {slot: family_of(m) for slot, (h, m) in DEFAULT_PAIRS.items()}
    glances = ["g" + seg for seg, team in TEAMS.items() if sorted(fams[t] for t in team) == ["claude", "gpt"]]
    classes = ["%s-%s" % kv for kv in DEFAULT_GAITS.items()] + glances
    return (SECTION.replace("@@CLASSES@@", " ".join(classes))
            .replace("@@SCENE@@", scene()).replace("@@ROAD@@", road_json()).strip("\n"))


# ---------------------------------------------------------------- outputs
def replace_between(text, begin, end, body):
    pat = re.compile(re.escape(begin) + r".*?" + re.escape(end), re.S)
    if not pat.search(text):
        raise SystemExit("markers %s … %s not found" % (begin, end))
    return pat.sub(lambda _: begin + body + end, text, count=1)


def outputs():
    light = {"ink": INK[0], "box-f": COLOURS["buggy-f"][0], "box-s": COLOURS["buggy-s"][0],
             "har": HARNESS_TEAL[0], "drv": DRIVER[0]}
    dark = {"ink": INK[1], "box-f": COLOURS["buggy-f"][1], "box-s": COLOURS["buggy-s"][1],
            "har": HARNESS_TEAL[1], "drv": DRIVER[1]}
    # MkDocs header: always on the teal primary colour, light lines
    header = {"ink": "#ffffff", "box-f": "#15171c", "box-s": "#ffffff", "har": "#b2f5ea", "drv": "#fed7aa"}
    fav = favicon_file()
    index_path = LANDING / "index.html"
    index = index_path.read_text()
    index = replace_between(index, "<!-- logo:begin -->", "<!-- logo:end -->", header_logo())
    index = replace_between(index, "/* journey:css:begin (generated by scripts/site_art.py) */",
                            "/* journey:css:end */", "\n" + journey_css() + "\n")
    index = replace_between(index, "<!-- journey:begin (generated by scripts/site_art.py) -->",
                            "<!-- journey:end -->", "\n" + journey_section() + "\n")
    return {LANDING / "logo.svg": logo_file(light, dark), LANDING / "favicon.svg": fav,
            DOCS_ASSETS / "logo.svg": logo_file(header, None), DOCS_ASSETS / "favicon.svg": fav,
            index_path: index}


def main(argv):
    check = "--check" in argv
    stale = []
    for path, content in outputs().items():
        current = path.read_text() if path.exists() else None
        if current != content:
            stale.append(path.relative_to(ROOT))
            if not check:
                path.write_text(content)
    if check and stale:
        print("stale (run python3 scripts/site_art.py): " + ", ".join(map(str, stale)))
        return 1
    print(("up to date" if check else "written: " + (", ".join(map(str, stale)) or "nothing changed")))
    return 0


if __name__ == "__main__":
    sys.exit(main(sys.argv[1:]))
