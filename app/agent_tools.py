"""Typed OpenAI function-tool registry and deterministic domain execution engine."""

from __future__ import annotations

import logging
import re
from copy import deepcopy
from dataclasses import dataclass, field
from typing import Annotated, Any, Literal

from pydantic import BaseModel, ConfigDict, Field, ValidationError

from app import design_routes
from app.bench_station import MODULES as STATION_MODULES
from app.bench_station import StationProposal, compile_station
from app.budget_allowances import SetLineBudgetArgs
from app.cad_instances import CadPlacement, Digest, Name, selected_source
from app.design_sources import reference_library, resolve_reference
from app.domain import apply_update
from app.engineering import EngineeringUpdate, apply_engineering_update, state_for
from app.lab_profile import (
    LabProfile,
    apply_profile_to_project,
    lab_profile_prompt_context,
    load_lab_profile,
    record_dimension,
    save_lab_profile,
)
from app.measurement_program import MeasurementSteps
from app.microplates import PROFILE_ID as CORNING_96_ID
from app.microplates import LabwareSelection, reapply_plate, select_plate
from app.part_choices import ChoosePartArgs, update_choice
from app.platform_profiles import platform_profiles
from app.services.runtime import api_hook
from app.spatial_fit import (
    evaluate_printable_fit,
    evaluate_spatial_fit,
    format_spatial_violations,
    refresh_spatial_fit,
)
from app.tip_racks import (
    PREVIOUS_ID as TIP_PREVIOUS_ID,
)
from app.tip_racks import (
    PRINTED_ID as TIP_PRINTED_ID,
)
from app.tip_racks import (
    REQUIRED_FIELDS as TIP_REQUIRED_FIELDS,
)
from app.tip_racks import (
    TipRackUpdate,
    reapply_tip_rack,
    select_tip_rack,
    show_tip_options,
    tip_rack_options,
    update_tip_request,
)
from app.workflows import (
    describe_workflow,
    has_workstation_design_context,
    workstation_context_question,
)

LOGGER = logging.getLogger("autolab.tools")

_UNSET = object()


def _strict_function_schema(model: type[BaseModel]) -> dict[str, Any]:
    """Compile a Pydantic parameter model into an OpenAI strict function JSON Schema."""
    schema = model.model_json_schema()

    def enforce(node: Any) -> None:
        if isinstance(node, dict):
            node.pop("default", None)
            if node.get("type") == "array":
                node.setdefault("minItems", 0)
            if "properties" in node:
                node["required"] = sorted(node["properties"])
                node["additionalProperties"] = False
            for child in node.values():
                enforce(child)
        elif isinstance(node, list):
            for child in node:
                enforce(child)

    enforce(schema)
    return schema


@dataclass
class ToolContext:
    """Mutable per-turn execution context wrapping a staged project copy."""

    project: dict
    user_message: str
    history: list[dict[str, str]] = field(default_factory=list)
    lab_profile: dict | None = None
    procurement_report: dict | None = None
    cost_estimate: dict | None = None
    supplier_hits: list[dict] = field(default_factory=list)
    design_sources: dict = field(default_factory=dict)
    recommendations: list[dict] = field(default_factory=list)
    action_runs: list[dict] = field(default_factory=list)
    mutated: bool = False
    loaded_tools: set[str] = field(default_factory=set)


@dataclass
class ToolResult:
    """Structured outcome returned to both the autonomous loop and telemetry."""

    tool: str
    ok: bool
    stage: str
    summary: str
    recoverable: bool = False
    mutated: bool = False
    requires_human_input: bool = False
    question: str | None = None
    diagnostics: list[str] = field(default_factory=list)
    data: dict[str, Any] = field(default_factory=dict)
    action_run: dict | None = None

    def to_tool_message(self) -> dict[str, Any]:
        payload: dict[str, Any] = {
            "tool": self.tool,
            "ok": self.ok,
            "recoverable": self.recoverable,
            "stage": self.stage,
            "summary": self.summary,
            "mutated": self.mutated,
        }
        if self.diagnostics:
            payload["diagnostics"] = self.diagnostics
        if self.requires_human_input:
            payload["requires_human_input"] = True
            if self.question:
                payload["question"] = self.question
        if self.data:
            payload["data"] = self.data
        return payload


class _StrictArgs(BaseModel):
    model_config = ConfigDict(extra="forbid")


class InspectProjectStateArgs(_StrictArgs):
    include_components: bool = False


class LoadToolsArgs(_StrictArgs):
    names: list[str] = Field(min_length=1)


class CheckSpatialFitArgs(_StrictArgs):
    platform_id: Literal["jubilee", "opentrons-ot2"] | None = None
    candidate_width_mm: float | None = Field(default=None, gt=0, le=5000)
    candidate_depth_mm: float | None = Field(default=None, gt=0, le=5000)
    candidate_height_mm: float | None = Field(default=None, gt=0, le=5000)
    include_printable_parts: bool = True


class LookupReviewedReferencesArgs(_StrictArgs):
    query: Annotated[str | None, Field(max_length=200)] = None
    reference_id: Annotated[str | None, Field(max_length=100)] = None
    include_platforms: bool = True
    library_kind: Literal["references", "labware", "hardware", "all"] | None = None


class UpdateProjectBriefArgs(_StrictArgs):
    workflow_kind: Literal["general", "liquid_preparation"] | None = None
    objective: Annotated[str | None, Field(max_length=1000)] = None
    process: Annotated[str | None, Field(max_length=1000)] = None
    materials: Annotated[str | None, Field(max_length=1000)] = None
    equipment: Annotated[str | None, Field(max_length=1000)] = None
    constraints: Annotated[str | None, Field(max_length=1000)] = None
    workspace_width_mm: float | None = Field(default=None, gt=0, le=5000)
    workspace_depth_mm: float | None = Field(default=None, gt=0, le=5000)
    workspace_height_mm: float | None = Field(default=None, gt=0, le=5000)
    cooling_enabled: bool | None = None
    cooling_target_c: float | None = Field(default=None, ge=2, le=20)


class UpdateLiquidRecipeArgs(_StrictArgs):
    stock_concentration: Annotated[str | None, Field(max_length=500)] = None
    target_concentrations: Annotated[str | None, Field(max_length=500)] = None
    final_volume: Annotated[str | None, Field(max_length=500)] = None


class SelectPlatformArgs(_StrictArgs):
    platform_id: Literal["jubilee", "opentrons-ot2"]
    rationale: Annotated[str | None, Field(max_length=300)] = None


class DevelopWorkstationLayoutArgs(_StrictArgs):
    instruction: Annotated[str, Field(min_length=1, max_length=1000)]


class ConfigureLabwareArgs(_StrictArgs):
    action: Literal[
        "configure_tube_rack",
        "remove_tube_rack",
        "select_microplate",
        "revert_to_tube_rack",
        "configure_tip_rack",
        "show_tip_rack_options",
    ]
    body_diameter_max_mm: float | None = Field(default=None, gt=0, le=300)
    rim_diameter_min_mm: float | None = Field(default=None, gt=0, le=300)
    rim_diameter_max_mm: float | None = Field(default=None, gt=0, le=300)
    cap_diameter_max_mm: float | None = Field(default=None, gt=0, le=300)
    below_rim_height_mm: float | None = Field(default=None, gt=0, le=300)
    count: int | None = Field(default=None, ge=1, le=96)
    columns: int | None = Field(default=None, ge=1, le=12)
    pitch_mm: float | None = Field(default=None, gt=0, le=300)
    radial_clearance_mm: float | None = Field(default=None, gt=0, le=5)
    plate_profile_id: Annotated[str, Field(min_length=1, max_length=120)] | None = None
    tip_profile_id: Literal[
        "tipone_96_tiprack_200ul",
        "opentrons_96_tiprack_300ul",
        "custom_printed_tip_rack",
        "previous_tip_rack",
    ] | None = None
    printer_available: Literal["yes", "no", "unknown"] | None = None
    confirm_tip_rack_shift: bool = False


class ManageExtensionArgs(_StrictArgs):
    extension: Literal["inspection", "cold_transfer"]
    operation: Literal["add", "remove"]
    instruction: Annotated[str | None, Field(max_length=1000)] = None


class DevelopBenchStationArgs(_StrictArgs):
    operations: list[Literal["weigh", "print_label", "read_barcode", "inspect"]] = Field(
        min_length=1, max_length=4
    )


class DevelopCustomEquipmentArgs(_StrictArgs):
    instruction: Annotated[str, Field(min_length=1, max_length=1200)]


class ApproveMachineArgs(_StrictArgs):
    explicit_user_confirmation: bool


class UpdateLabProfileArgs(_StrictArgs):
    lab_name: Annotated[str | None, Field(max_length=120)] = None
    printer_name: Annotated[str | None, Field(max_length=120)] = None
    printer_build_volume_mm: list[float] | None = Field(default=None, min_length=3, max_length=3)
    bench_width_mm: float | None = Field(default=None, gt=0, le=5000)
    bench_depth_mm: float | None = Field(default=None, gt=0, le=5000)
    bench_height_mm: float | None = Field(default=None, gt=0, le=5000)
    measuring_tools: list[Annotated[str, Field(max_length=80)]] | None = Field(
        default=None, max_length=16
    )
    owned_equipment: list[Annotated[str, Field(max_length=100)]] | None = Field(
        default=None, max_length=32
    )
    apply_to_current_project: bool = True


class ConfigureStandaloneFixtureArgs(_StrictArgs):
    title: Annotated[str | None, Field(max_length=140)] = None
    fixture_kind: Literal["locating_base", "sample_holder", "imaging_rig"] | None = None
    object_reference_id: Literal[
        "slas_96_well_plate",
        "petri_dish_90mm",
        "microscope_slide_75x25",
        "cuvette_12_5mm",
        "custom",
    ] = "custom"
    object_label: Annotated[str | None, Field(max_length=120)] = None
    object_shape: Literal["rectangular", "circular"] | None = None
    object_length_mm: float | None = Field(default=None, ge=5.0, le=400.0)
    object_width_mm: float | None = Field(default=None, ge=5.0, le=400.0)
    object_diameter_mm: float | None = Field(default=None, ge=5.0, le=300.0)
    object_height_mm: float | None = Field(default=None, ge=0.5, le=250.0)
    clearance_per_side_mm: float | None = Field(default=None, ge=0.05, le=3.0)
    fdm_undersize_mm: float | None = Field(default=None, ge=0.0, le=1.5)
    wall_thickness_mm: float | None = Field(default=None, ge=2.0, le=20.0)
    floor_thickness_mm: float | None = Field(default=None, ge=2.0, le=20.0)
    lip_height_mm: float | None = Field(default=None, ge=1.5, le=40.0)
    fastener_spec: Literal[
        "none", "m3_clearance", "m3_heatset_insert", "m4_clearance"
    ] | None = None
    include_scale_reference: bool | None = None
    include_camera_mount: bool | None = None
    camera_mount_type: Literal[
        "pi_camera_v2_v3", "usb_webcam_quarter20", "custom_m3"
    ] | None = None
    camera_working_distance_mm: float | None = Field(default=None, ge=40.0, le=400.0)
    material: Literal["PLA", "PETG", "ABS", "ASA"] | None = None
    export_parts: list[Annotated[str, Field(max_length=40)]] | None = Field(
        default=None, max_length=8
    )


class ToolMeasuredFeatureArg(_StrictArgs):
    feature: Annotated[str, Field(min_length=1, max_length=80)]
    axis: Literal["length", "width", "diameter", "height", "bore", "other"] = "length"
    nominal_mm: float = Field(gt=0.0, le=1000.0)
    measured_mm: float = Field(gt=0.0, le=1000.0)


class RecordBuildLogArgs(_StrictArgs):
    parts_printed: list[Annotated[str, Field(max_length=40)]] = Field(
        min_length=1, max_length=12
    )
    fit_result: Literal["pass", "too_tight", "too_loose", "warped", "failed_print"]
    printer_name: Annotated[str | None, Field(max_length=100)] = None
    material: Annotated[str | None, Field(max_length=40)] = None
    layer_height_mm: float | None = Field(default=None, ge=0.05, le=1.0)
    measured_features: list[ToolMeasuredFeatureArg] = Field(
        default_factory=list, max_length=20
    )
    notes: Annotated[str | None, Field(max_length=1000)] = None


class ApplyBuildFeedbackArgs(_StrictArgs):
    log_id: Annotated[str | None, Field(max_length=40)] = None
    clearance_delta_mm: float | None = Field(default=None, ge=-1.5, le=1.5)
    fdm_undersize_delta_mm: float | None = Field(default=None, ge=-1.0, le=1.0)


class ParamOverrideArg(_StrictArgs):
    name: Annotated[str, Field(min_length=1, max_length=80)]
    value: float | bool | str


class DiscoverOpenHardwareProjectsArgs(_StrictArgs):
    query: Annotated[str, Field(min_length=2, max_length=200)]
    sources: list[Literal["github", "gitlab", "openalex", "europepmc", "nih3d"]] | None = None
    max_results: int | None = Field(default=None, ge=1, le=12)
    require_cad: bool = True
    commercial_use_ok: bool = False


class IngestOpenHardwareProjectArgs(_StrictArgs):
    instance_id: Name | None = None
    placement: CadPlacement | None = None
    expected_sha256: Digest | None = None
    source_host: Literal["github", "gitlab", "nih3d"]
    repo_full_name: Annotated[str, Field(min_length=3, max_length=200)]
    commit_sha: Annotated[str, Field(min_length=2, max_length=64)]
    cad_file_path: Annotated[str, Field(min_length=3, max_length=400)]
    allow_non_commercial: bool = False
    allow_unknown_license: bool = False
    source_units: Literal["auto", "mm", "in"] = "auto"
    source_up_axis: Literal["z", "y"] = "z"


class AdaptOpenHardwareProjectArgs(_StrictArgs):
    instance_id: Name | None = None
    mode: Literal["mating", "parametric"]
    mating_kind: Literal[
        "adapter_plate", "bracket", "riser", "enclosure", "cutout_mount"
    ] | None = None
    placement: Literal["below", "above", "rear", "side_x", "surround"] | None = None
    clearance_mm: float | None = Field(default=None, ge=0.0, le=5.0)
    plate_thickness_mm: float | None = Field(default=None, ge=2.0, le=30.0)
    riser_height_mm: float | None = Field(default=None, ge=2.0, le=150.0)
    wall_thickness_mm: float | None = Field(default=None, ge=1.5, le=25.0)
    flange_margin_mm: float | None = Field(default=None, ge=4.0, le=60.0)
    hole_diameter_mm: float | None = Field(default=None, ge=2.0, le=12.0)
    secondary_hole_pitch_x_mm: float | None = Field(default=None, ge=10.0, le=300.0)
    secondary_hole_pitch_y_mm: float | None = Field(default=None, ge=10.0, le=300.0)
    cutout_width_mm: float | None = Field(default=None, ge=4.0, le=400.0)
    cutout_depth_mm: float | None = Field(default=None, ge=4.0, le=400.0)
    param_overrides: list[ParamOverrideArg] | None = Field(default=None, max_length=24)


class InspectUpstreamProjectArgs(_StrictArgs):
    instance_id: Name | None = None
    section: Literal[
        "all", "geometry", "license", "bom", "assembly", "software", "adaptation"
    ] = "all"


class GeneratePurchasableBomArgs(_StrictArgs):
    currency: Literal["USD", "CHF", "EUR"] = "USD"
    refresh_live_prices: bool = False


class CheckHardwareCompatibilityArgs(_StrictArgs):
    include_passed_checks: bool = False


class SearchSupplierComponentsArgs(_StrictArgs):
    query: Annotated[str, Field(min_length=2, max_length=160)]
    currency: Literal["USD", "CHF", "EUR"] = "USD"
    max_results: int = Field(default=6, strict=True, ge=1, le=12)


class GenerateRobotProtocolArgs(_StrictArgs):
    target_platform: Literal["auto", "opentrons-ot2", "jubilee"] = "auto"
    stock_concentration: Annotated[str | None, Field(min_length=1, max_length=200)] = None
    target_concentrations: Annotated[str | None, Field(min_length=1, max_length=4000)] = None
    final_volume: Annotated[str | None, Field(min_length=1, max_length=120)] = None
    allow_serial: bool = True
    allow_multi_dispense: bool = True


class SimulateRobotProtocolArgs(_StrictArgs):
    target_platform: Literal["auto", "opentrons-ot2", "jubilee"] = "auto"


class BuildHardwareCommissioningPackArgs(_StrictArgs):
    driver_kind: Literal["loopback", "grbl", "klipper"] = "grbl"
    watchdog_timeout_s: float = Field(default=30.0, gt=0.1, le=300.0)


_TOOL_SPECS: list[tuple[str, str, type[BaseModel]]] = [
    ("load_tools", "Make the named tools available for subsequent steps of this turn. "
     "Request all needed tools together. This does not change the project.", LoadToolsArgs),
    ("compare_design_routes",
     "Save the requested operations and compare reuse, buy and fabricate options. Map each "
     "operation to a capability from design_capabilities or manual. Use returned design_source_id "
     "values, bench:<operation>, platform:<id>, or catalog:<id>. Use inventory_quote for owned "
     "equipment, blocker for an unavailable route and measurement_question for a deciding unknown.",
     design_routes.CompareRoutes),
    ("choose_design_route", "Record one explicitly chosen option and any new measurements. "
     "Set make_passive_fixture to build a freestanding locating base or holder in this call. "
     "Other options expose their implementation tool.", design_routes.ChooseRoute),
    (
        "inspect_project_state",
        "Read the current project state (workflow, brief, 3D dimensions, platform, "
        "active modules, dilution plan, labware and lab profile). With include_components, "
        "inspect physical instances, their assembly frames, connections and purchasing links.",
        InspectProjectStateArgs,
    ),
    (
        "check_spatial_fit",
        "Evaluate 3D width/depth/height fit, ventilation/access clearances, and 3D printer "
        "build-volume checks for the current project or a candidate platform/footprint.",
        CheckSpatialFitArgs,
    ),
    (
        "lookup_reviewed_references",
        "Search ONLY AutoLab's internal reviewed library (Jubilee, OT-2, commercial tip-rack "
        "profiles, and verified supplier candidate entries). Pair with "
        "discover_open_hardware_projects rather than using alone when proposing platforms or "
        "alternatives.",
        LookupReviewedReferencesArgs,
    ),
    (
        "update_project_brief",
        "Record or update workflow classification, brief fields, 3D workspace dimensions "
        "(width, depth, height in mm), or cooling requirements on the project.",
        UpdateProjectBriefArgs,
    ),
    (
        "update_liquid_recipe",
        "Set or revise liquid preparation recipe parameters (stock_concentration, "
        "target_concentrations, final_volume) and recompute the dilution plan.",
        UpdateLiquidRecipeArgs,
    ),
    (
        "select_platform",
        "Explicitly select a reviewed liquid-preparation platform (jubilee or opentrons-ot2) "
        "after checking 3D workspace dimensions and clearances.",
        SelectPlatformArgs,
    ),
    (
        "develop_workstation_layout",
        "Generate or revise the 3D workstation deck layout or OT-2 slot assignments for the "
        "currently selected liquid-preparation platform.",
        DevelopWorkstationLayoutArgs,
    ),
    (
        "configure_labware",
        "Configure custom tube racks, switch to or revert from the reviewed Corning 96-well "
        "plate, or configure commercial/printed tip racks on a Jubilee workstation.",
        ConfigureLabwareArgs,
    ),
    (
        "manage_extension",
        "Add or remove the Jubilee camera inspection arm or dedicated cold-transfer plate "
        "refrigerator extension.",
        ManageExtensionArgs,
    ),
    (
        "develop_bench_station",
        "Compile or update a schematic human-operated bench station from ordered operations "
        "(weigh, print_label, read_barcode, inspect) under 3D bench constraints.",
        DevelopBenchStationArgs,
    ),
    (
        "develop_custom_equipment",
        "Author or revise an original general-purpose 3D custom benchtop assembly from the "
        "project brief and user request (jigs, imaging fixtures, custom mechanisms).",
        DevelopCustomEquipmentArgs,
    ),
    (
        "configure_standalone_fixture",
        "Create or revise a standalone printable locating base, sample holder, or overhead "
        "camera imaging rig independent of Jubilee/OT-2, with print-ready tolerances and "
        "3D printer bed-volume checks.",
        ConfigureStandaloneFixtureArgs,
    ),
    (
        "record_build_log",
        "Record physical 3D-print and caliper/repeatability measurements against the active "
        "fixture or labware revision.",
        RecordBuildLogArgs,
    ),
    (
        "apply_build_feedback",
        "Apply recorded caliper or fit feedback from a build log to revise FDM pocket "
        "clearances and report only which parts require reprinting.",
        ApplyBuildFeedbackArgs,
    ),
    (
        "approve_machine",
        "Approve the current hardware design so software protocol planning becomes active. "
        "Only call when the user explicitly confirms approval of the machine.",
        ApproveMachineArgs,
    ),
    (
        "update_lab_profile",
        "Persist laboratory 3D printer specs, bench dimensions, measuring tools, and owned "
        "equipment, and optionally apply them to the active project.",
        UpdateLabProfileArgs,
    ),
    (
        "discover_open_hardware_projects",
        "Proactively search approved open-source hardware indices (GitHub, GitLab, OpenAlex, "
        "Europe PMC, NIH 3D) for documented lab hardware projects, CAD files "
        "(.step/.stl/.scad/.py), SPDX licenses, and BOMs beyond Jubilee and OT-2. Call by default "
        "in BOTH liquid_preparation and general workflows whenever scoping, proposing, or "
        "comparing platforms/mechanisms, using concise 2-4 word English technical queries (retry "
        "once broader or with require_cad=False if 0 candidates match).",
        DiscoverOpenHardwareProjectsArgs,
    ),
    (
        "ingest_open_hardware_project",
        "Verify license and commit/SHA-256 provenance, download a discovered open-source CAD "
        "file, normalize its units/axes in a sandboxed worker, and extract mounting features, "
        "BOM, assembly steps, and software references into the project.",
        IngestOpenHardwareProjectArgs,
    ),
    (
        "adapt_open_hardware_project",
        "Adapt an ingested open-source hardware project via either non-destructive mating CAD "
        "(adapter_plate, bracket, riser, enclosure, cutout_mount with 3D interference checks) "
        "or sandboxed parametric source regeneration (.py/.scad with explicit parameter diffs).",
        AdaptOpenHardwareProjectArgs,
    ),
    (
        "inspect_upstream_project",
        "Inspect parsed sections (geometry, license, bom, assembly, software, adaptation) of "
        "the currently ingested open-source hardware project.",
        InspectUpstreamProjectArgs,
    ),
    (
        "generate_purchasable_bom",
        "List parts for the active design in USD, CHF, or EUR. Report unresolved requirements, "
        "a missing design or a partial priced subtotal explicitly. Include pack quantities, "
        "supplier SKUs/MPNs, lead times, analytical FDM filament/print-time estimates, explicit "
        "shipping/tax exclusions, and hardware compatibility status.",
        GeneratePurchasableBomArgs,
    ),
    (
        "set_line_budget",
        "Record the user's declared whole-line USD planning amount or range for one unpriced "
        "BOM line_id. Quote the user's own declaration verbatim. Use equal bounds for one "
        "amount or null budget to remove a declaration on request.",
        SetLineBudgetArgs,
    ),
    (
        "choose_requirement_part",
        "Propose, select, replace or clear one custom or imported BOM requirement by its line_id. "
        "Use an exact catalog_id, a supplier hit by issuer and SKU, or a manufacturer's "
        "or supplier's identifier stated by the user. Only select on the user's instruction. "
        "For user-supplied evidence quote their words verbatim, including the source URL, unit and "
        "any stated part values. For lab stock allocate owned_quantity in required units. "
        "Compare candidates by proposing each. Selection and inventory do not place an order.",
        ChoosePartArgs,
    ),
    (
        "check_hardware_compatibility",
        "Run deterministic electrical, I/O, mechanical, and 3D-printer compatibility checks "
        "(supply rails, motor vs driver currents, power margin, GPIO/CSI ports, NEMA pitch, "
        "shaft bores, bed fit) on the active design.",
        CheckHardwareCompatibilityArgs,
    ),
    (
        "search_supplier_components",
        "Search official supplier APIs (DigiKey v4, Mouser v2) and AutoLab's curated catalog "
        "for purchasable electronics, motors, PSUs, and mechanical hardware with real MPNs and "
        "multi-currency pricing.",
        SearchSupplierComponentsArgs,
    ),
    (
        "generate_robot_protocol",
        "Compile and simulate an executable Opentrons API v2 (apiLevel 2.19) or Science Jubilee "
        "Python liquid-handling protocol supporting up to 96 targets, automatic multi-step serial "
        "dilutions, and clean diluent multi-dispense.",
        GenerateRobotProtocolArgs,
    ),
    (
        "configure_measurement_steps",
        "Configure the complete ordered set of recorded instrument readings on an approved design. "
        "Call inspect_project_state with include_components=true to get native operations, "
        "component IDs and registered commands. Keep an explicitly requested unsupported command "
        "as a missing capability. Use an empty list to remove the readings.",
        MeasurementSteps,
    ),
    (
        "simulate_robot_protocol",
        "Run strict AST, tip/pipette-range, well-capacity, and simulator verification on the "
        "project's generated Opentrons API v2 or Science Jubilee protocol.",
        SimulateRobotProtocolArgs,
    ),
    (
        "build_hardware_commissioning_pack",
        "Generate a project-bound HTTP Hardware Gateway config template (GRBL/Klipper/Loopback "
        "driver, software travel limits, watchdog) and mandatory physical E-stop commissioning "
        "checklist.",
        BuildHardwareCommissioningPackArgs,
    ),
]


def openai_tool_schemas(
    project: dict, lab_profile: dict | None = None, *, names: set[str] | None = None
) -> list[dict[str, Any]]:
    """Return strict OpenAI chat-completions function tool definitions."""
    _ = project, lab_profile
    deferred = [
        name for name, _, _ in _TOOL_SPECS
        if name != "load_tools" and (names is None or name not in names)
    ]
    if names is not None and not deferred:
        names = names - {"load_tools"}
    schemas = [
        {
            "type": "function",
            "function": {
                "name": name,
                "description": description,
                "strict": True,
                "parameters": _strict_function_schema(model),
            },
        }
        for name, description, model in _TOOL_SPECS
        if names is None or name in names
    ]
    for entry in schemas:
        if entry["function"]["name"] == "load_tools":
            items = entry["function"]["parameters"]["properties"]["names"]["items"]
            items["enum"] = deferred
    return schemas


def planning_tool_schemas(ctx: ToolContext) -> list[dict[str, Any]]:
    """Offer planning tools, the selected design adapter and any tools requested this turn."""
    names = ctx.loaded_tools | {
        "load_tools", "inspect_project_state", "update_project_brief", "check_spatial_fit",
        "lookup_reviewed_references", "discover_open_hardware_projects", "compare_design_routes",
    }
    plan = ctx.project.get("design_plan") or {}
    if plan.get("options"):
        names.add("choose_design_route")
        selected = (plan.get("decision") or {}).get("key")
        names.update(option["adapter"] for option in plan["options"] if option["key"] == selected)
    elif (ctx.project.get("workflow") or {}).get("kind") == "general":
        names.add("configure_standalone_fixture")
    return openai_tool_schemas(ctx.project, ctx.lab_profile, names=names)


def _tool_load_tools(args: LoadToolsArgs, ctx: ToolContext) -> ToolResult:
    unknown = set(args.names) - _DISPATCH.keys()
    if unknown:
        message = "Unknown tool names: " + ", ".join(sorted(unknown))
        return _fail("load_tools", "inspecting", message)
    ctx.loaded_tools.update(args.names)
    return ToolResult(
        tool="load_tools", ok=True, stage="inspecting",
        summary="Requested tools are available in the next step.",
    )


def _fail(
    tool: str,
    stage: str,
    message: str,
    *,
    recoverable: bool = True,
    diagnostics: list[str] | None = None,
    data: dict[str, Any] | None = None,
    action_run: dict | None = None,
) -> ToolResult:
    return ToolResult(
        tool=tool,
        ok=False,
        recoverable=recoverable,
        stage=stage,
        summary=message,
        diagnostics=diagnostics or [message],
        data=data or {},
        action_run=action_run,
    )


def _format_validation_error(exc: ValidationError) -> list[str]:
    return [
        f"{'.'.join(str(part) for part in err['loc']) or 'arguments'}: {err['msg']}"
        for err in exc.errors(include_url=False, include_input=False)[:6]
    ]


def _compact_project_state(project: dict, lab_profile: dict | None) -> dict[str, Any]:
    from app.design_workflow import workflow_state

    design = project.get("design") or {}
    eng = state_for(project)
    active_modules = [
        name
        for name in (
            "engineering_design",
            "standalone_fixture",
            "upstream_project",
            "bench_station",
            "concept_design",
            "labware_rack",
            "labware_plate",
            "tip_rack",
            "inspection_extension",
            "cold_transfer_extension",
        )
        if project.get(name)
    ]
    compact: dict[str, Any] = {
        "revision": project.get("revision"),
        "status": project.get("status"),
        "workflow": project.get("workflow"),
        "design_workflow": workflow_state(project),
        "summary": project.get("summary"),
        "brief": project.get("brief"),
        "selected_platform_id": eng.get("selected_platform_id"),
        "constraint_status": design.get("constraint_status"),
        "footprint": design.get("footprint"),
        "spatial_fit": design.get("spatial_fit") or evaluate_spatial_fit(project),
        "plan": project.get("plan"),
        "active_modules": active_modules,
    }
    compact["design_capabilities"] = design_routes.CAPABILITIES
    if project.get("design_plan"):
        compact["design_plan"] = project["design_plan"]
        compact["design_plan_needs_review"] = design_routes.needs_review(project)
    if project.get("labware_brief"):
        compact["labware_brief"] = project["labware_brief"]
    if project.get("tip_rack_request"):
        compact["tip_rack_request"] = project["tip_rack_request"]
    if project.get("dimension_provenance"):
        compact["dimension_provenance"] = project["dimension_provenance"]
    if bench := project.get("bench_station"):
        compact["bench_station"] = {
            "operations": bench["proposal"]["operations"],
            "footprint_mm": bench["footprint_mm"],
            "steps_count": len(bench["steps"]),
        }
    if concept := project.get("concept_design"):
        compact["concept_design"] = {
            "title": concept.get("title"),
            "footprint_mm": concept.get("footprint_mm"),
            "bounds_mm": concept.get("bounds_mm"),
            "components": [c.get("key") for c in concept.get("components", [])],
        }
    if sf := project.get("standalone_fixture"):
        compact["standalone_fixture"] = {
            "spec": sf["spec"],
            "derived": {
                "pocket_length_mm": sf["derived"]["pocket_length_mm"],
                "pocket_width_mm": sf["derived"]["pocket_width_mm"],
                "pocket_diameter_mm": sf["derived"].get("pocket_diameter_mm"),
                "scale_reference_length_mm": sf["derived"]["scale_reference_length_mm"],
                "tolerance_coupon": sf["derived"]["tolerance_coupon"],
            },
            "footprint_mm": sf["footprint_mm"],
            "height_mm": sf["height_mm"],
            "printer_fit_ok": sf["printer_fit_ok"],
            "parts": [
                {
                    "id": p["id"],
                    "size_mm": p["size_mm"],
                    "printer_fit": p["printer_fit"],
                }
                for p in sf["parts"]
            ],
        }
    if project.get("build_logs"):
        compact["build_logs"] = [
            {
                "id": item["id"],
                "revision": item["revision"],
                "fit_result": item["fit_result"],
                "parts_printed": item["parts_printed"],
                "measured_features": item.get("measured_features", []),
                "applied_revision": item.get("applied_revision"),
            }
            for item in project["build_logs"][-5:]
        ]
    if project.get("build_feedback"):
        compact["build_feedback"] = project["build_feedback"]
    if up := project.get("upstream_project"):
        prov = up.get("provenance") or {}
        geom = up.get("geometry") or {}
        adapt = up.get("adaptation") or {}
        compact["upstream_project"] = {
            "repo_full_name": prov.get("repo_full_name"),
            "commit_sha": prov.get("commit_sha"),
            "spdx_id": prov.get("spdx_id"),
            "cad_kind": geom.get("cad_kind"),
            "app_size_mm": geom.get("app_size_mm"),
            "parametric_parameters": geom.get("parametric_parameters", [])[:20],
            "bom_count": len(up.get("bom") or []),
            "adaptation": {
                "mode": adapt.get("mode"),
                "part_id": adapt.get("part_id"),
                "interference_passed": (adapt.get("interference") or {}).get("passed"),
                "printer_fit_ok": (adapt.get("printer_fit") or {}).get("fits"),
            }
            if adapt
            else None,
        }
    profile_ctx = lab_profile_prompt_context(lab_profile)
    if profile_ctx:
        compact["lab_profile"] = profile_ctx
    return compact


def _tool_inspect_project_state(args: InspectProjectStateArgs, ctx: ToolContext) -> ToolResult:
    data = _compact_project_state(ctx.project, ctx.lab_profile)
    if ctx.project.get("bench_station") or ctx.project.get("concept_design"):
        from app.measurement_adapters import catalog
        from app.measurement_program import native_plan

        data["measurement_adapters"] = catalog(include_schema=args.include_components)
        base = native_plan(ctx.project)
        data["measurement_anchors"] = [
            {"id": r["id"], "action": r["action"]} for r in base["operations"]
        ]
        data["measurement_steps"] = (ctx.project.get("measurement_program") or {}).get("steps", [])
    if args.include_components:
        from app.project_assembly import physical_assembly

        assembly = physical_assembly(ctx.project)
        data["components"] = [
            {
                "id": p["native_id"], "instance_id": p["id"], "kind": p["kind"],
                "label": p["label"], "origin": p["origin"], "size": p["size_mm"],
                "position": (p["placement"] or {}).get("world_translation_mm"),
                "placement": p["placement"], "frame_id": p["frame_id"],
                "evidence_ids": p["evidence_ids"],
            }
            for p in assembly["instances"]
        ]
        data["frames"] = assembly["frames"]
        data["connections"] = assembly["connections"]
        data["interfaces"] = assembly["interfaces"]
        data["procurement_links"] = [
            {key: r[key] for key in ("id", "bom_line_id", "instance_ids", "evidence_ids",
                                     "selected_part", "owned_quantity") if key in r}
            for r in assembly["requirements"]
        ]
        data["assembly_evidence"] = assembly["evidence"]
    return ToolResult(
        tool="inspect_project_state",
        ok=True,
        stage="inspecting",
        summary=(
            f"Revision {data['revision']}, workflow={data['workflow']['kind']}, "
            f"constraint_status={data['constraint_status']}, "
            f"modules={data['active_modules']}."
        ),
        data=data,
    )


def _tool_check_spatial_fit(args: CheckSpatialFitArgs, ctx: ToolContext) -> ToolResult:
    candidate_foot = None
    if any(
        v is not None
        for v in (args.candidate_width_mm, args.candidate_depth_mm, args.candidate_height_mm)
    ):
        candidate_foot = {}
        if args.candidate_width_mm is not None:
            candidate_foot["width_mm"] = float(args.candidate_width_mm)
        if args.candidate_depth_mm is not None:
            candidate_foot["depth_mm"] = float(args.candidate_depth_mm)
        if args.candidate_height_mm is not None:
            candidate_foot["height_mm"] = float(args.candidate_height_mm)

    profile = ctx.lab_profile if args.include_printable_parts else None
    fit = evaluate_spatial_fit(
        ctx.project,
        lab_profile=profile,
        candidate_footprint=candidate_foot,
        platform_id=args.platform_id,
    )
    diagnostics = [v["message"] for v in fit["violations"]]
    data: dict[str, Any] = {"spatial_fit": fit}
    msg = (
        f"Spatial check ({fit['subject']}): status={fit['status']}. "
        + (format_spatial_violations(fit) if fit["violations"] else "No 3D violations.")
    )
    if mixed := fit.get("cad_assembly"):
        diagnostics.extend(f"Review envelope overlap: {a} / {b}."
                           for a, b in mixed["possible_overlaps"])
        for item in mixed["instances"]:
            if item["access_blockers"]:
                blockers = ", ".join(item["access_blockers"])
                diagnostics.append(f"Review {item['name']} access against {blockers}.")
            if item["printer_fit"] and not item["printer_fit"]["fits"]:
                diagnostics.append(item["printer_fit"]["detail"])
        msg = f"Workspace {fit['status']}. Checked {len(mixed['instances'])} CAD placements."
    if kin := fit.get("kinematics"):
        diagnostics.extend(kin.get("issues") or [])
        if kin.get("issues"):
            msg += " Reach/transit issues: " + "; ".join(kin["issues"])
    if concept := ctx.project.get("concept_design"):
        from app.kinematics_checks import evaluate_concept_kinematics

        concept_kin = evaluate_concept_kinematics(concept)
        data["concept_kinematics"] = concept_kin
        diagnostics.extend(concept_kin["issues"])
        if concept_kin["issues"]:
            msg += " Kinematics issues: " + "; ".join(concept_kin["issues"])
        else:
            msg += (
                f" Kinematics passed (min_clearance="
                f"{concept_kin['swept_clearances'].get('min_clearance_mm')}, "
                f"mass~{concept_kin['payload_and_com']['total_estimated_mass_g']} g)."
            )
    return ToolResult(
        tool="check_spatial_fit",
        ok=True,
        stage="verifying",
        summary=msg.strip(),
        diagnostics=diagnostics,
        data=data,
    )


def _tool_lookup_references(args: LookupReviewedReferencesArgs, ctx: ToolContext) -> ToolResult:
    refs = reference_library().get("references", [])
    matched_refs = []
    if args.reference_id:
        try:
            ref = resolve_reference(args.reference_id)
            matched_refs = [ref]
            if not any(r["reference"]["id"] == ref["id"] for r in ctx.recommendations):
                ctx.recommendations.append(
                    {"reference": ref, "reason": "Consulted during tool-assisted planning."}
                )
        except ValueError as exc:
            return _fail("lookup_reviewed_references", "researching", str(exc))
    elif args.query:
        q = args.query.casefold().strip()
        for entry in refs:
            haystack = " ".join(
                str(entry.get(k, ""))
                for k in ("id", "title", "kind", "publisher", "use", "limits")
            ).casefold()
            if any(token in haystack for token in q.split() if len(token) >= 2):
                matched_refs.append(entry)
    else:
        matched_refs = list(refs)

    compact_refs = [
        {
            k: entry.get(k)
            for k in ("id", "title", "kind", "publisher", "url", "checked_on", "use", "limits")
            if entry.get(k) is not None
        }
        for entry in matched_refs[:6]
    ]
    data: dict[str, Any] = {
        "references": compact_refs,
        "bench_station_operations": {
            op: {"label": info["label"], "size_mm": info["size"], "clearance_mm": info["clearance"]}
            for op, info in STATION_MODULES.items()
        },
        "tip_rack_profiles": [
            {"id": o["id"], "label": o["label"], "size_mm": o.get("geometry", {}).get("size_mm")}
            for o in tip_rack_options(ctx.project).get("options", [])
        ],
    }
    if args.include_platforms:
        data["platforms"] = platform_profiles()
    kind = args.library_kind or "all"
    if kind in ("labware", "all"):
        from app.labware_registry import list_labware

        data["labware_catalog"] = list_labware(query=args.query)
    if kind in ("hardware", "all"):
        from app.labware_registry import list_hardware_parts

        data["hardware_catalog"] = list_hardware_parts(query=args.query)[:12]
    return ToolResult(
        tool="lookup_reviewed_references",
        ok=True,
        stage="researching",
        summary=f"Found {len(compact_refs)} reviewed reference(s).",
        data=data,
    )


async def _tool_design_routes(args, ctx: ToolContext) -> ToolResult:
    comparing = isinstance(args, design_routes.CompareRoutes)
    name = "compare_design_routes" if comparing else "choose_design_route"
    plan = (design_routes.compare_routes(args, ctx) if comparing
            else design_routes.choose_route(args, ctx))
    if not comparing and args.make_passive_fixture and plan["decision"]["applied_revision"] is None:
        option = next(o for o in plan["options"] if o["key"] == args.key)
        parameters = design_routes.passive_fixture_arguments(option)
        previous = ctx.project
        ctx.project = {**previous, "design_plan": plan}
        result = await execute_tool("configure_standalone_fixture", parameters, ctx)
        if not result.ok or result.requires_human_input:
            ctx.project = previous
        result.tool = name
        return result
    changed = ctx.project.get("design_plan") != plan
    if changed:
        from app.project_storage import make_snapshot

        updated = {**ctx.project, "design_plan": plan, "revision": ctx.project["revision"] + 1}
        make_snapshot(updated, [])
        ctx.project = updated
        ctx.mutated = True
    question = None
    if comparing and not plan["decision"]:
        question = next((o["measurement_question"] for o in plan["options"]
                         if o["measurement_question"]), None)
        if question:
            labels = "\n".join(f"- {o['label']}" for o in plan["options"])
            question = f"I compared these options.\n\n{labels}\n\n{question}"
    if comparing:
        message = "Compared design routes against the recorded operations."
    elif plan["decision"]["applied_revision"] is not None:
        message = "The selected route has already been applied to this project."
    else:
        message = "Recorded the selected route. Run its implementation tool to apply it."
    return ToolResult(tool=name, ok=True, stage="designing", mutated=changed, summary=message,
                      data=plan, requires_human_input=bool(question), question=question)


def _tool_update_project_brief(args: UpdateProjectBriefArgs, ctx: ToolContext) -> ToolResult:
    apply_fn = api_hook("apply_update", apply_update)
    patch: dict[str, Any] = {}
    if args.workflow_kind is not None:
        patch["workflow_kind"] = args.workflow_kind
    for field_name in ("objective", "process", "materials", "equipment", "constraints"):
        val = getattr(args, field_name)
        if val is not None:
            patch[f"brief_{field_name}"] = val
    for dim_key in ("workspace_width_mm", "workspace_depth_mm", "workspace_height_mm"):
        val = getattr(args, dim_key)
        if val is not None:
            patch[dim_key] = float(val)
    if args.cooling_enabled is not None:
        patch["cooling_enabled"] = args.cooling_enabled
    if args.cooling_target_c is not None:
        patch["cooling_target_c"] = float(args.cooling_target_c)

    if not patch:
        return ToolResult(
            tool="update_project_brief",
            ok=True,
            stage="designing",
            summary="No brief or workspace fields supplied; project unchanged.",
        )

    previous = ctx.project
    try:
        candidate = apply_fn(previous, patch)
        for dim_key, prov_key in (
            ("workspace_width_mm", "bench_width_mm"),
            ("workspace_depth_mm", "bench_depth_mm"),
            ("workspace_height_mm", "bench_height_mm"),
        ):
            if dim_key in patch:
                candidate = record_dimension(
                    candidate,
                    prov_key,
                    float(patch[dim_key]),
                    provenance="user_measured",
                    source="user_chat_specification",
                )
        if candidate.get("bench_station"):
            station = compile_station(
                StationProposal.model_validate(candidate["bench_station"]["proposal"]),
                workspace_width_mm=candidate["summary"].get("workspace_width_mm"),
                workspace_depth_mm=candidate["summary"].get("workspace_depth_mm"),
                workspace_height_mm=candidate["summary"].get("workspace_height_mm"),
            )
            candidate["bench_station"] = station
        if candidate.get("concept_design"):
            from app.concept_design import compile_concept

            concept = compile_concept(
                candidate["concept_design"]["proposal"],
                candidate["summary"].get("workspace_width_mm"),
                workspace_depth_mm=candidate["summary"].get("workspace_depth_mm"),
                workspace_height_mm=candidate["summary"].get("workspace_height_mm"),
            )
            candidate["concept_design"] = concept
        if (previous.get("design_plan") and candidate.get("standalone_fixture")
                and any(k.startswith("workspace_") for k in patch)):
            from app.fixture_adapter import compile_standalone_fixture

            candidate["standalone_fixture"] = compile_standalone_fixture(
                candidate["standalone_fixture"]["spec"], lab_profile=ctx.lab_profile,
                **{key: candidate["summary"].get(key) for key in
                   ("workspace_width_mm", "workspace_depth_mm", "workspace_height_mm")},
            )
        refresh_spatial_fit(candidate)
    except ValueError as exc:
        plan = previous.get("design_plan")
        requested = {k: v for k, v in patch.items()
                     if k in design_routes.WorkspaceRequest.model_fields}
        kind = previous["workflow"]["kind"]
        if (not plan or not any(k.startswith("workspace_") for k in requested)
                or set(patch) - set(requested) - {"workflow_kind"}
                or patch.get("workflow_kind", kind) != kind):
            return _fail("update_project_brief", "verifying", str(exc))
        prior = {k: v for k, v in (plan.get("pending_workspace") or {}).items() if v is not None}
        pending = design_routes.WorkspaceRequest(**{**prior, **requested}).model_dump()
        changed = pending != plan.get("pending_workspace")
        if changed:
            ctx.project = {**previous, "revision": previous["revision"] + 1,
                           "design_plan": {**plan, "pending_workspace": pending}}
            ctx.mutated = True
        return ToolResult(tool="update_project_brief", ok=True, stage="designing", mutated=changed,
                          summary="The new workspace does not fit the previous design. "
                                  "Compare routes before replacing it.", diagnostics=[str(exc)],
                          data={"pending_workspace": pending})
    if previous.get("design_plan", {}).get("pending_workspace"):
        remaining = deepcopy(previous["design_plan"]["pending_workspace"])
        for key in patch:
            remaining.pop(key, None)
        candidate["design_plan"]["pending_workspace"] = (
            design_routes.WorkspaceRequest.model_validate(remaining).model_dump()
            if any(v is not None for v in remaining.values()) else None
        )

    mutated = candidate != previous
    ctx.project = candidate
    if mutated:
        ctx.mutated = True
    fit = candidate["design"].get("spatial_fit") or evaluate_spatial_fit(candidate)
    diagnostics = [v["message"] for v in fit.get("violations", [])]
    return ToolResult(
        tool="update_project_brief",
        ok=True,
        stage="designing",
        mutated=mutated,
        summary=(
            f"Updated project brief/constraints (revision {candidate['revision']}, "
            f"workflow={candidate['workflow']['kind']}, spatial={fit['status']})."
        ),
        diagnostics=diagnostics,
        data={
            "workflow": candidate["workflow"],
            "summary": candidate["summary"],
            "brief": candidate["brief"],
            "spatial_fit": fit,
        },
    )


def _tool_update_liquid_recipe(args: UpdateLiquidRecipeArgs, ctx: ToolContext) -> ToolResult:
    from app.design_workflow import workflow_state

    apply_fn = api_hook("apply_update", apply_update)
    patch: dict[str, Any] = {}
    for key in ("stock_concentration", "target_concentrations", "final_volume"):
        val = getattr(args, key)
        if val is not None:
            patch[key] = val
    if not patch:
        return _fail("update_liquid_recipe", "designing", "No liquid recipe fields provided.")

    previous = ctx.project
    try:
        candidate = apply_fn(previous, patch)
        if workflow_state(previous)["machine_approved"] and (
            candidate.get("plan", {}).get("status") != "ready"
        ):
            raise ValueError(candidate["plan"]["message"])
        if candidate.get("labware_plate"):
            candidate = reapply_plate(candidate, require_accepted=False)
        if candidate.get("labware_rack"):
            from app.labware import reapply_labware

            candidate = reapply_labware(candidate, require_accepted=False)
        if candidate.get("tip_rack"):
            candidate = reapply_tip_rack(candidate, require_accepted=False)
    except ValueError as exc:
        return _fail("update_liquid_recipe", "verifying", str(exc))

    mutated = candidate != previous
    ctx.project = candidate
    if mutated:
        ctx.mutated = True

    plan = candidate.get("plan") or {}
    diagnostics = []
    if plan.get("status") != "ready" and plan.get("message"):
        diagnostics.append(plan["message"])
    if plate := candidate.get("labware_plate"):
        compat = plate.get("compatibility") or {}
        diagnostics.extend(compat.get("blocking_issues") or [])
    return ToolResult(
        tool="update_liquid_recipe",
        ok=True,
        stage="designing",
        mutated=mutated,
        summary=f"Recipe updated (plan status={plan.get('status')}).",
        diagnostics=diagnostics,
        data={"plan": plan, "summary": candidate["summary"]},
    )


def _tool_select_platform(args: SelectPlatformArgs, ctx: ToolContext) -> ToolResult:
    from app.commercial_design import adopt_commercial
    from app.foundation_design import adopt_foundation, is_source_based
    from app.platform_profiles import commercial_sourcing

    previous = ctx.project
    if previous.get("workflow", {}).get("kind") != "liquid_preparation":
        return _fail(
            "select_platform",
            "verifying",
            "Liquid preparation workflow must be established before selecting a liquid platform.",
        )
    if args.platform_id == "opentrons-ot2" and (
        previous.get("labware_rack")
        or previous.get("labware_plate")
        or previous.get("tip_rack")
    ):
        return _fail(
            "select_platform",
            "verifying",
            "Remove custom Jubilee labware before switching platforms to Opentrons OT-2.",
        )

    fit = evaluate_spatial_fit(previous, platform_id=args.platform_id)
    if fit["status"] == "infeasible":
        detail = format_spatial_violations(fit)
        return _fail(
            "select_platform",
            "verifying",
            f"Cannot select {args.platform_id} because it does not fit the 3D workspace: {detail}",
            diagnostics=[v["message"] for v in fit["violations"]],
            data={"spatial_fit": fit},
        )

    try:
        candidate = deepcopy(previous)
        canonical_choice = (
            "Use Opentrons OT-2" if args.platform_id == "opentrons-ot2" else "Use Jubilee"
        )
        if state_for(candidate).get("selected_platform_id") != args.platform_id:
            candidate = apply_engineering_update(
                candidate,
                EngineeringUpdate(
                    platform_choice=args.platform_id, choice_source=canonical_choice
                ),
                canonical_choice,
                bump=True,
            )
        if args.platform_id == "opentrons-ot2":
            if candidate.get("design", {}).get("platform", {}).get("id") != "opentrons-ot2":
                candidate = adopt_commercial(candidate)
                candidate["sourcing"] = commercial_sourcing()
        else:
            if not is_source_based(candidate):
                candidate = adopt_foundation(candidate)
        refresh_spatial_fit(candidate)
        candidate["workflow"] = describe_workflow(candidate)
    except ValueError as exc:
        return _fail("select_platform", "designing", str(exc))

    mutated = candidate != previous
    ctx.project = candidate
    if mutated:
        ctx.mutated = True
    return ToolResult(
        tool="select_platform",
        ok=True,
        stage="designing",
        mutated=mutated,
        summary=f"Selected platform {args.platform_id} (3D fit status={fit['status']}).",
        data={
            "selected_platform_id": args.platform_id,
            "spatial_fit": candidate["design"].get("spatial_fit"),
        },
    )


async def _tool_develop_workstation_layout(
    args: DevelopWorkstationLayoutArgs, ctx: ToolContext
) -> ToolResult:
    from app.commercial_design import adopt_commercial
    from app.design_agent import develop_design
    from app.foundation_design import adopt_foundation, is_source_based
    from app.labware import reapply_labware
    from app.platform_profiles import commercial_sourcing

    previous = ctx.project
    selected = state_for(previous).get("selected_platform_id")
    if selected is None:
        return ToolResult(
            tool="develop_workstation_layout",
            ok=True,
            stage="verifying",
            requires_human_input=True,
            question=(
                "Should we design around the open-hardware Jubilee system or an assembled "
                "Opentrons OT-2 platform?"
            ),
            summary="Platform choice is required before developing the liquid layout.",
        )
    if previous.get("workflow", {}).get("kind") != "liquid_preparation":
        return _fail(
            "develop_workstation_layout",
            "verifying",
            "Workflow must be liquid_preparation before developing a workstation layout.",
        )
    if not has_workstation_design_context(previous):
        question = workstation_context_question(previous)
        return ToolResult(
            tool="develop_workstation_layout",
            ok=True,
            stage="inspecting",
            requires_human_input=True,
            question=question,
            summary="Need liquid workstation process/materials context before layout design.",
        )
    if (
        previous.get("labware_rack")
        or previous.get("labware_plate")
        or previous.get("tip_rack")
    ) and selected != "jubilee":
        return _fail(
            "develop_workstation_layout",
            "verifying",
            "Custom Jubilee labware must be removed before developing a non-Jubilee layout.",
        )

    fit = evaluate_spatial_fit(previous, platform_id=selected)
    if fit["status"] == "infeasible":
        detail = format_spatial_violations(fit)
        return _fail(
            "develop_workstation_layout",
            "verifying",
            f"Workspace 3D limits do not fit {selected}: {detail}",
            diagnostics=[v["message"] for v in fit["violations"]],
            data={"spatial_fit": fit},
        )

    commercial = selected == "opentrons-ot2"
    if commercial:
        base = (
            deepcopy(previous)
            if previous.get("design", {}).get("platform", {}).get("id") == selected
            else adopt_commercial(previous)
        )
        base["sourcing"] = commercial_sourcing()
    else:
        base = deepcopy(previous) if is_source_based(previous) else adopt_foundation(previous)

    develop_fn = api_hook("develop_design", develop_design)
    result, run = await develop_fn(base, args.instruction)
    run_entry = {**run, "kind": "develop_layout"}
    ctx.action_runs.append(run_entry)
    if run.get("status") != "accepted":
        return _fail(
            "develop_workstation_layout",
            "repairing",
            run.get("message") or "Layout proposal failed validation checks.",
            action_run=run_entry,
        )

    try:
        if result.get("labware_rack"):
            result = reapply_labware(result)
        if result.get("labware_plate"):
            result = reapply_plate(result)
        if result.get("tip_rack"):
            result = reapply_tip_rack(result)
    except ValueError as exc:
        return _fail("develop_workstation_layout", "repairing", str(exc), action_run=run_entry)

    if commercial:
        result["sourcing"] = commercial_sourcing()
    refresh_spatial_fit(result)
    result["workflow"] = describe_workflow(result)
    ctx.project = result
    ctx.mutated = True
    return ToolResult(
        tool="develop_workstation_layout",
        ok=True,
        stage="designing",
        mutated=True,
        summary=run.get("message") or "Workstation layout updated.",
        data={
            "revision": result["revision"],
            "spatial_fit": result["design"].get("spatial_fit"),
        },
        action_run=run_entry,
    )


def _tube_spec_patch_from_args(args: ConfigureLabwareArgs) -> tuple[dict[str, Any], dict[str, str]]:
    labels = {
        "body_diameter_max_mm": "body diameter",
        "rim_diameter_min_mm": "rim diameter",
        "cap_diameter_max_mm": "closed cap diameter",
        "below_rim_height_mm": "length below the rim",
        "pitch_mm": "pitch",
        "radial_clearance_mm": "radial clearance",
    }
    patch: dict[str, Any] = {}
    sources: dict[str, str] = {}
    for key, label in labels.items():
        val = getattr(args, key)
        if val is not None:
            patch[key] = float(val)
            sources[key] = f"{label} {float(val):g} mm"
    if args.count is not None:
        patch["count"] = int(args.count)
        sources["count"] = f"{int(args.count)} tubes"
    if args.columns is not None:
        patch["columns"] = int(args.columns)
        sources["columns"] = f"{int(args.columns)} columns"
    return patch, sources


def _apply_typed_tube_rack(args: ConfigureLabwareArgs, ctx: ToolContext) -> ToolResult:
    from app.labware import (
        FIELD_LABELS,
        REQUIRED_FIELDS,
        _brief,
        develop_labware,
    )

    previous = ctx.project
    patch, sources = _tube_spec_patch_from_args(args)
    prior_brief = previous.get("labware_brief") or _brief({}, {})
    spec = deepcopy(prior_brief["spec"])
    src_map = deepcopy(prior_brief["sources"])
    candidate = deepcopy(previous)
    for key, val in patch.items():
        spec[key] = val
        src_map[key] = {
            "literal": sources[key],
            "value": val,
            "recorded_at_revision": candidate["revision"],
        }
        if key.endswith("_mm"):
            candidate = record_dimension(
                candidate,
                f"tube_{key}",
                float(val),
                provenance="user_measured",
                source="user_chat_specification",
            )
    new_brief = _brief(spec, src_map)
    if new_brief != prior_brief:
        candidate["labware_brief"] = new_brief
        candidate["revision"] += 1
        candidate["status"] = "draft"

    missing = list(new_brief["missing_fields"])
    if missing:
        mutated = candidate != previous
        if mutated:
            ctx.project = candidate
            ctx.mutated = True
        readable = [FIELD_LABELS.get(k, k) for k in missing]
        return ToolResult(
            tool="configure_labware",
            ok=True,
            stage="inspecting",
            mutated=mutated,
            requires_human_input=True,
            question="Please provide the missing tube dimensions: " + "; ".join(readable) + ".",
            summary=f"Recorded partial tube dimensions; still missing {len(missing)} field(s).",
            data={"missing_fields": [k for k in REQUIRED_FIELDS if k in missing]},
        )

    try:
        built, run = develop_labware(candidate)
    except ValueError as exc:
        return _fail("configure_labware", "verifying", str(exc))

    refresh_spatial_fit(built)
    printable = evaluate_printable_fit(built, ctx.lab_profile)
    diagnostics = [c["detail"] for c in printable if not c["fits"]]
    ctx.project = built
    ctx.mutated = True
    ctx.action_runs.append(run)
    return ToolResult(
        tool="configure_labware",
        ok=True,
        stage="designing",
        mutated=True,
        summary=run.get("message") or "Custom tube rack built.",
        diagnostics=diagnostics,
        data={"labware_rack_size_mm": built["labware_rack"]["size_mm"], "printable": printable},
        action_run=run,
    )


def _apply_typed_tip_rack(args: ConfigureLabwareArgs, ctx: ToolContext) -> ToolResult:
    from app.tip_racks import REQUIRED_FIELDS as TIP_FIELDS

    previous = ctx.project
    candidate = deepcopy(previous)
    if args.printer_available is not None:
        candidate["fabrication_capabilities"] = {
            "printer_available": args.printer_available,
            "source": "agent_tool_declaration",
        }
    spec_fields = (
        "body_diameter_max_mm",
        "rim_diameter_min_mm",
        "rim_diameter_max_mm",
        "below_rim_height_mm",
        "count",
        "columns",
        "pitch_mm",
        "radial_clearance_mm",
    )
    spec_vals = {}
    spec_srcs = {}
    for key in spec_fields:
        val = getattr(args, key)
        if val is None:
            continue
        spec_vals[key] = int(val) if key in ("count", "columns") else float(val)
        if key == "count":
            spec_srcs[key] = f"{int(val)} positions"
        elif key == "columns":
            spec_srcs[key] = f"{int(val)} columns"
        elif key == "rim_diameter_min_mm":
            spec_srcs[key] = f"rim diameter minimum {float(val):g} mm"
        elif key == "rim_diameter_max_mm":
            spec_srcs[key] = f"rim diameter maximum {float(val):g} mm"
        elif key == "body_diameter_max_mm":
            spec_srcs[key] = f"body diameter {float(val):g} mm"
        elif key == "below_rim_height_mm":
            spec_srcs[key] = f"below rim {float(val):g} mm"
        elif key == "pitch_mm":
            spec_srcs[key] = f"pitch {float(val):g} mm"
        elif key == "radial_clearance_mm":
            spec_srcs[key] = f"radial clearance {float(val):g} mm"

    profile_id = args.tip_profile_id
    alias_text = {
        "tipone_96_tiprack_200ul": "TipOne 200 uL tip rack",
        "opentrons_96_tiprack_300ul": "Opentrons 96 Tip Rack 300 uL",
        TIP_PRINTED_ID: "custom printed tip holder",
        TIP_PREVIOUS_ID: "previous tip rack",
    }.get(profile_id or "", "")
    synthetic_parts = [alias_text] if alias_text else []
    synthetic_parts.extend(spec_srcs.values())
    synthetic_msg = ", ".join(synthetic_parts) or "tip rack"

    tip_update = TipRackUpdate.model_validate(
        {
            "profile_id": profile_id,
            "source": synthetic_msg if profile_id else None,
            "spec": spec_vals,
            "spec_sources": spec_srcs,
        }
    )
    if spec_vals or args.printer_available is not None or profile_id:
        candidate = update_tip_request(candidate, tip_update, synthetic_msg)

    eff_profile = profile_id or (candidate.get("tip_rack_request") or {}).get("profile_id")
    if eff_profile == TIP_PRINTED_ID:
        printer_state = (candidate.get("fabrication_capabilities") or {}).get(
            "printer_available", "unknown"
        )
        if printer_state != "yes":
            merged_ctx = candidate
        else:
            merged_spec = {
                **(candidate.get("tip_rack_request") or {}).get("spec", {}),
                **spec_vals,
            }
            missing = [k for k in TIP_FIELDS if k not in merged_spec]
            if missing:
                ctx.project = candidate
                ctx.mutated = candidate != previous
                return ToolResult(
                    tool="configure_labware",
                    ok=True,
                    stage="inspecting",
                    mutated=ctx.mutated,
                    requires_human_input=True,
                    question="Please provide the missing pipette tip dimensions: "
                    + ", ".join(missing)
                    + ".",
                    summary=f"Saved tip holder specs; still missing {len(missing)} field(s).",
                    data={"missing_fields": list(TIP_REQUIRED_FIELDS)},
                )
            merged_ctx = candidate
    else:
        merged_ctx = candidate

    try:
        tip_call = TipRackUpdate.model_validate(
            {
                "profile_id": eff_profile,
                "source": alias_text or (profile_id or "custom printed tip holder")
                if eff_profile
                else None,
                "spec": spec_vals,
                "spec_sources": spec_srcs,
            }
        )
        updated, run = select_tip_rack(
            merged_ctx, tip_call, synthetic_msg, accept_placement=args.confirm_tip_rack_shift
        )
    except ValueError as exc:
        return _fail("configure_labware", "verifying", str(exc))

    refresh_spatial_fit(updated)
    printable = evaluate_printable_fit(updated, ctx.lab_profile)
    mutated = updated != previous
    ctx.project = updated
    if mutated:
        ctx.mutated = True
    ctx.action_runs.append(run)
    pending_req = updated.get("tip_rack_request") or {}
    needs_input = bool(
        pending_req.get("awaiting_printer") or pending_req.get("awaiting_placement")
    )
    return ToolResult(
        tool="configure_labware",
        ok=run.get("status") != "rejected",
        recoverable=run.get("status") == "rejected",
        stage="inspecting" if needs_input else "designing",
        mutated=mutated,
        requires_human_input=needs_input,
        question=run.get("message") if needs_input else None,
        summary=run.get("message") or "Tip rack configuration updated.",
        diagnostics=[c["detail"] for c in printable if not c["fits"]],
        data={"printable": printable},
        action_run=run,
    )


def _tool_configure_labware(args: ConfigureLabwareArgs, ctx: ToolContext) -> ToolResult:
    from app.labware import remove_labware

    previous = ctx.project
    if args.action == "configure_tube_rack":
        return _apply_typed_tube_rack(args, ctx)

    if args.action == "remove_tube_rack":
        try:
            updated = remove_labware(previous)
        except ValueError as exc:
            return _fail("configure_labware", "verifying", str(exc))
        refresh_spatial_fit(updated)
        ctx.project = updated
        ctx.mutated = updated != previous
        return ToolResult(
            tool="configure_labware",
            ok=True,
            stage="designing",
            mutated=ctx.mutated,
            summary="Removed custom tube rack and restored generic rack envelope.",
        )

    if args.action == "select_microplate":
        profile = args.plate_profile_id or CORNING_96_ID
        if profile == CORNING_96_ID:
            literal = "Corning 96 well plate"
        else:
            from app.labware_registry import get_labware_definition

            try:
                summary = get_labware_definition(profile)["summary"]
            except ValueError as exc:
                return _fail("configure_labware", "verifying", str(exc))
            literal = f"{summary['display_name']} ({profile})"
        sel = LabwareSelection(profile_id=profile, source=literal)
        try:
            updated, run = select_plate(previous, sel, literal)
        except ValueError as exc:
            return _fail("configure_labware", "verifying", str(exc))
        refresh_spatial_fit(updated)
        printable = evaluate_printable_fit(updated, ctx.lab_profile)
        mutated = updated != previous
        ctx.project = updated
        if mutated:
            ctx.mutated = True
        ctx.action_runs.append(run)
        compat = (updated.get("labware_plate") or {}).get("compatibility") or {}
        diagnostics = list(compat.get("blocking_issues") or []) + [
            c["detail"] for c in printable if not c["fits"]
        ]
        return ToolResult(
            tool="configure_labware",
            ok=run.get("status") not in ("rejected", "failed"),
            recoverable=True,
            stage="designing",
            mutated=mutated,
            summary=run.get("message") or "Selected Corning 96-well microplate reference.",
            diagnostics=diagnostics,
            data={"compatibility": compat, "printable": printable},
            action_run=run,
        )

    if args.action == "revert_to_tube_rack":
        literal = "previous tube rack"
        sel = LabwareSelection(profile_id="previous_tube_rack", source=literal)
        try:
            updated, run = select_plate(previous, sel, literal)
        except ValueError as exc:
            return _fail("configure_labware", "verifying", str(exc))
        refresh_spatial_fit(updated)
        mutated = updated != previous
        ctx.project = updated
        if mutated:
            ctx.mutated = True
        ctx.action_runs.append(run)
        return ToolResult(
            tool="configure_labware",
            ok=True,
            stage="designing",
            mutated=mutated,
            summary=run.get("message") or "Restored previous tube rack.",
            action_run=run,
        )

    if args.action == "show_tip_rack_options":
        try:
            updated, run = show_tip_options(previous)
        except ValueError as exc:
            return _fail("configure_labware", "verifying", str(exc))
        ctx.project = updated
        ctx.mutated = updated != previous
        ctx.action_runs.append(run)
        return ToolResult(
            tool="configure_labware",
            ok=True,
            stage="researching",
            mutated=ctx.mutated,
            summary=run.get("message") or "Listed tip-rack options.",
            data={"tip_rack_options": tip_rack_options(updated)},
            action_run=run,
        )

    return _apply_typed_tip_rack(args, ctx)


async def _tool_manage_extension(args: ManageExtensionArgs, ctx: ToolContext) -> ToolResult:
    from app.cold_transfer import apply_cold_transfer, clear_cold_transfer
    from app.inspection import clear_extension, develop_inspection

    previous = ctx.project
    if args.extension == "inspection":
        if args.operation == "remove":
            if not previous.get("inspection_extension"):
                return ToolResult(
                    tool="manage_extension",
                    ok=True,
                    stage="designing",
                    summary="No inspection extension active; project unchanged.",
                )
            updated = clear_extension(previous)
            updated["revision"] += 1
            updated["status"] = "draft"
            refresh_spatial_fit(updated)
            ctx.project = updated
            ctx.mutated = True
            return ToolResult(
                tool="manage_extension",
                ok=True,
                stage="designing",
                mutated=True,
                summary="Removed the inspection module.",
            )
        try:
            updated, run = await develop_inspection(
                previous, args.instruction or "Add inspection camera module."
            )
        except Exception as exc:
            return _fail("manage_extension", "repairing", str(exc))
        ctx.action_runs.append(run)
        if run.get("status") != "accepted":
            return _fail(
                "manage_extension",
                "repairing",
                run.get("message") or "Inspection extension failed checks.",
                action_run=run,
            )
        ctx.project = updated
        ctx.mutated = True
        return ToolResult(
            tool="manage_extension",
            ok=True,
            stage="designing",
            mutated=True,
            summary=run.get("message") or "Added inspection extension.",
            data={"spatial_fit": updated["design"].get("spatial_fit")},
            action_run=run,
        )

    # cold_transfer
    if args.operation == "remove":
        if not previous.get("cold_transfer_extension"):
            return ToolResult(
                tool="manage_extension",
                ok=True,
                stage="designing",
                summary="No cold-transfer extension active; project unchanged.",
            )
        updated = clear_cold_transfer(previous)
        updated["revision"] += 1
        updated["status"] = "draft"
        refresh_spatial_fit(updated)
        ctx.project = updated
        ctx.mutated = True
        return ToolResult(
            tool="manage_extension",
            ok=True,
            stage="designing",
            mutated=True,
            summary="Removed cold-transfer refrigerator extension.",
        )

    try:
        updated = apply_cold_transfer(previous)
    except ValueError as exc:
        return _fail("manage_extension", "verifying", str(exc))
    ctx.project = updated
    ctx.mutated = True
    return ToolResult(
        tool="manage_extension",
        ok=True,
        stage="designing",
        mutated=True,
        summary="Added dedicated refrigerated 96-well plate cold-transfer concept.",
        data={"spatial_fit": updated["design"].get("spatial_fit")},
    )


def _tool_develop_bench_station(args: DevelopBenchStationArgs, ctx: ToolContext) -> ToolResult:
    apply_fn = api_hook("apply_update", apply_update)
    previous = ctx.project
    if (
        previous.get("workflow", {}).get("kind") == "liquid_preparation"
        and (previous.get("engineering_design") or previous.get("cold_transfer_extension"))
    ):
        return _fail(
            "develop_bench_station",
            "verifying",
            "Start a new project for a bench station so the liquid machine is preserved.",
            recoverable=False,
        )

    if len(set(args.operations)) != len(args.operations):
        return _fail(
            "develop_bench_station",
            "verifying",
            "Select each station operation at most once in the ordered sequence.",
        )

    candidate = deepcopy(previous)
    if candidate.get("workflow", {}).get("kind") != "general":
        candidate = apply_fn(candidate, {"workflow_kind": "general"})

    summary = candidate.get("summary") or {}
    try:
        proposal = StationProposal(schema_version="1", operations=list(args.operations))
        station = compile_station(
            proposal,
            workspace_width_mm=summary.get("workspace_width_mm"),
            workspace_depth_mm=summary.get("workspace_depth_mm"),
            workspace_height_mm=summary.get("workspace_height_mm"),
        )
    except ValueError as exc:
        return _fail("develop_bench_station", "verifying", str(exc))

    if not candidate["brief"].get("process"):
        candidate["brief"]["process"] = " -> ".join(m["label"] for m in station["modules"])
    candidate["bench_station"] = station
    candidate["revision"] = max(previous["revision"] + 1, candidate["revision"])
    candidate["status"] = "draft"
    candidate["workflow"] = describe_workflow(candidate)
    refresh_spatial_fit(candidate)
    run = {
        "kind": "develop_bench_station",
        "status": "accepted",
        "message": "The bench station layout, parts list, and operating steps are ready to review.",
    }
    ctx.project = candidate
    ctx.mutated = True
    ctx.action_runs.append(run)
    return ToolResult(
        tool="develop_bench_station",
        ok=True,
        stage="designing",
        mutated=True,
        summary=(
            f"Compiled bench station with {station['proposal']['operations']} "
            f"(footprint {station['footprint_mm'][0]:g} x {station['footprint_mm'][1]:g} mm)."
        ),
        data={
            "operations": station["proposal"]["operations"],
            "footprint_mm": station["footprint_mm"],
            "spatial_fit": candidate["design"].get("spatial_fit"),
        },
        action_run=run,
    )


async def _tool_develop_custom_equipment(
    args: DevelopCustomEquipmentArgs, ctx: ToolContext
) -> ToolResult:
    from app.concept_agent import attach_concept, develop_concept

    apply_fn = api_hook("apply_update", apply_update)
    previous = ctx.project
    if (
        previous.get("bench_station")
        or previous.get("engineering_design")
        or state_for(previous).get("selected_platform_id")
        or previous.get("workflow", {}).get("kind") == "liquid_preparation"
    ):
        return _fail(
            "develop_custom_equipment",
            "verifying",
            "Start a new project for a custom assembly to keep this existing workstation.",
            recoverable=False,
        )

    patch: dict[str, Any] = {"workflow_kind": "general"}
    if not (previous["brief"].get("objective") or previous["brief"].get("process")):
        patch["brief_objective"] = args.instruction[:1000]
    candidate = apply_fn(previous, patch)
    develop_fn = api_hook("develop_concept", develop_concept)
    try:
        compiled, reply, calls = await develop_fn(
            candidate, args.instruction, history=ctx.history
        )
    except Exception as exc:
        if getattr(exc, "failure_kind", None) == "concept_validation":
            return _fail(
                "develop_custom_equipment",
                "repairing",
                str(exc),
                diagnostics=[str(exc)],
            )
        raise

    if compiled is None:
        if not previous.get("concept_design") and candidate != previous:
            ctx.project = candidate
            ctx.mutated = True
        return ToolResult(
            tool="develop_custom_equipment",
            ok=True,
            stage="inspecting",
            mutated=ctx.mutated,
            requires_human_input=True,
            question=reply,
            summary="Need missing mating/process details before generating custom equipment.",
        )

    updated = attach_concept(previous, candidate, compiled)
    refresh_spatial_fit(updated)
    printable = evaluate_printable_fit(updated, ctx.lab_profile)
    run = {
        "kind": "develop_concept",
        "status": "accepted",
        "message": reply,
        "model_calls": calls,
    }
    ctx.project = updated
    ctx.mutated = True
    ctx.action_runs.append(run)
    return ToolResult(
        tool="develop_custom_equipment",
        ok=True,
        stage="designing",
        mutated=True,
        summary=reply,
        diagnostics=[c["detail"] for c in printable if not c["fits"]],
        data={
            "title": compiled["title"],
            "footprint_mm": compiled["footprint_mm"],
            "bounds_mm": compiled["bounds_mm"],
            "spatial_fit": updated["design"].get("spatial_fit"),
            "printable": printable,
        },
        action_run=run,
    )


def _tool_approve_machine(args: ApproveMachineArgs, ctx: ToolContext) -> ToolResult:
    from app.design_workflow import approve_machine, has_machine

    if not args.explicit_user_confirmation:
        return _fail(
            "approve_machine",
            "verifying",
            "Explicit user confirmation is required before approving the machine.",
        )
    previous = ctx.project
    if not has_machine(previous):
        return _fail(
            "approve_machine",
            "verifying",
            "No machine or station design exists yet to approve.",
        )
    fit = previous.get("design", {}).get("spatial_fit") or evaluate_spatial_fit(previous)
    if fit.get("status") == "infeasible":
        return _fail(
            "approve_machine",
            "verifying",
            f"Cannot approve an infeasible 3D layout: {format_spatial_violations(fit)}",
        )
    try:
        updated = approve_machine(previous)
    except ValueError as exc:
        return _fail("approve_machine", "verifying", str(exc))
    ctx.project = updated
    ctx.mutated = updated != previous
    return ToolResult(
        tool="approve_machine",
        ok=True,
        stage="verifying",
        mutated=ctx.mutated,
        summary="Machine design approved; software protocol planning is now unlocked.",
    )


def _tool_update_lab_profile(args: UpdateLabProfileArgs, ctx: ToolContext) -> ToolResult:
    current = load_lab_profile()
    merged = deepcopy(current)
    if args.lab_name is not None:
        merged["lab_name"] = args.lab_name
    printer = dict(merged.get("printer") or {})
    if args.printer_name is not None:
        printer["name"] = args.printer_name
    if args.printer_build_volume_mm is not None:
        printer["build_volume_mm"] = [float(v) for v in args.printer_build_volume_mm]
    merged["printer"] = printer

    bench = dict(merged.get("bench") or {})
    if args.bench_width_mm is not None:
        bench["width_mm"] = float(args.bench_width_mm)
    if args.bench_depth_mm is not None:
        bench["depth_mm"] = float(args.bench_depth_mm)
    if args.bench_height_mm is not None:
        bench["height_mm"] = float(args.bench_height_mm)
    merged["bench"] = bench

    if args.measuring_tools is not None:
        merged["measuring_tools"] = list(args.measuring_tools)
    if args.owned_equipment is not None:
        merged["owned_equipment"] = list(args.owned_equipment)

    try:
        validated = LabProfile.model_validate(merged)
        saved = save_lab_profile(validated)
    except ValueError as exc:
        return _fail("update_lab_profile", "verifying", str(exc))

    ctx.lab_profile = saved
    if args.apply_to_current_project:
        previous = ctx.project
        try:
            updated = apply_profile_to_project(previous, saved)
            refresh_spatial_fit(updated)
        except ValueError as exc:
            return _fail("update_lab_profile", "verifying", str(exc))
        ctx.project = updated
        if updated != previous:
            ctx.mutated = True
    return ToolResult(
        tool="update_lab_profile",
        ok=True,
        stage="designing",
        mutated=ctx.mutated,
        summary=(
            "Saved laboratory profile and applied bench limits to current project."
            if args.apply_to_current_project
            else "Saved laboratory profile."
        ),
        data={"lab_profile": lab_profile_prompt_context(saved) or saved},
    )


def _tool_configure_standalone_fixture(
    args: ConfigureStandaloneFixtureArgs, ctx: ToolContext
) -> ToolResult:
    from app.fixture_adapter import attach_standalone_fixture

    previous = ctx.project
    existing = previous.get("standalone_fixture")
    provided = args.model_dump(exclude_none=True)

    if args.object_diameter_mm is not None and args.object_shape is None:
        provided["object_shape"] = "circular"
    if existing:
        raw_spec = deepcopy(existing["spec"])
        if args.object_reference_id != raw_spec.get("object_reference_id"):
            for key in (
                "object_label",
                "object_shape",
                "object_length_mm",
                "object_width_mm",
                "object_diameter_mm",
                "object_height_mm",
                "dimension_provenance",
            ):
                raw_spec.pop(key, None)
        raw_spec.update(provided)
        if args.object_reference_id == "custom" and any(
            k in provided
            for k in (
                "object_length_mm",
                "object_width_mm",
                "object_diameter_mm",
                "object_height_mm",
            )
        ):
            raw_spec["dimension_provenance"] = "user_measured"
        if raw_spec.get("object_shape") == "rectangular":
            raw_spec["object_diameter_mm"] = None
        elif raw_spec.get("object_shape") == "circular":
            raw_spec["object_length_mm"] = None
            raw_spec["object_width_mm"] = None
    else:
        raw_spec = dict(provided)
        if "title" not in raw_spec:
            ref_title = provided.get("object_reference_id", "Custom").replace("_", " ").title()
            raw_spec["title"] = provided.get("object_label") or f"{ref_title} fixture"
        has_footprint = (
            args.object_diameter_mm is not None if provided.get("object_shape") == "circular" else
            args.object_length_mm is not None and args.object_width_mm is not None
        )
        if args.object_reference_id == "custom" and (
            not has_footprint or not args.object_label
        ):
            return ToolResult(
                tool="configure_standalone_fixture",
                ok=True,
                stage="inspecting",
                mutated=False,
                requires_human_input=True,
                question=(
                    "Please provide the object's name and measured resting footprint in mm "
                    "(length and width, or diameter), or pick a reference "
                    "(96-well plate, 90 mm Petri dish, 75x25 slide, or cuvette)."
                ),
                summary="Missing custom object dimensions for standalone fixture.",
            )

    try:
        updated = attach_standalone_fixture(previous, raw_spec, lab_profile=ctx.lab_profile)
    except ValueError as exc:
        return _fail(
            "configure_standalone_fixture",
            "repairing",
            str(exc),
            diagnostics=[str(exc)],
            recoverable="new general project" not in str(exc),
        )

    fixture = updated["standalone_fixture"]
    if not fixture["printer_fit_ok"]:
        failing = [
            f"{p['id']}: {p['printer_fit']['detail']}"
            for p in fixture["parts"]
            if p["printer_fit"].get("fits") is False
        ]
        return _fail(
            "configure_standalone_fixture",
            "repairing",
            "Printable part exceeds the configured printer build volume: " + "; ".join(failing),
            diagnostics=failing,
            recoverable=True,
        )

    run = {
        "kind": "configure_standalone_fixture",
        "status": "accepted",
        "message": (
            f"Configured {fixture['spec']['title']} "
            f"(pocket {fixture['derived']['pocket_length_mm']:g} x "
            f"{fixture['derived']['pocket_width_mm']:g} mm)."
        ),
    }
    ctx.project = updated
    ctx.mutated = True
    ctx.action_runs.append(run)
    first_cad = {}
    if not existing:
        first_cad = {"first_cad": {"revision": updated["revision"], "formats": ["STEP", "STL"]}}
        run["message"] += " STEP and STL are available in the fixture downloads."
        if any(part["printer_fit"]["fits"] is None for part in fixture["parts"]):
            run["message"] += " Printer fit is unverified."
    return ToolResult(
        tool="configure_standalone_fixture",
        ok=True,
        stage="designing",
        mutated=True,
        summary=run["message"],
        data={
            **first_cad,
            "spec": fixture["spec"],
            "derived": {
                "pocket_length_mm": fixture["derived"]["pocket_length_mm"],
                "pocket_width_mm": fixture["derived"]["pocket_width_mm"],
                "pocket_diameter_mm": fixture["derived"].get("pocket_diameter_mm"),
                "scale_reference_length_mm": fixture["derived"]["scale_reference_length_mm"],
                "tolerance_coupon": fixture["derived"]["tolerance_coupon"],
            },
            "parts": [
                {
                    "id": p["id"],
                    "size_mm": p["size_mm"],
                    "printer_fit": p["printer_fit"],
                }
                for p in fixture["parts"]
            ],
            "spatial_fit": updated["design"].get("spatial_fit"),
        },
        action_run=run,
    )


def _tool_record_build_log(args: RecordBuildLogArgs, ctx: ToolContext) -> ToolResult:
    from app.build_feedback import append_build_log

    payload = args.model_dump(exclude_none=True)
    try:
        updated, entry = append_build_log(ctx.project, payload)
    except ValueError as exc:
        return _fail("record_build_log", "verifying", str(exc), recoverable=False)

    run = {
        "kind": "record_build_log",
        "status": "accepted",
        "message": f"Recorded physical build log {entry['id']} ({entry['fit_result']}).",
    }
    ctx.project = updated
    ctx.mutated = True
    ctx.action_runs.append(run)
    return ToolResult(
        tool="record_build_log",
        ok=True,
        stage="inspecting",
        mutated=True,
        summary=run["message"],
        data={"log": entry, "build_feedback": updated.get("build_feedback")},
        action_run=run,
    )


def _tool_apply_build_feedback(args: ApplyBuildFeedbackArgs, ctx: ToolContext) -> ToolResult:
    from app.build_feedback import apply_build_log_revision

    logs = ctx.project.get("build_logs") or []
    log_id = args.log_id or (logs[-1]["id"] if logs else None)
    if not log_id:
        return _fail(
            "apply_build_feedback",
            "verifying",
            "No physical build log exists yet; call record_build_log first.",
        )

    try:
        updated, summary = apply_build_log_revision(
            ctx.project,
            log_id,
            clearance_delta_mm=args.clearance_delta_mm,
            fdm_undersize_delta_mm=args.fdm_undersize_delta_mm,
            lab_profile=ctx.lab_profile,
        )
    except ValueError as exc:
        return _fail("apply_build_feedback", "repairing", str(exc))

    run = {
        "kind": "apply_build_feedback",
        "status": "accepted",
        "message": (
            f"Revised fixture from {log_id} -> revision {summary['new_revision']}. "
            f"Reprint: {summary['parts_to_reprint']}; unchanged: {summary['parts_unchanged']}."
        ),
    }
    ctx.project = updated
    ctx.mutated = True
    ctx.action_runs.append(run)
    return ToolResult(
        tool="apply_build_feedback",
        ok=True,
        stage="repairing",
        mutated=True,
        summary=run["message"],
        data=summary,
        action_run=run,
    )


async def _tool_discover_open_hardware_projects(
    args: DiscoverOpenHardwareProjectsArgs, ctx: ToolContext
) -> ToolResult:
    from app.open_hardware_discovery import discover_open_hardware

    res = await discover_open_hardware(
        args.query,
        sources=list(args.sources) if args.sources else None,
        max_results=args.max_results or 6,
        require_cad=args.require_cad,
        commercial_use_ok=args.commercial_use_ok,
    )
    compact_candidates = [
        {
            "source_host": c["source_host"],
            "repo_full_name": c["repo_full_name"],
            "repo_url": c["repo_url"],
            "commit_sha": c["commit_sha"],
            "spdx_id": c["spdx_id"],
            "license_allowed": (c.get("license_gate") or {}).get("allowed"),
            "cad_files": (c.get("cad_files") or [])[:8],
            "license_files": (c.get("license_files") or [])[:4],
            "bom_files": (c.get("bom_files") or [])[:4],
            "assembly_files": (c.get("assembly_files") or [])[:4],
            "score": c.get("score"),
            "description": c.get("description", "")[:180],
        }
        for c in res.get("candidates") or []
    ]
    for candidate in compact_candidates:
        if source := design_routes.open_source(candidate):
            ctx.design_sources[source.key] = source.model_dump()
            candidate["design_source_id"] = source.key
    summary = (
        f"Discovered {len(compact_candidates)} open-source hardware candidate(s) for "
        f"'{args.query}'."
    )
    return ToolResult(
        tool="discover_open_hardware_projects",
        ok=True,
        stage="researching",
        mutated=False,
        summary=summary,
        data={
            "query": args.query,
            "candidate_count": len(compact_candidates),
            "candidates": compact_candidates,
            "literature_references": (res.get("literature_references") or [])[:4],
            "source_warnings": res.get("source_warnings") or [],
        },
    )


async def _tool_ingest_open_hardware_project(
    args: IngestOpenHardwareProjectArgs, ctx: ToolContext
) -> ToolResult:
    from app.upstream_service import attach_upstream_project

    key = f"open:{args.source_host}:{args.repo_full_name}@{args.commit_sha}"
    saved = next((s for s in (ctx.project.get("design_plan") or {}).get("sources", [])
                  if s["key"] == key), {})
    try:
        updated = await attach_upstream_project(
            ctx.project,
            instance_id=args.instance_id, placement=args.placement,
            expected_sha256=args.expected_sha256,
            license_files=saved.get("license_files") or None,
            source_host=args.source_host,
            repo_full_name=args.repo_full_name,
            commit_sha=args.commit_sha,
            cad_file_path=args.cad_file_path,
            allow_non_commercial=args.allow_non_commercial,
            allow_unknown_license=args.allow_unknown_license,
            source_units=args.source_units,
            source_up_axis=args.source_up_axis,
            lab_profile=ctx.lab_profile,
        )
    except PermissionError as exc:
        return ToolResult(
            tool="ingest_open_hardware_project",
            ok=False,
            recoverable=True,
            requires_human_input=True,
            stage="researching",
            summary=str(exc),
            diagnostics=[str(exc)],
        )
    except Exception as exc:
        return _fail("ingest_open_hardware_project", "researching", str(exc))

    up = selected_source(updated, args.instance_id)
    prov = up["provenance"]
    geom = up["geometry"]
    changed = updated is not ctx.project
    ctx.project = updated
    ctx.mutated |= changed
    msg = (
        f"Ingested {prov['repo_full_name']} ({prov['file_path']}, {prov['spdx_id']}, "
        f"size {geom['app_size_mm']} mm) at revision {updated['revision']}."
    )
    run = {"kind": "ingest_open_hardware_project", "status": "accepted", "message": msg}
    ctx.action_runs.append(run)
    return ToolResult(
        tool="ingest_open_hardware_project",
        ok=True,
        stage="researching",
        mutated=changed,
        summary=msg,
        data={
            "provenance": prov,
            "license_gate": up["license_gate"],
            "geometry": {
                "cad_kind": geom.get("cad_kind"),
                "is_valid": geom.get("is_valid"),
                "app_size_mm": geom.get("app_size_mm"),
                "hole_count": len(geom.get("hole_features") or []),
                "parametric_parameters": geom.get("parametric_parameters", [])[:20],
            },
            "bom_count": len(up.get("bom") or []),
            "software_stacks": (up.get("software") or {}).get("detected_stacks") or [],
        },
        action_run=run,
    )


async def _tool_adapt_open_hardware_project(
    args: AdaptOpenHardwareProjectArgs, ctx: ToolContext
) -> ToolResult:
    from app.upstream_service import apply_upstream_adaptation

    if not args.instance_id and not ctx.project.get("upstream_project"):
        return _fail(
            "adapt_open_hardware_project",
            "assembling",
            "Ingest an open-source hardware project before adapting it.",
        )

    mating_spec = None
    overrides = None
    if args.mode == "mating":
        named_source = (ctx.project.get("cad_instances") or {}).get(args.instance_id) or {}
        previous = (named_source.get("adaptation") or {}).get("spec") or {}
        kind = args.mating_kind or previous.get("kind")
        if not kind:
            return _fail(
                "adapt_open_hardware_project",
                "assembling",
                "mating_kind is required when mode='mating'.",
            )
        spec_fields = {
            "kind": kind,
            "placement": args.placement,
            "clearance_mm": args.clearance_mm,
            "plate_thickness_mm": args.plate_thickness_mm,
            "riser_height_mm": args.riser_height_mm,
            "wall_thickness_mm": args.wall_thickness_mm,
            "flange_margin_mm": args.flange_margin_mm,
            "hole_diameter_mm": args.hole_diameter_mm,
            "secondary_hole_pitch_x_mm": args.secondary_hole_pitch_x_mm,
            "secondary_hole_pitch_y_mm": args.secondary_hole_pitch_y_mm,
            "cutout_width_mm": args.cutout_width_mm,
            "cutout_depth_mm": args.cutout_depth_mm,
        }
        mating_spec = dict(previous) if kind == previous.get("kind") else {}
        mating_spec.update({k: v for k, v in spec_fields.items() if v is not None})
    else:
        if not args.param_overrides:
            return _fail(
                "adapt_open_hardware_project",
                "assembling",
                "param_overrides is required when mode='parametric'.",
            )
        overrides = [item.model_dump() for item in args.param_overrides]

    try:
        updated = await apply_upstream_adaptation(
            ctx.project,
            instance_id=args.instance_id, mode=args.mode,
            mating_spec=mating_spec,
            param_overrides=overrides,
            lab_profile=ctx.lab_profile,
        )
    except Exception as exc:
        return _fail("adapt_open_hardware_project", "assembling", str(exc))

    source = selected_source(updated, args.instance_id)
    adapt = source["adaptation"]
    changed = updated is not ctx.project
    ctx.project = updated
    ctx.mutated |= changed
    inter = adapt["interference"]
    pfit = adapt.get("printer_fit")
    msg = (
        f"Generated {adapt['mode']} adaptation '{adapt['part_id']}' "
        f"(interference_passed={inter['passed']}, "
        f"min_clearance={inter.get('min_clearance_mm')} mm) "
        f"at revision {updated['revision']}."
    )
    impact = None
    change = updated.get("mating_change") or {}
    if args.instance_id and change.get("instance") == args.instance_id:
        from app.mixed_revision import change_impact
        from app.project_assembly import physical_assembly

        impact = change_impact(updated, physical_assembly(updated))
        msg += f" Reprint the adapter for {args.instance_id} and recalibrate its mounting datum."
        if impact["rest_pose_overlap_review"]:
            msg += " Check the reported rest-pose envelope overlap before fabrication."
    run = {"kind": "adapt_open_hardware_project", "status": "accepted", "message": msg}
    ctx.action_runs.append(run)
    return ToolResult(
        tool="adapt_open_hardware_project",
        ok=True,
        stage="assembling",
        mutated=changed,
        summary=msg,
        data={
            "mode": adapt["mode"],
            "part_id": adapt["part_id"],
            "mating_change": impact,
            "interference": inter,
            "printer_fit": pfit,
            "parameter_diffs": adapt.get("parameter_diffs") or [],
            "custom_geometry": adapt.get("custom_geometry"),
        },
        action_run=run,
    )


def _tool_inspect_upstream_project(
    args: InspectUpstreamProjectArgs, ctx: ToolContext
) -> ToolResult:
    up = selected_source(ctx.project, args.instance_id)
    if not isinstance(up, dict):
        return _fail(
            "inspect_upstream_project",
            "inspecting",
            "No upstream open-source project is currently loaded.",
        )
    if args.section == "all":
        data = {
            "provenance": up.get("provenance"),
            "license_gate": up.get("license_gate"),
            "geometry": up.get("geometry"),
            "bom_count": len(up.get("bom") or []),
            "assembly": up.get("assembly"),
            "software": up.get("software"),
            "adaptation": up.get("adaptation"),
        }
    elif args.section == "license":
        data = {"provenance": up.get("provenance"), "license_gate": up.get("license_gate")}
    else:
        data = {args.section: up.get(args.section)}
    return ToolResult(
        tool="inspect_upstream_project",
        ok=True,
        stage="inspecting",
        mutated=False,
        summary=f"Inspected upstream section '{args.section}'.",
        data=data,
    )


def _tool_set_line_budget(args: SetLineBudgetArgs, ctx: ToolContext) -> ToolResult:
    from app.bom_engine import build_complete_bom

    name = "set_line_budget"
    lines = build_complete_bom(ctx.project, procurement_report=ctx.procurement_report)["lines"]
    line = next((row for row in lines if row["line_id"] == args.line_id), None)
    if line is None:
        return _fail(name, "designing", "Choose a current BOM line.")
    if args.budget and (
        args.budget.user_quote not in (ctx.user_message or "")
        or line["cost_category"] != "unpriced"
    ):
        return _fail(
            name, "designing", "Use an unpriced line and the user's exact budget declaration."
        )
    record = (
        {**args.budget.model_dump(), "scope_signature": line["budget_scope_signature"]}
        if args.budget
        else None
    )
    previous = (ctx.project.get("budget_allowances") or {}).get(args.line_id)
    changed = previous != record
    if changed:
        from app.project_storage import make_snapshot

        project = deepcopy(ctx.project)
        records = project.setdefault("budget_allowances", {})
        if record:
            records[args.line_id] = record
        else:
            records.pop(args.line_id, None)
        project["revision"] += 1
        make_snapshot(project, [])
        ctx.project = project
        ctx.mutated = True
    return ToolResult(
        tool=name,
        ok=True,
        stage="designing",
        mutated=changed,
        summary="Updated the planning budget for this line.",
        data={"line_id": args.line_id, "budget": record},
    )


def _tool_choose_requirement_part(args: ChoosePartArgs, ctx: ToolContext) -> ToolResult:
    from app.bom_engine import build_complete_bom, load_purchasable_catalog
    from app.design_workflow import machine_signature, refresh_workflow

    def current_line(project):
        bom = build_complete_bom(project, procurement_report=ctx.procurement_report)
        return next(
            (r for r in bom["lines"] if r["line_id"] == args.line_id and r.get("requirement")), None
        )

    name = "choose_requirement_part"
    line = current_line(ctx.project)
    if not line:
        return _fail(
            name, "designing", "Use a current requirement line_id from the purchasing list."
        )
    previous = ctx.project.get("part_choices", {}).get(args.line_id)
    try:
        hits = ctx.supplier_hits
        plan = ctx.project.get("design_plan") or {}
        selected = (plan.get("decision") or {}).get("key")
        option = next((o for o in plan.get("options", []) if o["key"] == selected), {})
        for source in plan.get("sources", []):
            identity = source.get("supplier_identity")
            if identity and source["key"] in option.get("source_ids", []) and not any(
                (h.get("supplier"), h.get("sku")) == (identity["issuer"], identity["part_number"])
                for h in hits
            ):
                hits = [*hits, {"supplier": identity["issuer"], "sku": identity["part_number"],
                                "source_url": identity["source_url"], "mpn": identity["mpn"],
                                "fetched_at": identity["source_observed_at"],
                                "evidence_sha256": identity["evidence_sha256"],
                                "pack_size": 1 if identity["unit"] == "unit" else None}]
        record = update_choice(args, line, previous, load_purchasable_catalog()["items"],
                               hits, ctx.user_message).model_dump()
    except ValueError as exc:
        return _fail(name, "designing", str(exc))
    changed = record != previous and bool(previous or record["candidates"] or
                                          record["required_specs"] or line.get("catalog_id"))
    if changed:
        updated = deepcopy(ctx.project)
        updated.setdefault("part_choices", {})[args.line_id] = record
        updated["revision"] += 1
        if updated.get("machine_approval") and machine_signature(ctx.project) != machine_signature(
            updated
        ):
            updated.pop("machine_approval", None)
            updated["status"] = "draft"
        from app.project_storage import make_snapshot

        make_snapshot(updated, [])
        ctx.project = refresh_workflow(updated)
        ctx.mutated = True
        line = current_line(updated)
    msg = {
        "specify": "Recorded required interface values and rechecked existing choices.",
        "select": "Recorded the selected part and purchase quantity.",
        "clear": "Cleared the part selection.",
        "propose": "Recorded candidate and interface checks.",
    }
    return ToolResult(tool=name, ok=True, stage="designing", mutated=changed,
                      summary=msg[args.action], data={"line": line})


async def _tool_generate_purchasable_bom(
    args: GeneratePurchasableBomArgs, ctx: ToolContext
) -> ToolResult:
    import asyncio

    from app.bom_engine import build_complete_bom, purchasing_route
    from app.hardware_compatibility import evaluate_hardware_compatibility
    from app.supplier_apis import (
        SupplierApiError,
        cached_fx_rates,
        fetch_fx_rates,
        fetch_official_quote,
        supplier_api_capabilities,
    )
    from app.supplier_catalogs import PRODUCTS

    fx_rates = None
    warnings: list[str] = []
    if args.currency != "USD" and purchasing_route(ctx.project) != "unselected":
        fx_rates = cached_fx_rates("USD")
        if fx_rates is None:
            try:
                fx_rates = await fetch_fx_rates("USD")
            except SupplierApiError as exc:
                return _fail("generate_purchasable_bom", "verifying", str(exc))

    live_quotes: dict[str, dict[str, Any]] = {}
    if args.refresh_live_prices:
        base_bom = build_complete_bom(
            ctx.project,
            procurement_report=ctx.procurement_report,
            lab_profile=ctx.lab_profile,
            currency="USD",
        )
        caps = supplier_api_capabilities()
        dk_or_ms = caps["digikey_v4"]["configured"] or caps["mouser_v2"]["configured"]
        sem = asyncio.Semaphore(4)

        async def _pull(line: dict[str, Any]) -> None:
            cid = line.get("offer_id") or line.get("catalog_id")
            if (
                not cid
                or line.get("kind") != "commercial_part"
                or line.get("purchase_quantity") == 0
            ):
                return
            is_shopify = cid in PRODUCTS and PRODUCTS[cid].adapter == "shopify"
            has_pn = bool(line.get("digikey_pn") or line.get("mouser_pn"))
            if not is_shopify and not (dk_or_ms and has_pn) and cid not in PRODUCTS:
                return
            async with sem:
                try:
                    live_quotes[str(cid)] = await fetch_official_quote(
                        {
                            "catalog_id": cid,
                            "option_id": cid,
                            "mpn": line.get("mpn") if line.get("catalog_id") else None,
                            "digikey_pn": line.get("digikey_pn"),
                            "mouser_pn": line.get("mouser_pn"),
                            "sku": line.get("sku"),
                        },
                        currency=args.currency,
                        fx_rates=fx_rates,
                    )
                except SupplierApiError as exc:
                    warnings.append(f"{cid}: {exc}")

        await asyncio.gather(*(_pull(line) for line in base_bom.get("lines") or []))

    bom = build_complete_bom(
        ctx.project,
        procurement_report=ctx.procurement_report,
        lab_profile=ctx.lab_profile,
        currency=args.currency,
        fx_rates=fx_rates,
        live_quotes=live_quotes,
    )
    compat = evaluate_hardware_compatibility(ctx.project, bom, lab_profile=ctx.lab_profile)
    s = bom["summary"]
    currency = bom["currency"]
    documented = s["documented_subtotal"]
    if bom["status"] == "design_required":
        msg = "Select or develop a hardware design to generate its parts list."
    elif documented is None:
        msg = "No current price is available for the listed requirements."
    else:
        msg = (f"Documented prices subtotal {documented:,.2f} {currency} across "
               f"{s['documented_count']} of {s['line_count']} lines. "
               f"{s['missing_price_count']} lines need a purchase price.")
    if s["planning_count"]:
        low, high = s["planning_low"], s["planning_high"]
        allowance = f"from {low:,.2f}" if high is None else f"{low:,.2f}–{high:,.2f}"
        msg += f" Separate planning allowances {allowance} {currency}."
    if s["printed_line_count"]:
        filament = s["printed_material_subtotal"]
        msg += f" Filament estimate for {s['printed_line_count']} lines {filament:,.2f} {currency}."
    if s["unavailable_count"] or s["stale_offer_count"]:
        msg += (f" Supplier offers unavailable: {s['unavailable_count']}. "
                f"Observations awaiting a new check: {s['stale_offer_count']}.")
    run = {"kind": "generate_purchasable_bom", "status": "accepted", "message": msg}
    ctx.action_runs.append(run)
    return ToolResult(
        tool="generate_purchasable_bom",
        ok=True,
        stage="verifying",
        mutated=False,
        summary=msg,
        data={
            "status": bom["status"],
            "currency": bom["currency"],
            "fx_rate_used": bom.get("fx_rate_used"),
            "fx_source": bom.get("fx_source"),
            "summary": s,
            "shipping_and_tax_scope": bom["shipping_and_tax_scope"],
            "supplier_api_status": supplier_api_capabilities(),
            "live_quote_warnings": warnings[:6],
            "compatibility_summary": {
                "status": compat["status"],
                "error_count": compat["error_count"],
                "warning_count": compat["warning_count"],
                "issues": [c for c in compat["checks"] if c["severity"] != "pass"][:8],
            },
            "lines": [
                {
                    "line_id": ln["line_id"],
                    "label": ln["label"],
                    "resolution_status": ln["resolution_status"],
                    "supplier": ln["supplier"],
                    "mpn": ln["mpn"],
                    "qty": ln["quantity"],
                    "unit": ln["unit"],
                    "sku": ln["sku"],
                    "requirement": ln.get("requirement"),
                    "part_choice": ln.get("part_choice"),
                    "reference_candidate": ln.get("reference_candidate"),
                    "owned_quantity": ln.get("owned_quantity"),
                    "purchase_quantity": ln.get("purchase_quantity"),
                    "packs_to_order": ln["packs_to_order"],
                    "line_total": ln["line_total"],
                    "cost_category": ln["cost_category"],
                    "planning_basis": ln.get("planning_basis"),
                    "price_source": ln["price_source"],
                    "availability": ln["availability"],
                    "fetched_at": ln["fetched_at"],
                    "source_url": ln["source_url"],
                    "lead_time_days": ln["lead_time_days"],
                }
                for ln in bom["lines"][:40]
            ],
            "additional_requirements": [
                {"line_id": ln["line_id"], "label": ln["label"]}
                for ln in bom["lines"][40:]
                if ln.get("requirement")
            ],
        },
        action_run=run,
    )


def _tool_check_hardware_compatibility(
    args: CheckHardwareCompatibilityArgs, ctx: ToolContext
) -> ToolResult:
    from app.hardware_compatibility import evaluate_hardware_compatibility

    report = evaluate_hardware_compatibility(ctx.project, lab_profile=ctx.lab_profile)
    checks = (
        report["checks"]
        if args.include_passed_checks
        else [c for c in report["checks"] if c["severity"] != "pass"]
    )
    return ToolResult(
        tool="check_hardware_compatibility",
        ok=True,
        stage="verifying",
        mutated=False,
        summary=report["summary"],
        data={
            "status": report["status"],
            "error_count": report["error_count"],
            "warning_count": report["warning_count"],
            "pass_count": report["pass_count"],
            "checks": checks[:15],
            "scope_disclaimer": report["scope_disclaimer"],
        },
    )


async def _tool_search_supplier_components(
    args: SearchSupplierComponentsArgs, ctx: ToolContext
) -> ToolResult:
    from app.supplier_apis import search_official_suppliers

    res = await search_official_suppliers(
        args.query, currency=args.currency, max_results=args.max_results
    )
    ctx.supplier_hits.extend(res["live_api_results"])
    for hit in res["live_api_results"]:
        if source := design_routes.supplier_source(hit):
            ctx.design_sources[source.key] = source.model_dump()
            hit["design_source_id"] = source.key
    for hit in res["catalog_matches"]:
        hit["design_source_id"] = "catalog:" + hit["catalog_id"]
    return ToolResult(
        tool="search_supplier_components",
        ok=True,
        stage="researching",
        mutated=False,
        summary=(
            f"Supplier search '{args.query}': {len(res['live_api_results'])} live API match(es), "
            f"{len(res['catalog_matches'])} curated catalog match(es) ({res['currency']})."
        ),
        data=res,
    )


def _tool_generate_robot_protocol(args: GenerateRobotProtocolArgs, ctx: ToolContext) -> ToolResult:
    from app.opentrons_verifier import verify_jubilee_protocol, verify_opentrons_protocol
    from app.operation_plan import resolve_platform
    from app.protocol_generators import (
        extract_or_build_protocol_plan,
        generate_jubilee_protocol,
        generate_opentrons_protocol,
    )

    platform = resolve_platform(ctx.project, args.target_platform)
    if platform not in ("jubilee", "opentrons-ot2"):
        from app.operation_plan import project_operation_plan

        sequence = project_operation_plan(ctx.project)
        return ToolResult(
            tool="generate_robot_protocol",
            ok=True,
            stage="designing",
            mutated=False,
            summary="Reviewed the device bindings and manual operations for this design.",
            data={"operation_plan": sequence},
        )
    try:
        plan = extract_or_build_protocol_plan(
            ctx.project,
            stock_concentration=args.stock_concentration,
            target_concentrations=args.target_concentrations,
            final_volume=args.final_volume,
            allow_serial=args.allow_serial,
            allow_multi_dispense=args.allow_multi_dispense,
        )
        if platform == "jubilee":
            generated = generate_jubilee_protocol(
                ctx.project,
                protocol_plan=plan,
                multi_dispense=args.allow_multi_dispense,
                allow_serial=args.allow_serial,
            )
            sim = verify_jubilee_protocol(generated["source_code"])
        else:
            generated = generate_opentrons_protocol(
                ctx.project,
                protocol_plan=plan,
                multi_dispense=args.allow_multi_dispense,
                allow_serial=args.allow_serial,
            )
            sim = verify_opentrons_protocol(generated["source_code"])
    except ValueError as exc:
        return _fail("generate_robot_protocol", "verifying", str(exc))

    msg = (
        f"Generated {generated['filename']} ({platform}, mode={plan['mode']}, "
        f"{plan['target_count']} targets, {len(plan['intermediate_steps'])} serial intermediates, "
        f"tips={sim.get('tips_used', 0)}, simulation={'PASS' if sim['ok'] else 'FAIL'})."
    )
    run = {"kind": "generate_robot_protocol", "status": "accepted", "message": msg}
    ctx.action_runs.append(run)
    return ToolResult(
        tool="generate_robot_protocol",
        ok=True,
        stage="verifying",
        mutated=False,
        summary=msg,
        action_run=run,
        data={
            "platform": platform,
            "filename": generated["filename"],
            "operation_binding": {
                key: generated["operation_plan"][key]
                for key in ("scope", "revision", "sha256", "bindings")
            },
            "deck_slots": generated.get("deck_slots"),
            "pipettes": generated.get("pipettes"),
            "plan_summary": {
                "mode": plan["mode"],
                "target_count": plan["target_count"],
                "intermediate_count": len(plan["intermediate_steps"]),
                "intermediate_steps": plan["intermediate_steps"],
                "totals": {**plan["totals"], "compiled_tips": sim.get("tips_used")},
                "target_steps_sample": plan["steps"][:16],
            },
            "simulation": {
                "ok": sim["ok"],
                "verifier": sim["verifier"],
                "opentrons_cli_available": sim.get("opentrons_cli_available", False),
                "steps_executed": sim.get("steps_executed", 0),
                "tips_used": sim.get("tips_used", 0),
                "errors": sim.get("errors", []),
                "warnings": sim.get("warnings", []),
            },
            "source_preview_lines": generated["source_code"].splitlines()[:45],
        },
    )


def _tool_configure_measurements(args: MeasurementSteps, ctx: ToolContext) -> ToolResult:
    from app.measurement_program import configure
    from app.operation_plan import project_operation_plan

    updated = configure(ctx.project, args)
    changed = updated is not ctx.project
    if changed:
        ctx.project = updated
        ctx.mutated = True
    sequence = project_operation_plan(updated) if args.steps else None
    unavailable = [
        {"component_id": b["component_id"], **b["adapter"]}
        for b in (sequence or {}).get("bindings", {}).values()
        if b.get("adapter", {}).get("integration_requirements")
    ]
    return ToolResult(
        tool="configure_measurement_steps",
        ok=True,
        mutated=changed,
        stage="designing",
        summary=(
            "Saved the requested readings with unresolved device requirements."
            if unavailable
            else "Updated the recorded measurement sequence."
        ),
        data={
            "measurement_program": updated.get("measurement_program"),
            "integration_gaps": unavailable,
        },
    )


def _tool_simulate_robot_protocol(args: SimulateRobotProtocolArgs, ctx: ToolContext) -> ToolResult:
    from app.opentrons_verifier import verify_jubilee_protocol, verify_opentrons_protocol
    from app.operation_plan import resolve_platform
    from app.protocol_generators import generate_jubilee_protocol, generate_opentrons_protocol

    platform = resolve_platform(ctx.project, args.target_platform)
    if platform not in ("jubilee", "opentrons-ot2"):
        from app.operation_plan import simulate_sequence

        sim = simulate_sequence(ctx.project)
        return ToolResult(
            tool="simulate_robot_protocol",
            ok=sim["ok"],
            stage="verifying",
            mutated=False,
            summary="Reviewed the operating sequence offline. No observations collected.",
            data={"platform": platform, "simulation": sim},
        )
    try:
        generated = (
            generate_jubilee_protocol(ctx.project)
            if platform == "jubilee"
            else generate_opentrons_protocol(ctx.project)
        )
    except ValueError as exc:
        return _fail("simulate_robot_protocol", "verifying", str(exc))

    sim = (
        verify_jubilee_protocol(generated["source_code"])
        if platform == "jubilee"
        else verify_opentrons_protocol(generated["source_code"])
    )
    return ToolResult(
        tool="simulate_robot_protocol",
        ok=sim["ok"],
        stage="verifying",
        mutated=False,
        summary=(
            f"Simulated {generated['filename']} with {sim['verifier']}: "
            f"{'PASS' if sim['ok'] else 'FAIL'} ({sim.get('steps_executed', 0)} steps, "
            f"{sim.get('tips_used', 0)} tips)."
        ),
        data={"platform": platform, "filename": generated["filename"], "simulation": sim},
    )


def _tool_build_hardware_commissioning_pack(
    args: BuildHardwareCommissioningPackArgs, ctx: ToolContext
) -> ToolResult:
    from app.hardware_gateway import build_gateway_commissioning_checklist

    pack = build_gateway_commissioning_checklist(
        ctx.project,
        driver_kind=args.driver_kind,
        watchdog_timeout_s=float(args.watchdog_timeout_s),
    )
    return ToolResult(
        tool="build_hardware_commissioning_pack",
        ok=True,
        stage="verifying",
        mutated=False,
        summary=(
            f"Prepared hardware gateway config template (driver={args.driver_kind}, "
            f"watchdog={args.watchdog_timeout_s:g}s, commissioning_reviewed=false) with "
            f"{len(pack['checklist'])} mandatory pre-arm checks."
        ),
        data=pack,
    )


_DISPATCH: dict[str, tuple[type[BaseModel], Any, bool]] = {
    "load_tools": (LoadToolsArgs, _tool_load_tools, False),
    "inspect_project_state": (InspectProjectStateArgs, _tool_inspect_project_state, False),
    "check_spatial_fit": (CheckSpatialFitArgs, _tool_check_spatial_fit, False),
    "lookup_reviewed_references": (LookupReviewedReferencesArgs, _tool_lookup_references, False),
    "compare_design_routes": (design_routes.CompareRoutes, _tool_design_routes, True),
    "choose_design_route": (design_routes.ChooseRoute, _tool_design_routes, True),
    "update_project_brief": (UpdateProjectBriefArgs, _tool_update_project_brief, False),
    "update_liquid_recipe": (UpdateLiquidRecipeArgs, _tool_update_liquid_recipe, False),
    "select_platform": (SelectPlatformArgs, _tool_select_platform, False),
    "develop_workstation_layout": (
        DevelopWorkstationLayoutArgs,
        _tool_develop_workstation_layout,
        True,
    ),
    "configure_labware": (ConfigureLabwareArgs, _tool_configure_labware, False),
    "manage_extension": (ManageExtensionArgs, _tool_manage_extension, True),
    "develop_bench_station": (DevelopBenchStationArgs, _tool_develop_bench_station, False),
    "develop_custom_equipment": (DevelopCustomEquipmentArgs, _tool_develop_custom_equipment, True),
    "configure_standalone_fixture": (
        ConfigureStandaloneFixtureArgs,
        _tool_configure_standalone_fixture,
        False,
    ),
    "record_build_log": (RecordBuildLogArgs, _tool_record_build_log, False),
    "apply_build_feedback": (ApplyBuildFeedbackArgs, _tool_apply_build_feedback, False),
    "approve_machine": (ApproveMachineArgs, _tool_approve_machine, False),
    "update_lab_profile": (UpdateLabProfileArgs, _tool_update_lab_profile, False),
    "discover_open_hardware_projects": (
        DiscoverOpenHardwareProjectsArgs,
        _tool_discover_open_hardware_projects,
        True,
    ),
    "ingest_open_hardware_project": (
        IngestOpenHardwareProjectArgs,
        _tool_ingest_open_hardware_project,
        True,
    ),
    "adapt_open_hardware_project": (
        AdaptOpenHardwareProjectArgs,
        _tool_adapt_open_hardware_project,
        True,
    ),
    "inspect_upstream_project": (
        InspectUpstreamProjectArgs,
        _tool_inspect_upstream_project,
        False,
    ),
    "generate_purchasable_bom": (
        GeneratePurchasableBomArgs,
        _tool_generate_purchasable_bom,
        True,
    ),
    "choose_requirement_part": (ChoosePartArgs, _tool_choose_requirement_part, False),
    "set_line_budget": (SetLineBudgetArgs, _tool_set_line_budget, False),
    "check_hardware_compatibility": (
        CheckHardwareCompatibilityArgs,
        _tool_check_hardware_compatibility,
        False,
    ),
    "search_supplier_components": (
        SearchSupplierComponentsArgs,
        _tool_search_supplier_components,
        True,
    ),
    "generate_robot_protocol": (
        GenerateRobotProtocolArgs,
        _tool_generate_robot_protocol,
        False,
    ),
    "configure_measurement_steps": (MeasurementSteps, _tool_configure_measurements, False),
    "simulate_robot_protocol": (
        SimulateRobotProtocolArgs,
        _tool_simulate_robot_protocol,
        False,
    ),
    "build_hardware_commissioning_pack": (
        BuildHardwareCommissioningPackArgs,
        _tool_build_hardware_commissioning_pack,
        False,
    ),
}


async def execute_tool(name: str, arguments: dict[str, Any], ctx: ToolContext) -> ToolResult:
    """Validate arguments strictly and execute the requested domain tool atomically."""
    entry = _DISPATCH.get(name)
    if entry is None:
        return _fail(name, "verifying", f"Unknown tool: {name!r}.")
    model_cls, handler, is_async = entry
    try:
        parsed = model_cls.model_validate(arguments or {})
    except ValidationError as exc:
        issues = _format_validation_error(exc)
        return _fail(
            name,
            "verifying",
            "Invalid tool arguments: " + "; ".join(issues),
            diagnostics=issues,
        )

    snapshot = deepcopy(ctx.project)
    was_mutated = ctx.mutated
    try:
        decision = design_routes.pending_adoption(name, parsed, ctx.project)
        if decision:
            ctx.project = design_routes.stage_adoption(name, parsed, ctx.project)
        result = await handler(parsed, ctx) if is_async else handler(parsed, ctx)
        if decision and (not result.ok or result.requires_human_input):
            ctx.project, ctx.mutated = snapshot, was_mutated
            result.mutated = False
        elif decision:
            if ctx.project["revision"] == snapshot["revision"]:
                ctx.project["revision"] += 1
            plan = ctx.project.setdefault("design_plan", deepcopy(snapshot["design_plan"]))
            plan["pending_workspace"] = None
            plan["brief_signature"] = design_routes.brief_signature(ctx.project)
            plan["decision"] = {**decision, "applied_revision": ctx.project["revision"]}
            ctx.mutated = result.mutated = True
            result.data["applied_design_route"] = plan["decision"]
        return result
    except Exception as exc:
        ctx.project, ctx.mutated = snapshot, was_mutated
        LOGGER.warning("Tool %s rolled back after error=%s", name, type(exc).__name__)
        if re.search(r"API key|OpenAI|timeout|reach OpenAI", str(exc), re.I):
            raise
        return _fail(name, "repairing", str(exc), diagnostics=[str(exc)])
