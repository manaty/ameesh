#!/usr/bin/env python3
# SPDX-License-Identifier: AGPL-3.0-only
"""#4887/#7736 : refuser au gel une spec OpenAPI changée sans le schéma généré.

Le job `plan` de la CI et le composeur (`scripts/paquet-auto.py`,
`check_client_types`) refusent déjà `ui/src/api/schema.d.ts` périmé, mais
seulement après le gel : #7670 (h-08) puis #7630 (h-09) ont modifié
`docs/api/openapi-v1.json` sans régénérer les types clients, et deux
compositions ont été perdues. Ce garde tourne dans `scripts/freeze-checks.py`,
sur la tête gelée :

1. les chemins du diff gelé viennent de `AMEESH_FREEZE_PATHS` (le contrat de
   l'avertissement d'herméticité) ; si ni la spec, ni le schéma, ni le
   générateur ne bougent, il n'y a rien à faire (sortie 0, cas courant) ;
2. `node ui/scripts/gen-api.mjs --check` tranche quand `node` et les
   `ui/node_modules` du cache sont là (lien par `ameesh deps link --no-fill`,
   sans réseau) ; seul son diagnostic explicite `is stale` prouve la
   péremption. Un module manquant, un signal (SIGKILL) ou un refus d'accès
   sont des échecs d'environnement : ils restent non concluants ;
3. sans `node` ni modules, la garde statique de #4887 s'applique : une spec
   changée sans le schéma refuse le gel ; les autres cas restent non
   concluants et ne bloquent pas (canon §3).

Sortie : 0 vert ou non concluant, 1 rouge (spec sans schéma, ou types périmés
prouvés par le générateur), 2 hors d'un arbre ameesh. Aucune écriture hors du
lien de dépendances.
"""
from __future__ import annotations

import json
import os
import shutil
import subprocess
import sys

SPEC = "docs/api/openapi-v1.json"
SCHEMA = "ui/src/api/schema.d.ts"
GENERATOR = "ui/scripts/gen-api.mjs"
#: les chemins dont le changement peut rendre le schéma généré périmé
TRIGGERS = (SPEC, SCHEMA, GENERATOR, "ui/package.json", "ui/package-lock.json")
#: le contrat du garde du gel : les chemins du diff gelé, en JSON
PATHS_ENV = "AMEESH_FREEZE_PATHS"
#: borne d'un sous-processus (secondes) ; le gel borne l'ensemble
TIMEOUT = 120
#: un rc non nul avec ces marqueurs dit une dépendance absente, pas un schéma périmé
MODULE_MISSING = ("ERR_MODULE_NOT_FOUND", "Cannot find module")
#: le seul diagnostic qui prouve la péremption : celui du générateur (#7736 B)
STALE_MARKER = "is stale"


def changed_paths(environ=None) -> list[str] | None:
    """Les chemins du diff gelé, ou None quand ils ne sont pas lisibles."""
    raw = (os.environ if environ is None else environ).get(PATHS_ENV)
    if raw is None:
        return None
    try:
        paths = json.loads(raw)
    except ValueError:
        return None
    if not isinstance(paths, list) or not all(isinstance(p, str) for p in paths):
        return None
    return paths


def _text(proc) -> str:
    text = (proc.stdout or "") + (proc.stderr or "")
    if isinstance(text, bytes):
        text = text.decode("utf-8", "replace")
    return text


def _one_line(text: str) -> str:
    return " ".join((text or "").split())[:240]


def _static_red(paths: list[str] | None) -> str | None:
    """#4887 : la spec change sans le schéma. None quand le diff ne le dit pas."""
    if paths is not None and SPEC in paths and SCHEMA not in paths:
        return ("%s change sans %s : régénérer les types clients "
                "(`npm run gen` dans ui/, #4887)" % (SPEC, SCHEMA))
    return None


def _unsure(why: str, static: str | None) -> tuple[int, str]:
    """Un non-concluant ne bloque pas, sauf si la garde statique a une preuve."""
    if static:
        return 1, "%s ; contrôle réel non concluant (%s)" % (static, why)
    return 0, "types clients : non concluant (%s) ; la CI tranche" % why


def _link(root: str, *, which, run) -> tuple[bool, str]:
    """Les `ui/node_modules` du cache, sans réseau (`ameesh deps link --no-fill`)."""
    exe = which("ameesh")
    if not exe:
        return False, "ameesh absent"
    try:
        proc = run([exe, "deps", "link", "--no-fill", "ui"], cwd=root,
                   stdin=subprocess.DEVNULL, capture_output=True, text=True,
                   timeout=TIMEOUT)
    except (OSError, subprocess.SubprocessError) as exc:
        return False, "deps link : %s" % _one_line(str(exc))
    if proc.returncode != 0:
        return False, "deps link rc %d : %s" % (proc.returncode, _one_line(_text(proc)))
    if not os.path.lexists(os.path.join(root, "ui", "node_modules")):
        return False, "cache incomplet"
    return True, ""


def check(root: str, paths: list[str] | None, *, which=None,
          run=None) -> tuple[int, str]:
    """`(sortie, message)` du garde pour un arbre et un diff gelé."""
    which = shutil.which if which is None else which
    run = subprocess.run if run is None else run
    if paths is not None and not (set(paths) & set(TRIGGERS)):
        return 0, "types clients : inchangés"
    if not os.path.isfile(os.path.join(root, GENERATOR)):
        return 0, "types clients : pas de générateur dans l'arbre"
    static = _static_red(paths)
    node = which("node")
    if not node:
        return _unsure("node absent", static)
    if not os.path.lexists(os.path.join(root, "ui", "node_modules")):
        linked, why = _link(root, which=which, run=run)
        if not linked:
            return _unsure("modules absents (%s)" % why, static)
    try:
        proc = run([node, GENERATOR, "--check"], cwd=root,
                   stdin=subprocess.DEVNULL, capture_output=True, text=True,
                   timeout=TIMEOUT)
    except (OSError, subprocess.SubprocessError) as exc:
        return _unsure("exécution impossible : %s" % _one_line(str(exc)), static)
    text = _text(proc)
    if proc.returncode == 0:
        return 0, "types clients : à jour (`gen-api --check`)"
    if any(marker in text for marker in MODULE_MISSING):
        return _unsure("modules incomplets : %s" % _one_line(text), static)
    if proc.returncode < 0:
        # SIGKILL, OOM, arrêt du groupe : un échec d'environnement, pas une
        # preuve de péremption (#7736 B)
        return _unsure("gen-api tué par le signal %d : %s"
                       % (-proc.returncode, _one_line(text)), static)
    if STALE_MARKER in text:
        return 1, ("types clients périmés (`gen-api --check`, rc %d) : %s"
                   % (proc.returncode, _one_line(text) or "sans sortie"))
    # EACCES, disque, sortie illisible : le générateur n'a pas pu conclure
    return _unsure("gen-api rc %d : %s"
                   % (proc.returncode, _one_line(text) or "sans sortie"), static)


def main(argv=None) -> int:
    root = os.getcwd()
    if not (os.path.isdir(os.path.join(root, "src", "ameesh"))
            and os.path.isdir(os.path.join(root, "ui"))):
        print("garde-types-clients: run from the root of an ameesh tree",
              file=sys.stderr)
        return 2
    paths = changed_paths()
    if paths is None:
        # Un lancement à la main n'a pas le diff du gel : le contrôle réel
        # tourne (et peut lier les modules du cache), il faut le dire.
        print("types clients : chemins du diff gelé illisibles, "
              "contrôle réel exécuté (les ui/node_modules du cache peuvent "
              "être liés)")
    code, message = check(root, paths)
    print(message)
    return code


if __name__ == "__main__":
    sys.exit(main(sys.argv[1:]))
