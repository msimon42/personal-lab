"""The complete fixture repair can be reviewed through real MCP calls."""
import asyncio
import base64
import hashlib
import io
import json
import zipfile

from mcp.client import Client

from agent_test_support import call_tool, result_data
from personal_lab.mcp_server import build_server_for_service
from test_lab_eu5 import lab as lab


def test_scan_stage_validate_prepare_export_and_policy_bound_artifact_over_mcp(lab):
    service, _ = lab

    async def run():
        async with Client(build_server_for_service(service, profile="dev"), mode="legacy") as client:
            async def tool(name, args):
                return await result_data(client, await call_tool(client, name, args))

            async def finished(run_id):
                for _ in range(300):
                    state = await tool("lab_run_get", {"run_id": run_id})
                    if state["state"] in {"succeeded", "failed", "cancelled", "interrupted"}:
                        assert state["state"] == "succeeded", state
                        return state["result"]
                    await asyncio.sleep(0.01)
                raise AssertionError("Fixture run did not finish")

            started = await tool("eu5_scan_start", {"installation_id": "fixture:F03",
                "playset_id": "fixture:F03:configured", "idempotency_key": "scan"})
            snapshot = (await finished(started["run_id"]))["scan_id"]
            findings = await tool("eu5_findings_list", {"snapshot_id": snapshot})
            assert findings["total"] and findings["items"]
            workspace = await tool("eu5_workspace_create", {"snapshot_id": snapshot, "idempotency_key": "stage"})
            mutation = {"workspace_id": workspace["workspace_id"], "expected_revision": 0}
            checked = await tool("eu5_validate_start", {**mutation, "idempotency_key": "validate"})
            assert (await finished(checked["run_id"]))["validation"]["experimental_eligible"]
            plan = await tool("eu5_deployment_prepare", {**mutation, "idempotency_key": "prepare"})
            assert plan["external_apply_available"] is False
            export_args = {**mutation, "idempotency_key": "export"}
            exported = await tool("eu5_workspace_export", export_args)
            assert exported == await tool("eu5_workspace_export", export_args)
            assert len(service.store.list("lab_artifact")) == 1
            raw, offset = bytearray(), 0
            while True:
                chunk = await tool("lab_artifact_read", {"artifact_id": exported["artifact_id"], "offset": offset, "limit": 128})
                raw.extend(base64.b64decode(chunk["bytes_base64"], validate=True))
                offset = chunk["next_offset"]
                if offset is None:
                    break
            assert hashlib.sha256(raw).hexdigest() == exported["sha256"]
            with zipfile.ZipFile(io.BytesIO(raw)) as archive:
                assert b"REPLACE:item = { x = 2 y = 3 }\n" in [archive.read(name) for name in archive.namelist()]
            service.store.put("configuration", {"id": "inference-policy", "policy": {"excluded_patterns": ["*"]}})
            denied = await call_tool(client, "lab_artifact_read", {"artifact_id": exported["artifact_id"]})
            assert denied.is_error
            assert json.loads(denied.content[0].text)["error"]["code"] == "PATH_DENIED"

    asyncio.run(run())
