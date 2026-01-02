-- Durable retry protection for a single submitted collection message.
-- Existing clients omit this optional value and remain compatible.
CREATE UNIQUE INDEX IF NOT EXISTS idx_messages_trip_client_message_id
ON messages (trip_id, (meta->>'client_message_id'))
WHERE meta ? 'client_message_id';
