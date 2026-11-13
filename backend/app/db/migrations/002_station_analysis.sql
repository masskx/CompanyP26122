-- Superseded: version 0.3 kept station configuration and its own analysis batches /
-- records / events here. Since 0.4 the main server schedules analyses and owns their
-- results, and this database stores only conversations and the work-order copy
-- (Question.txt). Migration 003 drops those tables where they were created.
SELECT 1;
