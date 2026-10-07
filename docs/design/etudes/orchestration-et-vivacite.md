---
type: Study
title: "Étude — un orchestrateur arrêté bloque un chantier : vivacité, reprise, alertes, délégation, cloisonnement"
description: "Analyse, dans le code d'ameesh, du blocage d'un chantier par son orchestrateur externe arrêté (2026-10-07) ; décision proposée « pas de travail sans réveil possible » et plan de lots L36–L41."
status: stable
tags: [orchestrateur, bail, sessions, alertes, delegation, cloisonnement, comptes]
generated: { by: "claude3/claude-opus-5-5", at: "2026-10-07T12:00:00+02:00" }
sources:
  - { resource: "decisions/0010-responsabilite-et-orchestrateurs.md", title: "Décision 0010 (responsable humain)" }
  - { resource: "decisions/0014-orchestrateurs-a-tours-et-placement.md", title: "Décision 0014 (orchestrateurs à tours, attach)" }
  - { resource: "decisions/0025-une-session-par-lot.md", title: "Décision 0025 (sessions, résumé de reprise)" }
  - { resource: "decisions/0027-bascule-automatique-entre-comptes.md", title: "Décision 0027 (bascule de compte)" }
  - { resource: "../BASCULE.md", title: "Bascule v0 → v1 (coexistence, étape 8)" }
---

# Contexte

Signalement du propriétaire (2026-10-07) : un chantier d'une trentaine de lots
est bloqué depuis le matin. Son orchestrateur est une session Codex
interactive, sans bail. Son dernier tour s'est terminé normalement, puis il a
attendu un prompt humain qui n'est jamais venu. Ses relais sont eux aussi
externes, arrêtés ou au repos, et le compte Codex de la session est au seuil.
Propositions P1 à P6 du propriétaire : adoption, reprise, alertes poussées,
délégation à échéance, cloisonnement, compteur de `ameesh list`.

L'état observé le jour même (`ameesh list`, `ameesh show`, `ameesh work
list`, `ameesh alerts`) confirme le constat et l'aggrave. Dans la suite,
« l'orchestrateur » désigne cette session.

# Ce que le code fait réellement

## 1. « Agent externe » n'existe pas dans le modèle

* Le statut de l'orchestrateur (« … mailbox identity only, no runner
  lease ») est un **texte libre** posé par
  `agent-mail status`. Aucune colonne ne distingue un agent mené par
  l'exécuteur d'une session humaine : pour le registre, c'est un agent
  `stopped` comme un autre.
* Un hook qui voit une identité sans bail l'**enregistre** au premier contact
  (`cli.py` `cmd_hook` → `registry.upsert`, `status` par défaut `idle`,
  `harness` `other`). L'identité « explicite » (`identity.py`, seulement
  `AGENT_MAIL_NAME`) est acceptée sans aucune vérification.
* L'orchestrateur n'a **aucun responsable humain** (`ameesh show` :
  « responsable : aucun »), ce qui contredit 0010.

## 2. Un lot s'assigne à n'importe quel nom

* `work_items.assignee` est un texte libre, fixé à la création
  (`work add --assignee`) ; il n'y a **pas de réassignation** et **aucune
  vérification** que l'assigné existe, a un bail ou un exécuteur.
* Sur le chantier bloqué, sept lots sont assignés à des noms **absents du
  registre** (des relais lancés hors d'ameesh, pour un tour borné) et une
  dizaine d'autres à des agents arrêtés.
* L'étiquette « attend : démarrage par X » / « travail de X »
  (`stagnation.py`, `_LABELS`) ne dit jamais que X est inconnu, arrêté ou
  externe.
* Aucune notion de délégation ni d'échéance sur un lot.

## 3. Les alertes ne voient pas un agent arrêté et ne sont jamais poussées

* `idle_with_mail` ne vise que l'état dérivé `idle` (`exploitation.py:177`) ;
  `stopped` et `dead` sont fusionnés en `stopped` (`progress.py:332-341`) et
  n'alertent pas. Au moment de l'analyse, plusieurs agents arrêtés avaient
  chacun de 5 à 17 messages non lus, sans aucune alerte.
* L'arrêt à la main ne se distingue d'un bail expiré que par `status_text`
  (« arrêté à la main », `runner.py:2490`, contre « bail expiré »,
  `registry.py:433`) : il n'existe aucune raison d'arrêt structurée.
* `stale_lot` ne se déclenche qu'après 6 h et ne regarde pas si l'assigné peut
  tourner.
* Aucun envoi vers un humain : ni Slack, ni webhook, ni notification de bureau.
  Le seul envoi est le NOTIFY Postgres vers les exécuteurs. Les alertes se
  lisent en tirant `ameesh alerts`. Cela contredit 0014 (« alertes à l'humain
  responsable concerné »).

## 4. La reprise n'est pas une opération ameesh

* Il n'y a ni `adopt` ni `resume`. Il existe `agent-runner register <nom>
  <harnais> --session S`, qui écrit l'identifiant d'une session que
  l'exécuteur reprendra, et `ameesh attach`, qui prend le bail et ouvre le
  harnais interactif sur la même session : c'est le modèle de 0014, déjà codé
  mais pas imposé. `register` sans `--session` garde la session enregistrée
  (`coalesce`), et `ameesh restart`, qui l'oublie, est refusé pour un agent
  arrêté : il n'y a donc aucun moyen propre de repartir d'une session neuve
  pour un agent arrêté.
* Le compte d'une session se lit dans le marqueur d'`events.jsonl`
  (`account_turn._session_account`) ; une session que l'exécuteur n'a pas
  lancée n'en a pas. Codex : une session n'est **jamais** portable vers un
  autre `CODEX_HOME` (`accounts.session_portable`) ; la bascule fait alors
  une rotation **avec un résumé produit sous l'ancien compte**, ce qui échoue
  si l'ancien compte est inutilisable.
* Le descripteur Codex déclare `interactive: []` (attach admis) et
  `session.flag: ["resume"]` : le « interactif : - » vu par le propriétaire est
  un affichage, pas une limite.

## 5. Cause profonde du cloisonnement et de la « surdité » des sessions externes

* Les hooks de ce poste (`~/.claude/settings.json`, `~/.codex/hooks.json`,
  `~/.codex-secondary/hooks.json`) appellent `~/.local/bin/agent-mail`, qui
  est **encore la v0** (coexistence, [BASCULE.md](../../BASCULE.md), avant
  l'étape 8). La v0 **déduit l'identité du dossier** : `AGENT_MAIL_NAME`, sinon
  un alias de `~/.config/agent-mail/aliases.tsv` par préfixe de chemin,
  sinon le nom du dossier. L'alias `orchestrateur → /home/smichea/Work`
  (chantier nexlink) capture tout ce qui vit sous `~/Work`, et un dossier
  nommé comme un agent Nexlink prend son identité. C'est l'explication la plus
  probable du courrier d'une autre équipe injecté dans une session du
  chantier bloqué, sans que le dossier exact soit vérifié.
* La v0 lit la boîte **fichier**. Le courrier v1 (Postgres, `ameesh mail send`)
  n'est remis que par l'exécuteur, dans la consigne d'un tour, donc **à un
  agent qui a un bail**. Une session externe est donc sourde à sa boîte v1
  par construction : même réveillé, l'orchestrateur n'aurait pas vu ses
  messages sans lancer `ameesh mail inbox` à la main.
* Rien dans le code d'ameesh ne lie une identité à l'identifiant de session
  ou à l'ascendance des processus ; seul `find_tty` remonte les processus, pour
  le titre du terminal. **Hors du dépôt**, un pont local écrit le
  2026-10-07 (`~/.local/bin/ameesh-session-mail-hook`, table
  `~/.config/ameesh/external-session-bindings.json`) le fait : il lie
  l'identifiant de session du harnais et un PID ancêtre à un nom, pose
  `AGENT_MAIL_NAME`, puis appelle l'`agent-mail` v0. Il n'est branché que
  sur les configurations Claude secondaire et tertiaire, pas sur Codex :
  l'orchestrateur n'en profitait pas. Et même lié, il ne remet que la boîte
  fichier v0, jamais le courrier v1. L41 reprend cette idée dans ameesh. Côté v1 : `send all` n'a **aucun filtre d'équipe ou de
  chantier** (`backend.py:230`), et `bk.register(..., session_id)` d'un hook
  peut **écraser** le `session_id` d'un agent de l'exécuteur, que celui-ci
  reprendra au tour suivant.

## 6. `ameesh list`

`mesh_cli.py:155-170` est dans la boucle des agents : le total global des lots
(`work.delays`, sans filtre) est imprimé, et recalculé, à chaque ligne.

# Décision proposée (adoptée le 2026-10-07 : [0030](../decisions/0030-pas-de-travail-sans-reveil-possible.md))

**« Pas de travail sans réveil possible. »** Elle prolonge 0014 : ce que 0014
annonçait pour les orchestrateurs devient une règle vérifiée par ameesh pour
tout porteur de travail.

1. **Mode d'agent explicite.** Chaque agent est `exécuté` (mené par un
   exécuteur, sous bail) ou `externe` (session humaine, boîte seulement). Le
   mode est une colonne, pas un texte libre. Un agent externe a
   **obligatoirement** un responsable humain, et `ameesh list` l'affiche
   comme tel.
2. **Attribution gardée.** Un lot, une délégation ou un rôle d'orchestrateur ne
   peut être confié qu'à un agent **réveillable** : connu, en mode `exécuté`,
   admis sur un hôte dont l'exécuteur vit. Les autres cas sont **refusés**
   avec la raison. Seul le responsable humain peut forcer une attribution à un
   agent externe (`--externe`). Le lot affiche alors « attend : <humain> (session
   externe) », et c'est cet humain qui reçoit les alertes. P1 (« pas
   d'orchestrateur externe sans bail ») en est le cas général.
3. **Adoption et reprise sont des opérations d'ameesh, jamais des prompts.**
   * `ameesh adopt <agent> --session <id> --account <compte> [--cwd D]` fait
     passer une session interactive existante en agent `exécuté`. Elle
     enregistre la session **et son compte d'origine**, exige que la session
     interactive soit fermée (aucun processus du harnais sur ce fichier de
     session), puis réveille l'agent. L'humain y revient par `ameesh attach`.
   * `ameesh resume <agent>` relance un agent arrêté sur sa session
     enregistrée et son compte, et lie l'identité à la nouvelle exécution
     (variables d'identité et de bail posées par l'exécuteur). Si le compte
     d'origine est au seuil, la règle de 0027 s'applique : même session si
     elle est portable, sinon rotation. Le résumé est fait sous l'ancien compte
     s'il reste utilisable. Sinon, **brief de reprise déterministe** construit
     par ameesh : fil du projet, lots, derniers messages, et chemin du
     transcript d'origine en lecture. On ne fouille plus `~/.codex*/sessions`
     à la main.
4. **Alertes de vivacité, poussées à l'humain responsable.**
   * Raison d'arrêt structurée : `manuel`, `bail_expiré`, `retiré_du_canon`,
     `externe`, `erreur`.
   * `stopped_with_mail` : agent arrêté ou mort avec du courrier non lu plus
     vieux que le seuil, sauf arrêt `manuel`.
   * `orphan_lot` : lot ouvert dont l'assigné est inconnu, arrêté, externe sans
     humain joignable, ou sans tour sur ce lot depuis N min (défaut 30). Le
     seuil est bien plus court que les 6 h de `stale_lot`.
   * `delegation_expired` (voir 5).
   * **Envoi** par un service `ameesh notify` (unité systemd, comme
     l'exécuteur), qui suit les alertes et les achemine vers le responsable
     humain de l'agent ou du lot. Destinations : notification de bureau, ntfy,
     Slack par webhook entrant (0004). Il évite les doublons, envoie la
     résolution et limite le débit. Les secrets restent ceux de l'hôte.
5. **Délégation à échéance.** `ameesh work delegate <lot> <agent> --within
   30m` enregistre le délégant, le délégué et l'échéance. À l'échéance, sans
   tour du délégué sur ce lot (`session_work_item`), le lot **revient au
   délégant**. Un événement part dans sa boîte, ce qui réveille un agent
   exécuté, et l'alerte part au responsable. `work assign` (réassignation)
   rejoint la CLI, gardée par la règle 2.
6. **Cloisonnement : l'identité ne vient jamais du dossier.**
   * Les hooks passent à la v1 (étape 8 de la bascule, en avance pour les
     hooks), puisque la v1 ne déduit déjà aucune identité du dossier.
   * Session externe : liaison explicite `ameesh mail bind <agent>`, qui lie
     l'identifiant de session du harnais (reçu par le hook en JSON) au nom.
     Le hook ne remet que si cette liaison existe ; sans identité liée, il ne
     remet **rien**.
   * Un hook n'écrit plus le `session_id` d'un agent `exécuté`.
   * `send all` est limité à l'équipe ou au chantier de l'expéditeur.
7. **`ameesh list`** : une ligne globale de lots, et le nombre de lots ouverts
   par agent sur sa ligne.

# Plan de lots proposé

Les migrations 0030 et 0031 sont réservées. Chaque lot : branche
`lot/L3x-…`, tests, revue indépendante, puis fusion **sur accord du
propriétaire**.

| Lot | Contenu | Dépend de | Taille |
|---|---|---|---|
| **L36** — correctifs immédiats | P6 (`list` : total une fois, compte par agent) ; étiquettes « attend » qui disent *inconnu* / *arrêté* / *externe* ; avertissement sur `work add` pour un assigné inconnu ; le hook n'écrase plus le `session_id` d'un agent de l'exécuteur ; `send all` limité au chantier | — | S |
| **L37** — mode, raison d'arrêt, alertes de vivacité | migration 0030 : `agent_registry.mode`, `stop_reason`, responsable requis pour `externe` ; `stopped_with_mail`, `orphan_lot` ; `work assign` ; règle 2 (refus, `--externe` par le responsable) | L36 | M |
| **L38** — alertes poussées | `ameesh notify` (systemd) : desktop, ntfy, Slack webhook ; acheminement au responsable humain ; doublons, résolution, débit ; `doctor --notify-test` étendu | L37 | M |
| **L39** — adoption et reprise | `ameesh adopt`, `ameesh resume` ; compte d'origine de la session en registre ; contrôle de session fermée ; brief de reprise déterministe quand l'ancien compte est inutilisable | L37 | M–L |
| **L40** — délégation à échéance | migration 0031 : `delegated_by`, `due_at` sur `work_items` ; `work delegate` ; échéance traitée par l'exécuteur (retour au délégant, événement, `delegation_expired`) | L37 | M |
| **L41** — cloisonnement des hooks | `ameesh mail bind` (liaison par identifiant de session du harnais) ; le hook v1 ne remet rien sans liaison ; procédure de bascule des hooks vers la v1 (`BASCULE.md`, étape 8 avancée, avec retour arrière) | L36 | M |

Ordre conseillé : L36, puis L37, puis L38 / L39 / L41 en parallèle, puis L40.
L36 et L38 suffisent à ce qu'un tel blocage ne passe plus
inaperçu ; L39 et L41 suppriment la cause.
