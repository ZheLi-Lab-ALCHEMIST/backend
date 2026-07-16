import importlib.util
import json
from pathlib import Path
import sys

import pytest
from aiohttp import web

ROOT = Path(__file__).resolve().parents[2]
sys.path.insert(0, str(ROOT))
sys.modules.pop("utils", None)
utils_spec = importlib.util.spec_from_file_location(
    "utils",
    ROOT / "utils" / "__init__.py",
    submodule_search_locations=[str(ROOT / "utils")],
)
assert utils_spec is not None and utils_spec.loader is not None
utils_module = importlib.util.module_from_spec(utils_spec)
sys.modules["utils"] = utils_module
utils_spec.loader.exec_module(utils_module)

import server


pytestmark = pytest.mark.asyncio


class FakeRequest:
    def __init__(self, payload):
        self.payload = payload

    async def json(self):
        return self.payload


class FakeQueue:
    def __init__(self):
        self.ordinary = []
        self.admitted = []

    def put(self, item):
        self.ordinary.append(item)

    def put_with_workbench_sidecar(self, item, sidecar):
        self.admitted.append((item, sidecar))


class FakeProvider:
    def __init__(self):
        self.terminal = []

    def matches(self, draft):
        return any(
            node.get("class_type") in {"FileInputNode", "TaskProcessNode"}
            for node in draft.get("prompt", {}).values()
        )

    def has_admission(self, draft):
        return "prompt_admission_id" in draft

    async def consume(self, _draft):
        return object()

    async def build_sidecar(self, _lease, _draft):
        return "sidecar"

    def terminalize_prequeue(self, lease, reason):
        self.terminal.append((lease, reason))


def make_server(provider=None):
    instance = server.PromptServer.__new__(server.PromptServer)
    instance.number = 1
    instance.prompt_queue = FakeQueue()
    instance.on_prompt_handlers = []
    instance.on_prompt_submit_handlers = []
    instance.prompt_admission_provider = provider
    return instance


def prompt_payload(class_type="OrdinaryNode", **extra):
    return {
        "client_id": "client-a",
        "prompt": {"node-a": {"class_type": class_type, "inputs": {}}},
        **extra,
    }


async def valid_prompt(_prompt_id, prompt, _targets):
    return True, None, list(prompt), {}


async def test_ordinary_prompt_keeps_native_hook_exception_behavior(monkeypatch):
    instance = make_server()
    instance.on_prompt_handlers.append(lambda _draft: 1 / 0)
    monkeypatch.setattr(server.execution, "validate_prompt", valid_prompt)

    response = await instance._post_prompt(FakeRequest(prompt_payload()))

    assert response.status == 200
    assert len(instance.prompt_queue.ordinary) == 1
    assert instance.prompt_queue.admitted == []


async def test_raw_workbench_requires_admission_before_hooks(monkeypatch):
    provider = FakeProvider()
    instance = make_server(provider)
    hook = lambda draft: draft
    instance.on_prompt_handlers.append(hook)
    monkeypatch.setattr(server.execution, "validate_prompt", valid_prompt)

    response = await instance._post_prompt(
        FakeRequest(prompt_payload("FileInputNode"))
    )

    assert response.status == 400
    assert json.loads(response.text)["error"]["type"] == "prompt_admission_required"
    assert instance.prompt_queue.admitted == []


async def test_admitted_workbench_cannot_downgrade_after_hooks(monkeypatch):
    provider = FakeProvider()
    instance = make_server(provider)
    instance.on_prompt_handlers.append(
        lambda draft: prompt_payload(
            "OrdinaryNode", prompt_admission_id=draft["prompt_admission_id"]
        )
    )
    monkeypatch.setattr(server.execution, "validate_prompt", valid_prompt)
    payload = prompt_payload(
        "FileInputNode", prompt_admission_id="admission-a"
    )

    response = await instance._post_prompt(FakeRequest(payload))

    assert response.status == 400
    assert json.loads(response.text)["error"]["type"] == "prompt_admission_downgrade_rejected"
    assert len(provider.terminal) == 1
    assert instance.prompt_queue.admitted == []


async def test_hooks_cannot_introduce_unadmitted_workbench(monkeypatch):
    provider = FakeProvider()
    instance = make_server(provider)
    instance.on_prompt_handlers.append(
        lambda draft: prompt_payload("TaskProcessNode")
    )
    monkeypatch.setattr(server.execution, "validate_prompt", valid_prompt)

    response = await instance._post_prompt(FakeRequest(prompt_payload()))

    assert response.status == 400
    assert json.loads(response.text)["error"]["type"] == "prompt_admission_required"
    assert instance.prompt_queue.ordinary == []


async def test_admitted_short_circuit_is_rejected_and_terminalized(monkeypatch):
    provider = FakeProvider()
    instance = make_server(provider)
    instance.on_prompt_submit_handlers.append(
        lambda _context: web.json_response({"short": True})
    )
    monkeypatch.setattr(server.execution, "validate_prompt", valid_prompt)

    response = await instance._post_prompt(
        FakeRequest(
            prompt_payload("FileInputNode", prompt_admission_id="admission-a")
        )
    )

    assert response.status == 400
    assert json.loads(response.text)["error"]["type"] == "prompt_admission_hook_short_circuit"
    assert len(provider.terminal) == 1
