import json as json_module

from app.user_manager import _mutation_response

from .user_manager_test import delete_result, move_result, write_result


def test_workflow_commit_storage_key_headers_are_ascii():
    source = "nested/试用 A!.json"
    destination = "nested/药物 B!.json"
    source_header = "nested%2F%E8%AF%95%E7%94%A8%20A!.json"
    destination_header = "nested%2F%E8%8D%AF%E7%89%A9%20B!.json"
    for base, projection in (
        (write_result(), {"workflow_storage_key": (source, source_header)}),
        (delete_result(), {"deleted_workflow_storage_key": (source, source_header)}),
        (move_result(), {
            "source_workflow_storage_key": (source, source_header),
            "destination_workflow_storage_key": (destination, destination_header),
        }),
    ):
        result = {**base, **{field: raw for field, (raw, _) in projection.items()}}
        response = _mutation_response(result)

        assert response.status == 200
        assert json_module.loads(response.body) == result
        for field, (_, header) in projection.items():
            assert response.headers["X-Alchem-" + field.replace("_", "-")] == header
