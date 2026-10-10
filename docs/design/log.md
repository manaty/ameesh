# Journal de la conception

## 2026-10-10 (auditeur interne)
* **Décision** : [auditeur interne](decisions/0036-auditeur-interne.md). Demande du propriétaire : « qu'ameesh ait un auditeur interne qui régulièrement, par exemple une fois par heure, regarde que tout se passe bien, que l'utilisation des ressources est optimale, et adapte les règles si besoin ». Persona `auditeur` (DeepSeek `deepseek-flash`), marge d'action en deux niveaux, consigne [AUDITEUR.md](../AUDITEUR.md). Fiche et placement proposés au canon manaty, mise en service après leur fusion. À ouvrir : un lot pour router les alertes urgentes vers un agent dans `ameesh notify`.

## 2026-10-10 (sous-utilisation)
* **L94** : la sous-utilisation alerte autant que la surcharge (« la surconso comme la sous-conso devrait alerter ameesh ») — `plan_underused` (forfait perdu à la remise à zéro, pertes au sens de L74), `idle_capacity` (agents réveillables au repos pendant que du travail attend, ou que le token travaille), `orchestrator_held` (orchestrateur tenu par `attach` avec du courrier, prolonge 0030), `host_underused` (suggestion de déplacement, 0028) ; poussées par `ameesh notify`. Ajout validé par le propriétaire : `balance_low`, autonomie d'un fournisseur payé au token au rythme réel des relevés de solde (48 h, 20 USD ; urgente sous 12 h ou 5 USD).

## 2026-10-10 (comptes au forfait)
* **Décision** : [consommer d'abord ce qui expire](decisions/0034-consommer-d-abord-ce-qui-expire.md), qui amende 0027 ; lot L74.
* **L74** : comptes au forfait en réservoir — choix par échéance de la capacité inutilisée sous le seuil de rythme, continuité de session, relevé échu à 0 % (même règle que L71), choix journalisés avec leur raison, capacité perdue à la remise à zéro dans `ameesh accounts list`.

## 2026-10-10
* **L60** : rotation de session DeepSeek et plafond de contexte qui agit. Les tours DeepSeek étaient inscrits sans session ni modèle, et leur usage par étape ignoré : jamais de rotation sur la taille, 2,34 milliards de jetons relus en 24 h. Au-delà de 15 M jetons relus au dernier tour (`context_max_tokens`, migration 0041), la session est tournée avec résumé de reprise. Le modèle du tour DeepSeek est connu (défaut du descripteur passé au harnais) : fin du barème `pro` appliqué par prudence, qui gonflait l'estimation d'un facteur 12,7 face au solde. Ligne du grand livre clé par son marqueur comptable : plus de doublon quand un exécuteur s'arrête entre l'écriture et l'effacement.
* **L62** : vue par projet, `ameesh projects` (schéma `ameesh-projects/1`) — agents, état et raison, lot en cours, non-lus, dépense 24 h, forfait ou token, lots ouverts sans agent, projets sans agent actif ; colonne PROJET dans `ameesh list` ; la même vue en tête de `ameesh progress`. Retour du propriétaire : on ne voyait pas quels projets étaient en cours ni qui travaillait sur quoi.

## 2026-10-09
* **Bascule** : deepseek1 à 7 et l'orchestrateur Nexlink menés par l'exécuteur, un service par agent (`ameesh-runner-agent@<nom>`) ; plus aucune boucle v0.
* **L48** (1.4.1) : échecs rapides de tour, attente doublée puis arrêt de l'agent.
* **L49** (1.4.2) : le plafond horaire payé au token ne met plus en pause un agent au forfait (0019 §2) ; constaté quand la dépense DeepSeek a arrêté l'orchestrateur.

## 2026-10-08
* **Fusion et déploiement** : conception (#7), lots L36–L46 (#8) et L47 (#9, périmètre sous `extensions.ameesh`, seule clé libre du schéma OKF Federation) ; migrations 0030–0036 appliquées ; deux canons en exploitation sur un même hôte.
* **Décision** : [plusieurs canons](decisions/0031-plusieurs-canons.md) passe `stable`, modalités confirmées telles quelles.
* **Convention** : un agent ameesh porte un nom de rôle ; les noms numérotés hérités de la v0 (`deepseekN`…) ne sont plus attribués, pour éviter les homonymes entre la boîte v0 et le registre.
* **Version** 1.4.0.

## 2026-10-07 (soir : lots codés, non fusionnés)
* **Relecture indépendante** de l'intégration : 5 défauts importants et une fuite possible de secret, corrigés par le lot L46 (migration 0036) ; `integ/0030-0031` @ 3eb40f9, suite complète verte (1408 tests, psql et psycopg).
* **Lots** L36–L45 codés et intégrés sur la branche locale `integ/0030-0031` (20 commits sur `origin/main`, migrations 0030–0035) ; suite complète verte sur les deux pilotes (1388 tests, psql et psycopg). Rien n'est poussé ni fusionné : relecture et accord du propriétaire attendus.
* **Second canon** : fiches ameesh de la seconde organisation préparées hors dépôt et validées par le code intégré.

## 2026-10-07 (plusieurs canons)
* **Décision** (modalités à confirmer) : [plusieurs canons](decisions/0031-plusieurs-canons.md) et [étude](etudes/plusieurs-canons.md) ; lots L42–L45 ; migration 0032 réservée.

## 2026-10-07 (orchestration et vivacité)
* **Décision** : [pas de travail sans réveil possible](decisions/0030-pas-de-travail-sans-reveil-possible.md) ; lots L36–L41 adoptés.
* **Étude** : [orchestration et vivacité](etudes/orchestration-et-vivacite.md) — blocage d'un chantier par un orchestrateur externe arrêté ; décision proposée et lots L36–L41 ; migrations 0030–0031 réservées.

## 2026-10-05 (persona et harnais)
* **Étude** : [persona et harnais](etudes/persona-et-harnais.md) ; spécification de la fiche Persona, des sessions, de la désignation au harnais et d'une mémoire neutre ; points à trancher dans les [questions ouvertes](questions-ouvertes.md).

## 2026-10-05 (bascule)
* **Décision** : [persona et session](decisions/0029-persona-et-session.md) ; L31 étendu à la refonte du placement (admissions sans position).
* **Décision** : [ressources des hôtes et répartition](decisions/0028-ressources-des-hotes-et-repartition.md) ; lot L31 ; migration 0029 réservée.
* **Décision** : [bascule automatique entre comptes](decisions/0027-bascule-automatique-entre-comptes.md) ; lot L30 ; migration 0028 réservée.
* **Lot** : L29, plan de travail (epics, fermeture sur fusion, projection GitHub) ; migrations réservées 0026 (L29) et 0027 (L26).
* **Décision** : [hébergement d'ameesh-approve par équipe](decisions/0026-hebergement-d-ameesh-approve-par-equipe.md) ; [contrat](ameesh-approve-hebergement-equipes.md) (codex3) ; lot L27.
* **Jalon** : ameesh v1.0.0 publiée (manaty/ameesh, tag v1.0.0) ; bascule étapes 0 à 2 faites (base durable, sauvegarde quotidienne hors du poste, canon manaty/home fusionné), étape 3.1 (utilisateur ameesh-approve) faite.
* **Précision** : [page d'approbation](decisions/0017-approbation-via-page-nexlink.md) servie par Nexlink sur `ameesh.nexlink.ph`.
* **Décision** : [une session neuve par lot](decisions/0025-une-session-par-lot.md) ; lot L26 (parité d'exploitation v0 → v1).
* **Lot** : L25, profil cluster (image, manifestes Kubernetes, rôle superviseur en lecture seule).

## 2026-10-04 (v1 terminée)
* **Jalon** : v1 terminée sur `main` 2ff67ff — tous les lots L1–L9 et L9b fusionnés après revue ; `scripts/test.sh` complet : 652 tests verts sur chacun des deux pilotes (psql : 5 sautés, psycopg : 2 sautés) ; essai de bout en bout vert. Hors v1 : L11, L12, L13 fusionnés.

## 2026-10-04 (v1)
* **Vérification** : `scripts/test.sh` complet sur `main` 9ffb2e2 — 618 tests verts sur chacun des deux pilotes (psql : 5 sautés, psycopg : 2 sautés), base temporaire.
* **Jalon** : v1 fonctionnellement complète sur `main` (d2611b0) — lots L2–L9 et L9b fusionnés après revue ; essai de bout en bout vert ; L1 proposé en v1.1.

## 2026-10-04 (critère d'adoption)
* **Décision** : [critère d'adoption des réglages](decisions/0023-critere-d-adoption-des-reglages.md).

## 2026-10-04 (auto-réparation)
* **Décision** : [auto-réparation](decisions/0022-auto-reparation.md) et [étude](etudes/boucle-auto-reparation.md) ; R23 ; lots L18–L23.

## 2026-10-04 (harnais)
* **Décision** : [catalogue des harnais](decisions/0021-catalogue-des-harnais.md) ; R22 ; lots L16–L17.

## 2026-10-04 (modèles)
* **Décision** : [catalogue et évaluation des modèles](decisions/0020-catalogue-et-evaluation-des-modeles.md) ; R21 ; lots L14–L15.

## 2026-10-04 (vitesse et budgets)
* **Décision** : [vitesse](decisions/0018-vitesse-des-agents.md) et [budgets](decisions/0019-budgets-et-routage.md) (transmises par l'orchestrateur Nexlink) ; exigences R19–R20 ; lots L10–L13.

## 2026-10-04 (revue codex3 de la spec, exposition)
* **Correction** : [spécification](specification.md) — canon lu à la révision fusionnée de la branche canonique ; grant permanent à nonce unique puis réservations atomiques ; invariant de bail à marge d'ordonnancement près ; garantie de la porte limitée en v1.
* **Décision** : [ameesh-approve via une page Nexlink](decisions/0017-approbation-via-page-nexlink.md).

## 2026-10-04 (spec v1)
* **Création** : [spécification d'ameesh v1](specification.md) et plan de lots L1–L9.

## 2026-10-04 (Q1, Q8)
* **Décision** : [stockage](decisions/0016-stockage.md) ; Q1 close. Q8 close (instantané public, voir [0015](decisions/0015-interfaces-des-facades.md)). Plus aucune question n'attend le propriétaire.

## 2026-10-04 (Q6)
* **Décision** : [aucun client nommé, interfaces publiques des façades](decisions/0015-interfaces-des-facades.md) ; Q6 close ; mentions de clients retirées des documents ; Q8 ouverte (historique avant passage public).

## 2026-10-04 (Q5)
* **Décision** : [orchestrateurs à tours et placement gouverné](decisions/0014-orchestrateurs-a-tours-et-placement.md) ; exigence R18 ; Q5 close.

## 2026-10-04 (Q4)
* **Décision** : [licence AGPL-3.0-only avec accord de contribution](decisions/0013-licence-agpl.md) ; Q4 close.

## 2026-10-04 (Q3)
* **Décision** : [abandon de la cérémonie Ed25519, autorité par ameesh-approve](decisions/0012-autorite-par-ameesh-approve.md) ; Q3 close, Q6 ouverte (application native).

## 2026-10-04 (revue codex3, B3)
* **Correction** : R5 — identité d'action stable (clé d'idempotence) distincte du nonce d'autorisation ; nouvelle action après issue inconnue seulement par décision humaine assumant le doublon ; `Message-ID` ramené à une poignée de réconciliation.

## 2026-10-04 (Q2)
* **Décision** : [hébergement configurable et provisionnement par le harnais de l'utilisateur](decisions/0011-hebergement-configurable.md) ; Q2 close, Q1 reformulée.

## 2026-10-04 (revue codex3)
* **Correction** : R8 — pas de délégation d'`approve` aux agents ; approbation permanente bornée signée par un humain.
* **Correction** : R14 et [0008](decisions/0008-agents-dans-le-canon.md) — un agent éphémère hérite de l'humain responsable de son créateur.
* **Ajout** : protocole d'exécution d'une action irréversible (idempotence, issue inconnue, réconciliation) dans les [exigences](exigences.md).

## 2026-10-04
* **Création** : bundle de conception — [exigences](exigences.md), [questions ouvertes](questions-ouvertes.md), dix [décisions](decisions/) du propriétaire prises le 2026-10-03 et cinq [études](etudes/).
