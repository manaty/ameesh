-- SPDX-License-Identifier: AGPL-3.0-only
-- 0564_review_finding_resolutions — #6669 (étude #6650, unité M3) : la
-- résolution explicite d'un constat de revue et son lien de régression.
--
-- Un « constat » est un jalon `verdict` `blocked` : c'est la seule trace
-- structurée d'un B, et son `id` l'identifie (plusieurs constats peuvent
-- porter sur le même gel, chacun a son jalon). Le `sha` du jalon est le
-- témoin de la tête réellement lue ; la résolution le recopie et le garde,
-- même si la branche avance ensuite.
--
-- La résolution est un journal **additif** : chaque geste ajoute une ligne,
-- l'état effectif est la ligne la plus récente pour ce constat. Une
-- correction de jugement s'écrit par une nouvelle ligne, jamais par une
-- réécriture ; le déclencheur refuse tout UPDATE. Trois états seulement :
-- confirmé, infirmé, non tranché. L'absence de ligne vaut non tranché : un B
-- suivi d'un OK sans correction ne devient jamais confirmé ni infirmé par
-- déduction.
--
-- L'adjudicateur est indépendant des familles jugées (auteur(s) prouvés au
-- gel, relecteur, correcteur éventuel). La contrainte `independent` interdit
-- de compter un confirmé/infirmé non indépendant ; l'écrivain refuse déjà le
-- geste, la base garde la même règle. Aucun compte d'avis, aucune famille et
-- aucune politique ne sont modifiés : ce journal est une observation, pas une
-- nouvelle porte de fusion.
--
-- 0564 est réservé au lot #6669 (`ameesh work migration`). Additif : une
-- table neuve seulement, aucune ligne ni contrainte existante réécrite (L151).
-- L'écrivain N-1 n'écrit pas cette table ; le lecteur du rapport affiche alors
-- les constats « non tranchés ». Le lien de régression vit dans
-- `regression_lot` : le genre `work_item_links.regressed_by` n'est pas ouvert
-- ici, parce qu'un lecteur N-1 de `work_dependencies.links()` lève
-- `KeyError` (`_INVERSE[kind]`) et que `packet_candidates` en fait un refus de
-- composition (B1 auditeur #209049, hôtes N-1 et retour arrière). Le genre
-- sera ouvert quand plus aucun lecteur N-1 ne tourne, ou derrière un plancher
-- de version.

SET LOCAL lock_timeout = '5s';

CREATE TABLE IF NOT EXISTS review_finding_resolutions (
    id                bigserial PRIMARY KEY,
    -- le constat : un jalon verdict blocked, identifié par son id
    milestone_id      bigint NOT NULL REFERENCES work_item_milestones(id) ON DELETE CASCADE,
    work_item_id      bigint NOT NULL REFERENCES work_items(id) ON DELETE CASCADE,
    -- le SHA de la tête lue, recopié du jalon et vérifié par le déclencheur
    witness_sha       text NOT NULL
                      CHECK (witness_sha ~ '^[0-9a-f]{7,64}$'),
    state             text NOT NULL
                      CHECK (state IN ('confirmed', 'refuted', 'undecided')),
    basis             text NOT NULL
                      CHECK (basis IN ('correction', 'regression', 'withdrawal',
                                       'evidence', 'insufficient')),
    correction_sha    text
                      CHECK (correction_sha IS NULL
                             OR correction_sha ~ '^[0-9a-f]{7,64}$'),
    -- #6669 N1 : pas de SET NULL — la preuve de régression ne se perd pas
    -- en silence, et l'append-only refuserait l'UPDATE du SET NULL
    regression_lot    bigint REFERENCES work_items(id),
    corrector         text NOT NULL DEFAULT '',
    withdrawn_by      text NOT NULL DEFAULT '',
    adjudicator       text NOT NULL CHECK (btrim(adjudicator) <> ''),
    adjudicator_family text NOT NULL DEFAULT '',
    judged_families   text[] NOT NULL DEFAULT '{}',
    independent       boolean NOT NULL DEFAULT false,
    -- projection de `review_correction.check` quand le relecteur corrige
    -- lui-même : observation seulement, aucune porte ne la lit
    review_room       jsonb
                      CHECK (review_room IS NULL
                             OR jsonb_typeof(review_room) = 'object'),
    note              text NOT NULL DEFAULT '',
    created_at        timestamptz NOT NULL DEFAULT clock_timestamp(),
    -- forme de chaque état : aucun lien contradictoire n'est stocké
    CONSTRAINT review_finding_resolutions_shape_ck CHECK (
        (state = 'confirmed'
         AND basis IN ('correction', 'regression', 'evidence'))
        OR (state = 'refuted'
            AND basis IN ('withdrawal', 'evidence'))
        OR (state = 'undecided'
            AND basis = 'insufficient')),
    CONSTRAINT review_finding_resolutions_basis_ck CHECK (
        (basis <> 'correction' OR correction_sha IS NOT NULL)
        AND (basis <> 'regression' OR regression_lot IS NOT NULL)
        AND (basis <> 'withdrawal' OR btrim(withdrawn_by) <> '')),
    CONSTRAINT review_finding_resolutions_regression_ck
        CHECK (regression_lot IS NULL OR regression_lot <> work_item_id),
    -- une confirmation ou une infirmation sans adjudication indépendante ne
    -- vaut pas une résolution : elle reste non tranchée
    CONSTRAINT review_finding_resolutions_independent_ck
        CHECK (state = 'undecided' OR independent)
);

CREATE INDEX IF NOT EXISTS review_finding_resolutions_item_idx
    ON review_finding_resolutions (work_item_id, milestone_id, id DESC);
CREATE INDEX IF NOT EXISTS review_finding_resolutions_regression_idx
    ON review_finding_resolutions (regression_lot)
    WHERE regression_lot IS NOT NULL;

-- Le constat doit appartenir au lot, être un verdict bloqué, et le témoin
-- doit être exactement le SHA lu par ce verdict (texte, sans équivalence de
-- préfixe : le jalon fait foi). Une résolution ne se réécrit pas.
CREATE OR REPLACE FUNCTION review_finding_resolution_guard() RETURNS trigger
LANGUAGE plpgsql AS $$
DECLARE
    verdict_row work_item_milestones%ROWTYPE;
BEGIN
    IF TG_OP = 'UPDATE' THEN
        RAISE EXCEPTION
            '#6669: review_finding_resolutions is append-only; write a new row';
    END IF;
    SELECT * INTO verdict_row FROM work_item_milestones
     WHERE id = NEW.milestone_id;
    IF NOT FOUND
       OR verdict_row.work_item_id <> NEW.work_item_id
       OR verdict_row.kind <> 'verdict'
       OR verdict_row.verdict <> 'blocked' THEN
        RAISE EXCEPTION
            '#6669: milestone % is not a blocked verdict of lot %',
            NEW.milestone_id, NEW.work_item_id;
    END IF;
    IF lower(btrim(coalesce(verdict_row.sha, '')))
       <> lower(btrim(NEW.witness_sha)) THEN
        RAISE EXCEPTION
            '#6669: witness % is not the read SHA % of milestone %',
            NEW.witness_sha, verdict_row.sha, NEW.milestone_id;
    END IF;
    RETURN NEW;
END $$;

DROP TRIGGER IF EXISTS review_finding_resolutions_guard
    ON review_finding_resolutions;
CREATE TRIGGER review_finding_resolutions_guard
    BEFORE INSERT OR UPDATE ON review_finding_resolutions
    FOR EACH ROW EXECUTE FUNCTION review_finding_resolution_guard();

COMMENT ON TABLE review_finding_resolutions IS
    '#6669 (M3) : journal additif des résolutions explicites d''un constat de revue (jalon verdict blocked). État effectif = dernière ligne ; absence = non tranché. Aucune porte de fusion ne le lit.';
COMMENT ON COLUMN review_finding_resolutions.witness_sha IS
    '#6669 : la tête réellement lue par le verdict (le sha du jalon, exact) ; la résolution reste liée à ce texte même si la branche avance.';
COMMENT ON COLUMN review_finding_resolutions.review_room IS
    '#6669 : projection review_correction.check quand le relecteur corrige lui-même ; observation, jamais un refus ni un compte d''avis.';
COMMENT ON COLUMN review_finding_resolutions.judged_families IS
    '#6669 : familles prises en compte pour l''indépendance de l''adjudicateur (auteurs prouvés au gel, relecteur, correcteur).';
