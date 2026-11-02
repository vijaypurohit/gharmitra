-- Mock provider schema. Only mock_api/provider.py reads or writes it.
-- Free slots are generated from the seed, so there is no slots table; holds and bookings take them.

create table providers (
    id          text primary key,
    name        text not null,
    service     text not null,
    areas       text[] not null,
    price       bigint not null,
    rating      numeric(2, 1) not null,
    attributes  jsonb not null default '[]',
    phone       text not null,
    address     text not null
);

create table reviews (
    id           serial primary key,
    provider_id  text not null references providers(id),
    author       text not null,
    rating       integer not null,
    text         text not null
);

create sequence hold_seq;
create sequence booking_seq start 1001;

create table holds (
    id            text primary key,
    provider_id   text not null references providers(id),
    user_id       text not null,
    slot_start    timestamptz not null,
    quoted_price  bigint not null,
    expires_at    timestamptz not null,
    used          boolean not null default false
);

create table bookings (
    id               text primary key,
    user_id          text not null,
    provider_id      text not null references providers(id),
    service          text not null,
    slot_start       timestamptz not null,
    status           text not null check (status in ('booked', 'cancelled', 'completed', 'provider_cancelled')),
    idempotency_key  text unique,
    created_at       timestamptz not null
);

-- First definite result per idempotency key (book, cancel, reschedule); replayed on repeats.
create table idempotency (
    key         text primary key,
    op          text not null check (op in ('book', 'cancel', 'reschedule')),
    status      text not null check (status in ('booked', 'failed', 'cancelled', 'rescheduled')),
    result      jsonb not null,
    created_at  timestamptz not null
);

-- Counts cancel calls per booking, for the "first call fails with 503" rule.
create table cancel_calls (
    booking_id  text primary key,
    calls       integer not null default 0
);
