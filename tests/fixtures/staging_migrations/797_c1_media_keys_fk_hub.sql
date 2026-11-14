-- =========================================================================
-- 797 — C1 #659 prérequis (#7288) : les cinq FK des tables média et clés
--       vers T1 sont retirées du hub ; la base de contenu les garde.
-- =========================================================================
-- Source : inventaire #7274 (codex3), `docs/p11/c1-t1-hub-access-inventory.md`
-- §4 et `scripts/p11-t1-hub-reads.manifest.json`. Après C1 (#659), les onze
-- tables T1 quittent `public` du hub. Les trois porteuses ci-dessous restent
-- au hub ; leurs cinq FK vers `message_keys`, `messages` et `conversations`
-- deviendraient des références à `ops_retired_p11` puis bloqueraient le C2.
-- Le précheck `--go` les refuse avant C1 : elles sont retirées ici, par une
-- migration revue et réversible, comme #7266 l'a fait pour ses deux FK.
--
-- Classement : aucune n'est `moved-with-t1` (la porteuse doit être l'une des
-- onze tables T1 ; ici elle reste au hub). Toutes sont **retirées du hub**.
-- La base de contenu applique aussi ce fichier, mais les trois cibles y
-- restent : elle garde ses cinq contraintes (intégrité, cascades de rétention
-- et de destruction de clé), et le fichier y est un no-op. Le marqueur
-- `graph` est celui de 242 (contenu seul), déjà employé par 586.
--
-- Ce que ce fichier ne fait pas : aucune table, aucune colonne, aucune donnée,
-- aucune ACL, aucune fonction. Seules les cinq contraintes du hub partent.
-- Aucune suppression d'orphelin : le rollback les tolère en `NOT VALID`.
--
-- Ordre : après E5 (`CONTENT_MESSAGING_MODE=content`, écrivain contenu) et
-- avant `supabase/contracts/p11-messaging-contract.sql`. Numéro 797 (réservé
-- L122) : 795 #7273, 796 #7266, 797 #7287.
--
-- Rollback : `supabase/rollback/797_restore.sql` (hub seulement, `NOT VALID`).
-- Témoins : `supabase/tests/797_c1_media_keys_fk_test.sql` (hub) et
-- `supabase/tests/797_c1_media_keys_fk_content_test.sql` (contenu).
-- Conception : `docs/p11/c1-media-keys-foreign-keys.md`.
--
-- migrate-target: hub
-- =========================================================================
BEGIN;
SET LOCAL search_path = public, pg_temp;
SET LOCAL lock_timeout = '5s';

DO $$
BEGIN
  -- La base de contenu a les mêmes tables, mais elle garde ses cibles T1 :
  -- retirer les FK y perdrait l'intégrité et les cascades de l'écrivain.
  IF to_regnamespace('graph') IS NOT NULL THEN
    RAISE NOTICE '797: base de contenu — les cinq FK restent ; rien à retirer';
    RETURN;
  END IF;

  -- 231 : la clé d'une ligne scellée est celle de SA conversation.
  ALTER TABLE public.media_at_rest
    DROP CONSTRAINT IF EXISTS media_at_rest_key_is_conversation;

  -- 351 : le ledger privé nomme sa conversation, son message source et sa clé.
  ALTER TABLE public.routing_media_private_copies
    DROP CONSTRAINT IF EXISTS routing_media_private_copies_conversation_id_fkey;
  ALTER TABLE public.routing_media_private_copies
    DROP CONSTRAINT IF EXISTS routing_media_private_copies_source_message_id_fkey;
  ALTER TABLE public.routing_media_private_copies
    DROP CONSTRAINT IF EXISTS routing_media_private_copies_key_is_conversation;

  -- 586 : la tombe de conversation crypto-effacée.
  ALTER TABLE public.message_keys_destroyed_conversations
    DROP CONSTRAINT IF EXISTS message_keys_destroyed_conversations_conversation_id_fkey;
END $$;

COMMIT;
