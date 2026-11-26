"""Operation-led route decisions using existing adapters, measurements and saved project inputs."""

import asyncio
from copy import deepcopy

import pytest

from app.agent_tools import ToolContext, execute_tool
from app.design_routes import needs_review, open_source
from app.domain import apply_update, new_project
from app.project_assembly import assembly_binding
from app.project_storage import make_snapshot, restore_snapshot

OWNED = "I own a USB camera on a fixed stand"
THICKNESS = "The aluminum coupon is 4 mm thick"
QUESTION = "How thick is the coupon in mm so the tray supports its edge?"


def run(ctx, name, **arguments):
    return asyncio.run(execute_tool(name, arguments, ctx))


def option(key, strategy, adapter, capabilities, **details):
    return dict(
        key=key,
        label=key.replace("-", " ").title(),
        strategy=strategy,
        adapter=adapter,
        coverage=[{"operation": k, "capability": v} for k, v in capabilities],
        **details,
    )


def coupon_options(height=None):
    fixture = [("locate", "locating_base"), ("photograph", "manual")]
    answer = (
        [dict(key="object_height_mm", value=str(height), unit="mm", quote=THICKNESS)]
        if height
        else []
    )
    return dict(
        operations=[
            dict(
                key="locate",
                action="Register the coupon against two edges.",
                material="Aluminum",
                performance="Return to the same datum.",
                interface="Two accessible edges.",
            ),
            dict(
                key="photograph",
                action="Capture its surface for visual comparison.",
                material="Coupon",
                performance="Fixed framing.",
                interface="USB camera on the existing stand.",
            ),
        ],
        options=[
            option(
                "existing-stand",
                "reuse",
                "configure_standalone_fixture",
                fixture,
                inventory_quote=OWNED,
                benefit="Keeps the installed camera.",
                tradeoff="The stand has no lateral stops.",
                blocker="The stand cannot locate a coupon.",
            ),
            option(
                "camera-kit",
                "buy",
                "develop_bench_station",
                [("photograph", "inspect")],
                source_ids=["bench:inspect"],
                benefit="Camera has a documented interface.",
                tradeoff="A separate coupon locator is still required.",
            ),
            option(
                "keyed-tray",
                "fabricate",
                "configure_standalone_fixture",
                fixture,
                inventory_quote=OWNED,
                benefit="A printed pocket locates both edges.",
                tradeoff="Check clearance on the first print.",
                measurement_question=None if height else QUESTION,
                measurement_key=None if height else "object_height_mm",
                measurements=answer,
            ),
        ],
    )


@pytest.fixture
def context(tmp_path, monkeypatch):
    monkeypatch.setenv("AUTOLAB_LAB_PROFILE_PATH", str(tmp_path / "profile.json"))
    project = apply_update(
        new_project(),
        {
            "workflow_kind": "general",
            "brief_objective": "Register a coupon.",
            "brief_equipment": OWNED,
            "workspace_width_mm": 800,
        },
    )
    return ToolContext(project, OWNED)


@pytest.mark.parametrize(
    ("quote", "fields", "unit", "expected"),
    [
        ("The part measures 5 × 3 × 2 cm", ("length", "width", "height"), "cm", (50, 30, 20)),
        ("Diameter 16 mm and height 25 mm", ("diameter", "height"), "mm", (16, 25)),
    ],
)
def test_choose_and_build_a_measured_draft(context, quote, fields, unit, expected):
    ctx = context
    options = coupon_options()
    options["options"][2]["measurement_key"] = "object_dimensions"
    assert run(ctx, "compare_design_routes", **options).ok
    before = deepcopy(ctx.project)
    values = [v / 10 if unit == "cm" else v for v in expected]
    measurements = [
        dict(key=f"object_{field}_mm", value=f"{value:g}", unit=unit, quote=quote)
        for field, value in zip(fields, values, strict=True)
    ]
    selection = "Choose Keyed Tray"
    args = dict(key="keyed-tray", user_quote=selection, make_passive_fixture=True)
    ctx.user_message = f"{quote}. {selection}."
    forged = [{**measurements[0], "value": "77"}, *measurements[1:]]
    assert not run(ctx, "choose_design_route", measurements=forged, **args).ok
    assert ctx.project == before
    assert not run(ctx, "choose_design_route", measurements=measurements[-1:], **args).ok
    assert ctx.project == before
    result = run(ctx, "choose_design_route", measurements=measurements, **args)
    assert result.ok and result.mutated
    revision = ctx.project["revision"]
    assert result.data["first_cad"]["revision"] == revision == before["revision"] + 1
    fixture = ctx.project["standalone_fixture"]
    assert tuple(fixture["spec"][f"object_{f}_mm"] for f in fields) == expected
    assert fixture["spec"]["fastener_spec"] == "none"
    assert all(p["printer_fit"]["fits"] is None for p in fixture["parts"])
    plan = ctx.project["design_plan"]
    assert plan["decision"] == {
        "key": "keyed-tray", "user_quote": selection, "applied_revision": revision,
    }
    assert not plan["blockers"]["keyed-tray"] and plan["blockers"]["camera-kit"]
    assert not run(ctx, "choose_design_route", measurements=measurements, **args).mutated
    restored, _ = restore_snapshot(make_snapshot(ctx.project, []))
    assert restored["design_plan"] == plan
    assert restored["standalone_fixture"]["spec"] == fixture["spec"]


def test_open_holder_uses_the_footprint_without_assuming_a_loaded_height(context):
    ctx = context
    options = coupon_options()
    options["options"][2]["measurement_key"] = "object_dimensions"
    assert run(ctx, "compare_design_routes", **options).ok
    ctx.user_message = "The diameter is 36 mm. I choose Keyed Tray for the first draft."
    diameter = dict(key="object_diameter_mm", value="36", unit="mm", quote="The diameter is 36 mm")
    result = run(ctx, "choose_design_route", key="keyed-tray", user_quote="I choose 'Keyed Tray'",
                 measurements=[diameter], make_passive_fixture=True)
    assert result.ok
    project = ctx.project
    fixture = project["standalone_fixture"]
    assert fixture["spec"]["object_height_mm"] is None
    assert "fixture_object_height_mm" not in project["dimension_provenance"]
    assert fixture["derived"]["pocket_diameter_mm"] == pytest.approx(36.9)
    assert "reference-object" not in {component["id"] for component in fixture["components"]}
    assert not run(ctx, "configure_standalone_fixture", include_camera_mount=True).ok
    assert ctx.project == project
    restored, _ = restore_snapshot(make_snapshot(project, []))
    assert restored["standalone_fixture"]["spec"] == fixture["spec"]


def test_need_and_measurements_precede_fabrication(context):
    ctx = context
    args = dict(object_label="Disk", object_diameter_mm=24, fastener_spec="none")
    ctx.user_message = "I need a support for a disk with a 24 mm diameter."
    before = deepcopy(ctx.project)
    assert not run(ctx, "configure_standalone_fixture", **args).ok
    assert ctx.project == before
    ctx.user_message = "Please print the disk support."
    using_profile = {"printer": {"name": "Bench printer", "build_volume_mm": [180, 180, 180]}}
    ctx.lab_profile = using_profile
    assert run(ctx, "configure_standalone_fixture", **args).ok
    restored, _ = restore_snapshot(make_snapshot(ctx.project, []), lab_profile=using_profile)
    assert restored["standalone_fixture"]["parts"] == ctx.project["standalone_fixture"]["parts"]


def test_changed_interface_can_supersede_an_obsolete_measurement(context):
    ctx = context
    assert run(ctx, "compare_design_routes", **coupon_options()).ok
    options = coupon_options()
    options["options"][2].update(measurement_key=None, measurement_question=None)
    assert not run(ctx, "compare_design_routes", **options).ok
    ctx.user_message = "Use the camera only. We no longer need edge registration."
    flags = dict(process="Use a fixed camera without edge registration.")
    assert run(ctx, "update_project_brief", **flags).ok
    assert needs_review(ctx.project)
    assert run(ctx, "compare_design_routes", **options).ok


def test_measurement_revises_the_route_then_applies_matching_geometry(context):
    ctx = context
    result = run(ctx, "compare_design_routes", **coupon_options())
    assert result.ok and result.question.endswith(QUESTION)
    assert "Keyed Tray" in result.question
    assert "Missing operation" in str(result.data["blockers"]["camera-kit"])
    revision = ctx.project["revision"]
    assert not run(ctx, "compare_design_routes", **coupon_options()).mutated
    ctx.user_message = "Choose Keyed Tray"
    assert not run(ctx, "choose_design_route", key="keyed-tray", user_quote=ctx.user_message).ok
    assert not run(ctx, "compare_design_routes", **coupon_options(4)).ok
    unrelated = coupon_options(4)
    unrelated["options"][2]["measurements"][0].update(key="object_width_mm", quote="Width is 4 mm")
    ctx.user_message = "Width is 4 mm"
    assert not run(ctx, "compare_design_routes", **unrelated).ok
    assert ctx.project["revision"] == revision
    ctx.user_message = THICKNESS + ". Choose Keyed Tray."
    assert run(ctx, "compare_design_routes", **coupon_options(4)).ok
    assert run(ctx, "choose_design_route", key="keyed-tray", user_quote="Choose Keyed Tray").ok
    spec = dict(
        fixture_kind="locating_base",
        object_shape="rectangular",
        object_length_mm=60,
        object_width_mm=40,
        object_height_mm=6,
        object_label="Coupon",
    )
    before = deepcopy(ctx.project)
    assert not run(ctx, "configure_standalone_fixture", **spec).ok
    assert ctx.project == before
    assert not run(ctx, "develop_bench_station", operations=["weigh"]).ok
    spec["object_height_mm"] = 4
    applied = run(ctx, "configure_standalone_fixture", **spec)
    assert (
        applied.ok
        and applied.data["applied_design_route"]["applied_revision"] == ctx.project["revision"]
    )
    assert ctx.project["standalone_fixture"]["spec"]["object_height_mm"] == 4
    restored, _ = restore_snapshot(make_snapshot(ctx.project, []))
    assert restored["design_plan"] == ctx.project["design_plan"]
    assert not needs_review(restored)
    assert assembly_binding(restored) == assembly_binding(ctx.project)


@pytest.mark.parametrize(
    ("action", "quote"),
    [
        ("Do not choose Keyed Tray", "choose Keyed Tray"),
        ("Could we choose Keyed Tray?", "choose Keyed Tray"),
        ("Choose Camera Kit", "Choose Camera Kit"),
    ],
)
def test_decision_is_bound_to_one_current_affirmative_option(context, action, quote):
    ctx = context
    ctx.user_message = OWNED + ". " + THICKNESS + "."
    assert run(ctx, "compare_design_routes", **coupon_options(4)).ok
    ctx.user_message = action
    assert not run(ctx, "choose_design_route", key="keyed-tray", user_quote=quote).ok
    assert not ctx.project["design_plan"]["decision"]


def test_bare_ordinal_selects_the_displayed_option(context):
    ctx = context
    ctx.user_message = OWNED + ". " + THICKNESS
    assert run(ctx, "compare_design_routes", **coupon_options(4)).ok
    ctx.user_message = "La terza, grazie."
    assert run(ctx, "choose_design_route", key="keyed-tray", user_quote=ctx.user_message).ok
    assert ctx.project["design_plan"]["decision"]["applied_revision"] is None


def test_sources_inventory_and_capabilities_cannot_be_invented(context):
    ctx = context
    for field, bad in (
        ("source_ids", ["open:github:invented/rig@" + "e" * 40]),
        ("inventory_quote", "I own a second camera"),
        ("coverage", [{"operation": "locate", "capability": "autonomous_capture"}]),
    ):
        proposal = coupon_options()
        proposal["options"][0][field] = bad
        assert not run(ctx, "compare_design_routes", **proposal).ok
        assert "design_plan" not in ctx.project


def bench_options():
    operations = [
        dict(key="identify", action="Read the reference block label.", interface="USB HID"),
        dict(
            key="mass",
            action="Record its indicated mass.",
            material="Brass",
            performance="Manual reading to 1 g.",
        ),
    ]
    caps = [("identify", "read_barcode"), ("mass", "weigh")]
    return dict(
        operations=operations,
        options=[
            option(
                "inventory-station",
                "reuse",
                "develop_bench_station",
                caps,
                inventory_quote="We own a USB scanner and a bench balance",
                benefit="Reuse both instruments.",
                tradeoff="The operator records each value.",
            ),
            option(
                "vendor-station",
                "buy",
                "develop_bench_station",
                caps,
                source_ids=["bench:weigh", "bench:read_barcode"],
                benefit="Identifiable manufacturers.",
                tradeoff="Readability and barcode options require verification.",
            ),
            option(
                "compact-cradle",
                "fabricate",
                "configure_standalone_fixture",
                [("identify", "locating_base"), ("mass", "manual")],
                benefit="A printed cradle positions the block for the scanner.",
                tradeoff="Mass is still read on the separate balance.",
            ),
        ],
    )


def test_new_limit_can_replace_a_station_in_the_same_project(context):
    ctx = context
    ctx.user_message = "We own a USB scanner and a bench balance"
    assert run(ctx, "compare_design_routes", **bench_options()).ok
    ctx.user_message = "Use Inventory Station"
    assert run(ctx, "choose_design_route", key="inventory-station", user_quote=ctx.user_message).ok
    assert run(ctx, "develop_bench_station", operations=["read_barcode", "weigh"]).ok
    previous = deepcopy(ctx.project)
    ctx.user_message = "Only 280 mm of width is now available."
    changed = run(ctx, "update_project_brief", workspace_width_mm=280)
    assert changed.ok and needs_review(ctx.project)
    assert ctx.project["bench_station"] == previous["bench_station"]
    assert ctx.project["summary"]["workspace_width_mm"] == 800
    revision = ctx.project["revision"]
    assert run(ctx, "update_project_brief", workspace_width_mm=280).ok
    assert ctx.project["revision"] == revision
    pending, _ = restore_snapshot(make_snapshot(ctx.project, []))
    assert (
        needs_review(pending)
        and pending["design_plan"]["pending_workspace"]["workspace_width_mm"] == 280
    )
    compared = run(ctx, "compare_design_routes", **bench_options())
    assert compared.ok and "width" in str(compared.data["blockers"]["inventory-station"]).lower()
    assert not compared.data["blockers"]["compact-cradle"]
    ctx.user_message = "Build Compact Cradle"
    assert run(ctx, "choose_design_route", key="compact-cradle", user_quote=ctx.user_message).ok
    # A partial or failed construction must keep the preceding physical station.
    assert run(
        ctx, "configure_standalone_fixture", fixture_kind="locating_base"
    ).requires_human_input
    assert "bench_station" in ctx.project and ctx.project["summary"]["workspace_width_mm"] == 800
    result = run(
        ctx,
        "configure_standalone_fixture",
        fixture_kind="locating_base",
        object_label="Brass block",
        object_length_mm=60,
        object_width_mm=40,
        object_height_mm=18,
    )
    assert result.ok and result.mutated
    assert (
        "bench_station" not in ctx.project and ctx.project["summary"]["workspace_width_mm"] == 280
    )
    assert ctx.project["id"] == previous["id"] and not needs_review(ctx.project)
    restored, _ = restore_snapshot(make_snapshot(ctx.project, []))
    assert restored["design_plan"] == ctx.project["design_plan"]


@pytest.mark.parametrize("supplier", [False, True])
def test_purchase_decision_updates_the_compared_bom_line(context, supplier):
    from test_custom_project_routes import project_fixture, proposal

    from app.bom_engine import build_complete_bom
    from app.concept_design import compile_concept
    from app.design_routes import supplier_source

    ctx = context
    ctx.project = project_fixture()
    raw = proposal()
    raw["supplies"][0]["unit"] = "item"
    ctx.project["concept_design"] = compile_concept(raw, 600)
    line = next(
        r
        for r in build_complete_bom(ctx.project)["lines"]
        if r["line_id"] == "concept:controller_board"
    )
    assert line["requirement"]
    ctx.user_message = "I own a controller without its ordering number."
    key, source_id, identity = "pico-h", "catalog:pico-h", {"catalog_id": "pico-h"}
    if supplier:
        found = supplier_source(
            {
                "supplier": "Test vendor",
                "sku": "board-42",
                "pack_size": 1,
                "source_url": "https://example.org/board-42",
                "mpn": "CTRL-42",
            }
        )
        ctx.design_sources[found.key] = found.model_dump()
        key, source_id, identity = (
            "board-42",
            found.key,
            {"issuer": "Test vendor", "part_number": "board-42"},
        )
    options = [
        option(
            "existing-control",
            "reuse",
            "choose_requirement_part",
            [("record", "identify_part")],
            inventory_quote=ctx.user_message,
            benefit="Uses existing hardware.",
            tradeoff="The part identity is missing.",
        ),
        option(
            key,
            "buy",
            "choose_requirement_part",
            [("record", "identify_part")],
            requirement_line_id=line["line_id"],
            source_ids=[source_id],
            benefit="An identified controller.",
            tradeoff="Check the interface on installation.",
        ),
        option(
            "carrier",
            "fabricate",
            "develop_custom_equipment",
            [("record", "custom_geometry")],
            benefit="A custom mounting interface.",
            tradeoff="A controller is still needed.",
            blocker="A printed mount alone cannot record data.",
        ),
    ]
    result = run(
        ctx,
        "compare_design_routes",
        operations=[dict(key="record", action="Record input signals.")],
        options=options,
    )
    assert result.ok and result.data["blockers"][key] == [], result
    reopened, _ = restore_snapshot(make_snapshot(ctx.project, []))
    ctx = ToolContext(reopened, "Choose " + key)
    assert run(ctx, "choose_design_route", key=key, user_quote=ctx.user_message).ok
    args = dict(action="select", line_id=line["line_id"], user_quote=ctx.user_message, **identity)
    assert not run(ctx, "choose_requirement_part", **{**args, "line_id": "unrelated"}).ok
    assert run(ctx, "choose_requirement_part", **args).ok
    accepted = next(
        r for r in build_complete_bom(ctx.project)["lines"] if r["line_id"] == line["line_id"]
    )
    chosen = accepted["part_choice"]["accepted"]
    if supplier:
        assert (chosen["issuer"], chosen["part_number"]) == ("Test vendor", "board-42")
        assert chosen["basis"] == "supplier_search"
        assert accepted["price_source"] == "quote_required"
        assert accepted["line_total"] is None
    else:
        assert chosen["catalog_id"] == "pico-h"
    assert ctx.project["design_plan"]["decision"]["applied_revision"] == ctx.project["revision"]


def test_discovered_route_can_only_ingest_its_observed_pin_and_file(context):
    ctx = context
    source = open_source(
        dict(
            source_host="github",
            repo_full_name="example/tool",
            commit_sha="c" * 40,
            repo_url="https://github.com/example/tool",
            license_allowed=True,
            cad_files=["cad/holder.step"],
            spdx_id="MIT",
        )
    )
    ctx.design_sources[source.key] = source.model_dump()
    proposal = coupon_options()
    proposal["options"][0] = option(
        "open-holder",
        "reuse",
        "ingest_open_hardware_project",
        [("locate", "pinned_cad"), ("photograph", "manual")],
        source_ids=[source.key],
        benefit="Editable source geometry.",
        tradeoff="Inspect the actual mating surface.",
    )
    assert run(ctx, "compare_design_routes", **proposal).ok
    restored, _ = restore_snapshot(make_snapshot(ctx.project, []))
    ctx = ToolContext(restored, "Choose Open Holder")
    assert run(ctx, "choose_design_route", key="open-holder", user_quote=ctx.user_message).ok
    from app.agent_tools import IngestOpenHardwareProjectArgs
    from app.design_routes import pending_adoption

    args = IngestOpenHardwareProjectArgs(
        source_host="github",
        repo_full_name="example/tool",
        commit_sha="c" * 40,
        cad_file_path="cad/holder.step",
    )
    assert (
        pending_adoption("ingest_open_hardware_project", args, ctx.project)["key"] == "open-holder"
    )
    assert not run(
        ctx, "ingest_open_hardware_project", **{**args.model_dump(), "commit_sha": "f" * 40}
    ).ok
    assert "upstream_project" not in ctx.project
