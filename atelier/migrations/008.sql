-- Ordinary customer memories are independently identified notes. Their key is
-- a topic label; only operational controls and authored rule IDs are slots.
CREATE TABLE memories_next (
 id TEXT PRIMARY KEY, scope TEXT NOT NULL CHECK(scope IN ('customer','advisor')),
 owner_id TEXT NOT NULL, key TEXT NOT NULL, value TEXT NOT NULL,
 provenance TEXT NOT NULL, source TEXT NOT NULL, source_event_id TEXT REFERENCES events(id),
 effective_at TEXT NOT NULL, expires_at TEXT, applicability TEXT NOT NULL,
 status TEXT NOT NULL DEFAULT 'active' CHECK(status IN ('active','disabled','deleted')),
 revision INTEGER NOT NULL DEFAULT 1, human_locked INTEGER NOT NULL DEFAULT 0,
 created_at TEXT NOT NULL, updated_at TEXT NOT NULL, source_quote TEXT,
 kind TEXT NOT NULL DEFAULT 'fact' CHECK(kind IN ('fact','rule','example'))
);
INSERT INTO memories_next SELECT * FROM memories;
DROP TABLE memories;
ALTER TABLE memories_next RENAME TO memories;
CREATE UNIQUE INDEX memories_slots ON memories(scope,owner_id,key)
 WHERE scope='advisor' OR kind!='fact'
 OR key IN ('contact_permission','service_permission','contact_after','contact_reason')
 OR key LIKE 'customer_note.%';
CREATE INDEX memories_owner_status ON memories(scope,owner_id,status);
