#!/usr/bin/env python3
# SPDX-License-Identifier: AGPL-3.0-only
"""Bascule atomique du venv d'ameesh (#1969).

    bascule.py preparer --venv ~/.local/share/ameesh/venv --src <code> --version <sha7>
    bascule.py activer  --venv <venv> --version <sha7>
    bascule.py retour   --venv <venv>
    bascule.py etat     --venv <venv> [--json]

Le venv vivant ne reçoit plus jamais de `pip` : un venv VERSIONNÉ
(`<base>/venv-<version>`, pip dans son coin, contrôles d'imports, de point
d'entrée et de shebang) est préparé à côté, puis la bascule ne fait qu'UN
rename : le lien `<venv>` pointe d'un coup sur le venv versionné complet.
Un appel CLI ou un import concurrent voit donc toujours l'ancien ou le
nouveau code entier, jamais un état incomplet (incident du 16/10 : pip dans
le venv actif, `ModuleNotFoundError ameesh.platform.process`, `ameesh`
absent/code 127).

Première conversion : quand `<venv>` est encore un vrai répertoire, la
bascule échange atomiquement le répertoire et un lien préparé par
`renameat2(RENAME_EXCHANGE)` (Linux >= 3.15, types différents acceptés).
Sans ce noyau, la commande refuse et dit comment convertir à la main : rien
n'est jamais remplacé en deux temps.

`retour` refait la même bascule en arrière vers la version précédente
(conservée sur disque), sans pip ; il est idempotent. Les fichiers de
l'ancien venv restent en place : un processus déjà lancé garde son
environnement chargé et ses chemins.
"""
from __future__ import annotations

import argparse
import ctypes
import errno
import json
import os
import shutil
import subprocess
import sys
import time

AT_FDCWD = -100
RENAME_EXCHANGE = 2
MARKER = ".pret"
REPORT = "venv.precedent"


def _die(message, code=1):
    print("bascule: %s" % message, file=sys.stderr)
    sys.exit(code)


def _base(venv):
    return os.path.dirname(os.path.abspath(venv))


def _version_dir(venv, version):
    return os.path.join(_base(venv), "venv-%s" % version)


def _report_path(venv):
    return os.path.join(_base(venv), REPORT)


def _live_target(venv):
    """Nom de la version vivante (cible du lien), ou None si `<venv>` est un
    répertoire réel (état d'avant la première bascule) ou absent."""
    if os.path.islink(venv):
        return os.path.basename(os.readlink(venv))
    return None


def _exchange(a, b):
    """Échange atomique de deux chemins (fichiers, liens ou répertoires)."""
    try:
        libc = ctypes.CDLL("libc.so.6", use_errno=True)
        renameat2 = libc.renameat2
    except (OSError, AttributeError):
        _die("renameat2 indisponible : convertir le venv en lien à la main "
             "(mv venv venv-reel && ln -s venv-<version> venv) puis relancer")
    renameat2.argtypes = [ctypes.c_int, ctypes.c_char_p,
                          ctypes.c_int, ctypes.c_char_p, ctypes.c_uint]
    renameat2.restype = ctypes.c_int
    if renameat2(AT_FDCWD, os.fsencode(a), AT_FDCWD, os.fsencode(b),
                 RENAME_EXCHANGE) != 0:
        code = ctypes.get_errno()
        _die("échange atomique impossible (%s) : %s" % (os.strerror(code), a))


def _switch(venv, target_name):
    """Fait pointer `<venv>` sur `target_name` en un seul rename."""
    temp = "%s.suivant.%d" % (venv, os.getpid())
    if os.path.lexists(temp):
        os.remove(temp)
    os.symlink(target_name, temp)
    try:
        if os.path.islink(venv) or not os.path.exists(venv):
            os.replace(temp, venv)          # rename() sur un lien : atomique
        else:
            # venv réel (première bascule) : échanger le répertoire et le lien,
            # puis nommer le répertoire déplacé. Aucune fenêtre sans venv.
            _exchange(venv, temp)
            ecrase = os.path.join(_base(venv), "venv-precedent")
            if os.path.isdir(ecrase) and not os.path.islink(ecrase):
                shutil.rmtree(ecrase)
            os.replace(temp, ecrase)
    except OSError as exc:
        if os.path.lexists(temp):
            os.remove(temp)
        _die("bascule impossible : %s" % exc)


# --------------------------------------------------------------------------
# préparation
# --------------------------------------------------------------------------

def _run(cmd, **kwargs):
    return subprocess.run(cmd, check=True, **kwargs)


def _creer_venv(directory, python):
    _run([python, "-m", "venv", directory])


def _pip_install(directory, src):
    pip = os.path.join(directory, "bin", "pip")
    _run([pip, "install", "-q", "--force-reinstall", "--no-deps", src])
    _run([pip, "install", "-q", src])


def _controles(directory):
    """Imports, point d'entrée et shebang : tout doit répondre AVANT la bascule."""
    python = os.path.join(directory, "bin", "python")
    ameesh = os.path.join(directory, "bin", "ameesh")
    _run([python, "-c", "import psycopg, ameesh; print('imports ok')"],
         stdout=subprocess.DEVNULL)
    _run([ameesh, "--version"], stdout=subprocess.DEVNULL)
    with open(ameesh, "rb") as handle:
        shebang = handle.readline().decode("utf-8", "replace").strip()
    if shebang != "#!%s" % python:
        raise RuntimeError("shebang inattendu dans %s : %r" % (ameesh, shebang))


def preparer(args):
    version = args.version.strip()
    if not version or "/" in version or version.startswith("."):
        _die("--version doit être un nom simple (sha court)")
    src = os.path.abspath(args.src)
    if not os.path.isdir(src):
        _die("--src introuvable : %s" % src)
    cible = _version_dir(args.venv, version)
    etat = etat_de(args.venv)
    if os.path.isdir(cible):
        if etat.get("versions", {}).get(version) == "complete":
            print("bascule: venv-%s déjà préparé" % version)
            return 0
        shutil.rmtree(cible)
    print("bascule: préparation de venv-%s depuis %s" % (version, src))
    try:
        _creer_venv(cible, args.python)
        _pip_install(cible, src)
        _controles(cible)
    except (OSError, subprocess.CalledProcessError, RuntimeError) as exc:
        shutil.rmtree(cible, ignore_errors=True)   # rien de partiel ne reste
        print("bascule: préparation de venv-%s ÉCHOUÉE, version active intacte : %s"
              % (version, exc), file=sys.stderr)
        return 1
    with open(os.path.join(cible, MARKER), "w") as handle:
        json.dump({"version": version, "src": src, "at": time.time()}, handle)
    print("bascule: venv-%s préparé et contrôlé" % version)
    return 0


# --------------------------------------------------------------------------
# bascule et retour
# --------------------------------------------------------------------------

def activer(args):
    version = args.version.strip()
    cible = _version_dir(args.venv, version)
    if not os.path.isfile(os.path.join(cible, MARKER)):
        _die("venv-%s n'est pas préparé (lancer « preparer »)" % version)
    venv = os.path.abspath(args.venv)
    avant = _live_target(venv)
    if avant is None and os.path.isdir(venv) and not os.path.islink(venv):
        # première conversion : `_switch` déplace le répertoire réel sous
        # `venv-precedent`, c'est cette version-là que `retour` ramènera.
        avant = "venv-precedent"
    if avant and os.path.abspath(os.path.join(_base(venv), avant)) == cible:
        print("bascule: venv-%s déjà actif" % version)
        return 0
    _switch(venv, "venv-%s" % version)
    if avant:
        with open(_report_path(venv), "w") as handle:
            handle.write(avant + "\n")
    print("bascule: venv-%s actif%s" % (version, "" if not avant else " (avant : %s)" % avant))
    return 0


def retour(args):
    venv = os.path.abspath(args.venv)
    try:
        with open(_report_path(venv)) as handle:
            precedent = handle.read().strip()
    except OSError:
        _die("aucune version précédente enregistrée (%s)" % _report_path(venv))
    if not precedent:
        _die("version précédente illisible")
    if not os.path.isdir(os.path.join(_base(venv), precedent)):
        _die("version précédente %s absente du disque" % precedent)
    if _live_target(venv) == precedent:
        print("bascule: déjà sur %s (retour idempotent)" % precedent)
        return 0
    _switch(venv, precedent)
    print("bascule: retour sur %s" % precedent)
    return 0


def etat_de(venv):
    """État lisible : version vivante, précédente, venvs préparés."""
    base = _base(venv)
    vivante = _live_target(venv)
    if vivante is None and os.path.isdir(venv):
        vivante = os.path.basename(venv) + " (répertoire réel)"
    precedente = None
    try:
        with open(_report_path(venv)) as handle:
            precedente = handle.read().strip() or None
    except OSError:
        pass
    versions = {}
    try:
        for name in sorted(os.listdir(base)):
            path = os.path.join(base, name)
            if not name.startswith("venv-") or name == "venv-precedent" \
                    or not os.path.isdir(path):
                continue
            versions[name[len("venv-"):]] = \
                "complete" if os.path.isfile(os.path.join(path, MARKER)) else "partiel"
    except OSError:
        pass
    return {"venv": os.path.abspath(venv), "vivante": vivante,
            "precedente": precedente, "versions": versions}


def main(argv=None):
    parser = argparse.ArgumentParser(description="bascule atomique du venv d'ameesh (#1969)")
    sub = parser.add_subparsers(dest="commande", required=True)
    p = sub.add_parser("preparer", help="venv versionné, pip et contrôles à l'écart")
    p.add_argument("--venv", required=True, help="chemin du venv vivant")
    p.add_argument("--src", required=True, help="répertoire du code à installer")
    p.add_argument("--version", required=True, help="nom de version (sha court)")
    p.add_argument("--python", default=os.environ.get("BASCULE_PYTHON") or "python3",
                   help="interpréteur qui crée le venv versionné (défaut : python3 du PATH)")
    p.set_defaults(func=preparer)
    p = sub.add_parser("activer", help="bascule atomique vers un venv préparé")
    p.add_argument("--venv", required=True)
    p.add_argument("--version", required=True)
    p.set_defaults(func=activer)
    p = sub.add_parser("retour", help="bascule en arrière, sans pip")
    p.add_argument("--venv", required=True)
    p.set_defaults(func=retour)
    p = sub.add_parser("etat", help="version vivante, précédente, préparées")
    p.add_argument("--venv", required=True)
    p.add_argument("--json", action="store_true")
    p.set_defaults(func=lambda a: _afficher_etat(a))
    args = parser.parse_args(argv)
    return args.func(args) or 0


def _afficher_etat(args):
    etat = etat_de(args.venv)
    if args.json:
        print(json.dumps(etat, ensure_ascii=False, indent=2))
    else:
        print("vivante : %s" % (etat["vivante"] or "?"))
        print("précédente : %s" % (etat["precedente"] or "?"))
        for version, state in sorted(etat["versions"].items()):
            print("  venv-%s : %s" % (version, state))
    return 0


if __name__ == "__main__":
    sys.exit(main())
