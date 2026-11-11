-- SPDX-License-Identifier: AGPL-3.0-only
-- 0565_work_item_external_claim — #6839 : la prise exclusive d'un geste
-- d'exécutant externe.
--
-- Constat du 11/11 : le bureau est une seule identité portée par deux
-- sessions (BG et PC). Le même feu d'achat de #6836 a été exécuté deux fois,
-- faute de prise exclusive et de clé d'unicité sur le geste. La prise est
-- écrite sur le lot du geste, AVANT l'effet externe, sous le verrou de sa
-- ligne : deux sessions concurrentes ne peuvent pas la réussir toutes les
-- deux, et la seconde reçoit le nom de la porteuse et l'heure.
--
--   external_session        : la session que le feu nomme (facultative).
--   external_claim_session  : la session qui a pris le geste (NULL = libre).
--   external_claimed_at     : l'instant de la prise, avec la session.
--
-- La prise n'est ni une attribution ni un droit de dépense : le lot reste
-- sans `assignee`, la règle du go owner et les autres gardes ne changent pas.
-- La levée de la réservation (`external-executor --clear`) efface la prise.
--
-- Additif seulement : trois colonnes nullables, aucune ligne existante ne
-- change de sens. Verrou borné, la table des lots est très écrite.
set local lock_timeout = '5s';

alter table work_items
    add column if not exists external_session text,
    add column if not exists external_claim_session text,
    add column if not exists external_claimed_at timestamptz;

alter table work_items
    add constraint work_items_external_session_ck
        check (external_session is null
               or (btrim(external_session) <> ''
                   and external_executor is not null)),
    add constraint work_items_external_claim_ck
        check ((external_claim_session is null) = (external_claimed_at is null)),
    add constraint work_items_external_claim_session_ck
        check (external_claim_session is null
               or (btrim(external_claim_session) <> ''
                   and external_executor is not null));

comment on column work_items.external_session is
    '#6839: session named by the fire for an external executor gesture; NULL = any session of the executor may take it. Not an assignment.';
comment on column work_items.external_claim_session is
    '#6839: session that atomically took the external gesture (NULL = free); written before the external effect.';
comment on column work_items.external_claimed_at is
    '#6839: instant of the exclusive external claim, read with external_claim_session.';
