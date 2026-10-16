# SPDX-License-Identifier: AGPL-3.0-only
"""Notification de bureau (L66) : le SEUL endroit qui sait afficher une
notification selon le systeme.

Backends, dans l'ordre du plan de portabilite (etude Mac/Windows, L66) :

* Linux : `notify-send` (libnotify), comme avant L66 ;
* macOS : `osascript`, `display notification "<corps>" with title "<titre>"` ;
* Linux sous WSL2 : un toast Windows par `powershell.exe` (interop), car
  `notify-send` n'atteint pas le bureau Windows ;
* Windows natif : refuse (`NotAvailable`) ; le toast natif viendra avec L67.

`send()` rend le nom du backend utilise. `describe()` dit, sans rien
afficher, ce que cet OS utiliserait et ce qui manque : c'est ce que
`ameesh doctor --notify-test` publie (L66).

Le binaire de chaque backend se surcharge par `AMEESH_NOTIFY_SEND_BIN`,
`AMEESH_OSASCRIPT_BIN` ou `AMEESH_POWERSHELL_BIN` (test et diagnostic),
sinon `AMEESH_BIN_DIR/notify-send`, sinon le PATH — comme `gh` et les
harnais.
"""
from __future__ import annotations

import base64
import os
import re
import shutil
import subprocess
from xml.sax.saxutils import escape as _xml_escape_raw

from . import NotAvailable, SYSTEM

#: XML 1.0 forbids most C0 control characters even escaped, and AppleScript
#: does not want them raw either: an ESC copied from a terminal by an agent
#: made `LoadXml` fail on Windows (a NotifyError that looks permanent and
#: triggers useless retries). Tab, newline and carriage return stay allowed.
_FORBIDDEN_CONTROL = re.compile("[\x00-\x08\x0b\x0c\x0e-\x1f]")


def _xml_escape(value) -> str:
    """`value` escaped for XML, after dropping the forbidden controls."""
    return _xml_escape_raw(_FORBIDDEN_CONTROL.sub("", str(value)))

#: delai par defaut d'un affichage (une notification ne bloque pas un tour)
DEFAULT_TIMEOUT_S = 10.0

#: AppUserModelID de Windows PowerShell : un toast exige une application
#: enregistree, celle-ci existe sur tout Windows 10/11.
TOAST_APP_ID = ("{1AC14E77-02E7-4E5D-B744-2EB1AE5198B7}\\WindowsPowerShell"
                "\\v1.0\\powershell.exe")

#: noms des backends rendus par `send()` et `describe()`
BACKENDS = ("notify-send", "osascript", "powershell")

_MISSING_NOTIFY_SEND = (
    "notify-send not found on this host (libnotify package): the `desktop` "
    "channel cannot show anything; install it or remove `desktop` from the routes")
_MISSING_OSASCRIPT = (
    "osascript not found on this host (macOS): the `desktop` channel cannot "
    "show anything")
_MISSING_POWERSHELL = (
    "powershell.exe not found: WSL interop is off (or Windows interop is "
    "unavailable): the `desktop` channel cannot show a toast under WSL2")


class NotifyError(RuntimeError):
    """Echec d'affichage ; `permanent` = inutile de reessayer en l'etat."""

    def __init__(self, message: str, permanent: bool = False) -> None:
        self.permanent = permanent
        super().__init__(message)


def _clean(text, limit: int = 160) -> str:
    return " ".join(str(text or "").split())[:limit]


def _which(name: str, env: dict) -> str | None:
    return shutil.which(name, path=env.get("PATH") or os.defpath)


def _explicit(env: dict, key: str):
    """(defini, chemin) : une surcharge explicite est sans repli."""
    value = env.get(key)
    if not value:
        return False, None
    return True, (value if os.access(value, os.X_OK) else None)


def notify_send_bin(env: dict) -> str | None:
    """`AMEESH_NOTIFY_SEND_BIN` (explicite : sans repli), puis
    `AMEESH_BIN_DIR/notify-send`, puis le PATH."""
    defini, explicit = _explicit(env, "AMEESH_NOTIFY_SEND_BIN")
    if defini:
        return explicit
    bin_dir = env.get("AMEESH_BIN_DIR") or env.get("AGENT_MESH_BIN_DIR")
    if bin_dir:
        candidate = os.path.join(bin_dir, "notify-send")
        if os.access(candidate, os.X_OK):
            return candidate
    return _which("notify-send", env)


def osascript_bin(env: dict) -> str | None:
    """`AMEESH_OSASCRIPT_BIN` (explicite : sans repli), puis le PATH."""
    defini, explicit = _explicit(env, "AMEESH_OSASCRIPT_BIN")
    return explicit if defini else _which("osascript", env)


def powershell_bin(env: dict) -> str | None:
    """`AMEESH_POWERSHELL_BIN` (explicite : sans repli), puis le PATH
    (`powershell.exe`, puis `pwsh.exe`). `pwsh` sous Linux est exclu : ce
    PowerShell hors Windows ne charge pas les types WinRT du toast."""
    defini, explicit = _explicit(env, "AMEESH_POWERSHELL_BIN")
    if defini:
        return explicit
    for name in ("powershell.exe", "pwsh.exe"):
        found = _which(name, env)
        if found:
            return found
    return None


def is_wsl(env: dict | None = None) -> bool:
    """Linux sous WSL : le bureau a joindre est celui de Windows.

    Les variables que WSL pose (`WSL_DISTRO_NAME`, `WSL_INTEROP`) suffisent
    quand elles sont la ; sinon `/proc/version` nomme « microsoft »."""
    if SYSTEM != "linux":
        return False
    env = dict(os.environ if env is None else env)
    if env.get("WSL_DISTRO_NAME") or env.get("WSL_INTEROP"):
        return True
    try:
        with open("/proc/version", encoding="utf-8", errors="replace") as fh:
            return "microsoft" in fh.read().lower()
    except OSError:
        return False


def _plan(env: dict, system: str, wsl: bool | None):
    """(backend, binaire, probleme) pour ce systeme, sans rien afficher."""
    if system == "macos":
        return "osascript", osascript_bin(env), _MISSING_OSASCRIPT
    if system == "linux":
        if wsl is None:
            wsl = is_wsl(env)
        if wsl:
            return "powershell", powershell_bin(env), _MISSING_POWERSHELL
        return "notify-send", notify_send_bin(env), _MISSING_NOTIFY_SEND
    if system == "windows":
        problem = str(NotAvailable(
            "desktop notification",
            "native Windows toasts arrive with L67; use WSL2", system))
        return None, None, problem
    problem = str(NotAvailable("desktop notification",
                               "unknown system %r" % system, system))
    return None, None, problem


def describe(env: dict | None = None, system: str | None = None,
             wsl: bool | None = None):
    """Ce que cet OS utiliserait : `(backend, binaire, probleme)`.

    `probleme` est None quand l'affichage est possible ; sinon une phrase
    (binaire absent, OS non couvert) que `doctor --notify-test` publie et
    que `send()` leve en `NotifyError` permanente."""
    env = dict(os.environ if env is None else env)
    backend, binary, problem = _plan(env, system or SYSTEM, wsl)
    if binary:
        return backend, binary, None
    return backend, None, problem


def send(title: str, body: str, *, urgency: str = "normal",
         env: dict | None = None, timeout: float = DEFAULT_TIMEOUT_S,
         system: str | None = None, wsl: bool | None = None) -> str:
    """Affiche une notification sur le bureau de cet OS ; rend le backend.

    Leve `NotifyError` (`permanent` quand reessayer ne sert a rien)."""
    env = dict(os.environ if env is None else env)
    backend, binary, problem = _plan(env, system or SYSTEM, wsl)
    if not binary:
        raise NotifyError(problem or "no desktop backend on this system", permanent=True)
    if backend == "notify-send":
        _run([binary, "--app-name=ameesh", "--urgency=%s" % urgency,
              str(title), str(body)], env, timeout, "notify-send")
    elif backend == "osascript":
        _run([binary, "-e", _applescript(title, body, urgency)],
             env, timeout, "osascript")
    elif backend == "powershell":
        _run([binary, "-NoProfile", "-NonInteractive", "-Command", "-"],
             env, timeout, "powershell.exe",
             stdin=_toast_script(title, body, urgency))
    else:  # pragma: no cover - `_plan` ne rend que les trois backends
        raise NotifyError("unknown desktop backend %r" % backend, permanent=True)
    return backend


def _run(argv: list, env: dict, timeout: float, name: str, stdin: str | None = None) -> None:
    try:
        proc = subprocess.run(argv, input=stdin, capture_output=True, text=True,
                              timeout=timeout, env=env)
    except subprocess.TimeoutExpired:
        raise NotifyError("%s did not answer within %gs" % (name, timeout)) from None
    except OSError as exc:
        raise NotifyError("%s: %s" % (name, exc.strerror or exc)) from None
    if proc.returncode != 0:
        raise NotifyError("%s failed (code %d): %s" % (
            name, proc.returncode, _clean(proc.stderr) or "no message"))


def _applescript_string(value) -> str:
    """Une chaine AppleScript entre guillemets : echappe \\, ", et les sauts
    de ligne (`\\n` est compris dans une chaine AppleScript).

    Forbidden control characters are dropped before escaping (N2)."""
    text = _FORBIDDEN_CONTROL.sub("", str(value))
    text = text.replace("\\", "\\\\").replace('"', '\\"')
    text = text.replace("\r\n", "\n").replace("\r", "\n").replace("\n", "\\n")
    return '"%s"' % text


def _applescript(title, body, urgency: str) -> str:
    script = "display notification %s with title %s" % (
        _applescript_string(body), _applescript_string(title))
    if urgency == "critical":
        # `display notification` n'a pas de niveau d'urgence : un son pour
        # les alertes urgentes, rien pour les autres.
        script += ' sound name "Basso"'
    return script


def _toast_script(title, body, urgency: str) -> str:
    """Le script PowerShell (lu sur l'entree standard) qui affiche le toast.

    Le XML du toast est encode en base64 (UTF-8) ; le script lui-meme reste
    du pur ASCII, sur une seule ligne, sans aucun texte de l'appelant en
    clair. C'est une exigence de surete : PowerShell traite les apostrophes
    courbes (U+2018 a U+201B) comme des quotes simples — un « ’ » ecrit par
    un agent fermerait la chaine et la suite s'executerait — et il lit stdin
    dans la page de code OEM, qui abime les caracteres non ASCII ('—', 'é')."""
    scenario = ' scenario="urgent"' if urgency == "critical" else ""
    audio = '<audio silent="true"/>' if urgency == "low" else ""
    xml = ('<toast%s><visual><binding template="ToastGeneric"><text>%s</text>'
           '<text>%s</text></binding></visual>%s</toast>'
           % (scenario, _xml_escape(str(title)), _xml_escape(str(body)), audio))
    payload = base64.b64encode(xml.encode("utf-8")).decode("ascii")
    return (
        "$ErrorActionPreference = 'Stop';"
        "[Windows.UI.Notifications.ToastNotificationManager, Windows.UI.Notifications, "
        "ContentType = WindowsRuntime] | Out-Null;"
        "[Windows.Data.Xml.Dom.XmlDocument, Windows.Data.Xml.Dom.XmlDocument, "
        "ContentType = WindowsRuntime] | Out-Null;"
        "$xml = New-Object Windows.Data.Xml.Dom.XmlDocument;"
        "$xml.LoadXml([Text.Encoding]::UTF8.GetString("
        "[Convert]::FromBase64String('%s')));"
        "$toast = [Windows.UI.Notifications.ToastNotification]::new($xml);"
        "$notifier = [Windows.UI.Notifications.ToastNotificationManager]"
        "::CreateToastNotifier('%s');"
        "$notifier.Show($toast)" % (payload, TOAST_APP_ID))
