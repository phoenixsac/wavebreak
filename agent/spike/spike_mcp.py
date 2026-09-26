"""Throwaway MCP server for TrueForge spike: one read tool, one unannotated write tool, one blocking tool."""
import asyncio
import time
from pathlib import Path

from mcp.server.fastmcp import FastMCP
from mcp.types import ToolAnnotations

LOG = Path(__file__).with_name("spike_mcp_calls.log")
mcp = FastMCP("spike", host="127.0.0.1", port=8791)


def log(msg: str) -> None:
    with LOG.open("a") as f:
        f.write(f"{time.strftime('%H:%M:%S')} {msg}\n")


@mcp.tool(annotations=ToolAnnotations(readOnlyHint=True))
def get_status() -> str:
    """Read the current status of the spike system."""
    log("get_status")
    return "status: ok, 4 devices, version v1.1"


@mcp.tool()  # deliberately no annotations
def write_marker(text: str) -> str:
    """Write a marker line (irreversible in this spike). Side effect: appends to spike_markers.txt."""
    log(f"write_marker EXECUTED text={text!r}")
    with Path(__file__).with_name("spike_markers.txt").open("a") as f:
        f.write(text + "\n")
    return f"marker written: {text}"


@mcp.tool(annotations=ToolAnnotations(readOnlyHint=True))
async def wait_seconds(seconds: int) -> str:
    """Block for the given number of seconds, then return. Used to measure tool-call timeouts."""
    log(f"wait_seconds START {seconds}")
    await asyncio.sleep(seconds)
    log(f"wait_seconds END {seconds}")
    return f"waited {seconds}s"


if __name__ == "__main__":
    mcp.run(transport="streamable-http")
