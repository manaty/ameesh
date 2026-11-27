-- SPDX-License-Identifier: AGPL-3.0-only
-- 0612_restore — #8344 : retour arrière de la table des alertes
-- d'application. Une alerte ouverte perdue n'est plus poussée ; l'application
-- la relève à sa prochaine observation (la levée est idempotente).
drop table if exists app_alerts;
