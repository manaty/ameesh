-- SPDX-License-Identifier: AGPL-3.0-only
-- 0612_app_alerts — #8344 (issue manaty/ameesh#6584).
--
-- `ameesh notify` ne poussait que les alertes dérivées du mesh. Une
-- application (fiche `Application` du canon, bloc `alerts:`) lève et résout
-- ici ses propres alertes : une ligne par (application, type, clé de
-- déduplication). Une condition durable lève une seule fois ; une levée
-- répétée sur une ligne ouverte ne change rien. `resolve` ferme la ligne
-- une seule fois ; une nouvelle levée après résolution ouvre un nouvel
-- épisode (`episode` + 1, `raised_at` neuf). `episode` est l'identité
-- durable de l'épisode pour le notificateur : deux épisodes levés dans la
-- même seconde restent distincts. La passe `alerts` du notificateur lit les
-- lignes ouvertes comme des alertes `app_alert` : routes, avatar, limite
-- par humain, reprises et registre `notified` sont ceux du mesh.
--
-- Le destinataire est un humain (`recipient`, `human:<id>`) ou une route
-- déclarée par la fiche (`route`), jamais les deux. Aucun secret : le
-- texte est celui que l'application pousse à l'humain.
--
-- Additive et compatible N-1 : une table neuve que l'ancien code ne lit ni
-- n'écrit.

create table if not exists app_alerts (
    app         text not null,
    type        text not null,
    key         text not null,
    recipient   text,
    route       text,
    urgent      boolean not null default false,
    episode     bigint not null default 1,
    text        text not null,
    raised_by   text,
    raised_at   timestamptz not null default clock_timestamp(),
    resolved_at timestamptz,
    resolved_by text,
    primary key (app, type, key),
    constraint app_alerts_one_recipient
        check ((recipient is null) <> (route is null)),
    constraint app_alerts_app_valid check (btrim(app) <> ''),
    constraint app_alerts_type_valid check (btrim(type) <> ''),
    constraint app_alerts_key_valid check (btrim(key) <> '')
);
create index if not exists app_alerts_open_idx
    on app_alerts (raised_at) where resolved_at is null;

comment on table app_alerts is
    '#8344 : alertes levées et résolues par une application (bloc alerts: de sa fiche Application)';
