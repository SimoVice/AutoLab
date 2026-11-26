"""Generate an offline Python preview from the selected machine's compiled operations."""

import inspect
from pprint import pformat

from app.design_workflow import workflow_state
from app.operation_plan import project_operation_plan
from app.protocol_preview import describe_operation, simulate_operations


def build_program(project: dict) -> str:
    if not workflow_state(project)["machine_approved"]:
        raise ValueError("Approve the machine before generating its program.")
    sequence = project_operation_plan(project)
    if sequence["platform"] not in ("opentrons-ot2", "jubilee") or not sequence["operations"]:
        raise ValueError("Use the operating sequence for this design.")
    targets = {
        row.get("well_name", ""): row["target"]
        for row in (project.get("plan") or {}).get("steps", [])
    }
    return f'''"""Preview the saved operation sequence offline. Run python program.py."""

OPERATION_PLAN = {pformat(sequence, width=100, sort_dicts=False)}
TARGETS = {targets!r}

{inspect.getsource(describe_operation)}

{inspect.getsource(simulate_operations)}

def simulate():
    result = simulate_operations(OPERATION_PLAN)
    print(f"Logical preview | {{OPERATION_PLAN['platform']}} | "
          f"revision {{OPERATION_PLAN['revision']}} | {{result['plan_sha256']}}")
    for op in OPERATION_PLAN["operations"]:
        print(f"{{op['id']}} · {{op['mode']}} · {{describe_operation(op)}}")
    for well, volume in result["destination_wells"].items():
        print(f"{{well}}: {{volume:g}} uL total at planned {{TARGETS.get(well, well)}}")
    used = result["consumed_ul"]
    print(f"Total stock {{used['stock']:g}} uL, diluent {{used['diluent']:g}} uL, "
          f"{{result['tips_used']}} tips")
    return result["destination_wells"]


if __name__ == "__main__":
    simulate()
'''
