-- SPDX-License-Identifier: AGPL-3.0-only
-- 0554_external_executors — #6091 (manaty/ameesh#4642, écart 4) : un
-- exécutant externe non-agent (un système, par exemple un serveur externe) se
-- déclare avec son responsable humain.
--
-- #3447 nomme l'exécutant externe d'un lot dans `work_items.external_executor`
-- et n'acceptait qu'un agent du registre `mode=externe` avec responsable, ou
-- un humain `human:<id>`. Un système non-agent n'a pas sa place dans
-- `agent_registry` : il ne se réveille pas, ne s'assigne pas, ne reçoit pas
-- de courrier, et le confondre avec un agent réveillable serait exactement le
-- défaut que ce lot corrige. Sa déclaration vit donc dans sa propre table :
-- un nom qualifié `system:<nom>` (le préfixe ne peut pas collisionner avec un
-- nom d'agent, `NAME_RE` interdit le deux-points), le responsable humain, et
-- la trace du geste.
--
-- Additif seulement : nouvelle table, aucune ligne existante ne change de
-- sens. Les lots déjà réservés à un agent `externe` ou à un humain restent
-- lisibles tels quels ; `external_executor` n'est pas retouché.
SET LOCAL lock_timeout = '5s';

CREATE TABLE external_executors (
    name         text PRIMARY KEY,
    responsible  text NOT NULL,
    note         text NOT NULL DEFAULT '',
    declared_by  text NOT NULL DEFAULT '',
    declared_at  timestamptz NOT NULL DEFAULT now(),
    retired_at   timestamptz,
    retired_by   text,
    -- Le préfixe `system:` distingue le système d'un agent ou d'un humain ;
    -- le suffixe suit la forme d'un nom d'agent (config.NAME_RE) et ne peut
    -- donc pas se confondre avec une identité existante.
    CONSTRAINT external_executors_system_name
        CHECK (name ~ '^system:[A-Za-z0-9][A-Za-z0-9._-]{0,63}$'),
    CONSTRAINT external_executors_responsible
        CHECK (responsible ~ '^human:[A-Za-z0-9][A-Za-z0-9._@+-]{0,127}$'),
    CONSTRAINT external_executors_retirement
        CHECK ((retired_at IS NULL) = (retired_by IS NULL))
);

COMMENT ON TABLE external_executors IS
    '#6091: declared non-agent external executors (systems such as an external server) with their human responsible; never an agent, never woken, never assignable.';
COMMENT ON COLUMN external_executors.name IS
    '#6091: qualified `system:<name>`; the value carried by work_items.external_executor.';
COMMENT ON COLUMN external_executors.responsible IS
    '#6091: human:<id> accountable for the system; shown by `work external-executor --list` and by `work show` on a reserved lot.';
COMMENT ON COLUMN external_executors.retired_at IS
    '#6091: retired declaration: no new reservation, existing lots keep the name until cleared or closed.';

-- #4299 : `declared_by` et `retired_by` nomment l'autorité du geste de pilote
-- (un agent, ou `human:<id>`) ; un nom d'agent suit le renommage, à l'écriture
-- comme au renommage, comme les autres colonnes d'identité. `name` reste le
-- nom du système et ne bouge jamais (classé hors identité dans test_4299).
CREATE TRIGGER ameesh_agent_spelling
    BEFORE INSERT OR UPDATE OF declared_by, retired_by ON external_executors
    FOR EACH ROW EXECUTE FUNCTION ameesh_agent_spelling('declared_by retired_by');
