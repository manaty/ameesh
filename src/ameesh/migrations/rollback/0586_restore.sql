-- SPDX-License-Identifier: AGPL-3.0-only
-- #7631: remove the file-key accord operations column and the accord claims
-- (0586). Without them, every ed25519 receipt is refused for human authority,
-- as before.
set local lock_timeout = '5s';
drop trigger if exists ameesh_agent_spelling on accord_claims;
drop table if exists accord_claims;
alter table authenticators drop column if exists accord_operations;
