# SPDX-License-Identifier: AGPL-3.0-only
"""#313 (L65b, décision 0033) : un compte de fournisseur déclaré par deux
meshes d'un même hôte.

Décision 0033 : un poste qui sert deux organisations fait tourner deux meshes,
« deux bases, deux jeux d'exécuteurs, deux jeux de comptes et de secrets ».
Le 10/10/2026, les deux meshes de pc-smichea déclaraient pourtant les mêmes
comptes Claude (primaire, secondaire, tertiaire) et Codex : quota commun,
sessions dans les mêmes dossiers. La base ne peut pas le voir — #1883 compare
les configurations d'UN mesh, et chaque mesh a sa base —, l'hôte si.

Registre local à l'hôte et à l'utilisateur (`account_registry`, défaut
`~/.local/state/ameesh-host/accounts`), partagé par tous les meshes du poste :
un dossier par identifiant — l'empreinte de l'emplacement résolu (`realpath`)
du dossier de configuration (`config_dir`) ou du fichier de clé (`key_file`)
— et, dedans, une revendication par mesh et par configuration :
`<clé du mesh>.<empreinte de la configuration>.json`. La clé du mesh est celle
de L219 (`housekeeping.mesh_key` : hôte, port, base et schéma du DSN, jamais
l'utilisateur ni le mot de passe) ; deux configurations d'un même mesh (profils
d'exécuteur, #1466) ne sont donc jamais en conflit. Une revendication vit
`CLAIM_WINDOW_S` après sa dernière écriture ; l'exécuteur réécrit les siennes
au démarrage puis toutes les `REFRESH_S`, et retire celles de SA configuration
pour un identifiant qu'elle ne déclare plus (configuration corrigée, relue à
chaud).

Un identifiant revendiqué, vivant, par un AUTRE mesh est partagé :

* `account_sharing: "report"` (défaut) : signalé — journal de l'exécuteur au
  démarrage et à chaque changement, alerte `account_shared` ;
* `account_sharing: "strict"` : en plus, CE mesh refuse le compte — un
  problème du profil (`accounts.check`) : jamais choisi, jamais lancé, et
  `ameesh accounts list` dit pourquoi. La politique est celle de chaque mesh :
  le mesh qui garde le compte reste en `report`, celui qui doit le rendre passe
  en `strict` ; deux meshes en `strict` le refusent tous deux.

Jamais de secret : le registre porte des empreintes, des noms de compte, le
nom (sans dossier) du fichier de configuration et la politique ; jamais une
clé ni le contenu d'un identifiant. Une clé d'API passée par variable
(`key_env`) n'a pas d'emplacement à comparer ; deux dossiers distincts
connectés au même compte du fournisseur ne se voient pas non plus (ameesh ne
lit jamais un identifiant) : hors du registre, dit dans COMPTES.md § 8.
"""
from __future__ import annotations

import hashlib
import json
import os
import re
import tempfile
import time

SCHEMA = "ameesh-account-claim/1"
#: registre par défaut : sous le dossier d'état par défaut de l'utilisateur,
#: jamais sous le `state_dir` d'un mesh (deux meshes peuvent en avoir deux)
DEFAULT_REGISTRY = "~/.local/state/ameesh-host/accounts"
POLICIES = ("report", "strict")
#: vivacité d'une revendication : au-delà, le mesh qui l'a écrite ne tourne
#: plus avec cette configuration — elle n'est plus un partage
CLAIM_WINDOW_S = 3600.0
#: cadence de réécriture des revendications par l'exécuteur
REFRESH_S = 600.0

_CLAIM_RE = re.compile(r"^([0-9a-f]{16})\.([0-9a-f]{16})\.json$")


def registry(cfg) -> str:
    path = str(getattr(cfg, "account_registry", "") or "") or DEFAULT_REGISTRY
    return os.path.abspath(os.path.expanduser(path))


def policy(cfg) -> str:
    return str(getattr(cfg, "account_sharing", "") or "report").strip().lower()


def location(profile) -> str | None:
    """L'emplacement résolu de l'identifiant d'un compte ; None pour une clé
    passée par variable (aucun emplacement à comparer)."""
    if profile.kind == "config_dir":
        return os.path.realpath(profile.home())
    if profile.key_file:
        return os.path.realpath(profile.key_file)
    return None


def _fingerprint(text: str) -> str:
    return hashlib.sha256(text.encode("utf-8")).hexdigest()


def _mesh(cfg) -> str:
    from .housekeeping import mesh_key
    return mesh_key(cfg)


def _claim_name(cfg) -> str:
    """`<clé du mesh>.<empreinte de la configuration>.json` : chaque
    configuration d'un mesh n'écrit et ne retire que la sienne."""
    path = str(getattr(cfg, "config_path", "") or "")
    config = _fingerprint(os.path.realpath(path) if path else "")[:16]
    return "%s.%s.json" % (_mesh(cfg), config)


def _declared(cfg) -> list[tuple[str, object]]:
    """(empreinte de l'emplacement, profil) des comptes de cette configuration
    qui ont un emplacement. Configuration refusée : aucun (les alertes de
    compte disent déjà pourquoi)."""
    from . import accounts
    try:
        declares = accounts.parse(getattr(cfg, "accounts", None) or {})
    except accounts.AccountError:
        return []
    out = []
    for harness in sorted(declares):
        for profile in declares[harness]:
            where = location(profile)
            if where:
                out.append((_fingerprint(where), profile))
    return out


def _write(folder: str, name: str, data: dict) -> None:
    """Écriture atomique d'une revendication (0600), dossier recréé une fois
    s'il vient d'être retiré par un autre exécuteur."""
    for attempt in range(2):
        os.makedirs(folder, mode=0o700, exist_ok=True)
        try:
            fd, tmp = tempfile.mkstemp(dir=folder, prefix=".claim-", suffix=".tmp")
        except FileNotFoundError:
            if attempt:
                raise
            continue
        try:
            with os.fdopen(fd, "w", encoding="utf-8") as fh:
                json.dump(data, fh, sort_keys=True)
            os.replace(tmp, os.path.join(folder, name))
        except BaseException:
            try:
                os.unlink(tmp)
            except OSError:
                pass
            raise
        return


def refresh(cfg, *, now: float | None = None) -> None:
    """Écrit les revendications de cette configuration, puis retire celles
    qu'elle a écrites pour un identifiant qu'elle ne déclare plus.

    OSError si le registre n'est pas inscriptible : l'appelant le journalise
    (un partage n'est alors plus détecté, rien d'autre ne change)."""
    now = time.time() if now is None else now
    root = registry(cfg)
    declared = _declared(cfg)
    if not declared and not os.path.isdir(root):
        return
    mine = _claim_name(cfg)
    config = os.path.basename(str(getattr(cfg, "config_path", "") or ""))
    os.makedirs(root, mode=0o700, exist_ok=True)
    for empreinte, profile in declared:
        _write(os.path.join(root, empreinte), mine, {
            "schema": SCHEMA, "harness": profile.harness, "account": profile.name,
            "config": config, "policy": policy(cfg), "seen_at": round(now, 3)})
    wanted = {empreinte for empreinte, _ in declared}
    for empreinte in os.listdir(root):
        if empreinte in wanted:
            continue
        try:
            os.unlink(os.path.join(root, empreinte, mine))
        except (FileNotFoundError, NotADirectoryError):
            continue
        try:
            os.rmdir(os.path.join(root, empreinte))
        except OSError:
            pass  # d'autres revendications y restent


def _claims(folder: str) -> list[dict]:
    """Les revendications lisibles d'un identifiant ; une illisible n'est pas
    une preuve de partage, elle est ignorée."""
    try:
        names = os.listdir(folder)
    except OSError:
        return []
    out = []
    for name in sorted(names):
        match = _CLAIM_RE.match(name)
        if not match:
            continue
        try:
            with open(os.path.join(folder, name), encoding="utf-8") as fh:
                data = json.load(fh)
            seen_at = float(data["seen_at"])
        except (OSError, ValueError, TypeError, KeyError):
            continue
        if not isinstance(data, dict) or data.get("schema") != SCHEMA:
            continue
        out.append({"mesh": match.group(1), "seen_at": seen_at,
                    "harness": str(data.get("harness") or ""),
                    "account": str(data.get("account") or ""),
                    "config": str(data.get("config") or ""),
                    "policy": str(data.get("policy") or "")})
    return out


def shared(cfg, *, now: float | None = None) -> list[dict]:
    """Les comptes de cette configuration dont l'identifiant est revendiqué,
    vivant, par un AUTRE mesh de l'hôte : `{harness, account, others}`, où
    `others` liste les revendications de ces meshes. Aucune écriture."""
    now = time.time() if now is None else now
    root = registry(cfg)
    declared = _declared(cfg)
    if not declared or not os.path.isdir(root):
        return []
    mesh = _mesh(cfg)
    out = []
    for empreinte, profile in declared:
        others = [claim for claim in _claims(os.path.join(root, empreinte))
                  if claim["mesh"] != mesh and now - claim["seen_at"] <= CLAIM_WINDOW_S]
        if others:
            out.append({"harness": profile.harness, "account": profile.name,
                        "others": others})
    return out


def describe(item: dict) -> str:
    """Les autres meshes d'un partage, en clair : fichier de configuration,
    début de la clé du mesh, et leur refus éventuel."""
    return ", ".join(
        "%s (mesh %s%s)" % (claim["config"] or "unnamed configuration",
                            claim["mesh"][:8],
                            ", refused there" if claim["policy"] == "strict" else "")
        for claim in item["others"])


def mark_refused(cfg, items: list, *, now: float | None = None) -> list:
    """`account_sharing: strict` : les profils partagés avec un autre mesh
    portent la raison de leur refus (`Profile.refused`, lue par
    `accounts.check`). Toute autre politique : les profils tels quels."""
    if not items or policy(cfg) != "strict":
        return items
    import dataclasses
    refus = {(item["harness"], item["account"]): item for item in shared(cfg, now=now)}
    out = []
    for profile in items:
        item = refus.get((profile.harness, profile.name))
        if item is not None:
            profile = dataclasses.replace(profile, refused=(
                "credentials also declared by another mesh of this host: %s — "
                "refused here (account_sharing: strict, decision 0033)" % describe(item)))
        out.append(profile)
    return out
