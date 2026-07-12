import pytest
import pytest_asyncio
import hashlib
import os
from aiohttp import web
from aiohttp.test_utils import TestClient, TestServer
from app.user_manager import UserManager, _canonical_json_text, _result_matches_request
from unittest.mock import AsyncMock, Mock, patch

pytestmark = (
    pytest.mark.asyncio
)  # This applies the asyncio mark to all test functions in the module


HEX_A = "a" * 64
HEX_B = "b" * 64
HEX_C = "c" * 64
WORKFLOW_BODY = b'{"nodes":[]}'
BODY_FINGERPRINT = hashlib.sha256(WORKFLOW_BODY).hexdigest()
CORRELATION_HEADERS = {
    "X-Alchem-Mutation-Request-Id": HEX_A,
    "X-Alchem-Project-Instance-Id": "project-instance",
}
CREATE_HEADERS = {
    **CORRELATION_HEADERS,
    "X-Alchem-Expected-Destination-Absent": "true",
    "X-Alchem-After-Content-Fingerprint": BODY_FINGERPRINT,
}
REPLACE_HEADERS = {
    **CORRELATION_HEADERS,
    "X-Alchem-Workflow-Id": HEX_C,
    "X-Alchem-Expected-Workflow-Revision": "2",
    "X-Alchem-Expected-Before-Fingerprint": HEX_B,
    "X-Alchem-Expected-Destination-Absent": "false",
    "X-Alchem-After-Content-Fingerprint": BODY_FINGERPRINT,
}
DELETE_HEADERS = {
    **CORRELATION_HEADERS,
    "X-Alchem-Workflow-Id": HEX_C,
    "X-Alchem-Expected-Workflow-Revision": "3",
    "X-Alchem-Expected-Before-Fingerprint": HEX_B,
    "X-Alchem-Expected-Destination-Absent": "false",
}
MOVE_HEADERS = {
    **CORRELATION_HEADERS,
    "X-Alchem-Workflow-Id": HEX_C,
    "X-Alchem-Expected-Workflow-Revision": "4",
    "X-Alchem-Expected-Before-Fingerprint": HEX_B,
    "X-Alchem-Expected-Destination-Absent": "true",
    "X-Alchem-After-Content-Fingerprint": HEX_B,
}


def _intent_digest(
    operation, write_mode, source, destination, workflow_id, revision,
    before_fingerprint, destination_absent, after_fingerprint,
):
    intent = {
        "schema_id": "pm.workflow-mutation-intent.v1",
        "operation": operation,
        "write_mode": write_mode,
        "source_storage_key": source,
        "destination_storage_key": destination,
        "workflow_id": workflow_id,
        "expected_workflow_revision": revision,
        "expected_before_fingerprint": before_fingerprint,
        "expected_destination_absent": destination_absent,
        "after_content_fingerprint": after_fingerprint,
    }
    return hashlib.sha256(_canonical_json_text(intent).encode()).hexdigest()


REPLACE_INTENT_DIGEST = _intent_digest(
    "write", "replace", "example.json", "example.json", HEX_C, 2,
    HEX_B, False, BODY_FINGERPRINT,
)
DELETE_INTENT_DIGEST = _intent_digest(
    "delete", None, "a.json", None, HEX_C, 3, HEX_B, False, None,
)
MOVE_INTENT_DIGEST = _intent_digest(
    "move", None, "a.json", "b.json", HEX_C, 4, HEX_B, True, HEX_B,
)


async def test_workflow_mutation_canonical_json_matches_browser_numbers():
    assert _canonical_json_text({
        "fixed": 0.00001,
        "small": 1e-7,
        "large_fixed": 1e20,
        "large_exponent": 1e21,
        "rounded_integer": 9_007_199_254_740_993,
        "negative_zero": -0.0,
    }) == (
        '{"fixed":0.00001,"large_exponent":1e+21,'
        '"large_fixed":100000000000000000000,"negative_zero":0,'
        '"rounded_integer":9007199254740992,"small":1e-7}'
    )


async def test_workflow_mutation_result_must_match_full_cas_request():
    request = {
        "operation": "write",
        "write_mode": "replace",
        "source_storage_key": "workflows/example.json",
        "destination_storage_key": "workflows/example.json",
        "mutation_request_id": HEX_A,
        "project_instance_id": "project-instance",
        "workflow_id": HEX_C,
        "expected_workflow_revision": 2,
        "expected_before_fingerprint": HEX_B,
        "expected_destination_absent": False,
        "after_content_fingerprint": BODY_FINGERPRINT,
    }
    assert _result_matches_request(write_result(), request)
    for field, value in (
        ("workflow_id", HEX_A),
        ("workflow_revision", 4),
        ("workflow_storage_key", "other.json"),
        ("workflow_fingerprint", HEX_A),
        ("mutation_intent_digest", HEX_A),
    ):
        result = {**write_result(), field: value}
        assert not _result_matches_request(result, request)


@pytest_asyncio.fixture
async def aiohttp_client():
    clients = []

    async def create_client(application):
        client = TestClient(TestServer(application))
        await client.start_server()
        clients.append(client)
        return client

    yield create_client
    for client in clients:
        await client.close()


def write_result(*, durability="confirmed"):
    result = {
        "status": "committed",
        "mutation_request_id": HEX_A,
        "mutation_intent_digest": REPLACE_INTENT_DIGEST,
        "project_instance_id": "project-instance",
        "operation": "write",
        "durability": durability,
        "workflow_revision": 3,
        "workflow_id": HEX_C,
        "workflow_storage_key": "example.json",
        "workflow_fingerprint": BODY_FINGERPRINT,
    }
    if durability == "uncertain":
        result.update({
            "transaction_id": HEX_C,
            "recovery_pending": True,
            "reason": "mutation_committed_durability_uncertain",
        })
    return result


def delete_result():
    return {
        "status": "committed", "mutation_request_id": HEX_A,
        "mutation_intent_digest": DELETE_INTENT_DIGEST, "project_instance_id": "project-instance",
        "operation": "delete", "durability": "confirmed", "workflow_revision": 4,
        "workflow_id": HEX_C, "deleted_workflow_storage_key": "a.json",
        "deleted_content_fingerprint": HEX_B, "workflow_absent": True,
    }


def move_result():
    return {
        "status": "committed", "mutation_request_id": HEX_A,
        "mutation_intent_digest": MOVE_INTENT_DIGEST, "project_instance_id": "project-instance",
        "operation": "move", "durability": "confirmed", "workflow_revision": 5,
        "workflow_id": HEX_C, "source_workflow_storage_key": "a.json",
        "source_absent": True,
        "destination_workflow_storage_key": "b.json",
        "destination_workflow_fingerprint": HEX_B,
    }


def precommit_result():
    return {
        "status": "precommit_failed", "mutation_request_id": HEX_A,
        "mutation_intent_digest": MOVE_INTENT_DIGEST, "project_instance_id": "project-instance",
        "operation": "move", "write_mode": None, "reason": "destination_exists",
    }


class MutationProvider:
    def __init__(self, *, result=None, side_effect=None, matches=True):
        self.matches = Mock(return_value=matches)
        self.execute = AsyncMock(return_value=result, side_effect=side_effect)
        self.get_userdata_mutation_disposition = AsyncMock()
        self.list_userdata_mutation_dispositions = AsyncMock()
        self.list_userdata_workflow_identities = AsyncMock()
        self.acknowledge_userdata_mutation_disposition = AsyncMock()


@pytest.fixture
def user_manager(tmp_path):
    um = UserManager()
    um.get_request_user_filepath = lambda req, file, **kwargs: os.path.join(
        tmp_path, file
    ) if file else tmp_path
    return um


@pytest.fixture
def app(user_manager):
    app = web.Application()
    routes = web.RouteTableDef()
    user_manager.add_routes(routes)
    app.add_routes(routes)
    return app


async def test_listuserdata_empty_directory(aiohttp_client, app, tmp_path):
    client = await aiohttp_client(app)
    resp = await client.get("/userdata?dir=test_dir")
    assert resp.status == 404


async def test_listuserdata_with_files(aiohttp_client, app, tmp_path):
    os.makedirs(tmp_path / "test_dir")
    with open(tmp_path / "test_dir" / "file1.txt", "w") as f:
        f.write("test content")

    client = await aiohttp_client(app)
    resp = await client.get("/userdata?dir=test_dir")
    assert resp.status == 200
    assert await resp.json() == ["file1.txt"]


async def test_listuserdata_recursive(aiohttp_client, app, tmp_path):
    os.makedirs(tmp_path / "test_dir" / "subdir")
    with open(tmp_path / "test_dir" / "file1.txt", "w") as f:
        f.write("test content")
    with open(tmp_path / "test_dir" / "subdir" / "file2.txt", "w") as f:
        f.write("test content")

    client = await aiohttp_client(app)
    resp = await client.get("/userdata?dir=test_dir&recurse=true")
    assert resp.status == 200
    assert set(await resp.json()) == {"file1.txt", "subdir/file2.txt"}


async def test_listuserdata_full_info(aiohttp_client, app, tmp_path):
    os.makedirs(tmp_path / "test_dir")
    with open(tmp_path / "test_dir" / "file1.txt", "w") as f:
        f.write("test content")

    client = await aiohttp_client(app)
    resp = await client.get("/userdata?dir=test_dir&full_info=true")
    assert resp.status == 200
    result = await resp.json()
    assert len(result) == 1
    assert result[0]["path"] == "file1.txt"
    assert "size" in result[0]
    assert "modified" in result[0]


async def test_listuserdata_split_path(aiohttp_client, app, tmp_path):
    os.makedirs(tmp_path / "test_dir" / "subdir")
    with open(tmp_path / "test_dir" / "subdir" / "file1.txt", "w") as f:
        f.write("test content")

    client = await aiohttp_client(app)
    resp = await client.get("/userdata?dir=test_dir&recurse=true&split=true")
    assert resp.status == 200
    assert await resp.json() == [["subdir/file1.txt", "subdir", "file1.txt"]]


async def test_listuserdata_invalid_directory(aiohttp_client, app):
    client = await aiohttp_client(app)
    resp = await client.get("/userdata?dir=")
    assert resp.status == 400


async def test_listuserdata_normalized_separator(aiohttp_client, app, tmp_path):
    os_sep = "\\"
    with patch("os.sep", os_sep):
        with patch("os.path.sep", os_sep):
            os.makedirs(tmp_path / "test_dir" / "subdir")
            with open(tmp_path / "test_dir" / "subdir" / "file1.txt", "w") as f:
                f.write("test content")

            client = await aiohttp_client(app)
            resp = await client.get("/userdata?dir=test_dir&recurse=true")
            assert resp.status == 200
            result = await resp.json()
            assert len(result) == 1
            assert "/" in result[0]  # Ensure forward slash is used
            assert "\\" not in result[0]  # Ensure backslash is not present
            assert result[0] == "subdir/file1.txt"

            # Test with full_info
            resp = await client.get(
                "/userdata?dir=test_dir&recurse=true&full_info=true"
            )
            assert resp.status == 200
            result = await resp.json()
            assert len(result) == 1
            assert "/" in result[0]["path"]  # Ensure forward slash is used
            assert "\\" not in result[0]["path"]  # Ensure backslash is not present
            assert result[0]["path"] == "subdir/file1.txt"


async def test_post_userdata_new_file(aiohttp_client, app, tmp_path):
    client = await aiohttp_client(app)
    content = b"test content"
    resp = await client.post("/userdata/test.txt", data=content)

    assert resp.status == 200
    assert await resp.text() == '"test.txt"'

    # Verify file was created with correct content
    with open(tmp_path / "test.txt", "rb") as f:
        assert f.read() == content


async def test_post_userdata_overwrite_existing(aiohttp_client, app, tmp_path):
    # Create initial file
    with open(tmp_path / "test.txt", "w") as f:
        f.write("initial content")

    client = await aiohttp_client(app)
    new_content = b"updated content"
    resp = await client.post("/userdata/test.txt", data=new_content)

    assert resp.status == 200
    assert await resp.text() == '"test.txt"'

    # Verify file was overwritten
    with open(tmp_path / "test.txt", "rb") as f:
        assert f.read() == new_content


async def test_post_userdata_no_overwrite(aiohttp_client, app, tmp_path):
    # Create initial file
    with open(tmp_path / "test.txt", "w") as f:
        f.write("initial content")

    client = await aiohttp_client(app)
    resp = await client.post("/userdata/test.txt?overwrite=false", data=b"new content")

    assert resp.status == 409

    # Verify original content unchanged
    with open(tmp_path / "test.txt", "r") as f:
        assert f.read() == "initial content"


async def test_post_userdata_full_info(aiohttp_client, app, tmp_path):
    client = await aiohttp_client(app)
    content = b"test content"
    resp = await client.post("/userdata/test.txt?full_info=true", data=content)

    assert resp.status == 200
    result = await resp.json()
    assert result["path"] == "test.txt"
    assert result["size"] == len(content)
    assert "modified" in result


async def test_non_workflow_userdata_ignores_mutation_only_query_grammar(
    aiohttp_client, app, tmp_path
):
    client = await aiohttp_client(app)
    response = await client.post(
        "/userdata/plain.txt?write_mode=overwrite",
        data=b"native",
    )
    assert response.status == 200
    assert (tmp_path / "plain.txt").read_bytes() == b"native"


async def test_nonmatching_provider_leaves_alchem_like_userdata_native(
    aiohttp_client, app, user_manager, tmp_path
):
    provider = MutationProvider(matches=False)
    user_manager.add_userdata_mutation_transaction_provider(provider)
    client = await aiohttp_client(app)

    response = await client.post(
        "/userdata/plain.json?write_mode=replace",
        data=WORKFLOW_BODY,
        headers=REPLACE_HEADERS,
    )

    assert response.status == 200
    assert (tmp_path / "plain.json").read_bytes() == WORKFLOW_BODY
    provider.matches.assert_called_once()
    provider.execute.assert_not_awaited()


async def test_nonmatching_provider_cannot_release_saved_workflow_to_native(
    aiohttp_client, app, user_manager, tmp_path
):
    provider = MutationProvider(matches=False)
    user_manager.add_userdata_mutation_transaction_provider(provider)
    client = await aiohttp_client(app)

    response = await client.post(
        "/userdata/workflows%252Fexample.json?write_mode=replace",
        data=WORKFLOW_BODY,
        headers=REPLACE_HEADERS,
    )

    assert response.status == 503
    assert not (tmp_path / "workflows" / "example.json").exists()
    provider.execute.assert_not_awaited()


async def test_duplicate_json_keys_reject_before_provider_execute(
    aiohttp_client, app, user_manager
):
    body = b'{"nodes":[],"nodes":[1]}'
    headers = {
        **REPLACE_HEADERS,
        "X-Alchem-After-Content-Fingerprint": hashlib.sha256(
            b'{"nodes":[1]}'
        ).hexdigest(),
    }
    provider = MutationProvider(result=write_result())
    user_manager.add_userdata_mutation_transaction_provider(provider)
    client = await aiohttp_client(app)

    response = await client.post(
        "/userdata/workflows%252Fexample.json?write_mode=replace",
        data=body,
        headers=headers,
    )

    assert response.status == 400
    provider.matches.assert_called_once()
    provider.execute.assert_not_awaited()


@pytest.mark.parametrize("header_name", [
    "X-Alchem-Mutation-Request-Id",
    "X-Alchem-Project-Instance-Id",
    "X-Alchem-Workflow-Id",
    "X-Alchem-Expected-Workflow-Revision",
    "X-Alchem-Expected-Before-Fingerprint",
    "X-Alchem-Expected-Destination-Absent",
    "X-Alchem-After-Content-Fingerprint",
])
async def test_duplicate_mutation_headers_reject_before_provider_execute(
    aiohttp_client, app, user_manager, header_name
):
    provider = MutationProvider(result=write_result())
    user_manager.add_userdata_mutation_transaction_provider(provider)
    client = await aiohttp_client(app)
    headers = [*REPLACE_HEADERS.items(), (header_name, "duplicate")]

    response = await client.post(
        "/userdata/workflows%252Fexample.json?write_mode=replace",
        data=WORKFLOW_BODY,
        headers=headers,
    )

    assert response.status == 400
    provider.matches.assert_not_called()
    provider.execute.assert_not_awaited()


async def test_duplicate_mutation_headers_do_not_change_ordinary_userdata(
    aiohttp_client, app, tmp_path
):
    client = await aiohttp_client(app)

    response = await client.post(
        "/userdata/plain.txt",
        data=b"native",
        headers=[
            ("X-Alchem-Mutation-Request-Id", HEX_A),
            ("X-Alchem-Mutation-Request-Id", HEX_B),
        ],
    )

    assert response.status == 200
    assert (tmp_path / "plain.txt").read_bytes() == b"native"


async def test_userdata_mutation_transaction_provider_encloses_commit(
    aiohttp_client, app, user_manager, tmp_path
):
    provider = MutationProvider(result=write_result())
    user_manager.add_userdata_mutation_transaction_provider(provider)
    client = await aiohttp_client(app)

    resp = await client.post(
        "/userdata/workflows%252Fexample.json?write_mode=replace",
        data=WORKFLOW_BODY,
        headers=REPLACE_HEADERS,
    )

    assert resp.status == 200
    assert await resp.json() == write_result()
    assert resp.headers["X-Alchem-Mutation-Disposition"] == "committed_confirmed"
    assert resp.headers["X-Alchem-Workflow-Revision"] == "3"
    assert resp.headers["X-Alchem-Workflow-Storage-Key"] == "example.json"
    assert not (tmp_path / "workflows" / "example.json").exists()
    assert provider.execute.await_args.kwargs["source_storage_key"] == "workflows/example.json"
    assert provider.execute.await_args.kwargs["destination_storage_key"] == "workflows/example.json"
    assert provider.execute.await_args.kwargs["raw_body"] == b'{"nodes":[]}'


async def test_userdata_mutation_transaction_provider_is_unique(user_manager):
    user_manager.add_userdata_mutation_transaction_provider(MutationProvider())

    with pytest.raises(RuntimeError, match="already registered"):
        user_manager.add_userdata_mutation_transaction_provider(MutationProvider())


async def test_userdata_mutation_provider_removal_is_exact_and_idempotent(user_manager):
    first = MutationProvider()
    remove = user_manager.add_userdata_mutation_transaction_provider(first)

    remove()
    remove()
    second = MutationProvider()
    user_manager.add_userdata_mutation_transaction_provider(second)

    assert user_manager._userdata_mutation_transaction_provider is second


async def test_matched_workflow_without_correlation_never_falls_back(
    aiohttp_client, app, user_manager, tmp_path
):
    provider = MutationProvider(result=write_result())
    user_manager.add_userdata_mutation_transaction_provider(provider)
    client = await aiohttp_client(app)

    resp = await client.post("/userdata/workflows%252Fexample.json", data=b"payload")

    assert resp.status == 400
    provider.execute.assert_not_awaited()
    assert not (tmp_path / "workflows" / "example.json").exists()


async def test_correlated_request_without_owner_never_falls_back(
    aiohttp_client, app, tmp_path
):
    client = await aiohttp_client(app)

    resp = await client.post(
        "/userdata/workflows%252Fexample.json?write_mode=create",
        data=WORKFLOW_BODY,
        headers=CREATE_HEADERS,
    )

    assert resp.status == 503
    assert not (tmp_path / "workflows" / "example.json").exists()


async def test_saved_workflow_without_owner_or_correlation_never_falls_back(
    aiohttp_client, app, tmp_path
):
    client = await aiohttp_client(app)

    response = await client.post(
        "/userdata/workflows%252Fno-owner.json",
        data=WORKFLOW_BODY,
    )

    assert response.status == 400
    assert not (tmp_path / "workflows" / "no-owner.json").exists()


@pytest.mark.parametrize("provider_result", [[], {"status": "committed"}])
async def test_matched_provider_malformed_result_never_falls_back(
    aiohttp_client, app, user_manager, tmp_path, provider_result
):
    user_manager.add_userdata_mutation_transaction_provider(
        MutationProvider(result=provider_result)
    )
    client = await aiohttp_client(app)

    resp = await client.post(
        "/userdata/workflows%252Fexample.json?write_mode=replace",
        data=WORKFLOW_BODY,
        headers=REPLACE_HEADERS,
    )

    assert resp.status == 500
    assert not (tmp_path / "workflows" / "example.json").exists()


async def test_provider_exception_never_falls_back(
    aiohttp_client, app, user_manager, tmp_path
):
    user_manager.add_userdata_mutation_transaction_provider(
        MutationProvider(side_effect=OSError("private failure"))
    )
    client = await aiohttp_client(app)

    resp = await client.post(
        "/userdata/workflows%252Fexample.json?write_mode=replace",
        data=WORKFLOW_BODY,
        headers=REPLACE_HEADERS,
    )

    assert resp.status == 503
    assert await resp.json() == {
        "status": "rejected",
        "reason": "mutation_provider_unavailable",
    }
    assert not (tmp_path / "workflows" / "example.json").exists()


async def test_uncertain_provider_result_projects_exact_202_headers(
    aiohttp_client, app, user_manager
):
    user_manager.add_userdata_mutation_transaction_provider(
        MutationProvider(result=write_result(durability="uncertain"))
    )
    client = await aiohttp_client(app)

    resp = await client.post(
        "/userdata/workflows%252Fexample.json?write_mode=replace",
        data=WORKFLOW_BODY,
        headers=REPLACE_HEADERS,
    )

    assert resp.status == 202
    assert resp.headers["X-Alchem-Mutation-Disposition"] == (
        "committed_durability_uncertain"
    )
    assert resp.headers["X-Alchem-Mutation-Transaction-Id"] == HEX_C
    assert resp.headers["X-Alchem-Mutation-Recovery-Pending"] == "true"


async def test_delete_and_move_use_transaction_provider(aiohttp_client, app, user_manager):
    async def execute(**kwargs):
        return delete_result() if kwargs["operation"] == "delete" else move_result()

    provider = MutationProvider(side_effect=execute)
    user_manager.add_userdata_mutation_transaction_provider(provider)
    client = await aiohttp_client(app)

    deleted = await client.delete(
        "/userdata/workflows%252Fa.json", headers=DELETE_HEADERS
    )
    moved = await client.post(
        "/userdata/workflows%252Fa.json/move/workflows%252Fb.json",
        headers=MOVE_HEADERS,
    )

    assert deleted.status == 200
    assert deleted.headers["X-Alchem-Workflow-Absent"] == "true"
    assert moved.status == 200
    assert moved.headers["X-Alchem-Source-Absent"] == "true"
    assert moved.headers["X-Alchem-Destination-Workflow-Storage-Key"] == (
        "b.json"
    )


async def test_nonmatching_provider_cannot_release_saved_move_to_native(
    aiohttp_client, app, user_manager, tmp_path
):
    source = tmp_path / "workflows" / "a.json"
    source.parent.mkdir()
    source.write_bytes(WORKFLOW_BODY)
    provider = MutationProvider(matches=False)
    user_manager.add_userdata_mutation_transaction_provider(provider)
    client = await aiohttp_client(app)

    response = await client.post(
        "/userdata/workflows%252Fa.json/move/workflows%252Fb.json",
        headers=MOVE_HEADERS,
    )

    assert response.status == 503
    assert source.exists()
    assert not (tmp_path / "workflows" / "b.json").exists()
    provider.execute.assert_not_awaited()


@pytest.mark.parametrize(("source_key", "destination_key", "url"), [
    (
        "workflows/a.json",
        "plain.json",
        "/userdata/workflows%252Fa.json/move/plain.json",
    ),
    (
        "plain.json",
        "workflows/b.json",
        "/userdata/plain.json/move/workflows%252Fb.json",
    ),
])
async def test_cross_namespace_saved_move_fails_closed(
    aiohttp_client, app, user_manager, tmp_path,
    source_key, destination_key, url,
):
    source = tmp_path / source_key
    source.parent.mkdir(parents=True, exist_ok=True)
    source.write_bytes(WORKFLOW_BODY)
    provider = MutationProvider()
    user_manager.add_userdata_mutation_transaction_provider(provider)
    client = await aiohttp_client(app)

    response = await client.post(url, headers=MOVE_HEADERS)

    assert response.status == 400
    assert await response.json() == {
        "status": "rejected",
        "reason": "workflow_namespace_mismatch",
        "field": "destination_storage_key",
    }
    assert source.exists()
    assert not (tmp_path / destination_key).exists()
    provider.matches.assert_not_called()
    provider.execute.assert_not_awaited()


async def test_precommit_result_has_exact_status_and_no_commit_headers(
    aiohttp_client, app, user_manager
):
    user_manager.add_userdata_mutation_transaction_provider(
        MutationProvider(result=precommit_result())
    )
    client = await aiohttp_client(app)

    resp = await client.post(
        "/userdata/workflows%252Fa.json/move/workflows%252Fb.json",
        headers=MOVE_HEADERS,
    )

    assert resp.status == 409
    assert await resp.json() == precommit_result()
    assert "X-Alchem-Mutation-Disposition" not in resp.headers


async def test_provider_owned_project_context_precommit_passes_exactly(
    aiohttp_client, app, user_manager
):
    result = precommit_result()
    result["reason"] = "project_context_unavailable"
    user_manager.add_userdata_mutation_transaction_provider(
        MutationProvider(result=result)
    )
    client = await aiohttp_client(app)

    response = await client.post(
        "/userdata/workflows%252Fa.json/move/workflows%252Fb.json",
        headers=MOVE_HEADERS,
    )

    assert response.status == 503
    assert await response.json() == result


async def test_replay_rejection_is_closed_409(aiohttp_client, app, user_manager):
    rejection = {
        "status": "rejected",
        "mutation_request_id": HEX_A,
        "mutation_intent_digest": HEX_B,
        "project_instance_id": "project-instance",
        "reason": "mutation_request_id_reused_with_different_intent",
    }
    user_manager.add_userdata_mutation_transaction_provider(
        MutationProvider(result=rejection)
    )
    client = await aiohttp_client(app)

    resp = await client.post(
        "/userdata/workflows%252Fexample.json?write_mode=replace",
        data=WORKFLOW_BODY,
        headers=REPLACE_HEADERS,
    )

    assert resp.status == 409
    assert await resp.json() == rejection
    assert "X-Alchem-Mutation-Disposition" not in resp.headers


async def test_invalid_write_mode_rejects_before_provider(
    aiohttp_client, app, user_manager
):
    provider = MutationProvider()
    user_manager.add_userdata_mutation_transaction_provider(provider)
    client = await aiohttp_client(app)

    resp = await client.post(
        "/userdata/workflows%252Fexample.json?write_mode=overwrite",
        data=WORKFLOW_BODY,
    )

    assert resp.status == 400
    assert await resp.json() == {
        "status": "rejected", "reason": "invalid_request", "field": "write_mode"
    }
    provider.execute.assert_not_awaited()


async def test_schema_valid_result_for_different_request_fails_closed(
    aiohttp_client, app, user_manager, tmp_path
):
    result = write_result()
    result["mutation_request_id"] = HEX_C
    user_manager.add_userdata_mutation_transaction_provider(
        MutationProvider(result=result)
    )
    client = await aiohttp_client(app)

    resp = await client.post(
        "/userdata/workflows%252Fexample.json?write_mode=replace",
        data=WORKFLOW_BODY,
        headers=REPLACE_HEADERS,
    )

    assert resp.status == 500
    assert not (tmp_path / "workflows" / "example.json").exists()


async def test_disposition_methods_delegate_to_registered_provider(user_manager):
    class Provider:
        matches = Mock(return_value=False)
        execute = AsyncMock()

        get_userdata_mutation_disposition = AsyncMock(return_value=write_result())
        list_userdata_mutation_dispositions = AsyncMock(return_value={
            "project_instance_id": "project-instance", "results": [write_result()]
        })
        list_userdata_workflow_identities = AsyncMock()
        acknowledge_userdata_mutation_disposition = AsyncMock(return_value=None)

    provider = Provider()
    user_manager.add_userdata_mutation_transaction_provider(provider)

    assert await user_manager.get_userdata_mutation_disposition(
        "project-instance", HEX_A
    ) == write_result()
    assert await user_manager.list_userdata_mutation_dispositions(
        "project-instance"
    ) == {"project_instance_id": "project-instance", "results": [write_result()]}
    await user_manager.list_userdata_workflow_identities("project-instance")
    provider.list_userdata_workflow_identities.assert_awaited_once_with(
        project_instance_id="project-instance"
    )
    assert await user_manager.acknowledge_userdata_mutation_disposition(
        "project-instance", HEX_A, HEX_B
    ) is None


async def test_disposition_routes_project_closed_results(aiohttp_client, app, user_manager):
    class Provider:
        matches = Mock(return_value=False)
        execute = AsyncMock()

        async def get_userdata_mutation_disposition(self, **kwargs):
            return write_result()

        async def list_userdata_mutation_dispositions(self, **kwargs):
            return {"project_instance_id": "project-instance", "results": [write_result()]}

        async def list_userdata_workflow_identities(self, **kwargs):
            return {
                "schema_id": "pm.workbench-workflow-identity-inventory.v1",
                "project_instance_id": "project-instance",
                "workflow_revision": 3,
                "identities": [{
                    "workflow_id": HEX_C,
                    "workflow_storage_key": "example.json",
                    "workflow_revision": 3,
                    "workflow_fingerprint": HEX_B,
                }],
            }

        async def acknowledge_userdata_mutation_disposition(self, **kwargs):
            return None

    user_manager.add_userdata_mutation_transaction_provider(Provider())
    client = await aiohttp_client(app)

    fetched = await client.get(
        f"/userdata/mutation-disposition/{HEX_A}?project_instance_id=project-instance"
    )
    listed = await client.get(
        "/userdata/mutation-dispositions?project_instance_id=project-instance"
    )
    identities = await client.get(
        "/userdata/workflow-identities?project_instance_id=project-instance"
    )
    acknowledged = await client.delete(
        f"/userdata/mutation-disposition/{HEX_A}",
        json={
            "project_instance_id": "project-instance",
            "mutation_request_id": HEX_A,
            "result_digest": HEX_B,
        },
    )

    assert fetched.status == 200
    assert await fetched.json() == write_result()
    assert listed.status == 200
    assert await listed.json() == {
        "project_instance_id": "project-instance", "results": [write_result()]
    }
    assert identities.status == 200
    assert (await identities.json())["identities"][0]["workflow_storage_key"] == (
        "example.json"
    )
    assert acknowledged.status == 204


async def test_get_disposition_rejects_mismatched_result_correlation(
    aiohttp_client, app, user_manager
):
    wrong_request = write_result()
    wrong_request["mutation_request_id"] = HEX_C
    wrong_project = write_result()
    wrong_project["project_instance_id"] = "other-project"
    wrong_rejection_request = {
        "status": "rejected",
        "project_instance_id": "project-instance",
        "mutation_request_id": HEX_C,
        "reason": "mutation_disposition_unknown",
    }
    wrong_rejection_project = {
        "status": "rejected",
        "project_instance_id": "other-project",
        "mutation_request_id": HEX_A,
        "reason": "project_context_unavailable",
    }
    provider = MutationProvider(matches=False)
    provider.get_userdata_mutation_disposition.side_effect = [
        wrong_request,
        wrong_project,
        wrong_rejection_request,
        wrong_rejection_project,
    ]
    user_manager.add_userdata_mutation_transaction_provider(provider)
    client = await aiohttp_client(app)

    for _ in range(4):
        response = await client.get(
            f"/userdata/mutation-disposition/{HEX_A}"
            "?project_instance_id=project-instance"
        )
        assert response.status == 500
        assert (await response.json())["reason"] == (
            "mutation_provider_result_invalid"
        )


async def test_list_dispositions_rejects_mismatched_result_correlation(
    aiohttp_client, app, user_manager
):
    wrong_item = write_result()
    wrong_item["project_instance_id"] = "other-project"
    provider = MutationProvider(matches=False)
    provider.list_userdata_mutation_dispositions.side_effect = [
        {"project_instance_id": "project-instance", "results": [wrong_item]},
        {
            "status": "rejected",
            "project_instance_id": "other-project",
            "reason": "project_context_unavailable",
        },
    ]
    user_manager.add_userdata_mutation_transaction_provider(provider)
    client = await aiohttp_client(app)

    for _ in range(2):
        response = await client.get(
            "/userdata/mutation-dispositions"
            "?project_instance_id=project-instance"
        )
        assert response.status == 500


async def test_ack_path_body_mismatch_rejects_before_provider(
    aiohttp_client, app, user_manager
):
    provider = MutationProvider(matches=False)
    user_manager.add_userdata_mutation_transaction_provider(provider)
    client = await aiohttp_client(app)

    resp = await client.delete(
        f"/userdata/mutation-disposition/{HEX_A}",
        json={
            "project_instance_id": "project-instance",
            "mutation_request_id": HEX_C,
            "result_digest": HEX_B,
        },
    )

    assert resp.status == 412
    provider.acknowledge_userdata_mutation_disposition.assert_not_awaited()


async def test_ack_disposition_rejects_mismatched_provider_correlation(
    aiohttp_client, app, user_manager
):
    provider = MutationProvider(matches=False)
    provider.acknowledge_userdata_mutation_disposition.side_effect = [
        {
            "status": "rejected",
            "project_instance_id": "project-instance",
            "mutation_request_id": HEX_C,
            "reason": "mutation_disposition_ack_mismatch",
        },
        {
            "status": "rejected",
            "project_instance_id": "other-project",
            "mutation_request_id": HEX_A,
            "reason": "project_context_unavailable",
        },
    ]
    user_manager.add_userdata_mutation_transaction_provider(provider)
    client = await aiohttp_client(app)
    body = {
        "project_instance_id": "project-instance",
        "mutation_request_id": HEX_A,
        "result_digest": HEX_B,
    }

    for _ in range(2):
        response = await client.delete(
            f"/userdata/mutation-disposition/{HEX_A}", json=body
        )
        assert response.status == 500


@pytest.mark.parametrize("route, include_request_id", [
    (f"/userdata/mutation-disposition/{HEX_A}", True),
    ("/userdata/mutation-dispositions", False),
])
async def test_provider_owned_project_context_unavailable_passes_exactly(
    aiohttp_client, app, user_manager, route, include_request_id
):
    rejection = {
        "status": "rejected",
        "project_instance_id": "project-instance",
        "reason": "project_context_unavailable",
    }
    if include_request_id:
        rejection["mutation_request_id"] = HEX_A
    provider = MutationProvider(matches=False)
    provider.get_userdata_mutation_disposition.return_value = rejection
    provider.list_userdata_mutation_dispositions.return_value = rejection
    user_manager.add_userdata_mutation_transaction_provider(provider)
    client = await aiohttp_client(app)

    response = await client.get(
        f"{route}?project_instance_id=project-instance"
    )

    assert response.status == 503
    assert await response.json() == rejection


async def test_ack_provider_owned_project_context_unavailable_passes_exactly(
    aiohttp_client, app, user_manager
):
    rejection = {
        "status": "rejected",
        "project_instance_id": "project-instance",
        "mutation_request_id": HEX_A,
        "reason": "project_context_unavailable",
    }
    provider = MutationProvider(matches=False)
    provider.acknowledge_userdata_mutation_disposition.return_value = rejection
    user_manager.add_userdata_mutation_transaction_provider(provider)
    client = await aiohttp_client(app)

    response = await client.delete(
        f"/userdata/mutation-disposition/{HEX_A}",
        json={
            "project_instance_id": "project-instance",
            "mutation_request_id": HEX_A,
            "result_digest": HEX_B,
        },
    )

    assert response.status == 503
    assert await response.json() == rejection


async def test_invalid_logical_key_rejects_before_provider(
    aiohttp_client, app, user_manager
):
    provider = MutationProvider()
    user_manager.add_userdata_mutation_transaction_provider(provider)
    client = await aiohttp_client(app)

    resp = await client.post("/userdata/workflows%255Cexample.json", data=b"payload")

    assert resp.status == 400
    assert await resp.json() == {
        "status": "rejected",
        "reason": "workflow_storage_key_invalid",
        "field": "destination_storage_key",
    }
    provider.matches.assert_not_called()
    provider.execute.assert_not_awaited()


async def test_native_write_failure_preserves_existing_bytes(
    aiohttp_client, app, tmp_path
):
    target = tmp_path / "test.txt"
    target.write_bytes(b"original")
    client = await aiohttp_client(app)

    with patch("app.user_manager.os.replace", side_effect=OSError("replace failed")):
        resp = await client.post("/userdata/test.txt", data=b"replacement")

    assert resp.status == 400
    assert target.read_bytes() == b"original"


async def test_move_invalid_destination_returns_rejection(aiohttp_client, app, tmp_path):
    (tmp_path / "source.txt").write_text("source")
    client = await aiohttp_client(app)

    resp = await client.post("/userdata/source.txt/move/%252e%252e%252fdest.txt")

    assert resp.status in {400, 403}
    assert (tmp_path / "source.txt").read_text() == "source"


async def test_move_userdata(aiohttp_client, app, tmp_path):
    # Create initial file
    with open(tmp_path / "source.txt", "w") as f:
        f.write("test content")

    client = await aiohttp_client(app)
    resp = await client.post("/userdata/source.txt/move/dest.txt")

    assert resp.status == 200
    assert await resp.text() == '"dest.txt"'

    # Verify file was moved
    assert not os.path.exists(tmp_path / "source.txt")
    with open(tmp_path / "dest.txt", "r") as f:
        assert f.read() == "test content"


async def test_move_userdata_no_overwrite(aiohttp_client, app, tmp_path):
    # Create source and destination files
    with open(tmp_path / "source.txt", "w") as f:
        f.write("source content")
    with open(tmp_path / "dest.txt", "w") as f:
        f.write("destination content")

    client = await aiohttp_client(app)
    resp = await client.post("/userdata/source.txt/move/dest.txt?overwrite=false")

    assert resp.status == 409

    # Verify files remain unchanged
    with open(tmp_path / "source.txt", "r") as f:
        assert f.read() == "source content"
    with open(tmp_path / "dest.txt", "r") as f:
        assert f.read() == "destination content"


async def test_move_userdata_full_info(aiohttp_client, app, tmp_path):
    # Create initial file
    with open(tmp_path / "source.txt", "w") as f:
        f.write("test content")

    client = await aiohttp_client(app)
    resp = await client.post("/userdata/source.txt/move/dest.txt?full_info=true")

    assert resp.status == 200
    result = await resp.json()
    assert result["path"] == "dest.txt"
    assert result["size"] == len("test content")
    assert "modified" in result

    # Verify file was moved
    assert not os.path.exists(tmp_path / "source.txt")
    with open(tmp_path / "dest.txt", "r") as f:
        assert f.read() == "test content"


async def test_listuserdata_v2_empty_root(aiohttp_client, app):
    client = await aiohttp_client(app)
    resp = await client.get("/v2/userdata")
    assert resp.status == 200
    assert await resp.json() == []


async def test_listuserdata_v2_nonexistent_subdirectory(aiohttp_client, app):
    client = await aiohttp_client(app)
    resp = await client.get("/v2/userdata?path=does_not_exist")
    assert resp.status == 404


async def test_listuserdata_v2_default(aiohttp_client, app, tmp_path):
    os.makedirs(tmp_path / "test_dir" / "subdir")
    (tmp_path / "test_dir" / "file1.txt").write_text("content")
    (tmp_path / "test_dir" / "subdir" / "file2.txt").write_text("content")

    client = await aiohttp_client(app)
    resp = await client.get("/v2/userdata?path=test_dir")
    assert resp.status == 200
    data = await resp.json()
    file_paths = {item["path"] for item in data if item["type"] == "file"}
    assert file_paths == {"test_dir/file1.txt", "test_dir/subdir/file2.txt"}


async def test_listuserdata_v2_normalized_separators(aiohttp_client, app, tmp_path, monkeypatch):
    # Force backslash as os separator
    monkeypatch.setattr(os, 'sep', '\\')
    monkeypatch.setattr(os.path, 'sep', '\\')
    os.makedirs(tmp_path / "test_dir" / "subdir")
    (tmp_path / "test_dir" / "subdir" / "file1.txt").write_text("x")

    client = await aiohttp_client(app)
    resp = await client.get("/v2/userdata?path=test_dir")
    assert resp.status == 200
    data = await resp.json()
    for item in data:
        assert "/" in item["path"]
        assert "\\" not in item["path"]\

async def test_listuserdata_v2_url_encoded_path(aiohttp_client, app, tmp_path):
    # Create a directory with a space in its name and a file inside
    os.makedirs(tmp_path / "my dir")
    (tmp_path / "my dir" / "file.txt").write_text("content")

    client = await aiohttp_client(app)
    # Use URL-encoded space in path parameter
    resp = await client.get("/v2/userdata?path=my%20dir&recurse=false")
    assert resp.status == 200
    data = await resp.json()
    assert len(data) == 1
    entry = data[0]
    assert entry["name"] == "file.txt"
    # Ensure the path is correctly decoded and uses forward slash
    assert entry["path"] == "my dir/file.txt"
