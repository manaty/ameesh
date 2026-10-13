# SPDX-License-Identifier: AGPL-3.0-only
"""Mesure de la carte test → code (L161e), côté processus mesuré.

Chargé dans le processus de la suite (`scripts/test-parts.py run --measure
DIR`) et, par un fichier `.pth` (`scripts/map.py install`), au démarrage de
chaque sous-processus Python lancé par un test (CLI, exécuteur) tant que
`CARTE_MESURE` désigne le dossier de mesure :

* couverture du code (`coverage.process_startup()`, contexte statique lu dans
  `CARTE_CONTEXTE`, posé par la suite au début de chaque test) ;
* hook d'audit : fichiers du dépôt qui ne sont pas du Python, lus
  (`open`) ou passés en argument d'un sous-processus (`psql -f
  deploy/sql/…`, qu'aucun hook Python ne voit), dossiers listés
  (`os.listdir`, `os.scandir`), et sous-processus Python lancés avec un
  environnement qui perd la mesure.

Chaque processus écrit ses relevés dans `DIR/lectures/<pid>-<hasard>.jsonl`,
une ligne par fait nouveau, aussitôt : un sous-processus tué en garde la trace.
Rien n'est jamais bloquant : une erreur de mesure est avalée.
"""
from __future__ import annotations

import json
import os
import sys

# Noms hors des préfixes AMEESH_ et AGENT_MESH_, que des tests retirent de
# l'environnement de leurs sous-processus pour les isoler du poste.
#: le dossier de mesure (vide : pas de mesure)
ENV_MEASURE_DIR = "CARTE_MESURE"
#: le test en cours (identifiant unittest), hérité par les sous-processus
ENV_MEASURE_CONTEXT = "CARTE_CONTEXTE"

REPO = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
_IGNORES = (".git", "__pycache__", "site-packages")
_CODE_SUFFIXES = (".py", ".pyc", ".pyo")
_WRITE_FLAGS = os.O_WRONLY | os.O_RDWR | os.O_APPEND | os.O_CREAT | os.O_TRUNC


def repository_path(chemin, repo: str = REPO) -> str | None:
    """Chemin relatif au dépôt d'un fichier de données, ou None (hors dépôt,
    code Python, métadonnées git, caches)."""
    if isinstance(chemin, bytes):
        chemin = os.fsdecode(chemin)
    if not isinstance(chemin, str) or not chemin:
        return None
    absolu = os.path.normpath(os.path.join(os.getcwd(), chemin))
    if absolu != repo and not absolu.startswith(repo + os.sep):
        return None
    relatif = os.path.relpath(absolu, repo)
    if relatif == ".":
        return "."
    morceaux = relatif.split(os.sep)
    if any(m in _IGNORES or m.startswith(".venv") for m in morceaux) \
            or relatif.endswith(_CODE_SUFFIXES):
        return None
    return "/".join(morceaux)


def _is_python(executable, args) -> bool:
    prog = executable or (args[0] if isinstance(args, (list, tuple)) and args else args)
    if isinstance(prog, bytes):
        prog = os.fsdecode(prog)
    if not isinstance(prog, str):
        return False
    return prog == sys.executable or os.path.basename(prog).startswith("python")


class Logbook:
    """Le journal d'un processus : un fait par ligne, sans doublon."""

    def __init__(self, dossier: str, repo: str = REPO):
        self.repo = repo
        os.makedirs(os.path.join(dossier, "lectures"), exist_ok=True)
        self.chemin = os.path.join(dossier, "lectures",
                                   "%d-%s.jsonl" % (os.getpid(), os.urandom(4).hex()))
        self._vus: set = set()
        self._dans_le_hook = False
        self._fd = None

    def record(self, cle: str, valeur: str) -> None:
        contexte = os.environ.get(ENV_MEASURE_CONTEXT, "")
        fait = (contexte, cle, valeur)
        if fait in self._vus:
            return
        self._vus.add(fait)
        if self._fd is None:
            self._fd = os.open(self.chemin, os.O_WRONLY | os.O_CREAT | os.O_APPEND, 0o644)
        ligne = json.dumps({"c": contexte, cle: valeur}, ensure_ascii=False) + "\n"
        os.write(self._fd, ligne.encode("utf-8"))

    def hook(self, evenement: str, args) -> None:
        if evenement not in ("open", "os.listdir", "os.scandir", "subprocess.Popen"):
            return
        if self._dans_le_hook:
            return
        self._dans_le_hook = True
        try:
            self._handle(evenement, args)
        except Exception:  # la mesure ne casse jamais un test
            pass
        finally:
            self._dans_le_hook = False

    def _arguments(self, argv, cwd) -> None:
        """Fichiers du dépôt nommés dans la ligne de commande (`-f F`,
        `--file=F`) : le programme lancé les lit hors de notre vue."""
        if isinstance(argv, (str, bytes)) or not argv:
            return
        base = os.fsdecode(cwd) if isinstance(cwd, (str, bytes)) else os.getcwd()
        for arg in list(argv)[1:]:
            if isinstance(arg, bytes):
                arg = os.fsdecode(arg)
            if not isinstance(arg, str) or not arg or arg.startswith("-") and "=" not in arg:
                continue
            for candidat in (arg, arg.split("=", 1)[-1]):
                chemin = os.path.join(base, candidat)
                if os.path.isfile(chemin):
                    relatif = repository_path(chemin, self.repo)
                    if relatif:
                        self.record("f", relatif)
                    break

    def _handle(self, evenement: str, args) -> None:
        if evenement == "subprocess.Popen":
            executable, argv, cwd, env = args
            self._arguments(argv, cwd)
            perdu = env is not None and not (
                "COVERAGE_PROCESS_START" in env and ENV_MEASURE_CONTEXT in env)
            if perdu and _is_python(executable, argv):
                commande = argv if isinstance(argv, str) else " ".join(map(str, list(argv)[:3]))
                self.record("sans_mesure", commande)
            return
        # les recherches de modules d'importlib listent les dossiers du code :
        # ce n'est pas une lecture du test
        appelant = sys._getframe(2).f_code.co_filename
        if appelant.startswith("<frozen"):
            return
        if evenement == "open":
            chemin, mode, drapeaux = args
            if isinstance(chemin, int):
                return
            if isinstance(mode, str):
                if any(c in mode for c in "wax+"):
                    return
            elif isinstance(drapeaux, int) and drapeaux & _WRITE_FLAGS:
                return
            relatif = repository_path(chemin, self.repo)
            # un fichier absent n'est pas une donnée du test
            if relatif and relatif != "." and os.path.isfile(os.path.join(self.repo, relatif)):
                self.record("f", relatif)
            return
        chemin = args[0] if args else None
        if chemin is None:
            chemin = "."
        if isinstance(chemin, int):
            return
        relatif = repository_path(chemin, self.repo)
        if relatif:
            self.record("d", relatif)


def start(couverture: bool = True) -> Logbook | None:
    """Démarre la mesure dans ce processus, une seule fois, si `CARTE_MESURE`
    est posé. `couverture=False` : le processus de la suite démarre sa
    couverture lui-même (contextes par test)."""
    dossier = os.environ.get(ENV_MEASURE_DIR)
    if not dossier or getattr(sys, "_ameesh_carte", None) is not None:
        return getattr(sys, "_ameesh_carte", None)
    if couverture and os.environ.get("COVERAGE_PROCESS_START"):
        try:
            import coverage
            coverage.process_startup()
        except Exception:
            pass
    releve = Logbook(dossier)
    sys._ameesh_carte = releve
    sys.addaudithook(releve.hook)
    return releve


def pth_line(chemin_script: str = os.path.abspath(__file__)) -> str:
    """La ligne du fichier `.pth` : sans `CARTE_MESURE`, elle ne fait rien."""
    return ("import os; os.environ.get(%r) and __import__('runpy').run_path(%r, "
            "run_name='ameesh_carte_demarrage')\n" % (ENV_MEASURE_DIR, chemin_script))


if __name__ == "ameesh_carte_demarrage":
    start()
