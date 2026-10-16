# SPDX-License-Identifier: AGPL-3.0-only
"""#1124: explorations stay out of plans and lots.

Database-free reader, run in CI on documentation PRs as well as on code
changes. The fixtures check the refusals; the repository test applies the
guard to versioned and new non-ignored documents. The external canon and the
Postgres lots are not reachable from this guard.

The guard is heuristic. A document counts as a plan/roadmap/lot when a token
of its path belongs to `PLANNING_NAMES`, or when its `type` field belongs to
`PLANNING_TYPES`; a reference to `explorations/` is then searched in the
decoded text (HTML entities, three bounded `unquote` passes, backslashes
normalized). Known false positives: a planning document that merely mentions
`explorations/` in prose is refused. Known false negatives: a synonym absent
from both lists (`feuille-de-route`, `chantier`…) is not seen as planning, and
a reference obfuscated beyond the three decoding passes is not detected. Both
lists are deliberately finite: extend them rather than rely on a full
Markdown parser.
"""
from __future__ import annotations

import html
import json
import pathlib
import re
import subprocess
import unicodedata
import unittest
from urllib.parse import unquote

REPO = pathlib.Path(__file__).resolve().parent.parent
EXPLORATIONS = pathlib.PurePosixPath("docs/design/explorations")
DOCUMENTS = {".md", ".yaml", ".yml", ".json", ".txt"}
PLANNING_TYPES = {"roadmap", "plan", "workpackage", "workitem", "lot", "task",
                  "epic", "milestone"}
PLANNING_NAMES = {"roadmap", "plan", "plans", "lot", "lots", "workpackage",
                  "workpackages", "workitem", "workitems", "backlog"}
DATE = re.compile(r"\b\d{4}-\d{2}-\d{2}\b|\b\d{1,2}[/.-]\d{1,2}[/.-]\d{4}\b|"
                  r"\b\d{1,2}\s+(?:janvier|fevrier|mars|avril|mai|juin|juillet|aout|"
                  r"septembre|octobre|novembre|decembre|january|february|march|april|"
                  r"may|june|july|august|september|october|november|december)\s+\d{4}\b")
FORBIDDEN_SECTION = re.compile(
    r"(?:lots?|work\s*packages?|tasks?|epics?)\s+(?:proposes?|proposed)|"
    r"proposed\s+(?:lots?|work\s*packages?|tasks?|epics?)|"
    r"dates?\s+(?:cibles?|de\s+livraison)|target\s+dates?|deadlines?|"
    r"estimations?|estimates?|criteres?.*(?:fait|termine)|(?:done|completion)\s+criteria"
)
FORBIDDEN_KEY = re.compile(
    r"^(?:proposed[_ -]?(?:lots?|tasks?)|lots?[_ -]?proposes?|target[_ -]?date|"
    r"date[_ -]?cible|deadline|due[_ -]?date|estimate|estimation|done[_ -]?criteria)\s*:",
    re.MULTILINE,
)


def normalized(text: str) -> str:
    text = unicodedata.normalize("NFKD", text.casefold())
    return "".join(c for c in text if not unicodedata.combining(c))


def header(text: str) -> str:
    match = re.match(r"\A---\s*\n(.*?)\n---\s*(?:\n|$)", text, re.DOTALL)
    return match.group(1) if match else text if not text.lstrip().startswith("#") else ""


def field(text: str, key: str) -> str:
    # YAML front matter / YAML cards, and JSON cards (no YAML dependency).
    if text.lstrip().startswith("{"):
        try:
            value = json.loads(text).get(key, "")
            return normalized(value) if isinstance(value, str) else ""
        except (ValueError, AttributeError):
            pass
    match = re.search(r'(?m)^\s*[\"\']?' + key + r'[\"\']?\s*:\s*[\"\']?([^\"\'\n,}]+)',
                      header(text))
    return normalized(match.group(1).strip()) if match else ""


def exploration_errors(path: pathlib.PurePosixPath, text: str) -> list[str]:
    errors = []
    expected_type = "index" if path.name == "index.md" else "exploration"
    if field(text, "type") != expected_type or field(text, "status") != "exploration":
        errors.append("missing or incorrect exploration type/status header")
    folded = normalized(text)
    # Forbidden dates apply to tables and prose too, not only to YAML.
    if DATE.search(folded):
        errors.append("date in an exploration")
    # ATX and Setext: a list of lots cannot hide in a sub-section.
    lines = folded.splitlines()
    headings = [re.sub(r"[*_`]", "", line.lstrip("# ").strip())
                for i, line in enumerate(lines)
                if line.lstrip().startswith("#") or
                (i + 1 < len(lines) and re.fullmatch(r"\s*(?:=+|-+)\s*", lines[i + 1]))]
    if any(FORBIDDEN_SECTION.search(line) for line in headings) or FORBIDDEN_KEY.search(folded):
        errors.append("planning section or field in an exploration")
    return errors


def is_planning(path: pathlib.PurePosixPath, text: str) -> bool:
    tokens = set(re.split(r"[^a-z]+", normalized(path.as_posix())))
    return bool(tokens & PLANNING_NAMES) or field(text, "type").replace(" ", "") in PLANNING_TYPES


def document_errors(path: pathlib.PurePosixPath, text: str) -> list[str]:
    if EXPLORATIONS in path.parents:
        return exploration_errors(path, text)
    if not is_planning(path, text):
        return []
    # Markdown links, YAML/JSON fields, URLs, Windows or encoded paths.
    # Bounded repeated decoding for doubly encoded references.
    decoded = html.unescape(text)
    for _ in range(3):
        decoded = unquote(decoded)
    decoded = decoded.replace("\\", "/").casefold()
    if re.search(r"(?:^|[/\s\"'(<:])explorations(?:/|[\"')>#?]|$)", decoded, re.MULTILINE):
        return ["plan/roadmap/lot references explorations/: reference a decided study"]
    return []


class RepositoryGuardTest(unittest.TestCase):
    def test_repository_documents(self):
        files = subprocess.check_output(
            ["git", "ls-files", "-z", "--cached", "--others", "--exclude-standard"], cwd=REPO,
        ).decode().split("\0")
        errors = []
        self.assertTrue((REPO / EXPLORATIONS / "index.md").is_file(), "exploration index missing")
        for name in sorted(set(files) - {""}):
            path = pathlib.PurePosixPath(name)
            if path.suffix not in DOCUMENTS or path.parts[0] in {"tests", "src", ".github"}:
                continue  # fixtures and code are not planning cards
            local = REPO / name
            if not local.is_file():
                continue  # local deletion before commit
            errors.extend(f"{name}: {message}" for message in
                          document_errors(path, local.read_text(encoding="utf-8")))
        self.assertEqual(errors, [], "\n".join(errors))


class GuardWitnessTest(unittest.TestCase):
    def exploration(self, body):
        return document_errors(EXPLORATIONS / "idea.md",
                               "---\ntype: Exploration\nstatus: exploration\n---\n" + body)

    def test_rejects_planning_in_exploration(self):
        for body in ("## Lots proposés\n- première tranche", "Proposed lots\n-------------\n",
                     "### **Lots proposés**\n", "## Date cible\nÀ préciser",
                     "target_date: demain", "| cible | 2027-01-02 |",
                     "Livraison le 2 janvier 2027", "date: 02/01/2027",
                     "## Estimation\n", "## Critère fait\n", "## Done criteria\n"):
            with self.subTest(body=body):
                self.assertTrue(self.exploration(body))

    def test_accepts_questions_and_context(self):
        self.assertEqual(self.exploration(
            "## Problem\nWhy?\n## Intuitions\nA lead.\n"
            "## Open questions\n[Study](../etudes/lead.md)\n"), [])

    def test_rejects_wrong_header(self):
        self.assertTrue(document_errors(EXPLORATIONS / "idea.md", "# An idea\n"))
        self.assertTrue(document_errors(EXPLORATIONS / "idea.md",
                                       "---\ntype: Study\nstatus: exploration\n---\n"))

    def test_rejects_references_from_planning(self):
        cases = [
            ("site/docs/roadmap.md", "[idea](../../docs/design/explorations/idea.md)"),
            ("plan/ameesh/epic.yaml", "source: docs/design/explorations/idea.md"),
            ("docs/other.md", '---\ntype: WorkPackage\n---\n[Idea](explorations/idea.md)'),
            ("cards/work.yml", 'type: Lot\nsource: https://example.org/explorations/idea.md'),
            ("cards/compact.json", '{"type":"WorkPackage","source":"explorations/idea.md"}'),
            ("cards/work.json", '{\n "type": "WorkItem",\n "source": "explorations/idea.md"\n}'),
            ("docs/l148-plan.md", 'source: docs%2Fdesign%2Fexplorations%2Fidea.md'),
            ("roadmap.md", 'source: docs%252Fdesign%252Fexplorations%252Fidea.md'),
            ("lots/idea.md", r'source: docs\design\explorations\idea.md'),
            ("roadmap.md", "[source](explorations)"),
        ]
        for name, text in cases:
            with self.subTest(name=name, text=text):
                self.assertTrue(document_errors(pathlib.PurePosixPath(name), text))

    def test_navigation_link_and_decided_study(self):
        self.assertEqual(document_errors(pathlib.PurePosixPath("docs/design/index.md"),
                                         "[Explorations](explorations/index.md)"), [])
        self.assertEqual(document_errors(pathlib.PurePosixPath("plan/work.md"),
                                         "[Study](../docs/design/etudes/idea.md)"), [])


if __name__ == "__main__":
    unittest.main()
