# SPDX-License-Identifier: AGPL-3.0-only
"""Migrations versionnées : application, idempotence, immuabilité."""
from __future__ import annotations

import dataclasses
import os
import tempfile
import time
import unittest
import uuid

from ameesh import authority, db as db_mod, mail, migrations, registry, signing

from .support import PgTestCase


def marque_temoin(genre: str, schema: str) -> str:
    """Marque `application_name` d'un témoin, unique à l'invocation (#835 R2).

    Le schéma porte déjà l'unicité de l'exécution (uuid4 par test). Deux
    exécutions de la suite peuvent partager le même cluster (AMEESH_TEST_DSN
    commun sur un hôte d'agents) : une marque constante rendrait les témoins
    complices — chacun verrait l'attente de l'autre et passerait vert sans rien
    prouver (relevé par cx2 et l'auditeur).
    """
    return "l835-%s-%s" % (genre, schema)


def attente_vue(observateur, marque: str, evenement: str, timeout: float = 30.0) -> bool:
    """Le backend `marque` est-il *vu* par le serveur en attente d'un verrou ?

    `evenement` est le `wait_event` de `pg_stat_activity` : `relation` pour un
    verrou de table, `advisory` pour le verrou consultatif des migrations. Le
    `application_name` porte la marque de CETTE invocation (`marque_temoin` :
    préfixe + schéma, lui-même unique) : jamais un `migrate` d'un autre test ni
    d'une autre exécution de la suite sur le même cluster (R2 cx2/auditeur).
    Cette preuve remplace le sleep : un témoin qui n'a pas encore atteint son
    SQL ne peut plus passer vert à tort (L835 R1).

    La borne est large (30 s, N1 de #920) : sous un runner saturé (#824), le
    fil de migration peut mettre plus de 5 s à atteindre son SQL et l'assertion
    échouerait à tort. En cas vert, la boucle rend la main dès l'attente vue :
    la borne ne coûte rien.
    """
    requete = (
        "SELECT count(*)::int AS n FROM pg_stat_activity "
        "WHERE application_name = %s AND wait_event_type = 'Lock' AND wait_event = %s"
    )
    fin = time.monotonic() + timeout
    while time.monotonic() < fin:
        if observateur.query(requete, (marque, evenement))[0]["n"]:
            return True
        time.sleep(0.02)
    return False


class MigrationsTest(PgTestCase):
    def connect_marque(self, schema: str, marque: str):
        """Une connexion dont le backend est identifiable dans pg_stat_activity."""
        dsn = self.cfg.dsn
        if "://" in dsn:                                  # URI libpq
            dsn = "%s%sapplication_name=%s" % (dsn, "&" if "?" in dsn else "?", marque)
        elif "=" in dsn:                                  # paires clé/valeur
            dsn = "%s application_name=%s" % (dsn, marque)
        else:                                             # nom de base nu
            dsn = "dbname=%s application_name=%s" % (dsn, marque)
        return db_mod.connect(dataclasses.replace(self.cfg, schema=schema, dsn=dsn))

    def test_schema_migre_versionne(self):
        rows = self.db.query(
            "SELECT version, name, checksum FROM schema_migrations ORDER BY version"
        )
        # toutes les migrations livrées sont appliquées, dans l'ordre (les
        # numéros réservés aux lots non encore fusionnés peuvent manquer)
        self.assertEqual([row["version"] for row in rows],
                         [m.version for m in migrations.discover()])
        names = {int(row["version"]): row["name"] for row in rows}
        expected = {1: "init", 2: "overview", 3: "authority", 4: "work_items", 5: "key_roles",
                    6: "authority_fixes", 7: "nonces_backfill", 8: "canon", 9: "threads",
                    11: "receipts", 12: "events", 14: "cost", 22: "placement",
                    23: "authenticator_syncs", 24: "canon_state_authenticators"}
        self.assertEqual({v: names.get(v) for v in expected}, expected)
        # 0024 : la partie authentificateurs de l'état du canon (revue L9b, codex2)
        colonnes = {row["column_name"] for row in self.db.query(
            "SELECT column_name FROM information_schema.columns "
            "WHERE table_schema = current_schema() AND table_name = 'canon_state'")}
        self.assertTrue({"auth_status", "auth_diagnostic", "auth_checked_at"} <= colonnes)
        for row in rows:
            self.assertEqual(len(row["checksum"]), 64)
        # la vue d'observabilité et les deux triggers de notification existent
        for relation in ("agent_mesh_overview", "mesh_approvals_status"):
            with self.subTest(relation=relation):
                found = self.db.query(
                    "SELECT to_regclass(%s) IS NOT NULL AS ok", (relation,))[0]
                self.assertTrue(found["ok"])
        # les colonnes d'autorité et de signature sont bien là
        colonnes = {row["column_name"] for row in self.db.query(
            "SELECT column_name FROM information_schema.columns "
            "WHERE table_schema = current_schema() AND table_name = 'agent_registry'")}
        self.assertTrue({"public_key", "public_key_fingerprint", "key_revoked_at",
                         "key_role", "current_prompt"} <= colonnes)
        colonnes = {row["column_name"] for row in self.db.query(
            "SELECT column_name FROM information_schema.columns "
            "WHERE table_schema = current_schema() AND table_name = 'agent_mailbox'")}
        self.assertTrue({"nonce", "signed_payload", "signature_expires_at"} <= colonnes)
        triggers = self.db.query(
            "SELECT t.tgname FROM pg_trigger t "
            "JOIN pg_class c ON c.oid = t.tgrelid "
            "JOIN pg_namespace n ON n.oid = c.relnamespace "
            "WHERE NOT t.tgisinternal AND n.nspname = current_schema() "
            "AND t.tgname IN ('agent_mailbox_notify','agent_registry_notify')"
        )
        self.assertEqual(len(triggers), 2)

    def test_remigration_idempotente(self):
        self.assertEqual(migrations.migrate(self.db), [])
        count = self.db.query("SELECT count(*)::int AS n FROM schema_migrations")[0]["n"]
        self.assertEqual(count, len(migrations.discover()))

    def test_migration_modifiee_refusee(self):
        connue = migrations.discover()[0].checksum
        self.db.execute("UPDATE schema_migrations SET checksum = 'deadbeef' WHERE version = 1")
        try:
            with self.assertRaises(db_mod.DbError) as ctx:
                migrations.pending(self.db)
            self.assertIn("immutable", str(ctx.exception))
        finally:
            self.db.execute(
                "UPDATE schema_migrations SET checksum = %s WHERE version = 1", (connue,)
            )

    def test_migrate_essai_puis_additive_seulement(self):
        """L151 : `migrate --dry-run` n'écrit rien ; `--additive-only` applique
        une migration additive manquante (la dernière, retirée du registre)."""
        # 0051 : un ajout de colonne « if not exists », réapplicable sans effet
        last = next(m for m in migrations.discover() if m.version == 51)
        self.assertEqual(migrations.non_additive(last), [])
        self.db.execute("DELETE FROM schema_migrations WHERE version = %s", (last.version,))
        try:
            out = self.cli("migrate", "--dry-run")
            self.assertEqual(out.returncode, 0, out.stderr)
            self.assertIn("%s: additive" % last.label, out.stdout)
            self.assertNotIn(last.version, migrations.applied(self.db))
            out = self.mesh("migrate", "--additive-only")
            self.assertEqual(out.returncode, 0, out.stderr)
            self.assertIn("%s applied" % last.label, out.stdout)
        finally:
            migrations.migrate(self.db)
        self.assertIn(last.version, migrations.applied(self.db))

    def test_doctor_n_ecrit_rien(self):
        """L146 : sur un schéma sans `schema_migrations`, `status()` (doctor)
        lit sans créer la table."""
        schema = "t_lecture_%s" % uuid.uuid4().hex[:8]
        self.db.execute('CREATE SCHEMA "%s"' % schema)
        db = self.connect(schema)
        try:
            known, done = migrations.status(db)
            self.assertEqual(done, set())
            self.assertTrue(known)
            self.assertFalse(db.query(
                "SELECT to_regclass('schema_migrations') IS NOT NULL AS ok")[0]["ok"])
        finally:
            db.close()
            self.db.execute('DROP SCHEMA IF EXISTS "%s" CASCADE' % schema)

    def test_version_en_double_refusee(self):
        with tempfile.TemporaryDirectory() as directory:
            for filename in ("0007_a.sql", "0007_b.sql"):
                with open(os.path.join(directory, filename), "w", encoding="utf-8") as fh:
                    fh.write("select 1;\n")
            with self.assertRaises(db_mod.DbError) as ctx:
                migrations.discover(directory)
            self.assertIn("duplicate", str(ctx.exception))

    def test_schema_neuf_cree_par_migrate(self):
        """Un schéma de test n'existe pas encore : migrate le crée et l'isole."""
        schema = "t_neuf_%s" % uuid.uuid4().hex[:8]
        db = self.connect(schema)
        try:
            # `SET search_path` accepte un schéma absent : on vérifie que
            # migrate le crée au lieu d'échouer sur « no schema has been selected ».
            self.assertFalse(self.db.query(
                "SELECT EXISTS (SELECT 1 FROM pg_namespace WHERE nspname = %s) AS ok", (schema,)
            )[0]["ok"])
            done = migrations.migrate(db)
            self.assertEqual([m.version for m in done],
                             [m.version for m in migrations.discover()])
            tables = db.query(
                "SELECT count(*)::int AS n FROM information_schema.tables "
                "WHERE table_schema = %s AND table_name IN ('agent_registry','agent_mailbox',"
                "'schema_migrations','mesh_approvals','work_items','work_item_events',"
                "'work_item_milestones','thread_index')",
                (schema,),
            )[0]["n"]
            self.assertEqual(tables, 8)
        finally:
            db.close()
            self.db.execute('DROP SCHEMA IF EXISTS "%s" CASCADE' % schema)

    def test_mise_a_niveau_remplit_le_ledger_des_consommations(self):
        """Sonde codex3 (B3 upgrade) : un reçu consommé avant 0006 ne se rejoue pas.

        On rejoue le chemin réel : schéma 0001–0004, approbation signée déjà
        consommée (colonne seule), puis application de 0005–0007, suppression et
        réinsertion du même reçu signé — find_approval doit refuser.
        """
        schema = "t_upgrade_%s" % uuid.uuid4().hex[:8]
        db = self.connect(schema)
        try:
            # 1) un schéma à l'ancienne : uniquement 0001–0004
            import tempfile
            with tempfile.TemporaryDirectory() as ancien:
                for name in sorted(os.listdir(migrations.MIGRATIONS_DIR)):
                    if name[:4] in ("0001", "0002", "0003", "0004"):
                        with open(os.path.join(migrations.MIGRATIONS_DIR, name), encoding="utf-8") as src:
                            with open(os.path.join(ancien, name), "w", encoding="utf-8") as dst:
                                dst.write(src.read())
                done = migrations.migrate(db, directory=ancien)
                self.assertEqual([m.version for m in done], [1, 2, 3, 4])

            # 2) l'ancien monde écrit avec les colonnes de 0003 seulement
            #    (le code v1 d'aujourd'hui connaît current_prompt/key_role, pas
            #    ce schéma : on simule l'ancien code, pas le nouveau).
            seed = signing.backend("pure").generate_seed()
            public = signing.backend("pure").public_from_seed(seed)
            db.execute(
                "INSERT INTO agent_registry (name, harness, host, public_key, "
                "public_key_fingerprint, key_updated_at) VALUES (%s, %s, %s, %s, %s, now())",
                ("proprietaire", "claude", "laptop", authority.b64(public),
                 signing.fingerprint(public)),
            )
            built = authority.build_approval(
                seed, approver="proprietaire", action="merge", artifact_kind="diff",
                artifact_hash=authority.hash_artifact("diff d'avant migration"), ttl=3600)
            approval_id = db.query(
                """
                INSERT INTO mesh_approvals
                    (approver, action, artifact_kind, artifact_hash, decision, nonce,
                     signed_payload, signature, signature_key, created_at, expires_at,
                     consumed_at, consumed_by)
                VALUES (%s, %s, %s, %s, %s, %s, %s, %s, %s,
                        to_timestamp(%s::bigint / 1000000.0),
                        to_timestamp(%s::bigint / 1000000.0), now(), 'avant')
                RETURNING id
                """,
                (built["approver"], built["action"], built["artifact_kind"],
                 built["artifact_hash"], built["decision"], built["nonce"],
                 built["signed_payload"], built["signature"], built["signature_key"],
                 built["created_us"], built["expires_us"]),
            )[0]["id"]
            artifact = built["artifact_hash"]
            built["id"] = int(approval_id)

            # 3) migration réelle : 0005, 0006, 0007 (le backfill). Une clé
            #    enregistrée avant 0005 devient « agent » (moindre privilège) :
            #    l'acte explicite --role owner est rejoué, comme à la bascule.
            done = migrations.migrate(db)
            self.assertEqual([m.version for m in done],
                             [m.version for m in migrations.discover() if m.version >= 5])
            with db.transaction() as tx:     # the owner's own gesture (0364)
                tx.execute("SET LOCAL ameesh.owner_gesture = 'on'")
                tx.execute("UPDATE agent_registry SET key_role = 'owner' "
                           "WHERE name = 'proprietaire'")
            ledger = db.query(
                "SELECT approver, nonce, consumed_by FROM mesh_consumed_nonces")
            self.assertEqual(len(ledger), 1)
            self.assertEqual(ledger[0]["consumed_by"], "avant")

            # 4) suppression puis réinsertion du même reçu signé : refusé
            db.execute("DELETE FROM mesh_approvals WHERE id = %s", (built["id"],))
            db.query(
                """
                INSERT INTO mesh_approvals
                    (approver, action, artifact_kind, artifact_hash, decision, nonce,
                     signed_payload, signature, signature_key, created_at, expires_at)
                VALUES (%s, %s, %s, %s, %s, %s, %s, %s, %s,
                        to_timestamp(%s::bigint / 1000000.0), to_timestamp(%s::bigint / 1000000.0))
                RETURNING id
                """,
                (built["approver"], built["action"], built["artifact_kind"],
                 built["artifact_hash"], built["decision"], built["nonce"],
                 built["signed_payload"], built["signature"], built["signature_key"],
                 built["created_us"], built["expires_us"]),
            )
            row, verdict = authority.find_approval(db, "merge", artifact)
            self.assertIsNone(row, "le reçu réinséré ne doit pas être reconsommable")
            self.assertIn("nonce already consumed", verdict.reason)
        finally:
            db.close()
            self.db.execute('DROP SCHEMA IF EXISTS "%s" CASCADE' % schema)

    def test_statement_timeout_borne_les_requetes(self):
        """Une requête qui pend est annulée : le battement de bail ne bloque pas."""
        import dataclasses
        import time as temps
        cfg = dataclasses.replace(self.cfg, statement_timeout_ms=300)
        db = db_mod.connect(cfg)
        try:
            debut = temps.monotonic()
            with self.assertRaises(db_mod.DbError) as ctx:
                db.query("SELECT pg_sleep(3)")
            self.assertLess(temps.monotonic() - debut, 2.5)
            self.assertIn("timeout", str(ctx.exception).lower())
        finally:
            db.close()

    def test_migration_longue_non_bornee_par_le_statement_timeout(self):
        """Les migrations lèvent le délai (SET LOCAL) : une longue passe aboutit."""
        import dataclasses
        import tempfile
        schema = "t_lente_%s" % uuid.uuid4().hex[:8]
        db = self.connect(schema)
        try:
            db.execute('CREATE SCHEMA "%s"' % schema)
            with tempfile.TemporaryDirectory() as dossier:
                with open(os.path.join(dossier, "0001_lente.sql"), "w", encoding="utf-8") as fh:
                    fh.write("create table lente (id int primary key);\n"
                             "select pg_sleep(0.6);\n")
                cfg = dataclasses.replace(self.cfg, schema=schema, statement_timeout_ms=200)
                lent = db_mod.connect(cfg)
                try:
                    # The shared bench's real election lock may be held by
                    # another schema. This test measures the migration body,
                    # not the deliberately bounded election wait.
                    from unittest import mock
                    with mock.patch.object(migrations, "_LOCK", "t_" + schema):
                        done = migrations.migrate(lent, directory=dossier)
                    self.assertEqual([m.version for m in done], [1])
                finally:
                    lent.close()
        finally:
            db.close()
            self.db.execute('DROP SCHEMA IF EXISTS "%s" CASCADE' % schema)

    def test_migration_attend_verrou_borne_et_reste_en_attente(self):
        """L835 : l'attente d'un verrou de table est bornée ; la transaction
        est annulée, la migration reste en attente, puis s'applique.

        R1 : le témoin atteste l'attente VUE par le serveur (pg_stat_activity)
        et juge l'erreur/le rollback — pas une durée totale."""
        import threading
        from unittest import mock
        schema = "t_verrou_%s" % uuid.uuid4().hex[:8]
        db = self.connect(schema)
        holder = self.connect(schema)
        libere = threading.Event()
        resultat: dict = {}
        fil = fil2 = None
        try:
            db.execute('CREATE SCHEMA "%s"' % schema)
            with tempfile.TemporaryDirectory() as dossier:
                with open(os.path.join(dossier, "0001_table.sql"), "w", encoding="utf-8") as fh:
                    fh.write("create table verrou (id int primary key);\n")
                self.assertEqual(
                    [m.version for m in migrations.migrate(db, directory=dossier)], [1])
                with open(os.path.join(dossier, "0002_colonne.sql"), "w", encoding="utf-8") as fh:
                    fh.write("alter table verrou add column note text;\n")
                pris = threading.Event()

                def tenir():
                    try:
                        with holder.transaction() as tx:
                            tx.execute("LOCK TABLE verrou IN ACCESS EXCLUSIVE MODE")
                            pris.set()
                            libere.wait(30)
                    except Exception as exc:  # le témoin doit dire pourquoi il n'a pas pris
                        resultat["tenir"] = exc

                fil = threading.Thread(target=tenir, daemon=True)
                fil.start()
                self.assertTrue(pris.wait(5),
                                "le verrou ACCESS EXCLUSIVE n'a pas été pris : %s"
                                % resultat.get("tenir"))

                def migrer():
                    con = self.connect_marque(schema, marque_temoin("table", schema))
                    try:
                        resultat["done"] = [
                            m.version for m in migrations.migrate(con, directory=dossier)]
                    except Exception as exc:  # témoin du verdict, remonté tel quel
                        resultat["erreur"] = exc
                    finally:
                        con.close()

                with mock.patch.object(migrations, "LOCK_TIMEOUT", "3s"):
                    fil2 = threading.Thread(target=migrer, daemon=True)
                    fil2.start()
                    # la migration est VUE en attente du verrou de table : le
                    # sleep seul pouvait passer avant qu'elle n'atteigne son SQL
                    self.assertTrue(
                        attente_vue(db, marque_temoin("table", schema), "relation"),
                        "le backend de la migration n'a pas été vu en attente du verrou de table")
                    fil2.join(15)
                self.assertFalse(fil2.is_alive(),
                                 "la migration attend toujours le verrou : la borne n'a pas joué")
                self.assertIn("erreur", resultat,
                              "la migration n'a pas échoué alors que la borne est dépassée")
                self.assertIn("lock timeout", str(resultat["erreur"]).lower())
                # transaction annulée : rien d'enregistré, le lot reste en attente
                self.assertNotIn(2, migrations.applied(db))
                self.assertEqual([m.version for m in migrations.pending(db, dossier)], [2])
                # verrou libéré : le lot s'applique
                libere.set()
                fil.join(10)
                self.assertFalse(fil.is_alive())
                self.assertEqual(
                    [m.version for m in migrations.migrate(db, directory=dossier)], [2])
                self.assertIn(2, migrations.applied(db))
        finally:
            libere.set()
            # les témoins rendent leurs verrous avant que le schéma ne tombe :
            # un migrate resté en attente et le DROP SCHEMA se bloqueraient
            # l'un l'autre (deadlock), et masqueraient l'échec réel du test
            for temoin in (fil, fil2):
                if temoin is not None:
                    temoin.join(15)
            holder.close()
            db.close()
            self.db.execute('DROP SCHEMA IF EXISTS "%s" CASCADE' % schema)

    def test_migration_qui_doit_attendre_pose_son_propre_delai(self):
        """L835 : le réglage de la migration l'emporte sur la borne du
        mécanisme — une migration qui assume d'attendre se pose son délai.

        R1 : le backend est VU en attente du verrou de table ; c'est seulement
        ensuite qu'on vérifie qu'il tient plus que la borne du mécanisme."""
        import threading
        import time as temps
        from unittest import mock
        schema = "t_propre_%s" % uuid.uuid4().hex[:8]
        db = self.connect(schema)
        holder = self.connect(schema)
        libere = threading.Event()
        resultat: dict = {}
        fil = fil2 = None
        try:
            db.execute('CREATE SCHEMA "%s"' % schema)
            with tempfile.TemporaryDirectory() as dossier:
                with open(os.path.join(dossier, "0001_table.sql"), "w", encoding="utf-8") as fh:
                    fh.write("create table propre (id int primary key);\n")
                self.assertEqual(
                    [m.version for m in migrations.migrate(db, directory=dossier)], [1])
                with open(os.path.join(dossier, "0002_colonne.sql"), "w", encoding="utf-8") as fh:
                    fh.write("set local lock_timeout = '10s';\n"
                             "alter table propre add column note text;\n")
                pris = threading.Event()

                def tenir():
                    try:
                        with holder.transaction() as tx:
                            tx.execute("LOCK TABLE propre IN ACCESS EXCLUSIVE MODE")
                            pris.set()
                            libere.wait(30)
                    except Exception as exc:  # le témoin doit dire pourquoi il n'a pas pris
                        resultat["tenir"] = exc

                fil = threading.Thread(target=tenir, daemon=True)
                fil.start()
                self.assertTrue(pris.wait(5),
                                "le verrou ACCESS EXCLUSIVE n'a pas été pris : %s"
                                % resultat.get("tenir"))

                def migrer():
                    con = self.connect_marque(schema, marque_temoin("propre", schema))
                    try:
                        resultat["done"] = [
                            m.version for m in migrations.migrate(con, directory=dossier)]
                    except Exception as exc:  # témoin du verdict, remonté tel quel
                        resultat["erreur"] = exc
                    finally:
                        con.close()

                # la borne du mécanisme est plus courte que l'attente : c'est le
                # réglage de la migration qui décide (sinon elle échouerait ici)
                with mock.patch.object(migrations, "LOCK_TIMEOUT", "300ms"):
                    fil2 = threading.Thread(target=migrer, daemon=True)
                    fil2.start()
                    self.assertTrue(
                        attente_vue(db, marque_temoin("propre", schema), "relation"),
                        "le backend de la migration n'a pas été vu en attente du verrou de table")
                    # vu en attente : 300 ms plus tard, la migration tient
                    # toujours — son `set local lock_timeout = '10s'` a primé
                    temps.sleep(0.8)
                    self.assertNotIn("erreur", resultat,
                                     "le réglage de la migration n'a pas primé sur LOCK_TIMEOUT")
                    libere.set()
                    fil.join(10)
                    fil2.join(10)
                self.assertEqual(resultat.get("done"), [2])
                self.assertIn(2, migrations.applied(db))
        finally:
            libere.set()
            # les témoins rendent leurs verrous avant que le schéma ne tombe :
            # un migrate resté en attente et le DROP SCHEMA se bloqueraient
            # l'un l'autre (deadlock), et masqueraient l'échec réel du test
            for temoin in (fil, fil2):
                if temoin is not None:
                    temoin.join(15)
            holder.close()
            db.close()
            self.db.execute('DROP SCHEMA IF EXISTS "%s" CASCADE' % schema)

    def test_verrou_consultatif_non_borne(self):
        """L835 : le verrou consultatif des migrations n'est pas borné par
        LOCK_TIMEOUT — un `migrate` concurrent attend son tour, puis applique.

        R1 : le backend est VU en attente `advisory` ; c'est seulement ensuite
        qu'on vérifie qu'il tient plus que la borne du mécanisme."""
        import threading
        import time as temps
        from unittest import mock
        schema = "t_consult_%s" % uuid.uuid4().hex[:8]
        db = self.connect(schema)
        holder = self.connect(schema)
        libere = threading.Event()
        resultat: dict = {}
        fil = fil2 = None
        try:
            db.execute('CREATE SCHEMA "%s"' % schema)
            with tempfile.TemporaryDirectory() as dossier:
                with open(os.path.join(dossier, "0001_table.sql"), "w", encoding="utf-8") as fh:
                    fh.write("create table consult (id int primary key);\n")
                pris = threading.Event()

                def tenir():
                    try:
                        with holder.transaction() as tx:
                            tx.execute(
                                "select pg_advisory_xact_lock(hashtext('%s'))" % migrations._LOCK)
                            pris.set()
                            libere.wait(30)
                    except Exception as exc:  # le témoin doit dire pourquoi il n'a pas pris
                        resultat["tenir"] = exc

                fil = threading.Thread(target=tenir, daemon=True)
                fil.start()
                self.assertTrue(pris.wait(5), "le verrou consultatif n'a pas été pris : %s"
                                % resultat.get("tenir"))

                def migrer():
                    con = self.connect_marque(schema, marque_temoin("consult", schema))
                    try:
                        resultat["done"] = [
                            m.version for m in migrations.migrate(con, directory=dossier)]
                    except Exception as exc:  # témoin du verdict, remonté tel quel
                        resultat["erreur"] = exc
                    finally:
                        con.close()

                # la borne de 300 ms ne doit pas s'appliquer au verrou consultatif
                with mock.patch.object(migrations, "LOCK_TIMEOUT", "300ms"):
                    fil2 = threading.Thread(target=migrer, daemon=True)
                    fil2.start()
                    self.assertTrue(
                        attente_vue(db, marque_temoin("consult", schema), "advisory"),
                        "le backend de la migration n'a pas été vu en attente consultative")
                    # vu en attente consultative : 300 ms plus tard, il attend
                    # toujours — la borne du mécanisme ne s'y applique pas
                    temps.sleep(0.8)
                    self.assertNotIn("erreur", resultat,
                                     "le verrou consultatif a été borné par LOCK_TIMEOUT")
                    libere.set()
                    fil.join(10)
                    fil2.join(10)
                self.assertEqual(resultat.get("done"), [1])
                self.assertIn(1, migrations.applied(db))
        finally:
            libere.set()
            # les témoins rendent leurs verrous avant que le schéma ne tombe :
            # un migrate resté en attente et le DROP SCHEMA se bloqueraient
            # l'un l'autre (deadlock), et masqueraient l'échec réel du test
            for temoin in (fil, fil2):
                if temoin is not None:
                    temoin.join(15)
            holder.close()
            db.close()
            self.db.execute('DROP SCHEMA IF EXISTS "%s" CASCADE' % schema)

    def test_marque_de_temoin_ne_voit_pas_une_autre_invocation(self):
        """#835 R2 : une marque d'une AUTRE invocation ne compte pas.

        Deux exécutions de la suite peuvent partager le cluster (AMEESH_TEST_DSN
        commun sur un hôte d'agents). Le témoin doit exiger SA marque : un
        backend en attente portant une autre marque ne doit jamais le faire
        passer vert (faux vert relevé par cx2 et l'auditeur).
        """
        import threading
        schema = "t_marque_%s" % uuid.uuid4().hex[:8]
        db = self.connect(schema)
        holder = self.connect(schema)
        fil = None
        try:
            db.execute('CREATE SCHEMA "%s"' % schema)
            db.execute("create table marquee (id int primary key)")
            marque = marque_temoin("table", schema)
            autre = marque_temoin("table", "t_autre_%s" % uuid.uuid4().hex[:8])

            def lire():
                con = self.connect_marque(schema, marque)
                try:
                    con.query("SELECT * FROM marquee")
                finally:
                    con.close()

            with holder.transaction() as tx:
                tx.execute("LOCK TABLE marquee IN ACCESS EXCLUSIVE MODE")
                fil = threading.Thread(target=lire, daemon=True)
                fil.start()
                self.assertTrue(attente_vue(db, marque, "relation"),
                                "le backend portant SA marque n'a pas été vu en attente")
                self.assertFalse(attente_vue(db, autre, "relation", timeout=0.3),
                                 "une marque d'une AUTRE invocation a été comptée")
            fil.join(10)
            self.assertFalse(fil.is_alive(), "le lecteur bloqué n'a pas rendu la main")
        finally:
            if fil is not None:
                fil.join(15)
            holder.close()
            db.close()
            self.db.execute('DROP SCHEMA IF EXISTS "%s" CASCADE' % schema)

    def test_schema_absent_message_actionnable(self):
        schema = "t_vide_%s" % uuid.uuid4().hex[:8]
        db = self.connect(schema)
        try:
            db.execute('CREATE SCHEMA "%s"' % schema)
            with self.assertRaises(db_mod.SchemaMissing) as ctx:
                db_mod.require_schema(db)
            self.assertIn("migrate", str(ctx.exception))
        finally:
            db.close()
            self.db.execute('DROP SCHEMA IF EXISTS "%s" CASCADE' % schema)


if __name__ == "__main__":
    unittest.main()
