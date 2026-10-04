-- 0025_spend_pending — l'état de pause comptable (L13, 0019 §3).
--
-- Le marqueur d'un tour dont la comptabilité n'est pas encore écrite vit en
-- BASE, jamais dans un fichier : une écriture partielle, illisible ou absente
-- ne doit jamais autoriser une reprise. Une ligne présente = pause jusqu'à
-- réparation (écriture de la ligne `turn_costs` puis effacement).
--
-- La ligne porte l'index du flux d'événements au début du tour, le libellé du
-- tour et le modèle effectif du lancement (le modèle annoncé par le flux, s'il
-- existe, est préféré au moment de l'écriture).
create table if not exists spend_pending (
    agent       text primary key,
    start_index integer not null,
    turn        text,
    model       text,
    created_at  timestamptz not null default now()
);
