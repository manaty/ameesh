-- 0492_work_fingerprint_open — un seul lot ouvert par empreinte d'échec (#3885)
--
-- Canon `processus/lots.md` §16 (owner, 27/10/2026) : une panne serveur est
-- prise en compte par un lot automatique par empreinte nouvelle, les doublons
-- regroupés dans le même lot. L'ouverture est un geste mécanique
-- (`ameesh work fingerprint open`), serialisé par un verrou consultatif de
-- transaction ; cet index partiel est la garde de dernier recours en base :
-- deux écrivains (ou un `work add --source 'empreinte:…'` à la main) ne
-- peuvent pas laisser deux lots OUVERTS pour la même empreinte. Un lot
-- fusionné, promu ou fermé ne compte pas : la même empreinte qui revient
-- ouvre un lot de récidive.
--
-- Additif seulement : aucune ligne existante n'est touchée, et les sources
-- hors de l'espace `empreinte:` ne sont pas contraintes.

create unique index if not exists work_items_fingerprint_open_uniq
    on work_items (lower(source))
    where source like 'empreinte:%'
      and state not in ('merged', 'promoted', 'closed');

-- La passe de veille reprend au dernier curseur lu par source (projet). Une
-- lecture en échec ne l'avance pas : le passage suivant relit la même fenêtre.
create table if not exists fingerprint_watch_state (
    project      text primary key,
    url          text not null default '',
    cursor       text,
    last_read_at timestamptz,
    last_error   text not null default '',
    updated_at   timestamptz not null default now()
);
