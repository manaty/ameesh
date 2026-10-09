-- SPDX-License-Identifier: AGPL-3.0-only
-- 0039_fin_de_fille — une session parallèle (fille, L52b) s'éteint à la fin
-- de son lot (lot L52e) : raison d'arrêt structurée `fin_de_lot`.

alter table agent_registry drop constraint if exists agent_registry_stop_reason_check;
alter table agent_registry add constraint agent_registry_stop_reason_check
    check (stop_reason is null
           or stop_reason in ('manuel', 'bail_expire', 'retire_du_canon', 'externe', 'erreur',
                              'fin_de_lot'));
