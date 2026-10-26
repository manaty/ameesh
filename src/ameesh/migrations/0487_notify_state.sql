-- SPDX-License-Identifier: AGPL-3.0-only
-- 0487_notify_state — #3576 : l'état de livraison de `ameesh notify` devient
-- partagé par le maillage.
--
-- La passe `alerts` suit le bail élu (#3383) et change donc d'hôte ; son état
-- de livraison vivait dans `<état>/notify/state.json`, propre à l'hôte. Au
-- premier passage du nouveau détenteur, chaque alerte encore active était
-- poussée une seconde fois (décision #138918 poussée à 12:41Z depuis
-- agents-2, de nouveau à 13:54Z depuis agents-1) et les résolutions de
-- l'ancien hôte étaient perdues ou annoncées en double.
--
-- La ligne `alerts` porte le document de livraison (mêmes clés que
-- `ameesh-notify-state/1` : active, resolved, rate, summary, last_summary,
-- briefs, notified) et le bail d'exécution de la passe qui l'écrit :
-- `owner`/`epoch`/`expires_at`, comme `merge_locks` (0058). Un seul hôte
-- livre à la fois ; toute écriture est fencée par l'owner et l'epoch du bail
-- vivant, donc un détenteur périmé ne peut pas écraser l'état du nouveau.
-- Le fichier local reste un miroir de secours (base injoignable, #532) : il
-- n'est plus la source de vérité entre deux hôtes.
--
-- Additive seulement : une table neuve, aucune donnée existante n'est lue ni
-- modifiée. Le premier passage qui prend le bail sème le document local.
set local lock_timeout = '5s';

create table if not exists notify_state (
    scope      text        not null primary key,
    -- état vide = aucune amorce : le premier détenteur y verse son état local.
    body       jsonb       not null default '{}'::jsonb
                           check (jsonb_typeof(body) = 'object'),
    -- bail d'exécution de la passe alerts (NULL : libre)
    owner      text,
    epoch      bigint      not null default 0,
    expires_at timestamptz,
    updated_at timestamptz not null default clock_timestamp()
);

comment on table notify_state is
    '#3576: shared delivery state of the alerts pass, with its execution lease (owner/epoch/expires_at), so a host change neither re-pushes nor loses a resolution.';
comment on column notify_state.scope is
    '#3576: `alerts` for the mesh delivery state.';
comment on column notify_state.body is
    '#3576: the `ameesh-notify-state/1` document; `{}` = not seeded yet.';
