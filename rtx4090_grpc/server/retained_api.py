"""Typed discovery and job receipts for the retained RTX scene operations."""

from pathlib import Path

from google.protobuf.json_format import MessageToDict

from a100sim.scenes.contracts import OPERATIONS as SHARED_OPERATIONS, PARAMETER_SCHEMAS as SHARED_SCHEMAS
from .rpc import native_pb2 as native


OPERATIONS = {**SHARED_OPERATIONS, "scene.preview": "Render one authored camera from the current retained USD stage"}
_capture_fields = SHARED_SCHEMAS["scene.capture"]["properties"]
PARAMETER_SCHEMAS = {**SHARED_SCHEMAS, "scene.preview": {
    "type": "object", "additionalProperties": False, "required": ["session_id", "camera"],
    "properties": {"session_id": _capture_fields["session_id"], "camera": _capture_fields["cameras"]["items"],
                   "size": _capture_fields["size"], "spp": _capture_fields["spp"]}}}


def capabilities(health):
    ready = health.get("status") == "ready"
    return native.Capabilities(
        api_version=health.get("api_version", ""), simulator_version=health.get("isaac_sim", ""),
        native_rtx=True, capabilities=[native.Capability(
            name="extension." + name, availability=native.AVAILABLE if ready else native.UNAVAILABLE,
            scope=scope, details={"parameter_schema": PARAMETER_SCHEMAS[name]})
            for name, scope in OPERATIONS.items()])


def catalog(health):
    operations = capabilities(health)
    return native.ApiCatalog(api_version=operations.api_version, simulator_version=operations.simulator_version,
        domains=[native.ApiDomain(id="sessions", title="Retained RTX scenes", features=[
            native.Feature(id="sessions." + name, title=scope, task_field="extension",
                request_type="isaacsim_rpc.v1.native.ExtensionRequest", support=native.AVAILABLE,
                availability=row.availability, request_schema=PARAMETER_SCHEMAS[name],
                fixed_arguments={"operation": name})
            for (name, scope), row in zip(OPERATIONS.items(), operations.capabilities)])])


def native_payload(request):
    field = request.WhichOneof("workload")
    workload = getattr(request, field)
    if workload.request_id and workload.request_id != request.request_id:
        raise ValueError("Nested and outer request_id disagree")
    if field == "native_task" and workload.WhichOneof("task") == "extension":
        operation = workload.extension.operation
        parameters = workload.extension.arguments
    elif field == "native_feature" and workload.feature_id.startswith("sessions."):
        operation = workload.feature_id.removeprefix("sessions.")
        parameters = workload.arguments
    else:
        raise ValueError("Select a retained operation advertised by this RTX service")
    if operation not in OPERATIONS:
        raise ValueError("Unknown retained scene operation")
    arguments = MessageToDict(parameters, preserving_proto_field_name=True)
    if {"operation", "source_path"} & arguments.keys():
        raise ValueError("operation and source_path are reserved by the retained executor")
    if operation == "asset.import":
        source = arguments.get("source")
        if not isinstance(source, dict) or set(source) != {"asset_id", "relative_path"}:
            raise ValueError("source requires asset_id and relative_path")
    return {"workload": field, "retained": {"operation": operation, "arguments": arguments}}


def artifact_handles(value, directory, artifact_id):
    if isinstance(value, dict):
        return {key: artifact_handles(item, directory, artifact_id) for key, item in value.items()}
    if isinstance(value, list):
        return [artifact_handles(item, directory, artifact_id) for item in value]
    if isinstance(value, str) and len(value) < 4096 and Path(value).is_absolute():
        path = Path(value)
        if path.is_relative_to(directory) and path.is_file():
            return "artifact://" + artifact_id(path.relative_to(directory.parent).as_posix())
    return value


def native_receipt(record):
    state = record["state"]
    states = {"queued": native.QUEUED, "running": native.RUNNING, "succeeded": native.SUCCEEDED,
              "interrupted": native.INTERRUPTED, "failed": native.FAILED}
    job = native.Job(job_id=record["id"], request_id=record.get("request_id", ""),
        operation="extension." + record["native_operation"], state=states[state],
        revision={"queued": 1, "running": 2}.get(state, 3), created_at_unix=record.get("queued_at", 0),
        result=record.get("native_result", {}))
    if state == "interrupted" and record.get("error") == "cancelled by client":
        job.state = native.CANCELLED
    if state in {"failed", "interrupted"}:
        job.failure.code = "FAILED_PRECONDITION" if state == "failed" else "CANCELLED"
        job.failure.message = record.get("error", "")
    for item in record.get("artifact_manifest", []):
        job.artifacts.add(**{key: item[key] for key in ("artifact_id", "name", "size_bytes", "media_type")})
    return job

