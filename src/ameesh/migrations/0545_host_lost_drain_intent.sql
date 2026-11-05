-- SPDX-License-Identifier: AGPL-3.0-only
-- #5869 : un drainage manuel en cours garde son intention à travers une perte.
-- Une ligne `lost` issue de `draining` le dit ; le reconcile la ramène alors à
-- `draining` (même génération, même échéance) au lieu de la rouvrir.
set local lock_timeout = '5s';
alter table host_lifecycle
    add column if not exists lost_from_drain boolean not null default false;
