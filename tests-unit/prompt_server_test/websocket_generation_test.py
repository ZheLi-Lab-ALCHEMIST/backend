import asyncio
import importlib.util
from pathlib import Path
import sys

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


def make_server():
    server = PromptServer.__new__(PromptServer)
    server._socket_session_lock = asyncio.Lock()
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
