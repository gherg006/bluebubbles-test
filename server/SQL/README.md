# Database SQL backup

`bluebubbles_database.sql` is a self-contained schema backup for the database
used by the server. It documents all tables, relationships, and indexes the
current server code needs: `users`, `encryption_keys`, `messages`, and
`chat_contacts`.

It is not imported, run, or otherwise referenced by the server. It also has no
data, credentials, or connection settings, so keeping it in Git cannot alter
the live database.

For a database created before the `recipient_id` spelling correction, run
`001_rename_reciepient_id_to_recipient_id.sql` once before deploying the
matching server code. It changes only the column name and preserves all data,
foreign keys, and indexes.
