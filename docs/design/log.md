# Journal de la conception

## 2026-10-05 (bascule)
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
