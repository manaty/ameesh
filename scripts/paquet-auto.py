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
5. l’instantané ameesh (rendu transitoire sous TMPDIR) : base et SHA de la
   cible, table `## Composition` produite par `paquet-compose.py --units`
   (gabarit #3629), admissions, écartés et leur motif, freeze-checks ; ses
   séries directes y sont dites jouées sur le préfixe composé de chaque
   unité, avant la poussée (#7740) ;
6. `ameesh work packet record` (liaison des unités au porteur), puis
   `scripts/paquet-inclusion.py` sur la tête ;
7. la passe de contrats de l'arbre final (plan commun, #6182), puis les
   **séries directes** de chaque unité, jouées sur son **préfixe composé**
   (`base + u1 + … + uN`, dans un worktree détaché) et **avant toute
   poussée** (#7740) : les modules de test apportés par l'unité, ses voisins
   de la carte et les témoins globaux que son empreinte touche (migration :
   rôles et schéma ; stockage : contrats des écrivains). Une série joue
   jusqu'au bout de `--tests-timeout` ; seule la borne totale
   (`--tests-budget`) la coupe en temps mural. Une série sans module direct
   est un **non-concluant nommé**, jamais un vert. Un test ciblé **rouge**
   d'un module **voisin** est situé en rejouant ce test sur les préfixes de
   l'empilement ; la première unité où il apparaît est retirée, son auteur
   est prévenu, et le paquet est recomposé **une seule fois** sans elle
   (#4217), sans consommer de CI de paquet. L'attribution par chevauchement
   de fichiers (`packet_red`, #3801) ne désigne plus un voisin ; elle reste
   la lecture des rouges propres et des contrats (#3931). Un rouge voisin
   impossible à situer sur les préfixes n'entraîne **aucun retrait** : le
   pilote est prévenu et le paquet s'arrête. Un test rouge sur la tête mais
   **vert sur tous les préfixes** est instable (#4221) : consigné non
   concluant, sans retrait, le paquet continue et le pilote est prévenu. Un
   rouge non attribuable garde le geste antérieur : une unité dont le module
   propre a rougi est retirée, un contrat rouge arrête (l'attribution demande
   un agent). Un run **non concluant** (délai, base absente, psql/psycopg
   indisponible, erreur de fixture DB en QueryCanceled/timeout, rc inattendu)
   est consigné à la PR, au porteur et au courrier, sans retirer d'unité ni
   arrêter le paquet : le canon §3 le veut, la ci-full tranche ;
8. une fois contrats et séries directes verts (ou seulement non concluants
   nommés) : poussée de `integration/paquet-<nom>`, PR ouverte ou mise à
   jour, gel `ameesh work milestone <porteur> frozen --sha <tête>` et demande
   `ameesh ci run <porteur> --full` (canon §6) ; un rouge de série a recomposé
   avant ce point, donc aucune CI de paquet n'est consommée par lui ;
9. le résultat des tests au corps de la PR et au journal du porteur,
   courrier au pilote.

Un rouge **du contenu** — contrat, test d'une unité, contrôle de gel, types
clients périmés — sort en `COMPOSITION_RED_RC` (#6272) : le composeur a fait
son travail, le pilote est prévenu, `paquet-horaire.py` le rapporte sans ouvrir
de lot d'empreinte §16. La panne du composeur (git, base, poussée, outil
manquant) reste rc 1 et ouvre seule cette empreinte.

Ce qui reste au pilote, hors de ce script : « verdict non requis », le feu et
toute fusion sur la cible. Le script ne fusionne jamais la cible et ne déclare
aucun verdict. La ci-full est demandée **après** les séries directes ; elle
arme la fusion automatique (#2813), et un porteur sans delta propre n'attend
aucun verdict. Le seul intervalle où une fusion peut précéder le relevé de la
PR est celui entre la poussée et la fin du lancement (canon §11 bis, panier
#4322) : le script ne recompose alors plus rien, un rouge d'unité va au porteur
et au pilote (corriger en avant ou revert), et la ligne `MERGED_LINE` dit à
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

#4463 : quand une tête a déjà été poussée (reprise), la garde relue avant
chaque série relit aussi l'état de la PR : fusionnée (ou close) pendant les
séries, leurs résultats ne peuvent plus retirer une unité ; les séries non
parties sont abandonnées et le porteur reçoit « tests par unité interrompus :
porteur fusionné à <sha> ». `--tests-budget S` (paquet-horaire :
`packet_tests.total_timeout_seconds` du canon compilé) borne la durée totale
des tests en temps mural : chaque série est arrêtée au temps restant, celles
qui ne partent plus avant l'échéance sont non concluantes (canon §3) et le tic
suivant peut partir.

#4887/#7740 : sous cette borne, une série directe ne prend plus une part du
budget total (c'est cette part qui la coupait après 29 à 43 s sur h-06/h-09) :
elle part avec `--tests-timeout`, sur le préfixe composé de son unité, et seule
la borne murale restante peut la couper. Les contrats gardent leur part
(`series_share`). Le corps de la PR dit, lot par lot, si ses modules directs
ont été joués jusqu'au bout (`_direct_lines`) ; une série coupée n'en
revendique aucun et 0 module joué est un non-concluant nommé (#4814, #7740).
#7950 : un module de contrat jamais lancé (arrêt après un contrat négatif,
borne globale déjà atteinte) ou coupé par la borne murale ne sort plus « code
de retour None » : il est dit « non lancé : … » ou « coupé par le délai
global … », avec la durée, et ne compte jamais comme joué. Dans la fin de
passe, le temps restant est réparti entre les modules pas encore partis pour
qu'un module long ne consomme plus la borne des suivants (h-07).
Le contrôle rapide du job plan « types clients à jour » passe avant la
poussée : le vrai `gen-api --check` si Node et les dépendances sont là, sinon
la garde déterministe « OpenAPI changé sans schéma » (#4831).

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

#7754 : après la poussée, la PR existe. La lecture `gh pr view` du corps final
(GraphQL) peut tomber sous la limite de débit du compte partagé : elle est
relue par l'API REST (`gh api`, bucket `core`, distinct du bucket GraphQL),
dans `ensure_pull_request` comme dans `pull_request_state`. Une mise à jour
finale du corps qui échoue encore (écriture refusée, repli REST indisponible)
n'arrête plus le paquet : sa cause part à la note du porteur et au courrier,
rc 0 — la poussée, le gel et la ci-full sont déjà faits.

Modes :

* normal : composition réelle, poussée, PR, gel, ci-full, tests et courrier ;
* `--local` : rendu transitoire, `packet record` sous bail et tests par unité, sans poussée,
  PR, gel, ci-full ni courrier (composition de contrôle) ;
* `--replay <réf> [--candidates-json <sortie paquet-candidats --json>]` :
  rejoue la table `## Composition` d'un manifeste déjà gelé depuis `--base`,
  reprend le manifeste historique de `--réf` seulement s’il existe (aucun effet sur le
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
#: #7950 : sous cette fin de passe (secondes, ou un cinquième de la borne
#: globale des contrats), le temps restant est réparti entre les modules pas
#: encore partis au lieu d'être donné en entier au premier : un module long
#: ne consomme plus la borne des suivants (h-07, test_7665 puis six modules
#: jamais lancés).
CONTRACT_TAIL_MIN_S = 60
#: #8207 : part maximale d'un module dans la queue de la passe. La répartition
#: par module ignorait les `parallel` lanes : à 240 s restantes, quinze modules
#: recevaient 16 s alors que deux lanes laissaient environ 150 s de travail ;
#: des contrats de 17 à 59 s étaient coupés et la passe sortait non concluante
#: avec 100 à 150 s de borne encore libres (h-01, h-02, h-03 du 24/11). Un
#: module peut donc prendre jusqu'à 90 s dans la queue, jamais plus de la
#: moitié de ce qui reste : l'autre lane garde de quoi lancer les suivants.
CONTRACT_TAIL_MODULE_S = 90
#: #7740 : témoins globaux qu'une empreinte touche, en plus de la sélection
#: des voisins : une migration change les rôles et le schéma, un stockage
#: change le contrat des écrivains, `alert_briefs.py` porte les alertes de
#: pause (veille 17:58, test_5424 rouge sur une tête qui le touchait).
MIGRATION_WITNESSES = ("tests.test_cluster", "tests.test_migrations",
                       "tests.test_migrations_additives")
STORAGE_WITNESSES = ("tests.test_l107_executor_contract",
                     "tests.test_contract_registry_ameesh",
                     "tests.test_contract_selection")
ALERT_BRIEF_WITNESSES = ("tests.test_5424_review_family_paused",)
#: (préfixes de chemin, modules témoins) : chaque groupe dont un préfixe est
#: touché ajoute ses témoins à la série de l'unité.
FOOTPRINT_WITNESSES = (
    (("src/ameesh/migrations/", "deploy/sql/", "deploy/migrations/"),
     MIGRATION_WITNESSES),
    (("src/ameesh/storage/",), STORAGE_WITNESSES),
    (("src/ameesh/alert_briefs.py",), ALERT_BRIEF_WITNESSES),
)
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
#: #8290 : le porteur est clos (abandonné, remplacé, livré) : relu avant
#: chaque geste, il arrête la composition sans rien pousser, geler ni armer ;
#: `paquet-horaire.py` s'arrête proprement, sans marque d'échec ni courrier.
CARRIER_CLOSED_RC = 7
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
#: #4304/#7740 : la ligne du manifeste poussé avant les séries directes
#: (relue par `prevalidation_calls.composer_tested`, #4136 : ne pas la
#: reformuler seule). #7740 : les tests ne tournent plus après la poussée en
#: parallèle de la CI, mais avant elle, sur le préfixe composé de chaque
#: unité ; l'ancienne formulation reste reconnue pour les manifestes déjà
#: poussés.
TESTS_PARALLEL_LINE = ("- Tests locaux : joués par le composeur sur le préfixe "
                       "composé de chaque unité, avant la poussée (#7740) ; "
                       "résultat à la PR, au porteur et au courrier du pilote.")
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
#: #8290 : un lot déjà membre d'un autre paquet ouvert n'entre pas dans
#: celui-ci (§11 bis : une unité, un paquet ; une CI de paquet à la fois).
CARRIED_ELSEWHERE_REASON = "déjà porté par un autre paquet ouvert"
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


class CarrierClosed(PaquetAutoError):
    """#8290 : le porteur a été clos pendant la composition (h-20261126-01)."""


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
class ContractFold:
    """#8395 : le commit de pliage des fragments de révision du contrat.

    ``revisions`` porte ``(numéro, lot, manière)`` dans l'ordre du manifeste ;
    ``files`` les chemins écrits ou retirés (contrat, surface,
    ``CONTRACT_VERSION`` et fragments). Le manifeste cite ``head`` et ces
    révisions : le contrôle d'inclusion sait alors que les lignes de contrat
    ont été repliées par le composeur, pas perdues par un lot.
    """

    head: str
    revisions: list[tuple[str, int, str]]
    files: list[str]


@dataclass
class Composition:
    units: list[Unit]
    discarded: list[Discarded]
    head: str
    #: #4025 : la provenance de la sélection des modules voisins (carte)
    selection: str = ""
    #: #8395 : le pliage du contrat, quand ce paquet porte des fragments
    contract: ContractFold | None = None


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
    #: #8262 : tests rouges sur la tête **et** sur la base exacte du paquet,
    #: même classe d'échec : rouges de la base, défauts du test (canon §3) ;
    #: non concluants, ils ne retirent aucune unité et ne retiennent pas le
    #: paquet. Un lot de déblocage les porte.
    base_red: list[str] = field(default_factory=list)
    #: #8262 : la classe d'échec (`FAIL`/`ERROR`) de chaque rouge de la base,
    #: reportée dans la preuve notée sur le lot de déblocage.
    base_kinds: dict = field(default_factory=dict)
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
    #: #7950 : la série n'a jamais été lancée ; la raison est dite en clair
    #: (« délai global de la série atteint (N s) », « arrêt après le contrat
    #: négatif X »). Une série non lancée n'est ni un module joué ni un
    #: « code de retour None ».
    not_started: str = ""
    #: #7950 : la série a été lancée puis coupée par la borne murale globale
    #: (secondes) : le processus n'a pas rendu de code, mais la série a tourné.
    deadline_cut: int | None = None
    #: #7950 : la série a été lancée puis coupée par sa part du délai global
    #: (secondes), réservée aux modules pas encore partis.
    share_cut: int | None = None

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
        if self.flaky_tests or self.base_red:
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
        if self.flaky_tests or self.base_red:
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
        if self.base_red:
            return ("test(s) rouge(s) aussi sur la base du paquet, même classe "
                    "d'échec (rouge de la base, défaut du test) : %s"
                    % ", ".join(self.base_red[:3]))
        # #7950 : un None ne dit pas la même chose selon que la série n'est
        # jamais partie ou qu'elle a été coupée ; le journal doit le dire.
        if self.not_started:
            return "non lancé : %s" % self.not_started
        if self.deadline_cut is not None:
            return ("coupé par le délai global de la série après %d s "
                    "(série lancée, code non rendu)" % self.deadline_cut)
        if self.share_cut is not None:
            return ("coupé par sa part (%d s) du délai global, réservée aux "
                    "modules suivants" % self.share_cut)
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


def local_commits(repo: Path) -> set[str]:
    """Les commits présents dans les refs locales, sans fetch promissoire.

    #7740 : un SHA de composition factice (banc) ne doit pas déclencher le
    fetch d'un clone partiel ; `rev-list --all` ne lit que le graphe local.
    """
    proc = git(repo, "rev-list", "--all", check=False)
    if proc.returncode != 0:
        return set()
    return {line.strip() for line in _decode(proc.stdout).splitlines()
            if re.fullmatch(r"[0-9a-f]{40,64}", line.strip())}


def unit_prefix_worktree(repo: Path, commit: str, lot: int) -> Path:
    """#7740 : un worktree détaché sur le **préfixe composé** d'une unité.

    `unit.commit` est la fusion cumulative de la composition à cette unité :
    sa série directe y voit les unités antérieures, jamais les suivantes.
    L'arbre est jetable ; l'interpréteur et les dépendances viennent de
    `project_python` / du clone, comme pour les autres arbres de contrôle.
    """
    parent = temporary_parent()
    path = Path(tempfile.mkdtemp(prefix="paquet-auto-unite-", dir=str(parent)))
    path.rmdir()
    proc = git(repo, "worktree", "add", "--detach", str(path), commit,
               check=False)
    if proc.returncode != 0:
        shutil.rmtree(path, ignore_errors=True)
        raise PaquetAutoError(
            "préfixe composé #%d (%s) illisible : %s"
            % (lot, commit[:12], _tail(_decode(proc.stderr), 3)))
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


def composition_text(repo: Path, head: str, name: str, *,
                     target: str | None = None, carrier: int = 0) -> str:
    """#7477: exact receipt first, historical Git rendering on absence."""
    sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "src"))
    from ameesh import packet_snapshot
    text = packet_snapshot.text(checkout=str(repo), head=head,
        branch=package_branch(name), target=target, package_id=carrier or None,
        fallback=lambda: _decode(git(repo, "show",
            "%s:%s" % (head, manifest_rel(name)), check=False).stdout))
    if not text:
        raise InputError("composition illisible à la tête %s" % head)
    return text


def composition_specs(repo: Path, reference: str, name: str, *,
                      target: str | None = None, carrier: int = 0) -> list[tuple[list[int], str]]:
    """#4581 : la table `## Composition` du manifeste gelé à `reference`."""
    sha = rev(repo, reference)
    if sha is None:
        raise InputError("--keep-composition : référence %r introuvable" % reference)
    rel = manifest_rel(name)
    try:
        return parse_composition_table(composition_text(
            repo, sha, name, target=target, carrier=carrier))
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
                       ready_out: Path, *, tolerate_landing: bool = False,
                       note_waiting: bool = False
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

    #7989 : `note_waiting` écrit sur chaque lot validé en attente une note
    idempotente par cause (premier calcul de composition). Jamais sur une
    relecture de gel (`tolerate_landing`) ni un calcul de contrôle.
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
    if note_waiting:
        cmd.append("--note-waiting")
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


def read_carrier(worktree: Path, carrier: int, lots=()) -> dict:
    """#8290 : l'état du porteur relu en base, et les lots de `lots` déjà
    membres d'un **autre** paquet ouvert (`{lot: porteur}`).

    Une lecture impossible n'autorise aucun geste : on n'écrit pas sur un
    état inconnu.
    """
    code = """
import json, sys
from ameesh import config, db, packet_link, work_lifecycle
conn = None
try:
    conn = db.connect(config.load())
    if conn is None:
        raise RuntimeError("base absente pour relire le porteur")
    carrier = int(sys.argv[1])
    lots = [int(value) for value in sys.argv[2:]]
    rows = conn.query("SELECT state, close_reason FROM work_items WHERE id = %s",
                      (carrier,))
    if not rows:
        raise RuntimeError("porteur #%d introuvable" % carrier)
    carried = {}
    for member in packet_link.memberships(conn, item_ids=lots) if lots else ():
        package = int(member.get("package_id") or 0)
        if package and package != carrier and str(
                member.get("package_state") or "") not in work_lifecycle.COMPLETED:
            carried.setdefault(str(int(member["work_item_id"])), package)
    state = str(rows[0].get("state") or "")
    print(json.dumps({"state": state,
                      "close_reason": str(rows[0].get("close_reason") or ""),
                      "terminal": state in work_lifecycle.COMPLETED,
                      "carried": carried}))
finally:
    if conn is not None:
        conn.close()
"""
    proc = run_command([project_python(worktree), "-c", code, str(int(carrier)),
                        *[str(int(lot)) for lot in lots]],
                       cwd=worktree, env=python_env(worktree), timeout=EPOCH_CHECK_TIMEOUT)
    text = _decode(proc.stdout).strip().splitlines()
    try:
        data = json.loads(text[-1]) if proc.returncode == 0 and text else None
    except ValueError:
        data = None
    if not isinstance(data, dict):
        raise PaquetAutoError("porteur #%d illisible : rien n'est écrit : %s"
                              % (carrier, _tail(_output(proc), 5)))
    data["carried"] = {int(lot): int(package) for lot, package
                       in (data.get("carried") or {}).items()}
    return data


def check_carrier_open(worktree: Path, carrier: int) -> None:
    """#8290 : un porteur clos (abandonné, remplacé) ou livré (`delivered`,
    `merged`, `promoted` : `work_lifecycle.COMPLETED`) n'est jamais recomposé,
    poussé ni armé.

    h-20261126-01 : la recomposition du pilote partie sur #8286 ouvert a
    continué ses tests après sa clôture, puis a poussé, ouvert la PR #6524,
    gelé et demandé la ci-full, en parallèle du paquet suivant. Relu dans la
    garde de chaque geste (enregistrement, poussée, PR, gel, ci-full).
    """
    data = read_carrier(worktree, carrier)
    if data.get("terminal") or data.get("state") == "closed":
        raise CarrierClosed("porteur #%d clos (%s) : rien n'est enregistré, "
                            "poussé ni armé"
                            % (carrier, data.get("close_reason")
                               or data.get("state") or "clos"))


def carried_elsewhere(worktree: Path, carrier: int,
                      candidates: list[Candidate]) -> tuple[list[Candidate],
                                                            list[Discarded]]:
    """#8290 : écarte les lots déjà membres d'un autre paquet ouvert."""
    if not candidates:
        return candidates, []
    carried = read_carrier(worktree, carrier,
                           [candidate.lot for candidate in candidates])["carried"]
    kept = [candidate for candidate in candidates if candidate.lot not in carried]
    gone = [Discarded(candidate.lot, CARRIED_ELSEWHERE_REASON,
                      "membre du paquet ouvert #%d (§11 bis)" % carried[candidate.lot])
            for candidate in candidates if candidate.lot in carried]
    return kept, gone


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


def _contract_fragments():
    """Le module de pliage, importé du ``src`` de ce composeur (#8395).

    Le processus parent reste stdlib-only pour un lancement manuel système :
    l'import n'a lieu qu'au pliage, et seulement si le projet porte le
    contrat module v1.
    """
    sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "src"))
    from ameesh import contract_fragments
    return contract_fragments


def legacy_contract_paragraphs(worktree: Path,
                               units: list[Unit]) -> dict[int, tuple[str, ...]]:
    """#8395 : les révisions écrites à l'ancienne manière, par unité.

    Le diff de l'unité de fusion contre sa première parente porte exactement
    ce que le lot a ajouté au contrat ; on n'y lit que les paragraphes
    « Revision 1.N adds … ». Un lot qui a écrit son numéro reste ainsi
    renuméroté mécaniquement, sans regel.
    """
    module = _contract_fragments()
    found: dict[int, tuple[str, ...]] = {}
    for unit in units:
        proc = git(worktree, "diff", "--no-color", "-U0", "%s^" % unit.commit,
                   unit.commit, "--", module.CONTRACT_REL, check=False)
        if proc.returncode != 0:
            raise PaquetAutoError(
                "diff du contrat illisible pour l'unité #%d : %s"
                % (unit.lot, _tail(_decode(proc.stderr))))
        paragraphs = module.added_revision_paragraphs(_decode(proc.stdout))
        if paragraphs:
            found[unit.lot] = paragraphs
    return found


def fold_contract(worktree: Path, base_sha: str, composition: Composition, *,
                  lots: list[int] | None = None, date: str = "",
                  log=print) -> Composition:
    """#8395 : plie les fragments de révision et commite le résultat.

    Après les unités de fusion et avant les contrôles locaux : les numéros
    sont attribués dans l'ordre du manifeste, l'en-tête, la surface et
    ``CONTRACT_VERSION`` sont alignés, les fragments retirés. Le commit est
    déterministe (même arbre, même date ⇒ même contenu) et devient la tête du
    paquet ; le manifeste le cite. Un fragment mal formé refuse la
    composition en nommant le lot. Un arbre sans contrat, ou sans fragment ni
    révision écrite, n'est pas touché.
    """
    module = _contract_fragments()
    contract_path = worktree / module.CONTRACT_REL
    if not contract_path.is_file():
        return composition
    try:
        fragments = module.read_fragments(worktree)
    except module.FragmentError as error:
        raise PaquetAutoError("composition refusée : %s" % error) from error
    legacy = legacy_contract_paragraphs(worktree, composition.units)
    if not fragments and not legacy:
        return composition
    base_show = git(worktree, "show",
                    "%s:%s" % (base_sha, module.CONTRACT_REL), check=False)
    if base_show.returncode != 0:
        raise PaquetAutoError(
            "composition refusée : contrat de la base %s illisible : %s"
            % (base_sha[:12], _tail(_decode(base_show.stderr))))
    try:
        fold = module.plan_fold(
            _decode(base_show.stdout), contract_path.read_text(encoding="utf-8"),
            (worktree / module.SURFACE_REL).read_text(encoding="utf-8"),
            (worktree / module.VERSION_REL).read_text(encoding="utf-8"),
            fragments, legacy,
            [unit.lot for unit in composition.units] if lots is None else lots,
            date or datetime.now().date().isoformat())
    except (module.FragmentError, OSError) as error:
        raise PaquetAutoError("composition refusée : %s" % error) from error
    if not fold.changed:
        return composition
    changed = module.apply_fold(worktree, fold)
    git(worktree, "add", "-A", "--", *changed)
    subjects = ", ".join("%s (#%d)" % (revision.number, revision.lot)
                         for revision in fold.revisions)
    lines = ["contrat module v1 : révisions %s attribuées" % subjects, ""]
    for revision in fold.revisions:
        lines.append("- %s : lot #%d (%s)"
                     % (revision.number, revision.lot,
                        "fragment" if revision.kind == "fragment"
                        else "révision écrite"))
    lines += ["",
              "Pliage mécanique par scripts/paquet-auto.py (#8395) : en-tête, "
              "surface et CONTRACT_VERSION alignés, fragments retirés. Aucun "
              "contenu nouveau : les révisions viennent des fragments relus "
              "avec leurs lots."]
    commit = git(worktree, "commit", "-m", "\n".join(lines), check=False)
    if commit.returncode != 0:
        raise PaquetAutoError(
            "pliage du contrat : git commit a échoué : %s"
            % _tail(_decode(commit.stderr)))
    head = rev(worktree, "HEAD")
    if head is None or head == composition.head:
        raise PaquetAutoError(
            "pliage du contrat : aucun nouveau commit (tête inchangée)")
    composition.head = head
    composition.contract = ContractFold(
        head=head,
        revisions=[(revision.number, revision.lot, revision.kind)
                   for revision in fold.revisions],
        files=changed)
    log("contrat module v1 : révisions %s, commit de composition %s"
        % (subjects, head[:12]))
    return composition


def compose_candidates(worktree: Path, candidates: list[Candidate], carrier: int,
                       *, compose_script: Path, record_package: bool,
                       expect_pushed: str = "",
                       removed: dict[int, str] | None = None,
                       log=print, guard=None,
                       contract_date: str = "") -> Composition:
    """Compose les candidats dans l'ordre ; un conflit est écarté, jamais résolu.

    `removed` (#5084) : lot → source des lots déjà écartés ; un candidat dont
    la source en contient une est écarté à son tour, et un lot écarté ici
    (conflit, contenance) s'y ajoute pour les candidats suivants.

    #8395 : après les unités, les fragments de révision du contrat sont pliés
    dans un commit de composition (`fold_contract`).
    """
    units: list[Unit] = []
    discarded: list[Discarded] = []
    removed = dict(removed or {})
    base = rev(worktree, "HEAD")
    if base is None:
        raise PaquetAutoError("worktree sans commit")
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
    composition = Composition(units=units, discarded=discarded, head=head)
    # #8395 : un lot écarté dont la source est déjà portée par une unité (le
    # contenu est dans l'arbre) garde son fragment ; il est plié à sa place
    # dans l'ordre des candidats. Un fragment qui n'appartient à aucun
    # candidat reste refusé par le pliage.
    unit_lots = {unit.lot for unit in units}
    contract_lots = [
        candidate.lot for candidate in candidates
        if candidate.lot in unit_lots
        or git(worktree, "merge-base", "--is-ancestor", candidate.source,
               head, check=False).returncode == 0]
    return fold_contract(worktree, base, composition, lots=contract_lots,
                         date=contract_date, log=log)


def recompose(worktree: Path, base_sha: str, candidates: list[Candidate],
              carrier: int, *, compose_script: Path, record_package: bool,
              expect_pushed: str = "",
              removed: dict[int, str] | None = None,
              log=print, guard=None,
              contract_date: str = "") -> Composition:
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
                              log=log, guard=guard,
                              contract_date=contract_date)


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


def unit_delta_paths(worktree: Path, unit: Unit) -> list[str]:
    """Les chemins du delta propre de l'unité (`commit^1..commit`).

    La fusion est comparée à sa première parente : c'est le delta de l'unité
    dans le paquet, sans résolution locale, par construction.
    """
    proc = git(worktree, "diff", "--name-only", unit.commit + "^1", unit.commit,
               check=False)
    if proc.returncode != 0:
        raise PaquetAutoError(
            "unité #%d : diff illisible : %s"
            % (unit.lot, _decode(proc.stderr).strip()))
    return [line.strip() for line in _decode(proc.stdout).splitlines()
            if line.strip()]


def unit_test_candidates(worktree: Path, unit: Unit,
                         paths: list[str] | None = None) -> list[str]:
    """#7740 : les modules de test du delta, **sans** filtre d'existence.

    Une unité suivante peut supprimer le test ; la tête finale ne le porte
    alors plus, mais le préfixe de l'unité, si. Le filtre se fait sur ce
    préfixe (`_dedupe_modules`), pas ici.
    """
    return [path[:-3].replace("/", ".")
            for path in (paths if paths is not None
                         else unit_delta_paths(worktree, unit))
            if re.fullmatch(r"tests/test_[A-Za-z0-9_]+\.py", path)]


def unit_test_modules(worktree: Path, unit: Unit, *, tree: Path | None = None
                      ) -> list[str]:
    """Les modules `tests/test_*.py` apportés par la fusion de l'unité.

    Un module supprimé par l'unité, ou absent de `tree` (son préfixe composé,
    #7740), n'est pas exécuté.
    """
    base = tree or worktree
    modules = [module for module in unit_test_candidates(worktree, unit)
               if (base / (module.replace(".", "/") + ".py")).is_file()]
    return _dedupe_modules(base, modules)


def footprint_witnesses(worktree: Path, unit: Unit, *,
                        tree: Path | None = None,
                        paths: list[str] | None = None) -> list[dict]:
    """#7740 : les témoins globaux que l'empreinte de l'unité touche.

    Une migration change les rôles (test_cluster, SuperviseurRoleTest) et le
    schéma (test_migrations*) ; une modification de stockage touche le
    contrat des écrivains ; `alert_briefs.py` porte les alertes de pause
    (test_5424, veille 17:58). `FOOTPRINT_WITNESSES` associe ces préfixes à
    leurs témoins. Ces modules sont joués dans la série de l'unité, sur son
    préfixe composé, même quand la carte d'impact ne les sélectionne pas.
    `tree` est ce préfixe : un témoin absent de l'arbre n'est pas joué.
    """
    paths = unit_delta_paths(worktree, unit) if paths is None else paths
    base = tree or worktree
    rows: list[dict] = []
    seen: set[str] = set()
    for prefixes, witnesses in FOOTPRINT_WITNESSES:
        files = [path for path in paths if path.startswith(prefixes)]
        if not files:
            continue
        for module in witnesses:
            if module in seen:
                continue
            if (base / (module.replace(".", "/") + ".py")).is_file():
                seen.add(module)
                rows.append({"module": module, "reason": "empreinte",
                             "files": files[:3]})
    return rows


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
    walled = {"wall": wall} if wall is not None else {}
    found = base_failure_kinds(worktree, base_sha, modules, timeout=timeout,
                               **walled)
    return None if found is None else set(found)


def base_failure_kinds(worktree: Path, base_sha: str, modules: list[str], *,
                       timeout: int,
                       wall: int | None = None) -> dict[str, str] | None:
    """#8262 : `{test: classe d'échec}` des `modules` rouges sur la base.

    Même rejeu que `base_failures` (worktree détaché de la base exacte, même
    pilote `test_driver` et même environnement que les séries du paquet) ;
    None si le rejeu n'est pas concluant (base absente, coupé, rc inattendu).
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
            return {}
        walled = {"wall": wall} if wall is not None else {}
        run = run_tests(path, "base", present, timeout=timeout, **walled)
        if run.inconclusive:
            return None
        return failure_kinds(run.output, _unusable(run))
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
                seconds=time.monotonic() - start, timed_out=True,
                deadline_cut=wall))
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
        # #7950 : les mêmes distinctions que TestRun avant le délai générique.
        if self.not_started:
            return "non lancé : %s" % self.not_started
        if self.deadline_cut is not None:
            return ("coupé par le délai global de la série après %d s "
                    "(série lancée, code non rendu)" % self.deadline_cut)
        if self.share_cut is not None:
            return ("coupé par sa part (%d s) du délai global, réservée aux "
                    "modules suivants" % self.share_cut)
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
        run = budget_spent("contrats", modules,
                           budget=max(0.0, end - start))
        return FlowTestRun(run.label, run.modules, None, run.output, 0.0,
                           timed_out=True, not_started=run.not_started)
    launcher = worktree / tooling.parts_script
    if not launcher.is_file():
        return save_test_log(FlowTestRun(
            "contrats", list(modules), None,
            "%s absent de l'arbre composé" % tooling.parts_script, 0.0,
            timed_out=True,
            not_started="lanceur de contrats %s absent de l'arbre composé"
                        % tooling.parts_script))
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
            time.monotonic() - start, timed_out=True, flow=parsed,
            deadline_cut=remaining))
    capped = bool(wrapper) and proc.returncode == HEAVY_CAPPED_EXIT
    output = _output(proc)
    if parsed["failed"]:
        output = "\n".join(["échecs (flux) : " + ", ".join(parsed["failed"]), output])
    return save_test_log(FlowTestRun(
        "contrats", list(modules), proc.returncode, output,
        time.monotonic() - start, timed_out=capped, flow=parsed))


def contract_tail_share(remaining: int, pending_after: int,
                        budget: float, parallel: int = 1) -> int:
    """#7950/#8207 : la part d'un contrat quand la borne globale va couper la passe.

    Hors de la fin de passe, le module garde le temps restant : les contrats
    mesurés (les plus longs d'abord) ont besoin de leur durée entière. Quand
    il reste moins d'un cinquième de la borne globale (et moins de
    `CONTRACT_TAIL_MIN_S` secondes), le temps restant est réparti entre les
    modules pas encore partis : un module long ne consomme plus la borne des
    suivants (h-07 : test_7665, puis six modules jamais lancés).

    #8207 : à plusieurs lanes, répartir par module ignorait le parallélisme.
    La part se calcule par vague (`ceil(pending / parallel)`) et un module
    peut réclamer jusqu'à `CONTRACT_TAIL_MODULE_S` secondes — sans jamais
    dépasser la moitié du reste — pour qu'un contrat de 20 à 60 s ne soit
    pas coupé alors que la borne a encore 100 à 150 s de marge. À une seule
    lane, la répartition égale d'origine est conservée.
    """
    if pending_after <= 0 or remaining > max(CONTRACT_TAIL_MIN_S,
                                             int(budget) // 5):
        return remaining
    if parallel <= 1:
        return max(1, remaining // (pending_after + 1))
    waves = max(1, (pending_after + 1 + parallel - 1) // parallel)
    part = max(1, remaining // waves)
    minimum = min(CONTRACT_TAIL_MODULE_S,
                  max(remaining // 2, remaining - CONTRACT_TAIL_MIN_S))
    return min(remaining, max(part, minimum))


def run_contract_modules(worktree: Path, modules: list[str], *,
                         end: float, parallel: int,
                         tooling: Tooling | None = None) -> TestRun:
    """One isolated heavy process per module under a shared wall deadline."""
    if not modules:
        return ContractTestRun("contrats", [], None, "aucun contrat sélectionné", 0.0)
    tooling = tooling or _TOOLING
    if tooling.declared:
        return run_declared_contracts(worktree, modules, end=end, tooling=tooling)
    start = time.monotonic()
    budget = max(0.0, end - start)
    stop = threading.Event()
    state_lock = threading.Lock()
    state = {"started": 0, "negative": ""}

    def module_job(module):
        def run():
            with state_lock:
                state["started"] += 1
                pending_after = max(0, len(modules) - state["started"])
            if stop.is_set():
                with state_lock:
                    culprit = state["negative"]
                reason = ("arrêt après le contrat négatif %s" % culprit
                          if culprit else "arrêt après un contrat négatif")
                # #7950 : jamais lancé ; ce n'est pas un « code de retour
                # None » et ce module ne compte pas comme joué.
                return TestRun("contrats", [module], None,
                               reason + " : module non joué", 0.0,
                               not_started=reason)
            remaining = int(end - time.monotonic())
            if remaining <= 0:
                return budget_spent("contrats", [module], budget=budget)
            share = contract_tail_share(remaining, pending_after, budget,
                                        parallel)
            result = run_tests(worktree, "contrats " + module, [module],
                               timeout=share, heavy=True, wall=remaining,
                               failfast=True)
            if share < remaining and result.timed_out \
                    and result.deadline_cut is None:
                # #7950 : coupé par la part de fin de passe, pas par une
                # borne de série ordinaire.
                result.share_cut = share
            if result.real_failure:
                with state_lock:
                    if not stop.is_set():
                        state["negative"] = module
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
                                 timeout: float, project: str = "", target: str | None = None,
                                 carrier: int = 0) -> tuple[dict, Path, list, int | None]:
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
    from ameesh import packet_snapshot
    branch = _decode(git(worktree, "symbolic-ref", "--short", "-q", "HEAD",
                         check=False, timeout=remaining()).stdout).strip()
    head = rev(worktree, "HEAD")
    receipt = (packet_snapshot.lookup(checkout=str(worktree), head=head, branch=branch,
                                      target=target, package_id=carrier or None)
               if head and branch.startswith("integration/") else None)
    if receipt:
        for unit in receipt["payload"]["units"]:
            lots.append({"id": str(unit["work_item_id"]),
                         "sha": unit.get("source_sha") or unit["unit_sha"]})
        remaining()
        return scope, path, lots, max_age
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
                             project: str = "", target: str | None = None) -> ContractPass:
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
        from ameesh import db as ameesh_db, packet_snapshot, prevalidation
        try:
            from ameesh.canon import GitDeadline
            try:
                context = contract_attestation_context(
                    worktree, base=base, modules=modules, timeout=end - time.monotonic(), project=project,
                    target=target, carrier=carrier)
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
        except (OSError, ValueError, PaquetAutoError, SystemExit,
                packet_snapshot.SnapshotError, ameesh_db.DbError) as exc:
            # The attestation is an optimization: an unreadable snapshot only
            # disables reuse, it never blocks the composition.
            context = None
            print("attestation contrats indisponible : " + str(exc), flush=True)
        remaining = int(end - time.monotonic())
        if cached is not None:
            run = TestRun("contrats", modules, 0,
                          "Attestation réutilisée, exécution originale : "
                          + cached["attestation"]["payload"]["source_sha"]
                          + "\nRan %d tests in 0.0s\nOK\n" % cached.get("tests", 0), 0.0)
        elif remaining <= 0:
            run = budget_spent("contrats", modules,
                               budget=max(0.0, end - start))
        else:
            run = run_contract_modules(worktree, modules, end=end, parallel=parallel)
    except PaquetAutoError as exc:
        # #7950 : la passe n'a pas démarré (budget, créneau, arbre) : ses
        # modules sont « non lancés », jamais un « code de retour None ».
        run = save_test_log(TestRun("contrats", modules, None, str(exc),
                                   time.monotonic() - start, timed_out=True,
                                   not_started=str(exc)))
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
                      and not child.skipped and child.tests > 0 and not child.flaky_tests
                      and not child.base_red):
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
    `plan` le refuse, mais le composeur ne le voyait qu'en CI. Ici :

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


def failure_kinds(output: str,
                  unusable_engines: frozenset = frozenset()) -> dict[str, str]:
    """#8262 : la classe d'échec (`FAIL`, `ERROR`) de chaque test rouge.

    Mêmes noms et mêmes exclusions d'environnement que `unittest_failures` :
    un rouge de la base ne vaut pour un rouge du paquet que si c'est le même
    test **et** la même classe d'échec.
    """
    out: dict[str, str] = {}
    for match in FAIL_LINE_RE.finditer(output or ""):
        kind, test = match.group(1), match.group(2).strip()
        if not test or test in out:
            continue
        if kind == "ERROR":
            rest = (output or "")[match.end():]
            end = BLOCK_END_RE.search(rest)
            block = rest[:end.start()] if end else rest
            if TestRun._error_is_environment(block, unusable_engines):
                continue
        out[test] = kind
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
                    failures: list[dict], *, timeout: int, start: int = 1,
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

    #7239 : `start` (1 par défaut) commence au préfixe de l'unité d'indice
    `start` et ne rejoue pas celles qui précèdent ; les tests sont alors
    situés au premier rouge entre cette unité et la tête. Sert à confirmer un
    retrait #3801 sur le préfixe de l'unité désignée, sans rejouer avant elle.

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
    start = max(1, min(int(start or 1), len(composition.units)))
    replay = replay or run_tests
    parent = temporary_parent()
    path = Path(tempfile.mkdtemp(prefix="paquet-auto-prefixe-", dir=str(parent)))
    path.rmdir()
    first = composition.units[start - 1].commit
    added = git(worktree, "worktree", "add", "--detach", str(path), first,
                check=False)
    if added.returncode != 0:
        shutil.rmtree(path, ignore_errors=True)
        return {}, ("worktree de préfixe impossible : %s"
                    % _tail(_decode(added.stderr), 3)), []
    try:
        unresolved = tests
        found: dict[int, list[dict]] = {}
        for index, unit in enumerate(composition.units[start - 1:],
                                     start=start):
            if index > start and guard is not None:
                guard()
            limit = series_timeout(timeout, deadline)
            if limit is None:
                raise BudgetSpent("borne totale atteinte avant le préfixe %d "
                                  "(#%d)" % (index, unit.lot))
            if index > start:
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

    Pour chaque retrait, quelle que soit sa preuve, dans l'ordre de
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
            if earlier is None or not names \
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


def designated_prefix(worktree: Path, composition: Composition,
                      removals: list[Removal], *, timeout: int,
                      prefix=None, log=print, guard=None,
                      deadline: float | None = None,
                      ) -> tuple[list[Removal], list[dict], str]:
    """#7239 : la couverture #3801 seule ne retire plus une unité innocente.

    Paquet h-20261113-11 : #7066 (unité 1) était désignée par la couverture
    pour un contrat que seul #7108 (unité 2) cassait, et #7229 (unité 3) pour
    un test rouge sous charge qui passait au rejeu. Avant un retrait #3801,
    les tests rouges sont rejoués sur le préfixe qui s'arrête à l'unité
    désignée, puis sur les préfixes suivants (`prefix_failures(start=…)`,
    injectable par `prefix`) :

    * rouge dès le préfixe de l'unité : le retrait est confirmé, gardé tel
      quel (preuve #3801) ;
    * vert au préfixe de l'unité et rouge plus loin : l'unité est innocente ;
      son retrait est abandonné et la première unité dont le préfixe le rend
      rouge est retirée à sa place (preuve `prefixes`, comme #4217) ;
    * vert sur tous les préfixes jusqu'à la tête : le test est instable
      (#4221), consigné non concluant, sans retrait.

    #7692 : un rejeu non concluant (worktree impossible, délai, run illisible)
    ne confirme pas le retrait #3801 : il n'est **pas appliqué** (canon §3, un
    non-concluant ne retient rien). L'unité reste dans la composition et la
    ci-full tranche ; le test est consigné non concluant. Les retraits d'une
    autre preuve passent inchangés. Rend `(retraits, tests instables,
    pourquoi)` ; `pourquoi` non vide signale un rejeu non concluant dont le
    retrait a été abandonné (l'appelant consigne les tests qu'il n'a pas
    confirmés).
    """
    subjects: list[Removal] = []
    untouched: list[Removal] = []
    for removal in removals:
        if removal.proof == "3801" and removal.tests and all(removal.tests):
            subjects.append(removal)
        else:
            untouched.append(removal)
    if not subjects:
        return removals, [], ""
    positions = {unit.lot: index for index, unit in enumerate(
        composition.units, start=1)}
    groups: dict[int, list[Removal]] = {}
    for removal in subjects:
        position = positions.get(removal.lot)
        if position is None:
            untouched.append(removal)
            continue
        groups.setdefault(position, []).append(removal)
    replay = prefix or prefix_failures
    confirmed: list[Removal] = []
    later: list[Removal] = []
    flaky: list[dict] = []
    why = ""
    for position in sorted(groups):
        group = groups[position]
        limit = series_timeout(timeout, deadline)
        if limit is None:
            why = ("borne totale atteinte avant le préfixe de #%d"
                   % group[0].lot)
            log("tests ciblés : rejeu du préfixe de #%d NON CONCLUANT (%s) — "
                "retrait #3801 NON appliqué (canon §3), l'unité reste"
                % (group[0].lot, why))
            continue
        if guard is not None:
            guard()
        names: list[str] = []
        for removal in group:
            for test in removal.tests:
                if test not in names:
                    names.append(test)
        bounds = ({"guard": guard} if guard is not None else {}) | (
            {"deadline": deadline} if deadline is not None else {})
        try:
            found, group_why, group_flaky = replay(
                worktree, composition,
                [{"test": name} for name in names], timeout=timeout,
                start=position, log=log, **bounds)
        except BudgetSpent as spent:
            why = str(spent)
            log("tests ciblés : rejeu du préfixe de #%d NON CONCLUANT (%s) — "
                "retrait #3801 NON appliqué (canon §3), l'unité reste"
                % (group[0].lot, spent))
            continue
        if group_why:
            why = group_why
            log("tests ciblés : rejeu du préfixe de #%d NON CONCLUANT (%s) — "
                "retrait #3801 NON appliqué (canon §3), l'unité reste"
                % (group[0].lot, group_why))
            continue
        # `found` est indexé par numéro de lot, pas par position (#7239 B) :
        # comparer les positions, garder les lots dans les retraits et les
        # mentions.
        by_test = {_failure_name(failure): lot
                   for lot, failures in found.items() for failure in failures}
        flaky_names = {_failure_name(failure) for failure in group_flaky}
        later_by_lot: dict[int, list[str]] = {}
        for removal in group:
            keep: list[str] = []
            for test in removal.tests:
                lot = by_test.get(test)
                at = positions.get(lot) if lot is not None else None
                if at == position:
                    keep.append(test)
                elif at is not None and at > position:
                    later_by_lot.setdefault(lot, []).append(test)
                elif test in flaky_names:
                    flaky.append({"test": test})
                else:
                    # ni confirmé ni situé : aucune preuve, aucun retrait
                    # (canon §3) ; l'appelant le consigne non concluant.
                    why = why or ("test %s ni confirmé ni situé sur les "
                                  "préfixes" % test)
                    log("tests ciblés : #%d désignée (#3801) mais %s ni "
                        "confirmé ni situé sur les préfixes — retrait non "
                        "appliqué (canon §3), l'unité reste"
                        % (removal.lot, test))
            if keep:
                confirmed.append(Removal(
                    lot=removal.lot, tests=keep,
                    evidence=list(removal.evidence), attributed=True,
                    proof="3801"))
            for test in removal.tests:
                lot = by_test.get(test)
                at = positions.get(lot) if lot is not None else None
                if at is not None and at > position:
                    log("tests ciblés : #%d désignée (#3801) mais %s rouge au "
                        "préfixe de #%d, vert au sien — unité innocente, "
                        "retrait de #%d à sa place"
                        % (removal.lot, test, lot, lot))
                elif test in flaky_names:
                    log("tests ciblés : #%d désignée (#3801) mais %s vert sur "
                        "tous les préfixes depuis son unité (instable) — "
                        "consigné non concluant, sans retrait"
                        % (removal.lot, test))
        designated_lot = composition.units[position - 1].lot
        for lot, tests in later_by_lot.items():
            later.append(Removal(
                lot=lot, tests=tests, attributed=True, proof="prefixes",
                evidence=["préfixe base+u%d (rouge à #%d, vert au préfixe "
                          "de #%d)" % (positions[lot], lot, designated_lot)]))
    if flaky:
        names = sorted({_failure_name(failure) for failure in flaky
                        if _failure_name(failure)})
        log("tests ciblés : %d test(s) rouge(s) sur la tête mais vert(s) au "
            "rejeu des préfixes depuis l'unité (instable) : %s — consigné(s) "
            "non concluant(s), le paquet continue"
            % (len(names), ", ".join(names[:5])))
    return _merge_removals(untouched, confirmed, later), flaky, why


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


def budget_spent(label: str, modules: list[str], *,
                 budget: float | None = None) -> TestRun:
    """#4463/#7950 : une série qui ne part plus, l'échéance totale passée (§3).

    Elle est dite **non lancée**, avec la durée de la borne quand elle est
    connue : un None ne se lit plus comme un module joué (« code de retour
    None ») mais comme une série qui n'a jamais démarré (#7950).
    """
    spent = " (%d s de budget)" % int(budget) if budget else ""
    reason = "délai global de la série atteint" + spent
    return TestRun(label=label, modules=list(modules), returncode=None,
                   output="borne totale des tests par unité atteinte : série "
                          "non jouée (#4463)" + spent, seconds=0.0,
                   timed_out=True, direct=list(modules), not_started=reason)


def _consign_flaky(red: list[tuple[TestRun, list[dict]]],
                   runs: list[TestRun], names: list[str], *,
                   base: bool = False,
                   kinds: dict | None = None) -> list[tuple[TestRun, list[dict]]]:
    """#4221/#7239 : consigne des tests verts au rejeu comme non concluants.

    Retire de `red` les échecs dont le nom est dans `names` : une série qui ne
    garde aucun rouge devient non concluante (`flaky_tests`), une série mixte
    est scindée en un rouge réel et une série instable consignée à part. Rend
    `red` sans ces échecs ; `runs` porte les séries non concluantes.

    #8262 : `base=True` consigne de même des rouges de la base (`base_red`),
    avec leur classe d'échec (`kinds` → `base_kinds`).
    """
    flaky_names = set(names)
    field_name = "base_red" if base else "flaky_tests"
    kept: list[tuple[TestRun, list[dict]]] = []
    for run, found in red:
        names_here = sorted({_failure_name(failure) for failure in found
                             if _failure_name(failure) in flaky_names})
        left = [failure for failure in found
                if _failure_name(failure) not in flaky_names]
        if len(left) == len(found):
            kept.append((run, found))
            continue
        if left:
            # des rouges réels restent dans la même série : la série reste
            # rouge, l'instable est consigné à part.
            kept.append((run, left))
            runs.append(TestRun(
                label="%s (%s)" % (run.label,
                                   "rouge de la base" if base else "instable"),
                modules=sorted({"tests." + str(failure.get("module"))
                                for failure in found
                                if failure.get("module")
                                and _failure_name(failure) in names_here}),
                returncode=1,
                output=("tests rouges sur la tête et sur la base du paquet, "
                        "même classe d'échec : %s" if base else
                        "tests rouges sur la tête mais verts sur tous les "
                        "préfixes : %s") % ", ".join(names_here),
                seconds=0.0, log_path=run.log_path,
                base_kinds={name: (kinds or {}).get(name, "")
                            for name in names_here} if base else {},
                **{field_name: names_here}))
        else:
            setattr(run, field_name, names_here)
            if base:
                run.base_kinds = {name: (kinds or {}).get(name, "")
                                  for name in names_here}
    return kept


def _owned_base_red(worktree: Path, red: list[tuple[TestRun, list[dict]]],
                    pending: list, contract: TestRun, *, base_sha: str,
                    skip: set[str], timeout: int, deadline: float | None,
                    guard, log, on_base) -> dict[str, str]:
    """#8262 : les rouges propres/de contrat aussi rouges sur la base.

    Les rouges de voisins ont déjà leur rejeu de base (#4025) et sont exclus,
    comme les rouges consignés non attribués (`skip`) et les identités
    opaques d'un lanceur déclaré (`FlowTestRun`, sans rejeu `unittest`).
    Rend `{test: classe}` des rouges sur la tête **et** sur la base avec la
    même classe d'échec ; {} si le rejeu n'est pas concluant (canon §3 : rien
    n'est classé, le geste antérieur s'applique).
    """
    neighbors = {id(run): neighbor
                 for _unit, run, _found, _proper, _mine, neighbor in pending}
    head_kinds: dict[str, str] = {}
    for run, found in red:
        if isinstance(run, FlowTestRun):
            continue
        kinds = failure_kinds(run.output, _unusable(run))
        neighbor = neighbors.get(id(run), set()) if run is not contract else set()
        for failure in found:
            name, module = _failure_name(failure), failure.get("module")
            if not name or not module or name in skip or module in neighbor:
                continue
            if name in kinds:
                head_kinds.setdefault(name, kinds[name])
    if not head_kinds or not base_sha:
        return {}
    if guard is not None:
        guard()
    limit = series_timeout(timeout, deadline)
    if limit is not None and deadline is not None:
        # même partage que le rejeu des voisins (#4887) : la moitié du temps
        # restant, le reste aux préfixes qui attribuent ce qui reste rouge.
        left = max(1, int(deadline - time.monotonic()))
        limit = min(limit, max(30, left // 2))
    walled = {"wall": limit} if deadline is not None and limit else {}
    modules = sorted({"tests." + str(TEST_MODULE_RE.search(name).group(1))
                      for name in head_kinds if TEST_MODULE_RE.search(name)})
    at_base = (None if limit is None else
               on_base(worktree, base_sha, modules, timeout=limit, **walled))
    if at_base is None or not isinstance(at_base, dict):
        # un ensemble de noms sans classe d'échec ne prouve pas le même rouge
        log("tests ciblés : rejeu sur la base %s des rouges propres/de "
            "contrat non concluant — attribution inchangée"
            % (base_sha[:12] or "?"))
        return {}
    same = {name: kind for name, kind in sorted(head_kinds.items())
            if at_base.get(name) == kind}
    other = sorted(name for name, kind in head_kinds.items()
                   if name in at_base and at_base[name] != kind)
    if other:
        log("tests ciblés : %d test(s) rouge(s) sur la base avec une autre "
            "classe d'échec (non concluant pour la base, attribution "
            "inchangée) : %s" % (len(other), ", ".join(other[:5])))
    log("tests ciblés : rouges propres/de contrat rejoués sur la base %s : "
        "%d sur %d aussi rouge(s), même classe d'échec"
        % (base_sha[:12], len(same), len(head_kinds)))
    return same


def check_local_tests(worktree: Path, composition: Composition, *,
                      timeout: int, log=print,
                      attribute=None, base_sha: str = "",
                      select=None, on_base=None, guard=None,
                      prefix=None, earlier=None, confirm=None,
                      parallel: int = DEFAULT_PARALLEL,
                      deadline: float | None = None,
                      contract_report: ContractPass | None = None,
                      legacy_contracts: bool = False,
                      unit_tree=None, owned_on_base=None,
                      ) -> tuple[list[TestRun], list[int], list[Removal]]:
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

    #7239 : avant un retrait #3801, ses tests rouges sont rejoués sur le
    préfixe de l'unité puis au-delà (`designated_prefix`, injectable par
    `confirm`). Préfixe vert = unité innocente (le premier préfixe rouge
    suivant est retiré à sa place) ; vert partout = instable consigné
    (#4221) ; rouge dès son préfixe = retrait confirmé. #7692 : un rejeu non
    concluant ne confirme pas le retrait — il n'est pas appliqué (canon §3),
    l'unité reste et le test est consigné non concluant ; la ci-full tranche.

    #4025 : la série d'une unité joue aussi ses modules voisins
    (`neighbor_modules`, injectable par `select`). Dans le geste antérieur,
    un rouge d'un module `carte` ne désigne l'unité que si aucune autre ne
    l'a sélectionné ; un rouge partagé reste à l'attribution et à la ci-full.
    Un rouge de module voisin aussi rouge sur la base (`base_failures`,
    injectable par `on_base`) ne compte pas : environnement ou préexistant.

    #8262 : un rouge **propre** ou de **contrat** est rejoué de même sur la
    base exacte du paquet avant tout retrait (`base_failure_kinds`,
    injectable par `owned_on_base`). Rouge aussi sur la base, même test et
    même classe d'échec (`FAIL`/`ERROR`) : rouge de la base, défaut du test
    (canon §3), consigné non concluant (`base_red`), sans retrait ni arrêt.
    Classe différente, rejeu coupé ou non concluant : rien n'est classé, le
    geste antérieur s'applique (h-09/h-10 du 24/11 : test_7652, rouge sur
    main, imputé aux unités).

    #7740 : chaque série d'unité est jouée sur son **préfixe composé**
    (`unit.commit`, l'empilement jusqu'à elle), dans un worktree détaché
    (`unit_tree`, injectable), et non sur la tête finale : un conflit de sens
    avec une unité antérieure est vu à l'unité qui le crée. La série reçoit
    `timeout` et non une part de `deadline` ; `deadline` ne la coupe qu'en
    temps mural, à la borne totale. Les témoins globaux de l'empreinte
    (`footprint_witnesses` : migration, stockage, alertes de pause)
    rejoignent ses modules. Une série sans module, ou dont le préfixe est
    indisponible, est un non-concluant nommé, jamais un vert.

    #4271 : avec `parallel` > 1, les séries des unités et celle des contrats
    sont indépendantes et jouées en même temps (au plus `parallel`), chacune
    dans un créneau `ameesh heavy` de l'hôte ; leurs résultats sont lus dans
    l'ordre des unités, comme en série. `guard` est relu avant chaque série.

    #4463 : `deadline` (horloge monotone) borne la durée totale : chaque série
    est bornée au temps restant ; une série qui n'est pas partie à l'échéance
    est non concluante sans être jouée. L'attente d'un créneau `ameesh heavy`
    compte dans ce temps (`run_tests(wall=…)`) : à l'échéance la série est
    arrêtée, au plus `WALL_GRACE_S` après. #7195 : pour une série `heavy`,
    cette attente ne consomme pas sa borne d'exécution (`timeout`, passée à
    `ameesh heavy --max-duration`). #7740 : la série directe n'a pas de part
    de `deadline` ; `timeout` borne son exécution et le temps restant son
    temps mural.
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
    factory = unit_tree or unit_prefix_worktree
    planned: list[tuple[Unit, list[str], list[dict], list[str], Path,
                        Path | None, TestRun | None]] = []
    prefixes: list[Path] = []
    local = local_commits(worktree)
    try:
        for unit in composition.units:
            prefix_path: Path | None = None
            error_run: TestRun | None = None
            base_tree = worktree
            proper: list[str] = []
            added: list[dict] = []
            if unit.commit in local:
                paths = unit_delta_paths(worktree, unit)
                proper_candidates = unit_test_candidates(worktree, unit, paths)
                witness_rows = footprint_witnesses(worktree, unit, paths=paths)
                rows = neighbors.get(unit.lot, [])
                if proper_candidates or rows or witness_rows:
                    try:
                        prefix_path = factory(worktree, unit.commit, unit.lot)
                        prefixes.append(prefix_path)
                        base_tree = prefix_path
                    except PaquetAutoError as exc:
                        log("tests unité #%d : préfixe composé %s indisponible "
                            "(%s) : série non concluante"
                            % (unit.lot, unit.commit[:12], exc))
                        error_run = TestRun(
                            "unité #%d" % unit.lot, [], None,
                            "préfixe composé %s indisponible : %s"
                            % (unit.commit[:12], exc), 0.0, timed_out=True)
                if prefix_path is not None:
                    # Un chemin supprimé par l'unité reste dans le delta : il
                    # n'existe pas sur son préfixe et n'est donc pas joué.
                    proper = [module for module in proper_candidates
                              if (prefix_path
                                  / (module.replace(".", "/") + ".py")).is_file()]
                    proper = _dedupe_modules(prefix_path, proper)
                    for row in rows + witness_rows:
                        if row["module"] in proper:
                            continue
                        path = prefix_path / (row["module"].replace(".", "/")
                                              + ".py")
                        if not path.is_file():
                            continue
                        if all(row["module"] != done["module"] for done in added):
                            added.append(row)
                elif error_run is None:
                    # Aucun module direct à jouer : le préfixe n'est pas créé.
                    proper, added = [], []
            else:
                # Un SHA de composition factice (banc, témoin) n'a pas de
                # préfixe : la série reste sur l'arbre composé, comme avant.
                log("tests unité #%d : préfixe %s introuvable, série sur "
                    "l'arbre composé (banc)" % (unit.lot, unit.commit[:12]))
                proper = unit_test_modules(worktree, unit)
                for row in neighbors.get(unit.lot, []):
                    if row["module"] in proper:
                        continue
                    if all(row["module"] != done["module"] for done in added):
                        added.append(row)
            modules = proper + [row["module"] for row in added]
            if not modules:
                # #7740 : 0 module joué n'est jamais une inclusion verte.
                log("tests unité #%d : aucun module direct sélectionné (non "
                    "concluant, canon §3)" % unit.lot)
                error_run = error_run or TestRun(
                    "unité #%d" % unit.lot, [], None,
                    "aucun module direct sélectionné : non concluant (#7740)",
                    0.0, timed_out=True)
            if added:
                log("tests unité #%d : %d module(s) voisin(s)/témoin(s) : %s"
                    % (unit.lot, len(added),
                       ", ".join("%s (%s)" % (row["module"], row["reason"])
                                 for row in added)))
            planned.append((unit, proper, added, modules, base_tree,
                            prefix_path, error_run))
    except BaseException:
        for path in prefixes:
            remove_worktree(worktree, path)
        raise

    def bounded(label: str, modules: list[str], *, tree: Path | None = None,
                direct: bool = False) -> TestRun:
        if direct:
            # #7740 : la série directe ne prend pas une part de la borne
            # totale (c'est cette part qui la coupait avant d'avoir joué) ;
            # elle est bornée par `--tests-timeout`, et par le temps restant
            # de la borne totale en temps mural. Une série non partie ou
            # coupée reste non concluante, nommée.
            # #7888 : ce temps mural s'arrête avant la réserve #4887. Sans
            # elle, les séries couraient jusqu'à la borne, le rejeu #3801 d'un
            # contrat rouge (h-20261119-12 : test_cluster, #7865) n'avait plus
            # une seconde, restait non concluant (#7692) et le rouge connu
            # partait en CI.
            limit = timeout
            walled = {}
            if deadline is not None:
                remaining = max(0, int(deadline - time.monotonic()) - reserve)
                if remaining <= 0:
                    log("tests %s : borne totale atteinte, série non jouée "
                        "(non concluant)" % label)
                    return budget_spent(label, modules)
                walled = {"wall": remaining}
            log("tests %s : série directe sur son préfixe composé, borne %d s%s"
                % (label, limit,
                   (" (temps total restant %d s)" % walled["wall"])
                   if walled else ""))
            run = run_tests(tree or worktree, label, modules, timeout=limit,
                            **slots, **walled)
            run.direct = list(modules)
            return run
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
        # tests, la borne restant le temps restant jusqu'à l'échéance.
        walled = {}
        if deadline is not None:
            wall = max(1, int(deadline - time.monotonic())) if slots else limit
            walled = {"wall": wall}
        run = run_tests(worktree, label, modules, timeout=limit, **slots, **walled)
        run.direct = list(modules)
        return run

    def unit_job(label: str, modules: list[str], tree: Path,
                 error: TestRun | None):
        if error is not None:
            return lambda: error
        return lambda: bounded(label, modules, tree=tree, direct=True)

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

    jobs = [unit_job("unité #%d" % unit.lot, modules, tree, error)
            for unit, _proper, _added, modules, tree, _prefix, error in planned]
    if contract_run is None:
        jobs.append(contract_job)
    # #4887 : la part de chaque série est calculée au départ, pas à la
    # composition : une série qui finit tôt rend sa part aux suivantes. Une
    # réserve reste pour le rejeu d'un rouge de voisin (base, préfixes), sans
    # quoi la part des séries le coupe et le vrai rouge n'est pas attribué.
    remaining = {"left": 1 if contract_run is None else 0}
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
        log("tests ciblés : %d série(s) directe(s) sur leur préfixe, %d à la "
            "fois, créneaux `ameesh heavy`, borne %d s par série (#7740)"
            % (len(planned), min(parallel, max(1, len(jobs))), timeout))
    try:
        results = _run_series(jobs, parallel=parallel, guard=guard)
    finally:
        for path in prefixes:
            remove_worktree(worktree, path)
    contract = results.pop() if contract_run is None else contract_run
    pending: list[tuple[Unit, TestRun, list[dict], set[str], set[str],
                        set[str]]] = []
    for (unit, proper, added, modules, _tree, _prefix, _error), run in zip(
            planned, results):
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
            # #7740 : un témoin d'empreinte (migration, stockage, alertes de
            # pause) est une preuve du lot lui-même, pas un voisin de la
            # carte : son rouge s'attribue au lot, sans rejeu de base.
            neighbor = {row["module"].split(".", 1)[1] for row in added
                        if row["reason"] != "empreinte"}
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
    base_red = _owned_base_red(
        worktree, red, pending, contract, base_sha=base_sha,
        skip=unattributed, timeout=timeout, deadline=deadline, guard=guard,
        log=log, on_base=owned_on_base or base_failure_kinds)
    if base_red:
        log("tests ciblés : %d rouge(s) propre(s)/de contrat aussi rouge(s) "
            "sur la base %s, même classe d'échec : rouge(s) de la base, "
            "consigné(s) non concluant(s) (canon §3), aucun retrait : %s"
            % (len(base_red), base_sha[:12], ", ".join(sorted(base_red)[:5])))
        red = _consign_flaky(red, runs, sorted(base_red), base=True,
                             kinds=base_red)
        gone = set(base_red)
        own = {lot: [failure for failure in found
                     if _failure_name(failure) not in gone]
               for lot, found in own.items()}
        own = {lot: found for lot, found in own.items()
               if found or lot in unparsed_lots}
        contract_left = [found for run, found in red if run is contract]
        contract_red = bool(contract_left and contract_left[0])
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
            red = _consign_flaky(red, runs, flaky_names)
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
    designated_before = [(removal.lot, test)
                         for removal in removals
                         if removal.proof == "3801"
                         for test in removal.tests]
    if any(removal.proof == "3801" for removal in removals):
        # #7239 : le préfixe de l'unité désignée par la couverture tranche.
        if guard is not None:
            guard()
        bounds = ({"guard": guard} if guard is not None else {}) | (
            {"deadline": deadline} if deadline is not None else {})
        try:
            removals, flaky_designated, designation_why = (
                confirm or designated_prefix)(
                    worktree, composition, removals, timeout=timeout,
                    log=log, **bounds)
        except BudgetSpent as spent:
            flaky_designated = []
            designation_why = str(spent)
            # #7692 : sans rejeu concluant, aucun retrait #3801 n'est appliqué.
            removals = [removal for removal in removals
                        if removal.proof != "3801"]
        flaky_names = sorted({_failure_name(failure)
                              for failure in flaky_designated
                              if _failure_name(failure)})
        if designation_why:
            log("tests ciblés : rejeu du préfixe de l'unité NON CONCLUANT "
                "(%s) — retrait #3801 NON appliqué (canon §3), l'unité reste"
                % designation_why)
        # #7692 : quand le retrait #3801 tombe et qu'aucune autre preuve ne
        # reste, le repli du rouge propre (geste antérieur) s'applique encore :
        # le module de l'unité a réellement rougi sur la tête. Un test vert au
        # rejeu (instable) est exclu : il ne retire rien (#4221).
        if not removals and own:
            fallback: list[Removal] = []
            for lot, found in own.items():
                tests = [_failure_name(failure) for failure in found
                         if _failure_name(failure) not in flaky_names]
                if tests:
                    fallback.append(Removal(lot=lot, tests=tests))
            removals = _merge_removals(removals, fallback)
            if removals:
                log("tests ciblés : repli sur les rouges propres : retrait de "
                    "%s" % ", ".join("#%d" % removal.lot
                                     for removal in removals))
        # #7692 : les tests dont le retrait #3801 n'a pas été confirmé (rejeu
        # non concluant, ni confirmé ni situé) ne retirent rien (canon §3) ;
        # ils sont consignés non concluants et la ci-full tranche.
        returned_tests = {test for removal in removals
                          for test in removal.tests}
        unconfirmed = sorted({test for _lot, test in designated_before
                              if test not in returned_tests
                              and test not in flaky_names})
        if unconfirmed:
            log("tests ciblés : %d test(s) non confirmé(s) sur les préfixes "
                "(#3801) : %s — consigné(s) non concluant(s), la ci-full "
                "tranche" % (len(unconfirmed), ", ".join(unconfirmed[:5])))
            runs.append(TestRun(
                label="préfixes (#3801 non confirmé)",
                modules=list(unconfirmed), returncode=None,
                output="rejeu du préfixe non concluant : %s ; retrait #3801 "
                       "non appliqué (canon §3), l'unité reste ; la ci-full "
                       "tranche"
                       % (designation_why or "test ni confirmé ni situé"),
                seconds=0.0, timed_out=True))
            # #7692 (relecture B) : un échec non confirmé ne retient pas le
            # paquet (canon §3) ; il sort de `red`. Un contrat rouge
            # indépendant, lui, reste bloquant : seuls les noms non confirmés
            # sont retirés.
            unconfirmed_set = set(unconfirmed)
            kept_red: list[tuple[TestRun, list[dict]]] = []
            for run, found in red:
                left = [failure for failure in found
                        if _failure_name(failure) not in unconfirmed_set]
                if left:
                    kept_red.append((run, left))
            red = kept_red
            contract_left = [found for run, found in red if run is contract]
            contract_red = bool(contract_left and contract_left[0])
        if flaky_names:
            log("tests ciblés : %d test(s) rouge(s) sur la tête mais "
                "vert(s) au rejeu des préfixes (instable) : %s — "
                "consigné(s) non concluant(s), le paquet continue"
                % (len(flaky_names), ", ".join(flaky_names[:5])))
            red = _consign_flaky(red, runs, flaky_names)
            if not red and not removals:
                return runs, [], []
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


def _packet_candidates_module():
    """#7989 : le module de la liste calculée, depuis le `src` de ce dépôt."""
    sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "src"))
    from ameesh import packet_candidates
    return packet_candidates


def _validated_waiting(payload: dict) -> list[dict]:
    """#7989 : les lots **validés** laissés en attente par le calcul.

    Un arbre de base antérieur à #7989 n'a pas `waiting_entries` : le
    manifeste garde alors son ancienne forme, jamais une composition cassée."""
    try:
        candidates = _packet_candidates_module()
        waiting = getattr(candidates, "waiting_entries", None)
        return waiting(payload) if waiting else []
    except Exception:  # noqa: BLE001 - un manifeste ne casse pas la composition
        return []


def _waiting_manifest_lines(entries: list[dict] | None) -> list[str]:
    """#7989 : les lots **validés** laissés en attente, motif et geste nommés.

    Jusqu'ici le manifeste ne donnait que leur nombre : un validé écarté pour
    `migration-collision` (#7697) ne se voyait qu'au JSON des candidats. La
    section est distincte des « Écartés » (le contrôle d'inclusion ne lit que
    ces derniers) : un lot en attente n'est pas prêt, il n'a pas à y figurer.
    Le geste vient de `packet_candidates.waiting_gesture`, même vocabulaire
    que la note écrite sur le lot."""
    if not entries:
        return []
    try:
        packet_candidates = _packet_candidates_module()
        gesture_of = getattr(packet_candidates, "waiting_gesture", None)
    except Exception:  # noqa: BLE001 - base antérieure à #7989 : motif seul
        gesture_of = None
    rows: list[str] = []
    for entry in entries:
        try:
            lot = int(entry.get("work_item") or 0)
        except (TypeError, ValueError):
            continue
        causes = [cause for cause in entry.get("causes") or ()
                  if isinstance(cause, dict)]
        if not lot or not causes:
            continue
        motives: list[str] = []
        gestures: list[str] = []
        for cause in causes:
            code = str(cause.get("code") or "cause")
            detail = str(cause.get("detail") or "").strip()
            motives.append(code + (": " + detail if detail else ""))
            gesture = gesture_of(cause, lot) if gesture_of else ""
            if gesture and gesture not in gestures:
                gestures.append(gesture)
        rows.append("| #%d | %s | %s |"
                    % (lot, _cell("; ".join(motives)),
                       _cell("; ".join(gestures) or "voir le motif")))
    return rows


def build_manifest(*, name: str, target: str, base_sha: str, carrier: int,
                   composition: Composition, table: str,
                   tests: list[TestRun] | None, freeze_seconds: float,
                   ready_count: int = 0, waiting: int = 0,
                   errors: int = 0, client_types: str = "",
                   waiting_entries: list[dict] | None = None) -> str:
    """Le manifeste §12, lisible par `paquet-inclusion.py` (#1644/#2409).

    #4304/#7740 : `tests=None` — le manifeste est écrit avant les séries
    directes, qui jouent sur le préfixe composé de chaque unité **avant la
    poussée** ; il le dit (`TESTS_PARALLEL_LINE`) et leur résultat va à la PR,
    au porteur et au courrier.

    La table d'admissions ne porte que le lot et sa source : un id cité dans
    un objet (`#N`) serait relu par le contrôle d'inclusion comme un lot
    attendu. Les objets et les classes restent à la PR et au courrier.

    #7989 : `waiting_entries` nomme les lots validés laissés en attente avec
    leur cause calculée et le geste attendu ; le manifeste ne se limite plus
    au compteur. La section « En attente » est ignorée du contrôle
    d'inclusion, qui ne lit que « Écartés » : un lot en attente n'est pas un
    lot prêt à expliquer.

    #8395 : la section « Contrat module v1 » cite le commit de pliage des
    fragments et les révisions attribuées ; `paquet-inclusion.py` la lit pour
    ne pas tenir un lot pour amputé quand le composeur a replié ses lignes."""
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
    # #8395 : le pliage des fragments de révision du contrat, cité au
    # manifeste. Un paquet sans fragment le dit : aucune révision attribuée.
    lines += ["## Contrat module v1", ""]
    if composition.contract is not None:
        revisions = ", ".join(
            "%s (#%d, %s)" % (number, lot,
                              "fragment" if kind == "fragment"
                              else "révision écrite")
            for number, lot, kind in composition.contract.revisions)
        lines += [
            "- Révisions attribuées : %s." % revisions,
            "- Commit de composition : `%s` (en-tête, surface et "
            "`CONTRACT_VERSION` alignés ; fragments retirés)."
            % composition.contract.head,
            "- Chemins du pliage : %s."
            % ", ".join("`%s`" % path for path in composition.contract.files),
        ]
    else:
        lines.append("- Aucun fragment de révision de contrat dans ce paquet : "
                     "aucune révision attribuée.")
    lines += [""]
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
    ]
    # #7989 : les validés en attente, nommés avec leur cause et leur geste.
    waiting_lines = _waiting_manifest_lines(waiting_entries)
    if waiting_lines:
        lines += [
            "## En attente (lots validés, motif calculé)",
            "",
            "| lot | motif | geste |",
            "|---|---|---|",
        ] + waiting_lines + [""]
    lines += [
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
    """#4887/#7740 : par lot, ses modules directs et s'ils ont été joués.

    Les modules directs d'une unité sont ceux de sa série, jouée sur le
    préfixe composé de l'unité : ses propres tests, les voisins que la
    sélection lui a donnés et les témoins de son empreinte. Une série coupée
    par la borne murale n'en a joué aucun jusqu'au bout (#4814) ; une série
    sans module direct est un non-concluant nommé (#7740). Le manifeste le dit
    lot par lot, au lieu de ne compter que les modules de la tête.
    """
    by_label = {run.label: run for run in tests}
    rows: list[str] = []
    for unit in composition.units:
        run = by_label.get("unité #%d" % unit.lot)
        direct = list(run.direct or run.modules) if run is not None else []
        if run is None or not direct:
            # #7740 : 0 module joué est un non-concluant nommé, jamais un
            # « oui (0/0) » qui laisserait croire à une inclusion verte.
            state = "aucun module direct (non concluant)"
        elif run.finished:
            state = "oui (%d/%d)" % (len(direct), len(direct))
        else:
            # Le décompte des modules **joués jusqu'au bout** est explicite :
            # une série coupée n'en a joué aucun (#7740, note d'audit #225168).
            state = "non (0/%d, série coupée après %.0f s%s)" % (
                len(direct), run.seconds,
                ", %d test(s)" % run.tests if run.tests else "")
        rows.append("| #%d | %d | %s |" % (unit.lot, len(direct), state))
    return ["", "Modules directs par lot (#4887, #7740) :", "",
            "| lot | modules directs | joués jusqu'au bout |", "|---|---|---|"] + rows


def _direct_note(composition: Composition, tests: list[TestRun]) -> str:
    """#4887/#7740 : les lots dont la série directe n'est pas allée au bout."""
    by_label = {run.label: run for run in tests}
    missing = []
    for unit in composition.units:
        run = by_label.get("unité #%d" % unit.lot)
        direct = list(run.direct or run.modules) if run is not None else []
        if run is None or not direct or not run.finished:
            missing.append(unit.lot)
    if not missing:
        return "modules directs : tous joués jusqu'au bout"
    return "modules directs non joués (non concluant) : " + ", ".join(
        "#%d" % lot for lot in missing)


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


def write_composition(worktree: Path, rel: str, text: str, *,
                      name: str, units: int, discarded: int) -> str:
    """Write the transient rendering outside the product tree, without a commit."""
    path = Path(rel).resolve()
    if path.is_relative_to(worktree.resolve()):
        raise PaquetAutoError("la composition transitoire doit rester hors du dépôt")
    path.write_text(text, encoding="utf-8")
    head = rev(worktree, "HEAD")
    if not head:
        raise PaquetAutoError("composition sans tête")
    return head


# ---------------------------------------------------------------------------
# Inclusion, paquet, poussée, PR, gel, courrier
# ---------------------------------------------------------------------------


def run_inclusion(worktree: Path, manifest: Path, head: str,
                  ready_file: Path, expected_file: Path, *,
                  target: str | None = None, carrier: int = 0) -> tuple[int, str]:
    script = worktree / INCLUSION_SCRIPT
    if not script.is_file():
        raise PaquetAutoError("%s absent de l'arbre composé" % INCLUSION_SCRIPT)
    cmd = [project_python(worktree), str(script), "--repo", str(worktree),
           "--manifest", str(manifest), "--head", head,
           "--ready-file", str(ready_file), "--expected-file", str(expected_file)]
    if target:
        cmd += ["--target", target]
    if carrier:
        cmd += ["--carrier", str(carrier)]
    proc = run_command(cmd, cwd=worktree, timeout=DEFAULT_TIMEOUT,
                       env=python_env(worktree))
    return proc.returncode, _output(proc)


def record_packet(worktree: Path, carrier: int, base_sha: str, head: str,
                  branch: str, target: str, expect_pushed: str = "", claim=None,
                  composition_file: Path | None = None) -> str:
    cmd = ["ameesh", "work", "packet", "record", str(carrier),
           "--repo", str(worktree), "--base", base_sha, "--head", head,
           "--branch", branch, "--target", target]
    if composition_file is not None:
        cmd += ["--composition-file", str(composition_file)]
    if claim is not None:
        claim.require()
        cmd += ["--composition-owner", claim.owner,
                "--composition-epoch", str(claim.epoch)]
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


#: #7754 : marques d'un refus de quota GitHub dans la sortie de `gh` (le
#: message GraphQL dit « API rate limit exceeded », le REST peut dire
#: « secondary rate limit »).
GH_QUOTA_MARKS = ("rate limit", "rate-limit", "quota")


def _gh_quota_refused(text: str) -> bool:
    """#7754 : la sortie de `gh` dit-elle un refus de débit ?"""
    lowered = text.casefold()
    return any(mark in lowered for mark in GH_QUOTA_MARKS)


def _pull_request_rest(worktree: Path, branch: str) -> dict | None:
    """#7754 : la PR de la branche par l'API REST (`gh api`).

    Le bucket REST `core` est distinct du bucket GraphQL : un `gh pr view`
    refusé pour quota reste lisible ici. Rend None quand la branche n'a pas
    de PR. Le corps de l'endpoint est celui du diagnostic : le filtre `head`
    porte `owner:branche`, `state=all` pour voir aussi une PR fusionnée ou
    fermée.
    """
    endpoint = "repos/{owner}/{repo}/pulls?head={owner}:%s&state=all" % branch
    proc = _gh(worktree, "api", endpoint)
    if proc.returncode != 0:
        raise PaquetAutoError(
            "gh api %s a échoué : %s" % (endpoint, _tail(_output(proc))))
    try:
        items = json.loads(_decode(proc.stdout))
    except (ValueError, TypeError) as exc:
        raise PaquetAutoError(
            "gh api %s : réponse illisible (%s)" % (endpoint, exc)) from exc
    if not isinstance(items, list):
        raise PaquetAutoError(
            "gh api %s : réponse inattendue (liste attendue)" % endpoint)
    if not items:
        return None
    item = items[0]
    if not isinstance(item, dict):
        raise PaquetAutoError("gh api %s : PR illisible" % endpoint)
    state = str(item.get("state") or "").upper()
    if item.get("merged_at") or state == "MERGED":
        state = "MERGED"
    elif state not in ("OPEN", "CLOSED"):
        state = ""
    return {"number": item.get("number"),
            "url": str(item.get("html_url") or ""),
            "state": state}


def _pull_request(worktree: Path, branch: str) -> dict | None:
    """#7754 : la PR de la branche, sans confondre un refus avec « absente ».

    `gh pr view` (GraphQL) est lu d'abord ; s'il échoue, l'API REST prend le
    relais : un refus de quota GraphQL n'est ni « not found », ni silencieux,
    ni fatal à la lecture. Rend None quand la branche n'a pas de PR.
    """
    view = _gh(worktree, "pr", "view", branch,
               "--json", "number,url,state")
    if view.returncode == 0:
        try:
            data = json.loads(_decode(view.stdout))
        except (ValueError, TypeError) as exc:
            raise PaquetAutoError(
                "gh pr view : réponse illisible (%s)" % exc) from exc
        if not isinstance(data, dict):
            raise PaquetAutoError("gh pr view : réponse inattendue")
        return data
    text = _output(view)
    lowered = text.casefold()
    if "no pull requests found" in lowered or "not found" in lowered:
        return None
    cause = _tail(text)
    quota = _gh_quota_refused(text)
    if quota:
        print("gh pr view %s : limite de débit GraphQL ; lecture REST (#7754)"
              % branch, flush=True)
    try:
        return _pull_request_rest(worktree, branch)
    except PaquetAutoError as fallback:
        raise PaquetAutoError(
            "gh pr view %s a échoué%s : %s ; repli REST : %s"
            % (branch, " (limite de débit)" if quota else "", cause, fallback)
        ) from fallback


def ensure_pull_request(worktree: Path, branch: str, target: str,
                        title: str, body: str) -> tuple[int | None, str]:
    """Ouvre ou met à jour la PR ; rend `(numéro, url)`.

    La lecture passe par `_pull_request` : un refus de quota GraphQL est relu
    par l'API REST au lieu d'être pris pour « pas de PR » (#7754).
    """
    data = _pull_request(worktree, branch)
    number: int | None = None
    url = ""
    if data is not None:
        try:
            number, url = int(data["number"]), str(data.get("url") or "")
            state = str(data.get("state") or "").upper()
        except (ValueError, KeyError, TypeError) as exc:
            raise PaquetAutoError("gh pr view : réponse illisible (%s)" % exc) from exc
        if state and state != "OPEN":
            raise PaquetAutoError(
                "PR #%d pour %s est %s, pas ouverte : choisir un autre --name "
                "(un paquet fusionné ou fermé ne se réécrit pas)"
                % (number, branch, state))
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
            try:
                created = _pull_request(worktree, branch)
            except PaquetAutoError:
                created = None
            if created:
                try:
                    number = int(created["number"])
                except (ValueError, KeyError, TypeError):
                    number = None
        return number, url
    finally:
        body_path.unlink(missing_ok=True)


def update_pull_request_body(worktree: Path, branch: str, target: str,
                             title: str, body: str,
                             pr_number: int | None = None,
                             pr_url: str = "") -> tuple[int | None, str, str]:
    """#7754 : met le corps de la PR finale à jour sans perdre le paquet.

    À ce stade la PR existe depuis la poussée. Une lecture encore refusée
    (même après le repli REST) ou une écriture refusée est dite, pas fatale :
    la poussée, le gel et la ci-full sont déjà faits. Rend
    `(numéro, url, échec)` ; `échec` est vide quand le corps est à jour,
    sinon « corps de PR non mis à jour : <cause> ».
    """
    try:
        number, url = ensure_pull_request(worktree, branch, target, title, body)
    except (OSError, PaquetAutoError) as error:
        return (pr_number, pr_url,
                "corps de PR non mis à jour : %s" % _one_line(error))
    return number, url, ""


def freeze_carrier(worktree: Path, carrier: int, head: str, note: str) -> str:
    cmd = ["ameesh", "work", "milestone", str(carrier), "frozen",
           "--sha", head, "--repo", str(worktree), "--note", note]
    proc = run_command(cmd, cwd=worktree, timeout=FREEZE_TIMEOUT)
    if proc.returncode != 0:
        raise PaquetAutoError(
            "gel du porteur #%d refusé : %s" % (carrier, _tail(_output(proc))))
    return _output(proc)


def request_full_ci(worktree: Path, carrier: int, head: str) -> str:
    """#4304/#7740 : la ci-full de la tête poussée, après contrats et séries.

    Jamais fatal : `ameesh ci run` est idempotent sur un run en vol de la même
    composition, et `paquet-horaire.py` (publish) la redemande après le retour.
    """
    cmd = ["ameesh", "ci", "run", str(carrier), "--full", "--sha", head,
           "--avis", "0", "--reason",
           "paquet-auto : tête poussée après contrats et séries directes "
           "(canon §6, #4304, #7740)"]
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
    """L'état de la PR de la branche (OPEN, MERGED, CLOSED), "" si illisible.

    #7754 : un refus de quota GraphQL est relu par l'API REST ; l'état n'est
    vide que si les deux lectures échouent (les tests continuent, #4463).
    """
    try:
        data = _pull_request(worktree, branch)
    except PaquetAutoError:
        return ""
    if not data:
        return ""
    return str(data.get("state") or "").upper()


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
        "par unité %s sur la tête %s ; instantané du porteur (#4322)"
        % (args.name, summary, head[:12])), flush=True)
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
              sender: str | None = None, *, info: bool = False) -> str:
    cmd = ["agent-mail", "send", recipient, "-", "--lot", str(lot)]
    if info:
        cmd.append("--info")
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
            why = ("les tests ciblés de ton unité ont rougi sur son préfixe "
                   "composé ; le test rouge apparaît pour la première fois "
                   "au préfixe (%s)"
                   % (" ; ".join(removal.evidence[:3])
                      or "préfixes de l'empilement"))
        elif removal.attributed:
            why = ("les tests ciblés de la composition ont rougi sur la tête "
                   "composée, et la lecture #3801 désigne ton unité (%s)"
                   % (" ; ".join(removal.evidence[:3]) or "attribution #3801"))
        else:
            why = ("les tests ciblés de ton unité ont rougi sur son préfixe "
                   "composé ; le module propre de ton unité est rouge "
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


#: #8262 : préfixe de la clé durable (#6090) du lot de déblocage d'un rouge
#: de la base ; `packet_red` la relit pour classer le même rouge en CI.
BASE_RED_REF = "paquet-rouge-base:"
BASE_RED_LOT_RE = re.compile(r"#(\d+) (created|already exists)")
#: L157 : un agent (le composeur passe `--from mesh-design`) ne crée pas de
#: lot sans estimation ; forfait d'un test dépendant de l'environnement.
BASE_RED_ESTIMATE = "2h"
#: #8262 (B mty-amh-impl-11) : la preuve structurée notée sur le lot ; seule
#: elle neutralise un rouge en CI (`packet_red.mark_base_red`), pour le même
#: test, la même classe, la base exacte et le même dépôt. Le lot n'est que
#: le suivi. Même préfixe que `packet_red.BASE_RED_PROOF`.
BASE_RED_PROOF = "ameesh-base-red/1 "
_REMOTE_SLUG_RE = re.compile(r"[:/]([^/:]+/[^/]+?)(?:\.git)?/?$")


def repo_slug(worktree: Path) -> str:
    """`owner/repo` (minuscules) du dépôt `origin` de l'arbre, "" si illisible."""
    proc = git(worktree, "remote", "get-url", "origin", check=False)
    if proc.returncode != 0:
        return ""
    found = _REMOTE_SLUG_RE.search(_decode(proc.stdout).strip())
    return found.group(1).lower() if found else ""


def base_red_proof(test: str, *, kind: str, base_sha: str, repo: str,
                   driver: str, name: str, carrier: int) -> str:
    """#8262 : la note de preuve d'un rouge de la base (une ligne, JSON)."""
    return BASE_RED_PROOF + json.dumps(
        {"test": test, "kind": kind, "base": base_sha.lower(), "repo": repo,
         "driver": driver, "packet": name, "carrier": carrier},
        ensure_ascii=False, sort_keys=True)
BASE_RED_ESTIMATE_SOURCE = "forfait #8262 : correction d'un test rouge sur la base"


def base_red_lot(test: str, *, name: str, carrier: int, base_sha: str,
                 target: str, kind: str = "", repo: str = "", driver: str = "",
                 sender: str | None = None, log=print) -> int:
    """#8262 : ouvre, ou relance par une note, le lot de déblocage d'un test.

    Une clé durable par test (`paquet-rouge-base:<test>`) : un second rouge
    du même test retrouve le même lot, quel que soit son état. Chaque constat
    y est noté comme preuve structurée (`base_red_proof` : test, classe, base
    exacte, dépôt) ; sans cette note, la CI ne neutralise rien. Rend le numéro du lot, 0 si le
    geste n'a pas abouti (consigné, sans arrêter la composition). Le projet
    est celui du composeur (`work add` sans `--app`), jamais écrit en dur.
    """
    body = (
        "Rouge de la base relevé par le composeur du paquet %s (porteur #%d) : "
        "`%s` est rouge sur la tête composée et sur la base exacte `%s` `%s`, "
        "même classe d'échec, même pilote (`%s`). Canon §3 : un test dépendant "
        "de l'environnement est un défaut du test ; le paquet n'est pas retenu, "
        "aucune unité n'est retirée pour ce rouge. Attendu : corriger le test "
        "(ou la cible) à la source, puis livrer."
        % (name, carrier, test, target, base_sha, driver or "?"))
    actor = ["--actor", sender] if sender else []
    try:
        proc = run_command(
            ["ameesh", "work", "add", "--title",
             "Rouge de la base : %s" % ".".join(test.split(".")[-2:]),
             "--type", "bug", "--priority", "1",
             "--source", "paquet-auto (#8262), rouge de la base",
             "--unique-ref", BASE_RED_REF + test,
             "--estimate", BASE_RED_ESTIMATE,
             "--estimate-source", BASE_RED_ESTIMATE_SOURCE,
             "--see-also", str(carrier), "--body", "-"] + actor,
            input_text=body, timeout=GIT_TIMEOUT)
    except PaquetAutoError as exc:
        log("lot de déblocage de %s non ouvert : %s" % (test, exc))
        return 0
    found = BASE_RED_LOT_RE.search(_output(proc))
    if proc.returncode != 0 or not found:
        log("lot de déblocage de %s non ouvert : %s" % (test, _tail(_output(proc))))
        return 0
    lot = int(found.group(1))
    # création ou relance : la preuve du constat est notée ; le lot garde son
    # état. Une note manquée laisse le suivi sans preuve (rien n'est
    # neutralisé en CI), jamais l'inverse.
    note = base_red_proof(test, kind=kind, base_sha=base_sha, repo=repo,
                          driver=driver, name=name, carrier=carrier)
    try:
        noted = run_command(["ameesh", "work", "note", str(lot), "-"] + actor,
                            input_text=note, timeout=GIT_TIMEOUT)
        if noted.returncode != 0:
            log("preuve du lot #%d non notée : %s" % (lot, _tail(_output(noted))))
    except PaquetAutoError as exc:
        log("preuve du lot #%d non notée : %s" % (lot, exc))
    log("lot de déblocage de %s : #%d (%s)"
        % (test, lot, "relancé" if found.group(2) == "already exists" else "ouvert"))
    return lot


def notify_base_red(name: str, carrier: int, tests: list[str], *,
                    base_sha: str, target: str, kinds: dict | None = None,
                    repo: str = "", driver: str = "",
                    pilot: str = "mesh-design",
                    sender: str | None = None, log=print,
                    open_lot=None) -> str:
    """#8262 : un rouge de la base ne retient pas le paquet ; lot et pilote.

    Chaque test (cinq au plus) reçoit son lot de déblocage (`base_red_lot`,
    injectable par `open_lot`) ; le pilote est prévenu. Rien n'arrête la
    composition : un courrier ou un lot manqué est consigné. Rend le motif
    d'échec du courrier.
    """
    opener = open_lot or base_red_lot
    lots = []
    for test in tests[:5]:
        lot = opener(test, name=name, carrier=carrier, base_sha=base_sha,
                     target=target, kind=(kinds or {}).get(test, ""),
                     repo=repo, driver=driver, sender=sender, log=log)
        lots.append("%s → %s" % (test, ("#%d" % lot) if lot else "lot non ouvert"))
    body = (
        "#%d paquet %s : test(s) rouge(s) sur la tête et sur la base `%s` "
        "(même classe d'échec) : rouge(s) de la base, défaut(s) du test (canon "
        "§3). Aucun retrait pour eux, le paquet continue. Déblocage : %s."
        % (carrier, name, base_sha[:12], " ; ".join(lots) or "aucun"))
    try:
        send_mail(pilot, carrier, body, sender)
    except PaquetAutoError as exc:
        log("courrier de rouge de la base au pilote non envoyé : %s" % exc)
        return str(exc)
    log("courrier de rouge de la base au pilote envoyé (%s)" % pilot)
    return ""


def pr_body(*, name: str, target: str, base_sha: str, carrier: int,
            composition: Composition, candidates: list[Candidate],
            tests: list[TestRun] | None, inclusion_rc: int,
            freeze_seconds: float, composition_text: str = "", head: str = "") -> str:
    """Le corps de la PR ; `tests=None` seulement quand aucune série n'a été
    consignée (#7740 : la poussée vient après elles)."""
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
        lines.append("- Tests par unité : aucun résultat consigné dans ce "
                     "corps ; les séries directes jouent avant la poussée "
                     "(#7740).")
    else:
        lines.append("- Tests locaux : %s." % _tests_summary(tests))
        lines += ["  " + line for line in _inconclusive_lines(tests)]
        lines += ["  " + line for line in _unattributed_lines(tests)]
        lines += _direct_lines(composition, tests)
    lines += [
        "- Contrôle d'inclusion : %s." % ("vert" if inclusion_rc == 0 else "à lire"),
        "- Freeze-checks : vert (%.1f s)." % freeze_seconds,
        "- Enregistrement ameesh : porteur #%d, cible `%s`, tête `%s`."
        % (carrier, target, head),
        "",
        "Porteur : #%d. CI complète demandée dès la poussée (#4304) ; "
        "« verdict non requis » reste au pilote." % carrier,
        "",
    ]
    if composition_text:
        lines += ["## Copie de la composition", "",
                  "Copie lisible ; l’instantané ameesh de cette tête fait autorité.",
                  "", composition_text]
    return "\n".join(lines)


def mail_body(*, name: str, target: str, base_sha: str, carrier: int,
              head: str, branch: str, composition: Composition,
              tests: list[TestRun], inclusion_rc: int, pr_number: int | None,
              pr_url: str, pr_note: str = "",
              report_info: bool = False) -> str:
    units = ", ".join("#%d" % unit.lot for unit in composition.units) or "aucun"
    discarded = "; ".join(
        "#%d (%s)" % (item.lot, item.reason) for item in composition.discarded
    ) or "aucun"
    pr = ("PR #%d %s" % (pr_number, pr_url)) if pr_number else (pr_url or "PR non lue")
    if pr_note:
        # #7754 : la cause d'une mise à jour finale refusée est dite au pilote.
        pr += " ; " + pr_note
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
- Gel : `ameesh work milestone` posé sur la tête dès la poussée, ci-full demandée (#4304) ; instantané ameesh du porteur #{carrier}, tête `{head}`.

{next_step}
""".format(
        name=name, head=head, target=target, base=base_sha[:12],
        branch=branch, pr=pr, n=len(composition.units), units=units,
        d=len(composition.discarded), discarded=discarded,
        tests=_tests_summary(tests), direct_note=_direct_note(composition, tests),
        non_section=non_section,
        inclusion="vert" if inclusion_rc == 0 else "à lire",
        carrier=carrier,
        next_step=("Le composeur horaire poursuit la publication et traite les demandes "
                   "de geste séparément." if report_info else
                   "« Verdict non requis » reste ton geste ; aucune fusion sur %s." % target),
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
           candidates_json: Path | None = None, log=print,
           target: str | None = None, carrier: int = 0) -> int:
    """Rejoue un manifeste gelé et compare l'arbre obtenu à celui de `reference`."""
    ref = rev(repo, reference)
    if ref is None:
        raise InputError("référence %r introuvable" % reference)
    manifest_path = manifest_rel(name)
    manifest_text = composition_text(repo, ref, name, target=target, carrier=carrier)
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
        parents = out(repo, "rev-list", "--parents", "-n", "1", ref).split()[1:]
        historical = (len(parents) == 1 and
            out(repo, "diff-tree", "--no-commit-id", "--name-only", "-r", ref)
                .splitlines() == [manifest_path])
        if historical:
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
                candidates: list[Candidate], *, target: str | None = None,
                carrier: int = 0) -> list[tuple[list[int], str]]:
    """#4237 : la tête testée est-elle publiable telle quelle sur `base_sha` ?

    Une chaîne de fusions (premier parent), éventuellement surmontée du seul
    manifeste historique, qui
    part de la tête fraîche de la cible ; une fusion par ligne de la table
    `## Composition`, dans l'ordre, son second parent égal à la source de la
    ligne et à la source de chacun de ses lots dans la liste calculée (la tête
    gelée enregistrée, ou sa remise), et son **arbre** égal à la fusion
    déterministe de ses deux parents (`git merge-tree --write-tree`, la même
    stratégie que `paquet-compose`) : un contenu glissé dans une fusion aux
    parents conformes est refusé. Rend la table ; sinon `ChainRefused`.
    """
    # Historical packets carry one metadata commit; current packets end at
    # the final unit merge. Validate every merge in either representation.
    end = head
    parents = out(repo, "rev-list", "--parents", "-n", "1", head).split()[1:]
    if len(parents) == 1:
        files = out(repo, "diff-tree", "--no-commit-id", "--name-only", "-r",
                    head).splitlines()
        if files != [rel]:
            raise ChainRefused("la tête %s n'est pas le seul commit du manifeste %s"
                               % (head[:12], rel))
        end = parents[0]
    try:
        name = Path(rel).stem.removeprefix("integration-paquet-")
        specs = parse_composition_table(composition_text(
            repo, head, name, target=target, carrier=carrier))
    except InputError as error:
        raise ChainRefused(str(error)) from error
    lines = out(repo, "rev-list", "--first-parent", "--parents", "--reverse",
                "%s..%s" % (base_sha, end), check=False).splitlines()
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
            check_carrier_open(repo, args.carrier)

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
        specs = check_chain(worktree, head, base_sha, rel, candidates,
                            target=args.target, carrier=args.carrier)
        print("chaîne vérifiée : %d unité(s) sur %s" % (len(specs), base_sha[:12]),
              flush=True)
        units = "\n".join("- %s `%s`" % (", ".join("#%d" % lot for lot in row_lots),
                                         source[:12]) for row_lots, source in specs)
        contract_note = "Repli legacy : aucun résultat de contrats réutilisé."
        if publication_policy(args) != "legacy":
            report = prepublication_contracts(
                worktree, base=base_sha, budget=contract_budget(args), guard=guard,
                expected_head=head, parallel=getattr(args, "tests_parallel", DEFAULT_PARALLEL),
                carrier=args.carrier, project=getattr(args, "pause_project", "") or "", target=args.target)
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
        body_failure = ""
        if state and state != "OPEN":
            body_failure = "corps de PR non mis à jour (PR %s)" % state.lower()
            print("PR %s : corps non mis à jour" % state.lower(), flush=True)
        else:
            body = "\n".join([
                "ameesh-work: %d" % args.carrier, "",
                "Paquet horaire %s (§12), base `%s` `%s`, **%d unité(s)**."
                % (args.name, args.target, base_sha[:12], len(specs)), "",
                units, "", origin, "",
                "- Contrôle d'inclusion : vert.",
                "- Enregistrement ameesh : porteur #%d, cible `%s`, tête `%s`."
                % (args.carrier, args.target, head), "",
                "## Copie de la composition", "",
                "Copie lisible ; l’instantané ameesh de cette tête fait autorité.",
                "", composition_text(repo, head, args.name,
                                     target=args.target, carrier=args.carrier), "",
                "Porteur : #%d. « Verdict non requis » et CI complète restent au pilote."
                % args.carrier, ""])
            pr_number, pr_url, body_failure = update_pull_request_body(
                worktree, branch, args.target,
                "Paquet %s — %d unités sur %s %s" % (
                    args.name, len(specs), args.target, base_sha[:12]), body)
            if body_failure:
                print(body_failure, flush=True)
        note = ("paquet %s : fin publiée par scripts/paquet-auto.py --publish (#4237) "
                "sur la tête poussée %s, tests par unité non rejoués (qualification "
                "au journal d'origine) ; instantané du porteur"
                % (args.name, head[:12]))
        if body_failure:
            note += " ; " + body_failure
        print(note_carrier(worktree, args.carrier, note), flush=True)
        check_pause(worktree, getattr(args, "pause_project", "") or "", args.target)
        # #8290 : clos pendant la publication : pas de ci-full
        check_carrier_open(repo, args.carrier)
        print(request_full_ci(worktree, args.carrier, head), flush=True)
        pr = ("PR #%d %s" % (pr_number, pr_url)) if pr_number else (pr_url or "PR non lue")
        if body_failure:
            pr += " ; " + body_failure
        mail = "\n".join([
            "Paquet %s publié sans retester : tête `%s` sur %s `%s` (branche `%s`, %s)."
            % (args.name, head, args.target, base_sha[:12], branch, pr), "",
            units, "", origin, "",
            ("Le composeur horaire poursuit la publication et signale séparément "
             "les gestes nécessaires." if getattr(args, "report_info", False) else
             "« Verdict non requis » et la ci-full restent tes gestes ; aucune "
             "fusion sur %s." % args.target), ""])
        try:
            send_mail(args.pilot, args.carrier, mail, getattr(args, "sender", None),
                      info=getattr(args, "report_info", False))
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
        if not args.no_packet_record:
            # #8290 : un porteur clos pendant la composition arrête tout geste
            check_carrier_open(repo, args.carrier)

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
        if not args.no_packet_record:
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
        candidates, payload = compute_candidates(
            worktree, args.target, base_sha, ready_out,
            # #7989 : le calcul de la composition réelle note les validés en
            # attente ; un calcul de contrôle (`--no-packet-record`) n'écrit rien.
            note_waiting=not getattr(args, "no_packet_record", False))
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
            specs = composition_specs(repo, keep, args.name,
                                      target=args.target, carrier=args.carrier)
            candidates, kept_discards = keep_composition(
                candidates, specs, worktree=worktree,
                already_discarded={item.lot for item in errors})
            errors = errors + kept_discards
        if not args.no_packet_record:
            # #8290 : une unité déjà portée par un autre paquet ouvert n'entre
            # pas dans celui-ci (h-20261126-01/02 : #8285 dans deux paquets).
            candidates, elsewhere = carried_elsewhere(repo, args.carrier, candidates)
            errors = errors + elsewhere
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
            guard=guard,
            contract_date=getattr(args, "contract_date", "") or "")
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
        # #4304/#7740 : la tête est poussée, gelée et sa ci-full demandée après
        # les contrats et les séries directes sur le préfixe de chaque unité,
        # avant la CI. Un rouge retire l'unité et la tête recomposée est la
        # seule poussée (aucun bail à remplacer).
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
                           freeze_seconds=freeze_seconds,
                           composition_text=manifest_text, head=head)

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
                errors=len(errors), client_types=client_note,
                # #7989 : chaque validé en attente nommé, motif et geste.
                waiting_entries=_validated_waiting(payload))
            # Keep metadata outside the product tree; no metadata commit.
            composition_file = Path(tmp.name) / Path(rel).name
            head = write_composition(worktree, str(composition_file), manifest_text,
                                     name=args.name, units=len(composition.units),
                                     discarded=len(composition.discarded))
            print("composition : tête %s" % head, flush=True)
            if not args.no_packet_record:
                # #8290 : relu juste avant l'enregistrement (après les contrôles)
                check_carrier_open(repo, args.carrier)
                print(record_packet(worktree, args.carrier, base_sha, head,
                                    branch, args.target,
                                    expect_pushed=lease or "", claim=claim,
                                    composition_file=composition_file), flush=True)
            _write_id_file(expected_file, [unit.lot for unit in composition.units])
            inclusion_rc, inclusion_output = run_inclusion(
                worktree, composition_file, head, ready_out, expected_file,
                target=args.target, carrier=args.carrier)
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
                    project=getattr(args, "pause_project", "") or "", target=args.target)
                contract_run = contract_report.run
                print("contrats avant publication : %s (%.1f s)%s" % (
                    "NON CONCLUANT" if contract_run.inconclusive else
                    ("vert" if contract_run.green else "ROUGE"), contract_run.seconds,
                    (" — " + contract_run.log_path) if contract_run.log_path else ""), flush=True)
                if not args.local:
                    record_contract_result(worktree, args.carrier, contract_report,
                                           base=base_sha, target=args.target)
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
            base_red = sorted({name for run in runs
                               for name in getattr(run, "base_red", [])})
            if base_red and not getattr(args, "local", False):
                # #8262 : rouge de la base consigné, le paquet continue ; le
                # lot de déblocage est ouvert ou relancé, le pilote prévenu.
                kinds = {name: kind for run in runs
                         for name, kind in getattr(run, "base_kinds", {}).items()}
                notify_base_red(args.name, args.carrier, base_red,
                                base_sha=base_sha, target=args.target,
                                kinds=kinds, repo=repo_slug(worktree),
                                driver=test_driver(worktree),
                                pilot=getattr(args, "pilot", "mesh-design"),
                                sender=getattr(args, "sender", None))
            if contract_report is not None and not args.local:
                # Classification may turn a flaky negative into an explicit
                # inconclusive result; the projection must reflect that too.
                record_contract_result(worktree, args.carrier, contract_report,
                                       base=base_sha, target=args.target)
            if not faulty:
                # #7740 : les séries directes (sur leur préfixe composé) et
                # les contrats ont été joués avant la poussée ; la tête
                # composée peut être publiée. Un rouge a déjà recomposé.
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
                            guard=guard,
                            contract_date=getattr(args, "contract_date", "") or "")
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
                    # #8290 : relu après les contrôles, juste avant la poussée
                    if not args.no_packet_record:
                        check_carrier_open(repo, args.carrier)
                    push_branch(worktree, branch, force_lease=lease)
                    print("poussée : %s%s" % (branch, " (recomposition)"
                                              if lease else ""), flush=True)
                    lease = pushed = head
                    # #8290 : clos pendant la poussée : ni PR, ni gel, ni ci-full
                    if not args.no_packet_record:
                        check_carrier_open(repo, args.carrier)
                    pr_number, pr_url = ensure_pull_request(
                        worktree, branch, args.target, title(), body(runs))
                    print("PR : %s" % (pr_url or pr_number or "?"), flush=True)
                    note = ("paquet %s poussé par scripts/paquet-auto.py après "
                            "les contrats et les séries directes sur leur préfixe "
                            "(canon §6, #4304, #7740) : %d unités, %d écartés, "
                            "inclusion verte, tests par unité joués ou non "
                            "concluants nommés ; instantané %s"
                            % (args.name, len(composition.units),
                               len(composition.discarded), head))
                    # #8290 : relu avant le gel et avant la ci-full (clos
                    # pendant la PR ou le gel : rien n'est armé)
                    if not args.no_packet_record:
                        check_carrier_open(repo, args.carrier)
                    print(freeze_carrier(worktree, args.carrier, head, note), flush=True)
                    if not args.no_packet_record:
                        check_carrier_open(repo, args.carrier)
                    print(request_full_ci(worktree, args.carrier, head), flush=True)
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
                guard=guard,
                contract_date=getattr(args, "contract_date", "") or "")
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
        body_failure = ""
        if state == "MERGED":
            success = True
            return merged_during_tests(args, worktree, head, runs, [], branch, rel)
        if state and state != "OPEN":
            body_failure = "corps de PR non mis à jour (PR %s)" % state.lower()
            print("PR %s pendant les tests par unité : corps non mis à jour"
                  % state.lower(), flush=True)
        else:
            pr_number, pr_url, body_failure = update_pull_request_body(
                worktree, branch, args.target, title(), body(runs),
                pr_number, pr_url)
            if body_failure:
                print(body_failure, flush=True)
        note = ("paquet %s : tests directs (préfixe composé) et contrats %s avant "
                "la poussée de %s (%s) ; %s ; inclusion verte ; instantané %s"
                % (args.name, tests_note, head[:12], _tests_summary(runs),
                   _direct_note(composition, runs), head))
        if body_failure:
            note += " ; " + body_failure
        print(note_carrier(worktree, args.carrier, note), flush=True)
        mail = mail_body(name=args.name, target=args.target, base_sha=base_sha,
                         carrier=args.carrier, head=head, branch=branch,
                         composition=composition, tests=runs,
                         inclusion_rc=inclusion_rc, pr_number=pr_number,
                         pr_url=pr_url, pr_note=body_failure,
                         report_info=getattr(args, "report_info", False))
        try:
            send_mail(args.pilot, args.carrier, mail, getattr(args, "sender", None),
                      info=getattr(args, "report_info", False))
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
#: Seul le lot change de sujet dans le scope. #7318 : conserver le runner,
#: l'époque et l'enveloppe de rôle ; sortir du cgroup ne donne pas un nouveau
#: bail. Les gardes continuent de vérifier ces preuves contre le bail vivant
#: et refusent une session périmée ou non liée, même après un redémarrage.
TURN_VARIABLES = ("AMEESH_LOT",)
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
    avec les preuves de son bail : la garde du pilote les vérifie encore
    dans le scope, sans transformer une session liée en identité explicite.
    `AMEESH_LOT` suit le porteur (#6810), jamais le lot que l'agent avait au moment du lancement.
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
    parser.add_argument("--report-info", action="store_true",
                        help="compte rendu de succès sans réveil ; le composeur poursuit la publication")
    parser.add_argument("--pilot", default="mesh-design",
                        help="destinataire du courrier (défaut : mesh-design)")
    parser.add_argument("--from", dest="sender",
                        default=os.environ.get("AMEESH_ROLE") or None,
                        help="expéditeur du courrier (défaut : AMEESH_ROLE, sinon "
                             "l'identité liée)")
    parser.add_argument("--local", action="store_true",
                        help="composition de contrôle : rendu transitoire, packet "
                             "record et tests par unité, sans poussée, PR, gel, "
                             "ci-full ni courrier ; prend le même bail que le composeur "
                             "réel (rc 3 si occupé) ; --no-packet-record évite ce bail")
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
    parser.add_argument("--contract-date", default="", metavar="AAAA-MM-JJ",
                        help="#8395 : date de l'en-tête du contrat pour le pliage "
                             "des fragments (défaut : le jour de composition ; "
                             "les tests fixent la date)")
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
                          candidates_json=candidates_json, target=args.target, carrier=args.carrier)
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
    except CarrierClosed as error:
        # #8290 : porteur clos pendant la tentative ; rien n'est écrit.
        print("paquet-auto : %s" % error, file=sys.stderr)
        return CARRIER_CLOSED_RC
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
