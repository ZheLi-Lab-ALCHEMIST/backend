from __future__ import annotations

import asyncio

from execution import PromptExecutor, WorkerExecutionContext


def test_worker_context_has_no_prompt_queue_bridge() -> None:
    context = WorkerExecutionContext()
    assert not hasattr(context, "prompt_queue")


def test_prompt_executor_independent_worker_path_does_not_read_prompt_queue() -> None:
    executor = PromptExecutor(WorkerExecutionContext())
    asyncio.run(executor.execute_async({}, "job-probe", independent_worker=True))
    assert executor.success is True
