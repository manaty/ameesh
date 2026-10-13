# #802 — R2 Codex, 2026-10-13

Tête : `c1e051272229f7e527a0a133c08a594382921f6c` (PR #618). **GO — 0 B / 0 N.**

B1 de R1 levé : chaque worker utilise connect(statement_timeout_ms=6000), configuration appliquée à la session par les deux pilotes ; une connexion de même configuration atteste pg_settings=6000 ms. Le SET isolé disparu ne peut plus donner un faux témoin psql. Le module figure dans tests/parts/pilote-psql.txt.

Lecture du diff complet depuis main 5bc2c5de : verrou consultatif de transaction pris avant lecture des packages, clé canon + schéma ; après commit du premier sync, les suivants relisent les fiches inchangées. Libération sur commit/rollback. Le témoin impose 12 connexions concurrentes et vérifie absence d'erreurs, un seul créateur et 44 fiches inchangées. Preuves auteur rouge/vert des deux pilotes et 129 régressions par pilote reçues ; aucune suite rejouée.

Limites déjà relevées en R1 : autres canons, rebase_default et plan_package ne partagent pas cette sérialisation (#831). Ce verdict porte sur #802, pas sur une sérialisation générale de tous les écrivains.

Contrôle API PR : tête exacte confirmée ; CI encore en file/en cours (run 38376598766). Ce GO de lecture ne remplace pas la CI verte ni le feu du pilote.
