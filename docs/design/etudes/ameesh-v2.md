---
type: Study
title: "Étude — ameesh v2 : des personas sur plusieurs appareils et serveurs, avec rôles, identité et autorisations"
description: "Plan de la v2 : le travail des agents ne dépend plus du matériel des humains (serveurs, placement selon la capacité, déplacement d'une persona avec sa mémoire) ; plusieurs appareils coordonnés via Nexlink ; rôles adressables avec titulaires et suppléants ; identité humaine OIDC et autorisations par relations ; isolation ; mémoire par persona avec droits. Options à trancher et ordre des lots."
status: draft
tags: [v2, serveurs, multi-hote, nexlink, persona, role, identite, autorisation, isolation, memoire]
generated: { by: "mesh-design/claude-opus-5-5", at: "2026-10-09T05:45:00+02:00" }
stale_after: 2027-01-09
sources:
  - { resource: "../decisions/0003-ameesh-et-nexlink.md", title: "Décision 0003 (ameesh et Nexlink)" }
  - { resource: "../decisions/0012-autorite-par-ameesh-approve.md", title: "Décision 0012 (autorité)" }
  - { resource: "../decisions/0015-interfaces-des-facades.md", title: "Décision 0015 (interfaces des façades)" }
  - { resource: "../decisions/0017-approbation-via-page-nexlink.md", title: "Décision 0017 (approbation via Nexlink)" }
  - { resource: "../decisions/0028-ressources-des-hotes-et-repartition.md", title: "Décision 0028 (ressources des hôtes)" }
  - { resource: "../decisions/0029-persona-et-session.md", title: "Décision 0029 (persona et session)" }
  - { resource: "../decisions/0032-persona-roles-et-sessions.md", title: "Décision 0032 (persona, rôles et sessions)" }
  - { resource: "persona-et-harnais.md", title: "Étude persona et harnais" }
---

# Objet

La v1 tourne sur **un seul appareil**, le poste de son propriétaire. Elle
comprend une base Postgres locale, un exécuteur par agent, les deux canons en
clones git et les notifications sur le bureau. Le 2026-10-09, ce poste avait
17 personas admises pour 16 places, avec du swap sous pression : le matériel
d'un humain limite le travail.

La v2 doit :

1. **faire travailler les personas sur des serveurs**, pour que le travail ne
   dépende plus des appareils des humains ;
2. **coordonner plusieurs appareils**, postes, téléphones et serveurs, via
   Nexlink ([0003](../decisions/0003-ameesh-et-nexlink.md)) ;
3. appliquer **persona, rôle et session**
   ([0032](../decisions/0032-persona-roles-et-sessions.md)) ;
4. apporter ce qu'exige le passage à plusieurs machines et à plusieurs
   humains : **identité**, **autorisations**, **isolation** et **mémoire avec
   droits**.

**Hors du périmètre v2** :
- la création de personas par clonage ou par assemblage (0032 §3) ;
- la spécialisation d'ouvriers par apprentissage (v3) ;
- la conception de l'interface Nexlink elle-même, qui relève de Nexlink
  (0015).

# Ce que la v1 fournit déjà

| Acquis v1 | Utilité en v2 |
|---|---|
| **Baux à epoch** : un seul exécuteur mène un agent, la reprise est monotone | l'exclusivité entre plusieurs hôtes est déjà correcte, avec une base partagée |
| **Fiches `Host`**, admissions, politique d'hôte, relevés de pression (L31, 0028) | le placement selon la capacité part de là |
| **Plusieurs canons** (0031) | plusieurs équipes ou organisations sur un même mesh |
| **Profil cluster** : un agent par pod, base extérieure, canon en lecture seule, sans port entrant | un modèle d'hébergement serveur, à étendre au-delà d'un seul agent |
| **ameesh-approve** et les reçus signés (0012, 0017) | l'autorité humaine à distance, sur téléphone |
| **Alertes poussées** (L38 : bureau, ntfy, Slack) | prévenir des humains qui ne sont pas devant la machine |
| **Interface de transport des fils** | le connecteur Nexlink et les autres |

Ce qui manque :
- la séparation entre persona et session ;
- la mémoire hors du transcript ;
- un rôle Postgres par agent ;
- l'identité humaine vérifiée ;
- l'isolation entre agents sur un même hôte ;
- le déplacement d'une persona d'un hôte à l'autre.

# Volet A — plusieurs hôtes, dont des serveurs

**A1. Base partagée.** Une base Postgres joignable par tous les hôtes : service
géré ou VM, en TLS. Elle a un rôle par hôte, puis par agent : un agent ne doit
plus pouvoir lire ou modifier l'état d'un autre (c'est une limite connue du
profil cluster). Il faut aussi des sauvegardes hors de la base, avec une
restauration testée. La base locale d'un poste reste possible pour un mesh
personnel.

**A2. Exécuteurs sur les serveurs.** Deux profils :
- **VM et systemd** : la v1 telle quelle, un service par agent. C'est simple, et
  on isole par utilisateur Unix (volet E).
- **Cluster** : un pod par persona active. L'isolation est plus forte, mais
  l'exploitation est plus lourde.

Les deux profils partagent la même base et les mêmes baux.

**A3. Placement selon la capacité.** Le canon déclare des **règles**, comme le
prévoit 0029 : hôtes ou étiquettes admis, contraintes de la persona, visibilité
du dépôt de la persona. Il ne déclare pas de position. ameesh choisit l'hôte
au moment d'ouvrir une session, selon la pression relevée. Quand un poste est
saturé, la session suivante part sur un serveur.

**A4. Déplacer une persona avec ce qu'elle sait.** C'est le point dur. Les
sessions des harnais sont des fichiers locaux : transcripts Claude, fils
Codex, sessions DeepSeek. Trois options :

| Option | Principe | Limites |
|---|---|---|
| **A4a — mémoire de persona** (0029, P1/P4) | la persona emporte sa mémoire, un dépôt git ; une nouvelle session sur le nouvel hôte la charge | le contexte fin de la conversation en cours est perdu ; on ne déplace qu'entre deux lots |
| **A4b — copie de session** | le transcript est copié sur le nouvel hôte avant la reprise | propre à chaque harnais ; chemins et comptes à réécrire ; fragile |
| **A4c — stockage commun** | les sessions vivent sur un volume partagé | dépend de l'infrastructure ; verrous ; ne convient pas aux postes |

**Recommandation** : A4a comme règle, conformément à 0032 §5. A4b seulement
pour finir un lot en cours, et pour les harnais qui le permettent.

**A7. Sauvegarde des sessions** (0032 §2). Les sessions font partie de la
persona : chaque exécuteur copie les sessions qu'il mène hors de l'appareil,
à chaque fin de tour. Les cibles sont un stockage d'objets du mesh, sur un
serveur ou chez l'hébergeur des sauvegardes. Entre appareils, la copie passe
par Nexlink (volet B). La copie est chiffrée, car une session contient tout ce
que l'agent a lu. Elle sert aussi à A4b : reprendre une session sur un autre
hôte, c'est restaurer sa dernière copie.

**A5. Comptes des modèles.** Un abonnement (Claude, Codex) utilisé sur un
serveur, sans humain devant, doit être **vérifié avec les conditions de chaque
fournisseur**. Hypothèse de travail :
- les personas des serveurs utilisent des **clés API** payées au jeton, sous les
  plafonds d'ameesh (0019) ;
- les abonnements restent sur les appareils des humains ;
- la politique d'hôte (`credential_modes`) le garantit.

**A6. Dépôts de travail.** Chaque hôte a ses clones et ses worktrees, avec des
clés de déploiement limitées aux dépôts de la persona (0029, visibilité).

# Volet B — plusieurs appareils via Nexlink

La décision 0003 répartit déjà les rôles :
- **Nexlink** : organisations, membres, comptes d'agents, conversations,
  notifications, signatures sur l'appareil, audit ;
- **ameesh** : exécuteurs, sessions, tours, baux, budgets, lots.

**B1. Une seule vérité pour les baux.** Deux architectures :
- **B1a — base ameesh centrale, Nexlink pour les humains et le transport.**
  Chaque appareil qui exécute des personas a son exécuteur, branché sur la
  base. Nexlink affiche, converse, notifie et recueille les signatures.
- **B1b — des ameesh locaux synchronisés par Nexlink.** Plus résistant hors
  ligne, mais il faut quand même une autorité unique pour les baux.

**Recommandation** : B1a. Un appareil hors ligne peut mener ses propres
personas locales, non partagées.

**B2. Parler à une persona depuis n'importe quel appareil.** Une conversation
Nexlink avec une persona ou un **rôle** remplace `ameesh attach` dans un
terminal. ameesh fournit pour cela une interface de conversation : un message
humain devient un tour ; la réponse revient dans le fil ; une prise de main
interactive passe par le bail `attach`. 0015 impose que cette interface soit
publique, pour qu'une organisation puisse construire la sienne sans Nexlink.

**B3. Approbations et notifications.** ameesh-approve est relayé par une page
Nexlink (0017) : la passkey de l'humain sur son téléphone. Les alertes passent
par un canal Nexlink, à côté du bureau, de ntfy et de Slack.

**B4. Les appareils comme hôtes.** Un poste peut rester un hôte, pour ses
abonnements et pour les personas privées d'un humain (0029, visibilité). Un
téléphone n'est qu'un client.

# Volet C — les rôles

0032 pose le principe : une fonction dans un périmètre, tenue par une persona
ou par un humain. Il reste à concevoir :

- **C1. Déclaration au canon.** Une fiche `Role` (nom, périmètre, description,
  droits attachés). La fiche persona porte les rôles qu'elle tient : titulaire
  ou suppléant, avec des dates.
- **C2. Adressage.** `ameesh mail send role:<rôle>@<périmètre>` et un lot
  affecté à un rôle se résolvent au moment de l'envoi : le titulaire disponible,
  sinon le suppléant. La résolution est inscrite dans le fil, pour savoir qui a
  reçu quoi.
- **C3. Absences.** Le titulaire, humain ou persona arrêtée, est
  **indisponible** sur une période. La délégation à échéance (L40), qui ne
  concerne aujourd'hui que les agents, s'étend aux humains. Les **supérieurs**
  du responsable (0032 §2) forment la chaîne d'escalade quand ni le titulaire
  ni les suppléants ne répondent.
- **C4. Vocabulaire.** Un petit socle commun (orchestrateur, relecteur, auteur,
  approbateur) que chaque canon complète par ses rôles propres, par exemple
  un vérificateur de migrations.
- **C5. Observation.** `ameesh list --role`, et l'orchestrateur d'un périmètre
  qui se retrouve par son rôle.

# Volet D — identité et autorisations

- **D1. Identité humaine.** ameesh devient client **OIDC**, avec un
  fournisseur générique : Keycloak en référence, Nexlink s'il devient
  fournisseur. Le fournisseur dit *qui* est l'humain ; le canon dit *ce qu'il
  est* dans l'organisation (membre, rôles, responsabilités). La passkey
  d'ameesh-approve reste l'autorité (0012).
- **D2. Autorisations par relations.** « Le titulaire du rôle approbateur-prod
  du projet X peut approuver un déploiement de X. » Le **canon reste la
  source**, modifiée par PR relue. ameesh évalue les règles lui-même, ou
  alimente un moteur **OpenFGA**, qui reste optionnel, à la synchronisation du
  canon, comme pour les authentificateurs.
- **D3. Agir pour le compte de quelqu'un.** Quand une persona agit pour un
  humain, l'outil appelé voit les deux identités, par un échange de jetons
  (RFC 8693). Ses propres droits s'appliquent.
- **D4. Base de données.** Un rôle Postgres par agent, avec des droits limités
  à ses lignes, ses messages et ses lots. Les migrations sont faites par un
  rôle propriétaire distinct, jamais par un exécuteur.

# Volet E — isolation

- **E1.** Un **utilisateur Unix par persona** sur une VM, ou un **pod** en
  cluster. C'est le plus gros risque réel aujourd'hui, puisque tout tourne
  sous l'utilisateur du propriétaire.
- **E2.** Un **proxy MCP** par lequel passent les outils d'une session. Il est
  le point d'application : il injecte l'identité (D3), demande au moteur de
  règles (D2) si l'appel est permis, et le journalise.
- **E3.** Le réseau sortant limité à ce dont la persona a besoin : fournisseur
  de modèle, forge, base.

# Volet F — mémoire par persona, avec droits

- **F1.** Le dépôt de mémoire de chaque persona (0029, P1/P4) : fichiers
  Markdown, consolidés aux points fixes (fin de lot, relais), chargés par tout
  harnais. **Priorité de la v2** (0032 §5).
- **F2.** Les niveaux : organisation (le canon, relu), projet (les fils),
  persona (son dépôt), personne (le dépôt privé de son double numérique).
- **F3.** Les droits de lecture passent par les mêmes relations que D2 : qui
  peut lire quelle mémoire. La règle de visibilité de 0029 en est le premier
  cas.

# Volet G — visibilité du responsable (0033 §9)

- **G1. Journal d'activité par persona.** Une vue chronologique : sessions
  ouvertes et closes, tours (déclencheur, durée, issue), messages reçus et
  envoyés, lots (états, verdicts), actions et reçus, dépenses. Le registre, les
  fils, les coûts par tour et les actions en ont déjà la matière. Il manque la
  vue par persona, et la conservation au-delà de la vie d'une session.
- **G2. Contenu des sessions.** Le responsable peut ouvrir le transcript
  d'une session, présente ou passée, depuis la sauvegarde (A7). C'est la
  seule façon de savoir *pourquoi* un agent a fait quelque chose.
- **G3. Accès.** Le responsable de la persona, ses suppléants et ses
  supérieurs (0033 §9). La règle
  passe par les relations du volet D (« responsable de » → « peut lire
  l'activité de »). Chaque consultation d'un transcript est tracée.
- **G4. Où.** En ligne de commande (`ameesh activity <persona>`), dans la vue
  temps réel de 0024 et dans Nexlink, avec la même interface publique (0015).
- **G5. Conservation.** Une durée par organisation, fixée dans son canon ; à
  défaut, 90 jours pour les transcripts et un an pour le journal (0033 §9).

# Ordre proposé

| Phase | Contenu | Dépend de |
|---|---|---|
| **1** | Persona et session séparées (fiche `Persona`, registre, plusieurs sessions par persona) ; mémoire de persona (F1) | 0029, 0032 |
| **2** | Rôles (C1–C5) ; journal d'activité par persona (G1, G3, G4) | 1 |
| **3** | Base partagée et rôles Postgres par agent (A1, D4) ; premier serveur en VM et systemd (A2) | 1 |
| **4** | Placement selon la capacité et déplacement entre deux lots (A3, A4a) ; comptes API sur serveur (A5) | 1, 3 |
| **5** | Identité OIDC et ameesh-approve déployé (D1, B3) | 3 |
| **6** | Interface de conversation et connecteur Nexlink (B2) | 2, 5 |
| **7** | Autorisations par relations, OpenFGA optionnel (D2, D3, F3) | 2, 5 |
| **8** | Isolation : utilisateur Unix ou pod, proxy MCP (E1–E3) | 3, 7 |

# Lots (découpage du 2026-10-09)

Le lot serveur passe en premier : dès qu'il est livré, des personas y
travaillent pour accélérer les suivants (consigne du propriétaire).

| Lot | Phase | Contenu |
|---|---|---|
| **L55** | 3 | **Profil serveur VM** : création de la VM par Terraform chez l'hébergeur de l'organisation ; installation (cloud-init) de Postgres en TLS, d'ameesh, des services systemd, des sauvegardes ; accès réseau privé des appareils (WireGuard) ; procédure et retour arrière |
| **L56** | 3 | **Rôles Postgres** par hôte et par agent (D4) ; migrations par un rôle propriétaire distinct |
| **L57** | 3 | **Scission des meshes** : exporter les personas et l'état d'un canon vers la base de son organisation (0033 §1) |
| **L51** | 1 | **Fiche `Persona`** (double lecture de `Agent`) : `harnesses` ordonnés, `roles`, `memory.repository` ; fiche `Member` : `deputies`, `superiors`, rôles |
| **L52** | 1 | **Personas et sessions au registre** : plusieurs sessions par persona, un bail par session |
| **L53** | 1 | **Sauvegarde des sessions** (A7) : copie chiffrée en fin de tour vers le stockage d'objets du mesh ; restauration sur un autre hôte |
| **L54** | 1 | **Mémoire de persona** (F1, P3/P4) : dépôt git, chargement au démarrage, consolidation en fin de lot |
| **L58** | 2 | **Rôles** (C1–C5) : fiche `Role`, adressage `role:`, suppléance, absences, escalade |
| **L59** | 2 | **Journal d'activité** (G1–G5) : `ameesh activity`, accès par responsabilité, conservation |

## État au 2026-10-09 (fin de matinée)

| Lot | PR | État | Dépend de |
|---|---|---|---|
| L55 profil serveur VM | #16 | VM du mesh Manaty créée et installée (Postgres 17, WireGuard, poste relié) | — |
| L57 scission d'un mesh | #22 | script et procédure ; répétition sur la VM réussie ; exécution réelle à faire avec le propriétaire | L55 |
| L51 fiche Persona, suppléants et supérieurs | #17 | prêt | — |
| L58 rôles adressables | #20 | prêt | L51 |
| L53 sauvegarde des sessions | #18 | prêt | — |
| L54 mémoire de persona | #21 | prêt | — |
| L52 historique des sessions (1re partie) | #24 | prêt ; sessions parallèles à faire | — |
| L59 journal d'activité | #19 | prêt ; filtrage par responsabilité à faire | — |
| L56 rôle Postgres des exécuteurs | #23 | prêt | — |
| D1 identité OIDC | #25 | prêt | — |
| D2 accès à l'activité par relation | #27 | prêt | L59, L51, D1 |
| E1 un utilisateur Unix par persona | #26 | éprouvé en conteneur | L57 |
| E2 proxy MCP | #28 | prêt | — |

Ordre de fusion conseillé : #14 (1.4.3), #15 (cette conception), #16 puis #22,
#17 puis #20, puis les autres dans n'importe quel ordre. Chaque PR a passé la
suite complète sur les deux pilotes.

Restent, à trancher ou à faire avec le propriétaire :
- **exécuter L57** : déplacer le mesh Manaty sur la VM ;
- **sessions parallèles** (fin de L52) : voir l'étude
  [sessions parallèles](sessions-paralleles.md), à trancher avant de coder ;
- **D2**, autorisations par relations : elles s'appuient sur L51, L59 et D1 ;
- **E**, isolation : un utilisateur Unix par persona sur la VM, puis le proxy
  MCP ;
- **B2**, interface de conversation : avec Nexlink (manaty/nexlink#47).

# Questions au propriétaire

1. ~~Un mesh commun ou un mesh par organisation~~ : **un mesh par
   organisation**, étanche : aucune persona ne travaille pour une autre
   organisation (0032 §7, 0033 §1).
2. ~~Premier serveur~~ : **l'organisation choisit** (son infrastructure ou
   une VM) ; profil VM et systemd d'abord (0033 §2).
3. ~~Personas sur les serveurs~~ : les personas payées au jeton, sur les clés
   API de l'organisation ; les abonnements restent sur les postes (0033 §3).
4. ~~Fournisseur d'identité~~ : **OIDC générique** choisi par l'organisation ;
   essais avec Keycloak et Supabase Auth ; Nexlink compatible (0033 §4).
5. ~~Les « agents » d'une persona~~ : **ses sessions**, sauvegardées hors de
   l'appareil (0032 §2, A7).
6. ~~Ordre~~ : phase 3 et phase 1 en parallèle, puis les rôles (0033 §5).

# Risques

- **Conditions des fournisseurs** pour les abonnements sur un serveur (A5) :
  à vérifier avant tout déploiement.
- **Perte de contexte** au déplacement d'une persona (A4) : acceptable entre
  deux lots, pas en plein lot.
- **Exposition de la base** (A1) : TLS, rôles par agent, réseau restreint.
  Sans D4, un agent compromis lit tout.
- **Dépendance à Nexlink** : elle est interdite par 0015. Toute fonction
  passant par Nexlink garde son interface publique.
- **Charge de la migration v1 → v2** : les agents v1 vivent jusqu'à la fin de
  leur lot (0032 §6). La double lecture `Agent`/`Persona` dure le temps de la
  transition.
