# SPDX-License-Identifier: AGPL-3.0-only
"""#6031 — relire la jauge Codex chez le fournisseur avant de tenir une pause.

Incident du 06/11 : les agents Codex sont restés en pause budget d'environ
00:33Z à 09:05Z sur un relevé à 100 % qui n'était plus la fenêtre courante.
Les agents en pause ne font pas de tour, donc ils ne publiaient aucun relevé
neuf : la pause s'entretenait jusqu'à ce qu'un humain lise la jauge chez le
fournisseur (0 %). Le défaut n'est pas la valeur du relevé au moment où il a
été produit, c'est qu'une pause pouvait rester fondée sur un relevé de
plusieurs heures sans jamais le rafraîchir.

Ce module fait la **relecture sans tour** que le fournisseur expose en lecture
seule : `codex app-server`, méthode JSON-RPC `account/rateLimits/read`. Aucun
tour, aucun jeton, aucun dépassement de forfait : la réponse porte les mêmes
fenêtres `primary`/`secondary` (`usedPercent`, `resetsAt`,
`windowDurationMins`) et les crédits que les journaux de session.

* `refresh` n'agit que sur un **relevé daté plus vieux que `PROBE_AFTER_S`**
  (une heure) et qui retient effectivement le compte (`held` ou overage) ;
  un relevé frais n'est jamais relu. Une tentative ratée n'est réessayée
  qu'après `PROBE_RETRY_S`, et les tentatives d'un même compte sont
  sérialisées dans le processus : un hôte ne mitraille pas le fournisseur.
* `probe` est en lecture seule et **ne lève jamais** : un binaire absent, un
  compte non connecté, un délai dépassé rendent None, et la garde de budget
  garde son comportement d'avant (pause sur le dernier relevé connu).
* Les jauges rendues sont datées (`read_at`/`produced_at` = maintenant) et
  `accounts.evaluate` les réécrit dans l'historique partagé, si bien que les
  autres hôtes voient la lecture fraîche comme n'importe quel relevé Codex.
"""
from __future__ import annotations

import os
import shutil
import subprocess
import threading
import time

from . import __version__, acp, config as config_mod
from .cost import Gauge, Gauges, Overage, _epoch, _fraction

#: âge au-delà duquel un relevé qui fonde une pause est relu au fournisseur
PROBE_AFTER_S = 3600.0
#: délai minimal entre deux tentatives (réussie ou non) pour un même compte
PROBE_RETRY_S = 600.0
#: délai d'une requête JSON-RPC (initialisation, puis lecture)
TIMEOUT_S = 8.0
#: harnais dont le fournisseur expose une lecture de jauge sans tour
SUPPORTED = ("codex",)
#: source publiée dans l'historique des jauges : l'hôte qui a lu
SOURCE = config_mod.hostname

_ATTEMPTS: dict[tuple[str, str], float] = {}
_ATTEMPTS_LOCK = threading.Lock()
#: dernière panne journalisée par compte : une erreur identique ne réécrit pas
#: le journal à chaque tentative (une sonde peut échouer toutes les 10 min)
_FAILURES: dict[tuple[str, str], str] = {}
#: une seule sonde à la fois par processus : pas de rafale de processus codex
_PROBE_LOCK = threading.Lock()


def supported(harness: str | None) -> bool:
    """Ce harnais a-t-il une lecture de jauge sans tour ?"""
    return (harness or "") in SUPPORTED


def _binary(environ=None) -> tuple[str, tuple]:
    """Le binaire `codex` de l'hôte et les dossiers à mettre en tête du PATH.

    Même résolution que l'exécuteur (`adapters.resolve_harness` : surcharge,
    variable, `harness_bins`, emplacements connus), pour ne pas sonder un
    autre binaire que celui des tours. Repli sur le PATH seul.
    """
    environ = os.environ if environ is None else environ
    for name in ("AMEESH_CODEX_BIN", "AGENT_MESH_CODEX_BIN"):
        value = str(environ.get(name) or "").strip()
        if value:
            return value, ()
    try:
        from . import adapters, harnesses
        descriptor = harnesses.get("codex")
        if descriptor is not None:
            resolution = adapters.resolve_harness(descriptor, env=environ)
            return resolution.path, tuple(resolution.path_prepend or ())
    except Exception:  # noqa: BLE001 - binaire introuvable : repli silencieux
        pass
    return (shutil.which("codex", path=environ.get("PATH") or os.defpath) or ""), ()


def _overage(snapshot: dict, gauges: list) -> Overage:
    """Les drapeaux d'overage de la réponse, comme `_codex_overage` du journal.

    `credits` en camelCase ici (`hasCredits`, `unlimited`) ; une incapacité
    n'est rendue que si elle est explicite, comme pour le journal (#1176 B1).
    """
    credits = snapshot.get("credits")
    if not isinstance(credits, dict):
        return Overage()
    has_credits = credits.get("hasCredits")
    unlimited = credits.get("unlimited")
    if has_credits is True or unlimited is True:
        capable: bool | None = True
    elif has_credits is False:
        capable = False
    else:
        capable = None
    return Overage(capable, capable is True and any(g.used >= 1.0 for g in gauges))


def gauges_from_response(result, now: float) -> Gauges | None:
    """Les jauges d'une réponse `account/rateLimits/read`, ou None.

    Une réponse sans fenêtre exploitable ne vaut pas un 0 % mesuré : None,
    la garde garde le relevé local.
    """
    if not isinstance(result, dict):
        return None
    # Le fournisseur dit explicitement si l'usage ordinaire est permis ; sinon
    # ses pourcentages ne doivent pas lever une pause (le schéma le précise :
    # on n'infère pas une reprise d'un pourcentage ou d'une remise à zéro).
    # Un champ ABSENT (version antérieure) garde le comportement d'avant.
    if "ordinaryUsageAllowed" in result and result.get("ordinaryUsageAllowed") is not True:
        return None
    snapshot = result.get("rateLimits")
    if not isinstance(snapshot, dict):
        by_id = result.get("rateLimitsByLimitId")
        snapshot = by_id.get("codex") if isinstance(by_id, dict) else None
    if not isinstance(snapshot, dict):
        return None
    gauges = []
    for slot in ("primary", "secondary"):
        window = snapshot.get(slot)
        if not isinstance(window, dict) or "usedPercent" not in window:
            continue
        try:
            minutes = int(window.get("windowDurationMins") or 0)
        except (TypeError, ValueError):
            minutes = 0
        key = "codex-%dmin" % minutes if minutes > 0 else "codex-%s" % slot
        gauges.append(Gauge(
            "codex", key, _fraction(window.get("usedPercent"), percent=True),
            _epoch(window.get("resetsAt")),
            float(minutes * 60) if minutes > 0 else 7 * 86400.0,
            read_at=now, produced_at=now, source=SOURCE()))
    if not gauges:
        return None
    return Gauges(gauges, _overage(snapshot, gauges))


def merge(local, fresh, *, now: float) -> Gauges:
    """La relecture fournisseur remplace les fenêtres qu'elle publie seulement.

    `account/rateLimits/read` n'expose pas toujours toutes les fenêtres du
    journal (Codex n'y publie que l'hebdomadaire) : jeter la fenêtre courte
    locale à 100 % parce que la réponse ne la porte pas lèverait la pause à
    tort. Une fenêtre absente de la réponse est donc conservée telle quelle ;
    une fenêtre publiée est remplacée. Les drapeaux d'overage sont recalculés
    sur l'union : une courte encore saturée reste un dépassement, une
    hebdomadaire remise à zéro ne l'est plus.
    """
    fresh_overage = getattr(fresh, "overage", None)
    fresh = list(fresh or ())
    if not fresh:
        return local
    replacement = {gauge.key: gauge for gauge in fresh}
    merged = []
    seen: set = set()
    for gauge in local:
        merged.append(replacement.get(gauge.key, gauge))
        seen.add(gauge.key)
    merged.extend(gauge for gauge in fresh if gauge.key not in seen)
    local_overage = getattr(local, "overage", None)
    capable = (fresh_overage.capable if fresh_overage is not None
               and fresh_overage.capable is not None
               else getattr(local_overage, "capable", None))
    in_use = capable is True and any(float(gauge.used or 0.0) >= 1.0
                                     for gauge in merged)
    return Gauges(merged, Overage(capable, in_use))


def probe(profile, *, environ=None, now: float | None = None,
          timeout: float = TIMEOUT_S) -> Gauges | None:
    """Lit la jauge du compte chez le fournisseur, sans tour ni dépense.

    None quand le harnais n'expose pas cette lecture, quand le binaire ou
    l'authentification du compte manque, ou quand le fournisseur ne répond
    pas dans le délai. Ne lève jamais : la garde de budget n'en dépend pas.
    """
    if not supported(getattr(profile, "harness", "")) \
            or getattr(profile, "kind", "") != "config_dir":
        return None
    now = time.time() if now is None else float(now)
    environ = os.environ if environ is None else environ
    binary, prepend = _binary(environ)
    if not binary:
        return None
    env = dict(environ)
    if prepend:
        env["PATH"] = os.pathsep.join(
            [*prepend, env.get("PATH") or os.defpath])
    # Le dossier du compte décide de l'authentification : jamais la clé
    # ambiante d'un autre compte.
    env["CODEX_HOME"] = profile.home(environ)
    env.pop("OPENAI_API_KEY", None)
    env.pop("CODEX_API_KEY", None)
    peer = None
    try:
        with _PROBE_LOCK:
            peer = acp.JsonRpcPeer(
                [binary, "app-server"], env=env, header=False,
                stderr=subprocess.DEVNULL, stoppable=True)
            peer.set_handlers(lambda method, params: {}, lambda method, params: None)
            peer.request("initialize", {
                "clientInfo": {"name": "ameesh-gauge-probe", "title": "ameesh",
                               "version": __version__},
            }, timeout=timeout)
            peer.notify("initialized")
            result = peer.request("account/rateLimits/read", {}, timeout=timeout)
    except Exception as exc:  # noqa: BLE001 - une sonde ne casse jamais la garde
        key = (getattr(profile, "harness", "?"), getattr(profile, "name", "?"))
        if _FAILURES.get(key) != type(exc).__name__:
            _FAILURES[key] = type(exc).__name__
            _log("[gauge-probe] %s/%s unreadable (%s)" % (
                key[0], key[1], type(exc).__name__))
        return None
    finally:
        if peer is not None:
            _close_peer(peer)
    return gauges_from_response(result, now)


def _close_peer(peer) -> None:
    """Ferme le processus et ses tuyaux (une sonde par heure ne fuit pas)."""
    try:
        peer.stop_reading()
        peer.close(grace=1.0)
    except Exception:  # noqa: BLE001 - fermeture au mieux
        pass
    proc = getattr(peer, "proc", None)
    if proc is None:
        return
    for stream in (getattr(proc, "stdout", None), getattr(proc, "stderr", None)):
        if stream is not None:
            try:
                stream.close()
            except OSError:
                pass


def _stamp(gauges, held: str | None) -> float | None:
    """L'instant de production du relevé qui retient le compte.

    `held` : la clé de la fenêtre qui bloque (une seule). Sans elle (overage),
    les fenêtres saturées ; à défaut toutes. None si aucune n'est datée —
    l'âge ne se prouve pas.
    """
    if held:
        gauges = [g for g in gauges if g.key == held] or list(gauges)
    elif hasattr(gauges, "spent"):
        gauges = gauges.spent()
    stamps = []
    for gauge in gauges:
        stamp = (gauge.produced_at if gauge.produced_at is not None
                 else gauge.read_at)
        if stamp:
            stamps.append(float(stamp))
    return max(stamps) if stamps else None


def _claim(profile, clock) -> bool:
    """Réserve une tentative pour ce compte, ou False si elle est trop récente."""
    key = (getattr(profile, "harness", ""), getattr(profile, "name", ""))
    now = clock()
    with _ATTEMPTS_LOCK:
        last = _ATTEMPTS.get(key)
        if last is not None and now - last < PROBE_RETRY_S:
            return False
        _ATTEMPTS[key] = now
        return True


def refresh(profile, gauges, *, held: str | None = None, environ=None,
            now: float | None = None, clock=time.monotonic) -> Gauges | None:
    """Relit la jauge si le relevé qui fonde la pause a plus de `PROBE_AFTER_S`.

    Rend les jauges fraîches (à réécrire dans l'historique par l'appelant), ou
    None quand il n'y a rien à relire, que la tentative est trop rapprochée,
    ou que la sonde échoue.
    """
    if not supported(getattr(profile, "harness", "")) \
            or getattr(profile, "kind", "") != "config_dir":
        return None
    now = time.time() if now is None else float(now)
    stamp = _stamp(gauges, held)
    if stamp is None or now - stamp < PROBE_AFTER_S:
        return None
    if not _claim(profile, clock):
        return None
    return probe(profile, environ=environ, now=now)


def last_reading_age(db, harness: str | None, account: str | None,
                     *, gauge: str | None = None,
                     now: float | None = None) -> float | None:
    """Âge du dernier relevé connu d'un compte (toutes sources), ou None.

    Sert à `budget_pause_long` : une pause qui dure sur un relevé ancien peut
    être une pause entretenue par ce relevé. Lecture facultative : une base
    sans historique, ou une lecture en échec, rend None.
    """
    if not harness:
        return None
    now = time.time() if now is None else float(now)
    try:
        from . import storage
        operations = storage.of(db).operations
        if account and account != "(default)":
            rows = operations.gauge_latest(harness=harness, account=account)
            if not rows:
                rows = [row for row in operations.latest_gauges(
                    since_s=30 * 86400.0) if row.get("harness") == harness]
        else:
            rows = [row for row in operations.latest_gauges(
                since_s=30 * 86400.0) if row.get("harness") == harness]
    except Exception:  # noqa: BLE001 - une alerte ne casse pas sur l'historique
        return None
    if gauge:
        # la fenêtre que nomme la pause : son âge, pas celui d'une autre
        # fenêtre relue plus récemment
        rows = [row for row in rows if row.get("key") == gauge] or rows
    stamps = []
    for row in rows:
        stamp = row.get("produced_ts")
        if stamp is None:
            stamp = row.get("observed_ts")
        if stamp:
            stamps.append(float(stamp))
    return max(0.0, now - max(stamps)) if stamps else None


def _log(message: str) -> None:
    from .runner import log_async
    log_async(message)
