#!/usr/bin/env python3
# SPDX-License-Identifier: AGPL-3.0-only
"""Validation hors ligne des modèles Patroni (EP8.4b, lot #6870).

Le validateur est celui de Patroni (`patroni.config.Config` avec le schéma
`patroni.validator.schema`), donc les mêmes règles que
`patroni --validate-config`. Les sondes réseau sont neutralisées : les adresses
10.78.0.0/24 de la grappe ne sont pas joignables depuis un poste d'essai, et
la validation porte sur la structure, pas sur l'état de la grappe. `shutil.which`
est aussi neutralisé : le `bin_dir` de PostgreSQL n'existe pas hors d'un nœud.

Un module Patroni absent n'est pas un échec (canon §3 : un test non concluant
ne retient rien) : le code de sortie 3 le signale à l'appelant. Le code 1 dit
qu'une configuration est invalide.
"""
from __future__ import annotations

import argparse
import os
import shutil
import socket
import sys


def neutraliser_sondes() -> None:
    """Empêche le validateur de Patroni de joindre etcd/Patroni/PostgreSQL."""
    def resoudre(*_args, **_kwargs):
        return [(socket.AF_INET, socket.SOCK_STREAM, 6, "", ("127.0.0.1", 0))]

    socket.getaddrinfo = resoudre
    socket.socket.connect_ex = lambda self, address: 0
    shutil.which = lambda commande, path=None: "/bin/true"


def main(argv=None) -> int:
    analyseur = argparse.ArgumentParser(
        description="Valide hors ligne des fichiers patroni.yml avec le schéma de Patroni.")
    analyseur.add_argument("fichiers", nargs="+", help="fichiers patroni.yml à valider")
    args = analyseur.parse_args(argv)

    try:
        from patroni.config import Config, ConfigParseError
        from patroni.validator import populate_validate_params, schema
    except ImportError as exc:
        print("valider-modeles : Patroni absent (%s) ; validation ignorée" % exc,
              file=sys.stderr)
        return 3

    # Le rendu ne contient pas les secrets : ils viennent des variables
    # d'environnement. Le schéma de Patroni les attend dans la configuration
    # effective ; des valeurs de validation suffisent ici.
    for nom in ("PATRONI_RESTAPI_USERNAME", "PATRONI_RESTAPI_PASSWORD",
                "PATRONI_REPLICATION_USERNAME", "PATRONI_REPLICATION_PASSWORD"):
        os.environ.setdefault(nom, "validation-structurelle")

    neutraliser_sondes()
    populate_validate_params(ignore_listen_port=True)
    code = 0
    for chemin in args.fichiers:
        try:
            Config(chemin, validator=schema)
        except ConfigParseError as exc:
            print("INVALIDE %s : %s" % (chemin, exc.value), file=sys.stderr)
            code = 1
        except Exception as exc:  # un bug du validateur reste un échec explicite
            print("INVALIDE %s : %s: %s" % (chemin, type(exc).__name__, exc),
                  file=sys.stderr)
            code = 1
        else:
            print("valide : %s" % chemin)
    return code


if __name__ == "__main__":
    sys.exit(main())
