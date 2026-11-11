#!/usr/bin/env python3
# SPDX-License-Identifier: AGPL-3.0-only
"""Corrige la forme de ``etcd3.hosts`` dans les configurations du banc.

Constat d'exécution du 11/11/2026 (lot #6902) : ``generer-configuration.py``
(#6870) rendait ``etcd3.hosts`` avec le schéma (« https://hôte:2379 »), alors
que Patroni 4.x attend « hôte:port » et applique ``protocol: https``. Avec le
schéma, Patroni refuse de démarrer (``ValueError: Invalid IPv6 URL``).

#6925 a corrigé le générateur ; sur une sortie actuelle, ce correcteur ne
trouve rien. Il reste pour les configurations héritées (antérieures à #6925)
et consigne alors le constat dans ``$BANC_ETAT/constats.json``.
"""
from __future__ import annotations

import argparse
import json
import pathlib
import re
import sys

MOTIF = re.compile(r"^  hosts: (\S.*)$", re.M)
CAUSE = "etcd3.hosts rendu avec schéma par #6870 ; Patroni 4.x attend hôte:port"
CORRECTION = "lot #6925 (fusionné) ; correcteur conservé pour les entrées héritées"


def corriger(dossier: pathlib.Path) -> list[dict]:
    constats = []
    for fichier in sorted((dossier / "config").glob("*/patroni.yml")):
        texte = fichier.read_text(encoding="utf-8")
        trouve = MOTIF.search(texte)
        if not trouve:
            continue
        hotes = [hote.strip() for hote in trouve.group(1).split(",")]
        if not any("://" in hote for hote in hotes):
            continue
        avant = trouve.group(1)
        corriges = [hote.split("://", 1)[1] if "://" in hote else hote for hote in hotes]
        apres = ",".join(corriges)
        fichier.write_text(
            texte[: trouve.start()] + "  hosts: " + apres + texte[trouve.end():],
            encoding="utf-8",
        )
        constats.append(
            {
                "fichier": str(fichier.relative_to(dossier)),
                "avant": avant,
                "apres": apres,
                "cause": CAUSE,
                "correction": CORRECTION,
            }
        )
    (dossier / "constats.json").write_text(
        json.dumps(constats, ensure_ascii=False, indent=2) + "\n", encoding="utf-8"
    )
    return constats


def main(argv=None) -> int:
    parseur = argparse.ArgumentParser(
        description="Corrige etcd3.hosts dans les configurations du banc et consigne le constat."
    )
    parseur.add_argument("--dossier", type=pathlib.Path, required=True, help="dossier d'état du banc")
    arguments = parseur.parse_args(argv)
    if not arguments.dossier.is_dir():
        print("dossier d'état introuvable : %s" % arguments.dossier, file=sys.stderr)
        return 2
    constats = corriger(arguments.dossier)
    if constats:
        print(
            "écart #6870 : %d configuration(s) etcd3.hosts corrigée(s) dans le banc "
            "(voir constats.json)" % len(constats),
            file=sys.stderr,
        )
    return 0


if __name__ == "__main__":
    sys.exit(main())
