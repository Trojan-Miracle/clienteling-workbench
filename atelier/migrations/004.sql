ALTER TABLE memories ADD COLUMN source_quote TEXT;
ALTER TABLE feedback ADD COLUMN selected_text TEXT;
ALTER TABLE feedback ADD COLUMN learning_scope TEXT NOT NULL DEFAULT 'example';
ALTER TABLE feedback ADD COLUMN rule_text TEXT NOT NULL DEFAULT '';
ALTER TABLE feedback ADD COLUMN applicability TEXT NOT NULL DEFAULT '';
ALTER TABLE feedback ADD COLUMN revision INTEGER NOT NULL DEFAULT 1;
ALTER TABLE feedback ADD COLUMN updated_at TEXT;
ALTER TABLE feedback ADD COLUMN memory_id TEXT REFERENCES memories(id);
ALTER TABLE feedback ADD COLUMN memory_revision INTEGER;
CREATE TABLE feedback_audit (
 id INTEGER PRIMARY KEY, feedback_id TEXT NOT NULL REFERENCES feedback(id),
 revision INTEGER NOT NULL, before_json TEXT, after_json TEXT NOT NULL,
 created_at TEXT NOT NULL, UNIQUE(feedback_id, revision)
);
CREATE TABLE rewrite_requests (
 id TEXT PRIMARY KEY, advisor_id TEXT NOT NULL, parent_draft_id TEXT NOT NULL REFERENCES drafts(id),
 instruction TEXT NOT NULL, candidate_id TEXT, edited_text TEXT, as_of TEXT NOT NULL,
 created_at TEXT NOT NULL
);
