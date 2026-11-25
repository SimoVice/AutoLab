"""Dimensioned 2D engineering drawings (SVG + vector PDF) and printable bench build sheets."""

from __future__ import annotations

import hashlib
import html
import io
import json
import zipfile
from typing import Any

from app.fixture_adapter import (
    COUPON_OFFSETS_MM,
    MAST_CROSS_SECTION_MM,
    SCALE_SHELF_DEPTH_MM,
)
from app.fixture_cad import build_fixture_cad_files


def _fmt(value: float | None, suffix: str = " mm") -> str:
    if value is None:
        return "—"
    return f"{float(value):.2f}{suffix}"


def _dimension_specs(fixture: dict[str, Any]) -> dict[str, Any]:
    spec = fixture["spec"]
    derived = fixture["derived"]
    outer_w = float(derived["base_outer_width_mm"])
    outer_d = float(derived["base_outer_depth_mm"])
    core_w = float(derived["core_width_mm"])
    core_d = float(derived["core_depth_mm"])
    base_h = float(derived["base_height_mm"])
    floor = float(spec["floor_thickness_mm"])
    lip = float(spec["lip_height_mm"])
    pocket_l = float(derived["pocket_length_mm"])
    pocket_w = float(derived["pocket_width_mm"])
    pocket_d = derived.get("pocket_diameter_mm")
    total_h = float(fixture["height_mm"])
    prov = "User measured" if spec["dimension_provenance"] == "user_measured" else "Nominal ref"
    return {
        "title": spec["title"],
        "kind": spec["fixture_kind"],
        "object_label": spec["object_label"],
        "object_shape": spec["object_shape"],
        "provenance_label": prov,
        "reference_id": spec["object_reference_id"],
        "outer_w": outer_w,
        "outer_d": outer_d,
        "core_w": core_w,
        "core_d": core_d,
        "base_h": base_h,
        "floor": floor,
        "lip": lip,
        "pocket_l": pocket_l,
        "pocket_w": pocket_w,
        "pocket_d": pocket_d,
        "total_h": total_h,
        "clearance": float(spec["clearance_per_side_mm"]),
        "undersize": float(spec["fdm_undersize_mm"]),
        "material": spec["material"],
        "nozzle": float(spec["nozzle_mm"]),
        "layer_h": float(spec["layer_height_mm"]),
        "fastener": spec["fastener_spec"],
        "scale_len": float(derived["scale_reference_length_mm"]),
        "include_scale": bool(spec.get("include_scale_reference")),
        "include_camera": bool(spec.get("include_camera_mount")),
        "working_dist": float(spec.get("camera_working_distance_mm", 0.0)),
        "mast_h": float(derived.get("mast_height_mm") or 0.0),
        "mast_z": float(derived.get("mast_center_z_mm") or 0.0),
        "center_z": float(derived.get("base_center_z_mm") or 0.0),
        "front_z": float(derived.get("front_edge_z_mm") or 0.0),
        "rear_z": float(derived.get("rear_edge_z_mm") or 0.0),
    }


def render_fixture_drawing_svg(fixture: dict[str, Any], revision: int) -> bytes:
    """Render an A4-landscape dimensioned orthographic engineering SVG (Top, Front, Side)."""
    d = _dimension_specs(fixture)
    page_w, page_h = 842.0, 595.0

    # Top view viewport box: (48, 56) -> 420 x 260
    top_box = (55.0, 62.0, 390.0, 245.0)
    scale_top = min(
        (top_box[2] - 70.0) / max(d["outer_w"], 10.0),
        (top_box[3] - 65.0) / max(d["outer_d"], 10.0),
    )
    scale_top = max(0.35, min(2.4, scale_top))

    # Front elevation box: (48, 330) -> 390 x 205
    front_box = (55.0, 342.0, 390.0, 195.0)
    scale_front = min(
        (front_box[2] - 70.0) / max(d["outer_w"], 10.0),
        (front_box[3] - 55.0) / max(d["total_h"], 10.0),
    )
    scale_front = max(0.35, min(2.4, scale_front))

    # Right side elevation box: (470, 62) -> 320 x 245
    side_box = (475.0, 62.0, 315.0, 245.0)
    scale_side = min(
        (side_box[2] - 70.0) / max(d["outer_d"], 10.0),
        (side_box[3] - 55.0) / max(d["total_h"], 10.0),
    )
    scale_side = max(0.35, min(2.4, scale_side))

    top_cx = top_box[0] + top_box[2] / 2.0
    top_cy = top_box[1] + top_box[3] / 2.0

    def tx(app_x: float) -> float:
        return top_cx + app_x * scale_top

    def ty(app_z: float) -> float:
        # Place front (+Z scale) at bottom of Top view, rear (-Z mast) at top.
        return top_cy + (app_z - d["center_z"]) * scale_top

    front_cx = front_box[0] + front_box[2] / 2.0
    front_base_y = front_box[1] + front_box[3] - 32.0

    def fx(app_x: float) -> float:
        return front_cx + app_x * scale_front

    def fy(app_y: float) -> float:
        return front_base_y - app_y * scale_front

    side_cz = side_box[0] + side_box[2] / 2.0
    side_base_y = side_box[1] + side_box[3] - 34.0

    def sx(app_z: float) -> float:
        return side_cz + (app_z - d["center_z"]) * scale_side

    def sy(app_y: float) -> float:
        return side_base_y - app_y * scale_side

    lines: list[str] = [
        '<?xml version="1.0" encoding="UTF-8"?>',
        f'<svg xmlns="http://www.w3.org/2000/svg" viewBox="0 0 {page_w:.0f} {page_h:.0f}" '
        f'width="{page_w:.0f}" height="{page_h:.0f}" role="img">',
        f"<title>{html.escape(d['title'])} — Revision {revision}</title>",
        "<style>",
        "  .border { fill: #ffffff; stroke: #0f172a; stroke-width: 1.5; }",
        "  .panel { fill: #f8fafc; stroke: #cbd5e1; stroke-width: 1; }",
        "  .part { fill: #e2e8f0; stroke: #0f172a; stroke-width: 1.2; }",
        "  .pocket { fill: #ffffff; stroke: #0284c7; stroke-width: 1.2; }",
        "  .hidden { fill: none; stroke: #64748b; stroke-width: 0.9; stroke-dasharray: 4 3; }",
        "  .dim { fill: none; stroke: #be123c; stroke-width: 0.9; }",
        "  .dim-text { font-family: Arial, sans-serif; font-size: 9px; fill: #9f1239; }",
        "  .hdr { font-family: Arial, sans-serif; font-size: 11px; font-weight: bold; "
        "fill: #0f172a; }",
        "  .lbl { font-family: Arial, sans-serif; font-size: 9px; fill: #334155; }",
        "</style>",
        f'<rect class="border" x="12" y="12" width="{page_w - 24:.0f}" '
        f'height="{page_h - 24:.0f}"/>',
        f'<text class="hdr" x="26" y="34">AutoLab Engineering Drawing — '
        f'{html.escape(d["title"])} (Rev {revision:02d}, Units: mm)</text>',
        f'<rect class="panel" x="{top_box[0]:.1f}" y="{top_box[1]:.1f}" '
        f'width="{top_box[2]:.1f}" height="{top_box[3]:.1f}"/>',
        f'<text class="hdr" x="{top_box[0] + 8:.1f}" y="{top_box[1] + 16:.1f}">TOP VIEW</text>',
        f'<rect class="panel" x="{front_box[0]:.1f}" y="{front_box[1]:.1f}" '
        f'width="{front_box[2]:.1f}" height="{front_box[3]:.1f}"/>',
        f'<text class="hdr" x="{front_box[0] + 8:.1f}" y="{front_box[1] + 16:.1f}">'
        "FRONT ELEVATION</text>",
        f'<rect class="panel" x="{side_box[0]:.1f}" y="{side_box[1]:.1f}" '
        f'width="{side_box[2]:.1f}" height="{side_box[3]:.1f}"/>',
        f'<text class="hdr" x="{side_box[0] + 8:.1f}" y="{side_box[1] + 16:.1f}">'
        "SIDE ELEVATION</text>",
    ]

    # --- Top view geometry ---
    cw, cd = d["core_w"] * scale_top, d["core_d"] * scale_top
    lines.append(
        f'<rect class="part" x="{tx(-d["core_w"] / 2.0):.2f}" '
        f'y="{ty(-d["core_d"] / 2.0):.2f}" width="{cw:.2f}" height="{cd:.2f}"/>'
    )
    if d["include_scale"]:
        sw = min(d["core_w"], d["scale_len"] + 16.0)
        lines.append(
            f'<rect class="part" x="{tx(-sw / 2.0):.2f}" y="{ty(d["core_d"] / 2.0):.2f}" '
            f'width="{sw * scale_top:.2f}" height="{SCALE_SHELF_DEPTH_MM * scale_top:.2f}"/>'
        )
    if d["include_camera"]:
        pw = MAST_CROSS_SECTION_MM[0] + 12.0
        pad_d = -d["core_d"] / 2.0 - d["rear_z"]
        lines.append(
            f'<rect class="part" x="{tx(-pw / 2.0):.2f}" y="{ty(d["rear_z"]):.2f}" '
            f'width="{pw * scale_top:.2f}" height="{pad_d * scale_top:.2f}"/>'
        )

    if d["object_shape"] == "rectangular":
        pw, pd = d["pocket_l"] * scale_top, d["pocket_w"] * scale_top
        lines.append(
            f'<rect class="pocket" x="{tx(-d["pocket_l"] / 2.0):.2f}" '
            f'y="{ty(-d["pocket_w"] / 2.0):.2f}" width="{pw:.2f}" height="{pd:.2f}"/>'
        )
    else:
        pr = (float(d["pocket_d"]) / 2.0) * scale_top
        lines.append(
            f'<circle class="pocket" cx="{tx(0):.2f}" cy="{ty(0):.2f}" r="{pr:.2f}"/>'
        )

    for hole in fixture["derived"].get("mounting_holes", []):
        hr = max(2.2, (float(hole["diameter_mm"]) / 2.0) * scale_top)
        lines.append(
            f'<circle class="pocket" cx="{tx(hole["x_mm"]):.2f}" '
            f'cy="{ty(hole["z_mm"]):.2f}" r="{hr:.2f}"/>'
        )

    # Top view dimension lines (outer width + pocket width/diameter)
    dim_y_top = ty(d["rear_z"]) - 14.0
    lines.append(
        f'<line class="dim" x1="{tx(-d["outer_w"] / 2.0):.2f}" y1="{dim_y_top:.2f}" '
        f'x2="{tx(d["outer_w"] / 2.0):.2f}" y2="{dim_y_top:.2f}"/>'
    )
    lines.append(
        f'<text class="dim-text" x="{top_cx:.2f}" y="{dim_y_top - 4:.2f}" '
        f'text-anchor="middle">Outer W = {_fmt(d["outer_w"])}</text>'
    )
    dim_y_btm = ty(d["front_z"]) + 15.0
    pocket_txt = (
        f"Pocket Ø = {_fmt(d['pocket_d'])}"
        if d["object_shape"] == "circular"
        else f"Pocket = {_fmt(d['pocket_l'], '')} x {_fmt(d['pocket_w'])}"
    )
    lines.append(
        f'<text class="dim-text" x="{top_cx:.2f}" y="{dim_y_btm:.2f}" '
        f'text-anchor="middle">{html.escape(pocket_txt)}</text>'
    )
    lines.append(
        f'<text class="dim-text" x="{tx(d["core_w"] / 2.0) + 10:.2f}" y="{top_cy:.2f}">'
        f'D = {_fmt(d["outer_d"])}</text>'
    )

    # --- Front elevation ---
    lines.append(
        f'<rect class="part" x="{fx(-d["core_w"] / 2.0):.2f}" y="{fy(d["base_h"]):.2f}" '
        f'width="{d["core_w"] * scale_front:.2f}" height="{d["base_h"] * scale_front:.2f}"/>'
    )
    lines.append(
        f'<line class="hidden" x1="{fx(-d["pocket_l"] / 2.0):.2f}" y1="{fy(d["floor"]):.2f}" '
        f'x2="{fx(d["pocket_l"] / 2.0):.2f}" y2="{fy(d["floor"]):.2f}"/>'
    )
    if d["include_camera"]:
        lines.append(
            f'<rect class="part" x="{fx(-14.0):.2f}" y="{fy(d["total_h"] - 8.0):.2f}" '
            f'width="{28.0 * scale_front:.2f}" '
            f'height="{(d["mast_h"]) * scale_front:.2f}"/>'
        )
        lines.append(
            f'<rect class="part" x="{fx(-18.0):.2f}" y="{fy(d["total_h"]):.2f}" '
            f'width="{36.0 * scale_front:.2f}" height="{8.0 * scale_front:.2f}"/>'
        )
    lines.append(
        f'<text class="dim-text" x="{front_cx:.2f}" y="{front_base_y + 18:.2f}" '
        f'text-anchor="middle">Base H = {_fmt(d["base_h"])} '
        f'(Floor {_fmt(d["floor"], "")} + Lip {_fmt(d["lip"])})</text>'
    )

    # --- Side elevation ---
    lines.append(
        f'<rect class="part" x="{sx(d["rear_z"]):.2f}" y="{sy(d["base_h"]):.2f}" '
        f'width="{d["outer_d"] * scale_side:.2f}" height="{d["base_h"] * scale_side:.2f}"/>'
    )
    if d["include_camera"]:
        mast_x0 = sx(d["mast_z"] - MAST_CROSS_SECTION_MM[1] / 2.0)
        lines.append(
            f'<rect class="part" x="{mast_x0:.2f}" y="{sy(d["total_h"] - 8.0):.2f}" '
            f'width="{MAST_CROSS_SECTION_MM[1] * scale_side:.2f}" '
            f'height="{d["mast_h"] * scale_side:.2f}"/>'
        )
        arm_x0 = sx(d["mast_z"] - 11.0)
        arm_len = (0.0 - (d["mast_z"] - 11.0)) + 16.0
        lines.append(
            f'<rect class="part" x="{arm_x0:.2f}" y="{sy(d["total_h"]):.2f}" '
            f'width="{arm_len * scale_side:.2f}" height="{8.0 * scale_side:.2f}"/>'
        )
        lines.append(
            f'<text class="dim-text" x="{sx(0):.2f}" y="{sy(d["total_h"] * 0.55):.2f}">'
            f'WD = {_fmt(d["working_dist"])}</text>'
        )
    lines.append(
        f'<text class="dim-text" x="{side_cz:.2f}" y="{side_base_y + 18:.2f}" '
        f'text-anchor="middle">Total H = {_fmt(d["total_h"])} · D = {_fmt(d["outer_d"])}</text>'
    )

    # --- Title block (bottom right) ---
    lines.extend(
        [
            '<rect class="panel" x="475" y="342" width="315" height="195"/>',
            '<text class="hdr" x="488" y="364">TITLE BLOCK &amp; PRINT NOTES</text>',
            f'<text class="lbl" x="488" y="384">Title: {html.escape(d["title"][:44])}</text>',
            f'<text class="lbl" x="488" y="401">Object: {html.escape(d["object_label"][:44])}'
            "</text>",
            f'<text class="lbl" x="488" y="418">Provenance: {d["provenance_label"]} '
            f'({html.escape(d["reference_id"])})</text>',
            f'<text class="lbl" x="488" y="435">Clearance/side: {_fmt(d["clearance"])} · '
            f'FDM undersize: {_fmt(d["undersize"])}</text>',
            f'<text class="lbl" x="488" y="452">Process: FDM · {d["material"]} · '
            f'Nozzle {d["nozzle"]:g} mm · Layer {d["layer_h"]:g} mm</text>',
            f'<text class="lbl" x="488" y="469">Fasteners: {html.escape(d["fastener"])} · '
            f'Supports: None required</text>',
            f'<text class="lbl" x="488" y="486">Coupon steps (mm): '
            f'{", ".join(f"{v:+.2f}" for v in COUPON_OFFSETS_MM)}</text>',
            '<text class="lbl" x="488" y="505">Verification: Print tolerance-coupon first; '
            "record real E13 fit log.</text>",
            f'<text class="lbl" x="488" y="522">Generated by AutoLab · '
            f'Revision {revision:02d}</text>',
            "</svg>",
        ]
    )
    return "\n".join(lines).encode("utf-8")


def _pdf_escape(text: str) -> str:
    safe = "".join(ch if 32 <= ord(ch) < 127 else " " for ch in text)
    return safe.replace("\\", "\\\\").replace("(", "\\(").replace(")", "\\)")


def _build_pdf_bytes(page_streams: list[str]) -> bytes:
    """Assemble a clean 2-page PDF 1.4 vector document with accurate byte offsets."""
    # Objects:
    # 1: Catalog, 2: Pages, 3: Font Helvetica, 4: Font Helvetica-Bold,
    # followed by pairs (Page, ContentStream) for each page.
    objects: list[bytes] = []
    page_ids = [5 + i * 2 for i in range(len(page_streams))]

    objects.append(b"<< /Type /Catalog /Pages 2 0 R >>")
    kids = " ".join(f"{pid} 0 R" for pid in page_ids)
    objects.append(f"<< /Type /Pages /Kids [{kids}] /Count {len(page_ids)} >>".encode("ascii"))
    objects.append(b"<< /Type /Font /Subtype /Type1 /BaseFont /Helvetica >>")
    objects.append(b"<< /Type /Font /Subtype /Type1 /BaseFont /Helvetica-Bold >>")

    for idx, stream_str in enumerate(page_streams):
        content_id = 6 + idx * 2
        page_dict = (
            f"<< /Type /Page /Parent 2 0 R /MediaBox [0 0 842 595] "
            f"/Resources << /Font << /F1 3 0 R /F2 4 0 R >> >> "
            f"/Contents {content_id} 0 R >>"
        ).encode("ascii")
        stream_bytes = stream_str.encode("latin-1", "replace")
        stream_obj = (
            f"<< /Length {len(stream_bytes)} >>\nstream\n".encode("ascii")
            + stream_bytes
            + b"\nendstream"
        )
        objects.append(page_dict)
        objects.append(stream_obj)

    buf = io.BytesIO()
    buf.write(b"%PDF-1.4\n%\xe2\xe3\xcf\xd3\n")
    offsets: list[int] = [0]
    for obj_num, payload in enumerate(objects, start=1):
        offsets.append(buf.tell())
        buf.write(f"{obj_num} 0 obj\n".encode("ascii"))
        buf.write(payload)
        buf.write(b"\nendobj\n")

    xref_pos = buf.tell()
    buf.write(f"xref\n0 {len(objects) + 1}\n".encode("ascii"))
    buf.write(b"0000000000 65535 f \n")
    for off in offsets[1:]:
        buf.write(f"{off:010d} 00000 n \n".encode("ascii"))
    buf.write(
        f"trailer\n<< /Size {len(objects) + 1} /Root 1 0 R >>\nstartxref\n{xref_pos}\n%%EOF\n"
        .encode("ascii")
    )
    return buf.getvalue()


def _pdf_text(cmds: list[str], x: float, y: float, size: float, text: str, *, bold: bool = False):
    font = "/F2" if bold else "/F1"
    cmds.append(f"BT {font} {size:.1f} Tf {x:.2f} {y:.2f} Td ({_pdf_escape(text)}) Tj ET")


def _pdf_rect(
    cmds: list[str],
    x: float,
    y_bottom: float,
    w: float,
    h: float,
    stroke_gray: float = 0.1,
):
    cmds.append(f"{stroke_gray:.2f} G 1.0 w {x:.2f} {y_bottom:.2f} {w:.2f} {h:.2f} re S")


def render_fixture_drawing_pdf(fixture: dict[str, Any], revision: int) -> bytes:
    """Render a 2-page vector PDF (Page 1 drawing, Page 2 bench build sheet & E13 log)."""
    d = _dimension_specs(fixture)

    # --- Page 1: Dimensioned orthographic views ---
    p1: list[str] = ["0.1 G 1.2 w 16 16 810 563 re S"]
    _pdf_text(
        p1,
        28,
        556,
        13,
        f"AutoLab Dimensioned Drawing — {d['title']} (Rev {revision:02d}, Units: mm)",
        bold=True,
    )
    _pdf_rect(p1, 28, 305, 420, 238, 0.65)
    _pdf_text(p1, 36, 526, 10, "TOP VIEW (Orthographic)", bold=True)

    scale_top = max(
        0.35,
        min(2.2, min(330.0 / max(d["outer_w"], 10.0), 175.0 / max(d["outer_d"], 10.0))),
    )
    tcx, tcy = 238.0, 424.0
    cw, cd = d["core_w"] * scale_top, d["core_d"] * scale_top
    _pdf_rect(p1, tcx - cw / 2.0, tcy - cd / 2.0, cw, cd, 0.1)
    if d["object_shape"] == "rectangular":
        pw, pd = d["pocket_l"] * scale_top, d["pocket_w"] * scale_top
        p1.append("0.0 0.45 0.75 RG 1.0 w")
        p1.append(f"{tcx - pw / 2.0:.2f} {tcy - pd / 2.0:.2f} {pw:.2f} {pd:.2f} re S")
        p1.append("0.0 0.0 0.0 RG")
        pocket_txt = f"Pocket: {d['pocket_l']:.2f} x {d['pocket_w']:.2f} mm"
    else:
        r = (float(d["pocket_d"]) / 2.0) * scale_top
        k = r * 0.55228
        p1.append("0.0 0.45 0.75 RG 1.0 w")
        p1.append(
            f"{tcx + r:.2f} {tcy:.2f} m "
            f"{tcx + r:.2f} {tcy + k:.2f} {tcx + k:.2f} {tcy + r:.2f} {tcx:.2f} {tcy + r:.2f} c "
            f"{tcx - k:.2f} {tcy + r:.2f} {tcx - r:.2f} {tcy + k:.2f} {tcx - r:.2f} {tcy:.2f} c "
            f"{tcx - r:.2f} {tcy - k:.2f} {tcx - k:.2f} {tcy - r:.2f} {tcx:.2f} {tcy - r:.2f} c "
            f"{tcx + k:.2f} {tcy - r:.2f} {tcx + r:.2f} {tcy - k:.2f} {tcx + r:.2f} {tcy:.2f} c S"
        )
        p1.append("0.0 0.0 0.0 RG")
        pocket_txt = f"Pocket Dia: {float(d['pocket_d']):.2f} mm"

    _pdf_text(
        p1,
        140,
        316,
        9,
        f"Outer W x D: {d['outer_w']:.2f} x {d['outer_d']:.2f} mm | {pocket_txt}",
    )

    # Front elevation panel
    _pdf_rect(p1, 28, 36, 420, 250, 0.65)
    _pdf_text(p1, 36, 268, 10, "FRONT ELEVATION", bold=True)
    sf = max(
        0.35,
        min(2.0, min(330.0 / max(d["outer_w"], 10.0), 175.0 / max(d["total_h"], 10.0))),
    )
    fw = d["core_w"] * sf
    fh = d["base_h"] * sf
    _pdf_rect(p1, 238.0 - fw / 2.0, 80.0, fw, fh, 0.1)
    if d["include_camera"]:
        _pdf_rect(p1, 238.0 - 14.0 * sf, 80.0 + fh, 28.0 * sf, d["mast_h"] * sf, 0.2)
        _pdf_rect(
            p1,
            238.0 - 18.0 * sf,
            80.0 + (d["total_h"] - 8.0) * sf,
            36.0 * sf,
            8.0 * sf,
            0.2,
        )
    _pdf_text(
        p1,
        80,
        52,
        9,
        f"Base H: {d['base_h']:.2f} mm (Floor {d['floor']:.2f} + Lip {d['lip']:.2f}) | "
        f"Total H: {d['total_h']:.2f} mm",
    )

    # Title block & specifications panel (right half)
    _pdf_rect(p1, 466, 36, 348, 507, 0.65)
    _pdf_text(p1, 480, 520, 11, "SPECIFICATIONS & TOLERANCES", bold=True)
    cam_line = (
        f"Camera Mount: {d['working_dist']:.1f} mm WD"
        if d["include_camera"]
        else "Camera Mount: None"
    )
    scale_label = "50 mm co-planar shelf" if d["include_scale"] else "Disabled"
    spec_lines = [
        f"Fixture Kind: {d['kind']}",
        f"Target Object: {d['object_label']}",
        f"Provenance: {d['provenance_label']} ({d['reference_id']})",
        f"Pocket Clearance/side: {d['clearance']:.2f} mm",
        f"Assumed FDM Undersize: {d['undersize']:.2f} mm",
        f"Process: FDM / {d['material']} / Nozzle {d['nozzle']:g} mm",
        f"Layer Height: {d['layer_h']:g} mm / Supports: None",
        f"Fastener Spec: {d['fastener']}",
        f"Scale Reference Bar: {scale_label}",
        cam_line,
        f"Coupon Steps (mm): {', '.join(f'{v:+.2f}' for v in COUPON_OFFSETS_MM)}",
        "Verify real part dimensions & print coupon first.",
    ]
    for i, line in enumerate(spec_lines):
        _pdf_text(p1, 480, 492 - i * 24, 9.5, line)

    # --- Page 2: Printable Bench Build Sheet & E13 Log ---
    p2: list[str] = ["0.1 G 1.2 w 16 16 810 563 re S"]
    _pdf_text(
        p2,
        28,
        556,
        13,
        f"AutoLab Bench Build Sheet & E13 Checklist — {d['title']} (Rev {revision:02d})",
        bold=True,
    )
    _pdf_text(
        p2,
        28,
        526,
        10,
        "PRINTABLE PARTS SCHEDULE (Z=0 build plate, zero supports)",
        bold=True,
    )
    y_cursor = 502.0
    for part in fixture["parts"]:
        pf = part["printer_fit"]
        fit_tag = (
            "FITS"
            if pf.get("fits") is True
            else ("CHECK" if pf.get("fits") is None else "EXCEEDS")
        )
        sx, sy, sz = part["size_mm"][0], part["size_mm"][2], part["size_mm"][1]
        row = (
            f"{part['id']} | Size {sx:.1f}x{sy:.1f}x{sz:.1f} mm | "
            f"{part['material']} h={part['layer_height_mm']}mm infill={part['infill_pct']}% | "
            f"~{part['estimated_filament_g']:.0f}g/{part['estimated_print_minutes']}m | "
            f"Bed: {fit_tag}"
        )
        _pdf_text(p2, 28, y_cursor, 9, row)
        y_cursor -= 18.0

    if fixture.get("fasteners_bom"):
        y_cursor -= 10.0
        _pdf_text(p2, 28, y_cursor, 10, "FASTENERS & HARDWARE", bold=True)
        y_cursor -= 18.0
        for item in fixture["fasteners_bom"]:
            line_txt = f"- {item['quantity']}x {item['item']} ({item['purpose']})"
            _pdf_text(p2, 28, y_cursor, 9, line_txt)
            y_cursor -= 16.0

    y_cursor -= 14.0
    _pdf_text(
        p2,
        28,
        y_cursor,
        10,
        "E13 REPEATABILITY TRIAL LOG (Target <= 1.0 mm over 10 placements + handoff)",
        bold=True,
    )
    y_cursor -= 16.0
    _pdf_rect(p2, 28, max(50.0, y_cursor - 190.0), 786, 184, 0.5)
    _pdf_text(
        p2,
        36,
        y_cursor - 18,
        9,
        "Trial | Seated OK | Orient Match | dX (mm) | dY (mm) | Notes",
        bold=True,
    )
    for idx in range(1, 11):
        row_y = y_cursor - 20 - idx * 16
        if row_y < 58:
            break
        row_txt = (
            f"{idx:02d}    | [   ]     | [   ]        | _______ | _______ |"
        )
        _pdf_text(p2, 36, row_y, 8.5, row_txt)

    return _build_pdf_bytes(["\n".join(p1), "\n".join(p2)])


def render_bench_build_sheet_html(fixture: dict[str, Any], project: dict, revision: int) -> bytes:
    """Render an offline printable HTML bench build sheet and caliper log."""
    spec = fixture["spec"]
    derived = fixture["derived"]
    rows = "\n".join(
        f"<tr><td><code>{html.escape(p['id'])}</code></td>"
        f"<td>{html.escape(p['print_orientation'])}</td>"
        f"<td>{html.escape(p['material'])} · {p['nozzle_mm']:g} mm nozzle · "
        f"{p['layer_height_mm']:g} mm layer · {p['perimeters']} perimeters · "
        f"{p['infill_pct']}% infill</td>"
        f"<td>{p['size_mm'][0]:.1f} × {p['size_mm'][2]:.1f} × {p['size_mm'][1]:.1f} mm</td>"
        f"<td>~{p['estimated_filament_g']:.1f} g / {p['estimated_print_minutes']} min</td>"
        f"<td>{html.escape(p['printer_fit']['detail'])}</td></tr>"
        for p in fixture["parts"]
    )
    bom_rows = "\n".join(
        f"<li><strong>{item['quantity']}×</strong> {html.escape(item['item'])} "
        f"— {html.escape(item['purpose'])}</li>"
        for item in fixture.get("fasteners_bom", [])
    )
    doc = (
        "<!doctype html><html lang='en'><head><meta charset='utf-8'>"
        f"<title>{html.escape(spec['title'])} — Build Sheet Rev {revision:02d}</title>"
        "<style>body{font-family:system-ui,sans-serif;max-width:960px;margin:24px auto;"
        "padding:0 16px;color:#0f172a}table{width:100%;border-collapse:collapse;margin:16px 0}"
        "th,td{border:1px solid #cbd5e1;padding:8px;text-align:left;font-size:13px}"
        "th{background:#f1f5f9}.note{background:#f8fafc;border-left:4px solid #0284c7;"
        "padding:10px 14px;margin:14px 0}</style></head><body>"
        f"<h1>{html.escape(spec['title'])} — Bench Build Sheet (Rev {revision:02d})</h1>"
        f"<p><strong>Object:</strong> {html.escape(spec['object_label'])} · "
        f"<strong>Provenance:</strong> {html.escape(spec['dimension_provenance'])} · "
        f"<strong>Pocket:</strong> {derived['pocket_length_mm']:.2f} × "
        f"{derived['pocket_width_mm']:.2f} mm · "
        f"<strong>Clearance/side:</strong> {spec['clearance_per_side_mm']:.2f} mm · "
        f"<strong>FDM undersize:</strong> {spec['fdm_undersize_mm']:.2f} mm</p>"
        "<h2>1. Printable Parts &amp; Slicer Settings</h2>"
        "<table><thead><tr><th>Part</th><th>Orientation</th><th>Print Settings</th>"
        "<th>Bed Envelope (W×D×H)</th><th>Est. Filament / Time</th>"
        "<th>Printer Bed Check</th></tr></thead>"
        f"<tbody>{rows}</tbody></table>"
        f"{'<h2>2. Fasteners</h2><ul>' + bom_rows + '</ul>' if bom_rows else ''}"
        "<h2>3. First-Print Calibration Workflow (E13 Prep)</h2>"
        "<div class='note'>Print <code>tolerance-coupon.stl</code> first (~15–25 min). "
        "Test all three stepped pockets (-0.15 / 0.00 / +0.15 mm) with calipers and your real "
        "specimen, record actual dimensions in AutoLab's Build Log, and apply any clearance delta "
        "before printing or revising the base.</div>"
        "</body></html>"
    )
    return doc.encode("utf-8")


def render_bench_build_sheet_md(fixture: dict[str, Any], revision: int) -> bytes:
    """Render a Markdown bench build sheet for archives and Git/lab notes."""
    spec = fixture["spec"]
    derived = fixture["derived"]
    lines = [
        f"# {spec['title']} — Bench Build Sheet (Rev {revision:02d})",
        "",
        f"- **Object:** {spec['object_label']} (`{spec['object_shape']}`)",
        f"- **Provenance:** `{spec['dimension_provenance']}` (`{spec['object_reference_id']}`)",
        f"- **Pocket:** `{derived['pocket_length_mm']:.2f} x {derived['pocket_width_mm']:.2f} mm`",
        f"- **Clearance / side:** `{spec['clearance_per_side_mm']:.2f} mm` | "
        f"**FDM undersize:** `{spec['fdm_undersize_mm']:.2f} mm`",
        "",
        "## Printable Parts",
        "",
        "| Part | Orientation | Material / Layer | Bed Envelope (mm) | Est. | Bed Check |",
        "| --- | --- | --- | --- | --- | --- |",
    ]
    for p in fixture["parts"]:
        lines.append(
            f"| `{p['id']}` | {p['print_orientation']} | "
            f"{p['material']} / {p['layer_height_mm']:g} mm | "
            f"{p['size_mm'][0]:.1f} x {p['size_mm'][2]:.1f} x {p['size_mm'][1]:.1f} | "
            f"~{p['estimated_filament_g']:.1f} g / {p['estimated_print_minutes']} min | "
            f"{p['printer_fit']['detail']} |"
        )
    if fixture.get("fasteners_bom"):
        lines.extend(["", "## Fasteners", ""])
        for item in fixture["fasteners_bom"]:
            lines.append(f"- **{item['quantity']}x** {item['item']} — {item['purpose']}")
    lines.extend(["", "## Limitations & Unverified Items", ""])
    for lim in fixture["limitations"]:
        lines.append(f"- {lim}")
    return "\n".join(lines).encode("utf-8")


def render_e13_checklist_md(fixture: dict[str, Any], revision: int) -> bytes:
    """Generate the printable E13 bench validation & second-person handoff sheet."""
    spec = fixture["spec"]
    derived = fixture["derived"]
    return "\n".join(
        [
            f"# E13 Physical Bench Trial Checklist — {spec['title']} (Rev {revision:02d})",
            "",
            f"- **Target item:** {spec['object_label']}",
            f"- **Target pocket:** "
            f"`{derived['pocket_length_mm']:.2f} x {derived['pocket_width_mm']:.2f} mm`",
            f"- **Process:** `{spec['material']}` / `{spec['nozzle_mm']:g} mm nozzle` / "
            f"`{spec['layer_height_mm']:g} mm layer`",
            "",
            "## Part A — Tolerance Coupon & Caliper Check",
            "- [ ] Printed `tolerance-coupon.stl` without supports",
            "- [ ] Measured step 1 (-0.15 mm), step 2 (0.00 mm), step 3 (+0.15 mm) with calipers",
            "- [ ] Recorded fit outcome (`fit`, `too_tight`, `too_loose`) inside AutoLab Build Log",
            "",
            "## Part B — 10-Placement Repeatability (Suggested target: <= 1.0 mm)",
            "Mark two separated points on empty specimen, keep camera/scale fixed, "
            "remove and reinsert 10 times:",
            "",
            "| Trial | Orientation OK (Y/N) | Mark 1 (mm) | Mark 2 (mm) | Notes |",
            "| --- | --- | --- | --- | --- |",
            *[f"| {i:02d} | | | | |" for i in range(1, 11)],
            "",
            "## Part C — Matched Work Time vs Baseline & Independent Handoff",
            "- [ ] Timed baseline positioning batch (seconds) and fixture batch (seconds)",
            "- [ ] Completed second-person handoff with zero off-app CAD edits",
        ]
    ).encode("utf-8")


def render_labware_drawing_svg(project: dict) -> bytes:
    """Render a dimensioned 2D orthographic SVG for active liquid labware (rack or plate)."""
    revision = int(project.get("revision", 0))
    if rack := project.get("labware_rack"):
        w, h, d = [float(v) for v in rack["size_mm"]]
        title = "Parametric Tube Rack"
        detail = (
            f"Bore = {rack['derived']['bore_diameter_mm']:.2f} mm | "
            f"Pitch = {rack['derived']['pitch_mm']:.2f} mm | "
            f"Grid = {rack['derived']['columns']} x {rack['derived']['rows']}"
        )
        sub = (
            f"Outer {w:.2f} x {d:.2f} x {h:.2f} mm | "
            f"Top plate {rack['derived']['top_thickness_mm']:.1f} mm"
        )
    elif plate := project.get("labware_plate"):
        w, h, d = [float(v) for v in plate["size_mm"]]
        title = f"Plate Holder ({plate['profile']['id']})"
        pocket_l, pocket_w = plate["derived"]["holder_pocket_mm"]
        detail = (
            f"Pocket = {pocket_l:.2f} x {pocket_w:.2f} mm | "
            f"Wall = {plate['derived']['holder_wall_mm']:.1f} mm"
        )
        sub = (
            f"Outer {w:.2f} x {d:.2f} x {h:.2f} mm | "
            f"Floor {plate['derived']['holder_floor_mm']:.1f} mm"
        )
    else:
        raise ValueError("No labware rack or microplate holder exists to draw.")

    scale = min(420.0 / max(w, 10.0), 220.0 / max(d, 10.0))
    rw, rd = w * scale, d * scale
    cx, cy = 421.0, 265.0
    return "\n".join(
        [
            '<?xml version="1.0" encoding="UTF-8"?>',
            '<svg xmlns="http://www.w3.org/2000/svg" viewBox="0 0 842 595" '
            'width="842" height="595">',
            f"<title>{html.escape(title)} Rev {revision:02d}</title>",
            '<rect x="16" y="16" width="810" height="563" fill="#fff" '
            'stroke="#0f172a" stroke-width="1.5"/>',
            f'<text x="32" y="46" font-family="Arial,sans-serif" font-size="14" '
            f'font-weight="bold">{html.escape(title)} (Rev {revision:02d}, Units: mm)</text>',
            f'<rect x="{cx - rw / 2:.2f}" y="{cy - rd / 2:.2f}" width="{rw:.2f}" '
            f'height="{rd:.2f}" fill="#e2e8f0" stroke="#0f172a" stroke-width="1.5"/>',
            f'<text x="421" y="455" text-anchor="middle" font-family="Arial,sans-serif" '
            f'font-size="12" fill="#9f1239">{html.escape(sub)}</text>',
            f'<text x="421" y="478" text-anchor="middle" font-family="Arial,sans-serif" '
            f'font-size="11" fill="#334155">{html.escape(detail)}</text>',
            '<text x="421" y="505" text-anchor="middle" font-family="Arial,sans-serif" '
            'font-size="10" fill="#475569">Process note: FDM inverted/flat build orientation; '
            "verify tolerance coupon fit before final batch.</text>",
            "</svg>",
        ]
    ).encode("utf-8")


def render_labware_drawing_pdf(project: dict) -> bytes:
    """Render a vector PDF dimensioned sheet for active liquid labware (rack or plate)."""
    revision = int(project.get("revision", 0))
    if rack := project.get("labware_rack"):
        w, h, d = [float(v) for v in rack["size_mm"]]
        title = "Parametric Tube Rack"
        detail = (
            f"Bore {rack['derived']['bore_diameter_mm']:.2f} mm | "
            f"Pitch {rack['derived']['pitch_mm']:.2f} mm | "
            f"Count {rack['derived']['count']}"
        )
    elif plate := project.get("labware_plate"):
        w, h, d = [float(v) for v in plate["size_mm"]]
        title = f"Plate Holder ({plate['profile']['id']})"
        pocket = plate["derived"]["holder_pocket_mm"]
        detail = f"Pocket {pocket[0]:.2f} x {pocket[1]:.2f} mm"
    else:
        raise ValueError("No labware rack or microplate holder exists to draw.")

    p1: list[str] = ["0.1 G 1.2 w 16 16 810 563 re S"]
    _pdf_text(p1, 28, 550, 14, f"{title} — Dimensioned Drawing (Rev {revision:02d}, mm)", bold=True)
    scale = min(460.0 / max(w, 10.0), 260.0 / max(d, 10.0))
    _pdf_rect(p1, 421.0 - w * scale / 2.0, 310.0 - d * scale / 2.0, w * scale, d * scale, 0.1)
    _pdf_text(p1, 140, 130, 10.5, f"Envelope W x D x H: {w:.2f} x {d:.2f} x {h:.2f} mm | {detail}")
    _pdf_text(
        p1,
        140,
        108,
        9.5,
        "FDM note: Print flat/inverted with 0.20 mm layers; check real tube/plate fit.",
    )
    return _build_pdf_bytes(["\n".join(p1)])


def build_fixture_package_zip(
    project: dict, selected_parts: list[str] | None = None
) -> bytes:
    """Package selected STL/STEP parts, dimensioned SVG/PDF drawings, build sheet, and manifest."""
    fixture = project.get("standalone_fixture")
    if not fixture:
        raise ValueError("No standalone fixture exists in this project.")
    revision = int(project.get("revision", 0))
    cad_files = build_fixture_cad_files(fixture, selected_parts=selected_parts)

    from app.bom_engine import bom_to_csv, bom_to_json, build_complete_bom
    from app.hardware_compatibility import evaluate_hardware_compatibility
    from app.lab_profile import has_lab_profile_data, load_lab_profile
    from app.project_assembly import assembly_json

    loaded_prof = load_lab_profile()
    active_prof = loaded_prof if has_lab_profile_data(loaded_prof) else None
    complete_bom = build_complete_bom(project, lab_profile=active_prof)
    compat = evaluate_hardware_compatibility(project, complete_bom, lab_profile=active_prof)

    bundle: dict[str, bytes] = {
        **cad_files,
        "physical-assembly.json": assembly_json(project),
        "drawing.svg": render_fixture_drawing_svg(fixture, revision),
        "drawing.pdf": render_fixture_drawing_pdf(fixture, revision),
        "build-sheet.html": render_bench_build_sheet_html(fixture, project, revision),
        "build-sheet.md": render_bench_build_sheet_md(fixture, revision),
        "e13-bench-checklist.md": render_e13_checklist_md(fixture, revision),
        "bom/purchasable-bom.json": bom_to_json(complete_bom),
        "bom/purchasable-bom.csv": bom_to_csv(complete_bom),
        "bom/hardware-compatibility.json": json.dumps(compat, indent=2, sort_keys=True).encode(
            "utf-8"
        ),
    }
    manifest = {
        "schema_version": 1,
        "project_id": project.get("id"),
        "revision": revision,
        "selected_parts": selected_parts or [p["id"] for p in fixture["parts"]],
        "files": [
            {
                "name": name,
                "size": len(bundle[name]),
                "sha256": hashlib.sha256(bundle[name]).hexdigest(),
            }
            for name in sorted(bundle)
        ],
    }
    bundle["manifest.json"] = json.dumps(manifest, indent=2, sort_keys=True).encode("utf-8")

    out = io.BytesIO()
    with zipfile.ZipFile(out, "w", compression=zipfile.ZIP_DEFLATED) as zf:
        for name in sorted(bundle):
            info = zipfile.ZipInfo(name, date_time=(1980, 1, 1, 0, 0, 0))
            info.compress_type = zipfile.ZIP_DEFLATED
            info.external_attr = 0o100644 << 16
            zf.writestr(info, bundle[name])
    return out.getvalue()
