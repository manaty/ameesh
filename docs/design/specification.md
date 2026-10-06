---
type: Specification
title: "Spécification d'ameesh v1"
description: "Ce que livre ameesh v1 : canon OKF, membres et responsabilité, placement gouverné, fil lisible, actions irréversibles sous porte, reçus d'approbation, orchestrateurs à tours, interface de stockage — avec le plan de lots."
status: draft
tags: [specification, v1, architecture, modele-de-donnees]
generated: { by: "mesh-design/claude-opus-5-5", at: "2026-10-04T01:45:00+02:00" }
sources:
  - { resource: "exigences.md", title: "Exigences R1–R18" }
  - { resource: "decisions/index.md", title: "Décisions du propriétaire" }
  - { resource: "../V1-MAILBOX-RUNNER.md", title: "Banc actuel : boîte et exécuteur" }
  - { resource: "../V1-AUTORITE-MESH.md", title: "Banc actuel : autorité, mesh list, work_items" }
---

# 1. Objet et vocabulaire

**ameesh v1** est la première version qu'on peut **mettre en service sur un vrai
projet** (bascule du chantier actuel depuis les scripts v0). Elle part du
**banc actuel** (boîte Postgres, exécuteur, baux, signatures Ed25519,
`work_items`, CLI `ameesh`, 160+ tests) et lui ajoute ce que les décisions
0001–0016 imposent pour une première mise en service.

| Terme | Sens |
|---|---|
| **canon** | les dépôts OKF fédérés (OKF Federation) qui décrivent équipes, membres, hôtes, placements, lots, décisions ([0005](decisions/0005-canon-okf.md)) |
| **membre** | un humain (`human:<id>`) ou un agent (`agent:<id>`) |
| **fiche** | la déclaration d'un membre, d'un hôte ou d'un placement dans le canon |
| **état** | ce qui change à chaque tour (bail, session, statut, dépense) : dans la base d'ameesh, jamais dans le canon |
| **hôte** | une machine ou un cluster, avec un humain responsable et une politique |
| **placement** | « tel agent tourne sur tel hôte, avec tel mode d'identifiants » |
| **action** | un effet sur le monde extérieur (envoi, remboursement, fusion…), classé lecture / réversible / irréversible / coûteux |
| **reçu** | la preuve signée qu'un humain a approuvé une tentative d'action |
| **fil** | la suite lisible des messages d'un lot ou d'un projet (R12) |

# 2. Périmètre

## 2.1 Dans la v1

| # | Capacité | Exigences | Lot |
|---|---|---|---|
| C1 | Interface de stockage : tout le SQL derrière des opérations nommées | [0016](decisions/0016-stockage.md) | L1 |
| C2 | Lecture du canon : fiches `Agent`, `Host`, `Placement`, rôles et politiques de revue de la fédération ; validation | R8, R14, R18 | L2 |
| C3 | Responsabilité : un agent sans humain responsable résolu n'est pas réclamable ; agents éphémères à responsable hérité | R14 | L2 |
| C4 | Placement gouverné : un agent dont le placement viole la politique de l'hôte n'est pas réclamable | R18 | L3 |
| C5 | Fil lisible : tout message (y compris agent↔agent) est écrit dans un fil lisible et consultable ; interface de transport | R12, R6 | L4 |
| C6 | Actions et porte : registre des actions, états durables, identité stable, idempotence, issue inconnue, réconciliation | R5 | L5 |
| C7 | Reçus d'approbation : format `ameesh-receipt/1`, vérification WebAuthn (ES256) et Ed25519, registre d'authentificateurs lu dans le canon, approbations permanentes bornées | R4, R8, R13 | L6 |
| C8 | Service `ameesh-approve` minimal : page d'approbation WebAuthn, notifications par le fil | R10, [0012](decisions/0012-autorite-par-ameesh-approve.md) | L7 |
| C9 | Orchestrateurs à tours : événements dans la boîte, `ameesh attach` (bail interactif) | [0014](decisions/0014-orchestrateurs-a-tours-et-placement.md) | L8 |
| C10 | File des décisions par humain : `ameesh decisions` (actions en attente d'approbation, lots en attente d'humain) | R17 | L5 |
| C11 | Bascule réécrite : canon d'amorçage, pas de cérémonie Ed25519, attach | — | L9 |

## 2.2 Après la v1 (v1.x)

Installateur conduit par le harnais (0011) ; connecteurs Slack, Matrix, Nexlink,
e-mail (0004) ; porte exposée en proxy MCP et serveur MCP `ameesh-fil` (0007) ;
pilotage par app-server (Codex) et ACP ; pilote SQLite (0016) ; un utilisateur
Unix par agent et signatures de provenance actives (0009) ; génération des
skills / AGENTS.md / plugins depuis le canon ; application mobile native de
référence (0015).

# 3. Architecture

```
            canon OKF (git, fédéré)                fils lisibles
   fiches Agent/Host/Placement, rôles,      (fichiers en v1 ; Slack, Matrix,
   politiques, lots, décisions, clés        Nexlink en v1.x)
                 │ lecture                         ▲ écriture/lecture
                 ▼                                 │
  ┌──────────────────────────── ameesh ───────────────────────────────┐
  │ canon.py ── placement.py ── runner (exécuteur par hôte, baux, tours)│
  │ fil.py (transports) ── mail.py (file d'attente, hooks v0)          │
  │ actions.py (porte, registre, réconciliation)                       │
  │ receipts.py (vérification) ◄── ameesh-approve (service séparé)     │
  │ storage/ (interface) ── pilote Postgres (psycopg | psql)           │
  └────────────────────────────────────────────────────────────────────┘
```

Règles d'architecture : ameesh **lit** le canon et n'y écrit que par
proposition (branche/PR) ; l'**état** est dans la base ; les **secrets** ne
sont ni dans le canon ni dans la base (trousseau ou gestionnaire de l'hôte) ;
`ameesh-approve` tourne **hors de portée des agents** (autre utilisateur Unix ou
autre hôte) et ne partage avec ameesh que des reçus.

# 4. Le canon

## 4.1 Où ameesh le lit

`AMEESH_CANON` (ou `canon` dans la configuration) désigne la racine d'un bundle
OKF ; si un `federation.yaml` s'y trouve, ameesh suit ses membres présents
localement (`workspace_path`). Lecture **seule**, au démarrage et sur demande
(`ameesh canon reload`), jamais au milieu d'un tour.

**Source approuvée** : ameesh ne lit pas les fichiers de travail du clone. Il
lit le canon **à la révision fusionnée de la branche canonique** (le `ref` du
manifeste, par exemple `origin/main` après `git fetch`), par les objets git
(`git show <commit>:<chemin>`), et enregistre ce commit (`canon_ref`) avec
chaque donnée synchronisée. Un arbre de travail modifié ou un commit absent de
la branche canonique distante est **ignoré** (diagnostic, aucune donnée prise
en compte). La branche canonique est protégée par la politique de revue de la
fédération (aucun agent n'a la capacité de fusionner). Limite v1 (§11) : tant
que les agents partagent l'utilisateur Unix d'ameesh, ils peuvent altérer le
clone ou la configuration git ; seule une vérification faite par un composant
isolé (`ameesh-approve`, autre utilisateur) est opposable à un agent
malveillant. Un canon invalide n'arrête
pas les agents déjà réclamés : il empêche les nouvelles réclamations et produit
un diagnostic.

## 4.2 Profil ameesh d'OKF (types et clés)

Tous les concepts sont des fichiers OKF v0.2 ordinaires ; ameesh ne lit que le
frontmatter. Clés inconnues conservées et ignorées.

**`type: Agent`** — une fiche par agent, nom de fichier libre.

```yaml
type: Agent
title: deepseek7                 # nom de l'agent (grammaire du registre)
responsible: human:smichea       # REQUIS (R14)
team: ameesh                     # équipe ou projet
capabilities: [read, report-drift, propose]   # `approve` interdit (R8)
harness: deepseek                # claude | codex | deepseek | …
model: deepseek-…                # facultatif
provider: deepseek               # fournisseur facturant le modèle
credential_mode: api-key         # api-key | subscription
budget_usd_per_day: 20           # facultatif
tools: [git, mcp:transport-readonly]   # outils autorisés (noms de politique)
reviewers: [codex3]              # facultatif
```

**`type: Host`**

```yaml
type: Host
title: pc-smichea
responsible: human:smichea       # REQUIS
policy:
  harnesses: [claude, codex, deepseek]   # absent = tous
  providers: [anthropic, openai, deepseek]
  credential_modes: [api-key, subscription]
  max_agents: 12
```

**`type: Placement`**

```yaml
type: Placement
title: deepseek7@pc-smichea
agent: deepseek7
host: pc-smichea
credential_mode: api-key
cwd: ~/src/acme                         # dossier de travail sur l'hôte
```

**`type: Member`** (humains) — `title: smichea`, `roles: [...]`,
`authenticators:` liste d'empreintes de clés publiques enrôlées (C7).

**`type: WorkPackage`** (plan de travail, L29 ; [modèle et commandes](../PLAN-DE-TRAVAIL.md))

```yaml
type: WorkPackage
title: "Recherche dans le catalogue"
kind: lot                        # milestone | epic | lot
parent: catalogue                # autre fiche WorkPackage (lot → epic|jalon, epic → jalon)
responsible: human:alice         # REQUIS
team: acme-web                   # facultatif
scope: ["src/catalogue/**"]      # globs, facultatif
status: draft                    # déclaratif, facultatif
id: cat-recherche                # facultatif (défaut : nom du fichier)
```

Recopiée par `canon sync` dans `work_packages` (0026) ; ses erreurs ne
bloquent aucun agent. L'état des lots reste dans `work_items`, projeté sur
GitHub (une issue par lot, sous-issues de l'epic) qui reste une vue.

Les rôles et politiques de revue viennent de `federation.yaml` (OKF Federation) ;
ameesh ne les redéfinit pas.

**`review_policies.risk_classes`** (profil ameesh, décision 0018) — la revue qu'un
changement demande, **par portée de fichiers** :

```yaml
review_policies:
  self_approval: forbidden        # OKF Federation : conservé, non interprété ici
  risk_classes:
    default: normal               # classe d'un fichier qu'aucune règle ne nomme
    rules:
      - paths: ["supabase/migrations/**", "**/*.sql"]
        class: sensible
      - paths: ["docs/**"]
        class: light
```

`default` est facultatif (`normal` par défaut) ; une politique déclarée sans lui
donne un **avertissement** de `canon check`, pas une erreur.

Les classes sont `light` (léger : fusion dès que les tests ciblés sont verts,
relecture après coup), `normal` (une revue d'un autre éditeur) et `sensitive`
(sensible : gel, revue avant fusion, accord explicite). La classe d'un fichier
est la **plus haute** des règles qui matchent, celle d'un changement la plus
haute de ses fichiers — en cas de doute, la classe supérieure. `canon check`
valide la déclaration ; `ameesh review-class <fichiers…|--diff REF>` calcule la
classe d'un changement (`--diff REF` lit `merge-base(REF, HEAD)` → arbre de
travail, renommages compris, plus les fichiers non suivis). Les motifs sont des
globs de chemin de dépôt : `*` ne traverse pas `/`, `**` oui, et `docs` ne
couvre pas `docs/a.md` (écrire `docs/**`).

**Les jalons d'un lot** (R19, migration 0013) sont **écrits**, pas déduits d'un
état : `requested` (création) et `merged` (entrée dans l'état) sont automatiques
par trigger ; `frozen` (branche gelée pour la revue) et `verdict` (`ok` ou
`blocked`, avec le commit relu) se déclarent par
`ameesh work milestone <id> frozen|verdict [ok|blocked] --sha S [--note …]`.
Les durées `demande → gel`, `gel → revue`, `gel → fusion` et le nombre de
verdicts bloqués se lisent dans `ameesh work list` (colonne DÉLAI), `ameesh work
show` (frise complète) et le pied de `ameesh list` ; la forme complète est
rendue par l'opération de stockage `WorkItems.delays` (L24 et Nexlink la
consomment).

## 4.3 Validation (`ameesh canon check`)

Erreurs bloquantes : fiche `Agent` sans `responsible` ou avec `approve` dans
`capabilities` ; `responsible` qui ne résout pas vers un `Member` humain ;
`Placement` vers un agent ou un hôte inconnu ; placement qui viole la politique
de l'hôte (C4) ; deux placements pour un même agent ; **`harness` non vide sans
descripteur connu** (`agent-harness-unknown`, `host-harness-unknown`, L16).
Avertissements : hôte sans placement, agent sans placement, agent sans
`harness`. Sortie lisible et `--json` (code, gravité,
fichier, explication), sur le modèle du validateur OKF Federation.

## 4.4 Du canon vers le registre

`ameesh canon sync` (et le démarrage de l'exécuteur) met à jour, pour chaque
agent placé sur l'hôte courant, les colonnes **déclaratives** du registre
(`harness`, `host`, `cwd`, `model`, `budget`, `responsible`, `team`,
`credential_mode`, `canon_ref` = chemin + SHA du fichier). Il ne touche jamais
aux colonnes d'**état** (bail, session, statut, dépense, consigne). Un agent
retiré du canon passe `stopped` à la fin de son tour en cours.

**Agents éphémères** : `ameesh agent spawn <nom> --by <agent-créateur>` crée une
ligne `ephemeral = true`, `responsible` = celui du créateur (copié à la
création, R14), capacités limitées à `read` et `propose`, échéance obligatoire.

# 5. Exécution

Inchangé par rapport au banc actuel, sauf :

- **Réclamation** (`claimable`) : un agent n'est réclamable que si `responsible`
  est résolu (C3) et que son placement est admis par la politique de l'hôte
  (C4). La raison du refus est écrite dans `status_text`.
- **Invariant de bail (cible)** : aucun processus du groupe d'un agent n'est
  vivant après l'échéance connue de son bail, sauf renouvellement réussi,
  **à une marge d'ordonnancement près** (délai entre l'échéance et l'effet du
  SIGKILL, documenté et mesuré par les tests). La garantie absolue n'est pas
  promise : suspension arbitraire du processus par le système, et chemins
  encore ouverts au moment de la rédaction (revue B5a-S/P), sont des limites
  documentées.
- **Événements** (C9) : `kind = 'event'` dans la boîte ; ils réveillent l'agent
  comme un message, avec un **regroupement** : au plus un réveil par
  `AMEESH_EVENT_COALESCE` secondes (défaut 120) sauf événement marqué `urgent`.
- **`ameesh attach <agent>`** (C9) : réclame le bail pour un exécuteur
  « interactif » (`runner_id = attach:<utilisateur>@<hôte>`), suspend la
  réclamation automatique de cet agent, lance le harnais en interactif sur la
  **même session**, renouvelle le bail tant que la session vit, puis rend le
  bail. Refusé si un tour est en cours, sauf `--wait`.
- **Harnais** (L16, R22) : plus de liste fermée. Un harnais est un
  **descripteur** (manifeste ACP étendu, [documentation](descripteurs-de-harnais.md))
  livré dans le paquet ou déposé par l'hôte ; l'adaptateur en ligne de commande
  est piloté par le descripteur, et tout agent qui parle **ACP** passe par le
  pont générique (`session/new|load|resume`, `session/prompt`,
  `session/update`, permissions refusées par défaut, annulation propre). Un
  `harness` inconnu des descripteurs est une erreur de `canon check`.

# 6. Fil lisible (C5)

Chaque message déposé par `mail.send` (et chaque événement, action, reçu) est
aussi **écrit dans un fil** par un **transport**. Interface :

```python
class Transport(Protocol):
    name: str
    def post(self, thread: ThreadRef, entry: Entry) -> str: ...      # renvoie l'id externe
    def read(self, thread: ThreadRef, since: str | None) -> list[Entry]: ...
```

`ThreadRef` = (projet, lot) ; à défaut de lot, le fil du projet. `Entry` porte
l'auteur (`human:`/`agent:`), l'horodatage, le texte en langage naturel
(autosuffisant) et des métadonnées structurées **en plus**.

Transport v1 : **`file`** — un fichier Markdown par fil sous
`AMEESH_THREADS` (défaut `~/.local/state/ameesh/fils/<projet>/<lot>.md`),
ajout seulement, une entrée = un titre `### <date> — <auteur> → <destinataire>`
puis le texte. `ameesh fil show <projet> [<lot>]`, `ameesh fil tail`. La boîte
Postgres reste la **file de distribution** (hooks, réveil) ; le fil est la
**référence lisible**. En v1.x : transports Slack, Matrix, Nexlink derrière la
même interface ; l'id externe est stocké dans `agent_mailbox.meta`.

Règle : un message dont le corps n'est pas du texte lisible (JSON seul, binaire,
encodage) est refusé par `mail.send` (`--allow-structured` réservé aux tests).

# 7. Actions et porte (C6)

## 7.1 Modèle

Table `actions` : `action_id` (texte stable, `act_` + 26 caractères), `project`,
`work_item`, `proposed_by`, `connector`, `operation`, `target` (compte ou objet
visé), `args` (jsonb, forme canonique JCS), `class`
(`read|reversible|irreversible|costly`), `amount` et `currency` (facultatifs),
`state` (`proposed|approved|launched|confirmed|failed|unknown|cancelled`),
`replaces` (action `unknown` remplacée, voir R5 §5), horodatages.
Table `action_attempts` : une ligne par tentative (`attempt_no`, `receipt_id`,
`state`, `external_ref`, `error`, horodatages). Chaque transition écrit un
événement (journal) et une entrée de fil.

**Empreinte** d'une action, signée par l'approbation :
`SHA-256("ameesh-action/1\0" || JCS({action_id, project, connector, operation,
target, args, amount, currency, policy_version}))`.

## 7.2 Protocole (R5)

1. `proposed` → `approved` : seulement avec un reçu valide (C7) dont
   l'empreinte est celle de l'action, ou une approbation permanente bornée qui
   la couvre. Les classes `read` et `reversible` n'exigent pas de reçu sauf
   politique contraire.
2. `approved` → `launched` : **écrit et validé en base avant l'appel externe** ;
   dans la même transaction, le nonce du reçu est consommé (reçu propre à
   l'action) ou le montant est réservé sur le grant (approbation permanente,
   §8.3).
3. L'appel passe la **clé d'idempotence = `action_id`** au connecteur.
4. Résultat certain → `confirmed` | `failed`. Pas de résultat (coupure, délai)
   → `unknown`.
5. `unknown` : pas de nouvelle tentative automatique. Si le connecteur déclare
   `dedupe: guaranteed`, une nouvelle tentative de la même action (nouveau reçu,
   même `action_id`) est permise. Sinon `ameesh action reconcile` appelle
   `reconcile(action)` du connecteur ; introuvable → attente d'un humain (C10).
   Seule une décision humaine signée qui **assume le doublon** crée une nouvelle
   action (`replaces` = l'ancienne).

## 7.3 Connecteurs

```python
class Connector(Protocol):
    name: str
    dedupe: Literal["guaranteed", "none"]
    def classify(self, operation: str, args: dict) -> ActionClass: ...
    def execute(self, action: Action, idempotency_key: str) -> Outcome: ...
    def reconcile(self, action: Action) -> Outcome | None: ...   # None = introuvable
```

v1 fournit le connecteur **`shell-noop`** (tests et démonstration : écrit un
fichier, idempotent par clé) et **`git-merge`** (fusion d'une PR GitHub par
`gh`, réconciliation par l'état de la PR). La classe d'une opération est
déclarée dans le canon (`Connector` ou politique d'équipe) et prime sur
`classify`.

CLI : `ameesh action propose|show|list|execute|reconcile|cancel`,
`ameesh decisions [--for human:<id>]` (C10).

# 8. Reçus d'approbation (C7)

## 8.1 Format

```
request = { v: 1, approver: "human:<id>", action_id, digest: "sha256:<empreinte>",
            decision: "approve"|"deny", summary_digest, requested_by,
            nonce (128 bits, base64url), iat, exp }
receipt = { v: "ameesh-receipt/1", request, facade: "webauthn"|"ed25519"|"device-es256",
            credential_id, proof: { … selon la façade … } }
challenge = SHA-256("ameesh-approval/1\0" || JCS(request))
```

Façades vérifiées en v1 :

- **`webauthn`** : `proof = {authenticatorData, clientDataJSON, signature}` ;
  vérifier `type = "webauthn.get"`, `challenge` = challenge, origine dans la
  liste autorisée, `crossOrigin` faux, `rpIdHash`, drapeaux UP et UV, signature
  ES256 (ou EdDSA) avec la clé COSE enrôlée ; BE=0 exigé si la politique
  demande le niveau « élevé ».
- **`ed25519`** : la signature Ed25519 existante sur `challenge` (tests, outils,
  provenance d'agent — **jamais** une autorité humaine si la clé est sur un hôte
  d'agents : la politique la refuse pour `approve`).
- **`device-es256`** : signature ES256 brute sur `challenge` par une clé
  d'appareil (application Nexlink ou application native tierce, 0015).

## 8.2 Registre de confiance

Les authentificateurs d'un humain (clé publique, façade, AAGUID, niveau,
date d'enrôlement) sont **dans le canon** (`Member.authenticators`, ou fichiers
`Authenticator` liés) : un ajout ou un retrait est une PR revue. La base garde
une **copie de travail** synchronisée par `ameesh canon sync`, plus le registre
des nonces consommés (existant, `mesh_consumed_nonces`).

## 8.3 Approbations permanentes bornées (R8)

Un reçu dont `request` porte `standing = {connector, operations, class,
max_amount, currency, until}` au lieu d'un `action_id` crée un **grant**. Son
nonce est consommé **une fois, à l'enregistrement du grant**. Ensuite, chaque
tentative couverte ne consomme pas de nonce : elle **réserve atomiquement** son
montant sur le plafond cumulé (`cumul + montant <= max_amount`, `until` non
dépassé, grant non révoqué) et la réservation est liée à l'`action_id` de la
tentative. Une réservation n'est libérée que sur un échec **certain**
(`failed`), jamais sur `unknown`. Révocable par une entrée du canon ou
`ameesh standing revoke` (qui exige elle-même un reçu).

# 9. ameesh-approve (C8)

Service séparé (`ameesh-approve`, Python stdlib `http.server` + vérification de
C7 ; `py_webauthn` facultatif), lancé **sous un autre utilisateur Unix ou sur un
autre hôte** que les agents. En v1 :

- `POST /requests` (depuis ameesh, jeton de service) : enregistre une demande,
  **recalcule le résumé** depuis l'action (jamais depuis le texte de l'agent),
  publie dans le fil un lien à usage unique ;
- `GET /a/<jeton>` : page qui affiche action, résumé, montant, empreinte courte,
  et appelle `navigator.credentials.get` (UV requis, `allowCredentials` limités
  à l'approbateur) ; `POST /a/<jeton>` reçoit l'assertion, construit le reçu ;
- `GET /receipts/<id>` : ameesh récupère le reçu et le vérifie lui-même (C7) ;
- enrôlement : `GET /enroll/<jeton>` crée une passkey et produit **une
  proposition de PR** au canon (jamais un enrôlement direct).

HTTPS obligatoire hors `localhost` (RP ID = domaine du service). Les tests
utilisent un authentificateur logiciel ES256 (aucun téléphone requis).

Une instance par équipe (décision 0026, lot L27) : profil strict par défaut
(RP ID exactement égal à l'hôte `H` de l'unique origine `https://H`, zones et
hôtes réservés refusés), `GET /health` sans secret et `ameesh approve-check`
pour la concordance avec la politique des vérificateurs, TLS local facultatif
derrière une passerelle en passthrough, rôle Postgres en lecture seule
(`deploy/sql/role-approve.sql`). Exploitation :
[héberger ameesh-approve](../profils/heberger-ameesh-approve.md).

# 10. Stockage (C1)

Paquet `ameesh.storage` : une classe `Storage` par pilote, des **opérations
nommées** regroupées par domaine (`leases.claim/renew/release`,
`mailbox.send/unread/mark_delivered`, `authority.consume_nonce`,
`work.move`, `actions.transition`, `threads.index`…). Les modules métier
n'écrivent plus de SQL. Les migrations restent des fichiers SQL versionnés
propres au pilote. Le pilote Postgres actuel (psycopg | psql) est le seul en v1.

# 11. Sécurité en v1 — ce qui est garanti et ce qui ne l'est pas

| Garanti en v1 | Pas encore (v1.x) |
|---|---|
| un agent sans responsable ou mal placé ne tourne pas | isolation des agents entre eux (même utilisateur Unix) |
| aucune action irréversible sans reçu valide **quand elle passe par la porte d'ameesh** — protection contre l'erreur et l'injection de consignes, pas contre un agent malveillant qui partage l'utilisateur Unix d'ameesh (il peut lire les identifiants métier, modifier ameesh ou sa base) | porte et identifiants métier sous un utilisateur distinct des agents ; bac à sable réseau ; proxy MCP |
| reçus liés à l'empreinte, à usage unique, avec échéance | non-répudiation forte pour les passkeys synchronisées (niveau élevé = clé matérielle) |
| registre des authentificateurs modifiable seulement par PR revue | signatures de provenance des messages d'agents |
| fil lisible de tous les messages passés par ameesh | messages échangés hors ameesh |

# 12. Existant / banc / cible (R1–R18)

| R | Banc actuel | v1 | Plus tard |
|---|---|---|---|
| R1 | plusieurs exécuteurs, bail exclusif ; une machine testée | idem, hôtes déclarés au canon | autres machines (0011) |
| R2 | CLI Claude/Codex/dsh | idem | app-server, ACP |
| R3 | baux d'agents | + invariant de bail, attach | baux sur objets métier |
| R4 | Ed25519, clé sur le PC (insuffisant) | reçus WebAuthn, registre au canon | application native, clé matérielle |
| R5 | consigne durable, nonces | actions, identité stable, réconciliation | connecteurs métier |
| R6 | boîte Postgres, repli fichiers | + transport `file` | Slack, Matrix, Nexlink, e-mail |
| R7 | budget stocké | + mode d'identifiants, fournisseur | contrôle du budget, forfaits |
| R8 | rôles `owner`/`agent` | capacités du canon, `approve` interdit aux agents | — |
| R9 | un schéma | un projet par canon | multi-organisation |
| R10 | — | page d'approbation | application native |
| R11 | `work_items` dev | idem | pipelines au canon |
| R12 | boîte privée | fil lisible | fils dans les outils d'équipe |
| R13 | signatures de messages Ed25519 | reçus d'autorité | provenance des agents |
| R14 | — | responsable requis, éphémères | — |
| R15 | — | projet = canon | scission, dépendances |
| R16 | décisions relayées | décisions au canon (pratique) | décisions signées |
| R17 | — | `ameesh decisions` | file multi-projets dans les façades |
| R18 | — | politique d'hôte, placements | supervision des coûts et forfaits |

# 13. Plan de lots

**État au 2026-10-04 20:45** : **v1 terminée** — L1 (phases 1 et 2), L2, L3, L4, L5, L6, L7, L8, L9 et L9b fusionnés sur `main` (2ff67ff) après revue d'un autre éditeur ; essai de bout en bout vert sans contournement ; `scripts/test.sh` complet vert sur les deux pilotes (652 tests chacun). Hors v1, déjà fusionnés : L11, L12, L13.

Règles : une branche par lot (`lot/L<n>-<nom>`), gel → revue d'un autre
éditeur → fusion ; tests sur Postgres réel (`scripts/test.sh`) ; aucun nom de
client ; migrations réservées ci-dessous ; aucune dépense, aucun service public.

| Lot | Contenu | Dépend de | Migration | Relecture |
|---|---|---|---|---|
| L1 | interface de stockage (C1), sans changement de comportement | fin B5a | — | codex3 |
| L2 | canon : lecture, profil, validation, sync, responsables, éphémères (C2, C3) | — | 0008 (`responsible`, `team`, `credential_mode`, `ephemeral`, `canon_ref`) | codex |
| L3 | placement et politique d'hôte dans le prédicat de réclamation (C4), `ameesh placement check` | L2 | 0022 | codex |
| L4 | fil lisible, transport `file`, refus des corps illisibles (C5) | — | 0009 (index des fils) | codex |
| L5 | actions, porte, connecteurs `shell-noop` et `git-merge`, `ameesh decisions` (C6, C10) | L6 (vérif.) | 0010 (`actions`, `action_attempts`) | codex3 |
| L6 | reçus `ameesh-receipt/1`, façades webauthn/ed25519/device-es256, registre au canon, permanentes bornées (C7) | L2 | 0011 (`authenticators`, `standing_approvals`) | codex3 |
| L7 | service `ameesh-approve` (C8) | L6 | — | codex3 |
| L8 | événements, regroupement, `ameesh attach` (C9) | fin B5a | 0012 (`kind = event`) | codex3 |
| L9 | bascule réécrite, documentation v1 (C11) | tous | — | codex |
| L9b | finitions d'intégration trouvées par l'essai de bout en bout : canon sync → registre des authentificateurs (verrou, monotonie, branche de confiance), demande et reçu depuis ameesh, transitions d'actions dans le fil, projet du fil = équipe | L2–L7 | 0023, 0024 | codex2 + codex3 |
| L10 | politique de revue par classe de risque au canon ; métriques de délai par lot (R19) | L2 | 0013 | codex |
| L11 | interruption de tour, rotation de session avec résumé, sessions robustes au déplacement (R19) | L8 | — | codex3 |
| L12 | lecteurs de jauges de forfait, comptabilité par tour, `ameesh cost` (R20) | — | 0014 | codex |
| L13 | garde de budget avant chaque tour, modèle et effort par harnais, `ameesh set` (R20) | L8, L12 | — | codex3 |
| L14 | catalogue et découverte des modèles (API des fournisseurs, options des harnais, barèmes), événements de changement, `ameesh models list|discover|show` (R21) | L12 | 0015 | codex |
| L15 | évaluation sur tâches de référence (action coûteuse sous la porte), rapports au canon, **rejeu isolé de toute trace de la solution** (dépôt reconstruit sans historique, branches, remote, board ni autres worktrees — sinon la mesure ne vaut rien), `ameesh models evaluate|recommend` ; métrique de qualité globale après revue et recette, adoption seulement sur gain significatif ([0023](decisions/0023-critere-d-adoption-des-reglages.md)) (R21) | L5, L14 | 0016 | codex3 |
| L16 | descripteurs de harnais au format du **manifeste du registre ACP** (`agent.json` : id, version, capacités, authentification, distribution), étendu par un espace de clés `ameesh` (pilotage sans interface, reprise de session, flux, hooks, jauges et coût, permissions) ; adaptateur ACP générique, adaptateur piloté par descripteur ; fin de la liste fermée de harnais (R22) | L8 | — | codex3 |
| L17 | catalogue central des harnais, banc de conformité, évaluation harnais × modèle × effort, installation signée (R22) | L14, L15, L16 | 0017 | codex3 |
| L18 | entrée des signaux (table `signals`, webhooks authentifiés, e-mail, fil), empreintes et déduplication à deux niveaux, liens signal ↔ lot (R23 : A1, A3) | L4 | 0018 | codex3 |
| L19 | classifieur et politique d'auto-réparation au canon ; passage automatique ouvert → en cours pour les bugs admis (R23 : A2, A5) | L2, L18 | — | codex |
| L20 | exécution de réparation : consigne « reproduire d'abord », rapport, gardes sur le diff (substituts, portée, contrats, tests), porte de changement sur l'empreinte du diff (R23 : A6, A7) | L5, L8 | 0019 | codex3 |
| L21 | preuves de vérification et QA bornée (R23 : A8) | L20 | — | codex |
| L22 | demandes aux humains : destinataire par le canon, échéances par gravité, remplaçants, escalade sans décision implicite (R23 : A9) | L2, L5 | 0020 | codex3 |
| L23 | surveillance après livraison, réouverture sur récidive, corrélation avec les versions, notification du demandeur, auto-guérison des exécutions, balayage (R23 : A4, A10–A12) | L18, L21 | 0021 | codex |
| L24 | vue temps réel de l'avancement : `ameesh progress --json` (lots, agents, jalons, budget) alimenté par les événements, vue de référence lisible sur téléphone (maquette : page de suivi du chantier Nexlink, privée) ([0024](decisions/0024-vue-temps-reel.md)) | L5, L8, L12 | — | codex |
| L25 | profil d'hébergement « cluster » : image de l'exécuteur, manifestes Kubernetes d'exemple (un agent persistant par pod, canon en lecture seule), sonde `doctor --probe`, rôle Postgres de superviseur en lecture seule (contenus exclus par défaut) ([0011](decisions/0011-hebergement-configurable.md)) | L1, L2 | — | codex3 |
| L26 | parité d'exploitation v0 → v1 : rotation de session au changement de lot et alerte « session trop grosse » ([0025](decisions/0025-une-session-par-lot.md)) ; `ameesh list --json` enrichi (harnais, modèle, effort, tier, état, depuis quand, lot courant, non-lus) ; usage par tour ; historique des jauges de forfait ; solde et dépense réelle du fournisseur payé au token ; alertes en flux `--json` (tour long, repos avec courrier, exécuteur mort, session trop grosse) ; `ameesh restart <agent> --brief` (session neuve) ; tier Codex dans `ameesh set` ; `ameesh interrupt` (R19, R20) | L11, L13 | 0027 | codex3 |
| L27 | ameesh-approve multi-équipe ([0026](decisions/0026-hebergement-d-ameesh-approve-par-equipe.md), [contrat](ameesh-approve-hebergement-equipes.md)) : `approve_url` distincte de `public_url`, profil strict RP ID = hôte et origine unique, refus d'une zone ou d'un parent réservé, concordance service ↔ vérificateurs, droits DB propres à approve, bascule à hôte constant | L7, L9b | — | codex3 |
| L29 | plan de travail : fiches `WorkPackage` au canon avec parent (jalon → epic → lot), responsable et périmètre ; `work_items` reliés à leur fiche et à leur parent ; fermeture automatique sur fusion de la PR ; projection GitHub (une issue par lot, sous-issues de l'epic, labels d'état), GitHub restant une vue et ses modifications des propositions ([0005](decisions/0005-canon-okf.md)) ; epics dans `ameesh progress` | L2, L10, L24 | 0026 | codex3 |
| L30 | comptes multiples par fournisseur ([0027](decisions/0027-bascule-automatique-entre-comptes.md)) : liste ordonnée de comptes par hôte (profils d'identifiants : dossier de configuration du harnais ou clé d'API), jauges par compte, bascule automatique avant chaque tour au lieu de la pause, retour au primaire après remise à zéro, continuité de session quand le harnais le permet, journal des bascules | L12, L13, L26 | 0028 | codex3 |
| L31 | ressources des hôtes et répartition ([0028](decisions/0028-ressources-des-hotes-et-repartition.md)) : relevés mémoire/swap/CPU/disque par exécuteur et `ameesh hosts`, seuils dans la politique d'hôte, contre-pression avant chaque tour, ressources orphelines (processus et conteneurs rattachés aux tours), admissions (persona → hôtes ou étiquettes d'hôtes admis, sans `cwd`, [0029](decisions/0029-persona-et-session.md)) avec lecture transitoire des anciennes fiches `Placement`, placement des sessions et déplacement entre deux tours | L3, L26, L30 | 0029 | codex3 |

Ordre : L2, L4 et L6 en parallèle dès maintenant (fichiers nouveaux) ; L1 et L8
après la fusion de B5a (ils touchent `runner.py` et `registry.py`) ; puis L3,
L5, L7 ; L9 en dernier. Définition de « v1 terminée » : tous les lots fusionnés
avec un OK de relecture, `scripts/test.sh` vert sur les deux pilotes, et un
**essai de bout en bout sur le banc** : canon d'exemple → `canon check` →
exécuteur qui réclame deux agents factices → message agent↔agent visible dans le
fil → action `git-merge` simulée bloquée sans reçu, puis exécutée avec un reçu
WebAuthn logiciel → issue inconnue → réconciliation.
