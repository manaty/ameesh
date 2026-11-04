#!/usr/bin/env python3
# SPDX-License-Identifier: AGPL-3.0-only
"""#5742 : refuser une part sans marge, récupérer ses temporaires à sa fin.

Usage : python3 scripts/ci-test-space.py -- <interpréteur> scripts/test-parts.py run ...
Le refus (75) est une panne d'infrastructure, sans test exécuté ni flux vert.
Le contrôle est ponctuel, pas une réservation contre les autres jobs de la VM.
"""
from __future__ import annotations

import argparse
import os
from pathlib import Path
import signal
import subprocess
import tempfile

GIB = 1024 ** 3
MIN_LIBRE_GIB = 5.0
MIN_TMP_GIB = 1.0  # le banc autre-fs dispose d'un tmpfs distinct
MIN_INODES = 10000


def verifier(chemin: Path, minimum_gib: float) -> None:
    etat = os.statvfs(chemin)
    libre = etat.f_bavail * etat.f_frsize
    # Certains FS n'ont pas de table d'inodes bornée (f_files == 0).
    if libre < minimum_gib * GIB or (etat.f_files and etat.f_favail < MIN_INODES):
        raise OSError("%s : %.2f GiB libres, %s inodes disponibles "
                      "(minimum %.1f GiB / %d inodes)"
                      % (chemin, libre / GIB, etat.f_favail, minimum_gib, MIN_INODES))


def executer(commande: list[str]) -> int:
    # Garder le parent temporaire choisi par le job (dont le tmpfs autre-fs),
    # et un nom court : les sockets Unix de gpg ont une limite de chemin.
    parent = Path(os.environ.get("TMPDIR") or tempfile.gettempdir()).resolve()
    try:
        # Le Worker écrit sur / et RUNNER_TEMP même quand les tests sont en tmpfs.
        verifier(Path("/"), MIN_LIBRE_GIB)
        verifier(Path(os.environ.get("RUNNER_TEMP") or parent), MIN_LIBRE_GIB)
        verifier(Path.cwd(), MIN_LIBRE_GIB)
        verifier(parent, MIN_TMP_GIB)
        dossier = tempfile.TemporaryDirectory(prefix="ci-", dir=parent)
    except OSError as erreur:
        print("::warning::CI non concluante : marge disque insuffisante ou "
              "illisible ; aucun test lancé (#5742) : %s" % erreur, flush=True)
        return 75
    with dossier as racine:
        env = dict(os.environ, TMPDIR=racine, TMP=racine, TEMP=racine)
        print("#5742 : temporaires de cette part : %s" % racine, flush=True)
        anciens = {}
        proc = None

        def interrompre(numero, _frame):
            raise SystemExit(128 + numero)

        try:
            for sig in (signal.SIGTERM, signal.SIGINT):
                anciens[sig] = signal.signal(sig, interrompre)
            # Groupe privé : les sous-processus parallèles et leurs descendants
            # doivent avoir fini avant de supprimer leurs fichiers temporaires.
            try:
                proc = subprocess.Popen(commande, env=env, start_new_session=True)
            except OSError as erreur:
                print("::warning::CI non concluante : lancement impossible (#5742) : %s"
                      % erreur, flush=True)
                return 75
            code = proc.wait()
            return code if code >= 0 else 128 - code
        finally:
            for sig in anciens:
                signal.signal(sig, signal.SIG_IGN)
            try:
                if proc is not None:
                    try:
                        os.killpg(proc.pid, signal.SIGTERM)
                    except ProcessLookupError:
                        pass
                    try:
                        proc.wait(timeout=10)
                    except subprocess.TimeoutExpired:
                        try:
                            os.killpg(proc.pid, signal.SIGKILL)
                        except ProcessLookupError:
                            pass
                        proc.wait()
                    # Le chef peut être sorti avant un descendant qui ignore TERM.
                    try:
                        os.killpg(proc.pid, signal.SIGKILL)
                    except ProcessLookupError:
                        pass
            finally:
                for sig, ancien in anciens.items():
                    signal.signal(sig, ancien)


def main(argv=None) -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("commande", nargs=argparse.REMAINDER)
    args = parser.parse_args(argv)
    commande = args.commande
    if commande[:1] == ["--"]:
        commande = commande[1:]
    if not commande:
        parser.error("commande de la part requise après --")
    return executer(commande)


if __name__ == "__main__":
    raise SystemExit(main())
