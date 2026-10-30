# SPDX-License-Identifier: AGPL-3.0-only
"""#4609 : la signature d'un `pg_dump` dans les statistiques de la base du mesh.

PostgreSQL 16 tient `pg_stat_user_tables.last_seq_scan`, l'instant du dernier
parcours séquentiel de chaque table. Un `pg_dump` lit toutes les tables dans
une seule transaction (`REPEATABLE READ`) : les tables qu'il a lues portent le
même instant, à la microseconde près. C'est la preuve, en lecture seule, que
la sauvegarde quotidienne a bien lu la base ; elle ne dit rien du fichier écrit
ni de la copie hors de la VM (#4300).

Lecture seule : aucune écriture, aucune inspection de fichier ni de S3.
"""

#: `last_seq_scan` est apparu avec PostgreSQL 16. Un serveur plus ancien ne
#: peut pas fournir la preuve : l'appelant se tait plutôt que de lever une
#: fausse alerte de sauvegarde manquante.
FIRST_VERSION = 160000


def read(db):
    """Les statistiques de parcours des tables, ou `None` si indisponibles.

    Chaque ligne est `{"relname": str, "last_seq_scan_us": int | None}`, en
    microsecondes depuis l'époque : une seconde flottante ne distingue pas
    deux instants à la microseconde, et le groupement du `pg_dump` en dépend.
    """
    cfg = getattr(db, "cfg", None)
    if getattr(cfg, "backend", "") == "mediated":
        return None
    if not hasattr(db, "query"):
        return None
    # Une passe à blanc de `db.batched` rend des listes vides : ne pas
    # conclure, la vraie passe relira. Une version illisible non plus.
    versions = db.query(
        "SELECT current_setting('server_version_num')::int AS version")
    if not versions or not versions[0].get("version"):
        return None
    if int(versions[0]["version"]) < FIRST_VERSION:
        return None
    return db.query(
        "SELECT relname,"
        " (EXTRACT(EPOCH FROM last_seq_scan) * 1000000)::bigint AS last_seq_scan_us"
        " FROM pg_stat_user_tables")
