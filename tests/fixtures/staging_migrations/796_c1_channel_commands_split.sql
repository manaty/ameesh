-- =========================================================================
-- 796 — C1 #659 prerequisite (#7266) : split hub/content des commandes de
--       canal (641/643/651). Le hub ne joint plus les onze tables T1.
-- =========================================================================
-- Après C1 (#659), `conversations`, `conversation_participants` et `messages`
-- quittent `public` du hub. Les fonctions de 641/643/651 qui les lisaient
-- deviennent donc des appels à une table absente (42P01). Cette migration
-- ajoute les variantes **T1-free** : le backend lit les faits du canal côté
-- contenu (org, archived, title, visibility, member) et les transmet en
-- paramètres bornés. Les anciennes signatures restent intactes pour N-1 et
-- seront droppées par un lot de suite après C1.
--
-- Ce qui reste au hub et y est revérifié : grant vivant, agent (org + état),
-- `is_org_admin`, `is_active_page_actor`, propriété de la commande, quotas,
-- machine d'état des invocations, `org_audit`. Aucun `p_authorized` global :
-- `p_member` n'ouvre que la règle d'adhésion, qui appartient au contenu.
--
-- Les deux gardes de trigger gardent leur branche pré-C1 sous
-- `to_regclass('public.conversations')` : tant que T1 est là, le contrôle
-- direct est identique ; après C1, la branche est sautée et la garde ne
-- vérifie plus que ce que le hub possède.
--
-- Aucune table, aucune donnée : seules les deux FK `channel_id` qui visaient
-- `conversations` sont retirées (elles bloqueraient le retrait de T1 et
-- refuseraient un canal né côté contenu après C1). Les gardes de trigger
-- reprennent le contrôle d'existence pré-C1 ; le rollback les rend.
-- Rollback : `supabase/rollback/796_restore.sql`.
-- Migration hub-release : le projet contenu rejoue le schéma hub et l'applique
-- aussi (aucun marqueur `migrate-target: content`).
-- =========================================================================
BEGIN;
SET LOCAL search_path = public, pg_temp;

-- Dépendance de schéma vers T1 : les deux FK `channel_id` visent
-- `conversations`. Après C1, un canal peut n'exister que côté contenu : la FK
-- refuserait alors le grant et le registre (23503). Le contrôle d'existence
-- pré-C1 reste porté par les gardes (COMMAND_CHANNEL_NOT_FOUND) ; la cascade
-- de suppression disparaît avec la table (le hub n'a pas de geste de
-- suppression de canal : l'archive est le geste, cf. 573).
ALTER TABLE public.channel_command_grants
  DROP CONSTRAINT IF EXISTS channel_command_grants_channel_id_fkey;
ALTER TABLE public.channel_commands
  DROP CONSTRAINT IF EXISTS channel_commands_channel_id_fkey;

-- =========================================================================
-- 1. 641 — grants : garde, helper admin, liste admin, set, liste agent
-- =========================================================================

-- La garde d'écriture directe. Pré-C1 : identique à 641 (canal T1, verrou,
-- org, archive). Post-C1 : l'org vient de l'agent (le RPC a vérifié
-- l'égalité avec le fait contenu) ; le canal n'existe plus côté hub.
CREATE OR REPLACE FUNCTION public.channel_command_grants_guard() RETURNS trigger
LANGUAGE plpgsql SECURITY DEFINER SET search_path = public, pg_temp AS $$
DECLARE v_org uuid; v_agent_org uuid; v_state text; v_archived timestamptz;
BEGIN
  IF TG_OP = 'UPDATE' THEN
    IF NEW.channel_id IS DISTINCT FROM OLD.channel_id
       OR NEW.agent_id IS DISTINCT FROM OLD.agent_id
       OR NEW.created_at IS DISTINCT FROM OLD.created_at
       OR (NEW.granted_by IS DISTINCT FROM OLD.granted_by AND NEW.granted_by IS NOT NULL)
       OR (OLD.revoked_at IS NOT NULL AND NEW.revoked_at IS DISTINCT FROM OLD.revoked_at) THEN
      RAISE EXCEPTION 'COMMAND_GRANT_IMMUTABLE' USING ERRCODE = '22023';
    END IF;
    -- Account erasure may clear granted_by; history must survive it.
    RETURN NEW;
  END IF;

  IF to_regclass('public.conversations') IS NOT NULL THEN
    SELECT c.org_page_id, c.archived_at INTO v_org, v_archived
      FROM public.conversations c
     WHERE c.id = NEW.channel_id AND c.kind = 'channel' FOR UPDATE;
    IF NOT FOUND THEN
      RAISE EXCEPTION 'COMMAND_CHANNEL_NOT_FOUND' USING ERRCODE = 'P0002';
    END IF;
    IF v_archived IS NOT NULL THEN
      RAISE EXCEPTION 'COMMAND_CHANNEL_ARCHIVED' USING ERRCODE = '22023';
    END IF;
  ELSE
    -- Fait contenu : kind/archived sont vérifiés par le RPC appelant, qui les
    -- tient du contenu. Le hub ne peut plus les relire.
    SELECT sa.organisation_id INTO v_org
      FROM public.service_actors sa WHERE sa.id = NEW.agent_id;
    IF NOT FOUND THEN
      RAISE EXCEPTION 'COMMAND_AGENT_UNAVAILABLE' USING ERRCODE = '22023';
    END IF;
  END IF;
  SELECT sa.organisation_id, sa.state INTO v_agent_org, v_state
    FROM public.service_actors sa WHERE sa.id = NEW.agent_id FOR SHARE;
  IF NOT FOUND OR v_agent_org IS DISTINCT FROM v_org OR v_state <> 'active' THEN
    RAISE EXCEPTION 'COMMAND_AGENT_UNAVAILABLE' USING ERRCODE = '22023';
  END IF;
  IF NEW.granted_by IS NULL OR NOT public.is_org_admin(v_org, NEW.granted_by)
     OR NOT public.is_active_page_actor(v_org) THEN
    RAISE EXCEPTION 'COMMAND_GRANT_FORBIDDEN' USING ERRCODE = '42501';
  END IF;
  RETURN NEW;
END $$;

-- Même porte admin, sans lecture T1 : l'org est un fait contenu borné.
CREATE FUNCTION public.command_grants_admin_org(
  p_channel uuid,
  p_account uuid,
  p_org     uuid
)
RETURNS uuid LANGUAGE plpgsql STABLE SECURITY DEFINER
SET search_path = public, pg_temp AS $$
BEGIN
  IF p_org IS NULL OR p_channel IS NULL THEN
    RAISE EXCEPTION 'COMMAND_CHANNEL_NOT_FOUND' USING ERRCODE = 'P0002';
  END IF;
  IF p_account IS NULL OR NOT public.is_org_admin(p_org, p_account)
     OR NOT public.is_active_page_actor(p_org) THEN
    RAISE EXCEPTION 'COMMAND_GRANT_FORBIDDEN' USING ERRCODE = '42501';
  END IF;
  RETURN p_org;
END $$;

CREATE FUNCTION public.channel_command_agents(
  p_channel uuid,
  p_account uuid,
  p_org     uuid,
  p_limit   int DEFAULT 200,
  p_offset  int DEFAULT 0
)
RETURNS TABLE(id uuid, channel_id uuid, agent_id uuid, name text, state text,
  granted_by uuid, created_at timestamptz, revoked_at timestamptz)
LANGUAGE plpgsql STABLE SECURITY DEFINER SET search_path = public, pg_temp AS $$
BEGIN
  PERFORM public.command_grants_admin_org(p_channel, p_account, p_org);
  RETURN QUERY SELECT g.id, g.channel_id, g.agent_id, sa.name, sa.state,
    g.granted_by, g.created_at, g.revoked_at
    FROM public.channel_command_grants g JOIN public.service_actors sa ON sa.id = g.agent_id
    -- Croisement du fait contenu : un grant de p_channel ne sort que si son
    -- agent est connu du hub dans l'organisation du canal. Un admin muni d'un
    -- fait d'une autre organisation ne lit donc rien (auditeur, B #218916).
    WHERE g.channel_id = p_channel AND sa.organisation_id = p_org
    ORDER BY g.created_at DESC, g.id LIMIT greatest(0, least(coalesce(p_limit, 200), 200)) OFFSET greatest(0, least(coalesce(p_offset, 0), 1000000));
END $$;

CREATE FUNCTION public.set_channel_command_grant(
  p_channel           uuid,
  p_account           uuid,
  p_agent             uuid,
  p_grant             boolean,
  p_channel_org       uuid,
  p_channel_archived  boolean
)
RETURNS uuid LANGUAGE plpgsql SECURITY DEFINER SET search_path = public, pg_temp AS $$
DECLARE v_org uuid; v_id uuid;
BEGIN
  v_org := public.command_grants_admin_org(p_channel, p_account, p_channel_org);
  IF p_channel_archived IS NOT FALSE THEN
    RAISE EXCEPTION 'COMMAND_CHANNEL_ARCHIVED' USING ERRCODE = '22023';
  END IF;
  IF p_agent IS NULL OR p_grant IS NULL THEN
    RAISE EXCEPTION 'COMMAND_GRANT_BAD_ARGUMENT' USING ERRCODE = '22023';
  END IF;
  -- Croisement du fait contenu : une révocation ne vise que le grant d'un
  -- agent encore connu du hub dans l'organisation du canal. Un agent supprimé
  -- est ignoré (la cascade a déjà retiré ses grants) ; un agent connu d'une
  -- autre organisation ne rend aucune ligne : aucun effet, aucun audit.
  SELECT g.id INTO v_id FROM public.channel_command_grants g
    WHERE g.channel_id = p_channel AND g.agent_id = p_agent AND g.revoked_at IS NULL
      AND NOT EXISTS (
        SELECT 1 FROM public.service_actors sa
         WHERE sa.id = g.agent_id
           AND sa.organisation_id IS DISTINCT FROM v_org);
  IF p_grant THEN
    -- Check even on an idempotent replay: a suspended/moved agent is unavailable.
    PERFORM 1 FROM public.service_actors sa WHERE sa.id = p_agent
      AND sa.organisation_id = v_org AND sa.state = 'active' FOR SHARE;
    IF NOT FOUND THEN
      RAISE EXCEPTION 'COMMAND_AGENT_UNAVAILABLE' USING ERRCODE = '22023';
    END IF;
    IF v_id IS NOT NULL THEN RETURN v_id; END IF;
    INSERT INTO public.channel_command_grants(channel_id, agent_id, granted_by)
      VALUES(p_channel, p_agent, p_account) RETURNING id INTO v_id;
  ELSE
    -- Revoke remains possible for a suspended/deleted agent; no revival.
    IF v_id IS NULL THEN RETURN NULL; END IF;
    UPDATE public.channel_command_grants SET revoked_at = clock_timestamp() WHERE id = v_id;
  END IF;
  PERFORM public.org_audit_record(v_org, p_account, 'account',
    CASE WHEN p_grant THEN 'command.granted' ELSE 'command.revoked' END,
    'service_actor', p_agent, jsonb_build_object('channel_id', p_channel, 'grant_id', v_id));
  RETURN v_id;
END $$;

-- Sortie sans titre ni org : le hub ne connaît plus le canal. Le backend
-- hydrate depuis le contenu (une lecture batch) et filtre kind/archived/org.
CREATE FUNCTION public.agent_command_channels_v2(
  p_agent uuid,
  p_org   uuid,
  p_limit int DEFAULT 200,
  p_offset int DEFAULT 0
)
RETURNS TABLE(grant_id uuid, channel_id uuid, created_at timestamptz)
LANGUAGE sql STABLE SECURITY DEFINER SET search_path = public, pg_temp AS $$
  SELECT g.id, g.channel_id, g.created_at
  FROM public.channel_command_grants g
  JOIN public.service_actors sa ON sa.id = g.agent_id
  WHERE g.agent_id = p_agent AND sa.organisation_id = p_org
    AND sa.state = 'active'
    AND public.is_active_page_actor(p_org)
    AND g.revoked_at IS NULL
  ORDER BY g.created_at, g.id LIMIT greatest(0, least(coalesce(p_limit, 200), 200)) OFFSET greatest(0, least(coalesce(p_offset, 0), 1000000));
$$;

-- =========================================================================
-- 2. 643 — commandes : garde, register, update, archive, disable, listes,
--    résolution commande → canal
-- =========================================================================

-- Même garde qu'en 643, avec la branche T1 sous `to_regclass`. Post-C1 :
-- l'org vient de l'agent (immuable sur la ligne) ; l'archive du canal est un
-- fait contenu que seul le RPC peut refuser.
CREATE OR REPLACE FUNCTION public.channel_commands_guard() RETURNS trigger
LANGUAGE plpgsql SECURITY DEFINER SET search_path = public, pg_temp AS $$
DECLARE
  v_org       uuid;
  v_archived  timestamptz;
  v_agent_org uuid;
  v_state     text;
BEGIN
  IF TG_OP = 'UPDATE' THEN
    -- Le nom et le canal sont l'identité de la commande : ils ne bougent pas.
    -- L'archive est monotone ; seul l'effacement du compte peut vider
    -- `archived_by` (ON DELETE SET NULL).
    IF NEW.channel_id IS DISTINCT FROM OLD.channel_id
       OR NEW.agent_id IS DISTINCT FROM OLD.agent_id
       OR NEW.name IS DISTINCT FROM OLD.name
       OR NEW.created_at IS DISTINCT FROM OLD.created_at
       OR (OLD.archived_at IS NOT NULL AND NEW.archived_at IS DISTINCT FROM OLD.archived_at)
       OR (OLD.archived_by IS NOT NULL AND NEW.archived_by IS DISTINCT FROM OLD.archived_by
           AND NEW.archived_by IS NOT NULL) THEN
      RAISE EXCEPTION 'COMMAND_IMMUTABLE' USING ERRCODE = '22023';
    END IF;

    IF to_regclass('public.conversations') IS NOT NULL THEN
      SELECT c.org_page_id, c.archived_at INTO v_org, v_archived
        FROM public.conversations c
       WHERE c.id = NEW.channel_id AND c.kind = 'channel'
       FOR SHARE;
      IF NOT FOUND THEN
        RAISE EXCEPTION 'COMMAND_CHANNEL_NOT_FOUND' USING ERRCODE = 'P0002';
      END IF;
    ELSE
      SELECT sa.organisation_id INTO v_org
        FROM public.service_actors sa WHERE sa.id = NEW.agent_id;
      IF NOT FOUND THEN
        RAISE EXCEPTION 'COMMAND_AGENT_UNAVAILABLE' USING ERRCODE = '22023';
      END IF;
    END IF;

    -- Une ligne déjà archivée ne se réveille pas et ne change plus de contenu.
    -- Le seul changement d'acteur admis est l'effacement du compte
    -- (archived_by → NULL) ; réclamer un archivage système avec un compte
    -- exige la même autorité qu'une désactivation.
    IF OLD.archived_at IS NOT NULL THEN
      IF NEW.description IS DISTINCT FROM OLD.description
         OR NEW.usage IS DISTINCT FROM OLD.usage
         OR NEW.invoker_role IS DISTINCT FROM OLD.invoker_role THEN
        RAISE EXCEPTION 'COMMAND_ARCHIVED' USING ERRCODE = '22023';
      END IF;
      IF NEW.archived_by IS NOT NULL
         AND NEW.archived_by IS DISTINCT FROM OLD.archived_by
         AND NOT (public.is_org_admin(v_org, NEW.archived_by)
                  AND public.is_active_page_actor(v_org)) THEN
        RAISE EXCEPTION 'COMMAND_ADMIN_REQUIRED' USING ERRCODE = '42501';
      END IF;
      RETURN NEW;
    END IF;

    -- L'archive elle-même : un geste d'admin porte son compte et son autorité.
    -- NULL est réservé au geste de l'agent (RPC) et aux transitions système
    -- (grant révoqué, agent supprimé), qui doivent aboutir après coup.
    IF NEW.archived_at IS NOT NULL THEN
      IF NEW.description IS DISTINCT FROM OLD.description
         OR NEW.usage IS DISTINCT FROM OLD.usage
         OR NEW.invoker_role IS DISTINCT FROM OLD.invoker_role THEN
        RAISE EXCEPTION 'COMMAND_IMMUTABLE' USING ERRCODE = '22023';
      END IF;
      IF NEW.archived_by IS NOT NULL
         AND NOT (public.is_org_admin(v_org, NEW.archived_by)
                  AND public.is_active_page_actor(v_org)) THEN
        RAISE EXCEPTION 'COMMAND_ADMIN_REQUIRED' USING ERRCODE = '42501';
      END IF;
      RETURN NEW;
    END IF;

    -- Modification des métadonnées : le grant vivant et l'agent actif sont la
    -- condition, comme à l'enregistrement.
    IF v_archived IS NOT NULL THEN
      RAISE EXCEPTION 'COMMAND_CHANNEL_ARCHIVED' USING ERRCODE = '22023';
    END IF;
    SELECT sa.organisation_id, sa.state INTO v_agent_org, v_state
      FROM public.service_actors sa WHERE sa.id = NEW.agent_id FOR SHARE;
    IF NOT FOUND
       OR v_agent_org IS DISTINCT FROM v_org
       OR v_state <> 'active' THEN
      RAISE EXCEPTION 'COMMAND_AGENT_UNAVAILABLE' USING ERRCODE = '22023';
    END IF;
    IF NOT EXISTS (
      SELECT 1 FROM public.channel_command_grants g
       WHERE g.channel_id = NEW.channel_id
         AND g.agent_id = NEW.agent_id
         AND g.revoked_at IS NULL
    ) THEN
      RAISE EXCEPTION 'COMMAND_GRANT_REQUIRED' USING ERRCODE = '42501';
    END IF;
    RETURN NEW;
  END IF;

  -- INSERT : une commande naît vivante, sous grant vivant, pour un agent
  -- actif de la même organisation. Pré-C1, le canal est relu dans T1.
  IF NEW.archived_at IS NOT NULL OR NEW.archived_by IS NOT NULL THEN
    RAISE EXCEPTION 'COMMAND_IMMUTABLE' USING ERRCODE = '22023';
  END IF;
  IF to_regclass('public.conversations') IS NOT NULL THEN
    SELECT c.org_page_id, c.archived_at INTO v_org, v_archived
      FROM public.conversations c
     WHERE c.id = NEW.channel_id AND c.kind = 'channel'
     FOR UPDATE;
    IF NOT FOUND THEN
      RAISE EXCEPTION 'COMMAND_CHANNEL_NOT_FOUND' USING ERRCODE = 'P0002';
    END IF;
    IF v_archived IS NOT NULL THEN
      RAISE EXCEPTION 'COMMAND_CHANNEL_ARCHIVED' USING ERRCODE = '22023';
    END IF;
  ELSE
    SELECT sa.organisation_id INTO v_org
      FROM public.service_actors sa WHERE sa.id = NEW.agent_id;
    IF NOT FOUND THEN
      RAISE EXCEPTION 'COMMAND_AGENT_UNAVAILABLE' USING ERRCODE = '22023';
    END IF;
  END IF;
  SELECT sa.organisation_id, sa.state INTO v_agent_org, v_state
    FROM public.service_actors sa WHERE sa.id = NEW.agent_id FOR SHARE;
  IF NOT FOUND
     OR v_agent_org IS DISTINCT FROM v_org
     OR v_state <> 'active' THEN
    RAISE EXCEPTION 'COMMAND_AGENT_UNAVAILABLE' USING ERRCODE = '22023';
  END IF;
  IF NOT EXISTS (
    SELECT 1 FROM public.channel_command_grants g
     WHERE g.channel_id = NEW.channel_id
       AND g.agent_id = NEW.agent_id
       AND g.revoked_at IS NULL
  ) THEN
    RAISE EXCEPTION 'COMMAND_GRANT_REQUIRED' USING ERRCODE = '42501';
  END IF;
  RETURN NEW;
END $$;

CREATE FUNCTION public.register_channel_command(
  p_channel          uuid,
  p_agent            uuid,
  p_org              uuid,
  p_name             text,
  p_description      text,
  p_channel_org      uuid,
  p_channel_archived boolean,
  p_usage            text DEFAULT NULL,
  p_invoker_role     text DEFAULT 'member'
)
RETURNS jsonb
LANGUAGE plpgsql
SECURITY DEFINER
SET search_path = public, pg_temp
AS $$
DECLARE
  v_limit   CONSTANT int := 50;
  v_agent_limit CONSTANT int := 20;
  v_org     uuid;
  v_agent_org uuid;
  v_state   text;
  v_name    text;
  v_desc    text;
  v_usage   text;
  v_role    text;
  v_row     public.channel_commands%ROWTYPE;
  v_count   int;
BEGIN
  IF p_channel IS NULL OR p_channel_org IS NULL THEN
    RAISE EXCEPTION 'COMMAND_CHANNEL_NOT_FOUND' USING ERRCODE = 'P0002';
  END IF;
  IF p_agent IS NULL OR p_org IS NULL THEN
    RAISE EXCEPTION 'COMMAND_GRANT_REQUIRED' USING ERRCODE = '42501';
  END IF;

  -- Grammaire, avant toute lecture : mêmes bornes que le CHECK de la table.
  v_name := lower(btrim(p_name));
  IF v_name IS NULL OR v_name !~ '^[a-z][a-z0-9_-]{0,31}$' THEN
    RAISE EXCEPTION 'COMMAND_NAME_INVALID' USING ERRCODE = '22023';
  END IF;
  IF v_name = 'help' THEN
    RAISE EXCEPTION 'COMMAND_NAME_RESERVED' USING ERRCODE = '22023';
  END IF;
  v_desc := btrim(p_description);
  IF v_desc IS NULL OR char_length(v_desc) < 1 OR char_length(v_desc) > 200
     OR position(chr(10) IN v_desc) > 0 OR position(chr(13) IN v_desc) > 0 THEN
    RAISE EXCEPTION 'COMMAND_DESCRIPTION_INVALID' USING ERRCODE = '22023';
  END IF;
  IF p_usage IS NULL THEN
    v_usage := '/' || v_name;
  ELSE
    v_usage := btrim(p_usage);
    IF char_length(v_usage) > 200
       OR position(chr(10) IN v_usage) > 0 OR position(chr(13) IN v_usage) > 0 THEN
      RAISE EXCEPTION 'COMMAND_USAGE_INVALID' USING ERRCODE = '22023';
    END IF;
  END IF;
  v_role := coalesce(p_invoker_role, 'member');
  IF v_role NOT IN ('member', 'admin') THEN
    RAISE EXCEPTION 'COMMAND_ROLE_INVALID' USING ERRCODE = '22023';
  END IF;

  -- Faits contenu bornés : org et archive. Le hub revérifie tout ce qu'il
  -- possède (agent, grant, quotas, audit).
  v_org := p_channel_org;
  IF p_channel_archived IS NOT FALSE THEN
    RAISE EXCEPTION 'COMMAND_CHANNEL_ARCHIVED' USING ERRCODE = '22023';
  END IF;
  SELECT sa.organisation_id, sa.state INTO v_agent_org, v_state
    FROM public.service_actors sa WHERE sa.id = p_agent FOR SHARE;
  IF NOT FOUND
     OR v_agent_org IS DISTINCT FROM p_org
     OR v_agent_org IS DISTINCT FROM v_org
     OR v_state <> 'active' THEN
    RAISE EXCEPTION 'COMMAND_AGENT_UNAVAILABLE' USING ERRCODE = '22023';
  END IF;
  IF NOT EXISTS (
    SELECT 1 FROM public.channel_command_grants g
     WHERE g.channel_id = p_channel
       AND g.agent_id = p_agent
       AND g.revoked_at IS NULL
  ) THEN
    RAISE EXCEPTION 'COMMAND_GRANT_REQUIRED' USING ERRCODE = '42501';
  END IF;

  -- Le verrou consultatif couvre le nom et les deux quotas.
  PERFORM pg_catalog.pg_advisory_xact_lock(
    pg_catalog.hashtextextended('channel_commands:' || p_channel::text, 0)
  );

  SELECT * INTO v_row
    FROM public.channel_commands
   WHERE channel_id = p_channel AND name = v_name AND archived_at IS NULL;
  IF FOUND THEN
    -- Un rejeu strictement identique est idempotent (réponse perdue) ; toute
    -- différence est un conflit de nom, jamais une seconde ligne.
    IF v_row.agent_id = p_agent
       AND v_row.description = v_desc
       AND v_row.usage = v_usage
       AND v_row.invoker_role = v_role THEN
      RETURN to_jsonb(v_row);
    END IF;
    RAISE EXCEPTION 'COMMAND_NAME_TAKEN' USING ERRCODE = '23505';
  END IF;

  SELECT count(*) INTO v_count
    FROM public.channel_commands
   WHERE channel_id = p_channel AND archived_at IS NULL;
  IF v_count >= v_limit THEN
    RAISE EXCEPTION 'COMMAND_CHANNEL_LIMIT: % commandes vivantes', v_count
      USING ERRCODE = '23514';
  END IF;
  SELECT count(*) INTO v_count
    FROM public.channel_commands
   WHERE channel_id = p_channel AND agent_id = p_agent AND archived_at IS NULL;
  IF v_count >= v_agent_limit THEN
    RAISE EXCEPTION 'COMMAND_AGENT_LIMIT: % commandes vivantes pour cet agent', v_count
      USING ERRCODE = '23514';
  END IF;

  BEGIN
    INSERT INTO public.channel_commands
      (channel_id, agent_id, name, description, usage, invoker_role)
    VALUES
      (p_channel, p_agent, v_name, v_desc, v_usage, v_role)
    RETURNING * INTO v_row;
  EXCEPTION
    WHEN unique_violation THEN
      -- La course est fermée par l'index partiel ; on rend le refus nommé.
      RAISE EXCEPTION 'COMMAND_NAME_TAKEN' USING ERRCODE = '23505';
  END;

  PERFORM public.org_audit_record(
    v_org, p_agent, 'service_actor', 'command.registered',
    'channel_command', v_row.id,
    jsonb_build_object('channel_id', p_channel, 'agent_id', p_agent, 'name', v_name)
  );
  RETURN to_jsonb(v_row);
END $$;

CREATE FUNCTION public.update_channel_command(
  p_command          uuid,
  p_agent            uuid,
  p_org              uuid,
  p_channel          uuid,
  p_channel_org      uuid,
  p_channel_archived boolean,
  p_patch            jsonb
)
RETURNS jsonb
LANGUAGE plpgsql
SECURITY DEFINER
SET search_path = public, pg_temp
AS $$
DECLARE
  v_channel  uuid;
  v_org      uuid;
  v_agent_org uuid;
  v_state    text;
  v_row      public.channel_commands%ROWTYPE;
  v_key      text;
  v_desc     text;
  v_usage    text;
  v_role     text;
BEGIN
  IF p_command IS NULL THEN
    RAISE EXCEPTION 'COMMAND_NOT_FOUND' USING ERRCODE = 'P0002';
  END IF;
  IF p_agent IS NULL OR p_org IS NULL THEN
    RAISE EXCEPTION 'COMMAND_GRANT_REQUIRED' USING ERRCODE = '42501';
  END IF;
  IF p_patch IS NULL OR jsonb_typeof(p_patch) <> 'object'
     OR p_patch = '{}'::jsonb
     OR NOT (
       (SELECT array_agg(k) FROM jsonb_object_keys(p_patch) AS k)
       <@ ARRAY['description', 'usage', 'invoker_role']::text[]
     ) THEN
    RAISE EXCEPTION 'COMMAND_PATCH_INVALID' USING ERRCODE = '22023';
  END IF;
  FOR v_key IN SELECT jsonb_object_keys(p_patch) LOOP
    IF jsonb_typeof(p_patch -> v_key) <> 'string' THEN
      RAISE EXCEPTION 'COMMAND_PATCH_INVALID' USING ERRCODE = '22023';
    END IF;
  END LOOP;
  IF p_channel IS NULL OR p_channel_org IS NULL THEN
    RAISE EXCEPTION 'COMMAND_NOT_FOUND' USING ERRCODE = 'P0002';
  END IF;

  SELECT * INTO v_row
    FROM public.channel_commands WHERE id = p_command FOR UPDATE;
  IF NOT FOUND
     OR v_row.channel_id IS DISTINCT FROM p_channel
     OR v_row.agent_id IS DISTINCT FROM p_agent THEN
    RAISE EXCEPTION 'COMMAND_NOT_FOUND' USING ERRCODE = 'P0002';
  END IF;
  IF v_row.archived_at IS NOT NULL THEN
    RAISE EXCEPTION 'COMMAND_NOT_FOUND' USING ERRCODE = 'P0002';
  END IF;
  v_channel := v_row.channel_id;
  v_org := p_channel_org;
  IF p_channel_archived IS NOT FALSE THEN
    RAISE EXCEPTION 'COMMAND_CHANNEL_ARCHIVED' USING ERRCODE = '22023';
  END IF;

  SELECT sa.organisation_id, sa.state INTO v_agent_org, v_state
    FROM public.service_actors sa WHERE sa.id = p_agent FOR SHARE;
  IF NOT FOUND
     OR v_agent_org IS DISTINCT FROM p_org
     OR v_agent_org IS DISTINCT FROM v_org
     OR v_state <> 'active' THEN
    RAISE EXCEPTION 'COMMAND_AGENT_UNAVAILABLE' USING ERRCODE = '22023';
  END IF;
  IF NOT EXISTS (
    SELECT 1 FROM public.channel_command_grants g
     WHERE g.channel_id = v_channel
       AND g.agent_id = p_agent
       AND g.revoked_at IS NULL
  ) THEN
    RAISE EXCEPTION 'COMMAND_GRANT_REQUIRED' USING ERRCODE = '42501';
  END IF;

  v_desc := v_row.description;
  v_usage := v_row.usage;
  v_role := v_row.invoker_role;
  IF p_patch ? 'description' THEN
    v_desc := btrim(p_patch ->> 'description');
    IF v_desc IS NULL OR char_length(v_desc) < 1 OR char_length(v_desc) > 200
       OR position(chr(10) IN v_desc) > 0 OR position(chr(13) IN v_desc) > 0 THEN
      RAISE EXCEPTION 'COMMAND_DESCRIPTION_INVALID' USING ERRCODE = '22023';
    END IF;
  END IF;
  IF p_patch ? 'usage' THEN
    v_usage := btrim(p_patch ->> 'usage');
    IF char_length(v_usage) > 200
       OR position(chr(10) IN v_usage) > 0 OR position(chr(13) IN v_usage) > 0 THEN
      RAISE EXCEPTION 'COMMAND_USAGE_INVALID' USING ERRCODE = '22023';
    END IF;
  END IF;
  IF p_patch ? 'invoker_role' THEN
    v_role := p_patch ->> 'invoker_role';
    IF v_role NOT IN ('member', 'admin') THEN
      RAISE EXCEPTION 'COMMAND_ROLE_INVALID' USING ERRCODE = '22023';
    END IF;
  END IF;

  UPDATE public.channel_commands
     SET description = v_desc,
         usage = v_usage,
         invoker_role = v_role,
         updated_at = clock_timestamp()
   WHERE id = p_command
   RETURNING * INTO v_row;
  RETURN to_jsonb(v_row);
END $$;

CREATE FUNCTION public.archive_agent_command(
  p_command          uuid,
  p_agent            uuid,
  p_org              uuid,
  p_channel          uuid,
  p_channel_org      uuid,
  p_channel_archived boolean
)
RETURNS jsonb
LANGUAGE plpgsql
SECURITY DEFINER
SET search_path = public, pg_temp
AS $$
DECLARE
  v_channel  uuid;
  v_org      uuid;
  v_agent_org uuid;
  v_state    text;
  v_row      public.channel_commands%ROWTYPE;
BEGIN
  IF p_command IS NULL OR p_channel IS NULL OR p_channel_org IS NULL THEN
    RAISE EXCEPTION 'COMMAND_NOT_FOUND' USING ERRCODE = 'P0002';
  END IF;
  IF p_agent IS NULL OR p_org IS NULL THEN
    RAISE EXCEPTION 'COMMAND_GRANT_REQUIRED' USING ERRCODE = '42501';
  END IF;

  SELECT * INTO v_row
    FROM public.channel_commands WHERE id = p_command FOR UPDATE;
  IF NOT FOUND
     OR v_row.channel_id IS DISTINCT FROM p_channel
     OR v_row.agent_id IS DISTINCT FROM p_agent THEN
    RAISE EXCEPTION 'COMMAND_NOT_FOUND' USING ERRCODE = 'P0002';
  END IF;
  v_channel := v_row.channel_id;
  v_org := p_channel_org;

  -- Le principal doit rester celui de la commande, même sur un rejeu d'archive.
  SELECT sa.organisation_id, sa.state INTO v_agent_org, v_state
    FROM public.service_actors sa WHERE sa.id = p_agent FOR SHARE;
  IF NOT FOUND
     OR v_agent_org IS DISTINCT FROM p_org
     OR v_agent_org IS DISTINCT FROM v_org THEN
    RAISE EXCEPTION 'COMMAND_AGENT_UNAVAILABLE' USING ERRCODE = '22023';
  END IF;
  -- Rejeu après une réponse perdue : la ligne est déjà archivée, on la rend
  -- sans exiger le grant (une révocation a pu la fermer entre-temps).
  IF v_row.archived_at IS NOT NULL THEN
    RETURN to_jsonb(v_row);
  END IF;
  IF p_channel_archived IS NOT FALSE THEN
    RAISE EXCEPTION 'COMMAND_CHANNEL_ARCHIVED' USING ERRCODE = '22023';
  END IF;
  IF v_state <> 'active' THEN
    RAISE EXCEPTION 'COMMAND_AGENT_UNAVAILABLE' USING ERRCODE = '22023';
  END IF;
  IF NOT EXISTS (
    SELECT 1 FROM public.channel_command_grants g
     WHERE g.channel_id = v_channel
       AND g.agent_id = p_agent
       AND g.revoked_at IS NULL
  ) THEN
    RAISE EXCEPTION 'COMMAND_GRANT_REQUIRED' USING ERRCODE = '42501';
  END IF;

  UPDATE public.channel_commands
     SET archived_at = clock_timestamp(),
         archived_by = NULL,
         updated_at = clock_timestamp()
   WHERE id = p_command
   RETURNING * INTO v_row;

  PERFORM public.org_audit_record(
    v_org, p_agent, 'service_actor', 'command.archived',
    'channel_command', v_row.id,
    jsonb_build_object('channel_id', v_channel, 'agent_id', p_agent, 'name', v_row.name)
  );
  RETURN to_jsonb(v_row);
END $$;

CREATE FUNCTION public.disable_channel_command(
  p_channel          uuid,
  p_command          uuid,
  p_account          uuid,
  p_channel_org      uuid,
  p_channel_archived boolean
)
RETURNS jsonb
LANGUAGE plpgsql
SECURITY DEFINER
SET search_path = public, pg_temp
AS $$
DECLARE
  v_org      uuid;
  v_row      public.channel_commands%ROWTYPE;
BEGIN
  IF p_channel IS NULL OR p_command IS NULL OR p_channel_org IS NULL THEN
    RAISE EXCEPTION 'COMMAND_NOT_FOUND' USING ERRCODE = 'P0002';
  END IF;
  v_org := public.command_grants_admin_org(p_channel, p_account, p_channel_org);
  IF p_channel_archived IS NOT FALSE THEN
    RAISE EXCEPTION 'COMMAND_CHANNEL_ARCHIVED' USING ERRCODE = '22023';
  END IF;

  SELECT * INTO v_row
    FROM public.channel_commands
   WHERE id = p_command AND channel_id = p_channel
    FOR UPDATE;
  IF NOT FOUND THEN
    RAISE EXCEPTION 'COMMAND_NOT_FOUND' USING ERRCODE = 'P0002';
  END IF;
  IF v_row.archived_at IS NOT NULL THEN
    RETURN to_jsonb(v_row);
  END IF;

  UPDATE public.channel_commands
     SET archived_at = clock_timestamp(),
         archived_by = p_account,
         updated_at = clock_timestamp()
   WHERE id = p_command
   RETURNING * INTO v_row;

  PERFORM public.org_audit_record(
    v_org, p_account, 'account', 'command.archived',
    'channel_command', v_row.id,
    jsonb_build_object(
      'channel_id', p_channel,
      'agent_id', v_row.agent_id,
      'name', v_row.name
    )
  );
  RETURN to_jsonb(v_row);
END $$;

CREATE FUNCTION public.channel_commands_list(
  p_channel            uuid,
  p_account            uuid,
  p_channel_org        uuid,
  p_channel_archived   boolean,
  p_channel_visibility text,
  p_member             boolean
)
RETURNS TABLE (
  id           uuid,
  name         text,
  description  text,
  usage        text,
  invoker_role text,
  agent_id     uuid,
  agent_name   text
)
LANGUAGE plpgsql
STABLE
SECURITY DEFINER
SET search_path = public, pg_temp
AS $$
DECLARE
  v_org    uuid;
  v_public text;
  v_ok     boolean;
BEGIN
  IF p_channel IS NULL OR p_account IS NULL OR p_channel_org IS NULL THEN
    RAISE EXCEPTION 'COMMAND_CHANNEL_NOT_FOUND' USING ERRCODE = 'P0002';
  END IF;
  v_public := p_channel_visibility;
  IF v_public IS NULL OR v_public NOT IN ('public', 'private') THEN
    -- Fail-closed : une visibilité inconnue ne s'ouvre pas.
    RAISE EXCEPTION 'COMMAND_CHANNEL_NOT_FOUND' USING ERRCODE = 'P0002';
  END IF;
  v_org := p_channel_org;

  v_ok := p_member IS TRUE
          OR (v_public = 'public'
              AND (public.is_active_org_member(v_org, p_account)
                   OR public.is_org_admin(v_org, p_account)));
  IF NOT v_ok THEN
    RAISE EXCEPTION 'COMMAND_CHANNEL_NOT_FOUND' USING ERRCODE = 'P0002';
  END IF;
  IF p_channel_archived IS NOT FALSE THEN
    RETURN;
  END IF;

  RETURN QUERY
    SELECT cc.id, cc.name, cc.description, cc.usage, cc.invoker_role,
           sa.id, sa.name
      FROM public.channel_commands cc
      JOIN public.service_actors sa ON sa.id = cc.agent_id
      JOIN public.channel_command_grants g
        ON g.channel_id = cc.channel_id
       AND g.agent_id = cc.agent_id
       AND g.revoked_at IS NULL
     WHERE cc.channel_id = p_channel
       AND cc.archived_at IS NULL
       AND sa.organisation_id = v_org
       AND sa.state = 'active'
       AND public.is_active_page_actor(v_org)
     ORDER BY cc.name, cc.id;
END $$;

-- Sortie sans titre de canal : le backend hydrate (une lecture batch).
CREATE FUNCTION public.agent_channel_commands_v2(
  p_agent uuid,
  p_org   uuid,
  p_limit int DEFAULT 200,
  p_offset int DEFAULT 0
)
RETURNS TABLE (
  id           uuid,
  channel_id   uuid,
  name         text,
  description  text,
  usage        text,
  invoker_role text,
  created_at   timestamptz,
  updated_at   timestamptz
)
LANGUAGE sql
STABLE
SECURITY DEFINER
SET search_path = public, pg_temp
AS $$
  SELECT cc.id, cc.channel_id, cc.name, cc.description, cc.usage,
         cc.invoker_role, cc.created_at, cc.updated_at
    FROM public.channel_commands cc
    JOIN public.service_actors sa ON sa.id = cc.agent_id
    JOIN public.channel_command_grants g
      ON g.channel_id = cc.channel_id
     AND g.agent_id = cc.agent_id
     AND g.revoked_at IS NULL
   WHERE cc.agent_id = p_agent
     AND sa.organisation_id = p_org
     AND sa.state = 'active'
     AND public.is_active_page_actor(p_org)
     AND cc.archived_at IS NULL
   ORDER BY cc.created_at DESC, cc.id
   LIMIT greatest(0, least(coalesce(p_limit, 200), 200))
   OFFSET greatest(0, least(coalesce(p_offset, 0), 1000000));
$$;

-- Résout la commande de l'agent vers son canal, pour que le backend lise les
-- faits du bon canal avant update/archive. Ne rend rien d'autre que l'id.
CREATE FUNCTION public.channel_command_target(
  p_command uuid,
  p_agent   uuid,
  p_org     uuid
)
RETURNS uuid
LANGUAGE plpgsql STABLE SECURITY DEFINER SET search_path = public, pg_temp AS $$
DECLARE
  v_channel uuid;
BEGIN
  IF p_command IS NULL OR p_agent IS NULL OR p_org IS NULL THEN
    RAISE EXCEPTION 'COMMAND_NOT_FOUND' USING ERRCODE = 'P0002';
  END IF;
  SELECT cc.channel_id INTO v_channel
    FROM public.channel_commands cc
    JOIN public.service_actors sa ON sa.id = cc.agent_id
   WHERE cc.id = p_command
     AND cc.agent_id = p_agent
     AND sa.organisation_id = p_org
     AND sa.state = 'active'
     AND public.is_active_page_actor(p_org);
  IF NOT FOUND THEN
    RAISE EXCEPTION 'COMMAND_NOT_FOUND' USING ERRCODE = 'P0002';
  END IF;
  RETURN v_channel;
END $$;

-- =========================================================================
-- 3. 651 — invocations : intention, file, poll, claim, réponse, réparation,
--    lectures, annulation à l'archive
-- =========================================================================

CREATE FUNCTION public.channel_command_invocation_intent(
  p_channel          uuid,
  p_account          uuid,
  p_name             text,
  p_request_key      text,
  p_channel_org      uuid,
  p_channel_archived boolean,
  p_member           boolean
)
RETURNS jsonb
LANGUAGE plpgsql
SECURITY DEFINER
SET search_path = public, pg_temp
AS $$
DECLARE
  v_existing public.channel_command_invocations%ROWTYPE;
  v_row      public.channel_command_invocations%ROWTYPE;
  v_name     text;
  v_org      uuid;
  v_command  public.channel_commands%ROWTYPE;
  v_agent_org uuid;
  v_state    text;
BEGIN
  IF p_channel IS NULL OR p_account IS NULL OR p_channel_org IS NULL THEN
    RAISE EXCEPTION 'COMMAND_CHANNEL_NOT_FOUND' USING ERRCODE = 'P0002';
  END IF;
  IF p_request_key IS NULL
     OR p_request_key !~ '^[ -~]{8,128}$'
     OR p_request_key <> btrim(p_request_key) THEN
    RAISE EXCEPTION 'COMMAND_REQUEST_KEY_INVALID' USING ERRCODE = '22023';
  END IF;
  v_name := lower(btrim(coalesce(p_name, '')));
  IF v_name !~ '^[a-z][a-z0-9_-]{0,31}$' OR v_name = 'help' THEN
    RAISE EXCEPTION 'COMMAND_NOT_FOUND' USING ERRCODE = 'P0002';
  END IF;

  -- Sérialise les rejeux de la même clé, y compris entre deux canaux.
  PERFORM pg_catalog.pg_advisory_xact_lock(
    pg_catalog.hashtextextended(
      'channel_command_invocation:' || p_account::text || ':' || p_request_key, 0));

  SELECT * INTO v_existing
    FROM public.channel_command_invocations
   WHERE invoker_account_id = p_account
     AND request_key = p_request_key
   ORDER BY created_at, id
   LIMIT 1;
  IF FOUND THEN
    -- Rejeu avant `p_member` (décision pilote #217764) : seule l'intention
    -- existante de l'appelant est rendue, sans effet nouveau ni autre donnée.
    IF v_existing.channel_id IS DISTINCT FROM p_channel
       OR v_existing.command_name IS DISTINCT FROM v_name THEN
      RAISE EXCEPTION 'COMMAND_REQUEST_KEY_CONFLICT' USING ERRCODE = '23505';
    END IF;
    PERFORM public.channel_command_invocation_expire_one(v_existing.id);
    SELECT * INTO v_existing
      FROM public.channel_command_invocations WHERE id = v_existing.id;
    RETURN jsonb_build_object(
      'invocation_id', v_existing.id,
      'command_id', v_existing.command_id,
      'channel_id', v_existing.channel_id,
      'command_name', v_existing.command_name,
      'message_id', v_existing.message_id,
      'status', v_existing.status,
      'expires_at', v_existing.expires_at
    );
  END IF;

  v_org := p_channel_org;
  -- Invoquer exige d'être participant actif ; un non-participant d'un canal
  -- privé reçoit 404, comme la découverte (pas de révélation d'existence).
  -- Ce test précède celui de l'archive : sinon un non-participant d'un canal
  -- privé archivé apprendrait son existence (COMMAND_CHANNEL_ARCHIVED).
  IF p_member IS NOT TRUE THEN
    RAISE EXCEPTION 'COMMAND_CHANNEL_NOT_FOUND' USING ERRCODE = 'P0002';
  END IF;
  IF p_channel_archived IS NOT FALSE THEN
    RAISE EXCEPTION 'COMMAND_CHANNEL_ARCHIVED' USING ERRCODE = '22023';
  END IF;
  IF NOT public.is_active_page_actor(v_org) THEN
    RAISE EXCEPTION 'COMMAND_UNAVAILABLE' USING ERRCODE = '22023';
  END IF;

  SELECT * INTO v_command
    FROM public.channel_commands cc
   WHERE cc.channel_id = p_channel
     AND cc.name = v_name
     AND cc.archived_at IS NULL;
  IF NOT FOUND THEN
    RAISE EXCEPTION 'COMMAND_NOT_FOUND' USING ERRCODE = 'P0002';
  END IF;

  SELECT sa.organisation_id, sa.state INTO v_agent_org, v_state
    FROM public.service_actors sa
   WHERE sa.id = v_command.agent_id;
  IF NOT FOUND
     OR v_agent_org IS DISTINCT FROM v_org
     OR v_state <> 'active'
     OR NOT EXISTS (
       SELECT 1 FROM public.channel_command_grants g
        WHERE g.channel_id = p_channel
          AND g.agent_id = v_command.agent_id
          AND g.revoked_at IS NULL
     ) THEN
    RAISE EXCEPTION 'COMMAND_UNAVAILABLE' USING ERRCODE = '22023';
  END IF;

  IF v_command.invoker_role = 'admin'
     AND NOT public.is_org_admin(v_org, p_account) THEN
    RAISE EXCEPTION 'COMMAND_ADMIN_REQUIRED' USING ERRCODE = '42501';
  END IF;

  INSERT INTO public.channel_command_invocations (
    command_id, channel_id, agent_id, command_name, invoker_account_id,
    request_key, status, expires_at
  ) VALUES (
    v_command.id, p_channel, v_command.agent_id, v_command.name, p_account,
    p_request_key, 'intent', clock_timestamp() + interval '24 hours'
  )
  RETURNING * INTO v_row;

  RETURN jsonb_build_object(
    'invocation_id', v_row.id,
    'command_id', v_row.command_id,
    'channel_id', v_row.channel_id,
    'command_name', v_row.command_name,
    'message_id', v_row.message_id,
    'status', v_row.status,
    'expires_at', v_row.expires_at
  );
END $$;

-- Le message est désormais sur le contenu : la vérification stricte
-- (canal, auteur, client_id, non supprimé) appartient au service ; l'index
-- unique `(conversation_id, client_id)` de la cible reste la borne.
CREATE FUNCTION public.channel_command_invocation_queued_v2(
  p_invocation uuid,
  p_account    uuid,
  p_message    uuid,
  p_client_id  text
)
RETURNS jsonb
LANGUAGE plpgsql
SECURITY DEFINER
SET search_path = public, pg_temp
AS $$
DECLARE
  v_row public.channel_command_invocations%ROWTYPE;
BEGIN
  IF p_invocation IS NULL OR p_account IS NULL OR p_message IS NULL
     OR p_client_id IS NULL
     OR p_client_id !~ '^[0-9a-f]{64}$' THEN
    RAISE EXCEPTION 'COMMAND_MESSAGE_INVALID' USING ERRCODE = '22023';
  END IF;

  SELECT * INTO v_row
    FROM public.channel_command_invocations
   WHERE id = p_invocation
   FOR UPDATE;
  IF NOT FOUND OR v_row.invoker_account_id IS DISTINCT FROM p_account THEN
    RAISE EXCEPTION 'COMMAND_INVOCATION_NOT_FOUND' USING ERRCODE = 'P0002';
  END IF;

  IF v_row.status = 'intent' THEN
    IF v_row.message_id IS NOT NULL THEN
      RAISE EXCEPTION 'COMMAND_INVOCATION_MESSAGE_IMMUTABLE' USING ERRCODE = '22023';
    END IF;
    UPDATE public.channel_command_invocations
       SET status = 'queued',
           message_id = p_message,
           expires_at = clock_timestamp() + interval '30 seconds'
     WHERE id = p_invocation
       AND status = 'intent'
     RETURNING * INTO v_row;
    IF NOT FOUND THEN
      SELECT * INTO v_row
        FROM public.channel_command_invocations WHERE id = p_invocation;
    END IF;
  ELSIF v_row.message_id IS NOT NULL AND v_row.message_id <> p_message THEN
    RAISE EXCEPTION 'COMMAND_INVOCATION_MESSAGE_CONFLICT' USING ERRCODE = '23505';
  END IF;

  RETURN jsonb_build_object(
    'invocation_id', v_row.id,
    'message_id', v_row.message_id,
    'status', v_row.status,
    'expires_at', v_row.expires_at
  );
END $$;

-- Poll sans titre de canal : le backend l'hydrate depuis le contenu.
CREATE FUNCTION public.agent_command_invocations_poll_v2(
  p_agent uuid,
  p_org   uuid,
  p_limit integer DEFAULT 20
)
RETURNS SETOF jsonb
LANGUAGE plpgsql
SECURITY DEFINER
SET search_path = public, pg_temp
AS $$
BEGIN
  PERFORM public.channel_command_agent_guard(p_agent, p_org);

  PERFORM public.channel_command_invocations_expire_filtered(
    p_agent, NULL, NULL, 500);

  RETURN QUERY
    SELECT jsonb_build_object(
      'invocation_id', i.id,
      'command_id', i.command_id,
      'command_name', i.command_name,
      'channel_id', i.channel_id,
      'invoker', jsonb_build_object(
        'id', i.invoker_account_id,
        'name', p.display_name
      ),
      'message_id', i.message_id,
      'status', i.status,
      'attempts', i.attempts,
      'expires_at', i.expires_at,
      'created_at', i.created_at
    )
      FROM public.channel_command_invocations i
      LEFT JOIN public.profiles p ON p.account_id = i.invoker_account_id
     WHERE i.agent_id = p_agent
       AND i.status IN ('queued', 'claimed', 'responding')
       AND i.message_id IS NOT NULL
       AND i.expires_at > clock_timestamp()
     ORDER BY i.created_at, i.id
     LIMIT greatest(1, least(coalesce(p_limit, 20), 50));
END $$;

CREATE FUNCTION public.agent_command_invocation_claim(
  p_invocation       uuid,
  p_agent            uuid,
  p_org              uuid,
  p_channel_org      uuid,
  p_channel_archived boolean
)
RETURNS jsonb
LANGUAGE plpgsql
SECURITY DEFINER
SET search_path = public, pg_temp
AS $$
DECLARE
  v_row   public.channel_command_invocations%ROWTYPE;
  v_live  boolean;
BEGIN
  IF p_invocation IS NULL THEN
    RAISE EXCEPTION 'COMMAND_INVOCATION_NOT_FOUND' USING ERRCODE = 'P0002';
  END IF;
  PERFORM public.channel_command_agent_guard(p_agent, p_org);

  SELECT * INTO v_row
    FROM public.channel_command_invocations
   WHERE id = p_invocation
   FOR UPDATE;
  IF NOT FOUND OR v_row.agent_id IS DISTINCT FROM p_agent THEN
    RAISE EXCEPTION 'COMMAND_INVOCATION_NOT_FOUND' USING ERRCODE = 'P0002';
  END IF;

  -- L'échéance s'applique dans la même transaction que le claim. Une ligne qui
  -- ne peut pas être claimée est **rendue** (jamais un RAISE après une mise à
  -- jour : l'exception annulerait l'expiration et laisserait la file polluée).
  PERFORM public.channel_command_invocation_expire_one(v_row.id);
  SELECT * INTO v_row
    FROM public.channel_command_invocations WHERE id = v_row.id;

  IF v_row.status <> 'queued' OR v_row.attempts >= 2 THEN
    RETURN jsonb_build_object(
      'invocation_id', v_row.id,
      'status', v_row.status,
      'attempts', v_row.attempts,
      'error_code', v_row.error_code
    );
  END IF;

  SELECT EXISTS (
    SELECT 1
      FROM public.channel_commands cc
      JOIN public.service_actors sa ON sa.id = cc.agent_id
      JOIN public.channel_command_grants g
        ON g.channel_id = cc.channel_id
       AND g.agent_id = cc.agent_id
       AND g.revoked_at IS NULL
     WHERE cc.id = v_row.command_id
       AND cc.channel_id = v_row.channel_id
       AND cc.agent_id = v_row.agent_id
       AND cc.archived_at IS NULL
       AND p_channel_org IS NOT NULL
       AND p_channel_org = p_org
       AND p_channel_archived IS FALSE
       AND sa.organisation_id = p_org
       AND sa.state = 'active'
       AND public.is_active_page_actor(p_org)
  ) INTO v_live;
  IF NOT v_live THEN
    UPDATE public.channel_command_invocations
       SET status = 'cancelled',
           error_code = 'cancelled',
           finished_at = clock_timestamp(),
           claim_id = NULL,
           claimed_at = NULL
     WHERE id = v_row.id
       AND status IN ('intent', 'queued', 'claimed');
    RETURN jsonb_build_object(
      'invocation_id', v_row.id,
      'status', 'cancelled',
      'attempts', v_row.attempts,
      'error_code', 'cancelled'
    );
  END IF;

  UPDATE public.channel_command_invocations
     SET status = 'claimed',
         attempts = attempts + 1,
         claim_id = gen_random_uuid(),
         claimed_at = clock_timestamp(),
         expires_at = clock_timestamp() + interval '60 seconds'
   WHERE id = v_row.id
     AND status = 'queued'
     AND attempts < 2
   RETURNING * INTO v_row;
  IF NOT FOUND THEN
    -- Le verrou de ligne interdit cette branche ; elle rend l'état courant au
    -- lieu de lever après coup.
    SELECT * INTO v_row
      FROM public.channel_command_invocations WHERE id = p_invocation;
    RETURN jsonb_build_object(
      'invocation_id', v_row.id,
      'status', v_row.status,
      'attempts', v_row.attempts,
      'error_code', v_row.error_code
    );
  END IF;

  RETURN jsonb_build_object(
    'invocation_id', v_row.id,
    'status', v_row.status,
    'claim_id', v_row.claim_id,
    'expires_at', v_row.expires_at,
    'attempts', v_row.attempts
  );
END $$;

CREATE FUNCTION public.agent_command_invocation_begin_response(
  p_invocation       uuid,
  p_agent            uuid,
  p_org              uuid,
  p_claim            uuid,
  p_channel_org      uuid,
  p_channel_archived boolean
)
RETURNS jsonb
LANGUAGE plpgsql
SECURITY DEFINER
SET search_path = public, pg_temp
AS $$
DECLARE
  v_row   public.channel_command_invocations%ROWTYPE;
  v_live  boolean;
BEGIN
  PERFORM public.channel_command_agent_guard(p_agent, p_org);
  SELECT * INTO v_row
    FROM public.channel_command_invocations
   WHERE id = p_invocation AND agent_id = p_agent
   FOR UPDATE;
  IF NOT FOUND THEN
    RAISE EXCEPTION 'COMMAND_INVOCATION_NOT_FOUND' USING ERRCODE = 'P0002';
  END IF;

  -- Pas d'expiration paresseuse ici : la réservation ne doit pas être
  -- requeuee par une fonction qui peut ensuite refuser le claim (l'exception
  -- annulerait la mise à jour). Le get/poll/balayage l'applique avant.
  IF v_row.status = 'claimed'
     AND v_row.claim_id = p_claim
     AND v_row.expires_at > clock_timestamp() THEN
    SELECT EXISTS (
      SELECT 1
        FROM public.channel_commands cc
        JOIN public.service_actors sa ON sa.id = cc.agent_id
        JOIN public.channel_command_grants g
          ON g.channel_id = cc.channel_id
         AND g.agent_id = cc.agent_id
         AND g.revoked_at IS NULL
       WHERE cc.id = v_row.command_id
         AND cc.channel_id = v_row.channel_id
         AND cc.agent_id = v_row.agent_id
         AND cc.archived_at IS NULL
         AND p_channel_org IS NOT NULL
         AND p_channel_org = p_org
         AND p_channel_archived IS FALSE
         AND sa.organisation_id = p_org
         AND sa.state = 'active'
         AND public.is_active_page_actor(p_org)
    ) INTO v_live;
    IF NOT v_live THEN
      UPDATE public.channel_command_invocations
         SET status = 'cancelled',
             error_code = 'cancelled',
             finished_at = clock_timestamp(),
             claim_id = NULL,
             claimed_at = NULL
       WHERE id = v_row.id
         AND status IN ('intent', 'queued', 'claimed');
      -- Mise à jour puis retour, jamais un RAISE : l'annulation doit survivre
      -- à la réponse 409 de S5.
      RETURN jsonb_build_object(
        'invocation_id', v_row.id,
        'status', 'cancelled',
        'claim_id', NULL,
        'response_message_id', NULL,
        'expires_at', v_row.expires_at
      );
    END IF;

    UPDATE public.channel_command_invocations
       SET status = 'responding',
           response_message_id = gen_random_uuid()
     WHERE id = v_row.id
       AND status = 'claimed'
       AND claim_id = p_claim
     RETURNING * INTO v_row;
    IF NOT FOUND THEN
      RAISE EXCEPTION 'COMMAND_CLAIM_STALE' USING ERRCODE = '22023';
    END IF;
  ELSIF v_row.status = 'responding'
        AND v_row.claim_id = p_claim
        AND v_row.expires_at > clock_timestamp() THEN
    -- Deux respond du même claim retrouvent la même réservation.
    NULL;
  ELSIF v_row.status = 'answered' AND v_row.claim_id = p_claim THEN
    -- Reçu déjà clos : S5 vérifie d'abord le message et rend le texte.
    NULL;
  ELSE
    RAISE EXCEPTION 'COMMAND_CLAIM_STALE' USING ERRCODE = '22023';
  END IF;

  RETURN jsonb_build_object(
    'invocation_id', v_row.id,
    'status', v_row.status,
    'claim_id', v_row.claim_id,
    'response_message_id', v_row.response_message_id,
    'expires_at', v_row.expires_at
  );
END $$;

CREATE FUNCTION public.channel_command_invocation_repair(
  p_invocation       uuid,
  p_channel_org      uuid,
  p_channel_archived boolean,
  p_message_found    boolean,
  p_message_id       uuid DEFAULT NULL
)
RETURNS jsonb
LANGUAGE plpgsql
SECURITY DEFINER
SET search_path = public, pg_temp
AS $$
DECLARE
  v_row  public.channel_command_invocations%ROWTYPE;
  v_live boolean;
BEGIN
  IF p_message_found AND p_message_id IS NULL THEN
    RAISE EXCEPTION 'COMMAND_MESSAGE_INVALID' USING ERRCODE = '22023';
  END IF;
  SELECT * INTO v_row
    FROM public.channel_command_invocations
   WHERE id = p_invocation
   FOR UPDATE;
  IF NOT FOUND THEN
    RAISE EXCEPTION 'COMMAND_INVOCATION_NOT_FOUND' USING ERRCODE = 'P0002';
  END IF;
  -- Un message écrit après l'annulation est relié à la ligne terminale
  -- (spec §4.4) : le reçu, et lui seul, est posé ; l'état ne bouge pas.
  IF v_row.status IN ('cancelled', 'expired', 'failed') THEN
    IF p_message_found AND v_row.response_message_id IS NULL THEN
      UPDATE public.channel_command_invocations
         SET response_message_id = p_message_id
       WHERE id = v_row.id
         AND response_message_id IS NULL
         AND status IN ('cancelled', 'expired', 'failed')
       RETURNING * INTO v_row;
    END IF;
    RETURN jsonb_build_object(
      'invocation_id', v_row.id,
      'status', v_row.status,
      'response_message_id', v_row.response_message_id
    );
  END IF;
  IF v_row.status <> 'responding' THEN
    RETURN jsonb_build_object(
      'invocation_id', v_row.id,
      'status', v_row.status,
      'response_message_id', v_row.response_message_id
    );
  END IF;
  -- La réparation n'est pas un geste de worker : elle ne reprend la
  -- réservation qu'à l'échéance du bail, jamais pendant qu'un writer vit.
  IF v_row.expires_at > clock_timestamp() THEN
    RETURN jsonb_build_object(
      'invocation_id', v_row.id,
      'status', v_row.status,
      'response_message_id', v_row.response_message_id
    );
  END IF;

  SELECT EXISTS (
    SELECT 1
      FROM public.channel_commands cc
      JOIN public.service_actors sa ON sa.id = cc.agent_id
      JOIN public.channel_command_grants g
        ON g.channel_id = cc.channel_id
       AND g.agent_id = cc.agent_id
       AND g.revoked_at IS NULL
     WHERE cc.id = v_row.command_id
       AND cc.channel_id = v_row.channel_id
       AND cc.agent_id = v_row.agent_id
       AND cc.archived_at IS NULL
       AND p_channel_org IS NOT NULL
       AND p_channel_org = sa.organisation_id
       AND p_channel_archived IS FALSE
       AND sa.state = 'active'
       AND public.is_active_page_actor(p_channel_org)
  ) INTO v_live;

  IF NOT v_live THEN
    -- Révocation/désactivation/canal fermé : la réponse réservée ne repart
    -- pas en file ; un message déjà écrit est relié à la ligne terminale.
    UPDATE public.channel_command_invocations
       SET status = 'cancelled',
           error_code = 'cancelled',
           finished_at = clock_timestamp(),
           response_message_id = CASE WHEN p_message_found
                                      THEN p_message_id
                                      ELSE NULL END,
           claim_id = NULL,
           claimed_at = NULL
     WHERE id = v_row.id AND status = 'responding'
     RETURNING * INTO v_row;
  ELSIF p_message_found THEN
    UPDATE public.channel_command_invocations
       SET status = 'answered',
           response_message_id = p_message_id,
           error_code = NULL,
           finished_at = clock_timestamp()
     WHERE id = v_row.id AND status = 'responding'
     RETURNING * INTO v_row;
  ELSIF v_row.attempts < 2 THEN
    UPDATE public.channel_command_invocations
       SET status = 'queued',
           response_message_id = NULL,
           claim_id = NULL,
           claimed_at = NULL,
           expires_at = clock_timestamp() + interval '30 seconds'
     WHERE id = v_row.id AND status = 'responding'
     RETURNING * INTO v_row;
  ELSE
    UPDATE public.channel_command_invocations
       SET status = 'expired',
           error_code = 'lease_expired',
           finished_at = clock_timestamp(),
           response_message_id = NULL,
           claim_id = NULL,
           claimed_at = NULL
     WHERE id = v_row.id AND status = 'responding'
     RETURNING * INTO v_row;
  END IF;

  RETURN jsonb_build_object(
    'invocation_id', v_row.id,
    'status', v_row.status,
    'response_message_id', v_row.response_message_id,
    'expires_at', v_row.expires_at
  );
END $$;

CREATE FUNCTION public.channel_command_invocation_get(
  p_channel     uuid,
  p_invocation  uuid,
  p_account     uuid,
  p_channel_org uuid
)
RETURNS jsonb
LANGUAGE plpgsql
SECURITY DEFINER
SET search_path = public, pg_temp
AS $$
DECLARE
  v_row   public.channel_command_invocations%ROWTYPE;
  v_org   uuid;
  v_agent_ok boolean;
  v_ok    boolean;
BEGIN
  IF p_channel_org IS NULL THEN
    RAISE EXCEPTION 'COMMAND_INVOCATION_NOT_FOUND' USING ERRCODE = 'P0002';
  END IF;
  SELECT * INTO v_row
    FROM public.channel_command_invocations
   WHERE id = p_invocation AND channel_id = p_channel;
  IF NOT FOUND THEN
    RAISE EXCEPTION 'COMMAND_INVOCATION_NOT_FOUND' USING ERRCODE = 'P0002';
  END IF;
  v_org := p_channel_org;

  -- Croisement du fait contenu : un agent encore connu du hub doit appartenir
  -- à l'organisation du canal. Une invocation survit à la suppression de son
  -- agent (aucune FK) : un agent absent ne porte plus d'organisation et garde
  -- la lecture administrateur d'avant C1 (auditeur, B #218916).
  SELECT NOT EXISTS (
    SELECT 1 FROM public.service_actors sa
     WHERE sa.id = v_row.agent_id
       AND sa.organisation_id IS DISTINCT FROM v_org) INTO v_agent_ok;

  v_ok := v_row.invoker_account_id = p_account
          OR (public.is_org_admin(v_org, p_account)
              AND public.is_active_page_actor(v_org)
              AND v_agent_ok);
  -- Un auteur effacé laisse invoker_account_id NULL : la comparaison vaut
  -- NULL et `IF NOT v_ok` ne refuserait pas (NULL n'est pas vrai). Seul un
  -- booléen vrai ouvre le statut ; un tiers, même authentifié, reste en 404.
  IF v_ok IS NOT TRUE THEN
    RAISE EXCEPTION 'COMMAND_INVOCATION_NOT_FOUND' USING ERRCODE = 'P0002';
  END IF;

  PERFORM public.channel_command_invocation_expire_one(v_row.id);
  SELECT * INTO v_row
    FROM public.channel_command_invocations WHERE id = v_row.id;

  RETURN jsonb_build_object(
    'invocation_id', v_row.id,
    'command_id', v_row.command_id,
    'command_name', v_row.command_name,
    'channel_id', v_row.channel_id,
    'status', v_row.status,
    'attempts', v_row.attempts,
    'message_id', v_row.message_id,
    'response_message_id', v_row.response_message_id,
    'error_code', v_row.error_code,
    'created_at', v_row.created_at,
    'updated_at', v_row.updated_at,
    'expires_at', v_row.expires_at,
    'finished_at', v_row.finished_at
  );
END $$;

CREATE FUNCTION public.channel_command_invocations_list(
  p_channel     uuid,
  p_account     uuid,
  p_channel_org uuid,
  p_mine        boolean DEFAULT true,
  p_limit       integer DEFAULT 20
)
RETURNS SETOF jsonb
LANGUAGE plpgsql
SECURITY DEFINER
SET search_path = public, pg_temp
AS $$
DECLARE
  v_org uuid;
BEGIN
  IF p_channel_org IS NULL THEN
    RAISE EXCEPTION 'COMMAND_CHANNEL_NOT_FOUND' USING ERRCODE = 'P0002';
  END IF;
  v_org := p_channel_org;
  -- Croisement du fait contenu avant toute expiration paresseuse : aucune
  -- invocation du canal ne doit venir d'un agent connu d'une autre
  -- organisation. Le refus précède `expire_filtered`, qui ne touche donc pas
  -- un canal étranger (auditeur, B #218916).
  IF NOT coalesce(p_mine, true)
     AND NOT (public.is_org_admin(v_org, p_account)
              AND public.is_active_page_actor(v_org)
              AND NOT EXISTS (
                SELECT 1
                  FROM public.channel_command_invocations i
                  JOIN public.service_actors sa ON sa.id = i.agent_id
                 WHERE i.channel_id = p_channel
                   AND sa.organisation_id IS DISTINCT FROM v_org)) THEN
    RAISE EXCEPTION 'COMMAND_INVOCATION_FORBIDDEN' USING ERRCODE = '42501';
  END IF;

  PERFORM public.channel_command_invocations_expire_filtered(
    NULL, p_channel, NULL, 500);

  RETURN QUERY
    SELECT jsonb_build_object(
      'invocation_id', i.id,
      'command_id', i.command_id,
      'command_name', i.command_name,
      'channel_id', i.channel_id,
      'status', i.status,
      'attempts', i.attempts,
      'message_id', i.message_id,
      'response_message_id', i.response_message_id,
      'error_code', i.error_code,
      'created_at', i.created_at,
      'updated_at', i.updated_at,
      'expires_at', i.expires_at,
      'finished_at', i.finished_at
    )
      FROM public.channel_command_invocations i
     WHERE i.channel_id = p_channel
       AND (NOT coalesce(p_mine, true) OR i.invoker_account_id = p_account)
     ORDER BY i.created_at DESC, i.id
     LIMIT greatest(1, least(coalesce(p_limit, 20), 50));
END $$;

-- Remplace le trigger T1 `..._cancel_on_channel_archive` : appelée par
-- `OrgChannelsService.setArchived` juste après une archive réussie, en
-- best-effort (le chemin paresseux claim/begin/repair reste le filet).
CREATE FUNCTION public.channel_command_invocations_cancel_channel(
  p_channel uuid
)
RETURNS integer
LANGUAGE plpgsql
SECURITY DEFINER
SET search_path = public, pg_temp
AS $$
DECLARE
  v_count integer;
BEGIN
  IF p_channel IS NULL THEN
    RETURN 0;
  END IF;
  UPDATE public.channel_command_invocations
     SET status = 'cancelled',
         error_code = 'cancelled',
         finished_at = clock_timestamp(),
         claim_id = NULL,
         claimed_at = NULL
   WHERE channel_id = p_channel
     AND status IN ('intent', 'queued', 'claimed');
  GET DIAGNOSTICS v_count = ROW_COUNT;
  RETURN v_count;
END $$;

-- =========================================================================
-- 4. ACL : service_role seulement, aucun rôle client
-- =========================================================================
REVOKE ALL ON FUNCTION
  public.command_grants_admin_org(uuid, uuid, uuid),
  public.channel_command_agents(uuid, uuid, uuid, int, int),
  public.set_channel_command_grant(uuid, uuid, uuid, boolean, uuid, boolean),
  public.agent_command_channels_v2(uuid, uuid, int, int),
  public.register_channel_command(uuid, uuid, uuid, text, text, uuid, boolean, text, text),
  public.update_channel_command(uuid, uuid, uuid, uuid, uuid, boolean, jsonb),
  public.archive_agent_command(uuid, uuid, uuid, uuid, uuid, boolean),
  public.disable_channel_command(uuid, uuid, uuid, uuid, boolean),
  public.channel_commands_list(uuid, uuid, uuid, boolean, text, boolean),
  public.agent_channel_commands_v2(uuid, uuid, int, int),
  public.channel_command_target(uuid, uuid, uuid),
  public.channel_command_invocation_intent(uuid, uuid, text, text, uuid, boolean, boolean),
  public.channel_command_invocation_queued_v2(uuid, uuid, uuid, text),
  public.agent_command_invocations_poll_v2(uuid, uuid, integer),
  public.agent_command_invocation_claim(uuid, uuid, uuid, uuid, boolean),
  public.agent_command_invocation_begin_response(uuid, uuid, uuid, uuid, uuid, boolean),
  public.channel_command_invocation_repair(uuid, uuid, boolean, boolean, uuid),
  public.channel_command_invocation_get(uuid, uuid, uuid, uuid),
  public.channel_command_invocations_list(uuid, uuid, uuid, boolean, integer),
  public.channel_command_invocations_cancel_channel(uuid)
  FROM PUBLIC, anon, authenticated;
GRANT EXECUTE ON FUNCTION
  public.channel_command_agents(uuid, uuid, uuid, int, int),
  public.set_channel_command_grant(uuid, uuid, uuid, boolean, uuid, boolean),
  public.agent_command_channels_v2(uuid, uuid, int, int),
  public.register_channel_command(uuid, uuid, uuid, text, text, uuid, boolean, text, text),
  public.update_channel_command(uuid, uuid, uuid, uuid, uuid, boolean, jsonb),
  public.archive_agent_command(uuid, uuid, uuid, uuid, uuid, boolean),
  public.disable_channel_command(uuid, uuid, uuid, uuid, boolean),
  public.channel_commands_list(uuid, uuid, uuid, boolean, text, boolean),
  public.agent_channel_commands_v2(uuid, uuid, int, int),
  public.channel_command_target(uuid, uuid, uuid),
  public.channel_command_invocation_intent(uuid, uuid, text, text, uuid, boolean, boolean),
  public.channel_command_invocation_queued_v2(uuid, uuid, uuid, text),
  public.agent_command_invocations_poll_v2(uuid, uuid, integer),
  public.agent_command_invocation_claim(uuid, uuid, uuid, uuid, boolean),
  public.agent_command_invocation_begin_response(uuid, uuid, uuid, uuid, uuid, boolean),
  public.channel_command_invocation_repair(uuid, uuid, boolean, boolean, uuid),
  public.channel_command_invocation_get(uuid, uuid, uuid, uuid),
  public.channel_command_invocations_list(uuid, uuid, uuid, boolean, integer),
  public.channel_command_invocations_cancel_channel(uuid)
  TO service_role;

COMMIT;

