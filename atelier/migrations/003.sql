CREATE TABLE comparison_votes (
 id TEXT PRIMARY KEY, advisor_id TEXT NOT NULL, card_id TEXT NOT NULL,
 source_run TEXT NOT NULL, outcome TEXT NOT NULL, choice_id TEXT,
 display_order TEXT NOT NULL, reason TEXT NOT NULL, created_at TEXT NOT NULL,
 UNIQUE(advisor_id, card_id)
);
