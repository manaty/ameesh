---
type: Design
title: "Avatars, charte et porte d'actions ; répartition IMA / ameesh (v1)"
description: "Spec validée par l'owner le 17/11/2026 : avatar commun à TPK et Manaty, répartition IMA/ameesh, charte traduite dans la porte d'actions, mode de la porte au canon (journal ou enforce), journal des quatre gestes Manaty, et lots de la série #7627-#7638 avec leur état."
status: draft
tags: [avatar, charte, porte-actions, action-gate, ima, repartition, gouvernance, autorite]
generated: { by: "ameesh-dev3/deepseek", at: "2026-11-17T02:45:00Z" }
sources:
  - { resource: "manaty/ameesh#5815", title: "Avatars, charte et porte d'actions : note de conception de la spec owner du 17/11" }
  - { resource: "5322-avatar-humain.md", title: "Avatar d'humain : conversation continue, placement à préférence, enfants restreints et attach distant" }
  - { resource: "specification.md", title: "ameesh v1 specification, §7 à §9 (actions, porte, approbations)" }
  - { resource: "generic-core-boundary.md", title: "Frontière du cœur générique : les modules n'y déclarent pas de connecteur" }
  - { resource: "thepackengers/home:architecture/aix-avatars.md", title: "Matière de la spec : charte et avatars TPK" }
---

**Lot #7627**, note de conception de référence de la série #7627-#7638.
Spec validée avec l'owner le 17/11/2026 (v1) : les décisions du §1 sont
opposables ; cette note ne contient aucun code. Le contrat de canon qui fixe
le mode de la porte pour tous les lots est au §3.7 ; la liste des lots de la
série et leur état au §8. La question Q4 de `5322-avatar-humain.md` renvoie
ici.

# Avatars, charte et porte d'actions ; répartition IMA / ameesh (v1)

> **Validée avec l'owner le 17/11/2026** (décisions prises en séance, §1). Remplace le premier jet `avatars-repartition-ima-ameesh-v0.md`.
> Matière : décisions owner des 12/11 et 17/11 ; `thepackengers/home`, `architecture/aix-avatars.md` ; ameesh 1.57 (`docs/design/5322-avatar-humain.md`, `docs/design/specification.md` §7 à §9) ; mesures du 17/11 dans les deux bases, sur le poste et sur la VM `bureau-goal`.
> Le plan d'implémentation est dans `avatars-plan-implementation.md`.

## 1. Décisions

| # | Décision | Date |
|---|---|---|
| 1 | Chaque collaborateur a un avatar qui le représente et qui se clone pour les tâches. | 12/11 |
| 2 | La répartition du code entre IMA et ameesh (§5). | 17/11 |
| 3 | Pas d'alerte « avatar silencieux » pour l'instant. | 17/11 |
| 4 | Le bureau devient l'avatar de Sébastien côté Manaty. Un avatar par organisation. | 17/11 |
| 5 | Pas de Memoria sur le mesh Manaty : Sébastien y est le seul humain. | 17/11 |
| 6 | La charte de chaque avatar est contrôlée par la porte d'actions d'ameesh. | 17/11 |
| 7 | À Manaty, quatre gestes passent par la porte : production, dépense, publication store ou publique, signature de décision. Le reste reste libre. | 17/11 |
| 8 | À Manaty, l'owner garde sa clé fichier. Son accord est son « go » dans la session ; le bureau signe avec la clé. La porte y journalise le geste, elle ne le bloque pas. | 17/11 |
| 9 | Réveil de l'avatar Manaty : les messages de l'owner, les urgences et les demandes tout de suite ; le reste groupé toutes les 30 minutes. | 17/11 |
| 10 | La mémoire de l'avatar Manaty va dans un dépôt GitHub privé au nom de l'owner. | 17/11 |
| 11 | Avatars TPK dans le cloud : un pod partagé, comme décidé le 12/11. Il attend un lot d'infrastructure ameesh. | 17/11 |
| 12 | Les agents personnels TPK (`tpk-dev-<login>`, `tpk-docs-<login>`) deviennent des clones de l'avatar de la personne. | 17/11 |
| 13 | Démarrage : tout en parallèle. La bascule du bureau attend le go de l'owner, après un essai avec un avatar de test. | 17/11 |

## 2. Le même modèle pour les deux meshs

Un avatar a six fonctions. Entre TPK et Manaty, la réception, la mémoire, la voix et le mode de contrôle diffèrent.

| Fonction | TPK (plusieurs personnes) | Manaty (Sébastien seul) |
|---|---|---|
| Corps : session permanente, poste ou cloud, attach, clones | ameesh | ameesh |
| Réception : ce qui est adressé à la personne | Memoria collecte Slack, GitHub et mail par personne, puis écrit dans la boîte ameesh de l'avatar | aucune collecte : courrier ameesh, et lecture directe avec les comptes de Sébastien |
| Mémoire | Memoria ; IMA en produit une copie Markdown dans le dépôt persona | le dépôt git de l'avatar, écrit par l'avatar (composant mémoire d'ameesh) |
| Voix : publier | apps TPK (issue-agent, issue-manager), sous le nom « Prénom-AI » | les comptes de Sébastien |
| Autorité | plafond d'organisation (canon) et charte de la personne (dépôt persona) | charte seule |
| Contrôle de l'autorité | porte d'actions : le code refuse ce que la charte ne couvre pas | porte d'actions : le code journalise les quatre gestes réservés |

Deux conséquences :
- **ameesh ne connaît pas Memoria.** Il fournit la boîte de l'avatar ; ce qui la remplit est extérieur. Memoria reste un composant de TPK.
- **Dans les deux cas, l'avatar lit sa mémoire dans son dépôt persona.** Seul l'auteur change : l'avatar lui-même à Manaty, Memoria à TPK.

## 3. La porte d'actions

### 3.1 Ce que c'est

C'est un guichet dans ameesh pour tout geste qui a un effet hors du mesh : fusionner une PR, publier un message, déployer, dépenser.

L'agent ne fait pas le geste lui-même. Il le **propose**, et ameesh l'exécute par un **connecteur** après avoir vérifié l'autorisation :
- un geste de lecture ou réversible passe tout de suite ;
- un geste irréversible ou coûteux attend **l'approbation de l'humain pour ce geste précis** ;
- un geste couvert par une **approbation permanente bornée**, signée une fois par l'humain (connecteur, opérations, plafond, échéance), passe tout de suite et est décompté du plafond.

Chaque geste est journalisé avec l'approbation qui l'a autorisé. Un geste dont l'issue est inconnue n'est jamais rejoué sans décision.

### 3.2 Son état (mesuré le 17/11)

- Elle est dans ameesh depuis la v1 : tables `actions` et `standing_approvals`, commandes `ameesh action …`, service `ameesh-approve`.
- Elle n'a jamais servi : 0 action et 0 approbation permanente dans les deux bases.
- Aucune passkey n'est enrôlée, pour personne : 0 ligne dans `authenticators` des deux bases.
- Deux connecteurs seulement : la fusion de PR (`git-merge`) et un connecteur de test (`shell-noop`).
- Un module extérieur ne peut pas déclarer de connecteur (`docs/design/generic-core-boundary.md` : « Installed entry points cannot declare connectors »).
- Une approbation permanente ne nomme ni son bénéficiaire ni sa cible. Colonnes de `standing_approvals` : `approver`, `connector`, `operations`, `action_class`, `max_amount`, `currency`, `valid_until`. La recherche d'une couverture (`receipts.py`, `standing_cover`) ne passe ni l'agent ni la cible.
- À Manaty, l'autorité de l'owner passe par une clé fichier : 319 décisions signées avec elle depuis le 12/10, dont 71 sur les sept derniers jours. La clé est sur le poste et sur la VM `bureau-goal`. La base ne distingue pas les signatures de l'owner de celles du bureau.

### 3.3 Comment la charte s'y traduit (TPK)

| Issue de la charte | Dans la porte |
|---|---|
| Fait seul | L'opération est couverte par une approbation permanente signée par la personne. Elle passe sans attendre. |
| Prépare et soumet | L'action reste « proposée » jusqu'à l'approbation de la personne. L'approbation porte l'empreinte de l'action : si l'action change, elle ne vaut plus. |
| Informe seulement | L'action est exécutée ; le récap la liste à partir du journal des actions. |
| Jamais | Aucun connecteur n'offre l'opération, ou le canon la refuse. |
| Non couvert par la charte | Aucune approbation permanente ne la couvre : elle attend l'approbation. C'est le comportement par défaut de la porte. |

Le plafond d'organisation devient la politique du canon : elle dit quelles opérations peuvent recevoir une approbation permanente. La charte de la personne devient ses approbations permanentes, qu'elle ne peut signer que sous ce plafond. À TPK l'approbation se donne par passkey.

### 3.4 À Manaty : un journal

- Les quatre gestes réservés sont inscrits à la porte : production, dépense, publication store ou publique, signature de décision.
- L'accord est le « go » de l'owner dans la session de l'avatar. Le bureau signe le reçu avec la clé fichier de l'owner et y cite l'entrée du journal de conversation qui porte ce go.
- Un « go » permanent (mandat de nuit, go de production daté) devient une approbation permanente bornée, signée de la même façon, avec son échéance.
- Le bureau garde ses outils : la porte n'empêche rien par le code. Elle donne la liste exacte des gestes réservés, avec le go qui les a autorisés, et distingue enfin ce que l'owner a signé lui-même de ce que son avatar a signé pour lui.

### 3.5 Ce qui manque

1. **Des connecteurs fournis par un module.** IMA apporte ceux de TPK : publier sur Slack, commenter et fusionner sur GitHub, envoyer un mail, répondre à une demande de gouvernance, lancer un run. Le module développement ou le module Nexlink apporte ceux de Manaty.
2. **Une approbation permanente liée à son bénéficiaire, à une cible et au plafond.** Sans cela, l'approbation d'une personne couvrirait l'avatar d'une autre.
3. **Le lien avec la charte.** Chaque approbation permanente cite la ligne et la version de la charte ; l'action enregistre cette version (champ `policy_version`, déjà dans l'empreinte).
4. **Aucun autre chemin (TPK).** L'avatar ne doit plus avoir d'outil qui fait ces gestes directement. La liste blanche d'outils existe, mais elle n'a été vue appliquée par le runner qu'aux clones (`runner.py`, profil de spawn). Pour l'avatar du pilote TPK, l'interdiction d'agir tient à 103 règles locales sur le PC.
5. **L'enrôlement des passkeys (TPK)**, une fois par personne. Il sert aussi à l'attach distant.
6. **Le récap des actions**, rangé par issue de la charte.
7. **Le mode journal (Manaty)** : reçu signé par la clé fichier accepté pour les quatre gestes, référence au go de la session, distinction entre la signature de l'owner et celle de son avatar.

### 3.6 Limite

La porte rend mécaniques « jamais », « prépare et soumet » et les bornes : quoi, où, combien, jusqu'à quand. Une condition de sens reste jugée par le modèle, par exemple « un diff qui reste dans les blocs pré-approuvés ». Un connecteur peut en tester une partie : CI verte, étiquette sécurité, branche cible. Le reste se contrôle après coup, par le journal et le récap.

### 3.7 Le mode de la porte, contrat de canon de la série

Le mode est **une seule clé de canon**, la même pour tous les lots de la
série (#7627-#7638). Elle se lit dans `federation.yaml` :

```yaml
extensions:
  ameesh:
    action_gate:
      mode: journal   # journal (défaut) | enforce
```

Chemin complet : `extensions.ameesh.action_gate.mode`. Le bloc est au
premier niveau du manifeste racine, à côté de
`extensions.ameesh.forge_checks` et `extensions.ameesh.naming`. `extensions`
est la seule partie libre du schéma OKF Federation : un `action_gate` à la
racine de `federation.yaml` ne serait pas conforme au schéma. Le nom court
`action_gate.mode` reste celui du contrat ; les lots citent le chemin
complet. Le lot qui lit la clé (#7630) l'ajoute aux clés connues de
`extensions.ameesh` : `canon check` ne la signale pas comme inconnue.

| Valeur | Sens | Mesh |
|---|---|---|
| `journal` (défaut) | La porte **inscrit** les gestes réservés, leur autorisation et leur issue, mais ne refuse pas un geste hors charte. `canon check` n'exige pas de liste d'outils pour un avatar. | Manaty (décisions 7 et 8). |
| `enforce` | La porte **refuse** tout geste qu'aucune approbation ne couvre (approbation du geste ou approbation permanente bornée). L'avatar n'a aucun outil qui fait directement un geste sous charte ; `canon check` refuse un avatar sans liste d'outils ; un harnais qui ne peut pas prouver l'application de la liste refuse de démarrer. | TPK (décisions 6 et 7). |

Règles du contrat :

1. **Défaut `journal`** : un canon qui ne déclare rien garde le comportement
   d'aujourd'hui — la porte journalise, elle ne bloque pas. `enforce` est un
   opt-in explicite du mesh qui exige le contrôle par le code. Une clé
   absente vaut `journal` ; une valeur inconnue est une erreur de
   `canon check` (fail closed).
2. **La clé ne change ni le protocole ni les classes** de la porte
   (proposée → approuvée → lancée → confirmée, `read`/`reversible`/
   `irreversible`/`costly`) : elle dit seulement si un geste non couvert est
   refusé ou journalisé. Les approbations permanentes bornées et leur
   plafond restent le sujet de #7629 ; les connecteurs, celui de #7628.
3. **`journal` n'est pas un mode libre** : les quatre gestes réservés de
   Manaty (production, dépense, publication store ou publique, signature de
   décision) sont inscrits avec le « go » de l'owner dans la session, le
   reçu signé par sa clé fichier et la référence à l'entrée du journal de
   conversation. Un geste réservé non inscrit est signalé quand il est
   détectable (§3.4, #7631).
4. **`enforce` ne donne aucun droit nouveau** : il retire les chemins
   directs et fait attendre ce qui n'est pas couvert. L'autorité reste celle
   de l'humain (approbation du geste ou approbation permanente bornée).
5. **Lu au canon de l'action** : le mode est une politique du canon, pas une
   constante du code ameesh. Un changement de mode est un changement de
   canon (PR relue), pas un redéploiement de code.

## 4. Le bureau devient l'avatar Manaty

### 4.1 Aujourd'hui (mesuré le 17/11)

- **Deux sessions séparées**, lancées à la main : celle du poste (76 fiches de mémoire) et celle de la VM `bureau-goal` (101 fiches, la plus récente du 16/11). La VM tourne encore ; elle devait être supprimée après le 26/10.
- **Mode `externe`** : le runner ne réveille pas le bureau, qui lit son courrier par un script. Aucun tour n'est enregistré, donc ni coût, ni journal de conversation, ni provenance.
- **Mémoire** : des dossiers locaux sans version, qui ont divergé entre le poste et la VM.
- **Un seul placement**, `pc-smichea`. PC éteint, la session du poste s'arrête (34 h les 21 et 22/10).
- **Le nom `bureau` est en dur** dans le cœur d'ameesh.
- **Le client Manaty du poste est en 1.48.0** ; les fonctions d'avatar datent des versions 1.52 à 1.54.

### 4.2 Cible

- La fiche `agents/bureau.md` reçoit le bloc `avatar` (`conversation: portable`) et `releve: preference`.
- Deux placements ordonnés : `pc-smichea`, puis une VM d'agents.
- Le runner porte la session : le courrier la réveille, chaque tour est compté et journalisé.
- Une seule conversation, qui suit l'avatar du poste à la VM et retour.
- La mémoire est dans un dépôt GitHub privé au nom de l'owner, déclaré sur la fiche (`memory.repository`).
- Une charte Manaty, tirée des consignes en mémoire, dit ce que l'avatar fait seul et nomme les quatre gestes réservés.

### 4.3 Ce qui change pour l'owner

- Il ouvre la session par `ameesh attach bureau` au lieu de `claude`. C'est la même interface Claude Code, sur la session de l'avatar.
- Quand il se détache, l'avatar continue : il traite son courrier et ses minuteries.
- PC éteint, l'avatar continue sur la VM. Il n'y a ni les fichiers du poste ni Chrome : il y tient la supervision du mesh, pas les affaires qui demandent le poste.
- Depuis un autre poste, l'attach distant demande une passkey et un appareil enrôlé (décision du 12/11). Tant que l'owner n'en enrôle pas, l'accès hors du poste passe par SSH sur la VM, comme aujourd'hui.
- Après un déplacement, la nouvelle session reçoit un extrait borné de la conversation, pas tout l'historique. C'est comparable à une compaction.
- L'accès depuis le téléphone passe aujourd'hui par Remote Control, actif sur la VM. Son maintien est à essayer avant la bascule.

### 4.4 Réveil et coût

- Le bureau a reçu 1 428 courriers en sept jours. 551 sont passifs (copie, information, accusé). Les 877 autres ouvriraient un tour sous le runner : 35 à 240 par jour. Parmi eux, 8 demandes ou réponses et 7 messages de l'owner.
- Un tour Opus coûte environ 0,35 $ estimé au tarif public (mesuré sur les trois agents de gouvernance et sur l'avatar du pilote TPK).
- Règle retenue : les messages de l'owner, les urgences et les demandes réveillent tout de suite ; le reste est traité en un tour toutes les 30 minutes. Cela fait au plus 50 tours par jour, environ 17 $ estimés, contre 85 $ si chaque courrier réveillait.

### 4.5 Étapes

1. **Mémoire.** Le bureau réunit les fiches du poste et de la VM dans le dépôt privé.
2. **Charte Manaty.** Le bureau l'écrit à partir des consignes en mémoire, au format de la charte TPK. L'owner la valide.
3. **Poste.** Mise à jour du client Manaty, sur secteur.
4. **Essai avec un avatar de test**, sur le poste et une VM d'agents : réveil par courrier, cadence de 30 minutes, déplacement vers la VM et retour, attach. Ces fonctions n'ont jamais tourné.
5. **Fiches.** PR au canon Manaty : bloc `avatar`, `releve: preference`, second placement. La fiche de l'hôte `pc-smichea` doit aussi accepter le partage d'utilisateur pour la mémoire (`memory_isolation: shared-uid-accepted`), car d'autres agents y tournent sous le même compte.
6. **Bascule**, avec le go de l'owner, à un moment calme : arrêt des deux sessions, prise en main par le runner. Une session à la main reste possible en secours pendant une semaine.
7. **VM `bureau-goal`.** Suppression après la bascule.
8. **Journal des quatre gestes**, dès que le lot ameesh est livré.

## 5. Répartition corrigée

La règle ne change pas : si le mesh Manaty en aurait besoin aussi, c'est dans ameesh ; sinon dans IMA. Les ajouts sont en gras.

| Où | Quoi |
|---|---|
| Cœur d'ameesh | agents, courrier, baux, comptes, lots, décisions, rôles élus, alertes, API publique, hébergement en conteneur ; **avatars et clones ; porte d'actions ; mémoire git d'un agent** |
| Module développement d'ameesh | git, PR, CI, relecture, file de fusion, release, projection GitHub ; **connecteurs de fusion et de déploiement** |
| Module Nexlink | release et alertes Nexlink |
| Canon de chaque mesh | noms des rôles, hôtes, personnes, processus, routes d'alerte ; **fiches d'avatar, plafond d'organisation, classe de chaque opération** |
| **Dépôt persona de chaque personne** | **charte, préférences, mémoire lue par l'avatar** |
| **Memoria (TPK seulement)** | **réception par personne et mémoire ; la mémoire d'IMA y migre** |
| IMA | entrée des demandes TPK, tri, QA et déploiements TPK, Jobs Kubernetes, documents, Studio ; **connecteurs de publication TPK ; relais de Memoria vers la boîte de l'avatar** |

Trois règles s'ajoutent :
- **Joindre un humain, c'est écrire à son avatar.** Alertes et demandes d'approbation vont à l'avatar, qui les présente. Slack et le mobile ne portent que le signal « ton avatar a besoin de toi ». Cela remplace le canal Slack direct envisagé pour notify, et le nom `bureau` en dur.
- **IMA est l'avatar du travail sans propriétaire.** Ses agents de rôle sont ses clones : `agent spawn`, outils restreints, compte rendu au parent.
- **Un avatar n'agit au nom de son humain que par la porte** : contrôle par le code à TPK, journal à Manaty.

## 6. Choix par défaut du bureau, non revus en séance

- Le nom `bureau` est conservé ; c'est le bloc `avatar` de la fiche qui en fait un avatar.
- La VM de repli est la VM d'agents la moins chargée, à mesurer.
- Les messages envoyés pour l'owner côté Manaty portent la mention « agent IA de Sébastien ».

## 7. Ce qui n'est pas vérifié

- Aucune fonction d'avatar n'a été essayée : déplacement, relève à préférence, attach distant.
- La version d'ameesh sur les VM Manaty (`runner_presence` est vide).
- Si la liste d'outils d'une fiche s'applique à un agent qui n'est pas un clone.
- Si une règle de réveil groupé existe déjà dans le runner.
- Si la politique d'un canon peut déjà accepter un reçu signé par clé fichier depuis un hôte d'agents.
- Si les connecteurs claude.ai (Gmail, Slack) et Remote Control marchent dans une session portée par le runner sur une VM. Les connecteurs marchent sur le poste pour l'avatar du pilote.
- Le Mac de Tony.

## 8. Lots de la série et état

La série compte douze lots, du #7627 au #7638. Les états ci-dessous sont un
instantané du **2026-11-17 à 02:45Z** (`ameesh work show <id>`) ; l'état
vivant est dans ameesh, pas dans cette note. Chaque lot cite la section de
la spec qu'il applique.

| Lot | Objet | Spec | État au 17/11 02:45Z | Assigné | Est. |
|---|---|---|---|---|---|
| **#7627** | Cette note : spec owner, contrat de mode, fermeture de Q4 | toutes | cette note | ameesh-dev3 | 2 h |
| **#7628** | Un module peut déclarer des connecteurs | §3.5.1 | build | mty-amh-impl-11 | 8 h |
| **#7629** | Approbation permanente liée au bénéficiaire, à la cible et au plafond ; version de charte ; récap | §3.3, §3.5.2, §3.5.3, §3.5.6 | intake | mty-amh-impl-05 | 10 h |
| **#7630** | La liste d'outils de la fiche s'applique à l'avatar (tour résident et attach) | §3.5.4 | qa | ameesh-cx2 | 8 h |
| **#7631** | Journal Manaty des quatre gestes réservés, go de la session, reçu clé fichier | §3.4, §3.5.7 | build | mty-amh-impl-15 | 10 h |
| **#7632** | Réveil borné de l'avatar (humain, urgent et demandes tout de suite ; le reste groupé) | §4.4 | intake | — | 6 h |
| **#7633** | Hôte partagé d'avatars en pod Kubernetes, clone dans son pod | décision 11 | intake | — | 3 j |
| **#7634** | Joindre un humain, c'est écrire à son avatar ; « bureau » hors du cœur | §5 | intake | mty-amh-impl-08 | 8 h |
| **#7635** | Qualification des fonctions d'avatar sur hôtes réels, avatar d'essai | §4.5.4 | intake | mty-amh-impl-18 | 1 j |
| **#7636** | Contrat public ameesh ↔ module extérieur (IMA), testé des deux côtés | §5 | intake | — | 2 j |
| **#7637** | Cœur générique : rôles, hôtes et release de Manaty au canon ou par module | §5 | intake | — | 2 j |
| **#7638** | Gouvernance Manaty : étude chiffrée de la part mécanique à mettre en code | §5 | build | mty-amh-impl-01 | 1 j |

La note de référence ne remplace pas les lots : elle fixe les décisions et
le contrat commun ; chaque lot porte son périmètre, ses tests et sa note.
