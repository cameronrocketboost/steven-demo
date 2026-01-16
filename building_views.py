from __future__ import annotations

from dataclasses import dataclass
from enum import Enum
from io import BytesIO
from typing import Dict, Iterable, Optional, Tuple

from PIL import Image, ImageColor, ImageDraw


@dataclass(frozen=True)
class BuildingColorScheme:
    roof: str
    trim: str
    sides: str


class BuildingSide(str, Enum):
    FRONT = "front"
    BACK = "back"
    LEFT = "left"
    RIGHT = "right"


class BuildingOpeningKind(str, Enum):
    WINDOW = "window"
    DOOR = "door"
    GARAGE_DOOR = "garage_door"


@dataclass(frozen=True)
class BuildingOpening:
    """
    A simple opening on a building face, for demo drawing purposes.

    Notes:
    - `offset_ft` is along the wall from the left edge (when facing that wall).
      If omitted, the renderer will auto-place openings evenly.
    """

    side: BuildingSide
    kind: BuildingOpeningKind
    width_ft: int
    height_ft: int
    offset_ft: Optional[int] = None


_NAMED_COLORS: dict[str, str] = {
    # Streamlit demo palette
    "White": "#f5f5f5",
    "Gray": "#9aa0a6",
    "Black": "#202124",
    "Tan": "#d2b48c",
    "Brown": "#7a5c3a",
    "Red": "#b3261e",
    "Blue": "#1a73e8",
    "Green": "#1e8e3e",
    # Screenshot-ish extras (best-effort)
    "Burgundy": "#5b0b1b",
    "Sandstone": "#c9c3a6",
}


def _color(value: str) -> Tuple[int, int, int]:
    """
    Convert a named color (demo palette) or any CSS hex to an RGB tuple.
    """
    v = (value or "").strip()
    if not v:
        v = "White"
    v = _NAMED_COLORS.get(v, v)
    return ImageColor.getrgb(v)


def _clamp_int(name: str, value: int, *, min_value: int, max_value: int) -> int:
    if not isinstance(value, int):
        raise TypeError(f"{name} must be int (got {type(value).__name__})")
    return max(min_value, min(max_value, value))


def _encode_png(img: Image.Image) -> bytes:
    buf = BytesIO()
    img.save(buf, format="PNG", optimize=True)
    return buf.getvalue()


def render_building_views_png(
    *,
    width_ft: int,
    length_ft: int,
    height_ft: int,
    colors: BuildingColorScheme,
    openings: Tuple[BuildingOpening, ...] = (),
    view_names: Iterable[str] = ("isometric", "front", "back", "left", "right"),
    canvas_px: Tuple[int, int] = (900, 520),
) -> Dict[str, bytes]:
    """
    Render simple, demo-friendly "3D-ish" building views as PNG bytes.

    This is not a true 3D renderer; it generates stylized drawings (like the screenshot) that
    are stable, fast, and local-only.
    """
    w = _clamp_int("width_ft", width_ft, min_value=6, max_value=120)
    l = _clamp_int("length_ft", length_ft, min_value=6, max_value=250)
    h = _clamp_int("height_ft", height_ft, min_value=6, max_value=30)

    safe_openings = _normalize_openings(openings, width_ft=w, length_ft=l, height_ft=h)

    views: Dict[str, bytes] = {}
    want = {str(v).strip().lower() for v in view_names if str(v).strip()}
    for name in ("isometric", "front", "back", "left", "right"):
        if name in want:
            img = _render_view(
                name=name,
                width_ft=w,
                length_ft=l,
                height_ft=h,
                colors=colors,
                openings=safe_openings,
                canvas_px=canvas_px,
            )
            views[name] = _encode_png(img)
    return views


def _render_view(
    *,
    name: str,
    width_ft: int,
    length_ft: int,
    height_ft: int,
    colors: BuildingColorScheme,
    openings: Tuple[BuildingOpening, ...],
    canvas_px: Tuple[int, int],
) -> Image.Image:
    cw, ch = canvas_px
    cw = _clamp_int("canvas_width_px", int(cw), min_value=320, max_value=2400)
    ch = _clamp_int("canvas_height_px", int(ch), min_value=240, max_value=1600)

    bg = (245, 245, 245)
    ground = (220, 220, 220)
    trim = _color(colors.trim)
    roof = _color(colors.roof)
    sides = _color(colors.sides)

    img = Image.new("RGB", (cw, ch), bg)
    d = ImageDraw.Draw(img)

    # ground plane
    ground_h = int(ch * 0.32)
    d.rectangle([0, ch - ground_h, cw, ch], fill=ground)
    ground_top_y = ch - ground_h

    if name == "isometric":
        _draw_isometric(
            d,
            canvas_px=(cw, ch),
            ground_top_y=ground_top_y,
            width_ft=width_ft,
            length_ft=length_ft,
            height_ft=height_ft,
            roof=roof,
            trim=trim,
            sides=sides,
            openings=openings,
        )
    else:
        _draw_elevation(
            d,
            canvas_px=(cw, ch),
            ground_top_y=ground_top_y,
            side=name,
            width_ft=width_ft,
            length_ft=length_ft,
            height_ft=height_ft,
            roof=roof,
            trim=trim,
            sides=sides,
            openings=openings,
        )

    # subtle frame
    d.rectangle([8, 8, cw - 9, ch - 9], outline=(190, 190, 190), width=2)
    return img


def _draw_isometric(
    d: ImageDraw.ImageDraw,
    *,
    canvas_px: Tuple[int, int],
    ground_top_y: int,
    width_ft: int,
    length_ft: int,
    height_ft: int,
    roof: Tuple[int, int, int],
    trim: Tuple[int, int, int],
    sides: Tuple[int, int, int],
    openings: Tuple[BuildingOpening, ...],
) -> None:
    cw, ch = canvas_px

    # Scale + placement: aim for the building to occupy most of the canvas (≈80% width),
    # similar to the vendor "BUILDING VIEW" pages.
    # We approximate the isometric extents as:
    # - width ≈ w + dx where w ~ width_ft*scale, dx ~ length_ft*scale*0.55*0.9
    # - height ≈ wall_h + roof_rise + dy where dy ~ length_ft*scale*0.55*0.45
    # Aim bigger than before so the building dominates the frame.
    target_w_px = cw * 0.88
    target_h_px = ch * 0.72
    denom_w = (width_ft * 1.0) + (length_ft * 0.55 * 0.9)
    denom_h = (height_ft * 1.18) + (length_ft * 0.55 * 0.45)
    scale_w = target_w_px / max(1.0, denom_w)
    scale_h = target_h_px / max(1.0, denom_h)
    scale = max(2.0, min(24.0, min(scale_w, scale_h)))

    w = int(width_ft * scale)
    h = int(height_ft * scale * 0.95)
    # isometric depth (diagonal) factor
    depth = int(length_ft * scale * 0.55)
    dx = int(depth * 0.9)
    dy = int(depth * 0.45)

    margin_x = 44
    x0 = max(margin_x, int((cw - (w + dx)) / 2))
    # Sit the building on the ground plane with a little padding.
    y0 = int(ground_top_y + 18)

    # Faces: front (rectangle), side (parallelogram), roof (parallelogram + gable ridge)
    front = [(x0, y0 - h), (x0 + w, y0 - h), (x0 + w, y0), (x0, y0)]
    side_face = [(x0 + w, y0 - h), (x0 + w + dx, y0 - h - dy), (x0 + w + dx, y0 - dy), (x0 + w, y0)]

    # roof geometry
    roof_rise = max(18, int(h * 0.22))
    roof_front_left = (x0, y0 - h)
    roof_front_right = (x0 + w, y0 - h)
    roof_back_right = (x0 + w + dx, y0 - h - dy)
    roof_back_left = (x0 + dx, y0 - h - dy)
    ridge_front = (x0 + int(w * 0.52), y0 - h - roof_rise)
    ridge_back = (ridge_front[0] + dx, ridge_front[1] - dy)

    # Draw order: far roof, side, front, near roof edge.
    d.polygon([roof_back_left, roof_back_right, ridge_back], fill=_shade(roof, 0.88), outline=trim)
    # Left roof plane (was missing; without it the roof looks like it has a hole).
    d.polygon([roof_front_left, roof_back_left, ridge_back, ridge_front], fill=_shade(roof, 0.92), outline=trim)
    d.polygon([roof_front_right, roof_back_right, ridge_back, ridge_front], fill=_shade(roof, 0.95), outline=trim)

    d.polygon(side_face, fill=_shade(sides, 0.93), outline=trim)
    d.polygon(front, fill=sides, outline=trim)

    # Simple vertical siding texture (subtle)
    _draw_siding_lines(d, x0=x0, y0=y0, w=w, h=h, line_color=_shade(trim, 0.8), every_px=10)

    # roof front triangle
    d.polygon([roof_front_left, roof_front_right, ridge_front], fill=roof, outline=trim)

    # trim lines
    d.line([roof_front_left, ridge_front, roof_front_right], fill=trim, width=3)
    d.line([ridge_front, ridge_back], fill=trim, width=3)
    d.line([roof_front_right, roof_back_right], fill=trim, width=3)
    d.line([roof_front_left, roof_back_left], fill=trim, width=2)

    # Roof panel/seam lines to distinguish roof planes
    seam = _shade(trim, 0.75)
    # Left roof plane lines (eave->ridge)
    for i in range(1, 7):
        u = i / 8.0
        p_eave = _lerp_pt(roof_front_left, roof_back_left, u)
        p_ridge = _lerp_pt(ridge_front, ridge_back, u)
        d.line([p_eave, p_ridge], fill=seam, width=1)
    # Right roof plane lines (eave->ridge)
    for i in range(1, 7):
        u = i / 8.0
        p_eave = _lerp_pt(roof_front_right, roof_back_right, u)
        p_ridge = _lerp_pt(ridge_front, ridge_back, u)
        d.line([p_eave, p_ridge], fill=seam, width=1)

    # Openings (front + right side are visible in this isometric orientation)
    _draw_openings_isometric_front(
        d,
        openings=_filter_openings(openings, BuildingSide.FRONT),
        wall_x0=x0,
        wall_y0=y0,
        wall_w_px=w,
        wall_h_px=h,
        width_ft=width_ft,
        height_ft=height_ft,
        trim=trim,
    )
    _draw_openings_isometric_side(
        d,
        openings=_filter_openings(openings, BuildingSide.RIGHT),
        wall_x0=x0 + w,
        wall_y0=y0,
        wall_h_px=h,
        length_ft=length_ft,
        height_ft=height_ft,
        dx=dx,
        dy=dy,
        trim=trim,
    )


def _draw_elevation(
    d: ImageDraw.ImageDraw,
    *,
    canvas_px: Tuple[int, int],
    ground_top_y: int,
    side: str,
    width_ft: int,
    length_ft: int,
    height_ft: int,
    roof: Tuple[int, int, int],
    trim: Tuple[int, int, int],
    sides: Tuple[int, int, int],
    openings: Tuple[BuildingOpening, ...],
) -> None:
    cw, ch = canvas_px
    is_end = side in ("front", "back")
    face_ft = width_ft if is_end else length_ft

    # Scale so the building fills most of the canvas (≈80% width).
    # Vertical extent includes roof rise (~26% of wall height).
    target_w_px = cw * 0.88
    target_h_px = ch * 0.74
    scale_w = target_w_px / max(1.0, float(face_ft))
    scale_h = target_h_px / max(1.0, float(height_ft) * 1.20)
    scale = max(2.0, min(24.0, min(scale_w, scale_h)))

    w = int(face_ft * scale)
    h = int(height_ft * scale * 0.95)
    roof_rise = max(20, int(h * 0.26))

    x0 = int((cw - w) / 2)
    y0 = int(ground_top_y + 18)

    # wall
    d.rectangle([x0, y0 - h, x0 + w, y0], fill=sides, outline=trim, width=3)
    _draw_siding_lines(d, x0=x0, y0=y0, w=w, h=h, line_color=_shade(trim, 0.8), every_px=10)

    if is_end:
        # Front/back: gable roof (matches vendor FRONT/BACK pages)
        ridge_x = x0 + int(w * 0.5)
        ridge_y = y0 - h - roof_rise
        d.polygon([(x0, y0 - h), (x0 + w, y0 - h), (ridge_x, ridge_y)], fill=roof, outline=trim)

        # roof edge line
        d.line([(x0, y0 - h), (ridge_x, ridge_y), (x0 + w, y0 - h)], fill=trim, width=3)
    else:
        # Left/right: vendor shows a shallow "roof cap" (not a gable peak).
        overhang = max(6, int(w * 0.02))
        cap_h = max(10, int(h * 0.10))
        bl = (x0 - overhang, y0 - h)
        br = (x0 + w + overhang, y0 - h)
        tr = (x0 + w + overhang - int(overhang * 0.65), y0 - h - cap_h)
        tl = (x0 - overhang + int(overhang * 0.65), y0 - h - cap_h)
        d.polygon([bl, br, tr, tl], fill=roof, outline=trim)
        # thicker eave line
        d.line([bl, br], fill=trim, width=3)

    # Openings
    side_enum = BuildingSide(side)
    face_ft = width_ft if is_end else length_ft
    _draw_openings_elevation(
        d,
        openings=_filter_openings(openings, side_enum),
        wall_x0=x0,
        wall_y0=y0,
        wall_w_px=w,
        wall_h_px=h,
        wall_ft=face_ft,
        height_ft=height_ft,
        trim=trim,
    )


def _shade(rgb: Tuple[int, int, int], factor: float) -> Tuple[int, int, int]:
    f = max(0.0, min(1.0, float(factor)))
    r, g, b = rgb
    return (int(r * f), int(g * f), int(b * f))


def _lerp_pt(a: tuple[int, int], b: tuple[int, int], t: float) -> tuple[int, int]:
    tt = max(0.0, min(1.0, float(t)))
    return (int(round(a[0] + (b[0] - a[0]) * tt)), int(round(a[1] + (b[1] - a[1]) * tt)))


def _normalize_openings(
    openings: Tuple[BuildingOpening, ...],
    *,
    width_ft: int,
    length_ft: int,
    height_ft: int,
) -> Tuple[BuildingOpening, ...]:
    """
    Validate and clamp openings to reasonable demo bounds.
    """
    if not openings:
        return ()
    out: list[BuildingOpening] = []
    for o in openings:
        if not isinstance(o, BuildingOpening):
            raise TypeError(f"openings must contain BuildingOpening (got {type(o).__name__})")

        wall_ft = width_ft if o.side in (BuildingSide.FRONT, BuildingSide.BACK) else length_ft
        w = _clamp_int("opening_width_ft", int(o.width_ft), min_value=1, max_value=max(1, wall_ft))
        h = _clamp_int("opening_height_ft", int(o.height_ft), min_value=1, max_value=max(1, height_ft))
        offset = None if o.offset_ft is None else _clamp_int("opening_offset_ft", int(o.offset_ft), min_value=0, max_value=max(0, wall_ft - 1))
        out.append(
            BuildingOpening(
                side=o.side,
                kind=o.kind,
                width_ft=w,
                height_ft=h,
                offset_ft=offset,
            )
        )
    return tuple(out)


def _filter_openings(openings: Tuple[BuildingOpening, ...], side: BuildingSide) -> Tuple[BuildingOpening, ...]:
    return tuple(o for o in openings if o.side == side)


def _auto_offsets_ft(openings: Tuple[BuildingOpening, ...], *, wall_ft: int) -> list[int]:
    """
    Auto-place openings evenly across the wall if they don't specify offsets.
    """
    if wall_ft <= 0:
        return [0 for _ in openings]

    n = len(openings)
    if n <= 0:
        return []

    usable = max(1, wall_ft - 2)
    # Evenly spaced centers; clamp to [1, wall_ft-1]
    centers = [int(round((i + 1) * usable / (n + 1))) for i in range(n)]
    return [max(1, min(wall_ft - 1, c)) for c in centers]


def _draw_siding_lines(
    d: ImageDraw.ImageDraw,
    *,
    x0: int,
    y0: int,
    w: int,
    h: int,
    line_color: Tuple[int, int, int],
    every_px: int,
) -> None:
    if every_px <= 0:
        return
    top = y0 - h
    for x in range(x0 + every_px, x0 + w, every_px):
        d.line([(x, top + 2), (x, y0 - 2)], fill=line_color, width=1)


def _draw_openings_elevation(
    d: ImageDraw.ImageDraw,
    *,
    openings: Tuple[BuildingOpening, ...],
    wall_x0: int,
    wall_y0: int,
    wall_w_px: int,
    wall_h_px: int,
    wall_ft: int,
    height_ft: int,
    trim: Tuple[int, int, int],
) -> None:
    if not openings:
        return

    offsets = _auto_offsets_ft(openings, wall_ft=wall_ft)
    for i, o in enumerate(openings):
        off_ft = o.offset_ft if o.offset_ft is not None else offsets[i]
        w_px = max(8, int((o.width_ft / max(1, wall_ft)) * wall_w_px))
        h_px = max(10, int((o.height_ft / max(1, height_ft)) * wall_h_px))
        cx = wall_x0 + int((off_ft / max(1, wall_ft)) * wall_w_px)
        x1 = max(wall_x0 + 6, min(wall_x0 + wall_w_px - 6 - w_px, cx - int(w_px / 2)))
        x2 = x1 + w_px

        if o.kind in (BuildingOpeningKind.DOOR, BuildingOpeningKind.GARAGE_DOOR):
            y2 = wall_y0 - 3
            y1 = max(wall_y0 - wall_h_px + 6, y2 - h_px)
            _draw_door_rect(d, x1=x1, y1=y1, x2=x2, y2=y2, trim=trim, is_garage=(o.kind == BuildingOpeningKind.GARAGE_DOOR))
        else:
            # window
            floor = wall_y0
            mid_y = wall_y0 - int(wall_h_px * 0.55)
            y1 = max(floor - wall_h_px + 6, mid_y - int(h_px / 2))
            y2 = min(floor - 10, y1 + h_px)
            _draw_window_rect(d, x1=x1, y1=y1, x2=x2, y2=y2, trim=trim)


def _draw_door_rect(
    d: ImageDraw.ImageDraw,
    *,
    x1: int,
    y1: int,
    x2: int,
    y2: int,
    trim: Tuple[int, int, int],
    is_garage: bool,
) -> None:
    fill = (250, 250, 250)
    outline = _shade(trim, 0.9)
    d.rectangle([x1, y1, x2, y2], fill=fill, outline=outline, width=2)
    if is_garage:
        # Simple panel lines
        step = max(8, int((y2 - y1) / 6))
        for y in range(y1 + step, y2, step):
            d.line([(x1 + 2, y), (x2 - 2, y)], fill=_shade(outline, 0.85), width=1)
    else:
        # Handle
        hx = x2 - 10
        hy = y1 + int((y2 - y1) * 0.55)
        d.ellipse([hx - 2, hy - 2, hx + 2, hy + 2], fill=_shade(outline, 0.8), outline=None)


def _draw_window_rect(
    d: ImageDraw.ImageDraw,
    *,
    x1: int,
    y1: int,
    x2: int,
    y2: int,
    trim: Tuple[int, int, int],
) -> None:
    glass = (245, 248, 255)
    outline = _shade(trim, 0.9)
    d.rectangle([x1, y1, x2, y2], fill=glass, outline=outline, width=2)
    # Simple muntin grid (4-lite look)
    mx = int((x1 + x2) / 2)
    my = int((y1 + y2) / 2)
    d.line([(mx, y1 + 2), (mx, y2 - 2)], fill=_shade(outline, 0.85), width=1)
    d.line([(x1 + 2, my), (x2 - 2, my)], fill=_shade(outline, 0.85), width=1)


def _draw_openings_isometric_front(
    d: ImageDraw.ImageDraw,
    *,
    openings: Tuple[BuildingOpening, ...],
    wall_x0: int,
    wall_y0: int,
    wall_w_px: int,
    wall_h_px: int,
    width_ft: int,
    height_ft: int,
    trim: Tuple[int, int, int],
) -> None:
    if not openings:
        return
    offsets = _auto_offsets_ft(openings, wall_ft=width_ft)
    for i, o in enumerate(openings):
        off_ft = o.offset_ft if o.offset_ft is not None else offsets[i]
        w_px = max(8, int((o.width_ft / max(1, width_ft)) * wall_w_px))
        h_px = max(10, int((o.height_ft / max(1, height_ft)) * wall_h_px))
        cx = wall_x0 + int((off_ft / max(1, width_ft)) * wall_w_px)
        x1 = max(wall_x0 + 6, min(wall_x0 + wall_w_px - 6 - w_px, cx - int(w_px / 2)))
        x2 = x1 + w_px
        if o.kind in (BuildingOpeningKind.DOOR, BuildingOpeningKind.GARAGE_DOOR):
            y2 = wall_y0 - 2
            y1 = max(wall_y0 - wall_h_px + 6, y2 - h_px)
            _draw_door_rect(d, x1=x1, y1=y1, x2=x2, y2=y2, trim=trim, is_garage=(o.kind == BuildingOpeningKind.GARAGE_DOOR))
        else:
            my = wall_y0 - int(wall_h_px * 0.55)
            y1 = max(wall_y0 - wall_h_px + 6, my - int(h_px / 2))
            y2 = min(wall_y0 - 10, y1 + h_px)
            _draw_window_rect(d, x1=x1, y1=y1, x2=x2, y2=y2, trim=trim)


def _draw_openings_isometric_side(
    d: ImageDraw.ImageDraw,
    *,
    openings: Tuple[BuildingOpening, ...],
    wall_x0: int,
    wall_y0: int,
    wall_h_px: int,
    length_ft: int,
    height_ft: int,
    dx: int,
    dy: int,
    trim: Tuple[int, int, int],
) -> None:
    if not openings:
        return
    offsets = _auto_offsets_ft(openings, wall_ft=length_ft)
    for i, o in enumerate(openings):
        off_ft = o.offset_ft if o.offset_ft is not None else offsets[i]
        u_center = off_ft / max(1, length_ft)
        u0 = max(0.0, u_center - (o.width_ft / max(1, length_ft)) / 2.0)
        u1 = min(1.0, u0 + (o.width_ft / max(1, length_ft)))

        v_h = o.height_ft / max(1, height_ft)
        if o.kind in (BuildingOpeningKind.DOOR, BuildingOpeningKind.GARAGE_DOOR):
            v0 = 0.0
            v1 = min(1.0, v_h)
        else:
            v1 = min(1.0, 0.70)
            v0 = max(0.0, v1 - v_h)

        poly = _iso_side_poly(wall_x0=wall_x0, wall_y0=wall_y0, wall_h_px=wall_h_px, dx=dx, dy=dy, u0=u0, u1=u1, v0=v0, v1=v1)
        fill = (250, 250, 250) if o.kind != BuildingOpeningKind.WINDOW else (245, 248, 255)
        outline = _shade(trim, 0.9)
        d.polygon(poly, fill=fill, outline=outline)
        # a little inner line for definition
        d.line([poly[0], poly[1]], fill=_shade(outline, 0.85), width=1)


def _iso_side_poly(
    *,
    wall_x0: int,
    wall_y0: int,
    wall_h_px: int,
    dx: int,
    dy: int,
    u0: float,
    u1: float,
    v0: float,
    v1: float,
) -> list[tuple[int, int]]:
    """
    Convert side-wall normalized coords into a 2D isometric quadrilateral.
    - u: along length (0..1) maps to (dx, -dy)
    - v: vertical (0..1) maps to -wall_h_px
    """

    def pt(u: float, v: float) -> tuple[int, int]:
        x = wall_x0 + int(dx * u)
        y = wall_y0 - int(dy * u) - int(wall_h_px * v)
        return (x, y)

    return [pt(u0, v1), pt(u1, v1), pt(u1, v0), pt(u0, v0)]

