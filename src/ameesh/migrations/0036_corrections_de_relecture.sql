-- SPDX-License-Identifier: AGPL-3.0-only
-- 0036_corrections_de_relecture — corrections issues de la relecture
-- indépendante des lots L36–L45 (lot L46).
--
-- 1. `actions.canon` immuable après l'insertion. Le déclencheur de 0035
--    (`ameesh_action_canon`) le fixe à l'INSERT ; rien n'empêchait ensuite un
--    UPDATE de le changer — or il décide quels authentificateurs valent pour
--    les reçus de l'action et quels grants la couvrent. Seul le rebase du
--    canon par défaut (`canon.rebase_default`, L42/L44) le change : il pose
--    dans SA transaction `SET LOCAL ameesh.rebase_default = 'on'`, que ce
--    déclencheur exige. Tout autre UPDATE de `canon` est refusé.
--
-- 2. `session_bindings.pid_start` (L41) : voir plus bas.

create or replace function ameesh_action_canon_guard() returns trigger
language plpgsql as $$
begin
    if new.canon is distinct from old.canon
       and coalesce(current_setting('ameesh.rebase_default', true), '') <> 'on' then
        raise exception 'ameesh_actions : canon d''action immuable (%), sauf rebase du '
                        'canon par défaut', old.action_id;
    end if;
    return new;
end $$;

drop trigger if exists actions_canon_guard on actions;
create trigger actions_canon_guard
    before update on actions
    for each row execute function ameesh_action_canon_guard();

-- 2. `session_bindings.pid_start` : l'heure de démarrage du processus lié
--    (champ 22 de /proc/<pid>/stat, en tops d'horloge depuis le démarrage de
--    l'hôte), relevée par `ameesh mail bind --pid`. Le hook ne reconnaît le
--    PID ancêtre que si son heure de démarrage est la même : un PID réutilisé
--    par un autre processus (harnais fermé, PID recyclé) ne donne rien. NULL :
--    liaison antérieure, ou processus introuvable à la liaison (contrôle du
--    seul PID, comme avant).
alter table session_bindings add column if not exists pid_start bigint;

comment on column session_bindings.pid_start is
  'L46 (0036) : heure de démarrage du PID lié (champ 22 de /proc/<pid>/stat) ; NULL = non contrôlée.';
