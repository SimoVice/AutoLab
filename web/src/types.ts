export type GeometryPrimitive = {
  id: string;
  kind: 'box' | 'cylinder' | 'frustum' | 'tube' | 'plate';
  position: [number, number, number];
  size: [number, number, number];
  appearance: string;
  axis?: 'x' | 'y' | 'z';
  rotation_y_deg?: number;
  radius_top_mm?: number;
  radius_bottom_mm?: number;
  inner_radius_mm?: number;
  holes?: { x_mm: number; z_mm: number; radius_mm: number }[];
};

export type ComponentGeometry = {
  schema_version: '1';
  primitives: GeometryPrimitive[];
};

export type Component = {
  id: string;
  kind: string;
  label: string;
  position: [number, number, number];
  size: [number, number, number];
  geometry?: ComponentGeometry;
  reference_geometry?: Record<string, unknown>;
  foundation_asset_id?: string;
};

export type DesignReference = {
  id: string; title: string; kind: string; publisher: string; url: string;
  revision: string; license: string; license_url: string | null; checked_on: string;
  body_size_app_mm?: [number, number, number]; facts: string[]; use: string; limits: string;
};
export type SourceBasis = {
  schema_version: string; checked_on: string; architecture: string; references: DesignReference[];
  bindings: { component_id: string; primitive_id?: string; basis: string; reference_id: string | null; detail: string }[];
};

export type InspectionFrame = {
  time_s: number;
  components: Component[];
  joint_angles_deg: { shoulder: number; elbow: number };
  camera_mm: [number, number, number];
  target_mm: [number, number, number];
};

export type InspectionExtension = {
  schema_version: '1';
  proposal: { schema_version: '1'; link_lengths_mm: [number, number]; mast_height_mm: number; rationale: string; assumptions: string[] };
  attachment: { parent_component_id: string; anchor_mm: [number, number, number]; concept_only: boolean; fasteners: string; loads: string };
  component_ids: string[];
  components: Component[];
  pose_frames: InspectionFrame[];
  bounds: { min_mm: [number, number, number]; max_mm: [number, number, number] };
  assembly_bounds: { min_mm: [number, number, number]; max_mm: [number, number, number] };
  provenance?: Record<string, unknown>;
  checks: { id: string; passed: boolean; detail: string }[];
  limitations: string[];
};

export type ColdTransferExtension = {
  schema_version: '1';
  kind: 'prepared_plate_cold_transfer';
  payload_component_id: string;
  payload_primitive_prefix: string;
  pickup_mm: [number, number, number];
  storage_mm: [number, number, number];
  transfer_height_mm: number;
  shelf_top_mm: number;
  lid_open_angle_deg: number;
  components: Component[];
  component_ids: string[];
  assembly_bounds: InspectionExtension['assembly_bounds'];
  checks: InspectionExtension['checks'];
  limitations: string[];
};

export type PlanStep = {
  id: string;
  source_role?: string;
  source_volume_ul?: number;
  target: string;
  stock_volume_ul: number;
  diluent_volume_ul: number;
  total_volume_ul: number;
};

export type OperationSequence = {
  schema_version: 1; project_id: string; revision: number; platform: string | null;
  scope: 'approved' | 'illustrative'; sha256: string; hardware_blockers: string[];
  bindings: Record<string, { component_id: string | null; capability: string; driver: string | null;
    unit: string | null; limits: Record<string, unknown>; prerequisites: string[];
    adapter?: { id: string; version: number | null; command: string; execution: string;
      supported_commands: string[]; missing_capability: string | null; integration_requirements: string[] } }>;
  operations: { id: string; binding: string; action: string; mode: 'driver' | 'manual' | 'missing_driver' | 'recorded_input';
    request_id: string | null; parameters: Record<string, string | number> }[];
};

export type PlanStatus = 'incomplete' | 'invalid' | 'ready';

export type PreparationPlan = {
  mode?: 'direct' | 'serial';
  status: PlanStatus;
  message: string;
  stock: string | null;
  final_volume_ul: number | null;
  steps: PlanStep[];
  assumptions: string[];
};

export type SourcingPriceKind = 'listed' | 'starting_at' | 'unpriced';

export type SourcingOption = {
  id: string;
  label: string;
  supplier: string;
  sku: string | null;
  unit_price: number | null;
  price_kind: SourcingPriceKind;
  source_url: string | null;
  checked_on: string | null;
  availability: string;
  pros: string[];
  cons: string[];
  compatibility_note: string;
  geometry?: {
    profile_id: string;
    effect: 'product_geometry' | 'supplier_only' | 'requires_dimensions' | 'printable_geometry';
    size_mm?: [number, number, number] | null;
    basis?: string; scope?: string; source_url?: string | null;
    source?: { source_url: string; scope?: string; revision?: string; checked_on?: string; sha256?: string; license?: string; license_url?: string };
    details?: { positions?: number; bore_diameter_mm?: number; first_center_xz_mm?: [number, number] };
    placement?: { status: 'fits' | 'requires_shift' | 'unavailable'; position_xz_mm?: [number, number];
      shift_xz_mm?: [number, number]; shift_mm?: number; message: string };
  };
};

export type TipRackOptions = {
  revision: number; component_id: 'tip-rack'; selected_profile_id: string | null;
  printer_available: 'unknown' | 'yes' | 'no'; supported: boolean;
  options: SourcingOption[];
};

export type TipRack = {
  schema_version: number; kind: 'commercial_reference' | 'rim_supported_tip_rack';
  profile_id: string; target_component_id?: string; size_mm: [number, number, number];
  geometry: ComponentGeometry; spec?: Record<string, unknown>; derived?: Record<string, unknown>;
  profile?: { display_name?: string; source?: { source_url?: string; scope?: string; revision?: string } };
  provenance?: Record<string, unknown>; checks?: Record<string, unknown> | DesignCheck[];
};

export type SourcingGroup = {
  id: string;
  label: string;
  component_ids: string[];
  quantity: number;
  selected_id: string;
  options: SourcingOption[];
};

export type Sourcing = {
  checked_on: string;
  currency: string;
  subtotal: number;
  is_lower_bound: boolean;
  incomplete: boolean;
  unpriced_count: number;
  groups: SourcingGroup[];
  exclusions: string[];
};

export type CoolingTargetSource = 'assumed' | 'user';

export type DesignCooling = {
  enabled: boolean;
  target_c: number;
  target_source: CoolingTargetSource;
  variant_id: string;
  transfer_mode: 'manual';
  validation: 'unverified';
  notes: string[];
};

export type WorkflowKind = 'unclassified' | 'general' | 'liquid_preparation';

export type WorkflowStatus = 'discovery' | 'planning' | 'supported_concept';

export type ProjectBrief = {
  objective: string | null;
  process: string | null;
  materials: string | null;
  equipment: string | null;
  constraints: string | null;
};

export type DesignPlan = {
  operations: { key: string; action: string; material?: string | null; performance?: string | null; interface?: string | null }[];
  options: {
    key: string; label: string; strategy: 'reuse' | 'buy' | 'fabricate'; benefit: string; tradeoff: string;
    coverage: { operation: string; capability: string }[]; source_ids: string[]; inventory_quote: string | null;
    measurements: { key: string; value: string; unit: string; quote: string }[];
  }[];
  sources: { key: string; label: string; url: string | null; detail: string }[];
  blockers: Record<string, string[]>;
  pending_workspace?: {
    workspace_width_mm?: number | null; workspace_depth_mm?: number | null;
    workspace_height_mm?: number | null; brief_constraints?: string | null;
  } | null;
  decision: { key: string; user_quote: string; applied_revision: number | null } | null;
};

export type ProjectWorkflow = {
  design_plan_needs_review?: boolean;
  kind: WorkflowKind;
  label: string;
  status: WorkflowStatus;
  design_available: boolean;
  parameters_started: boolean;
};

export type BenchStationOperation = 'weigh' | 'print_label' | 'read_barcode' | 'inspect';
export type BenchStationModule = {
  id: BenchStationOperation; label: string; position_mm: [number, number, number];
  size_mm: [number, number, number]; role: string; guidance: string;
  functional_surface_mm?: [number, number, number, number];
};
export type BenchStation = {
  proposal: { schema_version: '1'; operations: BenchStationOperation[] };
  footprint_mm: [number, number]; modules: BenchStationModule[];
  steps: string[]; limitations: string[];
  parts: { operation: BenchStationOperation; label: string; source_title: string | null; source_url: string | null; price_usd: number | null }[];
};

export type SpatialFitAxis = {
  body_mm: number;
  clearance_mm: number;
  required_mm: number;
  limit_mm: number | null;
  margin_mm: number | null;
  fits: boolean | null;
};

export type SpatialFitViolation = {
  axis: string;
  code: string;
  message: string;
  required_mm: number;
  limit_mm: number;
  overflow_mm: number;
};

export type CadVector = [number, number, number];
export type CadBounds = { cad_min_mm: CadVector; cad_max_mm: CadVector; app_size_mm: CadVector };
export type CadInstance = {
  name: string;
  provenance: UpstreamProjectState['provenance'];
  geometry: CadBounds & { detected_units: string };
  source_units: 'auto' | 'in' | 'mm';
  placement: { parent: string; datum: string; translation_mm: CadVector; rotation_deg_xyz: CadVector };
  adaptation?: { custom_geometry: CadBounds } | null;
};
export type CadPlacementCheck = {
  name: string; host: string; world_translation_mm: CadVector; world_rotation_matrix: CadVector[];
  access_status: 'clear' | 'review' | 'not_specified'; access_blockers: string[];
  mating_interference: { passed: boolean; interference_volume_mm3: number } | null;
  printer_fit: { fits: boolean; detail: string } | null;
};
export type SpatialFit = {
  cad_assembly?: {
    instances: CadPlacementCheck[]; possible_overlaps: [string, string][]; bounds_mm: [CadVector, CadVector];
    unplaced_parts: string[];
  };
  status: 'unconstrained' | 'fits' | 'infeasible';
  subject: string;
  axes: {
    width_mm: SpatialFitAxis;
    depth_mm: SpatialFitAxis;
    height_mm: SpatialFitAxis;
  };
  clearances: { axis: string; kind: string; mm: number; note: string }[];
  printable_checks: Record<string, unknown>[];
  violations: SpatialFitViolation[];
};

export type Project = {
  design_plan?: DesignPlan;
  project_notes?: string;
  design_workflow?: { phase: 'hardware_design' | 'software'; machine_approved: boolean; approval: { machine_signature: string; revision: number } | null; recipe_started: boolean };
  concept_design?: import('./ConceptWorkspace').ConceptDesign;
  id: string;
  lineage_id?: string;
  restored_from?: { project_id: string; revision: number; saved_at: string };
  revision: number;
  bench_station?: BenchStation | null;
  standalone_fixture?: StandaloneFixture | null;
  upstream_project?: UpstreamProjectState | null;
  cad_instances?: Record<string, CadInstance>;
  mating_change?: { instance: string; revision: number };
  build_logs?: BuildLogEntry[];
  build_feedback?: BuildFeedbackSummary | null;
  labware_brief?: LabwareBrief | null;
  labware_rack?: LabwareRack | null;
  labware_plate?: LabwarePlate | null;
  tip_rack?: TipRack | null;
  tip_rack_request?: { alternatives_requested?: boolean } | null;
  fabrication_capabilities?: { printer_available: 'unknown' | 'yes' | 'no'; source?: string | null };
  status: 'draft' | 'confirmed';
  brief: ProjectBrief;
  workflow: ProjectWorkflow;
  summary: {
    stock_concentration: string | null;
    target_concentrations: string | null;
    final_volume: string | null;
    equipment: string | null;
    workspace_width_mm: number | null;
    workspace_depth_mm: number | null;
    workspace_height_mm: number | null;
    cooling_enabled: boolean;
    cooling_target_c: number | null;
  };
  selections: Record<string, string>;
  sourcing: Sourcing;
  design: {
    footprint: { width_mm: number; depth_mm: number; height_mm: number };
    components: Component[];
    constraint_status: 'unconstrained' | 'fits' | 'infeasible';
    spatial_fit?: SpatialFit;
    estimated_cost: { amount: number | null; currency: string };
    cooling: DesignCooling;
    foundation?: { id: string; source_sha256?: string; bed_drop_mm: number; bbox_mm: [number, number, number] };
    assembly_bounds?: { min_mm: [number, number, number]; max_mm: [number, number, number] };
    platform?: { id: string; label: string; geometry_scope: string };
  };
  plan?: PreparationPlan | null;
  engineering_design?: EngineeringDesign | null;
  inspection_extension?: InspectionExtension | null;
  cold_transfer_extension?: ColdTransferExtension | null;
  design_basis?: SourceBasis;
  rebuild_notice?: string;
  engineering?: { schema_version: string; selected_platform_id: string | null;
    selection_source: string | null; requirements: Record<string, string | null>;
    options: { platform_id: string; reason: string; tradeoffs: string[] }[] };
  engineering_guidance?: { status: string; platform_id?: string; message: string;
    steps: { title: string; url: string; detail: string }[] };
};

export type RackSpec = {
  body_diameter_max_mm: number; rim_diameter_min_mm: number; cap_diameter_max_mm: number;
  below_rim_height_mm: number; count: number; columns: number;
  pitch_mm?: number; radial_clearance_mm?: number; max_width_mm?: number; max_depth_mm?: number; max_height_mm?: number;
};
export type RackFieldSource = { literal: string; value: number; recorded_at_revision: number };
export type LabwareBrief = {
  schema_version: number; kind: 'rim_supported_tube_rack'; target_component_id: string;
  spec: Partial<RackSpec>; sources: Record<string, RackFieldSource>; missing_fields: string[];
  status: 'needs_dimensions' | 'ready';
};
export type LabwareRack = {
  schema_version: number; kind: 'rim_supported_tube_rack'; target_component_id: string;
  spec: RackSpec; assumptions: { radial_clearance_mm: number; bore_diametral_tolerance_plus_minus_mm: number;
    minimum_rim_bearing_mm: number; top_thickness_mm: number; end_wall_mm: number;
    bottom_clearance_mm: number; process: string; tube_envelope: string; unverified: string[] };
  size_mm: [number, number, number]; geometry: ComponentGeometry;
  derived: { rows: number; columns: number; count: number; pitch_mm: number; bore_diameter_mm: number;
    bore_max_assumed_mm: number; rim_land_min_assumed_mm: number; tip_clearance_mm: number;
    top_thickness_mm: number; end_wall_mm: number; closed_cap_envelope_size_mm?: [number, number] };
  metrics: { body_radial_clearance_min_assumed_mm: number; rim_land_min_assumed_mm: number;
    closed_cap_gap_mm: number; hole_web_min_assumed_mm: number };
  checks: DesignCheck[];
  provenance: { basis: string; sources: Record<string, RackFieldSource>; compiler: string;
    source_project_revision: number; previous_size_mm?: [number, number, number] };
};

export type MicroplateWell = {
  label: string; x_mm: number; y_mm: number; z_mm: number;
  app_x_mm: number; app_y_mm: number; app_z_mm: number;
  diameter_mm: number; depth_mm: number; total_liquid_volume_ul: number;
};
export type LabwarePlate = {
  schema_version: number; kind: 'microplate_96'; profile_id: string; target_component_id: string;
  size_mm: [number, number, number]; geometry: ComponentGeometry;
  profile: { id: string; display_name: string; source_dimensions_mm: [number, number, number]; scope: string;
    source: { source_url: string; revision: string; sha256: string; checked_on: string; license: string; license_url: string; scope: string } };
  derived: { well_count: number; well_capacity_ul: number; well_ordering: string[]; wells: MicroplateWell[];
    source_to_app: string; holder_pocket_mm: [number, number]; holder_wall_mm: number; holder_floor_mm: number; holder_lip_mm: number };
  checks: DesignCheck[];
  compatibility: { status: 'ready' | 'volume_conflict' | 'needs_recipe'; blocking_issues: string[];
    well_capacity_ul: number; requested_volume_ul: number | null; current_target_count: number; occupied_wells: string[] };
  provenance: { selection_literal: string; compiler: string; source_project_revision: number };
};

export type ConversationEntry = { id: number; role: 'assistant' | 'user'; text: string; references?: DesignReference[] };

export type AssemblyStep = {
  id: string;
  title: string;
  component_ids: string[];
  instructions: string[];
  checks: string[];
  requires_design: boolean;
};

export type ExportFileEntry = {
  name: string;
  size: number;
  media_type: string;
  sha256: string;
};

export type ExportStatus = 'queued' | 'running' | 'ready' | 'failed' | 'stale';

export type ExportJob = {
  id: string;
  project_id: string;
  revision: number;
  status: ExportStatus;
  stage: string;
  completed_stages: number;
  total_stages: number;
  error: string | null;
  files: ExportFileEntry[];
  assembly_steps: AssemblyStep[];
  download_url: string | null;
};

export type DesignCheck = { id: string; label: string; passed: boolean; detail: string };

export type DesignUsage = { model?: string; input_tokens?: number; output_tokens?: number };

export type DesignAttempt = {
  number: number;
  proposal: Record<string, unknown> | null;
  checks: DesignCheck[];
  usage: DesignUsage | null;
  error?: string;
};

export type DesignRun = {
  id: string;
  source_revision: number;
  status: 'accepted' | 'rejected' | 'failed';
  model: string;
  instruction: string;
  attempts: DesignAttempt[];
  message: string;
};

export type LayoutPlacement = { component_id: string; x_mm: number; z_mm: number };

export type LayoutProposal = {
  schema_version: '1';
  deck_width_mm: number;
  deck_depth_mm: number;
  bed_drop_mm?: number;
  slot_assignments?: { component_id: string; slot_id: string }[];
  placements: LayoutPlacement[];
  rationale: string;
  assumptions: string[];
};

export type EngineeringDesign = {
  schema_version: '1';
  proposal: LayoutProposal;
  provenance: Record<string, unknown>;
};

export type ProjectResponse = { project: Project; assistant_message?: string; recommendations?: { reference: DesignReference; reason: string }[] };

export type DesignResponse = { project: Project; design_run: DesignRun; assistant_message: string };

export type ConfigurationUpdate = {
  cooling_enabled?: boolean;
  cooling_target_c?: number;
  selections?: Record<string, string>;
};

export type ProcurementRow = {
  group_id: string; component_ids: string[]; selected_id: string; label: string;
  sku: string | null; quantity: number; pack_size: number | null; pack_count: number | null;
  unit_price: number | null; line_total: number | null; currency: 'USD'; pack_price?: number | null;
  last_known_line_total?: number | null; compatibility_note?: string; is_expired?: boolean;
  is_candidate?: boolean; parent_group_id?: string; source_bom_url?: string; source_bom_row?: string;
  source_project_revision?: string; variant_id?: string; variant_title?: string;
  status: 'priced' | 'unavailable' | 'missing' | 'quote_needed' | 'unsupported';
  freshness: 'fresh' | 'stale' | 'unverified'; availability: string;
  price_source_url: string | null; availability_source_url: string | null;
  fetched_at: string | null; last_known_price: number | null; last_known_fetched_at: string | null; issue: string | null;
};

export type ProcurementReport = {
  report_id: string; project_id: string; project_revision: number; market: 'US'; currency: 'USD';
  fetched_at: string; expires_at: string; rows: ProcurementRow[];
  coverage: { row_count: number; fresh_priced_count: number; stale_priced_count: number; unpriced_count: number };
  known_partial_subtotal: number; stale_last_known_subtotal: number; exclusions: string[];
};

export type ProcurementResponse = {
  report: ProcurementReport | null; state: 'not_refreshed' | 'stale_revision' | 'current';
};

export type InstructionSource = { id: string; title: string; url: string; checked_on: string; scope: string; revision?: string };
export type InstructionPart = {
  component_id: string; label: string; quantity: number; position_mm: [number, number, number]; size_mm: [number, number, number];
  slot_id: string | null; geometry_basis: string; reference_id: string | null;
  selected_variants: { group_id: string; variant_id: string; label: string; quantity: number; source_url: string | null }[];
};
export type InstructionStep = {
  id: string; title: string; status: 'ready' | 'reference_only' | 'needs_design'; component_ids: string[];
  parts: InstructionPart[]; instructions: string[]; prerequisites: string[]; checks: string[];
  required_tools: { label: string; source_id: string }[]; required_materials: { label: string; source_id: string }[];
  sources: InstructionSource[]; engineering_tasks: { id: string; action: string; acceptance_criteria: string | string[] }[];
};
export type InstructionGuide = {
  presentation_names?: Record<string, string>;
  schema_version: number; project_id: string; project_revision: number; revision: number;
  platform: string; status: 'review_ready' | 'needs_design'; procurement_report_id: string | null;
  parts: InstructionPart[]; steps: InstructionStep[]; sources: InstructionSource[];
  summary: { ready_steps: number; reference_steps: number; needs_design_steps: number; total_steps: number };
  coordinate_system: string;
  software_guidance?: SoftwareGuidance;
};

export type SoftwareGuidance = {
  project_id: string; project_revision: number; platform: string;
  status: 'needs_integration' | 'planning_only'; reusable_hardware: string; current_preparation: string;
  recommendations: { title: string; description: string }[];
  authoring_steps: string[]; limitations: string[]; sources: InstructionSource[];
  protocol_template: { status: 'needs_review' | 'needs_recipe'; execution_enabled: false;
    current_preparation: { target_count: number }; unresolved_parameters: string[] };
};

export type PrinterProfile = {
  name: string | null;
  build_volume_mm: [number, number, number] | null;
  nozzle_mm: number | null;
  materials: string[];
};

export type BenchEnvelope = {
  width_mm: number | null;
  depth_mm: number | null;
  height_mm: number | null;
  front_clearance_mm: number | null;
  rear_clearance_mm: number | null;
  side_clearance_mm: number | null;
  notes: string | null;
};

export type LabProfile = {
  schema_version: '1';
  lab_name: string | null;
  printer: PrinterProfile;
  bench: BenchEnvelope;
  measuring_tools: string[];
  owned_equipment: string[];
  notes: string | null;
  updated_at: string | null;
};

export type PrinterPreset = {
  id: string;
  name: string;
  build_volume_mm: [number, number, number];
  nozzle_mm: number;
  materials: string[];
};

export type DimensionMeasurement = {
  key: string;
  value_mm: number;
  provenance: 'reference' | 'user_measured';
  source?: string;
  measured_with?: string;
  recorded_at?: string;
};

export type FixturePrinterFit = {
  checked: boolean;
  fits: boolean | null;
  orientation: string;
  build_volume_mm?: number[] | null;
  printer_name?: string | null;
  detail: string;
};

export type FixturePart = {
  id: string;
  label: string;
  role: string;
  printable: boolean;
  size_mm: [number, number, number];
  position_mm: [number, number, number];
  print_orientation: string;
  supports_required: boolean;
  material: string;
  nozzle_mm: number;
  layer_height_mm: number;
  infill_pct: number;
  perimeters: number;
  estimated_filament_g: number;
  estimated_print_minutes: number;
  part_signature: string;
  printer_fit: FixturePrinterFit;
};

export type StandaloneFixture = {
  schema_version: '1';
  kind: 'standalone_fixture_v1';
  spec: {
    schema_version: '1' | '2';
    title: string;
    fixture_kind: 'locating_base' | 'sample_holder' | 'imaging_rig';
    object_reference_id: string;
    object_label: string;
    object_shape: 'rectangular' | 'circular';
    object_length_mm?: number | null;
    object_width_mm?: number | null;
    object_diameter_mm?: number | null;
    object_height_mm: number | null;
    dimension_provenance: 'reference' | 'user_measured';
    clearance_per_side_mm: number;
    fdm_undersize_mm: number;
    wall_thickness_mm: number;
    floor_thickness_mm: number;
    lip_height_mm: number;
    chamfer_mm: number;
    fillet_mm: number;
    fastener_spec: string;
    include_alignment_stops: boolean;
    include_finger_cutouts: boolean;
    include_scale_reference: boolean;
    include_camera_mount: boolean;
    camera_mount_type: string;
    camera_working_distance_mm: number;
    material: string;
    nozzle_mm: number;
    layer_height_mm: number;
  };
  derived: {
    pocket_length_mm: number;
    pocket_width_mm: number;
    pocket_diameter_mm?: number | null;
    total_allowance_mm: number;
    base_outer_width_mm: number;
    base_outer_depth_mm: number;
    base_height_mm: number;
    scale_reference_length_mm: number;
    tolerance_coupon: {
      span_mm: number;
      nominal_opening_mm: number;
      step_openings_mm: number[];
      offsets_per_side_mm: number[];
      scaled_to_fit_coupon: boolean;
      height_mm: number;
    };
  };
  parts: FixturePart[];
  part_signatures: Record<string, string>;
  components: Component[];
  footprint_mm: [number, number];
  height_mm: number;
  fasteners_bom: { item: string; quantity: number; purpose: string }[];
  printer_fit_ok: boolean;
  checks: DesignCheck[];
  limitations: string[];
};

export type BuildMeasuredFeature = {
  feature: string;
  axis: 'length' | 'width' | 'diameter' | 'height' | 'bore' | 'other';
  nominal_mm: number;
  measured_mm: number;
  error_mm?: number;
};

export type BuildLogEntry = {
  id: string;
  revision: number;
  recorded_at?: string;
  parts_printed: string[];
  printer_name: string;
  material: string;
  nozzle_mm: number;
  layer_height_mm: number;
  fit_result: 'pass' | 'too_tight' | 'too_loose' | 'warped' | 'failed_print';
  measured_features: BuildMeasuredFeature[];
  notes: string;
  part_signatures: Record<string, string>;
  e13_summary?: {
    trial_count: number;
    seated_pass_count: number;
    all_orientations_correct: boolean;
    max_displacement_mm: number | null;
    target_threshold_mm: number;
    passes_1mm_target: boolean;
  } | null;
  applied_revision?: number | null;
};

export type BuildFeedbackPartDiff = {
  id: string;
  label: string;
  previously_printed: boolean;
  reason: string;
};

export type BuildFeedbackSummary = {
  tested_revision: number | null;
  current_revision: number;
  latest_log_id: string | null;
  latest_fit_result: string | null;
  parts_to_reprint: BuildFeedbackPartDiff[];
  parts_unchanged: BuildFeedbackPartDiff[];
  has_untested_changes: boolean;
};

export type UpstreamBomItem = {
  id: string;
  name: string;
  quantity: number | string;
  sku?: string | null;
  supplier?: string | null;
  source_url?: string | null;
  notes?: string | null;
  source: 'upstream_repo' | 'autolab_custom_adaptation';
  source_file: string;
  compatibility_status: string;
};

export type UpstreamAssemblyStep = {
  order: number;
  text: string;
  source: 'upstream_repo' | 'autolab_custom_adaptation';
  source_file: string;
};

export type UpstreamProjectState = {
  schema_version: '1';
  title: string;
  description?: string;
  publication_references?: { title: string; doi?: string | null; url?: string | null }[];
  provenance: {
    source_host: string;
    repo_full_name: string;
    repo_url: string;
    commit_sha: string;
    file_path: string;
    raw_url: string;
    file_sha256: string;
    file_bytes: number;
    retrieved_at: string;
    spdx_id: string;
    license_category: string;
    license_url?: string | null;
    license_flags?: string[];
    commercial_ok?: boolean;
    copyleft?: boolean;
    authors?: string;
  };
  license_gate: {
    spdx_id: string;
    category: string;
    allowed: boolean;
    flags?: string[];
    blocking_reasons?: string[];
    summary: string;
  };
  geometry: {
    cad_kind: string;
    is_valid: boolean;
    watertight?: boolean | null;
    solid_count?: number;
    volume_mm3?: number | null;
    cad_size_mm: [number, number, number];
    app_size_mm: [number, number, number];
    hole_features?: { x_mm: number; y_mm: number; diameter_mm: number }[];
    parametric_parameters?: { name: string; default: unknown; type: string }[];
  };
  bom: UpstreamBomItem[];
  assembly: {
    upstream_repo?: string | null;
    commit_sha?: string | null;
    upstream_steps: UpstreamAssemblyStep[];
    custom_steps: UpstreamAssemblyStep[];
  };
  software: {
    detected_stacks: string[];
    entrypoints: { path: string; url: string }[];
    execution_status: string;
    disclaimer: string;
  };
  adaptation?: {
    mode: 'mating' | 'parametric';
    part_id: string;
    spec: Record<string, unknown>;
    custom_geometry?: {
      app_size_mm: [number, number, number];
      cad_size_mm: [number, number, number];
      volume_mm3?: number | null;
    } | null;
    combined_geometry?: {
      app_size_mm: [number, number, number];
      cad_size_mm: [number, number, number];
    } | null;
    interference: {
      passed: boolean;
      method: string;
      interference_volume_mm3: number;
      min_clearance_mm?: number | null;
    };
    printer_fit?: {
      fits: boolean;
      orientation: string;
      detail: string;
    } | null;
    parameter_diffs?: { name: string; original: unknown; updated: unknown }[];
    render_status?: string;
  } | null;
};

export type BomCurrency = 'USD' | 'CHF' | 'EUR';

export type PrintEstimate = {
  part_id: string;
  label: string;
  quantity: number;
  bounding_box_mm: [number, number, number];
  material: string;
  nozzle_mm: number;
  layer_height_mm: number;
  wall_loops: number;
  infill_percent: number;
  filament_mass_g: number;
  filament_length_m: number;
  print_time_minutes: number;
  material_cost_usd: number;
  material_cost: number;
  currency: BomCurrency;
  fits_printer_bed?: boolean | null;
  material_available_in_lab?: boolean | null;
  printer_fit_detail?: string | null;
};

export type PartCandidate = {
  issuer: string; part_number: string; label: string; source_url: string | null;
  source_observed_at?: string | null;
  basis: 'catalog' | 'supplier_search' | 'user_record'; evidence: string;
  interface_status: 'matched' | 'unverified' | 'mismatch';
  checks: { key: string; comparison: string; required: string; unit: string;
    actual: string | null; status: 'match' | 'mismatch' | 'unknown' }[];
};

export type PlanningBasis = {
  kind: string; low_usd: number; high_usd: number | null;
  detail: string; source_url: string | null; observed_on: string | null;
};

export type PurchasableBomLine = {
  line_id: string;
  group_id: string;
  catalog_id: string | null;
  label: string;
  category: string;
  kind: 'commercial_part' | '3d_printed' | 'requirement';
  resolution_status: 'catalog_identified' | 'fabricate' | 'selection_required' | 'user_selected';
  part_choice?: { candidates: PartCandidate[]; accepted: PartCandidate | null; decision: string | null };
  owned_quantity?: number; purchase_quantity?: number | null;
  requirement?: {
    name: string; quantity: string | number; unit: string;
    sku?: string; mpn?: string; part_number?: string; supplier?: string; source_file?: string;
    critical_specs?: { key: string; value: string; unit: string; comparison: string; evidence?: string }[];
  };
  component_ids: string[];
  supplier: string | null;
  sku: string | null;
  mpn: string | null;
  quantity: number | string;
  unit: string;
  pack_size: number | null;
  packs_to_order: number | null;
  currency: BomCurrency;
  unit_price: number | null;
  pack_price: number | null;
  line_total: number | null;
  planning_basis?: PlanningBasis;
  planning_low: number | null;
  planning_high: number | null;
  price_source: 'live_api' | 'curated_reference' | 'slicer_material_estimate' | 'unpriced' | 'unavailable' | 'stale_observation' | 'quote_required' | 'lab_inventory' | 'quantity_required' | 'included_in_base';
  availability: string;
  stock_quantity?: number | null;
  lead_time_days?: number | null;
  source_url?: string | null;
  fetched_at?: string | null;
  adapter?: string | null;
  notes?: string | null;
  print_estimate?: PrintEstimate | null;
};

export type PurchasableBom = {
  schema_version: number;
  generated_at: string;
  project_id: string;
  project_revision: number;
  route_kind: string;
  status: 'design_required' | 'incomplete' | 'itemized';
  currency: BomCurrency;
  fx_rate_used?: number | null;
  fx_source?: string | null;
  fx_rate_date?: string | null;
  catalog_checked_on: string;
  lines: PurchasableBomLine[];
  summary: {
    line_count: number;
    commercial_line_count: number;
    printed_line_count: number;
    live_api_priced_count: number;
    reference_priced_count: number;
    unpriced_placeholder_count: number;
    unresolved_count: number;
    quote_required_count: number;
    unavailable_count: number;
    stale_offer_count: number;
    live_api_subtotal: number;
    reference_catalog_subtotal: number;
    printed_material_subtotal: number;
    documented_subtotal: number | null;
    documented_count: number;
    missing_price_count: number;
    planning_count: number;
    planning_low: number | null;
    planning_high: number | null;
    total_estimated_cost: number | null;
    total_filament_mass_g: number;
    total_print_time_minutes: number;
    max_lead_time_days: number;
  };
  shipping_and_tax_scope: string;
  exclusions: string[];
};

export type CompatibilityCheck = {
  id: string;
  category: string;
  severity: 'pass' | 'warning' | 'error';
  passed: boolean;
  title: string;
  detail: string;
  recommended_fix?: string | null;
  affected_ids: string[];
};

export type HardwareCompatibilityReport = {
  schema_version: number;
  project_id: string;
  project_revision: number;
  status: 'compatible' | 'warnings' | 'incompatible' | 'not_evaluated';
  error_count: number;
  warning_count: number;
  pass_count: number;
  summary: string;
  checks: CompatibilityCheck[];
  scope_disclaimer: string;
};
