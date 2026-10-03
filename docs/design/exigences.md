---
type: Specification
title: "Exigences d'ameesh (R1–R17)"
description: "Les exigences de la spec mesh d'origine (R1–R7), généralisées, et celles ajoutées par les décisions du 2026-10-03 (R8–R17)."
status: draft
tags: [exigences, specification]
generated: { by: "mesh-design/claude-opus-5-5", at: "2026-10-04T00:30:00+02:00" }
sources:
  - { resource: "../../MESH-SPEC.md", title: "Spec mesh d'origine (§4, R1–R7)" }
  - { resource: "decisions/index.md", title: "Décisions du propriétaire" }
---

# Exigences d'origine, généralisées

| # | Exigence | Changement depuis la spec d'origine |
|---|---|---|
| R1 | Les **membres** d'une équipe (humains et agents) travaillent depuis plusieurs machines — appareils, PC, serveurs, pods — et échangent en temps réel. | « agents » → « membres » ([0002](decisions/0002-portee-equipes-mixtes.md)) |
| R2 | N'importe quel harnais, par ses **standards** : MCP pour le sortant, pilotage par stream-json, app-server ou **ACP** pour le réveil. | adaptateurs → standards ([0007](decisions/0007-standards-des-harnais.md)) |
| R3 | Un agent tourne à un seul endroit à la fois (bail) ; un **objet métier** (ticket, préparation) est traité par un seul membre à la fois. | baux sur objets externes |
| R4 | L'autorité d'un humain est **prouvée** par une signature faite sur un appareil que les agents ne contrôlent pas, liée à l'empreinte exacte de ce qui est approuvé. | propriétaire → tout humain habilité ; clé hors de portée des agents |
| R5 | Rien ne se perd : courrier durable, tour rejouable, bail qui expire. Une **action irréversible** suit le protocole d'exécution ci-dessous : jamais exécutée deux fois, jamais réessayée à l'aveugle quand son issue est inconnue. | idempotence et issue inconnue |
| R6 | Transport interchangeable : Nexlink (natif), pile open source (Matrix, Zulip), outils courants (Slack, Teams, Google Chat, e-mail). | [0003](decisions/0003-ameesh-et-nexlink.md), [0004](decisions/0004-open-source-et-outils-courants.md) |
| R7 | Budget et modèle par agent ; vue des abonnements ; budget par projet. | budget par projet |

# Exigences ajoutées le 2026-10-03

| # | Exigence | Source |
|---|---|---|
| R8 | Humains et agents sont des membres de même rang dans le modèle (adresse, présence, délai de réponse) ; **l'autorité de décision est réservée aux humains** : un agent n'a jamais la capacité `approve`. Un humain peut signer une **approbation permanente bornée** (classe d'actions, plafond, échéance, révocable) que la porte applique ; l'autorité reste alors la signature de l'humain, jamais celle de l'agent. | [0002](decisions/0002-portee-equipes-mixtes.md) |
| R9 | Plusieurs organisations et plusieurs équipes, avec cloisonnement des données. | [0003](decisions/0003-ameesh-et-nexlink.md) |
| R10 | Les appareils des humains sont des points d'accès à part entière : notification, lecture, approbation signée depuis le téléphone. | [étude autorité](etudes/autorite-humaine.md) |
| R11 | Le domaine de travail n'est pas codé en dur : pipelines, types de livrables, règles de revue et politique d'autonomie se configurent par équipe, dans le canon. | [0002](decisions/0002-portee-equipes-mixtes.md) |
| R12 | **Lisibilité humaine** : tout échange, y compris d'agent à agent, se fait en langage naturel dans un fil lisible et auditable par l'équipe ; aucun canal caché. | [0006](decisions/0006-lisibilite-humaine.md) |
| R13 | **Paroles signées** : chaque message porte une signature de provenance ; chaque décision une signature d'autorité. | [0009](decisions/0009-paroles-signees.md) |
| R14 | **Responsabilité humaine** : tout agent, orchestrateur compris, a un humain responsable — déclaré dans sa fiche, ou, pour un agent éphémère sans fiche, **hérité de l'agent qui l'a créé** et enregistré par ameesh à sa création. Un agent sans responsable résolu ne tourne pas. | [0010](decisions/0010-responsabilite-et-orchestrateurs.md) |
| R15 | **Projets vivants** : un projet a des humains responsables, un ou plusieurs orchestrateurs, des membres (parfois partagés), un canon et des dépendances ; il se crée, se scinde, fusionne ou s'archive avec une filiation tracée. | [0010](decisions/0010-responsabilite-et-orchestrateurs.md) |
| R16 | **Décisions sans relais** : une décision prise dans n'importe quelle conversation humain–agent est enregistrée dans le canon du projet (une décision par fichier) ; les orchestrateurs la lisent là. | [0010](decisions/0010-responsabilite-et-orchestrateurs.md) |
| R17 | **Attention humaine** : chaque humain a une file unique des décisions qui l'attendent, tous projets confondus. | [0010](decisions/0010-responsabilite-et-orchestrateurs.md) |

# Protocole d'exécution d'une action irréversible (R5)

Une action irréversible (envoi au client, remboursement, étiquette, réservation
d'enlèvement, fusion) passe par la porte (ameesh-gate) et par des états
**journalisés de façon durable avant chaque effet** :

```
proposée → approuvée → lancée → confirmée | échouée | inconnue
```

1. **Clé d'idempotence** : chaque action approuvée reçoit une clé dérivée du
   reçu (son nonce). Elle est transmise au système externe quand il le permet
   (`Idempotency-Key` d'une API de paiement, `Message-ID` d'un e-mail, référence
   client d'une expédition).
2. **« lancée » est écrit avant l'appel externe.** Le reçu est consommé à ce
   moment : il ne peut plus servir à une autre exécution.
3. **Issue inconnue** (effet peut-être fait, réponse perdue : coupure, délai
   dépassé) : l'état passe à `inconnue` et **aucune nouvelle tentative
   automatique** n'a lieu, sauf si le connecteur garantit la déduplication par
   la clé d'idempotence.
4. **Réconciliation** : le connecteur déclare comment retrouver l'issue par la
   clé (chercher le `Message-ID` dans les envoyés, l'expédition par sa référence,
   le remboursement par sa clé). Résultat trouvé → `confirmée` ou `échouée` ;
   introuvable ou connecteur sans réconciliation → **attente d'un humain**, avec
   le contexte, dans sa file de décisions (R17).
5. Une nouvelle tentative après `échouée` ou une décision humaine sur
   `inconnue` exige une **nouvelle approbation** (nouveau reçu, nouvelle clé).

Chaque connecteur d'action irréversible déclare donc, dans le canon : s'il
accepte une clé d'idempotence, et comment réconcilier une issue inconnue.

# Principes d'architecture qui en découlent

1. **Trois couches** : le **canon** (OKF dans git, fédéré), la **communication**
   (fils lisibles sur Nexlink, Matrix, Slack…), l'**exécution** (ameesh).
2. ameesh **n'écrit dans le canon que par proposition** revue.
3. Les **fiches** (agents, équipes, lots) sont dans le canon ; leur **état** est
   dans ameesh.
4. Les intégrations sont des **serveurs MCP standard** ; ameesh fournit le
   réveil (runner), le fil (ameesh-fil), la porte (ameesh-gate) et la preuve
   (ameesh-approve).
