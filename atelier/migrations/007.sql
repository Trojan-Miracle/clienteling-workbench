-- Advisor-confirmed facts for this draft, distinct from wording instructions.
ALTER TABLE rewrite_requests ADD COLUMN confirmed_facts TEXT NOT NULL DEFAULT '';
