# #1132 — avis delta Codex3, contrat superviseur

Tête examinée : `30bb9f38961bc11a4a7bfe4ac8af6eb5cf73bd60`, PR #873.
Delta depuis le dernier GO `35fa9995` : une ligne de `deploy/sql/role-superviseur.sql` ajoute `produced_at` et `source` à la liste de colonnes lisibles de `quota_gauge_readings`.

**GO du delta, 0 B / 0 N nouveaux.** La date de production et le nom de l'agent source sont des métadonnées d'état cohérentes avec les colonnes de jauge déjà accordées. Vérifié contre la migration exacte `0434_gauge_provenance.sql`, le dataclass Gauge, `_stamp_gauges` (source = nom de l'agent, aucun extrait de journal), et `record_gauges`. Aucun droit d'écriture, contenu de journal ou matériel d'authentification ajouté. La mécanique transactionnelle du contrat et ses contrôles effectifs restent inchangés. Les corrections du précédent GO ne sont pas modifiées.

Validation : lecture du delta et des sources exactes, `git diff --check` propre. Aucune suite rejouée, aucun accès DB, aucune CI déclenchée ou sondée. Tests SuperviseurRoleTest 15/15 et test_1132 22/22 déclarés par l'auteur, non reproduits ici.

Cet avis complète le GO Codex sur `35fa9995` ; il ne remplace pas l'avis indépendant requis sur les droits, la porte #1078, la relecture des résolutions de rebase, la CI sur la tête finale ou le feu du pilote. Aucune fusion ni activation.
