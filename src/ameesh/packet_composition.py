# SPDX-License-Identifier: AGPL-3.0-only
"""#5941 — un seul composeur de paquet à la fois, sur une base partagée.

Le 05/11/2026, l'alerte « créneau manqué » a désigné un agent (mty-amh-impl-01)
pendant que la minuterie `ameesh-paquet-horaire` composait aussi : deux
porteurs h-21 et h-22 pour la même unité #5925, deux ci-full de paquet, contre
le canon §11 bis (une seule CI de paquet à la fois). L'alerte disait « les
autres destinataires ne composent pas en parallèle », mais rien ne le faisait
respecter : le composeur horaire ne lit pas ce courrier, et le verrou
`flock` par nom de paquet (#4329) ne voit que le **même** nom, dans le
**même** clone.

Ce module porte le verrou de composition unique, partagé par les deux
chemins :

* la minuterie (`scripts/paquet-horaire.py`) le prend après la lecture des
  candidats, relit les porteurs **sous le verrou**, puis crée le ou les
  porteurs (une paire parallèle #3256 C reste un seul composeur) ;
* `ameesh work add` le prend avant de créer un porteur de paquet, relit les
  porteurs ouverts sous le verrou, et refuse la création si un autre compose.

Le verrou vit dans `merge_locks` (la table de baux déjà partagée entre hôtes,
avec TTL et époque), sous une clé distincte de la cible d'atterrissage. Il
n'est donc pas un `flock` local : les deux hôtes et les deux clones voient le
même bail. Un processus mort le libère à l'échéance, comme les autres baux.

Le chemin de la minuterie transmet son propriétaire à `work add`
(`--composition-owner`) : la création d'un second porteur de la même paire
parallèle est alors permise — c'est le même composeur qui la décide — mais
tout autre composeur reste refusé.

#6563 — le verrou couvre aussi la **composition d'un porteur existant**. Le
09/11/2026 (h-17, #6562), l'alerte de créneau a fait reprendre par impl-01 un
porteur que le tic venait de créer : impl-01 a composé 4 unités dans son clone
et publié `cd5d5e859b57` à 16:50Z, pendant que le tic, qui ne voyait que son
`flock` local, relançait une composition de 7 unités sur le même porteur
(`92f21b21f4db`, poussée refusée). Le bail de création ci-dessus ne couvrait
que « décider et créer » ; une fois le porteur ouvert, deux clones pouvaient
le composer.

Un second bail, de clé **par porteur** (`packet-compose:carrier:<projet>:<id>`,
distincte de la clé projet qui sérialise la création), est donc tenu pendant
toute la composition réelle (`scripts/paquet-auto.py`). Il est renouvelé par
le détenteur (TTL cinq minutes) et libéré à la fin ; un processus mort le
laisse expirer. Le tic le lit avant de reprendre un porteur (`follow`,
`resume`, `attempt`) et s'arrête si un autre compose. La clé par porteur
laisse vivre la paire parallèle #3256 C, dont les deux compositions sont
indépendantes.
"""
from __future__ import annotations

import os
import re
import socket
import uuid

from . import storage

#: préfixe de branche d'un porteur de paquet (même convention que
#: `packet_watch.CARRIER_PREFIX` et `scripts/paquet-horaire.py`)
CARRIER_BRANCH_PREFIX = "integration/paquet"
#: titre d'un porteur horaire, même expression que `scripts/paquet-horaire.py`
HOURLY_TITLE_RE = re.compile(r"^Paquet horaire (h-\d{8}-\d+)\b")
#: états d'un porteur encore ouvert (même lecture que `packet_watch`)
OPEN_STATES = ("intake", "build", "qa", "validated", "blocked", "waiting_human")
#: durée du bail de composition : la section critique couvre la relecture des
#: porteurs et la création (un `work add` peut lire des voisins et un dépôt),
#: pas la CI. Un processus tué laisse donc au plus cinq minutes de silence.
TTL_S = 300.0
#: clé du bail dans `merge_locks` ; le projet suffit : deux cibles d'un même
#: projet ne composent jamais en parallèle (le canon §12 n'en prévoit qu'une).
KEY = "packet-compose:%s"
#: #6563 : clé du bail d'un **porteur** pendant sa composition réelle. Elle
#: est distincte de la clé projet (création) pour que la paire parallèle
#: #3256 C garde deux compositions indépendantes, et plus fine pour qu'un
#: composeur qui reprend un porteur existant soit vu du tic.
CARRIER_KEY = "packet-compose:carrier:%s:%d"


class CompositionError(RuntimeError):
    """Le verrou de composition est tenu ou illisible : aucun porteur créé."""


def _branch(value) -> str:
    """`origin/main`, `refs/remotes/origin/main` et `main` nomment `main`."""
    name = str(value or "").strip()
    for prefix in ("refs/heads/", "refs/remotes/origin/", "origin/"):
        if name.startswith(prefix):
            return name[len(prefix):]
    return name


def is_carrier(*, branch="", title="") -> bool:
    """La création est-elle celle d'un porteur de paquet ?

    La branche `integration/paquet*` est la marque durable lue par le reste
    du système ; le titre horaire est accepté seul, comme dans la veille
    (#2601) et le tic, pour un porteur qui n'a pas encore de branche.
    """
    if _branch(branch).startswith(CARRIER_BRANCH_PREFIX):
        return True
    return bool(HOURLY_TITLE_RE.match(str(title or "").strip()))


def _row_is_carrier(row: dict) -> bool:
    return is_carrier(branch=str(row.get("branch") or ""),
                      title=str(row.get("title") or ""))


def key(project: str, carrier: int | None = None) -> str:
    """La clé du bail de composition d'un projet, ou d'un porteur (#6563)."""
    if carrier is None:
        return KEY % (project or "")
    return CARRIER_KEY % (project or "", int(carrier))


def owner(role: str = "paquet-horaire", host: str = "", pid: int = 0) -> str:
    """Une identité de composeur unique (hôte:pid), vérifiable par `work add`."""
    return "%s:%s:%d:%s" % (role or "paquet", host or socket.gethostname(),
                           int(pid or os.getpid()), uuid.uuid4().hex[:12])


def holder(db, project: str, *, carrier: int | None = None) -> dict | None:
    """Le bail de composition vivant du projet (ou du porteur), ou None.

    Lecture de diagnostic, jamais une garde : la garde est la prise du bail.
    """
    if db is None:
        return None
    return storage.of(db).work.land_holder(key(project, carrier))


def carriers(db, project: str, target: str = "") -> list[int]:
    """Les porteurs de paquet **ouverts** du projet (et de la cible, si donnée).

    Un porteur fermé ou fusionné ne réserve pas la composition : le tic et la
    veille le traitent déjà ainsi (#4171, #5786). Une cible absente sur le
    porteur est conservée (le doute réserve), comme dans `packet_watch`.
    """
    if db is None:
        return []
    wanted = _branch(target)
    found: list[int] = []
    for state in OPEN_STATES:
        for row in storage.of(db).work.items(state=state, assignee=None, limit=None):
            if (row.get("app") or "") not in ("", project):
                continue
            if not _row_is_carrier(row):
                continue
            declared = str(row.get("branch_target") or "")
            if wanted and declared and _branch(declared) != wanted:
                continue
            found.append(int(row["id"]))
    return sorted(set(found))


class Lease:
    """Le bail de composition d'un projet, ou d'un porteur (#6563), dans
    `merge_locks`.

    `acquire` est non bloquant : un second composeur s'arrête tout de suite.
    `release` ne rend que le bail de ce propriétaire et de cette époque.
    ``carrier`` choisit la clé par porteur : la composition réelle d'un
    porteur existant, distincte de la clé projet qui sérialise la création.
    """

    def __init__(self, db, project: str, *, owner: str, ttl_s: float = TTL_S,
                 carrier: int | None = None):
        self.db = db
        self.project = project or ""
        self.carrier = None if carrier is None else int(carrier)
        self.key = key(self.project, self.carrier)
        self.owner = str(owner)
        self.ttl_s = float(ttl_s)
        self.epoch: int | None = None

    @property
    def held(self) -> bool:
        return self.epoch is not None

    def acquire(self) -> bool:
        """Prend le bail ; False quand un autre composeur le tient."""
        epoch = storage.of(self.db).work.land_lock(self.key, self.owner, self.ttl_s)
        self.epoch = int(epoch) if epoch is not None else None
        return self.epoch is not None

    def renew(self) -> bool:
        """Prolonge le bail tenu, à la même époque ; False s'il est perdu.

        Le détenteur d'une composition réelle peut durer plus que `TTL_S`
        (tests par unité) : il renouvelle, et un processus tué laisse le bail
        expirer au plus tard dans `TTL_S`.
        """
        epoch = self.epoch
        if epoch is None:
            return False
        try:
            got = storage.of(self.db).work.land_lock(
                self.key, self.owner, self.ttl_s, epoch=epoch)
        except Exception:  # noqa: BLE001 - un renouvellement rompu est un bail perdu
            got = None
        if got is None:
            self.epoch = None
            return False
        return True

    def verify(self, owner: str | None = None) -> None:
        """Le bail est-il encore tenu par `owner` (ou par ce composeur) ?

        Appelé par `ameesh work add --composition-owner` : la minuterie peut
        créer deux porteurs de la même paire, mais seulement tant qu'elle
        tient le bail ; un bail expiré ou pris ailleurs refuse la création.
        """
        wanted = str(owner or self.owner)
        current = storage.of(self.db).work.land_holder(self.key)
        if current is None or str(current.get("owner") or "") != wanted:
            raise CompositionError(
                "composition lock %s is not held by %s: no carrier created"
                % (self.key, wanted))
        if wanted == self.owner:
            self.epoch = int(current.get("epoch") or 0) or None

    def release(self) -> bool:
        """Rend le bail de ce propriétaire et de cette époque."""
        epoch, self.epoch = self.epoch, None
        if epoch is None:
            return False
        return bool(storage.of(self.db).work.land_unlock(self.key, self.owner, epoch))

    def describe(self) -> str:
        """« hôte:pid » du détenteur, pour le refus ; le bail reste la preuve."""
        current = storage.of(self.db).work.land_holder(self.key)
        return str((current or {}).get("owner") or "verrou de composition tenu")

    def close(self) -> None:
        close = getattr(self.db, "close", None)
        if close:
            close()
