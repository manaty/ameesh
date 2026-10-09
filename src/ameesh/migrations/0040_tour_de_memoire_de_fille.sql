-- SPDX-License-Identifier: AGPL-3.0-only
-- 0040_tour_de_memoire_de_fille — avant de s'éteindre en fin de lot, une
-- session parallèle (fille, L52b) qui a un dépôt de mémoire fait un tour de
-- mémoire (L54) : `closing_requested_at` dit quand il a été demandé.

alter table agent_registry add column if not exists closing_requested_at timestamptz null;
