# SPDX-License-Identifier: AGPL-3.0-only
"""Côté hôte d'un attach distant : bail, journal, page de conversation (#7121).

D2 (#7120) a livré le transport (`attach_host`, `attach_pty`,
`attach_transport`) ; D3 branche la session elle-même. Ce module porte les
petites pièces partagées entre `runner` (le wrapper `attach --host-attach`
lancé par la connexion sortante) et la CLI :

- **identité du bail** : `attach:<tenant>:<appareil>` (conception 5322
  §5.4), bornée, jamais un secret ;
- **événement `agent.attach`** : accepté ou refusé, avec l'humain du grant,
  l'hôte, le tenant, l'appareil et l'`attach_id` — jamais le reçu, le nonce
  ni la signature ;
- **page de conversation** : la dernière page du journal (A1) imprimée dans
  le PTY avant le harnais, pour que le poste la voie sans credential base ;
- **variables d'environnement** du wrapper : l'hôte les pose depuis le grant
  vérifié, le wrapper ne lit jamais un reçu dans l'environnement.

Aucun secret n'est journalisé : les valeurs ci-dessous sont des identités de
routage (hôte, tenant, humain, `attach_id`, `credential_id` d'appareil).
"""
from __future__ import annotations

import hashlib
import os
import re
import sys

#: posées par `attach_host.HostSession` après la vérification du grant
ENV_HUMAN = "AMEESH_ATTACH_HUMAN"
ENV_TENANT = "AMEESH_ATTACH_TENANT"
ENV_DEVICE = "AMEESH_ATTACH_DEVICE"
ENV_ID = "AMEESH_ATTACH_ID"
ENV_WAIT = "AMEESH_ATTACH_WAIT"

#: TTL du bail d'un attach distant, renouvelé par le wrapper. Court : si le
#: wrapper meurt sans rendre le bail (coupure, SIGKILL), le résident reprend
#: la session en au plus ce délai. Le renouvellement a lieu tous les TTL/3.
REMOTE_LEASE_TTL = 60.0

#: borne de la page de conversation affichée avant le harnais
HISTORY_PAGE = 12
HISTORY_LINE = 8 * 1024

#: une identité humaine du canon (`human:<id>`) ; même forme que
#: `agent_pause.HUMAN_RE`, sans dépendre de ce module.
HUMAN_RE = re.compile(r"^human:[A-Za-z0-9][A-Za-z0-9._@+-]{0,127}$")
#: caractères admis dans un `lease_owner` (l'identité de routage, pas un texte)
OWNER_RE = re.compile(r"^attach:[A-Za-z0-9_.@:=+~-]{1,240}$")
#: appareil du grant (credential_id `device:…`) ; `attach_host` impose déjà
#: cette forme dans sa trame, on la revalide avant d'écrire un bail.
DEVICE_RE = re.compile(r"^[A-Za-z0-9_:.=-]{1,1024}$")
TENANT_RE = re.compile(r"^[A-Za-z0-9][A-Za-z0-9._-]{0,127}$")


class SessionError(ValueError):
    """Entrée d'attach refusée avant tout effet (bail, journal, harnais)."""


def _device_label(device: str) -> str:
    """`<appareil>` du bail : le credential_id, borné sans collision.

    Un credential_id fait au plus 1 Kio ; le bail reste lisible et n'entre
    pas dans le texte de statut. Au-delà de 96 caractères, le préfixe est
    gardé et un digest court le distingue — deux appareils ne se confondent
    jamais.
    """
    label = str(device or "").strip()
    if not DEVICE_RE.fullmatch(label):
        raise SessionError("invalid attach device credential")
    if len(label) > 96:
        label = "%s~%s" % (label[:64], hashlib.sha256(
            label.encode("utf-8")).hexdigest()[:12])
    return label


def remote_owner(tenant: str, device: str) -> str:
    """`attach:<tenant>:<appareil>` (conception 5322 §5.4).

    Un hôte non partagé n'a pas de tenant : `-` le dit sans inventer un
    tenant. Le bail reste reconnaissable par `_reprise_attach` et par
    `attach_journal` (préfixe `attach:`, exactement comme l'attach local).
    """
    tenant = str(tenant or "").strip()
    if tenant and not TENANT_RE.fullmatch(tenant):
        raise SessionError("invalid attach tenant")
    owner = "attach:%s:%s" % (tenant or "-", _device_label(device))
    if not OWNER_RE.fullmatch(owner):
        raise SessionError("attach lease owner is not a routing identity")
    return owner


def human_from_env() -> str:
    """L'humain vérifié du grant, ou "" hors attach distant."""
    value = (os.environ.get(ENV_HUMAN) or "").strip()
    return value if HUMAN_RE.fullmatch(value) else ""


def wait_from_env() -> bool:
    return (os.environ.get(ENV_WAIT) or "").strip() == "1"


def metadata_from_env() -> dict:
    """Les champs posés par l'hôte, relus sans jamais faire confiance au texte."""
    tenant = (os.environ.get(ENV_TENANT) or "").strip()
    device = (os.environ.get(ENV_DEVICE) or "").strip()
    attach_id = (os.environ.get(ENV_ID) or "").strip()
    if tenant and not TENANT_RE.fullmatch(tenant):
        raise SessionError("invalid attach tenant in the host environment")
    if not DEVICE_RE.fullmatch(device):
        raise SessionError("invalid attach device in the host environment")
    if not re.fullmatch(r"att_[0-9a-z]{26}", attach_id):
        raise SessionError("invalid attach id in the host environment")
    return {"tenant": tenant, "device": device, "attach_id": attach_id,
            "human": human_from_env()}


def record_event(db, agent: str, *, outcome: str, code: str = "", reason: str = "",
                 owner: str = "", host: str = "", tenant: str = "", device: str = "",
                 human: str = "", attach_id: str = "", epoch: int = 0,
                 session: str = "") -> None:
    """Écrit `agent.attach` (accepté ou refusé), sans jamais bloquer la session.

    L'événement porte l'identité de routage et l'issue ; le reçu, le nonce et
    la signature d'appareil n'y entrent pas. Une base indisponible n'empêche
    ni le bail ni l'arrêt : l'échec est dit sur stderr (borné) et la session
    continue — le journal des grants (D1) reste la trace d'audit autoritaire.
    """
    from . import events
    data = {"agent": agent, "outcome": outcome, "mode": "attach"}
    for key, value in (("code", code), ("reason", reason), ("lease_owner", owner),
                       ("host", host), ("tenant", tenant), ("device", device),
                       ("human", human), ("attach_id", attach_id),
                       ("session", session)):
        if value not in (None, ""):
            data[key] = str(value)[:256]
    if epoch:
        data["lease_epoch"] = int(epoch)
    try:
        events.record(db, "agent.attach", "agent:" + agent, **data)
    except Exception as exc:  # noqa: BLE001 — journal non bloquant (§5.4)
        print("attach: agent.attach not recorded (%s)" % type(exc).__name__,
              file=sys.stderr)


def print_history(cfg, db, name: str, out=None) -> None:
    """Imprime la dernière page du journal dans le PTY, avant le harnais.

    Le poste n'a pas de credential base (§5.3) : c'est l'hôte qui lit le
    journal (A1) et pousse du texte, jamais un fichier ni une session. Borné
    (`HISTORY_PAGE` entrées, texte déjà borné par `conversation`), non
    bloquant : une base indisponible se dit en une ligne et la session part.
    """
    out = out if out is not None else sys.stdout
    try:
        from . import chat, conversation
        head = conversation.history(db, name, after=0, limit=1)
        last = int(head.get("last_seq") or 0)
        start = max(0, last - HISTORY_PAGE)
        page = conversation.history(db, name, after=start, limit=HISTORY_PAGE)
        entries = page.get("entries") or []
        out.write("\n— conversation avant l'attach (journal) —\n")
        if not entries:
            out.write("  (aucune entrée)\n")
        view = chat.ClientView(out)
        written = 0
        for entry in entries:
            view.entry(entry)
            written += len(entry.get("text") or "")
            if written > HISTORY_LINE:
                break
        if page.get("gap"):
            out.write("  … historique incomplet (rétention ou fil expiré)\n")
        out.write("— fin de la page ; le terminal passe au harnais —\n\n")
        out.flush()
    except Exception as exc:  # noqa: BLE001 — jamais bloquant
        try:
            out.write("\n(conversation indisponible : %s)\n\n" % type(exc).__name__)
            out.flush()
        except Exception:  # noqa: BLE001 — un PTY fermé n'arrête rien ici
            pass
