-- SPDX-License-Identifier: AGPL-3.0-only
-- L242b (5) / #1521 : dater le début du MOTIF de pause, pas celui du statut.
--
-- `status_since` ne bouge que si `status` change (0027). Un agent `blocked`
-- sur auth/pression depuis 3 h qui passe `blocked` sur budget depuis 1 min
-- gardait donc un `status_since` de 3 h, et l'alerte `critical_budget_pause`
-- (L242b (5)) annonçait une pause budget de 3 h — faux.
--
-- `status_reason_since` date le début du motif affiché : il bouge quand le
-- statut change, ou quand la CATÉGORIE du texte change — le texte jusqu'au
-- premier `:`, stable pour les motifs longs (`budget : `, `auth_blocked:`).
-- Une actualisation du détail (`… 87 % used with 62 % of the window elapsed`)
-- ne le remet donc pas à zéro. `status_since` garde sa sémantique (statut
-- seul). L'alerte de pause budget lit la nouvelle colonne.

alter table agent_registry add column if not exists status_reason_since timestamptz;

-- Historique inconnu : au mieux, la date du dernier changement de statut.
update agent_registry set status_reason_since = coalesce(status_since, updated_at, now())
 where status_reason_since is null;
alter table agent_registry alter column status_reason_since set default now();

create or replace function ameesh_status_since() returns trigger
language plpgsql as $$
declare
    motif  text;
    ancien text;
begin
    if tg_op = 'INSERT' then
        new.status_since := coalesce(new.status_since, now());
        new.status_reason_since := coalesce(new.status_reason_since, new.status_since);
        return new;
    end if;
    if new.status is distinct from old.status then
        new.status_since := now();
        new.status_reason_since := now();
        return new;
    end if;
    motif := split_part(coalesce(new.status_text, ''), ':', 1);
    ancien := split_part(coalesce(old.status_text, ''), ':', 1);
    if motif is distinct from ancien then
        new.status_reason_since := now();
    end if;
    return new;
end $$;

drop trigger if exists agent_registry_status_since on agent_registry;
create trigger agent_registry_status_since
    before insert or update of status, status_text on agent_registry
    for each row execute function ameesh_status_since();
