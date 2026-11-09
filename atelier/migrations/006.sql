CREATE TABLE conversation_imports (
 id TEXT PRIMARY KEY,
 customer_id TEXT NOT NULL REFERENCES customers(id),
 raw_text TEXT NOT NULL,
 confirmed_json TEXT NOT NULL,
 event_ids TEXT NOT NULL,
 created_at TEXT NOT NULL
);
