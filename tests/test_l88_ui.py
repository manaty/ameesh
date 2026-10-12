# SPDX-License-Identifier: AGPL-3.0-only
"""L88: front-end base. What Python checks without Node: the standalone page
served by `ameesh serve` (fixed files, strict CSP, no token, guards kept),
the committed page without inline code, the components' contract on their
sources (no raw HTML, no storage, no navigation), the theme's contrasts and
the packaging. The build itself (reproducible, budget) and the components
(axe-core) are checked by .github/workflows/ui.yml.
"""
from __future__ import annotations

import json
import os
import re
import tempfile
import unittest
from pathlib import Path

from ameesh import db as db_mod
from ameesh import serve

from .test_l84_serve import ServerMixin

ROOT = Path(__file__).resolve().parent.parent
STATIC = ROOT / "src" / "ameesh" / "ui" / "static"
UI_SRC = ROOT / "ui" / "src"


class StandalonePageTest(ServerMixin, unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.mkdtemp(prefix="l88-")
        cfg = type("Cfg", (), {"approve_url": None,
                               "threads_dir": os.path.join(self.tmp, "fils"),
                               "threads_root": os.path.join(self.tmp, "fils")})()
        self.reads = []

        def read(fn):
            self.reads.append(fn)
            raise db_mod.Unavailable("no database in this test")
        self.start(serve.Context(cfg=cfg, human="human:owner"), read, rate_per_minute=1000)

    def test_page_and_files_without_token_under_a_strict_csp(self):
        for path, ctype, name in (("/ui/", "text/html", "index.html"),
                                  ("/ui/ameesh-ui.js", "text/javascript", "ameesh-ui.js"),
                                  ("/ui/ameesh-ui.css", "text/css", "ameesh-ui.css"),
                                  ("/ui/page.css", "text/css", "page.css")):
            status, headers, body = self.get(path, auth=False)
            self.assertEqual(status, 200, path)
            self.assertTrue(headers["Content-Type"].startswith(ctype), path)
            self.assertEqual(body, (STATIC / name).read_bytes(), path)
            csp = headers["Content-Security-Policy"]
            self.assertEqual(csp, serve.UI_CSP)
            for directive in ("default-src 'none'", "script-src 'self'", "style-src 'self'",
                              "connect-src 'self'", "frame-ancestors 'none'",
                              "require-trusted-types-for 'script'"):
                self.assertIn(directive, csp)
            self.assertNotIn("unsafe", csp)
            self.assertEqual(headers["X-Content-Type-Options"], "nosniff")
        self.assertEqual(self.reads, [])  # never the database
        # the API keeps its own, closed policy
        status, headers, _ = self.get("/.well-known/ameesh", auth=False)
        self.assertEqual(headers["Content-Security-Policy"], serve.CSP)

    def test_only_the_listed_files(self):
        status, headers, _ = self.get("/ui", auth=False)
        self.assertEqual((status, headers["Location"]), (308, "/ui/"))
        for path in ("/ui/index.html.bak", "/ui/../serve.py", "/ui/%2e%2e/serve.py",
                     "/ui/static/ameesh-ui.js", "/ui//ameesh-ui.js", "/ui/x/ameesh-ui.js"):
            status, _, body = self.get(path, auth=False)
            self.assertIn(status, (400, 404), path)
            self.assertNotIn(b"import", body)
        status, _, _ = self.get("/ui/", method="HEAD", auth=False)
        self.assertEqual(status, 200)

    def test_guards_still_apply(self):
        status, _, _ = self.get("/ui/", auth=False, host="evil.example:%d" % self.port)
        self.assertEqual(status, 403)
        status, _, _ = self.get("/ui/", method="POST", auth=False)
        self.assertEqual(status, 405)


class CommittedPageTest(unittest.TestCase):
    def test_page_has_no_inline_code_and_its_files_exist(self):
        page = (STATIC / "index.html").read_text(encoding="utf-8")
        self.assertNotRegex(page, r"<style|\sstyle=|\son[a-z]+=|javascript:")
        for script in re.findall(r"<script\b[^>]*>(.*?)</script>", page, re.S):
            self.assertEqual(script.strip(), "")
        refs = re.findall(r'(?:src|href)="([^"]+)"', page)
        self.assertEqual(sorted(refs), ["ameesh-ui.css", "ameesh-ui.js", "page.css"])
        served = {name for name, _ in serve.UI_FILES.values()}
        self.assertEqual(served, {"index.html", *refs})
        for name in served:
            self.assertTrue((STATIC / name).is_file(), name)
        self.assertRegex(page, r'<html lang="[a-z]{2}"')

    def test_packaged(self):
        text = (ROOT / "pyproject.toml").read_text(encoding="utf-8")
        self.assertIn('"ui/static/*"', text)


class ComponentContractTest(unittest.TestCase):
    """Study §2.2 and §2.5, on the sources: the components never write raw
    HTML, never evaluate code, never keep a token nor navigate the host."""

    FORBIDDEN = {
        r"\.innerHTML\b|\.outerHTML\b|insertAdjacentHTML|unsafeHTML|unsafeSVG|DOMParser":
            "raw HTML",
        r"\beval\(|new Function\(|setTimeout\(\s*['\"]": "evaluated code",
        r"localStorage|sessionStorage|indexedDB|document\.cookie": "storage",
        r"window\.open|location\.(?:href|assign|replace)\s*[=(]|history\.": "navigation",
    }

    def sources(self):
        return [p for p in UI_SRC.rglob("*.ts") if p.name != "schema.d.ts"]

    def test_no_forbidden_call_in_the_sources(self):
        self.assertTrue(self.sources())
        for path in self.sources():
            text = path.read_text(encoding="utf-8")
            for pattern, what in self.FORBIDDEN.items():
                self.assertNotRegex(text, pattern, "%s: %s" % (path.name, what))

    def test_every_source_carries_the_licence(self):
        for path in [*self.sources(), *(ROOT / "ui" / "scripts").glob("*.mjs")]:
            self.assertTrue(path.read_text(encoding="utf-8").startswith(
                "// SPDX-License-Identifier: AGPL-3.0-only"), path.name)

    def test_catalogues_have_the_same_keys(self):
        en = json.loads((UI_SRC / "i18n" / "en.json").read_text(encoding="utf-8"))
        fr = json.loads((UI_SRC / "i18n" / "fr.json").read_text(encoding="utf-8"))
        self.assertEqual(sorted(en), sorted(fr))
        for key, value in en.items():
            self.assertEqual(type(value), type(fr[key]), key)
            self.assertEqual(sorted(re.findall(r"\{(\w+)\}", json.dumps(value))),
                             sorted(re.findall(r"\{(\w+)\}", json.dumps(fr[key]))), key)


def _luminance(hex_color):
    channels = [int(hex_color[i:i + 2], 16) / 255 for i in (1, 3, 5)]
    channels = [c / 12.92 if c <= 0.03928 else ((c + 0.055) / 1.055) ** 2.4 for c in channels]
    return 0.2126 * channels[0] + 0.7152 * channels[1] + 0.0722 * channels[2]


def contrast(a, b):
    high, low = sorted((_luminance(a), _luminance(b)), reverse=True)
    return (high + 0.05) / (low + 0.05)


class ThemeTest(unittest.TestCase):
    """WCAG 2.2 AA in both themes (study §5): text colours 4.5:1 on the page
    and on surfaces, the focus ring 3:1."""

    def test_contrasts_in_both_themes(self):
        colors = json.loads((UI_SRC / "tokens.json").read_text(encoding="utf-8"))["colors"]
        for name, values in colors.items():
            self.assertEqual(len(values), 2, name)
        for theme in (0, 1):
            for background in ("page", "surface"):
                bg = colors[background][theme]
                for name in ("ink", "ink-2", "muted", "critical"):
                    self.assertGreaterEqual(contrast(colors[name][theme], bg), 4.5,
                                            (name, background, theme))
                self.assertGreaterEqual(contrast(colors["focus"][theme], bg), 3.0,
                                        ("focus", background, theme))

    def test_published_tokens_are_those_of_the_table(self):
        colors = json.loads((UI_SRC / "tokens.json").read_text(encoding="utf-8"))["colors"]
        sheet = (STATIC / "ameesh-ui.css").read_text(encoding="utf-8")
        for name, (light, dark) in colors.items():
            self.assertIn("--ameesh-color-%s:%s;" % (name, light), sheet)
            self.assertIn("--ameesh-color-%s:%s;" % (name, dark), sheet)


if __name__ == "__main__":
    unittest.main()
