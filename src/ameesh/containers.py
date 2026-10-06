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


def turn_labels(turn_id: str, agent: str) -> str:
    """Les étiquettes d'un tour, au format `clé=valeur,clé=valeur`."""
    return "ameesh.turn=%s,ameesh.agent=%s" % (turn_id, agent)


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
