-- Agent schema. Runs with search_path set to the agent schema, so names are unqualified.
-- Money is bigint paise; times are timestamptz.

create table users (
    id          text primary key,
    name        text not null,
    created_at  timestamptz not null default now()
);

create table sessions (
    id          text primary key,
    user_id     text not null references users(id),
    phase       text not null,
    task        jsonb not null default '{}',
    turns       integer not null default 0,
    created_at  timestamptz not null,
    updated_at  timestamptz not null
);

create table messages (
    id          bigserial primary key,
    session_id  text not null references sessions(id),
    role        text not null check (role in ('user', 'agent')),
    text        text not null,
    created_at  timestamptz not null
);

create sequence approval_seq;

create table approvals (
    id                  text primary key,
    session_id          text not null references sessions(id),
    user_id             text not null references users(id),
    action              text not null check (action in ('book', 'cancel', 'reschedule')),
    provider_id         text not null,
    provider_name       text not null,
    slot_start          timestamptz not null,
    amount              bigint not null,
    booking_id          text,
    hold_id             text,
    fingerprint         text not null,
    status              text not null
                        check (status in ('pending', 'granted', 'consumed', 'voided', 'expired')),
    summary             text not null,
    expires_at          timestamptz not null,
    created_at          timestamptz not null,
    granted_at          timestamptz,
    granted_by_message  bigint references messages(id),
    status_reason       text
);

-- One approval -> one operation -> at most one side effect.
create table operations (
    op_key       text primary key,
    approval_id  text not null unique references approvals(id),
    session_id   text not null references sessions(id),
    action       text not null check (action in ('book', 'cancel', 'reschedule')),
    status       text not null check (status in ('started', 'unknown', 'succeeded', 'failed')),
    calls        integer not null default 0,
    booking_id   text,
    error        text,
    created_at   timestamptz not null,
    updated_at   timestamptz not null
);

-- The agent's copy of each booking, with the price and policy it was made under.
create table bookings (
    id             text primary key,
    user_id        text not null references users(id),
    provider_id    text not null,
    provider_name  text not null,
    service        text not null,
    area           text,
    slot_start     timestamptz not null,
    price          bigint not null,
    status         text not null check (status in ('booked', 'cancelled', 'completed', 'provider_cancelled')),
    policy         jsonb not null default '{}',
    op_key         text,
    created_at     timestamptz not null,
    updated_at     timestamptz not null
);

create unique index bookings_one_active_per_slot
    on bookings (user_id, provider_id, slot_start) where status = 'booked';

create table refunds (
    id          bigserial primary key,
    booking_id  text not null references bookings(id),
    amount      bigint not null,
    fee         bigint not null default 0,
    credit      bigint not null default 0,
    op_key      text,
    created_at  timestamptz not null
);

create table receipts (
    receipt_no  text primary key,
    op_key      text not null unique,
    kind        text not null check (kind in ('book', 'cancel', 'reschedule')),
    data        jsonb not null,
    created_at  timestamptz not null
);

create table facts (
    user_id     text not null references users(id),
    key         text not null,
    value       text,
    status      text not null check (status in ('stated', 'inferred', 'forgotten')),
    updated_at  timestamptz not null,
    primary key (user_id, key),
    check ((status = 'forgotten') = (value is null))
);

-- Audit of memory changes. Never holds the value.
create table fact_events (
    id          bigserial primary key,
    user_id     text not null,
    key         text not null,
    op          text not null check (op in ('state', 'infer', 'confirm', 'correct', 'forget')),
    created_at  timestamptz not null
);

create table trace_events (
    session_id  text not null,
    seq         integer not null,
    ts          timestamptz not null,
    type        text not null,
    payload     jsonb not null,
    primary key (session_id, seq)
);

create table settings (
    key         text primary key,
    value       jsonb not null,
    updated_at  timestamptz not null
);
