"""Exercise native producers and the explicit independent-worker observer."""
from types import SimpleNamespace

import pytest

from execution import PromptExecutor, WorkerExecutionContext, _send_node_execution_event
import nodes
from comfy.model_management import InterruptProcessingException
from comfy_execution.graph import ExecutionBlocker


class OutputNode:
    @classmethod
    def INPUT_TYPES(cls):
        return {"required": {"value": ("STRING",)}}

    RETURN_TYPES = ("STRING",)
    FUNCTION = "run"
    OUTPUT_NODE = True

    def run(self, value):
        if value == "block":
            return (ExecutionBlocker("blocked native input"),)
        if value == "cancel":
            raise InterruptProcessingException()
        if value == "fail":
            raise ValueError("native node failure 非法")
        return {"ui": {"text": [value]}, "result": (value,)}


def test_native_worker_receives_node_events_and_cached_execution(monkeypatch):
    monkeypatch.setitem(nodes.NODE_CLASS_MAPPINGS, "S18Output", OutputNode)
    events = []
    context = WorkerExecutionContext(lambda event, data: events.append((event, data)))
    executor = PromptExecutor(context)
    prompt = {"1": {"class_type": "S18Output", "inputs": {"value": "native result"}}}
    executor.execute(prompt, "job-native", execute_outputs=["1"], independent_worker=True)
    assert executor.success
    # The native progress observer also uses send_sync; it is not an execution log.
    native_events = [(event, data) for event, data in events if event != "progress_state"]
    assert [event for event, _ in native_events] == ["execution_start", "execution_cached", "executing", "executed", "execution_success"]
    assert all(type(data["timestamp"]) is int for _, data in native_events)
    assert all(data["prompt_id"] == "job-native" for _, data in native_events)
    assert context.client_id is None and not hasattr(context, "prompt_queue")
    events.clear()
    executor.execute(prompt, "job-cached", execute_outputs=["1"], independent_worker=True)
    assert executor.success
    cached = next(data for event, data in events if event == "execution_cached")
    assert cached["nodes"] == ["1"]
    assert "executing" not in [event for event, _ in events]


@pytest.mark.parametrize("value,event", [("fail", "execution_error"), ("cancel", "execution_interrupted")])
def test_native_worker_failure_retains_produced_events(monkeypatch, value, event):
    monkeypatch.setitem(nodes.NODE_CLASS_MAPPINGS, "S18Output", OutputNode)
    events = []
    executor = PromptExecutor(WorkerExecutionContext(lambda event, data: events.append((event, data))))
    executor.execute({"1": {"class_type": "S18Output", "inputs": {"value": value}}},
                     "job-failure", execute_outputs=["1"], independent_worker=True)
    assert not executor.success
    assert [name for name, _ in events if name != "progress_state"] == ["execution_start", "execution_cached", "executing", event]
    failure = events[-1][1]
    assert failure["node_id"] == "1"
    if value == "fail":
        assert failure["exception_type"] == "ValueError"
        assert "native node failure 非法" in failure["exception_message"]


def test_worker_observer_failure_is_not_swallowed():
    def refuse(_event, _data):
        raise OSError("publication unavailable")
    executor = PromptExecutor(WorkerExecutionContext(refuse))
    with pytest.raises(OSError, match="publication unavailable"):
        executor.add_message("execution_start", {"prompt_id": "job"}, broadcast=False)


@pytest.mark.parametrize("client_id", [None, "interactive-client"])
def test_interactive_event_delivery_and_payload_remain_native(client_id):
    received = []
    context = SimpleNamespace(client_id=client_id, send_sync=lambda *args: received.append(args))
    executor = PromptExecutor(context)
    executor.add_message("execution_start", {"prompt_id": "interactive"}, broadcast=False)
    assert len(received) == (0 if client_id is None else 1)
    received.clear()
    payload = {"node": "1", "display_node": "1", "prompt_id": "interactive", "output": {"text": ["result"]}}
    _send_node_execution_event(context, "executed", payload)
    assert received == ([] if client_id is None else [("executed", payload, client_id)])
    assert "timestamp" not in payload
    received.clear()
    executor.add_message("execution_interrupted", {"prompt_id": "interactive"}, broadcast=True)
    assert len(received) == 1


@pytest.mark.parametrize("worker", [True, False])
def test_native_execution_blocker_preserves_original_reason_and_delivery(monkeypatch, worker):
    monkeypatch.setitem(nodes.NODE_CLASS_MAPPINGS, "S18Output", OutputNode)
    events = []
    consume = lambda event, data, *_: events.append((event, data))
    context = WorkerExecutionContext(consume) if worker else SimpleNamespace(client_id=None, send_sync=consume)
    executor = PromptExecutor(context)
    prompt = {"1": {"class_type": "S18Output", "inputs": {"value": "block"}},
              "2": {"class_type": "S18Output", "inputs": {"value": ["1", 0]}}}
    executor.execute(prompt, "job-blocked", execute_outputs=["2"], independent_worker=True)
    failure = next(data for event, data in events if event == "execution_error")
    assert failure["exception_type"] == "ExecutionBlocked"
    assert failure["exception_message"] == "Execution Blocked: blocked native input"
    assert failure["prompt_id"] == "job-blocked" and failure["node_id"] == "2"
    if worker:
        assert type(failure["timestamp"]) is int
    else:
        assert "timestamp" not in failure
