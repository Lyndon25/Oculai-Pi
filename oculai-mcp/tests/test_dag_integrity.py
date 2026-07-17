"""Strict DAG queue, checkpoint transaction, and migration acceptance tests."""

from __future__ import annotations

import asyncio
import os
from pathlib import Path
from uuid import UUID, uuid4

import asyncpg
import pytest

from oculai_mcp.db import runs, tasks
from oculai_mcp.db.client import fetchrow_with_retry

pytestmark = pytest.mark.anyio


def _require_database() -> None:
    missing = [
        name
        for name in ("DB_HOST", "DB_PORT", "DB_USER", "DB_PASSWORD", "DB_NAME")
        if not os.environ.get(name)
    ]
    if missing:
        pytest.skip(f"Missing DB environment variables: {', '.join(missing)}")


def _plan(task_specs: list[dict]) -> dict:
    return {"strategy": "DAG integrity test", "tasks": task_specs}


async def _new_run(title: str) -> UUID:
    _require_database()
    return await runs.create_run(
        title=f"{title}-{uuid4()}",
        target_profile={"title": title},
        config={},
        created_by="dag-integrity-test",
    )


def test_plan_validation_rejects_duplicate_missing_and_cyclic_dependencies() -> None:
    base = {
        "task_type": "test",
        "task_name": "test task",
        "input_data": {},
    }

    with pytest.raises(ValueError, match="duplicate step_key"):
        tasks.validate_plan_json(
            _plan([{**base, "step_key": "same"}, {**base, "step_key": "same"}])
        )

    with pytest.raises(ValueError, match="unknown step_key"):
        tasks.validate_plan_json(_plan([{**base, "step_key": "child", "depends_on": ["missing"]}]))

    with pytest.raises(ValueError, match="cycle detected"):
        tasks.validate_plan_json(
            _plan(
                [
                    {**base, "step_key": "a", "depends_on": ["b"]},
                    {**base, "step_key": "b", "depends_on": ["a"]},
                ]
            )
        )


async def test_checkpoint_is_atomic_and_rolls_back_late_failure() -> None:
    run_id = await _new_run("checkpoint-rollback")
    invalid_mapping_plan = _plan(
        [
            {
                "step_key": "root",
                "task_type": "test",
                "task_name": "root",
            },
            {
                "step_key": "child",
                "task_type": "test",
                "task_name": "child",
                "depends_on": ["root"],
                # Valid DAG, deliberately invalid only after Plan and Task
                # inserts have occurred inside the transaction.
                "dependency_input_mappings": {"root": "not-an-object"},
            },
        ]
    )

    with pytest.raises(ValueError, match="input mapping"):
        await tasks.checkpoint_plan(run_id, invalid_mapping_plan)

    state = await fetchrow_with_retry(
        """
        SELECT status, active_plan_id,
               (SELECT count(*) FROM plan WHERE run_id = $1) AS plan_count,
               (SELECT count(*) FROM task WHERE run_id = $1) AS task_count
        FROM sourcingrun WHERE run_id = $1
        """,
        run_id,
    )
    assert state is not None
    assert state["status"] == "draft"
    assert state["active_plan_id"] is None
    assert state["plan_count"] == 0
    assert state["task_count"] == 0


async def test_dependencies_block_claim_and_ownership_is_enforced() -> None:
    run_id = await _new_run("dependency-and-ownership")
    plan_id, count = await tasks.checkpoint_plan(
        run_id,
        _plan(
            [
                {
                    "step_key": "root",
                    "task_type": "work",
                    "task_name": "root",
                },
                {
                    "step_key": "child",
                    "task_type": "work",
                    "task_name": "child",
                    "depends_on": ["root"],
                },
            ]
        ),
    )
    assert plan_id and count == 2

    first = await tasks.claim_task_batch(run_id, ["work"], 10, "agent-a")
    assert [task["step_key"] for task in first] == ["root"]
    root_id = first[0]["task_id"]

    with pytest.raises(asyncpg.PostgresError, match="owned by"):
        await tasks.complete_task(root_id, "agent-b", {"ok": True})
    with pytest.raises(asyncpg.PostgresError, match="owned by"):
        await tasks.fail_task(root_id, "wrong owner", "agent-b")
    unchanged = await tasks.get_task(root_id)
    assert unchanged is not None and unchanged["status"] == "claimed"

    await tasks.complete_task(root_id, "agent-a", {"ok": True})
    second = await tasks.claim_task_batch(run_id, ["work"], 10, "agent-b")
    assert [task["step_key"] for task in second] == ["child"]

    with pytest.raises(asyncpg.PostgresError, match="cannot transition"):
        await tasks.complete_task(root_id, "agent-a", {"again": True})


async def test_concurrent_claims_are_disjoint() -> None:
    run_id = await _new_run("concurrent-claim")
    await tasks.checkpoint_plan(
        run_id,
        _plan(
            [
                {
                    "step_key": f"independent-{index}",
                    "task_type": "parallel",
                    "task_name": f"independent {index}",
                }
                for index in range(8)
            ]
        ),
    )

    claimed_a, claimed_b = await asyncio.gather(
        tasks.claim_task_batch(run_id, ["parallel"], 8, "agent-a"),
        tasks.claim_task_batch(run_id, ["parallel"], 8, "agent-b"),
    )
    ids_a = {task["task_id"] for task in claimed_a}
    ids_b = {task["task_id"] for task in claimed_b}
    assert ids_a.isdisjoint(ids_b)
    assert len(ids_a | ids_b) == 8


async def test_failure_and_stale_release_reach_terminal_error_at_boundary() -> None:
    run_id = await _new_run("retry-boundary")
    await tasks.checkpoint_plan(
        run_id,
        _plan(
            [
                {
                    "step_key": "explicit-failure",
                    "task_type": "retry",
                    "task_name": "explicit failure",
                    "max_retries": 2,
                },
                {
                    "step_key": "stale-release",
                    "task_type": "stale",
                    "task_name": "stale release",
                    "max_retries": 1,
                },
            ]
        ),
    )

    pending = await fetchrow_with_retry(
        "SELECT task_id FROM task WHERE run_id = $1 AND step_key = 'explicit-failure'",
        run_id,
    )
    assert pending is not None
    with pytest.raises(asyncpg.PostgresError, match="cannot transition"):
        await tasks.fail_task(pending["task_id"], "not claimed", "agent-a")

    claimed = await tasks.claim_task_batch(run_id, ["retry"], 1, "agent-a")
    task_id = claimed[0]["task_id"]
    await tasks.fail_task(task_id, "first failure", "agent-a")
    after_first = await tasks.get_task(task_id)
    assert after_first is not None
    assert (after_first["status"], after_first["retry_count"]) == ("pending", 1)

    claimed = await tasks.claim_task_batch(run_id, ["retry"], 1, "agent-b")
    assert claimed[0]["task_id"] == task_id
    await tasks.fail_task(task_id, "second failure", "agent-b")
    terminal = await tasks.get_task(task_id)
    assert terminal is not None
    assert (terminal["status"], terminal["retry_count"]) == ("error", 2)
    assert await tasks.claim_task_batch(run_id, ["retry"], 1, "agent-c") == []

    stale = await tasks.claim_task_batch(run_id, ["stale"], 1, "agent-stale")
    stale_id = stale[0]["task_id"]
    from oculai_mcp.db.client import execute_with_retry

    await execute_with_retry(
        "UPDATE task SET claimed_at = now() - interval '11 minutes' WHERE task_id = $1",
        stale_id,
    )
    released = await tasks.release_stale_tasks()
    released_row = next(row for row in released if row["released_id"] == stale_id)
    assert (released_row["released_status"], released_row["released_retry"]) == (
        "error",
        1,
    )
    stale_terminal = await tasks.get_task(stale_id)
    assert stale_terminal is not None and stale_terminal["status"] == "error"


async def test_incremental_migration_upgrades_legacy_schema() -> None:
    _require_database()
    schema = f"migration_test_{uuid4().hex}"
    dsn = (
        f"postgresql://{os.environ['DB_USER']}:{os.environ['DB_PASSWORD']}"
        f"@{os.environ['DB_HOST']}:{os.environ['DB_PORT']}/{os.environ['DB_NAME']}"
    )
    conn = await asyncpg.connect(dsn)
    migration = (
        Path(__file__).resolve().parents[2]
        / "oculai-db"
        / "schema"
        / "migrations"
        / "001_dag_integrity_and_runtime_tables.sql"
    ).read_text(encoding="utf-8")

    try:
        await conn.execute(f'CREATE SCHEMA "{schema}"')
        await conn.execute(f'SET search_path TO "{schema}", public')
        await conn.execute(
            """
            CREATE DOMAIN task_status_t AS TEXT CHECK (
                VALUE IN ('pending','claimed','processing','done','error','timeout','skipped')
            );
            CREATE TABLE SourcingRun (
                run_id UUID PRIMARY KEY DEFAULT gen_random_uuid()
            );
            CREATE TABLE Task (
                task_id UUID PRIMARY KEY DEFAULT gen_random_uuid(),
                plan_id UUID NOT NULL,
                run_id UUID NOT NULL REFERENCES SourcingRun(run_id),
                task_type TEXT NOT NULL,
                task_name TEXT NOT NULL,
                step_key TEXT,
                status task_status_t DEFAULT 'pending',
                priority INTEGER DEFAULT 5,
                input_data JSONB DEFAULT '{}',
                output_data JSONB DEFAULT '{}',
                agent_id TEXT,
                claimed_by TEXT,
                claimed_at TIMESTAMPTZ,
                completed_at TIMESTAMPTZ,
                failed_at TIMESTAMPTZ,
                error_message TEXT,
                retry_count INTEGER DEFAULT 0,
                max_retries INTEGER DEFAULT 3,
                created_by_agent TEXT DEFAULT 'system',
                updated_by_agent TEXT DEFAULT 'system',
                created_at TIMESTAMPTZ DEFAULT now(),
                updated_at TIMESTAMPTZ DEFAULT now(),
                data_version INTEGER DEFAULT 1
            );
            CREATE TABLE TaskDependency (
                dependency_id UUID PRIMARY KEY DEFAULT gen_random_uuid(),
                plan_id UUID NOT NULL,
                task_id UUID NOT NULL REFERENCES Task(task_id),
                depends_on_task_id UUID NOT NULL REFERENCES Task(task_id),
                input_mapping JSONB DEFAULT '{}'
            );
            CREATE FUNCTION resolve_task_inputs(UUID) RETURNS JSONB
            LANGUAGE SQL AS 'SELECT ''{}''::jsonb';
            """
        )
        run_id = await conn.fetchval("INSERT INTO sourcingrun DEFAULT VALUES RETURNING run_id")
        zombie_id = await conn.fetchval(
            """
            INSERT INTO task (
                plan_id, run_id, task_type, task_name, status, retry_count, max_retries
            ) VALUES ($1, $2, 'legacy', 'zombie', 'pending', 3, 3)
            RETURNING task_id
            """,
            uuid4(),
            run_id,
        )

        async with conn.transaction():
            await conn.execute(migration)

        tables = await conn.fetch(
            """
            SELECT lower(table_name) AS name
            FROM information_schema.tables
            WHERE table_schema = $1
              AND lower(table_name) IN (
                  'taskiteration', 'agentbroadcast', 'searchroundstate', 'reviewsession'
              )
            """,
            schema,
        )
        assert {row["name"] for row in tables} == {
            "taskiteration",
            "agentbroadcast",
            "searchroundstate",
            "reviewsession",
        }
        assert await conn.fetchval(
            "SELECT status = 'error' FROM task WHERE task_id = $1", zombie_id
        )
        assert await conn.fetchval(
            "SELECT to_regprocedure('claim_task_batch(uuid,text[],integer,text,integer)') IS NOT NULL"
        )
    finally:
        await conn.execute("SET search_path TO public")
        await conn.execute(f'DROP SCHEMA IF EXISTS "{schema}" CASCADE')
        await conn.close()
