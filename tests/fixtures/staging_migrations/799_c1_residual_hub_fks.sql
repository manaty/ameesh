-- 799 — P11 C1 #659 / #7287 : les FK hub → conversations des tables survivantes
--       passent en références souples ; la garde des webhooks ne lit plus T1.
--
-- Source : inventaire #7274 (`review/codex3-7274-precheck`, gel 848be410d,
-- confirmé sur `pg_catalog` jusqu'à 793). Les onze tables T1 quittent `public`
-- au C1 ; ces dix porteuses restent au hub. Leur FK vers `conversations`
-- refuserait alors une écriture légitime (23503) dès qu'une conversation
-- n'existe que côté contenu : webhook de canal, carte d'import, lien de
-- demande de page, appel avec conversation, application des membres. Ce sont
-- des erreurs rendues à l'utilisateur, que §16 interdit.
--
-- Les deux FK `channel_command*` sont retirées par 795 (#7266) ; les cinq FK
-- des tables média/clés (`media_at_rest`, `routing_media_private_copies` ×3,
-- `message_keys_destroyed_conversations`) restent au lot média/clés.
--
-- **Hub seulement pour les FK.** La base de contenu applique aussi les
-- migrations du hub, mais elle garde `public.conversations` : retirer ses dix
-- FK y perdrait l'intégrité et les cascades des tables de contenu. La garde
-- est le marqueur `graph` de 242 (schéma de contenu seul, déjà employé par
-- 586 et 796) : sur le contenu, les contraintes et cascades restent intactes.
-- Le rollback porte la même garde.
--
-- **Ce qui remplace la cascade de `channel_webhooks`.** Après le C1, un canal
-- supprimé côté contenu ne peut plus emporter ses webhooks au hub par la FK.
-- Deux étages (docs/p11/c1-residual-hub-fks.md §3) :
--   * fail-closed : l'ingress lit le canal sur le côté qui le porte et refuse
--     le jeton (404) avant même de lire le corps ; l'écrivain relit le canal
--     dans sa propre transaction. Aucun message, aucun journal de livraison.
--   * nettoyage : un déclencheur contenu `AFTER DELETE` sur `conversations`
--     (canal, hors écriture de réplique) enfile une suppression
--     `channel_webhooks` dans la reverse outbox (251) ; le hub l'applique par
--     `apply_channel_webhooks_reverse`, qui supprime le webhook et, par la FK
--     de 595, ses livraisons. Rejeu idempotent.
-- Le choix du relais plutôt qu'un balayage périodique : le hub n'a pas la
-- liste des canaux vivants, et le déclencheur est précis et sans fenêtre
-- d'orphelin actif (le fail-closed couvre la fenêtre d'application).
--
-- Rollback : supabase/rollback/799_restore.sql (FK hub seulement ; relais
-- contenu retiré, corps de 790 rendu à la porte inverse).
-- Témoins : supabase/tests/799_c1_residual_hub_fks_test.sql (hub) et
-- supabase/tests/799_c1_residual_hub_fks_content_test.sql (contenu).
--
-- migrate-target: hub
-- =========================================================================

BEGIN;
SET LOCAL lock_timeout = '5s';
SET LOCAL search_path = public, pg_temp;

DO $hub$
BEGIN
  -- 242 : le schéma `graph` n'existe que sur la base de contenu. Les dix
  -- cibles y restent : on ne touche ni aux contraintes ni à la garde. En
  -- revanche le contenu est l'écrivain des canaux après le C1 : c'est lui qui
  -- relaie la suppression d'un canal au hub par la reverse outbox (251).
  IF to_regnamespace('graph') IS NOT NULL THEN
    CREATE OR REPLACE FUNCTION ops.content_reverse_channel_webhook_cleanup()
    RETURNS trigger
    LANGUAGE plpgsql
    SECURITY DEFINER
    SET search_path = ops, pg_temp
    AS $fn$
    BEGIN
      -- Une suppression de réplique (retour C1) vient du hub : la rejouer
      -- vers lui en reverse serait un écho.
      IF current_setting('nexlink.replica_write', true) = 'on' THEN
        RETURN NULL;
      END IF;
      IF OLD.kind IS DISTINCT FROM 'channel' THEN
        RETURN NULL;
      END IF;
      PERFORM ops.enqueue_content_reverse_op(
        'channel_webhooks',
        OLD.id::text,
        greatest(coalesce(OLD.version, 1), 1),
        'delete',
        NULL);
      RETURN NULL;
    END $fn$;

    COMMENT ON FUNCTION ops.content_reverse_channel_webhook_cleanup() IS
      '#7287 (799) : AFTER DELETE sur conversations du contenu — un canal supprimé enfile la suppression de ses webhooks au hub (domain channel_webhooks, op delete, version = conversations.version). Ignoré sous nexlink.replica_write (suppression venue du hub).';

    REVOKE EXECUTE ON FUNCTION ops.content_reverse_channel_webhook_cleanup()
      FROM PUBLIC, anon, authenticated;

    DROP TRIGGER IF EXISTS conversations_reverse_channel_webhook_cleanup
      ON public.conversations;
    CREATE TRIGGER conversations_reverse_channel_webhook_cleanup
      AFTER DELETE ON public.conversations
      FOR EACH ROW
      EXECUTE FUNCTION ops.content_reverse_channel_webhook_cleanup();

    -- Le domaine entre dans la liste connue de la file inverse, comme
    -- `conversation_participants` en 790 : sans cela il ne pourrait être ni
    -- mis en pause avec t1 ni retiré nommément, et la liste ne décrirait plus
    -- les domaines réellement enfilés. Le retrait C1 de T1 (runbook) ne le
    -- cite pas : le nettoyage doit rester vivant après la contraction.
    CREATE OR REPLACE FUNCTION ops.content_reverse_outbox_known_domains()
    RETURNS text[]
    LANGUAGE sql
    IMMUTABLE
    SET search_path = ''
    AS $fn$
      SELECT ARRAY[
        -- t1
        'conversation', 'messaging', 'message_attachments', 'chantier_members',
        'page_conversation_read', 'conversation_participants',
        'channel_webhooks',
        -- t1, les marques de 690 (ops.message_marks_mirror)
        'message_reaction', 'message_pin', 'saved_message',
        -- t2
        'reward_events',
        -- t3
        'circles', 'circle_members', 'circle_pending_members', 'groups',
        'group_members', 'group_join_requests', 'invitations', 'location_shares',
        'location_history_settings', 'location_history_points', 'routing_preferences'
      ]::text[];
    $fn$;

    COMMENT ON FUNCTION ops.content_reverse_outbox_known_domains() IS
      '#6067 (706) + #7251 (790) + #7287 (799) : les domaines de ops.content_outbox_reverse (t1, t2, t3), y compris le porteur inverse `conversation_participants` et le nettoyage `channel_webhooks` d''un canal supprimé. Même liste que REVERSE_OUTBOX_DOMAIN_GROUPS (backend/src/config/content-reverse-outbox-domains.ts), comparée par un témoin jest.';

    REVOKE EXECUTE ON FUNCTION ops.content_reverse_outbox_known_domains()
      FROM PUBLIC, anon, authenticated;
    GRANT EXECUTE ON FUNCTION ops.content_reverse_outbox_known_domains()
      TO service_role;

    RAISE NOTICE '799: base de contenu — FK et cascades conservées ; relais de nettoyage des webhooks posé';
    RETURN;
  END IF;

  -- Après le C1, `conversations` n'est plus dans `public` : ce fichier se joue
  -- avant, sinon le retour central recrée les contraintes.
  IF to_regclass('public.conversations') IS NULL THEN
    RAISE EXCEPTION '799_AFTER_C1: public.conversations a déjà quitté le hub — jouer le retour C1 avant 799'
      USING ERRCODE = '55006';
  END IF;

  -- -----------------------------------------------------------------------
  -- 1. Les dix FK : l'id de conversation devient une référence souple.
  --    Aucune table ne bouge ; aucune donnée n'est supprimée.
  -- -----------------------------------------------------------------------
  ALTER TABLE public.channel_calls
    DROP CONSTRAINT IF EXISTS channel_calls_conversation_id_fkey;
  ALTER TABLE public.channel_webhooks
    DROP CONSTRAINT IF EXISTS channel_webhooks_channel_id_fkey;
  ALTER TABLE public.chantier_envelope_claims
    DROP CONSTRAINT IF EXISTS chantier_envelope_claims_conversation_id_fkey;
  ALTER TABLE public.chantier_members_applied
    DROP CONSTRAINT IF EXISTS chantier_members_applied_conversation_id_fkey;
  ALTER TABLE public.conversation_agents
    DROP CONSTRAINT IF EXISTS conversation_agents_conversation_id_fkey;
  ALTER TABLE public.import_map
    DROP CONSTRAINT IF EXISTS import_map_conversation_org_fkey;
  ALTER TABLE public.location_shares
    DROP CONSTRAINT IF EXISTS location_shares_conversation_id_fkey;
  ALTER TABLE public.page_inquiries
    DROP CONSTRAINT IF EXISTS page_inquiries_conversation_id_fkey;
  ALTER TABLE public.routing_direct_conversations
    DROP CONSTRAINT IF EXISTS routing_direct_conversations_conversation_id_fkey;
  ALTER TABLE public.videocalls
    DROP CONSTRAINT IF EXISTS videocalls_conversation_id_fkey;

  -- -----------------------------------------------------------------------
  -- 2. `channel_webhooks` reste un chemin d'écriture hub après le C1 : sa
  --    garde ne peut plus joindre `conversations`. Même corps qu'en 595, avec
  --    la branche `to_regclass` ; le refus de déplacer un webhook reste
  --    inconditionnel. L'autorisation du service (canal, page, archive) est
  --    lue côté contenu avant l'appel (WebhookAdminService.authorize).
  -- -----------------------------------------------------------------------
  CREATE OR REPLACE FUNCTION public.channel_webhooks_channel_guard()
  RETURNS trigger
  LANGUAGE plpgsql
  SECURITY DEFINER
  SET search_path = public, pg_temp
  AS $fn$
  DECLARE
    v_kind text;
  BEGIN
    IF TG_OP = 'UPDATE' AND NEW.channel_id IS DISTINCT FROM OLD.channel_id THEN
      RAISE EXCEPTION 'CHANNEL_WEBHOOK_MOVE: a webhook does not change channel'
        USING ERRCODE = '23514';
    END IF;

    -- Avant C1 : contrôle direct identique à 595. Après C1, la conversation
    -- n'est plus dans public : le service a déjà vérifié le canal côté contenu
    -- et la branche n'est pas planifiée (aucun 42P01).
    IF to_regclass('public.conversations') IS NOT NULL THEN
      SELECT c.kind INTO v_kind
        FROM public.conversations c
       WHERE c.id = NEW.channel_id;

      IF v_kind IS DISTINCT FROM 'channel' THEN
        RAISE EXCEPTION 'CHANNEL_WEBHOOK_NOT_CHANNEL: conversation % is %',
          NEW.channel_id, coalesce(v_kind, 'missing')
          USING ERRCODE = '23514';
      END IF;
    END IF;

    RETURN NEW;
  END $fn$;

  COMMENT ON FUNCTION public.channel_webhooks_channel_guard() IS
    'AM12-1 (595), #7287 : un webhook nomme une conversation `kind=''channel''` et ne change jamais de canal. Après le C1 de T1, la branche canal est sautée (to_regclass) : le service a lu le canal côté contenu.';

  -- -----------------------------------------------------------------------
  -- 3. Nettoyage effectif d'un canal supprimé côté contenu. Le worker de la
  --    reverse outbox applique la ligne `channel_webhooks` enfilée par le
  --    déclencheur contenu ; le DELETE emporte le journal des livraisons par
  --    la FK de 595. Idempotent : un canal déjà nettoyé rend applied=true.
  -- -----------------------------------------------------------------------
  CREATE OR REPLACE FUNCTION public.apply_channel_webhooks_reverse(
    p_pk      text,
    p_version integer,
    p_op      text,
    p_payload jsonb
  )
  RETURNS jsonb
  LANGUAGE plpgsql
  SECURITY DEFINER
  SET search_path = public, pg_temp
  AS $fn$
  DECLARE
    v_channel uuid;
    v_deleted integer;
  BEGIN
    IF p_op IS DISTINCT FROM 'delete' THEN
      RAISE EXCEPTION 'CHANNEL_WEBHOOKS_REVERSE_BAD_OP: %', p_op
        USING ERRCODE = '22023';
    END IF;
    IF p_pk IS NULL THEN
      RAISE EXCEPTION 'CHANNEL_WEBHOOKS_REVERSE_BAD_KEY' USING ERRCODE = '22023';
    END IF;
    BEGIN
      v_channel := p_pk::uuid;
    EXCEPTION WHEN invalid_text_representation THEN
      RAISE EXCEPTION 'CHANNEL_WEBHOOKS_REVERSE_BAD_KEY: % is not a uuid', p_pk
        USING ERRCODE = '22023';
    END;

    DELETE FROM public.channel_webhooks WHERE channel_id = v_channel;
    GET DIAGNOSTICS v_deleted = ROW_COUNT;

    RETURN jsonb_build_object(
      'domain', 'channel_webhooks', 'pk', p_pk, 'version', p_version,
      'op', 'delete', 'applied', true, 'deleted', v_deleted);
  END $fn$;

  COMMENT ON FUNCTION public.apply_channel_webhooks_reverse(text, int, text, jsonb) IS
    '#7287 (799) : applique au hub la suppression des webhooks d''un canal supprimé côté contenu (reverse outbox 251, domain channel_webhooks, op delete). Le journal des livraisons part par la FK de 595. Idempotent : un canal déjà nettoyé rend applied=true, deleted=0. Interne à la porte inverse, aucun grant direct.';

  -- L'application ne doit être atteignable que par la porte SECURITY DEFINER
  -- `apply_content_reverse_outbox` : un appel direct sauterait le GUC du
  -- journal des mouvements que la porte pose (335).
  REVOKE ALL ON FUNCTION public.apply_channel_webhooks_reverse(text, int, text, jsonb)
    FROM PUBLIC, anon, authenticated, service_role;

  -- La porte inverse route le domaine avant l'implémentation générique, qui
  -- le refuserait comme inconnu. Corps de 790, un branchement ajouté.
  CREATE OR REPLACE FUNCTION public.apply_content_reverse_outbox(
    p_domain  text,
    p_pk      text,
    p_version integer,
    p_op      text,
    p_payload jsonb
  )
  RETURNS jsonb
  LANGUAGE plpgsql
  SECURITY DEFINER
  SET search_path = public, ops, pg_temp
  AS $fn$
  DECLARE
    v_journal_prior text;
    v_replica_prior text;
    v_result        jsonb;
  BEGIN
    v_journal_prior := current_setting('nexlink.actor_move_journal_replica', true);
    v_replica_prior := current_setting('nexlink.replica_write', true);
    PERFORM set_config('nexlink.actor_move_journal_replica', 'on', true);
    BEGIN
      -- #7287 (799) : les webhooks d'un canal supprimé côté contenu.
      IF p_domain = 'channel_webhooks' THEN
        v_result := public.apply_channel_webhooks_reverse(
          p_pk, p_version, p_op, p_payload);
      -- AM10-1 (#7251, 790) : l'instantané des participants d'un canal.
      ELSIF p_domain = 'conversation_participants' THEN
        v_result := public.apply_conversation_participants_reverse(
          p_pk, p_version, p_op, p_payload);
      -- P11 L1b (#5830, 689) : réactions, épingles, enregistrements.
      ELSIF p_domain IN ('message_reaction', 'message_pin', 'saved_message') THEN
        v_result := public.apply_message_mark_reverse(
          p_domain, p_pk, p_version, p_op, p_payload);
      ELSE
        v_result := public.apply_content_reverse_outbox_impl(
          p_domain, p_pk, p_version, p_op, p_payload);
      END IF;

      -- G4-3b (#6585, 746) : le témoin durable des trois domaines que la porte
      -- peut consommer sans laisser de ligne. `applied` vrai couvre l'upsert et
      -- le delete physique (491 rend appliqué même quand la ligne était déjà
      -- absente) ; le skip nommé est une consommation voulue, l'absence est son
      -- état final. Un stale (`applied` faux, sans skip) n'est pas revendiqué.
      IF p_domain IN ('messaging', 'conversation', 'page_conversation_read')
         AND (coalesce((v_result ->> 'applied')::boolean, false)
              OR v_result ->> 'skipped' = 'clear_left_no_ciphertext') THEN
        INSERT INTO ops.content_reverse_applied AS l
          (domain, pk, version, op, applied_at)
        VALUES (p_domain, p_pk, p_version, p_op, now())
        ON CONFLICT (domain, pk) DO UPDATE
          SET version = EXCLUDED.version, op = EXCLUDED.op,
              applied_at = EXCLUDED.applied_at
          WHERE l.version < EXCLUDED.version;
      END IF;
    EXCEPTION WHEN OTHERS THEN
      PERFORM set_config('nexlink.actor_move_journal_replica',
                         coalesce(v_journal_prior, ''), true);
      PERFORM set_config('nexlink.replica_write',
                         coalesce(v_replica_prior, ''), true);
      RAISE;
    END;
    PERFORM set_config('nexlink.actor_move_journal_replica',
                       coalesce(v_journal_prior, ''), true);
    PERFORM set_config('nexlink.replica_write',
                       coalesce(v_replica_prior, ''), true);
    RETURN v_result;
  END $fn$;

  COMMENT ON FUNCTION public.apply_content_reverse_outbox(text, text, integer, text, jsonb) IS
    'P05-WRITE (#618, 491) + 591 + P11 L1b (#5830, 689) + G4-3b (#6585, 746) + #7251 (790) + #7287 (799) : applique au HUB une mutation de la reverse outbox. Le domaine channel_webhooks va à apply_channel_webhooks_reverse (799), conversation_participants à 790, les marques à 689, les autres à apply_content_reverse_outbox_impl (491). La porte pose nexlink.actor_move_journal_replica = on pendant l''application (content → hub) pour que les déclencheurs du journal des mouvements (335) voient l''écriture de réplique, tout en laissant nexlink.replica_write couper la capture forward et les déclencheurs métier. Les deux GUC sont rendus à l''appelant avant chaque sortie, succès comme erreur. Pour messaging, conversation et page_conversation_read, une mutation honorée revendique son maximum dans ops.content_reverse_applied (746). Une écriture qui arrive pendant la barrière est refusée par actor_move_change_append et reste dans l''outbox inverse (rejouée).';
END $hub$;

COMMIT;
