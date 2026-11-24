"""Local USB camera enumeration/capture and planar fiducial metric calibration (DLT homography)."""

from __future__ import annotations

import base64
import io
import math
from collections import deque
from typing import Any

import numpy as np
from PIL import Image, ImageDraw

from app.vision import MAX_DECODED_BYTES, CanonicalImage, VisionError, prepare_image

try:
    import cv2  # type: ignore[import-not-found]
except ImportError:  # pragma: no cover - optional runtime dependency
    cv2 = None  # type: ignore[assignment]


def camera_capabilities() -> dict[str, Any]:
    """Report optional OpenCV USB camera and ArUco availability accurately."""
    has_cv2 = cv2 is not None
    has_aruco = bool(has_cv2 and hasattr(cv2, "aruco"))
    return {
        "opencv_available": has_cv2,
        "aruco_available": has_aruco,
        "numpy_fiducial_available": True,
    }


def list_local_cameras(max_index: int = 4) -> dict[str, Any]:
    """Probe connected local USB cameras via OpenCV when available."""
    caps = camera_capabilities()
    if cv2 is None:
        return {
            **caps,
            "cameras": [],
            "note": (
                "opencv-python is not installed; use browser camera / photo upload, "
                "or install opencv-python for direct server USB capture."
            ),
        }
    found: list[dict[str, Any]] = []
    for index in range(max(1, min(int(max_index), 8))):
        cap = cv2.VideoCapture(index)
        try:
            if cap is not None and cap.isOpened():
                width = int(cap.get(cv2.CAP_PROP_FRAME_WIDTH) or 0)
                height = int(cap.get(cv2.CAP_PROP_FRAME_HEIGHT) or 0)
                found.append({"index": index, "width": width, "height": height})
        finally:
            if cap is not None:
                cap.release()
    return {**caps, "cameras": found}


def capture_usb_camera_frame(
    camera_index: int = 0,
    width: int = 1280,
    height: int = 720,
) -> CanonicalImage:
    """Capture one JPEG frame from a local USB camera and return CanonicalImage."""
    if cv2 is None:
        raise VisionError(
            "Direct USB camera capture requires opencv-python; use browser capture or upload."
        )
    cap = cv2.VideoCapture(int(camera_index))
    try:
        if cap is None or not cap.isOpened():
            raise VisionError(f"Camera index {camera_index} could not be opened.")
        cap.set(cv2.CAP_PROP_FRAME_WIDTH, int(width))
        cap.set(cv2.CAP_PROP_FRAME_HEIGHT, int(height))
        ok, frame = cap.read()
        if not ok or frame is None:
            raise VisionError(f"Camera index {camera_index} returned no frame.")
        enc_ok, buf = cv2.imencode(".jpg", frame, [int(cv2.IMWRITE_JPEG_QUALITY), 92])
        if not enc_ok:
            raise VisionError("Could not encode captured camera frame as JPEG.")
        data_url = "data:image/jpeg;base64," + base64.b64encode(buf.tobytes()).decode("ascii")
        return prepare_image(data_url)
    finally:
        if cap is not None:
            cap.release()


def board_marker_centers_mm(
    board_width_mm: float = 160.0,
    board_height_mm: float = 110.0,
    marker_size_mm: float = 20.0,
) -> list[tuple[float, float]]:
    """Return physical marker center coordinates (mm) ordered TL, TR, BR, BL."""
    if (
        board_width_mm < 40
        or board_height_mm < 40
        or marker_size_mm < 5
        or marker_size_mm * 2.5 >= min(board_width_mm, board_height_mm)
    ):
        raise ValueError("Invalid calibration board dimensions or marker size.")
    margin = marker_size_mm * 0.5 + 8.0
    return [
        (round(margin, 4), round(margin, 4)),
        (round(board_width_mm - margin, 4), round(margin, 4)),
        (round(board_width_mm - margin, 4), round(board_height_mm - margin, 4)),
        (round(margin, 4), round(board_height_mm - margin, 4)),
    ]


def generate_calibration_board_svg(
    width_mm: float = 160.0,
    height_mm: float = 110.0,
    marker_size_mm: float = 20.0,
) -> str:
    """Generate a printable 1:1 mm SVG fiducial calibration mat with 50 mm scale check."""
    centers = board_marker_centers_mm(width_mm, height_mm, marker_size_mm)
    half = marker_size_mm / 2.0
    markers_svg: list[str] = []
    labels = ["TL", "TR", "BR", "BL"]
    for label, (cx, cy) in zip(labels, centers):
        x0 = cx - half
        y0 = cy - half
        markers_svg.append(
            f'<rect x="{x0:.2f}" y="{y0:.2f}" width="{marker_size_mm:.2f}" '
            f'height="{marker_size_mm:.2f}" fill="#111111"/>'
        )
        # Small orientation dot well outside center centroid invariance or asymmetric bright tick.
        markers_svg.append(
            f'<text x="{cx:.2f}" y="{y0 - 2.0:.2f}" font-family="sans-serif" font-size="3.2" '
            f'text-anchor="middle" fill="#6b7280">{label}</text>'
        )

    bar_x = (width_mm - 50.0) / 2.0
    bar_y = height_mm - 10.0
    ruler_ticks = []
    for mm in range(10, int(width_mm), 10):
        ruler_ticks.append(
            f'<line x1="{mm}" y1="0" x2="{mm}" y2="3" stroke="#9ca3af" stroke-width="0.35"/>'
        )

    return (
        f'<svg xmlns="http://www.w3.org/2000/svg" width="{width_mm:g}mm" '
        f'height="{height_mm:g}mm" viewBox="0 0 {width_mm:g} {height_mm:g}">\n'
        f'  <rect x="0" y="0" width="{width_mm:g}" height="{height_mm:g}" '
        f'fill="#ffffff" stroke="#d1d5db" stroke-width="0.6"/>\n'
        + "  "
        + "\n  ".join(ruler_ticks)
        + "\n  "
        + "\n  ".join(markers_svg)
        + f'\n  <line x1="{bar_x:.2f}" y1="{bar_y:.2f}" x2="{bar_x + 50:.2f}" '
        f'y2="{bar_y:.2f}" stroke="#6b7280" stroke-width="0.8"/>\n'
        f'  <text x="{width_mm / 2:.2f}" y="{bar_y - 2.0:.2f}" font-family="sans-serif" '
        f'font-size="3.6" text-anchor="middle" fill="#4b5563">'
        f"50 mm scale check · Print at 100% (Actual Size)</text>\n"
        "</svg>\n"
    )


def _solve_homography_dlt(
    src_pts: list[tuple[float, float]] | np.ndarray,
    dst_pts: list[tuple[float, float]] | np.ndarray,
) -> np.ndarray:
    """Compute exact 3x3 planar homography H mapping src -> dst via Hartley-normalized DLT."""
    src = np.asarray(src_pts, dtype=np.float64)
    dst = np.asarray(dst_pts, dtype=np.float64)
    if src.shape != dst.shape or src.ndim != 2 or src.shape[0] < 4 or src.shape[1] != 2:
        raise ValueError("Homography solver requires >= 4 corresponding 2D point pairs.")

    def _norm_matrix(pts: np.ndarray) -> np.ndarray:
        centroid = np.mean(pts, axis=0)
        shifted = pts - centroid
        rms = float(np.sqrt(np.mean(np.sum(shifted ** 2, axis=1))))
        scale = math.sqrt(2.0) / rms if rms > 1e-9 else 1.0
        return np.array(
            [
                [scale, 0.0, -scale * centroid[0]],
                [0.0, scale, -scale * centroid[1]],
                [0.0, 0.0, 1.0],
            ],
            dtype=np.float64,
        )

    t_src = _norm_matrix(src)
    t_dst = _norm_matrix(dst)
    src_n = (t_src @ np.column_stack([src, np.ones(len(src))]).T).T
    dst_n = (t_dst @ np.column_stack([dst, np.ones(len(dst))]).T).T

    rows: list[list[float]] = []
    for (x, y, _), (u, v, _) in zip(src_n, dst_n):
        rows.append([-x, -y, -1.0, 0.0, 0.0, 0.0, u * x, u * y, u])
        rows.append([0.0, 0.0, 0.0, -x, -y, -1.0, v * x, v * y, v])
    a = np.asarray(rows, dtype=np.float64)
    _, _, vt = np.linalg.svd(a)
    h_norm = vt[-1].reshape(3, 3)
    h = np.linalg.inv(t_dst) @ h_norm @ t_src
    if abs(h[2, 2]) < 1e-12:
        raise ValueError("Degenerate calibration geometry (collinear or singular corners).")
    return h / h[2, 2]


def pixel_to_mm(
    h_matrix: np.ndarray | list[list[float]], x_px: float, y_px: float
) -> tuple[float, float]:
    """Project one pixel coordinate (x_px, y_px) into physical plane coordinates (mm)."""
    h = np.asarray(h_matrix, dtype=np.float64)
    vec = h @ np.array([float(x_px), float(y_px), 1.0], dtype=np.float64)
    if abs(vec[2]) < 1e-12:
        raise ValueError("Homography projection denominator vanished.")
    return float(vec[0] / vec[2]), float(vec[1] / vec[2])


def render_synthetic_calibration_image(
    width_px: int = 800,
    height_px: int = 600,
    *,
    board_width_mm: float = 160.0,
    board_height_mm: float = 110.0,
    marker_size_mm: float = 20.0,
    perspective_shift_px: tuple[float, float] = (0.0, 0.0),
) -> tuple[bytes, list[tuple[float, float]]]:
    """Render a synthetic calibration board PNG (optionally perspective-warped) + true centers."""
    img = Image.new("RGB", (width_px, height_px), (246, 248, 250))
    draw = ImageDraw.Draw(img)
    pad_x = width_px * 0.12
    pad_y = height_px * 0.12
    scale_x = (width_px - 2 * pad_x) / board_width_mm
    scale_y = (height_px - 2 * pad_y) / board_height_mm
    px_per_mm = min(scale_x, scale_y)
    ox = (width_px - board_width_mm * px_per_mm) / 2.0
    oy = (height_px - board_height_mm * px_per_mm) / 2.0
    half_px = (marker_size_mm * px_per_mm) / 2.0

    centers_mm = board_marker_centers_mm(board_width_mm, board_height_mm, marker_size_mm)
    flat_centers = [(ox + cx * px_per_mm, oy + cy * px_per_mm) for cx, cy in centers_mm]
    for cx_p, cy_p in flat_centers:
        draw.rectangle(
            [cx_p - half_px, cy_p - half_px, cx_p + half_px, cy_p + half_px],
            fill=(10, 10, 10),
        )

    true_centers = flat_centers
    sx, sy = float(perspective_shift_px[0]), float(perspective_shift_px[1])
    if abs(sx) > 1e-3 or abs(sy) > 1e-3:
        src_quad = [
            (0.0, 0.0),
            (float(width_px), 0.0),
            (float(width_px), float(height_px)),
            (0.0, float(height_px)),
        ]
        dst_quad = [
            (max(8.0, sx), max(8.0, sy)),
            (width_px - max(8.0, sx * 0.6), max(4.0, sy * 0.3)),
            (width_px - max(6.0, abs(sx) * 0.4), height_px - max(8.0, abs(sy) * 0.5)),
            (max(4.0, abs(sx) * 0.5), height_px - max(6.0, sy * 0.2)),
        ]
        # Pillow PERSPECTIVE takes coeffs mapping output (x, y) -> input (x', y').
        h_fwd = _solve_homography_dlt(src_quad, dst_quad)
        h_inv = _solve_homography_dlt(dst_quad, src_quad)
        coeffs = (
            h_inv[0, 0],
            h_inv[0, 1],
            h_inv[0, 2],
            h_inv[1, 0],
            h_inv[1, 1],
            h_inv[1, 2],
            h_inv[2, 0],
            h_inv[2, 1],
        )
        img = img.transform(
            (width_px, height_px),
            Image.Transform.PERSPECTIVE,
            coeffs,
            resample=Image.Resampling.BICUBIC,
            fillcolor=(246, 248, 250),
        )
        true_centers = [pixel_to_mm(h_fwd, cx, cy) for cx, cy in flat_centers]

    buf = io.BytesIO()
    img.save(buf, format="PNG")
    return buf.getvalue(), true_centers


def _find_quadrant_dark_centroid(
    gray: np.ndarray, q_box: tuple[int, int, int, int]
) -> tuple[float, float]:
    """Locate the highest-contrast square dark fiducial marker in one quadrant."""
    x0, y0, x1, y1 = q_box
    patch = gray[y0:y1, x0:x1]
    if patch.size == 0:
        raise ValueError("Empty quadrant for fiducial detection.")
    bg_level = float(np.percentile(patch, 90))
    fg_min = float(np.min(patch))
    if bg_level - fg_min < 55.0:
        raise ValueError("Insufficient fiducial contrast detected in image quadrant.")
    thresh = fg_min + 0.35 * (bg_level - fg_min)

    # Downsample mask for fast O(N) connected-components, then refine on native full-res window.
    qh, qw = patch.shape
    stride = max(1, max(qh, qw) // 160)
    small = patch[::stride, ::stride] < thresh
    sh, sw = small.shape
    visited = np.zeros((sh, sw), dtype=bool)

    best_score = -1.0
    best_bbox: tuple[int, int, int, int] | None = None
    for sy in range(sh):
        for sx in range(sw):
            if not small[sy, sx] or visited[sy, sx]:
                continue
            queue: deque[tuple[int, int]] = deque([(sy, sx)])
            visited[sy, sx] = True
            count = 0
            min_x = max_x = sx
            min_y = max_y = sy
            while queue:
                cy, cx = queue.popleft()
                count += 1
                min_x = min(min_x, cx)
                max_x = max(max_x, cx)
                min_y = min(min_y, cy)
                max_y = max(max_y, cy)
                for dy, dx in ((-1, 0), (1, 0), (0, -1), (0, 1)):
                    ny, nx = cy + dy, cx + dx
                    if 0 <= ny < sh and 0 <= nx < sw and small[ny, nx] and not visited[ny, nx]:
                        visited[ny, nx] = True
                        queue.append((ny, nx))
            bw = max(1, max_x - min_x + 1)
            bh = max(1, max_y - min_y + 1)
            aspect = bw / bh
            fill_ratio = count / float(bw * bh)
            # Prefer compact, nearly square fiducial blocks with meaningful area.
            if count >= 4 and 0.35 <= aspect <= 2.8 and fill_ratio >= 0.45:
                score = count * fill_ratio
                if score > best_score:
                    best_score = score
                    best_bbox = (
                        min_x * stride,
                        min_y * stride,
                        (max_x + 1) * stride,
                        (max_y + 1) * stride,
                    )

    if best_bbox is None:
        raise ValueError("Could not detect four square corner fiducial markers in the image.")

    pad = max(6, int(max(best_bbox[2] - best_bbox[0], best_bbox[3] - best_bbox[1]) * 0.35))
    rx0 = max(0, best_bbox[0] - pad)
    ry0 = max(0, best_bbox[1] - pad)
    rx1 = min(qw, best_bbox[2] + pad)
    ry1 = min(qh, best_bbox[3] + pad)
    sub = patch[ry0:ry1, rx0:rx1]
    sub_bg = float(np.percentile(sub, 95))
    weights = np.clip(sub_bg - sub, 0.0, None)
    weights[weights < (0.55 * float(np.max(weights)))] = 0.0
    total_w = float(np.sum(weights))
    if total_w <= 1e-6:
        raise ValueError("Fiducial marker centroid refinement failed.")
    yy, xx = np.mgrid[ry0:ry1, rx0:rx1]
    cx_local = float(np.sum(xx * weights) / total_w)
    cy_local = float(np.sum(yy * weights) / total_w)
    return float(x0 + cx_local), float(y0 + cy_local)


def _decode_pil_image(image_input: str | bytes) -> Image.Image:
    if isinstance(image_input, str):
        canonical = prepare_image(image_input)
        raw = canonical.content
    elif isinstance(image_input, (bytes, bytearray)):
        if len(image_input) > MAX_DECODED_BYTES:
            raise VisionError("Calibration image exceeds maximum size.")
        raw = bytes(image_input)
    else:
        raise VisionError("Expected image bytes or data URL.")
    with Image.open(io.BytesIO(raw)) as img:
        return img.convert("RGB")


def calibrate_planar_fiducials(
    image_input: str | bytes,
    *,
    board_width_mm: float = 160.0,
    board_height_mm: float = 110.0,
    marker_size_mm: float = 20.0,
    manual_corners_px: list[list[float]] | None = None,
) -> dict[str, Any]:
    """Detect fiducials (or use manual corners) and solve pixel-to-mm planar homography."""
    img = _decode_pil_image(image_input)
    w_px, h_px = img.size
    physical_mm = board_marker_centers_mm(board_width_mm, board_height_mm, marker_size_mm)

    backend = "numpy_fiducial"
    corners_px: list[tuple[float, float]] | None = None

    if manual_corners_px is not None:
        if len(manual_corners_px) != 4:
            raise ValueError("manual_corners_px must contain exactly 4 [x, y] points.")
        parsed = []
        for pt in manual_corners_px:
            if len(pt) != 2 or not all(math.isfinite(float(v)) for v in pt):
                raise ValueError("Each manual corner must be a finite [x, y] pair.")
            parsed.append((float(pt[0]), float(pt[1])))
        corners_px = parsed
        backend = "manual_4_corner"
    elif cv2 is not None and hasattr(cv2, "aruco"):
        try:
            gray_u8 = np.asarray(img.convert("L"), dtype=np.uint8)
            dictionary = cv2.aruco.getPredefinedDictionary(cv2.aruco.DICT_4X4_50)
            detector = cv2.aruco.ArucoDetector(dictionary, cv2.aruco.DetectorParameters())
            marker_corners, marker_ids, _ = detector.detectMarkers(gray_u8)
            if marker_ids is not None and len(marker_ids) >= 4:
                by_id = {
                    int(mid): np.mean(mc[0], axis=0)
                    for mc, mid in zip(marker_corners, marker_ids.flatten())
                }
                if all(k in by_id for k in (0, 1, 2, 3)):
                    corners_px = [(float(by_id[k][0]), float(by_id[k][1])) for k in (0, 1, 2, 3)]
                    backend = "opencv_aruco"
        except Exception:
            corners_px = None

    if corners_px is None:
        gray = np.asarray(img.convert("L"), dtype=np.float32)
        mid_x, mid_y = w_px // 2, h_px // 2
        pad = 2
        quadrants = [
            (pad, pad, mid_x, mid_y),
            (mid_x, pad, w_px - pad, mid_y),
            (mid_x, mid_y, w_px - pad, h_px - pad),
            (pad, mid_y, mid_x, h_px - pad),
        ]
        corners_px = [_find_quadrant_dark_centroid(gray, q) for q in quadrants]
        backend = "numpy_fiducial"

    h_px_to_mm = _solve_homography_dlt(corners_px, physical_mm)
    h_mm_to_px = np.linalg.inv(h_px_to_mm)
    if abs(h_mm_to_px[2, 2]) > 1e-12:
        h_mm_to_px = h_mm_to_px / h_mm_to_px[2, 2]

    sq_errors_mm: list[float] = []
    projected_mm: list[list[float]] = []
    for (x_p, y_p), (x_t, y_t) in zip(corners_px, physical_mm):
        x_est, y_est = pixel_to_mm(h_px_to_mm, x_p, y_p)
        projected_mm.append([round(x_est, 4), round(y_est, 4)])
        sq_errors_mm.append((x_est - x_t) ** 2 + (y_est - y_t) ** 2)
    rmse_mm = float(math.sqrt(sum(sq_errors_mm) / len(sq_errors_mm)))

    # Estimate local metric scale near image center (px per mm).
    cx, cy = w_px / 2.0, h_px / 2.0
    p0x, p0y = pixel_to_mm(h_px_to_mm, cx, cy)
    p1x, p1y = pixel_to_mm(h_px_to_mm, cx + 10.0, cy)
    p2x, p2y = pixel_to_mm(h_px_to_mm, cx, cy + 10.0)
    mm_per_10px_x = math.hypot(p1x - p0x, p1y - p0y)
    mm_per_10px_y = math.hypot(p2x - p0x, p2y - p0y)
    px_per_mm_x = round(10.0 / mm_per_10px_x, 4) if mm_per_10px_x > 1e-9 else 0.0
    px_per_mm_y = round(10.0 / mm_per_10px_y, 4) if mm_per_10px_y > 1e-9 else 0.0

    return {
        "calibrated": bool(rmse_mm <= 1.0 and px_per_mm_x > 0 and px_per_mm_y > 0),
        "backend": backend,
        "image_size_px": [w_px, h_px],
        "board_mm": [float(board_width_mm), float(board_height_mm)],
        "marker_size_mm": float(marker_size_mm),
        "detected_corners_px": [[round(x, 3), round(y, 3)] for x, y in corners_px],
        "physical_corners_mm": [[x, y] for x, y in physical_mm],
        "reprojected_corners_mm": projected_mm,
        "rmse_mm": round(rmse_mm, 5),
        "px_per_mm_x": px_per_mm_x,
        "px_per_mm_y": px_per_mm_y,
        "px_per_mm_mean": round((px_per_mm_x + px_per_mm_y) / 2.0, 4),
        "homography_px_to_mm": [[round(float(v), 8) for v in row] for row in h_px_to_mm],
        "homography_mm_to_px": [[round(float(v), 8) for v in row] for row in h_mm_to_px],
    }


def measure_normalized_box_mm(
    calibration: dict[str, Any], box: dict[str, float]
) -> dict[str, float]:
    """Convert a 0..1000 normalized inspection Box into calibrated millimetres."""
    w_px, h_px = calibration["image_size_px"]
    h = calibration["homography_px_to_mm"]
    x0_px = float(box["x_min"]) * w_px / 1000.0
    y0_px = float(box["y_min"]) * h_px / 1000.0
    x1_px = float(box["x_max"]) * w_px / 1000.0
    y1_px = float(box["y_max"]) * h_px / 1000.0
    tl = pixel_to_mm(h, x0_px, y0_px)
    tr = pixel_to_mm(h, x1_px, y0_px)
    br = pixel_to_mm(h, x1_px, y1_px)
    bl = pixel_to_mm(h, x0_px, y1_px)
    center = pixel_to_mm(h, (x0_px + x1_px) / 2.0, (y0_px + y1_px) / 2.0)
    w_top = math.hypot(tr[0] - tl[0], tr[1] - tl[1])
    w_bot = math.hypot(br[0] - bl[0], br[1] - bl[1])
    h_left = math.hypot(bl[0] - tl[0], bl[1] - tl[1])
    h_right = math.hypot(br[0] - tr[0], br[1] - tr[1])
    width_mm = (w_top + w_bot) / 2.0
    height_mm = (h_left + h_right) / 2.0
    return {
        "center_x_mm": round(center[0], 2),
        "center_y_mm": round(center[1], 2),
        "width_mm": round(width_mm, 2),
        "height_mm": round(height_mm, 2),
    }
