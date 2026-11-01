"""One direct stdio execution owner per instance; history survives reconnects."""
import asyncio
import os
import sys

from mcp import StdioServerParameters
from mcp.client import Client


def test_stdio_owner_lock_and_reconnect_preserve_instance(tmp_path):
    root = tmp_path / "managed"
    command = [sys.executable, "-m", "personal_lab", "mcp", "--profile", "core-only", "--state-dir", str(root)]
    async def run():
        params = StdioServerParameters(command=command[0], args=command[1:])
        async with Client(params) as first:
            response = await first.call_tool("lab_capabilities", {})
            assert not response.is_error
            instance = response.structured_content["instance_id"]
            second = await asyncio.create_subprocess_exec(*command, stdin=asyncio.subprocess.DEVNULL,
                stdout=asyncio.subprocess.PIPE, stderr=asyncio.subprocess.PIPE,
                env={**os.environ, "CODEX_HOME": str(tmp_path / "codex")})
            try:
                stdout, stderr = await asyncio.wait_for(second.communicate(), 10)
            finally:
                if second.returncode is None:
                    second.terminate()
                    await second.wait()
            assert second.returncode != 0 and stdout == b""
            assert b"lock" in stderr.lower() or b"owner" in stderr.lower()
            assert not (await first.call_tool("lab_capabilities", {})).is_error
        async with Client(params) as reconnected:
            response = await reconnected.call_tool("lab_capabilities", {})
            assert response.structured_content["instance_id"] == instance
        assert not (root / "agent/engine.json").exists()
    asyncio.run(run())
