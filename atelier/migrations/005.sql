ALTER TABLE memories ADD COLUMN kind TEXT NOT NULL DEFAULT 'fact'
    CHECK(kind IN ('fact','rule','example'));
UPDATE memories SET kind=CASE
    WHEN scope='customer' THEN 'fact'
    WHEN key LIKE 'example.%' THEN 'example'
    WHEN EXISTS(SELECT 1 FROM feedback f WHERE f.memory_id=memories.id AND f.learning_scope='rule') THEN 'rule'
    WHEN EXISTS(SELECT 1 FROM feedback f WHERE f.memory_id=memories.id) THEN 'example'
    WHEN provenance='inferred' THEN 'example'
    ELSE 'rule' END;
