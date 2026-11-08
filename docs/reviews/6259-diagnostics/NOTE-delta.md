# #6259 — delta pg_stat_statements → schéma diagnostics (décision mesh-design #200197, option b)

Relevés lecture seule (central, base ameesh, 08/11 05:4xZ, `releves.out`) :
1. `pg_stat_statements` 1.11 installée, **relocatable = t** ; présente seulement dans la base `ameesh` (schéma public) ; `shared_preload_libraries = pg_stat_statements`.
2. Lecteurs : **aucun** — 0 requête citant la vue dans pg_stat_statements, 0 dans pg_stat_activity, aucun exporteur/supervision ni config sous /etc (les services `memoria-*export*` en échec n'ont pas de rapport). Donc **aucun GRANT USAGE** sur diagnostics (postgres, superuser, la lit toujours).
3. Dépendants non-extension : seulement les deux types de ligne des vues (déplacés avec elles) ; aucune vue (pg_rewrite) ni fonction (prosrc) applicative.

Delta : `delta-diagnostics.sql` (une transaction, gardes avant/après, lock_timeout 5 s). Puis relance de `role-superviseur.sql` (0 écart attendu), `role-superviseur-contenus.sql`, `role-auditeur.sql` selon le dossier v3.
Retour : `retour-diagnostics.sql`.
