import asyncio
import importlib.util
from pathlib import Path
import sys
from threading import Lock
from types import SimpleNamespace

import pytest

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

from server import PromptServer


pytestmark = pytest.mark.asyncio


class FakeSocket:
    def __init__(self):
        self.closed = False
        self.messages = []

    async def send_json(self, message):
        self.messages.append(message)

    async def close(self):
        self.closed = True


class TransitionOwner:
    def __init__(self):
        self.calls = []

    def try_begin(self, generation_current, *, abort_observed):
        self.calls.append(("begin", generation_current, abort_observed))
        return generation_current and not abort_observed

    def publish_terminal(self, generation_current, *, abort_observed):
        self.calls.append(("terminal", generation_current, abort_observed))
        return generation_current and not abort_observed


def make_server():
    server = PromptServer.__new__(PromptServer)
    server._socket_session_lock = asyncio.Lock()
    server._socket_generation_lock = Lock()
    server._socket_generation_counter = 0
    server._socket_sessions = {}
    server._socket_message_handlers = {}
    server._socket_lifecycle_callbacks = []
    server.sockets = {}
    server.sockets_metadata = {}
    return server


async def test_replacement_installs_new_generation_before_old_disconnect():
    server = make_server()
    events = []
    server.register_socket_lifecycle_callback(lambda event, sid, generation: events.append((event, sid, generation)))
    first = FakeSocket()
    second = FakeSocket()

    first_generation = await server._install_socket_generation("client", first)
    second_generation = await server._install_socket_generation("client", second)

    assert first_generation == 1
    assert second_generation == 2
    assert first.closed is True
    assert await server.get_current_socket_generation("client") == 2
    assert await server.send_json_exact("client", 1, first, "old", {}) == "replaced"
    assert await server.send_json_exact("client", 2, second, "new", {"ok": True}) == "delivered"
    await server._remove_socket_generation("client", 1, first)
    assert await server.get_current_socket_generation("client") == 2
    assert second.messages == [{"type": "new", "data": {"ok": True}}]
    assert events == [
        ("connected", "client", 1),
        ("disconnected", "client", 1),
        ("connected", "client", 2),
    ]


async def test_feature_flags_must_settle_before_typed_handler():
    server = make_server()
    socket = FakeSocket()
    generation = await server._install_socket_generation("client", socket)
    received = []

    async def handle(sid, captured_generation, captured_socket, data):
        received.append((sid, captured_generation, captured_socket, data))

    server.register_socket_message_handler("workbench.test", handle)
    assert await server._handle_socket_text(
        "client", generation, socket, '{"type":"workbench.test","data":{}}'
    ) is False
    assert await server._handle_socket_text(
        "client", generation, socket, '{"type":"feature_flags","data":{"test":true}}'
    ) is True
    assert await server._handle_socket_text(
        "client", generation, socket, '{"type":"workbench.test","data":{"value":1}}'
    ) is True
    assert received == [("client", generation, socket, {"value": 1})]
    assert socket.messages[0]["type"] == "feature_flags"


async def test_begin_and_terminal_use_exact_generation_and_current_abort():
    server = make_server()
    socket = FakeSocket()
    generation = await server._install_socket_generation("client", socket)
    owner = TransitionOwner()
    abort_token = SimpleNamespace(aborted=False)

    assert await server.try_begin_generation_bound_side_effect(
        "client", generation, socket, abort_token, owner,
    ) is True
    assert server.try_publish_generation_bound_terminal(
        "client", generation, FakeSocket(), abort_token, owner,
    ) is False
    assert server.try_publish_generation_bound_terminal(
        "other-client", generation, socket, abort_token, owner,
    ) is False
    abort_token.aborted = True
    assert server.try_publish_generation_bound_terminal(
        "client", generation, socket, abort_token, owner,
    ) is False
    assert await server.try_begin_generation_bound_side_effect(
        "client", generation, socket, abort_token, owner,
    ) is False

    replacement = FakeSocket()
    await server._install_socket_generation("client", replacement)
    abort_token.aborted = False
    assert server.try_publish_generation_bound_terminal(
        "client", generation, socket, abort_token, owner,
    ) is False
    assert await server.try_begin_generation_bound_side_effect(
        "client", generation, socket, abort_token, owner,
    ) is False
    assert owner.calls == [
        ("begin", True, False),
        ("terminal", False, False),
        ("terminal", False, False),
        ("terminal", True, True),
        ("begin", True, True),
        ("terminal", False, False),
        ("begin", False, False),
    ]


async def test_worker_terminal_can_publish_while_exact_send_waits_for_io():
    server = make_server()
    send_started = asyncio.Event()
    release_send = asyncio.Event()

    class PausedSendSocket(FakeSocket):
        async def send_json(self, message):
            send_started.set()
            await release_send.wait()
            await super().send_json(message)

    socket = PausedSendSocket()
    generation = await server._install_socket_generation("client", socket)
    owner = TransitionOwner()
    abort_token = SimpleNamespace(aborted=False)
    send = asyncio.create_task(server.send_json_exact("client", generation, socket, "status", {}, timeout=2))
    try:
        await asyncio.wait_for(send_started.wait(), timeout=1)
        assert await asyncio.wait_for(asyncio.to_thread(
            server.try_publish_generation_bound_terminal, "client", generation, socket, abort_token, owner,
        ), timeout=1) is True
        assert owner.calls == [("terminal", True, False)]
        assert not send.done()
    finally:
        release_send.set()
        disposition = await asyncio.wait_for(send, timeout=1)
    assert disposition == "delivered"


@pytest.mark.parametrize("retire", ["close_socket_generation", "_remove_socket_generation"])
async def test_retirement_fences_terminal_before_lifecycle_callback(retire):
    server = make_server()
    socket = FakeSocket()
    generation = await server._install_socket_generation("client", socket)
    owner = TransitionOwner()
    abort_token = SimpleNamespace(aborted=False)
    receipt = []

    def observe(event, sid, observed_generation):
        unlocked = server._socket_generation_lock.acquire(blocking=False)
        if unlocked:
            server._socket_generation_lock.release()
        accepted = server.try_publish_generation_bound_terminal(
            sid, observed_generation, socket, abort_token, owner,
        ) if unlocked else None
        receipt.append((event, observed_generation, unlocked, accepted))

    server.register_socket_lifecycle_callback(observe)
    await getattr(server, retire)("client", generation, socket)
    assert receipt == [("disconnected", generation, True, False)]
    assert owner.calls == [("terminal", False, False)]
    assert await server.get_current_socket_generation("client") is None


async def test_failed_exact_send_revokes_while_close_waits_and_successor_serves():
    server = make_server()
    close_started = asyncio.Event()
    release_close = asyncio.Event()

    class FailedSendSocket(FakeSocket):
        async def send_json(self, message):
            raise ConnectionError("exact send failed")

        async def close(self):
            close_started.set()
            await release_close.wait()
            await super().close()

    events = []
    server.register_socket_lifecycle_callback(lambda event, sid, generation: events.append((event, sid, generation)))
    failed = FailedSendSocket()
    generation = await server._install_socket_generation("client", failed)
    owner = TransitionOwner()
    abort_token = SimpleNamespace(aborted=False)
    send = asyncio.create_task(server.send_json_exact("client", generation, failed, "status", {}, timeout=2))
    try:
        await asyncio.wait_for(close_started.wait(), timeout=1)
        assert await server.get_current_socket_generation("client") is None
        assert events == [("connected", "client", generation)]
        assert await asyncio.wait_for(asyncio.to_thread(
            server.try_publish_generation_bound_terminal, "client", generation, failed, abort_token, owner,
        ), timeout=1) is False
        successor = FakeSocket()
        successor_generation = await server._install_socket_generation("client", successor)
        assert await server.send_json_exact("client", generation, failed, "old", {}) == "replaced"
        assert await server.send_json_exact("client", successor_generation, successor, "new", {}) == "delivered"
        assert server.try_publish_generation_bound_terminal(
            "client", successor_generation, successor, abort_token, owner,
        ) is True
    finally:
        release_close.set()
        disposition = await asyncio.wait_for(send, timeout=1)
    assert disposition == "closed"
    assert failed.closed
    assert await server.get_current_socket_generation("client") == successor_generation
    assert successor.messages == [{"type": "new", "data": {}}]
    assert owner.calls == [("terminal", False, False), ("terminal", True, False)]
    assert events == [
        ("connected", "client", generation),
        ("connected", "client", successor_generation),
        ("disconnected", "client", generation),
    ]
