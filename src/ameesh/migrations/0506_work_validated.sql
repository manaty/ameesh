-- L99 T3 (#4351): distinct review quorum state, without rewriting old rows.
ALTER TABLE work_items DROP CONSTRAINT IF EXISTS work_items_state_check;
ALTER TABLE work_items ADD CONSTRAINT work_items_state_check
    CHECK (state IN ('intake', 'build', 'qa', 'validated', 'merged', 'promoted',
                     'blocked', 'waiting_human', 'closed'));
