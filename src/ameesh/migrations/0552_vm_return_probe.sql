-- SPDX-License-Identifier: AGPL-3.0-only
-- #6017: the VM adapter's wake probe on a closed return.
--
-- A woken VM must prove its health (one probe turn, delivered mail) before
-- the adapter acknowledges `busy`. Left in `draining`, every claim of the
-- host is refused, the probe's included: the cycle could not complete.
-- The operator worker therefore moves the host to `returning` with claims
-- closed, naming in `return_probe` the single identity its health command
-- runs. Only that identity is admitted, only while the return stays closed
-- and no L232b fence is live; `draining` and `lost` stay closed to everyone.
-- No right changes: the executor already writes host_lifecycle and can open
-- the whole host; naming one identity on a closed return is strictly less.
-- An older binary never sets the column: its behaviour is unchanged.
ALTER TABLE host_lifecycle
    ADD COLUMN return_probe text
        CHECK (return_probe ~ '^[A-Za-z0-9][A-Za-z0-9._-]{0,63}$');

CREATE OR REPLACE FUNCTION ameesh_agent_claims_open(wanted_host text, wanted_agent text)
RETURNS boolean LANGUAGE plpgsql VOLATILE AS $$
BEGIN
    IF ameesh_host_claims_open(wanted_host) THEN
        RETURN true;
    END IF;
    -- A live L232b fence still admits only its own probe (0417).
    IF EXISTS (SELECT 1 FROM host_release_fences WHERE host=wanted_host) THEN
        RETURN NOT EXISTS (SELECT 1 FROM host_lifecycle
                           WHERE host=wanted_host AND mode IN ('draining','lost'))
           AND EXISTS (SELECT 1 FROM host_release_fences
                       WHERE host=wanted_host AND probe=wanted_agent);
    END IF;
    RETURN EXISTS (SELECT 1 FROM host_lifecycle
                   WHERE host=wanted_host AND mode='returning' AND NOT claims_open
                     AND return_probe=wanted_agent);
END $$;

-- The probe belongs to the closed return that named it: reopening, a new
-- drain, a loss or a new generation/epoch clears it, whichever binary writes
-- the row (reopen_return, drain, detect_losses, an older executor).
CREATE FUNCTION ameesh_return_probe_clear() RETURNS trigger
LANGUAGE plpgsql AS $$
BEGIN
    IF NEW.return_probe IS NOT NULL
       AND (NEW.mode <> 'returning' OR NEW.claims_open
            OR NEW.generation IS DISTINCT FROM OLD.generation
            OR NEW.release_epoch IS DISTINCT FROM OLD.release_epoch) THEN
        NEW.return_probe := NULL;
    END IF;
    RETURN NEW;
END $$;
-- ameesh: trigger compatible with previous code (it only clears a column
-- the previous code never sets).
CREATE TRIGGER host_lifecycle_return_probe_clear
    BEFORE UPDATE ON host_lifecycle
    FOR EACH ROW EXECUTE FUNCTION ameesh_return_probe_clear();
