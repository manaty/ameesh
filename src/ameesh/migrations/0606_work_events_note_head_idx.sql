-- SPDX-License-Identifier: AGPL-3.0-only
-- 0606_work_events_note_head_idx — #8051 : la lecture groupée des notes
-- hold/levée sonde le seul petit ensemble des notes candidates.
--
-- Constat (#8045, pg_stat_statements du central, 22/11) : `lot_hold_events`
-- pèse 10,7 % du temps SQL. Sa seconde requête lisait, pour chaque page de
-- 1 000 lots, toutes les notes de leur journal (21 638 lignes dans la mesure
-- du 22/11) puis appliquait les motifs ARE `^hold:` / `^unhold…:` à chacune :
-- les notes ordinaires représentaient ~20 800 détoastages et regex pour 797
-- lignes retenues. Les motifs sont ancrés : seul le premier mot peut décider.
--
-- L'index porte l'expression EXACTE du préfiltre (`progress.NOTE_HEAD_SQL`) :
-- `lower(split_part(btrim(note, <blancs de HOLD_NOTE_WHITESPACE>), ' ', 1))`,
-- collation `text_pattern_ops` pour les préfixes `LIKE`. Une tête
-- (`HoldPatterns.heads`) ne décide jamais : le motif exact reste le seul
-- juge ; sans tête, la requête ne pose aucun préfiltre et l'index n'est pas
-- requis. Une modification future de `HOLD_NOTE_WHITESPACE` ferait cesser
-- l'usage de l'index (repli exact, jamais un faux négatif).
--
-- Additif (L151) : un index, aucune colonne ni ligne modifiée. Construction
-- simple, pas CONCURRENTLY (`migrate()` tourne dans une transaction, comme
-- 0494, 0538 et 0603) : le journal des lots est écrit à chaque transition,
-- d'où une attente de verrou bornée à 5 s (échec et reprise, jamais une
-- écriture qui patiente derrière la construction).
set local lock_timeout = '5s';

create index if not exists work_item_events_item_note_head_idx
  on work_item_events (
    work_item_id,
    lower(split_part(btrim(note, E'\x09\x0a\x0b\x0c\x0d\x1c\x1d\x1e\x1f \u0085\u00a0\u1680\u2000\u2001\u2002\u2003\u2004\u2005\u2006\u2007\u2008\u2009\u200a\u2028\u2029\u202f\u205f\u3000'), ' ', 1)) text_pattern_ops
  );

comment on index work_item_events_item_note_head_idx is
  '#8051: lot_hold_events probes the first note word (hold/unhold, prod-gate, packet-exception) by (work_item_id, head) instead of reading and regexing every note of the requested lots; the exact anchored pattern still decides.';
