-- SPDX-License-Identifier: AGPL-3.0-only
-- 0035_authentificateurs_par_canon — les authentificateurs (passkeys des
-- humains) PAR CANON (lot L44, décision 0031, étude « plusieurs canons »
-- point 5).
--
-- Convention de L42 (`canon_state.canon`) : `canon` = '' pour le canon PAR
-- DÉFAUT de l'hôte, l'identifiant du canon (id de sa fédération) sinon. Toutes
-- les lignes existantes appartiennent au canon par défaut : avant L44, lui
-- seul synchronisait ce registre (aucun changement de données pour un hôte à
-- un seul canon).
--
-- 1. `authenticators.canon` : le canon qui a DÉCLARÉ la ligne (celui dont la
--    synchronisation l'a écrite). La clé d'unicité des lignes actives devient
--    (canon, facade, credential_id) : un humain déclaré dans deux canons, avec
--    la même passkey, y a DEUX lignes distinctes (deux `id`), chacune
--    synchronisée, mise à jour et révoquée par son seul canon. `canon_ref` ne
--    suffit pas : une référence du canon par défaut n'est pas préfixée, et
--    `rebase_default` (L42) rattache des lignes à un autre canon sans
--    réécrire leurs références — l'appartenance doit être une colonne,
--    contrôlée par l'index unique, pas déduite d'un texte.
--
-- 2. `authenticator_syncs.canon` : le journal des synchronisations devient
--    une séquence PAR canon. Le dernier commit appliqué, la monotonie
--    (descendance des commits) et la branche de confiance « applied » se
--    lisent dans le journal de CE canon ; synchroniser A, puis B, puis A ne
--    compare jamais un commit de B à un commit de A. Le verrou consultatif du
--    registre reste unique (toutes les synchronisations se sérialisent).
--
-- 3. `actions.canon` : le canon de l'action, fixé à l'INSERTION par le
--    déclencheur `ameesh_action_canon` (jamais par l'appelant) : celui de
--    l'agent qui la propose (`agent_registry.canon`, NULL = ''), celui de
--    l'action remplacée pour une action qui en remplace une autre (doublon
--    assumé) ; '' (canon par défaut) pour un proposant humain ou inconnu —
--    une portée qui ne se rattache à aucun canon relève du canon par défaut.
--    Un reçu d'approbation de l'action n'est vérifié que contre les
--    authentificateurs de ce canon (`receipts.Policy.canon`), et un grant
--    (approbation permanente) ne couvre que les actions du canon de son
--    authentificateur.

-- 1. ---------------------------------------------------------- registre
alter table authenticators add column if not exists canon text not null default '';

comment on column authenticators.canon is
  'L44 (0031) : canon déclarant ; '''' = canon par défaut de l''hôte, sinon l''id du canon.';

drop index if exists authenticators_live_idx;
create unique index if not exists authenticators_live_idx
    on authenticators (canon, facade, credential_id) where revoked_at is null;
create index if not exists authenticators_canon_approver_idx
    on authenticators (canon, approver, facade);

-- 2. ---------------------------------------------------------- journal
alter table authenticator_syncs add column if not exists canon text not null default '';

comment on column authenticator_syncs.canon is
  'L44 (0031) : canon synchronisé ; '''' = canon par défaut de l''hôte, sinon l''id du canon.';

create index if not exists authenticator_syncs_canon_idx
    on authenticator_syncs (canon, id);

-- 3. ---------------------------------------------------------- actions
alter table actions add column if not exists canon text not null default '';

comment on column actions.canon is
  'L44 (0031) : canon de l''action (celui de son proposant) ; '''' = canon par défaut.';

create or replace function ameesh_action_canon() returns trigger
language plpgsql as $$
begin
    if new.replaces is not null then
        new.canon := coalesce((select a.canon from actions a
                                where a.action_id = new.replaces), '');
    elsif new.proposed_by like 'agent:%' then
        new.canon := coalesce((select r.canon from agent_registry r
                                where r.name = substr(new.proposed_by, 7)), '');
    else
        new.canon := '';
    end if;
    return new;
end $$;

drop trigger if exists actions_canon on actions;
create trigger actions_canon
    before insert on actions
    for each row execute function ameesh_action_canon();
