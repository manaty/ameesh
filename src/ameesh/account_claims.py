# SPDX-License-Identifier: AGPL-3.0-only
"""#2580 (L65b, décision 0033) : un compte de fournisseur déclaré par deux
meshes d'un même hôte.

Décision 0033 : un poste qui sert deux organisations fait tourner deux meshes,
« deux bases, deux jeux d'exécuteurs, deux jeux de comptes et de secrets ».
Le 10/10/2026, les deux meshes de pc-smichea déclaraient pourtant les mêmes
comptes Claude (primaire, secondaire, tertiaire) et Codex : quota commun,
sessions dans les mêmes dossiers. La base ne peut pas le voir — #1883 compare
les configurations d'UN mesh, et chaque mesh a sa base —, l'hôte si.

Registre local à l'hôte et à l'utilisateur (`account_registry`, par défaut
`~/.local/state/ameesh-host/accounts` — sous `$XDG_STATE_HOME` quand il est
posé), partagé par tous les meshes du
poste : un dossier par identifiant — l'empreinte de l'emplacement résolu
(`realpath`) du dossier de configuration (`config_dir`), du fichier de clé
(`key_file`) ou de la valeur de la clé (`key_env`) — et, dedans, une
revendication par mesh et par configuration :
`<empreinte du mesh>.<empreinte de la configuration>.json`.

L'identité du mesh est **lue en base** (`ameesh_events_cluster()`, sinon
`system_identifier` + `current_database()` + `current_schema()`) : jamais
dérivée du DSN. Deux façons de joindre la même base (pgbouncer, TCP direct)
sont le même mesh ; deux bases ou deux schémas sont deux meshes. Deux
configurations d'un même mesh (profils d'exécuteur, #1466) ne sont donc jamais
en conflit. Une revendication vit `CLAIM_WINDOW_S` après sa dernière écriture.

Les revendications sont écrites là où #1883 publie ses identités
(`_publish_identities`, donc par TOUT processus qui place un tour), et par
l'exécuteur au démarrage puis toutes les `REFRESH_S`. Une clé d'API n'est
jamais écrite : seule l'empreinte de son fichier résolu ou de sa valeur
compte. La simulation (L39) n'écrit rien.

Un identifiant revendiqué, vivant, par un AUTRE mesh est partagé :

* `report` (défaut) : signalé — alerte `account_shared`, ligne dans
  `ameesh doctor` et `ameesh accounts list` (« partagé avec un autre mesh de
  l'hôte ») ;
* `account_sharing: "strict"` (#313) ou `accounts_shared: "refuse"` (#2580,
  clé d'hôte à activer après la séparation de #65) : en plus, CE mesh refuse
  le compte — un problème du profil (`accounts.check`) : jamais choisi, jamais
  lancé. La politique est celle de chaque mesh : celui qui garde le compte
  reste en `report`, celui qui doit le rendre passe en `strict`/`refuse`.

**Machine** (#2593, N3 de la relecture de #2580) : une revendication porte
l'empreinte de la machine (`machine_hash` : `/etc/machine-id`, sinon le nom
d'hôte du système), jamais filtrée sur `host` de la configuration
(`AMEESH_HOST`) : deux meshes d'un même poste peuvent nommer l'hôte
différemment et se verraient sinon l'un l'autre ignorés. Le registre est déjà
local à la machine et à l'utilisateur ; l'empreinte ne défend que contre un
`XDG_STATE_HOME` (ou `account_registry`) partagé entre machines, où un même
chemin désigne deux identifiants distincts. `host` reste dans la revendication
pour l'affichage ; une revendication sans empreinte de machine (écrite avant
#2593) est encore comparée sur `host`.

Jamais de secret : le registre porte des empreintes, des noms de compte, le
nom (sans dossier) du fichier de configuration, l'hôte et la politique ;
jamais une clé, jamais un chemin, jamais le contenu d'un identifiant. Deux
dossiers distincts connectés au même compte du fournisseur ne se voient pas
(ameesh ne lit jamais un identifiant) : hors du registre, dit dans
COMPTES.md § 8.
"""
from __future__ import annotations

import hashlib
import json
import os
import re
import socket
import tempfile
import time

#: version du format : la v2 porte l'hôte et l'identité de mesh lue en base ;
#: les revendications v1 (clé L219 dérivée du DSN) sont ignorées.
SCHEMA = "ameesh-account-claim/2"
#: registre par défaut : le registre d'hôte historique de #313, sous le
#: dossier d'état XDG de l'utilisateur, jamais sous le `state_dir` d'un mesh
#: (deux meshes peuvent en avoir deux) ; voir `default_registry`.
DEFAULT_REGISTRY = "~/.local/state/ameesh-host/accounts"
#: vivacité d'une revendication : au-delà, le mesh qui l'a écrite ne tourne
#: plus avec cette configuration — elle n'est plus un partage
CLAIM_WINDOW_S = 3600.0
#: cadence de réécriture des revendications par l'exécuteur
REFRESH_S = 600.0

_CLAIM_RE = re.compile(r"^([0-9a-f]{16})\.([0-9a-f]{16})\.json$")
#: attribut porté par la connexion : l'identité du mesh ne change pas pendant
#: la vie d'un processus, une seule lecture de base suffit
_IDENTITY_ATTR = "_ameesh_account_claim_mesh"
#: dernière identité retenue par ce processus (empreinte de mesh), pour les
#: lecteurs qui n'ont pas de connexion sous la main (`accounts.profiles`)
_LAST_MESH: str | None = None
#: fichiers d'identité de la machine (systemd, puis dbus), lus dans l'ordre
MACHINE_ID_FILES = ("/etc/machine-id", "/var/lib/dbus/machine-id")
_MACHINE: str | None = None


def default_registry(environ=None) -> str:
    """Le registre par défaut, sous `$XDG_STATE_HOME` (absolu seulement ;
    sinon le défaut XDG `~/.local/state`)."""
    env = os.environ if environ is None else environ
    base = str(env.get("XDG_STATE_HOME") or "").strip()
    if not base or not os.path.isabs(os.path.expanduser(base)):
        base = "~/.local/state"
    return os.path.join(os.path.expanduser(base), "ameesh-host", "accounts")


def registry(cfg) -> str:
    path = str(getattr(cfg, "account_registry", "") or "") or default_registry()
    return os.path.abspath(os.path.expanduser(path))


def policy(cfg) -> str:
    """La politique effective de ce mesh : `refuse` (clé d'hôte #2580),
    `strict` (#313) ou `report` (défaut). La plus stricte gagne."""
    shared = str(getattr(cfg, "accounts_shared", "") or "").strip().lower()
    if shared == "refuse":
        return "refuse"
    strict = str(getattr(cfg, "account_sharing", "") or "").strip().lower()
    if strict == "strict":
        return "strict"
    return "report"


def refuses(cfg) -> bool:
    """Ce mesh refuse-t-il un compte partagé ? (défaut : non, il le signale)"""
    return policy(cfg) != "report"


def policy_name(cfg) -> str:
    """La clé de configuration qui porte le refus, pour les messages."""
    return {"refuse": "accounts_shared: refuse",
            "strict": "account_sharing: strict"}.get(policy(cfg), "report")


def _fingerprint(text: str) -> str:
    return hashlib.sha256(text.encode("utf-8")).hexdigest()


def machine_hash() -> str:
    """#2593 : l'empreinte stable de la machine, identique pour tous les
    meshes du poste quel que soit leur `host` : `/etc/machine-id` (jamais
    écrit en clair), sinon le nom d'hôte du système. Lue une fois par
    processus."""
    global _MACHINE
    if _MACHINE is None:
        ident = ""
        for path in MACHINE_ID_FILES:
            try:
                with open(path, encoding="ascii") as fh:
                    ident = fh.read().strip()
            except (OSError, ValueError):
                continue
            if ident:
                break
        if not ident:
            ident = "hostname:" + socket.gethostname()
        _MACHINE = _fingerprint("ameesh-account-claim/machine:" + ident)[:16]
    return _MACHINE


def mesh_hash(identity: str) -> str:
    """L'empreinte d'une identité de mesh — jamais l'identité en clair dans le
    registre (elle décrit l'infrastructure, pas un compte)."""
    return _fingerprint("ameesh-account-claim/2:mesh\x00" + str(identity))[:16]


def remember(identity: str | None) -> None:
    """Retient l'identité du mesh de CE processus (interne : un processus sert
    un mesh ; les tests s'en servent pour simuler un second mesh)."""
    global _LAST_MESH
    _LAST_MESH = mesh_hash(identity) if identity else None


def current_mesh() -> str | None:
    """L'empreinte du mesh retenue par ce processus, si elle est connue."""
    return _LAST_MESH


def _db_of(source):
    """La connexion d'un `db`, d'un domaine de stockage (`store.db`) ou None."""
    if source is None:
        return None
    if hasattr(source, "query"):
        return source
    return getattr(source, "db", None)


def _read_identity(db) -> str | None:
    """L'identité du mesh LUE EN BASE : le journal d'événements (cluster,
    timeline, journal), sinon `system_identifier` + base + schéma. Jamais le
    DSN : pgbouncer et TCP direct désignent le même mesh."""
    try:
        rows = db.query("select ameesh_events_cluster() as cluster")
        if rows and rows[0].get("cluster"):
            return str(rows[0]["cluster"])
    except Exception:  # noqa: BLE001 — fonction absente, droits, pilote médié
        pass
    try:
        rows = db.query(
            "select (select to_hex(system_identifier) from pg_control_system())"
            " as system_identifier, current_database() as database,"
            " current_schema() as schema")
        if rows:
            row = rows[0]
            if row.get("system_identifier"):
                return "db:%s:%s:%s" % (row["system_identifier"],
                                        row.get("database") or "",
                                        row.get("schema") or "")
    except Exception:  # noqa: BLE001 — droits sur pg_control_system
        pass
    # L109 : une connexion médiée n'a pas de SQL ; `events.bounds` est
    # l'opération du contrat qui porte déjà `ameesh_events_cluster()`.
    try:
        from . import storage
        cluster = storage.of(db).events.bounds().get("cluster")
        if cluster:
            return str(cluster)
    except Exception:  # noqa: BLE001 — base sans journal, session sans jeton
        pass
    return None


def mesh_identity(source=None) -> str | None:
    """L'identité brute du mesh de cette connexion (`source` : db ou store),
    lue une fois en base et retenue sur la connexion. None si elle est
    illisible : on n'écrit alors aucune revendication (mieux vaut ne pas
    détecter que d'attribuer un compte au mauvais mesh)."""
    db = _db_of(source)
    if db is None:
        return None
    cached = getattr(db, _IDENTITY_ATTR, None)
    if cached:
        remember(str(cached))
        return str(cached)
    identity = _read_identity(db)
    if identity:
        try:
            setattr(db, _IDENTITY_ATTR, identity)
        except (AttributeError, TypeError):
            pass
        remember(identity)
    return identity


def fingerprint(profile, environ=None) -> str | None:
    """L'empreinte de l'identifiant d'un compte : dossier résolu
    (`config_dir`), fichier de clé résolu (`key_file`), ou valeur de la
    variable (`key_env`) — jamais le chemin ni la valeur en clair. None quand
    il n'y a rien à comparer (clé par variable absente de l'environnement)."""
    environ = os.environ if environ is None else environ
    if profile.kind == "config_dir":
        return _fingerprint("dir\x00" + os.path.realpath(profile.home(environ)))
    if profile.kind == "api_key_env":
        if profile.key_file:
            return _fingerprint("file\x00" + os.path.realpath(profile.key_file))
        if profile.key_env:
            value = environ.get(profile.key_env)
            if value:
                return _fingerprint("env\x00" + value)
    return None


def _config_hash(cfg) -> str:
    path = str(getattr(cfg, "config_path", "") or "")
    return _fingerprint("config\x00" + (os.path.realpath(path) if path else ""))[:16]


def _claim_name(cfg, mesh: str) -> str:
    """`<empreinte du mesh>.<empreinte de la configuration>.json` : chaque
    configuration d'un mesh n'écrit et ne retire que la sienne."""
    return "%s.%s.json" % (mesh, _config_hash(cfg))


def _declared(cfg, environ=None) -> list[tuple[str, object]]:
    """(empreinte de l'emplacement, profil) des comptes de cette configuration
    qui ont un identifiant comparable. Configuration refusée : aucun (les
    alertes de compte disent déjà pourquoi)."""
    from . import accounts
    try:
        declares = accounts.parse(getattr(cfg, "accounts", None) or {})
    except accounts.AccountError:
        return []
    out = []
    for harness in sorted(declares):
        for profile in declares[harness]:
            where = fingerprint(profile, environ)
            if where:
                out.append((where, profile))
    return out


def _write(folder: str, name: str, data: dict) -> None:
    """Écriture atomique d'une revendication (0600), dossier recréé une fois
    s'il vient d'être retiré par un autre exécuteur."""
    for attempt in range(2):
        os.makedirs(folder, mode=0o700, exist_ok=True)
        try:
            os.chmod(folder, 0o700)
        except OSError:
            pass
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


def _claim_data(cfg, host: str, profile, now: float) -> dict:
    return {
        "schema": SCHEMA,
        "host": host,
        "machine": machine_hash(),
        "harness": profile.harness,
        "account": profile.name,
        "config": os.path.basename(str(getattr(cfg, "config_path", "") or "")),
        "policy": policy(cfg),
        "seen_at": round(now, 3),
    }


def claim(cfg, store, host: str, harness: str, items: list, *,
          now: float | None = None, environ=None) -> bool:
    """Écrit (ou rafraîchit) les revendications de CE processus pour les
    comptes qu'il vient de publier (#1883 `_publish_identities`). Retourne
    False si l'identité du mesh est illisible ; OSError si le registre n'est
    pas inscriptible. Aucune écriture pour la simulation : l'appelant ne nous
    appelle pas (L39)."""
    identity = mesh_identity(store)
    if identity is None:
        return False
    mesh = mesh_hash(identity)
    now = time.time() if now is None else now
    declared = [(empreinte, profile) for profile in items
                if (empreinte := fingerprint(profile, environ)) is not None]
    if not declared:
        return True
    root = registry(cfg)
    name = _claim_name(cfg, mesh)
    for empreinte, profile in declared:
        _write(os.path.join(root, empreinte), name,
               _claim_data(cfg, host, profile, now))
    return True


def refresh(cfg, *, db=None, store=None, mesh: str | None = None,
            now: float | None = None) -> bool:
    """Écrit les revendications de cette configuration, puis retire celles
    qu'elle a écrites pour un identifiant qu'elle ne déclare plus.

    `db`/`store` : la connexion qui porte l'identité du mesh ; `mesh` : une
    identité brute explicite (tests, second mesh simulé). False si l'identité
    du mesh est illisible. OSError si le registre n'est pas inscriptible :
    l'appelant le journalise (un partage n'est alors plus détecté, rien
    d'autre ne change)."""
    now = time.time() if now is None else now
    source = store if store is not None else db
    identity = mesh or mesh_identity(source)
    if identity is not None:
        mesh = mesh_hash(identity)
        remember(identity)
    elif source is None and _LAST_MESH is not None:
        # Un lecteur sans connexion (profils, affichage d'un processus déjà
        # identifié) reprend l'identité retenue. Avec une connexion muette, on
        # n'écrit pas : le mesh serait inconnu.
        mesh = _LAST_MESH
    else:
        return False
    root = registry(cfg)
    declared = _declared(cfg)
    if not declared and not os.path.isdir(root):
        return True
    os.makedirs(root, mode=0o700, exist_ok=True)
    try:
        os.chmod(root, 0o700)
    except OSError:
        pass
    mine = _claim_name(cfg, mesh)
    for empreinte, profile in declared:
        _write(os.path.join(root, empreinte), mine,
               _claim_data(cfg, getattr(cfg, "host", "") or "", profile, now))
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
    return True


def _claims(folder: str, host: str) -> list[dict]:
    """Les revendications lisibles d'un identifiant, écrites sur CETTE
    machine ; une illisible n'est pas une preuve de partage, elle est ignorée.
    `host` ne départage que les revendications d'avant #2593 (sans machine)."""
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
        machine = data.get("machine")
        if machine:
            if str(machine) != machine_hash():
                continue
        elif str(data.get("host") or "") != host:
            continue
        out.append({"mesh": match.group(1), "seen_at": seen_at,
                    "harness": str(data.get("harness") or ""),
                    "account": str(data.get("account") or ""),
                    "config": str(data.get("config") or ""),
                    "policy": str(data.get("policy") or "")})
    return out


def shared(cfg, *, db=None, store=None, mesh: str | None = None,
           now: float | None = None) -> list[dict]:
    """Les comptes de cette configuration dont l'identifiant est revendiqué,
    vivant, par un AUTRE mesh de l'hôte : `{harness, account, others}`, où
    `others` liste les revendications de ces meshes. Aucune écriture."""
    now = time.time() if now is None else now
    source = store if store is not None else db
    identity = mesh or mesh_identity(source)
    if identity is not None:
        mesh = mesh_hash(identity)
        remember(identity)
    elif source is None and _LAST_MESH is not None:
        mesh = _LAST_MESH
    else:
        return []
    root = registry(cfg)
    declared = _declared(cfg)
    if not declared or not os.path.isdir(root):
        return []
    host = str(getattr(cfg, "host", "") or "")
    out = []
    for empreinte, profile in declared:
        others = [claim for claim in _claims(os.path.join(root, empreinte), host)
                  if claim["mesh"] != mesh and now - claim["seen_at"] <= CLAIM_WINDOW_S]
        if others:
            out.append({"harness": profile.harness, "account": profile.name,
                        "others": others})
    return out


def describe(item: dict) -> str:
    """Les autres meshes d'un partage, en clair : fichier de configuration,
    début de l'empreinte du mesh, et leur refus éventuel."""
    return ", ".join(
        "%s (mesh %s%s)" % (claim["config"] or "unnamed configuration",
                            claim["mesh"][:8],
                            ", refused there"
                            if claim["policy"] in ("strict", "refuse") else "")
        for claim in item["others"])


def mark_refused(cfg, items: list, *, db=None, now: float | None = None,
                 shares: list | None = None) -> list:
    """Politique `strict`/`refuse` : les profils partagés avec un autre mesh
    portent la raison de leur refus (`Profile.refused`, lue par
    `accounts.check`). Toute autre politique : les profils tels quels."""
    if not items or not refuses(cfg):
        return items
    import dataclasses
    if shares is None:
        shares = shared(cfg, db=db, now=now)
    refus = {(item["harness"], item["account"]): item for item in shares}
    out = []
    for profile in items:
        item = refus.get((profile.harness, profile.name))
        if item is not None:
            profile = dataclasses.replace(profile, refused=(
                "credentials also declared by another mesh of this host: %s — "
                "refused here (%s, decision 0033)"
                % (describe(item), policy_name(cfg))))
        out.append(profile)
    return out


def doctor_lines(cfg, *, db=None, now: float | None = None) -> list[str]:
    """Les lignes d'`ameesh doctor` : un compte partagé avec un autre mesh de
    l'hôte, et la politique de CE mesh. Registre illisible : une ligne le dit
    (le médecin ne peut pas conclure à l'absence de partage)."""
    try:
        partages = shared(cfg, db=db, now=now)
    except (OSError, ValueError) as exc:
        return ["registry unreadable (%s): a shared account would not be seen" % exc]
    out = []
    for item in partages:
        out.append("%s/%s shared with another mesh of the host: %s (%s)" % (
            item["harness"], item["account"], describe(item),
            "refused here" if refuses(cfg) else "reported only"))
    return out
