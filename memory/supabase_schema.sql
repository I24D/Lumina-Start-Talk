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
