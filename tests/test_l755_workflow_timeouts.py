# SPDX-License-Identifier: AGPL-3.0-only
"""Chaque job de CI porte un `timeout-minutes`, sous le plafond (#755).

Règle du bureau (manaty/home, `processus/lots.md` §3, 13/10/2026) : « Chaque job
de CI porte une durée maximale (`timeout-minutes`). Un job bloqué ne doit jamais
retenir un runner pendant des heures. » Le plafond retenu ici est 60 minutes :
la règle interdit des heures, pas une valeur précise, et le maximum en place à
l'ouverture du lot est 40 (`carte.yml`, job `mesure`).

#1410 : GitHub refuse `timeout-minutes` sur un job qui appelle un workflow
réutilisable (`uses:`) — la clé n'existe que pour un job à étapes. Ces jobs
portent donc leur borne dans le workflow appelé : celui-ci doit être local et
chacun de ses jobs doit déclarer son `timeout-minutes`, ce que ce module
vérifie (`caller_jobs`). Le même relevé strict sert aux deux lectures : un job
n'est jamais à la fois absent du relevé des timeouts et présent dans celui des
appels.

PyYAML n'est pas une dépendance du projet : le venv de la CI n'installe que
psycopg, cryptography et psutil, et les tests qui lisent du YAML se sautent
quand il manque (`tests/test_cluster.py`). Ce module lit donc le sous-ensemble
des workflows à la main : les deux extensions `.yml` et `.yaml` (GitHub Actions
les traite pareil), la clé `jobs:` de premier niveau, un job par clé à la
première indentation rencontrée sous `jobs:`, et `timeout-minutes` du job à
deux espaces de plus — une clé d'étape, plus profonde, ne compte pas. Un job
peut aussi s'écrire en mapping compact, `b: {runs-on: self-hosted,
timeout-minutes: 10}`, lu sans PyYAML.

Le sous-ensemble reste volontairement étroit, mais rien n'est ignoré en
silence : une forme qu'il ne sait pas interpréter (ancre, alias, liste,
mapping imbriqué, `jobs:` en ligne, indentation incohérente ou par tabulation,
guillemet non apparié) lève `UnsupportedWorkflow`, et un job compact dont le
`timeout-minutes` manque est rendu `None` — donc signalé — au lieu de
disparaître du relevé. Dans un mapping compact, la virgule d'un scalaire cité
(`name: "a, b"`) ne sépare pas les paires et ne peut donc pas fabriquer un
`timeout-minutes` absent. Le parseur est testé sur des textes synthétiques, clé
d'étape et formes compactes comprises.
"""
from __future__ import annotations

import re
import tempfile
import unittest
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
WORKFLOWS = ROOT / ".github" / "workflows"

#: GitHub Actions lit les deux extensions, sans différence de contenu.
WORKFLOW_SUFFIXES = (".yml", ".yaml")

#: La règle interdit « des heures » : une heure pleine est la borne garde-fou.
MAX_TIMEOUT_MINUTES = 60

#: Un job : une clé, nom nu ou entre guillemets, suivie du reste de la ligne.
_JOB = re.compile(r"""^(?:"([^"]+)"|'([^']+)'|([A-Za-z0-9_.-]+))\s*:(.*)$""")
#: `timeout-minutes` d'un job ou d'une étape, à l'indentation du corps.
_TIMEOUT = re.compile(r"""^timeout-minutes\s*:\s*(.*)$""")
#: Un `timeout-minutes` numérique, nu ou entre guillemets appariés.
_TIMEOUT_VALUE = re.compile(r"""^(?:(\d+)|"(\d+)"|'(\d+)')$""")
#: Une paire `clé: valeur` d'un mapping compact ; la valeur reste scalaire.
_FLOW_PAIR = re.compile(r"""^([A-Za-z0-9_.-]+)\s*:\s*([^{}\[\]]*)$""")
#: `uses:` du corps d'un job : l'appel d'un workflow réutilisable (#1410).
_USES = re.compile(r"""^uses\s*:\s*(.*)$""")
#: La cible d'un `uses:` : un seul mot, nu ou entre guillemets appariés.
_USES_VALUE = re.compile(r"""^(?:([^"'\s]+)|"([^"'\s]+)"|'([^"'\s]+)')$""")
#: Un guillemet ouvre un scalaire cité en tête de paire ou après `{`, `,`, `:`.
_QUOTE_OPENERS = " \t{,:"


class UnsupportedWorkflow(ValueError):
    """Forme de workflow que le lecteur ne sait pas interpréter.

    Refusée explicitement (ligne citée) : une forme non lue ne doit jamais
    produire un job absent du relevé.
    """


def _strip_comment(text: str) -> str:
    """Retire le commentaire YAML de fin de ligne, guillemets respectés."""
    quote: str | None = None
    for index, char in enumerate(text):
        if quote is not None:
            if char == quote:
                quote = None
        elif char in "\"'":
            quote = char
        elif char == "#" and (index == 0 or text[index - 1] in " \t"):
            return text[:index]
    return text


def _timeout_value(value: str, where: str) -> int | None:
    """Valeur d'un `timeout-minutes` : vide = absent, non numérique = refus."""
    if value == "":
        return None
    match = _TIMEOUT_VALUE.match(value)
    if match is None:
        raise UnsupportedWorkflow(
            "%s : timeout-minutes non numérique ou guillemets non appariés %r"
            % (where, value))
    return int(next(group for group in match.groups() if group is not None))


def _flow_pairs(inner: str, where: str) -> list[str]:
    """Paires d'un mapping compact, coupées aux virgules hors guillemets.

    `name: "a, b"` reste une seule paire : la virgule d'un scalaire cité ne
    sépare pas. Un guillemet non apparié est refusé, jamais deviné.
    """
    pairs: list[str] = []
    start = 0
    quote: str | None = None
    index = 0
    while index < len(inner):
        char = inner[index]
        if quote is not None:
            if quote == '"' and char == "\\":
                index += 2
                continue
            if quote == "'" and char == "'" and inner[index + 1:index + 2] == "'":
                index += 2
                continue
            if char == quote:
                quote = None
        elif char in "\"'" and (index == 0 or inner[index - 1] in _QUOTE_OPENERS):
            quote = char
        elif char == ",":
            pairs.append(inner[start:index])
            start = index + 1
        index += 1
    if quote is not None:
        raise UnsupportedWorkflow(
            "%s : guillemet %s non apparié dans le mapping compact %r"
            % (where, quote, inner))
    pairs.append(inner[start:])
    return pairs


def _uses_value(value: str, where: str) -> str:
    """Cible d'un `uses:` : un seul mot (chemin ou action), sinon refus."""
    match = _USES_VALUE.match(value)
    if match is None:
        raise UnsupportedWorkflow("%s : `uses:` illisible %r" % (where, value))
    return next(group for group in match.groups() if group is not None)


def _flow_job(rest: str, where: str) -> tuple[int | None, str | None]:
    """`timeout-minutes` et `uses:` d'un job compact, `None` s'ils manquent."""
    if not rest.startswith("{") or not rest.endswith("}"):
        raise UnsupportedWorkflow(
            "%s : mapping compact illisible %r" % (where, rest))
    timeout: int | None = None
    uses: str | None = None
    inner = rest[1:-1].strip()
    if inner:
        for pair in _flow_pairs(inner, where):
            match = _FLOW_PAIR.match(pair.strip())
            if match is None:
                raise UnsupportedWorkflow(
                    "%s : paire compacte illisible %r" % (where, pair.strip()))
            key, value = match.group(1), match.group(2).strip()
            if key == "timeout-minutes":
                timeout = _timeout_value(value, where)
            elif key == "uses":
                uses = _uses_value(value, where)
    return timeout, uses


def workflow_files(directory: Path) -> list[Path]:
    """Fichiers de workflow d'un répertoire : `.yml` et `.yaml`, triés."""
    return sorted(path for path in directory.iterdir()
                  if path.is_file() and path.suffix in WORKFLOW_SUFFIXES)


def _read_jobs(text: str) -> dict[str, tuple[int | None, str | None]]:
    """Relevé des jobs : `timeout-minutes` et cible `uses:`, par job.

    Une seule marche, stricte (#1279) : `UnsupportedWorkflow` est levée dès
    qu'une forme sort du sous-ensemble lu, si bien qu'un job ne peut pas être
    absent du relevé des timeouts et présent dans celui des appels (#1410).
    Le relevé couvre la forme en bloc comme le mapping compact.
    """
    jobs: dict[str, tuple[int | None, str | None]] = {}
    current: str | None = None
    in_jobs = False
    job_indent: int | None = None
    for number, raw in enumerate(text.splitlines(), 1):
        where = "ligne %d" % number
        line = _strip_comment(raw).rstrip()
        if not line.strip():
            continue
        leading = line[:len(line) - len(line.lstrip())]
        if "\t" in leading:
            raise UnsupportedWorkflow(
                "%s : indentation par tabulation" % where)
        indent = len(leading)
        if indent == 0:
            in_jobs = False
            current = None
            job_indent = None
            key, _, rest = line.partition(":")
            if key.strip() == "jobs":
                if rest.strip():
                    raise UnsupportedWorkflow(
                        "%s : `jobs:` en ligne (%r) non supporté"
                        % (where, rest.strip()))
                in_jobs = True
            continue
        if not in_jobs:
            continue
        if job_indent is None:
            job_indent = indent
        if indent == job_indent:
            match = _JOB.match(line.strip())
            if match is None:
                raise UnsupportedWorkflow(
                    "%s : clé de job illisible %r" % (where, line.strip()))
            current = next((name for name in match.groups()[:3] if name), None)
            rest = match.group(4).strip()
            jobs[current] = _flow_job(rest, where) if rest else (None, None)
            continue
        if indent == job_indent + 2:
            if current is None:
                continue
            body = line.strip()
            timeout, uses = jobs[current]
            match = _TIMEOUT.match(body)
            if match:
                jobs[current] = (
                    _timeout_value(match.group(1).strip(), where), uses)
                continue
            match = _USES.match(body)
            if match:
                jobs[current] = (
                    timeout, _uses_value(match.group(1).strip(), where))
            continue
        if indent > job_indent + 2:
            continue  # corps d'étape : son timeout est plus profond
        raise UnsupportedWorkflow(
            "%s : indentation %d incohérente sous `jobs:`" % (where, indent))
    return jobs


def job_timeouts(text: str) -> dict[str, int | None]:
    """`timeout-minutes` de chaque job du workflow, `None` quand il manque.

    `UnsupportedWorkflow` est levée dès qu'une forme sort du sous-ensemble lu :
    un job ne doit jamais être absent du relevé sans que ce soit dit.
    """
    return {job: timeout for job, (timeout, _) in _read_jobs(text).items()}


def caller_jobs(text: str) -> dict[str, str]:
    """Les jobs qui appellent un workflow réutilisable : job → cible `uses:`.

    GitHub refuse `timeout-minutes` sur ces jobs (#1410) ; leur borne est
    portée par les jobs du workflow appelé, que `WorkflowTimeoutsTest` vérifie.
    """
    return {job: uses
            for job, (_, uses) in _read_jobs(text).items() if uses is not None}


class WorkflowTimeoutsTest(unittest.TestCase):
    def workflows(self) -> list[Path]:
        files = workflow_files(WORKFLOWS)
        self.assertTrue(files, "aucun workflow sous %s" % WORKFLOWS)
        return files

    def test_chaque_job_declare_son_timeout(self):
        for path in self.workflows():
            text = path.read_text(encoding="utf-8")
            appeles = caller_jobs(text)
            timeouts = job_timeouts(text)
            with self.subTest(workflow=path.name):
                self.assertTrue(timeouts, "aucun job lu dans %s" % path.name)
            for job, value in timeouts.items():
                if job in appeles:
                    continue  # #1410 : `uses:` refuse timeout-minutes ; voir le test voisin
                with self.subTest(workflow=path.name, job=job):
                    self.assertIsNotNone(
                        value, "%s/%s : timeout-minutes manquant" % (path.name, job))

    def test_un_job_appele_est_borne_par_les_jobs_de_l_appele(self):
        # #1410 : la borne d'un job `uses:` est celle des jobs du workflow
        # appelé ; l'appel doit rester local et chaque job appelé être borné.
        for path in self.workflows():
            for job, cible in caller_jobs(path.read_text(encoding="utf-8")).items():
                with self.subTest(workflow=path.name, job=job):
                    self.assertTrue(cible.startswith("./.github/workflows/"),
                                    "%s/%s : appel non local (%s)" % (path.name, job, cible))
                    appele = ROOT / cible[2:]
                    self.assertTrue(appele.exists(),
                                    "%s/%s : workflow appelé absent (%s)" % (path.name, job, cible))
                    timeouts = job_timeouts(appele.read_text(encoding="utf-8"))
                    self.assertTrue(timeouts, "aucun job lu dans %s" % appele.name)
                    for nom, valeur in timeouts.items():
                        self.assertIsNotNone(
                            valeur, "%s (appelé par %s/%s) : timeout-minutes manquant"
                            % (nom, path.name, job))

    def test_le_plafond_est_respecte(self):
        for path in self.workflows():
            for job, value in job_timeouts(path.read_text(encoding="utf-8")).items():
                if value is None:
                    continue  # déjà signalé par test_chaque_job_declare_son_timeout
                with self.subTest(workflow=path.name, job=job):
                    self.assertLessEqual(
                        value, MAX_TIMEOUT_MINUTES,
                        "%s/%s : %s min, plafond %s" % (path.name, job, value,
                                                        MAX_TIMEOUT_MINUTES))

    def test_le_parseur_lit_le_timeout_du_job_pas_de_l_etape(self):
        text = (
            "name: ci\n"
            "on:\n"
            "  push:\n"
            "jobs:\n"
            "  a:\n"
            "    runs-on: ubuntu-latest\n"
            "    steps:\n"
            "      - run: sleep 1\n"
            "        timeout-minutes: 9\n"
            "  b:\n"
            "    runs-on: ubuntu-latest\n"
            "    timeout-minutes: 10  # plafond\n"
            '  "c:d":\n'
            "    runs-on: ubuntu-latest\n"
            "    timeout-minutes: 5\n"
            "  d:\n"
            "    runs-on: ubuntu-latest\n"
        )
        self.assertEqual(job_timeouts(text), {"a": None, "b": 10, "c:d": 5, "d": None})

    def test_les_deux_extensions_de_workflow_sont_lues(self):
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            (root / "a.yml").write_text(
                "jobs:\n  a:\n    timeout-minutes: 1\n", encoding="utf-8")
            (root / "b.yaml").write_text(
                "jobs:\n  b:\n    timeout-minutes: 2\n", encoding="utf-8")
            (root / "notes.txt").write_text("pas un workflow\n", encoding="utf-8")
            names = [path.name for path in workflow_files(root)]
        self.assertEqual(names, ["a.yml", "b.yaml"])

    def test_un_job_compact_n_est_pas_ignore(self):
        # Fixture négative du N1 : `b` n'était ni lu ni signalé.
        text = (
            "jobs:\n"
            "  a:\n"
            "    timeout-minutes: 10\n"
            "  b: {runs-on: self-hosted}\n"
        )
        self.assertEqual(job_timeouts(text), {"a": 10, "b": None})

    def test_le_timeout_d_un_job_compact_est_lu(self):
        text = (
            "jobs:\n"
            "  a:\n"
            "    timeout-minutes: 10\n"
            "  b: {runs-on: self-hosted, timeout-minutes: 12}  # compact\n"
        )
        self.assertEqual(job_timeouts(text), {"a": 10, "b": 12})

    def test_une_virgule_citee_ne_fabrique_pas_un_timeout(self):
        # Sonde de la R1 : le `timeout-minutes` cité dans `name` est du texte.
        text = (
            "jobs:\n"
            '  b: {runs-on: self-hosted, name: "display, timeout-minutes: 10"}\n'
        )
        self.assertEqual(job_timeouts(text), {"b": None})

    def test_un_timeout_reel_gagne_sur_un_scalaire_cite(self):
        text = (
            "jobs:\n"
            '  b: {timeout-minutes: 120, name: "display, timeout-minutes: 10"}\n'
        )
        self.assertEqual(job_timeouts(text), {"b": 120})

    def test_un_timeout_entre_guillemets_apparies_est_lu(self):
        text = (
            "jobs:\n"
            '  a: {timeout-minutes: "12"}\n'
            "  b: {timeout-minutes: '8'}\n"
        )
        self.assertEqual(job_timeouts(text), {"a": 12, "b": 8})

    def test_une_apostrophe_dans_un_scalaire_nu_ne_coupe_rien(self):
        text = (
            "jobs:\n"
            "  a: {name: don't, timeout-minutes: 10}\n"
        )
        self.assertEqual(job_timeouts(text), {"a": 10})

    def test_les_guillemets_non_apparies_sont_refuses(self):
        for text in (
            'jobs:\n  a: {timeout-minutes: 10"}\n',
            "jobs:\n  a: {timeout-minutes: '10}\n",
            'jobs:\n  a: {name: "display}\n',
            'jobs:\n  a: {name: "display, timeout-minutes: 10}\n',
        ):
            with self.subTest(text=text):
                with self.assertRaises(UnsupportedWorkflow):
                    job_timeouts(text)

    def test_la_cible_d_un_appel_exige_des_guillemets_apparies(self):
        appel = "jobs:\n  a:\n    uses: %s\n"
        for cible in ('./.github/workflows/x.yml', '"./.github/workflows/x.yml"',
                      "'./.github/workflows/x.yml'"):
            with self.subTest(cible=cible):
                self.assertEqual(caller_jobs(appel % cible),
                                 {"a": "./.github/workflows/x.yml"})
        for cible in ('"./.github/workflows/x.yml', "./.github/workflows/x.yml'",
                      '"./.github/workflows/x.yml\'', "'x\""):
            with self.subTest(cible=cible):
                with self.assertRaises(UnsupportedWorkflow):
                    caller_jobs(appel % cible)

    def test_les_formes_non_supportees_sont_refusees(self):
        for text in (
            "jobs: {a: {runs-on: self-hosted}}\n",
            "jobs:\n  a: &modele\n    timeout-minutes: 10\n",
            "jobs:\n  a: {runs-on: self-hosted, steps: [{run: sleep 1}]}\n",
            "jobs:\n  a:\n    timeout-minutes: ${{ env.DUREE }}\n",
            "jobs:\n  a:\n   timeout-minutes: 10\n",
            "jobs:\n\ta:\n\t\ttimeout-minutes: 10\n",
        ):
            with self.subTest(text=text):
                with self.assertRaises(UnsupportedWorkflow):
                    job_timeouts(text)


    def test_le_parseur_reconnait_un_appel_de_workflow(self):
        text = (
            "name: ci\n"
            "on:\n"
            "  push:\n"
            "jobs:\n"
            "  a:\n"
            "    uses: ./.github/workflows/appele.yml\n"
            "  b:\n"
            "    runs-on: ubuntu-latest\n"
            "    timeout-minutes: 5\n"
            "    steps:\n"
            "      - uses: actions/checkout@v4\n"
        )
        self.assertEqual(caller_jobs(text), {"a": "./.github/workflows/appele.yml"})
        self.assertEqual(job_timeouts(text), {"a": None, "b": 5})

    def test_un_job_compact_qui_appelle_est_releve(self):
        # Composition #1279 × #1410 : le même relevé strict lit l'appel compact.
        text = "jobs:\n  a: {uses: ./.github/workflows/x.yml}\n"
        self.assertEqual(caller_jobs(text), {"a": "./.github/workflows/x.yml"})
        self.assertEqual(job_timeouts(text), {"a": None})


if __name__ == "__main__":  # pragma: no cover
    unittest.main()
