#!/usr/bin/env python3
# SPDX-License-Identifier: AGPL-3.0-only
"""Paquet horaire déclenché par minuterie, sans agent (#3845, #1606 point 1).

    scripts/paquet-horaire.py tick --repo ~/ameesh-composeur --target main
    scripts/paquet-horaire.py recompose --repo ~/ameesh-composeur --carrier N \
        --exclude LOT [--exclude-reason TEXTE] [--wait DURÉE [--cancel-ci]] \
        [--expect-head SHA]
    scripts/paquet-horaire.py pause --until 20:15 --reason "staging hors créneau"
    scripts/paquet-horaire.py resume | status

`scripts/paquet-auto.py` (#3807) compose sans jugement, mais un agent devait
encore le lancer chaque heure, créer le lot porteur, poser « verdict non
requis », demander la ci-full, puis, sur un rouge, trouver l'unité fautive et
recomposer (h-32 : bisect à la main pour #3779). Ce script fait ces gestes, un
**tic** à la fois, sans état local : tout ce qu'il sait vient de la base
(lots, journal du porteur) et de la forge, si bien qu'un tic perdu, un hôte
arrêté ou un bail repris par un autre candidat ne laisse rien en suspens.

Avant tout geste, le tic pose les migrations **additives** de la tête que la
base partagée n'a pas encore (#4075) : sinon le code de la tête lit des
colonnes absentes. Une migration non additive en attente, ou une pose refusée
(droit DDL) : aucun geste, un lot d'empreinte (la cause notée au lot, #5097), le
pilote et le bureau prévenus. Un verrou non obtenu (`lock_timeout`) est d'abord
réessayé une fois dans le même tic.

Un tic (la minuterie `deploy/systemd/ameesh-paquet-horaire.timer`, sous
`ameesh role run`, ne le lance que sur l'hôte qui détient le bail) :

1. **Réservation (§12).** Un porteur de paquet ouvert sur la cible (branche
   `integration/paquet-*` ou titre « Paquet horaire h-… ») la réserve : le tic
   suit ce porteur s'il est à lui (étapes 3-4), sinon ne fait rien.
2. **Composition, au moins une fois par heure.** Cible libre et aucun porteur
   horaire créé dans l'heure locale (fuseau du canon compilé) dont la fusion
   ne soit pas vue (#4171 : un porteur fusionné libère le créneau, un porteur
   abandonné ou remplacé sans CI active le libère aussi, #5786) : la liste
   calculée est lue
   (`paquet-candidats.py` depuis un worktree à la tête exacte de la cible) ;
   sans lot prêt, rien n'est créé. Sinon le lot porteur « Paquet horaire
   h-AAAAMMJJ-NN » est créé et `paquet-auto.py` compose, pousse et gèle.
   #6891 : si la cible avance pendant la composition (le commit d'atterrissage
   d'une coupure, par exemple), `paquet-auto.py` relit la référence #3782
   **avant** les contrats — les tests locaux les plus longs — et sort en
   `auto.TARGET_MOVED_RC` : le refus est journalisé et la composition repart
   aussitôt sur la nouvelle tête, sans attendre le tick suivant. La relecture
   du gel juste avant la poussée reste en place (#6566).
3. **Publication.** Sur la tête gelée sans demande `ci-full` : « verdict non
   requis » **seulement** si aucune unité n'est une résolution (fusions
   propres, §2 : aucun avis) — avec une résolution le geste reste au pilote
   (#1979) et le courrier le dit ; puis `ameesh ci run <porteur> --full`, qui
   arme la fusion automatique (`packet_automerge`, tic de release).
4. **Rouge.** À la composition, un test ciblé rouge **d'un module voisin**
   est situé en le rejouant sur les préfixes de l'empilement (#4217) : la
   première unité où il apparaît est retirée, son auteur prévenu, et
   `paquet-auto.py` recompose sans elle dans le même lancement — la tentative
   n'est pas perdue. Les rouges propres et les contrats gardent la lecture
   #3801 (`packet_red`) : l'unité désignée par une preuve forte est retirée
   (#3931). Sans attribution sûre (rejeu des préfixes illisible, contrat
   rouge), `paquet-auto.py` ne retire rien, prévient le pilote et garde son
   échec antérieur ; ce rouge du contenu sort en `COMPOSITION_RED_RC`, déjà
   rapporté au pilote, sans lot d'empreinte §16 (#6272). Sur la ci-full rouge de la tête
   gelée : un rouge **déterministe** (chaque échec de test propre au paquet
   attribué par #3801 à une seule unité probable, rejoué rouge en local sur la
   tête) retire ces unités ; le paquet est recomposé sans elles
   (`paquet-auto.py --force --exclude --keep-composition`), **une fois**, et
   l'auteur de chaque unité retirée reçoit un courrier. La recomposition
   reprend les membres de la composition précédente moins les exclus, à leur
   source composée : un lot prêt entre-temps, un membre regelé (nouvelle tête)
   ou sorti de la liste prête va à la coupure suivante, nommé aux écartés du
   manifeste (#4581). Chaque lot retiré repasse en `build`
   avec sa trace (`paquet-horaire: exclu …` : paquet, porteur, run, tests ;
   #4028), sans consommer de boucle d'auteur (#4043) : il attend le correctif
   de son assigné jusqu'à un nouveau gel, et l'alerte SLO vise l'auteur, pas
   le pilote. Un rouge non concluant (§3 : run annulé ou
   hors délai, échec de job sans test, échec non attribuable ou ambigu, test
   vert en local, erreur de fixture DB en QueryCanceled/timeout) ne retire
   rien : il est consigné au journal du porteur et le pilote est prévenu, une
   fois. #4221 : un QueryCanceled/lock timeout du **corps** d'un test qui se
   reproduit au rejeu seul, hors charge, est un rouge réel et retire l'unité.
5. **Reprise.** Chaque lancement de `paquet-auto.py` laisse d'abord une
   marque « tentative » (étape, tête de départ, hôte:pid, époque du bail) ;
   son achèvement se lit à la tête gelée qui a bougé, son échec à une marque
   « échec ». Une tentative morte (échec, processus disparu sur cet hôte,
   époque d'un autre hôte différente de celle du tic — son `packet record`
   est clôturé par l'époque, #4087 —, ou, sans époque, délai de composition
   dépassé ailleurs) est relancée sur le même porteur avec
   `--force`, au plus 3 fois, puis le pilote est prévenu une fois. Un porteur
   créé sans tentative (arrêt entre `work add` et la marque) est repris de même.
   #4237 : si la tentative morte a laissé une **tête testée** (refus de geste
   après des tests verts, `paquet-auto: tête testée …` au journal), la reprise
   la publie (`paquet-auto.py --publish`) sans recomposer ; une chaîne refusée
   (cible bougée) recompose dans le même tic.
   #4329 : un verrou `flock` par nom de paquet, tenu par `paquet-auto.py` de
   `clear_local_branch` à la fin, est relu avant toute reprise. Une composition
   lancée hors du tic est ainsi « en cours » (`composition en cours
   (hôte:pid)`) au lieu de laisser le tic supprimer son worktree vivant ; le
   verrou tombe avec le processus, une tentative morte reste reprise.
   #5941 : ce `flock` ne voit qu'un **même nom** dans un **même clone**. Le
   verrou de composition partagé (bail `merge_locks` sur la base) est pris
   après la lecture des candidats, puis les porteurs sont relus dessous avant
   la création ; `ameesh work add` prend le même verrou pour un porteur de
   paquet, si bien que l'agent désigné par l'alerte « créneau manqué » et la
   minuterie ne peuvent pas créer deux porteurs (h-21/h-22 du 05/11).
   #6563 : le bail projet de #5941 ne couvre que la création. Sur un porteur
   **existant**, le tic lit le bail de composition du porteur (clé
   `packet-compose:carrier:<projet>:<n°>`, tenu par `paquet-auto` pendant toute
   composition réelle) avant `attempt`, `resume`, `follow` et la recomposition
   du pilote : un agent désigné qui compose le même porteur dans son clone est
   vu, et le tic ne relance rien (h-17 du 09/11, #6562). Sans tête gelée ni
   marque de tentative, une note `packet composition recorded` récente ou une
   tête de branche poussée sans gel bloque aussi la relance : c'est au pilote
   de trancher, jamais au tic de réécrire la tête d'un autre composeur.
   #4501 : la vie d'une tentative sur cet hôte se lit au **pid vivant**, jamais
   à la ligne de commande (une recomposition peut tourner sous un autre nom) ;
   et si une tête poussée du porteur (gelée, ou vue en avance) a une ci-full en
   cours, la reprise attend sa conclusion au lieu de recomposer et d'écraser
   son manifeste.
6. Un courrier au pilote à chaque étape (tête, unités, écartés, CI).

**Recomposition du pilote** (#4237) : `paquet-horaire.py recompose --carrier N
--exclude LOT [--exclude-reason TEXTE] [--wait DURÉE [--cancel-ci]]
[--expect-head SHA]` au lieu de `paquet-auto.py --force` à la main (#5696 :
`--wait` attend la fin d'une tentative vivante ou d'une ci-full au lieu de
refuser, `--cancel-ci` l'annule d'abord et exige `--wait`, `--expect-head` ne
recompose que si la tête gelée est encore celle-là et refuse un porteur sans
gel) : la marque « tentative » (hôte, pid, époque de la session, motifs) est
posée d'abord, si bien qu'un tic du détenteur courant reprend une recomposition
perdue à la relève du bail comme les siennes. Elle ne compte pas dans le
retrait automatique unique du point 4. `--repo` est le clone **dédié** du
composeur (comme le tic), jamais le dossier de travail d'un agent : la branche
locale `integration/**` y serait lue par le balayage de l'assigné comme une
tête de la forge (#4562).

Comme le retrait automatique, elle est **bornée par `--keep-composition`**
(`this.base`, la tête gelée du paquet rouge) : elle reprend les membres de la
composition précédente moins les exclus, chacun à sa source composée ; un lot
prêt entre-temps, un membre regelé ou sorti de la liste prête va à la coupure
suivante, nommé aux écartés. La **preuve parallèle** (#4319, `parallel-proof`),
qui rejoue la ci-full ordinaire sur le même porteur, passe par le même chemin
et la même borne. Une recomposition qui a publié une tête — ou l'a publiée
puis retirée — n'est jamais lue comme « rien composé » : l'abandon du porteur
exige la preuve que la branche porte encore sa tête de départ ; à défaut, il
reste ouvert et un agent lit (#6561).

#3256 C : quand la médiane d'attente `qa_merge` de la veille dépasse
`paquet.attente_mediane_s` (bloc du canon compilé, lu avec `ameesh debit
--json --no-jobs`), le tic ouvre **deux** porteurs dans la même heure. Les
lots prêts sont partagés par `ameesh.packet_parallel` : aucun fichier modifié
commun, et deux lots dont l'un dépend de l'autre dans le même groupe. Chaque
porteur compose avec `paquet-auto.py --only <son groupe>` ; les deux CI
partent ensemble et `packet_automerge` fusionne le second après le premier,
sans refaire la suite complète quand la fusion de sa tête dans la cible
avancée est propre et les contrôles de gel verts. Un conflit le rend à la
file. Sous le seuil, ou si les lots ne sont pas disjoints, un seul paquet
part.

**Pause (#4361, #5045).** `pause --until … --reason …` suspend le tic (staging hors
créneau) sans arrêter la minuterie : `role timers --check` reste vert et nomme
la pause. L'échéance est obligatoire (au plus 12 h) ; la pause vit au journal
partagé (`packet.pause`, `ameesh.packet_pause`), pas sur l'hôte : un bail qui
change de main la garde. `--await-announcement` transforme l’échéance en alerte,
relancée chaque heure tant que l’annonce manque (#5057) :
la pause reste jusqu’à `resume --source "auteur, date, message"` (canon §7). Pendant la pause, le tic ne fait aucun geste (ni
migration ni composition) et le dit ; sans attente d’annonce, à l'échéance il
reprend seul ; `resume` lève avant. Pose et levée sont dites au pilote et au bureau.
Une tentative déjà partie relit la pause avant de pousser (#5084,
`paquet-auto.py --pause-project`) : posée entre-temps, elle arrête la tentative
sans poussée, notée « non concluant … pause », non décomptée.

**Cohérence de version (#4473).** Le tic exécute le script de la cible exacte
(`check_fresh`) ; `main()` met `src/` de ce même clone en tête de `sys.path`
et de `PYTHONPATH`, si bien que sa bibliothèque `ameesh`, la CLI et les
migrations viennent de la cible, jamais du venv installé. Un hôte revenu en
arrière ne fait donc plus échouer l'import d'un module récent (`packet_pause`)
par le script de main.

Ce script ne fusionne jamais : la fusion reste celle de `packet_automerge`
sur la tête gelée, CI requise verte. Les marques d'idempotence sont des notes
`paquet-horaire: …` au journal du porteur.

Codes de retour : 0 tic fait (y compris « rien à faire »), 1 échec
opérationnel ou panne du composeur (empreinte §16), 2 entrée invalide,
3 code périmé (refus attendu), 5 paquet rouge par son contenu (contrat, test
d'une unité, gel, types clients) : le pilote est prévenu, sans lot d'empreinte
§16 (#6272).
"""
from __future__ import annotations

import argparse
import importlib.util
import json
import os
import re
import shutil
import socket
import subprocess
import sys
import tempfile
import time
from dataclasses import dataclass, field
from datetime import datetime, timedelta
from pathlib import Path
from zoneinfo import ZoneInfo

TITLE_PREFIX = "Paquet horaire "
HOURLY_RE = re.compile(r"^Paquet horaire (h-(\d{8})-(\d+))\b")
CARRIER_BRANCH_PREFIX = "integration/paquet"
OPEN_STATES = ("intake", "build", "qa", "validated", "blocked", "waiting_human")
#: #4171 : fusion vue d'un porteur (miroir de `work_lifecycle.DELIVERED` et
#: des raisons de clôture livrée de `work.CLOSE_REASONS`).
MERGED_STATES = ("merged", "promoted", "delivered")
MERGED_CLOSE_REASONS = ("delivered", "done")
#: #4506 : la note de fusion automatique du porteur porte la tête publiée
MERGED_NOTE_RE = re.compile(
    r"^auto-merge merged repo=\S+ pr=\d+ sha=(?P<sha>[0-9a-f]{40})\b")
#: #4506 : `paquet-auto` gèle lui-même la tête poussée dès la composition (#4304)
EARLY_PUSH_RE = re.compile(
    r"^paquet \S+ poussé par scripts/paquet-auto\.py dès la composition\b")
MARK = "paquet-horaire: "
MARK_CI = MARK + "ci demandée "
MARK_RED = MARK + "rouge consigné "
#: #6665 : les états d'un run de ci-full encore vivant (ou pas encore relevé)
RUN_IN_FLIGHT = ("running", "queued", "pending", None)
MARK_WAIT = MARK + "verdict au pilote "
#: #6933 : l'acteur du tic n'a pas le geste « verdict non requis » (`work
#: reviewer --no-verdict` : humains, orchestrateurs, agents de conception)
MARK_WAIVE_LEFT = MARK + "dispense laissée au pilote "
#: la marque de `work reviewer --no-verdict` (storage/postgres/work.py)
REVIEWER_NONE = "-none"
WAIVE_REFUSED_RE = re.compile(r"designation refused: .* is neither a human")
MARK_ATTEMPT = MARK + "tentative "
MARK_FAILED = MARK + "échec "
MARK_STUCK = MARK + "reprise épuisée "
#: #6563 : une unité enregistrée sur le porteur (`work packet record`) avant
#: tout gel prouve une composition en cours ou poussée par un autre.
COMPOSITION_RECORD_PREFIX = "packet composition recorded: "
#: #4245 : un refus de la garde d'époque (EP4) n'est pas un échec de
#: composition (§3, non concluant) : il ne consomme pas de tentative
MARK_FENCED = MARK + "non concluant "
MARK_EXCLUDED = MARK + "exclu "
#: #6891 : la référence #3782 a été relue avant les tests locaux ; la cible a
#: avancé, rien n'est poussé et la composition repart sur la nouvelle tête.
MARK_TARGET = MARK + "cible avancée avant tests "
#: #6561 : une composition a publié une tête (ou l'a publiée puis retirée) ;
#: l'abandon du porteur exige la preuve qu'aucune tête neuve n'est sur la
#: branche. À défaut, le porteur reste ouvert et un agent lit.
MARK_PUBLISHED = MARK + "tête publiée, abandon refusé "
#: #6561 : les sorties de `paquet-auto` qui n'ont composé aucune unité sans
#: publier. « aucune unité après recomposition » n'en fait pas partie : une
#: composition a été enregistrée, puis toutes ses unités retirées.
EMPTY_COMPOSITION_RE = re.compile(
    r"^(aucun lot prêt|aucune unité composée)", re.MULTILINE)
WITHDRAWN_COMPOSITION_RE = re.compile(
    r"^aucune unité après recomposition", re.MULTILINE)
COMPOSITION = "composition"
RECOMPOSITION = "recomposition"
ATTEMPT_RE = re.compile(
    r"^paquet-horaire: tentative (composition|recomposition) n=(\d+) base=(\S+) "
    r"par=(\S+):(\d+)(?: run=(\S+) sans=([\d,]*))?(?: epoque=(\d+))?"
    r"(?: publie=(\S+))?(?: motifs=(\[.*\]))?$")
#: #4237 : le `run=` d'une recomposition demandée par le pilote (sous-commande)
PILOT_RUN = "pilote"
FAILED_RE = re.compile(r"^paquet-horaire: échec (composition|recomposition) n=(\d+) base=(\S+)")
FENCED_RE = re.compile(
    r"^paquet-horaire: non concluant (composition|recomposition) n=(\d+) base=(\S+)")
#: #4245 : le refus de `pilot_gesture` d'une session dont l'époque n'est plus
#: celle du bail (et que la lignée au même hôte ne couvre pas)
FENCE_REFUSAL_RE = re.compile(r"refused for \S+: this session is fenced by the lease epoch")
#: #4245 : relances après refus de garde pour une même étape, au-delà
#: desquelles un agent lit le journal (une garde qui refuse toujours)
MAX_FENCED = 3
MAX_RECOMPOSITIONS = 1
#: #4201 : motif écrit au manifeste pour le retrait automatique d'une unité dont
#: la ci-full attribuée est rouge (`paquet-auto` seul écrit le motif neutre).
EXCLUDE_CI_FULL_REASON = "retiré : ci-full rouge attribuée à son unité (#3801)"
#: lancements de paquet-auto pour une même étape (même tête de départ)
MAX_ATTEMPTS = 3
#: un porteur sans tentative plus jeune que ce délai : création en cours
CREATION_GRACE = 300
#: #4113, mesure auditeur #153989 du 28/10 : médiane 164 min, 112 paquets.
CARRIER_ESTIMATE = "165m"
CARRIER_ESTIMATE_SOURCE = "auditeur #153989, 28/10/2026 : médiane 164 min sur 112 paquets (#4113)"
CREATED_RE = re.compile(r"^lot #(\d+) created", re.MULTILINE)
SCRIPT_DIR = Path(__file__).resolve().parent
#: #4473 : la bibliothèque du même arbre que le script (voir `use_tree_library`)
TREE_SRC = SCRIPT_DIR.parent / "src"
LIST_LIMIT = 300
COMMAND_TIMEOUT = 600
COMPOSE_TIMEOUT = 3 * 3600
#: au-delà, une tentative lancée sur un autre hôte sans époque est réputée morte
ATTEMPT_STALE = COMPOSE_TIMEOUT + 900
#: #5696 : pas de `recompose --wait` (le relevé de `ci status` suit la forge
#: avec retard : une ci-full annulée y reste « in_progress » un moment)
WAIT_POLL = 30
#: #5696 : sortie du code périmé (refus attendu, sans alerte) ; 1 reste la panne
STALE_RC = 3
#: #6272 : sortie d'un tic dont le paquet est rouge par son contenu (contrat,
#: test d'une unité, gel, types clients) ; le pilote est prévenu, aucun lot
#: d'empreinte §16 ne s'ouvre. Miroir de `paquet-auto.COMPOSITION_RED_RC`
#: (vérifié par les tests).
COMPOSITION_RED_RC = 5
REPLAY_TIMEOUT = 900
#: poids #3801 d'une preuve forte (fichier du test, liste, migration) :
#: `packet_red.attribute_tests` rend `best` = poids de la meilleure preuve
STRONG_EVIDENCE = 4


class HoraireError(RuntimeError):
    """Un geste a échoué ; le message porte l'état atteint."""


class InputError(HoraireError):
    """Un argument invalide."""


class Stale(HoraireError):
    """#5696 : code exécuté qui n'est plus celui de la cible ; refus attendu
    (sortie STALE_RC, sans alerte), pas une panne."""


class CompositionRed(HoraireError):
    """#6272 : la composition est rouge par son contenu (contrat, test d'une
    unité), pas par une panne du composeur : le pilote est déjà prévenu et le
    tic sort en COMPOSITION_RED_RC sans ouvrir de lot d'empreinte §16."""


class Busy(InputError):
    """#5696 : refus passager (tentative vivante, ci-full en cours) ;
    `recompose --wait` l'attend au lieu de sortir en 2."""

    def __init__(self, message: str, run_id: str = ""):
        super().__init__(message)
        self.run_id = run_id


def _load_auto():
    """`paquet-auto.py` en module : mêmes lecteurs de manifeste et de tests."""
    spec = importlib.util.spec_from_file_location(
        "paquet_auto", SCRIPT_DIR / "paquet-auto.py")
    module = importlib.util.module_from_spec(spec)
    sys.modules.setdefault("paquet_auto", module)
    spec.loader.exec_module(module)
    return module


auto = _load_auto()


def use_tree_library(src: Path | None = None) -> str:
    """#4473 : le script et sa bibliothèque viennent du même arbre.

    Le tic exécute le script de la cible (`check_fresh`), mais l'unité le
    lançait avec le Python du venv **installé** : après un retour arrière de
    l'hôte, le script de main importait une bibliothèque plus ancienne
    (`packet_pause` absent) et sortait en « pause illisible », sans composer.
    On met donc `src/` de l'arbre du script en tête de `sys.path` et dans
    `PYTHONPATH` : les enfants (`ameesh` CLI, migrations, outils) héritent de
    la même bibliothèque. Le staging reste la version **déployée** (§14) :
    le tic est l'outil qui compose la cible, il doit lire le code de la cible
    exacte que `check_fresh` vient de vérifier, pas celui d'un venv en retard
    ou revenu en arrière.

    Rend le chemin chargé, ou "" si `src/ameesh` manque ou si un `ameesh`
    déjà importé vient d'ailleurs : dans ce cas l'appelant refuse le geste
    plutôt que de mélanger les deux versions.
    """
    root = (Path(src) if src is not None else TREE_SRC).resolve()
    if not (root / "ameesh" / "__init__.py").is_file():
        return ""
    path = str(root)
    if path in sys.path:
        sys.path.remove(path)
    sys.path.insert(0, path)
    parts = [part for part in (os.environ.get("PYTHONPATH") or "").split(os.pathsep)
             if part and part != path]
    os.environ["PYTHONPATH"] = os.pathsep.join([path, *parts])
    import ameesh
    loaded = getattr(ameesh, "__file__", "") or ""
    if not loaded or Path(loaded).resolve().parent.parent != root:
        return ""
    return path


# ---------------------------------------------------------------------------
# Commandes (injectables pour les tests)
# ---------------------------------------------------------------------------


@dataclass
class Result:
    returncode: int
    stdout: str = ""
    stderr: str = ""

    @property
    def text(self) -> str:
        return (self.stdout + self.stderr).strip()


class Shell:
    """Lance les commandes ; les tests la remplacent par un faux."""

    def run(self, cmd: list[str], *, cwd: Path | None = None,
            input_text: str | None = None,
            timeout: int = COMMAND_TIMEOUT) -> Result:
        try:
            proc = subprocess.run(
                cmd, cwd=str(cwd) if cwd else None,
                input=input_text, text=True, capture_output=True,
                stdin=None if input_text is not None else subprocess.DEVNULL,
                timeout=timeout)
        except FileNotFoundError:
            return Result(127, "", "commande introuvable : %s" % cmd[0])
        except subprocess.TimeoutExpired:
            return Result(124, "", "délai de %s s dépassé : %s" % (timeout, cmd[0]))
        return Result(proc.returncode, proc.stdout or "", proc.stderr or "")


@dataclass
class Context:
    repo: Path
    target: str
    project: str
    pilot: str
    actor: str
    shell: Shell
    python: str = sys.executable
    dry_run: bool = False
    #: fuseau des coupures, lu au canon compilé (`mechanics.timezone`, #2449)
    zone: ZoneInfo = ZoneInfo("UTC")
    #: #4271 : `packet_tests` du canon compilé (borne d'une série d'unité,
    #: séries simultanées, #4463 borne totale) ; None : bloc absent, les
    #: défauts de paquet-auto
    unit_tests: object = None
    contract_publication: str | None = None  # compiled project policy
    now: float = field(default_factory=time.time)
    host: str = field(default_factory=socket.gethostname)
    pid: int = field(default_factory=os.getpid)
    #: époque du bail du tic (`AMEESH_LEASE_EPOCH`, `ameesh role run`) ; 0 : inconnue
    epoch: int = 0
    #: #3256 C : le bloc `paquet` du canon compilé (`attente_mediane_s`,
    #: `max_paquets`) ; None = aucun second paquet (bloc absent).
    parallel: object | None = None
    #: l'inventaire calculé du tic, gardé pour ne pas relire la cible
    candidates_cache: dict = field(default_factory=dict)
    #: #4361 : la pause active de la cible (`packet_pause`), None sans pause ;
    #: None ici : aucune lecture (tests)
    pause_of: object = None
    #: #5941 : fabrique le bail de composition (base partagée) ; None dans les
    #: bancs sans base, où le verrou n'est pas joué. La fabrique est appelée
    #: après la lecture des candidats, jamais avant (section critique courte).
    composition: object = None
    #: #6563 : lit le bail de composition d'un porteur existant (clé par
    #: porteur, tenue par `paquet-auto`) ; None dans les bancs sans base.
    carrier_claim: object = None
    #: #4562 : le clone de recomposition a déjà été vérifié (jamais le dossier
    #: de travail d'un agent) ; la CLI le vérifie avant `check_fresh`
    clone_checked: bool = False
    log: list[str] = field(default_factory=list)
    #: #5696 : horloge et attente de `recompose --wait` (les tests les remplacent)
    clock: object = time.monotonic
    sleep: object = time.sleep
    #: #5696 : la tête dont le code a été chargé (premier `check_fresh`) ; un
    #: clone avancé ensuite par un autre processus ne rend pas ce code frais
    executed_head: str = ""

    def alive(self, pid: int) -> bool:
        """Un processus vit-il encore sous ce pid, sur cet hôte ? (#4501)

        La ligne de commande ne prouve rien : une recomposition peut tourner
        sous un autre nom (copie `scripts/.h12-tick.py`, wrapper de session) et
        un pid vivant jugé mort fait recomposer le même porteur en parallèle.
        On lit donc l'état du processus (`/proc/<pid>/stat`, un zombie ne
        compte pas) ; hors Linux (`/proc` absent), `os.kill(pid, 0)`. Un pid
        recyclé reste un doute, jamais une fausse mort : le verrou de
        composition et le bail tranchent.
        """
        if pid <= 0:
            return False
        if os.path.isdir("/proc"):
            try:
                text = Path("/proc/%d/stat" % pid).read_text(
                    encoding="ascii", errors="replace")
            except FileNotFoundError:
                return False
            except OSError:
                # procfs lisible en général mais pas pour ce pid (droits,
                # course) : ne pas conclure à une mort sans le dire.
                return self._alive_by_signal(pid)
            # champ 3 = état ; le nom (champ 2) peut contenir espaces et
            # parenthèses, on coupe après la dernière.
            try:
                state = text.rsplit(")", 1)[1].split()[0]
            except (IndexError, ValueError):
                return True
            return state != "Z"
        return self._alive_by_signal(pid)

    @staticmethod
    def _alive_by_signal(pid: int) -> bool:
        try:
            os.kill(int(pid), 0)
        except ProcessLookupError:
            return False
        except PermissionError:
            return True
        except OSError:
            return False
        return True

    def say(self, text: str) -> None:
        self.log.append(text)
        print(text, flush=True)

    def ameesh(self, *args: str, input_text: str | None = None,
               timeout: int = COMMAND_TIMEOUT) -> Result:
        return self.shell.run(["ameesh", *args], cwd=self.repo,
                              input_text=input_text, timeout=timeout)

    def must(self, *args: str, what: str, timeout: int = COMMAND_TIMEOUT) -> Result:
        result = self.ameesh(*args, timeout=timeout)
        if result.returncode != 0:
            raise HoraireError("%s : rc %d : %s"
                               % (what, result.returncode, auto._tail(result.text, 20)))
        return result

    def git(self, *args: str, timeout: int = COMMAND_TIMEOUT) -> Result:
        return self.shell.run(["git", "-C", str(self.repo), *args], timeout=timeout)

    def note(self, lot: int, text: str) -> None:
        if self.dry_run:
            self.say("[essai] note #%d : %s" % (lot, text))
            return
        self.must("work", "note", str(lot), text, "--actor", self.actor,
                  what="note au porteur #%d" % lot)

    def mail(self, to: str, lot: int, body: str) -> None:
        if self.dry_run:
            self.say("[essai] courrier à %s (#%d) : %s" % (to, lot, body.splitlines()[0]))
            return
        # #3845 : `ameesh role run` retire AGENT_MAIL_NAME ; l'expéditeur est
        # l'acteur (le rôle), nommé explicitement.
        result = self.shell.run(["agent-mail", "send", to, "-", "--lot", str(lot),
                                 "--from", self.actor], input_text=body, timeout=120)
        if result.returncode != 0:
            # Un courrier perdu ne défait pas le geste : il est dit au journal.
            self.say("courrier à %s non envoyé : %s" % (to, auto._tail(result.text, 5)))


# ---------------------------------------------------------------------------
# Lectures
# ---------------------------------------------------------------------------


def _json(result: Result, what: str):
    try:
        return json.loads(result.stdout)
    except ValueError as exc:
        raise HoraireError("%s : JSON illisible : %s" % (what, auto._tail(result.text, 5))) from exc


def open_lots(ctx: Context) -> list[dict]:
    rows: dict[int, dict] = {}
    for state in OPEN_STATES:
        result = ctx.must("work", "list", "--json", "--state", state,
                          "--limit", str(LIST_LIMIT), what="lots %s" % state)
        for row in _json(result, "work list") or []:
            rows[int(row["id"])] = row
    return list(rows.values())


def recent_lots(ctx: Context) -> list[dict]:
    result = ctx.must("work", "list", "--json", "--limit", str(LIST_LIMIT),
                      what="lots récents")
    return _json(result, "work list") or []


def is_carrier(row: dict, project: str, target: str) -> bool:
    if (row.get("app") or "") not in ("", project):
        return False
    branch = str(row.get("branch") or "")
    title = str(row.get("title") or "")
    if not branch.startswith(CARRIER_BRANCH_PREFIX) and not HOURLY_RE.match(title):
        return False
    aimed = str(row.get("branch_target") or "")
    return aimed in ("", target)


def is_hourly(row: dict) -> bool:
    return bool(HOURLY_RE.match(str(row.get("title") or "")))


def hour_start(now: float, zone: ZoneInfo) -> float:
    local = datetime.fromtimestamp(now, zone)
    return local.replace(minute=0, second=0, microsecond=0).timestamp()


def merged_seen(row: dict) -> bool:
    """#4171 : la fusion du porteur est-elle vue (état livré, ou clos livré) ?"""
    state = str(row.get("state") or "")
    return state in MERGED_STATES or (
        state == "closed" and str(row.get("close_reason") or "") in MERGED_CLOSE_REASONS)


def merged_note(item: dict, sha: str) -> bool:
    """#4506 : une note `auto-merge merged` porte sur la tête gelée `sha`."""
    if not sha:
        return False
    for ev in item.get("events") or []:
        match = MERGED_NOTE_RE.match(str(ev.get("note") or ""))
        if match and match.group("sha") == sha:
            return True
    return False


def merged_head(ctx: Context, item: dict, sha: str) -> bool:
    """#4506 : le porteur est-il déjà fusionné dans la cible ?

    `merged_seen` ne lit que l'état du lot ; un porteur fusionné par `ameesh`
    reste ouvert (qa) jusqu'à sa clôture (hold du pilote, balayage de contenu).
    La tête gelée ancêtre de `origin/<cible>` ou la note `auto-merge merged`
    du journal disent la fusion avant l'état.
    """
    if merged_seen(item):
        return True
    if not sha:
        return False
    if merged_note(item, sha):
        return True
    result = ctx.git("merge-base", "--is-ancestor", sha, "origin/" + ctx.target)
    return result.returncode == 0


def freeze_foreign(item: dict, since: float) -> bool:
    """#4506 : le gel postérieur à `since` vient-il d'une autre main ?

    `paquet-auto` gèle lui-même la tête qu'il vient de pousser dès la
    composition (#4304) ; cette note est la sienne. Un gel postérieur avec une
    autre note (geste manuel, dépannage après échec) est la main d'un autre.
    Sans jalon lisible (les bancs peuvent l'omettre, `work show` le porte), on
    ne conclut pas.
    """
    declared = (item.get("freeze") or {}).get("declared") or {}
    if float(declared.get("at_ts") or 0) <= since:
        return False
    ident = int(declared.get("id") or 0)
    if not ident:
        return False
    for milestone in item.get("milestones") or []:
        if int(milestone.get("id") or 0) == ident:
            return not EARLY_PUSH_RE.match(str(milestone.get("note") or ""))
    return False


def slot_served(rows: list[dict], now: float, zone: ZoneInfo, project: str,
                target: str, ctx: Context | None = None) -> bool:
    """Un porteur horaire créé dans l'heure locale courante consomme-t-il le créneau ?

    Une fusion vue libère le créneau (#4171). Avec le contexte du tic,
    un abandon/remplacement explicite le libère aussi si la CI est arrêtée
    (#5786). Sans contexte, cette dernière preuve manque : on reste prudent.
    """
    start = hour_start(now, zone)
    return any(is_hourly(row) and is_carrier(row, project, target)
               and float(row.get("created_ts") or 0) >= start
               and not merged_seen(row)
               and not (ctx is not None and released_carrier(ctx, row))
               for row in rows)


def released_carrier(ctx: Context, row: dict) -> bool:
    """#5786 : abandon explicite sans CI active, même sur une ancienne tête.

    Une lecture de CI impossible remonte l'erreur sans autoriser de paquet.
    """
    if row.get("state") != "closed" or row.get("close_reason") not in (
            "abandoned", "superseded"):
        return False
    return not any(run.get("status") in (
        None, "running", "queued", "pending", "in_progress", "waiting", "requested")
        for run in ci_status(ctx, int(row["id"])).get("runs") or [])


def next_name(ctx: Context) -> str:
    """h-AAAAMMJJ-NN, NN = suivant le plus grand du jour (branches d'origin)."""
    day = datetime.fromtimestamp(ctx.now, ctx.zone).strftime("%Y%m%d")
    pattern = "refs/heads/%sh-%s-*" % (auto.PACKAGE_PREFIX, day)
    result = ctx.git("ls-remote", "origin", pattern, timeout=120)
    if result.returncode != 0:
        raise HoraireError("ls-remote : %s" % auto._tail(result.text, 5))
    numbers = [int(found) for found in re.findall(
        r"paquet-h-%s-(\d+)\s*$" % day, result.stdout, re.MULTILINE)]
    # Les branches abandonnées peuvent déjà avoir été supprimées.
    for row in recent_lots(ctx):
        match = HOURLY_RE.match(str(row.get("title") or ""))
        if match and match.group(2) == day:
            numbers.append(int(match.group(3)))
    return "h-%s-%02d" % (day, (max(numbers) + 1) if numbers else 1)


def show(ctx: Context, lot: int) -> dict:
    return _json(ctx.must("work", "show", str(lot), "--json",
                          what="lot #%d" % lot), "work show")


def marks(item: dict) -> list[str]:
    return [str(ev.get("note") or "") for ev in item.get("events") or []
            if str(ev.get("note") or "").startswith(MARK)]


def frozen_sha(item: dict) -> str:
    freeze = item.get("freeze") or {}
    declared = freeze.get("declared") or {}
    return str(declared.get("sha") or "").strip().lower()


def ci_status(ctx: Context, lot: int) -> dict:
    return _json(ctx.must("ci", "status", str(lot), "--json",
                          what="ci status #%d" % lot), "ci status")


def run_for(report: dict, sha: str) -> dict | None:
    """Le run le plus récent de la tête gelée (`ci status` liste du plus récent)."""
    for run in report.get("runs") or []:
        if str(run.get("head_sha") or "").lower() == sha:
            return run
    return None


def forge_completed(ctx: Context, run_id: str) -> bool:
    """#5696 : la forge dit-elle ce run terminé ? `ci status` suit GitHub avec
    10 à 40 s de retard (constat du pilote, 03/11 : run annulé « completed »
    à la forge, encore « en cours » au relevé). Une lecture impossible ne vaut
    jamais « terminé »."""
    if not str(run_id or "").isdigit():
        return False
    seen = ctx.shell.run(["gh", "run", "view", str(run_id), "--json", "status"],
                         cwd=ctx.repo, timeout=120)
    if seen.returncode != 0:
        return False
    try:
        return json.loads(seen.stdout).get("status") == "completed"
    except (ValueError, AttributeError):
        return False


def ci_in_progress(ctx: Context, item: dict, *, forge: bool = False) -> dict | None:
    """#4501 : le run en cours de la tête poussée du porteur, ou None.

    La tête poussée est la tête gelée et/ou la tête de branche vue en avance
    (`branch_head`, relevé partagé) : une poussée plus récente, pas encore
    gelée, est déjà sous CI. Un porteur dont une de ces têtes est sous ci-full
    n'est ni recomposé ni réenregistré par un second composeur : la reprise
    attend la conclusion du run. Une lecture impossible de `ci status` lève
    (aucun geste), elle ne vaut jamais « aucun run ».
    """
    heads = {str(item.get("branch_head") or "").strip().lower(), frozen_sha(item)}
    heads.discard("")
    if not heads:
        return None
    report = ci_status(ctx, int(item["id"]))
    for run in report.get("runs") or []:
        if str(run.get("head_sha") or "").lower() in heads \
                and str(run.get("status") or "") in (
                    "running", "queued", "pending", "in_progress") \
                and not (forge and forge_completed(ctx, str(run.get("run_id") or ""))):
            return run
    return None


# ---------------------------------------------------------------------------
# Composition et unités
# ---------------------------------------------------------------------------


@dataclass
class UnitRow:
    lots: list[int]
    source: str
    note: str

    @property
    def resolution(self) -> bool:
        return len(self.lots) > 1 or "résolution" in self.note.lower() \
            or "resolution" in self.note.lower()


def composition_units(text: str) -> list[UnitRow]:
    """Les unités de la table `## Composition` (gabarit #3629), avec leur note."""
    rows = auto.parse_composition_table(text)
    notes: list[str] = []
    in_table = False
    for line in text.splitlines():
        if line.strip().startswith("## Composition"):
            in_table = True
            continue
        if in_table and line.strip().startswith("|"):
            cells = [cell.strip() for cell in line.strip().strip("|").split("|")]
            if len(cells) >= 3 and re.search(r"#\d+", cells[1]):
                notes.append(cells[-1] if len(cells) >= 5 else "")
        elif in_table and notes:
            break
    notes += [""] * (len(rows) - len(notes))
    return [UnitRow(lots, source, note) for (lots, source), note in zip(rows, notes)]


def manifest_at(ctx: Context, item: dict, sha: str) -> str:
    branch = str(item.get("branch") or "")
    name = branch[len(auto.PACKAGE_PREFIX):] if branch.startswith(auto.PACKAGE_PREFIX) else ""
    if not name:
        raise HoraireError("porteur #%s sans branche %s*" % (item.get("id"), auto.PACKAGE_PREFIX))
    ctx.git("fetch", "--no-tags", "origin", branch, timeout=300)
    result = ctx.git("show", "%s:%s" % (sha, auto.manifest_rel(name)))
    if result.returncode != 0:
        raise HoraireError("manifeste de %s illisible sur %s : %s"
                           % (branch, sha[:12], auto._tail(result.text, 5)))
    return result.stdout


CONTRACT_PROOF_RUN = "contracts-proof"


def publication_mode(ctx: Context):
    from ameesh.packet_contract_proof import policy
    return ctx.contract_publication or policy(ctx.project)


def paquet_auto(ctx: Context, name: str, carrier: int, *,
                exclude: list[int] = (), exclude_reasons: list[str] = (),
                only: list[int] = (), force: bool = False,
                publish: str = "", keep_composition: str = "") -> Result:
    """`publish` : « réf@sha » de la tête testée (#4237), publiée sans retester.

    `keep_composition` (#4581) : la tête gelée dont la recomposition reprend
    les membres, moins les `--exclude` ; les lots prêts depuis vont à la
    coupure suivante.
    """
    cmd = [ctx.python, str(SCRIPT_DIR / "paquet-auto.py"), "--repo", str(ctx.repo),
           "--target", ctx.target, "--name", name, "--carrier", str(carrier),
           "--pilot", ctx.pilot, "--from", ctx.actor,
           # #5084 : la pause posée après le départ du tic est relue avant de pousser
           "--pause-project", ctx.project]
    cmd += ["--contract-publication", publication_mode(ctx)]
    if publish:
        ref, _, sha = publish.rpartition("@")
        cmd += ["--publish", ref, "--publish-sha", sha]
    if force:
        cmd.append("--force")
    if ctx.unit_tests is not None:
        cmd += ["--tests-timeout", str(ctx.unit_tests.unit_timeout_seconds),
                "--tests-parallel", str(ctx.unit_tests.parallel)]
        contract_budget = (getattr(ctx.unit_tests, "contracts_timeout_seconds", None)
                           or ctx.unit_tests.unit_timeout_seconds)
        cmd += ["--contracts-budget", str(contract_budget)]
        total = getattr(ctx.unit_tests, "total_timeout_seconds", None)
        if total:
            cmd += ["--tests-budget", str(total)]
        else:
            ctx.say("packet_tests.total_timeout_seconds absent du canon "
                    "compilé : tests par unité sans borne totale (#4463)")
    else:
        ctx.say("packet_tests absent du canon compilé : tests ciblés en série, "
                "borne par défaut de paquet-auto (#4271)")
    for lot in only:
        cmd += ["--only", str(lot)]
    for lot in exclude:
        cmd += ["--exclude", str(lot)]
    for reason in exclude_reasons:
        cmd += ["--exclude-reason", reason]
    if keep_composition:
        cmd += ["--keep-composition", keep_composition]
    if ctx.dry_run:
        ctx.say("[essai] %s" % " ".join(cmd))
        return Result(0, "aucun lot prêt : rien à composer (essai)\n", "")
    return ctx.shell.run(cmd, cwd=ctx.repo, timeout=COMPOSE_TIMEOUT)


def candidates(ctx: Context) -> dict:
    """L'inventaire calculé, lu depuis un worktree à la tête exacte de la cible.

    Le résultat est gardé sur le contexte (`candidates_cache`) : la
    composition et le partage #3256 C lisent la même liste, sans refaire un
    worktree ni relire la cible.
    """
    if ctx.candidates_cache:
        return ctx.candidates_cache
    fetched = ctx.git("fetch", "--tags", "--force", "origin", ctx.target, timeout=300)
    if fetched.returncode != 0:
        raise HoraireError("fetch %s : %s" % (ctx.target, auto._tail(fetched.text, 5)))
    head = ctx.git("rev-parse", "origin/%s^{commit}" % ctx.target)
    if head.returncode != 0:
        raise HoraireError("tête de %s illisible" % ctx.target)
    base = head.stdout.strip()
    parent = Path(os.environ.get("TMPDIR") or tempfile.gettempdir())
    path = Path(tempfile.mkdtemp(prefix="paquet-horaire-", dir=str(parent)))
    path.rmdir()
    added = ctx.git("worktree", "add", "--detach", str(path), base)
    if added.returncode != 0:
        raise HoraireError("worktree : %s" % auto._tail(added.text, 5))
    try:
        result = ctx.shell.run(
            [ctx.python, str(path / auto.CANDIDATES_SCRIPT), "--repo", str(path),
             "--target", ctx.target, "--base", base, "--json"],
            cwd=path, timeout=COMMAND_TIMEOUT)
        if result.returncode not in (0, 3):
            raise HoraireError("paquet-candidats rc %d : %s"
                               % (result.returncode, auto._tail(result.text, 10)))
        payload = _json(result, "paquet-candidats")
        payload["base"] = base
        ctx.candidates_cache = payload
        return payload
    finally:
        ctx.git("worktree", "remove", "--force", str(path))
        shutil.rmtree(path, ignore_errors=True)


def ready_lots(ctx: Context) -> list[int]:
    """Les identifiants prêts de l'inventaire calculé (jamais un courrier)."""
    payload = candidates(ctx)
    errored = {int(item.get("work_item") or item.get("lot") or 0)
               for item in payload.get("errors_by_lot") or []}
    return [int(lot) for lot in payload.get("ready") or [] if int(lot) not in errored]


# ---------------------------------------------------------------------------
# Rouge : déterministe ou non concluant
# ---------------------------------------------------------------------------


@dataclass
class Verdict:
    deterministic: bool
    lots: list[int]
    tests: list[str]
    why: str


def classify(run: dict) -> Verdict:
    """Le rouge est-il attribué sans ambiguïté par #3801 ?

    Déterministe seulement si le run a échoué (`failure`), porte au moins un
    échec de test propre au paquet, et si **chaque** échec propre est attribué
    (`probable`) à une **seule** meilleure unité. Un échec de job sans test, un
    test non attribuable ou partagé entre unités, un run annulé : non concluant.
    """
    status = str(run.get("status") or "")
    if status != "failure":
        return Verdict(False, [], [], "run %s, pas un échec de test" % (status or "?"))
    own = [f for f in run.get("failures") or [] if not f.get("inherited")]
    if not own:
        return Verdict(False, [], [], "aucun échec propre au paquet (hérités seulement)")
    if any(str(f.get("part") or "") == "job" or not f.get("test") for f in own):
        return Verdict(False, [], [], "échec de job sans test (délai, démarrage)")
    attribution = run.get("attribution") or {}
    if attribution.get("status") != "ok":
        return Verdict(False, [], [], "attribution #3801 : %s"
                       % (attribution.get("status") or "absente"))
    by_test = {str(t.get("test") or ""): t for t in attribution.get("tests") or []}
    lots: list[int] = []
    tests: list[str] = []
    for failure in own:
        name = str(failure.get("test"))
        entry = by_test.get(name)
        if not entry or entry.get("status") != "probable":
            return Verdict(False, [], [], "%s non attribuable" % name)
        units = entry.get("units") or []
        top = max((int(unit.get("best") or 0) for unit in units), default=0)
        best = [unit for unit in units if int(unit.get("best") or 0) == top]
        if top < STRONG_EVIDENCE:
            return Verdict(False, [], [], "%s : preuve faible (poids %d)" % (name, top))
        if len(best) != 1:
            return Verdict(False, [], [], "%s : %d unités à égalité" % (name, len(best)))
        lot = int(best[0]["lot"])
        tests.append(name)
        if lot not in lots:
            lots.append(lot)
    return Verdict(True, lots, tests, "attribué par #3801")


def replay_locally(ctx: Context, sha: str, tests: list[str]) -> str:
    """Rejoue les tests rouges sur la tête gelée ; "" si rouge confirmé.

    #4221 : un QueryCanceled/lock timeout **du corps d'un test** qui se
    reproduit à ce rejeu seul, hors charge, est un rouge réel (la ci-full est
    rouge, le test est attribué, l'unité est retirée). Une erreur de fixture
    (`setUpClass`/`tearDownClass`), un QueryCanceled de charge qui ne
    reproduit pas, ou tout autre rejeu non concluant restent non concluants :
    aucun retrait.
    """
    if ctx.dry_run:
        return ""
    parent = Path(os.environ.get("TMPDIR") or tempfile.gettempdir())
    path = Path(tempfile.mkdtemp(prefix="paquet-horaire-rejeu-", dir=str(parent)))
    path.rmdir()
    added = ctx.git("worktree", "add", "--detach", str(path), sha)
    if added.returncode != 0:
        return "rejeu local impossible (worktree : %s)" % auto._tail(added.text, 3)
    try:
        run = auto.run_tests(path, "rejeu", sorted(set(tests)), timeout=REPLAY_TIMEOUT)
        if run.green:
            return "vert en local (%d test(s)) : rouge non reproduit" % run.tests
        if run.inconclusive:
            if not run.timed_out and run.returncode == 1 \
                    and run.body_cancel_tests() & {str(test) for test in tests}:
                return ""
            return "rejeu local non concluant (%s)" % run.motive()
        return ""
    finally:
        ctx.git("worktree", "remove", "--force", str(path))
        shutil.rmtree(path, ignore_errors=True)


# ---------------------------------------------------------------------------
# Étapes
# ---------------------------------------------------------------------------

#: #3256 C : la trace qui fait d'un porteur le second d'une paire parallèle.
SECOND_PREFIX = "paquet-horaire: second paquet parallèle de #"
#: le groupe de lots composé dans ce porteur (les paires et les reprises)
GROUP_RE = re.compile(r"groupe=(\d+(?:,\d+)*)")
SECOND_RE = re.compile(r"^paquet-horaire: second paquet parallèle de #(\d+)\b")
#: #5941 : le corps du porteur cite le premier de la paire, écrit atomiquement
#: avec la création ; le journal le redit ensuite, sans être indispensable.
FIRST_RE = re.compile(r"premier=#(\d+)\b")


def carrier_group(item: dict) -> list[int]:
    """Les lots que ce porteur doit composer (`groupe=…` de sa note).

    #5941 : le corps du porteur porte aussi `groupe=…`, écrit dans la même
    transaction que le lot. Une interruption entre la création et la note de
    journal laisse donc la paire reconnaissable et reprenable.
    """
    for event in reversed(_events(item)):
        match = GROUP_RE.search(str(event.get("note") or ""))
        if match:
            return [int(lot) for lot in match.group(1).split(",") if lot]
    match = GROUP_RE.search(str(item.get("body") or ""))
    if match:
        return [int(lot) for lot in match.group(1).split(",") if lot]
    return []


def second_peer(item: dict) -> int | None:
    """Le porteur du premier paquet, si ce porteur est le second d'une paire."""
    for event in reversed(_events(item)):
        match = SECOND_RE.match(str(event.get("note") or ""))
        if match:
            return int(match.group(1))
    match = FIRST_RE.search(str(item.get("body") or ""))
    return int(match.group(1)) if match else None


def yesterday_measure(ctx: Context) -> dict | None:
    """La section `qa_merge` du débit (#3256 C), lue sans jobs GitHub.

    Une lecture impossible n'ouvre rien : le tic continue avec un seul paquet,
    et le dit. Le débit est la seule source de la mesure ; le seuil, lui,
    vient du canon compilé (`paquet.attente_mediane_s`).
    """
    if ctx.parallel is None:
        return None
    try:
        result = ctx.ameesh("debit", "--json", "--no-jobs", timeout=COMMAND_TIMEOUT)
    except Exception as exc:  # noqa: BLE001 - une mesure absente ne bloque pas
        ctx.say("paquet parallèle : débit illisible (%s), un seul paquet" % exc)
        return None
    if result.returncode != 0:
        ctx.say("paquet parallèle : débit rc %d (%s), un seul paquet"
                % (result.returncode, auto._tail(result.text, 5)))
        return None
    try:
        return _json(result, "debit").get("qa_merge") or {}
    except HoraireError as exc:
        ctx.say("paquet parallèle : débit illisible (%s), un seul paquet" % exc)
        return None


def parallel_decision(ctx: Context) -> dict | None:
    """Le second paquet est-il ouvert par la médiane de la veille ?"""
    if ctx.parallel is None:
        return None
    from ameesh import packet_parallel
    decision = packet_parallel.trigger(
        yesterday_measure(ctx), getattr(ctx.parallel, "attente_mediane_s", None),
        now=ctx.now, zone=ctx.zone)
    if not decision.get("enabled"):
        ctx.say("paquet : un seul paquet (%s)" % decision.get("why"))
        return None
    return decision


def parallel_partition(ctx: Context, ready: list[int]) -> dict | None:
    """Partage les lots prêts en deux groupes disjoints, ou None."""
    try:
        payload = candidates(ctx)
    except HoraireError as exc:
        ctx.say("paquet parallèle : inventaire illisible (%s), un seul paquet" % exc)
        return None
    by_lot = {int(entry.get("work_item") or 0): entry
              for entry in payload.get("candidates") or []}
    entries = [by_lot[lot] for lot in ready if lot in by_lot]
    from ameesh import packet_parallel
    return packet_parallel.partition(entries, ready)


def _create_carrier(ctx: Context, name: str, group: list[int], *,
                    parallel: int, composition_owner: str = "",
                    first: int = 0) -> int:
    branch = auto.package_branch(name)
    title = "%s%s — composition automatique (§11 bis, §12)" % (TITLE_PREFIX, name)
    detail = ""
    if parallel:
        # #5941 (B1 cx3) : le groupe et le premier porteur sont aussi dans le
        # corps, écrit dans la même transaction que le lot. Un arrêt entre la
        # création et la note de journal laisse donc une paire reconnaissable
        # (`carrier_group`/`second_peer` lisent le corps en repli).
        detail = (" Paquet parallèle #3256 C (%d/%d) : groupe=%s%s."
                  % (parallel, 2, ",".join(str(lot) for lot in group),
                     " premier=#%d" % first if first else ""))
    cmd = [
        "work", "add", "--title", title, "--type", "evolution",
        "--app", ctx.project, "--assignee", ctx.pilot, "--priority", "1",
        "--branch", branch, "--target", ctx.target,
        "--estimate", CARRIER_ESTIMATE,
        "--estimate-source", CARRIER_ESTIMATE_SOURCE,
        "--source", "paquet-horaire (#3845), minuterie",
        # #4667 : le corps ne cite plus le script par son chemin — une
        # empreinte d'intake inférée de cette citation faisait naître le
        # porteur libre (le véhicule n'a pas de delta propre).
        "--body", "Paquet horaire composé sans agent par le composeur horaire "
                  "(#3845) : %d lot(s) prêt(s) à la lecture (%s).%s"
                  % (len(group), ", ".join("#%d" % lot for lot in group), detail),
        "--actor", ctx.actor,
    ]
    if composition_owner:
        # #5941 : le tic tient déjà le verrou de composition ; la création
        # d'un second porteur de la même paire parallèle est permise parce
        # que c'est le même composeur, et le propriétaire est vérifié.
        cmd += ["--composition-owner", composition_owner]
    created = ctx.must(*cmd, what="création du porteur")
    found = CREATED_RE.search(created.stdout)
    if not found:
        raise HoraireError("porteur créé sans identifiant lisible : %s"
                           % auto._tail(created.text, 5))
    carrier = int(found.group(1))
    ctx.say("porteur #%d : %s" % (carrier, title))
    return carrier


#: #5941 : débuts des refus de `work add` qui disent « un autre compose » ou
#: « un porteur réserve déjà la cible ». Le tic s'arrête proprement : ce n'est
#: pas un échec opérationnel, rien n'a été poussé.
COMPOSITION_REFUSALS = ("composition in progress", "already reserves",
                        "composition lock")


def _close_lease(lease) -> None:
    """Referme la connexion du bail ; une fermeture ne juge pas le geste."""
    try:
        lease.close()
    except Exception:  # noqa: BLE001 - le bail expire seul de toute façon
        pass


#: prend le bail de composition pour la section critique « relire les porteurs
#: → créer le porteur » ; None si le banc n'en fournit pas (tests sans base).
def _composition_lease(ctx: Context):
    if ctx.composition is None:
        return None
    try:
        lease = ctx.composition()
    except Exception as exc:  # noqa: BLE001 - base illisible : aucun geste
        raise HoraireError("verrou de composition indisponible (%s) : aucun geste"
                           % exc) from exc
    try:
        taken = lease.acquire()
    except Exception as exc:  # noqa: BLE001 - bail illisible : aucun geste
        _close_lease(lease)
        raise HoraireError("verrou de composition illisible (%s) : aucun geste"
                           % exc) from exc
    if not taken:
        try:
            holder = lease.describe()
        except Exception as exc:  # noqa: BLE001 - le refus reste un refus
            holder = "détenteur illisible (%s)" % exc
        finally:
            _close_lease(lease)
        raise _CompositionBusy(holder)
    ctx.say("verrou de composition pris (%s)" % lease.owner)
    return lease


class _CompositionBusy(HoraireError):
    """Un autre composeur tient le bail : le tic n'a rien à faire."""


def compose(ctx: Context, rows: list[dict]) -> str:
    if slot_served(rows, ctx.now, ctx.zone, ctx.project, ctx.target, ctx):
        return "porteur horaire déjà créé dans l'heure, non fusionné : rien à faire"
    ready = ready_lots(ctx)
    if not ready:
        return "aucun lot prêt sur %s : rien à faire" % ctx.target
    groups = [ready]
    parallel = parallel_decision(ctx)
    if parallel is not None:
        plan = parallel_partition(ctx, ready)
        if plan and plan.get("second"):
            groups = [plan["first"], plan["second"]]
            ctx.say("paquets parallèles #3256 C : %s | %s (médiane %s s > seuil %s s)"
                    % (", ".join("#%d" % lot for lot in groups[0]),
                       ", ".join("#%d" % lot for lot in groups[1]),
                       parallel.get("median_s"), parallel.get("threshold_s")))
        else:
            ctx.say("paquet parallèle impossible : les lots ne sont pas disjoints, "
                    "un seul paquet")
    if ctx.dry_run:
        for index, group in enumerate(groups, start=1):
            ctx.say("[essai] paquet %d/%d, %d lot(s) prêt(s) : %s"
                    % (index, len(groups), len(group),
                       ", ".join("#%d" % lot for lot in group)))
        return "essai : %d paquet(s)" % len(groups)
    # #5941 : le verrou est pris APRÈS la lecture des candidats (elle peut
    # durer), puis les porteurs sont relus SOUS le verrou : un porteur créé
    # entre-temps par un autre composeur arrête le tic avant toute écriture.
    try:
        lease = _composition_lease(ctx)
    except _CompositionBusy as busy:
        return "composition en cours (%s) : rien à faire" % busy
    created: list[tuple[int, list[int], str, int]] = []
    results: list[str] = []
    try:
        if lease is not None:
            fresh = recent_lots(ctx)
            if slot_served(fresh, ctx.now, ctx.zone, ctx.project, ctx.target, ctx):
                return ("porteur horaire déjà créé dans l'heure, non fusionné : "
                        "rien à faire")
            reserved = [row for row in fresh
                        if is_carrier(row, ctx.project, ctx.target)
                        and str(row.get("state") or "") in OPEN_STATES]
            if reserved:
                return "%s réservée par %s : rien à faire" % (
                    ctx.target, ", ".join("#%d" % int(row["id"]) for row in reserved))
        first: int = 0
        for index, group in enumerate(groups):
            name = next_name(ctx)
            # #4329 : une composition du même paquet vit (lancement à la main ou
            # tick) ; ne pas créer un porteur qu'elle refuserait.
            holder = composition_in_progress(ctx, name)
            if holder:
                results.append("composition en cours (%s)" % holder)
                continue
            try:
                carrier = _create_carrier(
                    ctx, name, group,
                    parallel=(index + 1 if len(groups) == 2 else 0),
                    composition_owner=(lease.owner if lease is not None else ""),
                    first=first)
            except HoraireError as exc:
                # #5941 : le bail a expiré ou a été pris entre la relecture et
                # la création ; le premier porteur, s'il existe, reste suivi.
                refused = lease is not None and any(
                    marker in str(exc) for marker in COMPOSITION_REFUSALS)
                if refused or created:
                    # B1 cx3 : un échec du second `work add` ne doit pas
                    # laisser le premier porteur sans composition ; le corps
                    # du premier porte déjà son groupe, le tic le reprend.
                    results.append(
                        "composition en cours : rien de plus créé (%s)"
                        % auto._tail(str(exc), 2) if refused else
                        "porteur suivant non créé (%s) ; le premier reste composé"
                        % auto._tail(str(exc), 2))
                    break
                raise
            if len(groups) == 2:
                if index == 0:
                    first = carrier
                    ctx.note(carrier, "paquet-horaire: parallèle premier groupe=%s"
                             % ",".join(str(lot) for lot in group))
                elif first:
                    ctx.note(carrier, "%s%d groupe=%s"
                             % (SECOND_PREFIX, first,
                                ",".join(str(lot) for lot in group)))
            created.append((index, list(group), name, carrier))
    finally:
        # #5941 : le bail ne couvre que la décision et la création. La
        # composition elle-même (paquet-auto, tests par unité) reste tenue par
        # le `flock` par nom (#4329) ; le garder ici ferait expirer le bail
        # avant le second porteur d'une paire parallèle.
        if lease is not None:
            try:
                lease.release()
            except Exception as exc:  # noqa: BLE001 - le TTL libère le bail
                ctx.say("bail de composition non rendu : %s" % exc)
            _close_lease(lease)
    for index, group, name, carrier in created:
        try:
            results.append(attempt(ctx, carrier, name,
                                   Attempt(COMPOSITION, 1, "",
                                           only=(list(group) if len(groups) == 2
                                                 else []))))
        except HoraireError as exc:
            # The first carrier's journal already records its attempt. Keep
            # composing the independent second group; the next tick resumes it.
            if len(groups) != 2:
                raise
            results.append("#%d : %s" % (carrier, exc))
    return "\n".join(results)


# ---------------------------------------------------------------------------
# Tentatives et reprise
# ---------------------------------------------------------------------------


@dataclass
class Attempt:
    step: str
    n: int
    base: str  # tête gelée au départ ("" : aucune)
    host: str = ""
    pid: int = 0
    run_id: str = ""
    exclude: list[int] = field(default_factory=list)
    #: #3256 C : le groupe que ce porteur compose (vide = toute la liste prête)
    only: list[int] = field(default_factory=list)
    ts: float = 0.0
    failed: bool = False
    #: #4245 : arrêtée par un refus de garde d'époque (non concluant, §3)
    fenced: bool = False
    #: #4245 (B impl-04) : reprise d'un refus de garde : `--force` même à n=1,
    #: la branche et le worktree locaux de l'essai refusé sont restés
    force: bool = False
    epoch: int = 0  # époque du bail à l'entrée (0 : marque sans époque)
    #: #4237 : « réf@sha » de la tête testée publiée par cette tentative ("" : composer)
    publish: str = ""
    #: #4237 : motifs d'exclusion du pilote, appariés à `exclude`
    reasons: list[str] = field(default_factory=list)

    @property
    def key(self) -> str:
        return "%s base=%s" % (self.step, self.base or "-")


def _events(item: dict) -> list[dict]:
    """Les événements du plus ancien au plus récent (`work show` : récents d'abord)."""
    events = list(item.get("events") or [])
    return sorted(events, key=lambda ev: (float(ev.get("created_ts") or 0),
                                          int(ev.get("id") or 0)))


def attempts(item: dict) -> list[Attempt]:
    """Les tentatives du journal, dans l'ordre, chacune marquée si un échec a suivi."""
    found: list[Attempt] = []
    for ev in _events(item):
        note = str(ev.get("note") or "")
        match = ATTEMPT_RE.match(note)
        if match:
            step, n, base, host, pid, run_id, without, epoch, ref, why = match.groups()
            try:
                reasons = [str(text) for text in json.loads(why)] if why else []
            except ValueError:
                reasons = []
            found.append(Attempt(
                step, int(n), "" if base == "-" else base, host, int(pid), run_id or "",
                [int(lot) for lot in (without or "").split(",") if lot],
                ts=float(ev.get("created_ts") or 0), epoch=int(epoch or 0),
                publish=ref or "", reasons=reasons))
            continue
        fenced = FENCED_RE.match(note)
        match = fenced or FAILED_RE.match(note)
        if match:
            step, n, base = match.groups()
            base = "" if base == "-" else base
            for previous in reversed(found):
                if (previous.step, previous.n, previous.base) == (step, int(n), base):
                    previous.failed = True
                    previous.fenced = bool(fenced)
                    break
    return found


def composition_in_progress(ctx: Context, name: str) -> str:
    """« hôte:pid » du verrou vivant de `name`, ou "" (#4329)."""
    holder = auto.composition_holder(ctx.repo, name)
    return auto.holder_label(*holder) if holder is not None else ""


def carrier_composition(ctx: Context, carrier: int) -> str:
    """#6563 : le détenteur du bail de composition du porteur, ou "".

    Le bail est partagé entre clones et hôtes (`packet_composition.Lease`,
    clé par porteur) : il couvre un composeur désigné qui reprend le porteur
    existant, que le `flock` local du clone ne voit pas. Une lecture
    impossible n'autorise aucun geste.
    """
    if ctx.carrier_claim is None:
        return ""
    try:
        held = ctx.carrier_claim(int(carrier))
    except Exception as exc:  # noqa: BLE001 - base illisible : aucun geste
        raise HoraireError(
            "bail de composition du porteur #%d illisible (%s) : aucun geste"
            % (carrier, exc)) from exc
    if not held:
        return ""
    if isinstance(held, dict):
        return str(held.get("owner") or "verrou tenu")
    return str(held)


def attempt(ctx: Context, carrier: int, name: str, this: Attempt) -> str:
    """Lancer paquet-auto pour une étape, la marque d'intention d'abord."""
    holder = composition_in_progress(ctx, name)
    if holder:
        # #4329 : une composition du même paquet vit (tick ou lancement à la
        # main) ; ne pas écrire une tentative que son verrou va refuser.
        return "#%d : composition en cours (%s)" % (carrier, holder)
    claim = carrier_composition(ctx, carrier)
    if claim:
        # #6563 : un autre composeur — l'agent désigné par l'alerte — tient le
        # bail partagé du porteur ; le tic ne relance rien.
        return "#%d : composition en cours (%s)" % (carrier, claim)
    mark = "%s%s n=%d base=%s par=%s:%d" % (MARK_ATTEMPT, this.step, this.n,
                                            this.base or "-", ctx.host, ctx.pid)
    if this.step == RECOMPOSITION:
        mark += " run=%s sans=%s" % (this.run_id, ",".join(str(lot) for lot in this.exclude))
    if ctx.epoch:
        mark += " epoque=%d" % ctx.epoch
    if this.publish:
        mark += " publie=%s" % this.publish
    if this.reasons:
        mark += " motifs=%s" % json.dumps(this.reasons, ensure_ascii=False)
    ctx.note(carrier, mark)
    if this.publish:
        result = paquet_auto(ctx, name, carrier, publish=this.publish)
        return after_compose(ctx, carrier, name, result, this)
    if this.reasons:
        reasons = list(this.reasons)
    elif this.step == RECOMPOSITION and this.run_id != PILOT_RUN:
        reasons = [EXCLUDE_CI_FULL_REASON] * len(this.exclude)
    else:
        reasons = []
    # une reprise (n > 1) réécrit une branche peut-être déjà poussée
    result = paquet_auto(ctx, name, carrier, exclude=this.exclude,
                         exclude_reasons=reasons,
                         only=this.only,
                         keep_composition=(this.base
                                           if this.step == RECOMPOSITION else ""),
                         force=this.step == RECOMPOSITION or this.n > 1 or this.force)
    return after_compose(ctx, carrier, name, result, this)


def frozen_ts(item: dict) -> float:
    freeze = item.get("freeze") or {}
    declared = freeze.get("declared") or {}
    return float(declared.get("at_ts") or 0)


def achieved(item: dict, sha: str, since: float) -> bool:
    """#4237 : la tête gelée `sha` est finie : publiée par le tic (ci demandée)
    ou dite verte par paquet-auto après ses tests par unité (depuis `since`)."""
    if MARK_CI + sha in marks(item):
        return True
    for ev in _events(item):
        if float(ev.get("created_ts") or 0) < since:
            continue
        match = auto.TESTS_DONE_RE.match(str(ev.get("note") or ""))
        if match and sha.startswith(match.group(1)):
            return True
    return False


def pending(ctx: Context, item: dict, sha: str) -> Attempt | None:
    """La dernière tentative, si elle n'a pas abouti.

    La tête gelée n'a pas bougé depuis sa marque ; ou (#4237, B cx1) elle a
    bougé vers la tête que la tentative a elle-même poussée et gelée avant ses
    tests (#4304 : gel postérieur à la marque, ou tête qu'elle publie), sans
    que cette tête soit finie (`achieved`) : refus de garde après les tests,
    échec ou arrêt en cours de tests, ou tentative encore vivante. Un gel
    postérieur qu'elle n'a ni publié ni testé et qu'elle n'a pas posé
    elle-même (#4506) la clôt : c'est la main d'un autre.
    """
    found = attempts(item)
    if found and sha:
        last = found[-1]
        kept = tested_since(item, last.ts)
        expected = (last.publish.rsplit("@", 1)[-1]
                    if last.publish else kept[1] if kept else "")
        moved = frozen_ts(item) > last.ts > 0
        # Un autre gel ne donne pas à cette tentative le droit de le remplacer.
        if moved and expected and expected != sha:
            return None
        # #4506 : la tête a bougé après la tentative sans qu'elle l'ait publiée
        # ni testée, et le gel vient d'une autre main (note du jalon) : il la
        # clôt, sans la reprendre. Une tentative encore vivante garde la main.
        if moved and not expected and sha != last.base \
                and freeze_foreign(item, last.ts) and not running(ctx, last):
            return None
    if found and found[-1].base == sha:
        # #6183 : --publish conserve la tête/base ; sa CI demandée clôt
        # la tentative. Une recomposition ordinaire doit encore reprendre,
        # même si une ancienne CI existe déjà sur sa base.
        last = found[-1]
        if sha and last.publish.endswith("@" + sha) and achieved(item, sha, last.ts):
            return None
        return found[-1]
    if found and sha:
        last = found[-1]
        own = (frozen_ts(item) >= last.ts > 0
               or last.publish.endswith("@" + sha))
        # un gel posé après « reprise épuisée » est la main d'un agent
        stuck = [float(ev.get("created_ts") or 0) for ev in _events(item)
                 if str(ev.get("note") or "").startswith(
                     (MARK_STUCK + last.key, MARK_PUBLISHED + last.key))
                 and float(ev.get("created_ts") or 0) >= last.ts]
        if own and not achieved(item, sha, last.ts) \
                and not (stuck and frozen_ts(item) > stuck[-1]):
            return last
    if not sha and not found:
        # porteur créé, arrêt avant la première marque : une tentative 0 morte
        created = float(item.get("created_ts") or 0)
        if ctx.now - created < CREATION_GRACE:
            return Attempt(COMPOSITION, 0, "", ctx.host, ctx.pid, ts=created)
        return Attempt(COMPOSITION, 0, "", failed=True, ts=created)
    return None


def lease_epoch() -> int:
    """L'époque passée par `ameesh role run` ; 0 si absente ou illisible."""
    text = (os.environ.get("AMEESH_LEASE_EPOCH")
            or os.environ.get("AGENT_MESH_LEASE_EPOCH") or "").strip()
    try:
        return max(int(text), 0)
    except ValueError:
        return 0


def running(ctx: Context, this: Attempt, name: str = "") -> bool:
    if this.failed:
        return False
    # #4329 : un verrou vivant, même sans marque de tentative (composition
    # lancée hors du tic), fait compter la composition comme en cours.
    if name and composition_in_progress(ctx, name):
        return True
    if this.host == ctx.host:
        return this.pid == ctx.pid or ctx.alive(this.pid)
    if this.epoch and ctx.epoch:
        # le bail a changé de main : le `packet record` de l'ancienne époque
        # est refusé (EP4) avant la poussée, elle ne publie plus rien (#4087)
        return this.epoch == ctx.epoch
    return ctx.now - this.ts < ATTEMPT_STALE


def resume(ctx: Context, item: dict, this: Attempt) -> str:
    """Une tentative sans aboutissement : attendre, relancer, ou prévenir une fois."""
    carrier = int(item["id"])
    branch = str(item.get("branch") or "")
    name = auto.package_name(branch)
    holder = composition_in_progress(ctx, name) if name else ""
    if holder:
        # #4329 : le verrou du paquet prime le pid de la marque, qui peut être
        # celui d'une tentative morte.
        return "#%d : composition en cours (%s)" % (carrier, holder)
    claim = carrier_composition(ctx, carrier)
    if claim:
        # #6563 : un composeur d'un autre clone (agent désigné) tient le bail
        # partagé du porteur ; la tentative de la marque n'est pas la sienne.
        return "#%d : composition en cours (%s)" % (carrier, claim)
    if any(note.startswith(MARK_PUBLISHED + this.key) for note in marks(item)):
        # #6561 : la composition a publié une tête puis n'a rien laissé ; le
        # porteur a été laissé ouvert pour son pilote, le tic n'y recompose pas.
        return "#%d : %s abandonnée après publication, déjà signalée (#6561)" % (
            carrier, this.step)
    if running(ctx, this, name):
        return "#%d : %s en cours (%s:%d)" % (carrier, this.step, this.host, this.pid)
    # #4501 : la tête poussée est sous ci-full ; un second composeur ne
    # recompose ni ne réenregistre par-dessus (le premier peut encore aboutir).
    pending_run = ci_in_progress(ctx, item)
    if pending_run is not None:
        return "#%d : ci-full en cours sur %s (run %s), reprise suspendue" % (
            carrier, frozen_sha(item)[:12], pending_run.get("run_id") or "?")
    if this.fenced:
        # #4245 : refus de garde, non concluant : même numéro de tentative,
        # borné par MAX_FENCED ; `--force` ferme la branche et le worktree
        # locaux laissés par l'essai refusé (le refus précède la poussée)
        fenced = sum(1 for a in attempts(item) if a.fenced and a.key == this.key)
        if fenced < MAX_FENCED:
            ctx.say("#%d : reprise de la %s après refus de garde (tentative %d "
                    "non décomptée)" % (carrier, this.step, this.n))
            # #4237 : une tête testée gardée depuis se publie sans recomposer
            kept = tested_since(item, this.ts)
            return attempt(ctx, carrier, name, Attempt(
                this.step, this.n, this.base, run_id=this.run_id,
                exclude=list(this.exclude), only=carrier_group(item), force=True,
                publish=("%s@%s" % (kept[2], kept[1]) if kept else this.publish),
                reasons=list(this.reasons)))
        if MARK_STUCK + this.key in marks(item):
            return "#%d : reprise de la %s épuisée, déjà signalée" % (carrier, this.step)
        ctx.mail(ctx.pilot, carrier,
                 "#%d %s : %s refusée %d fois par la garde d'époque (tête %s) ; "
                 "plus de reprise automatique, un agent lit le journal du porteur."
                 % (carrier, name, this.step, fenced, (this.base or "aucune")[:12]))
        ctx.note(carrier, MARK_STUCK + this.key)
        return "#%d : reprise de la %s épuisée (garde)" % (carrier, this.step)
    if this.n >= MAX_ATTEMPTS:
        if MARK_STUCK + this.key in marks(item):
            return "#%d : reprise de la %s épuisée, déjà signalée" % (carrier, this.step)
        ctx.mail(ctx.pilot, carrier,
                 "#%d %s : %s lancée %d fois sans aboutir (tête %s) ; plus de reprise "
                 "automatique, un agent lit le journal du porteur."
                 % (carrier, name, this.step, this.n, (this.base or "aucune")[:12]))
        ctx.note(carrier, MARK_STUCK + this.key)
        return "#%d : reprise de la %s épuisée" % (carrier, this.step)
    kept = tested_since(item, this.ts)
    if kept:
        ctx.say("#%d : reprise de la %s (tentative %d morte) par la tête testée %s"
                % (carrier, this.step, this.n, kept[1][:12]))
    else:
        ctx.say("#%d : reprise de la %s (tentative %d morte)" % (carrier, this.step, this.n))
    return attempt(ctx, carrier, name, Attempt(this.step, this.n + 1, this.base,
                                               run_id=this.run_id,
                                               exclude=list(this.exclude),
                                               only=carrier_group(item),
                                               publish=("%s@%s" % (kept[2], kept[1])
                                                        if kept else ""),
                                               reasons=list(this.reasons)))


def tested_since(item: dict, since: float) -> tuple[str, str, str] | None:
    """#4237 : la dernière tête testée gardée depuis `since` : (base, tête, réf).

    Écrite par `paquet-auto.keep_tested` sur un refus de geste après des tests
    verts ; une réf « - » (poussée échouée) n'est pas publiable. Une tentative
    de publication ne réécrit jamais cette marque : une publication morte se
    reprend en recomposant.
    """
    found = None
    for ev in _events(item):
        if float(ev.get("created_ts") or 0) < since:
            continue
        match = auto.TESTED_RE.match(str(ev.get("note") or ""))
        if match:
            found = match.groups() if match.group(3) != "-" else None
    return found


def remote_branch_head(ctx: Context, branch: str) -> str | None:
    """#6561 : la tête de la branche sur la forge ; None si la lecture échoue.

    Une branche absente rend "" ; une lecture impossible ne prouve rien et
    rend None (l'appelant ne conclut alors pas). L'abandon d'un porteur exige
    la preuve qu'aucune tête neuve n'y a été publiée : la branche doit porter
    encore la tête de départ de la tentative.
    """
    if not branch:
        return ""
    ref = "refs/heads/" + branch
    result = ctx.git("ls-remote", "origin", ref, timeout=120)
    if result.returncode != 0:
        return None
    for line in result.stdout.splitlines():
        sha, _, found = line.partition("\t")
        if found.strip() != ref:
            continue
        sha = sha.strip()
        return sha if re.fullmatch(r"[0-9a-f]{40}", sha) else None
    return ""


def after_compose(ctx: Context, carrier: int, name: str, result: Result,
                  this: Attempt) -> str:
    step = this.step
    # La sauvegarde Git réussit parfois alors que sa note échoue ; récupérer
    # la marque exacte de la sortie avant de journaliser le refus de garde.
    from ameesh.packet_contract_proof import MARK as CONTRACT_MARK, latest
    for line in result.stdout.splitlines():
        if auto.TESTED_RE.match(line):
            ctx.note(carrier, line)
    # #6238 : seule la dernière projection imprimée peut faire foi (lecture de
    # E) ; récupérée seulement si le journal du porteur ne la porte pas déjà.
    projections = [line for line in result.stdout.splitlines()
                   if line.startswith(CONTRACT_MARK)]
    if projections:
        try:
            journal = latest(show(ctx, carrier))
        except HoraireError:
            journal = None
        if journal != projections[-1]:
            ctx.note(carrier, projections[-1])
    # N1 de relecture : la fin de la sortie seulement (l'erreur qui arrête
    # paquet-auto), pas une phrase citée plus haut par un test rouge
    if result.returncode != 0 and FENCE_REFUSAL_RE.search(auto._tail(result.text, 10)):
        # #4245 : la garde EP4 a refusé un geste (bail passé ailleurs, ou
        # lignée rompue) ; ni échec de composition ni tentative consommée
        ctx.note(carrier, "%s%s n=%d base=%s époque=%d" % (
            MARK_FENCED, step, this.n, this.base or "-", ctx.epoch))
        ctx.mail(ctx.pilot, carrier,
                 "#%d %s : %s refusée par la garde d'époque (époque %d, tentative "
                 "%d/%d non décomptée, §3 non concluant) ; le tic suivant reprend.\n\n%s"
                 % (carrier, name, step, ctx.epoch, this.n, MAX_ATTEMPTS,
                    auto._tail(result.text, 10)))
        return "#%d : %s non concluante (refus de garde d'époque)" % (carrier, step)
    if result.returncode not in (0, auto.PAUSED_RC, auto.IN_PROGRESS_RC):
        reason = closed_reason(ctx, carrier)
        if reason:
            # #6538 : porteur clos par le pilote pendant la tentative (abandon,
            # remplacement) ; son échec n'est ni une panne du composeur ni un
            # rouge à reprendre : arrêt propre, sans courrier (refusé sur un
            # lot clos) ni lot d'empreinte §16.
            ctx.say("#%d %s : porteur clos (%s) pendant la %s, rc %d ignoré"
                    % (carrier, name, reason, step, result.returncode))
            return "#%d : porteur clos (%s) pendant la %s, arrêt propre" % (
                carrier, reason, step)
    if this.publish and result.returncode == auto.PUBLISH_REFUSED:
        if this.n < MAX_ATTEMPTS:
            # tête testée non publiable (cible bougée, chaîne) : recomposer tout de suite
            ctx.note(carrier, "%s%s n=%d base=%s rc=%d" % (
                MARK_FAILED, step, this.n, this.base or "-", result.returncode))
            ctx.say("#%d : tête testée refusée (%s), recomposition"
                    % (carrier, auto._tail(result.text, 3)))
            # #3256 C : la recomposition d'un porteur parallèle garde son groupe ;
            # le repli relit la note du porteur si la tentative ne le portait pas.
            only = list(this.only)
            if not only:
                try:
                    only = carrier_group(show(ctx, carrier))
                except HoraireError:
                    only = []
            strict = publication_mode(ctx) != "legacy"
            return attempt(ctx, carrier, name, Attempt(
                RECOMPOSITION if strict else step, this.n + 1,
                this.publish.rsplit("@", 1)[-1] if strict else this.base,
                run_id=CONTRACT_PROOF_RUN if strict else this.run_id,
                exclude=list(this.exclude), only=only,
                reasons=list(this.reasons)))
    elif result.returncode == auto.PAUSED_RC:
        # #5084 : pause posée pendant la tentative, relue avant de pousser ;
        # rien n'est poussé, la tentative n'est pas décomptée (comme un refus
        # de garde) et le tic qui suit la levée reprend.
        ctx.note(carrier, "%s%s n=%d base=%s pause" % (
            MARK_FENCED, step, this.n, this.base or "-"))
        return "#%d : %s arrêtée par une pause posée pendant la tentative (%s)" % (
            carrier, step, auto._tail(result.text, 1).strip())
    elif result.returncode == auto.IN_PROGRESS_RC:
        # #4329 : le verrou du paquet est vivant ; ce n'est pas un échec de
        # composition et le tic n'écrit ni marque d'échec ni courrier.
        holder = composition_in_progress(ctx, name) or "verrou tenu"
        return "#%d : composition en cours (%s)" % (carrier, holder)
    if result.returncode == auto.TARGET_MOVED_RC:
        # #6891 : la cible a atterri pendant la composition. `paquet-auto` a
        # relu la référence #3782 AVANT les contrats, rien n'est poussé et son
        # clone est déjà à jour ; on relance tout de suite sur la nouvelle
        # tête. La perte se limite à la composition, jamais aux ~12 min de
        # tests locaux ni au tick suivant. La relecture du gel juste avant la
        # poussée reste en place (#6566) pour une cible qui bouge pendant les
        # tests. Bornée par MAX_ATTEMPTS, comme une tentative morte.
        detail = " ".join(auto._tail(result.text, 3).split())
        ctx.note(carrier, "%s%s n=%d base=%s : %s" % (
            MARK_TARGET, step, this.n, this.base or "-", detail))
        ctx.say("#%d : cible avancée avant les tests, recomposition immédiate"
                % carrier)
        if this.n >= MAX_ATTEMPTS:
            if MARK_STUCK + this.key in marks(show(ctx, carrier)):
                return "#%d : cible avancée, relance immédiate déjà épuisée" % carrier
            ctx.mail(ctx.pilot, carrier,
                     "#%d %s : la cible a encore avancé avant les tests (%d "
                     "relance(s) immédiate(s), §3 non concluant) ; plus de "
                     "relance automatique, le tic suivant reprend.\n\n%s"
                     % (carrier, name, this.n, auto._tail(result.text, 10)))
            ctx.note(carrier, MARK_STUCK + this.key)
            return "#%d : cible avancée, relance immédiate épuisée" % carrier
        return attempt(ctx, carrier, name, Attempt(
            this.step, this.n + 1, this.base, run_id=this.run_id,
            exclude=list(this.exclude), only=list(this.only),
            reasons=list(this.reasons)))
    if result.returncode == auto.COMPOSITION_RED_RC:
        # #6272 : un rouge de composition (contrat, test d'une unité, gel,
        # types clients) n'est pas une panne du composeur. L'échec est
        # consigné pour que le tic suivant reprenne, le pilote est prévenu
        # par courrier, et aucun lot d'empreinte §16 ne s'ouvre.
        ctx.note(carrier, "%s%s n=%d base=%s rc=%d" % (
            MARK_FAILED, step, this.n, this.base or "-", result.returncode))
        ctx.mail(ctx.pilot, carrier,
                 "#%d %s : composition rouge par son contenu (rc %d, %s, "
                 "tentative %d/%d) ; ce n'est pas une panne du composeur, "
                 "aucun lot d'empreinte n'est ouvert. Retirer l'unité fautive "
                 "ou renoncer ; le tic suivant reprend.\n\n%s"
                 % (carrier, name, result.returncode, step, this.n,
                    MAX_ATTEMPTS, auto._tail(result.text, 30)))
        raise CompositionRed("paquet-auto rc %d pour %s" % (
            result.returncode, name))
    if result.returncode != 0:
        ctx.note(carrier, "%s%s n=%d base=%s rc=%d" % (
            MARK_FAILED, step, this.n, this.base or "-", result.returncode))
        ctx.mail(ctx.pilot, carrier,
                 "#%d %s : paquet-auto.py a échoué (rc %d, %s, tentative %d/%d) ; "
                 "le tic suivant reprend.\n\n%s"
                 % (carrier, name, result.returncode, step, this.n, MAX_ATTEMPTS,
                    auto._tail(result.text, 30)))
        raise HoraireError("paquet-auto rc %d pour %s" % (result.returncode, name))
    empty = EMPTY_COMPOSITION_RE.search(result.stdout)
    withdrawn = WITHDRAWN_COMPOSITION_RE.search(result.stdout)
    if empty or withdrawn:
        # #5970 : garder le diagnostic et prévenir avant la clôture ; le
        # courrier lié à un lot clos serait refusé par agent-mail.
        lines = result.text.splitlines()
        kept = set(range(max(0, len(lines) - 30), len(lines)))
        for i, line in enumerate(lines):
            if re.match(r"tests (unité #\d+|contrats) : ROUGE", line):
                # paquet-auto imprime l'en-tête puis les 40 lignes du rouge.
                kept.update(range(i, min(i + 41, len(lines))))
            if line.startswith("écarté #"):
                kept.add(i)
        diagnostic = "\n".join(lines[i] for i in sorted(kept))
        head = remote_branch_head(ctx, auto.package_branch(name))
        # #6561 : une recomposition publiée n'est jamais « rien composé ».
        # L'abandon (annulation des runs, clôture du porteur) exige la preuve
        # qu'aucune tête neuve n'est publiée sur la branche : elle doit porter
        # encore la tête de départ. Une lecture impossible ne prouve rien.
        if withdrawn is not None or head != this.base:
            where = ("tête %s sur la branche" % head[:12] if head
                     else "aucune tête lisible sur la branche")
            ctx.note(carrier, "%s%s n=%d tête=%s\n\n%s" % (
                MARK_PUBLISHED, this.key, this.n,
                (head or "absente")[:12], diagnostic))
            ctx.mail(ctx.pilot, carrier,
                     "#%d %s : %s ; %s, abandon refusé (#6561). "
                     "Porteur laissé ouvert : un agent lit et décide.\n\n%s"
                     % (carrier, name,
                        "recomposition sans unité après retrait" if withdrawn
                        else "composition vide avec une tête déjà publiée",
                        where, diagnostic))
            return "#%d %s : abandon refusé, %s (#6561)" % (
                carrier, name, where)
        reason = "rien composé (%s)" % step
        ctx.note(carrier, "%s%s\n\n%s" % (MARK, reason, diagnostic))
        ctx.mail(ctx.pilot, carrier, "#%d %s : %s, clôture du porteur vide.\n\n%s"
                 % (carrier, name, reason, diagnostic))
        if not ctx.dry_run:
            ctx.must("work", "close", str(carrier), "--abandoned",
                     "--note", MARK + reason, "--actor", ctx.actor,
                     what="clôture du porteur vide #%d" % carrier)
        return "%s : %s" % (name, reason)
    if re.search(r"^%s" % re.escape(auto.MERGED_LINE), result.stdout, re.MULTILINE):
        # #4322 : fusionné au vert pendant les tests par unité (§11 bis) ;
        # paquet-auto a noté le porteur et prévenu le pilote d'un rouge
        return "#%d %s : fusionné pendant les tests par unité, rien à publier" % (
            carrier, name)
    item = show(ctx, carrier)
    return publish(ctx, item)


def closed_reason(ctx: Context, carrier: int) -> str:
    """#6538 : la raison de clôture du porteur s'il est clos, sinon "".

    Une lecture impossible laisse le porteur pour ouvert : l'échec suit
    alors son chemin ordinaire.
    """
    try:
        item = show(ctx, carrier)
    except HoraireError:
        return ""
    if str(item.get("state") or "") != "closed":
        return ""
    return str(item.get("close_reason") or "clos")


def contract_result_at(ctx: Context, item: dict, sha: str):
    from ameesh.packet_contract_proof import read
    # Fetch the frozen object before resolving its tree, including a carrier
    # composed by another host. Never substitute the target's current tree.
    manifest_at(ctx, item, sha)
    result = ctx.git("rev-parse", sha + "^{tree}")
    tree = result.stdout.strip() if result.returncode == 0 else ""
    return read(item, head=sha, tree=tree, target=ctx.target) if tree else None


def waived(item: dict) -> bool:
    """#6933 : « verdict non requis » déjà posé (pose automatique d'un
    véhicule sans delta propre, #4109, ou tic précédent)."""
    names = item.get("reviewers")
    if names is None:
        names = [item.get("reviewer")]
    return REVIEWER_NONE in [str(name or "").strip() for name in names]


def waive(ctx: Context, item: dict, sha: str) -> str:
    """#6933 : pose « verdict non requis » ; un acteur qui n'a pas ce geste
    (composeur délégué, §12) le laisse au pilote, consigné, sans faire
    échouer le tic : la ci-full est demandée quand même."""
    carrier = int(item["id"])
    result = ctx.ameesh("work", "reviewer", str(carrier), "--no-verdict", "--actor", ctx.actor)
    if result.returncode == 0:
        return "Fusions propres seulement : « verdict non requis » posé (§2)."
    if not WAIVE_REFUSED_RE.search(result.text):
        raise HoraireError("verdict non requis #%d : rc %d : %s"
                           % (carrier, result.returncode, auto._tail(result.text, 20)))
    if waived(show(ctx, carrier)):
        return "Fusions propres seulement : « verdict non requis » déjà posé (§2)."
    if MARK_WAIVE_LEFT + sha not in marks(item):
        ctx.note(carrier, MARK_WAIVE_LEFT + sha + " (%s sans ce geste)" % ctx.actor)
    return ("« verdict non requis » refusé à %s : laissé au pilote (#1979) ; "
            "la fusion automatique attend ce geste." % ctx.actor)


def publish(ctx: Context, item: dict) -> str:
    """Tête gelée : « verdict non requis » si aucune résolution, puis ci-full."""
    carrier = int(item["id"])
    sha = frozen_sha(item)
    if not sha:
        return "#%d sans tête gelée : rien à publier" % carrier
    landed = merged_head(ctx, item, sha)
    if landed or str(item.get("state") or "") == "closed":
        # #4322 : la ci-full précoce (#4304) a pu fusionner le porteur avant
        # le retour de paquet-auto ; ni verdict ni ci-full sur un porteur clos
        return "#%d déjà %s : rien à publier" % (
            carrier, "fusionné" if landed else "clos")
    # #6665 (récidive de #6565) : une ci-full déjà terminée non verte sur
    # cette tête n'est jamais redemandée — `ci run` la refuse (« already ran…
    # none green »), le tic sortait en rc 1 et ouvrait un lot d'empreinte à
    # chaque passage. Le rouge suit son chemin ordinaire. Une lecture
    # impossible laisse la demande ordinaire décider, comme avant.
    try:
        run = run_for(ci_status(ctx, carrier), sha)
    except HoraireError:
        run = None
    if (run is not None and run.get("status") not in RUN_IN_FLIGHT
            and run.get("status") != "success"):
        noted = marks(item)
        if MARK_CI + sha not in noted:
            ctx.note(carrier, MARK_CI + sha)
        return finished_red(ctx, item, sha, run, noted)
    if publication_mode(ctx) != "legacy":
        proof = contract_result_at(ctx, item, sha)
        if proof is None or proof["result"]["state"] == "red":
            return attempt(ctx, carrier, auto.package_name(str(item.get("branch") or "")),
                           Attempt(RECOMPOSITION, 1, sha, run_id=CONTRACT_PROOF_RUN,
                                   only=carrier_group(item),
                                   publish=str(item["branch"]) + "@" + sha))
        if proof["result"]["state"] == "inconclusive":
            ctx.say("#%d : contrats NON CONCLUANTS sur %s ; ci-full requise, aucune preuve verte"
                    % (carrier, sha[:12]))
    units = composition_units(manifest_at(ctx, item, sha))
    resolutions = [unit for unit in units if unit.resolution]
    lines = ["#%d %s : tête gelée %s, %d unité(s)."
             % (carrier, item.get("branch"), sha[:12], len(units))]
    lines += ["- %s %s%s" % (", ".join("#%d" % lot for lot in unit.lots), unit.source[:12],
                             " (résolution)" if unit.resolution else "")
              for unit in units]
    if resolutions:
        lines.append("Résolution(s) présente(s) : « verdict non requis » reste au "
                     "pilote (#1979) ; la fusion automatique attend ce geste.")
        if MARK_WAIT + sha not in marks(item):
            ctx.note(carrier, MARK_WAIT + sha)
    elif waived(item):
        lines.append("Fusions propres seulement : « verdict non requis » déjà posé (§2).")
    elif not ctx.dry_run:
        lines.append(waive(ctx, item, sha))
    if not ctx.dry_run:
        ctx.must("ci", "run", str(carrier), "--full", "--sha", sha, "--avis", "0",
                 "--reason", "paquet horaire automatique (#3845)",
                 what="ci-full #%d" % carrier)
    ctx.note(carrier, MARK_CI + sha)
    lines.append("ci-full demandée sur %s ; fusion automatique à la CI verte." % sha[:12])
    ctx.mail(ctx.pilot, carrier, "\n".join(lines))
    return "#%d : ci-full demandée sur %s" % (carrier, sha[:12])


def composition_evidence(item: dict, now: float) -> str:
    """#6563 : une composition déjà enregistrée ou poussée, sans gel.

    Deux preuves partagées entre clones, lues avant de relancer une composition
    sur un porteur sans tête gelée :

    * une note `packet composition recorded` récente — `paquet-compose`
      enregistre chaque unité avant la poussée, donc la trace existe même
      quand le composeur n'a pas encore publié. Au-delà de `ATTEMPT_STALE`,
      la composition est réputée morte et le tic peut reprendre ;
    * une tête de branche vue (`branch_head`), c'est-à-dire une poussée sans
      gel : elle n'est jamais réécrite par le tic, c'est au pilote de trancher.
    """
    recorded = [float(ev.get("created_ts") or 0) for ev in _events(item)
                if str(ev.get("note") or "").startswith(COMPOSITION_RECORD_PREFIX)]
    if recorded and now - max(recorded) < ATTEMPT_STALE:
        minutes = max(0, int((now - max(recorded)) / 60))
        return ("composition enregistrée par un autre composeur (il y a %d min) : "
                "rien à faire" % minutes)
    head = str(item.get("branch_head") or "").strip()
    if head:
        return ("tête %s publiée sans gel par un autre composeur : rien à faire, "
                "le pilote tranche" % head[:12])
    return ""


def follow(ctx: Context, item: dict) -> str:
    """Un porteur horaire ouvert : publier, attendre, ou traiter un rouge."""
    carrier = int(item["id"])
    name = auto.package_name(str(item.get("branch") or ""))
    if name:
        holder = composition_in_progress(ctx, name)
        if holder:
            # #4329 : une composition (tick ou lancement à la main) tient le
            # verrou du paquet ; ne rien publier, retirer ni recomposer avant.
            return "#%d : composition en cours (%s)" % (carrier, holder)
    claim = carrier_composition(ctx, carrier)
    if claim:
        # #6563 : le bail partagé du porteur est tenu par un autre composeur
        # (agent désigné, autre clone) ; le tic ne relance rien.
        return "#%d : composition en cours (%s)" % (carrier, claim)
    sha = frozen_sha(item)
    if merged_head(ctx, item, sha):
        # #4506 : un porteur fusionné reste ouvert (qa) le temps de sa clôture
        # (hold du pilote, balayage) ; le tic n'y recompose ni ne republie.
        return "#%d : fusionné, rien à faire (tête %s dans %s)" % (
            carrier, sha[:12] or "aucune", ctx.target)
    if not sha and not attempts(item):
        # #6563 : sans tête gelée ni marque de tentative, une composition déjà
        # enregistrée ou une tête poussée par un autre composeur n'est pas
        # réécrite. La vérification précède `pending`, qui fabrique sinon une
        # « tentative 0 morte » pour tout porteur ancien sans marque.
        evidence = composition_evidence(item, ctx.now)
        if evidence:
            return "#%d : %s" % (carrier, evidence)
    left = pending(ctx, item, sha)
    if left is not None:
        return resume(ctx, item, left)
    if not sha:
        # #5941 (B1 auditeur) : un porteur ouvert sans gel ni tentative
        # (arrêt entre la création et `attempt`) ne doit pas réserver la cible
        # pour toujours. Aucune composition n'est en cours (contrôle plus
        # haut) ; on lance celle du groupe que le corps/la note du porteur
        # nomme, comme le ferait une tentative 0 reprise.
        if not name:
            return "#%d sans gel ni tentative : rien à faire" % carrier
        return attempt(ctx, carrier, name,
                       Attempt(COMPOSITION, 1, "", only=carrier_group(item)))
    # #4319: an unavailable exemption proof requests the ordinary full-CI
    # path, on the same carrier. The marker is scoped to its frozen head.
    requested = any(
        str(event.get("note") or "").startswith("auto-merge parallel-recompose ")
        and "sha=%s " % sha in str(event.get("note") or "")
        for event in item.get("events") or ())
    if requested:
        return attempt(ctx, carrier, name,
                       Attempt(RECOMPOSITION, 1, sha, run_id="parallel-proof",
                               only=carrier_group(item), force=True))
    noted = marks(item)
    if MARK_CI + sha not in noted:
        return publish(ctx, item)
    if publication_mode(ctx) != "legacy":
        proof = contract_result_at(ctx, item, sha)
        state = proof["result"]["state"] if proof else "absent (non qualifié)"
        ctx.say("#%d : résultat contrats %s sur %s ; résultat ci-full lu séparément"
                % (carrier, state, sha[:12]))
    run = run_for(ci_status(ctx, carrier), sha)
    if run is None or run.get("status") in RUN_IN_FLIGHT:
        return "#%d : ci-full en cours sur %s" % (carrier, sha[:12])
    if run.get("status") == "success":
        return "#%d : ci-full verte sur %s, fusion automatique" % (carrier, sha[:12])
    return finished_red(ctx, item, sha, run, noted)


def finished_red(ctx: Context, item: dict, sha: str, run: dict,
                 noted: list[str]) -> str:
    """La ci-full terminée non verte de la tête gelée : retrait déterministe
    et recomposition (une fois), sinon rouge consigné au pilote.

    #6665 : partagé par `follow` et par `publish`, qui y arrive quand la tête
    gelée porte déjà une ci-full rouge sans la marque `ci demandée` (arrêt
    entre la demande et la marque, ou reprise d'une tentative qui republie la
    même tête)."""
    carrier = int(item["id"])
    run_id = str(run.get("run_id") or "?")
    if any(note.startswith(MARK_RED + run_id) for note in noted):
        return "#%d : rouge %s déjà consigné" % (carrier, run_id)
    verdict = classify(run)
    if verdict.deterministic:
        why = replay_locally(ctx, sha, verdict.tests)
        if why:
            verdict = Verdict(False, verdict.lots, verdict.tests, why)
    # #4237 : une recomposition du pilote n'use pas le retrait automatique
    recomposed = len({a.run_id for a in attempts(item)
                      if a.step == RECOMPOSITION
                      and a.run_id not in (PILOT_RUN, "parallel-proof", CONTRACT_PROOF_RUN)})
    if verdict.deterministic and recomposed < MAX_RECOMPOSITIONS:
        return recompose(ctx, item, run, verdict)
    reason = verdict.why if not verdict.deterministic else \
        "retrait automatique épuisé (%d recomposition)" % recomposed
    # courrier puis marque : un arrêt entre les deux redit, il ne tait pas
    ctx.mail(ctx.pilot, carrier,
             "#%d %s : ci-full rouge (run %s, %s) ; rien retiré : %s. "
             "Un agent lit (ameesh ci status %d)."
             % (carrier, item.get("branch"), run_id, sha[:12], reason, carrier))
    ctx.note(carrier, MARK_RED + "%s : %s" % (run_id, reason))
    return "#%d : rouge consigné (%s)" % (carrier, reason)


def recompose(ctx: Context, item: dict, run: dict, verdict: Verdict) -> str:
    carrier = int(item["id"])
    branch = str(item.get("branch") or "")
    name = auto.package_name(branch)
    holder = composition_in_progress(ctx, name) if name else ""
    if holder:
        # #4329 : ne pas retirer d'unité ni écrire aux auteurs pour une
        # composition que le verrou dit déjà en cours.
        return "#%d : composition en cours (%s)" % (carrier, holder)
    run_id = str(run.get("run_id") or "?")
    removed = ", ".join("#%d" % lot for lot in verdict.lots)
    for lot in verdict.lots:
        try:
            author = str(show(ctx, lot).get("assignee") or "")
        except HoraireError:
            author = ""
        if author:
            ctx.mail(author, lot,
                     "#%d retiré du paquet %s : la ci-full %s (tête %s) est rouge, "
                     "attribuée à ton unité par la carte d'impact (#3801) et "
                     "rouge en local. Tests : %s. Le paquet est recomposé sans "
                     "ton lot ; corrige puis regèle, il reviendra à une coupure "
                     "suivante. %s"
                     % (lot, name, run_id, frozen_sha(item)[:12], ", ".join(verdict.tests),
                        run.get("url") or ""))
        exclude(ctx, lot, carrier, name, run_id, verdict.tests)
    ctx.mail(ctx.pilot, carrier,
             "#%d %s : ci-full rouge (run %s), retrait de %s (%s) ; recomposition."
             % (carrier, name, run_id, removed, ", ".join(verdict.tests)))
    return attempt(ctx, carrier, name, Attempt(RECOMPOSITION, 1, frozen_sha(item),
                                               run_id=run_id,
                                               exclude=list(verdict.lots),
                                               only=carrier_group(item)))


def exclude(ctx: Context, lot: int, carrier: int, name: str, run_id: str,
            tests: list[str]) -> None:
    """#4028 : le lot retiré pour un test négatif porte sa propre trace et
    repasse en `build` : il attend le correctif de son assigné, et un nouveau
    gel le ramène en qa (puis au paquet). Sans cela il restait en qa, gelé avec
    son GO, et l'attente « merge » faisait réclamer au bureau son inclusion au
    paquet suivant. #4043 : le retrait n'est pas une boucle d'auteur
    (`--package-exclusion` : `loops` inchangé, jamais refusé à la limite). Une
    transition refusée (lot déjà sorti de qa) laisse la trace seule, en note."""
    trace = "%s%s porteur=#%d run=%s tests=%s : correctif dû par l'assigné, " \
            "regel pour revenir au paquet" % (MARK_EXCLUDED, name, carrier, run_id,
                                              ",".join(tests) or "?")
    if ctx.dry_run:
        ctx.say("[essai] #%d → build : %s" % (lot, trace))
        return
    moved = ctx.ameesh("work", "move", str(lot), "build", "--package-exclusion",
                       "--note", trace, "--actor", ctx.actor)
    if moved.returncode == 0:
        return
    ctx.say("#%d non repassé en build : %s" % (lot, auto._tail(moved.text, 5)))
    try:
        ctx.note(lot, trace)
    except HoraireError as exc:
        ctx.say("trace d'exclusion de #%d non écrite : %s" % (lot, exc))


def check_fresh(ctx: Context) -> None:
    """Le code exécuté est-il celui de la tête exacte de la cible ? Sinon, rien.

    La synchronisation de la minuterie (`git fetch` puis `git merge
    --ff-only` sur la référence nommée, #6128) peut échouer (réseau,
    divergence, autre branche) : ce tic chargerait alors un ancien
    orchestrateur et écrirait quand même. Il faut donc ce script dans le
    clone, la tête locale égale à `origin/<cible>` tout juste lue, et aucun
    fichier suivi modifié.
    """
    if SCRIPT_DIR != (ctx.repo / "scripts").resolve():
        raise HoraireError("script lancé hors du clone %s (%s)" % (ctx.repo, SCRIPT_DIR))
    fetched = ctx.git("fetch", "--tags", "--force", "origin", ctx.target, timeout=300)
    if fetched.returncode != 0:
        raise HoraireError("fetch %s : %s" % (ctx.target, auto._tail(fetched.text, 5)))
    local = ctx.git("rev-parse", "HEAD^{commit}")
    remote = ctx.git("rev-parse", "origin/%s^{commit}" % ctx.target)
    if local.returncode or remote.returncode or not remote.stdout.strip():
        raise HoraireError("têtes illisibles (HEAD, origin/%s)" % ctx.target)
    head = local.stdout.strip()
    if head != remote.stdout.strip():
        raise Stale("clone à %s, %s à %s : code périmé, aucun geste"
                    % (head[:12], ctx.target, remote.stdout.strip()[:12]))
    if ctx.executed_head and ctx.executed_head != head:
        raise Stale("code chargé à %s, clone et %s à %s : code périmé, aucun geste"
                    % (ctx.executed_head[:12], ctx.target, head[:12]))
    ctx.executed_head = head
    dirty = ctx.git("status", "--porcelain", "--untracked-files=no")
    if dirty.returncode or dirty.stdout.strip():
        raise HoraireError("clone modifié : aucun geste\n%s" % auto._tail(dirty.text, 5))


#: `agent-mail migrate` chargé depuis `src` du clone (argv[1]), pas depuis le
#: paquet installé : ce sont les migrations de la tête qu'il faut poser.
MIGRATE_CODE = ("import sys; sys.path.insert(0, sys.argv[1]); from ameesh import cli; "
                "sys.exit(cli.main(['migrate', sys.argv[2]]))")


#: le bureau reçoit, avec le pilote, l'échec de pose (#4075)
BUREAU = "bureau"
MISSING_RE = re.compile(r"^(\d{4}_[A-Za-z0-9._-]+): (additive|NOT additive)", re.MULTILINE)
DENIED_RE = re.compile(r"permission denied|must be owner|insufficient.privilege|42501",
                       re.IGNORECASE)
#: #5097 : `lock_timeout` (5 s) dépassé sur une table chaude ; passager, un
#: second essai suit avant toute alerte (h-10 : 0529 refusée à 11:40, posée à 11:50).
LOCK_RE = re.compile(r"lock timeout|55P03", re.IGNORECASE)


def _migrate(ctx: Context, flag: str) -> Result:
    return ctx.shell.run([ctx.python, "-c", MIGRATE_CODE, str(ctx.repo / "src"), flag],
                         cwd=ctx.repo, timeout=COMMAND_TIMEOUT)


def align_schema(ctx: Context) -> None:
    """#4075 : la base partagée porte-t-elle les migrations de la tête ?

    Le tic et `paquet-candidats` exécutent le code de la tête exacte de la
    cible (#3782, `check_fresh`) contre la base partagée. Un paquet qui vient
    de fusionner une migration rend ce code incompatible avec la base tant que
    personne ne l'a posée (h-45 : « column operator_pause_until does not
    exist » à chaque tic, jusqu'à la pose manuelle de 0499). Le tic lit donc
    d'abord les migrations manquantes de la tête (`--dry-run`, lecture seule)
    et pose les **additives** (§11 bis : l'ancien code des exécuteurs reste
    compatible). Une migration non additive n'est jamais posée ici, et un
    refus de la base (droit DDL absent du rôle du tic) n'est pas contourné :
    aucun geste, l'échec nomme les migrations et la cause, et `alert_schema`
    prévient le pilote et le bureau. En essai, seule la lecture a lieu.

    Pas d'instantané avant pose : `migrations.migrate` pose toute la passe
    dans une seule transaction (verrou consultatif, `lock_timeout` 5 s) et
    refuse en bloc dès qu'une manquante n'est pas additive ; un échec ne
    laisse rien de posé, une passe réussie n'ajoute que des objets.
    """
    read = _migrate(ctx, "--dry-run")
    missing = MISSING_RE.findall(read.stdout)
    if read.returncode != 0:
        fail_schema(ctx, "lecture", [], read)
    if not missing:
        return
    labels = [label for label, _ in missing]
    refused = [label for label, kind in missing if kind != "additive"]
    if refused:
        fail_schema(ctx, "non-additive", labels, read)
    if ctx.dry_run:
        ctx.say("[essai] migrations à poser : %s" % ", ".join(labels))
        return
    result = _migrate(ctx, "--additive-only")
    if result.returncode != 0 and LOCK_RE.search(result.text):
        ctx.say("schéma : verrou non obtenu (%s), second essai"
                % auto._tail(result.text, 1))
        result = _migrate(ctx, "--additive-only")
    if result.returncode != 0:
        kind = "droit-ddl" if DENIED_RE.search(result.text) else (
            "non-additive" if result.returncode == 3 else
            "verrou" if LOCK_RE.search(result.text) else "pose")
        fail_schema(ctx, kind, labels, result)
    ctx.say("schéma : %s posée(s) sur la base partagée" % ", ".join(labels))


SCHEMA_WHY = {
    "lecture": "lecture des migrations impossible",
    "non-additive": "migration non additive en attente : jamais posée par le tic (§11 bis)",
    "droit-ddl": "le rôle DB du tic n'a pas le droit DDL",
    "verrou": "verrou de table non obtenu deux fois (lock_timeout)",
    "pose": "pose refusée par la base",
}


def fail_schema(ctx: Context, kind: str, labels: list[str], result: Result) -> None:
    """Aucun geste ; pilote et bureau prévenus une fois par empreinte."""
    what = ", ".join(labels) or "?"
    message = ("schéma de la tête de %s non aligné (%s) : %s, %s : aucun geste\n%s"
               % (ctx.target, kind, what, SCHEMA_WHY[kind], auto._tail(result.text, 10)))
    alert_schema(ctx, kind, what, message)
    error = HoraireError(message)
    error.alerted = True
    raise error


def alert_schema(ctx: Context, kind: str, what: str, message: str) -> None:
    if ctx.dry_run:
        ctx.say("[essai] alerte schéma %s : %s" % (kind, what))
        return
    alert_once(ctx, "paquet-horaire:schema:%s:%s" % (kind, what),
               "Tic horaire bloqué : migrations %s non posées (%s)" % (what, kind),
               kind, message)


def tick_fingerprint(error: BaseException) -> str:
    """#5696 : l'empreinte d'un échec du tic, sans ce qui change d'une heure
    à l'autre (nom du paquet, numéros, SHA), pour qu'un même échec répété
    toutes les dix minutes note un seul lot."""
    text = str(error).splitlines()[0] if str(error) else type(error).__name__
    text = re.sub(r"h-\d{8}-\d+", "h-*", text)
    text = re.sub(r"\b[0-9a-f]{7,40}\b", "*", text)
    text = re.sub(r"\d+", "N", text)
    return text[:120]


def alert_tick(ctx: Context, error: BaseException) -> None:
    """#5696 : le tic sort en 1 (panne du composeur ou échec opérationnel) :
    pilote et bureau prévenus une fois par empreinte, comme le schéma. #6272 :
    un rouge de composition sort en COMPOSITION_RED_RC et ne passe pas ici.
    Une alerte perdue ne change pas le code de sortie."""
    message = ("tic paquet-horaire de %s sur %s en échec (rc 1) : %s\n%s"
               % (ctx.target, ctx.host, error, "\n".join(ctx.log[-10:])))
    if ctx.dry_run:
        ctx.say("[essai] alerte tic : %s" % error)
        return
    try:
        alert_once(ctx, "paquet-horaire:tick:%s" % tick_fingerprint(error),
                   "Tic horaire en échec : %s" % auto._tail(str(error), 1)[:120],
                   "tick", message)
    except Exception as failure:  # noqa: BLE001 - l'alerte ne masque pas l'échec
        print("paquet-horaire : alerte non posée (%s)" % failure, file=sys.stderr)


def alert_once(ctx: Context, key: str, title: str, code: str, message: str) -> None:
    """Un lot par empreinte (`work fingerprint open`, #3885) : le tic suivant,
    même échec, note ce lot au lieu d'écrire de nouveau ; un nouvel échec ou
    une récidive après clôture ouvre un lot et écrit au pilote et au bureau.
    Sans lot lisible, le courrier part quand même : se taire est pire."""
    opened = ctx.ameesh(
        "work", "fingerprint", "open", "--app", ctx.project,
        "--key", key, "--title", title,
        "--code", code, "--actor", ctx.actor, "--json")
    lot = 0
    try:
        payload = json.loads(opened.stdout) if opened.returncode == 0 else {}
        if payload.get("created") is False:
            return
        lot = int(payload.get("lot") or 0)
    except (ValueError, TypeError, AttributeError):
        lot = 0
    if lot:
        # #5097 : la cause vit dans le lot, pas seulement dans le courrier
        # (h-10 : lot ouvert avec le seul code « pose », courrier jamais lu).
        noted = ctx.ameesh("work", "note", str(lot), message, "--actor", ctx.actor)
        if noted.returncode != 0:
            ctx.say("note du lot #%d non écrite : %s" % (lot, auto._tail(noted.text, 5)))
    for to in dict.fromkeys((ctx.pilot, BUREAU)):
        cmd = ["agent-mail", "send", to, "-", "--from", ctx.actor]
        if lot:
            cmd += ["--lot", str(lot)]
        sent = ctx.shell.run(cmd, input_text=message, timeout=120)
        if sent.returncode != 0:
            ctx.say("courrier à %s non envoyé : %s" % (to, auto._tail(sent.text, 5)))


def paused(ctx: Context) -> str | None:
    """#4361 : la pause bornée de la cible, lue au journal partagé ; une
    lecture impossible n'autorise aucun geste."""
    if ctx.pause_of is None:
        return None
    try:
        data = ctx.pause_of()
    except Exception as error:  # noqa: BLE001 - base illisible : aucun geste
        raise HoraireError("pause illisible (%s) : aucun geste" % error) from error
    if not data:
        return None
    from ameesh import packet_pause
    return "%s %s : aucun geste" % (ctx.target, packet_pause.describe(data, ctx.zone))


def run(ctx: Context) -> str:
    """Un tic complet : pause d'abord (#4361 : ni migration ni composition
    pendant un staging hors créneau), puis fraîcheur, schéma et gestes."""
    held = paused(ctx)
    if held:
        return held
    check_fresh(ctx)
    align_schema(ctx)
    return tick(ctx)


def tick(ctx: Context) -> str:
    rows = open_lots(ctx)
    carriers = [row for row in rows if is_carrier(row, ctx.project, ctx.target)]
    reconciled = False
    for row in carriers:
        if not is_hourly(row) or "paquet-horaire" not in str(row.get("source") or ""):
            continue
        item = show(ctx, int(row["id"]))
        name = auto.package_name(str(item.get("branch") or ""))
        if name and composition_in_progress(ctx, name):
            continue
        sha = frozen_sha(item)
        if not sha or not merged_head(ctx, item, sha):
            continue
        # #5891 : une note de forge seule ne suffit pas. La cible rafraîchie
        # doit porter la tête, puis la voie native vérifie contenu, dernier
        # gel et holds sous le verrou du lot. Un hold garde le porteur ouvert.
        if ctx.git("merge-base", "--is-ancestor", sha, "origin/" + ctx.target).returncode:
            continue
        if ctx.dry_run:
            continue
        ctx.must("work", "sync-merges", "--id", str(item["id"]),
                 "--git-dir", str(ctx.repo), "--target", "origin/" + ctx.target,
                 what="constat de fusion du porteur #%d" % item["id"])
        if merged_seen(show(ctx, int(item["id"]))):
            reconciled = True
    if reconciled:
        # Relire aussi les autres réservations : ni récursion ni second tic.
        rows = open_lots(ctx)
        carriers = [row for row in rows if is_carrier(row, ctx.project, ctx.target)]
    if carriers:
        own = [row for row in carriers if is_hourly(row)
               and "paquet-horaire" in str(row.get("source") or "")]
        # #3256 C : une paire parallèle (premier + second) se suit sur place ;
        # le second ne fusionne qu'après le premier (packet_automerge).
        limit = int(getattr(ctx.parallel, "max_paquets", 1) or 1) \
            if ctx.parallel is not None else 1
        if len(own) == len(carriers) == 2 and limit >= 2:
            items = [show(ctx, int(row["id"])) for row in own]
            pair = all(carrier_group(item) or second_peer(item) is not None
                       for item in items)
            if pair:
                items.sort(key=lambda item: (float(item.get("created_ts") or 0),
                                             int(item["id"])))
                return "\n".join(follow(ctx, item) for item in items)
        if len(own) == 1 and len(carriers) == 1:
            return follow(ctx, show(ctx, int(own[0]["id"])))
        return "%s réservée par %s : rien à faire" % (
            ctx.target, ", ".join("#%d" % int(row["id"]) for row in carriers))
    return compose(ctx, recent_lots(ctx))


def session_epoch(ctx: Context) -> int:
    """#4237 : l'époque du bail de la session qui lance une recomposition.

    `AMEESH_LEASE_EPOCH` sous `ameesh role run` ; sinon la garde `packet` de
    `pilot_gesture`, lue par le Python du projet : une session de pilote liée
    par son runner rend son époque, une session qui ne détient plus le bail
    est refusée ici, avant toute marque. Un humain ou le bureau (aucune garde)
    rend 0 : la marque n'a pas d'époque et le délai de composition s'applique.
    """
    if ctx.epoch:
        return ctx.epoch
    code = (
        "import sys\n"
        "sys.path.insert(0, sys.argv[2])\n"
        "from ameesh import config, db, pilot_gesture\n"
        "conn = db.connect(config.load())\n"
        "try:\n"
        "    gate = pilot_gesture.gate(None, conn, 'packet', sys.argv[1])\n"
        "finally:\n"
        "    if conn is not None:\n"
        "        conn.close()\n"
        "print(gate.epoch if gate is not None else 0)\n")
    result = ctx.shell.run([ctx.python, "-c", code, "paquet-horaire recompose",
                            str(ctx.repo / "src")],
                           cwd=ctx.repo, timeout=60)
    if result.returncode != 0:
        raise HoraireError("recomposition refusée, bail non vérifié : %s"
                           % auto._tail(result.text, 5))
    try:
        return max(int(result.stdout.strip().splitlines()[-1]), 0)
    except (ValueError, IndexError):
        raise HoraireError("époque illisible : %r" % result.stdout[-200:]) from None


def assigned_clone(ctx: Context) -> str:
    """#4562 : le nom de l'agent dont `ctx.repo` est le dossier de travail, ou "".

    Une recomposition compose dans le clone qu'on lui donne. Lancée dans le
    clone d'un agent, `paquet-auto` y laisse une branche `integration/**`
    locale avant de la pousser : le balayage de cet agent la lit, l'enregistre
    comme tête vue en avance, et la garde #4501 la prend ensuite pour une tête
    poussée. Le clone dédié du composeur (`AMEESH_PAQUET_REPO`, comme le tic)
    n'appartient à aucun agent ; lui seul est admis.
    """
    result = ctx.must("list", "--json", what="liste des agents")
    rows = _json(result, "liste des agents")
    if not isinstance(rows, list):
        raise HoraireError("liste des agents : liste JSON attendue")
    repo = os.path.realpath(str(ctx.repo))
    host = str(ctx.host or "")
    for row in rows:
        row_host = str(row.get("host") or "")
        if host and row_host and row_host != host:
            continue
        cwd = str(row.get("cwd") or "").strip()
        if not cwd:
            continue
        if os.path.realpath(os.path.expanduser(cwd)) == repo:
            return str(row.get("name") or "?")
    return ""


def refuse_agent_clone(ctx: Context) -> None:
    """#4562 : refuse un `--repo` qui est le dossier de travail d'un agent."""
    if ctx.clone_checked:
        return
    holder = assigned_clone(ctx)
    if not holder:
        ctx.clone_checked = True
        return
    raise InputError(
        "--repo %s est le dossier de travail de l'agent %s ; une "
        "recomposition y laisserait une branche locale non poussée que le "
        "balayage lirait comme tête de la forge (#4562). Lance-la depuis "
        "le clone dédié du composeur (AMEESH_PAQUET_REPO, "
        "~/ameesh-composeur) ou laisse le tic recomposer."
        % (ctx.repo, holder))


def pilot_recompose(ctx: Context, carrier: int, exclude: list[int],
                    reasons: list[str], *, wait: float = 0, cancel_ci: bool = False,
                    expect_head: str = "") -> str:
    """#4237 : la recomposition du pilote, marquée comme celles du tic.

    #5696 : `wait` (secondes) attend la fin d'une tentative vivante ou d'une
    ci-full au lieu de refuser ; `cancel_ci` annule cette ci-full (une fois) ;
    `expect_head` ne recompose que si la tête gelée est encore celle-là (le
    tic a pu recomposer entre-temps : rien à faire, sortie 0). Ces trois
    options remplacent les enveloppes `systemd-run` écrites à la main, sorties
    en 2 le 03/11 (h16, h17, h19).

    #5703 : `cancel_ci` sans `wait` est refusé avant tout geste (il annulait
    la ci-full puis sortait en 2 sur le relevé en retard, sans recomposer).
    """
    if reasons and len(reasons) != len(exclude):
        raise InputError("--exclude-reason : %d motif(s) pour %d --exclude"
                         % (len(reasons), len(exclude)))
    if cancel_ci and wait <= 0:
        raise InputError("--cancel-ci exige --wait : le relevé de la ci-full annulée "
                         "reste « in_progress » un moment, la recomposition serait "
                         "refusée après l'annulation")
    refuse_agent_clone(ctx)
    deadline = ctx.clock() + max(wait, 0)
    cancelled: set[str] = set()
    while True:
        try:
            return _pilot_recompose_once(ctx, carrier, exclude, reasons, expect_head)
        except Busy as error:
            if cancel_ci and error.run_id and error.run_id not in cancelled:
                cancelled.add(error.run_id)
                if ctx.dry_run:
                    ctx.say("[essai] ci cancel #%d --all" % carrier)
                else:
                    ctx.must("ci", "cancel", str(carrier), "--all",
                             what="annulation de la ci-full de #%d" % carrier)
                ctx.say("#%d : ci-full %s annulée" % (carrier, error.run_id))
            left = deadline - ctx.clock()
            if left <= 0:
                raise
            ctx.say("%s ; nouvel essai dans %d s" % (error, min(WAIT_POLL, left)))
            ctx.sleep(min(WAIT_POLL, left))
            # B cx3 : la cible a pu avancer pendant l'attente ; le code chargé
            # doit rester celui de sa tête exacte avant tout geste.
            check_fresh(ctx)


def _pilot_recompose_once(ctx: Context, carrier: int, exclude: list[int],
                          reasons: list[str], expect_head: str) -> str:
    item = show(ctx, carrier)
    if not (item.get("state") in OPEN_STATES
            and is_carrier(item, ctx.project, ctx.target) and is_hourly(item)
            and "paquet-horaire" in str(item.get("source") or "")):
        raise InputError("#%d n'est pas un porteur horaire ouvert sur %s : aucun tic "
                         "ne reprendrait sa recomposition" % (carrier, ctx.target))
    sha = frozen_sha(item)
    expected = expect_head.strip().lower()
    if expected and not sha:
        # #5703 : sans gel, rien ne prouve que la tête attendue a été remplacée
        raise InputError("#%d : aucune tête gelée, --expect-head %s invérifiable"
                         % (carrier, expected[:12]))
    if expected and not sha.startswith(expected):
        return ("#%d : tête gelée %s, plus %s (recomposée entre-temps) : rien à faire"
                % (carrier, sha[:12], expected[:12]))
    left = pending(ctx, item, sha)
    if left is not None and running(ctx, left):
        raise Busy("#%d : %s en cours (%s:%d), attendre sa fin"
                   % (carrier, left.step, left.host, left.pid))
    claim = carrier_composition(ctx, carrier)
    if claim:
        # #6563 : un composeur (agent désigné) tient le bail partagé du
        # porteur ; la recomposition du pilote attend sa fin.
        raise Busy("#%d : composition en cours (%s), attendre sa fin"
                   % (carrier, claim))
    # #4501 : une ci-full tourne sur la tête poussée ; la recomposition
    # l'écraserait au lieu d'attendre son verdict.
    run = ci_in_progress(ctx, item, forge=True)
    if run is not None:
        raise Busy(
            "#%d : ci-full en cours sur %s (run %s), recomposition refusée"
            % (carrier, sha[:12], run.get("run_id") or "?"),
            run_id=str(run.get("run_id") or "?"))
    ctx.epoch = session_epoch(ctx)
    name = str(item.get("branch") or "")[len(auto.PACKAGE_PREFIX):]
    ctx.say("#%d : recomposition du pilote sans %s" % (
        carrier, ", ".join("#%d" % lot for lot in exclude) or "aucun lot"))
    return attempt(ctx, carrier, name, Attempt(RECOMPOSITION, 1, sha, run_id=PILOT_RUN,
                                               exclude=list(exclude),
                                               only=carrier_group(item),
                                               reasons=list(reasons)))


def pose_git_identity(pilot: str) -> dict:
    """#3916 : la minuterie compose sous l'identité de son rôle pilote.

    `ameesh role run` ne pose aucune identité git et le clone de la minuterie
    n'a pas de `user.*` (à juste titre) : `git merge` échouait « Committer
    identity unknown ». Comme le runner (#834), l'identité passe par
    l'**environnement** (auteur et committer), hérité par paquet-auto et ses
    `git`, jamais par `git config`.
    """
    from ameesh import identity
    env = identity.git_identity(pilot)
    os.environ.update(env)
    return env


DURATION_RE = re.compile(r"^(\d+)\s*(h|m|min)$")
CLOCK_RE = re.compile(r"^(\d{1,2}):(\d{2})$")


def parse_wait(text: str) -> float:
    """#5696 : `--wait` en secondes : 600, 600s, 90m, 2h ; 12 h au plus."""
    match = re.match(r"^\s*(\d+)\s*(s|m|min|h)?\s*$", str(text or "0"))
    if not match:
        raise InputError("--wait illisible : %r (600s, 90m, 2h)" % text)
    seconds = int(match.group(1)) * {"": 1, "s": 1, "m": 60, "min": 60,
                                     "h": 3600}[match.group(2) or ""]
    if seconds > 12 * 3600:
        raise InputError("--wait : 12 h au plus")
    return float(seconds)


def parse_until(text: str, now: float, zone: ZoneInfo) -> float:
    """#4361 : l'échéance d'une pause : durée (`90m`, `2h`), heure locale du
    canon (`20:15`, prochaine occurrence) ou date ISO (sans fuseau : celui du
    canon)."""
    text = (text or "").strip()
    match = DURATION_RE.match(text)
    if match:
        n = int(match.group(1))
        return now + n * (3600 if match.group(2) == "h" else 60)
    match = CLOCK_RE.match(text)
    if match:
        hour, minute = int(match.group(1)), int(match.group(2))
        if hour > 23 or minute > 59:
            raise InputError("heure invalide : %s" % text)
        base = datetime.fromtimestamp(now, zone).replace(
            hour=hour, minute=minute, second=0, microsecond=0)
        when = base.timestamp()
        if when <= now:
            when = (base + timedelta(days=1)).timestamp()
        return when
    try:
        when = datetime.fromisoformat(text)
    except ValueError:
        raise InputError("échéance illisible : %r (90m, 2h, 20:15 ou date ISO)" % text) from None
    if when.tzinfo is None:
        when = when.replace(tzinfo=zone)
    return when.timestamp()


def _connect():
    from ameesh import config as config_mod, db as db_mod
    return db_mod.connect(config_mod.load())


def read_pause(project: str, target: str, *, pilot: str = "mesh-design",
               notify: bool = True, now: float | None = None) -> dict | None:
    """#4361 : la pause active de la cible, une lecture par tic."""
    from ameesh import packet_pause
    db = _connect()
    try:
        data = packet_pause.active(db, project=project, target=target, now=now)
        if notify:
            packet_pause.alert_due(db, data, project=project, target=target,
                                   recipients=(pilot, BUREAU), now=now)
        return data
    finally:
        close = getattr(db, "close", None)
        if close:
            close()


def read_composition(project: str):
    """#5941 : le bail de composition du tic, sur la base partagée.

    Il n'est fabriqué qu'après la lecture des candidats : la connexion reste
    ouverte le temps de la section critique (relecture des porteurs, création)
    et `Lease.close` la referme après la restitution.
    """
    from ameesh import packet_composition
    return packet_composition.Lease(
        _connect(), project,
        owner=packet_composition.owner("paquet-horaire", socket.gethostname(),
                                       os.getpid()))


def read_carrier_claim(project: str, carrier: int) -> dict | None:
    """#6563 : le bail de composition d'un porteur existant, ou None.

    Lecture seule, sur une connexion courte : le tic ne prend pas le bail
    (c'est le composeur qui le tient), il refuse seulement de relancer une
    composition qu'un autre a déjà commencée.
    """
    from ameesh import packet_composition
    db = _connect()
    try:
        return packet_composition.holder(db, project, carrier=int(carrier))
    finally:
        close = getattr(db, "close", None)
        if close:
            close()


def _zone() -> tuple[ZoneInfo, object]:
    from ameesh import config as config_mod, mechanics
    rules = mechanics.configured(config_mod.load())
    return ZoneInfo(rules.timezone), rules


def tell(shell: Shell, pilot: str, body: str) -> None:
    """La pause et sa levée sont dites au pilote et au bureau (#4361)."""
    for to in dict.fromkeys((pilot, BUREAU)):
        sent = shell.run(["agent-mail", "send", to, "-"], input_text=body, timeout=120)
        if sent.returncode != 0:
            print("courrier à %s non envoyé : %s" % (to, auto._tail(sent.text, 5)),
                  file=sys.stderr)


def main_pause(args, *, connect=_connect, shell: Shell | None = None,
               now: float | None = None) -> int:
    """`pause`, `resume`, `status` (#4361) : l'état vit au journal partagé."""
    from ameesh import packet_pause
    try:
        zone, _ = _zone()
    except Exception as error:  # noqa: BLE001 - sans canon, aucune heure sûre
        print("paquet-horaire : fuseau du canon compilé illisible (%s)" % error,
              file=sys.stderr)
        return 1
    now = time.time() if now is None else now
    by = args.by or os.environ.get("AGENT_MAIL_NAME") or os.environ.get("AMEESH_ROLE") \
        or os.environ.get("USER") or "inconnu"
    try:
        db = connect()
    except Exception as error:  # noqa: BLE001 - base injoignable
        print("paquet-horaire : base injoignable (%s)" % error, file=sys.stderr)
        return 1
    where = "%s/%s" % (args.project, args.target)
    try:
        if args.command == "status":
            data = packet_pause.active(db, project=args.project, target=args.target, now=now)
            print("%s : %s" % (where, packet_pause.describe(data, zone) if data
                               else "aucune pause, le tic compose"))
            return 0
        if args.command == "resume":
            before = packet_pause.active(db, project=args.project, target=args.target, now=now)
            packet_pause.resume(db, project=args.project, target=args.target, by=by,
                                source=getattr(args, "source", ""))
            text = "paquet horaire %s : pause levée par %s%s" % (
                where, by, "" if before else " (aucune pause active)")
            if getattr(args, "source", ""):
                text += " ; annonce : " + args.source
        else:
            until = parse_until(args.until, now, zone)
            data = packet_pause.pause(db, project=args.project, target=args.target,
                                      until=until, reason=args.reason, by=by, now=now,
                                      await_announcement=getattr(args, "await_announcement", False))
            end = ("échéance = alerte, reprise après annonce explicite sourcée "
                   "(`paquet-horaire.py resume --source …`)"
                   if data.get("await_announcement") else
                   "le tic reprend seul à l'échéance (`paquet-horaire.py resume` pour lever avant)")
            text = "paquet horaire %s : %s ; %s" % (
                where, packet_pause.describe(data, zone), end)
    except InputError as error:
        print("paquet-horaire : %s" % error, file=sys.stderr)
        return 2
    except packet_pause.PauseError as error:
        print("paquet-horaire : pause refusée : %s" % error, file=sys.stderr)
        return 2
    print(text, flush=True)
    tell(shell or Shell(), args.pilot, text)
    return 0


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(
        description="Paquet horaire déclenché par minuterie, sans agent (#3845).")
    sub = parser.add_subparsers(dest="command", required=True)
    one = sub.add_parser("tick", help="un tic : composer, publier ou suivre")
    again = sub.add_parser(
        "recompose", help="#4237 : recomposition du pilote, marquée « tentative » "
                          "et reprise par le tic du détenteur courant")
    again.add_argument("--carrier", type=int, required=True, help="porteur horaire")
    again.add_argument("--exclude", type=int, action="append", default=[],
                       metavar="LOT", help="lot retiré (répétable)")
    again.add_argument("--exclude-reason", action="append", default=[], metavar="TEXTE",
                       help="motif au manifeste, apparié par ordre aux --exclude")
    again.add_argument("--wait", default="0", metavar="DURÉE",
                       help="#5696 : attendre la fin du tic ou de la ci-full "
                            "(90m, 2h, 600s ; défaut : refuser tout de suite)")
    again.add_argument("--cancel-ci", action="store_true",
                       help="#5696 : annuler la ci-full en cours du porteur, puis "
                            "attendre (exige --wait)")
    again.add_argument("--expect-head", default="", metavar="SHA",
                       help="#5696 : rien à faire (sortie 0) si la tête gelée a "
                            "changé ; refusé (sortie 2) sans tête gelée")
    for each in (one, again):
        each.add_argument("--repo", default=".", help="clone du projet (défaut : .)")
        each.add_argument("--actor", default=None,
                          help="auteur des gestes (défaut : AMEESH_ROLE, sinon --pilot)")
        each.add_argument("--python", default=None,
                          help="interpréteur des scripts enfants (défaut : celui-ci)")
        each.add_argument("--dry-run", action="store_true",
                          help="lire et dire, sans créer, pousser, noter ni écrire")
    hold = sub.add_parser("pause", help="#4361 : suspendre le tic jusqu'à une échéance")
    hold.add_argument("--until", required=True,
                      help="échéance obligatoire : 90m, 2h, 20:15 (canon) ou date ISO")
    hold.add_argument("--reason", required=True, help="raison, dite au tic et au pilote")
    hold.add_argument("--await-announcement", action="store_true",
                      help="#5045 : échéance = alerte ; reprise sur annonce sourcée seulement")
    lift = sub.add_parser("resume", help="#4361 : lever la pause avant l'échéance")
    lift.add_argument("--source", default="",
                      help="source de l'annonce de fin : auteur, date, message (200 caractères max)")
    look = sub.add_parser("status", help="#4361 : la pause active, s'il y en a une")
    for each in (one, again, hold, lift, look):
        each.add_argument("--target", default="main")
        each.add_argument("--project", default="ameesh")
        each.add_argument("--pilot", default="mesh-design",
                          help="destinataire des courriers et assigné du porteur")
    for each in (hold, lift, look):
        each.add_argument("--by", default=None,
                          help="auteur (défaut : AGENT_MAIL_NAME, AMEESH_ROLE, USER)")
    args = parser.parse_args(argv)
    if not use_tree_library():
        loaded = getattr(sys.modules.get("ameesh"), "__file__", "") or "aucune"
        print("paquet-horaire : bibliothèque ameesh de l'arbre %s indisponible "
              "ou déjà chargée ailleurs (%s) : aucun geste" % (TREE_SRC, loaded),
              file=sys.stderr)
        return 1
    if args.command in ("pause", "resume", "status"):
        return main_pause(args)
    repo = Path(args.repo).resolve()
    if not (repo / ".git").exists():
        print("paquet-horaire : --repo %s n'est pas un clone" % repo, file=sys.stderr)
        return 2
    actor = args.actor or os.environ.get("AMEESH_ROLE") or args.pilot
    pose_git_identity(args.pilot)
    try:
        zone, document = _zone()
        unit_tests = getattr(document, "packet_tests", None)
        parallel = getattr(document, "paquet", None)
        from ameesh.packet_contract_proof import from_document
        contract_publication = from_document(document, args.project)
    except Exception as error:  # noqa: BLE001 - sans canon, aucune heure sûre
        print("paquet-horaire : fuseau du canon compilé illisible (%s)" % error,
              file=sys.stderr)
        return 1
    ctx = Context(repo=repo, target=args.target, project=args.project,
                  pilot=args.pilot, actor=actor, shell=Shell(),
                  python=args.python or auto.project_python(repo),
                  dry_run=args.dry_run, zone=zone, epoch=lease_epoch(),
                  unit_tests=unit_tests, parallel=parallel,
                  contract_publication=contract_publication,
                  pause_of=lambda: read_pause(args.project, args.target,
                                               pilot=args.pilot, notify=not args.dry_run),
                  # #5941 : le verrou partagé avec `ameesh work add` ; la
                  # fabrique ne se connecte qu'au moment de composer.
                  composition=lambda: read_composition(args.project),
                  # #6563 : le bail par porteur tenu par `paquet-auto` ; une
                  # lecture par porteur suivi, jamais une prise.
                  carrier_claim=lambda carrier: read_carrier_claim(
                      args.project, carrier))
    try:
        if args.command == "recompose":
            # geste explicite du pilote (#4237) : la pause (#4361) ne vise que le tic
            refuse_agent_clone(ctx)
            check_fresh(ctx)
            align_schema(ctx)
            print(pilot_recompose(ctx, args.carrier, args.exclude, args.exclude_reason,
                                  wait=parse_wait(args.wait), cancel_ci=args.cancel_ci,
                                  expect_head=args.expect_head),
                  flush=True)
        else:
            print(run(ctx), flush=True)
        return 0
    except InputError as error:
        print("paquet-horaire : %s" % error, file=sys.stderr)
        return 2
    except Stale as error:
        # #5696 : refus attendu (hôte en retard sur la cible), distinct d'une panne
        print("paquet-horaire : refus attendu : %s" % error, file=sys.stderr)
        return STALE_RC
    except CompositionRed as error:
        # #6272 : paquet rouge par son contenu, déjà rapporté au pilote par
        # after_compose ; pas d'alerte ni de lot d'empreinte §16.
        print("paquet-horaire : %s" % error, file=sys.stderr)
        return COMPOSITION_RED_RC
    except (HoraireError, auto.PaquetAutoError) as error:
        print("paquet-horaire : %s" % error, file=sys.stderr)
        if args.command == "tick" and not getattr(error, "alerted", False):
            alert_tick(ctx, error)
        return 1


if __name__ == "__main__":
    sys.exit(main())
