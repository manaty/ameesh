-- SPDX-License-Identifier: AGPL-3.0-only
-- #501: a wait episode survives updates of its diagnostic and reason.
ALTER TABLE agent_registry ADD COLUMN lease_wait_since timestamptz;

-- Existing waits have no better recorded start than their last update.
UPDATE agent_registry SET lease_wait_since = updated_at
 WHERE starts_with(coalesce(last_error, ''), 'lease_not_taken: ');

CREATE FUNCTION ameesh_lease_wait_since() RETURNS trigger LANGUAGE plpgsql AS $$
BEGIN
    IF starts_with(coalesce(NEW.last_error, ''), 'lease_not_taken: ') THEN
        IF TG_OP = 'UPDATE' AND starts_with(coalesce(OLD.last_error, ''), 'lease_not_taken: ') THEN
            NEW.lease_wait_since := coalesce(OLD.lease_wait_since, clock_timestamp());
        ELSE
            NEW.lease_wait_since := clock_timestamp();
        END IF;
    ELSE
        NEW.lease_wait_since := NULL;
    END IF;
    RETURN NEW;
END;
$$;
CREATE TRIGGER agent_registry_lease_wait_since
    BEFORE INSERT OR UPDATE ON agent_registry
    FOR EACH ROW EXECUTE FUNCTION ameesh_lease_wait_since();

