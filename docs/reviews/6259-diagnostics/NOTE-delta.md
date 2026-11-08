# #6259 — delta pg_stat_statements → schéma diagnostics (décision mesh-design #200197, option b)

Relevés lecture seule (central, base ameesh, 08/11 05:4xZ, `releves.out`) :
1. `pg_stat_statements` 1.11 installée, **relocatable = t** ; présente seulement dans la base `ameesh` (schéma public) ; `shared_preload_libraries = pg_stat_statements`.
2. Lecteurs : **aucun lecteur observé** (borné : stats_reset 27/10 08:49Z, démarrage 27/10 06:16Z ; un lecteur intermittent ou externe reste possible) — 0 requête citant la vue dans pg_stat_statements, 0 dans pg_stat_activity, aucun exporteur/supervision ni config sous /etc (les services `memoria-*export*` en échec n'ont pas de rapport). Donc **aucun GRANT USAGE** sur diagnostics (postgres, superuser, la lit toujours).
3. Dépendants non-extension : seulement les deux types de ligne des vues (déplacés avec elles) ; aucune vue (pg_rewrite) ni fonction (prosrc) applicative.

Delta : `delta-diagnostics.sql` (une transaction, gardes avant/après, lock_timeout 5 s). Puis relance de `role-superviseur.sql` (0 écart attendu), `role-superviseur-contenus.sql`, `role-auditeur.sql` selon le dossier v3.
Retour : `retour-diagnostics.sql`.

## v2 (avis B impl-03 #200242, N cx3 #200238)
- `pg_default_acl` : **0 entrée de type schéma** (3 entrées d'autres types) ; garde ajoutée au delta (refus si une ACL par défaut de schéma existe).
- Post-check : aucun droit sur `diagnostics` hors propriétaire (nspacl/acldefault), aucun membre de l'extension resté dans `public` (relations, types, fonctions).
- Retour : `lock_timeout 5s`, sans CASCADE.
- Bornes des relevés : `stats_reset` 2026-10-27 08:49Z, démarrage 06:16Z ; template1 sans l'extension ; timers/cron/conteneurs/unités memoria : `releves2.out`.
