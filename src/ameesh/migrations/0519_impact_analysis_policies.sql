-- SPDX-License-Identifier: AGPL-3.0-only
-- 0519_impact_analysis_policies — #4818 (manaty/ameesh#3643) : analyse
-- d'impact avant dispatch et politique d'empreinte par dépôt.
--
-- `repository_footprint_policies` : la politique de la garde d'empreinte
-- (#4173) d'un dépôt (`app`), posée par un geste de pilote (`ameesh work
-- footprint-policy`) et lue par le SQL de la garde même, sous son verrou :
--   * `unknown_policy` : `repository` — un lot dont l'analyse d'impact n'a
--     trouvé aucun chemin porte le marqueur `.` (dépôt entier), qui chevauche
--     tout ; `open` (défaut, sans ligne) — il n'occupe rien (règle d'avant) ;
--   * `max_in_flight` : plafond de lots occupants par dépôt (NULL : aucun) ;
--   * `shared_paths` : fichiers ou dossiers à fusion triviale (CHANGELOG,
--     index de locales…), ignorés pour le chevauchement.
--
-- `work_item_impact_analyses` : la dernière analyse d'impact d'un lot (une
-- ligne par lot, réécrite à chaque essai), avec la confiance par chemin et le
-- budget consommé ; le journal du lot porte une ligne par estimation.
--
-- Additif : deux tables neuves, aucune ligne existante modifiée. Le marqueur
-- `.` respecte déjà la contrainte de `work_item_footprints.path` (0502).
CREATE TABLE IF NOT EXISTS repository_footprint_policies (
    app text PRIMARY KEY CHECK (btrim(app) <> ''),
    unknown_policy text NOT NULL DEFAULT 'open'
        CHECK (unknown_policy IN ('repository', 'open')),
    max_in_flight integer CHECK (max_in_flight IS NULL OR max_in_flight > 0),
    shared_paths text[] NOT NULL DEFAULT '{}',
    updated_by text NOT NULL,
    updated_at timestamptz NOT NULL DEFAULT clock_timestamp()
);

CREATE TABLE IF NOT EXISTS work_item_impact_analyses (
    work_item_id bigint PRIMARY KEY REFERENCES work_items(id) ON DELETE CASCADE,
    status text NOT NULL CHECK (status IN ('known', 'unknown')),
    reason text,
    paths jsonb NOT NULL DEFAULT '[]'::jsonb,
    weak jsonb NOT NULL DEFAULT '[]'::jsonb,
    policy text,
    elapsed_ms integer,
    git_calls integer,
    attempts integer NOT NULL DEFAULT 1,
    analyzed_by text NOT NULL,
    analyzed_at timestamptz NOT NULL DEFAULT clock_timestamp()
);

CREATE INDEX IF NOT EXISTS work_item_impact_analyses_unknown
    ON work_item_impact_analyses (analyzed_at) WHERE status = 'unknown';

-- #4299 : leurs auteurs suivent un renommage d'agent, à l'écriture aussi.
DROP TRIGGER IF EXISTS ameesh_agent_spelling ON repository_footprint_policies;
CREATE TRIGGER ameesh_agent_spelling
    BEFORE INSERT OR UPDATE OF updated_by ON repository_footprint_policies
    FOR EACH ROW EXECUTE FUNCTION ameesh_agent_spelling('updated_by');

DROP TRIGGER IF EXISTS ameesh_agent_spelling ON work_item_impact_analyses;
CREATE TRIGGER ameesh_agent_spelling
    BEFORE INSERT OR UPDATE OF analyzed_by ON work_item_impact_analyses
    FOR EACH ROW EXECUTE FUNCTION ameesh_agent_spelling('analyzed_by');
