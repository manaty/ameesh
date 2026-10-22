# SPDX-License-Identifier: AGPL-3.0-only
"""Services utilisateur (L65) : launchd sur macOS.

Le CLI `ameesh service` compose le plan d'un service (nom, arguments,
description) ; ce module est le seul endroit qui sait l'écrire et le confier
au gestionnaire de services de l'OS.

macOS : un **LaunchAgent** par service, dans `~/Library/LaunchAgents`, chargé
dans le domaine `gui/<uid>` (`launchctl bootstrap`). Linux garde ses unités
de `deploy/systemd/` (le CLI le rappelle) ; Windows viendra avec L67.

launchd n'a pas d'`EnvironmentFile=` : le service lance `/bin/sh -c` qui
source `~/.config/ameesh/env` — le même fichier que les unités systemd — puis
`exec` le binaire ameesh. Le plist ne contient donc **aucun secret** : ni le
DSN, ni une clé. Le fichier d'environnement reste en 0600, hors du plist.

Le service est un agent de la session de l'utilisateur : il démarre à
l'ouverture de session et s'arrête à la fermeture. C'est le modèle d'un poste
de travail (le mac d'un humain), pas d'une machine sans session graphique.
"""
from __future__ import annotations

import os
import plistlib
import re
import shlex
import shutil
import subprocess
from dataclasses import dataclass
from typing import Callable, Mapping

from . import NotAvailable, is_macos

#: dossier des LaunchAgents de l'utilisateur (surchargeable pour les tests)
LAUNCHD_DIR_ENV = "AMEESH_LAUNCHD_DIR"
#: dossier des journaux des services (surchargeable pour les tests)
LOG_DIR_ENV = "AMEESH_LAUNCHD_LOG_DIR"
#: préfixe des labels et des fichiers : `ameesh.runner`, `ameesh-notify.plist`
LABEL_PREFIX = "ameesh"
#: fichier d'environnement de l'hôte, le même que celui des unités systemd
HOST_ENV_FILE = "~/.config/ameesh/env"
#: PATH explicite des services : une session launchd n'importe pas celui du
#: shell (mise, ~/.local/bin), et le runner doit y trouver ses harnais (L106)
SERVICE_PATH = ("~/.local/share/mise/shims", "~/.local/bin",
                "/opt/homebrew/bin", "/opt/homebrew/sbin",
                "/usr/local/sbin", "/usr/local/bin", "/usr/bin", "/bin")
#: délai minimal entre deux relances (secondes) : le `RestartSec` de systemd
DEFAULT_THROTTLE_S = 10
#: une commande launchctl ne bloque pas un tour
LAUNCHCTL_TIMEOUT_S = 30.0


class ServiceError(RuntimeError):
    """Un service n'a pas pu être écrit, chargé ou retiré (code 1)."""


@dataclass(frozen=True)
class Service:
    """Le plan d'un service utilisateur : ce que `ameesh service` installe.

    `name` nomme le label (`ameesh.<name>`), le fichier (`ameesh-<name>.plist`)
    et les journaux (`<name>.log`, `<name>.err`). `args` sont les arguments du
    binaire ameesh ; le chemin du binaire est résolu à l'installation.
    """

    name: str
    args: tuple[str, ...]
    description: str = ""
    throttle: int = DEFAULT_THROTTLE_S
    #: label launchd explicite (`ameesh.runner.<agent>`) ; vide = `ameesh.<name>`
    label_name: str = ""

    @property
    def label(self) -> str:
        return self.label_name or "%s.%s" % (LABEL_PREFIX, self.name)


def _home(env: Mapping[str, str] | None = None) -> str:
    env = os.environ if env is None else env
    return env.get("HOME") or os.path.expanduser("~")


def launch_agents_dir(env: Mapping[str, str] | None = None) -> str:
    """`~/Library/LaunchAgents`, ou `AMEESH_LAUNCHD_DIR` (tests, poste isolé)."""
    env = os.environ if env is None else env
    return env.get(LAUNCHD_DIR_ENV) or os.path.join(
        _home(env), "Library", "LaunchAgents")


def log_dir(env: Mapping[str, str] | None = None) -> str:
    """`~/Library/Logs/ameesh`, ou `AMEESH_LAUNCHD_LOG_DIR` (tests)."""
    env = os.environ if env is None else env
    return env.get(LOG_DIR_ENV) or os.path.join(
        _home(env), "Library", "Logs", "ameesh")


def plist_name(service: Service) -> str:
    return "%s-%s.plist" % (LABEL_PREFIX, service.name)


def plist_path(service: Service, env: Mapping[str, str] | None = None) -> str:
    return os.path.join(launch_agents_dir(env), plist_name(service))


def service_path(env: Mapping[str, str] | None = None) -> str:
    """Le PATH du service, avec `~` résolu (launchd ne l'étend pas)."""
    home = _home(env)
    out = []
    for item in SERVICE_PATH:
        if item == "~":
            out.append(home)
        elif item.startswith("~/"):
            out.append(os.path.join(home, item[2:]))
        else:
            out.append(item)
    return ":".join(out)


def shell_command(service: Service, ameesh_bin: str,
                  env: Mapping[str, str] | None = None) -> str:
    """La ligne de commande du service : source l'environnement de l'hôte puis
    `exec` ameesh.

    `launchd` ne sait pas lire un `EnvironmentFile=` ; le shell lit le fichier
    de l'hôte (facultatif, 0600) pour que le DSN et les jetons ne soient pas
    recopiés dans le plist (0644). Un fichier absent n'est pas une erreur :
    la configuration peut venir de `config.json` ou du trousseau.
    """
    argv = " ".join(shlex.quote(str(item)) for item in (ameesh_bin, *service.args))
    env_file = os.path.join(_home(env), HOST_ENV_FILE[2:])
    return ("set -a\n"
            "if [ -r %(env)s ]; then . %(env)s; fi\n"
            "set +a\n"
            "exec %(argv)s\n"
            % {"env": shlex.quote(env_file), "argv": argv})


def payload(service: Service, ameesh_bin: str,
            env: Mapping[str, str] | None = None) -> dict:
    """Le contenu du plist (un objet, sérialisé en XML par `plistlib`)."""
    env = os.environ if env is None else env
    log = os.path.join(log_dir(env), service.name)
    return {
        "Label": service.label,
        "ProgramArguments": ["/bin/sh", "-c", shell_command(service, ameesh_bin, env)],
        "RunAtLoad": True,
        # le service repart après un échec, pas après une sortie propre
        # (le `Restart=on-failure` des unités systemd)
        "KeepAlive": {"SuccessfulExit": False},
        "ThrottleInterval": int(service.throttle),
        "ProcessType": "Background",
        "EnvironmentVariables": {
            "PATH": service_path(env),
            "HOME": _home(env),
        },
        "StandardOutPath": log + ".log",
        "StandardErrorPath": log + ".err",
    }


def plist_bytes(service: Service, ameesh_bin: str,
                env: Mapping[str, str] | None = None) -> bytes:
    return plistlib.dumps(payload(service, ameesh_bin, env),
                          fmt=plistlib.FMT_XML, sort_keys=False)


def _require_macos() -> None:
    if not is_macos():
        raise NotAvailable("launchd services",
                           "only macOS provides launchd user agents")


def _domain() -> str:
    return "gui/%d" % os.getuid()


def _launchctl(run: Callable, *args: str, timeout: float = LAUNCHCTL_TIMEOUT_S):
    """`launchctl <args>` (le code de sortie est rendu, pas levé)."""
    try:
        return run(["launchctl", *args], capture_output=True, text=True,
                   timeout=timeout, check=False)
    except (OSError, subprocess.SubprocessError) as exc:
        raise ServiceError("launchctl %s: %s" % (" ".join(args), exc)) from exc


def _output(result) -> str:
    text = (getattr(result, "stderr", "") or getattr(result, "stdout", "") or "").strip()
    return " ".join(text.split())[:300]


def _bootout(service: Service, run: Callable, env: Mapping[str, str] | None) -> None:
    """Décharge le service s'il l'est déjà (l'échec est normal s'il ne l'est pas).

    `bootout` est la forme moderne ; `unload -w` reste le repli des macOS
    anciens (et le chargement symétrique plus bas).
    """
    result = _launchctl(run, "bootout", "%s/%s" % (_domain(), service.label))
    if result.returncode != 0:
        _launchctl(run, "unload", "-w", plist_path(service, env))


def _write(service: Service, ameesh_bin: str,
           env: Mapping[str, str] | None) -> str:
    path = plist_path(service, env)
    directory = os.path.dirname(path)
    os.makedirs(directory, mode=0o755, exist_ok=True)
    os.makedirs(log_dir(env), mode=0o700, exist_ok=True)
    # un journal peut porter des erreurs de base ou de compte : le dossier
    # reste privé même s'il existait déjà avec des droits plus larges
    try:
        os.chmod(log_dir(env), 0o700)
    except OSError:
        pass
    data = plist_bytes(service, ameesh_bin, env)
    temporary = path + ".tmp"
    with open(temporary, "wb") as handle:
        handle.write(data)
    os.chmod(temporary, 0o644)
    os.replace(temporary, path)
    return path


def install(service: Service, ameesh_bin: str, *,
            run: Callable = subprocess.run,
            env: Mapping[str, str] | None = None) -> str:
    """Écrit le plist et charge le service ; rend son chemin.

    Idempotent : un service déjà chargé est déchargé puis rechargé, pour que
    le plist modifié soit celui que launchd exécute. Si `bootstrap` échoue
    (macOS ancien, session sans domaine `gui`), `load -w` est essayé ; les
    deux échecs sont une erreur, le plist restant écrit.
    """
    _require_macos()
    path = _write(service, ameesh_bin, env)
    _bootout(service, run, env)
    result = _launchctl(run, "bootstrap", _domain(), path)
    if result.returncode != 0:
        legacy = _launchctl(run, "load", "-w", path)
        if legacy.returncode != 0:
            raise ServiceError(
                "launchctl bootstrap %s %s: %s" % (_domain(), path, _output(result)))
    return path


def _ours(path: str, service: Service) -> bool:
    """Le plist est-il bien celui de ce service (avant de le supprimer) ?"""
    try:
        with open(path, "rb") as handle:
            document = plistlib.load(handle)
    except (OSError, ValueError):
        return False
    return isinstance(document, dict) and document.get("Label") == service.label


def uninstall(service: Service, *, run: Callable = subprocess.run,
              env: Mapping[str, str] | None = None) -> bool:
    """Décharge et supprime le plist ; rend False s'il n'était pas installé.

    Un fichier qui n'est pas le nôtre (label différent, plist illisible) n'est
    jamais supprimé : le service lève, le fichier reste.
    """
    _require_macos()
    path = plist_path(service, env)
    _bootout(service, run, env)
    if not os.path.exists(path):
        return False
    if not _ours(path, service):
        raise ServiceError(
            "%s is not the launchd agent of %s (label mismatch): not removed"
            % (path, service.label))
    os.unlink(path)
    return True


def status(service: Service, *, run: Callable = subprocess.run,
           env: Mapping[str, str] | None = None) -> dict:
    """L'état du service : installé, chargé, pid et état launchd."""
    _require_macos()
    path = plist_path(service, env)
    out = {"name": service.name, "label": service.label, "path": path,
           "installed": os.path.isfile(path), "loaded": False, "pid": None,
           "state": ""}
    if not out["installed"]:
        return out
    result = _launchctl(run, "print", "%s/%s" % (_domain(), service.label))
    if result.returncode != 0:
        return out
    out["loaded"] = True
    text = result.stdout or ""
    found = re.search(r"^\s*state = (\w+)\s*$", text, re.MULTILINE)
    if found:
        out["state"] = found.group(1)
    found = re.search(r"^\s*pid = (\d+)\s*$", text, re.MULTILINE)
    if found:
        out["pid"] = int(found.group(1))
    return out
