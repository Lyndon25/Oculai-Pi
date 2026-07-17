-- Upgrade existing Oculai databases to the durable DAG/runtime schema.
-- This migration is transactional when applied by either migration runner.

-- ---------------------------------------------------------------------------
-- Runtime tables that were added to the canonical schema after the desktop
-- baseline had already shipped.
-- ---------------------------------------------------------------------------
CREATE TABLE IF NOT EXISTS TaskIteration (
    iteration_id       UUID PRIMARY KEY DEFAULT gen_random_uuid(),
    task_id            UUID NOT NULL REFERENCES Task(task_id) ON DELETE CASCADE,
    iteration_number   INTEGER NOT NULL,
    iteration_type     TEXT NOT NULL,
    reasoning_text     TEXT,
    action_taken       TEXT,
    action_params      JSONB DEFAULT '{}',
    observation_text   TEXT,
    observation_data   JSONB DEFAULT '{}',
    decision           TEXT,
    decision_rationale TEXT,
    created_at         TIMESTAMPTZ NOT NULL DEFAULT now(),
    UNIQUE (task_id, iteration_number)
);

CREATE TABLE IF NOT EXISTS AgentBroadcast (
    broadcast_id   UUID PRIMARY KEY DEFAULT gen_random_uuid(),
    run_id         UUID NOT NULL REFERENCES SourcingRun(run_id) ON DELETE CASCADE,
    discovery_type TEXT NOT NULL,
    content        TEXT NOT NULL,
    discovered_by  TEXT NOT NULL,
    consumed_by    TEXT[] DEFAULT '{}',
    created_at     TIMESTAMPTZ NOT NULL DEFAULT now()
);

CREATE TABLE IF NOT EXISTS SearchRoundState (
    round_id          UUID PRIMARY KEY DEFAULT gen_random_uuid(),
    run_id            UUID NOT NULL REFERENCES SourcingRun(run_id) ON DELETE CASCADE,
    hypothesis_id     TEXT NOT NULL,
    source_name       TEXT NOT NULL,
    round_number      INTEGER NOT NULL,
    query_used        JSONB NOT NULL DEFAULT '{}',
    results_count     INTEGER NOT NULL DEFAULT 0,
    verified_count    INTEGER NOT NULL DEFAULT 0,
    persisted_count   INTEGER NOT NULL DEFAULT 0,
    signal_quality    REAL,
    result_diversity  REAL,
    is_saturated      BOOLEAN DEFAULT false,
    terminated_reason TEXT,
    created_at        TIMESTAMPTZ NOT NULL DEFAULT now(),
    UNIQUE (run_id, hypothesis_id, source_name, round_number)
);

DO $$ BEGIN
    CREATE DOMAIN review_pass_t AS TEXT
        CHECK (VALUE IN ('enrichment','initial_scoring','audit','adjustment','complete'));
EXCEPTION WHEN duplicate_object THEN NULL;
END $$;

CREATE TABLE IF NOT EXISTS ReviewSession (
    session_id              UUID PRIMARY KEY DEFAULT gen_random_uuid(),
    run_id                  UUID NOT NULL REFERENCES SourcingRun(run_id) ON DELETE CASCADE,
    status                  TEXT NOT NULL DEFAULT 'active'
                                CHECK (status IN ('active','paused','completed','failed')),
    current_pass            review_pass_t NOT NULL DEFAULT 'enrichment',
    role_type               TEXT NOT NULL DEFAULT 'default',
    target_candidate_ids    UUID[] NOT NULL DEFAULT '{}',
    completed_candidate_ids UUID[] NOT NULL DEFAULT '{}',
    failed_candidate_ids    UUID[] NOT NULL DEFAULT '{}',
    audit_findings          JSONB DEFAULT '{}',
    pass_timings            JSONB DEFAULT '{}',
    created_at              TIMESTAMPTZ DEFAULT now(),
    completed_at            TIMESTAMPTZ,
    updated_at              TIMESTAMPTZ DEFAULT now()
);

CREATE INDEX IF NOT EXISTS idx_task_iteration_task
    ON TaskIteration (task_id, iteration_number);
CREATE INDEX IF NOT EXISTS idx_task_iteration_type
    ON TaskIteration (iteration_type, created_at);
CREATE INDEX IF NOT EXISTS idx_task_iteration_decision
    ON TaskIteration (decision, created_at);
CREATE INDEX IF NOT EXISTS idx_broadcast_run
    ON AgentBroadcast (run_id, created_at DESC);
CREATE INDEX IF NOT EXISTS idx_broadcast_discovered_by
    ON AgentBroadcast (run_id, discovered_by, created_at DESC);
CREATE INDEX IF NOT EXISTS idx_srs_run ON SearchRoundState (run_id);
CREATE INDEX IF NOT EXISTS idx_srs_source ON SearchRoundState (source_name, is_saturated);
CREATE INDEX IF NOT EXISTS idx_srs_hypo ON SearchRoundState (run_id, hypothesis_id);
CREATE INDEX IF NOT EXISTS idx_review_session_run ON ReviewSession (run_id);
CREATE INDEX IF NOT EXISTS idx_review_session_status
    ON ReviewSession (status, current_pass);

-- ---------------------------------------------------------------------------
-- Repair old retry data before adding invariants.  A pending task that has
-- exhausted its budget is terminal; keeping it pending makes it unclaimable.
-- ---------------------------------------------------------------------------
UPDATE Task SET retry_count = 0 WHERE retry_count IS NULL OR retry_count < 0;
UPDATE Task SET max_retries = 1 WHERE max_retries IS NULL OR max_retries < 1;
UPDATE Task
SET status = 'error',
    error_message = COALESCE(error_message, 'Retry budget exhausted before migration'),
    updated_at = now(),
    updated_by_agent = 'system::migration-001',
    data_version = data_version + 1
WHERE status = 'pending' AND retry_count >= max_retries;

ALTER TABLE Task ALTER COLUMN retry_count SET DEFAULT 0;
ALTER TABLE Task ALTER COLUMN retry_count SET NOT NULL;
ALTER TABLE Task ALTER COLUMN max_retries SET DEFAULT 3;
ALTER TABLE Task ALTER COLUMN max_retries SET NOT NULL;

DO $$ BEGIN
    ALTER TABLE Task ADD CONSTRAINT chk_task_retry_count_nonnegative
        CHECK (retry_count >= 0);
EXCEPTION WHEN duplicate_object THEN NULL;
END $$;

DO $$ BEGIN
    ALTER TABLE Task ADD CONSTRAINT chk_task_max_retries_positive
        CHECK (max_retries >= 1);
EXCEPTION WHEN duplicate_object THEN NULL;
END $$;

-- PostgreSQL unique indexes treat NULL step keys as distinct.  Checkpointed
-- plans require non-NULL keys in application validation; legacy ad-hoc tasks
-- may remain NULL without preventing the upgrade.
CREATE UNIQUE INDEX IF NOT EXISTS uq_task_plan_step_key
    ON Task (plan_id, step_key) WHERE step_key IS NOT NULL;

-- ---------------------------------------------------------------------------
-- Concurrency-safe DAG state machine.
-- ---------------------------------------------------------------------------
CREATE OR REPLACE FUNCTION claim_task_batch(
    p_run_id      UUID,
    p_task_types  TEXT[],
    p_batch_size  INTEGER,
    p_agent_id    TEXT,
    p_timeout_min INTEGER DEFAULT 10
)
RETURNS SETOF Task
LANGUAGE plpgsql
AS $$
BEGIN
    IF p_agent_id IS NULL OR btrim(p_agent_id) = '' THEN
        RAISE EXCEPTION 'agent_id must not be empty';
    END IF;
    IF p_batch_size < 1 THEN
        RAISE EXCEPTION 'batch_size must be positive';
    END IF;
    IF p_timeout_min < 1 THEN
        RAISE EXCEPTION 'timeout_minutes must be positive';
    END IF;

    RETURN QUERY
    WITH batch AS (
        SELECT candidate.task_id
        FROM Task candidate
        WHERE candidate.run_id = p_run_id
          AND candidate.task_type = ANY(p_task_types)
          AND candidate.status = 'pending'
          AND candidate.retry_count < candidate.max_retries
          AND NOT EXISTS (
              SELECT 1
              FROM TaskDependency dependency
              JOIN Task prerequisite
                ON prerequisite.task_id = dependency.depends_on_task_id
              WHERE dependency.task_id = candidate.task_id
                AND prerequisite.status <> 'done'
          )
        ORDER BY candidate.priority DESC, candidate.created_at ASC
        LIMIT p_batch_size
        FOR UPDATE SKIP LOCKED
    )
    UPDATE Task task
    SET status = 'claimed',
        claimed_by = p_agent_id,
        agent_id = p_agent_id,
        claimed_at = now(),
        updated_at = now(),
        updated_by_agent = p_agent_id,
        data_version = data_version + 1
    FROM batch
    WHERE task.task_id = batch.task_id
    RETURNING task.*;
END;
$$;

CREATE OR REPLACE FUNCTION complete_task(
    p_task_id UUID,
    p_output_data JSONB,
    p_agent_id TEXT
)
RETURNS VOID
LANGUAGE plpgsql
AS $$
DECLARE
    v_task RECORD;
BEGIN
    SELECT status, claimed_by, agent_id
    INTO v_task
    FROM Task
    WHERE task_id = p_task_id
    FOR UPDATE;

    IF NOT FOUND THEN
        RAISE EXCEPTION 'Task % not found', p_task_id;
    END IF;
    IF v_task.status NOT IN ('claimed', 'processing') THEN
        RAISE EXCEPTION 'Task % cannot transition from % to done', p_task_id, v_task.status;
    END IF;
    IF v_task.claimed_by IS DISTINCT FROM p_agent_id
       OR v_task.agent_id IS DISTINCT FROM p_agent_id THEN
        RAISE EXCEPTION 'Task % is owned by %, not %', p_task_id, v_task.claimed_by, p_agent_id;
    END IF;

    UPDATE Task
    SET status = 'done',
        output_data = p_output_data,
        completed_at = now(),
        updated_at = now(),
        updated_by_agent = p_agent_id,
        data_version = data_version + 1
    WHERE task_id = p_task_id;

    UPDATE Task dependent
    SET input_data = resolve_task_inputs(dependent.task_id)
    FROM TaskDependency dependency
    WHERE dependency.task_id = dependent.task_id
      AND dependency.depends_on_task_id = p_task_id
      AND dependent.status = 'pending';
END;
$$;

CREATE OR REPLACE FUNCTION fail_task(
    p_task_id UUID,
    p_error_message TEXT,
    p_agent_id TEXT
)
RETURNS VOID
LANGUAGE plpgsql
AS $$
DECLARE
    v_task RECORD;
    v_next_retry INTEGER;
BEGIN
    SELECT status, claimed_by, agent_id, retry_count, max_retries
    INTO v_task
    FROM Task
    WHERE task_id = p_task_id
    FOR UPDATE;

    IF NOT FOUND THEN
        RAISE EXCEPTION 'Task % not found', p_task_id;
    END IF;
    IF v_task.status NOT IN ('claimed', 'processing') THEN
        RAISE EXCEPTION 'Task % cannot transition from % to failed', p_task_id, v_task.status;
    END IF;
    IF v_task.claimed_by IS DISTINCT FROM p_agent_id
       OR v_task.agent_id IS DISTINCT FROM p_agent_id THEN
        RAISE EXCEPTION 'Task % is owned by %, not %', p_task_id, v_task.claimed_by, p_agent_id;
    END IF;

    v_next_retry := v_task.retry_count + 1;
    UPDATE Task
    SET status = CASE
            WHEN v_next_retry >= max_retries THEN 'error'::task_status_t
            ELSE 'pending'::task_status_t
        END,
        error_message = p_error_message,
        retry_count = v_next_retry,
        failed_at = now(),
        claimed_by = NULL,
        claimed_at = NULL,
        agent_id = NULL,
        updated_at = now(),
        updated_by_agent = p_agent_id,
        data_version = data_version + 1
    WHERE task_id = p_task_id;
END;
$$;

CREATE OR REPLACE FUNCTION release_stale_tasks()
RETURNS TABLE (
    released_id UUID,
    released_status TEXT,
    released_retry INTEGER
)
LANGUAGE plpgsql
AS $$
BEGIN
    RETURN QUERY
    UPDATE Task task
    SET status = CASE
            WHEN retry_count + 1 >= max_retries THEN 'error'::task_status_t
            ELSE 'pending'::task_status_t
        END,
        claimed_by = NULL,
        claimed_at = NULL,
        agent_id = NULL,
        retry_count = retry_count + 1,
        failed_at = now(),
        error_message = COALESCE(error_message, 'Task claim expired'),
        updated_at = now(),
        updated_by_agent = 'system::stale_release',
        data_version = data_version + 1
    WHERE status = 'claimed'
      AND claimed_at < (now() - INTERVAL '10 minutes')
    RETURNING task.task_id, task.status::TEXT, task.retry_count;
END;
$$;
