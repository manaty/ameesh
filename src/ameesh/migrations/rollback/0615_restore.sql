-- SPDX-License-Identifier: AGPL-3.0-only
-- 0615_restore — #8459 : retour arrière des marqueurs de veille.
--
-- Perdre ces marqueurs ne change pas l'état du hub, mais une dette de sortie
-- en cours ne serait plus rejouée ni alertée : avant d'appliquer ce retour
-- arrière, vérifier `deploy/serveur/replication/etat.sh` et rejouer
-- `mode-veille.sh sortir` si la veille est active. Le code N-1 ne lit ni
-- n'écrit ces colonnes.
ALTER TABLE host_lifecycle
    DROP COLUMN IF EXISTS standby_exit_failed_at,
    DROP COLUMN IF EXISTS standby_entered_at;
