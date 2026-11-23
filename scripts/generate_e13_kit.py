"""Generate a verified candidate print kit for physical trial E13 (Phase 2.4)."""

from __future__ import annotations

import argparse
import hashlib
import json
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

from build123d import Align, Box, Pos, import_step  # noqa: E402

from app.domain import apply_update, new_project  # noqa: E402
from app.fixture_adapter import attach_standalone_fixture  # noqa: E402
from app.fixture_cad import build_fixture_cad_files  # noqa: E402
from app.fixture_drawings import (  # noqa: E402
    build_fixture_package_zip,
    render_bench_build_sheet_html,
    render_bench_build_sheet_md,
    render_e13_checklist_md,
    render_fixture_drawing_pdf,
    render_fixture_drawing_svg,
)
from app.lab_profile import PRINTER_PRESETS, default_lab_profile  # noqa: E402


def _bambu_mini_profile() -> dict:
    mini = next(p for p in PRINTER_PRESETS if p["id"] == "bambu-a1-mini")
    prof = default_lab_profile()
    prof["lab_name"] = "E13 Candidate Bench"
    prof["printer"] = {
        "name": mini["name"],
        "build_volume_mm": list(mini["build_volume_mm"]),
        "nozzle_mm": mini["nozzle_mm"],
        "materials": list(mini["materials"]),
    }
    return prof


def generate_e13_candidate_kit(out_dir: Path) -> dict:
    """Export, reimport-verify, and write the candidate E13 print kit to disk."""
    out_dir.mkdir(parents=True, exist_ok=True)
    profile = _bambu_mini_profile()
    base = apply_update(
        new_project(),
        {
            "workflow_kind": "general",
            "workspace_width_mm": 350.0,
            "workspace_depth_mm": 260.0,
            "workspace_height_mm": 300.0,
        },
    )
    project = attach_standalone_fixture(
        base,
        {
            "title": "E13 96-Well Plate Imaging Rig",
            "fixture_kind": "imaging_rig",
            "object_reference_id": "slas_96_well_plate",
            "fastener_spec": "m3_heatset_insert",
            "camera_mount_type": "pi_camera_v2_v3",
            "camera_working_distance_mm": 140.0,
            "material": "PLA",
            "nozzle_mm": 0.4,
            "layer_height_mm": 0.2,
        },
        lab_profile=profile,
    )
    fixture = project["standalone_fixture"]
    rev = int(project["revision"])

    files: dict[str, bytes] = {
        **build_fixture_cad_files(fixture),
        "drawing.svg": render_fixture_drawing_svg(fixture, rev),
        "drawing.pdf": render_fixture_drawing_pdf(fixture, rev),
        "build-sheet.html": render_bench_build_sheet_html(fixture, project, rev),
        "build-sheet.md": render_bench_build_sheet_md(fixture, rev),
        "e13-bench-checklist.md": render_e13_checklist_md(fixture, rev),
        "autolab-e13-candidate-package.zip": build_fixture_package_zip(project),
    }

    written = []
    for rel, content in sorted(files.items()):
        target = out_dir / rel
        target.parent.mkdir(parents=True, exist_ok=True)
        target.write_bytes(content)
        written.append(
            {
                "path": rel,
                "size_bytes": len(content),
                "sha256": hashlib.sha256(content).hexdigest(),
            }
        )

    step_checks = []
    for part in fixture["parts"]:
        pid = part["id"]
        solid = import_step(str(out_dir / "parts" / f"{pid}.step"))
        bb = solid.bounding_box().size
        # Compare CAD print-bed bbox [X, Y, Z] against [w, d, h] in part metadata.
        bed_w, bed_d, bed_h = round(float(bb.X), 2), round(float(bb.Y), 2), round(float(bb.Z), 2)
        fits_mini = (
            (bed_w <= 180.0 and bed_d <= 180.0 and bed_h <= 180.0)
            or (bed_d <= 180.0 and bed_w <= 180.0 and bed_h <= 180.0)
        )
        step_checks.append(
            {
                "part_id": pid,
                "is_valid": bool(solid.is_valid),
                "solids": len(solid.solids()),
                "cad_bbox_mm": [bed_w, bed_d, bed_h],
                "fits_bambu_a1_mini_180mm": fits_mini,
            }
        )

    base_solid = import_step(str(out_dir / "parts" / "locating-base.step"))
    gauge = Pos(0.0, 0.0, 3.5 + 2.0) * Box(
        127.76 + 0.6, 85.48 + 0.6, 3.8, align=(Align.CENTER,) * 3
    )
    overlap = base_solid & gauge
    pocket_clear_ok = overlap is None or float(overlap.volume) <= 1e-6

    asm = import_step(str(out_dir / "cad" / "assembly.step"))
    report = {
        "printer_target": "Bambu Lab A1 mini (180 x 180 x 180 mm)",
        "revision": rev,
        "pocket_mm": [
            fixture["derived"]["pocket_length_mm"],
            fixture["derived"]["pocket_width_mm"],
        ],
        "pocket_clear_ok": pocket_clear_ok,
        "assembly_solids": len(asm.solids()),
        "assembly_valid": bool(asm.is_valid),
        "step_checks": step_checks,
        "all_verified": (
            pocket_clear_ok
            and bool(asm.is_valid)
            and all(
                c["is_valid"] and c["solids"] == 1 and c["fits_bambu_a1_mini_180mm"]
                for c in step_checks
            )
        ),
        "files": written,
    }
    (out_dir / "verification-report.json").write_text(
        json.dumps(report, indent=2), encoding="utf-8"
    )
    return report


def main() -> int:
    parser = argparse.ArgumentParser(description="Generate candidate E13 print kit.")
    parser.add_argument(
        "--out",
        type=Path,
        default=ROOT / ".local" / "e13-candidate-kit-20261123",
        help="Destination folder for generated STL/STEP/PDF/checklist kit.",
    )
    args = parser.parse_args()
    report = generate_e13_candidate_kit(args.out)
    print(json.dumps(report, indent=2))
    return 0 if report["all_verified"] else 1


if __name__ == "__main__":
    raise SystemExit(main())
