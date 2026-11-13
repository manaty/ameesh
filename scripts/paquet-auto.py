#!/usr/bin/env python3
# SPDX-License-Identifier: AGPL-3.0-only
"""Composition scriptée du paquet horaire, sans jugement (#3807).

    scripts/paquet-auto.py --target main --name h-20261027-01 --carrier 3808
    scripts/paquet-auto.py --replay b57c0380 --base f444955b \\
        --name h-20261026-31

Le paquet horaire du canon §11 bis/§12 était composé à la main, en enchaînant
des outils qui existaient déjà (constat h-30/h-31 du 26/10 : h-30 a coûté
5 h 12 et quatre compositions). Ce script enchaîne les mêmes gestes, dans
l'ordre, sans agent :

1. un *worktree* jetable à la tête exacte d'`origin/<cible>` (SHA noté) ;
2. `scripts/paquet-candidats.py`, exécuté **depuis ce worktree** (le code de
   la cible, leçon #3767/#3782), avec `--base` égal à cette tête : la liste
   calculée, P1 d'abord, est la seule source des admissions ;
3. pour chaque candidat, `scripts/paquet-compose.py --lot N --source S
   --record-package <porteur>` ; un conflit est annulé (`git merge --abort`),
   le lot est écarté avec le motif « conflit, coupure suivante » et le script
   passe au suivant. Aucune résolution : résoudre est un jugement, il reste à
   un agent ;
4. `scripts/freeze-checks.py` sur la tête composée ;
5. le manifeste `reviews/integration-paquet-<nom>.md` : base et SHA de la
   cible, table `## Composition` produite par `paquet-compose.py --units`
   (gabarit #3629), admissions, écartés et leur motif, freeze-checks ; ses
   tests par unité y sont dits « en parallèle de la CI » (#4304) ;
6. `ameesh work packet record` (liaison des unités au porteur), puis
   `scripts/paquet-inclusion.py` sur la tête ;
7. **dès ce point** (canon §6, #4304) : poussée de
   `integration/paquet-<nom>`, PR ouverte ou mise à jour, gel
   `ameesh work milestone <porteur> frozen --sha <tête>` et demande
   `ameesh ci run <porteur> --full` — la CI complète part sans attendre les
   tests par unité ;
8. les tests par unité, pendant la CI : les modules de test apportés par
   chaque unité et leurs voisins. La passe de contrats de l'arbre final
   (plan commun, #6182) précède déjà la poussée, le gel et la ci-full :
   budget canon, attente de créneau comprise, résultat réutilisé sans rejeu.
   Une expiration est non concluante et laisse publier avec ci-full ; un
   contrat négatif rejoint contract_red après publication avec ci-full ; un arbre
   modifié invalide la passe et demande une nouvelle composition. Un test ciblé **rouge** d'un module **voisin**
   est situé en rejouant ce test sur les préfixes de l'empilement, dans
   l'ordre (`base + u1`, `base + u1 + u2`, …) : la première unité où il
   apparaît est retirée, son auteur est prévenu, et le paquet est recomposé
   **une seule fois** sans elle (#4217). L'attribution par chevauchement de
   fichiers (`packet_red`, #3801) ne désigne plus un voisin ; elle reste la
   lecture des rouges propres et des contrats (#3931). Un rouge voisin
   impossible à situer sur les préfixes n'entraîne **aucun retrait** : le
   pilote est prévenu et le paquet s'arrête. Un test rouge sur la tête mais
   **vert sur tous les préfixes** est instable (#4221) : consigné non
   concluant, sans retrait, le paquet continue et le pilote est prévenu. Un
   rouge non attribuable garde
   le geste antérieur : une unité dont le module propre a rougi est retirée,
   un contrat rouge arrête (l'attribution demande un agent). Un run **non
   concluant** (délai, base absente, psql/psycopg indisponible, erreur de
   fixture DB en QueryCanceled/timeout, rc inattendu) est consigné à la PR,
   au porteur et au courrier, sans retirer d'unité ni arrêter le paquet : le
   canon §3 le veut, la ci-full tranche ;
9. le résultat des tests au corps de la PR et au journal du porteur,
   courrier au pilote.

Un rouge **du contenu** — contrat, test d'une unité, contrôle de gel, types
clients périmés — sort en `COMPOSITION_RED_RC` (#6272) : le composeur a fait
son travail, le pilote est prévenu, `paquet-horaire.py` le rapporte sans ouvrir
de lot d'empreinte §16. La panne du composeur (git, base, poussée, outil
manquant) reste rc 1 et ouvre seule cette empreinte.

Ce qui reste au pilote, hors de ce script : « verdict non requis », le feu et
toute fusion sur la cible. Le script ne fusionne jamais la cible et ne déclare
aucun verdict. La ci-full demandée dès la poussée arme pourtant la fusion
automatique (#2813), et un porteur sans delta propre n'attend aucun verdict :
le paquet peut fusionner au vert **avant la fin des tests par unité** (canon
§11 bis, panier #4322). La ci-full tranche ; si la PR est fusionnée pendant les
tests, le script ne recompose plus rien : un rouge d'unité va au porteur et au
pilote (corriger en avant ou revert), et la ligne `MERGED_LINE` dit à
`paquet-horaire.py` de ne rien publier.

Un lot prêt peut être retiré de la composition par `--exclude LOT` (répétable,
#3845) ; `--exclude-reason TEXTE` (répétable, apparié par ordre) écrit son vrai
motif aux écartés du manifeste — B de relecture, regel, rouge local —, sans quoi
le motif neutre « exclu par le pilote » est écrit (#4201).

#3256 C : `--only LOT` (répétable) borne la composition à un groupe de lots
prêts — celui d'un paquet parallèle. Tout autre lot prêt est écrit aux écartés
avec le motif « paquet parallèle : autre groupe disjoint », jamais composé
dans deux paquets.

#4581 : `--keep-composition REF` reprend **les membres** de la composition du
manifeste gelé à REF, moins les `--exclude`, chacun à sa source composée. Un
lot prêt depuis cette composition, un membre regelé (nouvelle tête) ou disparu
de la liste prête n'est pas admis : il est écrit aux écartés avec le motif
« coupure suivante : … ». Après un rouge, le paquet ne grossit donc plus à
chaque recomposition, et aucune tête neuve n'entre sans ci de paquet.

#6566 : une tête composée n'est poussée que si le gel de chacune de ses unités
est encore celui de la liste calculée. Juste avant la poussée, la liste est
relue depuis un worktree à la base exacte (le code de la cible, jamais l'arbre
composé) ; une unité dont le lot n'est plus prêt ou dont la source a changé
(regel pendant la composition) est écartée « coupure suivante : … » et le
paquet est recomposé sans elle. Une tête dont une unité a été regelée depuis
la lecture ne part donc plus : sa CI et sa garde de fusion (« does not carry
its frozen head ») ne sont plus perdues.

#4113 : hors banc sans base, la même garde d’époque que `packet record`
est relue avant la composition, entre unités, avant chaque série de tests et
avant publication. Une perte de bail arrête avant l’opération suivante ;
une opération déjà lancée peut finir. Les contrats seuls peuvent réutiliser une attestation du même arbre et de
la même portée (#6185).

#4271 : `--tests-parallel N` joue les séries des unités et celle des contrats
en même temps (au plus N), chacune dans un créneau `ameesh heavy` de l'hôte et
bornée par `--tests-timeout` (au-delà : non concluant, canon §3) ;
paquet-horaire les lit dans `packet_tests` du canon compilé. Les voisins de la
carte sont pesés par `tests/parts/durees.json` pour le pilote des tests
(`test_driver`), que le manifeste nomme : sous psql, la suite est ~6 fois plus
lente que la mesure psycopg de la carte.

#4463 : une fois la tête poussée, la garde relue avant chaque série relit
aussi l'état de la PR : fusionnée (ou close) pendant les tests par unité, leurs
résultats ne peuvent plus retirer une unité ; les séries non parties sont
abandonnées et le porteur reçoit « tests par unité interrompus : porteur
fusionné à <sha> ». `--tests-budget S` (paquet-horaire :
`packet_tests.total_timeout_seconds` du canon compilé) borne la durée totale
des tests : chaque série est bornée au temps restant, celles qui ne partent
plus avant l'échéance sont non concluantes (canon §3) et le tic suivant peut
partir.

#4887 : sous cette borne, la première série ne mange plus le temps des autres.
Chaque série ne reçoit que sa part des vagues restantes (`series_share`) :
toutes les unités commencent, leurs modules directs (propres et voisins joués)
passent avant l'échéance, les contrats en dernier. Le corps de la PR dit, lot
par lot, si ses modules directs ont été joués jusqu'au bout (`_direct_lines`) ;
une série coupée n'en revendique aucun (#4814). Le contrôle rapide du job plan
« types clients à jour » passe avant la poussée et la demande de CI : le vrai
`gen-api --check` si Node et les dépendances sont là, sinon la garde
déterministe « OpenAPI changé sans schéma » (#4831).

#4237 : un refus de geste (bail relevé, `GestureRefused`) **après des tests
verts** ne perd plus le paquet : depuis #4304, la tête est déjà poussée,
enregistrée et gelée avant ses tests ; sa fin (corps de PR, note, ci-full,
courrier) est perdue. La tête testée est poussée sous
`refs/ameesh/paquet-auto/teste/<nom>` et dite au journal du porteur
(`paquet-auto: tête testée …`). Le détenteur courant finit sans retester par
`--publish <réf> --publish-sha <tête>` : la réf, la branche du paquet sur
origin et la marque désignent la même tête ; la chaîne est vérifiée (base =
tête fraîche de la cible ; une fusion par unité du manifeste, son second
parent = la source de la liste calculée, donc la tête gelée enregistrée, et
l'arbre égal à la fusion propre des deux parents ; le manifeste seul
au-dessus) ; les tests d'origine restent ceux du manifeste. Une chaîne
refusée sort en 3 : le porteur se recompose.

#4329 : une composition tient un verrou `flock` **par nom de paquet**, sous le
`.git` commun du clone, de `clear_local_branch` à la fin de la composition. Une
seconde composition du même paquet refuse « composition en cours (hôte:pid) »
(code 3) au lieu de prendre le worktree vivant pour celui d'une tentative
précédente et de le supprimer ; le tick la compte comme en cours. Le noyau rend
le verrou à la mort du processus, donc le worktree d'une tentative morte reste
nettoyable comme avant, sans expiration à deviner.

#6563 : ce `flock` ne voit ni un autre clone ni un autre hôte. Une composition
réelle (et la publication d'une tête testée, #4237) prend donc aussi le **bail
de composition du porteur** (`merge_locks`, clé
`packet-compose:carrier:<projet>:<n° de porteur>`), distinct du bail projet de
#5941 pour que la paire parallèle #3256 C reste permise. Le parent n'accède pas
à la base : un enfant `ameesh work packet hold` prend le bail, le renouvelle
(TTL/3) et le rend quand son entrée standard se ferme, c'est-à-dire à la fin,
à la mort ou au kill de la composition. Avant chaque geste, `require()` ne se
contente pas de vérifier que cet enfant vit : il demande à la base une preuve
fraîche et bornée (`ameesh work packet check`, propriétaire et époque), car un
enfant suspendu ou bloqué dans son renouvellement reste vivant pendant que le
TTL expire. Un second composeur — le tic horaire ou l'agent désigné par
l'alerte de créneau, sur le même porteur — est refusé (code 3) au lieu de
réécrire la tête de l'autre ; un bail expiré ou repris est refusé de même.

#6891 : la référence #3782 est relue **avant** les tests locaux
(`ensure_reference`), juste avant les contrats. Quand la cible avance pendant
la composition — le commit d'atterrissage d'une coupure, par exemple — la
composition ne part pas sur une base périmée et ne perd pas les ~12 min de
contrats : elle sort en 6, le clone est déjà à jour (`fetch`), et
`paquet-horaire.py` relance aussitôt la composition sur la nouvelle tête. La
relecture du gel juste avant la poussée reste en place (#6566) : la cible peut
encore bouger pendant les tests.
#6867 : cette référence est relue une seconde fois **après** les contrats,
juste avant le gel et la poussée, quand rien n'est encore poussé : une cible
qui atterrit pendant les ~12 min de contrats sort en 6 au lieu du refus
`paquet-candidats` (rc 2) traduit en rc 1 et lot d'empreinte §16 par
`paquet-horaire.py`.
#6981 : une cible avancée par les **seuls commits d'atterrissage** d'une
coupure (§12 : version et notes, `[skip ci]`) garde la composition, aux deux
relectures comme à celle du gel ; tout autre commit lève encore `TargetMoved`.
Seul un déplacement (`behind`, `diverged`) relance aussitôt : une référence
illisible (`absent`, `dirty`, `unknown`) sort en 1 avec sa cause. Des contrats
rouges restent le verdict même si la cible a bougé pendant eux (rc 5).

Modes :

* normal : composition réelle, poussée, PR, gel, ci-full, tests et courrier ;
* `--local` : manifeste, `packet record` et tests par unité, sans poussée,
  PR, gel, ci-full ni courrier (composition de contrôle) ;
* `--replay <réf> [--candidates-json <sortie paquet-candidats --json>]` :
  rejoue la table `## Composition` d'un manifeste déjà gelé depuis `--base`,
  reprend le manifeste de `--réf` tel quel (métadonnée, aucun effet sur le
  contenu composé) et compare l'arbre obtenu à `git rev-parse <réf>^{tree}`.
  Avec `--candidates-json`, chaque unité doit être prête et aucune en erreur
  (#3813) : h-31 v3 (`b57c0380`, #3506 en erreur) est refusé, h-31 v4
  (`fc59a14e`, 6 unités) passe et rend l'arbre `2d15e224…` de `fc59a14e`.
  Sans le JSON, c'est le témoin de fidélité d'arbre seul. Aucune poussée,
  aucune écriture en base.

Codes de retour : 0 composition réussie (ou aucun lot prêt), 1 échec
opérationnel (git, test, contrôle, poussée, PR, gel, courrier), 2 entrée
invalide, 3 une composition du même paquet est en cours (verrou vivant), ou
une tête testée n'est pas publiable (`--publish`, #4237), 4 tentative arrêtée
par une pause (#5084), 5 composition rouge par son contenu (#6272), 6 la
cible a avancé avant les tests locaux (#6891 : rien n'est poussé, le clone est
à jour, `paquet-horaire.py` recompose aussitôt sur la nouvelle tête).
"""
from __future__ import annotations

import argparse
import contextlib
import fcntl
import importlib.util
import json
import os
import re
import select
import shutil
import signal
import socket
import subprocess
import sys
import tempfile
import threading
import time
import uuid
from concurrent.futures import ThreadPoolExecutor
from dataclasses import dataclass, field
from pathlib import Path

#: branche de paquet et manifeste, pour un `--name` (ex. `h-20261027-01`)
PACKAGE_PREFIX = "integration/paquet-"
MANIFEST_DIR = "reviews"
MANIFEST_PREFIX = "integration-paquet-"
NAME_RE = re.compile(r"^[A-Za-z0-9][A-Za-z0-9._-]*$")
SHA_RE = re.compile(r"[0-9a-f]{7,64}", re.IGNORECASE)
CONTRACT_ALWAYS = "tests/parts/toujours.txt"
CONTRACT_MODULES = ("tests.test_l161f_plan", "tests.test_cluster")
#: #6274 : les outils de la convention d'ameesh ; un projet les remplace par
#: son bloc `prevalidation` (`plan_script`, `parts_script`, `contracts`)
DEFAULT_PLAN_SCRIPT = "scripts/test-plan.py"
DEFAULT_PARTS_SCRIPT = "scripts/test-parts.py"
#: le plan des contrats écrit hors de l'arbre composé pour le lanceur déclaré
DECLARED_PART_LABEL = "contrats"
COMPOSE_SCRIPT = "scripts/paquet-compose.py"
CANDIDATES_SCRIPT = "scripts/paquet-candidats.py"
INCLUSION_SCRIPT = "scripts/paquet-inclusion.py"
#: #4329 : verrou de composition par nom de paquet, sous le `.git` commun
LOCK_DIR = "ameesh-paquet-auto"
#: #4329 : code de retour d'un refus « composition en cours »
IN_PROGRESS_RC = 3
#: #5084 : code de retour d'une tentative arrêtée par une pause posée après son
#: départ (relue avant de pousser) : rien n'est poussé, tentative non décomptée
PAUSED_RC = 4
#: #6272 : code de retour d'un paquet rouge par son **contenu** (contrat, test
#: d'une unité, contrôle de gel ou types clients) : le composeur a fait son
#: travail et le pilote est déjà prévenu. Ce n'est pas une panne du composeur,
#: qui reste rc 1 et ouvre seule un lot d'empreinte §16.
COMPOSITION_RED_RC = 5
#: #6891 : la cible a avancé entre la composition et ses tests locaux. Rien
#: n'est poussé ; le clone est déjà à jour (le `fetch` de `fresh_base`) et
#: `paquet-horaire.py` relance aussitôt la composition sur la nouvelle tête,
#: sans attendre le tic suivant.
TARGET_MOVED_RC = 6
#: #6563 : bail de composition d'un porteur, tenu par un enfant `ameesh work
#: packet hold` pendant toute la composition réelle. Renouvelé par l'enfant
#: (TTL/3) ; un processus mort le laisse expirer en cinq minutes.
CLAIM_TTL_S = 300.0
#: #6563 : attente de la première réponse du détenteur (acquisition en base)
CLAIM_START_TIMEOUT = 60.0
#: #6563 : borne de la preuve fraîche du bail (lecture base) avant chaque
#: geste. Au-delà, le geste est refusé même si l'enfant détenteur vit encore :
#: un enfant suspendu ou bloqué dans son renouvellement ne prouve pas le bail.
CLAIM_CHECK_TIMEOUT = 30.0
#: #5084 : sortie du lecteur de pause (Python du projet) quand la pause est active
PAUSE_ACTIVE_EXIT = 10
#: #3916 : les remises (`ameesh.resolutions.REF_PREFIX`) et leur copie locale
REMISE_PREFIX = "refs/ameesh/resolutions/"
REMISE_LOCAL = "refs/ameesh/paquet-auto/remise/"
FREEZE_CHECKS_SCRIPT = "scripts/freeze-checks.py"
DEFAULT_TIMEOUT = 1800
#: #4271 : séries jouées en même temps, quand l'appelant ne dit rien (série)
DEFAULT_PARALLEL = 1
#: #4271 : code de sortie d'`ameesh heavy` arrêté par sa borne (`heavy.CAPPED_EXIT`)
HEAVY_CAPPED_EXIT = 124
#: #4463 : après SIGTERM au temps mural échu, le délai laissé à `ameesh heavy`
#: pour arrêter sa série : SIGKILL au groupe après `heavy.GROUP_GRACE_S`
#: (10 s), puis arrêt de ses autres descendants (SIGTERM, SIGKILL 10 s après)
WALL_GRACE_S = 30
FREEZE_TIMEOUT = 600
GIT_TIMEOUT = 300
#: #4245 : `RESTART_RESERVE_S` (90 s) de l'élection, plus la lecture
EPOCH_CHECK_TIMEOUT = 150
#: #4237 : la tête testée gardée sur un refus de geste, et sa marque au journal
TESTED_REF_PREFIX = "refs/ameesh/paquet-auto/teste/"
TESTED_MARK = "paquet-auto: tête testée "
TESTED_RE = re.compile(
    r"^paquet-auto: tête testée base=([0-9a-f]{40}) tête=([0-9a-f]{40}) réf=(\S+)")
#: #4237 (B cx1) : la note de fin des tests par unité verts sur la tête poussée
#: (#4304) ; paquet-horaire y lit qu'une tentative gelée avant ses tests a abouti
TESTS_DONE_RE = re.compile(
    r"^paquet \S+ : tests par unité .* sur la tête poussée ([0-9a-f]{12}) ")
#: le refus de `pilot_gesture` (« gesture packet refused for … »)
REFUSAL_RE = re.compile(r"\bgesture \w+ refused\b|GestureRefused")
#: sortie de `--publish` sur une chaîne refusée : recomposer
PUBLISH_REFUSED = 3
#: #5084 : une source qui contient celle d'un lot écarté le porterait au paquet
CONTAINS_DISCARDED_REASON = "contient un lot écarté, coupure suivante"
#: un rouge local n'est retiré du paquet qu'une fois ; au-delà, un agent lit.
MAX_RECOMPOSITIONS = 1
#: #4304 : la ligne du manifeste poussé avant les tests par unité (relue par
#: `prevalidation_calls.composer_tested`, #4136 : ne pas la reformuler seule).
TESTS_PARALLEL_LINE = ("- Tests locaux : joués par le composeur après la poussée, "
                       "en parallèle de la CI complète (#4304) ; résultat à la PR, "
                       "au porteur et au courrier du pilote.")
#: #4322 : début de ligne quand la PR a fusionné pendant les tests par unité ;
#: `paquet-horaire.py` (after_compose) n'appelle alors pas publish.
MERGED_LINE = "porteur fusionné pendant les tests par unité"
#: #4201 : motif neutre d'un `--exclude` sans `--exclude-reason`. Le vrai motif
#: (B de relecture, regel, rouge local) est celui que le pilote écrit.
EXCLUDE_DEFAULT_REASON = "exclu par le pilote"
#: #3256 C : motif écrit pour un lot prêt que `--only` laisse à l'autre paquet
#: parallèle ; il n'est ni perdu ni composé deux fois.
PARALLEL_EXCLUDE_REASON = "paquet parallèle : autre groupe disjoint"
#: #3256 C : un lot du groupe qui n'est plus prêt (gel, avis, conflit) est
#: écarté avec ce motif, jamais une erreur qui garde la cible réservée.
PARALLEL_GROUP_NOT_READY = "paquet parallèle : lot du groupe non prêt"
PARALLEL_GROUP_GONE = "paquet parallèle : lot du groupe introuvable"
#: #4581 : motifs écrits par une recomposition qui garde la composition
#: précédente. Les lots prêts depuis vont au paquet suivant, jamais dans
#: celui-ci ; un membre regelé n'est pas réadmis sur une tête sans ci de paquet.
KEEP_READY_REASON = "coupure suivante : recomposition"
KEEP_REFROZEN_REASON = "coupure suivante : tête regelée"
KEEP_NOT_READY_REASON = "coupure suivante : membre non prêt"
#: #6567 : un moteur de conteneurs inutilisable dans l'environnement du test.
#: Ces signes le prouvent seuls : le message du shim du ménage
#: (`menage/bin/docker` sans `AMEESH_REAL_DOCKER`), l'exécutable introuvable,
#: ou le démon injoignable dit par le client lui-même.
CONTAINER_ENGINE_UNUSABLE = (
    r"ameesh: container engine not found|"
    r"No such file or directory: '(?:[^']*/)?(?:docker|podman)'|"
    r"Cannot connect to the (?:Docker|Podman) daemon|"
    r"Is the docker daemon running|error during connect|"
    r"unable to connect to Podman socket")
DAEMON_UNREACHABLE_RE = re.compile(
    r"Cannot connect to the (?:Docker|Podman) daemon|"
    r"Is the docker daemon running|error during connect|"
    r"unable to connect to Podman socket", re.IGNORECASE)
#: #6567 : une commande docker/podman en échec. **Seul, son code ne prouve
#: rien** : un moteur sain rend aussi 127 quand la commande lancée dans le
#: conteneur est introuvable (`docker run busybox foo`, B/P2 cx1). L'échec ne
#: compte comme environnement que si la sonde `<exécutable> info`, dans
#: l'environnement de la série (`container_engine_state`), prouve le moteur
#: absent (127, exécutable introuvable : n'excuse qu'un 127) ou son démon
#: injoignable (excuse tout code). C'est le cas h-16, où le shim écrivait son
#: message sur un stderr capturé par le test. La sonde vise l'exécutable
#: **tel qu'écrit** (`exe`) : un nom nu par le `PATH` de la série, un chemin
#: absolu tel quel ; un chemin relatif dépend du cwd du test, inconnu ici, et
#: ne se prouve jamais (B/P2 cx1, second tour).
CONTAINER_FAILED_RE = re.compile(
    r"Command '(?:\[')?(?P<exe>(?:[^'\s]*/)?(?:docker|podman))'?[,\s][^\n]*"
    r"returned non-zero exit status (?P<code>\d+)")
ENGINE_ABSENT = "absent"
ENGINE_UNREACHABLE = "unreachable"


def engine_failure_excused(found: re.Match, unusable: frozenset) -> bool:
    """#6567 : cet échec docker/podman est-il expliqué par la sonde ?"""
    exe, code = found.group("exe"), int(found.group("code"))
    return ((exe, ENGINE_UNREACHABLE) in unusable
            or (code == 127 and (exe, ENGINE_ABSENT) in unusable))


#: sorties qui ne prouvent pas un rouge du lot (environnement, base absente)
#: #4217 : un `tearDownClass`/`setUpClass` en `QueryCanceled` (statement
#: timeout, base de test chargée) est une panne d'infrastructure, jamais un
#: rouge du lot, qu'il sorte dans un module propre ou voisin.
INCONCLUSIVE_RE = re.compile(
    r"(OperationalError|could not connect|connection refused|"
    r"server closed the connection|database system is starting up|"
    r"psql: error|No such file or directory: 'psql'|"
    r"could not translate host name|Temporary failure in name resolution|"
    r"QueryCanceled|canceling statement|statement timeout|timeout expired|"
    r"ModuleNotFoundError: No module named 'psycopg|"
    + CONTAINER_ENGINE_UNUSABLE + r")",
    re.IGNORECASE,
)
#: #4221 : un QueryCanceled ou un lock timeout **reproduit** en rejouant seul,
#: hors charge, est un rouge réel ; hors rejeu, il reste non concluant.
CANCEL_MESSAGE_RE = re.compile(
    r"canceling statement due to (?:statement|lock) timeout|timeout expired",
    re.IGNORECASE,
)
#: #4221 : une erreur levée dans `setUp`/`tearDown` (comme dans
#: `setUpClass`/`tearDownClass`) est une panne de fixture : non concluante
#: même rejouée seule. Seul le corps de la méthode de test peut se confirmer.
FIXTURE_FRAME_RE = re.compile(
    r"^\s+File .*, line \d+, in (?:setUp|tearDown)\s*$", re.MULTILINE)
#: le **message** d'une exception d'environnement (sans les noms de types :
#: un `AssertionError: expected OperationalError` reste un rouge réel, et un
#: `ValueError: expected QueryCanceled` aussi : le nom psycopg est reconnu par
#: la ligne d'exception, pas par le message)
ENV_MESSAGE_RE = re.compile(
    r"could not connect|connection refused|server closed the connection|"
    r"database system is starting up|psql: error|"
    r"No such file or directory: 'psql'|"
    r"could not translate host name|Temporary failure in name resolution|"
    r"canceling statement|statement timeout|timeout expired|"
    r"No module named 'psycopg'|"
    + CONTAINER_ENGINE_UNUSABLE,
    re.IGNORECASE,
)
#: une ligne de résumé unittest
RAN_RE = re.compile(r"^Ran (\d+) tests? in ", re.MULTILINE)
OK_RE = re.compile(r"^OK(?: \(skipped=(\d+)\))?", re.MULTILINE)
FAILED_SUMMARY_RE = re.compile(r"^FAILED \((.*?)\)", re.MULTILINE)
FAIL_HEADER_RE = re.compile(r"^FAIL: ", re.MULTILINE)
ERROR_HEADER_RE = re.compile(r"^ERROR: .*$", re.MULTILINE)
#: la ligne qui ouvre une trace Python ; la dernière du bloc porte l'exception
#: effective.
TRACEBACK_RE = re.compile(r"^Traceback \(most recent call last\):\s*$",
                          re.MULTILINE)
#: lignes de suite d'un message psycopg3 : ce ne sont pas des exceptions
_EXCEPTION_CONTINUATIONS = ("CONTEXT", "DETAIL", "HINT", "QUERY", "STATEMENT")


def last_exception(block: str) -> tuple[str, str] | None:
    """Le nom et le message de la dernière exception effective d'un bloc.

    On part du **dernier** `Traceback (most recent call last):` et on lit la
    première ligne non vide en colonne 0 après ses frames indentées : c'est la
    ligne d'exception, avec ou sans message (`ValueError` nu). Les lignes de
    suite psycopg3 (`CONTEXT:`, `DETAIL:`, `HINT:`…) ne sont jamais
    l'exception ; une ligne source est indentée. None quand le bloc n'a pas de
    traceback ou pas de ligne d'exception lisible (sortie psql, trace
    tronquée).
    """
    matches = list(TRACEBACK_RE.finditer(block or ""))
    if not matches:
        return None
    for raw in (block or "")[matches[-1].end():].splitlines():
        if not raw.strip():
            continue
        if raw[:1].isspace():
            continue
        name, sep, message = raw.partition(":")
        name = name.strip()
        if name in _EXCEPTION_CONTINUATIONS:
            return None
        if not name:
            continue
        return name, (message.strip() if sep else "")
    return None


BLOCK_END_RE = re.compile(r"^=+$", re.MULTILINE)
#: le seuil de preuve forte de #3801, le même que `paquet_horaire.classify`
STRONG_EVIDENCE = 4
#: l'outil qui rend l'attribution `packet_red` d'un rouge de composition
ATTRIBUTION_SCRIPT = "scripts/paquet-attribution.py"
#: #4025 : budget, en secondes (durées du pilote, `tests/parts/durees.json`),
#: des modules voisins ajoutés à une unité ; #4814 : les modules `support`
#: y entrent aussi, avant ceux de la carte
IMPACT_BUDGET_S = 180
#: #4887 : le contrôle « types clients à jour » du job plan, avant la CI
CLIENT_SPEC = "docs/api/openapi-v1.json"
CLIENT_SCHEMA = "ui/src/api/schema.d.ts"
CLIENT_GEN = "ui/scripts/gen-api.mjs"
#: #6676 : le cache partagé par lockfile (`ameesh deps link`, #248) prête au
#: worktree jetable les `ui/node_modules` de l'arbre composé ; `--no-fill` :
#: jamais de `npm ci` réseau dans le tic, une entrée absente reste absente
DEPS_LINK_TIMEOUT = 60
#: #6676 : un « non concluant » faute de modules est compté (`ameesh events`)
CLIENT_TYPES_EVENT = "packet.client_types"
#: les causes de non-concluant qui sont une panne de l'hôte, pas du contenu
CLIENT_MISSING_CAUSES = ("node-absent", "modules-absent")
#: un en-tête d'échec unittest (`FAIL: test (tests.test_x.T.t)`) ; une ligne
#: `FAIL:` produite par le test lui-même n'a pas ce nom pointé et est ignorée
FAIL_LINE_RE = re.compile(
    r"^(FAIL|ERROR):\s+.+?\(((?:tests\.|unittest\.loader\.)[^()]*)\)",
    re.MULTILINE)
TEST_MODULE_RE = re.compile(r"(?:^|\.)tests\.(test_[A-Za-z0-9_]+)")


class PaquetAutoError(RuntimeError):
    """Un geste du script a échoué ; le message porte l'état atteint."""


class CompositionRed(PaquetAutoError):
    """#6272 : la composition est rouge par son contenu, pas par le composeur.

    Un contrat, un test d'unité, un contrôle de gel ou des types clients
    rouges : le composeur a fait son travail et le pilote est déjà prévenu.
    ``main`` sort en ``COMPOSITION_RED_RC`` pour que `paquet-horaire.py` ne
    l'ouvre pas comme une panne du composeur (§16), qui reste rc 1.
    """


class InputError(PaquetAutoError):
    """Un argument ou un état du dépôt ne permet pas de composer."""


class GitError(PaquetAutoError):
    """Une commande git a échoué."""


class EpochLost(PaquetAutoError):
    """#4237 : un geste refusé par la garde d'époque (bail relevé)."""


class PacketPaused(PaquetAutoError):
    """#5084 : une pause de la cible posée pendant la tentative, relue avant de pousser."""


class CarrierSettled(PaquetAutoError):
    """#4463 : la PR du paquet a fusionné (ou a été close) pendant ses tests."""

    def __init__(self, state: str) -> None:
        self.state = state
        super().__init__("PR %s pendant les tests par unité" % state.lower())


class BudgetSpent(PaquetAutoError):
    """#4463 : l'échéance totale des tests par unité passée en plein rejeu des
    préfixes : non concluant, aucune unité retirée (§3)."""


class ChainRefused(PaquetAutoError):
    """#4237 : une tête testée que `--publish` ne publie pas (chaîne, base)."""


class CompositionBusy(PaquetAutoError):
    """#4329 : une autre composition du même paquet tient son verrou."""

    def __init__(self, name: str, host: str = "", pid: int = 0) -> None:
        self.name = name
        self.host = host
        self.pid = pid
        super().__init__("composition en cours (%s)" % holder_label(host, pid))


class TargetMoved(PaquetAutoError):
    """#6891 : la cible a avancé entre la composition et ses tests locaux.

    Le code de la composition (base `base`) n'est plus la référence de #3782
    (tête `head`) : les contrats et les tests par unité ne partent pas sur une
    base périmée. `main` sort en `TARGET_MOVED_RC` ; `paquet-horaire.py`
    journalise le refus avant tests et relance aussitôt la composition sur la
    nouvelle tête, sans attendre le tic suivant.
    """

    def __init__(self, message: str, *, base: str = "", head: str = "") -> None:
        self.base = base
        self.head = head
        super().__init__(message)


class CompositionClaim:
    """#6563 : le bail de composition du porteur, tenu pendant la composition.

    Le parent `paquet-auto` reste sans dépendance base : un enfant
    `ameesh work packet hold` prend le bail (`packet_composition.Lease`, clé
    par porteur), le renouvelle et le rend quand son entrée standard se ferme
    — c'est-à-dire quand cette composition se termine, meurt ou est tuée avec
    son tube. La première ligne JSON dit si le bail est pris ou tenu par un
    autre.

    `require` avant chaque geste ne se contente pas de vérifier que l'enfant
    vit : un enfant suspendu (SIGSTOP) ou bloqué dans un renouvellement reste
    vivant pendant que le TTL expire et qu'un autre composeur reprend la clé
    du porteur. Il demande donc une preuve fraîche et bornée à la base
    (`ameesh work packet check`, propriétaire et époque attendus) ; bail
    expiré, repris, preuve en dépassement de borne ou illisible = refus.
    """

    def __init__(self, repo: Path, carrier: int, name: str = "",
                 cli: list[str] | None = None, env: dict | None = None):
        self.repo = Path(repo)
        self.carrier = int(carrier)
        self.name = str(name or "")
        self.owner = "paquet-auto:%s:%d:%s" % (
            socket.gethostname(), os.getpid(), uuid.uuid4().hex[:12])
        #: #6563 : binaire `ameesh` ; les bancs peuvent passer
        #: `[sys.executable, "-m", "ameesh.main"]`.
        self.cli = list(cli) if cli else ["ameesh"]
        #: #6563 : environnement des enfants (`None` : celui du parent).
        self.env = dict(env) if env else None
        self.proc: subprocess.Popen | None = None
        self.epoch = 0

    def _command(self) -> list[str]:
        return self.cli + ["work", "packet", "hold", str(self.carrier),
                           "--owner", self.owner, "--ttl", str(CLAIM_TTL_S),
                           "--json"]

    def _check_command(self) -> list[str]:
        return self.cli + ["work", "packet", "check", str(self.carrier),
                           "--owner", self.owner, "--epoch", str(self.epoch),
                           "--json"]

    def start(self) -> "CompositionClaim":
        try:
            self.proc = subprocess.Popen(
                self._command(), cwd=str(self.repo), stdin=subprocess.PIPE,
                stdout=subprocess.PIPE, stderr=subprocess.PIPE, text=True,
                env=self.env)
        except FileNotFoundError as exc:
            raise PaquetAutoError(
                "bail de composition du porteur #%d : `ameesh` introuvable (%s) ; "
                "aucune composition" % (self.carrier, exc)) from exc
        try:
            ready, _, _ = select.select([self.proc.stdout], [], [],
                                        CLAIM_START_TIMEOUT)
        except (OSError, ValueError) as exc:
            self.stop(force=True)
            raise PaquetAutoError(
                "bail de composition du porteur #%d illisible : %s"
                % (self.carrier, exc)) from exc
        if not ready:
            self.stop(force=True)
            raise PaquetAutoError(
                "bail de composition du porteur #%d : aucune réponse en %d s ; "
                "aucune composition" % (self.carrier, int(CLAIM_START_TIMEOUT)))
        line = (self.proc.stdout.readline() or "").strip()
        data = {}
        if line:
            try:
                data = json.loads(line)
            except ValueError:
                data = {}
        if not data:
            proc, self.proc = self.proc, None
            # `communicate` ferme lui-même le tube (fin de l'entrée standard) ;
            # le fermer avant rendrait son `flush` sur un fichier déjà clos.
            try:
                _out, error = proc.communicate(timeout=10)
            except subprocess.TimeoutExpired:
                proc.kill()
                _out, error = proc.communicate()
            error = (error or "").strip()
            if proc.returncode == IN_PROGRESS_RC and "composition en cours" in error:
                raise CompositionBusy(
                    self.name, error.split("(", 1)[-1].rstrip(")"), 0)
            raise PaquetAutoError(
                "bail de composition du porteur #%d non pris (rc %s) : %s"
                % (self.carrier, proc.returncode,
                   _tail(error, 5) or "réponse illisible"))
        if data.get("busy") or not data.get("epoch"):
            owner = str(data.get("owner") or "verrou tenu")
            self.stop(force=True)
            raise CompositionBusy(self.name, owner, 0)
        self.epoch = int(data.get("epoch") or 0)
        return self

    def alive(self) -> bool:
        """L'enfant détenteur vit-il encore (tube ouvert, processus vivant) ?"""
        return self.proc is not None and self.proc.poll() is None

    def verify(self) -> bool:
        """#6563 : preuve fraîche, bornée, que le bail est encore à nous.

        Lecture seule dans un processus court : la base rend le propriétaire
        et l'époque vivants. Un dépassement de `CLAIM_CHECK_TIMEOUT`, une
        sortie non nulle ou illisible, ou une valeur différente vaut refus.
        """
        if not self.owner or not self.epoch:
            return False
        try:
            proc = subprocess.run(
                self._check_command(), cwd=str(self.repo),
                capture_output=True, text=True, timeout=CLAIM_CHECK_TIMEOUT,
                env=self.env)
        except (OSError, subprocess.SubprocessError):
            return False
        if proc.returncode != 0:
            return False
        lines = (proc.stdout or "").strip().splitlines()
        if not lines:
            return False
        try:
            data = json.loads(lines[-1])
        except ValueError:
            return False
        return (data.get("match") is True
                and str(data.get("owner") or "") == self.owner
                and int(data.get("epoch") or 0) == int(self.epoch))

    def require(self) -> None:
        """Refuse un geste quand le bail n'est plus tenu par cette composition.

        Un enfant vivant ne suffit pas : il a pu être suspendu, ou son
        renouvellement bloqué au-delà du TTL, pendant qu'un autre composeur
        reprenait la clé du porteur. La preuve base fraîche tranche.
        """
        if self.proc is None:
            raise EpochLost(
                "bail de composition du porteur #%d non pris : aucun geste"
                % self.carrier)
        if not self.alive():
            error = ""
            try:
                if self.proc.stderr is not None:
                    error = (self.proc.stderr.read() or "").strip()
            except (OSError, ValueError):
                error = ""
            raise EpochLost(
                "bail de composition du porteur #%d perdu%s : aucun geste"
                % (self.carrier, (" (%s)" % _tail(error, 3)) if error else ""))
        if not self.verify():
            raise EpochLost(
                "bail de composition du porteur #%d non prouvé (propriétaire "
                "%s, époque %s) : aucun geste"
                % (self.carrier, self.owner, self.epoch))
        if not self.alive():
            raise EpochLost(
                "bail de composition du porteur #%d perdu pendant la preuve : "
                "aucun geste" % self.carrier)

    def stop(self, *, force: bool = False) -> None:
        """Ferme le tube du détenteur ; le bail est rendu par l'enfant."""
        proc, self.proc = self.proc, None
        if proc is None:
            return
        try:
            if proc.stdin is not None:
                proc.stdin.close()
        except (OSError, ValueError):
            pass
        try:
            proc.wait(timeout=2 if force else 10)
        except subprocess.TimeoutExpired:
            try:
                proc.terminate()
                proc.wait(timeout=5)
            except (subprocess.TimeoutExpired, OSError):
                try:
                    proc.kill()
                except OSError:
                    pass
        for stream in (proc.stdout, proc.stderr):
            try:
                if stream is not None:
                    stream.close()
            except (OSError, ValueError):
                pass


class AttributionUnreadable(CompositionRed):
    """Un rouge de voisin ne peut pas être situé sur les préfixes (#4217).

    Aucun retrait automatique : le composeur prévient le pilote et s'arrête,
    pour qu'un agent lise le journal du porteur. C'est un rouge de composition
    (#6272), pas une panne du composeur.
    """

    def __init__(self, why: str, tests=()):
        self.why = " ".join(str(why or "").split())
        self.tests = [str(test) for test in tests if test]
        super().__init__("attribution des préfixes illisible : %s" % self.why)


@dataclass
class Candidate:
    """Un lot prêt de la liste calculée, avec la source exacte à composer."""

    lot: int
    source: str
    branch: str = ""
    title: str = ""
    #: #3916 : la ref de la remise (`refs/ameesh/resolutions/<lot>/…`) quand
    #: la source en est une ; elle n'est sur aucune branche, on la fetch.
    remise_ref: str = ""


@dataclass
class Unit:
    """Une unité de fusion composée."""

    lot: int
    source: str
    commit: str


@dataclass
class Discarded:
    """Un lot prêt non composé, avec le motif écrit au manifeste."""

    lot: int
    reason: str
    detail: str = ""


@dataclass
class Composition:
    units: list[Unit]
    discarded: list[Discarded]
    head: str
    #: #4025 : la provenance de la sélection des modules voisins (carte)
    selection: str = ""


@dataclass
class Removal:
    """Une unité retirée sur un test ciblé rouge, avec sa preuve."""

    lot: int
    tests: list[str] = field(default_factory=list)
    evidence: list[str] = field(default_factory=list)
    attributed: bool = False
    #: #4217 : "" (repli), "3801" (preuve de fichiers) ou "prefixes" (test
    #: rouge situé sur les préfixes de l'empilement).
    proof: str = ""


@dataclass
class TestRun:
    """Une exécution locale de unittest."""

    label: str
    modules: list[str]
    returncode: int | None
    output: str
    seconds: float
    timed_out: bool = False
    #: #4025 : les modules voisins ajoutés (`module`, `reason`, `files`)
    added: list = field(default_factory=list)
    #: Rouge écarté après rejeu des seuls voisins sur la base (#4048).
    ignored_red: bool = False
    log_path: str = ""
    #: #4221 : tests rouges sur la tête mais verts sur tous les préfixes de
    #: l'empilement (instables) : non concluants, ils ne retirent aucune unité.
    flaky_tests: list[str] = field(default_factory=list)
    #: #4887 : les modules directs de l'unité (ses propres tests et ses voisins
    #: sélectionnés), tels que la série devait les jouer ; vide pour les contrats.
    direct: list[str] = field(default_factory=list)
    #: #4887 : rouges de voisins conservés faute de rejeu de base concluant ;
    #: consignés non attribués, la ci-full tranche.
    unattributed: list[str] = field(default_factory=list)
    #: #6567 : `(exécutable tel qu'écrit, état)` des moteurs prouvés
    #: inutilisables par la sonde (`container_engine_state`) dans
    #: l'environnement de la série : `absent` ou `unreachable`.
    unusable_engines: frozenset = frozenset()

    @property
    def green(self) -> bool:
        return self.returncode == 0

    @property
    def finished(self) -> bool:
        """#4814/#4887 : la série est allée au bout de ses modules."""
        return not self.timed_out and self.returncode in (0, 1)

    def _summary(self) -> dict[str, int]:
        found = FAILED_SUMMARY_RE.search(self.output)
        if not found:
            return {}
        return {name: int(value) for name, value in
                re.findall(r"(\w+)=(\d+)", found.group(1))}

    @staticmethod
    def _error_is_environment(block: str,
                              unusable_engines: frozenset = frozenset()) -> bool:
        """La dernière exception effective du bloc est-elle une panne ?

        Le **nom du type** ne suffit pas : `AssertionError: expected
        OperationalError` est un rouge réel. On lit la ligne d'exception
        effective du **dernier** traceback : les frames indentées, les lignes
        source et les suites psycopg3 (`CONTEXT:`, `DETAIL:`…) ne sont pas des
        exceptions ; un nom nu (`ValueError`) est lu. Une exception réelle
        chaînée après une annulation de requête reste réelle, et un nom psycopg
        sans suffixe `Error` (`QueryCanceled`) est lu comme les autres. Sans
        traceback (sortie psql), on cherche un motif d'infrastructure.
        """
        exception = last_exception(block)
        if exception is None:
            return bool(INCONCLUSIVE_RE.search(block))
        kind, message = exception
        name = kind.rsplit(".", 1)[-1]
        if name == "AssertionError":
            return False
        if name == "QueryCanceled":
            return True
        if ENV_MESSAGE_RE.search(message):
            return True
        # #6567 : un échec de docker/podman n'est une panne que si la sonde de
        # cet exécutable, dans l'environnement de la série, l'a prouvé absent
        # (127 seulement) ou son démon injoignable.
        failed = CONTAINER_FAILED_RE.search(message)
        if failed and engine_failure_excused(failed, unusable_engines):
            return True
        if name in ("ModuleNotFoundError", "ImportError") and "psycopg" in message:
            return True
        return False

    def body_cancel_tests(self) -> set[str]:
        """#4221 : les tests dont un QueryCanceled/lock timeout est l'exception.

        Seuls les `ERROR:` du **corps** d'un test comptent : une erreur de
        `setUpClass`/`tearDownClass` reste une panne de fixture, non
        concluante même rejouée seule. Sert au rejeu hors charge : un même
        test qui redonne ce QueryCanceled reproduit une erreur déterministe,
        alors qu'un QueryCanceled de charge ne revient pas.
        """
        found: set[str] = set()
        for match in FAIL_LINE_RE.finditer(self.output or ""):
            kind, test = match.group(1), match.group(2).strip()
            if kind != "ERROR" or not test:
                continue
            header = match.group(0)
            if header.startswith("ERROR: setUpClass") \
                    or header.startswith("ERROR: tearDownClass"):
                continue
            rest = (self.output or "")[match.end():]
            end = BLOCK_END_RE.search(rest)
            block = rest[:end.start()] if end else rest
            if FIXTURE_FRAME_RE.search(block):
                continue
            exception = last_exception(block)
            if exception is None:
                continue
            name, message = exception
            short = name.rsplit(".", 1)[-1]
            if short == "AssertionError":
                continue
            if short == "QueryCanceled" or CANCEL_MESSAGE_RE.search(message):
                found.add(test)
        return found

    @property
    def real_failure(self) -> bool:
        """Un test négatif réel, même mêlé à une erreur d'environnement.

        Un `FAIL:` (assertion) est toujours réel. Un `ERROR:` n'est une panne
        d'environnement que si **sa** dernière exception matche les motifs
        d'infrastructure ; sinon il est réel. Un run mixte
        `FAIL + OperationalError` est donc rouge, jamais « non concluant ».
        """
        if self.returncode == 0:
            return False
        if self.flaky_tests:
            return False
        if self.timed_out or self.returncode != 1:
            return False
        if FAIL_HEADER_RE.search(self.output):
            return True
        headers = list(ERROR_HEADER_RE.finditer(self.output))
        env_error = False
        for header in headers:
            rest = self.output[header.end():]
            end = BLOCK_END_RE.search(rest)
            block = rest[:end.start()] if end else rest
            if self._error_is_environment(block, self.unusable_engines):
                env_error = True
            else:
                return True
        summary = self._summary()
        failures = int(summary.get("failures", 0))
        errors = int(summary.get("errors", 0))
        if failures:
            return True
        if errors and not env_error:
            return True
        if not headers and not summary and not INCONCLUSIVE_RE.search(self.output) \
                and not any(engine_failure_excused(found, self.unusable_engines)
                            for found in CONTAINER_FAILED_RE.finditer(self.output)):
            # rc 1 sans en-tête ni résumé ni motif connu : un rouge, jamais
            # un non-concluant présumé.
            return True
        return False

    @property
    def inconclusive(self) -> bool:
        if self.flaky_tests:
            return True
        if self.timed_out or self.returncode not in (0, 1):
            return True
        if self.returncode == 1:
            return not self.real_failure
        return False

    @property
    def tests(self) -> int:
        found = RAN_RE.search(self.output)
        return int(found.group(1)) if found else 0

    @property
    def skipped(self) -> int:
        found = OK_RE.search(self.output)
        return int(found.group(1) or 0) if found else 0

    def motive(self) -> str:
        """Pourquoi un run est non concluant, en une ligne."""
        if self.flaky_tests:
            return ("test(s) rouge(s) sur la tête mais vert(s) sur tous les "
                    "préfixes (instable) : %s"
                    % ", ".join(self.flaky_tests[:3]))
        if self.timed_out:
            return "délai dépassé"
        if self.returncode not in (0, 1):
            return "code de retour %s" % self.returncode
        found = INCONCLUSIVE_RE.search(self.output)
        if not found and self.unusable_engines:
            return ("moteur de conteneurs inutilisable dans l'environnement des "
                    "tests (%s)" % ", ".join("%s info : %s" % pair for pair in
                                              sorted(self.unusable_engines)))
        return _one_line(found.group(0)) if found else "résultat non concluant"

    def excerpt(self, lines: int = 3) -> str:
        """L'extrait : la ligne qui porte le motif, puis la fin de la sortie."""
        raw = [line.strip() for line in self.output.splitlines() if line.strip()]
        found = INCONCLUSIVE_RE.search(self.output)
        chosen: list[str] = []
        if found:
            chosen = [line for line in raw if found.group(0) in line][:1]
        chosen += raw[-lines:]
        return " | ".join(dict.fromkeys(chosen))


# ---------------------------------------------------------------------------
# Commandes
# ---------------------------------------------------------------------------


def _decode(data: bytes) -> str:
    return data.decode("utf-8", "replace")


def git(repo: Path, *args: str, check: bool = True,
        input_text: str | None = None, timeout: int = GIT_TIMEOUT
        ) -> subprocess.CompletedProcess:
    """Lance `git -C repo …`, sans terminal interactif."""
    try:
        proc = subprocess.run(
            ["git", "-C", str(repo), *args],
            input=input_text.encode("utf-8") if input_text is not None else None,
            stdin=subprocess.DEVNULL if input_text is None else None,
            capture_output=True,
            timeout=timeout,
        )
    except FileNotFoundError as exc:
        raise GitError("git introuvable : %s" % exc) from exc
    except subprocess.TimeoutExpired as exc:
        raise GitError("git %s : délai de %d s dépassé" % (" ".join(args), timeout)) from exc
    if check and proc.returncode != 0:
        raise GitError(
            "git %s : %s" % (" ".join(args), _decode(proc.stderr).strip() or
                             "code %d" % proc.returncode))
    return proc


def out(repo: Path, *args: str, check: bool = True) -> str:
    return _decode(git(repo, *args, check=check).stdout).strip()


def rev(repo: Path, ref: str) -> str | None:
    """Le commit désigné par `ref`, ou None s'il n'existe pas."""
    proc = git(repo, "rev-parse", "--verify", "--quiet", ref + "^{commit}",
               check=False)
    return _decode(proc.stdout).strip() if proc.returncode == 0 else None


def tree_of(repo: Path, ref: str) -> str | None:
    proc = git(repo, "rev-parse", "--verify", "--quiet", ref + "^{tree}",
               check=False)
    return _decode(proc.stdout).strip() if proc.returncode == 0 else None


def run_command(cmd: list[str], *, cwd: Path | None = None,
                input_text: str | None = None, timeout: int | None = None,
                env: dict | None = None) -> subprocess.CompletedProcess:
    """Lance une commande non-git, sortie texte, sans shell."""
    try:
        return subprocess.run(
            cmd,
            cwd=str(cwd) if cwd else None,
            input=input_text.encode("utf-8") if input_text is not None else None,
            stdin=subprocess.DEVNULL if input_text is None else None,
            capture_output=True,
            timeout=timeout,
            env=env,
        )
    except FileNotFoundError as exc:
        raise PaquetAutoError("commande introuvable : %s" % cmd[0]) from exc
    except subprocess.TimeoutExpired as exc:
        raise PaquetAutoError(
            "commande %s : délai de %s s dépassé" % (cmd[0], timeout)) from exc


def _output(proc: subprocess.CompletedProcess) -> str:
    return (_decode(proc.stdout) + _decode(proc.stderr)).strip()


def _tail(text: str, lines: int = 40) -> str:
    kept = text.rstrip().splitlines()
    if len(kept) > lines:
        kept = ["[… %d lignes au-dessus]" % (len(kept) - lines)] + kept[-lines:]
    return "\n".join(kept)


#: la marque du script d'étiquetage d'un tour (L73, `containers.SHIM`),
#: stable d'une version du shim à l'autre
CONTAINER_SHIM_MARK = "# Posé par ameesh (L73)"


def _is_container_shim(path: str) -> bool:
    """Le script d'étiquetage d'un tour (L73), pas un moteur."""
    try:
        with open(path, encoding="utf-8", errors="replace") as handle:
            return CONTAINER_SHIM_MARK in handle.read(256)
    except OSError:
        return False


def real_container_engines(env: dict) -> dict:
    """#6567 (attendu 2) : les moteurs réels à déclarer au shim du `PATH`.

    Quand le premier `docker`/`podman` du `PATH` est le shim du ménage et que
    `AMEESH_REAL_<MOTEUR>` manque (ou ne désigne pas un exécutable réel), le
    shim sort en 127 : un test de l'arbre jetable n'aurait aucun moteur alors
    que l'hôte en a un (h-16). On déclare le premier vrai moteur du même
    `PATH`, comme le tour le fait pour ses tests ciblés (`install_shims`).
    Sans vrai moteur, rien n'est déclaré : l'absence reste vraie et la sonde
    la prouve.
    """
    path = env.get("PATH") or os.defpath
    declared = {}
    for name in ("docker", "podman"):
        variable = "AMEESH_REAL_%s" % name.upper()
        current = env.get(variable) or ""
        if current and os.access(current, os.X_OK) and not _is_container_shim(current):
            continue
        first = shutil.which(name, path=path)
        if not first or not _is_container_shim(first):
            continue
        for directory in path.split(os.pathsep):
            found = shutil.which(name, path=directory or os.curdir)
            if found and not _is_container_shim(found):
                declared[variable] = found
                break
    return declared


def python_env(worktree: Path) -> dict:
    """L'environnement des outils exécutés depuis l'arbre jetable."""
    env = dict(os.environ)
    env["PYTHONPATH"] = str(worktree / "src")
    env["PYTHONDONTWRITEBYTECODE"] = "1"
    # #6567 : le shim du ménage du PATH reçoit son vrai moteur, comme les
    # tests ciblés d'un tour ; sinon `docker` y sort en 127.
    env.update(real_container_engines(env))
    return env


_PYTHON_INTERPRETER: str | None = None
_PYTHON_OVERRIDE: str | None = None


def _supports_psycopg(python: str) -> bool:
    sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "src"))
    from ameesh.project_python import _supports_psycopg as supports
    return supports(python)


def project_python(worktree: Path) -> str:
    sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "src"))
    from ameesh.project_python import project_python as select
    global _PYTHON_INTERPRETER
    if _PYTHON_OVERRIDE:
        return _PYTHON_OVERRIDE
    if not _PYTHON_INTERPRETER:
        _PYTHON_INTERPRETER = select(worktree)
    return _PYTHON_INTERPRETER


def package_branch(name: str) -> str:
    return PACKAGE_PREFIX + name


def manifest_rel(name: str) -> str:
    return "%s/%s%s.md" % (MANIFEST_DIR, MANIFEST_PREFIX, name)


def validate_name(name: str) -> None:
    if not name or not NAME_RE.fullmatch(name) or ".." in name:
        raise InputError(
            "--name : un nom de paquet sûr attendu (lettres, chiffres, `.`, `_`, "
            "`-` ; pas de `..`), reçu %r" % name)


def validate_branch(repo: Path, branch: str, label: str = "branche") -> None:
    if not branch or branch.startswith("-"):
        raise InputError("%s : une branche attendue, reçu %r" % (label, branch))
    proc = git(repo, "check-ref-format", "--branch", branch, check=False)
    if proc.returncode != 0:
        raise InputError("%s : branche invalide %r" % (label, branch))


def validate_target(repo: Path, target: str) -> None:
    validate_branch(repo, target, "--target")


def fresh_base(repo: Path, target: str) -> str:
    """La tête fraîche d'`origin/<cible>` ; le SHA exact est noté."""
    proc = git(repo, "fetch", "--tags", "--force", "origin", target, check=False,
               timeout=GIT_TIMEOUT)
    if proc.returncode != 0:
        raise PaquetAutoError(
            "fetch origin %s : %s" % (target, _decode(proc.stderr).strip()))
    sha = rev(repo, "origin/" + target) or rev(repo, "FETCH_HEAD")
    if not sha:
        raise PaquetAutoError("origin/%s introuvable après fetch" % target)
    return sha


def remote_branch(repo: Path, branch: str) -> str | None:
    """La tête distante de `branch`, ou None (réseau indisponible = doute)."""
    proc = git(repo, "ls-remote", "--heads", "origin",
               "refs/heads/" + branch, check=False, timeout=GIT_TIMEOUT)
    if proc.returncode != 0:
        raise PaquetAutoError(
            "ls-remote origin %s : %s" % (branch, _decode(proc.stderr).strip()))
    text = _decode(proc.stdout).strip()
    return text.split()[0] if text else None


# ---------------------------------------------------------------------------
# Verrou de composition (#4329)
# ---------------------------------------------------------------------------


def package_name(branch: str) -> str:
    """Le nom de paquet d'une branche `integration/paquet-<nom>`, sinon ""."""
    if not branch.startswith(PACKAGE_PREFIX):
        return ""
    name = branch[len(PACKAGE_PREFIX):]
    return name if NAME_RE.fullmatch(name) else ""


def _git_common_dir(repo: Path) -> Path:
    """Le `.git` **commun** du dépôt (les worktrees le partagent)."""
    text = out(repo, "rev-parse", "--path-format=absolute",
               "--git-common-dir", check=False).strip()
    if not text:
        text = out(repo, "rev-parse", "--git-common-dir").strip()
    path = Path(text)
    if not path.is_absolute():
        path = repo / path
    return path.resolve()


def package_lock_path(repo: Path, name: str) -> Path:
    """Le verrou de composition d'un nom de paquet, sous le `.git` commun."""
    return _git_common_dir(repo) / LOCK_DIR / (name + ".lock")


def holder_label(host: str, pid: int) -> str:
    """« hôte:pid », ou ce qu'on sait du détenteur du verrou."""
    if host and pid > 0:
        return "%s:%d" % (host, pid)
    if host:
        return host
    return "hôte inconnu"


def _read_holder(path: Path) -> tuple[str, int] | None:
    """`hôte:pid` écrit dans le verrou, sans croire qu'il est vivant."""
    try:
        text = path.read_text(encoding="ascii", errors="replace").strip()
    except OSError:
        return None
    host, sep, pid = text.rpartition(":")
    if not sep:
        return (host, 0) if host else None
    try:
        return host, int(pid)
    except ValueError:
        return host, 0


#: #4329 : les verrous tenus par ce processus (le sien ne se refuse pas)
_HELD_LOCKS: set[str] = set()


class PackageLock:
    """Le verrou `flock` d'une composition, par nom de paquet (#4329).

    Il vit sous le `.git` commun : le tick et une composition lancée à la main,
    dans le même clone, se voient. Le noyau le rend à la mort du processus,
    donc le worktree d'une tentative morte reste nettoyable sans expiration à
    deviner. Le fichier ne porte `hôte:pid` que pour le message de refus ; la
    vérité de la vie est le `flock`, jamais le pid relu.
    """

    def __init__(self, repo: Path, name: str) -> None:
        self.repo = repo
        self.name = name
        self.path = package_lock_path(repo, name)
        self.fd: int | None = None

    def acquire(self) -> None:
        try:
            self.path.parent.mkdir(parents=True, exist_ok=True, mode=0o700)
            fd = os.open(str(self.path), os.O_CREAT | os.O_RDWR, 0o644)
        except OSError as exc:
            raise PaquetAutoError(
                "verrou de composition %s : %s" % (self.path, exc)) from exc
        try:
            fcntl.flock(fd, fcntl.LOCK_EX | fcntl.LOCK_NB)
        except BlockingIOError:
            holder = _read_holder(self.path) or ("", 0)
            os.close(fd)
            raise CompositionBusy(self.name, *holder) from None
        except OSError as exc:
            os.close(fd)
            raise PaquetAutoError(
                "verrou de composition %s : %s" % (self.path, exc)) from exc
        self.fd = fd
        _HELD_LOCKS.add(str(self.path.resolve()))
        try:
            data = ("%s:%d\n" % (socket.gethostname(), os.getpid())).encode(
                "ascii", "replace")
            os.lseek(fd, 0, os.SEEK_SET)
            os.write(fd, data)
            os.ftruncate(fd, len(data))
        except OSError as exc:
            # le verrou tient : un message sans identité vaut mieux qu'un refus
            print("identité du verrou non écrite : %s" % exc, file=sys.stderr)

    def release(self) -> None:
        if self.fd is None:
            return
        fd, self.fd = self.fd, None
        _HELD_LOCKS.discard(str(self.path.resolve()))
        try:
            fcntl.flock(fd, fcntl.LOCK_UN)
        finally:
            os.close(fd)

    def __enter__(self) -> "PackageLock":
        self.acquire()
        return self

    def __exit__(self, *_exc) -> None:
        self.release()


def composition_holder(repo: Path, name: str) -> tuple[str, int] | None:
    """Qui tient le verrou de `name`, ou None (#4329).

    Le `flock` partagé non bloquant dit la vie, pas le contenu du fichier : un
    `hôte:pid` laissé par un processus mort ne fait pas refuser. Le processus
    appelant peut être le détenteur : à l'appelant de reconnaître son pid.
    """
    path = package_lock_path(repo, name)
    try:
        fd = os.open(str(path), os.O_RDONLY)
    except OSError:
        return None
    try:
        try:
            fcntl.flock(fd, fcntl.LOCK_SH | fcntl.LOCK_NB)
        except BlockingIOError:
            return _read_holder(path) or ("", 0)
        fcntl.flock(fd, fcntl.LOCK_UN)
        return None
    finally:
        os.close(fd)


def composition_holder_elsewhere(repo: Path, name: str) -> tuple[str, int] | None:
    """Le détenteur du verrou de `name` s'il n'est pas ce processus (#4329)."""
    if str(package_lock_path(repo, name).resolve()) in _HELD_LOCKS:
        return None
    return composition_holder(repo, name)


# ---------------------------------------------------------------------------
# Worktree jetable
# ---------------------------------------------------------------------------


def temporary_parent() -> Path:
    """#5204 : les checkouts volumineux ne vont pas dans le /tmp partagé.

    Garder le TMPDIR choisi par l'appelant hors de /tmp ; sans choix utilisable,
    employer le cache utilisateur, également déclaré par l'unité systemd.
    """
    configured = os.environ.get("TMPDIR")
    parent = Path(configured).resolve() if configured else None
    if parent is None or parent.is_relative_to(Path("/tmp").resolve()):
        parent = Path.home() / ".cache" / "ameesh" / "paquet-auto"
    parent.mkdir(parents=True, exist_ok=True, mode=0o700)
    return parent


def make_worktree(repo: Path, base_sha: str, branch: str) -> Path:
    if rev(repo, "refs/heads/" + branch):
        raise InputError(
            "branche locale %s déjà présente : la supprimer ou choisir un autre "
            "--name" % branch)
    parent = temporary_parent()
    path = Path(tempfile.mkdtemp(prefix="paquet-auto-", dir=str(parent)))
    path.rmdir()
    proc = git(repo, "worktree", "add", "-b", branch, str(path), base_sha,
               check=False)
    if proc.returncode != 0:
        shutil.rmtree(path, ignore_errors=True)
        raise PaquetAutoError(
            "worktree %s : %s" % (path, _decode(proc.stderr).strip()))
    return path


def reader_worktree(repo: Path, base_sha: str) -> Path:
    """Un worktree détaché à `base_sha` : le code de la cible, pas du paquet.

    #6566 : la liste prête est relue juste avant la poussée. `paquet-candidats`
    refuse un code qui n'est pas la tête de la cible (#3782) : l'arbre composé
    (base + unités) ne convient pas, il faut un checkout exact de la base.
    """
    parent = temporary_parent()
    path = Path(tempfile.mkdtemp(prefix="paquet-auto-base-", dir=str(parent)))
    path.rmdir()
    proc = git(repo, "worktree", "add", "--detach", str(path), base_sha,
               check=False)
    if proc.returncode != 0:
        shutil.rmtree(path, ignore_errors=True)
        raise PaquetAutoError("worktree de lecture %s : %s"
                              % (path, _decode(proc.stderr).strip()))
    return path


def _worktrees_on(repo: Path, branch: str) -> list[Path]:
    """Les worktrees du dépôt ouverts sur la branche locale `branch`."""
    listing = out(repo, "worktree", "list", "--porcelain")
    paths: list[Path] = []
    path = None
    for line in listing.splitlines():
        line = line.strip()
        if line.startswith("worktree "):
            path = Path(line[len("worktree "):])
        elif line == "branch refs/heads/" + branch and path is not None:
            paths.append(path)
    return paths


def clear_local_branch(repo: Path, branch: str, log=print) -> None:
    """Retire une branche locale de recomposition.

    #3916 : un worktree `paquet-auto-*` gardé par la tentative précédente sur
    cette branche (diagnostic d'échec, déjà dans son courrier) est fermé ;
    tout autre worktree ouvert dessus refuse la recomposition.

    #4329 : un verrou de composition vivant sur ce nom de paquet refuse avant
    toute suppression. Un worktree dont la composition est morte n'a plus de
    verrou (`flock` rendu par le noyau) : il reste nettoyable comme avant.
    """
    if rev(repo, "refs/heads/" + branch) is None:
        return
    name = package_name(branch)
    if name:
        holder = composition_holder_elsewhere(repo, name)
        if holder is not None:
            raise CompositionBusy(name, *holder)
    for path in _worktrees_on(repo, branch):
        if path == repo or not path.name.startswith("paquet-auto-"):
            raise InputError(
                "branche locale %s encore ouverte dans un worktree (%s) : le "
                "fermer avant de recomposer" % (branch, path))
        log("worktree gardé par la tentative précédente fermé : %s" % path)
        remove_worktree(repo, path)
    if _worktrees_on(repo, branch):
        raise InputError(
            "branche locale %s encore ouverte dans un worktree : le fermer "
            "avant de recomposer" % branch)
    git(repo, "branch", "-D", branch)


def remove_worktree(repo: Path, path: Path) -> None:
    git(repo, "worktree", "remove", "--force", str(path), check=False)
    shutil.rmtree(path, ignore_errors=True)
    git(repo, "worktree", "prune", check=False)


def _worktree_branches(listing: str) -> dict[Path, str]:
    """Le chemin -> branche locale de chaque bloc de `worktree list --porcelain`."""
    branches: dict[Path, str] = {}
    path: Path | None = None
    prefix = "branch refs/heads/"
    for line in listing.splitlines():
        if line.startswith("worktree "):
            path = Path(line[len("worktree "):])
            branches[path] = ""
        elif line.startswith(prefix) and path is not None:
            branches[path] = line[len(prefix):]
    return branches


def prune_stale_worktrees(repo: Path, max_age_hours: float,
                          *, now: float | None = None) -> list[str]:
    """N1 (revue #3807) : borne les worktrees d'échec gardés pour diagnostic.

    Un échec garde son worktree (`--keep-worktree` ou diagnostic) ; une
    minuterie horaire ne doit pas accumuler des checkouts complets. Seuls les
    chemins `paquet-auto-*` **du dépôt** sont retirés, au-delà de
    `max_age_hours` (0 = jamais), puis `worktree prune` ; la branche locale
    reste (petite), le prochain run peut la supprimer avec `--force`.

    #4329 : un worktree dont le paquet a une composition vivante (verrou tenu
    par un autre processus) n'est jamais purgé, quel que soit son âge.
    """
    if max_age_hours <= 0:
        return []
    cutoff = (time.time() if now is None else float(now)) - max_age_hours * 3600
    listing = out(repo, "worktree", "list", "--porcelain", check=False)
    branches = _worktree_branches(listing)
    removed: list[str] = []
    for line in listing.splitlines():
        if not line.startswith("worktree "):
            continue
        path = Path(line[len("worktree "):])
        if path == repo or not path.name.startswith("paquet-auto-"):
            continue
        name = package_name(branches.get(path, ""))
        if name:
            holder = composition_holder_elsewhere(repo, name)
            if holder is not None:
                continue
        try:
            stamp = max(path.stat().st_ctime, path.stat().st_mtime)
        except OSError:
            continue
        if stamp >= cutoff:
            continue
        git(repo, "worktree", "remove", "--force", str(path), check=False)
        shutil.rmtree(path, ignore_errors=True)
        removed.append(str(path))
    if removed:
        git(repo, "worktree", "prune", check=False)
    return removed


# ---------------------------------------------------------------------------
# Liste calculée
# ---------------------------------------------------------------------------


def _errors_by_lot(payload: dict) -> list[Discarded]:
    """Les lots en erreur de lecture du nouveau format (#3813)."""
    entries = payload.get("errors_by_lot")
    if entries is None:
        return []
    if not isinstance(entries, list):
        raise PaquetAutoError(
            "paquet-candidats : errors_by_lot n'est pas une liste")
    out: list[Discarded] = []
    for entry in entries:
        if not isinstance(entry, dict):
            raise PaquetAutoError(
                "paquet-candidats : entrée errors_by_lot illisible : %r" % (entry,))
        try:
            lot = int(entry["work_item"])
        except (KeyError, TypeError, ValueError) as exc:
            raise PaquetAutoError(
                "paquet-candidats : errors_by_lot sans work_item : %r" % (entry,)) from exc
        if lot <= 0:
            raise PaquetAutoError(
                "paquet-candidats : errors_by_lot, lot invalide %r" % lot)
        error = " ".join(str(entry.get("error") or "").split())
        out.append(Discarded(
            lot, "erreur paquet-candidats, jamais composé",
            error or "motif non fourni"))
    return out


def error_discards(payload: dict) -> list[Discarded]:
    """Les lots que `paquet-candidats` rend en erreur, jamais composés."""
    return _errors_by_lot(payload)


def check_exclude_reasons(requested: list[int], reasons: list[str]) -> None:
    """#4201 : `--exclude-reason` s'apparie par ordre aux `--exclude`."""
    if len(reasons) > len(requested):
        raise InputError(
            "--exclude-reason : %d raison(s) pour %d --exclude ; apparier "
            "chaque raison à son lot, dans l'ordre"
            % (len(reasons), len(requested)))


def exclude_discards(requested: list[int], reasons: list[str],
                     error_lots: set[int]) -> list[Discarded]:
    """#4201 : les lots retirés par `--exclude`, avec le motif du pilote.

    `--exclude-reason` est répétable et s'apparie **par ordre** aux `--exclude` :
    la i-ème raison décrit le i-ème lot exclu. Sans raison, le motif neutre
    « exclu par le pilote » ; une raison de plus que de lots est une entrée
    invalide (faute de frappe plutôt que motif perdu). Un lot déjà écarté par la
    liste calculée n'est pas répété au manifeste mais consomme sa raison :
    l'appariement des suivants ne glisse pas.
    """
    check_exclude_reasons(requested, reasons)
    discards: list[Discarded] = []
    seen: set[int] = set()
    for index, lot in enumerate(requested):
        if lot in seen:
            continue
        seen.add(lot)
        if lot in error_lots:
            continue
        reason = reasons[index].strip() if index < len(reasons) else ""
        discards.append(Discarded(
            lot, reason or EXCLUDE_DEFAULT_REASON,
            "--exclude, recomposition sans lui"))
    return discards


def composition_specs(repo: Path, reference: str, name: str) -> list[tuple[list[int], str]]:
    """#4581 : la table `## Composition` du manifeste gelé à `reference`."""
    sha = rev(repo, reference)
    if sha is None:
        raise InputError("--keep-composition : référence %r introuvable" % reference)
    rel = manifest_rel(name)
    show = git(repo, "show", "%s:%s" % (sha, rel), check=False)
    if show.returncode != 0:
        raise InputError(
            "--keep-composition : manifeste %s illisible sur %s : %s"
            % (rel, sha[:12], _tail(_decode(show.stderr))))
    try:
        return parse_composition_table(_decode(show.stdout))
    except InputError as error:
        raise InputError(
            "--keep-composition : %s illisible sur %s : %s"
            % (rel, sha[:12], error)) from error


def same_source(worktree: Path, composed: str, candidate: str) -> bool:
    """Le candidat pointe-t-il le commit de la source **composée** ?

    La table `## Composition` écrit la source abrégée (#3629, `--short=12`) ;
    on la résout donc dans le worktree avant de comparer au SHA complet du
    candidat. Une source composée absente du clone n'est jamais reprise en
    silence : elle va à la coupure suivante (l'invariant « second parent de la
    tête gelée » est ainsi vérifié, pas supposé). Le SHA du candidat est résolu
    de même quand il est présent.
    """
    if not composed or not candidate:
        return False
    resolved = rev(worktree, composed)
    if resolved is None:
        return False
    return resolved == (rev(worktree, candidate) or candidate)


def keep_composition(candidates: list[Candidate],
                     specs: list[tuple[list[int], str]], *,
                     worktree: Path,
                     already_discarded: set[int]) -> tuple[list[Candidate], list[Discarded]]:
    """#4581 : restreint la liste prête aux membres de la composition précédente.

    `specs` vient du manifeste gelé de la composition à conserver ;
    `already_discarded` porte les lots déjà écartés (erreurs de la liste
    calculée, `--exclude`, groupe parallèle). Un membre n'est repris que sur sa
    source composée : une nouvelle tête, un lot sorti de la liste prête ou les
    lots d'une unité de résolution sont écrits aux écartés pour la coupure
    suivante, jamais composés ici. Tout lot prêt qui n'était pas dans la
    composition précédente est nommé de même.
    """
    by_lot = {candidate.lot: candidate for candidate in candidates}
    members: list[Candidate] = []
    discarded: list[Discarded] = []
    seen: set[int] = set(already_discarded)
    for lots, source in specs:
        if any(lot in seen for lot in lots):
            seen.update(lots)
            continue
        seen.update(lots)
        if len(lots) != 1:
            # paquet-auto ne compose pas de résolution (jugement humain) ; une
            # telle unité ne se reprend pas sans la relire. Chaque lot de
            # l'unité est nommé aux écartés : sinon les suivants ne seraient
            # ni composés ni expliqués au contrôle d'inclusion (#4593).
            detail = ("unité de résolution (%s), non reprise par une recomposition"
                      % ", ".join("#%d" % lot for lot in lots))
            discarded.extend(Discarded(lot, KEEP_NOT_READY_REASON, detail)
                             for lot in lots)
            continue
        lot = lots[0]
        candidate = by_lot.get(lot)
        if candidate is None:
            discarded.append(Discarded(
                lot, KEEP_NOT_READY_REASON,
                "composé %s, plus dans la liste prête" % source[:12]))
            continue
        if rev(worktree, source) is None:
            # la source composée est le second parent d'une fusion de la tête
            # gelée : absente du clone, elle ne se reprend pas en silence.
            discarded.append(Discarded(
                lot, KEEP_NOT_READY_REASON,
                "source composée %s introuvable dans le clone" % source[:12]))
            continue
        if not same_source(worktree, source, candidate.source):
            discarded.append(Discarded(
                lot, KEEP_REFROZEN_REASON,
                "composé %s, tête actuelle %s : pas de ci de paquet sur la "
                "nouvelle tête" % (source[:12], candidate.source[:12])))
            continue
        candidate.source = source
        members.append(candidate)
    for candidate in candidates:
        if candidate.lot in seen:
            continue
        seen.add(candidate.lot)
        discarded.append(Discarded(
            candidate.lot, KEEP_READY_REASON,
            "hors de la composition conservée ; non admis, coupure suivante"))
    return members, discarded


def unique_discarded(items: list[Discarded],
                     units: list[Unit] = ()) -> list[Discarded]:
    """#6579 : conserver chaque motif une fois, dans son ordre d'apparition.

    #6727 : un écart d'un passage précédent (conflit avec une unité retirée
    depuis) tombe si la recomposition a composé son lot (`units`) : un lot
    composé n'est jamais aux écartés du même manifeste.
    """
    composed = {unit.lot for unit in units}
    seen: set[tuple[int, str]] = set()
    result: list[Discarded] = []
    for item in items:
        key = (item.lot, item.reason)
        if item.lot in composed:
            continue
        if key not in seen:
            seen.add(key)
            result.append(item)
    return result


def changed_units(units: list[Unit], current: dict[int, str]) -> list[Discarded]:
    """#6566 : les unités dont le gel a changé depuis la composition.

    `current` est la source courante de chaque lot prêt, relue sur la base
    exacte juste avant la poussée. Un lot sorti de la liste prête (build,
    erreur, conflit) ou regelé (nouvelle source) ne se pousse pas sur la tête
    composée : il va à la coupure suivante avec le motif de #4581, et le paquet
    est recomposé sans lui. Une unité de résolution n'a qu'une source composée
    et suit la même règle.
    """
    changes: list[Discarded] = []
    for unit in units:
        source = current.get(unit.lot)
        if source is None:
            changes.append(Discarded(
                unit.lot, KEEP_NOT_READY_REASON,
                "composé %s, plus dans la liste prête" % unit.source[:12]))
        elif source != unit.source:
            changes.append(Discarded(
                unit.lot, KEEP_REFROZEN_REASON,
                "composé %s, tête actuelle %s : pas de ci de paquet sur la "
                "nouvelle tête" % (unit.source[:12], source[:12])))
    return changes


def compute_candidates(worktree: Path, target: str, base_sha: str,
                       ready_out: Path, *, tolerate_landing: bool = False
                       ) -> tuple[list[Candidate], dict]:
    """`paquet-candidats.py` **du worktree**, à la base exacte du paquet.

    #3813 : rc 3 est une liste utilisable avec des lots en erreur ; ces lots
    sont écartés (`errors_by_lot`), jamais composés. rc 1/2, ou un rc 3 sans
    `errors_by_lot`, interrompent : on ne compose pas sur une liste dont la
    lecture globale a échoué.

    #6981 : avec `tolerate_landing`, le décalage #3782 est assumé
    (`--allow-code-mismatch`) puis relu ici : il ne passe que si le code est
    `base_sha`, propre, et que la référence n'en diffère que par des commits
    d'atterrissage de coupure (§12). Tout autre décalage reste un refus.
    """
    script = worktree / CANDIDATES_SCRIPT
    if not script.is_file():
        raise PaquetAutoError(
            "%s absent de l'arbre %s : la liste calculée ne peut pas être lue"
            % (CANDIDATES_SCRIPT, worktree))
    python = project_python(worktree)
    cmd = [python, str(script), "--repo", str(worktree),
           "--target", target, "--base", base_sha, "--json",
           "--ready-out", str(ready_out)]
    if tolerate_landing:
        cmd.append("--allow-code-mismatch")
    proc = run_command(cmd, cwd=worktree, timeout=DEFAULT_TIMEOUT,
                       env=python_env(worktree))
    stdout = _decode(proc.stdout)
    payload = None
    if stdout.lstrip().startswith("{"):
        try:
            payload = json.loads(stdout)
        except ValueError:
            payload = None
    if proc.returncode not in (0, 3):
        if payload and payload.get("errors"):
            raise PaquetAutoError(
                "paquet-candidats : liste inutilisable (rc %d), on ne compose "
                "pas dessus : %s"
                % (proc.returncode,
                   "; ".join(str(error) for error in payload["errors"])))
        raise PaquetAutoError(
            "paquet-candidats a refusé (rc %d) : %s"
            % (proc.returncode, _tail(_output(proc))))
    if payload is None:
        raise PaquetAutoError(
            "paquet-candidats : sortie JSON illisible : %s"
            % _tail(stdout or _decode(proc.stderr)))
    if payload.get("schema") != "ameesh-packet-candidates/1":
        raise PaquetAutoError(
            "paquet-candidats : schéma inattendu %r" % payload.get("schema"))
    errored = _errors_by_lot(payload)
    if proc.returncode == 3 and not errored:
        raise PaquetAutoError(
            "paquet-candidats : rc 3 sans errors_by_lot (format antérieur à "
            "#3813), on ne compose pas sur une liste dont l'export est partiel")
    if proc.returncode == 0 and payload.get("errors"):
        raise PaquetAutoError(
            "paquet-candidats : liste incohérente (rc 0 avec des erreurs), on "
            "ne compose pas dessus : %s"
            % "; ".join(str(error) for error in payload["errors"]))
    if payload.get("base_source") != "option":
        raise PaquetAutoError(
            "paquet-candidats n'a pas lu la base imposée (%r) : la liste ne "
            "correspond pas au worktree" % payload.get("base_source"))
    if tolerate_landing:
        refusal = landing_mismatch(worktree, base_sha, payload)
        if refusal:
            raise PaquetAutoError("paquet-candidats a refusé (rc 2) : %s" % refusal)
    error_lots = {item.lot for item in errored}
    by_id = {int(entry["work_item"]): entry for entry in payload.get("candidates") or []}
    candidates: list[Candidate] = []
    for lot in payload.get("ready") or []:
        lot = int(lot)
        if lot in error_lots:
            continue
        entry = by_id.get(lot)
        if entry is None:
            raise PaquetAutoError("paquet-candidats : #%d prêt sans entrée" % lot)
        source = str(entry.get("source") or entry.get("published_head") or "").strip()
        if not source:
            raise PaquetAutoError("paquet-candidats : #%d prêt sans source" % lot)
        remise = entry.get("remise")
        remise_ref = str(remise.get("ref") or "") if isinstance(remise, dict) else ""
        candidates.append(Candidate(
            lot=lot, source=source, branch=str(entry.get("branch") or ""),
            title=str(entry.get("title") or ""), remise_ref=remise_ref))
    return candidates, payload


#: #6891 : `scripts/paquet-candidats.py` chargé en module, pour appliquer ses
#: gardes #3782 (`code_reference`, `code_provenance`, `code_refusal`) sans en
#: recopier la lecture. Le module vient du même arbre que ce script : c'est le
#: code exécuté par le composeur.
_CANDIDATES_GUARD = None


def candidates_guard():
    """Les gardes #3782 de `paquet-candidats.py`, chargées une fois."""
    global _CANDIDATES_GUARD
    if _CANDIDATES_GUARD is None:
        path = Path(__file__).resolve().parent / Path(CANDIDATES_SCRIPT).name
        spec = importlib.util.spec_from_file_location(
            "paquet_candidats_guard", path)
        module = importlib.util.module_from_spec(spec)
        sys.modules["paquet_candidats_guard"] = module
        try:
            spec.loader.exec_module(module)
        except BaseException:
            # #6981 : un chargement en échec ne laisse pas de module à moitié
            # exécuté dans `sys.modules` ; l'appel suivant recharge.
            sys.modules.pop("paquet_candidats_guard", None)
            raise
        _CANDIDATES_GUARD = module
    return _CANDIDATES_GUARD


#: #6981 : les relations #3782 qui disent une cible déplacée (le code de la
#: base lui est antérieur, ou elle a été réécrite). Les autres refus
#: (`absent`, `dirty`, `unknown`) sont une lecture en panne, pas un
#: déplacement : ils ne consomment pas les relances immédiates de
#: `paquet-horaire.py`.
MOVED_RELATIONS = frozenset(("behind", "diverged"))


def cutoff_landing_only(repo: Path, base: str, head: str) -> bool:
    """#6981 : `head` ne diffère de `base` que par des commits d'atterrissage.

    §12 : « sauf pour les seuls commits d'atterrissage d'une coupure (version,
    notes) ». La preuve est celle de la fusion automatique
    (`packet_automerge._cutoff_landing_only`, #3534) : chaque commit de
    l'intervalle porte `ameesh-release:` et ne change que la ligne de version
    de `pyproject.toml` et `docs/versions/*.md`. Illisible = False.
    """
    sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "src"))
    from ameesh.packet_automerge import _cutoff_landing_only
    return _cutoff_landing_only(str(repo), base, head)


def landing_mismatch(worktree: Path, base_sha: str, payload: dict) -> str:
    """#6981 : le refus #3782 d'une liste lue avec `--allow-code-mismatch`.

    "" si le code est la référence, ou s'il est `base_sha`, propre, et que la
    référence n'en diffère que par des atterrissages de coupure ; sinon le
    motif du refus que `paquet-candidats` aurait rendu.
    """
    relation = str(payload.get("code_relation") or "unknown")
    code = str(payload.get("code_sha") or "")
    ref = str(payload.get("code_ref") or "")
    if relation == "same" and not payload.get("code_dirty"):
        return ""
    if (relation == "behind" and code == base_sha and ref
            and not payload.get("code_dirty")
            and cutoff_landing_only(worktree, base_sha, ref)):
        print("paquet-candidats : référence %s en avance sur la base %s par "
              "des atterrissages de coupure seuls, décalage toléré (§12)"
              % (ref[:12], base_sha[:12]), flush=True)
        return ""
    return candidates_guard().code_refusal(
        payload, ref, str(payload.get("code_ref_source") or "project-target")) \
        or "code %s décalé de la référence %s (%s)" % (
            code[:12] or "?", ref[:12] or "?", relation)


def ensure_reference(repo: Path, target: str, base_sha: str) -> None:
    """#6891 : la base de composition est-elle encore la référence du code ?

    `freeze_changes` applique déjà la garde #3782 juste avant la poussée, mais
    après les contrats — 12 min au constat h-11, quand le commit d'atterrissage
    de la coupure arrive pendant la composition. Ici la tête de la cible est
    relue (`fetch` du clone du composeur, donc le clone est à jour) et le code
    de `base_sha` est confronté à cette référence avec les fonctions de
    `paquet-candidats.py` (#3782) : un décalage lève `TargetMoved`, rien n'est
    poussé, et l'appelant recompose aussitôt sur la nouvelle tête
    (`TARGET_MOVED_RC`) sans attendre le tic suivant. La relecture du gel juste
    avant la poussée reste en place : la cible peut encore bouger pendant les
    tests.

    `code_refusal` fait foi : un code qui *contient* la référence (`ahead`)
    passe, comme pour `paquet-candidats` ; seuls les cas qu'il refuse
    (`behind`, `absent`, `diverged`, `dirty`, `unknown`) arrêtent les tests.

    #6981 : une cible avancée par les seuls commits d'atterrissage d'une
    coupure (§12 : version et notes, `[skip ci]`) garde la composition ; la
    relecture du gel tolère ce même décalage (`tolerate_landing`).
    Seuls `behind` et `diverged` lèvent `TargetMoved` (relance immédiate) ; un
    autre refus (`absent`, `dirty`, `unknown`) n'est pas un déplacement de la
    cible et lève `PaquetAutoError` avec sa cause, sans relance immédiate.
    """
    fresh = fresh_base(repo, target)
    if fresh == base_sha:
        return
    if cutoff_landing_only(repo, base_sha, fresh):
        print("cible %s avancée par des atterrissages de coupure seuls (%s → "
              "%s) : composition gardée (§12)"
              % (target, base_sha[:12], fresh[:12]), flush=True)
        return
    # Un worktree de lecture à la base exacte : c'est ce code-là que les tests
    # locaux exécuteraient, et `code_provenance` y lit l'arbre, pas le clone.
    reader = reader_worktree(repo, base_sha)
    try:
        guard = candidates_guard()
        ref, ref_source = guard.code_reference(reader, reader, fresh)
        provenance = guard.code_provenance(reader, ref)
        refusal = guard.code_refusal(provenance, ref, ref_source)
    finally:
        remove_worktree(repo, reader)
    if not refusal:
        return
    relation = str(provenance.get("code_relation") or "unknown")
    if relation not in MOVED_RELATIONS:
        raise PaquetAutoError(
            "référence #3782 illisible pour la cible %s (%s → %s, relation "
            "%s) : %s ; tests locaux non lancés, pas de relance immédiate"
            % (target, base_sha[:12], fresh[:12], relation, refusal))
    raise TargetMoved(
        "la cible %s a avancé pendant la composition (%s → %s) : %s ; "
        "tests locaux non lancés, clone mis à jour, recomposition immédiate"
        % (target, base_sha[:12], fresh[:12], refusal),
        base=base_sha, head=fresh)


def validate_remise_ref(repo: Path, ref: str, lot: int) -> None:
    """#3916 : une remise du lot, sous `refs/ameesh/resolutions/<lot>/`."""
    prefix = "%s%d/" % (REMISE_PREFIX, lot)
    if not ref.startswith(prefix) or len(ref) == len(prefix):
        raise InputError("remise du candidat #%d : ref inattendue %r" % (lot, ref))
    if git(repo, "check-ref-format", ref, check=False).returncode != 0:
        raise InputError("remise du candidat #%d : ref invalide %r" % (lot, ref))


def ensure_source(worktree: Path, candidate: Candidate) -> None:
    """Résout la source dans le clone ; la fetch au besoin (remise, sinon
    branche)."""
    sha = rev(worktree, candidate.source)
    if sha is None and candidate.remise_ref:
        # #3916 : une remise n'est sur aucune branche : fetcher la branche ne
        # l'apporte jamais ; on fetch sa ref, validée, sous un nom local.
        validate_remise_ref(worktree, candidate.remise_ref, candidate.lot)
        local = REMISE_LOCAL + candidate.remise_ref[len(REMISE_PREFIX):]
        git(worktree, "fetch", "--no-tags", "origin",
            "+%s:%s" % (candidate.remise_ref, local), check=False,
            timeout=GIT_TIMEOUT)
        sha = rev(worktree, candidate.source)
    if sha is None and candidate.branch:
        # N2 (revue #3807) : la branche vient de la base, jamais d'un argument
        # de commande ; `git fetch` la reçoit après validation, pas avant.
        validate_branch(worktree, candidate.branch,
                        "branche du candidat #%d" % candidate.lot)
        git(worktree, "fetch", "--no-tags", "origin", candidate.branch, check=False)
        sha = rev(worktree, candidate.source)
    if sha is None:
        raise PaquetAutoError(
            "source %s de #%d introuvable dans le clone : la branche%s n'a "
            "pas été fetchée (ou la source a été réécrite)"
            % (candidate.source[:12], candidate.lot,
               " ou la remise %s" % candidate.remise_ref
               if candidate.remise_ref else ""))
    candidate.source = sha


# ---------------------------------------------------------------------------
# Composition
# ---------------------------------------------------------------------------


def _compose(worktree: Path, candidate: Candidate, carrier: int,
             compose_script: Path, record_package: bool,
             expect_pushed: str = "") -> subprocess.CompletedProcess:
    cmd = [project_python(worktree), str(compose_script), "--repo", str(worktree),
           "--lot", str(candidate.lot), "--source", candidate.source]
    if record_package:
        cmd += ["--record-package", str(carrier)]
        if expect_pushed:
            # #4501 : la tête poussée dont ce composeur détient le bail ; le
            # `packet record` refuse une autre tête gelée.
            cmd += ["--expect-pushed", expect_pushed]
    return run_command(cmd, cwd=worktree, timeout=DEFAULT_TIMEOUT,
                       env=python_env(worktree))


def _abort_merge(worktree: Path, before: str) -> None:
    proc = git(worktree, "merge", "--abort", check=False)
    if proc.returncode != 0:
        raise PaquetAutoError(
            "conflit : `git merge --abort` a échoué : %s"
            % _decode(proc.stderr).strip())
    head = rev(worktree, "HEAD")
    if head != before:
        raise PaquetAutoError(
            "conflit : la tête a bougé pendant l'annulation (%s → %s)"
            % (before[:12], (head or "?")[:12]))
    if rev(worktree, "MERGE_HEAD") is not None:
        raise PaquetAutoError("conflit : MERGE_HEAD subsiste après l'annulation")
    status = out(worktree, "status", "--porcelain")
    if status:
        raise PaquetAutoError(
            "conflit : l'arbre n'est pas propre après l'annulation :\n%s" % status)


def check_epoch(carrier: int, worktree: Path) -> None:
    """#4113 : garde en lecture seule, dans le Python du projet.

    Le processus parent reste stdlib-only pour le lancement manuel système.
    La garde transactionnelle finale reste requise après ces lectures.
    """
    if not (os.environ.get("AGENT_MAIL_NAME") or os.environ.get("AMEESH_ROLE")):
        return
    code = """
import sys
from ameesh import config, db, pilot_gesture
conn = None
try:
    cfg = config.load()
    conn = db.connect(cfg)
    if conn is None:
        raise RuntimeError("base absente pour vérifier l'époque")
    pilot_gesture.gate(cfg, conn, "packet", "packet compose #%d" % int(sys.argv[1]))
finally:
    if conn is not None:
        conn.close()
"""
    # #4245 : la garde attend la reprise d'un rôle réservé à cet hôte
    # (redémarrage d'exécuteur, 90 s au plus) avant de conclure
    proc = run_command([project_python(worktree), "-c", code, str(carrier)],
                       cwd=worktree, env=python_env(worktree), timeout=EPOCH_CHECK_TIMEOUT)
    if proc.returncode:
        error = EpochLost if REFUSAL_RE.search(_output(proc)) else PaquetAutoError
        raise error("composition interrompue avant la prochaine opération : %s"
                    % _output(proc))


def check_pause(worktree: Path, project: str, target: str) -> None:
    """#5084 : relit la pause de la cible (`packet_pause`) avant de pousser.

    Le tic lit la pause une fois, à son départ ; une tentative qui dure (tests
    par unité) ignorait une pause posée ensuite. `project` vide (lancement à la
    main, sans `--pause-project`) : aucune lecture. Une lecture illisible
    arrête aussi la poussée : on ne pousse pas sur un état inconnu.
    """
    if not project:
        return
    code = """
import sys
from ameesh import config, db, packet_pause
conn = None
try:
    conn = db.connect(config.load())
    if conn is None:
        raise RuntimeError("base absente pour relire la pause")
    data = packet_pause.active(conn, project=sys.argv[1], target=sys.argv[2])
    if data:
        print(packet_pause.describe(data))
        sys.exit(%d)
finally:
    if conn is not None:
        conn.close()
""" % PAUSE_ACTIVE_EXIT
    proc = run_command([project_python(worktree), "-c", code, project, target],
                       cwd=worktree, env=python_env(worktree), timeout=EPOCH_CHECK_TIMEOUT)
    if proc.returncode == PAUSE_ACTIVE_EXIT:
        raise PacketPaused("pause posée pendant la tentative, relue avant de "
                           "pousser (%s %s) : rien n'est poussé"
                           % (target, _decode(proc.stdout).strip()))
    if proc.returncode:
        raise PacketPaused("pause illisible avant de pousser : rien n'est poussé : %s"
                           % _tail(_output(proc), 5))


def discarded_sources(payload: dict, candidates: list[Candidate],
                      lots) -> dict[int, str]:
    """#5084 : la source de chaque lot écarté, quand la liste calculée la donne."""
    by_lot = {candidate.lot: candidate.source for candidate in candidates}
    for entry in payload.get("candidates") or []:
        lot = int(entry.get("work_item") or 0)
        source = str(entry.get("source") or entry.get("published_head") or "").strip()
        if lot and source:
            by_lot.setdefault(lot, source)
    return {lot: by_lot[lot] for lot in lots if by_lot.get(lot)}


def contained_discard(worktree: Path, source: str, head: str,
                      removed: dict[int, str]) -> tuple[int, str] | None:
    """#5084 : le lot écarté dont `source` contient la source, ou None.

    Composer une telle source porterait au paquet les commits d'un lot que le
    manifeste dit écarté (h-09 : #5038 écarté, #5077 gardé alors que sa tête
    contient celle de #5038). Une source écartée déjà dans la tête composée
    (cible ou unité précédente) n'ajoute rien ; une source absente du clone
    n'est l'ancêtre d'aucune source présente.
    """
    for lot, gone in removed.items():
        if rev(worktree, gone) is None:
            continue
        if git(worktree, "merge-base", "--is-ancestor", gone, head,
               check=False).returncode == 0:
            continue
        if git(worktree, "merge-base", "--is-ancestor", gone, source,
               check=False).returncode == 0:
            return lot, gone
    return None


def compose_candidates(worktree: Path, candidates: list[Candidate], carrier: int,
                       *, compose_script: Path, record_package: bool,
                       expect_pushed: str = "",
                       removed: dict[int, str] | None = None,
                       log=print, guard=None) -> Composition:
    """Compose les candidats dans l'ordre ; un conflit est écarté, jamais résolu.

    `removed` (#5084) : lot → source des lots déjà écartés ; un candidat dont
    la source en contient une est écarté à son tour, et un lot écarté ici
    (conflit, contenance) s'y ajoute pour les candidats suivants.
    """
    units: list[Unit] = []
    discarded: list[Discarded] = []
    removed = dict(removed or {})
    for candidate in candidates:
        if guard is not None:
            guard()
        ensure_source(worktree, candidate)
        before = rev(worktree, "HEAD")
        if before is None:
            raise PaquetAutoError("worktree sans commit")
        contained = contained_discard(worktree, candidate.source, before, removed)
        if contained is not None:
            lot, gone = contained
            detail = ("sa source %s contient celle de #%d (%s), écarté"
                      % (candidate.source[:12], lot, gone[:12]))
            discarded.append(Discarded(candidate.lot, CONTAINS_DISCARDED_REASON, detail))
            removed[candidate.lot] = candidate.source
            log("écarté #%d : %s (%s)" % (candidate.lot, CONTAINS_DISCARDED_REASON, detail))
            continue
        if git(worktree, "merge-base", "--is-ancestor", candidate.source, before,
               check=False).returncode == 0:
            discarded.append(Discarded(
                candidate.lot,
                "source déjà portée par une unité précédente",
                "aucune unité propre : le contenu est déjà dans le paquet"))
            log("écarté #%d : source déjà portée par une unité précédente"
                % candidate.lot)
            continue
        proc = _compose(worktree, candidate, carrier, compose_script, record_package,
                        expect_pushed=expect_pushed)
        if proc.returncode == 0:
            commit = rev(worktree, "HEAD")
            if commit is None or commit == before:
                raise PaquetAutoError(
                    "#%d : paquet-compose a rendu 0 sans nouveau commit" % candidate.lot)
            units.append(Unit(candidate.lot, candidate.source, commit))
            log("unité %d/%d : #%d %s → %s"
                % (len(units), len(candidates), candidate.lot,
                   candidate.source[:12], commit[:12]))
            continue
        stderr = _decode(proc.stderr).strip()
        if proc.returncode == 1:
            files = [line.split(":", 1)[1].strip()
                     for line in stderr.splitlines() if line.strip().startswith("conflit :")]
            _abort_merge(worktree, before)
            detail = "fichiers : %s" % ", ".join(files) if files else ""
            discarded.append(Discarded(candidate.lot, "conflit, coupure suivante", detail))
            removed[candidate.lot] = candidate.source
            log("écarté #%d : conflit, coupure suivante%s"
                % (candidate.lot, (" (%s)" % detail) if detail else ""))
            continue
        after = rev(worktree, "HEAD")
        # Un échec de la seule liaison en base ne défait pas la composition
        # (#2443) : le commit existe, `packet record` le reprendra à la fin.
        if after != before and "liaison au paquet" in stderr:
            units.append(Unit(candidate.lot, candidate.source, after))
            log("unité %d/%d : #%d %s → %s (liaison en base différée)"
                % (len(units), len(candidates), candidate.lot,
                   candidate.source[:12], after[:12]))
            continue
        raise PaquetAutoError(
            "#%d : paquet-compose a refusé (rc %d) : %s"
            % (candidate.lot, proc.returncode, _tail(stderr)))
    head = rev(worktree, "HEAD")
    if head is None:
        raise PaquetAutoError("worktree sans commit après composition")
    return Composition(units=units, discarded=discarded, head=head)


def recompose(worktree: Path, base_sha: str, candidates: list[Candidate],
              carrier: int, *, compose_script: Path, record_package: bool,
              expect_pushed: str = "",
              removed: dict[int, str] | None = None,
              log=print, guard=None) -> Composition:
    """Repart de la base exacte et recompose, sans les unités fautives."""
    if guard is not None:
        guard()
    git(worktree, "reset", "--hard", base_sha)
    git(worktree, "clean", "-fd")
    return compose_candidates(worktree, candidates, carrier,
                              compose_script=compose_script,
                              record_package=record_package,
                              expect_pushed=expect_pushed,
                              removed=removed,
                              log=log, guard=guard)


# ---------------------------------------------------------------------------
# Contrôles locaux
# ---------------------------------------------------------------------------


@dataclass(frozen=True)
class Tooling:
    """#6274 : les outils de contrats du projet composé.

    `declared` : le bloc `prevalidation` du projet nomme un lanceur de part
    autre que la convention d'ameesh. Le composeur lit alors le plan par le
    `plan_script` déclaré (contrat d'appel de #6186), garde les identités de
    module telles que l'adaptateur les rend (opaques : `x.spec.ts`) et joue
    les contrats par le lanceur déclaré (`run --plan --part --driver
    --flow`), jamais par `unittest`. Sinon, la convention d'ameesh s'applique
    à l'identique (liste « toujours », modules `tests.*`, unittest natif).
    """
    plan_script: str = DEFAULT_PLAN_SCRIPT
    parts_script: str = ""
    contracts: tuple[str, ...] = (CONTRACT_ALWAYS,)
    project: str = ""

    @property
    def declared(self) -> bool:
        return bool(self.parts_script) and self.parts_script != DEFAULT_PARTS_SCRIPT


LEGACY_TOOLING = Tooling()
_TOOLING: Tooling = LEGACY_TOOLING


def resolve_tooling(project: str, *, log=print) -> Tooling:
    """Le bloc `prevalidation` du projet, relu au canon configuré (#6274).

    Sans projet, ou sans bloc lisible, la convention d'ameesh (N-1) : le
    composeur d'un dépôt ameesh se comporte comme avant. Le nom du projet
    vient de l'appelant (`--pause-project`), jamais d'un littéral du moteur.
    """
    project = (project or "").strip()
    if not project:
        return LEGACY_TOOLING
    try:
        sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "src"))
        from ameesh import config, prevalidation_calls
        params = prevalidation_calls.parameters(config.load(), project).params
    except (Exception, SystemExit) as exc:  # noqa: BLE001 — absence = convention
        log("outils de contrats : convention d'ameesh (bloc de %s illisible : %s)"
            % (project, _one_line(str(exc))[:200]))
        return LEGACY_TOOLING
    contracts = tuple(str(path) for path in (getattr(params, "contracts", ()) or ())
                      if str(path).strip()) or (CONTRACT_ALWAYS,)
    return Tooling(plan_script=str(getattr(params, "plan_script", None)
                                   or DEFAULT_PLAN_SCRIPT),
                   parts_script=str(getattr(params, "parts_script", None) or ""),
                   contracts=contracts, project=project)


def _contract_names(worktree: Path, rel: str) -> list[str]:
    path = worktree / rel
    if not path.is_file():
        raise PaquetAutoError("%s absent de l'arbre composé" % rel)
    names = []
    for line in path.read_text(encoding="utf-8").splitlines():
        name = line.split("#", 1)[0].strip()
        if name:
            names.append(name)
    return names


def declared_plan(worktree: Path, tooling: Tooling, *, base: str,
                  timeout: float) -> dict:
    """Le plan du projet par son adaptateur déclaré (#6186, #6274).

    Même appel que la prévalidation (`plan --out --event --ref --base --mode
    lot`, `AMEESH_PROJECT`), script lu dans l'arbre composé ; le bloc
    `contract_plan` est relu par le lecteur de ce script (schéma stable) et
    doit porter la tête composée. Aucune condition sur la présence de
    `src/ameesh` dans le dépôt candidat.
    """
    script = worktree / tooling.plan_script
    if not script.is_file():
        raise PaquetAutoError("%s absent de l'arbre composé" % tooling.plan_script)
    with tempfile.TemporaryDirectory(prefix="contract-plan-") as temporary:
        output = Path(temporary) / "plan.json"
        env = python_env(worktree)
        if tooling.project:
            env["AMEESH_PROJECT"] = tooling.project
        proc = run_command(
            [project_python(worktree), str(script), "plan", "--out", str(output),
             "--event", "workflow_dispatch", "--ref", "", "--base", base,
             "--mode", "lot"], cwd=worktree, timeout=max(0.1, timeout), env=env)
        if proc.returncode:
            raise PaquetAutoError("plan déclaré illisible : " + _tail(_output(proc), 3))
        try:
            plan = json.loads(output.read_text(encoding="utf-8"))
            shared = plan["contract_plan"]
            sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "src"))
            from ameesh import contract_plan
            contract_plan.consume(shared)
            if shared["commit"] != _decode(git(worktree, "rev-parse", "HEAD").stdout).strip():
                raise ValueError("contract plan candidate differs")
        except (OSError, ValueError, KeyError, TypeError) as exc:
            raise PaquetAutoError("plan déclaré illisible : " + str(exc)) from exc
    return plan


def _contract_choice(plan: dict, shared: dict) -> list[str]:
    """Contrats + impactés d'une sélection sûre, sinon les seuls contrats.

    #7091 : une carte périmée vaut une carte absente (celle du 27/10, 386 h,
    mettait 1324 modules dans la passe). #7092 : une carte fraîche ne suffit
    pas ; sur une fondation (`tests/support.py`), un retrait ou une
    sélection incertaine, les impactés sont toute la suite (1011 modules),
    déjà due à la CI complète du paquet (canon §12). Un plan sans `lot_safe`
    (adaptateur déclaré d'un autre projet) est jugé sur `full_suite_required`.
    """
    card = plan.get("map") or {}
    safe = (card.get("used") and not card.get("stale")
            and plan.get("lot_safe", True) and not shared.get("full_suite_required"))
    return shared["modules"] if safe else shared["contracts"]


def declared_contract_modules(worktree: Path, tooling: Tooling, *, base: str = "",
                              timeout: float = GIT_TIMEOUT) -> list[str]:
    """#6274 : contrats déclarés + plan actif, identités préservées.

    Ni préfixe `tests.`, ni modules propres à ameesh : une identité vaut si
    l'inventaire du plan la porte, sinon si c'est un fichier de l'arbre.
    """
    modules: list[str] = []
    for rel in tooling.contracts:
        modules.extend(_contract_names(worktree, rel))
    inventory: set[str] = set()
    if base:
        plan = declared_plan(worktree, tooling, base=base, timeout=timeout)
        shared = plan["contract_plan"]
        inventory = set(shared.get("inventory") or ())
        if shared["active"]:
            modules.extend(_contract_choice(plan, shared))
    out: list[str] = []
    for module in modules:
        if module in out:
            continue
        if module not in inventory and not (worktree / module).is_file():
            raise PaquetAutoError(
                "module de test %s absent de l'arbre composé et de l'inventaire du plan"
                % module)
        out.append(module)
    return out


def contract_modules(worktree: Path, *, base: str = "",
                     timeout: float = GIT_TIMEOUT,
                     tooling: Tooling | None = None) -> list[str]:
    """La liste « toujours », `test_l161f_plan` et `test_cluster`.

    #6274 : un projet au lanceur déclaré passe par
    `declared_contract_modules`."""
    tooling = tooling or _TOOLING
    if tooling.declared:
        return declared_contract_modules(worktree, tooling, base=base, timeout=timeout)
    path = worktree / CONTRACT_ALWAYS
    if not path.is_file():
        raise PaquetAutoError("%s absent de l'arbre composé" % CONTRACT_ALWAYS)
    modules: list[str] = []
    for line in path.read_text(encoding="utf-8").splitlines():
        name = line.split("#", 1)[0].strip()
        if not name:
            continue
        modules.append(name if name.startswith("tests.") else "tests." + name)
    modules.extend(CONTRACT_MODULES)
    if base and (worktree / "src/ameesh/contract_plan.py").is_file():
        # The candidate's L161f serializes the very same registry/impact union
        # as CI and prevalidation. Map absence keeps the packet contract-only
        # fallback; neither the neighbor budget nor the map may drop a contract.
        with tempfile.TemporaryDirectory(prefix="contract-plan-") as temporary:
            output = Path(temporary) / "plan.json"
            proc = run_command(
                [project_python(worktree), str(worktree / tooling.plan_script),
                 "plan", "--out", str(output), "--base", base, "--mode", "lot",
                 "--local-map"], cwd=worktree, timeout=max(0.1, timeout),
                env=python_env(worktree))
            if proc.returncode:
                raise PaquetAutoError("plan commun de contrats illisible : " + _tail(_output(proc), 3))
            try:
                plan = json.loads(output.read_text())
                shared = plan["contract_plan"]
                # Use the reader from this script's environment only for the
                # stable serialized schema; candidate generation owns selection.
                sys.path.insert(0, str(worktree / "src"))
                from ameesh import contract_plan
                contract_plan.consume(shared)
                if shared["commit"] != _decode(git(worktree, "rev-parse", "HEAD").stdout).strip():
                    raise ValueError("contract plan candidate differs")
            except (OSError, ValueError, KeyError) as exc:
                raise PaquetAutoError("plan commun de contrats illisible : " + str(exc)) from exc
            if shared["active"]:
                modules.extend("tests." + name for name in _contract_choice(plan, shared))
    return _dedupe_modules(worktree, modules)


def unit_test_modules(worktree: Path, unit: Unit) -> list[str]:
    """Les modules `tests/test_*.py` apportés par la fusion de l'unité.

    La fusion est comparée à sa première parente : c'est le delta propre de
    l'unité dans le paquet (aucune résolution locale, par construction). Un
    module supprimé par l'unité n'est pas exécuté.
    """
    proc = git(worktree, "diff", "--name-only", unit.commit + "^1", unit.commit,
               check=False)
    if proc.returncode != 0:
        raise PaquetAutoError(
            "unité #%d : diff illisible : %s"
            % (unit.lot, _decode(proc.stderr).strip()))
    modules = []
    for path in _decode(proc.stdout).splitlines():
        path = path.strip()
        if re.fullmatch(r"tests/test_[A-Za-z0-9_]+\.py", path) \
                and (worktree / path).is_file():
            modules.append(path[:-3].replace("/", "."))
    return _dedupe_modules(worktree, modules)


def neighbor_modules(worktree: Path, composition: Composition, base_sha: str,
                     *, budget: int = IMPACT_BUDGET_S) -> tuple[dict, str]:
    """#4025 : les modules voisins de chaque unité, et la note de la carte.

    `paquet-attribution.py --select` de l'arbre composé rend, par unité, les
    modules qui importent un module de test qu'elle modifie (`support`) et
    ceux que la carte d'impact relie aux blocs `src/` qu'elle change
    (`carte`, dans `budget` secondes). Un outil absent ou illisible n'ajoute
    rien et le dit : les tests propres et les contrats restent joués, la
    ci-full tranche. #7195 : une carte qui ne couvre pas un fichier ou un bloc
    changé est dite « incomplète » dans la note, sans bloquer.
    """
    script = worktree / ATTRIBUTION_SCRIPT
    if not composition.units:
        return {}, ""
    if not script.is_file():
        return {}, "modules voisins non sélectionnés : %s absent" % ATTRIBUTION_SCRIPT
    driver = test_driver(worktree)
    payload = {
        "schema": "ameesh-paquet-selection-request/1",
        "base": base_sha, "budget": budget, "driver": driver,
        "units": [{"lot": unit.lot, "unit_sha": unit.commit}
                  for unit in composition.units],
    }
    try:
        proc = run_command(
            [project_python(worktree), str(script), "--select",
             "--repo", str(worktree)],
            cwd=worktree, input_text=json.dumps(payload),
            timeout=GIT_TIMEOUT, env=python_env(worktree))
        report = json.loads(_decode(proc.stdout).strip() or "{}")
    except (PaquetAutoError, ValueError) as exc:
        return {}, "modules voisins non sélectionnés : %s" % _one_line(str(exc))[:200]
    if proc.returncode != 0 or not isinstance(report, dict) \
            or report.get("status") != "ok":
        why = (report.get("why") if isinstance(report, dict) else "") \
            or _tail(_output(proc), 3) or "sortie illisible"
        return {}, "modules voisins non sélectionnés : %s" % _one_line(why)[:200]
    out: dict[int, list[dict]] = {}
    dropped = 0
    spent = 0.0
    unknown_paths: set[str] = set()
    unmapped_blocks: set[str] = set()
    for entry in report.get("units") or []:
        lot = int(entry.get("lot") or 0)
        rows = []
        for row in entry.get("added") or []:
            module = str(row.get("module") or "")
            if not re.fullmatch(r"tests\.test_[A-Za-z0-9_]+", module) \
                    or not (worktree / (module.replace(".", "/") + ".py")).is_file():
                continue
            rows.append({"module": module, "reason": str(row.get("reason") or ""),
                         "files": [str(path) for path in row.get("files") or []]})
        out.setdefault(lot, []).extend(rows)
        dropped += int(entry.get("dropped") or 0)
        spent += float(entry.get("seconds") or 0)
        # #7195 : une carte antérieure ne couvre pas les fichiers/blocs neufs ;
        # le dire, sinon la série paraît complète alors qu'elle ne l'est pas.
        coverage = entry.get("coverage") or {}
        unknown_paths.update(str(path) for path in
                             coverage.get("unknown_paths") or [] if path)
        unmapped_blocks.update(str(block) for block in
                               coverage.get("unmapped_blocks") or [] if block)
    card = report.get("card") or {}
    if card.get("commit"):
        note = "carte d'impact `%s` (%s, %s)" % (
            str(card["commit"])[:12], card.get("status") or "lue",
            {"à jour": "à la base", "antérieure": "antérieure à la base",
             }.get(card.get("base"), "%s par rapport à la base"
                   % (card.get("base") or "inconnue")))
    else:
        note = "carte d'impact absente (%s) : seuls les modules `support` " \
            "sont ajoutés" % (card.get("why") or card.get("status") or "?")
    priced = report.get("durations") or {}
    if priced.get("table"):
        note += " ; durées `%s` (pilote %s)" % (priced["table"], driver)
    elif priced:
        note += " ; durées %s (pilote %s)" % (
            "de la carte" if priced.get("card") else "forfaitaires", driver)
    note += " ; budget de carte %d s par unité, %.0f s pris" % (budget, spent)
    if dropped:
        note += ", %d module(s) au-delà du budget non joué(s)" % dropped
    if unknown_paths or unmapped_blocks:
        note += (" ; carte incomplète : %d fichier(s) hors carte, %d bloc(s) "
                 "non mesuré(s) (#7195)"
                 % (len(unknown_paths), len(unmapped_blocks)))
    return out, note


def base_failures(worktree: Path, base_sha: str, modules: list[str], *,
                  timeout: int, wall: int | None = None) -> set[str] | None:
    """#4025 : les tests de `modules` rouges sur la base, ou None (non concluant).

    Un module voisin rouge ne désigne l'unité que s'il est vert sur la base :
    un rouge aussi présent sur la base vient de l'environnement de l'hôte ou
    lui préexiste. Les modules sont rejoués dans un worktree détaché de la base.
    #4463 : `wall` borne le rejeu en temps mural (`run_tests(wall=…)`).
    """
    if not base_sha or not modules:
        return None
    parent = temporary_parent()
    path = Path(tempfile.mkdtemp(prefix="paquet-auto-base-", dir=str(parent)))
    path.rmdir()
    proc = git(worktree, "worktree", "add", "--detach", str(path), base_sha,
               check=False)
    if proc.returncode != 0:
        shutil.rmtree(path, ignore_errors=True)
        return None
    try:
        present = [module for module in modules
                   if (path / (module.replace(".", "/") + ".py")).is_file()]
        if not present:
            return set()
        walled = {"wall": wall} if wall is not None else {}
        run = run_tests(path, "base", present, timeout=timeout, **walled)
        if run.inconclusive:
            return None
        return {failure["test"] for failure in unittest_failures(run.output, _unusable(run))}
    finally:
        remove_worktree(worktree, path)


def _dedupe_modules(worktree: Path, modules) -> list[str]:
    out: list[str] = []
    for module in modules:
        if module in out:
            continue
        path = worktree / (module.replace(".", "/") + ".py")
        if not path.is_file():
            raise PaquetAutoError(
                "module de test %s absent de l'arbre composé (%s)" % (module, path))
        out.append(module)
    return out


def _state_dir() -> str:
    """Le script n'a que la bibliothèque standard (voir `project_python`) :
    sans le paquet `ameesh` importable, le défaut de `ameesh.config`."""
    try:
        from ameesh.config import load
        return load().state_dir
    except (ImportError, SystemExit):
        return os.path.expanduser("~/.local/state/ameesh")


def _mtime(path: Path) -> float:
    try:
        return path.stat().st_mtime
    except OSError:
        return 0.0


def save_test_log(run: TestRun) -> TestRun:
    """Preuve locale privée, hors arbre jetable ; 48 h et 500 séries au plus.

    #4887 : 100 séries ne gardaient pas les journaux d'une composition d'une
    heure (h-10 : 5 sur 10 avaient disparu au moment de la mesure) ; 500
    laissent le temps de lire un rouge sans grossir (quelques dizaines de Ko
    par série). Au mieux : un journal impossible à écrire ne change rien à la
    série.
    """
    try:
        return _save_test_log(run)
    except Exception as exc:  # noqa: BLE001 — la preuve ne retient pas la série
        print("journal de série non conservé : %s" % exc, file=sys.stderr)
        return run


def _save_test_log(run: TestRun) -> TestRun:
    root = Path(_state_dir()) / "roles" / "mesh-design" / "test-logs"
    root.mkdir(parents=True, exist_ok=True, mode=0o700)
    files = sorted(root.glob("series-*.log"), key=_mtime, reverse=True)
    cutoff = time.time() - 48 * 3600
    for index, path in enumerate(files):
        if index >= 499 or _mtime(path) < cutoff:
            path.unlink(missing_ok=True)
    with tempfile.NamedTemporaryFile(mode="w", encoding="utf-8", prefix="series-",
                                     suffix=".log", dir=root, delete=False) as log:
        log.write("%s\nmodules: %s\nreturncode: %s\nseconds: %.3f\n\n%s"
                  % (run.label, ", ".join(run.modules), run.returncode,
                     run.seconds, run.output))
        run.log_path = log.name
    return run


def test_driver(worktree: Path) -> str:
    """#4271 : le pilote Postgres des tests joués par ce script.

    Celui que l'environnement impose (`AMEESH_DRIVER`, comme
    `ameesh.config`), sinon psycopg si l'interpréteur des tests le porte,
    sinon psql : la durée d'un module en dépend d'un facteur ~6."""
    for name in ("AMEESH_DRIVER", "AGENT_MESH_DRIVER"):
        wanted = (os.environ.get(name) or "").strip()
        if wanted in ("psycopg", "psql"):
            return wanted
        if wanted:
            break
    return "psycopg" if _supports_psycopg(project_python(worktree)) else "psql"


_SERIES = iter(range(1, 1 << 30))


def _group_alive(pgid: int) -> bool:
    try:
        os.killpg(pgid, 0)
    except ProcessLookupError:
        return False
    except PermissionError:
        return True
    return True


def run_walled(cmd: list[str], *, cwd: Path, env: dict,
               wall: int) -> subprocess.CompletedProcess | None:
    """#4463 : `cmd` borné en temps mural (attente d'un créneau comprise).

    La commande a sa propre session, dont elle mène le groupe. À l'échéance,
    SIGTERM à ce groupe : une série directe et ce qu'elle a lancé le
    reçoivent ; `ameesh heavy` le relaie au groupe de sa série, l'escalade en
    SIGKILL et arrête ses autres descendants avant de rendre le créneau. Ce
    qui reste du groupe reçoit SIGKILL au plus `WALL_GRACE_S` après, même si
    le meneur est déjà sorti ; None si la borne a été atteinte. La lecture des
    sorties reste bornée."""
    proc = subprocess.Popen(cmd, cwd=str(cwd), env=env, stdin=subprocess.DEVNULL,
                            stdout=subprocess.PIPE, stderr=subprocess.PIPE,
                            start_new_session=True)
    try:
        out, err = proc.communicate(timeout=wall)
        return subprocess.CompletedProcess(cmd, proc.returncode, out, err)
    except subprocess.TimeoutExpired:
        pass
    stop_by = time.monotonic() + WALL_GRACE_S      # une seule grâce en tout
    with contextlib.suppress(ProcessLookupError, PermissionError):
        os.killpg(proc.pid, signal.SIGTERM)
    try:
        proc.communicate(timeout=WALL_GRACE_S)
        # meneur sorti : le reste de son groupe a le reste de la grâce
        while time.monotonic() < stop_by and _group_alive(proc.pid):
            time.sleep(0.1)
    except subprocess.TimeoutExpired:
        pass
    with contextlib.suppress(ProcessLookupError, PermissionError):
        os.killpg(proc.pid, signal.SIGKILL)
    # un descendant hors de la session peut tenir les tubes : on les lâche
    for pipe in (proc.stdout, proc.stderr):
        with contextlib.suppress(OSError):
            pipe.close()
    with contextlib.suppress(subprocess.TimeoutExpired):
        proc.wait(timeout=max(1.0, stop_by - time.monotonic()))
    return None


_ENGINE_PROBES: dict[tuple, bool] = {}


def container_engine_state(executable: str, env: dict) -> str:
    """#6567 : l'état du moteur `executable` dans l'environnement `env`.

    Sonde `<executable> info`, qui ne lance aucune commande dans un
    conteneur. `absent` : 127 (shim du ménage sans moteur réel) ou exécutable
    introuvable ; `unreachable` : démon injoignable, dit par le client ;
    `ok` : réponse ; `unknown` sinon (délai, autre erreur) — rien n'est prouvé
    et le rouge reste un rouge. C'est **l'exécutable de la commande en échec**
    qui est sondé : un nom nu par le `PATH` de la série, un chemin absolu tel
    quel ; un chemin relatif (cwd du test inconnu) reste `unknown`. Une
    réponse par exécutable, `PATH` et moteurs réels déclarés, pour tout le
    lancement.
    """
    if "/" in executable and not executable.startswith("/"):
        return "unknown"
    key = (executable, env.get("PATH", ""), env.get("AMEESH_REAL_DOCKER", ""),
           env.get("AMEESH_REAL_PODMAN", ""))
    if key not in _ENGINE_PROBES:
        try:
            proc = subprocess.run([executable, "info"], env=env, capture_output=True,
                                  stdin=subprocess.DEVNULL, timeout=20)
        except FileNotFoundError:
            state = ENGINE_ABSENT
        except (OSError, subprocess.SubprocessError):
            state = "unknown"
        else:
            said = _decode(proc.stdout) + "\n" + _decode(proc.stderr)
            if proc.returncode == 0:
                state = "ok"
            elif proc.returncode == 127:
                state = ENGINE_ABSENT
            elif DAEMON_UNREACHABLE_RE.search(said):
                state = ENGINE_UNREACHABLE
            else:
                state = "unknown"
        _ENGINE_PROBES[key] = state
    return _ENGINE_PROBES[key]


def probe_engines(result: TestRun, env: dict) -> TestRun:
    """#6567 : sonde les exécutables cités par un échec de la série, eux seuls."""
    if result.returncode == 1 and result.output:
        executables = {found.group("exe")
                       for found in CONTAINER_FAILED_RE.finditer(result.output)}
        states = {executable: container_engine_state(executable, env)
                  for executable in sorted(executables)}
        result.unusable_engines = frozenset(
            (executable, state) for executable, state in states.items()
            if state in (ENGINE_ABSENT, ENGINE_UNREACHABLE))
    return result


def run_tests(worktree: Path, label: str, modules: list[str], *,
              timeout: int, heavy: bool = False,
              wall: int | None = None, failfast: bool = False) -> TestRun:
    """Une série unittest bornée par `timeout` (au-delà : non concluant).

    #4271 : avec `heavy`, la série prend un créneau de l'hôte
    (`ameesh heavy --max-duration`) et son propre schéma par défaut : la borne
    est tenue par `heavy`, qui arrête tout le groupe de la série ; l'attente
    d'un créneau a la même borne en plus.

    #4463 : avec `wall` (le temps restant de l'échéance totale), attente du
    créneau et exécution tiennent ensemble dans `wall` secondes (`run_walled`) ;
    au-delà, la série est non concluante. #7195 : `timeout` reste le plafond
    d'exécution (`ameesh heavy --max-duration`), l'attente du créneau ne le
    consomme donc pas."""
    if not modules:
        return TestRun(label=label, modules=[], returncode=0, output="",
                       seconds=0.0)
    start = time.monotonic()
    cmd = [project_python(worktree), "-m", "unittest", "-q",
           *(["-f"] if failfast else []), *modules]
    env = python_env(worktree)
    wait = 0
    wrapper = shutil.which("ameesh") if heavy else None
    if wrapper:
        cmd = [wrapper, "heavy", "--max-duration", "%ds" % timeout, "--", *cmd]
        env["AMEESH_SCHEMA"] = "t_paq_%d_%d" % (os.getpid(), next(_SERIES))
        wait = timeout + 60
    if wall is not None:
        proc = run_walled(cmd, cwd=worktree, env=env, wall=wall)
        if proc is None:
            return save_test_log(TestRun(
                label=label, modules=list(modules), returncode=None,
                output="borne totale des tests par unité atteinte après %d s "
                       "(attente du créneau comprise) : série arrêtée (#4463)" % wall,
                seconds=time.monotonic() - start, timed_out=True))
        return save_test_log(probe_engines(TestRun(
            label=label, modules=list(modules), returncode=proc.returncode,
            output=_output(proc), seconds=time.monotonic() - start,
            timed_out=bool(wrapper) and proc.returncode == HEAVY_CAPPED_EXIT), env))
    try:
        proc = run_command(cmd, cwd=worktree, timeout=timeout + wait, env=env)
        result = probe_engines(TestRun(
            label=label, modules=list(modules),
            returncode=proc.returncode, output=_output(proc),
            seconds=time.monotonic() - start,
            timed_out=bool(wrapper) and proc.returncode == HEAVY_CAPPED_EXIT), env)
    except PaquetAutoError as exc:
        result = TestRun(label=label, modules=list(modules), returncode=None,
                       output=str(exc), seconds=time.monotonic() - start,
                       timed_out=True)
    return save_test_log(result)


@dataclass
class ContractTestRun(TestRun):
    """Merged module reports, retaining partial/inconclusive evidence."""
    children: list[TestRun] = field(default_factory=list)

    def _summary(self) -> dict[str, int]:
        out: dict[str, int] = {}
        for child in self.children:
            for key, value in child._summary().items():
                out[key] = out.get(key, 0) + value
        return out

    @property
    def tests(self) -> int:
        return sum(child.tests for child in self.children)

    @property
    def skipped(self) -> int:
        return sum(child.skipped for child in self.children)

    @property
    def finished(self) -> bool:
        return bool(self.children) and all(child.finished for child in self.children)


def contract_order(worktree: Path, modules: list[str]) -> list[str]:
    """Longest measured modules first, historical invariants before fallback."""
    try:
        data = json.loads((worktree / "tests/parts/durees.json").read_text())
        table = data.get(test_driver(worktree), {}) if isinstance(data, dict) else {}
        if not isinstance(table, dict):
            table = {}
    except (OSError, ValueError):
        table = {}
    try:
        names = [line.split("#", 1)[0].strip()
                 for line in (worktree / CONTRACT_ALWAYS).read_text().splitlines()]
        legacy = {name if name.startswith("tests.") else "tests." + name
                  for name in names if name}
    except OSError:
        legacy = set()
    legacy.update(CONTRACT_MODULES)
    def order(module):
        duration = table.get(module.removeprefix("tests."), 0)
        if not isinstance(duration, (int, float)):
            duration = 0
        return module not in legacy, -duration, module
    return sorted(modules, key=order)


@dataclass
class FlowTestRun(TestRun):
    """#6274 : une part jouée par le lanceur déclaré, lue sur son flux.

    Le flux `ameesh-test-event/1` (lu par `prevalidation.parse_flow`) fait
    foi, pas une sortie unittest : vert = code 0, une fin `succes`, aucun
    échec et **chaque module demandé couvert** par au moins un événement
    `test` joué ; rouge = un test en échec hors environnement ; le reste
    (délai, fin absente, module sans test, test sauté, échecs
    d'environnement seuls) est non concluant (B/P2 cx2 : une fin `succes`
    seule ne prouve rien ; B/P2 dev3/cx3 : un `saute` n'est pas joué, §3).
    """
    flow: dict = field(default_factory=dict)

    @property
    def executed(self) -> set[str]:
        """Les modules demandés qu'un test joué (non sauté) du flux couvre."""
        played = self.flow.get("played")
        if played is None:
            played = self.flow.get("modules") or ()
        return set(self.modules) & set(played)

    @property
    def unexecuted(self) -> list[str]:
        return [module for module in self.modules if module not in self.executed]

    @property
    def failed_tests(self) -> list[str]:
        environment = set(self.flow.get("environment") or ())
        return [name for name in self.flow.get("failed") or ()
                if name not in environment]

    @property
    def finished(self) -> bool:
        return (not self.timed_out and self.returncode is not None
                and bool(self.flow.get("fins")))

    @property
    def green(self) -> bool:
        return (self.returncode == 0 and self.finished
                and all(fin == "succes" for fin in self.flow["fins"])
                and not self.flow.get("failed") and not self.skipped
                and bool(self.modules) and not self.unexecuted)

    @property
    def real_failure(self) -> bool:
        return not self.timed_out and self.returncode is not None \
            and bool(self.failed_tests)

    @property
    def inconclusive(self) -> bool:
        return not self.green and not self.real_failure

    @property
    def tests(self) -> int:
        return int(self.flow.get("tests") or 0)

    @property
    def skipped(self) -> int:
        return int(self.flow.get("skipped") or 0)

    def motive(self) -> str:
        if self.timed_out:
            return "délai dépassé"
        if not self.flow.get("fins"):
            return "flux sans fin de part (code %s)" % self.returncode
        if self.skipped:
            return "%d test(s) sauté(s) au flux : non joué(s)" % self.skipped
        if self.unexecuted:
            return "module(s) demandé(s) sans test au flux : %s" % ", ".join(
                self.unexecuted[:3])
        if self.flow.get("environment"):
            return "échecs d'environnement seuls : %s" % ", ".join(
                self.flow["environment"][:3])
        return "résultat non concluant (code %s)" % self.returncode


def _flow_play(text: str, schema: str) -> dict:
    """Les tests sautés et les modules réellement joués d'un flux (#6274).

    `parse_flow` compte tout événement `test` ; un `saute` (addSkip de
    `scripts/test-parts.py`, fin `succes` conservée) n'a rien exécuté.
    """
    skipped, played = 0, set()
    for line in (text or "").splitlines():
        line = line.strip()
        if not line.startswith("{"):
            continue
        try:
            event = json.loads(line)
        except ValueError:
            continue
        if not isinstance(event, dict) or event.get("schema") != schema \
                or event.get("type") != "test":
            continue
        if str(event.get("statut") or "") == "saute":
            skipped += 1
        elif event.get("module"):
            played.add(str(event["module"]))
    return {"skipped": skipped, "played": tuple(sorted(played))}


def run_declared_contracts(worktree: Path, modules: list[str], *, end: float,
                           tooling: Tooling) -> FlowTestRun:
    """#6274 : les contrats en une part du lanceur déclaré, sous `heavy`.

    Même appel que la prévalidation (`prevalidation.remote_script`) : `run
    --plan --part 1 --driver contrats --flow`. Le plan de la part est écrit
    hors de l'arbre composé : l'arbre testé reste celui du manifeste.
    """
    from ameesh import prevalidation
    start = time.monotonic()
    remaining = int(end - time.monotonic())
    if remaining <= 0:
        run = budget_spent("contrats", modules)
        return FlowTestRun(run.label, run.modules, None, run.output, 0.0,
                           timed_out=True)
    launcher = worktree / tooling.parts_script
    if not launcher.is_file():
        return save_test_log(FlowTestRun(
            "contrats", list(modules), None,
            "%s absent de l'arbre composé" % tooling.parts_script, 0.0,
            timed_out=True))
    with tempfile.TemporaryDirectory(prefix="contrats-declares-") as temporary:
        plan = Path(temporary) / "plan.json"
        flow = Path(temporary) / "flux.jsonl"
        plan.write_text(json.dumps({
            "schema": "ameesh-test-plan/1", "mode": "lot",
            "head": _decode(git(worktree, "rev-parse", "HEAD").stdout).strip(),
            "parts": {DECLARED_PART_LABEL: [{
                "part": 1, "of": 1, "label": DECLARED_PART_LABEL,
                "modules": list(modules), "estimate_s": 0.0}]},
            "contract": list(modules)}), encoding="utf-8")
        cmd = [project_python(worktree), str(launcher), "run", "--plan", str(plan),
               "--part", "1", "--driver", DECLARED_PART_LABEL, "--flow", str(flow)]
        env = python_env(worktree)
        if tooling.project:
            env["AMEESH_PROJECT"] = tooling.project
        wrapper = shutil.which("ameesh")
        if wrapper:
            cmd = [wrapper, "heavy", "--max-duration", "%ds" % remaining, "--", *cmd]
        proc = run_walled(cmd, cwd=worktree, env=env, wall=remaining)
        try:
            events = flow.read_text(encoding="utf-8")
        except OSError:
            events = ""
    parsed = prevalidation.parse_flow(events)
    parsed.update(_flow_play(events, prevalidation.FLOW_SCHEMA))
    if proc is None:
        return save_test_log(FlowTestRun(
            "contrats", list(modules), None,
            "borne des contrats atteinte après %d s (lanceur déclaré)" % remaining,
            time.monotonic() - start, timed_out=True, flow=parsed))
    capped = bool(wrapper) and proc.returncode == HEAVY_CAPPED_EXIT
    output = _output(proc)
    if parsed["failed"]:
        output = "\n".join(["échecs (flux) : " + ", ".join(parsed["failed"]), output])
    return save_test_log(FlowTestRun(
        "contrats", list(modules), proc.returncode, output,
        time.monotonic() - start, timed_out=capped, flow=parsed))


def run_contract_modules(worktree: Path, modules: list[str], *,
                         end: float, parallel: int,
                         tooling: Tooling | None = None) -> TestRun:
    """One isolated heavy process per module under a shared wall deadline."""
    if not modules:
        return ContractTestRun("contrats", [], None, "aucun contrat sélectionné", 0.0)
    tooling = tooling or _TOOLING
    if tooling.declared:
        return run_declared_contracts(worktree, modules, end=end, tooling=tooling)
    stop = threading.Event()
    def module_job(module):
        def run():
            remaining = int(end - time.monotonic())
            if stop.is_set():
                return TestRun("contrats", [module], None,
                               "module non joué après un contrat négatif", 0.0)
            if remaining <= 0:
                return budget_spent("contrats", [module])
            result = run_tests(worktree, "contrats " + module, [module], timeout=remaining,
                               heavy=True, wall=remaining, failfast=True)
            if result.real_failure:
                stop.set()
            return result
        return run
    children = _run_series([module_job(m) for m in contract_order(worktree, modules)],
                           parallel=max(1, parallel))
    negative = any(run.real_failure for run in children)
    incomplete = any(run.inconclusive for run in children)
    rc = 1 if negative else (None if incomplete else 0)
    output = "\n".join("=== %s (%.3f s, rc=%s) ===\n%s" % (
        ", ".join(run.modules), run.seconds, run.returncode, run.output) for run in children)
    return ContractTestRun("contrats", list(modules), rc, output, 0.0,
                           timed_out=not negative and any(r.timed_out for r in children),
                           children=children)


@dataclass
class ContractPass:
    """Result bound to the final, clean Git tree, including its manifest."""
    head: str
    tree: str
    run: TestRun
    evidence: dict | None = None


def contract_scope(worktree: Path) -> tuple[str, str]:
    head = _decode(git(worktree, "rev-parse", "HEAD").stdout).strip()
    tree = _decode(git(worktree, "rev-parse", "HEAD^{tree}").stdout).strip()
    dirty = _decode(git(worktree, "status", "--porcelain", "--untracked-files=all").stdout)
    if dirty.strip():
        raise PaquetAutoError("arbre des contrats hors portée : contenu local modifié ; recomposer")
    return head, tree


def verify_contract_scope(worktree: Path, report: ContractPass) -> None:
    if contract_scope(worktree) != (report.head, report.tree):
        raise PaquetAutoError("arbre des contrats hors portée : tête ou arbre changé ; recomposer")


# Stable execution inputs only. Lease/turn/container/runner IDs, temporary
# directories and role bookkeeping do not describe the tests. PGAPPNAME is
# telemetry; other PG settings and Python/locale settings can change outcomes.
# Effective database configuration is fingerprinted separately, never secrets
# in clear text. The physical host is an explicit boundary: reuse on another
# host is deliberately unsupported, even when a stored report can be read.
ATTESTATION_ENV = frozenset({
    "PATH", "HOME", "USER", "LANG", "LANGUAGE", "TZ", "VIRTUAL_ENV",
    "AMEESH_PYTHON", "AMEESH_DRIVER", "AGENT_MESH_DRIVER",
    "AMEESH_TEST_DSN", "AGENT_MESH_TEST_DSN", "AMEESH_REQUIRE_DB",
    "AGENT_MESH_REQUIRE_DB", "AMEESH_TEST_VENV", "AMEESH_CONNECT_TIMEOUT",
})
ATTESTATION_CONFIG = (
    "backend", "dsn", "schema", "driver", "connect_timeout",
    "statement_timeout_ms", "read_only", "exec_url", "exec_token_file",
)


def attestation_execution_inputs(cfg):
    """Host-local semantic inputs; no per-turn or per-lease metadata."""
    from ameesh import test_attestation as evidence
    import hashlib
    environment = {key: value for key, value in os.environ.items()
                   if key in ATTESTATION_ENV or key.startswith(("PYTHON", "LC_"))
                   or (key.startswith("PG") and key != "PGAPPNAME")}
    settings = {key: getattr(cfg, key, None) for key in ATTESTATION_CONFIG}
    return {"host": socket.gethostname(),
            "environment_digest": evidence.digest(environment),
            "config_digest": hashlib.sha256(json.dumps(
                settings, sort_keys=True, default=str).encode("utf-8")).hexdigest()}


def contract_attestation_context(worktree: Path, *, base: str, modules: list,
                                 timeout: float, project: str = "") -> tuple[dict, Path, list, int | None]:
    """Exact local execution inputs; unavailable context disables caching.

    The Git tree covers runner code, migrations and repository fixtures. The
    external interpreter/dependencies, heavy launcher, declared execution
    inputs and pinned canon are also fingerprinted. No environment value is
    stored. Reuse is host-local, and disabled without an explicit age policy.
    """
    from ameesh import canon, config, mechanics, test_attestation as evidence
    from ameesh.deadline import Deadline
    import hashlib
    deadline = Deadline(timeout, clock=time.monotonic)
    def remaining():
        if deadline.expired():
            raise ValueError("attestation context budget exhausted")
        return deadline.remaining()
    cfg = config.load()
    configured = canon.load_configured(cfg, deadline=deadline)
    if not configured or not configured[0].sources:
        raise ValueError("no readable canon")
    sources = configured[0].sources
    if sources[0].prefix:
        raise ValueError("canon mechanics outside root")
    # Validate the exact pinned policy within the same total budget.
    document = mechanics.load_global(sources[0].repo, sources[0].commit, deadline=deadline)
    override = document.override(project) if project else None
    policy = override.contract_selection if override else None
    max_age = policy.attestation_max_age_seconds if policy else None
    canon_ids = [{"commit": source.commit, "repo": source.repo,
                  "prefix": source.prefix, "mode": source.mode} for source in sources]
    if any(source.mode != "git" or not source.commit for source in sources):
        raise ValueError("unpinned canon")
    heavy = shutil.which("ameesh")
    if not heavy:
        raise ValueError("heavy unavailable")
    probe = run_command([project_python(worktree), "-c",
        "import importlib.metadata as m,json,platform,sys; "
        "print(json.dumps([sys.version,sys.executable,platform.platform(),"
        "sorted((d.metadata['Name'],d.version) for d in m.distributions())]))"],
        cwd=worktree, timeout=remaining(), env=python_env(worktree))
    if probe.returncode:
        raise ValueError("runner unavailable")
    runtime = json.loads(_decode(probe.stdout))
    binary = hashlib.sha256(Path(runtime[1]).read_bytes()).hexdigest()
    launcher = hashlib.sha256(Path(heavy).read_bytes()).hexdigest()
    common = _decode(git(worktree, "rev-parse", "--git-common-dir", timeout=remaining()).stdout).strip()
    common = str((worktree / common).resolve())
    branch_key = evidence.digest({"repository": common, "base": base})
    path = Path(_state_dir()) / "contract-attestations" / (branch_key + ".json")
    # Source SHA is provenance. Including it in the plan would prevent safe
    # reuse when only commit metadata changes and the exact tree is identical.
    plan = {"selection": "packet-contracts", "modules": modules, "base": base}
    tree = _decode(git(worktree, "rev-parse", "HEAD^{tree}", timeout=remaining()).stdout).strip()
    scope = {"plan": plan, "selection_digest": evidence.digest(plan),
             "tests": modules, "canon_digest": evidence.digest(canon_ids),
             "registry_digest": evidence.digest({"tree": tree}),
             "runner_digest": evidence.digest({"runtime": runtime, "binary": binary,
                                                "launcher": launcher}),
             "profile": "local-heavy", "drivers": dict(
                 attestation_execution_inputs(cfg), driver=test_driver(worktree)),
             "database_schema": tree, "fixtures": {"tree": tree}, "exclusions": {}}
    lots = []
    changed = git(worktree, "diff", "--name-only", base, "HEAD", "--", "reviews/",
                  timeout=remaining())
    for rel in sorted(_decode(changed.stdout).splitlines()):
        manifest = worktree / rel
        if manifest.name.startswith("integration-paquet-") and manifest.suffix == ".md" and manifest.is_file():
            for ids, sha in parse_composition_table(manifest.read_text(encoding="utf-8")):
                for lot in ids:
                    resolved = _decode(git(worktree, "rev-parse", sha + "^{commit}",
                                           timeout=remaining()).stdout).strip()
                    lots.append({"id": str(lot), "sha": resolved})
    remaining()
    return scope, path, lots, max_age


def read_contract_attestation(path: Path):
    try:
        return json.loads(path.read_text(encoding="utf-8"))
    except (OSError, ValueError):
        return None


def previous_contract_attestation(path: Path, carrier: int, *, worktree: Path,
                                  timeout: float):
    """Local artifact first, bounded stored-report read after local state loss.

    Reading a report from storage does not authorize cross-host reuse: the
    attested physical host must still match the current execution inputs.
    """
    previous = read_contract_attestation(path)
    if previous is not None or carrier <= 0:
        return previous
    # The subprocess bounds both connection and query, including mediated
    # backends, within the pass's remaining budget. No DB lock spans a test.
    script = """import json, sys
from ameesh import config, db, storage
connection = db.connect(config.load())
try:
    row = storage.of(connection).prevalidation.latest(int(sys.argv[1]))
    value = row.get('report') if row else None
    print(json.dumps(json.loads(value) if isinstance(value, str) else value))
finally:
    connection.close()
"""
    try:
        if timeout <= 0:
            return None
        proc = run_command([project_python(worktree), "-c", script, str(carrier)],
                           cwd=worktree, env=python_env(worktree), timeout=timeout)
        return json.loads(_decode(proc.stdout)) if proc.returncode == 0 else None
    except (OSError, ValueError, PaquetAutoError):
        return None


def save_contract_attestation(path: Path, report: dict):
    """Private atomic artifact outside the tested tree, also on recomposition."""
    path.parent.mkdir(parents=True, exist_ok=True, mode=0o700)
    fd, name = tempfile.mkstemp(prefix=".attestation-", dir=str(path.parent))
    try:
        with os.fdopen(fd, "w", encoding="utf-8") as stream:
            json.dump(report, stream, sort_keys=True, allow_nan=False)
        os.replace(name, path)
    finally:
        with contextlib.suppress(FileNotFoundError):
            os.unlink(name)


def prepublication_contracts(worktree: Path, *, base: str,
                             budget: int | None, guard=None, expected_head: str = "",
                             parallel: int = DEFAULT_PARALLEL, carrier: int = 0,
                             project: str = "") -> ContractPass:
    """Plan + slot wait + isolated execution share one canon budget (#6182).

    A timeout/absent prerequisite records an inconclusive pass, never a green
    proof. A changed tree refuses this attempt before any publication.
    """
    if guard is not None:
        guard()
    head, tree = contract_scope(worktree)
    if expected_head and head != expected_head:
        raise PaquetAutoError("arbre des contrats hors portée : tête composée changée avant la passe")
    start = time.monotonic()
    end = start + budget if budget else start
    modules = []
    context = None
    cached = None
    try:
        if not budget:
            raise PaquetAutoError("budget de contrats absent du canon")
        if shutil.which("ameesh") is None:
            raise PaquetAutoError("créneau heavy indisponible")
        modules = contract_modules(worktree, base=base,
                                   timeout=max(0.1, end - time.monotonic()))
        from datetime import datetime, timezone
        from ameesh import prevalidation
        try:
            from ameesh.canon import GitDeadline
            try:
                context = contract_attestation_context(
                    worktree, base=base, modules=modules, timeout=end - time.monotonic(), project=project)
            except GitDeadline as exc:
                raise ValueError("attestation context budget exhausted") from exc
            scope, path, lots, max_age = context
            cached, causes = None, ("reuse-policy-absent",)
            if max_age is not None:
                cached, causes = prevalidation.reuse_report(
                    previous_contract_attestation(path, carrier, worktree=worktree,
                                                  timeout=end - time.monotonic()),
                    head=head, tree=tree, scope=scope,
                    now=datetime.now(timezone.utc), max_age_seconds=max_age)
            print("attestation contrats : " + ("réutilisée" if cached else ", ".join(causes)),
                  flush=True)
        except (OSError, ValueError, PaquetAutoError, SystemExit) as exc:
            context = None
            print("attestation contrats indisponible : " + str(exc), flush=True)
        remaining = int(end - time.monotonic())
        if cached is not None:
            run = TestRun("contrats", modules, 0,
                          "Attestation réutilisée, exécution originale : "
                          + cached["attestation"]["payload"]["source_sha"]
                          + "\nRan %d tests in 0.0s\nOK\n" % cached.get("tests", 0), 0.0)
        elif remaining <= 0:
            run = budget_spent("contrats", modules)
        else:
            run = run_contract_modules(worktree, modules, end=end, parallel=parallel)
    except PaquetAutoError as exc:
        run = save_test_log(TestRun("contrats", modules, None, str(exc),
                                   time.monotonic() - start, timed_out=True))
    run.seconds = time.monotonic() - start
    report = ContractPass(head, tree, run)
    verify_contract_scope(worktree, report)
    if guard is not None:
        guard()
    verify_contract_scope(worktree, report)
    if isinstance(run, ContractTestRun):
        save_test_log(run)
    if context is not None and modules:
        from ameesh import prevalidation, packet_contract_proof
        scope, path, lots, max_age = context
        if cached is None:
            state = packet_contract_proof.projection(report, base=base, target="")["result"]["state"]
            children = getattr(run, "children", ()) or (run,)
            results = {module: "inconclusive" for module in modules}
            for child in children:
                child_state = "inconclusive"
                if child.real_failure and not child.inconclusive:
                    child_state = "failed"
                elif (child.green and child.finished and not child.inconclusive
                      and not child.skipped and child.tests > 0 and not child.flaky_tests):
                    child_state = "passed"
                # #6274 : un lanceur déclaré n'atteste que les modules que
                # son flux a réellement joués ; les autres restent non concluants.
                played = child.executed if isinstance(child, FlowTestRun) \
                    else set(child.modules)
                for module in child.modules:
                    if module in results:
                        results[module] = child_state if module in played \
                            else "inconclusive"
            if state == "green" and any(value != "passed" for value in results.values()):
                state = "inconclusive"
            cached = prevalidation.attest_report({
                "schema": prevalidation.REPORT_SCHEMA, "lot": carrier, "sha": head,
                "base": base, "card": "", "modules": [
                    module for module, result in results.items() if result != "inconclusive"],
                "missing": [module for module, result in results.items() if result == "inconclusive"],
                "selection": "contract", "profile": "local-heavy", "host": socket.gethostname(),
                "planned": modules, "ok": state == "green",
                "outcome": {"green": "green", "red": "run-red",
                            "inconclusive": "run-incomplete"}[state],
                "causes": [] if state == "green" else [state],
                "seconds": run.seconds, "tests": run.tests,
                # N-1 values remain strings; the inline execution plan is
                # carried by attestation.payload.scope.plan, not a Git file.
                "tree": {"head": head, "snapshot": head, "plan": "", "git_tree": tree},
            }, tree=tree, scope=scope, lots=lots, results=results,
                timestamp=datetime.now(timezone.utc).isoformat(),
                links=[run.log_path] if run.log_path else [])
        report.evidence = cached
        try:
            save_contract_attestation(path, cached)
        except OSError as exc:
            print("attestation non persistée : " + str(exc), flush=True)
    return report


def publication_policy(args):
    supplied = getattr(args, "contract_publication", None)
    if supplied:
        return supplied
    project = getattr(args, "pause_project", "") or ""
    if not project:
        return "contracts-first"  # D compatibility for direct, non-project calls
    from ameesh.packet_contract_proof import policy
    return policy(project)


#: #6238 : la dernière projection écrite par ce processus, par porteur.
_RECORDED_CONTRACTS: dict[int, str] = {}


def record_contract_result(worktree, carrier, report, *, base, target):
    from ameesh.packet_contract_proof import encode
    verify_contract_scope(worktree, report)
    if report.evidence is not None:
        from ameesh import config, db, prevalidation
        connection = None
        try:
            connection = db.connect(config.load())
            stored = dict(report.evidence, lot=int(carrier))
            prevalidation.record(connection, stored)
        except (Exception, SystemExit) as exc:  # optional storage never changes an executed result
            print("attestation : rapport non persisté — " + str(exc), flush=True)
        finally:
            if connection is not None:
                connection.close()
    note = encode(report, base=base, target=target)
    print(note, flush=True)  # scheduler can recover a failed journal write
    if _RECORDED_CONTRACTS.get(carrier) == note:
        # #6238 : une seule note par tête/arbre/cible ; le bail exclut un autre
        # composeur du porteur, la note écrite reste donc la dernière.
        print("note du porteur #%d déjà à jour" % carrier, flush=True)
        return
    outcome = note_carrier(worktree, carrier, note)
    if outcome == "note du porteur #%d écrite" % carrier:
        _RECORDED_CONTRACTS[carrier] = note
    print(outcome, flush=True)


def contract_budget(args) -> int | None:
    """CLI value supplied by the scheduler, otherwise existing compiled policy.

    N-1 keeps its declared unit budget until the new optional key is adopted.
    No local numeric default silently becomes the prepublication policy.
    """
    supplied = getattr(args, "contracts_budget", None)
    if supplied is not None:
        return supplied
    from ameesh import config, mechanics
    try:
        policy = mechanics.configured(config.load()).packet_tests
    except (OSError, ValueError, mechanics.MechanicsError):
        return None
    if policy is None:
        return None
    return getattr(policy, "contracts_timeout_seconds", None) or policy.unit_timeout_seconds


def run_freeze_checks(worktree: Path, *, timeout: int) -> tuple[int, str, float]:
    script = worktree / FREEZE_CHECKS_SCRIPT
    if not script.is_file():
        raise PaquetAutoError("%s absent de l'arbre composé" % FREEZE_CHECKS_SCRIPT)
    start = time.monotonic()
    proc = run_command([project_python(worktree), str(script)], cwd=worktree,
                       timeout=timeout, env=python_env(worktree))
    return proc.returncode, _output(proc), time.monotonic() - start


def _changed_paths(worktree: Path, base_sha: str) -> list[str] | None:
    """#4887 : les chemins changés par la composition, ou None (diff illisible)."""
    if not base_sha:
        return None
    proc = git(worktree, "diff", "--name-only", base_sha, check=False)
    if proc.returncode != 0:
        return None
    return [line.strip() for line in _decode(proc.stdout).splitlines()
            if line.strip()]


def _cached_client_modules(worktree: Path) -> tuple[Path | None, str]:
    """#6676 : lie `ui/node_modules` du worktree jetable au cache partagé.

    La clé de l'entrée est celle du `ui/package-lock.json` **composé**
    (`ameesh deps link --no-fill`) : aucun `npm ci`, aucune écriture hors du
    clone et du cache. Rend `(lien, "")` si posé, sinon `(None, motif)`.
    """
    ui = worktree / "ui"
    link = ui / "node_modules"
    if not (ui / "package-lock.json").is_file():
        return None, "pas de ui/package-lock.json"
    try:
        proc = run_command(["ameesh", "deps", "link", "--no-fill", "--json",
                            str(ui)], cwd=worktree, timeout=DEPS_LINK_TIMEOUT)
    except PaquetAutoError as exc:
        return None, "ameesh deps link : %s" % _one_line(str(exc))[:160]
    if proc.returncode != 0:
        return None, ("ameesh deps link rc %d : %s"
                      % (proc.returncode, _one_line(_output(proc))[:160]))
    try:
        items = json.loads(_decode(proc.stdout)).get("items") or []
    except (ValueError, AttributeError):
        return None, "ameesh deps link : sortie illisible"
    status = ""
    for item in items:
        if (isinstance(item, dict) and item.get("package")
                and Path(str(item["package"])).resolve() == ui.resolve()):
            status = str(item.get("status") or "")
    if status == "linked":
        if (link / "openapi-typescript").is_dir():
            return link, ""
        if link.is_symlink():
            with contextlib.suppress(OSError):
                link.unlink()
        return None, "cache partagé lié sans openapi-typescript"
    return None, "cache partagé : %s" % (status or "ui/ absent de la réponse")


def _client_node_modules(worktree: Path, repo: Path | None
                         ) -> tuple[Path | None, bool, str]:
    """#4887 : les `node_modules` de `ui/`, dans l'arbre ou prêtés.

    Rend `(chemin, posé, motif)`, `posé` disant qu'un lien a été créé dans le
    worktree jetable et doit être retiré après le contrôle ; `motif` dit
    pourquoi aucun n'a été trouvé. #6676 : le cache partagé par lockfile
    passe avant le clone dédié, qui n'a souvent aucun `ui/node_modules`.
    """
    direct = worktree / "ui" / "node_modules"
    if (direct / "openapi-typescript").is_dir():
        return direct, False, ""
    why = "ui/node_modules absent"
    if not os.path.lexists(direct):
        cached, why = _cached_client_modules(worktree)
        if cached is not None:
            return cached, True, ""
    if repo is not None:
        source = repo / "ui" / "node_modules"
        if (source / "openapi-typescript").is_dir():
            link = worktree / "ui" / "node_modules"
            if not os.path.lexists(link):
                link.symlink_to(source.resolve(), target_is_directory=True)
                return link, True, ""
        why += " ; clone sans ui/node_modules"
    return None, False, why


def _client_culprits(worktree: Path, units) -> list[int]:
    """#4887 : les unités qui changent l'OpenAPI sans le schéma généré."""
    out: list[int] = []
    for unit in units or ():
        proc = git(worktree, "diff", "--name-only", unit.commit + "^1",
                   unit.commit, check=False)
        if proc.returncode != 0:
            continue
        files = {line.strip() for line in _decode(proc.stdout).splitlines()
                 if line.strip()}
        if CLIENT_SPEC in files and CLIENT_SCHEMA not in files:
            out.append(unit.lot)
    return out


def check_client_types(worktree: Path, base_sha: str, *,
                       repo: Path | None = None, units=(),
                       timeout: int = GIT_TIMEOUT,
                       outcome: dict | None = None) -> tuple[bool, str]:
    """#4887 : le contrôle rapide du job plan (`types clients à jour`) avant la CI.

    h-10 a perdu sa CI sur `ui/src/api/schema.d.ts` périmé (#4831) : le job
    `plan` le refuse, mais le composeur ne le voyait qu'après la poussée. Ici :

    * le vrai `node ui/scripts/gen-api.mjs --check` tourne quand `node` et
      `openapi-typescript` sont disponibles (le clone dédié peut prêter ses
      `ui/node_modules` au worktree jetable) ; lui seul **bloque** une
      composition, sur une preuve de types périmés ;
    * sans eux, le contrôle est **non concluant** : il est dit, avec l'indice
      « OpenAPI changé sans schéma » et l'unité qui le porte, mais il ne bloque
      pas (§3) — un reformatage JSON ou un `info.version` peut laisser les
      types identiques (cx1, #4887) ; la CI tranche.

    #6676 : `outcome` reçoit la cause d'un non-concluant (`cause`, `detail`) ;
    `node-absent` et `modules-absent` sont une panne de l'hôte, consignée au
    manifeste et comptée par l'appelant.
    """
    outcome = {} if outcome is None else outcome
    script = worktree / CLIENT_GEN
    if not script.is_file():
        return True, "types clients : pas de générateur dans l'arbre, contrôle sauté"
    changed = _changed_paths(worktree, base_sha)
    if changed is None:
        return True, ("types clients : diff de la composition illisible, "
                      "contrôle non concluant (la CI tranche)")
    if CLIENT_SPEC not in changed and CLIENT_SCHEMA not in changed:
        return True, "types clients : inchangés"
    if "ui/package.json" in changed or "ui/package-lock.json" in changed:
        # les dépendances du générateur ont bougé : le node_modules prêté par
        # le clone n'est plus celui de l'arbre composé.
        outcome.update(cause="deps-changed")
        return True, ("types clients : non concluant (dépendances ui modifiées, "
                      "node_modules du clone peut être périmé) ; la CI tranche")
    node = shutil.which("node")
    modules, linked, why = (_client_node_modules(worktree, repo) if node
                            else (None, False, "node absent"))
    try:
        if node and modules is not None:
            try:
                proc = run_command([node, CLIENT_GEN, "--check"], cwd=worktree,
                                   timeout=timeout)
            except PaquetAutoError as exc:
                outcome.update(cause="check-failed", detail=_one_line(str(exc))[:160])
                return True, ("types clients : contrôle non concluant (%s) ; "
                              "la CI tranche" % _one_line(str(exc))[:160])
            text = _output(proc)
            if proc.returncode == 0:
                return True, "types clients : à jour (`gen-api --check`)"
            if re.search(r"ERR_MODULE_NOT_FOUND|Cannot find module", text):
                # dépendances incomplètes : garde déterministe
                why = "modules incomplets : %s" % _one_line(text)[:120]
            else:
                return False, ("types clients périmés (`gen-api --check`, rc %d) : %s"
                               % (proc.returncode, _one_line(text)[:240]))
        outcome.update(cause="node-absent" if not node else "modules-absent",
                       detail=why)
        if CLIENT_SPEC in changed and CLIENT_SCHEMA not in changed:
            culprits = _client_culprits(worktree, units)
            hint = ((" ; indice : unité(s) #"
                     + ", #".join(str(lot) for lot in culprits))
                    if culprits else "")
            return True, (
                "types clients : non concluant (node/node_modules absent : %s) ; "
                "`%s` change sans `%s`%s ; la CI tranche (#4831)"
                % (why, CLIENT_SPEC, CLIENT_SCHEMA, hint))
        return True, ("types clients : non concluant (node/node_modules absent : "
                      "%s) ; la CI tranche" % why)
    finally:
        if linked and modules is not None:
            with contextlib.suppress(OSError):
                modules.unlink()


def record_client_types_event(worktree: Path, *, name: str, project: str,
                              outcome: dict) -> str:
    """#6676 : compte un non-concluant faute de modules (`packet.client_types`).

    Sans lui, la panne de l'hôte se répétait en silence et coûtait une CI de
    paquet rouge. Au mieux : un échec d'écriture est dit, jamais bloquant.
    Rend la ligne à afficher.
    """
    code = """
import json, sys
from ameesh import config, db, events
conn = None
try:
    conn = db.connect(config.load())
    if conn is None:
        raise RuntimeError("base absente")
    data = json.loads(sys.argv[3])
    print(events.record(conn, sys.argv[1], sys.argv[2],
                        project=sys.argv[4] or None, **data))
finally:
    if conn is not None:
        conn.close()
"""
    data = {"cause": str(outcome.get("cause") or ""),
            "detail": str(outcome.get("detail") or "")[:300],
            "host": socket.gethostname()}
    try:
        proc = run_command(
            [project_python(worktree), "-c", code, CLIENT_TYPES_EVENT,
             package_branch(name), json.dumps(data, ensure_ascii=False), project],
            cwd=worktree, env=python_env(worktree), timeout=EPOCH_CHECK_TIMEOUT)
    except PaquetAutoError as exc:
        return "événement %s non écrit : %s" % (CLIENT_TYPES_EVENT, exc)
    if proc.returncode:
        return "événement %s non écrit : %s" % (CLIENT_TYPES_EVENT,
                                                 _tail(_output(proc), 3))
    return "événement %s #%s" % (CLIENT_TYPES_EVENT, _decode(proc.stdout).strip())


def _unusable(run) -> frozenset:
    """#6567 : les moteurs prouvés inutilisables d'une série (vide hors `TestRun`)."""
    return getattr(run, "unusable_engines", frozenset()) or frozenset()


def unittest_failures(output: str,
                      unusable_engines: frozenset = frozenset()) -> list[dict]:
    """Les échecs d'une sortie unittest, au format des échecs de CI (#3801).

    `FAIL: test_x (tests.test_module.Class.test_x)` et
    `ERROR: setUpClass (tests.test_module.Class)` donnent le même nom pointé
    que la CI ; un sous-test (`... (i=1)`) ne change pas le nom. Une
    `ERROR:` d'environnement (psql/psycopg absent) n'est pas un rouge du lot :
    elle est ignorée, comme `TestRun.real_failure` la classe. #6567 :
    `unusable_engines` est celui de la série (`TestRun.unusable_engines`).
    """
    seen: set[str] = set()
    out: list[dict] = []
    for match in FAIL_LINE_RE.finditer(output or ""):
        kind, test = match.group(1), match.group(2).strip()
        if not test or test in seen:
            continue
        if kind == "ERROR":
            rest = (output or "")[match.end():]
            end = BLOCK_END_RE.search(rest)
            block = rest[:end.start()] if end else rest
            if TestRun._error_is_environment(block, unusable_engines):
                continue
        seen.add(test)
        module = ""
        found = TEST_MODULE_RE.search(test)
        if found:
            module = found.group(1)
        out.append({"test": test, "module": module})
    return out


def attribute_composition(worktree: Path, composition: Composition,
                          failures: list[dict]) -> dict:
    """La lecture #3801 du rouge, par l'outil de l'arbre composé (#3931).

    Le helper lit la carte d'impact localement et applique
    `packet_red.attribute_tests` : même lecture que le rouge de ci-full. Un
    helper absent ou illisible ne conclut pas (`status` le dit) ; l'appelant
    garde alors l'échec actuel, sans retrait inventé.
    """
    script = worktree / ATTRIBUTION_SCRIPT
    if not script.is_file():
        return {"status": "no-helper", "tests": [],
                "why": "%s absent de l'arbre composé" % ATTRIBUTION_SCRIPT}
    payload = {
        "schema": "ameesh-paquet-attribution-request/1",
        "units": [{"lot": unit.lot, "unit_sha": unit.commit}
                  for unit in composition.units],
        "failures": failures,
    }
    try:
        proc = run_command(
            [project_python(worktree), str(script), "--repo", str(worktree)],
            cwd=worktree, input_text=json.dumps(payload),
            timeout=GIT_TIMEOUT, env=python_env(worktree))
    except PaquetAutoError as exc:
        return {"status": "error", "tests": [], "why": str(exc)}
    text = _decode(proc.stdout).strip()
    try:
        report = json.loads(text) if text else {}
    except ValueError:
        report = {}
    if proc.returncode != 0 or not isinstance(report, dict):
        return {"status": "error", "tests": [],
                "why": _tail(_output(proc), 5) or "sortie illisible"}
    return report


def _failure_name(failure) -> str:
    return str((failure or {}).get("test") or "").strip()


def _failure_matches(observed: str, failed: set[str]) -> bool:
    """Le test observé fait-il partie des échecs d'un préfixe ?

    Un nom exact, une classe dont un test a rougi, ou un test dont la classe
    a rougi (`setUpClass`) : les trois formes désignent le même échec.
    """
    if not observed:
        return False
    if observed in failed:
        return True
    for name in failed:
        if name and (name.startswith(observed + ".")
                     or observed.startswith(name + ".")):
            return True
    return False


def prefix_failures(worktree: Path, composition: Composition,
                    failures: list[dict], *, timeout: int,
                    replay=None, log=print, guard=None,
                    deadline: float | None = None,
                    ) -> tuple[dict[int, list[dict]], str, list[dict]]:
    """#4217 : la première unité où chaque test rouge apparaît.

    Rejoue les tests rouges des modules **voisins** sur les préfixes de
    l'empilement, dans l'ordre (`base + u1`, `base + u1 + u2`, …), dans un
    worktree détaché jetable : le commit de chaque unité est la tête cumulée
    de la composition à ce point. Une unité est retenue pour un test dès que
    le préfixe correspondant le rend rouge ; les préfixes suivants ne le
    rejouent plus.

    Rend `({lot: [échecs]}, "", [])`, ou `({}, "", [échecs])` quand des tests
    restent rouges sur la tête mais verts sur **tous** les préfixes : ce sont
    des instables (#4221), consignés non concluants, sans retrait. Rend
    `({}, pourquoi, [])` quand le rejeu ne conclut pas (worktree impossible,
    run non concluant, rouge sans nom de test) : l'appelant ne retire alors
    aucune unité. Aucune décision de retrait ici.

    #4463 : `guard` est relu avant chaque préfixe après le premier (fusion du
    porteur comprise) ; sous `deadline`, chaque rejeu est borné au temps
    restant, en temps mural, et `BudgetSpent` est levée quand il n'en reste
    plus, avant un préfixe ou pendant (rejeu coupé par l'échéance).
    """
    tests: list[dict] = []
    seen: set[str] = set()
    for failure in failures or ():
        name = _failure_name(failure)
        if name and name not in seen:
            seen.add(name)
            tests.append(dict(failure))
    if not tests:
        return {}, "aucun nom de test à rejouer sur les préfixes", []
    if not composition.units:
        return {}, "composition sans unité à rejouer", []
    replay = replay or run_tests
    parent = temporary_parent()
    path = Path(tempfile.mkdtemp(prefix="paquet-auto-prefixe-", dir=str(parent)))
    path.rmdir()
    first = composition.units[0].commit
    added = git(worktree, "worktree", "add", "--detach", str(path), first,
                check=False)
    if added.returncode != 0:
        shutil.rmtree(path, ignore_errors=True)
        return {}, ("worktree de préfixe impossible : %s"
                    % _tail(_decode(added.stderr), 3)), []
    try:
        unresolved = tests
        found: dict[int, list[dict]] = {}
        for index, unit in enumerate(composition.units, start=1):
            if index > 1 and guard is not None:
                guard()
            limit = series_timeout(timeout, deadline)
            if limit is None:
                raise BudgetSpent("borne totale atteinte avant le préfixe %d "
                                  "(#%d)" % (index, unit.lot))
            if index > 1:
                moved = git(path, "reset", "--hard", unit.commit, check=False)
                if moved.returncode != 0:
                    return {}, ("préfixe %d (#%d) illisible : %s"
                                % (index, unit.lot,
                                   _tail(_decode(moved.stderr), 3))), []
                git(path, "clean", "-fdq", check=False)
            run = replay(path, "préfixe %d (#%d)" % (index, unit.lot),
                         [_failure_name(failure) for failure in unresolved],
                         timeout=limit,
                         **({"wall": limit} if deadline is not None else {}))
            if run.inconclusive:
                if deadline is not None and series_timeout(timeout, deadline) is None:
                    # #4463 : coupé par l'échéance totale, pas par le préfixe
                    raise BudgetSpent("borne totale atteinte pendant le préfixe "
                                      "%d (#%d) : %s" % (index, unit.lot,
                                                         run.motive()))
                return {}, ("préfixe %d (#%d) non concluant : %s"
                            % (index, unit.lot, run.motive())), []
            failed = {entry["test"]
                      for entry in unittest_failures(run.output, _unusable(run))}
            if not run.green and not failed:
                return {}, ("préfixe %d (#%d) rouge sans nom de test"
                            % (index, unit.lot)), []
            still = []
            for failure in unresolved:
                if _failure_matches(_failure_name(failure), failed):
                    found.setdefault(unit.lot, []).append(failure)
                else:
                    still.append(failure)
            log("préfixe %d/%d (#%d) : %d test(s) situé(s), %d à suivre"
                % (index, len(composition.units), unit.lot,
                   len(unresolved) - len(still), len(still)))
            unresolved = still
            if not unresolved:
                break
        if unresolved:
            # #4221 : verts au rejeu sur tous les préfixes : instables,
            # l'appelant les consigne non concluants sans arrêter le paquet ;
            # les tests déjà situés (`found`, B1 cx2) restent retirables.
            return found, "", unresolved
        return found, "", []
    finally:
        remove_worktree(worktree, path)


def earlier_red(worktree: Path, composition: Composition,
                removals: list[Removal], *, timeout: int,
                replay=None, log=print, guard=None,
                deadline: float | None = None) -> dict[int, int]:
    """#6232 : les retraits dont le rouge vient d'une unité antérieure retirée.

    Pour chaque retrait hors préfixes (repli, preuve #3801), dans l'ordre de
    l'empilement, les tests rouges de l'unité sont rejoués sur le préfixe qui
    s'arrête à la dernière unité antérieure retirée. S'ils y sont **tous**
    rouges, le rouge précède l'unité : elle est innocente, ni retirée ni
    marquée `packet-unit-red`. Un rejeu non concluant, un retrait sans nom de
    test ou un test seulement en partie rouge garde le retrait (geste
    antérieur). Rend `{lot innocent: lot antérieur retiré}`.
    """
    positions = {unit.lot: index for index, unit in enumerate(composition.units)}
    ordered = sorted((removal for removal in removals
                      if removal.lot in positions),
                     key=lambda removal: positions[removal.lot])
    if len(ordered) < 2:
        return {}
    replay = replay or run_tests
    innocent: dict[int, int] = {}
    kept: list[Removal] = []
    path: Path | None = None
    try:
        for removal in ordered:
            earlier = kept[-1] if kept else None
            names = [name for name in removal.tests if name]
            if earlier is None or removal.proof == "prefixes" or not names \
                    or len(names) != len(removal.tests):
                kept.append(removal)
                continue
            limit = series_timeout(timeout, deadline)
            if limit is None:
                kept.append(removal)
                continue
            if guard is not None:
                guard()
            commit = composition.units[positions[earlier.lot]].commit
            if path is None:
                parent = temporary_parent()
                path = Path(tempfile.mkdtemp(prefix="paquet-auto-anterieur-",
                                             dir=str(parent)))
                path.rmdir()
                added = git(worktree, "worktree", "add", "--detach", str(path),
                            commit, check=False)
                if added.returncode != 0:
                    shutil.rmtree(path, ignore_errors=True)
                    path = None
                    log("tests ciblés : préfixe antérieur impossible, retraits "
                        "gardés : %s" % _tail(_decode(added.stderr), 3))
                    return {}
            else:
                moved = git(path, "reset", "--hard", commit, check=False)
                git(path, "clean", "-fdq", check=False)
                if moved.returncode != 0:
                    kept.append(removal)
                    continue
            label = "préfixe de #%d (rouge de #%d)" % (earlier.lot, removal.lot)
            run = replay(path, label, names, timeout=limit,
                         **({"wall": limit} if deadline is not None else {}))
            failed = ({entry["test"] for entry in unittest_failures(run.output, _unusable(run))}
                      if not run.inconclusive and not run.green else set())
            if failed and all(_failure_matches(name, failed) for name in names):
                innocent[removal.lot] = earlier.lot
                log("tests unité #%d : rouge déjà présent sur le préfixe de #%d "
                    "(retirée) — unité innocente, gardée : %s"
                    % (removal.lot, earlier.lot, ", ".join(names[:5])))
            else:
                kept.append(removal)
    finally:
        if path is not None:
            remove_worktree(worktree, path)
    return innocent


def designated_removals(attribution: dict, failures: list[dict], *,
                        known_lots=None) -> tuple[list[Removal], str]:
    """Les unités désignées sans ambiguïté pour **tous** les échecs.

    Même règle que `paquet_horaire.classify` (#3801) : chaque échec est
    `probable`, sa meilleure preuve atteint `STRONG_EVIDENCE`, et une seule
    unité est à égalité à ce poids. Un seul échec non attribuable annule le
    retrait : on ne retire pas une unité sur une lecture partielle.
    `known_lots` refuse une unité qui ne fait pas partie de la composition.
    """
    if attribution.get("status") != "ok":
        return [], (str(attribution.get("why") or "")
                    or "attribution %s" % (attribution.get("status") or "absente"))
    entries = attribution.get("tests") or []
    if len(entries) != len(failures):
        return [], ("attribution incomplète (%d échec(s), %d entrée(s))"
                    % (len(failures), len(entries)))
    expected = {str(failure.get("test") or "") for failure in failures}
    seen: set[str] = set()
    by_lot: dict[int, Removal] = {}
    for entry in entries:
        name = str(entry.get("test") or "?")
        if name not in expected:
            return [], "entrée %s hors des échecs" % name
        if name in seen:
            return [], "entrée %s en double" % name
        seen.add(name)
        if entry.get("status") != "probable":
            return [], "test %s non attribuable" % name
        units = entry.get("units") or []
        top = max((int(unit.get("best") or 0) for unit in units), default=0)
        best = [unit for unit in units if int(unit.get("best") or 0) == top]
        if top < STRONG_EVIDENCE:
            return [], "test %s : preuve faible (poids %d)" % (name, top)
        if len(best) != 1:
            return [], "test %s : %d unités à égalité" % (name, len(best))
        unit = best[0]
        lot = int(unit["lot"])
        if known_lots is not None and lot not in known_lots:
            return [], "unité #%d hors de la composition" % lot
        removal = by_lot.setdefault(
            lot, Removal(lot=lot, attributed=True, proof="3801"))
        if name not in removal.tests:
            removal.tests.append(name)
        for item in unit.get("evidence") or []:
            label = " ".join((str(item.get("kind") or ""),
                              str(item.get("file") or ""))).strip()
            if label and label not in removal.evidence:
                removal.evidence.append(label)
    return list(by_lot.values()), ""


def _run_series(jobs: list, *, parallel: int, guard=None) -> list[TestRun]:
    """#4271 : les séries `jobs` (sans argument), leurs résultats dans l'ordre.

    En série (`parallel` <= 1), `guard` passe avant chaque série et une garde
    refusée arrête la suite. En parallèle, `guard` passe au départ de chaque
    série ; un refus (ou toute erreur d'une série) annule celles qui ne sont
    pas parties et remonte une fois les séries en cours terminées."""
    if parallel <= 1 or len(jobs) <= 1:
        out = []
        for job in jobs:
            if guard is not None:
                guard()
            out.append(job())
        return out
    stop = threading.Event()
    lock = threading.Lock()

    def start(job):
        if stop.is_set():
            return None
        try:
            if guard is not None:
                with lock:
                    guard()
            return job()
        except BaseException:
            stop.set()
            raise

    pool = ThreadPoolExecutor(max_workers=min(parallel, len(jobs)),
                              thread_name_prefix="paquet-series")
    try:
        futures = [pool.submit(start, job) for job in jobs]
        results, error = [], None
        for future in futures:
            try:
                results.append(future.result())
            except BaseException as exc:  # noqa: BLE001 - remonté après les autres
                error = error or exc
        if error is not None:
            raise error
        return results
    finally:
        pool.shutdown(wait=True, cancel_futures=True)


def _merge_removals(*groups: list[Removal]) -> list[Removal]:
    """Fusionne des retraits par lot, sans perdre tests ni preuves."""
    by_lot: dict[int, Removal] = {}
    order: list[int] = []
    for group in groups:
        for removal in group:
            current = by_lot.get(removal.lot)
            if current is None:
                current = Removal(lot=removal.lot,
                                  attributed=removal.attributed,
                                  proof=removal.proof)
                by_lot[removal.lot] = current
                order.append(removal.lot)
            for test in removal.tests:
                if test not in current.tests:
                    current.tests.append(test)
            for item in removal.evidence:
                if item not in current.evidence:
                    current.evidence.append(item)
            current.attributed = current.attributed or removal.attributed
            if removal.proof == "prefixes":
                current.proof = "prefixes"
            elif removal.proof and not current.proof:
                current.proof = removal.proof
    return [by_lot[lot] for lot in order]


def series_timeout(timeout: int, deadline: float | None) -> int | None:
    """#4463 : la borne d'une série sous l'échéance totale ; None si échue."""
    if deadline is None:
        return timeout
    left = int(deadline - time.monotonic())
    return min(timeout, left) if left > 0 else None


def series_share(timeout: int, deadline: float | None, pending: int,
                 parallel: int = 1, reserve: int = 0) -> int | None:
    """#4887 : la part d'une série dans la borne totale ; None si échue.

    h-10 : dix séries, deux créneaux, 1800 s. La première série pouvait prendre
    jusqu'à 1200 s (`packet_tests.unit_timeout_seconds`) et la deuxième le
    reste : les huit autres ne partaient jamais, et leurs rouges déterministes
    n'étaient vus qu'en CI. Ici chaque série ne reçoit que sa part des vagues
    restantes (`pending` séries pas encore parties, celle-ci comprise, divisé
    par `parallel` séries à la fois) : toutes les unités commencent avant
    l'échéance, la borne totale restant la même. `reserve` garde de quoi
    rejouer un rouge de voisin sur la base et sur les préfixes : sans elle, la
    part des séries consomme tout et le rejeu est coupé, le vrai rouge reste
    sans attribution (h-10, `test_2442`). Une part arrondie vaut au moins une
    seconde ; `timeout` reste le plafond de la série.
    """
    if deadline is None:
        return timeout
    left = int(deadline - time.monotonic())
    if left <= 0:
        return None
    width = max(1, int(parallel))
    waves = max(1, (max(1, pending) + width - 1) // width)
    usable = left - max(0, int(reserve))
    if usable <= 0:
        return None
    return max(1, min(timeout, left, usable // waves))


def budget_spent(label: str, modules: list[str]) -> TestRun:
    """#4463 : une série qui ne part plus, l'échéance totale passée (§3)."""
    return TestRun(label=label, modules=list(modules), returncode=None,
                   output="borne totale des tests par unité atteinte : série "
                          "non jouée (#4463)", seconds=0.0, timed_out=True,
                   direct=list(modules))



def check_local_tests(worktree: Path, composition: Composition, *,
                      timeout: int, log=print,
                      attribute=None, base_sha: str = "",
                      select=None, on_base=None, guard=None,
                      prefix=None, earlier=None,
                      parallel: int = DEFAULT_PARALLEL,
                      deadline: float | None = None,
                      contract_report: ContractPass | None = None,
                      legacy_contracts: bool = False) -> tuple[list[TestRun], list[int],
                                                              list[Removal]]:
    """Exécute les modules des unités puis les contrats.

    Rend `(exécutions, unités fautives, retraits)`. Une unité est fautive
    quand un test ciblé la désigne :
    * pour un rouge de module **voisin**, par le rejeu du test sur les
      préfixes de l'empilement (`prefix_failures`, injectable par `prefix`) :
      la première unité où il apparaît est retirée (#4217) ;
    * pour les rouges propres et les contrats, par attribution #3801
      (`packet_red`) quand **chaque** échec est attribué sans ambiguïté à une
      seule unité avec une preuve forte (#3931) ;
    * sinon par le geste antérieur : le module propre de l'unité a rougi, et un
      contrat rouge non attribuable arrête.

    Un rouge de voisin que les préfixes ne permettent pas de situer lève
    `AttributionUnreadable` : aucun retrait automatique, le pilote est
    prévenu et un agent lit. Un test rouge sur la tête mais **vert sur tous
    les préfixes** est instable (#4221) : consigné non concluant dans `runs`
    (`flaky_tests`), sans retrait, et le paquet continue. Un résultat non
    concluant (délai, base absente, psql/psycopg indisponible, rc inattendu)
    est **consigné** dans `runs` et ne retire aucune unité : le canon §3 veut
    qu'il ne retienne ni paquet ni release ; la ci-full tranche.
    `attribute` est injectable pour les témoins (`attribute_composition`).

    #6232 : un retrait dont les tests rouges le sont déjà sur le préfixe d'une
    unité antérieure retirée est abandonné (`earlier_red`, injectable par
    `earlier`) : l'unité innocente n'est pas retirée avec la fautive.

    #4025 : la série d'une unité joue aussi ses modules voisins
    (`neighbor_modules`, injectable par `select`). Dans le geste antérieur,
    un rouge d'un module `carte` ne désigne l'unité que si aucune autre ne
    l'a sélectionné ; un rouge partagé reste à l'attribution et à la ci-full.
    Un rouge de module voisin aussi rouge sur la base (`base_failures`,
    injectable par `on_base`) ne compte pas : environnement ou préexistant.

    #4271 : avec `parallel` > 1, les séries des unités et celle des contrats
    sont indépendantes et jouées en même temps (au plus `parallel`), chacune
    dans un créneau `ameesh heavy` de l'hôte ; leurs résultats sont lus dans
    l'ordre des unités, comme en série. `guard` est relu avant chaque série.

    #4463 : `deadline` (horloge monotone) borne la durée totale : chaque série
    est bornée au temps restant ; une série qui n'est pas partie à l'échéance
    est non concluante sans être jouée. L'attente d'un créneau `ameesh heavy`
    compte dans ce temps (`run_tests(wall=…)`) : à l'échéance la série est
    arrêtée, au plus `WALL_GRACE_S` après. #7195 : pour une série `heavy`,
    cette attente ne consomme plus sa part d'exécution (`timeout`, passée à
    `ameesh heavy --max-duration`) ; seule la série directe voit sa part
    borner aussi son temps mural.
    """
    contract_run = None
    if contract_report is not None:
        verify_contract_scope(worktree, contract_report)
        contract_run = contract_report.run
    runs: list[TestRun] = []
    red: list[tuple[TestRun, list[dict]]] = []
    own: dict[int, list[dict]] = {}
    neighbors, composition.selection = (select or neighbor_modules)(
        worktree, composition, base_sha)
    shared: dict[str, int] = {}
    for rows in neighbors.values():
        for module in {row["module"] for row in rows}:
            shared[module] = shared.get(module, 0) + 1
    if composition.selection:
        log("tests voisins : %s" % composition.selection)
    # #4298 : sans `ameesh` dans le PATH, ni créneau ni schéma isolé : des
    # séries simultanées partageraient la base ; repli en série, dit au journal
    if parallel > 1 and shutil.which("ameesh") is None:
        log("tests ciblés : `ameesh` absent du PATH, pas de créneau `ameesh "
            "heavy` : séries jouées l'une après l'autre (au lieu de %d à la "
            "fois)" % parallel)
        parallel = 1
    # le créneau de l'hôte n'est demandé qu'en parallèle : la série reste le
    # geste antérieur, à l'identique
    slots = {"heavy": True} if parallel > 1 else {}
    planned: list[tuple[Unit, list[str], list[dict], list[str]]] = []
    for unit in composition.units:
        proper = unit_test_modules(worktree, unit)
        added = []
        for row in neighbors.get(unit.lot, []):
            if row["module"] not in proper \
                    and all(row["module"] != done["module"] for done in added):
                added.append(row)
        modules = proper + [row["module"] for row in added]
        if not modules:
            log("tests unité #%d : aucun module propre" % unit.lot)
            continue
        if added:
            log("tests unité #%d : %d module(s) voisin(s) : %s"
                % (unit.lot, len(added),
                   ", ".join("%s (%s)" % (row["module"], row["reason"])
                             for row in added)))
        planned.append((unit, proper, added, modules))

    def bounded(label: str, modules: list[str]) -> TestRun:
        limit, here = next_share()
        if limit is None:
            log("tests %s : borne totale atteinte, série non jouée (non "
                "concluant)" % label)
            return budget_spent(label, modules)
        if deadline is not None:
            log("tests %s : part de %d s sur la borne totale (%d série(s) à "
                "partir, %d à la fois)" % (label, limit, here,
                                           max(1, parallel)))
        # #4463 : l'attente du créneau compte dans l'échéance totale mais,
        # #7195, pas dans la part d'exécution d'une série `heavy` : une série
        # qui attend longtemps son créneau garde sa part pour jouer ses
        # tests, la borne restant le temps restant jusqu'à l'échéance. Une
        # série directe n'attend aucun créneau : sa part borne aussi son
        # temps mural.
        walled = {}
        if deadline is not None:
            wall = max(1, int(deadline - time.monotonic())) if slots else limit
            walled = {"wall": wall}
        run = run_tests(worktree, label, modules, timeout=limit, **slots, **walled)
        run.direct = list(modules)
        return run

    def unit_job(label: str, modules: list[str]):
        return lambda: bounded(label, modules)

    def contract_job():
        remaining = max(0.1, deadline - time.monotonic()) if deadline is not None else GIT_TIMEOUT
        try:
            modules = contract_modules(worktree, base="" if legacy_contracts else base_sha, timeout=remaining)
        except PaquetAutoError as exc:
            # Unavailable planning is not a negative test. CI still runs the
            # full suite; this pass records no qualification of the contracts.
            log("tests contrats : plan indisponible (non concluant) : %s" % exc)
            return TestRun("contrats", [], None, "plan indisponible : %s" % exc,
                           0.0, timed_out=True)
        if _TOOLING.declared:
            # #6274 : identités opaques, jamais `unittest` ; même borne.
            limit = series_timeout(timeout, deadline)
            if limit is None:
                return budget_spent("contrats", modules)
            return run_declared_contracts(worktree, modules,
                                          end=time.monotonic() + limit,
                                          tooling=_TOOLING)
        return bounded("contrats", modules)

    jobs = [unit_job("unité #%d" % unit.lot, modules)
            for unit, _proper, _added, modules in planned]
    if contract_run is None:
        jobs.append(contract_job)
    # #4887 : la part de chaque série est calculée au départ, pas à la
    # composition : une série qui finit tôt rend sa part aux suivantes. Une
    # réserve reste pour le rejeu d'un rouge de voisin (base, préfixes), sans
    # quoi la part des séries le coupe et le vrai rouge n'est pas attribué.
    remaining = {"left": len(jobs)}
    remaining_lock = threading.Lock()
    reserve = 0
    if deadline is not None:
        total = max(0, int(deadline - time.monotonic()))
        reserve = min(300, max(30, total // 5))
        log("tests ciblés : réserve de %d s sur la borne totale pour les "
            "rejeux base/préfixes (#4887)" % reserve)

    def next_share() -> tuple[int | None, int]:
        with remaining_lock:
            here = max(1, remaining["left"])
            remaining["left"] = max(0, remaining["left"] - 1)
        return series_share(timeout, deadline, here, parallel, reserve), here

    if slots:
        log("tests ciblés : %d série(s), %d à la fois, créneaux `ameesh heavy`, "
            "borne %d s par série, part de la borne totale (#4887)"
            % (len(jobs), min(parallel, len(jobs)), timeout))
    results = _run_series(jobs, parallel=parallel, guard=guard)
    contract = results.pop() if contract_run is None else contract_run
    pending: list[tuple[Unit, TestRun, list[dict], set[str], set[str],
                        set[str]]] = []
    for (unit, proper, added, modules), run in zip(planned, results):
        run.added = added
        run.direct = list(modules)
        runs.append(run)
        if run.inconclusive:
            log("tests unité #%d : NON CONCLUANT (%s) — consigné, le paquet "
                "continue" % (unit.lot, run.motive()))
            continue
        if not run.green:
            found = unittest_failures(run.output, _unusable(run))
            proper_names = {module.split(".", 1)[1] for module in proper}
            mine = set(proper_names)
            neighbor = {row["module"].split(".", 1)[1] for row in added}
            mine |= {row["module"].split(".", 1)[1] for row in added
                     if row["reason"] != "carte" or shared.get(row["module"]) == 1}
            pending.append((unit, run, found, proper_names, mine, neighbor))
            log("tests unité #%d : ROUGE (%d modules) — modules : %s%s\n%s"
                % (unit.lot, len(modules), ", ".join(modules),
                   (" — journal local : " + run.log_path) if run.log_path else "",
                   _tail(run.output)))
        else:
            log("tests unité #%d : vert (%d modules, %d tests)"
                % (unit.lot, len(modules), run.tests))
    neighbor_red = sorted({"tests." + failure["module"]
                           for _unit, _run, found, _proper, _mine, neighbor
                           in pending
                           for failure in found
                           if failure.get("module") in neighbor})
    at_base: set[str] | None = set()
    if neighbor_red:
        if guard is not None:
            guard()
        limit = series_timeout(timeout, deadline)
        if limit is not None and deadline is not None:
            # #4887 : garder la moitié du temps restant au rejeu des préfixes,
            # qui attribue le rouge ; un rejeu de base coupé reste non concluant
            # et le rouge est conservé (voir plus bas).
            left = max(1, int(deadline - time.monotonic()))
            limit = min(limit, max(30, left // 2))
        walled = {"wall": limit} if deadline is not None and limit else {}
        at_base = (None if limit is None else
                   (on_base or base_failures)(worktree, base_sha, neighbor_red,
                                              timeout=limit, **walled))
        log("tests voisins rouges rejoués sur la base %s : %s"
            % (base_sha[:12] or "?", "non concluant" if at_base is None
               else "%d rouge(s) aussi sur la base" % len(at_base)))
    neighbor_tests: dict[str, dict] = {}
    owned_by_lot: dict[int, list[dict]] = {}
    unparsed_lots: set[int] = set()
    unattributed: set[str] = set()
    for unit, run, found, _proper, mine, neighbor in pending:
        kept = []
        unread: list[dict] = []
        for failure in found:
            if failure.get("module") in neighbor:
                if at_base is None:
                    # #4887 (auditeur B) : sans rejeu de base concluant, le
                    # préfixe 1 ne peut pas distinguer un rouge préexistant
                    # d'un rouge de u1 ; on conserve le rouge mais on ne
                    # l'attribue pas (la ci-full tranche).
                    kept.append(failure)
                    unread.append(failure)
                    continue
                if failure["test"] in at_base:
                    continue
            kept.append(failure)
        owned = [failure for failure in kept
                 if failure.get("module") not in neighbor
                 and (not failure.get("module") or failure["module"] in mine)]
        owned_by_lot[unit.lot] = owned
        if not found:
            unparsed_lots.add(unit.lot)
        for failure in kept:
            if failure.get("module") in neighbor and not unread:
                neighbor_tests.setdefault(_failure_name(failure), failure)
        if unread:
            names = sorted({_failure_name(failure) for failure in unread
                            if _failure_name(failure)})
            run.unattributed = names
            unattributed.update(names)
            log("tests unité #%d : %d rouge(s) de voisin conservé(s), rejeu de "
                "base non concluant — non attribué(s), la ci-full tranche : %s"
                % (unit.lot, len(names), ", ".join(names[:5])))
        if kept or not found:
            red.append((run, kept))
        else:
            run.ignored_red = True
            log("tests unité #%d : rouge des seuls voisins, aussi rouge(s) sur "
                "la base — sans effet" % unit.lot)
    own_names = {_failure_name(failure) for failures in owned_by_lot.values()
                 for failure in failures}
    prefix_names = {name for name in neighbor_tests
                    if name and name not in own_names}
    prefix_red = [neighbor_tests[name] for name in neighbor_tests
                  if name in prefix_names]
    for lot, owned in owned_by_lot.items():
        # #4221 (P3) : `prefix_names` exclut déjà `own_names`, donc aucun
        # échec propre ne peut y figurer ; le filtre `remaining_own` était mort.
        if owned or lot in unparsed_lots:
            own[lot] = owned
    modules = contract.modules
    runs.append(contract)
    contract_red = False
    if contract.inconclusive:
        log("tests contrats : NON CONCLUANT (%s) — consigné, le paquet "
            "continue" % contract.motive())
    elif not contract.green:
        contract_red = True
        red.append((contract, [{"test": name, "module": ""}
                               for name in contract.failed_tests]
                    if isinstance(contract, FlowTestRun)
                    else unittest_failures(contract.output, _unusable(contract))))
        log("tests contrats : ROUGE (%d modules)\n%s"
            % (len(modules), _tail(contract.output)))
    else:
        log("tests contrats : vert (%d modules, %d tests)"
            % (len(modules), contract.tests))
    if not red:
        return runs, [], []
    failures: list[dict] = []
    seen_failures: set[str] = set()
    for _run, found in red:
        for failure in found:
            name = str(failure.get("test") or "")
            if name and name in seen_failures:
                continue
            if name:
                seen_failures.add(name)
            failures.append(failure)
    why = ""
    removals: list[Removal] = []
    if prefix_red:
        if not all(found for _run, found in red):
            raise AttributionUnreadable(
                "un rouge sans nom de test ne peut pas être situé sur les "
                "préfixes",
                tests=[_failure_name(failure) for failure in prefix_red])
        if guard is not None:
            guard()
        log("tests voisins : rejeu du rouge sur les préfixes de l'empilement "
            "(%d test(s), %d unité(s))"
            % (len(prefix_red), len(composition.units)))
        bounds = ({"guard": guard} if guard is not None else {}) | (
            {"deadline": deadline} if deadline is not None else {})
        try:
            found_by_lot, prefix_why, flaky = (prefix or prefix_failures)(
                worktree, composition, prefix_red, timeout=timeout, log=log,
                **bounds)
        except BudgetSpent as spent:
            # #4463 : non concluant (§3) ; la ci-full tranche, rien n'est retiré
            log("tests voisins : préfixes NON CONCLUANTS (%s) — consigné, le "
                "paquet continue" % spent)
            runs.append(budget_spent("préfixes", [_failure_name(failure)
                                                  for failure in prefix_red]))
            found_by_lot, prefix_why, flaky = {}, "", []
        except (AttributionUnreadable, CarrierSettled, EpochLost):
            raise
        except PaquetAutoError as exc:
            raise AttributionUnreadable(
                str(exc), tests=[_failure_name(failure)
                                 for failure in prefix_red]) from exc
        if prefix_why:
            raise AttributionUnreadable(
                prefix_why,
                tests=[_failure_name(failure) for failure in prefix_red])
        if flaky:
            # #4221 : rouge sur la tête mais vert sur tous les préfixes =
            # instable. Consigné non concluant, le paquet continue (canon §3).
            flaky_names = sorted({_failure_name(failure) for failure in flaky
                                  if _failure_name(failure)})
            log("tests voisins : %d test(s) rouge(s) sur la tête mais vert(s) "
                "sur tous les préfixes (instable) : %s — consigné(s) non "
                "concluant(s), le paquet continue"
                % (len(flaky_names), ", ".join(flaky_names[:5])))
            kept_red: list[tuple[TestRun, list[dict]]] = []
            for run, found in red:
                names_here = sorted({_failure_name(failure)
                                     for failure in found
                                     if _failure_name(failure) in flaky_names})
                left = [failure for failure in found
                        if _failure_name(failure) not in flaky_names]
                if len(left) == len(found):
                    kept_red.append((run, found))
                    continue
                if left:
                    # des rouges réels restent dans la même série : la série
                    # reste rouge, l'instable est consigné à part.
                    kept_red.append((run, left))
                    runs.append(TestRun(
                        label="%s (instable)" % run.label,
                        modules=sorted({"tests." + str(failure.get("module"))
                                        for failure in found
                                        if failure.get("module")
                                        and _failure_name(failure)
                                        in names_here}),
                        returncode=1,
                        output="tests rouges sur la tête mais verts sur tous "
                               "les préfixes : %s" % ", ".join(names_here),
                        seconds=0.0, flaky_tests=names_here,
                        log_path=run.log_path))
                else:
                    run.flaky_tests = names_here
            red = kept_red
            if not red:
                return runs, [], []
            failures = [failure for failure in failures
                        if _failure_name(failure) not in flaky_names]
        positions = {unit.lot: index for index, unit in enumerate(
            composition.units, start=1)}
        unknown = [lot for lot in found_by_lot if lot not in positions]
        if unknown:
            raise AttributionUnreadable(
                "unité(s) hors de la composition : %s"
                % ", ".join("#%d" % lot for lot in unknown),
                tests=[_failure_name(failure) for failure in prefix_red])
        for lot, found in found_by_lot.items():
            removals.append(Removal(
                lot=lot,
                tests=[_failure_name(failure) for failure in found],
                evidence=["préfixe base+u1…u%d (premier rouge à #%d)"
                          % (positions.get(lot, 0), lot)],
                attributed=True, proof="prefixes"))
    remaining = [failure for failure in failures
                 if _failure_name(failure) not in prefix_names
                 and _failure_name(failure) not in unattributed]
    designated: list[Removal] = []
    if remaining:
        if all(found for _run, found in red):
            reader = attribute or attribute_composition
            report = reader(worktree, composition, remaining)
            designated, why = designated_removals(
                report, remaining,
                known_lots={unit.lot for unit in composition.units})
        else:
            why = "un rouge sans nom de test attribuable"
    if designated:
        removals = _merge_removals(removals, designated)
    elif own:
        fallback = [Removal(lot=lot,
                            tests=[_failure_name(failure) for failure in found])
                    for lot, found in own.items()]
        removals = _merge_removals(removals, fallback)
        if contract_red:
            log("tests contrats : rouge non attribuable (%s) — retrait des "
                "unités aux tests propres rouges, puis recomposition"
                % (why or "attribution incertaine"))
    if len(removals) > 1:
        if guard is not None:
            guard()
        bounds = ({"guard": guard} if guard is not None else {}) | (
            {"deadline": deadline} if deadline is not None else {})
        innocent = (earlier or earlier_red)(
            worktree, composition, removals, timeout=timeout, log=log,
            **bounds)
        removals = [removal for removal in removals
                    if removal.lot not in innocent]
    if removals:
        how = why
        if not how and any(removal.proof == "prefixes" for removal in removals):
            how = "préfixes de l'empilement"
        log("tests ciblés : retrait de %s (%s)"
            % (", ".join("#%d" % removal.lot for removal in removals),
               how or "attribution"))
        return runs, [removal.lot for removal in removals], removals
    if contract_red and not own:
        raise CompositionRed(
            "tests de contrat rouges sur la tête composée (aucune unité retirée "
            "automatiquement : %s) :\n%s"
            % (why or "attribution incertaine", _tail(contract.output)))
    return runs, [], []


# ---------------------------------------------------------------------------
# Manifeste
# ---------------------------------------------------------------------------


def composition_table(worktree: Path, base_sha: str,
                      compose_script: Path | None = None) -> str:
    """La table `## Composition` produite par `paquet-compose.py --units`."""
    script = Path(compose_script) if compose_script else (worktree / COMPOSE_SCRIPT)
    if not script.is_file():
        raise PaquetAutoError("%s absent de l'arbre composé" % COMPOSE_SCRIPT)
    proc = run_command([project_python(worktree), str(script), "--repo",
                        str(worktree), "--units", "--base", base_sha],
                       cwd=worktree, timeout=DEFAULT_TIMEOUT,
                       env=python_env(worktree))
    if proc.returncode != 0:
        raise PaquetAutoError(
            "paquet-compose --units a échoué : %s" % _tail(_output(proc)))
    text = _decode(proc.stdout)
    start = text.find("## Composition")
    if start < 0:
        raise PaquetAutoError(
            "paquet-compose --units n'a pas rendu la table ## Composition :\n%s"
            % _tail(text))
    return text[start:].rstrip() + "\n"


def _one_line(value: str) -> str:
    return " ".join(str(value or "").split())


def _cell(value: str) -> str:
    """Une cellule de tableau markdown : ni saut de ligne ni `|`."""
    return _one_line(value).replace("|", "/")


def build_manifest(*, name: str, target: str, base_sha: str, carrier: int,
                   composition: Composition, table: str,
                   tests: list[TestRun] | None, freeze_seconds: float,
                   ready_count: int = 0, waiting: int = 0,
                   errors: int = 0, client_types: str = "") -> str:
    """Le manifeste §12, lisible par `paquet-inclusion.py` (#1644/#2409).

    #4304 : `tests=None` — le manifeste est écrit et poussé avant les tests par
    unité, qui tournent en parallèle de la CI complète ; il le dit
    (`TESTS_PARALLEL_LINE`) et leur résultat va à la PR, au porteur et au
    courrier.

    La table d'admissions ne porte que le lot et sa source : un id cité dans
    un objet (`#N`) serait relu par le contrôle d'inclusion comme un lot
    attendu. Les objets et les classes restent à la PR et au courrier.
    """
    try:
        admissions = parse_composition_table(table)
    except InputError:
        admissions = [([unit.lot], unit.source) for unit in composition.units]
    lots = len({lot for members, _source in admissions for lot in members})
    # #6727 : Composition ∩ Écartés = ∅ ; `packet_close` tiendrait le lot
    # composé pour retiré et ne le clôturerait pas à la fusion.
    composed = {unit.lot for unit in composition.units} | {
        lot for members, _source in admissions for lot in members}
    both = sorted(composed & {item.lot for item in composition.discarded})
    if both:
        raise PaquetAutoError(
            "manifeste refusé : %s à la fois composé(s) et écarté(s)"
            % ", ".join("#%d" % lot for lot in both))
    lines = [
        "# Paquet %s — composition (§12), sur %s `%s`"
        % (package_branch(name), target, base_sha[:12]),
        "",
        "Base : `%s` **`%s`** (tête exacte d'`origin/%s`). Branche `%s`, cible `%s`."
        % (target, base_sha, target, package_branch(name), target),
        "Porteur : **#%d**. Composeur : `scripts/paquet-auto.py` (sans agent)."
        % carrier,
        "",
        "## Admissions (%d unités, %d lots)" % (len(composition.units), lots),
        "",
        "| lot | source composée |",
        "|---|---|",
    ]
    for members, source in admissions:
        for lot in members:
            lines.append("| #%d | `%s` |" % (lot, source))
    lines += ["", table.rstrip(), ""]
    lines += [
        "## Écartés",
        "",
        "| lot | motif |",
        "|---|---|",
    ]
    if composition.discarded:
        for item in composition.discarded:
            motive = _cell(item.reason)
            if item.detail:
                motive += " — " + _cell(item.detail)
            lines.append("| #%d | %s |" % (item.lot, motive))
    else:
        lines.append("| — | aucun |")
    lines += [
        "",
        "Liste calculée : %d lot(s) prêt(s), %d en attente, %d en erreur "
        "(les erreurs sont écartées ci-dessus, jamais composées)."
        % (ready_count, waiting, errors),
        "",
        "## Contrôles locaux",
        "",
        "- `scripts/freeze-checks.py` : vert en %.1f s." % freeze_seconds,
    ]
    if client_types:
        # #6676 : un non-concluant faute de modules se voit au manifeste ; les
        # ids d'indice (« unité(s) #N ») ne sont pas relus par l'inclusion
        lines.append("- Types clients : %s."
                     % re.sub(r"#(\d+)", r"lot \1", _cell(client_types)))
    if tests is None:
        lines.append(TESTS_PARALLEL_LINE)
        lines.append("- Contrats du plan commun : passe bornée sur cet arbre après "
                     "inclusion et avant poussée ; rapport réutilisé par les tests "
                     "locaux, résultat à la PR et au porteur (#6182).")
        lines.append("- Modules directs par lot : lus au corps de la PR après "
                     "les tests (#4887).")
        tests = []
    else:
        lines.append("- Tests locaux : %s." % _tests_summary(tests))
    lines += _neighbor_lines(composition, tests)
    inconclusive = _inconclusive_lines(tests)
    if inconclusive:
        lines += [
            "",
            "Non concluants (canon §3 : ne retiennent ni paquet ni release) :",
            "",
        ] + inconclusive
    unattributed = _unattributed_lines(tests)
    if unattributed:
        lines += [
            "",
            "Rouges de voisins non attribués (rejeu de base non concluant, "
            "#4887) :",
            "",
        ] + unattributed
    lines += [
        "",
        "- `scripts/paquet-inclusion.py` : exécuté sur cette tête après le "
        "manifeste (résultat au courrier du pilote).",
        "",
    ]
    return "\n".join(lines)


def _tests_summary(tests: list[TestRun]) -> str:
    modules = sorted({module for run in tests for module in run.modules})
    total = sum(run.tests for run in tests)
    skipped = sum(run.skipped for run in tests)
    red = sum(1 for run in tests if not run.green and not run.inconclusive
              and not run.ignored_red)
    ignored = sum(1 for run in tests if run.ignored_red)
    inconclusive = sum(1 for run in tests if run.inconclusive)
    # #4814 : une série arrêtée par sa borne n'a joué aucun de ses modules
    # jusqu'au bout ; le rapport ne les compte plus comme joués
    finished = {module for run in tests if run.finished for module in run.modules}
    # #6042 : le compte des sautés est un compte de tests ; la liste qui le
    # suivait est celle des modules des séries, pas celle des modules sautés
    # (unittest -q ne nomme pas les tests sautés). Elle est donc nommée.
    return ("%d séries, %d modules dont %d joués jusqu'au bout, %d tests "
            "exécutés, %d échec(s), %d run(s) rouge(s) sans effet, "
            "%d non concluant(s), %d test(s) sauté(s) ; "
            "modules des séries : %s"
            % (len(tests), len(modules), len(finished), total, red, ignored,
               inconclusive, skipped,
               ", ".join("`%s`" % module for module in modules) or "aucun"))


def _neighbor_lines(composition: Composition, tests: list[TestRun]) -> list[str]:
    """#4025 : les modules voisins joués par unité, avec leur raison."""
    rows = []
    for run in tests:
        for row in run.added:
            rows.append("| %s | `%s` | %s | %s |" % (
                run.label.replace("#", ""), row["module"], row["reason"],
                _cell(", ".join("`%s`" % path for path in row["files"][:3])
                      + (" …" if len(row["files"]) > 3 else ""))))
    if not rows and not composition.selection:
        return []
    lines = ["", "Modules voisins joués (support importé, carte d'impact) : %s."
             % (composition.selection or "sélection sans note"), ""]
    if rows:
        lines += ["| unité | module | raison | fichiers de l'unité |",
                  "|---|---|---|---|"] + rows
    else:
        lines.append("Aucun module voisin ajouté.")
    return lines


def _direct_lines(composition: Composition, tests: list[TestRun]) -> list[str]:
    """#4887 : par lot, ses modules directs et s'ils ont été joués.

    Les modules directs d'une unité sont ceux de sa série : ses propres tests
    et les voisins que la sélection lui a donnés. Une série coupée par sa part
    de borne n'en a joué aucun jusqu'au bout (#4814) : le manifeste le dit lot
    par lot, au lieu de ne compter que les modules de la tête.
    """
    by_label = {run.label: run for run in tests}
    rows: list[str] = []
    for unit in composition.units:
        run = by_label.get("unité #%d" % unit.lot)
        direct = list(run.direct or run.modules) if run is not None else []
        if run is None:
            state = "aucun module"
        elif run.finished:
            state = "oui (%d/%d)" % (len(direct), len(direct))
        else:
            state = "non (série coupée après %.0f s%s)" % (
                run.seconds,
                ", %d test(s)" % run.tests if run.tests else "")
        rows.append("| #%d | %d | %s |" % (unit.lot, len(direct), state))
    return ["", "Modules directs par lot (#4887) :", "",
            "| lot | modules directs | joués jusqu'au bout |", "|---|---|---|"] + rows


def _direct_note(composition: Composition, tests: list[TestRun]) -> str:
    """#4887 : les lots dont la série de modules directs n'est pas allée au bout."""
    by_label = {run.label: run for run in tests}
    missing = [unit.lot for unit in composition.units
               if (run := by_label.get("unité #%d" % unit.lot)) is not None
               and not run.finished]
    if not missing:
        return "modules directs : tous joués jusqu'au bout"
    return "modules directs non joués : " + ", ".join("#%d" % lot for lot in missing)


def _inconclusive_lines(tests: list[TestRun]) -> list[str]:
    """Une ligne par run non concluant : module, motif, extrait (canon §3)."""
    lines: list[str] = []
    for run in tests:
        if not run.inconclusive:
            continue
        modules = ", ".join(run.modules) or "aucun module"
        spent = ("%.0f s de série" % run.seconds) if run.seconds else ""
        tests_run = ("%d test(s) exécuté(s)" % run.tests) if run.tests else ""
        detail = " ; ".join(part for part in (spent, tests_run) if part)
        lines.append("- NON CONCLUANT : %s — modules %s — %s%s — extrait : %s%s"
                     % (run.label.replace("#", ""), modules, run.motive(),
                        (" (%s)" % detail) if detail else "",
                        _one_line(run.excerpt())[:240],
                        (" — journal local : " + run.log_path) if run.log_path else ""))
    return lines


def _unattributed_lines(tests: list[TestRun]) -> list[str]:
    """#4887 : les rouges de voisins conservés faute de rejeu de base concluant."""
    lines: list[str] = []
    for run in tests:
        if not run.unattributed:
            continue
        lines.append("- NON ATTRIBUÉ (#4887) : %s — rouges de voisin : %s — "
                     "rejeu de base non concluant, la ci-full tranche%s"
                     % (run.label.replace("#", ""), ", ".join(run.unattributed),
                        (" — journal local : " + run.log_path)
                        if run.log_path else ""))
    return lines


def write_manifest(worktree: Path, rel: str, text: str, *,
                   name: str, units: int, discarded: int) -> str:
    path = worktree / rel
    if not path.parent.is_dir():
        path.parent.mkdir(parents=True)
    path.write_text(text, encoding="utf-8")
    git(worktree, "add", "--", rel)
    message = ("paquet %s : manifeste — %d unité(s), %d écarté(s)"
               % (name, units, discarded))
    proc = git(worktree, "commit", "-m", message, check=False)
    if proc.returncode != 0:
        raise PaquetAutoError(
            "commit du manifeste : %s" % _decode(proc.stderr).strip())
    head = rev(worktree, "HEAD")
    if head is None:
        raise PaquetAutoError("manifeste commité sans tête")
    return head


# ---------------------------------------------------------------------------
# Inclusion, paquet, poussée, PR, gel, courrier
# ---------------------------------------------------------------------------


def run_inclusion(worktree: Path, manifest: Path, head: str,
                  ready_file: Path, expected_file: Path) -> tuple[int, str]:
    script = worktree / INCLUSION_SCRIPT
    if not script.is_file():
        raise PaquetAutoError("%s absent de l'arbre composé" % INCLUSION_SCRIPT)
    cmd = [project_python(worktree), str(script), "--repo", str(worktree),
           "--manifest", str(manifest), "--head", head,
           "--ready-file", str(ready_file), "--expected-file", str(expected_file)]
    proc = run_command(cmd, cwd=worktree, timeout=DEFAULT_TIMEOUT,
                       env=python_env(worktree))
    return proc.returncode, _output(proc)


def record_packet(worktree: Path, carrier: int, base_sha: str, head: str,
                  branch: str, target: str, expect_pushed: str = "") -> str:
    cmd = ["ameesh", "work", "packet", "record", str(carrier),
           "--repo", str(worktree), "--base", base_sha, "--head", head,
           "--branch", branch, "--target", target]
    if expect_pushed:
        # #4501 : la tête poussée que cette composition remplace (bail du
        # composeur) ; le refus d'une tête gelée étrangère vient de packet_link.
        cmd += ["--expect-pushed", expect_pushed]
    proc = run_command(cmd, cwd=worktree, timeout=DEFAULT_TIMEOUT)
    if proc.returncode != 0:
        error = EpochLost if REFUSAL_RE.search(_output(proc)) else PaquetAutoError
        raise error(
            "ameesh work packet record a échoué (rc %d) : %s"
            % (proc.returncode, _tail(_output(proc))))
    return _output(proc)


def ensure_carrier_branch(repo: Path, carrier: int, branch: str,
                          target: str) -> None:
    """Vérifie que le porteur est sur la branche du paquet ; la pose si vide."""
    proc = run_command(["ameesh", "work", "show", str(carrier), "--json"],
                       cwd=repo, timeout=GIT_TIMEOUT)
    if proc.returncode != 0:
        raise PaquetAutoError(
            "porteur #%d illisible : %s" % (carrier, _tail(_output(proc))))
    try:
        item = json.loads(_decode(proc.stdout))
    except ValueError as exc:
        raise PaquetAutoError("porteur #%d : JSON illisible (%s)" % (carrier, exc)) from exc
    current = str(item.get("branch") or "").strip()
    if current == branch:
        return
    if current:
        raise InputError(
            "porteur #%d est déjà sur %s, pas sur %s : détacher sa branche ou "
            "créer un autre porteur" % (carrier, current, branch))
    cmd = ["ameesh", "work", "branch", str(carrier), branch, "--target", target]
    proc = run_command(cmd, cwd=repo, timeout=GIT_TIMEOUT)
    if proc.returncode != 0:
        raise PaquetAutoError(
            "ameesh work branch #%d %s a échoué : %s"
            % (carrier, branch, _tail(_output(proc))))


def push_branch(worktree: Path, branch: str,
                force_lease: str | None = None) -> str:
    cmd = ["push"]
    if force_lease:
        cmd.append("--force-with-lease=refs/heads/%s:%s" % (branch, force_lease))
    cmd += ["--set-upstream", "origin", branch]
    proc = git(worktree, *cmd, check=False)
    if proc.returncode != 0:
        raise PaquetAutoError(
            "git push origin %s : %s" % (branch, _tail(_decode(proc.stderr))))
    return _decode(proc.stderr).strip()


def _gh(cwd: Path, *args: str, timeout: int = GIT_TIMEOUT
        ) -> subprocess.CompletedProcess:
    return run_command(["gh", *args], cwd=cwd, timeout=timeout)


def ensure_pull_request(worktree: Path, branch: str, target: str,
                        title: str, body: str) -> tuple[int | None, str]:
    """Ouvre ou met à jour la PR ; rend `(numéro, url)`."""
    view = _gh(worktree, "pr", "view", branch,
               "--json", "number,url,state")
    number: int | None = None
    url = ""
    if view.returncode == 0:
        try:
            data = json.loads(_decode(view.stdout))
            number, url = int(data["number"]), str(data.get("url") or "")
            state = str(data.get("state") or "").upper()
        except (ValueError, KeyError, TypeError) as exc:
            raise PaquetAutoError("gh pr view : réponse illisible (%s)" % exc) from exc
        if state and state != "OPEN":
            raise PaquetAutoError(
                "PR #%d pour %s est %s, pas ouverte : choisir un autre --name "
                "(un paquet fusionné ou fermé ne se réécrit pas)"
                % (number, branch, state))
    else:
        text = _output(view)
        if "no pull requests found" not in text.lower() \
                and "not found" not in text.lower():
            raise PaquetAutoError(
                "gh pr view %s a échoué : %s" % (branch, _tail(text)))
    with tempfile.NamedTemporaryFile("w", encoding="utf-8", suffix=".md",
                                     delete=False, dir=str(temporary_parent())) as handle:
        handle.write(body)
        body_path = Path(handle.name)
    try:
        if number is not None:
            proc = _gh(worktree, "pr", "edit", str(number), "--title", title,
                       "--body-file", str(body_path))
            if proc.returncode != 0:
                raise PaquetAutoError(
                    "gh pr edit #%d : %s" % (number, _tail(_output(proc))))
            return number, url
        proc = _gh(worktree, "pr", "create", "--base", target, "--head", branch,
                   "--title", title, "--body-file", str(body_path))
        if proc.returncode != 0:
            raise PaquetAutoError(
                "gh pr create : %s" % _tail(_output(proc)))
        url = _output(proc)
        if url.startswith("http"):
            view = _gh(worktree, "pr", "view", branch, "--json", "number")
            if view.returncode == 0:
                try:
                    number = int(json.loads(_decode(view.stdout))["number"])
                except (ValueError, KeyError, TypeError):
                    number = None
        return number, url
    finally:
        body_path.unlink(missing_ok=True)


def freeze_carrier(worktree: Path, carrier: int, head: str, note: str) -> str:
    cmd = ["ameesh", "work", "milestone", str(carrier), "frozen",
           "--sha", head, "--repo", str(worktree), "--note", note]
    proc = run_command(cmd, cwd=worktree, timeout=FREEZE_TIMEOUT)
    if proc.returncode != 0:
        raise PaquetAutoError(
            "gel du porteur #%d refusé : %s" % (carrier, _tail(_output(proc))))
    return _output(proc)


def request_full_ci(worktree: Path, carrier: int, head: str) -> str:
    """#4304 : la ci-full de la tête poussée, sans attendre les tests par unité.

    Jamais fatal : `ameesh ci run` est idempotent sur un run en vol de la même
    composition, et `paquet-horaire.py` (publish) la redemande après le retour.
    """
    cmd = ["ameesh", "ci", "run", str(carrier), "--full", "--sha", head,
           "--avis", "0", "--reason",
           "paquet-auto : tête poussée dès la composition (canon §6, #4304)"]
    proc = run_command(cmd, cwd=worktree, timeout=FREEZE_TIMEOUT)
    if proc.returncode != 0:
        return ("ci-full non demandée sur %s (rc %d, le publish de paquet-horaire "
                "la redemande) : %s" % (head[:12], proc.returncode, _tail(_output(proc))))
    return "ci-full demandée sur %s" % head[:12]


def note_carrier(worktree: Path, carrier: int, note: str) -> str:
    """Une note au journal du porteur ; une erreur est dite, jamais fatale.

    #7091 : le texte passe par l'entrée standard (`work note <lot> -`) ; une
    projection de contrats de plus de 128 Kio (MAX_ARG_STRLEN) faisait mourir
    l'appel en E2BIG, une OSError qui arrêtait toute la composition."""
    try:
        proc = run_command(["ameesh", "work", "note", str(carrier), "-"],
                           cwd=worktree, input_text=note, timeout=GIT_TIMEOUT)
    except (OSError, PaquetAutoError) as exc:
        return "note du porteur #%d non écrite : %s" % (carrier, exc)
    if proc.returncode != 0:
        return "note du porteur #%d non écrite : %s" % (carrier, _tail(_output(proc)))
    return "note du porteur #%d écrite" % carrier


def pull_request_state(worktree: Path, branch: str) -> str:
    """L'état de la PR de la branche (OPEN, MERGED, CLOSED), "" si illisible."""
    view = _gh(worktree, "pr", "view", branch, "--json", "state")
    if view.returncode != 0:
        return ""
    try:
        return str(json.loads(_decode(view.stdout)).get("state") or "").upper()
    except (ValueError, AttributeError):
        return ""


def withdraw_packet(worktree: Path, carrier: int, pr_number: int | None,
                    reason: str) -> list[str]:
    """#4304 : un paquet déjà poussé qui n'a plus d'unité : CI annulée, PR close.

    Rend les échecs (dits, jamais fatals) : le porteur est clos par l'appelant.
    """
    failed: list[str] = []
    proc = run_command(["ameesh", "ci", "cancel", str(carrier), "--all"],
                       cwd=worktree, timeout=GIT_TIMEOUT)
    if proc.returncode != 0:
        failed.append("ci cancel #%d : %s" % (carrier, _tail(_output(proc))))
    if pr_number:
        proc = _gh(worktree, "pr", "close", str(pr_number), "--comment", reason)
        if proc.returncode != 0:
            failed.append("gh pr close #%d : %s" % (pr_number, _tail(_output(proc))))
    return failed


def pull_request_merge_sha(worktree: Path, branch: str) -> str:
    """Le commit de fusion de la PR de la branche, "" si illisible."""
    view = _gh(worktree, "pr", "view", branch, "--json", "mergeCommit")
    if view.returncode != 0:
        return ""
    try:
        commit = json.loads(_decode(view.stdout)).get("mergeCommit") or {}
        return str(commit.get("oid") or "")
    except (ValueError, AttributeError):
        return ""


def watch_carrier(worktree: Path, branch: str) -> None:
    """#4463 : la PR poussée a-t-elle fusionné (ou été close) ? Lu avant chaque
    série ; un état illisible laisse les tests continuer."""
    state = pull_request_state(worktree, branch)
    if state and state != "OPEN":
        raise CarrierSettled(state)


def interrupted_by_carrier(args: argparse.Namespace, worktree: Path,
                           branch: str, head: str, state: str) -> int:
    """#4463 : la PR a fusionné (ou été close) pendant les tests par unité.

    Leurs résultats ne peuvent plus retirer une unité : les séries non parties
    sont abandonnées, le porteur le dit en une ligne. Fusionnée, la sortie finit
    sur `MERGED_LINE` (paquet-horaire ne publie pas) ; close, un agent lit.
    """
    if state == "MERGED":
        sha = pull_request_merge_sha(worktree, branch) or "?"
        print(note_carrier(
            worktree, args.carrier,
            "paquet %s : tests par unité interrompus : porteur fusionné à %s "
            "(tête %s) ; les résultats ne pouvaient plus retirer d'unité (#4463)"
            % (args.name, sha[:12], head[:12])), flush=True)
        print("%s : %s, tête %s, tests par unité interrompus (fusionné à %s)"
              % (MERGED_LINE, branch, head[:12], sha[:12]), flush=True)
        return 0
    print(note_carrier(
        worktree, args.carrier,
        "paquet %s : tests par unité interrompus : PR %s (tête %s) (#4463)"
        % (args.name, state.lower(), head[:12])), flush=True)
    raise PaquetAutoError(
        "PR de %s %s pendant les tests par unité : tests interrompus, rien "
        "n'est recomposé, un agent lit" % (branch, state.lower()))


def merged_during_tests(args: argparse.Namespace, worktree: Path, head: str,
                        runs: list[TestRun], faulty: list[int], branch: str,
                        rel: str) -> int:
    """#4322 : la PR a fusionné au vert pendant les tests par unité (§11 bis).

    Rien n'est recomposé ni retiré : les unités sont sur la cible. Le résultat
    va au journal du porteur ; un rouge part au pilote (corriger en avant ou
    revert). La dernière ligne (`MERGED_LINE`) dit à `paquet-horaire.py` de ne
    pas publier sur un porteur déjà fusionné.
    """
    red = ", ".join("#%d" % lot for lot in faulty)
    summary = ("rouges pour %s (%s)" % (red, _tests_summary(runs)) if faulty
               else "verts (%s)" % _tests_summary(runs))
    print(note_carrier(
        worktree, args.carrier,
        "paquet %s : PR fusionnée au vert pendant les tests par unité ; tests "
        "par unité %s sur la tête %s ; manifeste %s (#4322)"
        % (args.name, summary, head[:12], rel)), flush=True)
    if faulty:
        body = ("Paquet %s (%s, tête %s) : la ci-full verte a fusionné la PR "
                "pendant les tests par unité, puis ceux-ci ont rougi pour %s "
                "(%s). Rien n'est retiré ni recomposé : les unités sont sur la "
                "cible. Rouge après fusion : corriger en avant ou revert "
                "(canon §11 bis) ; un rouge local seul reste non bloquant.\n"
                % (args.name, branch, head[:12], red, _tests_summary(runs)))
        try:
            send_mail(args.pilot, args.carrier, body, getattr(args, "sender", None))
        except PaquetAutoError as error:
            print("courrier non envoyé : %s\n\n%s" % (error, body),
                  file=sys.stderr, flush=True)
    print("%s : %s, tête %s, tests par unité %s" % (
        MERGED_LINE, branch, head[:12], summary), flush=True)
    return 0


def send_mail(recipient: str, lot: int, body: str,
              sender: str | None = None) -> str:
    cmd = ["agent-mail", "send", recipient, "-", "--lot", str(lot)]
    if sender:
        # #3845 : sous `ameesh role run`, AGENT_MAIL_NAME est retiré ; sans
        # --from, agent-mail refuse (« unbound sender »).
        cmd += ["--from", sender]
    proc = run_command(cmd, input_text=body, timeout=120)
    if proc.returncode != 0:
        raise PaquetAutoError(
            "courrier à %s : %s" % (recipient, _tail(_output(proc))))
    return _output(proc)


def lot_author(lot: int, repo: Path) -> str:
    """L'assigné du lot, ou "" si la base ne le dit pas (jamais bloquant)."""
    try:
        proc = run_command(["ameesh", "work", "show", str(lot), "--json"],
                           cwd=repo, timeout=GIT_TIMEOUT)
    except PaquetAutoError:
        return ""
    if proc.returncode != 0:
        return ""
    try:
        item = json.loads(_decode(proc.stdout))
    except ValueError:
        return ""
    return str(item.get("assignee") or "").strip()


def unit_red_bound_label() -> str:
    """#5989 : la borne de `packet_unit_red`, lue à sa source, jamais recopiée."""
    sys.path.insert(0, str(Path(__file__).resolve().parents[1] / 'src'))
    from ameesh import packet_unit_red
    hours = packet_unit_red.EXCLUSION_S / 3600
    return ('%d h' % hours) if hours == int(hours) else ('%g h' % hours)


def remember_unit_red(repo: Path, lot: int, source: str, target: str,
                      tests: list[str], sender: str | None, *, base=None) -> bool:
    """#5971 : note durable par base avant recomposition ; False si déjà signalé.

    Ne jamais réarmer la borne d'une tête expirée : la première note reste
    l'origine de l'exclusion sur cette base, même si un nouvel essai rougit encore.
    Une nouvelle base a sa propre borne, sans deuxième courrier par source/cible.
    """
    sys.path.insert(0, str(Path(__file__).resolve().parents[1] / 'src'))
    from ameesh import config, db, packet_unit_red
    connection = None
    try:
        connection = db.connect(config.load())
        return packet_unit_red.remember(connection, lot, source, target, tests,
                                        actor=sender or '', base=base)
    except Exception as exc:
        raise PaquetAutoError('mémoire du rouge #%d non enregistrée : %s' % (lot, exc)) from exc
    finally:
        if connection is not None:
            connection.close()


def notify_removals(repo: Path, name: str, removals: list[Removal], *,
                    sender: str | None, log=print, sources=None, target='', base=None) -> list[str]:
    """Prévient l'auteur de chaque unité retirée (§11 bis, #3931).

    Un courrier qui ne part pas n'annule pas la recomposition : il est
    consigné, et le courrier du pilote porte les lots retirés. Rend les
    échecs, pour la trace du tic. Une mémoire impossible à écrire arrête en
    revanche la tentative : aucun retrait réputé durable sans sa note.
    """
    failed: list[str] = []
    for removal in removals:
        if sources is not None:
            try:
                if not remember_unit_red(repo, removal.lot, sources[removal.lot],
                                         target, removal.tests, sender, base=base):
                    continue
            except (PaquetAutoError, ValueError, KeyError) as exc:
                raise PaquetAutoError('retrait #%d non mémorisé : %s'
                                      % (removal.lot, exc)) from exc
        author = lot_author(removal.lot, repo)
        if not author:
            log("auteur de #%d inconnu : courrier non envoyé" % removal.lot)
            continue
        tests = ", ".join(removal.tests[:3]) or "tests ciblés"
        if removal.proof == "prefixes":
            why = ("les tests ciblés de la composition ont rougi sur la tête "
                   "composée, et le test rouge apparaît pour la première fois "
                   "au préfixe de ton unité (%s)"
                   % (" ; ".join(removal.evidence[:3])
                      or "préfixes de l'empilement"))
        elif removal.attributed:
            why = ("les tests ciblés de la composition ont rougi sur la tête "
                   "composée, et la lecture #3801 désigne ton unité (%s)"
                   % (" ; ".join(removal.evidence[:3]) or "attribution #3801"))
        else:
            why = ("les tests ciblés de la composition ont rougi sur la tête "
                   "composée ; le module propre de ton unité est rouge "
                   "(retrait de repli, sans attribution #3801 sûre)")
        body = (
            "#%d retiré du paquet %s : %s. Tests : %s. Le paquet est recomposé "
            "sans ton lot ; corrige puis regèle, il reviendra à une coupure "
            "suivante."
            % (removal.lot, name, why, tests))
        if sources is not None:
            body += (' Source : %s. Mémoire sur le lot : cette source est exclue '
                     'pendant au plus %s depuis le premier retrait ; '
                     'une nouvelle source est réadmise immédiatement.'
                     % (sources[removal.lot], unit_red_bound_label()))
        try:
            send_mail(author, removal.lot, body, sender)
        except PaquetAutoError as exc:
            failed.append("#%d : %s" % (removal.lot, exc))
            log("courrier à %s (#%d) non envoyé : %s"
                % (author, removal.lot, exc))
        else:
            log("courrier à %s (#%d) envoyé" % (author, removal.lot))
    return failed


def notify_unattributable(name: str, carrier: int, error,
                          *, pilot: str = "mesh-design",
                          sender: str | None = None, log=print) -> str:
    """#4217 : aucun retrait automatique ; le pilote reçoit le rouge situé.

    Un courrier qui ne part pas n'efface pas l'arrêt : il est consigné et le
    gestionnaire de l'horaire prévient aussi le pilote. Rend le motif d'échec.
    """
    tests = ", ".join(error.tests[:5]) or "test(s) non nommé(s)"
    body = (
        "#%d paquet %s : un rouge de voisin n'a pas pu être situé sur les "
        "préfixes de l'empilement (%s). Tests : %s. Aucun retrait automatique ; "
        "un agent lit le journal du porteur."
        % (carrier, name, error.why, tests))
    try:
        send_mail(pilot, carrier, body, sender)
    except PaquetAutoError as exc:
        log("courrier au pilote non envoyé : %s" % exc)
        return str(exc)
    log("courrier au pilote envoyé (%s)" % pilot)
    return ""


def notify_flaky(name: str, carrier: int, tests: list[str],
                 *, pilot: str = "mesh-design",
                 sender: str | None = None, log=print) -> str:
    """#4221 : un test instable est consigné non concluant, le paquet continue.

    Le canon §3 veut qu'un instable ne retienne ni paquet ni release ; le
    pilote le sait pour le corriger à part. Un courrier qui ne part pas est
    consigné, sans arrêter la composition. Rend le motif d'échec.
    """
    listed = ", ".join(tests[:5]) or "test(s) non nommé(s)"
    body = (
        "#%d paquet %s : test(s) rouge(s) sur la tête mais vert(s) sur tous "
        "les préfixes de l'empilement (%s) : instable(s), consigné(s) non "
        "concluant(s) (canon §3) ; aucun retrait pour eux, le paquet "
        "continue. À corriger à part." % (carrier, name, listed))
    try:
        send_mail(pilot, carrier, body, sender)
    except PaquetAutoError as exc:
        log("courrier d'instabilité au pilote non envoyé : %s" % exc)
        return str(exc)
    log("courrier d'instabilité au pilote envoyé (%s)" % pilot)
    return ""


def pr_body(*, name: str, target: str, base_sha: str, carrier: int,
            composition: Composition, candidates: list[Candidate],
            tests: list[TestRun] | None, inclusion_rc: int,
            freeze_seconds: float) -> str:
    """Le corps de la PR ; `tests=None` à la poussée, avant les tests (#4304)."""
    by_lot = {candidate.lot: candidate for candidate in candidates}
    units = ", ".join("#%d" % unit.lot for unit in composition.units) or "aucun"
    discarded = ", ".join("#%d" % item.lot for item in composition.discarded) or "aucun"
    lines = [
        "ameesh-work: %d" % carrier,
        "",
        "Paquet horaire %s (§12), base `%s` `%s`, **%d unité(s)**."
        % (name, target, base_sha[:12], len(composition.units)),
        "",
        "- Unités : %s." % units,
        "- Écartés : %s." % discarded,
    ]
    for item in composition.discarded:
        detail = (" — " + _one_line(item.detail)) if item.detail else ""
        lines.append("  - #%d : %s%s." % (item.lot, item.reason, detail))
    for unit in composition.units:
        candidate = by_lot.get(unit.lot)
        title = _one_line(candidate.title if candidate else "")[:160]
        lines.append("- #%d `%s`%s" % (
            unit.lot, unit.source[:12], (": " + title) if title else ""))
    if tests is None:
        lines.append("- Tests par unité : en cours, en parallèle de la CI "
                     "complète (#4304).")
    else:
        lines.append("- Tests locaux : %s." % _tests_summary(tests))
        lines += ["  " + line for line in _inconclusive_lines(tests)]
        lines += ["  " + line for line in _unattributed_lines(tests)]
        lines += _direct_lines(composition, tests)
    lines += [
        "- Contrôle d'inclusion : %s." % ("vert" if inclusion_rc == 0 else "à lire"),
        "- Freeze-checks : vert (%.1f s)." % freeze_seconds,
        "- Manifeste : `%s`." % manifest_rel(name),
        "",
        "Porteur : #%d. CI complète demandée dès la poussée (#4304) ; "
        "« verdict non requis » reste au pilote." % carrier,
        "",
    ]
    return "\n".join(lines)


def mail_body(*, name: str, target: str, base_sha: str, carrier: int,
              head: str, branch: str, composition: Composition,
              tests: list[TestRun], inclusion_rc: int, pr_number: int | None,
              pr_url: str) -> str:
    units = ", ".join("#%d" % unit.lot for unit in composition.units) or "aucun"
    discarded = "; ".join(
        "#%d (%s)" % (item.lot, item.reason) for item in composition.discarded
    ) or "aucun"
    pr = ("PR #%d %s" % (pr_number, pr_url)) if pr_number else (pr_url or "PR non lue")
    inconclusive = _inconclusive_lines(tests)
    non_section = ("Non concluants (canon §3, consignés) :\n"
                   + "\n".join("  " + line for line in inconclusive)) \
        if inconclusive else "Non concluants : aucun."
    unattributed = _unattributed_lines(tests)
    if unattributed:
        non_section += ("\nRouges de voisins non attribués (#4887) :\n"
                        + "\n".join("  " + line for line in unattributed))
    return """\
Paquet {name} composé sans agent : tête `{head}` sur {target} `{base}` (branche `{branch}`, {pr}).

- Unités ({n}) : {units}.
- Écartés ({d}) : {discarded}.
- Tests locaux : {tests}.
- {direct_note}
- {non_section}
- Inclusion : {inclusion}.
- Gel : `ameesh work milestone` posé sur la tête dès la poussée, ci-full demandée (#4304) ; manifeste `{manifest}`.

« Verdict non requis » reste ton geste ; aucune fusion sur {target}.
""".format(
        name=name, head=head, target=target, base=base_sha[:12],
        branch=branch, pr=pr, n=len(composition.units), units=units,
        d=len(composition.discarded), discarded=discarded,
        tests=_tests_summary(tests), direct_note=_direct_note(composition, tests),
        non_section=non_section,
        inclusion="vert" if inclusion_rc == 0 else "à lire",
        manifest=manifest_rel(name),
    )


# ---------------------------------------------------------------------------
# Rejeu (témoin)
# ---------------------------------------------------------------------------


def parse_composition_table(text: str) -> list[tuple[list[int], str]]:
    """[(lots, source)] de la table `## Composition` d'un manifeste.

    Seule la forme du gabarit #3629 est lue (colonnes `lot(s)`, `source`,
    `commit`) ; toute autre table est ignorée.
    """
    lines = text.splitlines()
    start = None
    for index, line in enumerate(lines):
        if line.strip().startswith("## Composition"):
            start = index
            break
    if start is None:
        raise InputError("manifeste de rejeu : aucune section `## Composition`")
    header = None
    for index in range(start, len(lines)):
        if lines[index].strip().startswith("|"):
            header = [cell.strip().lower() for cell in lines[index].strip().strip("|").split("|")]
            if any("lot" in cell for cell in header) \
                    and any("source" in cell for cell in header):
                start = index + 2
                break
            header = None
    if header is None:
        raise InputError("manifeste de rejeu : table `## Composition` introuvable")
    rows: list[tuple[list[int], str]] = []
    for line in lines[start:]:
        if not line.strip().startswith("|"):
            break
        cells = [cell.strip() for cell in line.strip().strip("|").split("|")]
        if len(cells) < 3:
            continue
        lots = [int(found) for found in re.findall(r"#(\d+)", cells[1])]
        source_match = SHA_RE.search(cells[2])
        if not lots or not source_match:
            raise InputError("manifeste de rejeu : ligne de composition illisible : %s"
                             % line)
        rows.append((lots, source_match.group(0)))
    if not rows:
        raise InputError("manifeste de rejeu : table de composition vide")
    return rows


def _legacy_error_discards(payload: dict) -> list[Discarded]:
    """Les `errors` du format antérieur à #3813, quand ils nomment un lot."""
    out: list[Discarded] = []
    for text in payload.get("errors") or []:
        match = re.match(r"^\s*[#!](\d+)\s*:?\s*(.*)$", str(text))
        if not match:
            raise PaquetAutoError(
                "--candidates-json : erreur globale, la liste n'est pas "
                "utilisable : %s" % text)
        out.append(Discarded(
            int(match.group(1)), "erreur paquet-candidats, jamais composé",
            " ".join(match.group(2).split()) or str(text)))
    return out


def check_replay_admission(specs: list[tuple[list[int], str]], payload: dict,
                           base_sha: str, repo: Path) -> None:
    """#3813 : aucune unité du manifeste n'est en erreur ou hors des prêts."""
    stated = str(payload.get("base") or "").strip()
    if stated:
        resolved = rev(repo, stated)
        if resolved and resolved != base_sha:
            raise InputError(
                "--candidates-json : base %s, rejeu sur %s"
                % (resolved[:12], base_sha[:12]))
        if not resolved and not (stated.startswith(base_sha)
                                 or base_sha.startswith(stated)):
            raise InputError(
                "--candidates-json : base %r, rejeu sur %s"
                % (stated, base_sha[:12]))
    errors = _errors_by_lot(payload)
    if not errors:
        errors = _legacy_error_discards(payload)
    by_lot = {item.lot: item for item in errors}
    ready = {int(lot) for lot in payload.get("ready") or []}
    for lots, _source in specs:
        for lot in lots:
            if lot in by_lot:
                raise PaquetAutoError(
                    "rejeu refusé : #%d est en erreur dans paquet-candidats "
                    "(jamais composé) : %s"
                    % (lot, by_lot[lot].detail or by_lot[lot].reason))
            if lot not in ready:
                raise PaquetAutoError(
                    "rejeu refusé : #%d n'est pas dans la liste des lots prêts "
                    "de --candidates-json" % lot)


def replay(repo: Path, reference: str, name: str, base: str, *,
           compose_script: Path | None = None, keep: bool = False,
           candidates_json: Path | None = None, log=print) -> int:
    """Rejoue un manifeste gelé et compare l'arbre obtenu à celui de `reference`."""
    ref = rev(repo, reference)
    if ref is None:
        raise InputError("référence %r introuvable" % reference)
    manifest_path = manifest_rel(name)
    show = git(repo, "show", "%s:%s" % (ref, manifest_path), check=False)
    if show.returncode != 0:
        raise InputError(
            "manifeste %s introuvable dans %s : %s"
            % (manifest_path, ref[:12], _decode(show.stderr).strip()))
    manifest_text = _decode(show.stdout)
    specs = parse_composition_table(manifest_text)
    base_sha = rev(repo, base)
    if base_sha is None:
        raise InputError("base %r introuvable" % base)
    if candidates_json is not None:
        try:
            payload = json.loads(Path(candidates_json).read_text(encoding="utf-8"))
        except (OSError, ValueError) as exc:
            raise InputError(
                "--candidates-json %s illisible : %s" % (candidates_json, exc)) from exc
        if not isinstance(payload, dict):
            raise InputError("--candidates-json : un objet JSON attendu")
        check_replay_admission(specs, payload, base_sha, repo)
    branch = "tmp/paquet-auto-rejeu-%d" % os.getpid()
    worktree = make_worktree(repo, base_sha, branch)
    try:
        script = compose_script or (worktree / COMPOSE_SCRIPT)
        if not Path(script).is_file():
            raise PaquetAutoError("%s introuvable pour le rejeu" % script)
        for lots, source in specs:
            candidate = Candidate(lot=lots[0], source=source)
            ensure_source(worktree, candidate)
            cmd = [project_python(worktree), str(script), "--repo", str(worktree),
                   "--source", candidate.source]
            for lot in lots:
                cmd += ["--lot", str(lot)]
            proc = run_command(cmd, cwd=worktree, timeout=DEFAULT_TIMEOUT,
                               env=python_env(worktree))
            if proc.returncode != 0:
                raise PaquetAutoError(
                    "rejeu : composition de %s refusée : %s"
                    % ("/".join("#%d" % lot for lot in lots), _tail(_output(proc))))
            log("rejeu : %s ← %s" % ("/".join("#%d" % lot for lot in lots),
                                     candidate.source[:12]))
        path = worktree / manifest_path
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text(manifest_text, encoding="utf-8")
        git(worktree, "add", "--", manifest_path)
        proc = git(worktree, "commit", "-m",
                   "paquet %s : manifeste de rejeu (%s)" % (name, ref[:12]),
                   check=False)
        if proc.returncode != 0:
            raise PaquetAutoError(
                "rejeu : commit du manifeste : %s" % _decode(proc.stderr).strip())
        head = rev(worktree, "HEAD")
        got = tree_of(worktree, "HEAD")
        expected = tree_of(repo, ref)
        if got is None or expected is None:
            raise PaquetAutoError("rejeu : arbre illisible")
        if got != expected:
            diff = git(worktree, "diff", "--stat", ref, "HEAD", check=False)
            raise PaquetAutoError(
                "rejeu : arbre différent de %s\nattendu %s\nobtenu  %s\n%s"
                % (ref[:12], expected, got, _decode(diff.stdout).strip()))
        log("rejeu conforme : tête %s, arbre %s = %s" % (head[:12], got, ref[:12]))
        return 0
    finally:
        if keep:
            log("worktree de rejeu conservé : %s" % worktree)
        else:
            remove_worktree(repo, worktree)
            git(repo, "branch", "-D", branch, check=False)


# ---------------------------------------------------------------------------
# Flux normal
# ---------------------------------------------------------------------------


def _write_id_file(path: Path, lots) -> None:
    path.write_text("\n".join("#%d" % lot for lot in lots) + ("\n" if lots else ""),
                    encoding="utf-8")


def keep_tested(repo: Path, carrier: int, name: str, base_sha: str, head: str,
                *, tests: str = "", sender: str | None = None) -> str:
    """#4237 : la tête testée survit au refus de geste.

    Poussée sous une réf réservée (lisible depuis l'hôte du détenteur courant)
    et dite au journal du porteur, avec le résumé des tests par unité (le
    manifeste poussé avant eux ne les porte pas, #4304) ; le tic du détenteur
    finit la publication par `--publish` sans retester. Rien ici ne lève : le
    refus d'origine reste l'erreur rendue.
    """
    ref = TESTED_REF_PREFIX + name
    proc = git(repo, "push", "origin", "+%s:%s" % (head, ref),
               check=False)
    if proc.returncode != 0:
        print("tête testée %s non poussée (%s) : le détenteur recompose"
              % (head[:12], _tail(_decode(proc.stderr), 5)), file=sys.stderr)
        ref = "-"
    else:
        print("tête testée %s gardée sous %s" % (head[:12], ref), flush=True)
    mark = "%sbase=%s tête=%s réf=%s" % (TESTED_MARK, base_sha, head, ref)
    if tests:
        mark += " tests=%s" % tests
    # Le tic peut réécrire la marque si cette première note échoue.
    print(mark, flush=True)
    cmd = ["ameesh", "work", "note", str(carrier), mark]
    if sender:
        cmd += ["--actor", sender]
    try:
        noted = run_command(cmd, cwd=repo, timeout=GIT_TIMEOUT)
    except PaquetAutoError as error:
        print("marque de tête testée non écrite : %s" % error, file=sys.stderr)
        return ref
    if noted.returncode != 0:
        print("marque de tête testée non écrite : %s" % _tail(_output(noted), 5),
              file=sys.stderr)
    return ref


def check_chain(repo: Path, head: str, base_sha: str, rel: str,
                candidates: list[Candidate]) -> list[tuple[list[int], str]]:
    """#4237 : la tête testée est-elle publiable telle quelle sur `base_sha` ?

    Le manifeste seul au-dessus d'une chaîne de fusions (premier parent) qui
    part de la tête fraîche de la cible ; une fusion par ligne de la table
    `## Composition`, dans l'ordre, son second parent égal à la source de la
    ligne et à la source de chacun de ses lots dans la liste calculée (la tête
    gelée enregistrée, ou sa remise), et son **arbre** égal à la fusion
    déterministe de ses deux parents (`git merge-tree --write-tree`, la même
    stratégie que `paquet-compose`) : un contenu glissé dans une fusion aux
    parents conformes est refusé. Rend la table ; sinon `ChainRefused`.
    """
    parents = out(repo, "rev-list", "--parents", "-n", "1", head).split()[1:]
    files = out(repo, "diff-tree", "--no-commit-id", "--name-only", "-r",
                head).splitlines()
    if len(parents) != 1 or files != [rel]:
        raise ChainRefused("la tête %s n'est pas le seul commit du manifeste %s"
                           % (head[:12], rel))
    try:
        specs = parse_composition_table(out(repo, "show", "%s:%s" % (head, rel)))
    except InputError as error:
        raise ChainRefused(str(error)) from error
    lines = out(repo, "rev-list", "--first-parent", "--parents", "--reverse",
                "%s..%s" % (base_sha, parents[0]), check=False).splitlines()
    merges = [line.split() for line in lines]
    if not merges or len(merges[0]) < 2 or merges[0][1] != base_sha:
        raise ChainRefused("tête %s composée sur une autre base que la tête "
                           "de la cible %s" % (head[:12], base_sha[:12]))
    if len(merges) != len(specs) or any(len(merge) != 3 for merge in merges):
        raise ChainRefused("%d commit(s) sous le manifeste pour %d unité(s) : "
                           "une fusion à deux parents par unité attendue"
                           % (len(merges), len(specs)))
    by_lot = {candidate.lot: candidate for candidate in candidates}
    for (lots, source), merge in zip(specs, merges):
        second = merge[2]
        merged = git(repo, "merge-tree", "--write-tree", merge[1], second, check=False)
        tree = _decode(merged.stdout).split("\n", 1)[0].strip()
        if merged.returncode != 0 or tree != tree_of(repo, merge[0]):
            raise ChainRefused("fusion %s : arbre différent de la fusion propre de "
                               "ses parents (%s)" % (merge[0][:12],
                                                    "conflit" if merged.returncode
                                                    else (tree[:12] or "?")))
        if not second.startswith(source.lower()):
            raise ChainRefused("fusion %s : second parent %s, manifeste %s"
                               % (merge[0][:12], second[:12], source[:12]))
        for lot in lots:
            candidate = by_lot.get(lot)
            if candidate is None:
                raise ChainRefused("#%d n'est plus prêt sur %s" % (lot, base_sha[:12]))
            if (rev(repo, candidate.source) or candidate.source) != second:
                raise ChainRefused("#%d : source enregistrée %s, composée %s"
                                   % (lot, candidate.source[:12], second[:12]))
    return specs


def publish_tested(args: argparse.Namespace) -> int:
    """#4237 : publier une tête testée gardée, sans retester."""
    repo = Path(args.repo).resolve()
    validate_name(args.name)
    validate_target(repo, args.target)
    if args.carrier <= 0:
        raise InputError("--carrier : un identifiant de lot positif attendu")
    branch = package_branch(args.name)
    validate_branch(repo, branch, "--name")
    rel = manifest_rel(args.name)
    # #6563 : la publication d'une tête testée écrit la branche du porteur et
    # demande sa ci-full ; elle prend le même bail de composition du porteur
    # qu'une composition, pour qu'un second composeur ne travaille pas dessus.
    claim = CompositionClaim(repo, args.carrier, args.name).start()
    # un refus avant la publication (garde, tête, chaîne) ne doit pas
    # laisser le bail du porteur tenu jusqu'à la fin du processus.
    try:
        def guard() -> None:
            claim.require()
            check_epoch(args.carrier, repo)

        guard()
        fetched = git(repo, "fetch", "--no-tags", "origin", args.publish, check=False,
                      timeout=GIT_TIMEOUT)
        head = rev(repo, "FETCH_HEAD") if fetched.returncode == 0 else rev(repo, args.publish)
        if not head:
            raise ChainRefused("tête testée %s introuvable (ni sur origin ni ici)"
                               % args.publish)
        # la réf est mobile : seule la tête attestée par la marque est publiée
        if head != args.publish_sha:
            raise ChainRefused("%s désigne %s, la marque de tests atteste %s"
                               % (args.publish, head[:12], args.publish_sha[:12]))
        base_sha = fresh_base(repo, args.target)
        print("paquet %s : publication de la tête testée %s sur %s %s"
              % (args.name, head, args.target, base_sha), flush=True)
        # #4304 : la tête est poussée, enregistrée et gelée avant ses tests ; seule
        # la fin reste due, sur la branche du paquet qui la désigne toujours
        remote_sha = remote_branch(repo, branch)
        if remote_sha:
            git(repo, "fetch", "--no-tags", "origin", branch, check=False)
            remote_sha = rev(repo, "origin/" + branch) or remote_sha
        if remote_sha != head:
            raise ChainRefused("%s sur origin désigne %s, pas la tête testée %s"
                               % (branch, (remote_sha or "rien")[:12], head[:12]))
        worktree = Path(tempfile.mkdtemp(prefix="paquet-auto-publie-",
                                         dir=str(temporary_parent())))
        worktree.rmdir()
        reader = Path(tempfile.mkdtemp(prefix="paquet-auto-base-",
                                       dir=str(temporary_parent())))
        reader.rmdir()
        tmp = tempfile.TemporaryDirectory(prefix="paquet-auto-", dir=str(temporary_parent()))
        success = False
    except BaseException:
        claim.stop()
        raise
    try:
        git(repo, "worktree", "add", "--detach", str(worktree), head)
        git(repo, "worktree", "add", "--detach", str(reader), base_sha)
        ready_out = Path(tmp.name) / "prets.txt"
        # la liste calculée par le code de la cible, à sa tête (comme à la composition)
        candidates, _payload = compute_candidates(reader, args.target, base_sha, ready_out)
        specs = check_chain(worktree, head, base_sha, rel, candidates)
        print("chaîne vérifiée : %d unité(s) sur %s" % (len(specs), base_sha[:12]),
              flush=True)
        units = "\n".join("- %s `%s`" % (", ".join("#%d" % lot for lot in row_lots),
                                         source[:12]) for row_lots, source in specs)
        contract_note = "Repli legacy : aucun résultat de contrats réutilisé."
        if publication_policy(args) != "legacy":
            report = prepublication_contracts(
                worktree, base=base_sha, budget=contract_budget(args), guard=guard,
                expected_head=head, parallel=getattr(args, "tests_parallel", DEFAULT_PARALLEL),
                carrier=args.carrier, project=getattr(args, "pause_project", "") or "")
            record_contract_result(worktree, args.carrier, report, base=base_sha, target=args.target)
            from ameesh.packet_contract_proof import projection
            contract_state = projection(report, base=base_sha, target=args.target)["result"]["state"]
            contract_note = "Contrats de reprise : " + contract_state
            if contract_state == "red":
                # Returning PUBLISH_REFUSED enters the scheduler's existing
                # recomposition path, where contract_red attributes/removes.
                raise ChainRefused("contrats rouges sur la tête reprise : recomposer pour attribution")
        origin = ("Tête `%s` poussée et gelée par un détenteur précédent du bail, "
                  "fin publiée par reprise (#4237), tests par unité non rejoués : "
                  "leur qualification reste celle du journal d'origine du porteur #%d "
                  "et n'est pas déduite de cette reprise." % (head[:12], args.carrier)) + " " + contract_note
        guard()
        # #6579 : les contrats de reprise peuvent durer ; relire les gels
        # sur la base exacte avant tout geste de publication de la tête testée.
        fresh, _ = compute_candidates(
            reader, args.target, base_sha, Path(tmp.name) / "prets-relus.txt")
        changes = changed_units(
            [Unit(lot, rev(worktree, source) or source, "")
             for lots, source in specs for lot in lots],
            {candidate.lot: candidate.source for candidate in fresh})
        if changes:
            raise ChainRefused(
                "gel changé avant publication : %s ; recomposer"
                % "; ".join("#%d : %s" % (item.lot, item.reason)
                            for item in changes))
        state = pull_request_state(worktree, branch)
        pr_number: int | None = None
        pr_url = ""
        if state and state != "OPEN":
            print("PR %s : corps non mis à jour" % state.lower(), flush=True)
        else:
            body = "\n".join([
                "ameesh-work: %d" % args.carrier, "",
                "Paquet horaire %s (§12), base `%s` `%s`, **%d unité(s)**."
                % (args.name, args.target, base_sha[:12], len(specs)), "",
                units, "", origin, "",
                "- Contrôle d'inclusion : vert.",
                "- Manifeste : `%s`." % rel, "",
                "Porteur : #%d. « Verdict non requis » et CI complète restent au pilote."
                % args.carrier, ""])
            pr_number, pr_url = ensure_pull_request(
                worktree, branch, args.target,
                "Paquet %s — %d unités sur %s %s" % (
                    args.name, len(specs), args.target, base_sha[:12]), body)
        print(note_carrier(
            worktree, args.carrier,
            "paquet %s : fin publiée par scripts/paquet-auto.py --publish (#4237) "
            "sur la tête poussée %s, tests par unité non rejoués (qualification au journal d'origine) ; "
            "manifeste %s" % (args.name, head[:12], rel)), flush=True)
        check_pause(worktree, getattr(args, "pause_project", "") or "", args.target)
        print(request_full_ci(worktree, args.carrier, head), flush=True)
        pr = ("PR #%d %s" % (pr_number, pr_url)) if pr_number else (pr_url or "PR non lue")
        mail = "\n".join([
            "Paquet %s publié sans retester : tête `%s` sur %s `%s` (branche `%s`, %s)."
            % (args.name, head, args.target, base_sha[:12], branch, pr), "",
            units, "", origin, "",
            "« Verdict non requis » et la ci-full restent tes gestes ; aucune "
            "fusion sur %s." % args.target, ""])
        try:
            send_mail(args.pilot, args.carrier, mail, getattr(args, "sender", None))
        except PaquetAutoError as error:
            print("courrier non envoyé : %s\n\n%s" % (error, mail), file=sys.stderr)
            raise
        print("courrier envoyé à %s" % args.pilot, flush=True)
        if args.publish.startswith(TESTED_REF_PREFIX):
            git(repo, "push", "origin", ":" + args.publish, check=False)
        success = True
        return 0
    finally:
        remove_worktree(repo, reader)
        shutil.rmtree(reader, ignore_errors=True)
        tmp.cleanup()
        if success and not args.keep_worktree:
            remove_worktree(repo, worktree)
        elif not success:
            print("worktree conservé pour diagnostic : %s" % worktree, file=sys.stderr)
        claim.stop()


def compose_packet(args: argparse.Namespace) -> int:
    repo = Path(args.repo).resolve()
    validate_name(args.name)
    validate_target(repo, args.target)
    if args.carrier <= 0:
        raise InputError("--carrier : un identifiant de lot positif attendu")
    check_exclude_reasons(list(getattr(args, "exclude", None) or []),
                          list(getattr(args, "exclude_reason", None) or []))
    branch = package_branch(args.name)
    validate_branch(repo, branch, "--name")
    rel = manifest_rel(args.name)
    # #6563 : le bail partagé du porteur vérifie, avant chaque geste, que
    # cette composition est toujours la seule sur #carrier ; l'époque du bail
    # du rôle (#4113) reste vérifiée par `check_epoch` pour les gestes base.
    claim: CompositionClaim | None = None
    base_guard = (lambda: check_epoch(args.carrier, repo)) \
        if not args.no_packet_record else None

    def guard() -> None:
        if claim is not None:
            claim.require()
        if base_guard is not None:
            base_guard()

    if base_guard is not None:
        base_guard()
    # #4329 : le verrou par nom de paquet est pris avant toute lecture d'état
    # et toute suppression de worktree. Une composition vivante du même paquet
    # le tient : on refuse au lieu de prendre son worktree pour celui d'une
    # tentative précédente ; aucun geste (porteur, branche) n'est fait avant.
    lock = PackageLock(repo, args.name)
    lock.acquire()
    try:
        # #6563 : le bail partagé du porteur est pris avant toute lecture ou
        # écriture de la branche. Il couvre la composition réelle (pas les
        # modes de contrôle) et il est renouvelé par l'enfant détenteur ; un
        # second composeur — le tic, un agent désigné — est refusé ici (rc 3).
        if not args.no_packet_record and not args.local:
            claim = CompositionClaim(repo, args.carrier, args.name).start()
        base_sha = fresh_base(repo, args.target)
        print("paquet %s : base %s %s"
              % (args.name, args.target, base_sha), flush=True)
        remote_sha: str | None = None
        if not args.no_packet_record:
            remote_sha = remote_branch(repo, branch)
            if remote_sha and not args.force:
                raise InputError(
                    "%s existe déjà sur origin (%s) : un paquet n'est pas réécrit "
                    "sans --force (recomposition) ; choisir un autre --name ou "
                    "inspecter la PR en cours" % (branch, remote_sha[:12]))
            if remote_sha:
                git(repo, "fetch", "--no-tags", "origin", branch, check=False)
                remote_sha = rev(repo, "origin/" + branch) or remote_sha
            ensure_carrier_branch(repo, args.carrier, branch, args.target)
        if args.force:
            clear_local_branch(repo, branch, log=lambda line: print(line, flush=True))
        stale = prune_stale_worktrees(repo, args.keep_failed_hours)
        for path in stale:
            print("worktree d'échec purgé : %s" % path, flush=True)
        worktree = make_worktree(repo, base_sha, branch)
        # #6566 : worktree de lecture à la base, créé à la première relecture
        # du gel avant poussée (le worktree composé porte le code des unités).
        reader: Path | None = None
        keep_worktree = bool(args.keep_worktree)
        success = False
        empty = False
        tmp = tempfile.TemporaryDirectory(prefix="paquet-auto-",
                                          dir=str(temporary_parent()))
    except BaseException:
        if claim is not None:
            claim.stop()
        lock.release()
        raise
    try:
        ready_out = Path(tmp.name) / "prets.txt"
        if guard is not None:
            guard()
        candidates, payload = compute_candidates(worktree, args.target,
                                                 base_sha, ready_out)
        listed = list(candidates)
        errors = error_discards(payload)
        error_lots = {item.lot for item in errors}
        # #3845/#4201 : un lot retiré sur un rouge de ci-full (paquet-horaire.py,
        # unité nommée par #3801) ou par le pilote n'est pas recomposé ; il reste
        # au manifeste, avec le vrai motif quand `--exclude-reason` le donne.
        requested = list(getattr(args, "exclude", None) or [])
        excluded_discards = exclude_discards(
            requested, list(getattr(args, "exclude_reason", None) or []),
            error_lots)
        excluded = [item.lot for item in excluded_discards]
        if excluded:
            candidates = [candidate for candidate in candidates
                          if candidate.lot not in set(excluded)]
            errors = errors + excluded_discards
            error_lots |= set(excluded)
        # #3256 C : deux paquets parallèles partent de la même liste prête ;
        # `--only` borne celui-ci à son groupe. Tout lot prêt hors du groupe
        # est écrit « écarté » dans ce manifeste (motif vrai), jamais composé
        # dans les deux paquets.
        only = {int(lot) for lot in (getattr(args, "only", None) or [])}
        if only:
            present = {candidate.lot for candidate in candidates}
            # #3256 C : un lot du groupe qui n'est plus prêt (gel, avis,
            # conflit) est **écarté avec son motif**, pas une erreur d'entrée :
            # `resume` et `recompose` repassent le même groupe, et un refus
            # garderait la cible réservée avec MARK_STUCK (avis auditeur B2).
            by_lot = {int(entry.get("work_item") or 0): entry
                      for entry in payload.get("candidates") or []}
            missing = sorted(only - present - set(excluded) - error_lots)
            for lot in missing:
                entry = by_lot.get(lot) or {}
                causes = ", ".join(str(cause.get("code") or "")
                                   for cause in entry.get("causes") or ()
                                   if cause.get("code"))
                detail = (str(entry.get("error") or "").strip()
                          or causes or "lot absent de la liste calculée")
                errors.append(Discarded(
                    lot,
                    (PARALLEL_GROUP_NOT_READY if entry else PARALLEL_GROUP_GONE),
                    detail))
            outside = [candidate for candidate in candidates
                       if candidate.lot not in only]
            if outside:
                candidates = [candidate for candidate in candidates
                              if candidate.lot in only]
                errors = errors + [
                    Discarded(candidate.lot, PARALLEL_EXCLUDE_REASON,
                              "--only, second paquet parallèle (#3256 C)")
                    for candidate in outside]
        # #4581 : une recomposition après un rouge garde les membres de la
        # composition précédente, moins les exclus, à leur source composée.
        # Les lots devenus prêts entre-temps vont à la coupure suivante ; ils
        # sont nommés aux écartés (le contrôle d'inclusion lit ce motif).
        keep = str(getattr(args, "keep_composition", None) or "")
        if keep:
            specs = composition_specs(repo, keep, args.name)
            candidates, kept_discards = keep_composition(
                candidates, specs, worktree=worktree,
                already_discarded={item.lot for item in errors})
            errors = errors + kept_discards
        for item in errors:
            print("écarté #%d : %s — %s"
                  % (item.lot, item.reason, item.detail), flush=True)
        print("liste calculée : %d lot(s) prêt(s), %d en attente, %d en erreur"
              % (len(candidates), len(payload.get("waiting") or []),
                 len(errors)), flush=True)
        if not candidates:
            print("aucun lot prêt : rien à composer", flush=True)
            success = True
            empty = True
            return 0
        compose_script = worktree / COMPOSE_SCRIPT
        if not compose_script.is_file():
            raise PaquetAutoError("%s absent de l'arbre %s"
                                  % (COMPOSE_SCRIPT, worktree))
        # #5084 : une unité qui contient un lot écarté le porterait au paquet
        removed_sources = discarded_sources(
            payload, listed, [item.lot for item in errors])
        composition = compose_candidates(
            worktree, candidates, args.carrier,
            compose_script=compose_script,
            record_package=not args.no_packet_record,
            expect_pushed=remote_sha or "", removed=removed_sources,
            guard=guard)
        if not composition.units:
            print("aucune unité composée : %s"
                  % ", ".join("#%d (%s)" % (item.lot, item.reason)
                              for item in composition.discarded), flush=True)
            success = True
            empty = True
            return 0
        runs: list[TestRun] = []
        freeze_seconds = 0.0
        recomp = 0
        # #4304 (canon §6) : la tête est poussée, gelée et sa ci-full demandée
        # dès la composition et les freeze-checks verts ; les tests par unité
        # tournent ensuite, en parallèle de la CI. Un rouge retire l'unité et la
        # tête recomposée remplace la poussée (bail sur la tête poussée).
        lease = remote_sha
        pushed = ""
        pr_number: int | None = None
        pr_url = ""
        inclusion_rc = 0
        head = ""
        composition.discarded = errors + composition.discarded
        ready_count = len([lot for lot in (payload.get("ready") or [])
                           if int(lot) not in error_lots])
        expected_file = Path(tmp.name) / "attendus.txt"
        # #4463 : l'échéance totale des tests par unité, recompositions comprises
        budget = int(getattr(args, "tests_budget", 0) or 0)
        deadline = time.monotonic() + budget if budget > 0 else None

        def tests_guard() -> None:
            if guard is not None:
                guard()
            if pushed:
                watch_carrier(worktree, branch)

        def title() -> str:
            return "Paquet %s — %d unités sur %s %s" % (
                args.name, len(composition.units), args.target, base_sha[:12])

        def body(tests: list[TestRun] | None) -> str:
            return pr_body(name=args.name, target=args.target, base_sha=base_sha,
                           carrier=args.carrier, composition=composition,
                           candidates=candidates, tests=tests,
                           inclusion_rc=inclusion_rc,
                           freeze_seconds=freeze_seconds)

        def freeze_changes() -> list[Discarded]:
            """#6566 : relit le gel des unités sur la base, juste avant la poussée.

            `paquet-candidats` refuse un code qui n'est pas la tête de la cible
            (#3782) : l'arbre composé ne convient pas, d'où le worktree de
            lecture à `base_sha`, créé une fois pour tout le lancement. #6981 :
            une cible avancée par les seuls atterrissages de coupure ne refuse
            pas la relecture (§12), avant comme après la poussée.
            """
            nonlocal reader
            if reader is None:
                reader = reader_worktree(repo, base_sha)
            refreshed = Path(tmp.name) / "prets-relus.txt"
            fresh, _ = compute_candidates(reader, args.target, base_sha,
                                          refreshed, tolerate_landing=True)
            return changed_units(composition.units,
                                 {candidate.lot: candidate.source
                                  for candidate in fresh})

        def empty_after_recomposition(why: str) -> int:
            """Aucune unité après recomposition : le paquet poussé est retiré."""
            nonlocal success, empty
            if pushed:
                for line in withdraw_packet(
                        worktree, args.carrier, pr_number,
                        "Paquet %s : aucune unité après recomposition (%s), PR "
                        "close par scripts/paquet-auto.py (#4304)."
                        % (args.name, why)):
                    print("retrait du paquet poussé : %s" % line,
                          file=sys.stderr, flush=True)
            print("aucune unité après recomposition : %s"
                  % ", ".join("#%d" % item.lot
                              for item in composition.discarded), flush=True)
            success = True
            empty = True
            return 0

        while True:
            if guard is not None:
                guard()
            freeze_rc, freeze_output, freeze_seconds = run_freeze_checks(
                worktree, timeout=FREEZE_TIMEOUT)
            if freeze_rc != 0:
                # #6272 : rc 1 = au moins un contrôle rouge (le contenu de la
                # composition) ; rc 2 = hors d'un arbre ameesh, qui reste une
                # panne du composeur et ouvre seul l'empreinte §16.
                error = CompositionRed if freeze_rc == 1 else PaquetAutoError
                raise error(
                    "freeze-checks rouge sur la tête composée :\n%s"
                    % _tail(freeze_output))
            print("freeze-checks : vert (%.1f s)" % freeze_seconds, flush=True)
            client_outcome: dict = {}
            client_ok, client_note = check_client_types(
                worktree, base_sha, repo=repo, units=composition.units,
                outcome=client_outcome)
            print("types clients : %s" % client_note, flush=True)
            if client_outcome.get("cause") in CLIENT_MISSING_CAUSES:
                print("types clients : %s" % record_client_types_event(
                    worktree, name=args.name,
                    project=getattr(args, "pause_project", "") or "",
                    outcome=client_outcome), flush=True)
            if not client_ok:
                # #6272 : types clients périmés = rouge du contenu, pas du
                # composeur (le contrôle ne bloque que sur une preuve).
                raise CompositionRed(
                    "types clients rouges sur la tête composée, avant la CI : %s"
                    % client_note)
            table = composition_table(worktree, base_sha)
            manifest_text = build_manifest(
                name=args.name, target=args.target, base_sha=base_sha,
                carrier=args.carrier, composition=composition, table=table,
                tests=None, freeze_seconds=freeze_seconds,
                ready_count=ready_count,
                waiting=len(payload.get("waiting") or []),
                errors=len(errors), client_types=client_note)
            head = write_manifest(worktree, rel, manifest_text, name=args.name,
                                  units=len(composition.units),
                                  discarded=len(composition.discarded))
            print("manifeste %s : tête %s" % (rel, head), flush=True)
            if not args.no_packet_record:
                print(record_packet(worktree, args.carrier, base_sha, head,
                                    branch, args.target,
                                    expect_pushed=lease or ""), flush=True)
            _write_id_file(expected_file, [unit.lot for unit in composition.units])
            inclusion_rc, inclusion_output = run_inclusion(
                worktree, worktree / rel, head, ready_out, expected_file)
            print(inclusion_output, flush=True)
            if inclusion_rc != 0:
                raise PaquetAutoError(
                    "contrôle d'inclusion rouge sur la tête %s : voir le rapport "
                    "ci-dessus" % head[:12])
            print("inclusion : vert", flush=True)
            if not pushed:
                # #6891 : la cible peut avoir atterri pendant la composition
                # (composition, freeze-checks, inclusion). La référence #3782
                # est relue AVANT les contrats, les tests locaux les plus longs
                # (≈12 min au constat h-11) : un décalage lève `TargetMoved`
                # avant de les lancer. Après une poussée, le refus de
                # `freeze_changes` juste avant la poussée reste en place.
                ensure_reference(repo, args.target, base_sha)
            contract_report = None
            if publication_policy(args) != "legacy":
                contract_report = prepublication_contracts(
                    worktree, base=base_sha, budget=contract_budget(args), guard=guard, expected_head=head,
                    parallel=getattr(args, "tests_parallel", DEFAULT_PARALLEL), carrier=args.carrier,
                    project=getattr(args, "pause_project", "") or "")
                contract_run = contract_report.run
                print("contrats avant publication : %s (%.1f s)%s" % (
                    "NON CONCLUANT" if contract_run.inconclusive else
                    ("vert" if contract_run.green else "ROUGE"), contract_run.seconds,
                    (" — " + contract_run.log_path) if contract_run.log_path else ""), flush=True)
                if not args.local:
                    record_contract_result(worktree, args.carrier, contract_report,
                                           base=base_sha, target=args.target)
            if not args.local:
                if not pushed:
                    # #6867 : la cible peut atterrir pendant les contrats (≈12
                    # min au constat h-11) ; la relecture #3782 tombait alors
                    # dans `freeze_changes` (`paquet-candidats` rc 2), traduite
                    # en rc 1 et lot d'empreinte §16 au lieu de la
                    # recomposition immédiate de #6891. Relue ici, avant le gel
                    # et la poussée, elle garde le même geste : rien n'est
                    # poussé, `TARGET_MOVED_RC` et relance aussitôt.
                    try:
                        ensure_reference(repo, args.target, base_sha)
                    except TargetMoved as moved:
                        # #6981 : des contrats rouges restent le verdict :
                        # le pilote reçoit le rouge, pas « cible avancée ».
                        run = contract_report.run if contract_report else None
                        if run is None or run.green or run.inconclusive:
                            raise
                        raise CompositionRed(
                            "contrats rouges sur la tête composée %s (la cible "
                            "a aussi avancé : %s)" % (head[:12], moved)) from moved
                # #6566 : le gel de chaque unité est relu sur la base exacte
                # juste avant la poussée ; une unité regelée (ou sortie de la
                # liste prête) pendant la composition ne part pas sur cette
                # tête : le paquet est recomposé sans elle.
                changes = freeze_changes()
                if changes:
                    removed = {item.lot for item in changes}
                    print("recomposition : gel changé pour %s ; la tête composée "
                          "n'est pas poussée"
                          % ", ".join("#%d" % lot for lot in sorted(removed)),
                          flush=True)
                    for item in changes:
                        print("écarté #%d : %s — %s"
                              % (item.lot, item.reason, item.detail), flush=True)
                    previous = list(composition.discarded)
                    removed_sources.update(
                        (candidate.lot, candidate.source)
                        for candidate in candidates if candidate.lot in removed)
                    candidates = [candidate for candidate in candidates
                                  if candidate.lot not in removed]
                    composition = recompose(
                        worktree, base_sha, candidates, args.carrier,
                        compose_script=compose_script,
                        record_package=not args.no_packet_record,
                        expect_pushed=lease or "", removed=removed_sources,
                        guard=guard)
                    composition.discarded = unique_discarded(
                        previous + composition.discarded + changes,
                        composition.units)
                    if not composition.units:
                        return empty_after_recomposition("gel changé")
                    continue
                if guard is not None:
                    guard()
                if pushed:
                    state = pull_request_state(worktree, branch)
                    if state and state != "OPEN":
                        raise PaquetAutoError(
                            "PR de %s %s pendant les tests par unité : la "
                            "recomposition n'est pas poussée, un agent lit"
                            % (branch, state.lower()))
                check_pause(worktree, getattr(args, "pause_project", "") or "",
                            args.target)
                if contract_report is not None:
                    verify_contract_scope(worktree, contract_report)
                push_branch(worktree, branch, force_lease=lease)
                print("poussée : %s%s" % (branch, " (recomposition)"
                                          if lease else ""), flush=True)
                lease = pushed = head
                pr_number, pr_url = ensure_pull_request(
                    worktree, branch, args.target, title(), body(None))
                print("PR : %s" % (pr_url or pr_number or "?"), flush=True)
                note = ("paquet %s poussé par scripts/paquet-auto.py dès la "
                        "composition (canon §6, #4304) : %d unités, %d écartés, "
                        "inclusion verte, tests par unité en cours ; manifeste %s"
                        % (args.name, len(composition.units),
                           len(composition.discarded), rel))
                print(freeze_carrier(worktree, args.carrier, head, note), flush=True)
                print(request_full_ci(worktree, args.carrier, head), flush=True)
            try:
                runs, faulty, removals = check_local_tests(
                    worktree, composition, timeout=args.tests_timeout,
                    base_sha=base_sha, guard=tests_guard,
                    parallel=getattr(args, "tests_parallel", DEFAULT_PARALLEL),
                    deadline=deadline, contract_report=contract_report,
                    legacy_contracts=publication_policy(args) == "legacy")
            except CarrierSettled as settled:
                rc = interrupted_by_carrier(args, worktree, branch, head,
                                            settled.state)
                success = True
                return rc
            except AttributionUnreadable as error:
                # #4217 : aucun retrait automatique ; le pilote reçoit le
                # rouge que les préfixes n'ont pas permis de situer.
                if not getattr(args, "local", False):
                    notify_unattributable(
                        args.name, args.carrier, error,
                        pilot=getattr(args, "pilot", "mesh-design"),
                        sender=getattr(args, "sender", None))
                raise
            flaky = sorted({name for run in runs
                            for name in getattr(run, "flaky_tests", [])})
            if flaky and not getattr(args, "local", False):
                # #4221 : instable consigné non concluant, le paquet continue ;
                # le pilote le sait même si une recomposition suit.
                notify_flaky(args.name, args.carrier, flaky,
                             pilot=getattr(args, "pilot", "mesh-design"),
                             sender=getattr(args, "sender", None))
            if contract_report is not None and not args.local:
                # Classification may turn a flaky negative into an explicit
                # inconclusive result; the projection must reflect that too.
                record_contract_result(worktree, args.carrier, contract_report,
                                       base=base_sha, target=args.target)
            if not faulty:
                break
            if pushed:
                # #4322 : la ci-full verte a pu fusionner la tête pendant les
                # tests ; ses unités sont sur la cible, rien à retirer
                state = pull_request_state(worktree, branch)
                if state == "MERGED":
                    success = True
                    return merged_during_tests(
                        args, worktree, head, runs, faulty, branch, rel)
                if state and state != "OPEN":
                    raise PaquetAutoError(
                        "PR de %s %s pendant les tests par unité : rien n'est "
                        "recomposé, un agent lit" % (branch, state.lower()))
            if recomp >= MAX_RECOMPOSITIONS:
                # #6272 : des tests d'unité encore rouges après le retrait
                # automatique sont un rouge du contenu, pas une panne du
                # composeur ; le pilote est prévenu par le tic.
                raise CompositionRed(
                    "tests locaux encore rouges après recomposition : %s ; "
                    "retrait automatique épuisé, un agent lit les modules"
                    % ", ".join("#%d" % lot for lot in faulty))
            recomp += 1
            removed = set(faulty)
            if not removals:
                removals = [Removal(lot) for lot in faulty]
            reasons = []
            for removal in removals:
                modules = next(
                    (run.modules for run in runs
                     if run.label == "unité #%d" % removal.lot), [])
                if not removal.tests:
                    removal.tests = list(modules)
                if removal.proof == "prefixes":
                    reason = ("tests ciblés rouges (%s), situés au premier "
                              "préfixe de l'empilement, retirés de la "
                              "recomposition"
                              % (", ".join(removal.tests[:3])
                                 or "tests illisibles"))
                    detail = " ; ".join(removal.evidence[:3])
                elif removal.attributed:
                    reason = ("tests ciblés rouges (%s), attribués à son "
                              "unité (#3801), retirés de la recomposition"
                              % (", ".join(removal.tests[:3])
                                 or "tests illisibles"))
                    detail = " ; ".join(removal.evidence[:3])
                else:
                    reason = ("tests locaux rouges (%s), retiré de la "
                              "recomposition"
                              % (", ".join(modules) or "modules illisibles"))
                    detail = ""
                reasons.append(Discarded(removal.lot, reason, detail))
            # #5971 : mémoire et signalement avant de perdre les sources de
            # la composition retirée, même si la recomposition échoue ensuite.
            if not getattr(args, 'local', False):
                failed = notify_removals(
                    worktree, args.name, removals,
                    sender=getattr(args, 'sender', None),
                    sources={candidate.lot: candidate.source for candidate in candidates},
                    target=args.target, base=base_sha)
                if failed:
                    print('mémoire/courriers de retrait non enregistrés : %s'
                          % ' ; '.join(failed), file=sys.stderr, flush=True)
            print("recomposition : retrait de %s"
                  % ", ".join("#%d" % lot for lot in faulty), flush=True)
            removed_sources.update(
                (candidate.lot, candidate.source) for candidate in candidates
                if candidate.lot in removed)
            candidates = [candidate for candidate in candidates
                          if candidate.lot not in removed]
            # les écartés déjà écrits (liste calculée, recomposition
            # précédente) restent au manifeste : le contrôle d'inclusion les lit
            previous = list(composition.discarded)
            composition = recompose(
                worktree, base_sha, candidates, args.carrier,
                compose_script=compose_script,
                record_package=not args.no_packet_record,
                expect_pushed=lease or "", removed=removed_sources,
                guard=guard)
            composition.discarded = unique_discarded(
                previous + composition.discarded + reasons,
                composition.units)
            if not composition.units:
                return empty_after_recomposition("tests par unité rouges")
        inconclusive = sum(1 for run in runs if run.inconclusive)
        tests_note = ("verts" if not inconclusive
                      else "verts + %d non concluant(s) consigné(s)" % inconclusive)
        try:
            if guard is not None:
                guard()
        except EpochLost:
            # #4237 : tête déjà poussée et gelée (#4304), tests verts : le
            # détenteur courant en finit la publication sans retester
            if not args.local:
                keep_tested(repo, args.carrier, args.name, base_sha, head,
                            tests="%s (%s)" % (tests_note, _tests_summary(runs)),
                            sender=getattr(args, "sender", None))
            raise
        if args.local:
            print("paquet %s prêt localement : branche %s, tête %s"
                  % (args.name, branch, head), flush=True)
            success = True
            return 0
        state = pull_request_state(worktree, branch)
        if state == "MERGED":
            success = True
            return merged_during_tests(args, worktree, head, runs, [], branch, rel)
        if state and state != "OPEN":
            print("PR %s pendant les tests par unité : corps non mis à jour"
                  % state.lower(), flush=True)
        else:
            pr_number, pr_url = ensure_pull_request(
                worktree, branch, args.target, title(), body(runs))
        print(note_carrier(
            worktree, args.carrier,
            "paquet %s : tests par unité %s sur la tête poussée %s (%s) ; %s ; "
            "inclusion verte ; manifeste %s"
            % (args.name, tests_note, head[:12], _tests_summary(runs),
               _direct_note(composition, runs), rel)),
            flush=True)
        mail = mail_body(name=args.name, target=args.target, base_sha=base_sha,
                         carrier=args.carrier, head=head, branch=branch,
                         composition=composition, tests=runs,
                         inclusion_rc=inclusion_rc, pr_number=pr_number,
                         pr_url=pr_url)
        try:
            send_mail(args.pilot, args.carrier, mail, getattr(args, "sender", None))
        except PaquetAutoError as error:
            print("courrier non envoyé : %s\n\n%s" % (error, mail),
                  file=sys.stderr)
            raise
        print("courrier envoyé à %s" % args.pilot, flush=True)
        success = True
        return 0
    finally:
        try:
            if reader is not None:
                remove_worktree(repo, reader)
            if success and not keep_worktree:
                remove_worktree(repo, worktree)
                if empty:
                    git(repo, "branch", "-D", branch, check=False)
            elif not success:
                print("worktree conservé pour diagnostic : %s" % worktree,
                      file=sys.stderr)
        finally:
            if claim is not None:
                claim.stop()
            lock.release()


# ---------------------------------------------------------------------------
# #6814 : la composition d'un agent ne vit pas dans le cgroup de son exécuteur
# ---------------------------------------------------------------------------

#: Un redémarrage de l'exécuteur (mise à jour staging) arrête l'unité et tue
#: son cgroup : une composition lancée dans un tour mourait avec lui, sous son
#: bail (h-03, #6810). Le scope transitoire vit sous le gestionnaire de
#: l'utilisateur, hors de l'unité : l'arrêt du tour ne l'atteint plus. Il lui
#: faut aussi une session neuve : l'exécuteur arrête son harnais par
#: `killpg(groupe du tour)` (runner.py, `_signal_group`), et un changement de
#: cgroup ne change pas de groupe de processus (B/P1 impl-11).
RUNNER_CGROUP_RE = re.compile(
    r"/ameesh-runner(?:-agent)?@[^/]*\.service(?:/|$)")
#: Posé dans le scope : une seconde exécution ne se ré-enveloppe pas.
SCOPED_ENV = "AMEESH_PAQUET_AUTO_SCOPED"
#: Variables du tour et de son bail, retirées du scope. Le nom de l'agent
#: reste (`AGENT_MAIL_NAME`, identité explicite) : après le redémarrage,
#: l'exécuteur reprend un bail neuf et une identité liée à l'ancienne époque
#: serait refusée (« unbound sender ») au milieu de la composition.
TURN_VARIABLES = (
    "AMEESH_RUNNER_ID", "AGENT_MESH_RUNNER_ID",
    "AMEESH_LEASE_EPOCH", "AGENT_MESH_LEASE_EPOCH",
    "AMEESH_LOT",
)
#: Dossier stable des worktrees, celui du tic (`CacheDirectory`) : le TMPDIR
#: d'un tour peut être vidé à une rotation de session pendant la composition.
SCOPE_TMPDIR = ".cache/ameesh/paquet-auto"
#: Le scope écrit dans un journal, jamais dans le tube du tour : quand
#: l'exécuteur est arrêté, le tube se ferme et le premier `print` lèverait
#: `BrokenPipeError` au milieu de la composition. Le parent recopie le journal
#: tant que le tour vit ; une fois tué, le scope continue d'y écrire.
SCOPE_LOG_ENV = "AMEESH_PAQUET_AUTO_LOG"
SCOPE_LOG_DIR = "logs"


def in_runner_cgroup(path: str = "/proc/self/cgroup") -> bool:
    """#6814 : ce processus vit-il dans le cgroup d'un exécuteur d'agent ?

    `path` est un fichier au format `/proc/self/cgroup` (les témoins).
    """
    try:
        text = Path(path).read_text(encoding="utf-8")
    except (OSError, UnicodeError):
        return False
    return bool(RUNNER_CGROUP_RE.search(text))


def scope_unit(name: str) -> str:
    """Un nom d'unité systemd sûr, préfixé `ameesh-paquet-auto-`."""
    cleaned = re.sub(r"[^A-Za-z0-9_.-]", "-", str(name or "composition"))
    cleaned = cleaned.strip(".-")[:80] or "composition"
    return "ameesh-paquet-auto-%s-%d-%s" % (
        cleaned, os.getpid(), uuid.uuid4().hex[:8])


def scope_log(unit: str) -> Path:
    """#6814 : le journal du scope, sous le dossier stable du composeur."""
    directory = Path.home() / SCOPE_TMPDIR / SCOPE_LOG_DIR
    directory.mkdir(parents=True, exist_ok=True, mode=0o700)
    return directory / (unit + ".log")


def scope_environment(carrier: int = 0, *, log: Path | None = None) -> dict:
    """#6814 : l'environnement de la composition dans son scope.

    Le scope hérite du tour (DSN, `AGENT_MAIL_NAME`, `AMEESH_CONFIG`, caches),
    mais pas de son bail : l'agent reste nommé, en identité explicite, et le
    courrier de fin part encore après le redémarrage. `AMEESH_LOT` suit le
    porteur (#6810), jamais le lot que l'agent avait au moment du lancement.
    """
    env = dict(os.environ)
    env[SCOPED_ENV] = "1"
    for variable in TURN_VARIABLES:
        env.pop(variable, None)
    if carrier:
        env["AMEESH_LOT"] = str(carrier)
        labels = [
            part for part in str(env.get("AMEESH_CONTAINER_LABELS") or "").split(",")
            if part and not part.startswith("ameesh.lot=")]
        labels.append("ameesh.lot=%d" % carrier)
        env["AMEESH_CONTAINER_LABELS"] = ",".join(labels)
    if log is not None:
        env[SCOPE_LOG_ENV] = str(log)
    parent = Path.home() / SCOPE_TMPDIR
    try:
        parent.mkdir(parents=True, exist_ok=True, mode=0o700)
    except OSError:
        pass
    else:
        for variable in ("TMPDIR", "TMP", "TEMP", "AMEESH_TMPDIR"):
            env[variable] = str(parent)
    return env


def scope_command(argv: list[str], *, unit: str, binary: str) -> list[str]:
    """#6814 : la ligne `systemd-run --scope` qui relance ce script.

    Le shell du scope redirige les deux sorties vers le journal : les tubes du
    tour peuvent disparaître sans toucher la composition. Le parent, lui,
    recopie le journal tant qu'il vit (#6814, `_follow_scope`).
    """
    return [binary, "--user", "--scope", "--quiet", "--collect",
            "--unit", unit, "/bin/sh", "-c",
            'exec "$0" "$@" >>"$%s" 2>&1' % SCOPE_LOG_ENV,
            sys.executable, str(Path(__file__).resolve())] + list(argv)


def _copy_log(path: Path, offset: int) -> int:
    """Recopie ce qui a été écrit dans le journal depuis `offset` ; rend le
    nouvel offset. Une sortie de tour fermée n'arrête pas la copie."""
    try:
        with open(path, "rb") as stream:
            stream.seek(offset)
            data = stream.read()
            offset = stream.tell()
    except OSError:
        return offset
    if not data:
        return offset
    try:
        sys.stdout.buffer.write(data)
        sys.stdout.buffer.flush()
    except AttributeError:
        try:
            sys.stdout.write(data.decode("utf-8", "replace"))
            sys.stdout.flush()
        except (OSError, ValueError):
            pass
    except (OSError, ValueError):
        pass
    return offset


def _follow_scope(proc, log: Path) -> int:
    """Suit le scope jusqu'à sa fin et rend son code de sortie.

    La boucle ne dépend pas du tube du tour : si le parent est tué, le scope
    continue, ses écritures restent dans le journal.
    """
    offset = 0
    while True:
        offset = _copy_log(log, offset)
        code = proc.poll()
        if code is not None:
            _copy_log(log, offset)
            return int(code)
        time.sleep(0.2)


def maybe_scope(argv: list[str], *, name: str, carrier: int = 0) -> int | None:
    """#6814 : relance la composition hors du cgroup de l'exécuteur.

    Le scope a son propre cgroup (gestionnaire utilisateur) et sa propre
    session (PGID distinct), donc l'arrêt du tour ne l'atteint ni par le
    cgroup ni par `killpg`. Rend None quand le processus n'est pas dans un
    tour, quand le scope est déjà posé, ou quand `systemd-run` manque (repli
    direct, dit à l'appelant) ; sinon rend le code de sortie du scope après
    avoir recopié son journal.
    """
    if os.environ.get(SCOPED_ENV) or not in_runner_cgroup():
        return None
    binary = shutil.which("systemd-run")
    if not binary:
        print("paquet-auto : systemd-run absent ; composition dans le cgroup de "
              "l'exécuteur (un redémarrage staging la tuerait)", file=sys.stderr,
              flush=True)
        return None
    unit = scope_unit(name)
    try:
        log = scope_log(unit)
        descriptor = os.open(log, os.O_WRONLY | os.O_CREAT | os.O_TRUNC, 0o600)
        os.close(descriptor)
    except OSError as error:
        print("paquet-auto : journal du scope indisponible (%s) ; composition dans "
              "le cgroup de l'exécuteur (un redémarrage staging la tuerait)"
              % error, file=sys.stderr, flush=True)
        return None
    env = scope_environment(carrier, log=log)
    command = scope_command(argv, unit=unit, binary=binary)
    print("paquet-auto : composition hors du cgroup de l'exécuteur (%s), "
          "journal %s" % (unit, log), file=sys.stderr, flush=True)
    try:
        # #6814 (B/P1 impl-11) : changer de cgroup ne détache pas le groupe de
        # processus. L'exécuteur lance son harnais avec `start_new_session` et
        # son arrêt fait `killpg(groupe du tour)` (`runner.py`, `_signal_group`) :
        # sans session neuve, le scope héritait de ce groupe et mourait quand
        # même. Le scope a donc sa propre session (PGID = pid de systemd-run).
        proc = subprocess.Popen(command, env=env, stdin=subprocess.DEVNULL,
                                start_new_session=True)
    except OSError as error:
        print("paquet-auto : scope indisponible (%s) ; composition dans le cgroup "
              "de l'exécuteur (un redémarrage staging la tuerait)" % error,
              file=sys.stderr, flush=True)
        return None
    return _follow_scope(proc, log)


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(
        description="Compose le paquet horaire sans agent (#3807).")
    parser.add_argument("--repo", default=".", help="clone du projet (défaut : .)")
    parser.add_argument("--target", default="main", help="branche cible (défaut : main)")
    parser.add_argument("--name", required=True, help="nom du paquet (h-AAAAMMJJ-NN)")
    parser.add_argument("--carrier", type=int, default=0,
                        help="lot porteur du paquet")
    parser.add_argument("--pilot", default="mesh-design",
                        help="destinataire du courrier (défaut : mesh-design)")
    parser.add_argument("--from", dest="sender",
                        default=os.environ.get("AMEESH_ROLE") or None,
                        help="expéditeur du courrier (défaut : AMEESH_ROLE, sinon "
                             "l'identité liée)")
    parser.add_argument("--local", action="store_true",
                        help="composition de contrôle : manifeste, packet "
                             "record et tests par unité, sans poussée, PR, gel, "
                             "ci-full ni courrier")
    parser.add_argument("--force", action="store_true",
                        help="recomposer un paquet existant : réécrit la "
                             "branche distante sous --force-with-lease (retirer "
                             "la CI et le feu avant)")
    parser.add_argument("--exclude", type=int, action="append", default=[],
                        metavar="LOT",
                        help="#3845 : ne pas composer ce lot prêt (répétable) ; "
                             "il est écrit aux écartés du manifeste")
    parser.add_argument("--exclude-reason", action="append", default=[],
                        metavar="TEXTE",
                        help="#4201 : motif du lot exclu, apparié par ordre aux "
                             "--exclude (répétable) ; sans motif, le manifeste "
                             "écrit « %s »" % EXCLUDE_DEFAULT_REASON)
    parser.add_argument("--only", type=int, action="append", default=[],
                        metavar="LOT",
                        help="#3256 C : ne composer que ces lots prêts (répétable) ; "
                             "les autres, prêts, sont écrits aux écartés avec le "
                             "motif du paquet parallèle, jamais composés deux fois")
    parser.add_argument("--keep-composition", default=None, metavar="REF",
                        help="#4581 : recomposition après un rouge : ne garder "
                             "que les membres de la composition du manifeste gelé "
                             "à REF, moins les --exclude, chacun à sa source "
                             "composée ; un lot prêt depuis, regelé ou disparu va "
                             "à la coupure suivante (écarté, non composé)")
    parser.add_argument("--replay", default=None, metavar="REF",
                        help="rejouer le manifeste d'une référence et comparer "
                             "les arbres (avec --candidates-json : refuser une "
                             "unité en erreur ou non prête, #3813)")
    parser.add_argument("--base", default=None,
                        help="avec --replay : la base du rejeu (seul usage ; "
                             "la composition part toujours de la tête fraîche "
                             "d'origin/<cible>)")
    parser.add_argument("--candidates-json", default=None,
                        help="avec --replay : la sortie `paquet-candidats "
                             "--json` pour refuser une unité en erreur ou "
                             "non prête (#3813)")
    parser.add_argument("--python", default=None,
                        help="interpréteur des outils enfants (défaut : un "
                             "Python du projet qui porte psycopg)")
    parser.add_argument("--contracts-budget", type=int, default=None,
                        help="borne canon de la passe avant publication, attente comprise")
    parser.add_argument("--tests-timeout", type=int, default=DEFAULT_TIMEOUT,
                        help="borne d'une exécution unittest (défaut : %d s)"
                        % DEFAULT_TIMEOUT)
    parser.add_argument("--tests-parallel", type=int, default=DEFAULT_PARALLEL,
                        help="#4271 : séries d'unités jouées en même temps, "
                             "dans les créneaux `ameesh heavy` (défaut : %d, "
                             "en série)" % DEFAULT_PARALLEL)
    parser.add_argument("--tests-budget", type=int, default=0, metavar="S",
                        help="#4463 : borne totale des tests par unité, en "
                             "secondes (paquet-horaire : packet_tests."
                             "total_timeout_seconds ; défaut : 0, sans borne totale)")
    parser.add_argument("--keep-worktree", action="store_true",
                        help="conserver le worktree jetable (diagnostic)")
    parser.add_argument("--keep-failed-hours", type=float, default=24.0,
                        help="N1 : purger un worktree d'échec au-delà de N "
                             "heures (défaut : 24 ; 0 = jamais)")
    parser.add_argument("--contract-publication", choices=("contracts-first", "legacy"),
                        default=None, help="#6183 : politique du projet compilée par le composeur")
    parser.add_argument("--publish", default=None, metavar="REF",
                        help="#4237 : publier sans retester la tête testée gardée "
                             "sous REF (%s<nom>) : chaîne et branche vérifiées, "
                             "corps de PR, note, ci-full et courrier ; "
                             "sortie %d si la chaîne est refusée"
                             % (TESTED_REF_PREFIX, PUBLISH_REFUSED))
    parser.add_argument("--publish-sha", default=None, metavar="SHA",
                        help="#4237 : avec --publish, le SHA complet de la tête "
                             "testée (marque `%s`) ; une réf qui en diffère est "
                             "refusée" % TESTED_MARK.strip())
    parser.add_argument("--pause-project", default="", metavar="PROJET",
                        help="#5084 : relire la pause de la cible de ce projet "
                             "(`packet_pause`) avant de pousser ; une pause "
                             "active arrête la tentative sans poussée (sortie %d)"
                             % PAUSED_RC)
    parser.add_argument("--no-packet-record", action="store_true",
                        help="banc : ne pas lire/écrire la base (porteur, packet record)")
    raw_argv = list(sys.argv[1:] if argv is None else argv)
    args = parser.parse_args(raw_argv)
    if __name__ == "__main__":
        # #6814 : seul le point d'entrée du script se ré-enveloppe ; un module
        # importé (tests, `paquet-horaire`) ne remplace pas le processus de
        # son appelant.
        scoped = maybe_scope(raw_argv, name=args.name, carrier=args.carrier)
        if scoped is not None:
            return scoped
    global _PYTHON_OVERRIDE
    if args.python:
        if not Path(args.python).is_file():
            print("paquet-auto : --python : %s introuvable" % args.python,
                  file=sys.stderr)
            return 2
        _PYTHON_OVERRIDE = str(Path(args.python).resolve())

    global _TOOLING
    _TOOLING = resolve_tooling(args.pause_project)
    if _TOOLING.declared:
        print("outils de contrats déclarés par %s : plan %s, lanceur %s, contrats %s"
              % (_TOOLING.project, _TOOLING.plan_script, _TOOLING.parts_script,
                 ", ".join(_TOOLING.contracts)), flush=True)
    try:
        if args.contracts_budget is not None and args.contracts_budget <= 0:
            raise InputError("--contracts-budget doit être strictement positif")
        if args.replay:
            if args.carrier or args.local:
                raise InputError("--replay : ni --carrier ni --local")
            if args.keep_composition:
                raise InputError("--keep-composition ne s'applique pas à --replay")
            if not args.base:
                raise InputError("--replay exige --base")
            if args.candidates_json:
                candidates_json = Path(args.candidates_json)
            else:
                candidates_json = None
            return replay(Path(args.repo).resolve(), args.replay, args.name,
                          args.base, keep=args.keep_worktree,
                          candidates_json=candidates_json)
        if args.base:
            raise InputError("--base ne s'applique qu'à --replay : la "
                             "composition part de la tête fraîche de la cible")
        if args.candidates_json:
            raise InputError("--candidates-json ne s'applique qu'à --replay")
        if not args.carrier:
            raise InputError("--carrier : le lot porteur est requis")
        if args.publish:
            if not re.fullmatch(r"[0-9a-f]{40}", args.publish_sha or ""):
                raise InputError("--publish exige --publish-sha : le SHA complet "
                                 "attesté par la marque de tests")
            if args.local or args.no_packet_record or args.exclude \
                    or args.keep_composition:
                raise InputError("--publish : ni --local, ni --no-packet-record, "
                                 "ni --exclude, ni --keep-composition (la tête "
                                 "testée est publiée telle quelle)")
            return publish_tested(args)
        if args.no_packet_record and not args.local:
            raise InputError("--no-packet-record est réservé au banc avec --local")
        return compose_packet(args)
    except InputError as error:
        print("paquet-auto : %s" % error, file=sys.stderr)
        return 2
    except ChainRefused as error:
        print("paquet-auto : tête testée non publiable : %s" % error, file=sys.stderr)
        return PUBLISH_REFUSED
    except CompositionBusy as error:
        print("paquet-auto : %s" % error, file=sys.stderr)
        return IN_PROGRESS_RC
    except PacketPaused as error:
        print("paquet-auto : %s" % error, file=sys.stderr)
        return PAUSED_RC
    except CompositionRed as error:
        # #6272 : paquet rouge par son contenu, pas par le composeur ; le tic
        # le rapporte au pilote sans ouvrir de lot d'empreinte §16.
        print("paquet-auto : %s" % error, file=sys.stderr)
        return COMPOSITION_RED_RC
    except TargetMoved as error:
        # #6891 : la cible a avancé avant les tests locaux ; rien n'est poussé.
        # `paquet-horaire.py` relance aussitôt sur la nouvelle tête.
        print("paquet-auto : %s" % error, file=sys.stderr)
        return TARGET_MOVED_RC
    except PaquetAutoError as error:
        print("paquet-auto : %s" % error, file=sys.stderr)
        return 1
    except KeyboardInterrupt:
        print("paquet-auto : interrompu", file=sys.stderr)
        return 1


if __name__ == "__main__":
    sys.exit(main())
