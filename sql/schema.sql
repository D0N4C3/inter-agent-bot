create schema if not exists inter_agent_apply;

create extension if not exists "pgcrypto";

grant usage on schema inter_agent_apply to anon, authenticated, service_role;
grant create on schema inter_agent_apply to service_role;

create table if not exists inter_agent_apply.agent_applications (
  application_id uuid primary key default gen_random_uuid(),
  telegram_user_id text not null,
  full_name text not null,
  phone text not null,
  applicant_type text not null check (applicant_type in ('sales_only', 'installer_only', 'sales_installer')),
  region text not null,
  zone text not null,
  woreda text not null,
  kebele text not null,
  village text not null,
  experience boolean not null,
  experience_years integer not null default 0,
  work_type text not null,
  has_shop boolean not null,
  can_install boolean not null,
  preferred_territory text not null,
  id_file_front_url text not null,
  id_file_back_url text not null,
  profile_photo_url text,
  qualification_score integer not null,
  qualification_flag text not null,
  agent_tag text not null default 'Hybrid' check (agent_tag in ('Sales Agent', 'Installer Agent', 'Hybrid')),
  performance_potential text not null default 'Medium' check (performance_potential in ('High', 'Medium', 'Low')),
  status text not null default 'Submitted' check (status in ('Submitted', 'Under Review', 'Approved', 'Rejected', 'More Info Required')),
  admin_notes text,
  internal_remarks text,
  submitted_at timestamptz not null default now()
);

alter table inter_agent_apply.agent_applications
  add column if not exists agent_tag text not null default 'Hybrid';
alter table inter_agent_apply.agent_applications
  add column if not exists performance_potential text not null default 'Medium';
alter table inter_agent_apply.agent_applications
  add column if not exists internal_remarks text;

create index if not exists idx_agent_applications_telegram_user
  on inter_agent_apply.agent_applications (telegram_user_id);

create index if not exists idx_agent_applications_phone
  on inter_agent_apply.agent_applications (phone);

create index if not exists idx_agent_applications_status
  on inter_agent_apply.agent_applications (status);

create table if not exists inter_agent_apply.territories (
  territory_id uuid primary key default gen_random_uuid(),
  region text not null,
  zone text not null,
  woreda text not null,
  kebele text not null,
  village text not null,
  latitude double precision,
  longitude double precision,
  availability_status text not null default 'open' check (availability_status in ('open', 'assigned', 'blocked')),
  is_locked boolean not null default false,
  assigned_application_id uuid references inter_agent_apply.agent_applications(application_id)
);

create unique index if not exists uq_territories_location
  on inter_agent_apply.territories(region, zone, woreda, kebele, village);

create unique index if not exists uq_territories_assigned_application
  on inter_agent_apply.territories(assigned_application_id)
  where assigned_application_id is not null;

alter table inter_agent_apply.territories
  add column if not exists latitude double precision;
alter table inter_agent_apply.territories
  add column if not exists longitude double precision;
alter table inter_agent_apply.territories
  add column if not exists availability_status text not null default 'open';

create table if not exists inter_agent_apply.bot_admins (
  admin_id uuid primary key default gen_random_uuid(),
  telegram_user_id text not null unique,
  created_by text,
  created_at timestamptz not null default now()
);

create table if not exists inter_agent_apply.application_drafts (
  draft_id uuid primary key default gen_random_uuid(),
  telegram_user_id text not null unique,
  applicant_type text not null check (applicant_type in ('sales_only', 'installer_only', 'sales_installer')),
  language text not null default 'en',
  step_index integer not null default 0,
  answers jsonb not null default '{}'::jsonb,
  reminder_sent_at timestamptz,
  updated_at timestamptz not null default now()
);

create table if not exists inter_agent_apply.agent_performance_events (
  event_id uuid primary key default gen_random_uuid(),
  application_id uuid not null references inter_agent_apply.agent_applications(application_id) on delete cascade,
  event_type text not null check (event_type in ('sale_closed', 'installer_job_completed', 'training_completed')),
  event_value numeric not null default 0,
  metadata jsonb not null default '{}'::jsonb,
  occurred_at timestamptz not null default now(),
  created_at timestamptz not null default now()
);

create index if not exists idx_agent_performance_events_application
  on inter_agent_apply.agent_performance_events(application_id, occurred_at desc);

create table if not exists inter_agent_apply.agent_training_progress (
  progress_id uuid primary key default gen_random_uuid(),
  application_id uuid not null references inter_agent_apply.agent_applications(application_id) on delete cascade,
  module_key text not null,
  completed boolean not null default false,
  completed_at timestamptz,
  created_at timestamptz not null default now(),
  updated_at timestamptz not null default now()
);

create unique index if not exists uq_agent_training_progress_module
  on inter_agent_apply.agent_training_progress(application_id, module_key);

create or replace function inter_agent_apply.set_application_draft_updated_at()
returns trigger as $$
begin
  new.updated_at = now();
  return new;
end;
$$ language plpgsql;

drop trigger if exists trg_application_drafts_updated_at on inter_agent_apply.application_drafts;
create trigger trg_application_drafts_updated_at
before update on inter_agent_apply.application_drafts
for each row execute function inter_agent_apply.set_application_draft_updated_at();

create or replace function inter_agent_apply.set_training_progress_updated_at()
returns trigger as $$
begin
  new.updated_at = now();
  return new;
end;
$$ language plpgsql;

drop trigger if exists trg_agent_training_progress_updated_at on inter_agent_apply.agent_training_progress;
create trigger trg_agent_training_progress_updated_at
before update on inter_agent_apply.agent_training_progress
for each row execute function inter_agent_apply.set_training_progress_updated_at();

create or replace function inter_agent_apply.get_stale_application_drafts(cutoff_hours integer default 24)
returns setof inter_agent_apply.application_drafts
language sql
as $$
  select *
  from inter_agent_apply.application_drafts
  where updated_at < (now() - make_interval(hours => cutoff_hours))
    and (reminder_sent_at is null or reminder_sent_at < updated_at);
$$;

create or replace function inter_agent_apply.top_sales_agents(result_limit integer default 10)
returns table (
  application_id uuid,
  full_name text,
  phone text,
  total_sales numeric
)
language sql
as $$
  select
    app.application_id,
    app.full_name,
    app.phone,
    coalesce(sum(ev.event_value), 0) as total_sales
  from inter_agent_apply.agent_applications app
  left join inter_agent_apply.agent_performance_events ev
    on ev.application_id = app.application_id and ev.event_type = 'sale_closed'
  group by app.application_id, app.full_name, app.phone
  order by total_sales desc, app.submitted_at desc
  limit greatest(result_limit, 1);
$$;

create or replace function inter_agent_apply.top_installer_agents(result_limit integer default 10)
returns table (
  application_id uuid,
  full_name text,
  phone text,
  completed_jobs numeric
)
language sql
as $$
  select
    app.application_id,
    app.full_name,
    app.phone,
    coalesce(sum(ev.event_value), 0) as completed_jobs
  from inter_agent_apply.agent_applications app
  left join inter_agent_apply.agent_performance_events ev
    on ev.application_id = app.application_id and ev.event_type = 'installer_job_completed'
  group by app.application_id, app.full_name, app.phone
  order by completed_jobs desc, app.submitted_at desc
  limit greatest(result_limit, 1);
$$;

grant select, insert, update, delete on all tables in schema inter_agent_apply to anon, authenticated, service_role;
grant usage, select on all sequences in schema inter_agent_apply to anon, authenticated, service_role;
grant execute on function inter_agent_apply.top_sales_agents(integer) to anon, authenticated, service_role;
grant execute on function inter_agent_apply.top_installer_agents(integer) to anon, authenticated, service_role;

alter default privileges for role postgres in schema inter_agent_apply
  grant select, insert, update, delete on tables to anon, authenticated, service_role;

alter default privileges for role postgres in schema inter_agent_apply
  grant usage, select on sequences to anon, authenticated, service_role;
