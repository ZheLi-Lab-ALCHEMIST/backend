from __future__ import annotations
import json
import hashlib
import os
import re
import uuid
import glob
import shutil
import logging
import math
import tempfile
import unicodedata
from decimal import Decimal
from aiohttp import web
from urllib import parse
from comfy.cli_args import args
import folder_paths
from .app_settings import AppSettings
from collections.abc import Mapping
from typing import TypedDict

default_user = "default"
_MUTATION_RESPONSE_FAULT_PROJECTOR = None

_HEX64 = re.compile(r"^[a-f0-9]{64}$")
_STORAGE_KEY = re.compile(
    r"^(?!/)(?!.*(?:^|/)\.\.(?:/|$))(?!.*(?:^|/)\.(?:/|$))"
    r"(?!.*\\)(?!.*%)(?!.*//)(?!.*[?#\x00\r\n]).+$"
)
_PRECOMMIT_REASONS = {
    "destination_exists": 409,
    "cross_device_move_unavailable": 409,
    "filesystem_precondition_failed": 409,
    "atomic_no_replace_unavailable": 503,
    "project_context_unavailable": 503,
    "workflow_context_unavailable": 503,
    "workflow_identity_recovery_required": 503,
    "filesystem_operation_not_committed": 503,
}
_COMMON_PRECOMMIT_REASONS = {
    "project_context_unavailable",
    "workflow_context_unavailable",
    "workflow_identity_recovery_required",
    "filesystem_precondition_failed",
    "filesystem_operation_not_committed",
}


def _canonical_number(value):
    if isinstance(value, int):
        if abs(value) <= 9_007_199_254_740_991:
            return str(value)
        value = float(value)
    if not math.isfinite(value):
        raise ValueError("canonical JSON number must be finite")
    if value == 0:
        return "0"
    decimal_value = Decimal(repr(value))
    if 1e-6 <= abs(value) < 1e21:
        if value.is_integer():
            return str(int(value))
        return format(decimal_value, "f")
    sign, digits, exponent = decimal_value.normalize().as_tuple()
    coefficient = "".join(str(digit) for digit in digits)
    scientific_exponent = len(coefficient) + exponent - 1
    mantissa = coefficient[0]
    if len(coefficient) > 1:
        mantissa += "." + coefficient[1:]
    exponent_sign = "+" if scientific_exponent >= 0 else ""
    return (
        ("-" if sign else "")
        + mantissa
        + "e"
        + exponent_sign
        + str(scientific_exponent)
    )


def _canonical_json_text(value):
    if value is None:
        return "null"
    if value is True:
        return "true"
    if value is False:
        return "false"
    if isinstance(value, (int, float)):
        return _canonical_number(value)
    if isinstance(value, str):
        return json.dumps(value, ensure_ascii=False, separators=(",", ":"))
    if isinstance(value, list):
        return "[" + ",".join(_canonical_json_text(item) for item in value) + "]"
    if isinstance(value, dict):
        if any(not isinstance(key, str) for key in value):
            raise ValueError("canonical JSON object keys must be strings")
        keys = sorted(value, key=lambda key: key.encode("utf-16be"))
        return "{" + ",".join(
            f"{_canonical_json_text(key)}:{_canonical_json_text(value[key])}"
            for key in keys
        ) + "}"
    raise ValueError("value is outside the canonical JSON data model")


def _json_object_without_duplicate_keys(pairs):
    value = {}
    for key, item in pairs:
        if key in value:
            raise ValueError("duplicate JSON object key")
        value[key] = item
    return value


_CREATE_OR_MOVE_PRECOMMIT_REASONS = _COMMON_PRECOMMIT_REASONS | {
    "destination_exists",
    "atomic_no_replace_unavailable",
}
_MOVE_PRECOMMIT_REASONS = _CREATE_OR_MOVE_PRECOMMIT_REASONS | {
    "cross_device_move_unavailable",
}


def _valid_hex64(value):
    return isinstance(value, str) and _HEX64.fullmatch(value) is not None


def _valid_storage_key(value):
    return (
        isinstance(value, str)
        and value == unicodedata.normalize("NFC", value)
        and _STORAGE_KEY.fullmatch(value) is not None
    )


def _valid_workflow_storage_key(value):
    return _valid_storage_key(value) and value.endswith(".json")


def _valid_precommit_reason(operation, write_mode, reason):
    if operation == "write" and write_mode == "create":
        return reason in _CREATE_OR_MOVE_PRECOMMIT_REASONS
    if operation == "write" and write_mode == "replace":
        return reason in _COMMON_PRECOMMIT_REASONS
    if operation == "delete":
        return reason in _COMMON_PRECOMMIT_REASONS
    return reason in _MOVE_PRECOMMIT_REASONS


def _closed_result_fields(operation, durability=None):
    common = {
        "status", "mutation_request_id", "mutation_intent_digest",
        "project_instance_id", "operation",
    }
    if durability is None:
        return common | {"write_mode", "reason"}
    fields = common | {
        "durability", "ledger_revision", "workflow_revision", "workflow_id",
    }
    if operation == "write":
        fields |= {"workflow_storage_key", "workflow_fingerprint"}
    elif operation == "delete":
        fields |= {
            "deleted_workflow_storage_key", "deleted_content_fingerprint",
            "workflow_absent",
        }
    else:
        fields |= {
            "source_workflow_storage_key", "source_absent",
            "destination_workflow_storage_key",
            "destination_workflow_fingerprint",
        }
    if durability == "uncertain":
        fields |= {"transaction_id", "recovery_pending", "reason"}
    return fields


def _validate_mutation_result(result):
    if not isinstance(result, Mapping):
        return None
    result = dict(result)
    operation = result.get("operation")
    if operation not in {"write", "delete", "move"}:
        return None
    if not all(_valid_hex64(result.get(key)) for key in (
        "mutation_request_id", "mutation_intent_digest"
    )) or not result.get("project_instance_id") or not isinstance(
        result.get("project_instance_id"), str
    ):
        return None
    if result.get("status") == "precommit_failed":
        if set(result) != _closed_result_fields(operation):
            return None
        write_mode = result.get("write_mode")
        if write_mode not in ({"create", "replace"} if operation == "write" else {None}):
            return None
        if not _valid_precommit_reason(operation, write_mode, result.get("reason")):
            return None
        return result
    if result.get("status") != "committed":
        return None
    durability = result.get("durability")
    if durability not in {"confirmed", "uncertain"}:
        return None
    if set(result) != _closed_result_fields(operation, durability):
        return None
    if (
        not isinstance(result.get("ledger_revision"), int)
        or isinstance(result["ledger_revision"], bool)
        or result["ledger_revision"] < 1
        or not isinstance(result.get("workflow_revision"), int)
        or isinstance(result["workflow_revision"], bool)
        or result["workflow_revision"] < 1
    ):
        return None
    if not _valid_hex64(result.get("workflow_id")):
        return None
    if operation == "write":
        valid_operation = (
            _valid_workflow_storage_key(result.get("workflow_storage_key"))
            and _valid_hex64(result.get("workflow_fingerprint"))
        )
    elif operation == "delete":
        valid_operation = (
            _valid_workflow_storage_key(result.get("deleted_workflow_storage_key"))
            and _valid_hex64(result.get("deleted_content_fingerprint"))
            and result.get("workflow_absent") is True
        )
    else:
        valid_operation = (
            _valid_workflow_storage_key(result.get("source_workflow_storage_key"))
            and result.get("source_absent") is True
            and _valid_workflow_storage_key(
                result.get("destination_workflow_storage_key")
            )
            and _valid_hex64(result.get("destination_workflow_fingerprint"))
        )
    if not valid_operation:
        return None
    if durability == "uncertain" and not (
        _valid_hex64(result.get("transaction_id"))
        and result.get("recovery_pending") is True
        and result.get("reason") == "mutation_committed_durability_uncertain"
    ):
        return None
    return result


def _mutation_response(result, request=None):
    projector = _MUTATION_RESPONSE_FAULT_PROJECTOR
    if callable(projector):
        projected = projector(result, request)
        if projected is not None:
            return projected
    if result["status"] == "precommit_failed":
        return web.json_response(result, status=_PRECOMMIT_REASONS[result["reason"]])
    uncertain = result["durability"] == "uncertain"
    headers = _mutation_headers(result)
    return web.json_response(result, status=202 if uncertain else 200, headers=headers)


def _mutation_headers(result):
    uncertain = result["durability"] == "uncertain"
    headers = {
        "X-Alchem-Mutation-Disposition": (
            "committed_durability_uncertain" if uncertain
            else "committed_confirmed"
        ),
        "X-Alchem-Mutation-Operation": result["operation"],
        "X-Alchem-Mutation-Request-Id": result["mutation_request_id"],
        "X-Alchem-Mutation-Intent-Digest": result["mutation_intent_digest"],
        "X-Alchem-Project-Instance-Id": result["project_instance_id"],
        "X-Alchem-Workflow-Revision": str(result["workflow_revision"]),
        "X-Alchem-Workflow-Id": result["workflow_id"],
    }
    operation = result["operation"]
    if operation == "write":
        headers.update({
            "X-Alchem-Workflow-Storage-Key": result["workflow_storage_key"],
            "X-Alchem-Workflow-Fingerprint": result["workflow_fingerprint"],
        })
    elif operation == "delete":
        headers.update({
            "X-Alchem-Deleted-Workflow-Storage-Key": (
                result["deleted_workflow_storage_key"]
            ),
            "X-Alchem-Workflow-Absent": "true",
            "X-Alchem-Deleted-Content-Fingerprint": (
                result["deleted_content_fingerprint"]
            ),
        })
    else:
        headers.update({
            "X-Alchem-Source-Workflow-Storage-Key": (
                result["source_workflow_storage_key"]
            ),
            "X-Alchem-Source-Absent": "true",
            "X-Alchem-Destination-Workflow-Storage-Key": (
                result["destination_workflow_storage_key"]
            ),
            "X-Alchem-Destination-Workflow-Fingerprint": (
                result["destination_workflow_fingerprint"]
            ),
        })
    if uncertain:
        headers.update({
            "X-Alchem-Mutation-Transaction-Id": result["transaction_id"],
            "X-Alchem-Mutation-Recovery-Pending": "true",
        })
    return headers


def _invalid_request(reason="invalid_request", field="request_body"):
    return web.json_response(
        {"status": "rejected", "reason": reason, "field": field},
        status=400,
    )


def _is_canonical_saved_workflow_key(value):
    prefix = "workflows/"
    if not isinstance(value, str) or not value.startswith(prefix):
        return False
    relative = value[len(prefix):]
    return relative != ".index.json" and not relative.startswith(
        "workflows_active/"
    )


def _requires_workflow_mutation_owner(*storage_keys):
    return any(_is_canonical_saved_workflow_key(key) for key in storage_keys)


def _validate_transaction_request(
    *, operation, write_mode, source_key, destination_key, raw_body,
    mutation_request_id, project_instance_id, require_correlation=False
):
    if mutation_request_id is None and not require_correlation:
        return None
    if operation == "write":
        if write_mode is not None and write_mode not in {"create", "replace"}:
            return _invalid_request(field="write_mode")
    elif write_mode is not None:
        return _invalid_request(field="write_mode")
    if mutation_request_id is None:
        return _invalid_request()
    if (
        not _valid_hex64(mutation_request_id)
        or not project_instance_id
        or (operation == "write" and not raw_body)
    ):
        return _invalid_request()
    if operation == "write" and write_mode is None:
        return _invalid_request(field="write_mode")
    if source_key is not None and not _valid_workflow_storage_key(source_key):
        return _invalid_request(
            "workflow_storage_key_invalid", "source_storage_key"
        )
    if destination_key is not None and not _valid_workflow_storage_key(destination_key):
        return _invalid_request(
            "workflow_storage_key_invalid", "destination_storage_key"
        )
    return None


_MUTATION_SINGLE_VALUE_HEADERS = {
    "x-alchem-mutation-request-id",
    "x-alchem-project-instance-id",
    "x-alchem-workflow-id",
    "x-alchem-expected-workflow-revision",
    "x-alchem-expected-before-fingerprint",
    "x-alchem-expected-destination-absent",
    "x-alchem-after-content-fingerprint",
}


def _header_values(headers, name):
    getall = getattr(headers, "getall", None)
    if callable(getall):
        return list(getall(name, []))
    lowered = name.lower()
    return [value for key, value in headers.items() if str(key).lower() == lowered]


def _parse_mutation_correlation_headers(headers):
    values = {
        name: _header_values(headers, name)
        for name in (
            "X-Alchem-Mutation-Request-Id",
            "X-Alchem-Project-Instance-Id",
        )
    }
    if any(len(items) > 1 for items in values.values()):
        return None
    return tuple(items[0] if items else None for items in values.values())


def _has_duplicate_mutation_headers(headers):
    return any(
        len(_header_values(headers, name)) > 1
        for name in _MUTATION_SINGLE_VALUE_HEADERS
    )


def _parse_mutation_cas_headers(operation, write_mode, headers, raw_body):
    normalized = {}
    for name in _MUTATION_SINGLE_VALUE_HEADERS:
        values = _header_values(headers, name)
        if len(values) > 1:
            return None
        if values:
            normalized[name] = values[0]
    allowed_expected = {
        "x-alchem-expected-workflow-revision",
        "x-alchem-expected-before-fingerprint",
        "x-alchem-expected-destination-absent",
    }
    if any(
        key.startswith("x-alchem-expected-") and key not in allowed_expected
        for key in {str(header).lower() for header in headers.keys()}
    ):
        return None
    workflow_id = normalized.get("x-alchem-workflow-id")
    raw_revision = normalized.get("x-alchem-expected-workflow-revision")
    before_fingerprint = normalized.get("x-alchem-expected-before-fingerprint")
    raw_destination_absent = normalized.get(
        "x-alchem-expected-destination-absent"
    )
    after_fingerprint = normalized.get("x-alchem-after-content-fingerprint")
    if operation == "write":
        try:
            parsed_body = json.loads(
                bytes(raw_body).decode("utf-8"),
                object_pairs_hook=_json_object_without_duplicate_keys,
            )
            canonical_body = _canonical_json_text(parsed_body).encode("utf-8")
        except (TypeError, UnicodeError, json.JSONDecodeError, ValueError):
            return None
        if hashlib.sha256(canonical_body).hexdigest() != after_fingerprint:
            return None
    revision = None
    if raw_revision is not None:
        if not raw_revision.isdigit() or str(int(raw_revision)) != raw_revision:
            return None
        revision = int(raw_revision)
        if revision < 1:
            return None
    if raw_destination_absent not in {"true", "false"}:
        return None
    destination_absent = raw_destination_absent == "true"
    if operation == "write" and write_mode == "create":
        valid = (
            _valid_hex64(workflow_id)
            and revision is not None
            and before_fingerprint is None
            and destination_absent
            and _valid_hex64(after_fingerprint)
        )
    elif operation == "write" and write_mode == "replace":
        valid = (
            _valid_hex64(workflow_id)
            and revision is not None
            and _valid_hex64(before_fingerprint)
            and not destination_absent
            and _valid_hex64(after_fingerprint)
        )
    elif operation == "delete":
        valid = (
            _valid_hex64(workflow_id)
            and revision is not None
            and _valid_hex64(before_fingerprint)
            and not destination_absent
            and after_fingerprint is None
        )
    else:
        valid = (
            operation == "move"
            and _valid_hex64(workflow_id)
            and revision is not None
            and _valid_hex64(before_fingerprint)
            and destination_absent
            and after_fingerprint == before_fingerprint
        )
    if not valid:
        return None
    return {
        "workflow_id": workflow_id,
        "expected_workflow_revision": revision,
        "expected_before_fingerprint": before_fingerprint,
        "expected_destination_absent": destination_absent,
        "after_content_fingerprint": after_fingerprint,
    }


_DISPOSITION_REJECTION_STATUS = {
    "mutation_disposition_unknown": 404,
    "mutation_disposition_ack_mismatch": 412,
    "mutation_disposition_not_terminal": 409,
    "mutation_already_acknowledged": 409,
    "project_identity_mismatch": 409,
    "project_context_unavailable": 503,
}


def _disposition_rejection_response(
    result, *, project_instance_id, mutation_request_id=None
):
    if not isinstance(result, Mapping):
        return None
    required = {"status", "project_instance_id", "reason"}
    if mutation_request_id is not None:
        required.add("mutation_request_id")
    if result.get("status") != "rejected":
        return None
    reason = result.get("reason")
    status = _DISPOSITION_REJECTION_STATUS.get(reason)
    if reason == "mutation_already_acknowledged":
        required |= {"mutation_intent_digest", "result_digest"}
    if set(result) != required:
        return None
    if (
        status is None
        or result.get("project_instance_id") != project_instance_id
    ):
        return None
    if (
        mutation_request_id is not None
        and result.get("mutation_request_id") != mutation_request_id
    ):
        return None
    if any(not _valid_hex64(result.get(key)) for key in (
        "mutation_intent_digest", "result_digest"
    ) if key in result):
        return None
    return web.json_response(dict(result), status=status)


def _provider_failure_response():
    return web.json_response(
        {"status": "rejected", "reason": "mutation_provider_unavailable"},
        status=503,
    )


def _invalid_provider_result_response():
    return web.json_response(
        {"status": "rejected", "reason": "mutation_provider_result_invalid"},
        status=500,
    )


def _mutation_request_rejection_response(result):
    if not isinstance(result, Mapping) or set(result) != {
        "status", "mutation_request_id", "mutation_intent_digest",
        "project_instance_id", "reason",
    }:
        return None
    if result.get("status") != "rejected" or result.get("reason") not in {
        "mutation_already_acknowledged",
        "mutation_request_id_reused_with_different_intent",
    }:
        return None
    if not all(_valid_hex64(result.get(key)) for key in (
        "mutation_request_id", "mutation_intent_digest"
    )) or not isinstance(result.get("project_instance_id"), str) or not result[
        "project_instance_id"
    ]:
        return None
    return web.json_response(dict(result), status=409)


def _validate_workflow_identity_inventory(result, project_instance_id):
    if not isinstance(result, Mapping) or set(result) != {
        "schema_id", "project_instance_id", "ledger_revision", "identities"
    }:
        return None
    ledger_revision = result.get("ledger_revision")
    identities = result.get("identities")
    if (
        result.get("schema_id") != "pm.workbench-workflow-identity-inventory.v2"
        or result.get("project_instance_id") != project_instance_id
        or type(ledger_revision) is not int
        or ledger_revision < 0
        or not isinstance(identities, list)
    ):
        return None
    filenames = []
    storage_keys = []
    workflow_ids = set()
    for identity in identities:
        if not isinstance(identity, Mapping):
            return None
        storage_key = identity.get("workflow_storage_key")
        expected_fields = {
            "workflow_id", "workflow_filename", "workflow_storage_key",
            "workflow_revision", "workflow_generation",
            "host_workflow_instance_id",
        }
        if storage_key is not None:
            expected_fields.add("workflow_fingerprint")
        if (
            set(identity) != expected_fields
            or not _valid_hex64(identity.get("workflow_id"))
            or not _valid_hex64(identity.get("workflow_filename"))
            or type(identity.get("workflow_revision")) is not int
            or identity["workflow_revision"] < 1
            or type(identity.get("workflow_generation")) is not int
            or identity["workflow_generation"] < 1
            or not isinstance(identity.get("host_workflow_instance_id"), str)
            or not identity["host_workflow_instance_id"]
            or (
                storage_key is not None
                and (
                    not _valid_workflow_storage_key(storage_key)
                    or not _valid_hex64(identity.get("workflow_fingerprint"))
                )
            )
        ):
            return None
        if identity["workflow_id"] in workflow_ids:
            return None
        workflow_ids.add(identity["workflow_id"])
        filenames.append(identity["workflow_filename"])
        if storage_key is not None:
            storage_keys.append(storage_key)
    if (
        filenames != sorted(set(filenames))
        or len(storage_keys) != len(set(storage_keys))
    ):
        return None
    return dict(result)


def _result_matches_request(result, request):
    if result.get("operation") != request["operation"]:
        return False
    request_id = request.get("mutation_request_id")
    project_id = request.get("project_instance_id")
    if request_id is not None and result.get("mutation_request_id") != request_id:
        return False
    if project_id is not None and result.get("project_instance_id") != project_id:
        return False
    prefix = "workflows/"

    def _relative_storage_key(value):
        return value[len(prefix):] if isinstance(value, str) and value.startswith(prefix) else None

    operation = request["operation"]
    write_mode = request.get("write_mode")
    source = _relative_storage_key(request.get("source_storage_key"))
    destination = _relative_storage_key(request.get("destination_storage_key"))
    intent = {
        "schema_id": "pm.workflow-mutation-intent.v1",
        "operation": operation,
        "write_mode": write_mode,
        "source_storage_key": (
            None if operation == "write" and write_mode == "create" else source
        ),
        "destination_storage_key": destination,
        "workflow_id": request.get("workflow_id"),
        "expected_workflow_revision": request.get("expected_workflow_revision"),
        "expected_before_fingerprint": request.get("expected_before_fingerprint"),
        "expected_destination_absent": request.get("expected_destination_absent"),
        "after_content_fingerprint": request.get("after_content_fingerprint"),
    }
    intent_digest = hashlib.sha256(
        _canonical_json_text(intent).encode("utf-8")
    ).hexdigest()
    if result.get("mutation_intent_digest") != intent_digest:
        return False
    if result.get("status") == "precommit_failed":
        return result.get("write_mode") == request.get("write_mode")
    workflow_id = request.get("workflow_id")
    if workflow_id is not None and result.get("workflow_id") != workflow_id:
        return False
    expected_revision = request.get("expected_workflow_revision")
    if (
        expected_revision is not None
        and result.get("workflow_revision") != expected_revision + 1
    ):
        return False

    if operation == "write":
        return (
            result.get("workflow_storage_key") == destination
            and result.get("workflow_fingerprint")
            == request.get("after_content_fingerprint")
        )
    if operation == "delete":
        return (
            result.get("deleted_workflow_storage_key") == source
            and result.get("deleted_content_fingerprint")
            == request.get("expected_before_fingerprint")
        )
    return (
        result.get("source_workflow_storage_key") == source
        and result.get("destination_workflow_storage_key") == destination
        and result.get("destination_workflow_fingerprint")
        == request.get("expected_before_fingerprint")
    )


class FileInfo(TypedDict):
    path: str
    size: int
    modified: int
    created: int


def get_file_info(path: str, relative_to: str) -> FileInfo:
    return {
        "path": os.path.relpath(path, relative_to).replace(os.sep, '/'),
        "size": os.path.getsize(path),
        "modified": os.path.getmtime(path),
        "created": os.path.getctime(path)
    }


class UserManager():
    def __init__(self):
        user_directory = folder_paths.get_user_directory()

        self.settings = AppSettings(self)
        self._userdata_mutation_transaction_provider = None
        if not os.path.exists(user_directory):
            os.makedirs(user_directory, exist_ok=True)
            if not args.multi_user:
                logging.warning("****** User settings have been changed to be stored on the server instead of browser storage. ******")
                logging.warning("****** For multi-user setups add the --multi-user CLI argument to enable multiple user profiles. ******")

        if args.multi_user:
            if os.path.isfile(self.get_users_file()):
                with open(self.get_users_file()) as f:
                    self.users = json.load(f)
            else:
                self.users = {}
        else:
            self.users = {"default": "default"}

    def add_userdata_mutation_transaction_provider(self, provider):
        required_methods = {
            "matches",
            "execute",
            "get_userdata_mutation_disposition",
            "list_userdata_mutation_dispositions",
            "list_userdata_workflow_identities",
            "acknowledge_userdata_mutation_disposition",
        }
        if any(not callable(getattr(provider, name, None)) for name in required_methods):
            raise TypeError("userdata mutation transaction provider contract is incomplete")
        if self._userdata_mutation_transaction_provider is not None:
            raise RuntimeError("userdata mutation transaction provider already registered")
        self._userdata_mutation_transaction_provider = provider

        removed = False

        def remove_provider():
            nonlocal removed
            if removed:
                return
            if self._userdata_mutation_transaction_provider is not provider:
                raise RuntimeError("userdata mutation transaction provider ownership changed")
            self._userdata_mutation_transaction_provider = None
            removed = True

        return remove_provider

    async def get_userdata_mutation_disposition(
        self, project_instance_id, mutation_request_id
    ):
        return await self._call_disposition_provider(
            "get_userdata_mutation_disposition",
            project_instance_id=project_instance_id,
            mutation_request_id=mutation_request_id,
        )

    async def list_userdata_mutation_dispositions(self, project_instance_id):
        return await self._call_disposition_provider(
            "list_userdata_mutation_dispositions",
            project_instance_id=project_instance_id,
        )

    async def list_userdata_workflow_identities(self, project_instance_id):
        return await self._call_disposition_provider(
            "list_userdata_workflow_identities",
            project_instance_id=project_instance_id,
        )

    async def acknowledge_userdata_mutation_disposition(
        self, project_instance_id, mutation_request_id, result_digest
    ):
        return await self._call_disposition_provider(
            "acknowledge_userdata_mutation_disposition",
            project_instance_id=project_instance_id,
            mutation_request_id=mutation_request_id,
            result_digest=result_digest,
        )

    async def _call_disposition_provider(self, method_name, **kwargs):
        provider = self._userdata_mutation_transaction_provider
        method = getattr(provider, method_name, None) if provider is not None else None
        if method is None:
            raise RuntimeError("userdata mutation disposition provider unavailable")
        return await method(**kwargs)

    async def _run_userdata_mutation_transaction(self, **kwargs):
        response_request = kwargs.pop("response_request", None)
        require_correlation = kwargs.pop("require_correlation", False)
        provider = self._userdata_mutation_transaction_provider
        if provider is None:
            return _provider_failure_response() if require_correlation else None
        try:
            matched = provider.matches(**kwargs)
        except Exception:
            logging.exception("Userdata mutation transaction provider match failed")
            return _provider_failure_response()
        if matched is False:
            return _provider_failure_response() if require_correlation else None
        if matched is not True:
            return _invalid_provider_result_response()
        invalid = _validate_transaction_request(
            operation=kwargs["operation"],
            write_mode=kwargs["write_mode"],
            source_key=kwargs["source_storage_key"],
            destination_key=kwargs["destination_storage_key"],
            raw_body=kwargs["raw_body"],
            mutation_request_id=kwargs["mutation_request_id"],
            project_instance_id=kwargs["project_instance_id"],
            require_correlation=True,
        )
        if invalid is not None:
            return invalid
        cas_facts = _parse_mutation_cas_headers(
            kwargs["operation"],
            kwargs["write_mode"],
            kwargs["request_headers"],
            kwargs["raw_body"],
        )
        if cas_facts is None:
            return _invalid_request()
        kwargs.update(cas_facts)
        try:
            result = await provider.execute(**kwargs)
        except Exception:
            logging.exception("Userdata mutation transaction provider failed")
            return _provider_failure_response()
        rejection = _mutation_request_rejection_response(result)
        if rejection is not None:
            if kwargs.get("mutation_request_id") not in {
                None, result["mutation_request_id"]
            } or kwargs.get("project_instance_id") not in {
                None, result["project_instance_id"]
            }:
                return _invalid_provider_result_response()
            return rejection
        result = _validate_mutation_result(result)
        if result is None or not _result_matches_request(result, kwargs):
            logging.error("Userdata mutation transaction provider returned invalid result")
            return web.json_response(
                {"status": "rejected", "reason": "mutation_provider_result_invalid"},
                status=500,
            )
        return _mutation_response(result, response_request)

    def _atomic_native_write(self, path, body):
        parent = os.path.dirname(path)
        fd, stage_path = tempfile.mkstemp(prefix=".comfy-userdata-", dir=parent)
        try:
            with os.fdopen(fd, "wb") as stage:
                stage.write(body)
                stage.flush()
                os.fsync(stage.fileno())
            os.replace(stage_path, path)
            stage_path = None
            dir_fd = os.open(parent, os.O_RDONLY)
            try:
                os.fsync(dir_fd)
            finally:
                os.close(dir_fd)
        finally:
            if stage_path is not None:
                try:
                    os.unlink(stage_path)
                except FileNotFoundError:
                    pass

    def get_users_file(self):
        return os.path.join(folder_paths.get_user_directory(), "users.json")

    def get_request_user_id(self, request):
        user = "default"
        if args.multi_user and "comfy-user" in request.headers:
            user = request.headers["comfy-user"]

        if user not in self.users:
            raise KeyError("Unknown user: " + user)

        return user

    def get_request_user_filepath(self, request, file, type="userdata", create_dir=True):
        user_directory = folder_paths.get_user_directory()

        if type == "userdata":
            root_dir = user_directory
        else:
            raise KeyError("Unknown filepath type:" + type)

        user = self.get_request_user_id(request)
        path = user_root = os.path.abspath(os.path.join(root_dir, user))

        # prevent leaving /{type}
        if os.path.commonpath((root_dir, user_root)) != root_dir:
            return None

        if file is not None:
            # Check if filename is url encoded
            if "%" in file:
                file = parse.unquote(file)

            # prevent leaving /{type}/{user}
            path = os.path.abspath(os.path.join(user_root, file))
            if os.path.commonpath((user_root, path)) != user_root:
                return None

        parent = os.path.split(path)[0]

        if create_dir and not os.path.exists(parent):
            os.makedirs(parent, exist_ok=True)

        return path

    def add_user(self, name):
        name = name.strip()
        if not name:
            raise ValueError("username not provided")
        user_id = re.sub("[^a-zA-Z0-9-_]+", '-', name)
        user_id = user_id + "_" + str(uuid.uuid4())

        self.users[user_id] = name

        with open(self.get_users_file(), "w") as f:
            json.dump(self.users, f)

        return user_id

    def add_routes(self, routes):
        self.settings.add_routes(routes)

        @routes.get("/users")
        async def get_users(request):
            if args.multi_user:
                return web.json_response({"storage": "server", "users": self.users})
            else:
                user_dir = self.get_request_user_filepath(request, None, create_dir=False)
                return web.json_response({
                    "storage": "server",
                    "migrated": os.path.exists(user_dir)
                })

        @routes.post("/users")
        async def post_users(request):
            body = await request.json()
            username = body["username"]
            if username in self.users.values():
                return web.json_response({"error": "Duplicate username."}, status=400)

            user_id = self.add_user(username)
            return web.json_response(user_id)

        @routes.get("/userdata")
        async def listuserdata(request):
            """
            List user data files in a specified directory.

            This endpoint allows listing files in a user's data directory, with options for recursion,
            full file information, and path splitting.

            Query Parameters:
            - dir (required): The directory to list files from.
            - recurse (optional): If "true", recursively list files in subdirectories.
            - full_info (optional): If "true", return detailed file information (path, size, modified time).
            - split (optional): If "true", split file paths into components (only applies when full_info is false).

            Returns:
            - 400: If 'dir' parameter is missing.
            - 403: If the requested path is not allowed.
            - 404: If the requested directory does not exist.
            - 200: JSON response with the list of files or file information.

            The response format depends on the query parameters:
            - Default: List of relative file paths.
            - full_info=true: List of dictionaries with file details.
            - split=true (and full_info=false): List of lists, each containing path components.
            """
            directory = request.rel_url.query.get('dir', '')
            if not directory:
                return web.Response(status=400, text="Directory not provided")

            path = self.get_request_user_filepath(request, directory)
            if not path:
                return web.Response(status=403, text="Invalid directory")

            if not os.path.exists(path):
                return web.Response(status=404, text="Directory not found")

            recurse = request.rel_url.query.get('recurse', '').lower() == "true"
            full_info = request.rel_url.query.get('full_info', '').lower() == "true"
            split_path = request.rel_url.query.get('split', '').lower() == "true"

            # Use different patterns based on whether we're recursing or not
            if recurse:
                pattern = os.path.join(glob.escape(path), '**', '*')
            else:
                pattern = os.path.join(glob.escape(path), '*')

            def process_full_path(full_path: str) -> FileInfo | str | list[str]:
                if full_info:
                    return get_file_info(full_path, path)

                rel_path = os.path.relpath(full_path, path).replace(os.sep, '/')
                if split_path:
                    return [rel_path] + rel_path.split('/')

                return rel_path

            results = [
                process_full_path(full_path)
                for full_path in glob.glob(pattern, recursive=recurse)
                if os.path.isfile(full_path)
            ]

            return web.json_response(results)

        @routes.get("/v2/userdata")
        async def list_userdata_v2(request):
            """
            List files and directories in a user's data directory.

            This endpoint provides a structured listing of contents within a specified
            subdirectory of the user's data storage.

            Query Parameters:
            - path (optional): The relative path within the user's data directory
                               to list. Defaults to the root ('').

            Returns:
            - 400: If the requested path is invalid, outside the user's data directory, or is not a directory.
            - 404: If the requested path does not exist.
            - 403: If the user is invalid.
            - 500: If there is an error reading the directory contents.
            - 200: JSON response containing a list of file and directory objects.
                   Each object includes:
                   - name: The name of the file or directory.
                   - type: 'file' or 'directory'.
                   - path: The relative path from the user's data root.
                   - size (for files): The size in bytes.
                   - modified (for files): The last modified timestamp (Unix epoch).
            """
            requested_rel_path = request.rel_url.query.get('path', '')

            # URL-decode the path parameter
            try:
                requested_rel_path = parse.unquote(requested_rel_path)
            except Exception as e:
                logging.warning(f"Failed to decode path parameter: {requested_rel_path}, Error: {e}")
                return web.Response(status=400, text="Invalid characters in path parameter")


            # Check user validity and get the absolute path for the requested directory
            try:
                 base_user_path = self.get_request_user_filepath(request, None, create_dir=False)

                 if requested_rel_path:
                     target_abs_path = self.get_request_user_filepath(request, requested_rel_path, create_dir=False)
                 else:
                     target_abs_path = base_user_path

            except KeyError as e:
                 # Invalid user detected by get_request_user_id inside get_request_user_filepath
                 logging.warning(f"Access denied for user: {e}")
                 return web.Response(status=403, text="Invalid user specified in request")


            if not target_abs_path:
                 # Path traversal or other issue detected by get_request_user_filepath
                 return web.Response(status=400, text="Invalid path requested")

            # Handle cases where the user directory or target path doesn't exist
            if not os.path.exists(target_abs_path):
                # Check if it's the base user directory that's missing (new user case)
                if target_abs_path == base_user_path:
                    # It's okay if the base user directory doesn't exist yet, return empty list
                     return web.json_response([])
                else:
                    # A specific subdirectory was requested but doesn't exist
                     return web.Response(status=404, text="Requested path not found")

            if not os.path.isdir(target_abs_path):
                 return web.Response(status=400, text="Requested path is not a directory")

            results = []
            try:
                for root, dirs, files in os.walk(target_abs_path, topdown=True):
                    # Process directories
                    for dir_name in dirs:
                        dir_path = os.path.join(root, dir_name)
                        rel_path = os.path.relpath(dir_path, base_user_path).replace(os.sep, '/')
                        results.append({
                            "name": dir_name,
                            "path": rel_path,
                            "type": "directory"
                        })

                    # Process files
                    for file_name in files:
                        file_path = os.path.join(root, file_name)
                        rel_path = os.path.relpath(file_path, base_user_path).replace(os.sep, '/')
                        entry_info = {
                            "name": file_name,
                            "path": rel_path,
                            "type": "file"
                        }
                        try:
                            stats = os.stat(file_path) # Use os.stat for potentially better performance with os.walk
                            entry_info["size"] = stats.st_size
                            entry_info["modified"] = stats.st_mtime
                        except OSError as stat_error:
                            logging.warning(f"Could not stat file {file_path}: {stat_error}")
                            pass # Include file with available info
                        results.append(entry_info)
            except OSError as e:
                logging.error(f"Error listing directory {target_abs_path}: {e}")
                return web.Response(status=500, text="Error reading directory contents")

            # Sort results alphabetically, directories first then files
            results.sort(key=lambda x: (x['type'] != 'directory', x['name'].lower()))

            return web.json_response(results)

        def get_user_data_path(request, check_exists = False, param = "file"):
            file = request.match_info.get(param, None)
            if not file:
                return web.Response(status=400)

            path = self.get_request_user_filepath(request, file)
            if not path:
                return web.Response(status=403)

            if check_exists and not os.path.exists(path):
                return web.Response(status=404)

            return path

        def get_logical_storage_key(request, param="file"):
            raw_key = request.match_info.get(param)
            if not raw_key:
                return None
            logical_key = parse.unquote(raw_key)
            if logical_key.startswith("workflows\\"):
                return None
            if any(part in {".", ".."} for part in logical_key.split("/")):
                return None
            if "%" in logical_key:
                decoded_again = parse.unquote(logical_key)
                if (
                    decoded_again != logical_key
                    and (
                        "\\" in decoded_again
                        or any(
                            part in {".", ".."}
                            for part in decoded_again.split("/")
                        )
                    )
                ):
                    return None
            return logical_key

        @routes.get("/userdata/mutation-disposition/{mutation_request_id}")
        async def get_mutation_disposition(request):
            mutation_request_id = request.match_info["mutation_request_id"]
            project_instance_id = request.query.get("project_instance_id")
            if not _valid_hex64(mutation_request_id) or not project_instance_id:
                return _invalid_request()
            try:
                result = await self.get_userdata_mutation_disposition(
                    project_instance_id, mutation_request_id
                )
            except Exception:
                logging.exception("Userdata mutation disposition lookup failed")
                return _provider_failure_response()
            mutation_result = _validate_mutation_result(result)
            if (
                mutation_result is not None
                and mutation_result["project_instance_id"] == project_instance_id
                and mutation_result["mutation_request_id"] == mutation_request_id
            ):
                return _mutation_response(mutation_result, request)
            rejection = _disposition_rejection_response(
                result,
                project_instance_id=project_instance_id,
                mutation_request_id=mutation_request_id,
            )
            return rejection if rejection is not None else _invalid_provider_result_response()

        @routes.get("/userdata/mutation-dispositions")
        async def list_mutation_dispositions(request):
            project_instance_id = request.query.get("project_instance_id")
            if not project_instance_id:
                return _invalid_request()
            try:
                result = await self.list_userdata_mutation_dispositions(
                    project_instance_id
                )
            except Exception:
                logging.exception("Userdata mutation disposition listing failed")
                return _provider_failure_response()
            if isinstance(result, Mapping) and set(result) == {
                "project_instance_id", "results"
            } and result.get("project_instance_id") == project_instance_id:
                results = result.get("results")
                validated = (
                    [_validate_mutation_result(item) for item in results]
                    if isinstance(results, list) else None
                )
                if (
                    validated is not None
                    and all(validated)
                    and all(
                        item["project_instance_id"] == project_instance_id
                        for item in validated
                    )
                ):
                    ids = [item["mutation_request_id"] for item in validated]
                    if ids == sorted(set(ids)):
                        return web.json_response(dict(result))
            rejection = _disposition_rejection_response(
                result, project_instance_id=project_instance_id
            )
            return rejection if rejection is not None else _invalid_provider_result_response()

        @routes.get("/userdata/workflow-identities")
        async def list_workflow_identities(request):
            project_instance_id = request.query.get("project_instance_id")
            if not project_instance_id:
                return _invalid_request()
            try:
                result = await self.list_userdata_workflow_identities(
                    project_instance_id
                )
            except Exception:
                logging.exception("Userdata workflow identity listing failed")
                return _provider_failure_response()
            inventory = _validate_workflow_identity_inventory(
                result, project_instance_id
            )
            if inventory is not None:
                return web.json_response(inventory)
            rejection = _disposition_rejection_response(
                result, project_instance_id=project_instance_id
            )
            return (
                rejection
                if rejection is not None
                else _invalid_provider_result_response()
            )

        @routes.delete("/userdata/mutation-disposition/{mutation_request_id}")
        async def acknowledge_mutation_disposition(request):
            path_id = request.match_info["mutation_request_id"]
            try:
                body = await request.json()
            except (json.JSONDecodeError, TypeError):
                return _invalid_request()
            if not isinstance(body, Mapping) or set(body) != {
                "project_instance_id", "mutation_request_id", "result_digest"
            }:
                return _invalid_request()
            project_instance_id = body.get("project_instance_id")
            body_id = body.get("mutation_request_id")
            result_digest = body.get("result_digest")
            if (
                not project_instance_id
                or not _valid_hex64(path_id)
                or not _valid_hex64(body_id)
                or not _valid_hex64(result_digest)
            ):
                return _invalid_request()
            if path_id != body_id:
                return web.json_response({
                    "status": "rejected",
                    "project_instance_id": project_instance_id,
                    "mutation_request_id": body_id,
                    "reason": "mutation_disposition_ack_mismatch",
                }, status=412)
            try:
                result = await self.acknowledge_userdata_mutation_disposition(
                    project_instance_id, body_id, result_digest
                )
            except Exception:
                logging.exception("Userdata mutation disposition acknowledgement failed")
                return _provider_failure_response()
            if result is None:
                return web.Response(status=204)
            rejection = _disposition_rejection_response(
                result,
                project_instance_id=project_instance_id,
                mutation_request_id=body_id,
            )
            return rejection if rejection is not None else _invalid_provider_result_response()

        @routes.get("/userdata/{file}")
        async def getuserdata(request):
            path = get_user_data_path(request, check_exists=True)
            if not isinstance(path, str):
                return path

            return web.FileResponse(path)

        @routes.post("/userdata/{file}")
        async def post_userdata(request):
            """
            Upload or update a user data file.

            This endpoint handles file uploads to a user's data directory, with options for
            controlling overwrite behavior and response format.

            Query Parameters:
            - overwrite (optional): If "false", prevents overwriting existing files. Defaults to "true".
            - full_info (optional): If "true", returns detailed file information (path, size, modified time).
                                  If "false", returns only the relative file path.

            Path Parameters:
            - file: The target file path (URL encoded if necessary).

            Returns:
            - 400: If 'file' parameter is missing.
            - 403: If the requested path is not allowed.
            - 409: If overwrite=false and the file already exists.
            - 200: JSON response with either:
                  - Full file information (if full_info=true)
                  - Relative file path (if full_info=false)

            The request body should contain the raw file content to be written.
            """
            overwrite = request.query.get("overwrite", 'true') != "false"
            full_info = request.query.get('full_info', 'false').lower() == "true"

            logical_key = get_logical_storage_key(request)
            if logical_key is None:
                return _invalid_request(
                    "workflow_storage_key_invalid", "destination_storage_key"
                )

            body = await request.read()
            write_mode = request.query.get("write_mode")
            require_correlation = _requires_workflow_mutation_owner(logical_key)
            if require_correlation and _has_duplicate_mutation_headers(
                request.headers
            ):
                return _invalid_request()
            correlation = _parse_mutation_correlation_headers(request.headers)
            if correlation is None:
                if require_correlation:
                    return _invalid_request()
                mutation_request_id = request.headers.get(
                    "X-Alchem-Mutation-Request-Id"
                )
                project_instance_id = request.headers.get(
                    "X-Alchem-Project-Instance-Id"
                )
            else:
                mutation_request_id, project_instance_id = correlation
            if require_correlation:
                invalid = _validate_transaction_request(
                    operation="write",
                    write_mode=write_mode,
                    source_key=logical_key,
                    destination_key=logical_key,
                    raw_body=body,
                    mutation_request_id=mutation_request_id,
                    project_instance_id=project_instance_id,
                    require_correlation=True,
                )
                if invalid is not None:
                    return invalid
            transaction_response = await self._run_userdata_mutation_transaction(
                operation="write",
                write_mode=write_mode,
                source_storage_key=logical_key,
                destination_storage_key=logical_key,
                raw_body=body,
                mutation_request_id=mutation_request_id,
                project_instance_id=project_instance_id,
                require_correlation=require_correlation,
                user_id=self.get_request_user_id(request),
                request_headers=request.headers,
                request_query=dict(request.query),
                response_request=request,
            )
            if transaction_response is not None:
                return transaction_response

            path = get_user_data_path(request)
            if not isinstance(path, str):
                return path

            if not overwrite and os.path.exists(path):
                return web.Response(status=409, text="File already exists")

            try:
                self._atomic_native_write(path, body)
            except OSError as e:
                logging.warning(f"Error saving file '{path}': {e}")
                return web.Response(
                    status=400,
                    reason="Invalid filename. Please avoid special characters like :\\/*?\"<>|"
                )

            user_path = self.get_request_user_filepath(request, None)
            if full_info:
                resp = get_file_info(path, user_path)
            else:
                resp = os.path.relpath(path, user_path)

            return web.json_response(resp)

        @routes.delete("/userdata/{file}")
        async def delete_userdata(request):
            logical_key = get_logical_storage_key(request)
            if logical_key is None:
                return _invalid_request(
                    "workflow_storage_key_invalid", "source_storage_key"
                )
            require_correlation = _requires_workflow_mutation_owner(logical_key)
            if require_correlation and _has_duplicate_mutation_headers(
                request.headers
            ):
                return _invalid_request()
            correlation = _parse_mutation_correlation_headers(request.headers)
            if correlation is None:
                if require_correlation:
                    return _invalid_request()
                mutation_request_id = request.headers.get(
                    "X-Alchem-Mutation-Request-Id"
                )
                project_instance_id = request.headers.get(
                    "X-Alchem-Project-Instance-Id"
                )
            else:
                mutation_request_id, project_instance_id = correlation
            if require_correlation:
                invalid = _validate_transaction_request(
                    operation="delete",
                    write_mode=request.query.get("write_mode"),
                    source_key=logical_key,
                    destination_key=None,
                    raw_body=b"",
                    mutation_request_id=mutation_request_id,
                    project_instance_id=project_instance_id,
                    require_correlation=True,
                )
                if invalid is not None:
                    return invalid
            transaction_response = await self._run_userdata_mutation_transaction(
                operation="delete",
                write_mode=None,
                source_storage_key=logical_key,
                destination_storage_key=None,
                raw_body=b"",
                mutation_request_id=mutation_request_id,
                project_instance_id=project_instance_id,
                require_correlation=require_correlation,
                user_id=self.get_request_user_id(request),
                request_headers=request.headers,
                request_query=dict(request.query),
                response_request=request,
            )
            if transaction_response is not None:
                return transaction_response

            path = get_user_data_path(request, check_exists=True)
            if not isinstance(path, str):
                return path

            os.remove(path)

            return web.Response(status=204)

        @routes.post("/userdata/{file}/move/{dest}")
        async def move_userdata(request):
            """
            Move or rename a user data file.

            This endpoint handles moving or renaming files within a user's data directory, with options for
            controlling overwrite behavior and response format.

            Path Parameters:
            - file: The source file path (URL encoded if necessary)
            - dest: The destination file path (URL encoded if necessary)

            Query Parameters:
            - overwrite (optional): If "false", prevents overwriting existing files. Defaults to "true".
            - full_info (optional): If "true", returns detailed file information (path, size, modified time).
                                  If "false", returns only the relative file path.

            Returns:
            - 400: If either 'file' or 'dest' parameter is missing
            - 403: If either requested path is not allowed
            - 404: If the source file does not exist
            - 409: If overwrite=false and the destination file already exists
            - 200: JSON response with either:
                  - Full file information (if full_info=true)
                  - Relative file path (if full_info=false)
            """
            source_key = get_logical_storage_key(request)
            if source_key is None:
                return _invalid_request(
                    "workflow_storage_key_invalid", "source_storage_key"
                )
            destination_key = get_logical_storage_key(request, param="dest")
            if destination_key is None:
                return _invalid_request(
                    "workflow_storage_key_invalid", "destination_storage_key"
                )
            source_is_saved = _is_canonical_saved_workflow_key(source_key)
            destination_is_saved = _is_canonical_saved_workflow_key(
                destination_key
            )
            if source_is_saved != destination_is_saved:
                return _invalid_request(
                    "workflow_namespace_mismatch", "destination_storage_key"
                )
            require_correlation = source_is_saved or destination_is_saved
            if require_correlation and _has_duplicate_mutation_headers(
                request.headers
            ):
                return _invalid_request()
            correlation = _parse_mutation_correlation_headers(request.headers)
            if correlation is None:
                if require_correlation:
                    return _invalid_request()
                mutation_request_id = request.headers.get(
                    "X-Alchem-Mutation-Request-Id"
                )
                project_instance_id = request.headers.get(
                    "X-Alchem-Project-Instance-Id"
                )
            else:
                mutation_request_id, project_instance_id = correlation
            if require_correlation:
                invalid = _validate_transaction_request(
                    operation="move",
                    write_mode=request.query.get("write_mode"),
                    source_key=source_key,
                    destination_key=destination_key,
                    raw_body=b"",
                    mutation_request_id=mutation_request_id,
                    project_instance_id=project_instance_id,
                    require_correlation=True,
                )
                if invalid is not None:
                    return invalid
            transaction_response = await self._run_userdata_mutation_transaction(
                operation="move",
                write_mode=None,
                source_storage_key=source_key,
                destination_storage_key=destination_key,
                raw_body=b"",
                mutation_request_id=mutation_request_id,
                project_instance_id=project_instance_id,
                require_correlation=require_correlation,
                user_id=self.get_request_user_id(request),
                request_headers=request.headers,
                request_query=dict(request.query),
                response_request=request,
            )
            if transaction_response is not None:
                return transaction_response

            source = get_user_data_path(request, check_exists=True)
            if not isinstance(source, str):
                return source

            dest = get_user_data_path(request, check_exists=False, param="dest")
            if not isinstance(dest, str):
                return dest

            overwrite = request.query.get("overwrite", 'true') != "false"
            full_info = request.query.get('full_info', 'false').lower() == "true"

            if not overwrite and os.path.exists(dest):
                return web.Response(status=409, text="File already exists")

            logging.info(f"moving '{source}' -> '{dest}'")
            shutil.move(source, dest)

            user_path = self.get_request_user_filepath(request, None)
            if full_info:
                resp = get_file_info(dest, user_path)
            else:
                resp = os.path.relpath(dest, user_path)

            return web.json_response(resp)
