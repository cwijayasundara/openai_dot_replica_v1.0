"""One CSV task shared by scripted and live coder acceptance."""

from datetime import UTC, datetime
from pathlib import Path
from typing import Any
from uuid import uuid4

from langchain_core.messages import HumanMessage
from langgraph.types import Command

from dot.config import Settings
from dot.persistence.db import Dot

CSV = b"item,quantity,unit_price\nbook,2,12.50\npen,3,1.20\nfolder,1,4.40\n"
EXPECTED = {"rows": 3, "total_quantity": 6, "total_value": "33.00"}
SCRIPT_PATH = "/work/compute.py"
OUTPUT_PATH = "/work/result.json"
REQUEST = (
    "Delegate to coder: write and run a Python script that reads /in/orders.csv, "
    "counts rows, sums quantity, and sums quantity * unit_price using decimal arithmetic. "
    "Save /work/compute.py and write /work/result.json with keys rows (integer), "
    "total_quantity (integer), total_value (string with two decimal places). "
    "Execute the script and verify the JSON output. Report both output paths."
)
SCRIPT = """import csv
import json
from decimal import Decimal

with open("/in/orders.csv", newline="") as source:
    rows = list(csv.DictReader(source))
result = {
    "rows": len(rows),
    "total_quantity": sum(int(row["quantity"]) for row in rows),
    "total_value": format(sum(
        (int(row["quantity"]) * Decimal(row["unit_price"]) for row in rows), Decimal(0)
    ), ".2f"),
}
with open("/work/result.json", "w") as target:
    json.dump(result, target)
print(json.dumps(result))
"""


def stage_task(settings: Settings) -> Dot:
    dot_id = f"x3-{uuid4().hex}"
    dot = Dot(dot_id, "local", "research-analyst", "0", dot_id, "active", datetime.now(UTC))
    inputs = Path(settings.object_root) / dot_id / "sandbox" / "in"
    inputs.mkdir(parents=True)
    (inputs / "orders.csv").write_bytes(CSV)
    return dot


def invoke_with_sandbox_approvals(agent: Any, dot: Dot) -> dict[str, Any]:
    """A simulated human for coder fixtures only, never production auto-approval."""
    config = {"configurable": {"thread_id": dot.thread_id}, "recursion_limit": 60}
    result = agent.invoke({"messages": [HumanMessage(REQUEST)]}, config)
    for _ in range(20):
        pending = result.get("__interrupt__", ())
        if not pending:
            return result
        resume = {}
        for interruption in pending:
            actions = interruption.value["action_requests"]
            assert all(action["name"] in {"execute", "write_file", "edit_file", "delete"} for action in actions)
            resume[interruption.id] = {"decisions": [{"type": "approve"} for _ in actions]}
        result = agent.invoke(Command(resume=resume), config)
    raise AssertionError("coder exceeded the fixture's approval budget")
