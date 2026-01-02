-- A positive selection flag makes the reusable ResearchReport explicit.
-- is_stale remains for backwards compatibility with existing artifact types.
ALTER TABLE planning_artifacts
    ADD COLUMN IF NOT EXISTS is_current BOOLEAN NOT NULL DEFAULT TRUE;

UPDATE planning_artifacts
SET is_current = NOT is_stale
WHERE is_current IS DISTINCT FROM NOT is_stale;

CREATE INDEX IF NOT EXISTS idx_planning_artifacts_current_research_report
    ON planning_artifacts (trip_id, version DESC, id DESC)
    WHERE artifact_type = 'research_report' AND is_current = TRUE;
