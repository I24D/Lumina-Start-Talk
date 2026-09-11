-- Supabase schema for Lumina Start Talk's private memory document.
-- Apply this file through a trusted migration connection, never from the app.

create table if not exists public.lumina_state_documents (
    workspace_id text not null,
    scope text not null,
    document_key text not null,
    payload jsonb not null default '{}'::jsonb,
    updated_at timestamptz not null default now(),
    primary key (workspace_id, scope, document_key)
);

comment on table public.lumina_state_documents is
    'Private JSONB state documents shared by trusted Lumina services.';
comment on column public.lumina_state_documents.workspace_id is
    'Stable identifier for the Lumina application that owns the document.';
comment on column public.lumina_state_documents.scope is
    'Logical state namespace, such as memory.';
comment on column public.lumina_state_documents.document_key is
    'Versioned key within the application and scope.';
comment on column public.lumina_state_documents.payload is
    'Application-owned JSONB document.';
comment on column public.lumina_state_documents.updated_at is
    'Last successful document write time.';

alter table public.lumina_state_documents enable row level security;

revoke all privileges on table public.lumina_state_documents
    from public, anon, authenticated, service_role;
grant select, insert, update, delete
    on table public.lumina_state_documents to service_role;

drop policy if exists service_role_only on public.lumina_state_documents;
create policy service_role_only
    on public.lumina_state_documents
    for all
    to service_role
    using (true)
    with check (true);

-- ── Conversation history ─────────────────────────────────────────────────────
-- public.interaction_log already existed and was never written to: one row per
-- finished turn is what memory/conversation_log.py now appends, so that asking
-- "do you remember what we worked out yesterday?" has something to read.
--
-- Recall has to work in Spanish, and Spanish is where a plain ILIKE gives up:
-- the model asks about "computacion cuantica" while the stored turn says
-- "computación cuántica", the two never meet, and Lumina reports forgetting
-- something she is holding. unaccent() folds both sides, and it runs in the
-- database because the rows that need folding are the ones still on the server.

create extension if not exists unaccent;

create or replace function public.lumina_search_interactions(
    p_user_id text,
    p_query   text,
    p_limit   int default 12
)
returns table (user_message text, reply text, created_at timestamptz)
language sql
stable
security definer
set search_path = public, extensions
as $$
    select i.user_message, i.reply, i.created_at
      from public.interaction_log i
     where i.user_id = p_user_id
       and (
             p_query is null
          or btrim(p_query) = ''
          or unaccent(coalesce(i.user_message, '')) ilike '%' || unaccent(p_query) || '%'
          or unaccent(coalesce(i.reply, ''))        ilike '%' || unaccent(p_query) || '%'
       )
     order by i.created_at desc
     limit greatest(1, least(coalesce(p_limit, 12), 50));
$$;

comment on function public.lumina_search_interactions(text, text, int) is
    'Accent-insensitive search over Lumina''s conversation history.';

revoke all on function public.lumina_search_interactions(text, text, int)
    from public, anon, authenticated;
grant execute on function public.lumina_search_interactions(text, text, int)
    to service_role;
