CREATE TABLE jobs (
 id TEXT PRIMARY KEY, kind TEXT NOT NULL, payload TEXT NOT NULL,
 status TEXT NOT NULL, result TEXT, error TEXT, created_at TEXT NOT NULL,
 started_at TEXT, finished_at TEXT
);
CREATE TABLE memory_extractions (
 event_id TEXT PRIMARY KEY REFERENCES events(id), status TEXT NOT NULL,
 record TEXT NOT NULL, created_at TEXT NOT NULL
);
CREATE TABLE followups (
 id TEXT PRIMARY KEY, customer_id TEXT NOT NULL REFERENCES customers(id),
 title TEXT NOT NULL, request TEXT NOT NULL, due_at TEXT NOT NULL,
 status TEXT NOT NULL DEFAULT 'scheduled', last_review_id TEXT REFERENCES reviews(id),
 last_fingerprint TEXT, last_error TEXT, updated_at TEXT NOT NULL
);
CREATE TABLE draft_edits (
 id INTEGER PRIMARY KEY, draft_id TEXT NOT NULL REFERENCES drafts(id),
 candidate_id TEXT NOT NULL, text TEXT NOT NULL, created_at TEXT NOT NULL
);
CREATE TABLE settings (key TEXT PRIMARY KEY, value TEXT NOT NULL);
ALTER TABLE reviews ADD COLUMN fingerprint TEXT;
CREATE INDEX reviews_customer_date ON reviews(customer_id, created_at);
