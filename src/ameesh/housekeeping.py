# SPDX-License-Identifier: AGPL-3.0-only
"""Ménage de ce que les agents créent (lot L73 ; décision 0028 point 4 ; lot
de base #1, cycle de vie des worktrees).

ameesh fait le ménage de ce que SES agents créent, au lieu de le laisser à
l'humain. Chaque famille a sa règle :

* **Dossier temporaire d'un agent** (jetable) : `TMPDIR`/`TMP`/`TEMP` d'un
  tour pointent vers `<tmp_root>/<agent>` (par défaut
  `<état>/.menage/tmp/<agent>`, sur disque : pas un tmpfs en mémoire). Il vit
  le temps d'une SESSION, pas d'un tour : une session reprise retrouve ses
  fichiers (bloc-notes du harnais, sorties de tests) d'un tour à l'autre. Il
  est vidé au premier tour d'une session neuve (rotation, changement de lot,
  `restart`), borné par un quota (éviction des fichiers les plus anciens à la
  fin de chaque tour) et supprimé quand l'agent n'existe plus au registre —
  L219 : seulement s'il appartient à CE mesh (fichier `.<agent>.owner`),
  sans tour en cours (verrou `.<agent>.lock`, bail vivant) ni activité depuis
  `orphan_idle` ; un homonyme d'un autre mesh a son propre dossier. Un
  dossier sans fichier de propriétaire de ce mesh n'est jamais supprimé
  (#2326) : il reste suivi par un constat `tmp_unowned` dédupliqué, avec la
  commande d'inspection (#2493). Un dossier à nous qui porte une copie Git
  n'est retiré qu'après la preuve qu'aucun travail n'y est à garder : les
  worktrees passent par `git worktree remove`, jamais `--force` (#2493).
* **Caches partagés de l'hôte** (réutilisables) : `npm_config_cache`, store
  pnpm (`npm_config_store_dir`/`pnpm_config_store_dir`), `YARN_CACHE_FOLDER`
  — et, sur demande de la politique, `XDG_CACHE_HOME` — pointent vers
  `<cache_root>/<nom>`. Partagés entre agents (un store pnpm par agent
  multiplierait le disque), bornés par un quota global, évincés du plus
  ancien au plus récent, et jamais pendant un tour de l'hôte (un store pnpm
  modifié sous une installation casse l'installation) — sauf le cache npm,
  fait de copies, qui peut partir pendant un tour quand aucun npm ne tourne
  et que rien ne le tient (#3293). Sous pression disque (espace libre sous le
  `min_disk_free` de l'hôte), l'éviction libère en plus le déficit, même
  sous le quota. Une variable déjà posée dans l'environnement de l'exécuteur
  est respectée : c'est un choix de l'hôte.
* **Worktrees** : ceux qui APPARAISSENT pendant un tour (diff de `git
  worktree list` avant/après, sous-agents compris) sont enregistrés avec
  l'agent, le tour et le lot — s'ils sont sous le dossier de travail de
  l'agent, ou si leur branche (`lot/<id>`, `agent/<nom>-<id>`,
  `review/<nom>-<id>`) ou leur dossier (`<nom>-<id>`) nomme un lot assigné à
  cet agent (#1665). #3266 : un worktree suivi sans lot est relu à chaque
  passage (pas seulement ceux marqués « non attribué ») et le dossier le
  rattache par les mêmes conventions que les clones (#2743) — préfixe de
  projet (`nexlink-<agent>-<lot>`), `.wt<lot>`, `<agent>.wt<lot>` — puis
  l'attribution est persistée ; aucune règle de suppression nouvelle. Ceux
  d'ailleurs que rien ne rattache à lui (un autre agent qui travaille sur le
  même dépôt) sont suivis « non attribués » (L73b) : ils ne partent que par la
  règle des orphelins ci-dessous. À la fin
  du lot (fusion ou fermeture), ils sont retirés par `git worktree remove`
  (jamais forcé) s'ils sont propres, non verrouillés, âgés d'au moins une
  heure, sans tour en cours de leur agent ni processus dedans, et que leur
  HEAD est INTÉGRÉ à la branche principale distante (ancêtre, rebase, fusion
  écrasée : `git cherry`, `git merge-tree`) ; sinon GARDÉS et signalés
  (`worktree_kept`) au responsable. L230: one kept although its lot is merged
  is removed after 24 h if clean and pushed; its branch stays. #4442 : la
  raison gardée est NORMALISÉE (sans SHA de HEAD ni nombre de fichiers), donc
  un détail volatil ne fait plus repartir la grâce de 24 h ; et un worktree
  gardé pour des fichiers modifiés ou des commits absents de la forge est
  archivé (`patchs` + `HEAD.bundle`) puis retiré une fois TOUS ses lots
  finis depuis `policy.housekeeping.dirty_worktree_after` et inactif autant
  (copies étrangères comprises). #1665 : un
  worktree orphelin (aucun lot nommé, ou lot fini sans intégration) est
  retiré après `ORPHAN_WORKTREE_IDLE_S` (24 h) d'inactivité s'il est propre
  et poussé et qu'aucun agent ne l'utilise (aucun tour, aucun processus
  dedans) ; ses sorties de build partent d'abord, puis son dossier, par la
  poubelle journalisée (L230).
* **Build output of idle worktrees** (L230, [`build_artifacts`]):
  `node_modules`, Rust `target/`, deleted once the worktree is idle for 24 h
  (6 h when every lot it names is finished, #3956) with no process of the
  user inside; never in a main checkout, nor while its agent is in turn or a
  lot it names is open (#3195). An agent's working directory is no longer
  exempt by its status (#3956): the same guards decide, and the finished-lot
  worktrees are scanned first so a pass cut by its budget frees them first.

* **Clones ad hoc d'un lot** (#2743, [`housekeeping_clones`]) : enfant direct
  d'une racine de travail dont le `.git` est un dossier ; rattaché à un lot
  par son nom ou une branche `lot/`/`work/`, il part après 24 h si tous les
  lots nommés sont finis depuis plus de 24 h, qu'il est inactif, propre,
  poussé (toutes ses branches, sans stash), sans copie Git imbriquée, sans
  autre worktree lié ni alternate dépendant (#2738), et que rien n'y
  travaille. Un clone sans lot nommé est seulement signalé ; l'essai
  (`ameesh menage`) montre le plan, `--apply` exécute.

* **Volumes Docker sans conteneur** (L73/#2003/#4560, [`reap_volumes`]) :
  seuls ceux dont le mesh a DÉCLARÉ le cycle de vie jetable sont récoltés —
  l'étiquette de durée de vie `ameesh.lifetime=test`, posée par
  `scripts/pg-up.sh` sur le volume d'un banc de lot déclaré jetable
  (`AMEESH_PG_DISPOSABLE=1`), ou l'ancienne `ameesh.bench=disposable` — et
  seulement après `docker_volume_idle` (24 h) d'ABSENCE CONTINUE, mesurée par
  les relevés successifs du ménage (jamais l'âge de création). Un volume de
  BASE nommé (`ameesh-pg-data`, `*-pg-data`) n'est JAMAIS récolté, même
  étiqueté ; tout volume nommé sans étiquette et tout volume anonyme
  (`com.docker.volume.anonymous`, sans provenance du mesh : le shim d'un tour
  force `rm -v`, donc il part avec le conteneur que la suite supprime) sont
  listés dans `others` (borné) avec la commande `docker volume rm`, JAMAIS
  supprimés (précédent #411 : les volumes nommés existants sont conservés) ;
  `docker volume rm` refuse de lui-même un volume encore monté.

* **Images Docker sans tag** (#4560, [`reap_images`]) : une image construite
  dans un tour porte `ameesh.lifetime=test` (shim L73) ; une fois SANS TAG et
  après le même délai d'absence continue, elle est récoltée (`docker image
  rm`). Une image taguée n'est JAMAIS supprimée, même étiquetée : un tag est
  une référence. Les images dangling sans étiquette sont seulement comptées.

* **Scopes systemd `dsh-subprocess-*`** (#3226, [`reap_dsh_scopes`]) : sans
  la garde #490, dsh lance ses outils dans des scopes du gestionnaire
  utilisateur, hors du groupe et du cgroup du tour, qui survivent au tour.
  Un scope dont le processus dsh n'existe plus et dont tous les processus
  portent l'`AMEESH_TURN_ID` d'un tour clos de cet hôte est tué (SIGTERM puis
  SIGKILL) une heure après la fin du tour ; dsh vivant ou tour en cours :
  gardé ; attribution impossible : gardé et signalé (alerte `dsh_scope`).

* **Entrées de `/tmp` apparues pendant un tour** (L162 : clone de relecture,
  `--cacheDirectory` explicite, image `docker save`) : elles sont signalées
  (`tmp_orphan`) avec la commande à lancer, NON ATTRIBUÉES (#222 : `/tmp` est
  commun à l'hôte, rien ne prouve quel tour les a créées), donc JAMAIS
  supprimées par ameesh ; l'alerte les suit, taille à jour, tant qu'elles
  existent. Seul un signalement attribué d'avant #222 est encore SUPPRIMÉ par
  le passage périodique si l'entrée est un enfant direct du `/tmp` du
  système, à l'utilisateur, la même que celle signalée (inode), attribuée à
  un agent du registre sans tour en cours, rien dedans n'a changé depuis
  `orphan_idle` (2 h, mtime ET ctime), aucun processus ne la tient, et aucune
  copie git dedans n'a de travail non commité ou non poussé
  (`orphan_cleanup: false` : jamais, par hôte).

Les worktrees liés découverts dans les dépôts des agents locaux rejoignent
le même suivi, avec un âge neuf (#2180). Les clones et dossiers sans Git
sont seulement signalés. Toute suppression dans un dossier géré passe par
[`_safe_rmtree`], qui refuse un chemin hors des dossiers gérés, un lien
symbolique, ou une racine sans marqueur `.ameesh-menage` ; dans `/tmp`, par
[`_remove_tmp_entry`], qui revérifie l'entrée juste avant.

Les dossiers gérés ne sont jamais en mémoire (L162) : une racine déclarée sur
un tmpfs est écartée au profit de `<état>/.menage/…`.

Tout est journalisé (`housekeeping_log`, migration 0045) et visible dans
`ameesh hosts` ; `ameesh menage [--apply]` montre ou déclenche un passage.
"""
from __future__ import annotations

import getpass
import json
import math
import os
import shlex
import re
import shutil
import stat
import subprocess
import time
from dataclasses import dataclass, field
from urllib.parse import unquote, urlsplit

from . import work_lifecycle as lifecycle
from . import build_artifacts
from . import platform
from . import resources as resources_mod
from . import storage

GIB = 1024 ** 3
MIB = 1024 ** 2

#: valeurs par défaut de `policy.housekeeping` (fiche Host)
DEFAULT_TMP_QUOTA = 4 * GIB          # par agent
DEFAULT_CACHE_QUOTA = 10 * GIB       # tous caches partagés de l'hôte
DEFAULT_TMPFS_ALERT = 0.6            # alerte (la contre-pression est à 0,8)
DEFAULT_ORPHAN_MIN_SIZE = 50 * MIB   # entrée de /tmp signalée au-delà
DEFAULT_EVENTS_QUOTA = 64 * MIB      # #2820 : flux events.jsonl d'un agent, avant rotation
#: #2820 : plus petit `events_quota` admis (la reprise du dernier tour en est
#: le quart)
MIN_EVENTS_QUOTA = 1 * MIB
#: âge minimal d'un fichier évincé (secondes) : jamais un fichier en écriture
EVICT_MIN_AGE_S = 3600.0
#: #1665 : inactivité (secondes) d'un worktree orphelin (aucun lot nommé par
#: sa branche ou son dossier) avant que le ménage ne le retire s'il est
#: propre et poussé ; owner, 15/10/2026 14:50 (« fais en sorte que les agents
#: d'ameesh nettoient leurs worktrees »)
ORPHAN_WORKTREE_IDLE_S = 24 * 3600.0
#: âge minimal d'un worktree retiré (secondes, depuis son enregistrement ET
#: depuis la dernière modification de son dossier) : jamais un worktree neuf
WORKTREE_MIN_AGE_S = 3600.0
#: préfixe du détail d'un worktree apparu pendant un tour mais pas créé par
#: l'agent du tour (L73b) : suivi pour être vu, jamais retiré automatiquement
UNATTRIBUTED = "unattributed"
#: the same prefix as written before L232 (rows already stored)
UNATTRIBUTED_LEGACY = "non attribué"
#: #3266 : détail d'un worktree rattaché à son lot par le nom de son dossier
ATTRIBUTED_DETAIL = "attributed from the worktree name: lot #%s (assigned to %s)"
#: #3266 : le dossier nomme l'agent, mais aucun lot ne lui est assigné
OWNER_DETAIL = "owner from the worktree name: %s (lot not proved)"
#: borne d'un parcours de taille (fichiers) : au-delà, la taille est un minimum
WALK_LIMIT = 300_000
#: délai maximal d'une commande git (secondes)
GIT_TIMEOUT = 20.0
#: #3802 : budget d'un passage de ménage sur l'hôte (secondes). Passé ce
#: plafond, le passage abandonne sa phase au prochain point de contrôle, rend
#: le verrou `host-pass` et le dit. Sans lui, une phase qui accumule des
#: appels bornés (822 worktrees × plusieurs `git` à 20 s) tenait le verrou
#: des heures : agents-2, 26/10, 3 h 17, plus aucun ménage.
HOST_PASS_BUDGET_S = 15 * 60.0
#: #3802 : au-delà de ce temps de détention du verrou `host-pass`, un passage
#: SAUTÉ nomme le détenteur et lève l'alerte `housekeeping_blocked` (un
#: passage sain rend le verrou à son plafond ; un détenteur plus vieux est
#: bloqué, ou d'une version qui ne se borne pas).
HOST_PASS_ALERT_AFTER_S = HOST_PASS_BUDGET_S
#: #3802 : un blocage constaté n'est re-journalisé qu'à ce rythme, pour que le
#: journal dise l'épisode sans une ligne par passage sauté.
HOST_PASS_REFRESH_S = 3600.0
#: #3802 (B1) : part du budget du passage réservée aux phases qui suivent les
#: worktrees (clones, artefacts, conteneurs, volumes, scopes, /tmp, mesure).
#: La boucle des worktrees s'arrête quand le temps restant passe sous cette
#: réserve ; sans elle, un hôte à 298 worktrees refaisait les mêmes premiers
#: à chaque passage et les phases suivantes ne tournaient plus jamais.
HOST_PASS_RESERVE_S = 300.0
#: #4234 : plafond de la SEULE phase worktrees. Mesuré sur ameesh-agents-1
#: (484 worktrees suivis, 28/10/2026) : 394 s, soit 81 % d'une passe de 488 s ;
#: avec un passage toutes les 10 min, le verrou host-pass restait tenu presque
#: en permanence et une passe manuelle était presque toujours sautée. La phase
#: s'arrête à ce plafond et le curseur (#3802 B1) reprend les arbres suivants
#: au passage d'après ; le verrou est rendu au plus tard après ~3 min de
#: worktrees.
HOST_PASS_WORKTREES_BUDGET_S = 180.0
#: #3802 (B1) : clé réservée de l'état du verrou qui garde le curseur de
#: reprise des worktrees (dernier `(created_ts, id)` examiné), par mesh.
WORKTREES_CURSOR_KEY = "_worktrees_cursor"


class _PassExpired(Exception):
    """#3802 : the host pass reached its time budget at a checkpoint."""

    def __init__(self, phase: str):
        super().__init__(phase)
        self.phase = phase


class PassBudget:
    """The bounded time of one host pass and the trace of its phases (#3802).

    `check(name)` closes the previous phase, raises `_PassExpired` when the
    budget is gone and otherwise opens the next one; loops call `expired()`
    themselves and stop. Every external call takes `timeout(cap)`, so no
    single `git`, `docker` or `systemctl` can outlive the pass. `on_phase`
    lets the host lock say which phase its holder is in.
    """

    def __init__(self, seconds: float, clock=time.monotonic, on_phase=None):
        self.budget = max(0.0, float(seconds))
        self.clock = clock
        self.started = clock()
        self.deadline = self.started + self.budget
        self.phase = ""
        self.phase_started: float | None = None
        self.phases: list[dict] = []
        self.on_phase = on_phase
        self.timed_out = False
        #: lists of the pass (`entries`, `plan`, ...) kept reachable when a
        #: checkpoint raises out of `_run_pass`
        self.context: dict = {}

    def remaining(self) -> float:
        return max(0.0, self.deadline - self.clock())

    def timeout(self, cap: float) -> float:
        """The delay of one external call: `cap`, never past the budget."""
        return max(0.0, min(float(cap), self.remaining()))

    def expired(self) -> bool:
        return self.clock() >= self.deadline

    def reserved(self, seconds: float) -> bool:
        """True when no more than `seconds` remain: a phase that must leave
        room for the following ones stops there (#3802 B1)."""
        return self.remaining() <= max(0.0, float(seconds))

    def window(self, reserve: float = 0.0):
        """A view of this budget that ends `reserve` seconds earlier: the
        external calls of a removal or an archive cannot eat the reserve
        kept for the following phases (#3802 B1, cx2)."""
        return _BudgetWindow(self, reserve)

    def slice(self, seconds: float) -> "_BudgetSlice":
        """#4234 : une phase s'arrête à `seconds` au plus, sans consommer le
        budget des phases suivantes ; le passage garde son échéance."""
        return _BudgetSlice(self, seconds)

    def check(self, name: str) -> None:
        """Start phase `name`; raise when the budget is already spent, naming
        the phase it was spent in (the one that just ended)."""
        current = self.phase
        self.close_phase()
        if self.expired():
            self.timed_out = True
            raise _PassExpired(current or name)
        self.phase = name
        self.phase_started = self.clock()
        if self.on_phase is not None:
            self.on_phase(name)

    def close_phase(self) -> None:
        if self.phase_started is None:
            return
        end = self.clock()
        self.phases.append({"phase": self.phase,
                            "seconds": round(max(0.0, end - self.phase_started), 3)})
        self.phase = ""
        self.phase_started = None

    def summary(self) -> dict:
        self.close_phase()
        return {"budget_s": self.budget,
                "spent_s": round(max(0.0, self.clock() - self.started), 3),
                "timed_out": self.timed_out, "phases": list(self.phases)}


class _BudgetWindow:
    """`PassBudget` minus a reserve: same duck-typed interface (`remaining`,
    `expired`, `timeout`), used to bound the external calls of a removal or an
    archive (#3802 B1, cx2)."""

    def __init__(self, budget, reserve: float = 0.0):
        self.budget = budget
        self.reserve = max(0.0, float(reserve))

    def remaining(self) -> float:
        return max(0.0, self.budget.remaining() - self.reserve)

    def expired(self) -> bool:
        return self.remaining() <= 0.0

    def timeout(self, cap: float) -> float:
        return max(0.0, min(float(cap), self.remaining()))


class _BudgetSlice:
    """#4234 : la part du passage qu'une phase s'autorise (les worktrees).

    Le verrou `host-pass` était tenu ~8 min sur un hôte à ~480 worktrees : une
    passe toutes les 10 min, donc un traitement manuel presque toujours
    « skipped ». La boucle des worktrees s'arrête à sa propre échéance et le
    curseur reprend au passage suivant ; les appels externes de la phase sont
    bornés par le temps qui lui reste, jamais au-delà du passage (#3802)."""

    def __init__(self, budget: "PassBudget", seconds: float):
        self.budget = budget
        self.deadline = budget.clock() + max(0.0, float(seconds))

    def remaining(self) -> float:
        return max(0.0, min(self.budget.remaining(),
                            self.deadline - self.budget.clock()))

    def expired(self) -> bool:
        return self.remaining() <= 0.0

    def reserved(self, seconds: float) -> bool:
        return self.remaining() <= max(0.0, float(seconds))

    def timeout(self, cap: float) -> float:
        return max(0.0, min(float(cap), self.remaining()))

    def window(self, reserve: float = 0.0) -> _BudgetWindow:
        return _BudgetWindow(self, reserve)


def _phase_budget(budget: PassBudget | None, reserve_s: float = 0.0):
    """The `build_artifacts.Budget` of one phase: its own cap, never past
    the pass's remaining time (#3802), and never into `reserve_s`."""
    seconds = build_artifacts.PASS_BUDGET_S
    if budget is not None:
        seconds = min(seconds, max(0.0, budget.remaining() - reserve_s))
    return build_artifacts.Budget(seconds)


#: L162: idle time (seconds) after which a /tmp entry signaled with an agent
#: (before #222) is deleted (`policy.housekeeping.orphan_idle`)
DEFAULT_ORPHAN_IDLE_S = 2 * 3600.0
#: L162 : horizon des signalements relus par le passage (le journal en garde 30 j)
ORPHAN_LOOKBACK_S = 7 * 24 * 3600.0
#: #222: an unattributed /tmp entry, never deleted, is signaled again by the
#: periodic pass once its last line is this old, so that it stays in the 24 h
#: window of the alerts for as long as it exists
UNATTRIBUTED_REFRESH_S = 12 * 3600.0
#: #2493 : a temporary directory WITHOUT owner file of this mesh is never
#: deleted; it is re-signaled at most this often (one `tmp_unowned` alert per
#: path follows it), so the pass no longer repeats it in `skipped` every time
UNOWNED_REFRESH_S = 12 * 3600.0
#: L162 : nombre de plus grosses entrées de /tmp dites quand le tmpfs se remplit
TMP_TOP = 5
#: #1547 (N3) : conteneurs hors tour détaillés dans le relevé de ménage — le
#: journal garde le compte total (`others_total`), jamais une liste non bornée
OTHERS_TOP = 20
#: L73/#2003 : délai (secondes) d'ABSENCE CONTINUE, mesuré par les relevés
#: successifs du ménage, avant qu'un volume Docker DÉCLARÉ JETABLE
#: (`ameesh.bench=disposable`) ne soit récolté. L'âge de création n'en tient
#: pas lieu ; les volumes non déclarés ne sont jamais supprimés.
DEFAULT_DOCKER_VOLUME_IDLE_S = 24 * 3600.0
#: #2003 : volumes Docker non déclarés jetables détaillés dans le relevé — le
#: journal garde le compte total, jamais une liste non bornée (comme
#: `OTHERS_TOP`)
VOLUMES_TOP = 20
#: L162/L230 : début de la raison rendue quand l'OS ne dit pas qui tient une
#: entrée ; l'hôte qui a déclaré des processus illisibles sans doute
#: (`unreadable_ok`) repose alors la question avec son allow
HOLDERS_UNREADABLE = "holders unreadable"
#: #3396 : la clé du canon qui excuse un démon par son nom ; dite dans le plan
#: pour que l'opérateur sache quoi déclarer quand gpg-agent retient tout
UNREADABLE_OK_HINT = "policy.housekeeping.unreadable_ok if it is a daemon"
#: L162 : systèmes de fichiers en mémoire — jamais une racine gérée
MEMORY_FS = ("tmpfs", "ramfs")
#: L162 : l'action attendue quand le tmpfs se remplit (alertes, journal)
#: #234: since #222 the housekeeping deletes by itself only a signal attributed
#: before #222; a new /tmp entry is never charged to a turn nor deleted
TMPFS_ACTION = ("expected action: delete what is no longer needed (one command per entry "
                "in `ameesh menage` or in the `tmp_orphan` alert); housekeeping deletes on "
                "its own only a signaled entry attributed before #222, unchanged for %s "
                "and held by no process; an entry appeared since is never deleted by "
                "ameesh; agents write in $TMPDIR, never directly in /tmp")

#: dossier géré sous l'état d'ameesh (un nom d'agent ne commence jamais par
#: un point : pas de collision avec `<état>/<agent>`)
MANAGED_DIR = ".menage"
#: marqueur posé par ameesh à la racine d'un dossier qu'il gère
MARKER = ".ameesh-menage"
#: L219: owner file of an agent's temporary directory, next to it in the tmp
#: root (`.<agent>.owner`, never inside: the start of a session empties the
#: directory and the quota evicts its oldest files). It names the mesh that
#: created the directory (`mesh_key`).
OWNER_SUFFIX = ".owner"
#: L219: lock file of an agent's temporary directory, next to it (`flock`)
LOCK_SUFFIX = ".lock"
#: L219: directory of an agent whose homonym of another mesh owns
#: `<agent>`: `<agent>~<first ALT_LEN characters of the mesh key>`
ALT_SEP, ALT_LEN = "~", 8
#: L219: a directory without owner file (another mesh, or before L219) is
#: only touched after this long without any activity
UNOWNED_IDLE_S = 24 * 3600.0
#: L219: origin written in the owner file (after the key)
ORIGIN_CREATED, ORIGIN_ADOPTED = "created", "adopted"
#: L219: fallback directory of a turn that could not lock its own:
#: `<name>+<random>`, used once, deleted by the pass once idle
FALLBACK_SEP = "+"
#: L219: longest wait for the lock of a temporary directory at a turn start
#: (a pass holds it while deleting); past it the turn gets a fallback
#: directory. Emptying (exclusive) waits less: a live session of the agent
#: (`attach`) holds it shared, and then the directory is not emptied
LOCK_WAIT_S, CLEAR_WAIT_S = 30.0, 5.0
#: L219: fallback names tried before giving up
FALLBACK_TRIES = 5

#: caches partagés : nom du sous-dossier → variables d'environnement
CACHES = (
    ("npm", ("npm_config_cache",)),
    ("pnpm", ("npm_config_store_dir", "pnpm_config_store_dir")),
    ("yarn", ("YARN_CACHE_FOLDER",)),
)
XDG_CACHE = ("xdg", ("XDG_CACHE_HOME",))

#: états de lot qui terminent le cycle de vie des worktrees du lot
LOT_ENDED = lifecycle.COMPLETED
#: L230: of those, the lot states where the work reached main
LOT_MERGED = lifecycle.DELIVERED
#: L230: a worktree kept although its lot is merged is removed after this
KEPT_MERGED_GRACE_S = 24 * 3600.0

POLICY_KEYS = ("tmp_root", "cache_root", "tmp_quota", "cache_quota", "tmpfs_alert",
               "redirect", "xdg_cache", "worktrees", "orphan_min_size",
               "orphan_cleanup", "orphan_idle", "deps", "deps_idle", "artifacts",
               "artifact_idle", "artifact_ended_idle", "unreadable_ok",
               "docker_orphan_volumes_alert",
               "docker_build_cache_alert", "docker_volumes", "docker_images",
               "docker_volume_idle",
               "events_quota", "former_mesh_keys", "dirty_worktree_after",
               "dirty_archive_retention")
#: racines refusées : le ménage n'y supprimerait que ses sous-dossiers, mais
#: un dossier partagé par tout le système n'est pas un dossier GÉRÉ
FORBIDDEN_ROOTS = ("/", "~", "/tmp", "/var/tmp", "/dev/shm", "/home", "/root")


# --------------------------------------------------------------------------
# politique (fiche Host, `policy.housekeeping`)
# --------------------------------------------------------------------------

@dataclass(frozen=True)
class Policy:
    tmp_root: str | None = None
    cache_root: str | None = None
    tmp_quota: int = DEFAULT_TMP_QUOTA
    cache_quota: int = DEFAULT_CACHE_QUOTA
    tmpfs_alert: float = DEFAULT_TMPFS_ALERT
    docker_orphan_volumes_alert: int = 100
    docker_build_cache_alert: int = 20 * 1024 ** 3
    #: #2003 : récolter les volumes Docker DÉCLARÉS JETABLES par le mesh
    #: (`ameesh.bench=disposable`, `ameesh.lifetime=test`) ; `false` : aucun
    #: volume n'est supprimé
    docker_volumes: bool = True
    #: #4560 : récolter les images SANS TAG déclarées jetables par le mesh
    #: (`ameesh.lifetime=test`, posée par le shim sur les constructions d'un
    #: tour) ; `false` : aucune image n'est supprimée
    docker_images: bool = True
    #: #2003/#4560 : absence continue (secondes, mesurée par les relevés) avant
    #: cette récolte (volumes et images)
    docker_volume_idle: float = DEFAULT_DOCKER_VOLUME_IDLE_S
    redirect: bool = True
    xdg_cache: bool = False
    worktrees: bool = True
    orphan_min_size: int = DEFAULT_ORPHAN_MIN_SIZE
    #: L162 : supprimer les entrées de /tmp abandonnées par un tour
    orphan_cleanup: bool = True
    #: L162 : inactivité (secondes) avant cette suppression
    orphan_idle: float = DEFAULT_ORPHAN_IDLE_S
    #: #248: node_modules shared per lockfile (npm wrapper, links, GC)
    deps: bool = True
    #: #248: idle time (seconds) before an unreferenced entry is deleted
    #: (#3746: 24 h, `deps.DEFAULT_DEPS_IDLE_S`)
    deps_idle: float = 24 * 3600.0
    #: L230: regenerable build dirs of idle worktrees (None: the defaults of
    #: `build_artifacts.DEFAULT_RULES`; empty: none deleted)
    artifacts: tuple | None = None
    #: L230: inactivity (seconds) of a worktree before its artifacts go
    artifact_idle: float = build_artifacts.DEFAULT_IDLE_S
    #: #3956: inactivity (seconds) of a worktree whose cited lots are ALL
    #: finished — shorter, since nothing rebuilds there; same guards (no open
    #: lot, no turn, no process inside) and the never-touch rule of a worktree
    #: that contains an agent's working directory still apply
    artifact_ended_idle: float = build_artifacts.DEFAULT_ENDED_IDLE_S
    #: L230/L162: names of the user's processes whose unreadability (not
    #: dumpable: capabilities, setuid) does not block a deletion — the build
    #: artifacts of an idle worktree AND the holder check of an abandoned /tmp
    #: entry; empty by default, set per host in its Host card
    unreadable_ok: tuple = ()
    #: #2820 : taille du flux `events.jsonl` d'un agent au-delà de laquelle il
    #: tourne au début du tour suivant (`events_rotation`)
    events_quota: int = DEFAULT_EVENTS_QUOTA
    #: #3277 : anciennes clés de CE mesh sur cet hôte (une route vers la même
    #: base, avant un changement de DSN : #532, 10.77.0.1:5432 → pgbouncer) ;
    #: leurs dossiers temporaires sont à nous, `housekeeping_stale` les range
    former_mesh_keys: tuple = ()
    #: #3728 : un worktree d'un lot fini depuis plus longtemps que cela (et
    #: inactif autant), gardé pour des fichiers modifiés ou non suivis, est
    #: archivé puis retiré ; None (non déclaré) : jamais, il reste gardé
    dirty_worktree_after: float | None = None
    #: #3728 : durée de conservation de ces archives ; None : aucun archivage
    dirty_archive_retention: float | None = None
    #: #2180: discovery roots derived from Host work_root/work_roots, never
    #: deletion roots. Not a separate housekeeping setting.
    worktree_roots: tuple = ()
    #: provenance de chaque clé déclarée (identifiant du canon)
    origin: dict = field(default_factory=dict, compare=False)

    def as_dict(self) -> dict:
        return {key: getattr(self, key) for key in POLICY_KEYS}


def _path_problem(value) -> str | None:
    if not isinstance(value, str) or not value.strip():
        return "empty path"
    text = value.strip()
    if "{" in text or "}" in text:
        return "templates not allowed"
    if not (text.startswith("/") or text.startswith("~")):
        return "absolute path (or ~) expected"
    if os.path.normpath(text) in FORBIDDEN_ROOTS or text.rstrip("/") in FORBIDDEN_ROOTS:
        return "directory shared by the system (%s): choose a dedicated subdirectory" % text
    return None


def parse_policy(raw) -> tuple[dict, list[str], list[str]]:
    """(clés valides, problèmes, clés inconnues) de `policy.housekeeping`."""
    if not isinstance(raw, dict):
        return {}, ["mapping expected"], []
    parsed: dict = {}
    problems: list[str] = []
    for key in ("tmp_root", "cache_root"):
        if raw.get(key) is None:
            continue
        problem = _path_problem(raw[key])
        if problem:
            problems.append("`%s`: %s" % (key, problem))
        else:
            parsed[key] = raw[key].strip()
    for key in ("tmp_quota", "cache_quota", "orphan_min_size", "docker_build_cache_alert"):
        if raw.get(key) is None:
            continue
        octets = resources_mod.parse_bytes(raw[key])
        if octets is None:
            problems.append("`%s`: size in bytes expected (e.g. 4GiB)" % key)
        else:
            parsed[key] = octets
    if raw.get("events_quota") is not None:
        octets = resources_mod.parse_bytes(raw["events_quota"])
        if octets is None or octets < MIN_EVENTS_QUOTA:
            problems.append("`events_quota`: size of at least 1MiB expected (e.g. 64MiB)")
        else:
            parsed["events_quota"] = octets
    if raw.get("docker_orphan_volumes_alert") is not None:
        count = raw["docker_orphan_volumes_alert"]
        if type(count) is not int or count <= 0:
            problems.append("`docker_orphan_volumes_alert`: positive integer expected")
        else:
            parsed["docker_orphan_volumes_alert"] = count
    if raw.get("tmpfs_alert") is not None:
        part = resources_mod.parse_fraction(raw["tmpfs_alert"])
        if part is None:
            problems.append("`tmpfs_alert`: fraction expected (0 < x <= 1, or \"60%\")")
        else:
            parsed["tmpfs_alert"] = part
    for key in ("orphan_idle", "deps_idle", "artifact_idle", "artifact_ended_idle",
                "docker_volume_idle", "dirty_worktree_after", "dirty_archive_retention"):
        if raw.get(key) is None:
            continue
        seconds = parse_idle(raw[key])
        if seconds is None:
            problems.append("`%s`: duration expected (e.g. 2h, 90m, 7200)" % key)
        else:
            parsed[key] = seconds
    if raw.get("artifacts") is not None:
        rules, problem = build_artifacts.parse_rules(raw["artifacts"])
        if rules is None:
            problems.append("`artifacts` : %s" % problem)
        else:
            parsed["artifacts"] = tuple(str(r) for r in rules)
    if raw.get("unreadable_ok") is not None:
        names, problem = build_artifacts.parse_names(raw["unreadable_ok"])
        if names is None:
            problems.append("`unreadable_ok` : %s" % problem)
        else:
            parsed["unreadable_ok"] = names
    if raw.get("former_mesh_keys") is not None:
        keys = raw["former_mesh_keys"]
        if not isinstance(keys, list) or not all(
                isinstance(k, str) and re.fullmatch(r"[0-9a-f]{16}", k) for k in keys):
            problems.append("`former_mesh_keys`: list of 16-hex mesh keys expected "
                            "(the owner files of `.menage/tmp`)")
        else:
            parsed["former_mesh_keys"] = tuple(sorted(set(keys)))
    for key in ("redirect", "xdg_cache", "worktrees", "orphan_cleanup", "deps",
                "docker_volumes", "docker_images"):
        if raw.get(key) is None:
            continue
        if not isinstance(raw[key], bool):
            problems.append("`%s`: boolean expected" % key)
        else:
            parsed[key] = raw[key]
    unknown = sorted(str(k) for k in set(raw) - set(POLICY_KEYS))
    return parsed, problems, unknown


def parse_idle(value) -> float | None:
    """Durée d'inactivité (L162) : secondes (nombre), ou texte « 2h », « 90m »,
    « 1d » ; None si illisible ou non positive."""
    if value is None or isinstance(value, bool):
        return None
    if isinstance(value, (int, float)):
        return float(value) if value > 0 else None
    from . import stagnation  # import tardif : pas de cycle
    try:
        return stagnation.parse_duration(str(value))
    except ValueError:
        return None


def effective(canons, host: str) -> Policy:
    """Politique de ménage de `host` d'après toutes les fiches Host des canons.

    Chemins et booléens : la première fiche qui les déclare (canon par défaut
    d'abord). Quotas, taille de signalement et seuil d'alerte : le plus strict
    (le plus bas), comme les limites physiques de L43. L162 : la suppression
    des entrées de /tmp abandonnées est coupée dès qu'UNE fiche la coupe, et
    l'inactivité exigée est la plus longue. L230: `artifact_idle` too, the
    longest, et `artifact_ended_idle` (#3956) de même. #3728 :
    `dirty_worktree_after` et `dirty_archive_retention`
    aussi, les plus longs (garder plus longtemps est le choix prudent)."""
    values: dict = {}
    origin: dict = {}
    roots: set[str] = set()
    for canon in canons or ():
        fiche = canon.host(host) if hasattr(canon, "host") else None
        host_policy = getattr(fiche, "policy", None)
        declared = getattr(host_policy, "housekeeping", None) or {}
        for template in [getattr(host_policy, "work_root", None),
                         *(getattr(host_policy, "work_roots", None) or {}).values()]:
            if not template:
                continue
            path = os.path.expanduser(template)
            if "{agent}" in path:
                path = os.path.dirname(path.split("{agent}", 1)[0] + "x")
            path = os.path.abspath(path)
            if path not in (*FORBIDDEN_ROOTS, os.path.expanduser("~")):
                roots.add(path)
        label = getattr(canon, "id", "") or "?"
        for key, value in declared.items():
            if key not in POLICY_KEYS:
                continue
            if key == "former_mesh_keys":
                # #3277 : une clé déclarée par une fiche est une ancienne route
                # de ce mesh : l'union des fiches
                values[key] = tuple(sorted(set(values.get(key, ())) | set(value)))
                origin.setdefault(key, label)
                continue
            if key not in values:
                values[key], origin[key] = value, label
            elif key in ("tmp_quota", "cache_quota", "orphan_min_size", "tmpfs_alert",
                         "docker_orphan_volumes_alert", "docker_build_cache_alert",
                         "events_quota") \
                    and value < values[key]:
                values[key], origin[key] = value, label
            elif (key in ("orphan_idle", "deps_idle", "artifact_idle",
                          "artifact_ended_idle", "docker_volume_idle",
                          "dirty_worktree_after",
                          "dirty_archive_retention") and value > values[key]) or \
                    (key in ("orphan_cleanup", "deps", "docker_volumes",
                             "docker_images") and value is False
                     and values[key] is not False):
                values[key], origin[key] = value, label
    return Policy(origin=origin, worktree_roots=tuple(sorted(roots)), **values)


def disabled(env=None) -> bool:
    """`AMEESH_HOUSEKEEPING=off` : ni redirection, ni ménage (échappatoire)."""
    value = (env if env is not None else os.environ).get("AMEESH_HOUSEKEEPING", "")
    return value.strip().lower() in ("off", "0", "none", "non")


# --------------------------------------------------------------------------
# dossiers gérés
# --------------------------------------------------------------------------

def _expand(path: str) -> str:
    return os.path.abspath(os.path.expanduser(path))


def in_memory(path: str) -> bool:
    """`path` (existant ou non) est-il sur un système de fichiers en mémoire
    (tmpfs) ? Faux si on ne sait pas le dire (L162)."""
    try:
        mount = platform.mount_of(path)
    except Exception:  # lecture des montages impossible : on ne devine pas
        return False
    return bool(mount) and mount[1] in MEMORY_FS


def _disk_root(declared: str | None, default: str) -> str:
    """La racine déclarée, sauf si elle est en mémoire alors que le défaut
    (sous l'état d'ameesh) ne l'est pas : un dossier géré vit sur disque (L162,
    le /tmp en tmpfs de 12 Go plein le 2026-10-11 a bloqué tous les tours)."""
    if not declared:
        return default
    chosen = _expand(declared)
    if in_memory(chosen) and not in_memory(default):
        return default
    return chosen


def declared_root_refused(cfg, policy: Policy) -> list[str]:
    """Les racines déclarées écartées parce qu'en mémoire (pour le rapport)."""
    out = []
    for key, effective_root in (("tmp_root", tmp_root(cfg, policy)),
                                ("cache_root", cache_root(cfg, policy))):
        declared = getattr(policy, key)
        if declared and _expand(declared) != effective_root:
            out.append("%s %s in memory (tmpfs): set aside, %s used"
                       % (key, declared, effective_root))
    return out


def tmp_root(cfg, policy: Policy) -> str:
    return _disk_root(policy.tmp_root, os.path.join(cfg.state_dir, MANAGED_DIR, "tmp"))


def cache_root(cfg, policy: Policy) -> str:
    return _disk_root(policy.cache_root, os.path.join(cfg.state_dir, MANAGED_DIR, "cache"))


def agent_tmp(cfg, policy: Policy, agent: str) -> str:
    """The agent's temporary directory for THIS mesh (L219: `tmp_name`)."""
    troot = tmp_root(cfg, policy)
    return os.path.join(troot, tmp_name(troot, agent, mesh_key(cfg)))


def cache_names(policy: Policy) -> list[tuple[str, tuple[str, ...]]]:
    return list(CACHES) + ([XDG_CACHE] if policy.xdg_cache else [])


#: gestionnaires de paquets dont le cache est partagé (le titre de leur
#: processus les nomme : « npm install … », « pnpm add … »)
PACKAGE_MANAGERS = ("npm", "pnpm", "yarn")


def package_managers_running() -> set[str]:
    """#3293: les gestionnaires de paquets qui tournent sur l'hôte, d'après le
    titre de leur processus (`process.title` : `npm install …`, tronqué à 15
    caractères par le noyau). Un relevé illisible est un doute : tous sont
    dits en cours, et rien n'est évincé sous un tour."""
    try:
        rows = platform.process_table()
    except Exception:  # noqa: BLE001 - un passage de ménage ne lève jamais
        return set(PACKAGE_MANAGERS)
    found: set[str] = set()
    for row in rows:
        head = (row.get("name") or "").strip().split(" ", 1)[0].lower()
        if head in PACKAGE_MANAGERS:
            found.add(head)
    return found


def _conninfo_fields(dsn: str) -> dict[str, str]:
    """The fields of a DSN, URI or key=value, following libpq's rules (quotes
    and backslashes, percent-encoding, query parameters, several hosts). Pure
    Python: the psql driver runs without psycopg, and the key must not depend
    on the driver."""
    from urllib.parse import parse_qsl, unquote

    fields: dict[str, str] = {}
    if re.match(r"postgres(?:ql)?://", dsn):
        rest = dsn.split("://", 1)[1]
        rest, _, query = rest.partition("?")
        netloc, _, path = rest.partition("/")
        netloc = netloc.rpartition("@")[2]
        hosts, ports = [], []
        for item in netloc.split(","):
            m = re.fullmatch(r"(\[[^\]]*\]|[^:]*)(?::(.*))?", item)
            hosts.append(unquote(m.group(1).strip("[]")))
            ports.append(unquote(m.group(2) or ""))
        if any(hosts):
            fields["host"] = ",".join(hosts)
        if any(ports):
            fields["port"] = ",".join(ports)
        if path:
            fields["dbname"] = unquote(path)
        fields.update(parse_qsl(query, keep_blank_values=True))
        return fields
    pos, n = 0, len(dsn)
    while True:
        while pos < n and dsn[pos].isspace():
            pos += 1
        if pos >= n:
            return fields
        m = re.compile(r"([^=\s]+)\s*=\s*").match(dsn, pos)
        if not m:
            return fields
        pos, value, quoted = m.end(), [], False
        if pos < n and dsn[pos] == "'":
            pos, quoted = pos + 1, True
        while pos < n:
            ch = dsn[pos]
            if ch == "\\" and pos + 1 < n:
                value.append(dsn[pos + 1])
                pos += 2
                continue
            if (quoted and ch == "'") or (not quoted and ch.isspace()):
                pos += 1
                break
            value.append(ch)
            pos += 1
        fields[m.group(1)] = "".join(value)


def _dsn_where(dsn: str) -> str:
    """Where a DSN points (host, hostaddr, port, dbname), whatever its form
    (URI, key=value, query parameters)."""
    fields = _conninfo_fields(dsn)
    # host names are case-insensitive, socket paths are not
    host = ",".join(h if h.startswith(("/", "@")) else h.lower()
                    for h in (fields.get("host") or "").split(","))
    return "%s|%s|%s|%s" % (host, fields.get("hostaddr") or "",
                            fields.get("port") or "5432", fields.get("dbname") or "")


def mesh_key(cfg) -> str:
    """L219: identity of the mesh (database and schema) shared by its runners:
    a hash of host, hostaddr, port and database of the DSN (never user,
    password or options) and of the schema. Two meshes of one host that share the state
    directory have two keys."""
    import hashlib

    where = _dsn_where(str(getattr(cfg, "dsn", "") or ""))
    text = "%s|%s" % (where, getattr(cfg, "schema", "") or "public")
    return hashlib.sha256(text.encode("utf-8")).hexdigest()[:16]


def owner_file(troot: str, agent: str) -> str:
    return os.path.join(troot, ".%s%s" % (agent, OWNER_SUFFIX))


def lock_file(troot: str, name: str) -> str:
    return os.path.join(troot, ".%s%s" % (name, LOCK_SUFFIX))


def _owner_lines(troot: str, name: str) -> list[str]:
    try:
        with open(owner_file(troot, name), encoding="utf-8") as fh:
            return fh.read().split()
    except OSError:
        return []


def read_owner(troot: str, name: str) -> str | None:
    """The mesh key written for the temporary directory `name`, or None."""
    lines = _owner_lines(troot, name)
    return lines[0] if lines else None


def created_by(troot: str, name: str, key: str) -> bool:
    """L219: true if the mesh `key` CREATED the directory `name` (owner file
    `<key> created`). One it adopted (found without owner file) is never
    emptied by a new session: it may hold another mesh's files."""
    return _owner_lines(troot, name)[:2] == [key, ORIGIN_CREATED]


def _write_owner(troot: str, name: str, key: str, origin: str) -> None:
    path = owner_file(troot, name)
    tmp = "%s.%d" % (path, os.getpid())
    with open(tmp, "w", encoding="utf-8") as fh:
        fh.write("%s %s\n" % (key, origin))
    os.replace(tmp, path)


def tmp_name(troot: str, agent: str, key: str) -> str:
    """L219: name of `agent`'s temporary directory in `troot` for the mesh
    `key`: the agent's name, unless a homonymous agent of another mesh owns
    it — then `<agent>~<key prefix>` (never shared, never taken over)."""
    owner = read_owner(troot, agent)
    if owner is None or owner == key:
        return agent
    return "%s%s%s" % (agent, ALT_SEP, key[:ALT_LEN])


def tmp_agent(name: str, key: str) -> str:
    """The agent of a directory name of this mesh (`tmp_name` reversed,
    fallback suffix included)."""
    name = name.split(FALLBACK_SEP, 1)[0]
    suffix = "%s%s" % (ALT_SEP, key[:ALT_LEN])
    return name[:-len(suffix)] if name.endswith(suffix) else name


def tmp_lock(troot: str, name: str, *, exclusive: bool, timeout: float = 0.0):
    """L219: lock of the temporary directory `name` (flock on a file NEXT to
    it), shared during a turn, exclusive to empty, evict or delete it.
    Returns the open file (closing it releases the lock), None if not obtained within
    `timeout` seconds. The lock file is checked to be still the one at its
    path once locked (a deletion may have unlinked it meanwhile)."""
    import fcntl

    path = lock_file(troot, name)
    deadline = time.monotonic() + max(0.0, timeout)
    mode = fcntl.LOCK_EX if exclusive else fcntl.LOCK_SH
    while True:
        try:
            fh = open(path, "a+")
        except OSError:
            return None
        try:
            fcntl.flock(fh.fileno(), mode | fcntl.LOCK_NB)
        except OSError:
            fh.close()
            if time.monotonic() >= deadline:
                return None
            time.sleep(0.05)
            continue
        try:
            same = os.fstat(fh.fileno()).st_ino == os.stat(path).st_ino
        except OSError:
            same = False
        if same:
            return fh
        fh.close()


def last_activity(path: str, budget: PassBudget | None = None) -> float | None:
    """Latest mtime or ctime (an extraction keeps old mtimes) of `path` and
    of everything under it, at any depth; None if the walk is incomplete
    (unreadable entry, mount point, too many entries, pass budget reached):
    what was not seen is never judged idle."""
    scan = scan_activity(path, budget=budget)
    return float(scan["latest"]) if scan["complete"] and scan["latest"] is not None else None


def _ensure_root(root: str) -> None:
    os.makedirs(root, mode=0o700, exist_ok=True)
    marker = os.path.join(root, MARKER)
    if not os.path.exists(marker):
        with open(marker, "w", encoding="utf-8") as fh:
            fh.write("directory managed by ameesh (L73): its content may be deleted "
                     "by housekeeping\n")


def _managed(root: str) -> bool:
    return os.path.isfile(os.path.join(root, MARKER)) and not os.path.islink(root)


def _shims_wanted(cfg) -> bool:
    from . import containers as containers_mod
    return containers_mod.runtime_binary(getattr(cfg, "container_runtime", "none") or "none") \
        is not None


@dataclass
class TmpHold:
    """L219: an agent's temporary directory held for a turn or a session:
    its path, and the shared lock (`tmp_lock`) to close at the end."""

    path: str
    lock: object
    entries: list = field(default_factory=list)

    def release(self) -> None:
        if self.lock is not None:
            self.lock.close()
            self.lock = None

    def exclusive_now(self) -> bool:
        """Turn the shared lock into the exclusive one without waiting: false
        if another holder of the shared lock remains (a runner taking over
        the lease). On failure the lock may be lost (flock converts by
        releasing first): only for the end of the hold."""
        import fcntl

        if self.lock is None:
            return False
        try:
            fcntl.flock(self.lock.fileno(), fcntl.LOCK_EX | fcntl.LOCK_NB)
        except OSError:
            return False
        return True


def hold_tmp(cfg, policy: Policy, agent: str, *, new_session: bool) -> TmpHold:
    """L219: the agent's temporary directory, locked (shared) for a turn.

    A new session empties it under the exclusive lock, and only if THIS mesh
    created it (never a homonym's of another mesh, never one adopted without
    owner file). The shared lock only waits for a pass deleting or evicting
    (exclusive, the time of an rm); if it cannot be had within
    `LOCK_WAIT_S`, the turn gets a fresh fallback directory, never an
    unlocked one (fallback names are tried `FALLBACK_TRIES` times, then
    OSError). The owner file is only written under the exclusive lock, and
    the name and owner are checked again once the lock is held; the lock is
    then turned into the shared one."""
    import fcntl

    troot, key = tmp_root(cfg, policy), mesh_key(cfg)
    _ensure_root(troot)
    entries: list = []
    lock, name, exclusive = None, "", False
    for _attempt in range(3):          # a homonym of another mesh may claim it meanwhile
        name = tmp_name(troot, agent, key)
        # the owner is only written under the exclusive lock: two meshes
        # starting a homonym at once never both see it free
        claim = new_session or read_owner(troot, name) is None
        lock = tmp_lock(troot, name, exclusive=True, timeout=CLEAR_WAIT_S) if claim else None
        exclusive = lock is not None
        if lock is None:
            lock = tmp_lock(troot, name, exclusive=False, timeout=LOCK_WAIT_S)
        if lock is None:
            break
        # checked again under the lock: the name is still ours, and an
        # unowned one is only taken under the exclusive lock
        if tmp_name(troot, agent, key) == name \
                and (exclusive or read_owner(troot, name) is not None):
            break
        lock.close()
        lock = None
    if lock is None:
        import secrets
        base = name
        for _attempt in range(FALLBACK_TRIES):
            name = "%s%s%s" % (base, FALLBACK_SEP, secrets.token_hex(3))
            lock = tmp_lock(troot, name, exclusive=True)
            if lock is not None and read_owner(troot, name) is None:
                break
            if lock is not None:
                lock.close()
                lock = None
        if lock is None:
            raise OSError("L219: no lockable temporary directory for %s in %s"
                          % (agent, troot))
        exclusive = True
    mine = os.path.join(troot, name)
    created = not os.path.isdir(mine)
    os.makedirs(mine, mode=0o700, exist_ok=True)
    if read_owner(troot, name) is None:            # under the exclusive lock (above)
        _write_owner(troot, name, key, ORIGIN_CREATED if created else ORIGIN_ADOPTED)
    elif new_session and not created and exclusive and created_by(troot, name, key):
        # #2704 : une application lancée depuis ce dossier (AppImage extraite)
        # peut le tenir hors tour ; la vider la ferait crasher. On la garde et
        # on le dit, plutôt que de supprimer sous un processus vivant.
        held = tmp_user_holder(mine)
        if held:
            entries.append({"kind": "tmp", "action": "held", "path": mine,
                            "agent": agent,
                            "detail": "new session: %s (not emptied)" % held})
        else:
            freed = _clear_dir(mine, troot)
            if freed:
                entries.append({"kind": "tmp", "action": "deleted", "path": mine,
                                "bytes": freed, "agent": agent,
                                "detail": "new session: temporary directory emptied"})
    fcntl.flock(lock.fileno(), fcntl.LOCK_SH)
    # a resumed session writes nothing at first: the pass must not see it idle
    try:
        os.utime(mine)
    except OSError:
        pass
    return TmpHold(path=mine, lock=lock, entries=entries)


def turn_env(cfg, policy: Policy, agent: str, base_env: dict, *,
             hold: TmpHold | None = None) -> dict:
    """Variables à poser dans l'environnement d'un tour (crée les dossiers).

    `TMPDIR`/`TMP`/`TEMP` sont toujours redirigés (c'est l'objet du lot) vers
    le dossier de l'agent, SUR DISQUE (L162 : une racine déclarée en tmpfs est
    écartée) : le harnais et tous ses enfants en héritent — Node
    (`os.tmpdir()`, donc le cache de Jest), Python, Go, Rust, et le client
    docker/podman (podman y copie ses images). Une variable de cache déjà
    posée dans `base_env` est respectée. L219 : le dossier est celui de
    `hold` (verrouillé pour le tour) ; sans `hold`, il est pris puis relâché
    aussitôt (préparation, tests)."""
    if not policy.redirect or disabled(base_env):
        return {}
    if hold is None:
        hold = hold_tmp(cfg, policy, agent, new_session=False)
        hold.release()
    croot = cache_root(cfg, policy)
    _ensure_root(croot)
    mine = hold.path
    out = {"TMPDIR": mine, "TMP": mine, "TEMP": mine,
           "AMEESH_TMPDIR": mine, "AMEESH_CACHE_DIR": croot}
    for name, variables in cache_names(policy):
        if any(base_env.get(v) for v in variables):
            continue
        directory = os.path.join(croot, name)
        os.makedirs(directory, mode=0o700, exist_ok=True)
        for variable in variables:
            out[variable] = directory
    if _shims_wanted(cfg):
        # L73 : `docker|podman run|create` d'un tour portent ses étiquettes
        from . import containers as containers_mod
        out.update(containers_mod.install_shims(
            os.path.join(cfg.state_dir, MANAGED_DIR, "bin"),
            base_env.get("PATH") or os.defpath))
    if policy.deps:
        # #248: `npm ci` links the node_modules shared per lockfile; no
        # install in a review worktree
        from . import deps as deps_mod
        out.update(deps_mod.install_shim(
            os.path.join(cfg.state_dir, MANAGED_DIR, deps_mod.SHIM_DIR),
            out.get("PATH") or base_env.get("PATH") or os.defpath))
    return out


# --------------------------------------------------------------------------
# tailles, éviction et suppression sûre
# --------------------------------------------------------------------------

def _used_bytes(st) -> int:
    """#500 r3 (codex P2) : octets qu'un fichier occupe réellement.

    Toujours `st_blocks * 512` : un 0 rapporté veut dire 0 (fichier vide ou
    entièrement creux), jamais « inconnu » — l'ancien repli sur `st_size`
    comptait un fichier creux d'1 To à sa taille logique, le faisait évincer
    lui et tout ce qui le suivait. Sur une plateforme qui ne rapporte pas
    `st_blocks` (Windows), la taille logique est la seule mesure."""
    blocks = getattr(st, "st_blocks", None)
    if blocks is None or platform.is_windows():
        return st.st_size
    return blocks * 512


def _mount_points(root: str) -> set | None:
    """#500 r5 (codex P1 r4) : les points de montage SOUS la racine gérée,
    lus dans `/proc/self/mountinfo` (champ 5, `platform.mountinfo`). Un bind
    mount d'un dossier extérieur, sur le MÊME système de fichiers, garde le
    même `st_dev` : la table des montages est le seul moyen de le voir.

    Rend `None` quand la table est illisible sur Linux : fail-closed, la
    descente et l'élagage sont refusés (on ne peut pas distinguer un dossier
    monté d'un dossier géré). Hors Linux, ensemble vide : pas de mountinfo, la
    frontière reste `st_dev`."""
    if not platform.is_linux():
        return set()
    entries = platform.mountinfo()
    if not entries:
        return None          # Linux a toujours au moins « / » : table illisible
    real = os.path.realpath(root).rstrip(os.sep) or os.sep
    prefix = real if real.endswith(os.sep) else real + os.sep
    out: set = set()
    for entry in entries:
        point = str(entry.get("point") or "").rstrip(os.sep) or os.sep
        if point != real and point.startswith(prefix):
            out.add(point)
    return out


def _is_mount_point(directory: str, mounts: set | None) -> bool:
    """#500 r5 : `directory` est un point de montage de `mounts` (comparaison
    de chemins : on ne `stat` pas un dossier qu'un montage peut cacher).

    #500 r6 (codex P1 r5) : `mounts` ne contient que des chemins canoniques
    (mountinfo) ; `normpath` ne résout pas un ancêtre symbolique (`/state` →
    `/disk/state`), donc la marche par `/state/...` ne verrait pas le montage.
    Comparaison sur `realpath` : un dossier dont l'ancêtre est un lien est vu
    comme le montage qu'il cache."""
    if not mounts:
        return False
    return os.path.realpath(directory) in mounts


def _files(top: str, limit: int | None = None, *, state: dict | None = None,
           protect_git: bool = False, device: int | None = None,
           mounts: set | None = None, budget: PassBudget | None = None):
    """(chemin, taille, mtime, stat) des fichiers sous `top`, sans suivre les
    liens ni changer de système de fichiers ; s'arrête à `limit` entrées
    comptées — fichiers rendus ET dossiers à ouvrir (#530) — (défaut
    `WALK_LIMIT`) et le dit alors dans `state["truncated"]` si `state` est
    fourni.

    #500 : les octets d'un même inode (liens durs) ne comptent qu'une fois ;
    la taille est celle des blocs occupés (`_used_bytes`). Avec `state`, tous
    les noms de l'inode vus par la marche sont gardés dans `state["links"]`
    (l'éviction les retire tous avant de compter les octets, #500 r6).
    #530 : la racine et chaque dossier empilé consomment le budget comme un
    fichier, sinon un arbre de 10 001 dossiers se parcourait entièrement avec
    `WALK_LIMIT = 3`.
    Avec `protect_git`, un dossier qui porte un marqueur git est reconnu AVANT
    d'en lister les fichiers et n'est pas parcouru : sa racine est ajoutée à
    `state["protected"]`, donc le sous-arbre n'est jamais évincé, même quand
    la marche s'arrête à `limit` avant de l'avoir atteint.

    #500 r3 (codex P1) : `device` (le `st_dev` de la racine gérée) est exigé
    pour chaque entrée — un point de montage apparu dans le dossier n'est ni
    parcouru ni compté. Le `stat` de l'entrée est rendu avec la taille, pour
    que l'éviction le revérifie juste avant l'unlink.

    #500 r5 (codex P1 r4) : `mounts` (`_mount_points`) nomme les points de
    montage SOUS la racine gérée — un bind mount d'un dossier extérieur garde
    le même `st_dev` ; un tel dossier n'est ni parcouru ni compté, et il est
    ajouté à `state["protected"]` (marqueur `mount`)."""
    limit = WALK_LIMIT if limit is None else limit
    if device is None:
        try:
            device = os.lstat(top).st_dev
        except OSError:
            return
    count = 1                          # #530 : la racine se consomme aussi
    seen: set = set()
    stack = [top]
    while stack:
        current = stack.pop()
        if _is_mount_point(current, mounts):
            if state is not None:
                state.setdefault("protected", []).append(
                    {"path": current, "marker": "mount"})
            continue
        if protect_git:
            marker = _git_marker(current)
            if marker is not None:
                if state is not None:
                    state.setdefault("protected", []).append(
                        {"path": marker[1], "marker": marker[0]})
                continue
        try:
            with os.scandir(current) as it:
                for entry in it:
                    if count >= limit or (budget is not None and budget.expired()):
                        # #530 : budget épuisé alors que des entrées restent à
                        # lire : la liste est partielle. #3802 : le budget du
                        # passage arrête de même un parcours trop long.
                        if state is not None:
                            state["truncated"] = True
                            if budget is not None and budget.expired():
                                state["budget_reached"] = True
                            state.setdefault("truncated_paths", []).append(top)
                        return
                    try:
                        st = entry.stat(follow_symlinks=False)
                    except OSError:
                        continue
                    if st.st_dev != device:  # #500 r3: mount point, never crossed
                        continue
                    if stat.S_ISDIR(st.st_mode):
                        stack.append(entry.path)
                        count += 1       # #530 : un dossier à ouvrir se consomme
                        continue
                    inode = (st.st_dev, st.st_ino)
                    # #500 r6 (codex P2 r5): every name of the inode seen by
                    # the walk is kept, so the eviction can remove them all
                    # before counting the bytes (a link left behind means the
                    # bytes were not freed).
                    names = state.setdefault("links", {}).setdefault(inode, []) \
                        if state is not None else None
                    if names is not None:
                        names.append(entry.path)
                    if inode in seen:            # #500: hard link, counted once
                        continue
                    seen.add(inode)
                    count += 1
                    yield entry.path, _used_bytes(st), st.st_mtime, st
        except OSError:
            continue


def size_of(path: str, limit: int = WALK_LIMIT,
            budget: PassBudget | None = None) -> int:
    """Octets occupés sous `path` (fichier ou dossier), bornés à `limit`
    entrées comptées (#530 : fichiers et dossiers) et au budget du passage
    (#3802) ; 0 si illisible, un minimum si le parcours s'arrête."""
    try:
        st = os.lstat(path)
    except OSError:
        return 0
    if not stat.S_ISDIR(st.st_mode):
        return _used_bytes(st)
    return sum(size for _p, size, _m, _st in _files(path, limit, budget=budget))


def _in_subtree(path: str, directory: str) -> bool:
    """#500 r5 : `path` est `directory` lui-même ou l'un de ses descendants
    (frontière de chemin, sans `stat`)."""
    directory = directory.rstrip(os.sep)
    return path == directory or path.startswith(directory + os.sep)


def _located_in(path: str, root: str) -> bool:
    """Vrai si `path` lui-même (pas sa cible, s'il est un lien) est sous
    `root`, sans être `root` ni son marqueur."""
    real_root = os.path.realpath(root).rstrip("/") or "/"
    parent = os.path.realpath(os.path.dirname(os.path.abspath(path)))
    name = os.path.basename(os.path.abspath(path))
    if not name or name in (".", "..") or (parent == real_root and name == MARKER):
        return False
    return parent == real_root or parent.startswith(real_root + "/")


class RefusedDeletion(RuntimeError):
    """Suppression refusée : hors des dossiers gérés."""


class StaleCandidate(Exception):
    """#500 r3 : le candidat n'est plus ce que l'énumération a vu — contenu
    remplacé ou disparu, marqueur `.git` apparu dans un dossier ouvert, ou
    dossier sorti du système de fichiers de la racine. Il est sauté, sans être
    compté (ni évincé, ni irrécupérable). `path` est le dossier à protéger
    (marqueur git, montage) ou le candidat lui-même ; `reason` dit lequel."""

    def __init__(self, path: str, reason: str):
        super().__init__("%s: %s" % (reason, path))
        self.path = path
        self.reason = reason


def _safe_rmtree(path: str, root: str) -> int:
    """Supprime `path` s'il est DANS une racine gérée (marqueur présent) ;
    rend les octets libérés. Un lien symbolique est retiré, jamais suivi ;
    jamais la racine elle-même ni son marqueur, jamais un chemin hors d'elle."""
    if not _managed(root):
        raise RefusedDeletion("%s is not a directory managed by ameesh" % root)
    if not _located_in(path, root):
        raise RefusedDeletion("%s is outside %s" % (path, root))
    size = size_of(path)
    if os.path.islink(path) or not os.path.isdir(path):
        os.unlink(path)
    else:
        shutil.rmtree(path, ignore_errors=True)
    return size


def _clear_dir(directory: str, root: str) -> int:
    """Vide `directory` (géré) sans le supprimer ; rend les octets libérés."""
    freed = 0
    try:
        names = os.listdir(directory)
    except OSError:
        return 0
    for name in names:
        try:
            freed += _safe_rmtree(os.path.join(directory, name), root)
        except (OSError, RefusedDeletion):
            continue
    return freed


def _prune_empty_dirs(top: str, device: int | None = None, *,
                      mounts: set | None = None,
                      limit: int | None = None) -> None:
    """#500 r3 : retire les dossiers vidés par une éviction, du plus profond au
    plus haut, sans traverser un point de montage. Un dossier dont le `st_dev`
    n'est pas celui de la racine gérée (`device`) est laissé en place, lui et
    son sous-arbre : `os.rmdir` échoue de toute façon sur un point de montage,
    mais on ne l'ouvre même pas.

    #500 r4 (codex P2 r3) : un dossier porteur d'un marqueur git — et tout son
    sous-arbre — n'est ni parcouru ni retiré : une éviction ne doit pas vider
    l'intérieur d'un arbre de travail protégé.

    #500 r5 (codex P1 r4) : `mounts` (`_mount_points`) nomme les points de
    montage sous la racine gérée ; aucun n'est parcouru ni retiré, même quand
    le bind mount garde le `st_dev` de la racine.

    #530 : au plus `limit` dossiers ouverts (défaut `WALK_LIMIT`), la racine
    comprise. Passé ce budget, l'élagage ne liste ni n'ouvre plus de dossier :
    les dossiers déjà empilés restent candidats au `rmdir` (ils sont vides ou
    non), les autres sont laissés en place. L'élagage est un nettoyage de
    confort, jamais une condition de l'éviction."""
    limit = WALK_LIMIT if limit is None else limit
    if device is None:
        try:
            device = os.lstat(top).st_dev
        except OSError:
            return
    visited = 1                          # #530 : la racine compte
    stack = [(top, False)]
    while stack:
        current, post = stack.pop()
        if post:
            if current != top and not _git_protected(current) \
                    and not _is_mount_point(current, mounts):
                try:
                    os.rmdir(current)    # échoue si le dossier n'est pas vide
                except OSError:
                    pass
            continue
        if _git_protected(current) or _is_mount_point(current, mounts):
            continue         # arbre de travail ou montage : ni entré ni élagué
        stack.append((current, True))
        try:
            with os.scandir(current) as it:
                for entry in it:
                    if visited >= limit:
                        break            # #530 : budget épuisé, on n'ouvre plus
                    try:
                        st = entry.stat(follow_symlinks=False)
                    except OSError:
                        continue
                    if stat.S_ISDIR(st.st_mode) and st.st_dev == device:
                        stack.append((entry.path, False))
                        visited += 1
        except OSError:
            continue


def _git_marker(directory: str) -> tuple[str, str] | None:
    """#500 : le marqueur git d'un dossier, lu AVANT d'en lister les fichiers :
    `("root", dossier)` pour un clone (`.git` est un dossier) ou un worktree
    lié (`.git` est un fichier dont le `gitdir:` existe) ; `("symlink",
    dossier)` pour un `.git` qui est un lien symbolique — jamais suivi, le
    sous-arbre est protégé par prudence et signalé ; None sinon, dont un
    worktree dont le gitdir a disparu (il est évincable, comme avant).
    La règle est celle de `_git_marker_at`, lue ici relativement au chemin."""
    flags = os.O_RDONLY | getattr(os, "O_DIRECTORY", 0) | getattr(os, "O_NOFOLLOW", 0)
    try:
        fd = os.open(directory, flags)
    except OSError:
        return None
    try:
        marker = _git_marker_at(fd, directory)
    finally:
        os.close(fd)
    return (marker, directory) if marker else None


def _git_marker_at(fd: int, directory: str) -> str | None:
    """#500 r3 : le marqueur git du dossier ouvert `fd` (« root », « symlink »,
    ou None), avec la MÊME règle que le scan : un `.git` régulier dont le
    `gitdir:` a disparu n'est pas un marqueur (le worktree mort reste
    évincable), un `.git` illisible est protecteur (fail-closed). Lu
    relativement au descripteur, sans repasser par le chemin : un dossier
    remplacé entre le scan et l'unlink ne peut pas tromper ce contrôle."""
    try:
        st = os.stat(".git", dir_fd=fd, follow_symlinks=False)
    except FileNotFoundError:
        return None
    except OSError:
        return "symlink"               # illisible : protecteur, jamais suivi
    if stat.S_ISLNK(st.st_mode):
        return "symlink"
    if stat.S_ISDIR(st.st_mode):
        return "root"
    if not stat.S_ISREG(st.st_mode):
        return None
    marker_fd = None
    try:
        marker_fd = os.open(".git", os.O_RDONLY | getattr(os, "O_NOFOLLOW", 0),
                            dir_fd=fd)
        with os.fdopen(marker_fd, encoding="utf-8", errors="replace") as fh:
            marker_fd = None           # fdopen owns it from here
            line = fh.readline(4096).strip()
    except OSError:
        return "symlink"               # illisible : protecteur
    finally:
        if marker_fd is not None:
            os.close(marker_fd)
    if not line.startswith("gitdir:"):
        return None
    gitdir = os.path.join(directory, line[len("gitdir:"):].strip())
    return "root" if os.path.isdir(gitdir) else None


def _git_protected(directory: str) -> bool:
    """#500 r4 (codex P2 r3) : `_prune_empty_dirs` doit laisser intact un
    sous-arbre protégé par un marqueur git. Même règle que le scan
    (`_git_marker_at` : un worktree mort reste évincable) ; fail-closed, un
    dossier qu'on ne peut pas ouvrir n'est pas élagué."""
    flags = os.O_RDONLY | getattr(os, "O_DIRECTORY", 0) | getattr(os, "O_NOFOLLOW", 0)
    try:
        fd = os.open(directory, flags)
    except OSError:
        return True
    try:
        return _git_marker_at(fd, directory) is not None
    finally:
        os.close(fd)


def _check_open_dir(fd: int, directory: str, device: int | None) -> None:
    """#500 r3 : le dossier ouvert `fd` est-il encore dans la racine gérée —
    même système de fichiers (`device`), aucun marqueur `.git` apparu ? Sinon
    `StaleCandidate` : on n'évince rien dans ce sous-arbre."""
    if device is not None:
        try:
            current = os.fstat(fd).st_dev
        except OSError:
            current = None
        if current != device:
            raise StaleCandidate(directory, "mount point")
    if _git_marker_at(fd, directory):
        raise StaleCandidate(directory, "git marker")


def _unlink_in_root(path: str, root: str, *, expected=None,
                    device: int | None = None) -> bool:
    """#500 : retire `path` relativement à `root` : chaque composant est ouvert
    avec `O_DIRECTORY|O_NOFOLLOW`, puis `os.unlink(nom, dir_fd=fd)`. Un dossier
    parent remplacé par un lien symbolique entre la vérification `_located_in`
    et la suppression ne peut donc plus faire sortir la suppression de la
    racine gérée (ELOOP/ENOTDIR, l'appelant reclasse le fichier). Lève OSError
    aussi si `path` n'est pas sous `root`.

    #500 r3 (codex P1) : juste avant l'unlink, `os.stat(nom, dir_fd=fd,
    follow_symlinks=False)` est comparé à `expected` (`(st_dev, st_ino,
    st_mtime_ns, st_size)` du scan) ; toute différence, un `.git` apparu dans
    un dossier ouvert, ou un dossier dont le `st_dev` n'est plus celui de la
    racine (`device`) lève `StaleCandidate` au lieu de supprimer.

    #530 (codex r2 P2) : rend True quand l'allocation de l'inode est
    réellement libérée — `st_nlink == 1` au contrôle frais, donc le nom retiré
    était le dernier. False quand un autre lien dur garde l'inode (ou sans
    `expected`, faute de preuve) : l'appelant ne crédite alors aucun octet.

    Résiduels, hors modèle de menace : un montage substitué APRÈS ce contrôle
    demanderait des droits de montage ; un dossier parent déplacé hors de la
    racine après son ouverture demanderait un processus malveillant de même
    UID dans le TMPDIR de l'agent (comme #414) ; de même, un lien dur créé
    entre ce `stat` et l'unlink peut faire créditer des octets encore
    occupés — la suppression, elle, reste dans la racine."""
    rel = os.path.relpath(os.path.abspath(path), os.path.abspath(root))
    parts = [part for part in rel.split(os.sep) if part]
    if not parts or any(part == os.pardir for part in parts):
        raise OSError("path outside the managed root: %s" % path)
    flags = os.O_RDONLY | getattr(os, "O_DIRECTORY", 0) | getattr(os, "O_NOFOLLOW", 0)
    fd = os.open(root, flags)
    freed = False
    try:
        opened = root
        _check_open_dir(fd, opened, device)
        for part in parts[:-1]:
            following = os.open(part, flags, dir_fd=fd)
            os.close(fd)
            fd = following
            opened = os.path.join(opened, part)
            _check_open_dir(fd, opened, device)
        if expected is not None:
            target = os.path.join(root, *parts)
            try:
                st = os.stat(parts[-1], dir_fd=fd, follow_symlinks=False)
            except OSError:
                raise StaleCandidate(target, "gone") from None
            if (st.st_dev, st.st_ino, st.st_mtime_ns, st.st_size) != (
                    expected.st_dev, expected.st_ino, expected.st_mtime_ns,
                    expected.st_size):
                raise StaleCandidate(target, "changed")
            freed = st.st_nlink == 1      # #530: dernier nom de l'inode ?
        os.unlink(parts[-1], dir_fd=fd)
    finally:
        os.close(fd)
    return freed


def _dir_writable(path: str, writable: dict) -> bool:
    """#762: `path`'s directory lets us unlink it (cached by directory).

    Unlike the ownership test of `_removable`, this is what the kernel
    enforces: an unlink in a directory we may not write is refused whatever
    the file's owner."""
    parent = os.path.dirname(path)
    if parent not in writable:
        writable[parent] = os.access(parent, os.W_OK | os.X_OK)
    return writable[parent]


def _removable(path: str, uid: int | None, writable: dict) -> bool:
    """#500: this pass can unlink `path`: owned by us (any owner when the
    platform has no uid) and its directory writable (cached in `writable`)."""
    try:
        st = os.lstat(path)
    except OSError:
        return False
    if uid is not None and st.st_uid != uid:
        return False
    return _dir_writable(path, writable)


def evict_lru(directories: list[str], root: str, quota: int, *, now: float | None = None,
              min_age: float = EVICT_MIN_AGE_S, dry_run: bool = False,
              evict_at_least: int = 0, budget: PassBudget | None = None) -> dict:
    """Éviction du plus ancien (mtime) au plus récent, jusqu'au quota.

    Un fichier modifié depuis moins de `min_age` n'est jamais évincé (il est
    peut-être en écriture) : le quota peut donc rester dépassé, et c'est dit
    (`over_quota`).

    #3293 : `evict_at_least` évince aussi jusqu'à libérer au moins ces
    octets, même quand le total tient déjà dans le quota — une pression
    disque se traite en libérant ce qu'il faut, pas seulement au-delà d'un
    plafond. L'éviction s'arrête quand les deux conditions tiennent (quota
    respecté et octets libérés) ; un fichier trop récent ou irrécupérable
    l'arrête comme avant, et `over_quota` ne dit que le quota.

    #500: only what this pass can remove counts toward the quota (owned by
    us, directory writable, outside a git work tree); the rest is reported
    (`unremovable`, `worktrees`). When the unremovable part alone exceeds the
    quota, nothing is evicted (`blocked`): before, root-owned leftovers kept
    the total over the quota and every turn end emptied the agent's directory.
    A git subtree is recognised before its files are listed (so WALK_LIMIT can
    no longer hide it) and is never evicted. When the listing is truncated by
    `WALK_LIMIT`, nothing is evicted at all (`truncated`, alerted): the pass
    cannot prove what is removable. Rend `{before, after, evicted, files,
    over_quota, unremovable, worktrees, worktree_symlinks, truncated,
    truncated_paths, failed, blocked, mounts_unreadable}`.

    #500 r3 (codex P1): the scan never leaves the managed root's filesystem
    (`device`), and every candidate is re-verified just before its unlink
    (`_unlink_in_root`) — replaced, gone, under a `.git` marker that appeared,
    or on another filesystem: skipped, counted neither as evicted nor as
    unremovable, and a git marker or a mount leaves its whole subtree alone.
    #500 r4 (codex P2 r3): the bytes of a candidate that is gone or whose
    subtree became protected leave `counted` with it, so the pass stops as
    soon as what is still evictable fits the quota (and the empty-directory
    pruning never enters such a subtree). #500 r5 (codex P1/P2 r4): a mount
    point under the managed root (`_mount_points`, same `st_dev` for a bind
    mount) is neither crossed nor pruned, and the whole remaining subtree of a
    candidate that became protected leaves `counted` at once. When the mount
    table is unreadable on Linux (`mounts_unreadable`), nothing is scanned,
    evicted or pruned: fail-closed. #500 r6 (codex P1/P2 r5): a mount point is
    recognised through a symlinked ancestor (`realpath`, mountinfo is
    canonical), and every name of a hard-linked inode inside the root is
    removed before its bytes leave `counted` — a link left behind frees
    nothing.

    #530 (codex r2): the unremovable bytes are classified and reported even
    when the total is under the quota (they exist, so they are said, without
    alerting when the quota holds); and the bytes of a hard-linked inode are
    credited only when its LAST name goes (`st_nlink == 1` at the fresh check
    before the unlink; dry-run reads it from the scan) — a name the walk did
    not see (protected subtree, another root, another filesystem) keeps the
    inode counted and reported as unremovable."""
    now = time.time() if now is None else float(now)
    try:
        root_dev = os.stat(root).st_dev
    except OSError:
        root_dev = None
    mounts = _mount_points(root)
    if mounts is None:
        return {"before": 0, "after": 0, "evicted": 0, "files": 0,
                "over_quota": False, "unremovable": 0, "worktrees": [],
                "worktree_symlinks": [], "truncated": False,
                "truncated_paths": [], "failed": [], "blocked": False,
                "mounts_unreadable": True, "budget_reached": False}
    scan: dict = {}
    found = []
    for directory in directories:
        if scan.get("truncated"):
            break                      # the listing is partial: no eviction (below)
        if os.path.isdir(directory) and not os.path.islink(directory):
            found.extend(_files(directory, state=scan, protect_git=True,
                                device=root_dev, mounts=mounts, budget=budget))
    before = sum(size for _p, size, _m, _st in found)
    total, evicted, files = before, 0, 0
    unremovable = 0
    blocked = False
    truncated = bool(scan.get("truncated"))
    failed: list[str] = []
    # a protected subtree is left out of the scan itself: one git root per
    # subtree, the nested ones would never be reached (kept for safety)
    roots = sorted({m["path"] for m in scan.get("protected", ()) if m["marker"] == "root"})
    worktrees = [r for r in roots
                 if not any(r.startswith(o + "/") for o in roots if o != r)]
    symlinks = sorted({m["path"] for m in scan.get("protected", ())
                       if m["marker"] == "symlink"})
    counted = before
    uid = os.geteuid() if hasattr(os, "geteuid") else None
    writable: dict = {}
    candidates: list = []
    if not truncated:
        # #530: classify even under the quota — the foreign bytes are reported
        # as soon as they exist instead of reading as zero until the quota is
        # exceeded (the journal line says them without raising the alert).
        # A truncated listing is not classified: a partial walk proves nothing,
        # and the `truncated` entry is the one that alerts.
        for item in found:
            if not _removable(item[0], uid, writable):
                unremovable += item[1]
            else:
                candidates.append(item)
        counted = before - unremovable
    if not truncated and (before > quota or evict_at_least > 0):
        if not dry_run and not _managed(root):
            raise RefusedDeletion("%s is not a directory managed by ameesh" % root)
        # #3293 : sous pression disque, des octets irrécupérables au-delà du
        # quota n'interdisent pas de libérer ce qui peut l'être (le déficit).
        blocked = unremovable > quota and evict_at_least <= 0
        protected: list[str] = []      # #500 r3: subtrees a stale candidate protects
        uncounted: set = set()         # #500 r5: candidates already out of `counted`
        handled: set = set()          # #1190: dry-run inodes already processed
        order = sorted(candidates, key=lambda item: item[2])
        for index, (path, size, mtime, st) in enumerate(order):
            if blocked or (counted <= quota and evicted >= evict_at_least):
                break
            if any(_in_subtree(path, d) for d in protected):
                # #500 r4 (codex P2 r3): the subtree became protected during
                # the pass (`.git` appeared, mount point): its bytes are not
                # evictable any more, so they leave `counted` too — otherwise
                # newer files would be evicted although what is still
                # evictable already fits the quota. r5 excludes the remaining
                # candidates of the subtree at once (below), so this branch
                # normally sees bytes that are still counted.
                if path not in uncounted:
                    counted -= size
                    uncounted.add(path)
                continue               # a `.git` appeared, or a mount point
            if now - mtime < min_age:
                continue
            inode = (st.st_dev, st.st_ino)
            if dry_run and inode in handled:
                # #1190: separate cache walks yield the inode again, but its
                # names would already be gone. Mirror apply's `gone` case.
                counted -= size
                continue
            # #500: a candidate whose deletion is refused (path no longer in
            # the root, or unlink failed) is reclassified as unremovable and
            # STOPS the eviction: never a more recent file deleted to make up
            # for a failure.
            if not _located_in(path, root):
                unremovable += size
                counted -= size
                blocked = unremovable > quota
                failed.append(path)
                break
            others = [p for p in (scan.get("links") or {}).get(
                (st.st_dev, st.st_ino), ()) if p != path and _located_in(p, root)]
            if dry_run:
                # #762 (codex P2): the dry-run announces what the pass would
                # do, and the pass removes every name the walk saw before
                # crediting. The inode is therefore freed exactly when no name
                # escapes the walk: `st_nlink` counts them all, the walked
                # ones included. Reading `st_nlink == 1` alone counted a fully
                # walked inode as unremovable and blocked the dry-run.
                # A walked name whose directory forbids the unlink would be
                # refused by the pass, which stops there: predict that stop
                # rather than crediting the block.
                if not all(_dir_writable(other, writable) for other in others):
                    unremovable += size
                    counted -= size
                    blocked = unremovable > quota
                    failed.append(path)
                    break
                freed = st.st_nlink == len(set(others)) + 1
            else:
                # #500 r6 (codex P2 r5): an inode with hard links is counted
                # once, but it can still have several names inside the managed
                # root. Removing only one leaves the bytes in place while
                # `counted` drops, so the pass stops believing it freed them.
                # Every name the scan saw goes first; the bytes only leave
                # `counted` once none is left. A name that cannot be removed
                # keeps the whole inode counted, and stops the pass like any
                # other refused deletion.
                kept = False
                for other in others:
                    try:
                        _unlink_in_root(other, root, expected=st, device=root_dev)
                    except StaleCandidate as exc:
                        kept = kept or exc.reason != "gone"
                    except OSError:
                        kept = True
                if kept:
                    unremovable += size
                    counted -= size
                    blocked = unremovable > quota
                    failed.append(path)
                    break
                try:
                    freed = _unlink_in_root(path, root, expected=st,
                                            device=root_dev)
                except StaleCandidate as exc:
                    # #500 r3: the candidate changed between the scan and the
                    # unlink: skipped, without being counted. A git marker or a
                    # mount protects every candidate of that subtree.
                    # #500 r4 (codex P2 r3): a candidate gone, or under a
                    # marker/mount that appeared, leaves `counted` as well; a
                    # candidate merely replaced is still there and stays
                    # counted (a later pass sees the replacement).
                    # #500 r5 (codex P2 r4): the WHOLE remaining subtree leaves
                    # `counted` at once — otherwise a later candidate of it
                    # still counts and a file outside is evicted to make up for
                    # bytes that are already out of reach.
                    if exc.reason in ("git marker", "mount point"):
                        protected.append(exc.path)
                        for other, other_size, _m, _st in order[index:]:
                            if _in_subtree(other, exc.path) and other not in uncounted:
                                counted -= other_size
                                uncounted.add(other)
                    elif exc.reason == "gone" and path not in uncounted:
                        counted -= size
                        uncounted.add(path)
                    continue
                except OSError:
                    unremovable += size
                    counted -= size
                    blocked = unremovable > quota
                    failed.append(path)
                    break
            files += 1
            if dry_run:
                handled.add(inode)
            if freed:
                # #530: a name removed from an inode another hard link still
                # holds frees no byte; only the last link credits the quota.
                total -= size
                counted -= size
                evicted += size
            else:
                # #530: every name the walk saw is gone, but the inode still
                # has one it did not see (a protected subtree, another managed
                # root, or outside the root): the bytes are not freed, so they
                # leave `counted` and are reported as unremovable. Not in
                # `failed`: the deletion itself was not refused.
                unremovable += size
                counted -= size
                blocked = unremovable > quota
        if not dry_run and files:
            for directory in directories:
                _prune_empty_dirs(directory, root_dev, mounts=mounts)
    return {"before": before, "after": total, "evicted": evicted, "files": files,
            "over_quota": counted > quota, "unremovable": unremovable,
            "worktrees": worktrees, "worktree_symlinks": symlinks,
            "truncated": truncated, "truncated_paths": scan.get("truncated_paths") or [],
            "failed": failed, "blocked": blocked, "mounts_unreadable": False,
            "budget_reached": bool(scan.get("budget_reached"))}


def tmp_episode(db, host: str, path: str, result: dict, *, now: float) -> dict:
    """#500, #530: what the previous journal line for `path` says about the
    episode this pass belongs to, so that `--follow` announces one alert per
    episode and not one per pass:

    * `since` — first observation of the episode (carried from line to line
      while the previous line is the same episode: an `unremovable` one that
      blocked, a `truncated` one, or a `mounts_unreadable` one), `now` when the
      episode starts or the journal is unreadable;
    * `resolved` — the kind (`unremovable`, `truncated` or `mounts_unreadable`)
      of the episode this pass ends, when it has nothing else to report for the
      path (no eviction, no unremovable bytes, no truncation): the alert must
      be lifted, so an entry says it even though nothing was evicted; None when
      there is nothing to lift, and always None when this pass could not read
      the mount table (#718: it did not scan, so it proves nothing).

    A journal that cannot be read never raises: the alert then just gets a new
    `since`, as before."""
    out = {"since": float(now), "resolved": None}
    if db is None:
        return out
    try:
        rows = storage.of(db).housekeeping.latest_by_path(host or None, "tmp")
    except Exception:
        return out
    for row in rows:
        if row.get("path") != path:
            continue
        data = row.get("data") or {}
        action = row.get("action")
        if action == "truncated":
            previous = "truncated"
        elif action == "mounts_unreadable":
            previous = "mounts_unreadable"
        elif action == "unremovable" and data.get("blocked", True) is not False:
            previous = "unremovable"
        else:
            break
        if result.get("mounts_unreadable"):
            # #718: a pass that could not read the mount table never closes the
            # previous episode — it says the alert again with the same `since`,
            # and `eviction_entries` writes no `resolved` entry for it.
            out["since"] = float(data.get("episode_since") or row.get("at_ts") or now)
            break
        current = ("truncated" if result.get("truncated") else
                   "unremovable" if result.get("unremovable") else None)
        # #762 (codex P3): `truncated` and `mounts_unreadable` feed the same
        # `tmp_unscanned` alert; a pass whose mount table was unreadable and
        # the truncated listing that follows are one episode, so the second
        # keeps the first's `since` instead of re-announcing the alert.
        same_episode = current == previous or (
            previous == "mounts_unreadable" and current == "truncated")
        if same_episode:
            # #530: same episode (a second truncated listing, or the bytes
            # still there): keep its first observation
            out["since"] = float(data.get("episode_since") or row.get("at_ts") or now)
        elif current is None and not result.get("files"):
            # nothing else will be written for this path: say the episode is
            # over (`resolved`), otherwise its last line would alert until the
            # 24 h expiry
            out["since"] = float(data.get("episode_since") or row.get("at_ts") or now)
            out["resolved"] = previous
        break
    return out


def eviction_entries(result: dict, *, kind: str, path: str, quota: int,
                     agent: str | None = None, lot: str | None = None,
                     since: float | None = None,
                     resolved: str | None = None) -> list[dict]:
    """#500: the log lines of one `evict_lru` result: the eviction, the files
    this pass cannot remove (`unremovable`, reported as soon as they exist —
    read by the `tmp_unremovable` alert when they block the quota), a
    truncated listing (`truncated`, read by `tmp_unscanned`), a listing that
    could not even start because the mount table is unreadable
    (`mounts_unreadable`, #718, read by `tmp_unscanned`), the lifting of a
    previous alert (`resolved`), and each git work tree left out (`signaled`).

    `since` is the first observation of the episode (`tmp_episode`): it is
    stored in the data so the next line carries it, and `--follow` does not
    announce the same alert again at every pass. `resolved` names the episode
    this pass ends (`unremovable`, `truncated` or `mounts_unreadable`), #530:
    the truncated listing had no lifting entry before, so `tmp_unscanned`
    stayed raised until its 24 h expiry even after a clean scan. A pass that
    could not read the mount table never writes a `resolved` entry (#718):
    it proves nothing about the files it did not scan."""
    q = resources_mod.fmt_value("q", quota)
    out = []
    data = {"unremovable_bytes": result["unremovable"], "quota": quota,
            "blocked": bool(result["blocked"]), "over_quota": bool(result["over_quota"])}
    if since is not None:
        data["episode_since"] = float(since)
    if result.get("mounts_unreadable"):
        # #718: fail-closed (r5) is said in the journal and alerts; it never
        # resolves the episode of a previous line, since no file was listed.
        resolved = None
        out.append({"kind": kind, "action": "mounts_unreadable", "path": path,
                    "bytes": 0, "agent": agent, "lot": lot,
                    "data": dict(data, mounts_unreadable=True),
                    "detail": "quota %s: mount table unreadable (platform layer), "
                              "nothing was scanned or evicted; what is removable is "
                              "unknown" % q})
    if result["files"]:
        out.append({"kind": kind, "action": "evicted", "path": path,
                    "bytes": result["evicted"], "agent": agent, "lot": lot, "data": data,
                    "detail": "quota %s: %d oldest file(s) evicted%s" % (
                        q, result["files"],
                        "; %s not removable by this pass" % resources_mod.fmt_value(
                            "q", result["unremovable"]) if result["unremovable"] else "")})
    if result["blocked"]:
        detail = ("quota %s exceeded by %s of files this pass cannot remove "
                  "(other owner or read-only directory): nothing evicted"
                  % (q, resources_mod.fmt_value("q", result["unremovable"])))
        if result.get("failed"):
            detail += "; deletion refused for %s" % result["failed"][0]
        out.append({"kind": kind, "action": "unremovable", "path": path,
                    "bytes": result["unremovable"], "agent": agent, "lot": lot, "data": data,
                    "detail": detail})
    elif result["unremovable"]:
        # the quota still holds (the unremovable part alone is under it), but
        # the bytes exist: reported, without raising the alert (`blocked`
        # false in the data)
        detail = ("%s of files this pass cannot remove (other owner or "
                  "read-only directory); the quota %s still holds"
                  % (resources_mod.fmt_value("q", result["unremovable"]), q))
        if result.get("failed"):
            detail += "; deletion refused for %s" % result["failed"][0]
        out.append({"kind": kind, "action": "unremovable", "path": path,
                    "bytes": result["unremovable"], "agent": agent, "lot": lot, "data": data,
                    "detail": detail})
    if result["truncated"]:
        out.append({"kind": kind, "action": "truncated", "path": path,
                    "bytes": result["before"], "agent": agent, "lot": lot, "data": data,
                    "detail": "listing of %s truncated at %d file(s) (WALK_LIMIT): nothing "
                              "evicted, what is removable is unknown"
                              % (", ".join(result["truncated_paths"]) or path,
                                 WALK_LIMIT)})
    if resolved:
        # #530: a truncated episode ends as soon as the listing is complete
        # again; an unremovable one when no file is left unremovable
        if resolved == "truncated":
            detail = ("quota %s: the listing is complete again; the previous "
                      "`truncated` episode is over" % q)
        elif resolved == "mounts_unreadable":
            # #718: the mount table is readable again and the pass scanned
            detail = ("quota %s: the mount table is readable again; the previous "
                      "`mounts_unreadable` episode is over" % q)
        else:
            detail = ("quota %s: no file this pass cannot remove any more; the "
                      "previous `unremovable` episode is over" % q)
        out.append({"kind": kind, "action": "resolved", "path": path, "bytes": 0,
                    "agent": agent, "lot": lot, "data": data,
                    "detail": detail})
    for tree in result["worktrees"]:
        out.append({"kind": kind, "action": "signaled", "path": tree, "agent": agent,
                    "lot": lot, "data": {"worktree": tree, "quota": quota},
                    "detail": "git work tree inside %s left out of the quota eviction" % path})
    for link in result.get("worktree_symlinks", ()):
        out.append({"kind": kind, "action": "signaled", "path": link, "agent": agent,
                    "lot": lot, "data": {"worktree": link, "symlink": True, "quota": quota},
                    "detail": "git marker `.git` of %s is a symbolic link: not followed, "
                              "the subtree is left out of the eviction" % link})
    return out


# --------------------------------------------------------------------------
# /tmp du système : signalé, jamais supprimé
# --------------------------------------------------------------------------

def tmp_names(path: str | None = None) -> set[str] | None:
    """Noms de premier niveau du /tmp du système (None si illisible)."""
    try:
        return set(os.listdir(path or resources_mod.system_tmp()))
    except OSError:
        return None


def new_tmp_entries(before: set[str] | None, *, path: str | None = None,
                    min_size: int = DEFAULT_ORPHAN_MIN_SIZE,
                    exclude: tuple[str, ...] = ()) -> list[dict]:
    """Entrées de /tmp apparues depuis `before`, à nous, d'au moins
    `min_size` octets : chemin, taille, commande proposée, et l'identité de
    l'entrée (`ino`, `dev`) — le ménage ne supprime que CETTE entrée-là, pas
    une homonyme recréée depuis (L162)."""
    if before is None:
        return []
    base = path or resources_mod.system_tmp()
    after = tmp_names(base)
    if after is None:
        return []
    uid = os.getuid()
    real_excluded = {os.path.realpath(e) for e in exclude if e}
    out = []
    for name in sorted(after - before):
        full = os.path.join(base, name)
        try:
            st = os.lstat(full)
        except OSError:
            continue
        if st.st_uid != uid or os.path.realpath(full) in real_excluded:
            continue
        size = size_of(full)
        if size < min_size:
            continue
        out.append({"path": full, "bytes": size, "command": "rm -rf -- %s" % shlex.quote(full),
                    "ino": st.st_ino, "dev": st.st_dev})
    return out


# --------------------------------------------------------------------------
# system /tmp: entries that appeared during a turn, signaled and never deleted
# since #222; a signal attributed before #222 is deleted once abandoned (L162;
# incident of 2026-10-11 at 03:38: 12 GB tmpfs 83 % full, pressure gate
# closed, thirteen agents idle)
# --------------------------------------------------------------------------

def _blocks(st) -> int:
    return st.st_blocks * 512 if getattr(st, "st_blocks", None) else st.st_size


def fmt_idle(seconds: float) -> str:
    """« 2 h », « 90 min », « 45 s »."""
    seconds = max(0, int(round(float(seconds))))
    if seconds >= 3600 and seconds % 3600 == 0:
        return "%d h" % (seconds // 3600)
    if seconds >= 60:
        return "%d min" % (seconds // 60)
    return "%d s" % seconds


def scan_activity(path: str, limit: int = WALK_LIMIT,
                  budget: PassBudget | None = None) -> dict:
    """Parcours d'une entrée pour juger de son abandon : `bytes`, `latest` (la
    plus récente des dates de modification ET de changement d'état de
    l'entrée et de tout ce qu'elle contient — le ctime parce qu'une archive
    extraite garde les mtimes de l'archive), `complete`, `why`.

    Sans suivre les liens ni changer de système de fichiers. Un point de
    montage dedans, une entrée illisible, plus de `limit` entrées ou le budget
    du passage (#3802) rendent le parcours incomplet : on ne juge pas ce qu'on
    n'a pas vu."""
    out: dict = {"bytes": 0, "latest": None, "complete": True, "why": ""}
    try:
        top = os.lstat(path)
    except OSError as exc:
        out.update(complete=False, why="unreadable (%s)" % (exc.strerror or exc))
        return out
    latest = max(top.st_mtime, top.st_ctime)
    if not stat.S_ISDIR(top.st_mode):
        out.update(bytes=_blocks(top), latest=latest)
        return out
    count = 0
    stack = [path]
    while stack:
        current = stack.pop()
        try:
            with os.scandir(current) as it:
                for entry in it:
                    try:
                        st = entry.stat(follow_symlinks=False)
                    except OSError:
                        out.update(complete=False, why="unreadable entry: %s" % entry.path)
                        continue
                    count += 1
                    latest = max(latest, st.st_mtime, st.st_ctime)
                    if stat.S_ISDIR(st.st_mode):
                        if st.st_dev != top.st_dev:
                            out.update(complete=False, latest=latest,
                                       why="mount point inside: %s" % entry.path)
                            return out
                        stack.append(entry.path)
                    else:
                        out["bytes"] += _blocks(st)
                    if count >= limit:
                        out.update(complete=False, latest=latest,
                                   why="more than %d entries: activity not verifiable" % limit)
                        return out
                    if budget is not None and budget.expired():
                        out.update(complete=False, latest=latest,
                                   why="time budget of the pass reached")
                        return out
        except OSError:
            out.update(complete=False, why="unreadable directory: %s" % current)
    out["latest"] = latest
    return out


def _bare_git_copy(path: str) -> bool:
    """Un dépôt nu : `HEAD`, `objects` et `refs`, sans `.git` (#2525).
    Signature seule, sans lancer git : un faux positif garde l'entrée, jamais
    l'inverse."""
    return not os.path.lexists(os.path.join(path, ".git")) \
        and os.path.isfile(os.path.join(path, "HEAD")) \
        and os.path.isdir(os.path.join(path, "objects")) \
        and os.path.isdir(os.path.join(path, "refs"))


def _git_copies_report(path: str, limit: int | None = None,
                       skip=()) -> tuple[list[str], str]:
    """(copies git de l'entrée, doute). Une copie est reconnue par son `.git`
    (dossier, ou fichier d'un worktree) ou sa signature de dépôt nu, à toute
    profondeur ; un lien symbolique n'est ni suivi ni ouvert, et le dedans
    d'un dépôt nu (aucun arbre de travail) n'est pas parcouru.

    Parcours borné à `limit` dossiers ouverts (défaut `WALK_LIMIT`, comme les
    autres parcours du ménage) et arrêté au premier doute — dossier ou entrée
    illisible, budget épuisé — pour que l'appelant garde l'entrée au lieu de
    conclure à tort qu'il n'y a rien. Les copies à arbre de travail passent
    avant les dépôts nus : leurs gardes sont plus précises, un dépôt nu n'est
    qu'un doute conservateur (#2525).

    #3277 : `skip`, les dossiers d'un AUTRE utilisateur que celui-ci ne peut
    pas lire (relevés par `housekeeping_stale.walk`) : ni ouverts ni comptés
    comme doute — l'appelant ne peut pas plus les supprimer que les lire."""
    if os.path.islink(path) or not os.path.isdir(path):
        return [], ""
    skip = frozenset(skip or ())
    limit = WALK_LIMIT if limit is None else limit
    trees: list[str] = []
    bare: list[str] = []
    stack = [path]
    walked = 0
    while stack:
        current = stack.pop()
        walked += 1
        if walked > limit:
            return trees + bare, "more than %d directories: contents not verifiable" % limit
        if _bare_git_copy(current):
            bare.append(current)
            continue          # le dedans d'un dépôt nu ne cache pas d'arbre de travail
        if current in skip:
            continue
        if os.path.lexists(os.path.join(current, ".git")):
            trees.append(current)
        try:
            with os.scandir(current) as it:
                entries = sorted(it, key=lambda e: e.name)
        except OSError:
            return trees + bare, "unreadable directory: %s" % current
        for entry in reversed(entries):
            if entry.name == ".git":
                continue      # métadonnées d'une copie, pas une copie
            try:
                st = entry.stat(follow_symlinks=False)
            except OSError:
                return trees + bare, "unreadable entry: %s" % entry.path
            if stat.S_ISDIR(st.st_mode):
                stack.append(entry.path)
    return trees + bare, ""


def _git_copies(path: str) -> list[str]:
    """Les copies git de l'entrée à toute profondeur, sans le doute
    (`_git_copies_report`) : clone, worktree de relecture, dépôt nu."""
    return _git_copies_report(path)[0]


def unsaved_git_work(path: str, rules=()) -> str:
    """Pourquoi une copie git de l'entrée porte du travail à garder, vide
    sinon : verrouillée, modifications non commitées (ou état illisible),
    fichiers ignorés hors artefacts de build, HEAD sur aucune branche
    distante (commits jamais poussés), et pour un clone ses branches locales,
    son stash, ses autres worktrees liés, un remote qui vit dans l'entrée ou
    le dépôt commun d'un worktree lié qui y vit ; un dépôt nu et un parcours
    incomplet gardent l'entrée (#2493/#2512/#2525)."""
    copies, doubt = _git_copies_report(path)
    for copy in copies:
        work = _git_copy_work(copy, path, rules)
        if work:
            return work
    if doubt:
        return "copies not fully inspected (%s)" % doubt
    return ""


def tmp_recovery_command(path: str, *,
                         report: tuple[list[str], str] | None = None) -> str:
    """#2493 : the commands that inspect a temporary directory before a human
    moves or removes it (never a blind `rm -rf`). Git copies get the
    saved-work checks; a plain directory just its listing. `report` (#2556)
    is the `_git_copies_report` already read for this entry, so a pass does
    not walk a deep tree once per helper."""
    copies = _git_copies_report(path)[0] if report is None else report[0]
    if not copies:
        return "ls -la -- %s" % shlex.quote(path)
    commands = []
    for copy in copies:
        if _bare_git_copy(copy):
            commands.append("git -C %s show-ref" % shlex.quote(copy))
            commands.append("git -C %s log --all --not --remotes" % shlex.quote(copy))
            continue
        commands.append("git -C %s status --short" % shlex.quote(copy))
        commands.append("git -C %s log --all --not --remotes" % shlex.quote(copy))
    return " ; ".join(commands)


def _clone_remote_inside(path: str, entry: str) -> str:
    """#2493 : why the remotes of this CLONE do not prove it saved: one lives
    inside the temporary directory `entry` that is about to be removed, so
    the commits its remote-tracking refs vouch for go with it. Empty when
    every remote is outside (or is not a local path)."""
    remotes = _git(["remote"], path)
    if remotes is None or remotes.returncode != 0:
        return "remote URLs unreadable"
    urls = []
    for remote in remotes.stdout.splitlines():
        # get-url applies Git's insteadOf rewrites, unlike raw config URLs.
        resolved = _git(["remote", "get-url", "--all", remote], path)
        if resolved is None or resolved.returncode != 0:
            return "remote URLs unreadable"
        urls.extend(resolved.stdout.splitlines())
    root = _real(entry)
    for value in urls:
        if value.startswith("file:"):
            try:
                parsed = urlsplit(value)
            except ValueError:
                return "local remote URL unreadable"
            if parsed.netloc not in ("", "localhost"):
                return "local remote URL authority unknown"
            value = unquote(parsed.path)
        elif ":" in value and not os.path.isabs(value):
            # Network URL or scp-style host:path; all other Git URLs are
            # local paths, including relative names without a leading dot.
            continue
        local = _real(os.path.join(path, os.path.expanduser(value)))
        if _under(local, root):
            return "remote %s lives inside the temporary directory" % value
    return ""


def _git_copy_work(copy: str, entry: str, rules=()) -> str:
    """#2493/#2512 : why the Git copy `copy` of the temporary directory
    `entry` holds work to keep, empty when it is safe to let go. One source
    for both passes — the attributed /tmp entry of `unsaved_git_work` and the
    agent directory of `tmp_git_work` — so a guard added for one never misses
    the other: locked, unreadable metadata, submodules, modified or untracked
    files, ignored files outside the build output `rules`, HEAD on no remote
    branch, for a CLONE all local refs, stash, other linked worktrees and
    a remote that lives inside `entry`, and no linked worktree whose common
    repository lives inside `entry`. A bare repository has no working tree to
    prove its refs saved: it is a doubt and keeps the entry (#2525)."""
    if _bare_git_copy(copy):
        return "%s: bare repository (no working tree to prove its commits saved)" % copy
    st = worktree_state(copy)
    if not st["exists"]:
        return "%s: %s" % (copy, st["reason"] or "directory gone")
    if st["locked"]:
        return "%s locked (git worktree lock)" % copy
    if not st["gitdir"]:
        return "%s: git metadata unreadable" % copy
    if os.path.isfile(os.path.join(copy, ".git")) and _under(
            _real(common_dir(copy)), _real(entry)):
        return "%s: common repository lives inside the temporary directory" % copy
    if st["submodules"]:
        return "%s has submodules" % copy
    if not st["clean"]:
        return "%s: %s" % (copy, st["reason"] or "uncommitted changes")
    if not (st["pushed"] or st["integrated"]):
        return "%s: HEAD %s on no remote branch" % (copy, st["head"][:12] or "?")
    # an ignored `.env` or dataset would go with the directory (relecture
    # cx1 sur ab2a2c8c, #2512) ; only regenerable build output is exempt
    leftovers = ignored_leftovers(copy, rules)
    if leftovers is None:
        return "%s: ignored files unreadable (git status --ignored)" % copy
    if leftovers:
        return "%s: %d ignored file(s) outside build artifacts (%s)" % (
            copy, len(leftovers), leftovers[0])
    if os.path.isdir(os.path.join(copy, ".git")):
        linked = _git(["worktree", "list", "--porcelain"], copy)
        if linked is None or linked.returncode != 0:
            return "%s: linked worktrees unreadable" % copy
        if sum(line.startswith("worktree ") for line in linked.stdout.splitlines()) > 1:
            return "%s: the clone has other linked worktrees" % copy
        # A clone takes all its refs with it (a linked worktree leaves them
        # in its common repository): branches alone miss tags and custom
        # refs (#2827). Every reachable commit must be on a remote branch.
        # Unreadable: a doubt, the copy stays.
        local = _git(["log", "--all", "--not", "--remotes", "--format=%H",
                      "-1"], copy)
        if local is None or local.returncode != 0:
            return "%s: local refs unreadable" % copy
        if local.stdout.strip():
            return "%s: local ref commit %s on no remote branch" % (
                copy, local.stdout.strip()[:12])
        stash = _git(["rev-parse", "-q", "--verify", "refs/stash"], copy)
        if stash is None or stash.returncode not in (0, 1):
            return "%s: stash unreadable" % copy
        if stash.returncode == 0:
            return "%s: has a stash" % copy
        remote = _clone_remote_inside(copy, entry)
        if remote:
            return "%s: %s" % (copy, remote)
    return ""


def tmp_git_work(path: str, rules=(), *,
                 report: tuple[list[str], str] | None = None) -> str:
    """#2493 (N2) : why a Git copy inside a temporary directory must be kept,
    empty when every copy is safe to recover: the saved-work guards of
    `_git_copy_work` (clean, pushed or integrated, no ignored file outside
    the build output, for a clone no local ref commit outside remote branches,
    stash, other linked worktree nor remote inside the directory, and no linked worktree
    whose common repository is inside it) and no process inside.
    A directory is never deleted merely because its agent left the registry;
    this check is the second condition. A bare repository, or a directory
    tree whose copies could not all be inspected (#2525), keeps it.
    `report` (#2556) reuses the `_git_copies_report` of the pass."""
    copies, doubt = _git_copies_report(path) if report is None else report
    for copy in copies:
        work = _git_copy_work(copy, path, rules)
        if work:
            return work
        in_use, detail = worktree_holders(copy)
        if in_use:
            return "%s: %s" % (copy, detail)
    if doubt:
        return "copies not fully inspected (%s)" % doubt
    return ""


def recover_tmp_git_copies(path: str, *,
                           report: tuple[list[str], str] | None = None
                           ) -> tuple[bool, str, list[str]]:
    """#2493 (N2) : remove the linked worktrees of `path` with `git worktree
    remove` (never `--force`, its own checks still apply), after `tmp_git_work`
    proved them saved. Clones stay for `_safe_rmtree`; a bare repository is a
    doubt and never gets here. An inspection cut short by a doubt removes
    nothing (#2525). `report` (#2556) reuses the `_git_copies_report` of the
    pass. (ok, error, common repositories to prune)."""
    repos: list[str] = []
    copies, doubt = _git_copies_report(path) if report is None else report
    if doubt:
        return False, "git copies not fully inspected (%s)" % doubt, []
    for copy in copies:
        if _bare_git_copy(copy):
            continue                     # no worktree to unlink: gone with the dir
        if not os.path.isfile(os.path.join(copy, ".git")):
            continue                     # a clone: clean and pushed, deleted with the dir
        repo = common_dir(copy)
        ok, error = remove_worktree(copy, repo)
        if not ok:
            return False, "%s: %s" % (copy, error), repos
        if repo:
            repos.append(repo)
    return True, "", repos


def tmp_holder(path: str, allow=()) -> str:
    """Le processus qui tient `path` (fichier ouvert dedans, dossier courant,
    chemin sur sa ligne de commande), en clair ; vide si aucun. Si l'OS ne
    permet pas de le savoir, une raison : l'entrée est gardée (fail-closed).
    `allow` (L230) : noms des processus dont l'illisibilité n'est pas un doute
    — le choix de l'hôte, vide par défaut ; un détenteur constaté reste dit.
    L'appel sans `allow` pose la question d'origine (un seul argument) : c'est
    lui qui découvre l'illisibilité, `_orphan_verdict` repose la question avec
    l'allow de l'hôte quand il y en a un."""
    try:
        found = platform.holders(_real(path), needle=os.path.abspath(path),
                                 allow=allow)
    except platform.NotAvailable as exc:
        return "%s (%s)" % (HOLDERS_UNREADABLE, exc)
    if not found:
        return ""
    first = found[0]
    why = first.get("why") or ""
    if "command line" in why or "ligne de commande" in why:
        why = "path on the command line"
    return "held by process %s (%s: %s)" % (first.get("pid"), why,
                                           holder_name(first))


def holder_name(holder: dict) -> str:
    """Le nom d'un détenteur de `platform.holders`, bon pour le journal
    partagé : la base de son argv[0], jamais l'argv (#3403 : identifiant de
    session, DSN avec mot de passe, en-tête curl passés en argument)."""
    name = holder.get("name")
    if not name:
        words = (holder.get("cmd") or "").split(None, 1)
        name = os.path.basename(words[0]) if words else ""
    return name or holder.get("why") or "?"


def tmp_user_holder(path: str, procs=None) -> str:
    """#2704 : le processus qui utilise `path`, d'après les chemins observés de
    l'utilisateur (dossier courant, exécutable, fichiers ouverts et projetés en
    mémoire — `/proc/<pid>/maps`), en clair ; vide si aucun.

    `procs` : un relevé `platform.user_paths()` déjà lu (un seul par passage,
    False quand la lecture a échoué), sinon il est lu ici. Contrairement à
    `tmp_holder`, un processus illisible n'est pas un doute qui retient tout le
    ménage : un démon non dumpable (`gpg-agent`) n'a rien à faire dans le
    dossier temporaire d'un agent, et l'ignorer évite d'arrêter toute éviction
    de l'hôte. Seuls les chemins observés retiennent l'entrée. Hors Linux
    (`user_paths` indisponible), on ne peut rien observer : vide, comme avant
    #2704 — le défaut est propre à `/proc`."""
    if procs is None:
        try:
            procs = platform.user_paths()
        except Exception:  # noqa: BLE001 - un passage de ménage ne lève jamais
            return ""
    if procs is False or not getattr(procs, "available", False):
        return ""
    found = procs.holders(_real(path))
    return "held by process %s" % found[0] if found else ""


def _tmp_entry_problem(path: str, base: str, ino=None, dev=None) -> str:
    """Pourquoi `path` n'est pas une entrée de /tmp que le ménage peut
    supprimer, vide si elle l'est : enfant direct de `base` (le /tmp du
    système, jamais ailleurs), dossier ou fichier ordinaire (pas un lien), à
    l'utilisateur, et — si connue — la MÊME entrée que celle signalée (inode)."""
    name = os.path.basename(path)
    if not name or name in (".", "..") or \
            os.path.realpath(os.path.dirname(os.path.abspath(path))) != os.path.realpath(base):
        return "outside the system /tmp (%s)" % base
    try:
        st = os.lstat(path)
    except OSError:
        return "unreadable"
    if stat.S_ISLNK(st.st_mode) or not (stat.S_ISDIR(st.st_mode) or stat.S_ISREG(st.st_mode)):
        return "neither a directory nor a regular file"
    if st.st_uid != os.getuid():
        return "belongs to another user"
    if ino is not None and (st.st_ino, st.st_dev) != (ino, dev):
        return "recreated since it was flagged (different inode)"
    return ""


def _remove_tmp_entry(path: str, base: str, ino=None, dev=None) -> int:
    """Supprime une entrée de /tmp, revérifiée juste avant
    (`_tmp_entry_problem`) ; rend les octets libérés."""
    problem = _tmp_entry_problem(path, base, ino, dev)
    if problem:
        raise RefusedDeletion("%s: %s" % (path, problem))
    before = size_of(path)
    if stat.S_ISDIR(os.lstat(path).st_mode):
        _make_owned_dirs_writable(path)
        shutil.rmtree(path, ignore_errors=True)
    else:
        os.unlink(path)
    left = size_of(path) if os.path.lexists(path) else 0
    return max(0, before - left)


def _make_owned_dirs_writable(top: str) -> None:
    """Rend lecture, écriture et traversée à NOS dossiers sous `top` (un cache
    de modules Go est en lecture seule) : sans cela, la suppression resterait
    partielle. Aucun lien suivi, aucun dossier d'un autre utilisateur touché."""
    uid = os.getuid()

    def open_path(full: str) -> None:
        try:
            st = os.lstat(full)
            if stat.S_ISDIR(st.st_mode) and st.st_uid == uid and (st.st_mode & 0o700) != 0o700:
                os.chmod(full, stat.S_IMODE(st.st_mode) | 0o700)
        except OSError:
            pass

    open_path(top)
    for current, dirs, _files in os.walk(top):   # followlinks=False
        for name in dirs:   # avant que os.walk n'y descende
            open_path(os.path.join(current, name))


def _orphan_verdict(row: dict, policy: Policy, *, base: str, registered, protected: set,
                    running: set, now: float) -> tuple[str, str]:
    """(`suppression` | `attend` | `garde`, raison) pour une entrée signalée
    encore présente. Toutes les conditions de la suppression, la moins chère
    d'abord ; le moindre doute garde l'entrée."""
    path = row["path"]
    data = row.get("data") or {}
    if not policy.orphan_cleanup:
        return "garde", "deletion disabled on this host (orphan_cleanup: false)"
    problem = _tmp_entry_problem(path, base, data.get("ino"), data.get("dev"))
    if problem:
        return "garde", problem
    agent = row.get("agent")
    if not agent:   # #222: unattributed, hence never deleted
        return "garde", "never deleted by ameesh: %s" % UNATTRIBUTED
    if registered is None:
        return "garde", "registry unreadable"
    if agent not in registered:
        return "garde", "agent %s not in the registry" % agent
    if agent in running:
        return "attend", "turn in progress for %s" % agent
    target = _real(path)
    if any(_under(p, target) for p in protected):
        return "garde", "contains an agent's working directory or a managed directory"
    scan = scan_activity(path)
    if not scan["complete"]:
        return "garde", scan["why"]
    idle = now - float(scan["latest"])
    if idle < policy.orphan_idle:
        return "attend", "modified %s ago (deleted after %s without change)" % (
            fmt_idle(max(0.0, idle)), fmt_idle(policy.orphan_idle))
    holder = tmp_holder(path)
    if holder.startswith(HOLDERS_UNREADABLE) and policy.unreadable_ok:
        # L230 : la première lecture est muette (processus non dumpable) ;
        # l'hôte qui a nommé ces processus repose la question avec son allow,
        # et un détenteur CONSTATÉ reste rapporté.
        holder = tmp_holder(path, allow=policy.unreadable_ok)
    if holder:
        return "attend", holder
    # #2512 : an attributed entry (before #222) gets the same saved-work
    # guards as the agent directories, build artifacts exempted by the host's
    # policy — an ignored `.env` or an unpushed local branch keeps it too.
    work = unsaved_git_work(path, build_artifacts.rules_of(policy.artifacts))
    if work:
        return "garde", "git work to keep — %s" % work
    return "suppression", "left by turn %s of %s, unchanged for %s, held by no " \
        "process" % (str(data.get("turn") or "?")[:8], agent, fmt_idle(idle))


def orphan_pass(db, host: str, policy: Policy, *, registered, protected: set,
                running: set, dry_run: bool, now: float,
                budget: PassBudget | None = None) -> tuple[list, list, list]:
    """Entrées de /tmp signalées pendant un tour (L162) : (lignes du journal,
    plan lisible, entrées encore présentes avec leur sort).

    La dernière ligne de chaque chemin compte : `signaled` est candidate,
    `deleted` est close (une homonyme recréée hors d'un tour n'est pas à
    nous). Suppression seulement si `_orphan_verdict` le permet ; sinon
    l'entrée reste signalée, avec la raison. Rien n'est écrit en base ici."""
    base = resources_mod.system_tmp()
    protected_real = {_real(p) for p in protected if p}
    entries: list[dict] = []
    plan: list[dict] = []
    present: list[dict] = []
    if not host:   # sans nom d'hôte, les signalements des autres hôtes s'y mêleraient
        return entries, plan, present
    try:
        # #222: the last line of every path, with no window nor limit: an
        # unattributed entry is followed for as long as it exists
        rows = storage.of(db).housekeeping.latest_by_path(host, "orphan")
    except Exception:
        return entries, plan, present
    for row in rows:
        if budget is not None and budget.expired():
            budget.timed_out = True
            break                      # #3802: a bounded host pass
        path = row.get("path") or ""
        if not path or row.get("action") != "signaled":
            continue
        if row.get("agent") and now - float(row.get("at_ts") or 0) > ORPHAN_LOOKBACK_S:
            continue   # an attributed signal (before #222) is read for 7 days, as before
        data = row.get("data") or {}
        base_item = {"kind": "orphan", "path": path, "agent": row.get("agent"),
                     "lot": row.get("lot")}
        if not os.path.lexists(path):
            if not row.get("agent") and not dry_run:
                # #222: the tracking of an unattributed entry ends when it is gone
                entries.append(dict(base_item, action="gone", bytes=0, data=data,
                                    detail="unattributed entry gone from /tmp"))
            continue
        action, why = _orphan_verdict(row, policy, base=base, registered=registered,
                                      protected=protected_real, running=running, now=now)
        size = size_of(path)
        plan.append(dict(base_item, action=action, bytes=size, detail=why))
        if not row.get("agent") and not dry_run \
                and now - float(row.get("at_ts") or 0) >= UNATTRIBUTED_REFRESH_S:
            # #222: never deleted, an unattributed entry stays signaled, with
            # its current size, for as long as it is on disk (alerts read 24 h)
            # #2933: retain the first observation, including legacy signals
            # whose data predates first_seen; at_ts is only the latest reading.
            entries.append(dict(base_item, action="signaled", bytes=size,
                                data=dict(data, first_seen=data.get("first_seen")
                                          or row.get("at_ts")),
                                detail=row.get("detail") or UNATTRIBUTED))
        item = {"path": path, "bytes": size, "agent": row.get("agent"),
                "at_ts": row.get("at_ts"), "status": action, "detail": why,
                "command": data.get("command") or "rm -rf -- %s" % shlex.quote(path)}
        if action != "suppression" or dry_run:
            present.append(item)
            continue
        copies, doubt = _git_copies_report(path)
        if doubt:
            item.update(status="garde",
                        detail="git copies not fully inspected (%s)" % doubt)
            present.append(item)
            continue
        worktrees = [common_dir(c) for c in copies
                     if os.path.isfile(os.path.join(c, ".git"))]
        try:
            freed = _remove_tmp_entry(path, base, data.get("ino"), data.get("dev"))
        except (OSError, RefusedDeletion) as exc:
            item.update(status="garde", detail="deletion refused: %s" % exc)
            present.append(item)
            continue
        for repo in worktrees:
            prune_worktrees(repo)
        entries.append(dict(base_item, action="deleted", bytes=freed,
                            detail="abandoned /tmp entry: %s" % why,
                            data={"turn": data.get("turn"), "command": item["command"]}))
        if os.path.lexists(path):  # suppression partielle : ce qui reste est dit
            present.append(dict(item, bytes=size_of(path), status="garde",
                                detail="partial deletion (files of another "
                                       "user?)"))
    return entries, plan, present


def _owner(uid: int) -> str:
    try:
        import pwd  # Unix seulement
        return pwd.getpwuid(uid).pw_name
    except (ImportError, KeyError):
        return str(uid)


def tmp_top(base: str | None = None, n: int = TMP_TOP,
            budget: PassBudget | None = None) -> list[dict]:
    """Les `n` plus grosses entrées de premier niveau du /tmp du système
    (lecture seule ; taille bornée à `WALK_LIMIT` fichiers par entrée) :
    `path`, `bytes`, `owner`, `mtime`."""
    base = base or resources_mod.system_tmp()
    try:
        names = os.listdir(base)
    except OSError:
        return []
    out = []
    for name in names:
        full = os.path.join(base, name)
        try:
            st = os.lstat(full)
        except OSError:
            continue
        if stat.S_ISLNK(st.st_mode):
            continue
        if budget is not None and budget.expired():
            budget.timed_out = True
            break                      # #3802: a bounded host pass
        out.append({"path": full, "bytes": size_of(full, budget=budget),
                    "owner": _owner(st.st_uid), "mtime": st.st_mtime})
    out.sort(key=lambda e: (-e["bytes"], e["path"]))
    return out[:n]


def describe_top(top) -> str:
    """« /tmp/a 2.2 GiB (smichea, laissé par b1 : attend — …) ; … »."""
    parts = []
    for e in top or ():
        who = e.get("owner") or "?"
        if e.get("agent"):
            who += ", left by %s" % e["agent"]
            if e.get("status"):
                who += ": %s" % action_label(e["status"])
        parts.append("%s %s (%s)" % (e.get("path"), _fmt(e.get("bytes")), who))
    return "; ".join(parts)


def tmpfs_hint(top, policy: Policy) -> str:
    """Le complément d'une alerte de tmpfs plein : les plus grosses entrées
    (si un bilan les a relevées) et l'action attendue."""
    plus_gros = ("largest in /tmp: %s" % describe_top(top)) if top else \
        "largest in /tmp: `ameesh menage` on the host"
    return "%s — %s" % (plus_gros, TMPFS_ACTION % fmt_idle(policy.orphan_idle))


# --------------------------------------------------------------------------
# worktrees
# --------------------------------------------------------------------------

def _git(args: list[str], cwd: str, timeout: float = GIT_TIMEOUT, env=None,
         budget: PassBudget | None = None):
    if budget is not None:
        timeout = budget.timeout(timeout)
        if timeout <= 0.0:
            return None
    try:
        proc = subprocess.run(["git", "-C", cwd, *args], capture_output=True, text=True,
                              timeout=timeout, env=env)
    except (OSError, subprocess.SubprocessError):
        return None
    return proc


def list_worktrees(cwd: str | None, budget: PassBudget | None = None) -> list[dict]:
    """Worktrees du dépôt de `cwd` (`git worktree list --porcelain`) ; le
    premier est le worktree principal. Vide si `cwd` n'est pas un dépôt ou si
    le budget du passage (#3802) est épuisé."""
    if not cwd or not os.path.isdir(cwd):
        return []
    proc = _git(["worktree", "list", "--porcelain"], cwd, budget=budget)
    if proc is None or proc.returncode != 0:
        return []
    out: list[dict] = []
    current: dict = {}
    for line in proc.stdout.splitlines() + [""]:
        if not line.strip():
            if current.get("path"):
                out.append(current)
            current = {}
            continue
        key, _, value = line.partition(" ")
        if key == "worktree":
            current = {"path": os.path.abspath(value), "head": "", "branch": "",
                       "detached": False, "bare": False, "locked": False}
        elif key == "HEAD":
            current["head"] = value
        elif key == "branch":
            current["branch"] = value.removeprefix("refs/heads/")
        elif key == "detached":
            current["detached"] = True
        elif key == "bare":
            current["bare"] = True
        elif key == "locked":
            current["locked"] = True
    return out


def common_dir(cwd: str, budget: PassBudget | None = None) -> str:
    proc = _git(["rev-parse", "--git-common-dir"], cwd, budget=budget)
    if proc is None or proc.returncode != 0 or not proc.stdout.strip():
        return ""
    return os.path.abspath(os.path.join(cwd, proc.stdout.strip()))


def same_repository(path: str, repo: str,
                    budget: PassBudget | None = None) -> bool:
    """#2180: `path` is a worktree of `repo`, whichever form the record kept
    (common dir from `common_dir`, or top level) and through symlinks."""
    here = common_dir(path, budget=budget)
    there = common_dir(repo, budget=budget) if repo and os.path.isdir(repo) else ""
    return bool(here and there) and _real(here) == _real(there)


def _scratch_merge_tree(path: str, ref: str, budget: PassBudget | None = None):
    """`git merge-tree --write-tree ref HEAD` whose new objects go to a
    scratch directory (the repository's objects as alternates), deleted
    afterwards: nothing is written in `.git` (L230, like the radar)."""
    import tempfile
    objects = _git(["rev-parse", "--path-format=absolute", "--git-path", "objects"], path,
                   budget=budget)
    if objects is None or objects.returncode != 0 or not objects.stdout.strip():
        return None
    scratch = tempfile.mkdtemp(prefix="ameesh-menage-merge-")
    try:
        env = dict(os.environ, GIT_OBJECT_DIRECTORY=scratch,
                   GIT_ALTERNATE_OBJECT_DIRECTORIES=objects.stdout.strip())
        return _git(["merge-tree", "--write-tree", ref, "HEAD"], path, env=env,
                    budget=budget)
    finally:
        shutil.rmtree(scratch, ignore_errors=True)


def ignored_leftovers(path: str, rules=(), budget: PassBudget | None = None
                      ) -> list[str] | None:
    """L230: what `git status --ignored` lists in the worktree (modified,
    untracked or ignored; a directory with nothing tracked inside as one
    line), except the directories of the build output `rules` (none:
    nothing is exempt). An ignored `.env` or dataset that removing the
    worktree would delete. None if git cannot say."""
    proc = _git(["--no-optional-locks", "status", "--porcelain", "--ignored",
                 "--untracked-files=normal"], path, budget=budget)
    if proc is None or proc.returncode != 0:
        return None
    out = []
    for line in proc.stdout.splitlines():
        if not line.strip():
            continue
        rel = line[3:].strip().strip('"').rstrip("/")
        parent, name = os.path.split(os.path.join(path, rel))
        if line.startswith("!! ") and os.path.isdir(os.path.join(parent, name)) \
                and not os.path.islink(os.path.join(parent, name)) \
                and any(rule.matches(parent, name) for rule in rules):
            continue
        out.append(rel)
    return out


def _default_remote_ref(path: str, budget: PassBudget | None = None) -> str:
    """Branche principale du dépôt distant (`origin/HEAD`, sinon `origin/main`,
    sinon `origin/master`) ; vide si aucune n'existe."""
    upstream = _git(["rev-parse", "--abbrev-ref", "origin/HEAD"], path, budget=budget)
    candidates = []
    if upstream is not None and upstream.returncode == 0 and upstream.stdout.strip() \
            and upstream.stdout.strip() != "origin/HEAD":
        candidates.append(upstream.stdout.strip())
    candidates += ["origin/main", "origin/master"]
    for ref in candidates:
        found = _git(["rev-parse", "--verify", "--quiet", ref + "^{commit}"], path,
                     budget=budget)
        if found is not None and found.returncode == 0:
            return ref
    return ""


def worktree_state(path: str, budget: PassBudget | None = None) -> dict:
    """État d'un worktree pour son retrait : `exists`, `clean`, `pushed` (HEAD
    sur une branche distante), `integrated` (HEAD contenu dans la branche
    principale distante, ou intégré par rebase), `locked` (`git worktree
    lock`), `head`, `reason` (pourquoi il ne peut pas être retiré, vide
    sinon).

    Un retrait DIRECT exige `integrated` : une branche poussée mais pas
    fusionnée est du travail en cours (L73b) ; un worktree non intégré peut
    partir par la grâce L230 (`pushed`) ou, pour un lot fini, après archivage
    de son travail (`policy.housekeeping.dirty_worktree_after`, #3728/#4442,
    `HEAD.bundle` pour les commits absents de la forge)."""
    out = {"exists": os.path.isdir(path), "clean": False, "pushed": False,
           "integrated": False, "locked": False, "head": "", "branch": "", "reason": "",
           "gitdir": "", "submodules": ""}
    if not out["exists"]:
        out["reason"] = "directory gone"
        return out
    gitdir = _git(["rev-parse", "--absolute-git-dir"], path, budget=budget)
    if gitdir is not None and gitdir.returncode == 0 and gitdir.stdout.strip():
        out["gitdir"] = gitdir.stdout.strip()
        out["locked"] = os.path.exists(os.path.join(out["gitdir"], "locked"))
    out["submodules"] = submodules(path, out["gitdir"])
    # L230: never a write in .git (no index refresh), dry run or not
    status = _git(["--no-optional-locks", "status", "--porcelain", "--untracked-files=normal"],
                  path, budget=budget)
    if status is None or status.returncode != 0:
        out["reason"] = "git status unreadable"
        return out
    dirty = [line for line in status.stdout.splitlines() if line.strip()]
    out["clean"] = not dirty
    head = _git(["rev-parse", "HEAD"], path, budget=budget)
    out["head"] = head.stdout.strip() if head is not None and head.returncode == 0 else ""
    branch = _git(["symbolic-ref", "-q", "--short", "HEAD"], path, budget=budget)
    out["branch"] = branch.stdout.strip() if branch is not None and branch.returncode == 0 else ""
    remote = _git(["for-each-ref", "--contains", "HEAD", "--count=1", "refs/remotes"],
                  path, budget=budget)
    out["pushed"] = bool(remote is not None and remote.returncode == 0 and remote.stdout.strip())
    ref = _default_remote_ref(path, budget=budget) if out["head"] else ""
    if ref:
        ancestor = _git(["merge-base", "--is-ancestor", "HEAD", ref], path,
                        budget=budget)
        if ancestor is not None and ancestor.returncode == 0:
            out["integrated"] = True
        else:
            # Intégré par rebase : chaque commit propre a un équivalent sur la
            # branche principale du dépôt distant (`git cherry` : lignes « - »).
            cherry = _git(["cherry", ref, "HEAD"], path, budget=budget)
            if cherry is not None and cherry.returncode == 0:
                lines = [line for line in cherry.stdout.splitlines() if line.strip()]
                out["integrated"] = all(line.startswith("-") for line in lines)
        if not out["integrated"]:
            # Fusion par écrasement (squash) : fusionner HEAD dans la branche
            # principale ne changerait rien (`git merge-tree`, git ≥ 2.38).
            merged = _scratch_merge_tree(path, ref, budget=budget)
            tree = _git(["rev-parse", ref + "^{tree}"], path, budget=budget)
            if merged is not None and merged.returncode == 0 and tree is not None \
                    and tree.returncode == 0 and merged.stdout.strip():
                out["integrated"] = (merged.stdout.split()[0] == tree.stdout.strip())
    if out["locked"]:
        out["reason"] = "locked (git worktree lock)"
    elif out["submodules"]:
        out["reason"] = "submodules (%s)" % out["submodules"]
    elif dirty:
        out["reason"] = "%d file(s) modified or untracked" % len(dirty)
    elif not out["integrated"]:
        out["reason"] = "HEAD %s not integrated into %s (%s)" % (
            out["head"][:12] or "?", ref or "the remote main branch",
            "pushed but not merged" if out["pushed"] else "commits not pushed")
    return out


#: #4442 : ce qui, dans `worktree_state()["reason"]`, bouge d'un passage à
#: l'autre sans que la RAISON change (SHA de HEAD, nombre de fichiers) : la
#: grâce de 24 h et le statut `kept` se fondent sur la raison normalisée, donc
#: un détail volatil ne fait plus repartir le compteur.
_KEPT_REASON_RULES = (
    (re.compile(r"\bHEAD [0-9a-f]{7,40} not integrated\b"), "HEAD not integrated"),
    (re.compile(r"\b\d+ file\(s\) modified or untracked\b"),
     "modified or untracked files"),
    (re.compile(r"\b\d+ ignored file\(s\) outside build artifacts \([^)]*\)"),
     "ignored files outside build artifacts"),
)


def kept_reason(reason: str) -> str:
    """#4442 : la raison d'un worktree gardé, sans son détail volatil (SHA de
    HEAD, nombre de fichiers) : deux passages qui gardent le même travail
    gardent la même raison, et la grâce de 24 h ne repart pas."""
    out = reason or ""
    for pattern, stable in _KEPT_REASON_RULES:
        out = pattern.sub(stable, out)
    return out


def kept_detail(why: str, st: dict) -> str:
    """#4442 : `<why>: kept — <raison normalisée>` ; des commits non poussés
    restent nommés même quand des fichiers modifiés expliquent la garde."""
    reason = kept_reason(st.get("reason") or "")
    if not st.get("pushed") and not st.get("integrated") \
            and "commits not pushed" not in reason:
        reason = "commits not pushed" + ("; %s" % reason if reason else "")
    return "%s: kept — %s" % (why, reason)


def _unsaved_work(st: dict) -> str:
    """#4442 : ce qu'un retrait perdrait — fichiers modifiés ou non suivis,
    commits absents de la forge, ou les deux (texte stable pour l'archive)."""
    dirty = not st.get("clean")
    unpushed = bool(st.get("head")) and not st.get("pushed") and not st.get("integrated")
    if dirty and unpushed:
        return "modified or untracked files and commits not pushed"
    if dirty:
        return "modified or untracked files"
    return "commits not pushed"


def submodules(path: str, gitdir: str) -> str:
    """L230: why the worktree `path` may hold submodules ('' if it cannot):
    a `.gitmodules`, or a non-empty `<gitdir>/modules` (their repositories,
    with possibly unpushed commits, live in the worktree's administrative
    directory). Unknown gitdir or unreadable `modules`: a doubt, said so."""
    if os.path.lexists(os.path.join(path, ".gitmodules")):
        return ".gitmodules"
    if not gitdir:
        return "git directory of the worktree unknown"
    try:
        return "%s/modules" % gitdir if os.listdir(os.path.join(gitdir, "modules")) else ""
    except FileNotFoundError:
        return ""
    except OSError as exc:
        return "%s/modules illisible (%s)" % (gitdir, exc.strerror or exc)


def _under(path: str, root: str) -> bool:
    """`path` est `root` ou se trouve dessous (chemins absolus normalisés)."""
    path, root = os.path.normpath(path), os.path.normpath(root)
    return path == root or path.startswith(root.rstrip(os.sep) + os.sep)


def _real(path: str) -> str:
    return os.path.realpath(os.path.abspath(os.path.expanduser(path)))


def owns_worktree(path: str, cwd: str | None, other_cwds) -> bool:
    """Le worktree `path`, apparu pendant un tour lancé dans `cwd`, a-t-il été
    créé par CET agent ? (L73b)

    `git worktree list` couvre tout le dépôt : un worktree apparu pendant le
    tour peut être celui d'un autre agent qui travaille en parallèle sur le
    même dépôt. Critère retenu, le plus sûr dont on dispose sans trace du
    processus créateur : le worktree est SOUS le dossier de travail de
    l'agent (sous-agents, `.claude/worktrees/…`), et aucun autre agent n'a ce
    même dossier ni un dossier plus proche du worktree. Dans le doute : non."""
    if not cwd:
        return False
    mine, target = _real(cwd), _real(path)
    if target == mine or not _under(target, mine):
        return False
    for other in other_cwds or ():
        if not other:
            continue
        theirs = _real(other)
        if theirs == mine or (_under(target, theirs) and _under(theirs, mine)):
            return False
    return True


#: #1665 : branches qui nomment le lot qu'un worktree sert — `lot/<id>[-…]`
#: (L118b), `work/<id>[-…]`, `agent/<nom>-<id>[-…]`, `review/<nom>-<id>[-…]`.
#: Le nom de l'agent peut porter un chiffre (`mty-nxl-rel-01`, `deepseek3`) :
#: sous `lot/` et `work/` l'id est le premier groupe numérique, sous `agent/`
#: et `review/` le dernier. #1694 : une branche peut citer plusieurs groupes
#: (`review/claude2-t3-unit-459-460-r2`) ; ils sont essayés dans cet ordre.
#: #2182 : le chiffre que porte le nom de l'agent (`review/mty-amh-impl-02-…`)
#: n'est pas un lot — il est écarté quand l'agent de la branche est connu, et
#: un groupe complété d'un zéro (`-02`) ne l'est jamais (un id de lot s'écrit
#: en décimal, `#2`, jamais `#02`).
CITED_LOT = re.compile(r"^(lot|work|agent|review)/(.+)$")

#: #3266 : marqueurs d'un nom de dossier de worktree (`wt`, `work`, `review`…)
#: suivis d'un numéro ; sans agent nommé ni marqueur, un nombre du dossier
#: n'est pas un lot (une date `release-2026-10-23`, un SHA).
WORKTREE_NAME = re.compile(
    r"(?:^|[._-])(?:wt|work|lot|review|prep|tmp|scratch|agent|paquet)[._-]?\d",
    re.IGNORECASE)


def branch_agent(branch: str, agents=()) -> str:
    """#2182 : l'agent que nomme une branche `agent/<agent>-…` ou
    `review/<agent>-…` en tête, quand l'un de `agents` (le registre, ou
    l'agent enregistré pour ce worktree) le nomme ; le plus long gagne
    (`mty-amh-impl-02` plutôt que `mty-amh`). '' sinon — un nom qu'aucun
    agent connu ne porte n'est pas deviné."""
    found = CITED_LOT.match(branch or "")
    if not found or found.group(1) not in ("agent", "review"):
        return ""
    rest = found.group(2)
    for name in sorted((name for name in agents or () if name), key=len, reverse=True):
        if rest.startswith(name + "-"):
            return name
    return ""


def _numeric_runs(rest: str, *, skip_padded: bool) -> list[list[int]]:
    """#4234 : les suites CONTIGUËS de segments purement numériques.

    Un nom de branche mêle l'identité du lot et son contexte (`am10-16`,
    `suite-1176`, `offload-agents-3`) : ces nombres de contexte ne sont pas
    des lots. Un segment mixte (`am10`, `w5`, `r2`, `pass2`, `t3`) coupe la
    suite et n'apporte aucun numéro ; `skip_padded` écarte en plus les
    groupes complétés d'un zéro (#2182)."""
    runs: list[list[int]] = []
    current: list[int] = []
    for part in rest.split("-"):
        if part.isdigit() and not (
                skip_padded and len(part) > 1 and part.startswith("0")):
            current.append(int(part))
        elif current:
            runs.append(current)
            current = []
    if current:
        runs.append(current)
    return runs


#: #4234 : en dessous de ce numéro, une suite numérique qui n'est pas la
#: première du nom de branche est un repère interne (`am10-16`, `w5`,
#: `offload-agents-3`), pas un lot lié. Les lots d'un numéro inférieur
#: existent (les premiers du registre), mais jamais comme second groupe d'un
#: nom de branche : ils sont toujours écrits en tête (`lot/67-…`).
RELATED_LOT_MIN = 100


def _date_run(run: list[int]) -> bool:
    """#4234 : une suite `2026-10-24` est une date, pas trois lots (une
    branche `review/ep7-3024-preuves-2026-10-24` existe)."""
    return (len(run) == 3 and 1900 <= run[0] <= 2100
            and 1 <= run[1] <= 12 and 1 <= run[2] <= 31)


def cited_lots(branch: str, agent: str = "") -> list[int]:
    """#1694 : les lots qu'un nom de branche cite, dans l'ordre d'essai.

    La PREMIÈRE suite numérique est le lot de la branche (`lot/`, `work/`,
    `agent/`) ; les suites suivantes dont le plus grand numéro atteint
    `RELATED_LOT_MIN` sont des lots liés à vérifier aussi (une branche
    `lot/431-432` sert deux lots). Les petits nombres de contexte
    (`am10-16`, `offload-agents-3`) ne sont plus lus comme des lots : ils
    bloquaient à jamais le retrait de worktrees de lots pourtant livrés
    (#4234, 11 arbres et 5,7 Gio sur ameesh-agents-1).

    Sous `review/`, ce sont les lots relus qui comptent : toutes les suites
    dont un numéro atteint `RELATED_LOT_MIN`, du dernier au premier numéro
    (#1694) ; sans une telle suite, toutes les suites (lots anciens < 100).

    Une suite de trois nombres qui forme une date (`2026-10-24`) est un
    repère, pas des lots ; dans une suite liée, seuls les numéros de lot
    (>= `RELATED_LOT_MIN`) sont vérifiés, jamais le petit numéro d'étiquette
    qui les suit.

    #2182: the number an agent name carries is not a lot. `agent` — the
    branch's agent, see `branch_agent` — is dropped from the groups first;
    without it, a zero-padded group (`review/mty-amh-impl-02-1234`, the
    `-02` of the agent's name) is still not a lot: a lot id is written as
    its decimal (`#2`), never padded. So that review branch cites #1234."""
    found = CITED_LOT.match(branch or "")
    if not found:
        return []
    kind, rest = found.group(1), found.group(2)
    if agent and kind in ("agent", "review") and rest.startswith(agent + "-"):
        rest = rest[len(agent) + 1:]
    # #2182 : un lot s'écrit en décimal, jamais complété d'un zéro — vrai
    # sous tous les préfixes, pas seulement quand le nom d'agent en porte un.
    runs = [run for run in _numeric_runs(rest, skip_padded=True)
            if not _date_run(run)]
    if not runs:
        return []
    related = [run for run in runs if max(run) >= RELATED_LOT_MIN]
    if kind == "review":
        # aucune suite « grande » : un nom de revue d'un lot ancien (numéro
        # < 100) garde toutes ses suites, comme avant #4234
        chosen = related or runs
        return list(reversed([ident for run in chosen for ident in run]))
    # aucune suite grande : mêmes lots anciens, on les garde tous (le nom ne
    # contient que des numéros de lot, pas de repère interne). Sinon la
    # première suite est le lot, et les suivantes ne gardent que leurs numéros
    # de lot (un `4` de contexte ne devient pas un lot).
    if related:
        chosen = [runs[0]] + [[ident for ident in run if ident >= RELATED_LOT_MIN]
                              for run in related if run is not runs[0]]
    else:
        chosen = runs
    return [ident for run in chosen for ident in run]


def cited_lot(branch: str, agent: str = "") -> int | None:
    """#1665 : l'id de lot cité par un nom de branche, ou None — le premier
    candidat de `cited_lots`, pour les appelants qui n'en veulent qu'un."""
    lots = cited_lots(branch, agent=agent)
    return lots[0] if lots else None


def named_lot(name: str, agent: str) -> int | None:
    """#1665 : l'id de lot du dossier `<agent>-<id>[-…]` (convention des
    worktrees de lot), ou None."""
    if not name or not agent or not name.startswith(agent + "-"):
        return None
    found = re.match(r"^(\d+)(?:-|$)", name[len(agent) + 1:])
    return int(found.group(1)) if found else None


def worktree_named_lots(path: str, agents=()) -> tuple[str, list[int]]:
    """#3266 : (agent que nomme le dossier, lots qu'il nomme), pour un
    worktree découvert ou déjà suivi.

    La convention stricte `<agent>-<id>` (#1665) vient d'abord ; les suivantes
    sont celles que le ménage des clones connaît déjà (#2743) : préfixe de
    projet (`nexlink-<agent>-<id>`), nom de grattage de l'agent (`.wt<id>`,
    `<agent>.wt<id>`, `.work-<id>`), groupes numériques d'un dossier de lot.
    L'agent nommé est le plus long des noms du registre présents comme jeton
    (`<agent>-`, `-<agent>-`, `-<agent>` ou `<agent>.`) : un dossier qui nomme
    un AUTRE agent ne prouve rien pour celui qui le suit. Le lot est le
    dernier groupe numérique (`.wt1349-r1` = #1349, pas #1), comme sous
    `agent/`/`review/` (#1694).

    Rien n'est attribué ici : `lot_of_worktree` vérifie que le lot est bien
    assigné à l'agent. Aucun agent nommé rend `("", …)` ; un nom inconnu rend
    `("", [])` — il reste non attribué. Un nom sans agent et sans convention
    de worktree (`wt`, `work`, `lot`, `review`, `prep`, `tmp`, `scratch`,
    `agent`, `paquet`) n'est pas lu : une date (`release-2026-10-23`) n'est
    pas une suite de lots.

    La convention stricte `<agent>-<id>` garde sa priorité : `a1-431-22`
    nomme #431, le `-22` est un suffixe, jamais un second lot (#3266,
    relecture). Les autres conventions rendent tous leurs groupes, du
    dernier au premier, pour que l'appelant les vérifie tous."""
    name = os.path.basename(os.path.normpath(path))
    owner = ""
    for candidate in sorted((n for n in agents or () if n), key=len, reverse=True):
        # Un nom qui se termine par `-<agent>` n'est pas une preuve : un
        # dossier de relecture `relecture-a2` nommerait a2 sans le prouver
        # (#3266, suite L73b). Le préfixe de projet est couvert par le
        # marqueur `-<agent>-` ; le dossier de travail d'un agent est protégé.
        if (name == candidate or name.startswith(candidate + "-")
                or name.startswith(candidate + ".")
                or ("-" + candidate + "-") in name
                or ("-" + candidate + ".") in name):
            owner = candidate
            break
    if not owner and not WORKTREE_NAME.search(name):
        return "", []
    if owner:
        strict = named_lot(name, owner)
        if strict is not None:
            return owner, [strict]
    from . import housekeeping_clones
    return owner, list(reversed(housekeeping_clones.named_lots(name, agents)))


def lot_of_worktree(db, agent: str, path: str, branch: str = "",
                    branch_owner: str = "", agents=()) -> str | None:
    """#1665 : le lot qu'un worktree SERT, quand c'est prouvable : sa branche
    ou, à défaut, son dossier nomment l'id, et ce lot est assigné à CET agent.
    Sinon None : le worktree reste non attribué (jamais retiré à ce titre, il
    suivra la règle des orphelins). Un lot d'un autre agent, inconnu, ou un
    registre illisible ne prouve rien : None.

    #1694: the branch candidates are tried in `cited_lots` order and the first
    lot owned by the agent wins; the folder name is only read when the branch
    cites no number (the #1665 rule). #2182: `branch_owner` — the agent named
    at the head of an `agent/`/`review/` branch — is not a lot candidate
    (else `review/reviewer-02-459` could wrongly attach to lot #2).

    #3266: the folder is read with the shared conventions of
    `worktree_named_lots` (`nexlink-<agent>-<lot>`, `.wt<lot>`), not only
    `<agent>-<lot>`; a folder naming ANOTHER agent of `agents` proves nothing
    for this one (the caller keeps it as a foreign copy). When the folder
    names a lot the branch does not cite, the two must agree on exactly one
    lot: a disagreement (`nexlink-a2-431` with the current branch `lot/432`)
    or several named lots is a doubt, None here — the caller verifies every
    candidate of the branch AND the folder before a removal (`wt-432-433`
    must not follow #433 alone, #432 being open)."""
    if not agent:
        return None
    idents = cited_lots(branch, agent=branch_owner or agent)
    owner, folder_idents = worktree_named_lots(path, agents or (agent,))
    if owner and owner != agent:
        return None
    # Le dossier n'ajoute un candidat que s'il nomme un lot que la branche ne
    # cite pas : là seulement, branche et dossier doivent s'accorder sur un
    # seul lot. Un dossier qui ne fait que répéter une branche multi-lots
    # garde l'ordre #1694 (premier lot assigné à l'agent).
    if any(ident not in idents for ident in folder_idents):
        idents = list(dict.fromkeys(idents + folder_idents))
        if len(idents) != 1:
            return None
    if not idents:
        return None
    for ident in idents:
        try:
            row = storage.of(db).work.get(ident)
        except Exception:
            return None
        if row and row.get("assignee") == agent:
            return str(ident)
    return None


def worktree_holders(path: str, allow=()) -> tuple[bool, str]:
    """(tenu, détail) d'un worktree : un détenteur CONSTATÉ, ou le doute qui
    nomme les processus illisibles (#3396).

    Lu par la couche plateforme (L63). Si l'OS ne permet pas de le savoir, la
    réponse est OUI (fail-closed) : le worktree attend plutôt que d'être retiré
    sous les pieds d'un agent ; le détail le dit, au lieu du « a process is
    working in it » qui faisait passer un doute pour une observation. `allow`
    (L230) : noms des processus dont l'illisibilité n'est pas un doute — le
    choix de l'hôte, vide par défaut ; un détenteur CONSTATÉ reste rapporté."""
    try:
        found = platform.holders(_real(path), allow=allow)
    except platform.NotAvailable as exc:
        names = getattr(exc, "holders", None)
        if names:
            return True, "%s: %s; %s" % (HOLDERS_UNREADABLE, ", ".join(names),
                                         UNREADABLE_OK_HINT)
        return True, "%s (%s)" % (HOLDERS_UNREADABLE,
                                  getattr(exc, "reason", str(exc)))
    if not found:
        return False, ""
    return True, "processes inside (%s)" % ", ".join(
        "%s %s" % (holder.get("pid"), holder_name(holder)) for holder in found[:3])


def worktree_in_use(path: str, allow=()) -> bool:
    """Un processus de l'hôte travaille-t-il dans ce worktree ? Fail-closed :
    un doute répond OUI. `worktree_holders` dit lequel des deux (#3396)."""
    return worktree_holders(path, allow=allow)[0]


def _blocked_by_holders(blocked: dict, detail: str) -> None:
    """#3396 : un doute sur les processus retient un worktree d'un lot FINI.

    `blocked` compte ces worktrees et garde le premier détail — celui qui
    nomme les processus illisibles et la clé du canon — pour le relevé de
    l'hôte (`housekeeping_blocked`). Un détenteur constaté n'est pas un doute
    et ne compte pas."""
    if not detail.startswith(HOLDERS_UNREADABLE):
        return
    blocked["count"] = int(blocked.get("count") or 0) + 1
    blocked.setdefault("detail", detail)


def current_branch(path: str, budget: PassBudget | None = None) -> tuple[bool, str]:
    """(lisible, branche) de HEAD du worktree, "" s'il est détaché.

    Un Git indisponible, ou un chemin qui n'est pas un worktree, est un
    doute : False, l'appelant garde le worktree au lieu de l'attribuer sur la
    branche enregistrée, qui peut être périmée (#3266, relecture cx3)."""
    proc = _git(["symbolic-ref", "-q", "--short", "HEAD"], path, budget=budget)
    if proc is None or proc.returncode not in (0, 1):
        return False, ""
    return True, proc.stdout.strip() if proc.returncode == 0 else ""


def remove_worktree(path: str, repo: str,
                    budget: PassBudget | None = None) -> tuple[bool, str]:
    """`git worktree remove` (jamais `--force`), lancé depuis le dépôt commun.

    #3802 N3 : un budget plus court que `GIT_TIMEOUT` ne lance pas cette
    commande destructive — la tuer en plein milieu laisserait un retrait
    partiel. Elle attend le passage suivant, qui aura le temps."""
    if budget is not None and budget.remaining() < GIT_TIMEOUT:
        return False, "pass budget too short for git worktree remove; kept for the next pass"
    base = repo if repo and os.path.isdir(repo) else path
    proc = _git(["worktree", "remove", path], base, budget=budget)
    if proc is None:
        return False, "git unavailable"
    if proc.returncode != 0:
        return False, (proc.stderr or proc.stdout).strip()[:300] or "refused by git"
    return True, ""


def delete_merged_branch(branch: str, repo: str) -> str:
    """L230: `git branch -d` (never `-D`: git itself refuses a branch not
    merged) once its worktree is removed; what happened, for the journal."""
    if not branch or not repo or not os.path.isdir(repo):
        return ""
    proc = _git(["branch", "-d", "--", branch], repo)
    if proc is None:
        return "branch %s kept: git unavailable" % branch
    if proc.returncode != 0:
        return "branch %s kept: %s" % (
            branch, ((proc.stderr or proc.stdout).strip().splitlines() or ["refused by git"])[0])
    return "branch %s deleted (merged)" % branch


def prune_worktrees(repo: str, budget: PassBudget | None = None) -> None:
    if repo and os.path.isdir(repo):
        _git(["worktree", "prune"], repo, budget=budget)


def _other_agent_cwds(db, agent: str):
    """Dossiers de travail des AUTRES agents du registre ; False si le
    registre est illisible (aucun worktree n'est alors attribué)."""
    try:
        return [row["cwd"] for row in storage.of(db).operations.listing()
                if row.get("name") != agent and row.get("cwd")]
    except Exception:
        return False


# --------------------------------------------------------------------------
# un tour
# --------------------------------------------------------------------------

@dataclass
class Turn:
    """Ménage autour d'un tour : préparé avant le lancement, conclu après.

    `begin` (avant le lancement) : vide le dossier temporaire de l'agent si
    la session est neuve, rend l'environnement à poser, relève les worktrees
    du dépôt et les noms de /tmp. `end` (après le tour, même en échec) :
    quota du dossier temporaire, enregistrement des worktrees apparus,
    signalement des entrées de /tmp apparues ; rend les lignes du journal."""

    cfg: object
    policy: Policy
    agent: str
    cwd: str | None
    env: dict = field(default_factory=dict)
    entries: list = field(default_factory=list)
    worktrees_before: set = field(default_factory=set)
    tmp_before: set | None = None
    started: float = 0.0
    #: `AMEESH_HOUSEKEEPING=off` : rien n'est fait autour du tour
    off: bool = False
    #: L219: the temporary directory, locked (`TmpHold`) until `end`
    lock: object = None
    tmp: str = ""

    @classmethod
    def begin(cls, cfg, policy: Policy, agent: str, cwd: str | None, *,
              new_session: bool, base_env: dict) -> "Turn":
        turn = cls(cfg=cfg, policy=policy, agent=agent, cwd=cwd, started=time.time())
        if disabled(base_env):
            turn.off = True
            return turn
        if policy.redirect:
            # L219: locked (shared) until `end`, so that no pass deletes or
            # evicts it meanwhile
            hold = hold_tmp(cfg, policy, agent, new_session=new_session)
            turn.lock, turn.tmp = hold, hold.path
            turn.entries.extend(hold.entries)
            try:
                turn.env = turn_env(cfg, policy, agent, base_env, hold=hold)
            except Exception:
                hold.release()
                raise
        if policy.worktrees:
            turn.worktrees_before = {w["path"] for w in list_worktrees(cwd)}
        if policy.deps and cwd and os.path.isdir(cwd):
            # #248: packages of the agent's worktree without node_modules get
            # the shared tree when it is already in the cache (never installs)
            from . import deps as deps_mod
            try:
                for done in deps_mod.link_ready(cwd, cache_root(cfg, policy)):
                    turn.entries.append({
                        "kind": "deps", "action": done["status"], "path": done["package"],
                        "agent": agent, "detail": ("node_modules -> %s" % done["target"]
                            if done["status"] == "linked" else
                            done.get("reason") or done.get("error"))})
            except Exception:
                pass                       # never blocks a turn
        turn.tmp_before = tmp_names()
        return turn

    def end(self, *, lot: str | None, turn_id: str, db=None, host: str = "") -> list[dict]:
        if self.off:
            return self.entries
        policy = self.policy
        try:
            if self.env and self.tmp and self.lock is not None and self.lock.exclusive_now():
                # L219: under the exclusive lock, or no eviction (another
                # runner holding it shared may be using the directory)
                root = tmp_root(self.cfg, policy)
                result = evict_lru([self.tmp], root, policy.tmp_quota,
                                   min_age=EVICT_MIN_AGE_S)
                episode = tmp_episode(db, host, self.tmp, result,
                                      now=time.time())
                self.entries.extend(eviction_entries(
                    result, kind="tmp", path=self.tmp, quota=policy.tmp_quota,
                    agent=self.agent, lot=lot, since=episode["since"],
                    resolved=episode["resolved"]))
        finally:
            if self.lock is not None:      # L219: the turn is over
                self.lock.release()
                self.lock = None
        if policy.worktrees and self.cwd and db is not None:
            known_cwd = os.path.abspath(self.cwd)
            repo = ""
            others = None  # dossiers des autres agents, lus au premier besoin
            for wt in list_worktrees(self.cwd):
                if wt["path"] in self.worktrees_before or wt["path"] == known_cwd \
                        or wt.get("bare"):
                    continue
                if others is None:
                    others = _other_agent_cwds(db, self.agent)
                # L73b : `git worktree list` couvre tout le dépôt ; un worktree
                # d'un autre agent apparu pendant ce tour n'est pas à nous.
                mine = others is not False and owns_worktree(wt["path"], self.cwd, others)
                branch = wt.get("branch") or ""
                served = None
                if not mine and others is not False:
                    # #1665 : hors du dossier de l'agent, la branche (ou le
                    # dossier) nomme le lot, et ce lot lui est assigné : c'est
                    # le sien, il suivra la fin de ce lot. Un worktree sous le
                    # dossier d'un AUTRE agent reste au sien (L73b).
                    ailleurs = any(other and _under(_real(wt["path"]), _real(other))
                                   for other in others)
                    if not ailleurs:
                        served = lot_of_worktree(db, self.agent, wt["path"], branch)
                        mine = served is not None
                repo = repo or common_dir(self.cwd)
                hk = storage.of(db).housekeeping
                row = hk.register_worktree(
                    host=host, path=wt["path"], repo=repo, agent=self.agent,
                    lot=(served or lot) if mine else None, turn_id=turn_id,
                    branch=branch, head=wt.get("head") or "")
                if row is None:
                    continue
                if mine:
                    detail = ("appeared during turn %s (%s); removed at the end of the lot if "
                              "clean and integrated into the main branch"
                              % (turn_id[:8], branch or "detached"))
                else:
                    detail = ("%s: appeared during turn %s (%s) outside the agent's "
                              "directory; removed once orphaned (clean, pushed, idle for "
                              "24 h, no agent using it)"
                              % (UNATTRIBUTED, turn_id[:8], branch or "detached"))
                    hk.set_worktree_status(row["id"], "active", detail)
                self.entries.append({
                    "kind": "worktree", "action": "registered", "path": wt["path"],
                    "agent": self.agent, "lot": (served or lot) if mine else None,
                    "detail": detail})
        exclude = tuple(v for v in (self.env.get("TMPDIR"), self.env.get("AMEESH_CACHE_DIR"))
                        if v)
        found_all = new_tmp_entries(self.tmp_before, min_size=policy.orphan_min_size,
                                    exclude=exclude)
        signaled = self._signaled(db, host) if found_all else set()
        for found in found_all:
            if (found["path"], str(found["ino"]), str(found["dev"])) in signaled:
                continue  # #222: already signaled by another turn, its fate stays
            # #222: /tmp is shared by the whole host (other agents, other meshes
            # whose state directory is elsewhere, processes outside any turn):
            # nothing proves that this turn created the entry, so it is never
            # charged to it. Unattributed, it is never deleted (`_orphan_verdict`
            # keeps an entry without agent); the `tmp_orphan` alert follows it,
            # with its size and the command, for as long as it exists.
            self.entries.append({
                "kind": "orphan", "action": "signaled", "path": found["path"],
                "bytes": found["bytes"], "agent": None, "lot": None,
                "detail": "%s: appeared in /tmp during turn %s of %s; /tmp is shared by "
                          "the host (other turns, other meshes, processes outside turns): "
                          "never deleted automatically" % (UNATTRIBUTED, turn_id[:8],
                                                                 self.agent),
                "data": {"first_seen": time.time(),
                         "command": found["command"], "turn": turn_id, "witness": self.agent,
                         "lot": lot, "ino": found["ino"], "dev": found["dev"]}})
        return self.entries

    def _signaled(self, db, host: str) -> set:
        """#222: (path, inode, device) of the /tmp entries whose last line is
        a signal (sticky rule: a later turn does not take it again). Without
        database, or if the log cannot be read: none."""
        if db is None or not host:
            return set()
        try:
            rows = storage.of(db).housekeeping.latest_by_path(host, "orphan")
        except Exception:
            return set()
        return {(r.get("path"), str((r.get("data") or {}).get("ino")),
                 str((r.get("data") or {}).get("dev")))
                for r in rows if r.get("action") == "signaled"}


# --------------------------------------------------------------------------
# un passage (exécuteur périodique, ou `ameesh menage`)
# --------------------------------------------------------------------------

def measure(cfg, policy: Policy, budget: PassBudget | None = None) -> dict:
    """Occupation des dossiers gérés et du /tmp du système (lecture seule).

    L162 : dès que le /tmp en tmpfs atteint l'alerte (ou le seuil de pression
    par défaut, s'il est plus bas), ses `TMP_TOP` plus grosses entrées
    (`system_tmp_top`), que les alertes reprennent."""
    troot, croot = tmp_root(cfg, policy), cache_root(cfg, policy)
    agents: dict = {}
    if os.path.isdir(troot):
        for name in sorted(os.listdir(troot)):
            if budget is not None and budget.expired():
                budget.timed_out = True
                break                  # #3802: a bounded host pass
            full = os.path.join(troot, name)
            if name != MARKER and os.path.isdir(full) and not os.path.islink(full):
                agents[name] = size_of(full, budget=budget)
    caches: dict = {}
    from . import deps as deps_mod
    # Shared installs occupy the cache even when linking is disabled.
    for name in [n for n, _vars in cache_names(policy)] + [deps_mod.NM_DIR]:
        if budget is not None and budget.expired():
            budget.timed_out = True
            break                      # #3802: a bounded host pass
        full = os.path.join(croot, name)
        if os.path.isdir(full):
            caches[name] = size_of(full, budget=budget)
    system = resources_mod.fs_usage(resources_mod.system_tmp())
    fraction = resources_mod.tmpfs_fraction(system)
    seuil = min(policy.tmpfs_alert, resources_mod.DEFAULT_MAX_TMPFS_USED)
    return {
        "tmp_root": troot, "tmp_bytes": sum(agents.values()), "tmp_by_agent": agents,
        "tmp_quota": policy.tmp_quota,
        "tmp_root_fstype": (resources_mod.fs_usage(troot).get("tmp_fstype")
                            if os.path.isdir(troot) else None),
        "cache_root": croot, "cache_bytes": sum(caches.values()), "cache_by_name": caches,
        "cache_quota": policy.cache_quota,
        "system_tmp": system,
        "system_tmp_fraction": fraction,
        "system_tmp_top": (tmp_top(budget=budget)
                           if fraction is not None and fraction >= seuil else []),
        "tmpfs_alert": policy.tmpfs_alert,
    }


def _lot_state(db, lot) -> str | None:
    try:
        ident = int(str(lot).lstrip("#"))
    except (TypeError, ValueError):
        return None
    row = storage.of(db).work.get(ident)
    return row.get("state") if row else None


def _mtime(path: str) -> float | None:
    try:
        return os.stat(path).st_mtime
    except OSError:
        return None


def _lots_ended_ts(db, idents) -> float | None:
    """#3728 : l'instant où le dernier des lots `idents` a fini (`closed_ts`,
    sinon `updated_ts`, plus tard donc prudent) ; None si l'un est illisible."""
    moments = []
    for ident in idents:
        try:
            row = storage.of(db).work.get(int(str(ident).lstrip("#")))
        except Exception:  # registre illisible : rien n'est prouvé
            return None
        moment = (row or {}).get("closed_ts") or (row or {}).get("updated_ts")
        if moment is None:
            return None
        moments.append(float(moment))
    return max(moments) if moments else None


def _archive_due(db, idents, archive: dict | None, *, now: float, youngest: float) -> bool:
    """#3728 : la politique d'archivage est déclarée, TOUS les lots `idents`
    ont fini depuis plus de `after` et le worktree est inactif depuis autant."""
    if not archive:
        return False
    after = float(archive["after"])
    ended_ts = _lots_ended_ts(db, idents)
    return ended_ts is not None and now - ended_ts >= after and now - youngest >= after


def _archive_dirty_worktree(path: str, repo: str, base: dict, *, why: str, st: dict, state,
                            archive: dict, now: float, dry_run: bool, artifacts, allow,
                            lock_path, journal_path, skip_roots,
                            held: str = "modified or untracked files",
                            budget: PassBudget | None = None,
                            reserve_s: float = 0.0) -> dict:
    """#3728 : archive puis retrait d'un worktree gardé pour des fichiers
    modifiés ou non suivis (#3734 : ou seulement ignorés ; #4442 : ou des
    commits absents de la forge, alors aussi dans `HEAD.bundle`, `held`).
    `plan` (action `archivage` en essai, `retrait`, `garde` ou `attend`),
    `entries` du journal, `removed`."""
    from . import housekeeping_archive
    after = float(archive["after"])
    why = "%s; kept for %s, ended and idle for more than %s" % (why, held, fmt_idle(after))
    # #3802 B1 (cx2) : l'archivage et le retrait ne commencent pas dans la
    # réserve gardée aux phases suivantes.
    if budget is not None and budget.reserved(reserve_s):
        return {"plan": dict(base, action="attend",
                             detail="%s; pass budget reached: archive and removal "
                                    "left for the next pass" % why),
                "entries": [], "removed": False}
    if dry_run:
        size, _whole = build_artifacts.size_of(
            path, budget=_phase_budget(budget, reserve_s))
        return {"plan": dict(base, action="archivage", bytes=size, detail=why),
                "entries": [], "removed": False}
    made: dict = {}

    def keep_archive() -> str:
        try:
            made.update(housekeeping_archive.archive(
                path, archive["root"], rules=build_artifacts.rules_of(artifacts),
                lot=base.get("lot"), state=state or "", branch=st.get("branch") or "",
                head=st.get("head") or "", repo=repo or "", now=now,
                retention_s=float(archive["retention"]),
                deadline=(None if budget is None
                          else budget.deadline - max(0.0, reserve_s))))
        except (housekeeping_archive.ArchiveRefused, OSError) as exc:
            return "not archived: %s" % exc
        return ""

    outcome, freed, note, built = _remove_kept_worktree(
        path, repo or common_dir(path), st["gitdir"], artifacts=artifacts,
        allow=allow, lock_path=lock_path, journal_path=journal_path, skip_roots=skip_roots,
        idle_before=now - after, idle_note="worktree active within %s" % fmt_idle(after),
        archive=keep_archive, budget=budget, reserve_s=reserve_s)
    entries = [dict(base, kind="artifact", detail="%s; %s" % (why, b.pop("detail")), **b)
               for b in built]
    if outcome in ("garde", "attend"):
        if made:
            # removal refused after the archive: the archive goes, the
            # worktree stays as it was (nothing half done)
            shutil.rmtree(made["archive"], ignore_errors=True)
        return {"plan": dict(base, action=outcome, detail="%s; %s" % (why, note)),
                "entries": entries, "removed": False}
    detail = "%s: archived to %s (%d tracked change(s), %d untracked file(s), " \
             "%d unpushed commit(s), kept until %s), deleted (logged trash), " \
             "git worktree prune%s" % (
                 why, made.get("archive"), made.get("tracked", 0), made.get("files", 0),
                 made.get("commits", 0),
                 time.strftime("%Y-%m-%d", time.gmtime(made.get("retain_until_ts") or now)),
                 "; %s" % note if note else "")
    entries.append(dict(base, action="archived", bytes=freed, detail=detail, data={
        "archive": made.get("archive"), "archive_bytes": made.get("bytes"),
        "tracked": made.get("tracked"), "files": made.get("files"),
        "commits": made.get("commits"), "bundle_bytes": made.get("bundle_bytes"),
        "retain_until_ts": made.get("retain_until_ts"), "outcome": outcome}))
    return {"plan": dict(base, action="retrait", bytes=freed, detail=detail),
            "entries": entries, "removed": True}


def _worktree_pass(db, host: str, *, dry_run: bool,
                   protected: set[str], now: float,
                   running: set | None = None, artifacts=None,
                   allow=(), lock_path: str | None = None, journal_path: str | None = None,
                   skip_roots=(), known_agents=(),
                   blocked: dict | None = None,
                   archive: dict | None = None,
                   budget: PassBudget | None = None,
                   reserve_s: float = 0.0,
                   cursor=None, on_cursor=None) -> tuple[list[dict], list[dict]]:
    """Fin de lot des worktrees suivis : (lignes du journal, plan lisible).

    Garde-fous (L73b) avant tout retrait : worktree attribué à l'agent du
    tour (ou à un lot que sa branche ou son dossier nomme et qui lui est
    assigné, #1665), pas le dossier de travail d'un agent ni un dossier qui
    en contient un, agent propriétaire sans tour en cours (`running`), au
    moins `WORKTREE_MIN_AGE_S` depuis son enregistrement et depuis la
    dernière modification de son dossier, non verrouillé, aucun processus
    dedans, propre, et HEAD INTÉGRÉ à la branche principale distante (poussé
    ne suffit pas).

    L230: a worktree kept although its lot is merged (`LOT_MERGED`, state
    of the lot in ameesh) is removed once kept for more than a day for the
    same reason, if clean, pushed, without ignored files other than build
    output (`artifacts` rules), and with no process of the user inside by
    the strict check of the artifacts (`platform.user_paths`, `allow`);
    how: `_remove_kept_worktree` (host lock `lock_path`, journal
    `journal_path`, managed roots `skip_roots`).

    #3266 : l'attribution persistée est une trace, pas la branche courante :
    avant tout retrait, la branche ACTUELLE est relue et tous les lots
    qu'elle nomme, plus le lot persisté et ceux du dossier, doivent avoir
    fini (un lot ouvert, inconnu, ou une branche illisible garde le
    worktree).

    #1665 : un worktree orphelin — aucun lot nommé par sa branche ou son
    dossier — suit la même fin, mais après `ORPHAN_WORKTREE_IDLE_S` (24 h)
    d'inactivité, propre et poussé (la branche reste) ; c'est le ménage que
    l'owner a demandé le 15/10 14:50.

    `blocked` (#3396) : le dict que le passage remplit quand un doute sur les
    processus retient le retrait de worktrees de lots FINIS — `count` et le
    premier `detail` (qui nomme les processus et la clé du canon) ; le relevé
    d'hôte en fait l'alerte `housekeeping_blocked` après six heures.

    `archive` (#3728, #4442) : `after`, `retention` (secondes, de la fiche
    Host), `root`. Un worktree gardé pour des fichiers modifiés ou non suivis
    OU pour des commits absents de la forge, dont TOUS les lots nommés ont
    fini depuis plus de `after` et sans activité depuis autant, ni verrou git,
    ni sous-module, ni processus, est archivé (`housekeeping_archive` : patchs
    et `HEAD.bundle` des commits non poussés) puis retiré — copies étrangères
    comprises. Absent : il reste gardé.

    #3802 (B1) : `reserve_s` garde aux phases suivantes cette part du budget ;
    la boucle s'arrête quand il ne reste que cela. `cursor` est le dernier
    `(created_ts, id)` examiné au passage précédent : la boucle reprend
    APRÈS lui (et repart au début après la fin), puis `on_cursor` reçoit le
    nouveau curseur — `None` quand tous les worktrees ont été examinés. Un
    hôte dont la phase worktrees dépasse le budget finit donc par tous les
    voir, au lieu de refaire les mêmes premiers à chaque passage."""
    entries: list[dict] = []
    plan: list[dict] = []
    blocked = blocked if blocked is not None else {}
    running = set(running or ())
    store = storage.of(db).housekeeping
    # #2174: every tracked worktree — with the 500 oldest stuck, a capped
    # read never reached the newer ones (17/10: 822 on agents-1, 789 on
    # agents-2, 1 309 on the workstation)
    rows = store.worktrees(host, ("active", "kept"), limit=None)
    # #3802 (B1) : reprendre après le curseur du passage précédent, puis
    # boucler au début : un worktree ajouté avant le curseur n'est pas oublié.
    start = 0
    if cursor is not None and rows:
        try:
            after = (float(cursor[0]), int(cursor[1]))
        except (TypeError, ValueError, IndexError):
            after = None
        if after is not None:
            for index, row in enumerate(rows):
                try:
                    here = (float(row.get("created_ts") or 0), int(row.get("id")))
                except (TypeError, ValueError):
                    continue
                if here > after:
                    start = index
                    break
    ordered = rows[start:] + rows[:start]
    processed = 0
    last = None
    # view of the budget that ends at the reserve: the git calls of one row
    # cannot eat the time kept for the following phases (#3802 B1, cx2)
    window = budget.window(reserve_s) if budget is not None else None
    for row in ordered:
        if budget is not None and (budget.expired()
                                   or budget.reserved(reserve_s)):
            # #3802 (B1) : le budget du passage s'arrête ici ; le curseur dit
            # où reprendre au prochain passage, et la réserve garde de quoi
            # faire tourner les phases suivantes.
            plan.append({"kind": "worktree", "path": "", "agent": None, "lot": None,
                         "action": "attend",
                         "detail": "pass budget reached: remaining worktrees left "
                                   "for the next pass"})
            break
        processed += 1
        last = row
        path, lot = row["path"], row.get("lot")
        branch = row.get("branch") or ""
        branch_checked = False
        # #2182: the agent at the head of an `agent/`/`review/` branch is
        # known from the registry (else from the recorded agent), so its own
        # number is not read as a cited lot (`review/reviewer-02-1234`).
        branch_owner = branch_agent(branch, known_agents) or (row.get("agent") or "")
        base = {"kind": "worktree", "path": path, "agent": row["agent"], "lot": lot}
        if not os.path.isdir(path):
            plan.append(dict(base, action="disparu", detail=UNATTRIBUTED))
            if not dry_run:
                prune_worktrees(row.get("repo") or "", budget=window)
                store.set_worktree_status(row["id"], "gone",
                                          "directory gone (%s)" % UNATTRIBUTED)
                entries.append(dict(base, action="gone", detail="directory gone; "
                                    "git worktree prune (%s)" % UNATTRIBUTED))
            continue
        # #1665/#3266 : tout worktree suivi sans lot est relu — pas seulement
        # ceux marqués « non attribué » : la branche a pu citer le lot après
        # l'enregistrement, ou le dossier le nommer par une convention que le
        # registre d'alors ne lisait pas (`nexlink-<agent>-<lot>`, `.wt<lot>`,
        # #2743). L'attribution est PERSISTÉE pour que le registre dise ce que
        # le passage a prouvé ; la politique de fin de lot, elle, ne change
        # pas (aucune règle de suppression nouvelle, #3266).
        if lot is None:
            # Un processus dans le worktree : inutile de lire Git, on attend
            # (le test #2167 exige qu'aucune lecture Git ne soit faite).
            in_use, holder_detail = worktree_holders(path, allow=allow)
            if in_use:
                plan.append(dict(base, action="attend", detail=holder_detail))
                continue
            # La branche enregistrée est une trace, pas la vérité : un
            # worktree change de branche après la découverte (`topic` puis
            # `lot/432`). Relire la branche ACTUELLE avant toute attribution ;
            # illisible = doute, on garde (#3266, relecture cx3).
            readable, branch = current_branch(path, budget=window)
            branch_checked = True
            if not readable:
                plan.append(dict(base, action="garde",
                                 detail="current branch unreadable; kept"))
                continue
            branch_owner = branch_agent(branch, known_agents) or (row.get("agent") or "")
            # Le dossier peut nommer un AUTRE agent que celui qui a vu
            # apparaître le worktree (`work/<agent>-<lot>` d'un voisin, le
            # dépôt commun étant partagé) : l'agent nommé fait foi et reste,
            # lot prouvé ou non (relecture #3266) ; un lot assigné ailleurs
            # laisse lot=None et suit la garde des copies étrangères.
            owner, _candidates = worktree_named_lots(
                path, known_agents or (row.get("agent") or "",))
            owner_detail = ""
            if owner and owner != (row.get("agent") or ""):
                row = dict(row, agent=owner)
                owner_detail = OWNER_DETAIL % owner
            lot = lot_of_worktree(db, row.get("agent") or "", path, branch,
                                  branch_owner=branch_owner, agents=known_agents)
            if lot is not None:
                detail = ATTRIBUTED_DETAIL % (lot, row.get("agent") or "?")
                if not dry_run:
                    store.set_worktree_attribution(row["id"], row.get("agent") or "",
                                                   lot, detail)
                    entries.append({"kind": "worktree", "action": "attributed",
                                    "path": path, "agent": row.get("agent"), "lot": lot,
                                    "detail": detail})
            elif owner_detail and not dry_run:
                store.set_worktree_attribution(row["id"], row.get("agent") or "",
                                               None, owner_detail)
                entries.append({"kind": "worktree", "action": "attributed",
                                "path": path, "agent": row.get("agent"), "lot": None,
                                "detail": owner_detail})
            base = {"kind": "worktree", "path": path, "agent": row["agent"], "lot": lot}
        # #3266 : TOUS les lots que la branche et le dossier nomment, y
        # compris inconnus, sont vérifiés avant le moindre retrait : un lot
        # fini ne prouve rien des autres encore servis (`wt-432-433` avec #432
        # ouvert, `wt-999999-433` avec #999999 absent). Le ménage des clones
        # applique déjà cette règle (#2743).
        idents = cited_lots(branch, agent=branch_owner)
        _owner, folder_idents = worktree_named_lots(
            path, known_agents or (row.get("agent") or "",))
        idents = list(dict.fromkeys(idents + folder_idents))
        named = cited_lot(branch, agent=branch_owner)
        if named is None:
            named = folder_idents[0] if folder_idents else None
        # #2174: a named lot that cannot be attributed (another agent's: a
        # review copy, a reassigned lot) follows the orphan rule once every
        # named lot has ended — 24 h idle, clean and pushed; an open, unknown
        # or unreadable lot proves nothing and the worktree stays.
        foreign = lot is None and named is not None
        if foreign:
            try:
                states = [_lot_state(db, ident) for ident in idents]
            except Exception:  # registre illisible : rien n'est prouvé
                states = [None]
            if any(state not in LOT_ENDED for state in states):
                plan.append(dict(base, action="garde",
                                 detail="named lot #%s not attributable" % named))
                continue
            state = states[0]
            lot = str(named)
            base["lot"] = lot
        else:
            state = _lot_state(db, lot) if lot else None
        ended = state in LOT_ENDED
        orphan = lot is None
        if not ended and not orphan:
            plan.append(dict(base, action="attend",
                             detail="lot %s" % (state or "unknown")))
            continue
        why = ("lot #%s %s%s" % (lot, state, " (assigned to another agent)" if foreign
                                 else "")) if ended else \
            "no lot named by the branch or the directory (orphan)"
        target = _real(path)
        if any(_under(_real(cwd), target) for cwd in protected):
            plan.append(dict(base, action="garde", detail="an agent's working directory"))
            continue
        if row["agent"] in running:
            plan.append(dict(base, action="attend",
                             detail="%s; turn in progress for %s" % (why, row["agent"])))
            continue
        moments = [float(row.get("created_ts") or now), _mtime(path)]
        youngest = max(m for m in moments if m is not None)
        floor = ORPHAN_WORKTREE_IDLE_S if orphan or foreign else WORKTREE_MIN_AGE_S
        if now - youngest < floor:
            plan.append(dict(base, action="attend",
                             detail="%s; created or modified less than %s ago" % (
                                 why, "24 h" if orphan or foreign else "an hour")))
            continue
        # #2167: holders (including unreadable processes) can retain the
        # whole host's worktrees; do not pay for Git before that decision.
        in_use, holder_detail = worktree_holders(path, allow=allow)
        if in_use:
            if ended:
                _blocked_by_holders(blocked, holder_detail)
            plan.append(dict(base, action="attend",
                             detail="%s; %s" % (why, holder_detail)))
            continue
        # #3266 (relecture cx3) : une attribution persistée n'est qu'une trace,
        # comme `row.branch` : la branche a pu changer depuis (`topic` →
        # `lot/432`). Avant CHAQUE décision de retrait, la branche ACTUELLE est
        # relue si elle ne l'a pas déjà été, et TOUS les lots qu'elle nomme,
        # PLUS le lot persisté et le dossier, sont vérifiés : un lot ouvert,
        # inconnu ou illisible prouve que le worktree sert encore. Lire la
        # branche pour attribuer ne dispense pas de cette vérification
        # (#3266, relecture cx3 : `nexlink-a2-431` sur `lot/431-432` attribue
        # #431, #432 restant en construction). La base (f71414d8), qui
        # n'attribuait jamais, n'avait rien à relire : ce garde protège
        # l'attribution persistée, il ne change pas la règle de fin de lot.
        if not branch_checked:
            readable, branch = current_branch(path, budget=window)
            if not readable:
                plan.append(dict(base, action="garde",
                                 detail="current branch unreadable; kept"))
                continue
            branch_checked = True
            branch_owner = branch_agent(branch, known_agents) or (row.get("agent") or "")
        branch_idents = cited_lots(branch, agent=branch_owner)
        _named_owner, folder_idents = worktree_named_lots(
            path, known_agents or (row.get("agent") or "",))
        idents = list(dict.fromkeys(branch_idents + folder_idents + (
            [str(lot)] if lot is not None else [])))
        try:
            states = [_lot_state(db, ident) for ident in idents]
        except Exception:  # registre illisible : rien n'est prouvé
            states = [None]
        still = [(ident, state) for ident, state in zip(idents, states)
                 if state not in LOT_ENDED]
        if still:
            ident, state = still[0]
            detail = ("current branch names lot #%s %s; kept" % (
                ident, state or "unknown") if ident != str(lot) else
                "persisted lot #%s %s; kept" % (ident, state or "unknown"))
            plan.append(dict(base, action="garde", detail=detail))
            continue
        # #2180: a reused tracked path may now be a clone, a symlink or a
        # worktree of another repository. Old metadata never authorises it.
        from .housekeeping_discovery import _marker
        if _marker(path) != "linked" or not same_repository(
                path, row.get("repo") or "", budget=window):
            plan.append(dict(base, action="garde",
                             detail="linked worktree no longer matches its tracked repository"))
            continue
        st = worktree_state(path, budget=window)
        if not st["exists"]:
            plan.append(dict(base, action="disparu", detail=why))
            if not dry_run:
                prune_worktrees(row.get("repo") or "", budget=window)
                store.set_worktree_status(row["id"], "gone", "directory gone (%s)" % why)
                entries.append(dict(base, action="gone", detail="directory gone; "
                                    "git worktree prune (%s)" % why))
            continue
        if st["locked"]:
            plan.append(dict(base, action="garde", detail="%s; %s" % (why, st["reason"])))
            continue
        in_use, holder_detail = worktree_holders(path, allow=allow)
        if in_use:
            if ended:
                _blocked_by_holders(blocked, holder_detail)
            plan.append(dict(base, action="attend",
                             detail="%s; %s" % (why, holder_detail)))
            continue
        # #4442 : le statut `kept` (et son alerte) porte la raison NORMALISÉE,
        # sans le SHA ni le nombre de fichiers : un détail qui bouge d'un
        # passage à l'autre ne remet plus la grâce de 24 h à zéro, et
        # #2182/#4442 : une copie étrangère non poussée n'est plus écartée du
        # flux — elle est gardée, signalée, puis archivée/retirée comme les
        # autres quand la politique d'archivage le permet.
        detail = kept_detail(why, st)
        # L230: a worktree kept although its lot is merged goes once kept
        # (same reason) for more than a day, if clean and its HEAD is on the
        # remote: nothing is lost; its branch stays (not merged into main).
        # #1665: an orphan (or a lot that ended without integration) follows
        # the same end after ORPHAN_WORKTREE_IDLE_S of inactivity, clean and
        # pushed; the branch stays too.
        # Foreign copies always need 24 h of source/git inactivity, even
        # when the same integration failure has kept them for a day.
        grace = (not foreign and state in LOT_MERGED and row["status"] == "kept"
                 and row.get("detail") == detail
                 and now - float(row.get("checked_ts") or now) >= KEPT_MERGED_GRACE_S)
        abandoned = (ended or orphan) and now - youngest >= ORPHAN_WORKTREE_IDLE_S
        if (grace or abandoned) and st["clean"] and st["pushed"] and not st["submodules"]:
            idle_before = None if grace else now - ORPHAN_WORKTREE_IDLE_S
            if idle_before is not None:
                activity = build_artifacts.survey(path, build_artifacts.rules_of(artifacts),
                    skip_roots=skip_roots,
                    budget=_phase_budget(budget, reserve_s))
                if not activity["complete"] or activity["latest"] > idle_before:
                    plan.append(dict(base, action="garde", detail="%s; %s" % (
                        detail, activity["why"] or "worktree active within 24 h")))
                    continue
            leftovers = ignored_leftovers(path, build_artifacts.rules_of(artifacts),
                                          budget=window)
            if leftovers is None:
                hold = "ignored files unreadable (git status --ignored)"
            elif leftovers:
                hold = "%d ignored file(s) outside build artifacts (%s)" % (
                    len(leftovers), leftovers[0])
            else:
                procs = platform.user_paths(allow=allow)
                held = procs.holders(path)
                doubt = procs.doubt()
                if doubt:
                    hold = "%s (%s)" % (HOLDERS_UNREADABLE, doubt)
                else:
                    hold = ("processes inside (%s)" % ", ".join(held[:3])
                            if held else "")
            if leftovers and ended and st["head"] \
                    and _archive_due(db, idents, archive, now=now, youngest=youngest):
                # #3734 (dev3 N2) : gardé pour des fichiers ignorés seulement
                # (un `.env`, un `__pycache__`), lot fini depuis plus de
                # `after` : archivé puis retiré comme un worktree modifié,
                # mêmes gardes (en cas de doute, gardé)
                done = _archive_dirty_worktree(
                    path, row.get("repo") or "", base, why=why, st=st, state=state,
                    archive=archive, now=now, dry_run=dry_run, artifacts=artifacts,
                    allow=allow, lock_path=lock_path, journal_path=journal_path,
                    skip_roots=skip_roots, held="ignored files only",
                    budget=budget, reserve_s=reserve_s)
                entries += done["entries"]
                plan.append(done["plan"])
                if done["removed"]:
                    store.set_worktree_status(row["id"], "removed", done["plan"]["detail"])
                continue
            if hold:
                if ended:
                    _blocked_by_holders(blocked, hold)
                plan.append(dict(base, action="garde", detail="%s; %s" % (detail, hold)))
                continue
            why = "%s; %s, clean and pushed" % (
                why, "kept for more than 24 h" if grace else "idle for more than 24 h")
            if dry_run:
                size, _whole = build_artifacts.size_of(
                    path, budget=_phase_budget(budget, reserve_s))
                plan.append(dict(base, action="retrait", bytes=size, detail=why))
                continue
            outcome, freed, note, built = _remove_kept_worktree(
                path, row.get("repo") or common_dir(path, budget=window), st["gitdir"],
                artifacts=artifacts,
                allow=allow,
                lock_path=lock_path, journal_path=journal_path, skip_roots=skip_roots,
                idle_before=idle_before, budget=budget, reserve_s=reserve_s)
            entries += [dict(base, kind="artifact", detail="%s; %s" % (why, b.pop("detail")),
                             **b) for b in built]
            if outcome in ("garde", "attend"):
                plan.append(dict(base, action=outcome, detail="%s; %s" % (detail, note)))
                continue
            plan.append(dict(base, action="retrait", bytes=freed, detail=why))
            store.set_worktree_status(row["id"], "removed", why)
            entries.append(dict(base, action=outcome, bytes=freed,
                                detail="%s: clean and pushed, deleted (logged trash), "
                                       "git worktree prune%s" % (why, "; %s" % note if note
                                                                 else "")))
            continue
        # #3728/#4442 : gardé pour des fichiers modifiés ou non suivis, ou
        # pour des commits absents de la forge, lot fini depuis plus de
        # `after` : archivé (patchs + `HEAD.bundle` quand des commits ne sont
        # sur aucun remote) puis retiré (en cas de doute, gardé). #4442 : la
        # condition ne demande plus `pushed or integrated` — le bundle couvre
        # précisément le travail non poussé — et les copies étrangères
        # suivent la même fin au lieu d'être écartées avant ce bloc.
        held_by_archive = None
        unpushed = bool(st.get("head")) and not st["pushed"] and not st["integrated"]
        unsaved = not st["clean"] or unpushed
        if archive and ended and unsaved and st["head"] and not st["locked"] \
                and not st["submodules"] \
                and _archive_due(db, idents, archive, now=now, youngest=youngest):
            done = _archive_dirty_worktree(
                path, row.get("repo") or "", base, why=why, st=st, state=state,
                archive=archive, now=now, dry_run=dry_run, artifacts=artifacts,
                allow=allow, lock_path=lock_path, journal_path=journal_path,
                skip_roots=skip_roots, held=_unsaved_work(st),
                budget=budget, reserve_s=reserve_s)
            entries += done["entries"]
            if done["plan"]["action"] != "garde":
                plan.append(done["plan"])
                if done["removed"]:
                    store.set_worktree_status(row["id"], "removed", done["plan"]["detail"])
                continue
            held_by_archive = done["plan"]
            # dev3 N3: the refusal is the kept reason, in the status and
            # the `kept` line (written when it changes), not only in a plan
            detail = held_by_archive["detail"]
        if not st["clean"] or not st["integrated"] or st["submodules"]:
            plan.append(held_by_archive or dict(base, action="garde", detail=detail))
            if not dry_run and (row["status"] != "kept" or row.get("detail") != detail):
                store.set_worktree_status(row["id"], "kept", detail)
                entries.append(dict(base, action="kept", detail=detail, data={
                    "reason": st["reason"],
                    "command": "git -C %s status ; git -C %s log --all --not --remotes"
                               % (shlex.quote(path), shlex.quote(path))}))
            continue
        # #3802 B1 (cx2) : le relevé d'octets du retrait est borné par le
        # temps qui reste, réserve déduite — il ne parcourt pas l'arbre entier
        # sous host-pass.
        size = size_of(path, budget=_phase_budget(budget, reserve_s))
        plan.append(dict(base, action="retrait", bytes=size, detail=why))
        if dry_run:
            continue
        ok, error = remove_worktree(path, row.get("repo") or "", budget=window)
        if ok:
            store.set_worktree_status(row["id"], "removed", why)
            branch_note = delete_merged_branch(st["branch"], row.get("repo") or "") \
                if st["integrated"] else ""
            entries.append(dict(base, action="removed", bytes=size,
                                detail="%s: clean and integrated, git worktree remove%s" % (
                                    why, "; %s" % branch_note if branch_note else "")))
        else:
            detail = ("%s: %s" % (why, error) if error.startswith("pass budget")
                      else "%s: removal refused by git — %s" % (why, error))
            store.set_worktree_status(row["id"], "kept", detail)
            entries.append(dict(base, action="kept", detail=detail))
    if on_cursor is not None:
        if processed and processed >= len(rows):
            on_cursor(None)            # a full cycle was examined
        elif last is not None:
            try:
                on_cursor([float(last.get("created_ts") or 0), int(last["id"])])
            except (TypeError, ValueError):
                pass
    return entries, plan


def _remove_kept_worktree(path: str, repo: str, gitdir: str, *, artifacts, allow, lock_path,
                          journal_path, skip_roots=(), idle_before: float | None = None,
                          idle_note: str = "worktree active within 24 h", archive=None,
                          budget: PassBudget | None = None, reserve_s: float = 0.0
                          ) -> tuple[str, int, str, list]:
    """L230/#1665: removal of a kept worktree (lot merged, or orphan), under the
    host lock of the deleting passes (`build_artifacts`). Its build output
    goes first like an idle worktree's (descriptors, journal, `.git`,
    filesystem and managed-root protections); then the tree must be fully
    clean, ignored files included (nothing exempt); then the processes are
    read again, and the git lock (`<gitdir>/locked`) and submodules checked
    again (`submodules`), right before its directory, still the planned one,
    is renamed to a trash recorded in the journal and deleted; git forgets
    it (`git worktree prune`; `git worktree remove` cannot follow a renamed
    directory, its checks are the ones above, stricter).

    #3728 : `archive` (appelable, rend un refus ou "") remplace l'exigence
    d'un arbre propre : appelé sous le verrou, après la sortie de build, il
    archive les fichiers modifiés et non suivis ; un refus garde le worktree.

    (outcome, bytes freed, note, build output deleted): outcome `removed`,
    `partial` (gone from its path, something left in its trash), or `garde`
    / `attend` (note: why); the build output as journal entries (`path`,
    `action` `deleted` or `partial`, `bytes`, `detail`)."""
    built: list[dict] = []
    if not lock_path or not journal_path:
        return "garde", 0, "neither lock nor housekeeping journal", built
    # #3802 B1 (cx2) : le retrait ne commence pas dans la réserve gardée aux
    # phases suivantes ; son budget interne est celui qui reste, pas 600 s.
    if budget is not None and budget.reserved(reserve_s):
        return "garde", 0, "pass budget reached: removal left for the next pass", built
    window = budget.window(reserve_s) if budget is not None else None
    lock = build_artifacts._lock(lock_path)
    if lock is None:
        return "attend", 0, "another pass is deleting on the host", built
    try:
        journal = build_artifacts.Journal(journal_path)
        try:
            skip_ids = [tuple(r["id"]) for r in journal.records() if r.get("id")]
        except OSError as exc:
            return "garde", 0, "housekeeping journal unreadable (%s)" % (exc.strerror or exc), built
        work_budget = _phase_budget(budget, reserve_s)
        rules = build_artifacts.rules_of(artifacts)
        roots = [_real(p) for p in skip_roots if p]
        avoid = build_artifacts._identities(roots)
        real = _real(path)
        ident = build_artifacts.identity(real)
        parent_id = build_artifacts.identity(os.path.dirname(real))

        def check() -> str:
            # read again right before each rename: a process may have come in,
            # `git worktree lock` or a submodule may have been done meanwhile
            again = platform.user_paths(allow=allow)
            held = again.holders(path)
            refusal = again.doubt() or ("processus dedans (%s)" % ", ".join(held[:3])
                                        if held else "")
            if refusal or not gitdir:
                return refusal or "git directory of the worktree unknown"
            if os.path.lexists(os.path.join(gitdir, "locked")):
                return "locked (git worktree lock)"
            subs = submodules(path, gitdir)
            return "sous-modules (%s)" % subs if subs else ""

        def notes(done: dict) -> str:
            more = done["left"][:3] + done["errors"][:3]
            if done["stopped"]:
                more.append("budget de temps atteint")
            return " ; ".join(more)

        freed = 0
        found = build_artifacts.survey(path, rules, skip_roots=skip_roots, skip_ids=skip_ids,
                                       budget=work_budget)
        if not found["complete"]:
            return "garde", 0, "artifacts: %s" % found["why"], built
        if idle_before is not None and found["latest"] > idle_before:
            return "garde", 0, idle_note, built
        if ident is None or found["identity"] != ident:
            return "garde", 0, "directory changed during the pass", built
        for artifact in found["artifacts"]:
            inside = [r for r in roots if _under(r, _real(artifact))]
            refusal = ("contains a managed root (%s)" % inside[0]) if inside else \
                build_artifacts.tracked(path, artifact, budget=window)
            if not refusal:
                try:
                    done = build_artifacts.remove_artifact(
                        artifact, path, root=(real, ident), journal=journal, rules=rules,
                        avoid=avoid, budget=work_budget, check=check)
                except (OSError, ValueError) as exc:
                    refusal = str(exc)
                else:
                    freed += done["bytes"]
                    refusal = notes(done) and "partly deleted: %s" % notes(done)
                    built.append({"path": artifact, "bytes": done["bytes"],
                                  "action": "partial" if refusal else "deleted",
                                  "detail": refusal or "regenerable build output",
                                  "data": {"worktree": path, "trash": done["trash"]}})
            if refusal:
                return "garde", freed, "artifact %s: %s" % (artifact, refusal), built
        if archive is not None:
            refusal = archive()
            if refusal:
                return "garde", freed, refusal, built
        else:
            leftovers = ignored_leftovers(path, budget=window)
            if leftovers is None:
                return "garde", freed, "git state unreadable (git status --ignored)", built
            if leftovers:
                return "garde", freed, "%d untracked or ignored file(s) (%s)" % (
                    len(leftovers), leftovers[0]), built
        try:
            done = build_artifacts.remove_worktree_dir(
                path, (real, ident), parent_id, journal=journal, avoid=avoid,
                budget=work_budget, check=check)
        except (OSError, ValueError) as exc:
            return "garde", freed, "removal refused: %s" % exc, built
        prune_worktrees(repo, budget=window)
        freed += done["bytes"]
        if notes(done):
            return "partial", freed, "left in %s: %s" % (done["trash"], notes(done)), built
        return "removed", freed, "", built
    finally:
        lock.close()


def _shared_roots() -> tuple:
    """#248: the shared Rust target dirs (systemd drop-in), never entered."""
    return (os.path.expanduser("~/.cache/cargo-target"),)


def _artifact_pass(db, host: str, policy: Policy, *, cwd_of: dict, dry_run: bool,
                   now: float, skip_roots=(), lock_path: str | None = None,
                   journal_path: str | None = None, running=(), known_agents=(),
                   budget: PassBudget | None = None,
                   reserve_s: float = 0.0) -> dict:
    """L230: the worktrees of the repositories of the registered agents (and
    the tracked ones), their build artifacts deleted once idle; in a thread
    at the lowest priority, one worktree at a time.

    #3195: an idle worktree keeps them while the agent it is charged to is in
    turn (`running`), or while a lot recorded for it (tracked worktree, R2
    cx1) or named by its branch or its directory is open (an unknown id names
    no lot; an unreadable state or record keeps it). A worktree of the clone
    an agent works in is that agent's. #3956 : quand TOUS les lots cités sont
    finis, l'inactivité exigée est `artifact_ended_idle` (plus courte) — plus
    rien ne s'y reconstruit ; le dossier de travail d'un agent n'est plus
    exempté par son seul statut, seules les mêmes gardes (lot ouvert, tour,
    processus dedans, doute) le retiennent."""
    from . import housekeeping_clones

    rules = build_artifacts.rules_of(policy.artifacts)
    tracked = storage.of(db).housekeeping.worktrees(host, ("active", "kept"), limit=None)
    owner = {_real(row["path"]): row["agent"] for row in tracked}
    recorded = {_real(row["path"]): row.get("lot") for row in tracked
                if row.get("lot") not in (None, "")}
    agents = sorted(cwd_of.items(), key=lambda item: -len(item[1]))
    clone_of = {_real(cwd): name for name, cwd in cwd_of.items()}
    names = tuple(known_agents or ()) or tuple(cwd_of)
    main_of: dict[str, str] = {}
    states: dict[int, str | None] = {}

    def owner_of(path: str):
        real = _real(path)
        if real in owner:
            return owner[real]
        return next((name for name, cwd in agents if _under(real, _real(cwd))), None) \
            or clone_of.get(main_of.get(real, ""))

    def cited(wt: dict):
        """Les lots nommés par un worktree (None si un lot enregistré est illisible)."""
        branch = wt.get("branch") or ""
        lots = []
        if _real(wt["path"]) in recorded:
            # R2 cx1: the lot the tracked worktree was registered for counts
            # first, whatever its branch or directory names
            try:
                lots.append(int(str(recorded[_real(wt["path"])]).lstrip("#")))
            except ValueError:
                return None
        lots += cited_lots(branch, agent=branch_agent(branch, names)) \
            + housekeeping_clones.named_lots(os.path.basename(os.path.normpath(wt["path"])),
                                             names)
        return list(dict.fromkeys(lots))

    def lot_state(ident: int) -> str | None:
        if ident not in states:
            states[ident] = _lot_state(db, ident)
        return states[ident]

    def ended_of(wt: dict) -> bool:
        """#3956 : au moins un lot cité, et TOUS finis — plus rien ne s'y
        reconstruit, ses sorties régénérables peuvent partir plus tôt."""
        lots = cited(wt)
        if not lots:
            return False
        for ident in lots:
            state = lot_state(ident)
            if state is None or state not in LOT_ENDED:
                return False
        return True

    def keep(wt: dict) -> str:
        agent = owner_of(wt["path"])
        if agent and agent in running:
            return "turn in progress for %s" % agent
        lots = cited(wt)
        if lots is None:
            return "recorded lot %r unreadable" % (recorded[_real(wt["path"])],)
        for ident in lots:
            state = lot_state(ident)
            if state is not None and state not in LOT_ENDED:
                return "lot #%d %s" % (ident, state)
        return ""

    def listed(seed: str) -> list[dict]:
        return list_worktrees(seed, budget=budget)

    def candidates() -> list[dict]:
        found = build_artifacts.worktrees(seeds, listed)
        main_of.update((_real(wt["path"]), _real(wt["main"])) for wt in found)
        return found

    seeds = list(cwd_of.values()) + [row["path"] for row in tracked]
    skip = tuple(skip_roots) + _shared_roots()
    seconds = build_artifacts.PASS_BUDGET_S
    if budget is not None:
        # #3802 (B1) : l'artefact ne mange pas la réserve des phases suivantes
        seconds = min(seconds, max(0.0, budget.remaining() - reserve_s))
    return build_artifacts.gently(
        lambda: build_artifacts.run(
            candidates(), rules,
            idle_s=policy.artifact_idle,
            ended_idle_s=policy.artifact_ended_idle, ended_of=ended_of,
            protected=list(cwd_of.values()), skip_roots=skip,
            owner_of=owner_of, keep=keep, dry_run=dry_run, now=now,
            allow=policy.unreadable_ok, lock_path=lock_path, journal_path=journal_path,
            budget_s=seconds))


def reap_containers(runtime, db, host: str, *, dry_run: bool = False,
                    only_turn: str | None = None,
                    budget: PassBudget | None = None) -> dict:
    """Conteneurs de l'hôte (L73) : ceux qu'un tour d'ameesh a lancés
    (étiquette `ameesh.turn`) sont supprimés une fois leur tour fini — ou,
    s'ils portent `ameesh.lot`, une fois le lot fini ; les autres sont
    seulement signalés, avec la commande proposée.

    #1411 : le relevé couvre aussi les conteneurs ARRÊTÉS (`ps -a`) — un
    conteneur étiqueté sorti ou jamais démarré ne consomme plus de
    processus, mais retient sa couche et ses volumes anonymes, et restait
    invisible du ménage comme de la mesure. Une base de test d'un lot ENCORE
    OUVERT reste gardée jusqu'à la fin du lot (L73) : une photographie de
    l'état des tours ne prouve pas l'absence d'usage d'un hôte entier, et
    rien ne l'exclut d'un démarrage de tour qui suivrait le constat (R1
    #1411). On ne détruit donc pas sur cette photographie.

    #1547 (N4) : ce repli ne réduit pas la pression (couches, swap) des bases
    de lots encore ouverts — c'est le prix de la sûreté tant qu'un verrou
    commun avec le démarrage d'un tour n'existe pas ; le relevé `ps -a` les
    rend au moins visibles et comptées.

    Un conteneur étiqueté par un tour INCONNU de cette base, ou d'un autre
    hôte, n'est pas supprimé (signalé). Rend `{entries, plan, ameesh,
    others, others_total}` ; `ameesh`/`others` à None si le moteur ne répond
    pas ; `others` est borné à `OTHERS_TOP` entrées (#1547 N3), le compte
    total restant dans `others_total`."""
    out: dict = {"entries": [], "plan": [], "ameesh": None, "others": None,
                 "others_total": None}
    if runtime is None:
        return out
    rows = runtime.list_all(stopped=True)
    if rows is None:
        return out
    binary = os.path.basename(getattr(runtime, "binary", "docker") or "docker")
    ours, others, others_total = [], [], 0
    turns = storage.of(db).turn_resources
    for c in rows:
        if budget is not None and budget.expired():
            budget.timed_out = True
            break                      # #3802: a bounded host pass
        labels = c.get("labels") or {}
        turn = labels.get("ameesh.turn")
        ref = c.get("name") or c.get("id", "")[:12]
        if not turn:
            if only_turn is None:
                others_total += 1
                if len(others) < OTHERS_TOP:
                    others.append({"id": c.get("id", "")[:12], "name": c.get("name"),
                                   "image": c.get("image"), "status": c.get("status"),
                                   "command": "%s rm -f %s" % (binary, shlex.quote(ref))})
            continue
        if only_turn is not None and turn != only_turn:
            continue
        lot, agent = labels.get("ameesh.lot"), labels.get("ameesh.agent")
        base = {"kind": "container", "path": ref, "agent": agent, "lot": lot}
        row = turns.get(turn)
        if row is None or (row.get("host") or "") != (host or ""):
            why = "turn %s unknown on this host: flagged only" % turn[:8]
            ours.append(dict(base, action="signalé", detail=why))
            out["plan"].append(dict(base, action="signalé", detail=why))
            continue
        if row.get("status") == "running":
            ours.append(dict(base, action="attend", detail="turn %s in progress" % turn[:8]))
            continue
        if lot:
            state = _lot_state(db, lot)
            if state not in LOT_ENDED:
                detail = "lot #%s %s" % (lot, state or "unknown")
                ours.append(dict(base, action="attend", detail=detail))
                out["plan"].append(dict(base, action="attend", detail=detail))
                continue
            why = "lot #%s %s" % (lot, state)
        else:
            why = "turn %s finished" % turn[:8]
        out["plan"].append(dict(base, action="suppression", detail=why))
        if dry_run:
            ours.append(dict(base, action="suppression", detail=why))
            continue
        ok, error = runtime.remove(c.get("id") or ref)
        entry = dict(base, action="removed" if ok else "kept",
                     detail=("%s: %s rm -f -v" % (why, binary)) if ok
                     else "%s: deletion refused — %s" % (why, error),
                     data={"id": c.get("id"), "image": c.get("image"), "turn": turn})
        out["entries"].append(entry)
        if not ok:
            ours.append(dict(base, action="kept", detail=entry["detail"]))
    out["ameesh"] = ours
    out["others"] = others if only_turn is None else None
    out["others_total"] = others_total if only_turn is None else None
    return out


#: #2003 : actions du journal qui portent un épisode d'absence continue d'un
#: volume (le volume était sans conteneur à ce relevé-là)
_VOLUME_EPISODE_ACTIONS = ("waiting", "kept")

#: #4560 : les mêmes actions pour une image sans tag (dangling)
_IMAGE_EPISODE_ACTIONS = _VOLUME_EPISODE_ACTIONS


def _lifetime_declared(labels: dict) -> bool:
    """#4560 : cette étiquette de durée de vie déclare-t-elle la ressource
    jetable ? Le label générique `ameesh.lifetime=test` d'abord ; l'ancien
    `ameesh.bench=disposable` des volumes de banc reste honoré (#2003)."""
    from . import containers as containers_mod
    return (labels.get(containers_mod.LIFETIME_LABEL) == containers_mod.LIFETIME_TEST
            or labels.get(containers_mod.BENCH_LABEL) == containers_mod.BENCH_DISPOSABLE)


def _volume_episode(previous: dict | None, created: float | None) -> float | None:
    """#2003 : début de l'épisode d'absence continue d'un volume, tel que le
    dernier relevé du journal l'a laissé ; None quand l'épisode commence à ce
    passage — aucune ligne, ligne d'une autre action (une reprise a refermé
    l'épisode), identité de volume illisible ou différente (le nom a été
    recréé : c'est un autre volume)."""
    if not previous or created is None:
        return None
    if (previous.get("action") or "") not in _VOLUME_EPISODE_ACTIONS:
        return None
    data = previous.get("data") or {}
    try:
        if float(data.get("created")) != float(created):
            return None
        return float(data.get("episode_since"))
    except (TypeError, ValueError):
        return None


def _image_episode(previous: dict | None, created: float | None) -> float | None:
    """#4560 : début de l'épisode d'absence continue d'une image, exactement
    comme pour un volume (#2003) : l'identité est le couple `id`/`Created`, une
    image retaguée referme l'épisode, une image reconstruite en ouvre un
    autre."""
    return _volume_episode(previous, created)


def reap_volumes(runtime, db, host: str, policy: Policy, *, dry_run: bool = False,
                 now: float | None = None, budget: PassBudget | None = None) -> dict:
    """Volumes Docker de l'hôte (L73/#2003/#4560) : récolte les volumes SANS
    conteneur (arrêtés compris, `volume ls --filter dangling=true`) dont le
    mesh a DÉCLARÉ le cycle de vie jetable — l'étiquette de durée de vie
    `ameesh.lifetime` [`containers.LIFETIME_TEST`], posée par
    `scripts/pg-up.sh` sur le volume d'un banc de lot, ou l'ancienne étiquette
    `ameesh.bench` [`containers.BENCH_DISPOSABLE`] — et seulement après
    `policy.docker_volume_idle` d'ABSENCE CONTINUE.

    La provenance n'est jamais déduite d'une proximité d'heure : un volume
    anonyme (`com.docker.volume.anonymous`) ne porte aucune identité du mesh
    et n'est jamais supprimé ici (le shim d'un tour force `rm -v`, donc les
    volumes anonymes d'un conteneur supprimé par une suite partent avec lui) ;
    un volume de BASE nommé (`ameesh-pg-data`, `*-pg-data`) n'est jamais
    récolté, même si une étiquette jetable y a été posée : ses données
    survivent aux conteneurs et aux suites.

    Le délai sans conteneur se mesure sur les relevés successifs : chaque
    passage journalise l'épisode (`episode_since`, avec l'identité `created`
    du volume) ; un premier relevé ne supprime donc jamais, une reprise du
    volume (`referenced`) referme l'épisode, un volume recréé sous le même nom
    en ouvre un nouveau. Le journal est celui du ménage (30 jours de
    rétention) : au-delà, l'épisode repart, ce qui ne fait que retarder.

    Les autres volumes — anonyme, nommé sans étiquette, étiquette non
    jetable — ne sont JAMAIS supprimés : listés dans `others` (borné à
    `VOLUMES_TOP`, compte total dans `others_total`) avec la commande
    `docker volume rm` qu'un humain peut décider (précédent #411 : les volumes
    nommés existants sont conservés). Un volume jetable dont l'identité est
    illisible est signalé dans `ours` sans suppression. `docker volume rm`
    refuse de lui-même un volume encore monté : la course avec un `docker run`
    qui le reprend est fermée par Docker. Seul `docker` est couvert (comme la
    mesure #411). `dry_run` : le plan seulement, aucun épisode n'est écrit.

    Rend `{entries, plan, ours, others, others_total}` ; `ours`/`others` à
    None si le moteur ne répond pas, comme [`reap_containers`]."""
    from . import containers as containers_mod
    out: dict = {"entries": [], "plan": [], "ours": None, "others": None,
                 "others_total": None}
    if runtime is None or not getattr(policy, "docker_volumes", True):
        return out
    binary = os.path.basename(getattr(runtime, "binary", "docker") or "docker")
    if binary != "docker" or not hasattr(runtime, "volumes"):
        return out
    all_rows = runtime.volumes(dangling=False)
    dangling_rows = runtime.volumes(dangling=True)
    if all_rows is None or dangling_rows is None:
        return out
    now = time.time() if now is None else float(now)
    idle = max(0.0, float(policy.docker_volume_idle))
    free = {row["name"] for row in dangling_rows}
    ours, others, others_total = [], [], 0

    def _other(row: dict, name: str, reason: str) -> None:
        nonlocal others_total
        others_total += 1
        if len(others) < VOLUMES_TOP:
            others.append({"kind": "volume", "path": name, "driver": row.get("driver"),
                           "detail": reason,
                           "command": "%s volume rm %s" % (binary, shlex.quote(name))})

    declared = []
    for row in all_rows:
        if budget is not None and budget.expired():
            budget.timed_out = True
            break                      # #3802: a bounded host pass
        labels = row.get("labels") or {}
        bench = labels.get(containers_mod.BENCH_LABEL)
        lifetime = labels.get(containers_mod.LIFETIME_LABEL)
        if bench is None and lifetime is None:
            continue
        if _lifetime_declared(labels):
            if containers_mod.is_base_volume(row["name"]):
                # #4560 : une base nommée n'est jamais récoltée, même
                # étiquetée jetable par erreur. Dite, sans commande.
                why = ("named base volume (*%s): never reaped, even labelled disposable"
                       % containers_mod.BASE_VOLUME_SUFFIX)
                base = {"kind": "volume", "path": row["name"]}
                out["plan"].append(dict(base, action="garde", detail=why))
                ours.append(dict(base, action="garde", detail=why))
                continue
            declared.append(row)
        elif row["name"] in free:
            marks = []
            if bench is not None:
                marks.append("%s=%s" % (containers_mod.BENCH_LABEL, bench))
            if lifetime is not None:
                marks.append("%s=%s" % (containers_mod.LIFETIME_LABEL, lifetime))
            _other(row, row["name"],
                   "labelled %s: not declared disposable by the mesh"
                   % ", ".join(marks))
    for row in dangling_rows:
        if budget is not None and budget.expired():
            budget.timed_out = True
            break                      # #3802: a bounded host pass
        labels = row.get("labels") or {}
        if (containers_mod.BENCH_LABEL in labels
                or containers_mod.LIFETIME_LABEL in labels):
            continue                    # déclaré (plus bas) ou déjà signalé
        if containers_mod.ANONYMOUS_LABEL in labels:
            _other(row, row["name"],
                   "anonymous volume: no mesh provenance (never deleted)")
        else:
            _other(row, row["name"],
                   "no container references it; not declared disposable by the mesh")

    previous: dict = {}
    if db is not None:
        try:
            for line in storage.of(db).housekeeping.latest_by_path(host or None, "volume"):
                if line.get("path"):
                    previous[str(line["path"])] = line
        except Exception:               # journal illisible : chaque épisode repart
            previous = {}
    stamps = runtime.volume_created([row["name"] for row in declared]) \
        if declared and hasattr(runtime, "volume_created") else {}
    if stamps is None:                  # inspection illisible : aucune identité
        stamps = {}
    for row in declared:
        if budget is not None and budget.expired():
            budget.timed_out = True
            break                      # #3802: a bounded host pass
        name = row["name"]
        base = {"kind": "volume", "path": name}
        stamp = stamps.get(name)
        if name not in free:
            # Une référence suffit à fermer l'absence, même si l'inspection
            # de CreatedAt échoue : elle ne doit pas préserver l'ancien délai.
            if (previous.get(name) or {}).get("action") in _VOLUME_EPISODE_ACTIONS:
                out["entries"].append(dict(
                    base, action="referenced",
                    detail="volume %s is referenced again: the absence episode is closed" % name,
                    data={"created": stamp}))
            continue
        if stamp is None:
            why = "declared disposable: creation identity unreadable: flagged only"
            ours.append(dict(base, action="signalé", detail=why))
            out["plan"].append(dict(base, action="signalé", detail=why))
            continue
        since = _volume_episode(previous.get(name), stamp)
        if since is None:
            since = now
        absence = max(0.0, now - since)
        if absence < idle:
            detail = "declared disposable; no container for %s (under %s)" % (
                fmt_idle(absence), fmt_idle(idle))
            out["entries"].append(dict(base, action="waiting", detail=detail,
                                       data={"created": stamp, "episode_since": since}))
            ours.append(dict(base, action="attend", detail=detail))
            continue
        why = "declared disposable; no container for %s" % fmt_idle(absence)
        out["plan"].append(dict(base, action="suppression", detail=why))
        if dry_run:
            ours.append(dict(base, action="suppression", detail=why))
            continue
        ok, error = runtime.remove_volume(name)
        entry = dict(base, action="removed" if ok else "kept",
                     detail=("%s: %s volume rm" % (why, binary)) if ok
                     else "%s: deletion refused — %s" % (why, error),
                     data={"created": stamp, "episode_since": since})
        out["entries"].append(entry)
        ours.append(entry)
    out["ours"] = ours
    out["others"] = others
    out["others_total"] = others_total
    return out


def reap_images(runtime, db, host: str, policy: Policy, *, dry_run: bool = False,
                now: float | None = None, budget: PassBudget | None = None) -> dict:
    """Images Docker de l'hôte (L73/#4560) : récolte les images SANS TAG
    (`dangling`) dont le mesh a DÉCLARÉ le cycle de vie jetable — l'étiquette
    de durée de vie `ameesh.lifetime` [`containers.LIFETIME_TEST`], posée par
    le shim sur une image construite dans un tour, ou l'ancienne
    `ameesh.bench` [`containers.BENCH_DISPOSABLE`] — et seulement après
    `policy.docker_volume_idle` d'ABSENCE CONTINUE.

    Une image TAGUÉE n'est JAMAIS supprimée, même étiquetée jetable : un tag
    est une référence (socle de l'hôte, image de release ou de suite). Une
    image retaguée referme l'épisode, une image reconstruite (autre `Id` ou
    `Created`) en ouvre un autre, une image disparue le referme. Le délai est
    celui des volumes (`docker_volume_idle`) et se mesure sur les relevés
    successifs, jamais sur l'âge de création. Le retrait passe par un tag
    TEMPORAIRE à nous (`docker tag`, atomique) puis `docker image rm
    --no-prune <tag>` : un tag concurrent ajouté entre-temps survit (seul le
    nôtre est retiré) et les images parentes non taguées ne sont jamais
    visées. Le moteur refuse encore une image utilisée par un conteneur, même
    arrêté : la course avec un `docker run` qui la reprend est fermée. Seul
    `docker` est couvert (comme la mesure #411). `dry_run` : le plan seulement.

    Rend `{entries, plan, ours, others_total}` ; `ours` à None si le moteur ne
    répond pas."""
    from . import containers as containers_mod
    out: dict = {"entries": [], "plan": [], "ours": None, "others_total": None}
    if runtime is None or not getattr(policy, "docker_images", True):
        return out
    binary = os.path.basename(getattr(runtime, "binary", "docker") or "docker")
    if binary != "docker" or not hasattr(runtime, "images"):
        return out
    rows = runtime.images()
    if rows is None:
        return out
    now = time.time() if now is None else float(now)
    idle = max(0.0, float(policy.docker_volume_idle))
    ours: list = []
    declared, others_total = [], 0
    for row in rows:
        if budget is not None and budget.expired():
            budget.timed_out = True
            break                      # #3802: a bounded host pass
        labels = row.get("labels") or {}
        if _lifetime_declared(labels):
            declared.append(row)
        elif not (row.get("tags") or ()):
            # image dangling sans provenance du mesh : signalée par le compte,
            # jamais supprimée (précédent #411).
            others_total += 1
    previous: dict = {}
    if db is not None:
        try:
            for line in storage.of(db).housekeeping.latest_by_path(host or None, "image"):
                if line.get("path"):
                    previous[str(line["path"])] = line
        except Exception:               # journal illisible : chaque épisode repart
            previous = {}
    present = {str(row.get("id") or "") for row in rows}
    for ident, line in previous.items():
        if (line.get("action") in _IMAGE_EPISODE_ACTIONS and ident not in present):
            out["entries"].append({"kind": "image", "path": ident, "action": "gone",
                                   "detail": "image gone: the absence episode is closed"})
    for row in declared:
        if budget is not None and budget.expired():
            budget.timed_out = True
            break                      # #3802: a bounded host pass
        ident = str(row.get("id") or "")
        if not ident:
            continue
        base = {"kind": "image", "path": ident}
        stamp = row.get("created")
        tags = row.get("tags") or ()
        if tags:
            # Un tag suffit à fermer l'absence, même si l'inspection de
            # Created échoue : elle ne doit pas préserver l'ancien délai.
            if (previous.get(ident) or {}).get("action") in _IMAGE_EPISODE_ACTIONS:
                out["entries"].append(dict(
                    base, action="referenced",
                    detail="image %s is tagged again: the absence episode is closed"
                           % ident[:19], data={"created": stamp}))
            continue
        if stamp is None:
            why = "declared disposable: creation identity unreadable: flagged only"
            ours.append(dict(base, action="signalé", detail=why))
            out["plan"].append(dict(base, action="signalé", detail=why))
            continue
        since = _image_episode(previous.get(ident), stamp)
        if since is None:
            since = now
        absence = max(0.0, now - since)
        if absence < idle:
            detail = "declared disposable image; no tag for %s (under %s)" % (
                fmt_idle(absence), fmt_idle(idle))
            out["entries"].append(dict(base, action="waiting", detail=detail,
                                       data={"created": stamp, "episode_since": since}))
            ours.append(dict(base, action="attend", detail=detail))
            continue
        why = "declared disposable image; no tag for %s" % fmt_idle(absence)
        out["plan"].append(dict(base, action="suppression", detail=why))
        if dry_run:
            ours.append(dict(base, action="suppression", detail=why))
            continue
        ok, error = runtime.remove_image(ident)
        entry = dict(base, action="removed" if ok else "kept",
                     detail=("%s: %s image rm" % (why, binary)) if ok
                     else "%s: deletion refused — %s" % (why, error),
                     data={"created": stamp, "episode_since": since})
        out["entries"].append(entry)
        ours.append(entry)
    out["ours"] = ours
    out["others_total"] = others_total
    return out


#: #3226 : délai après la fin du tour avant de récolter un scope dsh laissé
DSH_SCOPE_GRACE_S = 3600.0
#: #3226 : un scope gardé (non attribuable) est re-signalé au plus si souvent
DSH_SCOPE_REFRESH_S = 12 * 3600.0
_MENAGE_TMP = "/%s/tmp/" % MANAGED_DIR


def _tmpdir_agent(tmpdir: str | None) -> str | None:
    """L'agent d'un TMPDIR `…/.menage/tmp/<agent>[/…]`, pour le seul constat."""
    if not tmpdir or _MENAGE_TMP not in tmpdir:
        return None
    return tmpdir.split(_MENAGE_TMP, 1)[1].split("/", 1)[0] or None


def dsh_scope_verdict(scope: dict, row: dict | None, host: str,
                      now: float) -> tuple[str, str, str | None]:
    """#3226 : (action, raison, tour) d'un scope `dsh-subprocess-*` relevé par
    `platform.dsh_scopes.scan` ; action `kill`, `wait` ou `signal`.

    Récolté seulement si le processus dsh qui l'a lancé n'existe plus, que
    TOUS ses processus portent le même `AMEESH_TURN_ID`, que ce tour est connu
    de CET hôte, clos (`done`, `orphan`) et fini depuis `DSH_SCOPE_GRACE_S`.
    dsh vivant, tour `running` : attente. Liste, heure de démarrage du dsh ou
    environnement illisible, aucun tour ou plusieurs, tour inconnu ici, fin de
    tour illisible : gardé et signalé (comme `_orphan_verdict`) — un doute ne
    se tait jamais et une grâce non mesurable ne se remplace pas par le
    démarrage du processus."""
    members = scope["members"]
    if scope["unreadable"] or scope.get("owner_unread"):
        return "signal", "process list or dsh start time unreadable: not attributable", None
    if any(m.get("unread") for m in members):
        return "signal", "process start or environment unreadable: not attributable", None
    if scope["owner_alive"]:
        return "wait", "dsh pid %d alive" % scope["dsh_pid"], None
    turns = {m["turn"] for m in members}
    if turns == {None}:
        return "signal", "no AMEESH_TURN_ID: not attributable", None
    if len(turns) != 1:
        return "signal", "several turns (%s): not attributable" % ", ".join(
            sorted(t[:8] if t else "none" for t in turns)), None
    turn = turns.pop()
    if row is None or (row.get("host") or "") != (host or ""):
        return "signal", "turn %s unknown on this host: not attributable" % turn[:8], turn
    status = row.get("status")
    if status == "running":
        return "wait", "turn %s not closed" % turn[:8], turn
    if status not in ("done", "orphan"):
        return "signal", "turn %s status %r: not attributable" % (
            turn[:8], status), turn
    try:
        since = float(row.get("ended_ts"))
        if not math.isfinite(since):
            raise ValueError("not finite")
    except (TypeError, ValueError):
        return "signal", "turn %s closed but end time unreadable: not attributable" % (
            turn[:8]), turn
    if now - since < DSH_SCOPE_GRACE_S:
        return "wait", "turn %s closed %ds ago (grace %ds)" % (
            turn[:8], int(now - since), int(DSH_SCOPE_GRACE_S)), turn
    return "kill", "turn %s %s, dsh pid %d gone" % (
        turn[:8], status, scope["dsh_pid"]), turn


def reap_dsh_scopes(db, host: str, *, dry_run: bool = False, now: float | None = None,
                    scan=None, terminate=None,
                    budget: PassBudget | None = None) -> dict:
    """#3226 : les scopes systemd `dsh-subprocess-*` laissés par un tour clos
    (dsh sans la garde #490) sont tués — SIGTERM, puis SIGKILL — après
    `DSH_SCOPE_GRACE_S`, par pidfd, journalisés (`dsh_scope`, `killed`) ; ceux qu'on ne
    sait pas attribuer sont gardés et signalés (`signaled`, re-signalé au
    plus toutes les `DSH_SCOPE_REFRESH_S`, `gone` quand ils disparaissent).
    `scan`/`terminate` : ceux de `platform.dsh_scopes` (surchargés par les
    tests). Un scope dont le traitement échoue est signalé sans interrompre
    les suivants ; un scope dont tous les processus ont disparu avant le
    signal est `gone`, jamais `killed`. Rend `{entries, plan}`."""
    from .platform import dsh_scopes
    scan = dsh_scopes.scan if scan is None else scan
    terminate = dsh_scopes.terminate if terminate is None else terminate
    now = time.time() if now is None else float(now)
    out: dict = {"entries": [], "plan": []}
    try:
        scopes = scan()
    except (OSError, platform.NotAvailable):
        scopes = None
    if scopes is None:
        return out
    hk = storage.of(db).housekeeping
    turns = storage.of(db).turn_resources
    last = {row["path"]: row for row in hk.latest_by_path(host, "dsh_scope")}
    seen = set()
    for scope in scopes:
        if budget is not None and budget.expired():
            budget.timed_out = True
            break                      # #3802: a bounded host pass
        unit = (scope or {}).get("unit") or ""
        seen.add(unit)
        try:
            members = scope["members"]
            turn_ids = {m["turn"] for m in members if m.get("turn")}
            row = turns.get(next(iter(turn_ids))) if len(turn_ids) == 1 else None
            action, why, turn = dsh_scope_verdict(scope, row, host, now)
            agent = (row or {}).get("agent") or next(
                (a for a in (_tmpdir_agent(m.get("tmpdir")) for m in members) if a), None)
            previous = last.get(unit) or {}
            data = {"dsh_pid": scope["dsh_pid"], "turn": turn,
                    "pids": [m["pid"] for m in members],
                    "commands": [m["command"] for m in members[:3]],
                    "first_seen": ((previous.get("data") or {}).get("first_seen")
                                   if previous.get("action") == "signaled" else None) or now}
            base = {"kind": "dsh_scope", "path": unit, "agent": agent}
            if action == "wait":
                out["plan"].append(dict(base, action="attend", detail=why))
                continue
            if action == "kill":
                out["plan"].append(dict(base, action="suppression", detail=why))
                if dry_run:
                    continue
                if not members:
                    out["entries"].append(dict(base, action="gone", detail=(
                        "%s: empty scope gone, nothing to signal" % why)))
                    continue
                left = terminate(members)
                if not left:
                    out["entries"].append(dict(base, action="killed", data=data,
                                               detail="%s: SIGTERM then SIGKILL" % why))
                    continue
                if (len(left) == len(members)
                        and all((m.get("why") or "") == "gone before signal" for m in left)):
                    out["entries"].append(dict(base, action="gone", detail=(
                        "%s: every process gone before signal" % why)))
                    continue
                why = "%s: %d process(es) not killed (%s)" % (
                    why, len(left), ", ".join("%d %s" % (m["pid"], m.get("why") or "alive")
                                              for m in left))
            else:
                out["plan"].append(dict(base, action="signalé", detail=why))
                if dry_run:
                    continue
            fresh = (previous.get("action") == "signaled" and previous.get("detail") == why
                     and now - float(previous.get("at_ts") or 0) < DSH_SCOPE_REFRESH_S)
            if not fresh:
                out["entries"].append(dict(base, action="signaled", detail=why, data=data))
        except Exception as exc:  # un scope ne doit pas perdre les suivants
            out["entries"].append({"kind": "dsh_scope", "path": unit,
                                   "action": "signaled",
                                   "detail": "scope pass failed (%s): not attributable"
                                             % type(exc).__name__})
    if not dry_run:
        for unit, previous in last.items():
            if unit not in seen and previous.get("action") == "signaled":
                out["entries"].append({"kind": "dsh_scope", "action": "gone", "path": unit,
                                       "agent": previous.get("agent"),
                                       "detail": "dsh scope gone"})
    return out


def tmp_problem(path: str, owner: str | None, key: str, *, registered: bool,
                idle_after: float, now: float,
                budget: PassBudget | None = None) -> str:
    """L219: why the pass keeps the temporary directory `path` of an agent
    not in turn, empty if it may evict (`registered`) or delete it. Another
    mesh's is never touched; one without owner file (another mesh sharing
    the state directory, or before L219) only after `UNOWNED_IDLE_S`
    without any activity; this mesh's of an agent ABSENT from the registry
    after `idle_after` (a turn writes in it).

    #3802 : `budget` borne le parcours d'activité ; une échéance atteinte
    rend le dossier « non jugeable » (gardé), jamais idle."""
    if owner is not None and owner != key:
        return "belongs to another mesh"
    if owner is not None and registered:
        return ""
    latest = last_activity(path, budget=budget)
    if latest is None:
        return "activity unreadable (incomplete walk or pass budget)"
    idle = now - latest
    if owner is None and idle < UNOWNED_IDLE_S:
        return "no owner (another mesh on this machine, or older than L219), " \
               "active %d h ago (less than %d h)" % (idle // 3600, UNOWNED_IDLE_S // 3600)
    if not registered and idle < idle_after:
        return "active %d min ago (less than %d min)" % (idle // 60, idle_after // 60)
    return ""


#: #3802 : action of the journal line that names a blocked host pass
HOST_PASS_KIND = "host_pass"


def host_pass_lock(cfg) -> str:
    """The stable path of the host-wide housekeeping lock (#3802)."""
    return os.path.join(cfg.state_dir, MANAGED_DIR, "host-pass.lock")


def _host_pass_block_line(cfg, host: str, holder: dict, now: float,
                          reason: str) -> dict:
    """The journal line of a pass skipped behind a too-old holder (#3802):
    pid, runner and since, never the holder's argv (#3403)."""
    since = holder.get("since")
    return {"kind": HOST_PASS_KIND, "action": "blocked", "path": host_pass_lock(cfg),
            "bytes": 0, "detail": reason,
            "data": {"pid": holder.get("pid"), "runner": holder.get("runner") or "",
                     "since": since, "host": host,
                     "phase": holder.get("phase") or "",
                     "source": holder.get("source") or "",
                     "first_seen": since, "at": now}}


def _report_host_pass_blocked(db, cfg, host: str, actor: str, holder: dict,
                              now: float, reason: str) -> None:
    """Journal a blocked host pass, at most once per holder and per
    `HOST_PASS_REFRESH_S`; never raises (a blocked pass has no other voice)."""
    try:
        store = storage.of(db).housekeeping
        path = host_pass_lock(cfg)
        previous = next((row for row in store.latest_by_path(host, HOST_PASS_KIND)
                         if row.get("path") == path), None)
        if previous is not None and previous.get("action") == "blocked":
            data = previous.get("data") or {}
            same = (data.get("pid") == holder.get("pid")
                    and data.get("since") == holder.get("since"))
            age = now - float(previous.get("at_ts") or 0)
            if same and age < HOST_PASS_REFRESH_S:
                return
        line = _host_pass_block_line(cfg, host, holder, now, reason)
        if previous is not None and previous.get("action") == "blocked":
            old = (previous.get("data") or {}).get("first_seen")
            line["data"]["first_seen"] = old or line["data"]["first_seen"]
        store.log(host, [line], actor=actor)
    except Exception:  # noqa: BLE001 - a diagnostic line never breaks a pass
        pass


def _timed_out_report(host: str, dry_run: bool, budget: PassBudget,
                      phase: str) -> dict:
    """#3802 : the report of a pass stopped by its own budget. The lists of
    `_run_pass` are read through `budget.context` (they were mutated in
    place), so what the pass did before stopping is still journaled."""
    context = budget.context or {}
    entries = list(context.get("entries") or ())
    plan = list(context.get("plan") or ())
    skipped = list(context.get("skipped") or ())
    unowned = list(context.get("unowned") or ())
    orphans = list(context.get("orphans") or ())
    note = ("pass budget of %ds reached in phase %r: phase abandoned, host lock "
            "released" % (round(budget.budget), phase))
    skipped.append(note)
    summary = budget.summary()
    return {"host": host, "dry_run": dry_run, "status": "timed_out",
            "timed_out": True, "phase": phase, "budget": summary,
            "entries": entries, "plan": plan, "skipped": skipped,
            "orphans": orphans, "unowned": unowned, "measure": None}


def host_units_dir() -> str | None:
    """#4754 : le dossier des unités utilisateur de l'hôte (veilles, réveils),
    ou None hors systemd."""
    if not platform.is_linux():
        return None
    from . import watch
    return watch.unit_dir()


def run_pass(cfg, db, policy: Policy, *, host: str, actor: str, dry_run: bool = False,
             busy: bool = False, agents_in_turn: set | None = None,
             now: float | None = None,
             runtime=None, artifact_scan: bool = True, min_interval_s: float = 0.0,
             free_target: int = 0, budget_s: float | None = None,
             progress=None, units_dir: str | None = None) -> dict:
    """One host pass across all runners and the CLI, without waiting.

    Periodic callers share the last completed pass's timestamp, under the
    same stable OS lock. An explicit or pressure-triggered pass bypasses the
    interval; dry runs never postpone real cleanup. Nothing is cached about
    whether a path may be deleted.

    #3293: `free_target` is the free space (bytes) the cache's filesystem
    should keep — the host's `min_disk_free`, the pause threshold. Under it,
    the caches are evicted enough to free the deficit, the npm cache even
    during a turn when no npm runs.

    #3802: `budget_s` bounds the pass (default `HOST_PASS_BUDGET_S`). Past
    it, the pass stops at its next checkpoint, releases `host-pass.lock` and
    reports `status: "timed_out"`; a caller woken by a lock it finds held
    past `HOST_PASS_ALERT_AFTER_S` journals a `host_pass` blocked line that
    names the holder (pid, runner, since) — the alert `housekeeping_blocked`
    reads it. The lock file carries the holder record while held, and never
    after the pass (a dead holder releases the flock in the OS anyway).
    """
    from .platform import session_lock

    lock_dir = os.path.join(cfg.state_dir, MANAGED_DIR)
    lock_path = host_pass_lock(cfg)
    tick = time.time() if now is None else float(now)
    budget_s = HOST_PASS_BUDGET_S if budget_s is None else float(budget_s)

    def skipped(reason, holder=None):
        # #2173: a pass that did not run must not read as executed. The
        # dedicated `status` lets the report (and `--json`) tell a skipped
        # pass from an empty one, without touching the lock itself.
        report = {"host": host, "dry_run": dry_run, "status": "skipped",
                  "entries": [], "plan": [], "skipped": [reason], "orphans": [],
                  "unowned": [], "measure": None}
        if holder is not None:
            report["holder"] = holder
            age = None
            try:
                age = max(0.0, tick - float(holder.get("since")))
            except (TypeError, ValueError):
                age = None
            if age is not None:
                report["held_s"] = round(age, 3)
                if age >= HOST_PASS_ALERT_AFTER_S:
                    report["blocked"] = dict(holder, age_s=round(age, 3))
        return report

    lock = session_lock.try_lock(lock_dir, "host-pass")
    if lock is None:
        holder = session_lock.holder(lock_dir, "host-pass")
        if holder is not None:
            age = None
            try:
                age = max(0.0, tick - float(holder.get("since")))
            except (TypeError, ValueError):
                age = None
            if age is not None and age >= HOST_PASS_ALERT_AFTER_S:
                reason = ("another housekeeping pass is running on this host: "
                          "host-pass lock held by %s (pid %s) for %s"
                          % (holder.get("runner") or "?",
                             holder.get("pid") if holder.get("pid") is not None else "?",
                             fmt_idle(age)))
                _report_host_pass_blocked(db, cfg, host, actor, holder, tick, reason)
                return skipped("another housekeeping pass is running on this host",
                               holder)
        return skipped("another housekeeping pass is running on this host", holder)

    state = session_lock.read_content(lock)
    holder = {"pid": os.getpid(), "runner": actor or "", "since": tick,
              "host": host, "phase": "", "phase_since": tick, "source": "lock"}
    key = mesh_key(cfg)
    cursors = state.get(WORKTREES_CURSOR_KEY)
    if not isinstance(cursors, dict):
        cursors = {}
    state[WORKTREES_CURSOR_KEY] = cursors

    def notify(message: str) -> None:
        # #4234 : une passe qui dure (jusqu'à 15 min) ne doit plus être muette.
        # Le rapport final reste sur stdout ; cette progression est un
        # diagnostic, elle ne fait jamais échouer la passe.
        if progress is None:
            return
        try:
            progress(message)
        except Exception:  # noqa: BLE001 - un affichage cassé n'arrête pas la passe
            pass

    def on_phase(name: str) -> None:
        # the lock names the phase its holder is in: what a skipped pass —
        # and its alert — can say beyond the pid (#3802)
        holder["phase"] = name
        holder["phase_since"] = time.time()
        state[session_lock.HOLDER_KEY] = holder
        session_lock.write_content(lock, state)
        notify("phase:%s" % name)

    def on_cursor(value) -> None:
        # #3802 (B1) : le curseur de reprise des worktrees vit dans l'état du
        # verrou, comme les heures de passage : il survit à une passe
        # timed_out, sinon chaque passage referait les mêmes premiers.
        if value is None:
            cursors.pop(key, None)
        else:
            cursors[key] = value
        session_lock.write_content(lock, state)

    # write the holder before the interval test: the window during which a
    # skipped pass sees no record is as short as possible (#3802 N1)
    state[session_lock.HOLDER_KEY] = holder
    session_lock.write_content(lock, state)
    try:
        completed_passes = state
        if min_interval_s > 0:
            try:
                elapsed = tick - float(completed_passes[key])
            except (KeyError, TypeError, ValueError):  # new or interrupted timestamp
                elapsed = -1.0
            if 0 <= elapsed < min_interval_s:
                return skipped("housekeeping already ran on this host less than %gs ago"
                               % min_interval_s)
        budget = PassBudget(budget_s, on_phase=on_phase)
        notify("started")
        try:
            report = _run_pass(cfg, db, policy, host=host, actor=actor, dry_run=dry_run,
                               busy=busy, agents_in_turn=agents_in_turn, now=now,
                               runtime=runtime, artifact_scan=artifact_scan,
                               free_target=free_target, budget=budget,
                               cursor=cursors.get(key), on_cursor=on_cursor,
                               units_dir=units_dir)
        except _PassExpired as expired:
            notify("timed_out:%s" % expired.phase)
            report = _timed_out_report(host, dry_run, budget, expired.phase)
            report["entries"] = list(report["entries"])
            if not dry_run:
                report["entries"].append({
                    "kind": HOST_PASS_KIND, "action": "timed_out", "path": lock_path,
                    "bytes": 0,
                    "detail": report["skipped"][-1], "data": report["budget"]})
                try:
                    # the work done before the timeout is journaled too: the
                    # pass will not reach `_run_pass`'s own write
                    storage.of(db).housekeeping.log(
                        host, report["entries"], actor=actor)
                except Exception:  # noqa: BLE001 - the report is already built
                    pass
        else:
            report.setdefault("budget", budget.summary())
            if not dry_run and not report.get("disabled") and not report.get("timed_out"):
                state[key] = time.time() if now is None else float(now)
        return report
    finally:
        state.pop(session_lock.HOLDER_KEY, None)
        try:
            session_lock.write_content(lock, state)
        finally:
            lock.close()


def _run_pass(cfg, db, policy: Policy, *, host: str, actor: str, dry_run: bool = False,
              busy: bool = False, agents_in_turn: set | None = None,
              now: float | None = None,
              runtime=None, artifact_scan: bool = True,
              free_target: int = 0, budget: PassBudget | None = None,
              cursor=None, on_cursor=None, units_dir: str | None = None) -> dict:
    """Un passage de ménage sur CET hôte ; rend le rapport.

    * dossiers temporaires : quota par agent (sauf agent en tour) ; dossier
      d'un agent absent du registre supprimé ;
    * caches partagés : éviction jusqu'au quota, seulement si aucun tour ne
      tourne (`busy` faux) — sauf le cache npm pendant un tour, quand aucun
      npm ne tourne et que rien ne le tient (#3293) ; sous pression disque
      (`free_target`, espace libre à préserver), l'éviction libère en plus le
      déficit ;
    * worktrees suivis dont le lot est fini : retirés s'ils sont propres et
      intégrés (garde-fous de `_worktree_pass`), sinon gardés et signalés ;
      #1665 : un worktree orphelin (aucun lot nommé, ou lot fini sans
      intégration) est retiré après 24 h d'inactivité s'il est propre et
      poussé et qu'aucun agent ne l'utilise ;
    * #2743 : clones ad hoc rattachés à un lot fini depuis plus de 24 h
      (`housekeeping_clones`), retirés sous les garde-fous de ce module ;
    * L230: regenerable build dirs of idle worktrees (`build_artifacts`),
      when `artifact_scan` (the runner: at most every `SCAN_EVERY_S`);
    * conteneurs des tours d'ameesh finis (L73, `reap_containers`) ;
    * volumes Docker sans conteneur (L73/#2003, `reap_volumes`) : ceux que le
      mesh a déclarés jetables (`ameesh.bench=disposable`) sont récoltés après
      `docker_volume_idle` d'absence continue, les autres signalés avec leur
      commande, jamais supprimés ;
    * scopes systemd `dsh-subprocess-*` d'un tour clos (#3226,
      `reap_dsh_scopes`), tués après `DSH_SCOPE_GRACE_S` ; non attribuables :
      gardés et signalés ;
    * entrées de /tmp signalées pendant un tour : jamais supprimées si non
      attribuées (#222) ; un signalement attribué d'avant #222, supprimé si
      abandonné (garde-fous de `_orphan_verdict`, L162) ; sinon dites avec la
      raison ;
    * bilan (`mesure`), avec les plus grosses entrées de /tmp si le tmpfs
      atteint l'alerte.

    #3802 : `budget` borne chaque phase ; le prochain point de contrôle lève
    `_PassExpired`, que `run_pass` rend en rapport `timed_out` (avec ce que
    le passage avait déjà fait, par `budget.context`).

    `dry_run` : rien n'est supprimé ni écrit en base ; le plan est rendu."""
    now = time.time() if now is None else float(now)
    agents_in_turn = set(agents_in_turn or ())
    entries: list[dict] = []
    plan: list[dict] = []
    skipped: list[str] = []
    unowned: list[dict] = []
    if disabled():
        return {"host": host, "dry_run": dry_run, "disabled": True, "entries": [],
                "plan": [], "skipped": ["AMEESH_HOUSEKEEPING=off"], "unowned": [],
                "measure": None}
    if budget is None:
        budget = PassBudget(HOST_PASS_BUDGET_S)
    # #3802 (B1) : réserve des phases qui suivent les worktrees ; jamais plus
    # du tiers d'un budget court (un appelant peut borner la passe à moins).
    reserve = min(HOST_PASS_RESERVE_S, max(0.0, budget.budget / 3.0))
    budget.context = {"entries": entries, "plan": plan, "skipped": skipped,
                      "unowned": unowned, "orphans": []}
    budget.check("registry")
    troot, croot = tmp_root(cfg, policy), cache_root(cfg, policy)
    registered: set[str] = set()
    protected: set[str] = set()
    cwd_of: dict[str, str] = {}
    agent_rows: list[dict] = []
    running: set[str] = set(agents_in_turn)
    try:
        agent_rows = list(storage.of(db).operations.listing())
        for row in agent_rows:
            registered.add(row["name"])
            if row.get("cwd"):
                protected.add(os.path.abspath(os.path.expanduser(row["cwd"])))
                cwd_of[row["name"]] = os.path.abspath(os.path.expanduser(row["cwd"]))
            if row.get("status") == "running" and row.get("lease_live"):
                running.add(row["name"])
    except Exception:  # registre illisible : on ne supprime aucun dossier d'agent
        registered = None  # type: ignore[assignment]
    budget.check("tmp")
    # 1. dossiers temporaires des agents
    key = mesh_key(cfg)
    # #2704 : un seul relevé des processus de l'utilisateur par passage (il
    # lit /proc/*/maps : ~200 ms) ; False quand la lecture est impossible.
    procs: object = None

    def held_by_user(path: str) -> str:
        nonlocal procs
        if procs is None:
            try:
                procs = platform.user_paths()
            except Exception:  # noqa: BLE001 - un passage de ménage ne lève jamais
                procs = False
        return tmp_user_holder(path, procs)

    # #2493 : dernier signalement d'un dossier sans propriétaire, pour ne pas
    # le répéter dans `skipped` à chaque passe (un constat persistant suffit).
    last_unowned: dict[str, dict] = {}
    # #3277 : dernier constat `over_quota` d'un dossier qu'un autre mesh (ou
    # une ancienne clé de celui-ci) possède, levé par `resolved` ou `gone`
    last_over: dict[str, dict] = {}
    try:
        for row in storage.of(db).housekeeping.latest_by_path(host, "tmp"):
            if row.get("action") == "unowned":
                last_unowned[row["path"]] = row
            elif row.get("action") == "over_quota":
                last_over[row["path"]] = row
    except Exception:  # journal illisible : on signale, sans doublon garanti
        last_unowned, last_over = {}, {}
    seen_unowned: set[str] = set()
    seen_over: set[str] = set()
    if os.path.isdir(troot) and _managed(troot):
        for name in sorted(os.listdir(troot)):
            if budget.expired():
                budget.timed_out = True
                skipped.append("tmp: pass budget reached, remaining temporary "
                               "directories left for the next pass")
                break
            full = os.path.join(troot, name)
            if name == MARKER or not os.path.isdir(full) or os.path.islink(full):
                continue
            owner = read_owner(troot, name)
            if owner is not None and owner != key:
                # L219: another mesh of this host (same state directory)
                # manages it: never deleted nor evicted here. #3277 : mais
                # mesuré, et signalé au-delà du quota ; une ancienne clé de CE
                # mesh (`former_mesh_keys`) range ses entrées de lots clos.
                if FALLBACK_SEP in name:
                    continue
                from . import housekeeping_stale
                seen_over.add(full)
                found = housekeeping_stale.check(
                    db, troot=troot, name=name, agent=tmp_agent(name, owner),
                    owner=owner, key=key, policy=policy, now=now, dry_run=dry_run,
                    previous=last_over.get(full), agents=sorted(registered or ()),
                    holder=held_by_user)
                entries += found["entries"]
                plan += found["plan"]
                skipped += found["skipped"]
                continue
            agent = tmp_agent(name, key)
            if registered is None and owner is None:
                continue
            # L219: an agent in turn here (`agents_in_turn`) or with a live
            # lease (another runner, a CLI) keeps its directory, registered
            # or not
            if agent in running:
                if registered is not None and agent not in registered:
                    skipped.append("tmp %s kept: in turn" % full)
                continue
            # a fallback directory (`hold_tmp`) served one turn: deleted once
            # idle, like the directory of an agent absent from the registry
            orphan = FALLBACK_SEP in name or (registered is not None
                                              and agent not in registered)
            # #2326 : la suppression ne vise que ce qui est À NOUS. Un dossier
            # sans fichier de propriétaire de ce mesh (worktree créé par un
            # agent à la racine partagée, dossier d'un autre mesh d'avant L219,
            # nom inconnu) n'est jamais supprimé, même ancien : un registre
            # vide, partiel ou ancien ne doit rien détruire.
            if orphan and owner != key:
                # #2493 (N1) : ce dossier n'est jamais supprimé (#2326) ; on
                # ne le répète plus dans `skipped` à chaque passe. Un constat
                # unique le suit, re-signalé seulement s'il change ou après
                # UNOWNED_REFRESH_S, et l'alerte `tmp_unowned` le reprend.
                seen_unowned.add(full)
                previous = last_unowned.get(full)
                data = (previous or {}).get("data") or {}
                try:
                    st = os.stat(full)
                    ino, dev, mtime = st.st_ino, st.st_dev, st.st_mtime
                except OSError:
                    ino = dev = mtime = None
                changed = (previous is None or data.get("ino") != ino
                           or data.get("dev") != dev or data.get("mtime") != mtime)
                # #2493 : ne pas parcourir un dossier inchangé à chaque passe ;
                # sa taille journalisée suffit tant que rien ne le modifie.
                size = (size_of(full) if changed or previous.get("bytes") is None
                        else int(previous["bytes"]))
                first_seen = data.get("first_seen") or now
                # #2556 : la commande d'inspection est la même pour l'entrée
                # journalisée et pour la liste ; un seul parcours des copies.
                command = tmp_recovery_command(full)
                if changed or now - float((previous or {}).get("at_ts") or 0) \
                        >= UNOWNED_REFRESH_S:
                    entries.append({
                        "kind": "tmp", "action": "unowned", "path": full,
                        "bytes": size, "agent": agent,
                        "detail": "no owner file of this mesh: never deleted; "
                                  "inspect with the command, then move it or "
                                  "remove it manually once its work is saved",
                        "data": {"ino": ino, "dev": dev, "mtime": mtime,
                                 "first_seen": first_seen, "command": command}})
                unowned.append({"path": full, "bytes": size, "agent": agent,
                                "first_seen": first_seen,
                                "command": command})
                continue
            why = tmp_problem(full, owner, key, registered=not orphan,
                              idle_after=policy.orphan_idle, now=now, budget=budget)
            if why:
                skipped.append("tmp %s kept: %s" % (full, why))
                continue
            # L219: a turn holds the lock (shared) from its start to its end;
            # everything is checked again under the exclusive lock, right
            # before deleting or evicting
            lock = None if dry_run else tmp_lock(troot, name, exclusive=True)
            if not dry_run and lock is None:
                skipped.append("tmp %s kept: in turn (lock held)" % full)
                continue
            try:
                if not dry_run and (read_owner(troot, name) != owner or tmp_problem(
                        full, owner, key, registered=not orphan,
                        idle_after=policy.orphan_idle, now=now, budget=budget)):
                    skipped.append("tmp %s kept: changed during the pass" % full)
                    continue
                if orphan:
                    # #2704 : un dossier utilisé par un processus vivant
                    # (AppImage extraite et lancée hors tour) n'est jamais
                    # supprimé, même ancien et sans copie git : ses fichiers
                    # projetés en mémoire (/proc/<pid>/maps) le trahissent.
                    held = held_by_user(full)
                    if held:
                        skipped.append("tmp %s kept: %s" % (full, held))
                        continue
                    size = size_of(full)          # avant tout retrait : total libéré
                    # #2493 (N2) : un dossier qui porte une copie git n'est
                    # supprimé qu'après la preuve qu'aucun travail n'y est à
                    # garder (propre, poussé ou intégré, sans verrou, sans
                    # sous-module, sans processus dedans) ; les worktrees sont
                    # retirés par `git worktree remove`, jamais `--force`.
                    # Sinon il reste, avec la raison et la commande à lancer.
                    git_repos: list[str] = []
                    copies, doubt = _git_copies_report(full)
                    if doubt:
                        skipped.append("tmp %s kept: git copies not fully inspected "
                                       "— %s" % (full, doubt))
                        continue
                    if copies:
                        work = tmp_git_work(
                            full, build_artifacts.rules_of(policy.artifacts),
                            report=(copies, doubt))
                        if work:
                            command = tmp_recovery_command(full, report=(copies, doubt))
                            skipped.append("tmp %s kept: git work — %s ; inspect "
                                           "with: %s" % (full, work, command))
                            continue
                        if not dry_run:
                            ok, error, git_repos = recover_tmp_git_copies(
                                full, report=(copies, doubt))
                            if not ok:
                                skipped.append("tmp %s kept: git worktree removal "
                                               "refused — %s" % (full, error))
                                continue
                    plan.append({"kind": "tmp", "path": full, "agent": agent,
                                 "action": "suppression", "bytes": size,
                                 "detail": "agent not in the registry"})
                    if not dry_run:
                        _safe_rmtree(full, troot)
                        for repo in git_repos:
                            prune_worktrees(repo)
                        for leftover in (owner_file(troot, name), lock_file(troot, name)):
                            try:
                                os.unlink(leftover)
                            except OSError:
                                pass
                        entries.append({"kind": "tmp", "action": "deleted", "path": full,
                                        "bytes": size, "agent": agent,
                                        "detail": "agent not in the registry"
                                        + ("; git worktrees recovered"
                                           if git_repos else "")})
                    continue
                # #2704 : l'éviction ne touche pas un dossier qu'un processus
                # vivant utilise (cwd, fichiers ouverts ou projetés en
                # mémoire) : une AppImage extraite y tourne hors tour, et
                # libérer son quota la fait crasher. Le quota reste dit par le
                # relevé tmpfs ; on ne supprime pas sous le processus.
                held = held_by_user(full)
                if held:
                    skipped.append("tmp %s kept: %s (no eviction)" % (full, held))
                    entries.append({"kind": "tmp", "action": "held", "path": full,
                                    "bytes": size_of(full), "agent": agent,
                                    "detail": held})
                    continue
                result = evict_lru([full], troot, policy.tmp_quota, now=now,
                                   dry_run=dry_run, budget=budget)
                episode = tmp_episode(db, host, full, result, now=now)
                if result["files"]:
                    plan.append({"kind": "tmp", "path": full, "agent": agent,
                                 "action": "éviction", "bytes": result["evicted"],
                                 "detail": "%d file(s), quota %s" % (
                                     result["files"],
                                     resources_mod.fmt_value("q", policy.tmp_quota))})
                if result.get("mounts_unreadable"):
                    skipped.append("tmp %s: mount table unreadable "
                                   "(platform layer), nothing evicted" % full)
                elif result["truncated"]:
                    skipped.append("tmp %s: listing truncated (%s), nothing evicted"
                                   % (full, "pass budget reached" if result.get("budget_reached")
                                      else "%s file limit" % WALK_LIMIT))
                elif result["blocked"]:
                    skipped.append("tmp %s: quota held by %s this pass cannot remove, "
                                   "nothing evicted" % (full, resources_mod.fmt_value(
                                       "q", result["unremovable"])))
                for tree in result["worktrees"]:
                    skipped.append("tmp %s: git work tree %s left out of the eviction"
                                   % (full, tree))
                for link in result["worktree_symlinks"]:
                    skipped.append("tmp %s: git marker `.git` of %s is a symbolic link, "
                                   "left out of the eviction" % (full, link))
                if not dry_run:
                    entries.extend(eviction_entries(
                        result, kind="tmp", path=full, quota=policy.tmp_quota, agent=agent,
                        since=episode["since"], resolved=episode["resolved"]))
            finally:
                if lock is not None:
                    lock.close()
        # #2493 : un constat sans propriétaire dont le dossier a disparu est
        # clos (l'alerte `tmp_unowned` s'arrête), comme une entrée de /tmp.
        if not dry_run:
            for path in last_unowned:
                if path in seen_unowned or os.path.lexists(path):
                    continue
                entries.append({"kind": "tmp", "action": "gone", "path": path,
                                "detail": "unowned temporary directory gone"})
            for path in last_over:
                if path in seen_over or os.path.lexists(path):
                    continue
                entries.append({"kind": "tmp", "action": "gone", "path": path,
                                "detail": "temporary directory over quota gone"})
    budget.check("caches")
    # 2. caches partagés
    # #3293 : sous pression disque (moins que `free_target` libre sur le
    # système du cache), libérer au moins le déficit — et, pour le cache npm
    # (copies, pas un store à liens durs), sans attendre qu'aucun tour ne
    # tourne : il suffit qu'aucun npm ne tourne et que rien ne le tienne.
    deficit = 0
    free = None
    if free_target > 0 and os.path.isdir(croot):
        try:
            free = shutil.disk_usage(croot).free
        except OSError:
            free = None
        if free is not None and free < int(free_target):
            deficit = int(free_target) - int(free)
    directories: list[str] = []
    if os.path.isdir(croot) and _managed(croot):
        managers = None
        busy_said = False
        for name, _variables in cache_names(policy):
            full = os.path.join(croot, name)
            if not os.path.isdir(full) or os.path.islink(full):
                continue
            if name == "npm":
                if managers is None:
                    managers = package_managers_running()
                if "npm" in managers:
                    skipped.append("cache npm kept: npm is running on the host")
                    continue
                held = held_by_user(full)
                if held:
                    skipped.append("cache npm kept: %s (no eviction)" % held)
                    continue
                directories.append(full)
            elif busy:
                if not busy_said:
                    skipped.append("caches: a turn is in progress on the host, "
                                   "eviction postponed")
                    busy_said = True
            else:
                directories.append(full)
        if directories:
            result = evict_lru(directories, croot, policy.cache_quota, now=now,
                               dry_run=dry_run, evict_at_least=deficit, budget=budget)
            detail = "%d file(s), quota %s" % (
                result["files"], resources_mod.fmt_value("q", policy.cache_quota))
            if deficit:
                detail += ", %s freed under disk pressure (%s needed)" % (
                    resources_mod.fmt_value("q", result["evicted"]),
                    resources_mod.fmt_value("q", deficit))
            if result["files"]:
                plan.append({"kind": "cache", "path": croot, "action": "éviction",
                             "bytes": result["evicted"], "detail": detail})
                if not dry_run:
                    entries.append({"kind": "cache", "action": "evicted", "path": croot,
                                    "bytes": result["evicted"],
                                    "detail": "quota %s: %d oldest file(s) evicted%s"
                                              % (resources_mod.fmt_value("q", policy.cache_quota),
                                                 result["files"],
                                                 " (disk pressure)" if deficit else "")})
            if result.get("mounts_unreadable"):
                skipped.append("caches: mount table unreadable (platform layer), "
                               "nothing evicted")
            elif result["truncated"]:
                skipped.append("caches: listing truncated (%s), nothing evicted"
                               % ("pass budget reached" if result.get("budget_reached")
                                  else "%s file limit" % WALK_LIMIT))
            elif result["blocked"]:
                skipped.append("caches: quota held by %s this pass cannot remove, nothing evicted"
                               % resources_mod.fmt_value("q", result["unremovable"]))
            elif result["over_quota"]:
                skipped.append("caches: quota still exceeded (files less than an hour old)")
            if deficit and not (result.get("mounts_unreadable") or result["truncated"]
                                or result["evicted"] >= deficit):
                # ce qui reste libre après l'éviction, pour dire l'écart
                left = (free + result["evicted"]) if free is not None else 0
                skipped.append("caches: disk pressure persists (%s free under %s target), "
                               "%s freed" % (resources_mod.fmt_value("q", left),
                                             resources_mod.fmt_value("q", int(free_target)),
                                             resources_mod.fmt_value("q", result["evicted"])))
    budget.check("deps")
    # 2b. #248: node_modules shared per lockfile, entries no worktree links to
    if policy.deps and registered is None:
        skipped.append("deps: registry unreadable, worktrees unknown, nothing deleted")
    elif policy.deps and os.path.isdir(croot) and _managed(croot):
        from . import deps as deps_mod
        def known_worktrees() -> list[str]:
            # every worktree of the agents' repositories: links nobody
            # recorded are found there before an entry is deleted
            tops: set = set()
            for cwd in sorted(protected):
                if budget.expired() or budget.reserved(reserve):
                    break              # #3802: a bounded host pass
                tops.update(w["path"] for w in list_worktrees(cwd, budget=budget)
                            if not w.get("bare"))
            return sorted(tops | protected)

        for line in deps_mod.gc_node_modules(croot, now=now, idle=policy.deps_idle,
                                             dry_run=dry_run, worktrees=known_worktrees,
                                             budget=budget,
                                             reserve=reserve):
            if line["action"] == "would-delete":
                plan.append({"kind": "deps", "path": line["path"], "action": "suppression",
                             "bytes": line["bytes"], "detail": line["detail"]})
            elif line["action"] == "kept":
                skipped.append("deps %s kept: %s" % (line["path"], line["detail"]))
            else:
                entries.append(line)
    budget.check("archive")
    # L230: one deleting pass at a time on the host, and the journal of its trash dirs
    lock_path = os.path.join(cfg.state_dir, MANAGED_DIR, "artifacts.lock")
    journal_path = os.path.join(cfg.state_dir, MANAGED_DIR, "artifacts.journal")
    #: #3396 : worktrees de lots finis qu'un doute sur les processus retient
    wt_blocked: dict = {}
    # #3728 : archives des worktrees gardés pour fichiers modifiés (les deux
    # durées de la fiche Host déclarées, sinon aucun archivage) ; celles dont
    # la conservation est échue partent à chaque passage, politique ou non
    from . import housekeeping_archive
    archive_root = housekeeping_archive.root_of(cfg.state_dir, MANAGED_DIR)
    archive = {"after": policy.dirty_worktree_after,
               "retention": policy.dirty_archive_retention, "root": archive_root} \
        if policy.dirty_worktree_after and policy.dirty_archive_retention else None
    for line in housekeeping_archive.prune(archive_root, now=now, dry_run=dry_run):
        if dry_run:
            plan.append(dict(line, action="suppression"))
        else:
            entries.append(line)
    budget.check("worktrees")
    # #4234 : la phase worktrees a son propre plafond, pour que le verrou
    # host-pass ne reste pas tenu ~8 min sur un hôte à ~480 arbres (mesuré
    # sur ameesh-agents-1) ; le curseur reprend au passage suivant.
    worktrees_budget = budget.slice(min(
        HOST_PASS_WORKTREES_BUDGET_S, max(0.0, budget.remaining() - reserve)))
    # 3. worktrees
    if policy.worktrees and registered is None:
        # sans registre, ni dossiers d'agents ni tours en cours connus
        skipped.append("worktrees: registry unreadable, no removal")
    elif policy.worktrees:
        if artifact_scan:
            from . import housekeeping_discovery
            local = {row["name"]: row["cwd"] for row in agent_rows
                     if row.get("host") == host and row.get("cwd")}
            found = housekeeping_discovery.discover(
                db, host, local, policy.worktree_roots, dry_run=dry_run,
                budget=worktrees_budget)
            entries += found["entries"]
            plan += found["plan"]
            skipped += found["skipped"]
        wt_entries, wt_plan = _worktree_pass(db, host, dry_run=dry_run,
                                             protected=protected, now=now, running=running,
                                             artifacts=policy.artifacts,
                                             allow=policy.unreadable_ok, lock_path=lock_path,
                                             journal_path=journal_path,
                                             skip_roots=(troot, croot, cfg.state_dir)
                                             + _shared_roots(),
                                             known_agents=registered or (),
                                             blocked=wt_blocked, archive=archive,
                                             budget=worktrees_budget,
                                             # la tranche porte déjà la réserve
                                             reserve_s=0.0,
                                             cursor=cursor, on_cursor=on_cursor)
        entries += wt_entries
        plan += wt_plan
        budget.check("clones")
        # 3c. #2743: ad-hoc clones of ended lots (dry run by default);
        #     like the discovery, the runner rescans at most every 6 h.
        if artifact_scan:
            from . import housekeeping_clones
            try:
                cl_entries, cl_plan, cl_skipped = housekeeping_clones.reap(
                    db, host, cfg, roots=policy.worktree_roots, agents=cwd_of,
                    protected=protected, dry_run=dry_run, now=now,
                    artifacts=policy.artifacts, allow=policy.unreadable_ok,
                    lock_path=lock_path, journal_path=journal_path,
                    skip_roots=(troot, croot, cfg.state_dir) + _shared_roots(),
                    budget_s=max(0.0, budget.remaining() - reserve))
            except Exception as exc:  # never fatal for the pass
                cl_entries, cl_plan, cl_skipped = [], [], [
                    "clones: pass failed (%s)" % type(exc).__name__]
            entries += cl_entries
            plan += cl_plan
            skipped += cl_skipped
    budget.check("artifacts")
    # 3b. L230: regenerable build dirs of idle worktrees
    if artifact_scan and registered is not None:
        try:
            art = _artifact_pass(db, host, policy, cwd_of=cwd_of, dry_run=dry_run, now=now,
                                 skip_roots=(troot, croot, cfg.state_dir),
                                 lock_path=lock_path, journal_path=journal_path,
                                 running=running, known_agents=registered,
                                 budget=budget, reserve_s=reserve)
        except Exception as exc:  # jamais fatal pour le passage
            art = {"entries": [], "plan": [], "skipped": [
                "artifacts: pass impossible (%s)" % type(exc).__name__]}
        entries += art["entries"]
        plan += art["plan"]
        skipped += art["skipped"]
    budget.check("containers")
    # 4. conteneurs : ceux des tours d'ameesh finis supprimés, autres signalés
    conteneurs = reap_containers(runtime, db, host, dry_run=dry_run, budget=budget)
    entries += conteneurs["entries"]
    plan += conteneurs["plan"]
    # 4b. #2003 : volumes Docker sans conteneur — ceux que le mesh a déclarés
    #     jetables (`ameesh.bench=disposable`, banc de lot) récoltés après
    #     `docker_volume_idle` d'absence continue ; les autres signalés,
    #     jamais supprimés
    budget.check("volumes")
    volumes = reap_volumes(runtime, db, host, policy, dry_run=dry_run, now=now,
                           budget=budget)
    entries += volumes["entries"]
    plan += volumes["plan"]
    # 4b-bis. #4560 : images Docker sans tag — celles que le mesh a déclarées
    #     jetables (`ameesh.lifetime=test`, construction d'un tour) récoltées
    #     après le même délai d'absence continue ; les images taguées et les
    #     images sans étiquette ne sont jamais supprimées.
    budget.check("images")
    images = reap_images(runtime, db, host, policy, dry_run=dry_run, now=now,
                         budget=budget)
    entries += images["entries"]
    plan += images["plan"]
    # 4c. #3226 : scopes systemd `dsh-subprocess-*` laissés par un tour clos
    #     (dsh sans la garde #490) : tués après le délai de grâce ; ceux qu'on
    #     ne sait pas attribuer gardés et signalés
    budget.check("scopes")
    try:
        scopes = reap_dsh_scopes(db, host, dry_run=dry_run, now=now, budget=budget)
    except Exception as exc:  # jamais fatal pour le passage
        scopes = {"entries": [], "plan": []}
        skipped.append("dsh scopes: pass failed (%s)" % type(exc).__name__)
    entries += scopes["entries"]
    plan += scopes["plan"]
    # 5. /tmp entries signaled during a turn (L162): unattributed ones are
    #    never deleted (#222), an attributed one (before #222) is deleted once
    #    abandoned (`_orphan_verdict` guards), the others are told
    budget.check("orphans")
    gardes = protected | {troot, croot, cfg.state_dir}
    try:
        orphan_entries, orphan_plan, orphans = orphan_pass(
            db, host, policy, registered=registered, protected=gardes, running=running,
            dry_run=dry_run, now=now, budget=budget)
    except Exception as exc:  # jamais fatal pour le passage
        orphan_entries, orphan_plan, orphans = [], [], []
        skipped.append("/tmp: pass failed (%s)" % type(exc).__name__)
    entries += orphan_entries
    plan += orphan_plan
    skipped += declared_root_refused(cfg, policy)
    budget.context["orphans"] = orphans
    # 6. #4754 : unités de veille et de réveil qui nomment un venv versionné
    #    (retiré par le ménage des venv : 203/EXEC à chaque tir). Seulement
    #    quand l'appelant nomme le dossier des unités (exécuteur, CLI).
    if units_dir:
        budget.check("units")
        from . import watch
        try:
            repinned = watch.repin_units(units_dir, dry_run=dry_run)
        except Exception as exc:  # jamais fatal pour le passage
            repinned = []
            skipped.append("watch units: pass failed (%s)" % type(exc).__name__)
        for fix in repinned:
            entries.append({
                "kind": "unit", "action": "planned" if dry_run else "repinned",
                "path": fix["path"], "bytes": 0,
                "detail": "ExecStart %s -> %s" % (", ".join(fix["before"]),
                                                  ", ".join(fix["after"])),
                "data": {"before": fix["before"], "after": fix["after"]}})
    budget.check("measure")
    report_measure = measure(cfg, policy, budget=budget)
    # #3802 : la trace des phases reste au journal (relevé `mesure`), pas
    # seulement dans la sortie de cette passe.
    report_measure["host_pass"] = budget.summary()
    report_measure["worktree_archives"] = dict(
        housekeeping_archive.survey(archive_root),
        after=policy.dirty_worktree_after, retention=policy.dirty_archive_retention)
    # les plus grosses entrées de /tmp, avec ce que le ménage en fera
    sorts = {o["path"]: o for o in orphans}
    for top in report_measure.get("system_tmp_top") or ():
        known = sorts.get(top["path"])
        if known is not None:
            top.update(agent=known.get("agent"), status=known.get("status"),
                       detail=known.get("detail"))
        top["command"] = "rm -rf -- %s" % shlex.quote(top["path"])
    report_measure["containers"] = {
        "ameesh": conteneurs["ameesh"], "others": conteneurs["others"],
        "others_total": conteneurs["others_total"]}
    report_measure["volumes"] = {
        "ameesh": volumes["ours"], "others": volumes["others"],
        "others_total": volumes["others_total"]}
    report_measure["images"] = {
        "ameesh": images["ours"], "others_total": images["others_total"]}
    from . import docker_storage
    report_measure["docker_storage"] = docker_storage.collect(runtime, now=now)
    # #3103 : un pont sur le réseau local de l'hôte (incident #3092), relevé
    # pour l'alerte critique `host_network_overlap` ; jamais fatal au passage
    from . import netcheck
    try:
        report_measure["docker_network"] = (
            None if runtime is None else netcheck.collect(cfg, now=now, runtime=runtime))
    except Exception as exc:
        report_measure["docker_network"] = None
        skipped.append("docker networks: not read (%s)" % type(exc).__name__)
    if wt_blocked:
        # #3396 : le doute tient des worktrees de lots finis. Le relevé le dit,
        # et l'épisode garde son début d'un passage à l'autre (alerte
        # `housekeeping_blocked` après HOUSEKEEPING_BLOCKED_S).
        previous = storage.of(db).housekeeping.last_measure(host)
        old = ((previous[0].get("data") or {}).get("worktrees_blocked")
               if previous else None)
        since = float(old.get("since")) if old and old.get("since") else now
        report_measure["worktrees_blocked"] = dict(wt_blocked, since=since)
    if not dry_run:
        entries.append({"kind": "mesure", "action": "measured", "path": troot,
                        "bytes": report_measure["tmp_bytes"] + report_measure["cache_bytes"],
                        "detail": "housekeeping summary",
                        "data": dict(report_measure, orphans=len(orphans),
                                     orphan_bytes=sum(o["bytes"] for o in orphans))})
        storage.of(db).housekeeping.log(host, entries, actor=actor)
    return {"host": host, "dry_run": dry_run, "entries": entries, "plan": plan,
            "skipped": skipped, "orphans": orphans, "unowned": unowned,
            "measure": report_measure,
            "containers": report_measure["containers"],
            "volumes": report_measure["volumes"],
            "images": report_measure["images"],
            "policy": policy.as_dict()}


# --------------------------------------------------------------------------
# CLI : ameesh menage [--dry-run | --apply] [--json]
# --------------------------------------------------------------------------

def caller_refusal(env=None) -> str | None:
    """Pourquoi l'appelant ne peut pas déclencher un ménage, ou None.

    Réservé à un humain habilité (session sans identité d'agent, utilisateur
    système qui possède les dossiers gérés) ou à l'exécuteur (qui appelle
    `run_pass` directement, sans passer par la CLI). Une session d'agent —
    lancée par l'exécuteur ou liée — ne déclenche jamais de suppression."""
    env = env if env is not None else os.environ
    for name in ("AMEESH_TURN_ID", "AGENT_MAIL_NAME"):
        if env.get(name):
            return ("agent session (%s set): housekeeping is reserved to a human or "
                    "the runner" % name)
    return None


#: L232: English labels of the plan actions and verdicts, for display only
#: (the values themselves stay as stored and as in `--json`)
ACTION_LABELS = {"attend": "waiting", "garde": "kept", "suppression": "delete",
                 "retrait": "remove", "disparu": "gone", "signalé": "flagged",
                 "éviction": "evict", "archivage": "archive then remove"}


def action_label(action) -> str:
    return ACTION_LABELS.get(action, action)


def _fmt(value) -> str:
    return resources_mod.fmt_value("bytes", value)


def print_report(report: dict) -> None:
    # #2173: a pass skipped by the host lock ran nothing; it must not print
    # "executed" (nor "nothing to do": the concurrent pass is doing the work).
    skipped_pass = report.get("status") == "skipped"
    timed_out = report.get("status") == "timed_out"
    if skipped_pass:
        reason = next(iter(report.get("skipped") or ()), "")
        mode = "skipped" + (" — %s" % reason if reason else "")
    elif timed_out:
        reason = next((note for note in report.get("skipped") or ()
                       if "budget" in note), "")
        mode = "TIMED OUT" + (" — %s" % reason if reason else "")
    else:
        mode = "DRY RUN (nothing is deleted; --apply to execute)" if report["dry_run"] \
            else "executed"
    print("housekeeping of %s — %s" % (report["host"] or "?", mode))
    blocked = report.get("blocked")
    if blocked:
        # #3802 : a skipped pass past the holder's budget names it here and
        # raises `housekeeping_blocked` (the journal line is already written).
        print("  held       by %s (pid %s) for %s%s — alert housekeeping_blocked"
              % (blocked.get("runner") or "?",
                 blocked.get("pid") if blocked.get("pid") is not None else "?",
                 fmt_idle(blocked.get("age_s") or 0),
                 " in phase %r" % blocked.get("phase") if blocked.get("phase") else ""))
    m = report.get("measure") or {}
    if m:
        print("  temporary dirs        %s (%s); quota %s per agent"
              % (_fmt(m["tmp_bytes"]), m["tmp_root"], _fmt(m["tmp_quota"])))
        print("  shared caches         %s / %s (%s)"
              % (_fmt(m["cache_bytes"]), _fmt(m["cache_quota"]), m["cache_root"]))
        sys_tmp = m.get("system_tmp") or {}
        if sys_tmp.get("tmp_size_bytes"):
            print("  %-21s %s / %s (%s)%s" % (
                sys_tmp.get("tmp_path"), _fmt(sys_tmp.get("tmp_used_bytes")),
                _fmt(sys_tmp.get("tmp_size_bytes")), sys_tmp.get("tmp_fstype") or "?",
                " — ALERT: above %d %%" % round(m["tmpfs_alert"] * 100)
                if (m.get("system_tmp_fraction") or 0) >= m["tmpfs_alert"] else ""))
    # en essai, les entrées de /tmp ont leur propre section (avec la commande)
    rows = [p for p in report["plan"] if p.get("kind") != "orphan"
            and p.get("action") != "unowned"] if report["dry_run"] \
        else [e for e in report["entries"] if e["kind"] != "mesure"
              and e.get("action") != "unowned"]
    if not rows and not skipped_pass:
        print("  nothing to do")
    for row in rows:
        print("  %-9s %-12s %-10s %s%s" % (
            row.get("kind", ""), action_label(row.get("action", "")),
            _fmt(row["bytes"]) if row.get("bytes") is not None else "",
            row.get("path", ""), " — %s" % row["detail"] if row.get("detail") else ""))
    # #2493 : les dossiers sans propriétaire ne sont ni supprimés ni répétés
    # dans `skipped` ; une section stable dit quoi inspecter.
    unowned = report.get("unowned") or ()
    if unowned:
        print("  unowned   temporary dirs without owner file (never deleted; "
              "inspect before any manual move):")
        for row in unowned:
            print("            %-10s %s — %s" % (
                _fmt(row.get("bytes")), row.get("path"), row.get("command")))
    if not skipped_pass:  # #2173: the reason is already in the banner, once
        for note in report.get("skipped") or ():
            print("  skipped   %s" % note)
    conteneurs = report.get("containers") or {}
    volumes = report.get("volumes") or {}
    images = report.get("images") or {}
    docker = (report.get("measure") or {}).get("docker_storage")
    if docker:
        print("  Docker storage (orphan count reported; only volumes and images this mesh "
              "DECLARED DISPOSABLE are reaped, after a measured idle delay): "
              "%s orphan volumes; %s private reclaimable build cache" % (
                  docker.get("orphan_volumes"), _fmt(docker.get("build_cache_reclaimable_bytes"))))
        if docker.get("build_cache_unsupported"):
            print("    build cache not measured: the docker buildx plugin is absent "
                  "(finding, not an alert)")
        for error in docker.get("errors") or ():
            print("    %s" % error)
    for c in conteneurs.get("ameesh") or ():
        if c["action"] != "suppression":
            print("  container %-12s %s — %s" % (action_label(c["action"]), c["path"],
                                                 c.get("detail") or ""))
    vus = {row.get("path") for row in rows}
    for v in volumes.get("ameesh") or ():
        # les états déjà listés dans `plan` (essai) ou `entries` (réalisation)
        # ne sont pas répétés ici : la section dit ce qui reste (attente,
        # signalement sans suppression)
        if v["path"] in vus:
            continue
        print("  volume %-12s %s — %s" % (action_label(v["action"]), v["path"],
                                          v.get("detail") or ""))
    for v in images.get("ameesh") or ():
        if v["path"] in vus:
            continue
        print("  image %-13s %s — %s" % (action_label(v["action"]), v["path"][:19],
                                         v.get("detail") or ""))
    if images.get("others_total"):
        print("    … %d untagged image(s) without the lifetime label "
              "(never deleted by ameesh)" % images["others_total"])
    autres = conteneurs.get("others") or []
    if autres:
        print("  containers not started by an ameesh turn (never deleted by ameesh):")
        for c in autres:
            print("    %s  %s  (%s)  →  %s" % (c.get("name") or c.get("id"), c.get("image"),
                                              c.get("status") or "?", c["command"]))
    reste = (conteneurs.get("others_total") or len(autres)) - len(autres)
    if reste > 0:
        print("    … %d more not listed (report bounded to %d)" % (reste, OTHERS_TOP))
    volumes_autres = volumes.get("others") or []
    if volumes_autres:
        print("  Docker volumes without a container, not declared disposable by this mesh "
              "(never deleted by ameesh):")
        for v in volumes_autres:
            print("    %s  (%s)  →  %s" % (v.get("path"), v.get("detail") or "?",
                                           v.get("command") or ""))
        reste = (volumes.get("others_total") or len(volumes_autres)) - len(volumes_autres)
        if reste > 0:
            print("    … %d more not listed (report bounded to %d)" % (reste, VOLUMES_TOP))
    orphans = report.get("orphans") or []
    if orphans:
        print("  /tmp entries that appeared during a turn (never deleted by ameesh, except an "
              "attributed signal from before #222 once abandoned):")
        for o in orphans:
            print("    %s  %s  (agent %s)  %s%s  →  %s" % (
                _fmt(o["bytes"]), o["path"], o.get("agent") or "?", action_label(o.get("status") or ""),
                " — %s" % o["detail"] if o.get("detail") else "", o["command"]))
    top = (report.get("measure") or {}).get("system_tmp_top") or []
    if top:
        print("  largest entries of %s:" % ((m.get("system_tmp") or {}).get("tmp_path")
                                             or "/tmp"))
        for e in top:
            if e.get("agent"):
                sort = ", left by %s: %s" % (e["agent"], action_label(e.get("status") or "?"))
            elif e.get("status"):   # #222: signaled during a turn, never charged to it
                sort = ", appeared during a turn, unattributed: to decide"
            else:
                sort = ", outside any agent turn: to decide"
            print("    %s  %s  (%s%s)  →  %s" % (
                _fmt(e.get("bytes")), e["path"], e.get("owner") or "?", sort,
                e.get("command") or ""))
    # #4234 : le volume des arbres de lots livrés gardés pour des fichiers
    # modifiés/non suivis, les plus gros en tête. Le relevé est borné ; il ne
    # supprime rien et ne remplace pas la politique d'archivage du Host.
    sizes = report.get("worktree_sizes") or {}
    if sizes.get("dirty"):
        print("  kept dirty worktrees by volume (biggest first; %d of %d measured "
              "in %ss):" % (sizes.get("measured") or 0, sizes["dirty"],
                            sizes.get("seconds") or 0))
        for row in sizes.get("top") or ():
            print("    %s%s  %s — %s" % (
                "≥ " if row.get("complete") is False else "",
                _fmt(row.get("bytes")), row.get("path"), row.get("detail") or ""))
        if sizes.get("unmeasured"):
            print("    … %d not measured this pass" % sizes["unmeasured"])
        print("    total measured %s; %s" % (
            _fmt(sizes.get("total_bytes")),
            "archive policy declared: kept until the archive grace"
            if sizes.get("archive_declared") else
            "archive policy NOT declared (Host card dirty_worktree_after / "
            "dirty_archive_retention): these trees stay"))


#: #4234 : temps maximal du relevé de volume des arbres gardés pour du travail
#: non commité (81 arbres, 7,2 Gio mesurés en 3,4 s sur agents-1) ; le relevé
#: est un affichage borné, jamais une condition de suppression.
WORKTREE_SIZE_BUDGET_S = 20.0
WORKTREE_SIZE_TOP = 10


def kept_worktree_sizes(plan, *, seconds: float = WORKTREE_SIZE_BUDGET_S) -> dict:
    """#4234 : tailles des worktrees gardés pour des fichiers modifiés, non
    suivis ou ignorés, les plus grosses d'abord.

    `plan` est le plan d'une passe en essai (les lignes `garde`/`attend` du
    kind `worktree`) ; chaque parcours est borné par le temps restant du
    relevé (20 s au total) et par 5 s pour un seul arbre. Rien n'est supprimé
    ni écrit : un arbre non mesuré est dit, jamais compté à zéro."""
    wanted = []
    for row in plan or ():
        detail = row.get("detail") or ""
        if (row.get("kind") == "worktree" and row.get("action") in ("garde", "attend")
                and ("modified or untracked" in detail
                     or "ignored file(s) outside build artifacts" in detail)):
            wanted.append(row)
    started = time.monotonic()
    deadline = started + max(0.0, float(seconds))
    measured = []
    for row in wanted:
        left = deadline - time.monotonic()
        if left <= 0:
            break
        size, complete = build_artifacts.size_of(
            row["path"], budget=build_artifacts.Budget(min(left, 5.0)))
        measured.append({"path": row.get("path"), "bytes": size,
                         "complete": complete, "detail": row.get("detail") or "",
                         "lot": row.get("lot")})
    top = sorted(measured, key=lambda item: item["bytes"], reverse=True)[:WORKTREE_SIZE_TOP]
    return {"dirty": len(wanted), "measured": len(measured),
            "unmeasured": len(wanted) - len(measured),
            "total_bytes": sum(item["bytes"] for item in measured),
            "seconds": round(time.monotonic() - started, 1),
            "top": top}


def cmd_housekeeping(cfg, args) -> int:
    """`ameesh menage [--dry-run | --apply] [--json] [--by NOM]`.

    Essai par défaut : supprimer est un acte, pas un effet de bord d'une
    commande de lecture — l'exécuteur fait déjà le ménage automatique ; la
    commande sert d'abord à voir, et `--apply` à déclencher."""
    from . import canon as canon_mod
    from . import db as db_mod
    import sys

    apply = bool(getattr(args, "apply", False)) and not getattr(args, "dry_run", False)
    json_mode = bool(getattr(args, "json", False))
    if apply:
        refusal = caller_refusal()
        if refusal:
            print("ameesh menage: refused — %s" % refusal, file=sys.stderr)
            return 1

    def progress(message: str) -> None:
        # #4234 : le rapport n'est imprimé qu'à la fin d'une passe qui dure
        # jusqu'à 15 min. En texte, une ligne de vie va sur stdout tout de
        # suite (une sortie capturée n'est plus vide) ; la progression des
        # phases va sur stderr pour ne jamais polluer le JSON.
        mode = "apply" if apply else "dry run"
        if message == "started":
            print("housekeeping of %s — started (%s)" % (cfg.host or "?", mode),
                  file=sys.stderr, flush=True)
            if not json_mode:
                print("housekeeping of %s — running (%s; budget up to %s)…"
                      % (cfg.host or "?", mode, fmt_idle(HOST_PASS_BUDGET_S)),
                      flush=True)
        elif message.startswith("phase:"):
            print("  housekeeping: phase %s" % message.split(":", 1)[1],
                  file=sys.stderr, flush=True)
        elif message.startswith("timed_out:"):
            print("  housekeeping: pass budget reached in phase %s"
                  % message.split(":", 1)[1], file=sys.stderr, flush=True)

    db = db_mod.connect(cfg)
    try:
        db_mod.require_schema(db)
        host = cfg.host or ""
        try:
            canons = canon_mod.load_configured(cfg)
        except Exception:
            canons = []
        policy = effective(canons, host)
        try:
            limits = resources_mod.host_limits(canons, host)["limits"]
        except Exception:  # un canon illisible ne bloque pas la commande
            limits = {}
        busy = False
        if apply:
            try:
                busy = storage.of(db).hosts.turns_in_progress(host) > 0
            except Exception:
                busy = True
        actor = getattr(args, "by", "") or "human:%s" % getpass.getuser()
        from . import containers as containers_mod
        report = run_pass(cfg, db, policy, host=host, actor=actor, dry_run=not apply,
                          busy=busy,
                          # #3293 : le seuil de pause disque de l'hôte
                          # déclenche l'éviction des caches sous pression.
                          free_target=int(limits.get("min_disk_free") or 0),
                          runtime=containers_mod.Runtime.from_config(cfg),
                          progress=progress,
                          # #4754 : les unités de veille de CET hôte
                          units_dir=host_units_dir())
        if report.get("dry_run") and not report.get("status") == "skipped":
            # #4234 : le volume des arbres gardés est enfin visible, les plus
            # gros en tête ; le relevé ne supprime rien et reste borné.
            report["worktree_sizes"] = kept_worktree_sizes(report.get("plan") or ())
            report["worktree_sizes"]["archive_declared"] = bool(
                policy.dirty_worktree_after and policy.dirty_archive_retention)
        if json_mode:
            print(json.dumps(dict(report, schema="ameesh-menage/1"), ensure_ascii=False,
                             sort_keys=True, default=str))
        else:
            print_report(report)
        return 0
    finally:
        db.close()


#: L148 : ancienne graphie de :func:`cmd_housekeeping`, gardée pour les
#: importeurs et les tests pendant la transition ; retirée au stage 7.
cmd_menage = cmd_housekeeping


__all__ = ["Policy", "parse_policy", "effective", "turn_env", "hold_tmp", "Turn", "run_pass",
           "measure", "evict_lru", "worktree_state", "list_worktrees", "cmd_housekeeping",
           #: L148 : `from ameesh.menage import *` doit encore exposer
           #: l'ancienne graphie, retirée au stage 7 (comme menage.py).
           "cmd_menage",
           "caller_refusal", "orphan_pass", "tmp_top", "tmpfs_hint", "in_memory"]
