-- SPDX-License-Identifier: AGPL-3.0-only
-- 0475_host_lifecycle_reconcile — #3035 (L232e): name a lost host `lost` in
-- its gate state instead of the generic `draining`.
--
-- The claim closure is unchanged (`ameesh_host_claims_open` still closes on
-- `draining` and `lost`, and on a release fence from 0417). Only the
-- diagnostic string changes: an operator reading `ameesh hosts` must see the
-- actual bound instead of guessing whether the host was drained or lost.
CREATE OR REPLACE FUNCTION ameesh_host_gate_state(wanted_host text, observed text)
RETURNS text
LANGUAGE plpgsql VOLATILE AS $$
BEGIN
    IF EXISTS (SELECT 1 FROM host_lifecycle
               WHERE host=wanted_host AND mode='lost') THEN
        RETURN 'lost';
    END IF;
    IF NOT ameesh_host_claims_open(wanted_host) THEN
        RETURN 'draining';
    END IF;
    RETURN observed;
END $$;
