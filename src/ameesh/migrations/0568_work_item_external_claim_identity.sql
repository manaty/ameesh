-- SPDX-License-Identifier: AGPL-3.0-only
-- 0568_work_item_external_claim_identity — #6877 : la prise d'un geste hôte
-- nomme l'identité COMPLÈTE de la liaison de session.
--
-- #6839 ne persistait que le libellé (`external_claim_session`) et
-- l'identifiant de session (`external_claim_proof`). Or l'unicité d'une
-- liaison active est (hôte, harnais, session), pas l'identifiant seul : deux
-- sessions du bureau sur deux hôtes peuvent porter le même libellé et le même
-- identifiant de session, et une liaison révoquée puis refaite est une autre
-- ligne. La porte du geste hôte doit donc comparer l'agent, l'hôte, le
-- harnais et la LIGNE de liaison, pas une étiquette réutilisable.
--
--   external_claim_harness  : le harnais de la liaison qui a pris.
--   external_claim_binding  : la ligne `session_bindings.id` qui a pris
--                             (une révocation puis nouvelle liaison = autre
--                             ligne, donc autre preuve ; le pilote peut
--                             libérer en --force).
--
-- Les deux colonnes vont ensemble (NULL,NULL pour une prise écrite par le
-- code d'avant 0568 ou par un autre écrivain) ; la porte refuse alors la
-- prise et demande de la libérer puis de la reprendre depuis une session liée.
-- Additif seulement : colonnes nullables, aucune ligne existante ne change de
-- sens, et le code d'avant ne les nomme jamais.
set local lock_timeout = '5s';

alter table work_items
    add column if not exists external_claim_harness text,
    add column if not exists external_claim_binding bigint;

-- ameesh: constraint not tightened
-- Les deux colonnes sont neuves et vont ensemble : le code d'avant n'écrit ni
-- l'une ni l'autre (NULL,NULL), le nouveau les écrit toujours ensemble. La
-- contrainte ne peut donc refuser aucune ligne existante ni aucune écriture
-- N-1 ; elle refuse une écriture partielle d'un autre écrivain.
alter table work_items
    add constraint work_items_external_claim_identity_ck
        check ((external_claim_harness is null) = (external_claim_binding is null));

comment on column work_items.external_claim_harness is
    '#6877: harness of the verified L41 binding that took the external gesture; with external_claim_binding, NULL = claim written before the full identity (the host-gesture gate refuses it).';
comment on column work_items.external_claim_binding is
    '#6877: session_bindings.id of the verified binding that took the external gesture; a revoked-then-rebound session is another row, so the gate refuses until release (pilot --force if the holder is gone).';
