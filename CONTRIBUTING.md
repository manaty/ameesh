# Contribuer à ameesh

Merci de votre intérêt. Quelques règles avant d'ouvrir une demande de fusion :

- **Licence** : ameesh est publié sous **AGPL-3.0-only** (voir `LICENSE`).
- **Accord de contribution** : les contributions externes ne sont acceptées
  qu'après signature d'un accord de contribution (CLA) qui permet à Manaty de
  continuer à proposer ameesh sous d'autres licences. Le texte de l'accord est
  en préparation : ouvrez d'abord une issue pour discuter de votre proposition.
- **Aucun nom de client** dans le code, les tests, les exemples ou la
  documentation : utilisez des organisations fictives (par exemple « acme »).
- **Revue proportionnée au risque** : SQL, sécurité, autorisation, exécuteur et
  tout ce qui touche à la production sont relus avant fusion ; voir
  `docs/design/decisions/0018-vitesse-des-agents.md`.
- **Tests** : la suite tourne sur un vrai Postgres avec les deux pilotes ;
  `scripts/test.sh` en local (ou `scripts/test-parallele.sh -n 4`, en parts
  parallèles), l'intégration continue sur chaque PR : psycopg complet et psql
  sur les modules de `tests/parts/pilote-psql.txt` ; le psql complet tourne
  sur `main` et `release/*`. Un module de test qui touche la couche base ou un
  comportement propre à un pilote s'ajoute à cette liste.
- **En-tête de licence** `SPDX-License-Identifier: AGPL-3.0-only` en tête des
  nouveaux fichiers source.

La conception (décisions, exigences, spécification) est dans `docs/design/`.
