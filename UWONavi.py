"""
UWONavi - Uncharted Waters Online Navigator
Automatically reads in-game coordinates via pixel OCR and displays
player position on a world map. Configure via UWONavi.ini.

Requirements:
    pip install pillow pywin32

Files (all in the same folder):
    UWONavi.py
    UWONavi.ini
    nums.bmp
    <map image named in UWONavi.ini>

Hotkeys (configurable in UWONavi.ini):
    R  - Re-center map on player
    C  - Clear track history
"""

import tkinter as tk
from PIL import Image, ImageTk
import time
import threading
import math
import os
import json
import configparser
import sys

try:
    import win32gui
    import win32ui
    import win32con
    WINDOWS = True
except ImportError:
    WINDOWS = False
    print("WARNING: pywin32 not found. Install with: pip install pywin32")


# ─── Constants ────────────────────────────────────────────────────────────────

if getattr(sys, "frozen", False):
    SCRIPT_DIR = os.path.dirname(sys.executable)
else:
    SCRIPT_DIR = os.path.dirname(os.path.abspath(__file__))

INI_PATH          = os.path.join(SCRIPT_DIR, "UWONavi.ini")
STATE_PATH        = os.path.join(SCRIPT_DIR, "UWONavi.state")
NUMS_BMP_PATH     = os.path.join(SCRIPT_DIR, "nums.bmp")

GAME_WINDOW_TITLE = "Uncharted Waters Online"

# nums.bmp sprite dimensions (fixed)
SPRITE_W = 6
SPRITE_H = 11

# Coordinate display placement within the game client.
# Based on calibration across multiple resolutions, the OCR strip is anchored
# to fixed client-edge margins rather than resolution-relative percentages.
COORD_RIGHT_MARGIN  = 12    # capture strip right edge is 12px from client right
COORD_BOTTOM_MARGIN = 273   # capture strip top edge is 273px above client bottom

# Capture dimensions are fixed pixel sizes — always exactly 10 chars × 6px wide,
# and SPRITE_H tall, regardless of resolution.
CAPTURE_W = SPRITE_W * 10   # 60px: 5 lon digits + comma + 4 lat digits
CAPTURE_H = SPRITE_H        # 11px: exact sprite height

# In-game world coordinate bounds
# Derived from map pixel measurements: 1 game unit = 0.25 pixels on the 4096x2048 map
# Lon: 0-16383 (wraps), each 1000 units = 250px  → 16384 / 0.25 = 4096px ✓
# Lat: 0-8191,  each 1000 units = 250px          → 8192  / 0.25 = 2048px ✓
MAP_COORD_W = 16384
MAP_COORD_H = 8192


# ─── Config ───────────────────────────────────────────────────────────────────

def load_config():
    cfg = configparser.ConfigParser()

    # Built-in defaults — used if the INI is missing or a key is absent
    cfg.read_dict({
        "Window":   {"resolutionx": "1600", "resolutiony": "1024"},
        "Map":      {"file": "map.png"},
        "Hotkeys":  {"recenter": "r", "clear_track": "c", "relock": "l"},
        "Display":  {"show_coords": "true", "show_status": "false",
                     "debug_capture": "false"},
        "Behaviour": {
            "auto_center_first":       "true",
            "poll_interval":           "1.0",
            "track_max_points":        "2000",
            "ocr_noise_threshold":     "0.35",
            "direction_vector_length":  "18500",
            "direction_history_size":   "6",
        },
        "OfflineTesting": {
            "initial_c_coord": "",
            "initial_p_coord": "",
            "initial_r_coord": "",
        },
    })

    cfg.read(INI_PATH)
    return cfg


# ─── OCR Engine ───────────────────────────────────────────────────────────────

class DigitOCR:
    """
    Matches screen digit regions against sprites from nums.bmp.

    Coordinate format (right-justified in capture region):
        [  empty space  ][ lon digits ][ , ][ 4 lat digits ]

    Reads right-to-left:
      slots 0–3 = latitude  (4 fixed digits)
      slot  4   = comma     (skipped)
      slots 5–9 = longitude (1–5 digits, stops when background is hit)

    Digits render at native 6px width regardless of game resolution.
    """

    BRIGHT_THRESHOLD = 200   # raised: only clearly bright pixels count as white

    def __init__(self, noise_threshold=0.20):
        self.noise_threshold = noise_threshold

        img = Image.open(NUMS_BMP_PATH).convert("RGB")
        self.sprites = []

        for d in range(10):
            x0   = d * SPRITE_W
            crop = img.crop((x0, 0, x0 + SPRITE_W, SPRITE_H))
            px   = crop.load()
            pat  = []
            for y in range(SPRITE_H):
                for x in range(SPRITE_W):
                    r, g, b = px[x, y]
                    if r == 255 and g == 0 and b == 0:
                        pat.append(None)   # red = sprite background, ignore
                    elif (r + g + b) / 3 < 50:
                        pat.append(0)      # black = digit outline
                    else:
                        pat.append(1)      # white = digit fill
            self.sprites.append(pat)

    def _match_region(self, region_img):
        """
        Returns (digit, score). Score 0.0 = perfect match, 1.0 = worst.
        Region is exactly SPRITE_W × SPRITE_H — no crop or resize needed.
        """
        rpx = region_img.convert("RGB").load()

        region_pat = []
        for y in range(SPRITE_H):
            for x in range(SPRITE_W):
                r, g, b = rpx[x, y]
                brightness = (r + g + b) / 3
                if brightness > 200:
                    region_pat.append(1)
                elif brightness < 40:
                    region_pat.append(0)
                else:
                    region_pat.append(-1)

        best_digit, best_score = -1, float("inf")
        for d, sprite_pat in enumerate(self.sprites):
            misses = care = 0
            for i, expected in enumerate(sprite_pat):
                if expected is None:
                    continue
                care += 1
                if region_pat[i] != expected:
                    misses += 1
            score = misses / care if care > 0 else 1.0
            if score < best_score:
                best_score = score
                best_digit = d

        return best_digit, best_score

    def read_coordinates(self, screen_img):
        """
        Returns (longitude, latitude) or None if the coordinate display
        is absent or the read is too noisy to trust.

        The capture is exactly 60px wide (10 chars × 6px), right-aligned
        to the coordinate display. The comma is always at x=30 (fixed):

          x=00-06  lon digit 5 (leftmost, may be empty)
          x=06-12  lon digit 4
          x=12-18  lon digit 3
          x=18-24  lon digit 2
          x=24-30  lon digit 1 (rightmost)
          x=30-36  comma (skipped)
          x=36-42  lat digit 1
          x=42-48  lat digit 2
          x=48-54  lat digit 3
          x=54-60  lat digit 4 (rightmost)
        """
        img_w, img_h = screen_img.size
        COMMA_X = SPRITE_W * 5   # always 30px from left

        # ── Latitude: 4 fixed digits to the right of comma ────────────
        lat_digits  = []
        total_score = 0.0
        for n in range(1, 5):
            x0 = COMMA_X + SPRITE_W * n
            x1 = x0 + SPRITE_W
            if x1 > img_w:
                return None
            digit, score = self._match_region(screen_img.crop((x0, 0, x1, img_h)))
            if score > self.noise_threshold:
                return None
            lat_digits.append(digit)
            total_score += score

        # ── Longitude: 1-5 digits to the left of comma ────────────────
        lon_digits = []
        for n in range(1, 6):
            x1 = COMMA_X - SPRITE_W * (n - 1)
            x0 = x1 - SPRITE_W
            if x0 < 0:
                break
            digit, score = self._match_region(screen_img.crop((x0, 0, x1, img_h)))
            if score > self.noise_threshold:
                break
            lon_digits.append(digit)
            total_score += score

        if not lon_digits:
            return None

        # Average score gate — rejects noise that squeaks past per-digit threshold
        if total_score / (4 + len(lon_digits)) > 0.15:
            return None

        lat_str = "".join(str(d) for d in lat_digits)
        lon_str = "".join(str(d) for d in reversed(lon_digits))

        try:
            lon = int(lon_str)
            lat = int(lat_str)
        except ValueError:
            return None

        if not (0 <= lon <= MAP_COORD_W) or not (0 <= lat <= MAP_COORD_H):
            return None

        return lon, lat


# ─── Window & Screen Capture ──────────────────────────────────────────────────

class GameWindow:
    """Locates GVO windows and manages locking to a specific instance."""

    def __init__(self):
        self.locked_hwnd = None   # None = follow foreground, int = locked instance

    def find_all_gvo_windows(self):
        """Return list of all hwnds whose title contains GAME_WINDOW_TITLE."""
        results = []
        def cb(hwnd, _):
            try:
                if GAME_WINDOW_TITLE.lower() in win32gui.GetWindowText(hwnd).lower():
                    if win32gui.IsWindowVisible(hwnd):
                        results.append(hwnd)
            except Exception:
                pass
        win32gui.EnumWindows(cb, None)
        return results

    def lock_to(self, hwnd):
        self.locked_hwnd = hwnd

    def unlock(self):
        self.locked_hwnd = None

    def get_hwnd(self):
        """
        Return the hwnd to read from, or None if unavailable.
        If locked, return the locked hwnd (verifying it still exists).
        If unlocked, return the foreground hwnd if it's a GVO window.
        """
        if not WINDOWS:
            return None
        if self.locked_hwnd is not None:
            try:
                if win32gui.IsWindow(self.locked_hwnd):
                    return self.locked_hwnd
            except Exception:
                pass
            self.locked_hwnd = None   # window gone, fall through
        try:
            hwnd  = win32gui.GetForegroundWindow()
            title = win32gui.GetWindowText(hwnd)
            if GAME_WINDOW_TITLE.lower() in title.lower():
                return hwnd
        except Exception:
            pass
        return None

    def get_client_size(self, hwnd):
        """Return (client_width, client_height) for the target game window."""
        if not WINDOWS or hwnd is None:
            return None
        try:
            left, top, right, bottom = win32gui.GetClientRect(hwnd)
            return right - left, bottom - top
        except Exception:
            return None

    def get_capture_region(self, hwnd):
        """
        Return (client_x, client_y, width, height) of the coordinate display.
        Width and height are fixed pixel sizes (60×11).
        Placement is derived from fixed client-edge margins.
        """
        size = self.get_client_size(hwnd)
        if size is None:
            return None

        client_w, client_h = size
        right = client_w - COORD_RIGHT_MARGIN
        x     = right - CAPTURE_W
        y     = client_h - COORD_BOTTOM_MARGIN

        if x < 0 or y < 0 or x + CAPTURE_W > client_w or y + CAPTURE_H > client_h:
            return None

        return x, y, CAPTURE_W, CAPTURE_H


class ScreenCapture:
    """Captures a client-relative region using GDI BitBlt (works with DirectX)."""

    def capture(self, hwnd, client_x, client_y, w, h):
        hwnd_dc  = win32gui.GetDC(hwnd)
        mfc_dc   = win32ui.CreateDCFromHandle(hwnd_dc)
        save_dc  = mfc_dc.CreateCompatibleDC()

        bmp = win32ui.CreateBitmap()
        bmp.CreateCompatibleBitmap(mfc_dc, w, h)
        save_dc.SelectObject(bmp)
        save_dc.BitBlt((0, 0), (w, h), mfc_dc, (client_x, client_y), win32con.SRCCOPY)

        bmp_info = bmp.GetInfo()
        bmp_bits = bmp.GetBitmapBits(True)
        img = Image.frombuffer(
            "RGB",
            (bmp_info["bmWidth"], bmp_info["bmHeight"]),
            bmp_bits, "raw", "BGRX", 0, 1
        )

        win32gui.DeleteObject(bmp.GetHandle())
        save_dc.DeleteDC()
        mfc_dc.DeleteDC()
        win32gui.ReleaseDC(hwnd, hwnd_dc)

        return img


# ─── Map Widget ───────────────────────────────────────────────────────────────

class MapWidget(tk.Canvas):
    """
    World map canvas:
    - Seamless horizontal tiling (longitude wraps 16383 → 0)
    - Player dot and course history line
    - Mouse-drag to pan, scroll wheel to zoom in
    - Auto-centers on first position fix only
    """

    DOT_RADIUS     = 5
    DOT_COLOR      = "#ff3333"
    DOT_OUTLINE    = "#ffffff"
    TRACK_COLOR    = "#44aaff"
    TRACK_WIDTH    = 2
    VECTOR_COLOR   = "#ffff00"
    VECTOR_WIDTH   = 1
    WRAP_JUMP_FRAC = 0.4
    ZOOM_STEP      = 1.15
    MAX_ZOOM       = 16.0

    def __init__(self, parent, map_image, auto_center_first, track_max,
                 vector_length=300, dir_history_size=6, **kwargs):
        super().__init__(parent, bg="#0a0a1a", highlightthickness=0, **kwargs)
        self.map_image         = map_image
        self.map_w, self.map_h = map_image.size
        self.auto_center_first = auto_center_first
        self.track_max         = track_max
        self.vector_length     = vector_length
        self.dir_history_size  = dir_history_size
        self.track             = []
        self.pos               = None
        self.c_coord           = None   # current polled coord
        self.p_coord           = None   # previous polled coord
        self.r_coord           = None   # reference coord (last distinct position)
        self.dir_history       = []     # last N distinct coords for angle averaging
        self.offset_x          = 0.0
        self.offset_y          = 0.0
        self.zoom              = 1.0
        self.view_w            = 800
        self.view_h            = 500
        self._photo            = None
        self._drag_x           = None
        self._drag_y           = None
        self._first_pos        = True

        self.bind("<Configure>",     self._on_resize)
        self.bind("<ButtonPress-1>", self._on_drag_start)
        self.bind("<B1-Motion>",     self._on_drag)
        self.bind("<MouseWheel>",    self._on_zoom)   # Windows
        self.bind("<Button-4>",      self._on_zoom)   # Linux scroll up
        self.bind("<Button-5>",      self._on_zoom)   # Linux scroll down

    # ── Events ────────────────────────────────────────────────────────────

    def _on_resize(self, event):
        self.view_w = event.width
        self.view_h = event.height
        self.redraw()

    def _on_drag_start(self, event):
        self._drag_x = event.x
        self._drag_y = event.y

    def _on_drag(self, event):
        if self._drag_x is None:
            return
        scale        = self._scale()
        dx           = (event.x - self._drag_x) / scale
        dy           = (event.y - self._drag_y) / scale
        self.offset_x = (self.offset_x - dx) % self.map_w
        self.offset_y = self._clamp_offset_y(self.offset_y - dy)
        self._drag_x = event.x
        self._drag_y = event.y
        self.redraw()

    def _on_zoom(self, event):
        if event.num == 4 or event.delta > 0:
            factor = self.ZOOM_STEP
        else:
            factor = 1.0 / self.ZOOM_STEP

        new_zoom = max(1.0, min(self.MAX_ZOOM, self.zoom * factor))
        if new_zoom == self.zoom:
            return

        old_scale = self._scale()
        self.zoom  = new_zoom
        new_scale  = self._scale()

        # Keep map point under cursor fixed
        mx = self.offset_x + event.x / old_scale
        my = self.offset_y + event.y / old_scale
        self.offset_x = (mx - event.x / new_scale) % self.map_w
        self.offset_y = self._clamp_offset_y(my - event.y / new_scale)
        self.redraw()

    # ── Helpers ───────────────────────────────────────────────────────────

    def _scale(self):
        return (self.view_h / self.map_h) * self.zoom

    def _clamp_offset_y(self, y):
        max_y = max(0.0, self.map_h - self.view_h / self._scale())
        return max(0.0, min(max_y, y))

    def _lon_to_cx(self, lon):
        scale = self._scale()
        map_x = (lon / MAP_COORD_W) * self.map_w
        return ((map_x - self.offset_x) % self.map_w) * scale

    def _lat_to_cy(self, lat):
        scale = self._scale()
        map_y = (lat / MAP_COORD_H) * self.map_h
        return (map_y - self.offset_y) * scale

    def _center_on(self, lon, lat):
        scale         = self._scale()
        map_x         = (lon / MAP_COORD_W) * self.map_w
        map_y         = (lat / MAP_COORD_H) * self.map_h
        self.offset_x = (map_x - (self.view_w / scale) / 2) % self.map_w
        self.offset_y = self._clamp_offset_y(map_y - (self.view_h / scale) / 2)

    # ── Public API ────────────────────────────────────────────────────────

    def update_position(self, lon, lat):
        c = (lon, lat)

        if self.c_coord is None:
            # First fix — initialise all three, no ray yet
            self.c_coord = c
            self.p_coord = c
            self.r_coord = c
            self.dir_history = [c]
        else:
            if c != self.p_coord:
                # Ship moved — rCoord saves old pCoord before advancing
                self.r_coord = self.p_coord
                self.p_coord = self.c_coord
                # Keep last 4 distinct positions for angle averaging
                self.dir_history.append(c)
                if len(self.dir_history) > self.dir_history_size:
                    self.dir_history.pop(0)
            self.c_coord = c

        self.pos = (lon, lat)
        # Only record track points when the ship actually moves
        if self.c_coord is None or c != self.p_coord:
            self.track.append((lon, lat))
            if len(self.track) > self.track_max:
                self.track.pop(0)
        if self._first_pos and self.auto_center_first:
            self._center_on(lon, lat)
            self._first_pos = False
        self.redraw()

    def recenter(self):
        if self.pos:
            self._center_on(self.pos[0], self.pos[1])
            self.redraw()

    def clear_track(self):
        self.track.clear()
        self.redraw()

    # ── Drawing ───────────────────────────────────────────────────────────

    def redraw(self):
        self.delete("all")
        self._draw_map()
        self._draw_track()
        self._draw_vector()
        self._draw_dot()

    def _draw_vector(self):
        """
        Direction ray intersected with viewport rectangle.

        All math done in map pixels. Steps:
          1. Compute direction in map pixels
          2. Compute viewport in map pixels
          3. Two transforms of viewport (for seam handling)
          4. Keep the transform on the correct side of the ray
          5. Intersect ray with kept viewport edges
          6. Check if ship is encased in viewport
          7. Map intercepts to canvas pixels via edge percentage
          8. Draw
        """
        if self.c_coord is None:
            return
        if self.c_coord == self.p_coord == self.r_coord:
            return

        cur_lon, cur_lat = self.c_coord
        if self.c_coord != self.p_coord:
            ref_lon, ref_lat = self.p_coord
        else:
            ref_lon, ref_lat = self.r_coord

        # ── Step 1: Direction in map pixels ───────────────────────────
        if len(self.dir_history) >= 2:
            n        = len(self.dir_history)
            ts       = list(range(n))
            t_mean   = (n - 1) / 2.0
            ss_t     = sum((t - t_mean) ** 2 for t in ts)
            lons     = []
            base_lon = self.dir_history[0][0]
            for lon_i, _ in self.dir_history:
                d = lon_i - base_lon
                if d > MAP_COORD_W / 2:    d -= MAP_COORD_W
                elif d < -MAP_COORD_W / 2: d += MAP_COORD_W
                lons.append(base_lon + d)
            lats     = [lat_i for _, lat_i in self.dir_history]
            lon_mean = sum(lons) / n
            lat_mean = sum(lats) / n
            dlon_gc  = sum((ts[i] - t_mean) * (lons[i] - lon_mean) for i in range(n)) / ss_t
            dlat_gc  = sum((ts[i] - t_mean) * (lats[i] - lat_mean) for i in range(n)) / ss_t
        else:
            dlon_gc = cur_lon - ref_lon
            if abs(dlon_gc) > MAP_COORD_W / 2:
                dlon_gc -= math.copysign(MAP_COORD_W, dlon_gc)
            dlat_gc = cur_lat - ref_lat

        if dlon_gc == 0 and dlat_gc == 0:
            return

        # Convert direction to map pixels
        dx = (dlon_gc / MAP_COORD_W) * self.map_w
        dy = (dlat_gc / MAP_COORD_H) * self.map_h

        # ── Step 2: Viewport in map pixels ────────────────────────────
        scale = self._scale()
        vl = self.offset_x
        vr = self.offset_x + self.view_w / scale
        vt = self.offset_y
        vb = self.offset_y + self.view_h / scale

        # ── Step 3: Ship in map pixels, two viewport transforms ───────
        sx = (cur_lon / MAP_COORD_W) * self.map_w
        sy = (cur_lat / MAP_COORD_H) * self.map_h

        xt = -sx
        yt = -sy

        # vpmt1: standard transform
        vl1 = vl + xt;  vr1 = vr + xt
        vt1 = vt + yt;  vb1 = vb + yt

        # vpmt2: shifted one map width to the left
        vl2 = vl + xt - self.map_w;  vr2 = vr + xt - self.map_w
        vt2 = vt1;                    vb2 = vb1

        # vpmt3: shifted one map width to the right
        vl3 = vl + xt + self.map_w;  vr3 = vr + xt + self.map_w
        vt3 = vt1;                    vb3 = vb1

        all_vpts = [
            (vl1, vr1, vt1, vb1),
            (vl2, vr2, vt2, vb2),
            (vl3, vr3, vt3, vb3),
        ]

        # ── Step 4: Keep viewport on correct side of ray ──────────────
        # Prefer the transform that encases the ship (vl<=0<=vr).
        # Otherwise pick the closest valid one in the ray direction.
        if dx >= 0:
            candidates = [v for v in all_vpts if v[1] > 0]   # vr > 0
        else:
            candidates = [v for v in all_vpts if v[0] < 0]   # vl < 0

        if not candidates:
            return

        # Prefer encasing transform; otherwise closest to origin
        encasing = [v for v in candidates if v[0] <= 0 <= v[1]]
        if encasing:
            vpt = encasing[0]
        else:
            if dx >= 0:
                vpt = min(candidates, key=lambda v: v[0])  # smallest vl (closest left edge)
            else:
                vpt = max(candidates, key=lambda v: v[1])  # largest vr (closest right edge)

        tvl, tvr, tvt, tvb = vpt

        # ── Step 5: Intersect ray P(t)=(t*dx, t*dy), t>=0 with edges ─
        # For each edge, compute t and the intersection point.
        # Only keep intersections where t>=0 and point lies on the edge.
        EPS = 1e-9
        hits = []   # each hit: (t, 'edge_name', param) where param is
                    # fraction along that edge [0..1] for canvas mapping

        def check_vertical(t, x, y, edge):
            if t >= 0 and tvt - EPS <= y <= tvb + EPS:
                frac = (y - tvt) / (tvb - tvt) if (tvb - tvt) != 0 else 0
                hits.append((t, edge, frac))

        def check_horizontal(t, x, y, edge):
            if t >= 0 and tvl - EPS <= x <= tvr + EPS:
                frac = (x - tvl) / (tvr - tvl) if (tvr - tvl) != 0 else 0
                hits.append((t, edge, frac))

        if dx != 0:
            t = tvl / dx;  check_vertical  (t, tvl, t * dy, 'left')
            t = tvr / dx;  check_vertical  (t, tvr, t * dy, 'right')
        if dy != 0:
            t = tvt / dy;  check_horizontal(t, t * dx, tvt, 'top')
            t = tvb / dy;  check_horizontal(t, t * dx, tvb, 'bottom')

        # Deduplicate corner hits (same t within tolerance)
        unique_hits = []
        for h in hits:
            if not any(abs(h[0] - u[0]) < EPS for u in unique_hits):
                unique_hits.append(h)
        hits = sorted(unique_hits, key=lambda h: h[0])

        if not hits:
            return

        # ── Step 6: Is ship encased in viewport? ──────────────────────
        encased = tvl <= 0 <= tvr and tvt <= 0 <= tvb
        print(f"DEBUG: dx={dx:.3f} dy={dy:.3f} tvl={tvl:.1f} tvr={tvr:.1f} tvt={tvt:.1f} tvb={tvb:.1f} encased={encased} hits={len(hits)}")

        # ── Step 7: Map intercepts to canvas pixels ───────────────────
        def hit_to_canvas(hit):
            _, edge, frac = hit
            if edge == 'left':
                return (0, frac * self.view_h)
            elif edge == 'right':
                return (self.view_w, frac * self.view_h)
            elif edge == 'top':
                return (frac * self.view_w, 0)
            else:  # bottom
                return (frac * self.view_w, self.view_h)

        # ── Step 8: Draw ──────────────────────────────────────────────
        if encased:
            # Ship inside viewport: draw from ship canvas pos to the 1 hit
            ship_cx = self._lon_to_cx(cur_lon)
            ship_cy = self._lat_to_cy(cur_lat)
            ex, ey  = hit_to_canvas(hits[-1])  # furthest hit
            self.create_line(ship_cx, ship_cy, ex, ey,
                             fill=self.VECTOR_COLOR, width=self.VECTOR_WIDTH,
                             arrow=tk.LAST, arrowshape=(8, 10, 3))
        else:
            if len(hits) < 2:
                return  # corner hit only — skip
            ax, ay = hit_to_canvas(hits[0])
            bx, by = hit_to_canvas(hits[-1])
            self.create_line(ax, ay, bx, by,
                             fill=self.VECTOR_COLOR, width=self.VECTOR_WIDTH,
                             arrow=tk.LAST, arrowshape=(8, 10, 3))



    @staticmethod
    def _clip_line(x0, y0, x1, y1, xmin, ymin, xmax, ymax):
        """
        Cohen-Sutherland line clipping. Returns clipped (x0,y0,x1,y1) or None
        if the segment is entirely outside the rectangle.
        """
        INSIDE, LEFT, RIGHT, BOTTOM, TOP = 0, 1, 2, 4, 8

        def code(x, y):
            c = INSIDE
            if x < xmin:   c |= LEFT
            elif x > xmax: c |= RIGHT
            if y < ymin:   c |= TOP
            elif y > ymax: c |= BOTTOM
            return c

        c0, c1 = code(x0, y0), code(x1, y1)

        while True:
            if not (c0 | c1):       # both inside
                return x0, y0, x1, y1
            if c0 & c1:             # both outside same region
                return None
            # Pick the point outside
            c = c0 if c0 else c1
            if c & BOTTOM:
                x = x0 + (x1 - x0) * (ymax - y0) / (y1 - y0)
                y = ymax
            elif c & TOP:
                x = x0 + (x1 - x0) * (ymin - y0) / (y1 - y0)
                y = ymin
            elif c & RIGHT:
                y = y0 + (y1 - y0) * (xmax - x0) / (x1 - x0)
                x = xmax
            else:  # LEFT
                y = y0 + (y1 - y0) * (xmin - x0) / (x1 - x0)
                x = xmin
            if c == c0:
                x0, y0, c0 = x, y, code(x, y)
            else:
                x1, y1, c1 = x, y, code(x, y)


    def _draw_map(self):
        scale = self._scale()
        src_w = int(self.view_w / scale)
        src_h = int(self.view_h / scale)
        ox    = int(self.offset_x) % self.map_w
        oy    = int(self.offset_y)
        oy2   = min(oy + src_h, self.map_h)

        if ox + src_w <= self.map_w:
            strip = self.map_image.crop((ox, oy, ox + src_w, oy2))
        else:
            right = self.map_image.crop((ox, oy, self.map_w, oy2))
            left  = self.map_image.crop((0, oy, (ox + src_w) - self.map_w, oy2))
            strip = Image.new("RGB", (src_w, oy2 - oy))
            strip.paste(right, (0, 0))
            strip.paste(left, (right.width, 0))

        rendered    = strip.resize((self.view_w, self.view_h), Image.LANCZOS)
        self._photo = ImageTk.PhotoImage(rendered)
        self.create_image(0, 0, anchor=tk.NW, image=self._photo)

    def _draw_track(self):
        if len(self.track) < 2:
            return
        points = [(self._lon_to_cx(lon), self._lat_to_cy(lat))
                  for lon, lat in self.track]
        for i in range(1, len(points)):
            x0, y0 = points[i - 1]
            x1, y1 = points[i]
            if abs(x1 - x0) > self.view_w * self.WRAP_JUMP_FRAC:
                continue
            self.create_line(x0, y0, x1, y1,
                             fill=self.TRACK_COLOR, width=self.TRACK_WIDTH)

    def _draw_dot(self):
        if not self.pos:
            return
        cx = self._lon_to_cx(self.pos[0])
        cy = self._lat_to_cy(self.pos[1])
        r  = self.DOT_RADIUS
        self.create_oval(cx - r, cy - r, cx + r, cy + r,
                         fill=self.DOT_COLOR, outline=self.DOT_OUTLINE, width=2)


# ─── Lock Dialog ──────────────────────────────────────────────────────────────

class LockDialog:
    """
    Topmost dialog that polls GetForegroundWindow until the user clicks
    a GVO instance, then calls on_locked(hwnd) and closes itself.
    """

    def __init__(self, parent, game_window, on_locked):
        self.game_window = game_window
        self.on_locked   = on_locked
        self._cancelled  = False

        self.win = tk.Toplevel(parent)
        self.win.title("Select GVO Instance")
        self.win.attributes("-topmost", True)
        self.win.resizable(False, False)
        self.win.protocol("WM_DELETE_WINDOW", self._cancel)

        tk.Label(self.win,
                 text="Please click on the GVO instance\nyou want UWONavi to track.",
                 font=("Segoe UI", 11), padx=24, pady=16).pack()
        tk.Button(self.win, text="Cancel", command=self._cancel,
                  padx=12, pady=4).pack(pady=(0, 12))

        # Centre over parent
        self.win.update_idletasks()
        px = parent.winfo_x() + parent.winfo_width()  // 2 - self.win.winfo_width()  // 2
        py = parent.winfo_y() + parent.winfo_height() // 2 - self.win.winfo_height() // 2
        self.win.geometry(f"+{px}+{py}")

        self._poll()

    def _poll(self):
        if self._cancelled:
            return
        gvo_windows = self.game_window.find_all_gvo_windows()
        if gvo_windows:
            try:
                fg = win32gui.GetForegroundWindow()
                if fg in gvo_windows:
                    self.win.destroy()
                    self.on_locked(fg)
                    return
            except Exception:
                pass
        self.win.after(250, self._poll)

    def _cancel(self):
        self._cancelled = True
        self.win.destroy()




class UWONavi:

    def __init__(self, root):
        self.root = root
        self.root.title("UWONavi")
        self.root.configure(bg="#0a0a1a")

        # Load config
        cfg = load_config()

        map_file   = cfg.get       ("Map",       "file")
        hk_recen   = cfg.get       ("Hotkeys",   "recenter").lower()
        hk_clear   = cfg.get       ("Hotkeys",   "clear_track").lower()
        hk_lock    = cfg.get       ("Hotkeys",   "relock").lower()
        show_coords  = cfg.getboolean("Display",   "show_coords")
        show_status  = cfg.getboolean("Display",   "show_status")
        debug_capture= cfg.getboolean("Display",   "debug_capture")
        auto_ctr   = cfg.getboolean("Behaviour", "auto_center_first")
        poll_int   = cfg.getfloat  ("Behaviour", "poll_interval")
        track_max  = cfg.getint    ("Behaviour", "track_max_points")
        noise_thr  = cfg.getfloat  ("Behaviour", "ocr_noise_threshold")
        vec_len    = cfg.getint    ("Behaviour", "direction_vector_length")
        dir_hist   = cfg.getint    ("Behaviour", "direction_history_size")

        init_c     = self._parse_coord(cfg.get("OfflineTesting", "initial_c_coord"))
        init_p     = self._parse_coord(cfg.get("OfflineTesting", "initial_p_coord"))
        init_r     = self._parse_coord(cfg.get("OfflineTesting", "initial_r_coord"))

        self.poll_interval = poll_int
        self.debug_capture = debug_capture
        self.speed_window  = 4.0       # seconds for moving average
        self.pos_history   = []        # list of (time, lon, lat)
        self.speed         = 0.0
        self.running       = False

        self.game    = GameWindow()
        self.capture = ScreenCapture()
        self.ocr     = None

        # ── Restore saved window geometry ──────────────────────────────────
        geo = self._load_state().get("geometry", "1000x600")
        try:
            root.geometry(geo)
        except Exception:
            root.geometry("1000x600")

        root.bind("<Configure>", self._on_window_configure)

        # ── UI ────────────────────────────────────────────────────────────
        self._build_ui(auto_ctr, track_max, vec_len, dir_hist, show_coords, show_status)
        self._bind_hotkeys(hk_recen, hk_clear, hk_lock)

        # ── Load assets ───────────────────────────────────────────────────
        error = self._load_assets(map_file, noise_thr)
        if error:
            self._set_status(error)
            return

        # ── Apply offline testing coords (if set in INI) ──────────────────
        # Only populate fields that haven't already been set by a real poll.
        # All three must be provided together to be meaningful.
        if init_c and init_p and init_r:
            self.map_widget.c_coord = init_c
            self.map_widget.p_coord = init_p
            self.map_widget.r_coord = init_r
            self.map_widget.pos     = init_c
            if auto_ctr:
                self.map_widget._center_on(init_c[0], init_c[1])
            self.map_widget.redraw()

        # ── Start polling immediately ──────────────────────────────────────
        self.running = True
        threading.Thread(target=self._poll_loop, daemon=True).start()
        self._set_status(
            f"Running — {map_file}  |  "
            f"[{hk_recen.upper()}] re-center  "
            f"[{hk_clear.upper()}] clear track  "
            f"[{hk_lock.upper()}] re-lock"
        )

        # ── Auto-lock on startup ───────────────────────────────────────────
        self.root.after(100, self._auto_lock)

    @staticmethod
    def _parse_coord(value):
        """
        Parse a 'lon,lat' string from the INI into a (lon, lat) tuple.
        Returns None if the value is blank or malformed.
        """
        value = value.strip()
        if not value:
            return None
        try:
            parts = value.split(",")
            if len(parts) != 2:
                return None
            return (int(parts[0].strip()), int(parts[1].strip()))
        except ValueError:
            return None

    # ── Asset loading ─────────────────────────────────────────────────────

    def _load_assets(self, map_file, noise_thr):
        if not os.path.exists(NUMS_BMP_PATH):
            return "ERROR: nums.bmp not found next to UWONavi.py"
        try:
            self.ocr = DigitOCR(noise_threshold=noise_thr)
        except Exception as e:
            return f"ERROR loading nums.bmp: {e}"

        map_path = os.path.join(SCRIPT_DIR, map_file)
        if not os.path.exists(map_path):
            return f"ERROR: map file '{map_file}' not found (check UWONavi.ini)"
        try:
            img = Image.open(map_path).convert("RGB")
            self.map_widget.map_image  = img
            self.map_widget.map_w, self.map_widget.map_h = img.size
            self.map_widget.redraw()
        except Exception as e:
            return f"ERROR loading map: {e}"

        return None   # success

    # ── UI construction ───────────────────────────────────────────────────

    def _build_ui(self, auto_ctr, track_max, vec_len, dir_hist, show_coords, show_status):
        # Status bar (optional, default off)
        self.status_var = tk.StringVar(value="Initialising…")
        if show_status:
            tk.Label(self.root, textvariable=self.status_var,
                     bg="#070d1a", fg="#8899bb", anchor=tk.W,
                     padx=8, font=("Segoe UI", 8)
                     ).pack(side=tk.BOTTOM, fill=tk.X)

        # Coordinate / speed bar (optional, default on)
        if show_coords:
            info = tk.Frame(self.root, bg="#0a1628", pady=3)
            info.pack(side=tk.BOTTOM, fill=tk.X)

            self.coord_var = tk.StringVar(value="Lon: -----   Lat: ----")
            self.speed_var = tk.StringVar(value="Speed: --.-- kt")

            tk.Label(info, textvariable=self.coord_var,
                     bg="#0a1628", fg="#c8d8ff",
                     font=("Consolas", 11), padx=16).pack(side=tk.LEFT)
            tk.Label(info, textvariable=self.speed_var,
                     bg="#0a1628", fg="#66ffcc",
                     font=("Consolas", 11), padx=16).pack(side=tk.LEFT)
        else:
            self.coord_var = tk.StringVar()
            self.speed_var = tk.StringVar()

        # Map canvas (placeholder until map loads)
        placeholder = Image.new("RGB", (800, 400), (10, 16, 40))
        self.map_widget = MapWidget(
            self.root, placeholder,
            auto_center_first=auto_ctr,
            track_max=track_max,
            vector_length=vec_len,
            dir_history_size=dir_hist,
        )
        self.map_widget.pack(fill=tk.BOTH, expand=True)

    def _bind_hotkeys(self, hk_recen, hk_clear, hk_lock):
        self.root.bind(f"<Key-{hk_recen}>",
                       lambda e: self.map_widget.recenter())
        self.root.bind(f"<Key-{hk_clear}>",
                       lambda e: self.map_widget.clear_track())
        self.root.bind(f"<Key-{hk_lock}>",
                       lambda e: self._start_lock_dialog())

    def _auto_lock(self):
        """On startup: lock immediately if one GVO window, else show dialog."""
        windows = self.game.find_all_gvo_windows()
        if len(windows) == 1:
            self.game.lock_to(windows[0])
            self._set_status(
                f"Locked to: {win32gui.GetWindowText(windows[0])}")
        else:
            self._start_lock_dialog()

    def _start_lock_dialog(self):
        self.game.unlock()
        LockDialog(self.root, self.game, self._on_locked)

    def _on_locked(self, hwnd):
        self.game.lock_to(hwnd)
        try:
            title = win32gui.GetWindowText(hwnd)
            self._set_status(f"Locked to: {title}")
        except Exception:
            pass

    @staticmethod
    def _load_state():
        try:
            with open(STATE_PATH, "r") as f:
                return json.load(f)
        except Exception:
            return {}

    def _on_window_configure(self, event):
        if event.widget is not self.root:
            return
        if hasattr(self, '_geo_save_id'):
            self.root.after_cancel(self._geo_save_id)
        self._geo_save_id = self.root.after(500, self._save_geometry)

    def _save_geometry(self):
        state = self._load_state()
        state["geometry"] = self.root.geometry()
        try:
            with open(STATE_PATH, "w") as f:
                json.dump(state, f, indent=2)
        except Exception:
            pass

    def _set_status(self, msg):
        self.status_var.set(msg)

    # ── Poll loop ─────────────────────────────────────────────────────────

    def _poll_loop(self):
        while self.running:
            try:
                self._poll_once()
            except Exception as e:
                self.root.after(0, self._set_status, f"Poll error: {e}")
            time.sleep(self.poll_interval)

    def _poll_once(self):
        hwnd = self.game.get_hwnd()
        if hwnd is None:
            try:
                fg    = win32gui.GetForegroundWindow()
                title = win32gui.GetWindowText(fg)
                self.root.after(0, self._set_status,
                                f"Waiting for GVO window — foreground: '{title}'")
            except Exception:
                pass
            return

        region = self.game.get_capture_region(hwnd)
        if region is None:
            self.root.after(0, self._set_status,
                            "Unable to determine OCR capture region from game client size")
            return

        x, y, w, h = region
        img         = self.capture.capture(hwnd, x, y, w, h)
        result      = self.ocr.read_coordinates(img)

        if result is None:
            if self.debug_capture:
                try:
                    debug_dir = os.path.join(SCRIPT_DIR, "debug")
                    os.makedirs(debug_dir, exist_ok=True)
                    img.save(os.path.join(debug_dir, "capture.png"))
                    img.resize((w * 10, h * 10), Image.NEAREST).save(
                        os.path.join(debug_dir, "capture_10x.png"))
                except Exception:
                    pass
            return   # coords not visible — silently skip

        lon, lat = result
        now      = time.time()

        # Add to position history and trim to speed_window
        self.pos_history.append((now, lon, lat))
        cutoff = now - self.speed_window
        self.pos_history = [p for p in self.pos_history if p[0] >= cutoff]

        # Moving average speed: total distance / total time over window
        if len(self.pos_history) >= 2:
            total_dist = 0.0
            for i in range(1, len(self.pos_history)):
                _, lon0, lat0 = self.pos_history[i - 1]
                _, lon1, lat1 = self.pos_history[i]
                dlon = lon1 - lon0
                if abs(dlon) > MAP_COORD_W / 2:
                    dlon -= math.copysign(MAP_COORD_W, dlon)
                dlat = lat1 - lat0
                total_dist += math.sqrt(dlon ** 2 + dlat ** 2)
            total_time = self.pos_history[-1][0] - self.pos_history[0][0]
            self.speed = total_dist / total_time if total_time > 0 else 0.0

        self.root.after(0, self._update_ui, lon, lat)

    def _update_ui(self, lon, lat):
        self.coord_var.set(f"Lon: {lon:>5}   Lat: {lat:04d}")
        self.speed_var.set(f"Speed: {self.speed:5.2f} kt")
        self.map_widget.update_position(lon, lat)


# ─── Entry point ──────────────────────────────────────────────────────────────

def main():
    root = tk.Tk()
    root.title("UWONavi")
    root.configure(bg="#0a0a1a")
    UWONavi(root)
    root.mainloop()

if __name__ == "__main__":
    main()
