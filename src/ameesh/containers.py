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

import datetime
import shutil
import subprocess
from dataclasses import dataclass, field

#: moteurs essayés en mode `auto` (le premier trouvé gagne)
CANDIDATES = ("docker", "podman")

#: délai maximal accordé à une commande du moteur (secondes)
DEFAULT_TIMEOUT = 5.0

#: variable qui porte les étiquettes à poser sur un conteneur du tour
LABELS_ENV = "AMEESH_CONTAINER_LABELS"

#: L73/#2003 : étiquette d'un volume nommé créé par un banc de test du mesh
#: (`scripts/pg-up.sh`). Le ménage ne récolte que les volumes dont le mesh a
#: DÉCLARÉ le cycle de vie : cette étiquette, avec la valeur
#: [`BENCH_DISPOSABLE`], posée sur déclaration explicite de l'appelant
#: (`AMEESH_PG_DISPOSABLE=1`). Le banc partagé et persistant (`ameesh-pg-data`)
#: n'en porte aucune : conservation par défaut. Un volume anonyme ne porte, lui,
#: aucune provenance du mesh (il part avec le conteneur étiqueté qui l'a créé,
#: `rm -f -v`) : jamais récolté par le ménage.
BENCH_LABEL = "ameesh.bench"

#: #2003 : seule valeur de [`BENCH_LABEL`] qui autorise la récolte — un banc
#: de lot que son appelant a DÉCLARÉ jetable, dont les données se recréent.
#: Toute autre valeur (ou l'absence d'étiquette) est conservée et signalée.
BENCH_DISPOSABLE = "disposable"

#: marque que Docker pose sur un volume créé implicitement pour un conteneur
ANONYMOUS_LABEL = "com.docker.volume.anonymous"

#: volumes inspectés par commande (`volume inspect` accepte une liste ; l'argv
#: reste borné sur un hôte qui en porte des centaines)
INSPECT_BATCH = 200


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
    #: #339 : contexte (docker) ou connexion (podman) interrogé, "" : le courant
    context: str = ""
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

    def _argv(self, *args: str) -> list[str]:
        if not self.context:
            return [self.binary, *args]
        flag = "--connection" if _engine(self.binary) == "podman" else "--context"
        return [self.binary, flag, self.context, *args]

    def _ids(self, label: str) -> list[str]:
        argv = self._argv("ps", "--no-trunc", "--quiet", "--filter", "label=%s" % label)
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

    def turn_containers(self, turn_id: str) -> list[dict] | None:
        """Les conteneurs EN COURS qui portent l'étiquette du tour, relevé
        frais : `id`, `name`, `image`, `lot` (étiquette `ameesh.lot`, ou None).
        Un conteneur sans lot est un travail de fond du tour ; un conteneur de
        lot (base de test réutilisée d'un tour à l'autre) vit jusqu'à la fin
        du lot (L73). Met à jour le cache de `running_for_turn`. None si le
        moteur ne répond pas."""
        if not turn_id:
            return []
        rows = self.list_all()
        if rows is None:
            return None
        out = [{"id": row["id"], "name": row["name"], "image": row["image"],
                "lot": row["labels"].get("ameesh.lot") or None}
               for row in rows if row["labels"].get("ameesh.turn") == turn_id]
        self._labels[turn_id] = [c["id"] for c in out]
        return out

    def list_all(self, stopped: bool = False) -> list[dict] | None:
        """Les conteneurs de l'hôte (L73, lecture seule) : `id`, `name`,
        `image`, `created`, `status`, `labels` ; None si le moteur ne répond
        pas. Sans `stopped`, seuls ceux qui tournent (`ps`).

        `stopped=True` (#1411) : aussi ceux qui ne tournent plus (`ps -a`) —
        un conteneur étiqueté sorti ou jamais démarré ne consomme plus de
        processus, mais retient sa couche et ses volumes anonymes ; sans `-a`
        il restait invisible du ménage comme de la mesure."""
        argv = self._argv("ps", "--no-trunc", *(["-a"] if stopped else []),
                          "--format", "{{json .}}")
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
        argv = self._argv("rm", "-f", "-v", ident)
        try:
            proc = self.run(argv, capture_output=True, text=True, timeout=self.timeout * 6)
        except (OSError, subprocess.SubprocessError) as exc:
            return False, str(exc)
        if getattr(proc, "returncode", 1) != 0:
            return False, (getattr(proc, "stderr", "") or "refused").strip()[:300]
        return True, ""

    def running_for_agent(self, agent: str) -> list[str]:
        """Identifiants des conteneurs étiquetés par un agent (repli, lecture
        seule : l'âge et l'absence de connexion se lisent ensuite sur place)."""
        if not agent:
            return []
        return self._ids("ameesh.agent=%s" % agent)

    def volumes(self, *, dangling: bool = True) -> list[dict] | None:
        """Les volumes nommés de l'hôte (L73/#2003, lecture seule) : `name`,
        `driver`, `labels`, `mountpoint` ; None si le moteur ne répond pas ou
        rend une ligne illisible (une liste partielle ne décide jamais d'une
        suppression).

        `dangling` (défaut) : seulement ceux qu'AUCUN conteneur ne référence,
        arrêtés compris (`volume ls --filter dangling=true`) — un volume
        encore monté, même par un conteneur arrêté, est en service.
        `dangling=False` : TOUS les volumes, pour distinguer une absence
        continue d'une reprise (le ménage #2003 réinitialise alors l'épisode)."""
        args = ["volume", "ls"]
        if dangling:
            args += ["--filter", "dangling=true"]
        args += ["--format", "{{json .}}"]
        try:
            proc = self.run(self._argv(*args), capture_output=True, text=True,
                            timeout=self.timeout)
        except (OSError, subprocess.SubprocessError):
            return None
        if getattr(proc, "returncode", 1) != 0:
            return None
        import json
        out: list[dict] = []
        for line in (proc.stdout or "").splitlines():
            if not line.strip():
                continue
            try:
                row = json.loads(line)
            except ValueError:
                return None
            if not isinstance(row, dict) or not str(row.get("Name") or ""):
                return None
            out.append({"name": str(row["Name"]),
                        "driver": str(row.get("Driver") or ""),
                        "labels": parse_labels(row.get("Labels")),
                        "mountpoint": str(row.get("Mountpoint") or "")})
        return out

    def volume_created(self, names) -> dict[str, float] | None:
        """`CreatedAt` (secondes epoch) des volumes `names`, par lots bornés ;
        None si le moteur ne répond pas, refuse, ou rend une date illisible
        (l'identité du volume n'est alors pas établie, donc rien n'est
        supprimé).

        #2003 : sert d'IDENTITÉ d'un volume (un nom recréé est un autre
        volume, dont l'épisode d'absence repart) et d'information de relevé.
        Ce n'est jamais l'âge de création qui tient lieu de délai sans
        conteneur : ce délai se mesure sur les relevés successifs du ménage.

        Docker refuse tout l'appel si un nom a disparu entre le relevé et
        l'inspection : le passage suivant repart d'un relevé frais."""
        out: dict[str, float] = {}
        names = [str(name) for name in names]
        for start in range(0, len(names), INSPECT_BATCH):
            chunk = names[start:start + INSPECT_BATCH]
            argv = self._argv("volume", "inspect", "--format", "{{json .}}", *chunk)
            try:
                proc = self.run(argv, capture_output=True, text=True,
                                timeout=self.timeout * 6)
            except (OSError, subprocess.SubprocessError):
                return None
            if getattr(proc, "returncode", 1) != 0:
                return None
            import json
            for line in (proc.stdout or "").splitlines():
                if not line.strip():
                    continue
                try:
                    row = json.loads(line)
                except ValueError:
                    return None
                name = str(row.get("Name") or "") if isinstance(row, dict) else ""
                stamp = parse_created(row.get("CreatedAt")) if isinstance(row, dict) else None
                if not name or stamp is None:
                    return None
                out[name] = stamp
        return out

    def remove_volume(self, name: str) -> tuple[bool, str]:
        """`volume rm <name>` (L73/#2003) : réservé aux volumes que l'appelant
        a explicitement DÉCLARÉS jetables ; Docker refuse de son côté un volume
        encore monté (la course avec un `docker run` qui le reprend est donc
        fermée)."""
        argv = self._argv("volume", "rm", name)
        try:
            proc = self.run(argv, capture_output=True, text=True, timeout=self.timeout * 6)
        except (OSError, subprocess.SubprocessError) as exc:
            return False, str(exc)
        if getattr(proc, "returncode", 1) != 0:
            return False, (getattr(proc, "stderr", "") or "refused").strip()[:300]
        return True, ""


def parse_created(value) -> float | None:
    """`CreatedAt` d'un volume (RFC 3339, `Z` ou décalage), en secondes epoch ;
    None si illisible. Python 3.9/3.10 ne lit pas le `Z` de `fromisoformat`."""
    text = str(value or "").strip()
    if not text:
        return None
    if text[-1:] in ("Z", "z"):
        text = text[:-1] + "+00:00"
    try:
        stamp = datetime.datetime.fromisoformat(text)
    except ValueError:
        return None
    if stamp.tzinfo is None:
        stamp = stamp.replace(tzinfo=datetime.timezone.utc)
    return stamp.timestamp()


def _engine(binary: str) -> str:
    import os
    return os.path.basename(binary).lower()


def _is_shim(path: str) -> bool:
    """Le script d'étiquetage d'un tour (L73), pas un moteur."""
    try:
        with open(path, encoding="utf-8", errors="replace") as fh:
            return fh.read(len(SHIM) + 1) == SHIM
    except OSError:
        return False


def _real_binary(name: str) -> str | None:
    """Le premier `name` du PATH qui n'est pas un script d'étiquetage."""
    import os
    for directory in (os.environ.get("PATH") or os.defpath).split(os.pathsep):
        found = shutil.which(name, path=directory or os.curdir)
        if found and not _is_shim(found):
            return found
    return None


def _contexts(runtime: Runtime) -> list[str] | None:
    """#339 : les contextes (docker) ou connexions (podman) du moteur ; None
    s'il ne répond pas."""
    if _engine(runtime.binary) == "podman":
        argv = [runtime.binary, "system", "connection", "list", "--format", "{{.Name}}"]
    else:
        argv = [runtime.binary, "context", "ls", "--format", "{{.Name}}"]
    try:
        proc = runtime.run(argv, capture_output=True, text=True, timeout=runtime.timeout)
    except (OSError, subprocess.SubprocessError):
        return None
    if getattr(proc, "returncode", 1) != 0:
        return None
    return [line.strip() for line in (proc.stdout or "").splitlines() if line.strip()]


def all_runtimes(cfg, run=None) -> tuple[list[Runtime], list[str]]:
    """#339 : tous les moteurs où un tour a pu lancer un conteneur étiqueté —
    chaque moteur de CANDIDATES présent (les scripts d'étiquetage d'un tour
    les couvrent tous) et celui que nomme la configuration, avec chacun de
    leurs contextes (docker) ou connexions (podman). Rend (moteurs, muets) :
    `muets`, les moteurs qui n'ont pas listé leurs contextes. Le point
    d'accès courant de chaque moteur est toujours interrogé (codex r2). Aucun si le
    moteur est désactivé (`none` : aucune étiquette n'est posée)."""
    import os
    configured = runtime_binary(getattr(cfg, "container_runtime", "") or "")
    if configured is None:
        return [], []
    timeout = max(0.5, float(getattr(cfg, "container_timeout", DEFAULT_TIMEOUT)
                             or DEFAULT_TIMEOUT))
    binaries: dict = {}
    for binary in [configured] + [_real_binary(c) for c in CANDIDATES]:
        if binary and not _is_shim(binary):
            binaries.setdefault(os.path.realpath(binary), binary)
    out: list[Runtime] = []
    silent: list[str] = []
    for binary in binaries.values():
        base = Runtime(binary=binary, timeout=timeout, run=run or subprocess.run)
        contexts = _contexts(base)
        if contexts is None:
            silent.append(binary)
            continue
        # le point d'accès courant d'abord (DOCKER_HOST, CONTAINER_HOST hérités
        # passent avant tout contexte nommé), puis chaque contexte ou connexion
        out.append(base)
        out += [Runtime(binary=binary, timeout=timeout, run=base.run, context=c)
                for c in contexts]
    return out, silent


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
  echo "ameesh: container engine not found (${0##*/})" >&2
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
