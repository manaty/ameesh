---
type: Specification
title: "Exigences d'ameesh (R1–R23)"
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
| R5 | Rien ne se perd : courrier durable, tour rejouable, bail qui expire. Une **action irréversible** suit le protocole d'exécution ci-dessous : elle n'est **jamais relancée tant que le risque de doublon n'est pas levé** (déduplication garantie ou réconciliation) **ou explicitement assumé par un humain habilité**. | idempotence et issue inconnue |
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
| R18 | **Placement gouverné** : quel agent tourne sur quel hôte, et avec une clé d'API ou un forfait, est décidé par le responsable ameesh du projet (ou l'infra à qui il délègue), dans les limites de la politique posée par le responsable de chaque hôte ; ameesh facilite cette gestion et la supervise. | [0014](decisions/0014-orchestrateurs-a-tours-et-placement.md) |
| R19 | **Vitesse** : revue proportionnée à la classe de risque (déclarée au canon par portée de fichiers) ; interruption de tour par message prioritaire ; rotation de session avec résumé de reprise ; sessions robustes au déplacement ; délais par lot mesurés. | [0018](decisions/0018-vitesse-des-agents.md) |
| R20 | **Budgets** : modèle et effort par tâche ; plafonds par harnais, agent et projet ; jamais de dépassement d'un forfait, lu à sa source ; comptabilité par tour. | [0019](decisions/0019-budgets-et-routage.md) |
| R21 | **Catalogue des modèles** : ameesh liste, découvre et évalue les modèles et niveaux d'effort sur des tâches de référence, et recommande modèle et effort par classe de tâche ; la politique de routage reste au canon. | [0020](decisions/0020-catalogue-et-evaluation-des-modeles.md) |
| R22 | **Catalogue des harnais** : les harnais ne sont plus codés en dur ; un service centralisé les décrit, les certifie par un banc de conformité, les évalue avec les modèles et les met à disposition (installation signée, version épinglée). | [0021](decisions/0021-catalogue-des-harnais.md) |
| R23 | **Auto-réparation** : les bugs remontés par les interactions sont détectés, dédupliqués, triés selon une politique au canon, réparés, vérifiés, livrés et surveillés par des agents ; aucune décision humaine déduite d'un texte ou du silence pour une action irréversible. | [0022](decisions/0022-auto-reparation.md) |

# Protocole d'exécution d'une action irréversible (R5)

Une action irréversible (envoi au client, remboursement, étiquette, réservation
d'enlèvement, fusion) passe par la porte (ameesh-gate) et par des états
**journalisés de façon durable avant chaque effet** :

```
proposée → approuvée → lancée → confirmée | échouée | inconnue
```

Deux identifiants distincts :

- l'**identité de l'action** (`action_id`) : attribuée à la proposition, **stable
  à travers toutes les tentatives** de la même action. C'est la **clé
  d'idempotence** transmise au système externe quand il le permet ;
- le **nonce d'autorisation** : propre à chaque reçu signé, consommé une fois.
  Un reçu autorise une tentative d'une action donnée ; il ne crée jamais une
  nouvelle action.

1. **« lancée » est écrit avant l'appel externe** ; le reçu est consommé à ce
   moment et ne peut plus servir.
2. **Issue inconnue** (effet peut-être fait, réponse perdue) : état `inconnue`,
   **aucune nouvelle tentative automatique**.
3. **Déduplication garantie** : si le connecteur déclare que le système externe
   déduplique sur la clé d'idempotence (par exemple un en-tête
   `Idempotency-Key` documenté par une API de paiement), une nouvelle tentative
   **de la même action** — même `action_id`, nouveau reçu — est sans risque de
   doublon.
4. **Réconciliation** : sinon, le connecteur retrouve l'issue par une lecture
   (chercher l'e-mail dans les envoyés par son `Message-ID`, l'expédition par sa
   référence). Trouvée → `confirmée` ou `échouée`.
5. **Ni déduplication ni réconciliation concluante** : attente d'un humain
   (R17), avec le contexte. Seule une **décision explicite d'un humain habilité,
   qui assume le risque de doublon**, peut créer une **nouvelle action**
   (nouvel `action_id`) ; cette décision est signée et liée à l'action
   `inconnue` qu'elle remplace.
6. Après `échouée` (échec certain, aucun effet), une nouvelle tentative de la
   même action demande un nouveau reçu ; elle garde le même `action_id`.

**`Message-ID` n'est pas une garantie de déduplication** : les serveurs SMTP ne
dédupliquent pas sur cet en-tête. Il ne sert que de **poignée de réconciliation**.

Chaque connecteur d'action irréversible déclare donc, dans le canon : si le
système externe **garantit** la déduplication sur une clé (avec la référence de
sa documentation), et comment réconcilier une issue inconnue.

# Principes d'architecture qui en découlent

1. **Trois couches** : le **canon** (OKF dans git, fédéré), la **communication**
   (fils lisibles sur Nexlink, Matrix, Slack…), l'**exécution** (ameesh).
2. ameesh **n'écrit dans le canon que par proposition** revue.
3. Les **fiches** (agents, équipes, lots) sont dans le canon ; leur **état** est
   dans ameesh.
4. Les intégrations sont des **serveurs MCP standard** ; ameesh fournit le
   réveil (runner), le fil (ameesh-fil), la porte (ameesh-gate) et la preuve
   (ameesh-approve).
