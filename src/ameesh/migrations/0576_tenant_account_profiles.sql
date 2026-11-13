-- SPDX-License-Identifier: AGPL-3.0-only
-- 0576_tenant_account_profiles — #7108 (avatar B4, conception 5322 §3.5) :
-- profils de compte publiés par le runner d'un tenant d'hôte partagé.
--
-- Le déplacement manuel d'un avatar `releve: preference` vers un hôte
-- partagé doit refuser avant tout effet quand le tenant de la cible n'a
-- aucun profil de compte pour le harnais de l'agent (`move-account-missing`).
-- Les profils vivent sous le home 0700 du tenant, que le superviseur ne lit
-- pas : seul le runner du tenant les connaît, et il les publie dans son
-- battement (`runner_presence`, 0575). Seuls des noms `harnais/compte`
-- passent ici, jamais un chemin ni un secret.
--
-- Additif seulement : colonne neuve à défaut vide ; un exécuteur d'avant la
-- nomme jamais, et une ligne sans profil ne change pas la présence (§3.2).
alter table runner_presence
    add column if not exists profiles text[] not null default '{}';

comment on column runner_presence.profiles is
    '#7108: account profile names `harness/account` published by the tenant runner (never a path or a secret), read by the manual avatar move preflight.';
