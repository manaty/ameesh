# SPDX-License-Identifier: AGPL-3.0-only
"""#2763 : le venv de production d'ameesh n'est jamais exposé à un tour.

Incident du 19/10/2026 (20:03, pc-smichea) : le lanceur stable du venv
(`bascule.py`) exporte `VIRTUAL_ENV=<venv-sha7>` et met `<venv-sha7>/bin` en
tête du `PATH`. Le runner en hérite, et chaque tour d'agent aussi : un
`uv pip install -e .` lancé depuis un worktree de relecture a installé la copie
de travail dans le venv vivant, et tout le poste a exécuté ce worktree
(« known descriptors: none », six agents critiques en pause à 20:07).

Ce module retire de l'environnement d'un tour :

* `VIRTUAL_ENV` quand il désigne le venv d'ameesh qui tourne ;
* du `PATH`, les `bin/` de ce venv, du venv stable et des venvs versionnés
  frères (`venv`, `venv-<sha7>`) : ni `pip`, ni `python3` du venv vivant ;

et rend au tour les deux seuls points d'entrée dont il a besoin, `ameesh` et
`agent-mail`, par des shims qui exécutent le binaire du venv par chemin
absolu. Un échec d'écriture des shims ne rouvre jamais le PATH de production
(#2772) : les `bin/` cachés restent retirés, le tour peut perdre les deux
points d'entrée s'ils ne viennent pas d'un autre dossier du PATH. Les tests
d'une branche passent par `PYTHONPATH=src` ou par un venv du worktree, jamais
par le venv partagé.

`installation_problems` sert à `ameesh doctor` : une installation éditable
(`__editable__*.pth`, `direct_url.json` éditable) ou un paquet `ameesh`
chargé hors du `site-packages` du venv sont signalés.
"""
from __future__ import annotations

import glob
import json
import logging
import os
import re
import sys
import tempfile

LOG = logging.getLogger(__name__)

#: points d'entrée rendus au tour (les hooks des harnais appellent agent-mail)
SHIMMED = ("ameesh", "agent-mail")
SHIM_DIR = "bin-ameesh"
SHIM = """#!/bin/sh
# #2763 : point d'entrée d'ameesh pour un tour, sans exposer le venv de
# production (ni pip, ni python3) dans le PATH du tour.
exec %s "$@"
"""

_VENV_NAME = re.compile(r"venv(?:-[0-9a-z.]+)?")
#: instantané de déploiement écrit par `poste.sh`/`vm.sh` (git archive)
_SOURCE_NAME = re.compile(r"src-[0-9a-z]+")


def _real(path: str) -> str:
    return os.path.realpath(path) if path else ""


def running_venv() -> str:
    """Le venv d'où s'exécute ameesh (`sys.prefix`), vide hors venv."""
    if sys.prefix == getattr(sys, "base_prefix", sys.prefix):
        return ""
    return sys.prefix


def hidden_venvs(env) -> set:
    """Les venvs d'ameesh à cacher : celui qui tourne, et ses frères
    `venv`/`venv-<sha7>` (le lanceur stable et les versions de `bascule.py`)."""
    out = set()
    courant = running_venv()
    if courant:
        out.add(_real(courant))
        parent = os.path.dirname(_real(courant))
        try:
            noms = os.listdir(parent)
        except OSError:
            noms = []
        for nom in noms:
            if _VENV_NAME.fullmatch(nom):
                out.add(_real(os.path.join(parent, nom)))
    virtual = env.get("VIRTUAL_ENV") or ""
    if virtual and courant and _real(virtual) in out:
        out.add(_real(virtual))
    return out


def install_shims(directory: str) -> str:
    """Écrit les shims `ameesh` et `agent-mail` dans `directory` ; rend le
    dossier, ou vide si ameesh ne tourne pas depuis un venv."""
    courant = running_venv()
    if not courant:
        return ""
    import shlex
    os.makedirs(directory, mode=0o700, exist_ok=True)
    for nom in SHIMMED:
        reel = os.path.join(courant, "bin", nom)
        if not os.access(reel, os.X_OK):
            continue
        texte = SHIM % shlex.quote(reel)
        cible = os.path.join(directory, nom)
        try:
            with open(cible, encoding="utf-8") as fh:
                if fh.read() == texte:
                    continue
        except OSError:
            pass
        # Les tours partagent le PID du runner : un fichier par écriture,
        # pour qu'un thread ne renomme pas le temporaire d'un autre.
        with tempfile.NamedTemporaryFile(mode="w", encoding="utf-8",
                                         dir=directory, delete=False) as fh:
            tmp = fh.name
            try:
                fh.write(texte)
                fh.flush()
                os.chmod(tmp, 0o755)
                os.replace(tmp, cible)
            finally:
                if os.path.exists(tmp):
                    os.unlink(tmp)
    return directory


def scrub(env: dict, shim_dir: str | None = None) -> dict:
    """Retire le venv de production de `env` (sur place) ; avec `shim_dir`,
    y installe les shims et les met en tête du PATH. Un échec d'écriture des
    shims retire quand même les `bin/` cachés : le PATH de production n'est
    jamais rouvert (#2772). Idempotent."""
    caches = hidden_venvs(env)
    if not caches:
        return env
    virtual = env.get("VIRTUAL_ENV") or ""
    if virtual and _real(virtual) in caches:
        env.pop("VIRTUAL_ENV", None)
    dossier = ""
    if shim_dir:
        try:
            dossier = install_shims(shim_dir)
        except OSError as exc:
            # #2772 : sans shims, le tour perd `ameesh`/`agent-mail` s'ils ne
            # viennent pas d'un autre dossier du PATH ; le venv de production,
            # lui, ne doit jamais revenir dans le PATH sur cette erreur.
            LOG.warning("venv guard: shims not written in %s (%s); "
                        "production venv hidden without them", shim_dir, exc)
            dossier = ""
    entrees = [e for e in (env.get("PATH") or "").split(os.pathsep) if e]
    bins = {os.path.join(v, "bin") for v in caches}
    gardees = [e for e in entrees if _real(e) not in bins]
    if dossier and dossier not in gardees:
        gardees.insert(0, dossier)
    env["PATH"] = os.pathsep.join(gardees) or os.defpath
    return env


def shim_dir(cfg) -> str:
    """Dossier des shims d'un tour, sous l'état géré de l'hôte."""
    return os.path.join(cfg.state_dir, ".menage", SHIM_DIR)


def _from_deployed_snapshot(charge: str, prefix: str) -> bool:
    """Le code chargé vient-il d'un instantané de déploiement `src-<sha>` ?

    `poste.sh install/migrer` sonde le code cible avec
    `PYTHONPATH=$PARTAGE/src-<sha>/src "$VENV/bin/python" -m ameesh` (le venv
    n'est pas encore installé) : cet instantané est voisin du venv, et sa
    présence n'est pas l'écart que `doctor` doit signaler. Un worktree
    quelconque, ou un `.menage/tmp/...`, reste signalé.
    """
    if not prefix:
        return False
    parent = os.path.dirname(_real(prefix))
    snapshot = os.path.dirname(os.path.dirname(charge))
    return (os.path.dirname(snapshot) == parent
            and _SOURCE_NAME.fullmatch(os.path.basename(snapshot)) is not None)


def installation_problems(prefix: str | None = None) -> list:
    """Écarts d'installation du venv d'ameesh : éditable, ou paquet chargé
    hors du site-packages du venv. Liste vide : installation saine."""
    import ameesh
    problemes = []
    prefix = prefix if prefix is not None else running_venv()
    charge = _real(os.path.dirname(ameesh.__file__))
    if prefix:
        sites = glob.glob(os.path.join(prefix, "lib", "python*", "site-packages"))
        for site in sites:
            for pth in sorted(glob.glob(os.path.join(site, "__editable__*ameesh*.pth"))):
                problemes.append("editable install: %s" % pth)
            for direct in sorted(glob.glob(os.path.join(site, "ameesh-*.dist-info",
                                                        "direct_url.json"))):
                try:
                    with open(direct, encoding="utf-8") as fh:
                        donnees = json.load(fh)
                except (OSError, ValueError):
                    continue
                if (donnees.get("dir_info") or {}).get("editable"):
                    problemes.append("editable install: %s -> %s"
                                     % (direct, donnees.get("url", "?")))
        # seulement quand ameesh est INSTALLÉ dans ce venv : un venv d'outils
        # (CI, worktree) lancé avec PYTHONPATH=src n'a rien à comparer
        installe = {_real(s) for s in sites
                    if glob.glob(os.path.join(s, "ameesh-*.dist-info"))}
        if installe and os.path.dirname(charge) not in installe \
                and not _from_deployed_snapshot(charge, prefix):
            problemes.append("ameesh loaded outside the venv: %s" % charge)
    return problemes
