-- 0006_authority_fixes — les sept trous des sondes de codex3 sur le gel 345aca6.
--
-- R4 :
--   * la consommation d'une approbation passe d'une colonne par ligne à une
--     table (approver, nonce) : réinsérer le reçu sous un nouvel id ne le
--     rejoue plus, et le reçu de consommation survit à la suppression de la
--     ligne d'approbation ;
--   * l'unicité du nonce est aussi garantie par un index unique.
-- R3 :
--   * `current_prompt` : la consigne est déplacée, pas effacée, quand le tour
--     démarre. Si l'exécuteur meurt avant la fin, le prochain détenteur du bail
--     la retrouve (R5 : rien n'est perdu), au prix d'un tour rejoué au pire.

alter table agent_registry
    add column if not exists current_prompt text;

create unique index if not exists mesh_approvals_nonce_idx
    on mesh_approvals (approver, nonce);

create table if not exists mesh_consumed_nonces (
    approver    text not null,
    nonce       text not null,
    approval_id bigint,
    consumed_by text,
    consumed_at timestamptz not null default now(),
    primary key (approver, nonce)
);

create index if not exists mesh_consumed_nonces_approval_idx
    on mesh_consumed_nonces (approval_id);
