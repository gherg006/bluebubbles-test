-- Store file notices as encrypted messages and point them at UUID-addressed blobs.
BEGIN;

ALTER TABLE messages
    ADD COLUMN IF NOT EXISTS message_type TEXT NOT NULL DEFAULT 'text',
    ADD COLUMN IF NOT EXISTS attachment_uuid UUID;

ALTER TABLE messages DROP CONSTRAINT IF EXISTS messages_message_type_check;
ALTER TABLE messages
    ADD CONSTRAINT messages_message_type_check
    CHECK (message_type IN ('text', 'file'));

CREATE UNIQUE INDEX IF NOT EXISTS messages_attachment_uuid_unique_idx
    ON messages (attachment_uuid) WHERE attachment_uuid IS NOT NULL;

COMMIT;
