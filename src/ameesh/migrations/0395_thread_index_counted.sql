-- SPDX-License-Identifier: AGPL-3.0-only
-- 0395_thread_index_counted — jusqu'où l'index a recompté un fil (L361, lot #361).
--
-- Une expiration avec base recompte `entries` : toutes les entrées gardées,
-- indexées ou pas encore. `counted_until` note la fin logique du fil à ce
-- recomptage, dans la même transaction : une entrée d'avant cette position,
-- indexée plus tard, est déjà comptée. NULL : jamais recompté (une expiration
-- sans base, ou dont le recomptage a échoué, ne compte rien). Le fichier n'en
-- est pas la preuve.

alter table thread_index add column if not exists counted_until bigint;
