import execution


class FakeServer:
    def __init__(self):
        self.updates = 0

    def queue_updated(self):
        self.updates += 1


class FakeSidecar:
    def __init__(self):
        self.events = []

    def queue_committed(self):
        self.events.append("queued")

    def mark_running(self):
        self.events.append("running")

    def mark_cancelled(self):
        self.events.append("cancelled")

    def terminalize(self, reason):
        self.events.append(("terminal", reason))


def item(number, prompt_id):
    return (number, prompt_id, {"1": {}}, {}, ["1"])


def test_sidecar_moves_with_queue_item_until_task_done():
    queue = execution.PromptQueue(FakeServer())
    sidecar = FakeSidecar()
    queue.put_with_workbench_sidecar(item(1, "prompt-a"), sidecar)

    assert queue.get_workbench_sidecar("prompt-a") is sidecar
    queued_item, task_id = queue.get(timeout=0)
    assert queued_item[1] == "prompt-a"
    assert queue.get_workbench_sidecar("prompt-a") is sidecar

    interrupted = []
    assert queue.interrupt_running(
        "prompt-a", lambda: interrupted.append("interrupt")
    ) is True
    assert interrupted == ["interrupt"]
    assert queue.get_workbench_sidecar("prompt-a") is sidecar

    queue.task_done(
        task_id,
        {},
        execution.PromptQueue.ExecutionStatus("error", False, []),
    )
    assert queue.get_workbench_sidecar("prompt-a") is None
    assert sidecar.events == [
        "queued",
        "running",
        "cancelled",
        ("terminal", "execution_fault"),
    ]


def test_delete_and_wipe_terminalize_only_queued_sidecars():
    queue = execution.PromptQueue(FakeServer())
    first = FakeSidecar()
    second = FakeSidecar()
    queue.put_with_workbench_sidecar(item(1, "prompt-a"), first)
    queue.put_with_workbench_sidecar(item(2, "prompt-b"), second)

    assert queue.delete_queue_item(lambda queued: queued[1] == "prompt-a")
    assert first.events[-1] == ("terminal", "queue_deleted")
    assert queue.get_workbench_sidecar("prompt-b") is second

    queue.wipe_queue()
    assert second.events[-1] == ("terminal", "queue_wiped")
    assert queue.workbench_sidecars == {}


def test_targeted_interrupt_uses_one_mutex_linearized_running_set():
    queue = execution.PromptQueue(FakeServer())
    first = FakeSidecar()
    second = FakeSidecar()
    queue.put_with_workbench_sidecar(item(1, "prompt-a"), first)
    queue.put_with_workbench_sidecar(item(2, "prompt-b"), second)
    queue.get(timeout=0)
    queue.get(timeout=0)

    callbacks = []
    assert queue.interrupt_running(
        "prompt-a", lambda: callbacks.append(tuple(queue.currently_running))
    )
    assert callbacks and len(callbacks[0]) == 2
    assert "cancelled" in first.events
    assert "cancelled" not in second.events
