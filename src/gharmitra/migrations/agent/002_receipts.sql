-- Receipt numbers, and receipts for refunds when a provider cancels.
create sequence receipt_seq start 1;
alter table receipts drop constraint receipts_kind_check;
alter table receipts add constraint receipts_kind_check
    check (kind in ('book', 'cancel', 'reschedule', 'provider_cancel'));
alter table receipts add column booking_id text;
alter table receipts add column user_id text;
create index receipts_booking on receipts (booking_id);
