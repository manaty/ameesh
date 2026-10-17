# SPDX-License-Identifier: AGPL-3.0-only
"""#1124 : les explorations restent hors des plans et des lots.

Lecteur sans base, exécuté en CI sur les PR de documentation aussi bien que
sur les changements de code. Les fixtures vérifient les refus ; le test du
dépôt applique la garde aux documents versionnés et nouveaux non ignorés.
Le canon externe et les lots Postgres ne sont pas accessibles à cette garde.
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
        errors.append("en-tête type/status d’exploration absent ou incorrect")
    folded = normalized(text)
    # Dates interdites aussi dans les tableaux ou la prose, pas seulement YAML.
    if DATE.search(folded):
        errors.append("date dans une exploration")
    # ATX et Setext : une liste de lots ne peut pas être cachée en sous-section.
    lines = folded.splitlines()
    headings = [re.sub(r"[*_`]", "", line.lstrip("# ").strip())
                for i, line in enumerate(lines)
                if line.lstrip().startswith("#") or
                (i + 1 < len(lines) and re.fullmatch(r"\s*(?:=+|-+)\s*", lines[i + 1]))]
    if any(FORBIDDEN_SECTION.search(line) for line in headings) or FORBIDDEN_KEY.search(folded):
        errors.append("section ou champ de planning dans une exploration")
    return errors


def is_planning(path: pathlib.PurePosixPath, text: str) -> bool:
    tokens = set(re.split(r"[^a-z]+", normalized(path.as_posix())))
    return bool(tokens & PLANNING_NAMES) or field(text, "type").replace(" ", "") in PLANNING_TYPES


def document_errors(path: pathlib.PurePosixPath, text: str) -> list[str]:
    if EXPLORATIONS in path.parents:
        return exploration_errors(path, text)
    if not is_planning(path, text):
        return []
    # Liens Markdown, champs YAML/JSON, URLs, chemins Windows ou encodés.
    # Décodage répété borné pour les références doublement encodées.
    decoded = html.unescape(text)
    for _ in range(3):
        decoded = unquote(decoded)
    decoded = decoded.replace("\\", "/").casefold()
    if re.search(r"(?:^|[/\s\"'(<:])explorations(?:/|[\"')>#?]|$)", decoded, re.MULTILINE):
        return ["plan/roadmap/lot référence explorations/ : référencer une étude décidée"]
    return []


class RepositoryGuardTest(unittest.TestCase):
    def test_documents_du_depot(self):
        files = subprocess.check_output(
            ["git", "ls-files", "-z", "--cached", "--others", "--exclude-standard"], cwd=REPO,
        ).decode().split("\0")
        errors = []
        self.assertTrue((REPO / EXPLORATIONS / "index.md").is_file(), "index du sas absent")
        for name in sorted(set(files) - {""}):
            path = pathlib.PurePosixPath(name)
            if path.suffix not in DOCUMENTS or path.parts[0] in {"tests", "src", ".github"}:
                continue  # fixtures et code ne sont pas des fiches de planning
            local = REPO / name
            if not local.is_file():
                continue  # suppression locale avant commit
            errors.extend(f"{name}: {message}" for message in
                          document_errors(path, local.read_text(encoding="utf-8")))
        self.assertEqual(errors, [], "\n".join(errors))


class GuardWitnessTest(unittest.TestCase):
    def exploration(self, body):
        return document_errors(EXPLORATIONS / "idee.md",
                               "---\ntype: Exploration\nstatus: exploration\n---\n" + body)

    def test_refuse_planning_dans_exploration(self):
        for body in ("## Lots proposés\n- première tranche", "Proposed lots\n-------------\n",
                     "### **Lots proposés**\n", "## Date cible\nÀ préciser",
                     "target_date: demain", "| cible | 2027-01-02 |",
                     "Livraison le 2 janvier 2027", "date: 02/01/2027",
                     "## Estimation\n", "## Critère fait\n", "## Done criteria\n"):
            with self.subTest(body=body):
                self.assertTrue(self.exploration(body))

    def test_accepte_questions_et_contexte(self):
        self.assertEqual(self.exploration(
            "## Problème\nPourquoi ?\n## Intuitions\nUne piste.\n"
            "## Questions ouvertes\n[Étude](../etudes/piste.md)\n"), [])

    def test_refuse_entete_incorrect(self):
        self.assertTrue(document_errors(EXPLORATIONS / "idee.md", "# Une idée\n"))
        self.assertTrue(document_errors(EXPLORATIONS / "idee.md",
                                       "---\ntype: Study\nstatus: exploration\n---\n"))

    def test_refuse_references_depuis_planning(self):
        cases = [
            ("site/docs/roadmap.md", "[idée](../../docs/design/explorations/idee.md)"),
            ("plan/ameesh/epic.yaml", "source: docs/design/explorations/idee.md"),
            ("docs/autre.md", '---\ntype: WorkPackage\n---\n[Idée](explorations/idee.md)'),
            ("cards/travail.yml", 'type: Lot\nsource: https://example.org/explorations/idee.md'),
            ("cards/compact.json", '{"type":"WorkPackage","source":"explorations/idee.md"}'),
            ("cards/travail.json", '{\n "type": "WorkItem",\n "source": "explorations/idee.md"\n}'),
            ("docs/l148-plan.md", 'source: docs%2Fdesign%2Fexplorations%2Fidee.md'),
            ("roadmap.md", 'source: docs%252Fdesign%252Fexplorations%252Fidee.md'),
            ("lots/idee.md", r'source: docs\design\explorations\idee.md'),
            ("roadmap.md", "[source](explorations)"),
        ]
        for name, text in cases:
            with self.subTest(name=name, text=text):
                self.assertTrue(document_errors(pathlib.PurePosixPath(name), text))

    def test_lien_de_navigation_et_etude_decidee(self):
        self.assertEqual(document_errors(pathlib.PurePosixPath("docs/design/index.md"),
                                         "[Explorations](explorations/index.md)"), [])
        self.assertEqual(document_errors(pathlib.PurePosixPath("plan/travail.md"),
                                         "[Étude](../docs/design/etudes/idee.md)"), [])


if __name__ == "__main__":
    unittest.main()
