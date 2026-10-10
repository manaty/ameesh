# SPDX-License-Identifier: AGPL-3.0-only
"""Conteneurs rattachés à un tour (lot L31, décision 0028).

L'exécuteur pose l'étiquette `ameesh.turn=<turn_id>` (et
`ameesh.agent=<nom>`) dans l'environnement du tour : les outils qui lancent un
conteneur la reprennent (`AMEESH_CONTAINER_LABELS`). À la fin d'un tour,
l'exécuteur demande en **lecture seule** au moteur de conteneurs ceux qui
portent encore son étiquette — des ressources orphelines, signalées, jamais
supprimées ici.

Aucun moteur n'est requis : sans binaire (ou sans droit, ou sans démon), la
liste est vide et le rattachement par groupe de processus reste la garantie.
"""
from __future__ import annotations

import shutil
import subprocess
from dataclasses import dataclass, field

#: moteurs essayés en mode `auto` (le premier trouvé gagne)
CANDIDATES = ("docker", "podman")

#: délai maximal accordé à une commande du moteur (secondes)
DEFAULT_TIMEOUT = 5.0

#: variable qui porte les étiquettes à poser sur un conteneur du tour
LABELS_ENV = "AMEESH_CONTAINER_LABELS"


def turn_labels(turn_id: str, agent: str, lot: str | None = None) -> str:
    """Les étiquettes d'un tour, au format `clé=valeur,clé=valeur`.

    L73 : `ameesh.lot=<lot>` quand le lot du tour est connu — le conteneur vit
    alors jusqu'à la fin du lot (base de test réutilisée d'un tour à l'autre),
    sinon jusqu'à la fin du tour."""
    out = "ameesh.turn=%s,ameesh.agent=%s" % (turn_id, agent)
    if lot:
        out += ",ameesh.lot=%s" % lot
    return out


def parse_labels(value) -> dict:
    """Étiquettes d'une ligne `ps` : mapping (podman) ou texte `k=v,k=v`
    (docker)."""
    if isinstance(value, dict):
        return {str(k): str(v) for k, v in value.items()}
    out: dict = {}
    for part in str(value or "").split(","):
        key, sep, val = part.partition("=")
        if sep and key.strip():
            out[key.strip()] = val.strip()
    return out


def runtime_binary(name: str = "") -> str | None:
    """Chemin du moteur de conteneurs, ou None (aucun, ou désactivé).

    `name` vide ou `auto` : `docker` puis `podman`. `none`/`off`/`0` :
    désactivé.
    """
    cleaned = (name or "").strip().lower()
    if cleaned in ("none", "off", "0"):
        return None
    if cleaned not in ("", "auto"):
        return shutil.which(cleaned)
    for candidate in CANDIDATES:
        found = shutil.which(candidate)
        if found:
            return found
    return None


@dataclass
class Runtime:
    """Une façade bornée sur le moteur de conteneurs (lecture seule)."""

    binary: str
    timeout: float = DEFAULT_TIMEOUT
    #: injectable pour les tests : `(argv, capture_output, text, timeout)`
    run: object = subprocess.run
    _labels: dict = field(default_factory=dict)

    @classmethod
    def from_config(cls, cfg) -> "Runtime | None":
        binary = runtime_binary(getattr(cfg, "container_runtime", "") or "")
        if not binary:
            return None
        return cls(binary=binary,
                   timeout=max(0.5, float(getattr(cfg, "container_timeout",
                                                DEFAULT_TIMEOUT) or DEFAULT_TIMEOUT)))

    def _ids(self, label: str) -> list[str]:
        argv = [self.binary, "ps", "--no-trunc", "--quiet", "--filter", "label=%s" % label]
        try:
            proc = self.run(argv, capture_output=True, text=True, timeout=self.timeout)
        except (OSError, subprocess.SubprocessError):
            return []
        if getattr(proc, "returncode", 1) != 0:
            return []
        return [line.strip() for line in (proc.stdout or "").splitlines() if line.strip()]

    def running_for_turn(self, turn_id: str) -> list[str]:
        """Identifiants des conteneurs qui portent encore l'étiquette du tour."""
        if not turn_id:
            return []
        if turn_id not in self._labels:
            self._labels[turn_id] = self._ids("ameesh.turn=%s" % turn_id)
        return list(self._labels[turn_id])

    def list_all(self) -> list[dict] | None:
        """Tous les conteneurs EN COURS (L73, lecture seule) : `id`, `name`,
        `image`, `created`, `labels` ; None si le moteur ne répond pas."""
        argv = [self.binary, "ps", "--no-trunc", "--format", "{{json .}}"]
        try:
            proc = self.run(argv, capture_output=True, text=True, timeout=self.timeout)
        except (OSError, subprocess.SubprocessError):
            return None
        if getattr(proc, "returncode", 1) != 0:
            return None
        import json
        out: list[dict] = []
        text = (proc.stdout or "").strip()
        rows: list = []
        if text.startswith("["):        # podman ancien : un tableau JSON
            try:
                rows = json.loads(text)
            except ValueError:
                rows = []
        else:
            for line in text.splitlines():
                try:
                    rows.append(json.loads(line))
                except ValueError:
                    continue
        for row in rows:
            if not isinstance(row, dict):
                continue
            names = row.get("Names") or row.get("Name") or ""
            if isinstance(names, list):
                names = ",".join(str(n) for n in names)
            out.append({
                "id": str(row.get("ID") or row.get("Id") or ""),
                "name": str(names),
                "image": str(row.get("Image") or ""),
                "created": str(row.get("CreatedAt") or row.get("Created") or ""),
                "status": str(row.get("Status") or row.get("State") or ""),
                "labels": parse_labels(row.get("Labels")),
            })
        return out

    def remove(self, ident: str) -> tuple[bool, str]:
        """`rm -f -v <id>` (L73) : réservé aux conteneurs ÉTIQUETÉS par un tour
        d'ameesh (l'appelant le vérifie) ; les volumes anonymes partent avec."""
        argv = [self.binary, "rm", "-f", "-v", ident]
        try:
            proc = self.run(argv, capture_output=True, text=True, timeout=self.timeout * 6)
        except (OSError, subprocess.SubprocessError) as exc:
            return False, str(exc)
        if getattr(proc, "returncode", 1) != 0:
            return False, (getattr(proc, "stderr", "") or "refusé").strip()[:300]
        return True, ""

    def running_for_agent(self, agent: str) -> list[str]:
        """Identifiants des conteneurs étiquetés par un agent (repli, lecture
        seule : l'âge et l'absence de connexion se lisent ensuite sur place)."""
        if not agent:
            return []
        return self._ids("ameesh.agent=%s" % agent)


def running_for_turn(cfg, turn_id: str) -> list[str]:
    """Raccourci : les conteneurs d'un tour, vide si aucun moteur configuré."""
    runtime = Runtime.from_config(cfg)
    return runtime.running_for_turn(turn_id) if runtime is not None else []


# --------------------------------------------------------------------------
# L73 : étiquetage automatique (`docker run` / `create` d'un tour)
# --------------------------------------------------------------------------

#: script posé devant le vrai binaire dans le PATH d'un tour : il ajoute les
#: étiquettes du tour (`AMEESH_CONTAINER_LABELS`) à `run` et `create` (et
#: `container run|create`), et transmet tout le reste tel quel au vrai binaire
#: (`AMEESH_REAL_DOCKER` / `AMEESH_REAL_PODMAN`). Un conteneur lancé par
#: l'API (bibliothèque, compose) n'est pas étiqueté : il reste signalé.
SHIM = r"""#!/bin/sh
# Posé par ameesh (L73) : étiquette les conteneurs lancés par un tour.
case "${0##*/}" in
  docker) real="$AMEESH_REAL_DOCKER" ;;
  podman) real="$AMEESH_REAL_PODMAN" ;;
  *) real="" ;;
esac
if [ -z "$real" ] || [ ! -x "$real" ]; then
  echo "ameesh : moteur de conteneurs introuvable (${0##*/})" >&2
  exit 127
fi
if [ "$1" = run ] || [ "$1" = create ]; then
  pre="$1"; shift
elif [ "$1" = container ] && { [ "$2" = run ] || [ "$2" = create ]; }; then
  pre="$1 $2"; shift 2
else
  exec "$real" "$@"
fi
if [ -n "$AMEESH_CONTAINER_LABELS" ]; then
  old_ifs=$IFS; IFS=,; set -f
  rev=""
  for l in $AMEESH_CONTAINER_LABELS; do rev="$l,$rev"; done
  for l in $rev; do set -- --label "$l" "$@"; done
  IFS=$old_ifs; set +f
fi
exec "$real" $pre "$@"
"""


def install_shims(directory: str, path_env: str) -> dict:
    """Écrit les scripts d'étiquetage dans `directory` pour les moteurs
    présents dans `path_env` ; rend les variables à poser (PATH, binaires
    réels), vide si aucun moteur."""
    import os
    found = {}
    for name in CANDIDATES:
        real = shutil.which(name, path=path_env)
        if real and os.path.dirname(os.path.realpath(real)) != os.path.realpath(directory):
            found[name] = real
    if not found:
        return {}
    os.makedirs(directory, mode=0o700, exist_ok=True)
    for name in found:
        target = os.path.join(directory, name)
        current = None
        try:
            with open(target, encoding="utf-8") as fh:
                current = fh.read()
        except OSError:
            pass
        if current != SHIM:
            tmp = target + ".tmp-%d" % os.getpid()
            with open(tmp, "w", encoding="utf-8") as fh:
                fh.write(SHIM)
            os.chmod(tmp, 0o755)
            os.replace(tmp, target)
    env = {"PATH": directory + os.pathsep + path_env}
    for name, real in found.items():
        env["AMEESH_REAL_%s" % name.upper()] = real
    return env
