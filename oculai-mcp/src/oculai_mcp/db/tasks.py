"""DAG task queue operations. (Adapted from Phase7 for new schema)"""

import logging
import re
from typing import Any
from uuid import UUID

from oculai_mcp.db.client import (
    execute_with_retry,
    fetch_with_retry,
    fetchrow_with_retry,
    get_db_pool,
)
from oculai_mcp.db.iterations import get_task_iterations

logger = logging.getLogger(__name__)

_TEMPLATE_RE = re.compile(r"^\$(\w[\w-]*)\.(\w+)$")


async def claim_task_batch(
    run_id: UUID,
    task_types: list[str],
    batch_size: int,
    agent_id: str,
    timeout_minutes: int = 10,
) -> list[dict[str, Any]]:
    """Claim a batch of ready tasks using FOR UPDATE SKIP LOCKED.

    If a task has retry_count > 0, its previous TaskIteration history is
    injected into the returned record under 'previous_iterations' so the
    new agent instance can resume from where the previous one left off.
    """
    rows = await fetch_with_retry(
        "SELECT * FROM claim_task_batch($1, $2, $3, $4, $5)",
        run_id, task_types, batch_size, agent_id, timeout_minutes,
    )
    result = [dict(row) for row in rows]

    # Resume enhancement: inject previous iteration history for retried tasks
    for task in result:
        if task.get("retry_count", 0) > 0:
            iterations = await get_task_iterations(task["task_id"])
            if iterations:
                task["previous_iterations"] = iterations
                task["resume_hint"] = (
                    f"This task was previously attempted {task['retry_count']} time(s). "
                    f"Review previous_iterations ({len(iterations)} steps) and continue "
                    f"from where it left off. Do NOT repeat searches already performed."
                )
                logger.info(
                    "Injected %d previous iterations into task=%s for resume by %s",
                    len(iterations), task["task_id"], agent_id,
                )

    if result:
        logger.info("Agent %s claimed %d tasks for run=%s", agent_id, len(result), run_id)
    return result


async def complete_task(task_id: UUID, agent_id: str, output_data: dict[str, Any]) -> None:
    await execute_with_retry("SELECT complete_task($1, $2, $3)", task_id, output_data, agent_id)
    logger.info("Agent %s completed task_id=%s", agent_id, task_id)


async def fail_task(task_id: UUID, error_msg: str, agent_id: str = "system") -> None:
    await execute_with_retry("SELECT fail_task($1, $2, $3)", task_id, error_msg, agent_id)
    logger.warning("Task %s failed: %s", task_id, error_msg)


async def release_stale_tasks() -> list[dict[str, Any]]:
    rows = await fetch_with_retry("SELECT * FROM release_stale_tasks()")
    result = [dict(row) for row in rows]
    if result:
        logger.warning("Released %d stale tasks", len(result))
    return result


def validate_plan_json(plan_json: dict[str, Any]) -> list[dict[str, Any]]:
    """Validate and return a checkpointable DAG task list.

    A checkpoint is intentionally rejected before any database write when a
    task has no stable key, keys are duplicated, a dependency is missing, or
    the dependency graph contains a cycle.  Stable step keys are required so
    dependencies and ``$step.field`` input mappings cannot become ambiguous.
    """
    if not isinstance(plan_json, dict):
        raise ValueError("plan_json must be an object")

    task_list = plan_json.get("tasks")
    if not isinstance(task_list, list) or not task_list:
        raise ValueError("plan_json.tasks must be a non-empty list")

    task_by_key: dict[str, dict[str, Any]] = {}
    for index, task in enumerate(task_list):
        if not isinstance(task, dict):
            raise ValueError(f"plan_json.tasks[{index}] must be an object")

        step_key = task.get("step_key")
        if not isinstance(step_key, str) or not step_key.strip():
            raise ValueError(f"plan_json.tasks[{index}].step_key must be a non-empty string")
        if step_key in task_by_key:
            raise ValueError(f"duplicate step_key: {step_key}")

        for field in ("task_type", "task_name"):
            if not isinstance(task.get(field), str) or not task[field].strip():
                raise ValueError(
                    f"plan_json.tasks[{index}].{field} must be a non-empty string"
                )

        priority = task.get("priority", 5)
        if not isinstance(priority, int) or isinstance(priority, bool) or not 1 <= priority <= 10:
            raise ValueError(f"task {step_key!r} priority must be an integer from 1 to 10")

        max_retries = task.get("max_retries", 3)
        if (
            not isinstance(max_retries, int)
            or isinstance(max_retries, bool)
            or max_retries < 1
        ):
            raise ValueError(f"task {step_key!r} max_retries must be a positive integer")

        depends_on = task.get("depends_on", [])
        if not isinstance(depends_on, list) or any(
            not isinstance(key, str) or not key for key in depends_on
        ):
            raise ValueError(f"task {step_key!r} depends_on must be a list of step keys")
        if len(depends_on) != len(set(depends_on)):
            raise ValueError(f"task {step_key!r} contains duplicate dependencies")

        task_by_key[step_key] = task

    for step_key, task in task_by_key.items():
        for dependency in task.get("depends_on", []):
            if dependency not in task_by_key:
                raise ValueError(
                    f"task {step_key!r} depends on unknown step_key {dependency!r}"
                )

    # DFS with three colours: 0=unvisited, 1=visiting, 2=complete.
    state: dict[str, int] = {}
    path: list[str] = []

    def visit(step_key: str) -> None:
        if state.get(step_key) == 2:
            return
        if state.get(step_key) == 1:
            cycle_start = path.index(step_key)
            cycle = path[cycle_start:] + [step_key]
            raise ValueError(f"task dependency cycle detected: {' -> '.join(cycle)}")

        state[step_key] = 1
        path.append(step_key)
        for dependency in task_by_key[step_key].get("depends_on", []):
            visit(dependency)
        path.pop()
        state[step_key] = 2

    for step_key in task_by_key:
        visit(step_key)

    return task_list


async def checkpoint_plan(
    run_id: UUID,
    plan_json: dict[str, Any],
    strategy_summary: str = "",
    created_by_agent: str = "system",
) -> tuple[UUID, int]:
    """Atomically persist a validated plan DAG and activate its run.

    The connection-level transaction is deliberate: helper functions that
    independently acquire the pool cannot provide rollback across Plan, Task,
    TaskDependency, and SourcingRun writes.
    """
    task_list = validate_plan_json(plan_json)
    pool = await get_db_pool()

    async with pool.acquire() as conn, conn.transaction():
        run = await conn.fetchrow(
            "SELECT status, active_plan_id FROM sourcingrun WHERE run_id = $1 FOR UPDATE",
            run_id,
        )
        if run is None:
            raise ValueError(f"run not found: {run_id}")
        if run["status"] in ("completed", "aborted"):
            raise ValueError(
                f"cannot checkpoint plan for run {run_id} in {run['status']!r} state"
            )

        plan_id = await conn.fetchval(
            """
            INSERT INTO plan (
                run_id, planner_state_json, status, strategy_summary,
                replan_triggers, created_by_agent, updated_by_agent
            )
            VALUES ($1, $2, 'active', $3, $4, $5, $5)
            RETURNING plan_id
            """,
            run_id,
            plan_json,
            strategy_summary,
            plan_json.get("replan_triggers", []),
            created_by_agent,
        )

        created_tasks: dict[str, UUID] = {}
        for task in task_list:
            step_key = task["step_key"]
            task_id = await conn.fetchval(
                """
                INSERT INTO task (
                    plan_id, run_id, task_type, task_name, step_key, priority,
                    input_data, max_retries, created_by_agent, updated_by_agent
                )
                VALUES ($1, $2, $3, $4, $5, $6, $7, $8, $9, $9)
                RETURNING task_id
                """,
                plan_id,
                run_id,
                task["task_type"],
                task["task_name"],
                step_key,
                task.get("priority", 5),
                task.get("input_data", task.get("input", {})),
                task.get("max_retries", 3),
                created_by_agent,
            )
            created_tasks[step_key] = task_id

        for task in task_list:
            task_id = created_tasks[task["step_key"]]
            mappings = task.get("dependency_input_mappings", {})
            if mappings is not None and not isinstance(mappings, dict):
                raise ValueError(
                    f"task {task['step_key']!r} dependency_input_mappings must be an object"
                )
            for dependency in task.get("depends_on", []):
                input_mapping = (mappings or {}).get(dependency, {})
                if not isinstance(input_mapping, dict):
                    raise ValueError(
                        f"input mapping for {task['step_key']!r} <- {dependency!r} "
                        "must be an object"
                    )
                await conn.execute(
                    """
                    INSERT INTO taskdependency (
                        plan_id, task_id, depends_on_task_id, input_mapping
                    )
                    VALUES ($1, $2, $3, $4)
                    """,
                    plan_id,
                    task_id,
                    created_tasks[dependency],
                    input_mapping,
                )

        previous_plan_id = run["active_plan_id"]
        if previous_plan_id is not None:
            await conn.execute(
                """
                UPDATE plan
                SET status = 'aborted', updated_at = now(), updated_by_agent = $2,
                    data_version = data_version + 1
                WHERE plan_id = $1 AND status IN ('draft', 'active')
                """,
                previous_plan_id,
                created_by_agent,
            )

        result = await conn.execute(
            """
            UPDATE sourcingrun
            SET active_plan_id = $2, status = 'running',
                started_at = COALESCE(started_at, now()), updated_at = now(),
                updated_by_agent = $3, data_version = data_version + 1
            WHERE run_id = $1
            """,
            run_id,
            plan_id,
            created_by_agent,
        )
        if result != "UPDATE 1":
            raise RuntimeError(f"failed to activate plan {plan_id} for run {run_id}")

    logger.info("Checkpointed plan %s with %d tasks for run %s", plan_id, len(task_list), run_id)
    return plan_id, len(task_list)


async def create_task(
    plan_id: UUID,
    run_id: UUID,
    task_type: str,
    task_name: str,
    input_data: dict[str, Any],
    step_key: str | None = None,
    priority: int = 5,
    created_by_agent: str = "system",
    max_retries: int = 3,
) -> UUID:
    row = await fetchrow_with_retry(
        """
        INSERT INTO task (plan_id, run_id, task_type, task_name, step_key, priority,
                          input_data, max_retries, created_by_agent, updated_by_agent)
        VALUES ($1, $2, $3, $4, $5, $6, $7, $8, $9, $9)
        RETURNING task_id
        """,
        plan_id, run_id, task_type, task_name, step_key, priority,
        input_data, max_retries, created_by_agent,
    )
    if row is None:
        raise RuntimeError(f"database did not return the created task for plan {plan_id}")
    task_id = row["task_id"]
    logger.info("Created task %s: %s (%s)", task_id, task_name, task_type)
    return task_id


async def create_task_dependency(plan_id: UUID, task_id: UUID, depends_on_task_id: UUID, input_mapping: dict[str, Any] | None = None) -> None:
    await execute_with_retry(
        """
        INSERT INTO taskdependency (plan_id, task_id, depends_on_task_id, input_mapping)
        VALUES ($1, $2, $3, $4)
        ON CONFLICT (task_id, depends_on_task_id) DO NOTHING
        """,
        plan_id, task_id, depends_on_task_id, input_mapping or {},
    )


async def get_task(task_id: UUID) -> dict[str, Any] | None:
    row = await fetchrow_with_retry("SELECT * FROM task WHERE task_id = $1", task_id)
    return dict(row) if row else None


async def get_plan(plan_id: UUID) -> dict[str, Any] | None:
    row = await fetchrow_with_retry("SELECT * FROM plan WHERE plan_id = $1", plan_id)
    return dict(row) if row else None


async def create_plan(
    run_id: UUID,
    planner_state_json: dict[str, Any],
    strategy_summary: str = "",
    replan_triggers: list[str] | None = None,
    created_by_agent: str = "system",
) -> UUID:
    row = await fetchrow_with_retry(
        """INSERT INTO plan (run_id, planner_state_json, strategy_summary, replan_triggers, created_by_agent, updated_by_agent)
           VALUES ($1, $2, $3, $4, $5, $5) RETURNING plan_id""",
        run_id, planner_state_json, strategy_summary, replan_triggers or [],
        created_by_agent,
    )
    if row is None:
        raise RuntimeError(f"database did not return the created plan for run {run_id}")
    plan_id = row["plan_id"]
    logger.info("Created plan %s for run %s", plan_id, run_id)
    return plan_id


async def update_plan_status(plan_id: UUID, status: str) -> None:
    await execute_with_retry(
        "UPDATE plan SET status = $2, updated_at = now(), updated_by_agent = 'system' WHERE plan_id = $1",
        plan_id, status,
    )


async def update_run_active_plan(run_id: UUID, plan_id: UUID) -> None:
    await execute_with_retry(
        "UPDATE sourcingrun SET active_plan_id = $2, updated_at = now(), updated_by_agent = 'system' WHERE run_id = $1",
        run_id, plan_id,
    )


async def get_task_depths(plan_id: UUID) -> dict[str, dict[str, int]]:
    rows = await fetch_with_retry(
        "SELECT task_type, status, COUNT(*) as cnt FROM task WHERE plan_id = $1 GROUP BY task_type, status",
        plan_id,
    )
    result: dict[str, dict[str, int]] = {}
    for row in rows:
        tt = str(row["task_type"])
        result.setdefault(tt, {})[row["status"]] = row["cnt"]
    return result
