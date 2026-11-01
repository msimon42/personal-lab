"""SDK2 contract helpers; artifact continuations still cross actual MCP tools."""
import base64
import hashlib
import json
import uuid


async def call_tool(client, name, arguments):
    name = name if name.startswith(("ck3_", "lab_", "eu5_")) else "ck3_" + name
    tools = {tool.name: tool for tool in (await client.list_tools()).tools}
    args = dict(arguments)
    if "idempotency_key" in tools[name].input_schema.get("required", []):
        args.setdefault("idempotency_key", uuid.uuid4().hex)
    return await client.call_tool(name, args)


async def result_data(client, response):
    assert not response.is_error, response
    envelope = json.loads(response.content[0].text)
    if envelope["data"] is not None:
        return envelope["data"]
    assert len(envelope["artifacts"]) == 1, envelope
    artifact = envelope["artifacts"][0]
    raw, offset = bytearray(), 0
    while True:
        page = await client.call_tool("lab_artifact_read", {"artifact_id": artifact["id"], "offset": offset})
        assert not page.is_error, page
        data = json.loads(page.content[0].text)["data"]
        raw.extend(base64.b64decode(data["bytes_base64"], validate=True))
        offset = data["next_offset"]
        if offset is None:
            break
    assert hashlib.sha256(raw).hexdigest() == artifact["sha256"]
    return json.loads(raw)
