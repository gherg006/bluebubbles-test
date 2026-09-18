-- One-time migration for an existing BlueBubbles database.
--
-- This preserves all message data. PostgreSQL also preserves the column's
-- constraints and updates dependent indexes when a column is renamed.
-- The conditional block makes the script safe to retain as a backup after it
-- has been applied.

BEGIN;

DO $$
BEGIN
    IF EXISTS (
        SELECT 1
        FROM information_schema.columns
        WHERE table_schema = current_schema()
          AND table_name = 'messages'
          AND column_name = 'reciepient_id'
    ) AND NOT EXISTS (
        SELECT 1
        FROM information_schema.columns
        WHERE table_schema = current_schema()
          AND table_name = 'messages'
          AND column_name = 'recipient_id'
    ) THEN
        ALTER TABLE messages RENAME COLUMN reciepient_id TO recipient_id;
    END IF;
END $$;

COMMIT;
