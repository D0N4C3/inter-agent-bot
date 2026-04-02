create extension if not exists "pgcrypto";

-- Run this in your existing Supabase schema context (for example `public`),
-- or set `search_path` before executing if you use a custom schema.
create table if not exists agent_applications (
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
  status text not null default 'Submitted' check (status in ('Submitted', 'Under Review', 'Approved', 'Rejected', 'More Information Required')),
  submitted_at timestamptz not null default now()
);

create index if not exists idx_agent_applications_telegram_user
  on agent_applications (telegram_user_id);

create index if not exists idx_agent_applications_phone
  on agent_applications (phone);

create table if not exists territories (
  territory_id uuid primary key default gen_random_uuid(),
  region text not null,
  zone text not null,
  woreda text not null,
  kebele text not null,
  village text not null,
  is_locked boolean not null default false,
  assigned_application_id uuid references agent_applications(application_id)
);

create unique index if not exists uq_territories_location
  on territories(region, zone, woreda, kebele, village);
