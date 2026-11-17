-- SPDX-License-Identifier: AGPL-3.0-only
-- #7631: remove the file-key accord operations column (0586).
-- Without it, every ed25519 receipt is refused for human authority, as before.
set local lock_timeout = '5s';
alter table authenticators drop column if exists accord_operations;
