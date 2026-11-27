-- SPDX-License-Identifier: AGPL-3.0-only
-- 0611_budget_lineage_reservations — #8347 (issue manaty/ameesh#6588).
--
-- Le plafond d'une lignée (racine + éphémères via `created_by`) se lisait sur
-- la seule dépense écrite : deux clones pouvaient être admis sur le même
-- reste avant que l'un des deux ne règle son tour, et dépasser ensemble le
-- plafond. Une ligne par tour admis réserve ici la part du plafond de la
-- racine qui lui est destinée (fenêtres horaire et journalière, en dollars),
-- sous la clé comptable du tour (`spend_key`, unique : le rejeu de la même
-- admission est idempotent). Le règlement du tour — ou son annulation avant
-- lancement — efface la ligne, la dépense écrite prenant le relais ; une
-- réservation sans règlement expire (tour perdu) et n'est plus comptée.
--
-- Additive et compatible N-1 : une table neuve que l'ancien code ne lit ni
-- n'écrit ; aucun verrou de ligne du schéma existant. L'expiration est lue
-- par `clock_timestamp()` au moment de l'admission, comme les réservations de
-- compte (0443).

create table if not exists budget_lineage_reservations (
    spend_key  text primary key,
    root       text not null,
    agent      text not null,
    hour_usd   numeric not null check (hour_usd >= 0),
    day_usd    numeric not null check (day_usd >= 0),
    created_at timestamptz not null default clock_timestamp(),
    expires_at timestamptz not null,
    constraint budget_lineage_reservations_key_valid check (btrim(spend_key) <> ''),
    constraint budget_lineage_reservations_root_valid check (btrim(root) <> ''),
    constraint budget_lineage_reservations_agent_valid check (btrim(agent) <> '')
);

create index if not exists budget_lineage_reservations_root
    on budget_lineage_reservations (root, expires_at);

create index if not exists budget_lineage_reservations_expiry
    on budget_lineage_reservations (expires_at);

comment on table budget_lineage_reservations is
    '#8347 : part du plafond d''une lignée réservée par un tour admis, effacée au règlement ou à l''annulation';
