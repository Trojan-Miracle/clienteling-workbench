CREATE TABLE customers (
 id TEXT PRIMARY KEY, name TEXT NOT NULL, salutation TEXT NOT NULL,
 summary TEXT NOT NULL, color TEXT NOT NULL, created_at TEXT NOT NULL
);
CREATE TABLE events (
 id TEXT PRIMARY KEY, customer_id TEXT NOT NULL REFERENCES customers(id),
 occurred_at TEXT NOT NULL, kind TEXT NOT NULL, actor TEXT NOT NULL,
 text TEXT NOT NULL, data TEXT NOT NULL, recorded_at TEXT NOT NULL
);
CREATE INDEX events_customer_time ON events(customer_id, occurred_at, id);
CREATE TABLE memories (
 id TEXT PRIMARY KEY, scope TEXT NOT NULL CHECK(scope IN ('customer','advisor')),
 owner_id TEXT NOT NULL, key TEXT NOT NULL, value TEXT NOT NULL,
 provenance TEXT NOT NULL, source TEXT NOT NULL, source_event_id TEXT REFERENCES events(id),
 effective_at TEXT NOT NULL, expires_at TEXT, applicability TEXT NOT NULL,
 status TEXT NOT NULL DEFAULT 'active' CHECK(status IN ('active','disabled','deleted')),
 revision INTEGER NOT NULL DEFAULT 1, human_locked INTEGER NOT NULL DEFAULT 0,
 created_at TEXT NOT NULL, updated_at TEXT NOT NULL,
 UNIQUE(scope, owner_id, key)
);
CREATE TABLE memory_audit (
 id INTEGER PRIMARY KEY, memory_id TEXT NOT NULL REFERENCES memories(id),
 revision INTEGER NOT NULL, action TEXT NOT NULL, before_json TEXT, after_json TEXT NOT NULL,
 created_at TEXT NOT NULL, UNIQUE(memory_id, revision)
);
CREATE TABLE drafts (
 id TEXT PRIMARY KEY, customer_id TEXT REFERENCES customers(id),
 status TEXT NOT NULL, created_at TEXT NOT NULL, record TEXT NOT NULL
);
CREATE TABLE feedback (
 id TEXT PRIMARY KEY, advisor_id TEXT NOT NULL, draft_id TEXT NOT NULL REFERENCES drafts(id),
 mode TEXT NOT NULL CHECK(mode IN ('calibration','blind')),
 outcome TEXT NOT NULL, candidate_id TEXT, display_order TEXT NOT NULL,
 reason TEXT NOT NULL, created_at TEXT NOT NULL
);
CREATE TABLE questions (
 advisor_id TEXT NOT NULL, question_id TEXT NOT NULL, state TEXT NOT NULL,
 answer TEXT, asked_at TEXT NOT NULL, answered_at TEXT,
 PRIMARY KEY(advisor_id, question_id)
);
CREATE TABLE reviews (
 id TEXT PRIMARY KEY, customer_id TEXT NOT NULL REFERENCES customers(id),
 as_of TEXT NOT NULL, decision TEXT NOT NULL, record TEXT NOT NULL, created_at TEXT NOT NULL
);
