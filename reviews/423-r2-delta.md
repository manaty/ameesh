# #423 — PR #568 — R2 delta Codex

13/10/2026, codex3. Tête relue : `c074dc1113c2f488a1c03b0d5f2c220fbe018c87` ; delta depuis `65a0d085`.

**GO delta — B1 corrigé, 0 B résiduel dans le delta / 0 N nouveau / 0 n.** Classe sensible ; le lot complet inclut la migration SQL 0418.

Périmètre : `src/ameesh/reprise.py`, `src/ameesh/exploitation.py`, `tests/test_423_agent_aliases.py`, avec lecture du résolveur `agent_names.resolve` et des opérations de stockage appelées. Aucun élargissement aux étapes 2-3 du renommage.

- `adopt`, `resume` et `restart` résolvent désormais le nom reçu avant toute lecture/écriture opérationnelle. Les écritures SQL strictes (`operations.adopt/resume/request_restart/apply_restart`) reçoivent le primaire ; les contrôles de bail, notifications, oubli local, marqueur de worktree et traces utilisent ensuite cette même variable. B1 (lecture résolue, écriture sous alias refusée) est fermé sur ces trois entrées.
- Le résolveur commun renvoie les noms inconnus inchangés : les refus existants restent applicables. Il journalise les alias résolus sans ajouter une identité ni conférer de droit. Les opérations SQL conservent leurs verrous et conditions de bail ; le correctif ne les affaiblit pas.
- Les trois témoins `SessionCommandAliasTest` vérifient adoption avec refus de bail vivant puis succès, reprise de la même session et reprise fraîche avec oubli local sous le primaire, redémarrage avec notification sous bail vivant puis application sans bail. Ils vérifient les traces de résolution et l'absence d'identité parallèle. Les résultats ciblés et mutations rouges cités dans #423 sont ceux de l'auteur ; aucune suite rejouée par le relecteur.

Les observations non bloquantes antérieures ne sont pas annulées par ce delta. Le GO porte sur le correctif B1 ; aucune attestation de CI finale ni du second avis.

Portes confirmées dans #423 (mesh-design, 13/10, 21:40) : deux avis indépendants de familles différentes hors auteur, CI exacte verte sans part annulée et tête inchangée ; fusion par l'auteur avec `--match-head-commit`. Le second avis Claude reste requis. Aucun changement des identités existantes, déploiement, fusion ou migration appliquée dans cette revue.
