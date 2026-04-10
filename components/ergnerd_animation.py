"""
components/ergnerd_animation.py

Animated rowing logo for the Erg Nerd app.

Generates a self-contained SMIL-animated SVG of a rower performing a
complete stroke cycle (catch → legs → body → finish → recovery).

Drive phase (catch→finish) is half as long as recovery (finish→catch).
Drive easing: ease-in-cubic (acceleration).
Recovery easing: ease-out-cubic (deceleration into catch).

Usage:
    from components.ergnerd_animation import ergnerd_animation
    ergnerd_animation(width=20)          # HyperDiv component
"""

import base64
import pathlib
import re

import hyperdiv as hd

_ASSETS = pathlib.Path(__file__).parent.parent / "assets"

# ---------------------------------------------------------------------------
# Load head image (base64 data URI) from one of the source SVGs
# ---------------------------------------------------------------------------


def _load_head_uri() -> str:
    path = _ASSETS / "logo_light_bg_catch.svg"
    with open(path) as f:
        content = f.read()
    m = re.search(r'xlink:href="(data:[^"]+)"', content)
    return m.group(1) if m else ""


_HEAD_URI: str = _load_head_uri()

# ---------------------------------------------------------------------------
# SMIL timing constants
# ---------------------------------------------------------------------------
#
# Full cycle = 4 s
#   Drive (catch→finish): 1.333 s  = 3 segments × 0.444 s  → 3/9 of cycle
#   Recovery (finish→catch): 2.667 s = 3 segments × 0.889 s → 6/9 of cycle
#
# keyTimes split: 0, 1/9, 2/9, 3/9, 5/9, 7/9, 1
# (animation visits: catch, legs, body, finish, body, legs, catch)
#
_DUR = "4s"
_KT = "0; 0.1111; 0.2222; 0.3333; 0.5556; 0.7778; 1"
_KS_DRIVE = "0.42 0 1 1"  # ease-in cubic
_KS_RECOV = "0 0 0.58 1"  # ease-out cubic
_KS = f"{_KS_DRIVE}; {_KS_DRIVE}; {_KS_DRIVE}; {_KS_RECOV}; {_KS_RECOV}; {_KS_RECOV}"


# ---------------------------------------------------------------------------
# Flattened keyframe path data [catch, legs, body, finish]
#
# All SVG transforms have been pre-applied in Python so every path is in
# the same global SVG coordinate space and needs no transform attribute.
# ---------------------------------------------------------------------------

_D: dict[str, list[str]] = {
    "Lower-Leg": [
        # catch (rotation pre-applied)
        "M168.0682,214.8441 L167.4935,239.2291"
        " C168.5413,244.5583 169.6259,247.8003 170.7470,248.9554"
        " C172.7932,251.0635 174.1304,255.9298 175.5040,256.5441"
        " C178.0067,257.6633 182.6706,255.1715 189.4960,249.0686"
        " L191.5328,211.0103"
        " C191.3863,208.9556 191.0128,206.9408 190.4123,204.9661"
        " C189.8118,202.9913 188.6968,201.5117 187.0674,200.5272"
        " C181.5277,199.4891 177.1543,200.1784 173.9473,202.5950"
        " C170.7403,205.0117 168.7806,209.0947 168.0682,214.8441 Z",
        # legs / body / finish (same)
        "M153.774375,239.041206 L172.903281,254.175484"
        " C177.789939,256.545771 181.031857,257.630785 182.629035,257.430524"
        " C185.544075,257.065025 190.235221,258.925756 191.552519,258.198395"
        " C193.952456,256.873242 194.769247,251.648808 194.002891,242.525093"
        " L164.833945,217.994331"
        " C163.104804,216.874755 161.270982,215.960529 159.332481,215.251655"
        " C157.393979,214.542782 155.541277,214.542782 153.774375,215.251655"
        " C149.611458,219.051137 147.53,222.95869 147.53,226.974312"
        " C147.53,230.989934 149.611458,235.012232 153.774375,239.041206 Z",
    ],
    "Upper-Leg": [
        # catch (rotation pre-applied)
        "M117.0151,214.8794"
        " C117.8360,225.1252 118.5247,231.5882 119.0813,234.2684"
        " C120.8678,242.8713 123.9849,245.1817 130.8858,247.8886"
        " C135.4864,249.6932 141.9857,249.2459 150.3837,246.5468"
        " L190.5360,221.6116"
        " C191.7798,217.6474 191.4657,213.7462 189.5937,209.9081"
        " C187.7217,206.0699 184.5596,202.8438 180.1073,200.2297"
        " L146.9146,212.8398"
        " C126.3579,206.4140 116.3914,207.0939 117.0151,214.8794 Z",
        # legs / body / finish (same)
        "M90.6425453,200.759332"
        " C86.8888553,210.327982 84.6746736,216.438804 84,219.091797"
        " C81.8344783,227.607195 83.6232793,231.05021 88.6391602,236.508301"
        " C91.9830807,240.147028 98.0206784,242.594131 106.751953,243.849609"
        " L153.771484,239.039551"
        " C156.62717,236.021812 158.055013,232.377769 158.055013,228.107422"
        " C158.055013,223.837075 156.62717,219.551267 153.771484,215.25"
        " L118.410156,212.033203"
        " C102.750774,197.246252 93.4949036,193.488295 90.6425453,200.759332 Z",
    ],
    "Torso": [
        # catch
        "M152.617676,155.503906"
        " C149.19594,154.812672 146.212541,155.079924 143.66748,156.305664"
        " C141.12242,157.531404 138.726098,159.617016 136.478516,162.5625"
        " C126.77356,186.089351 120.494904,201.488295 117.642545,208.759332"
        " C115.140085,215.138432 124.395956,218.896389 145.410156,220.033203"
        " L161.329102,214.091797 L174.5,181.46875"
        " C174.728467,172.020485 173.512856,165.718401 170.853165,162.5625"
        " C168.193474,159.406599 162.114978,157.053734 152.617676,155.503906 Z",
        # legs
        "M125.617676,147.503906"
        " C122.19594,146.812672 119.212541,147.079924 116.66748,148.305664"
        " C114.12242,149.531404 111.726098,151.617016 109.478516,154.5625"
        " C99.7735604,178.089351 93.4949036,193.488295 90.6425453,200.759332"
        " C88.1400853,207.138432 97.3959556,210.896389 118.410156,212.033203"
        " L134.329102,206.091797 L147.5,173.46875"
        " C147.728467,164.020485 146.512856,157.718401 143.853165,154.5625"
        " C141.193474,151.406599 135.114978,149.053734 125.617676,147.503906 Z",
        # body / finish (rotation pre-applied, same for both)
        "M60.1930,159.1623"
        " C57.5024,161.3865 55.8326,163.8732 55.1835,166.6224"
        " C54.5344,169.3717 54.6472,172.5465 55.5218,176.1469"
        " C67.6981,198.4950 75.7141,213.0653 79.5697,219.8578"
        " C82.9523,225.8171 91.6977,220.9888 105.8058,205.3732"
        " L111.2066,189.2628 L94.1425,158.4967"
        " C86.9436,152.3732 81.2809,149.3519 77.1545,149.4328"
        " C73.0281,149.5136 67.3743,152.7568 60.1930,159.1623 Z",
    ],
    "Upper-Arm": [
        # catch
        "M194.448242,167 L152.617676,155.503906"
        " C149.19594,154.812672 146.212541,155.079924 143.66748,156.305664"
        " C141.12242,157.531404 138.726098,159.617016 136.478516,162.5625"
        " C140.098276,168.363802 144.097625,172.35317 148.476562,174.530605"
        " C152.8555,176.708039 166.308299,180.458905 188.834961,185.783203"
        " C191.657422,182.477987 193.528516,179.28658 194.448242,176.208984"
        " C195.367969,173.131388 195.367969,170.061727 194.448242,167 Z",
        # legs
        "M167.448242,159 L125.617676,147.503906"
        " C122.19594,146.812672 119.212541,147.079924 116.66748,148.305664"
        " C114.12242,149.531404 111.726098,151.617016 109.478516,154.5625"
        " C113.098276,160.363802 117.097625,164.35317 121.476562,166.530605"
        " C125.8555,168.708039 139.308299,172.458905 161.834961,177.783203"
        " C164.657422,174.477987 166.528516,171.28658 167.448242,168.208984"
        " C168.367969,165.131388 168.367969,162.061727 167.448242,159 Z",
        # body
        "M119.448242,162 L77.6176758,150.503906"
        " C74.1959395,149.812672 71.2125411,150.079924 68.6674805,151.305664"
        " C66.1224198,152.531404 63.7260982,154.617016 61.4785156,157.5625"
        " C65.0982764,163.363802 69.0976254,167.35317 73.4765625,169.530605"
        " C77.8554996,171.708039 91.3082991,175.458905 113.834961,180.783203"
        " C116.657422,177.477987 118.528516,174.28658 119.448242,171.208984"
        " C120.367969,168.131388 120.367969,165.061727 119.448242,162 Z",
        # finish (rotation pre-applied)
        "M63.7642,208.0319 L74.5285,166.0071"
        " C75.1599,162.5738 74.8406,159.5955 73.5707,157.0722"
        " C72.3007,154.5489 70.1736,152.1894 67.1893,149.9936"
        " C61.4521,153.7140 57.5331,157.7824 55.4324,162.1986"
        " C53.3317,166.6149 49.8162,180.1311 44.8859,202.7473"
        " C48.2399,205.5116 51.4634,207.3267 54.5566,208.1926"
        " C57.6498,209.0585 60.7190,209.0049 63.7642,208.0319 Z",
    ],
    "Lower-Arm": [
        # catch
        "M231.169922,174.5 L194.448242,167"
        " C190.706055,168.333333 187.889974,170.666667 186,174"
        " C184.110026,177.333333 185.055013,181.261068 188.834961,185.783203"
        " L225.438965,193.806152"
        " C231.052897,194.506022 234.685872,192.267578 236.337891,187.09082"
        " C237.989909,181.914063 236.267253,177.717122 231.169922,174.5 Z",
        # legs
        "M204.169922,166.5 L167.448242,159"
        " C163.706055,160.333333 160.889974,162.666667 159,166"
        " C157.110026,169.333333 158.055013,173.261068 161.834961,177.783203"
        " L198.438965,185.806152"
        " C204.052897,186.506022 207.685872,184.267578 209.337891,179.09082"
        " C210.989909,173.914063 209.267253,169.717122 204.169922,166.5 Z",
        # body
        "M156.169922,169.5 L119.448242,162"
        " C115.706055,163.333333 112.889974,165.666667 111,169"
        " C109.110026,172.333333 110.055013,176.261068 113.834961,180.783203"
        " L150.438965,188.806152"
        " C156.052897,189.506022 159.685872,187.267578 161.337891,182.09082"
        " C162.989909,176.914063 161.267253,172.717122 156.169922,169.5 Z",
        # finish (rotation pre-applied)
        "M93.4848,177.2141 L57.9329,189.0798"
        " C55.3588,192.1056 54.0866,195.5343 54.1165,199.3661"
        " C54.1464,203.1978 56.9287,206.1268 62.4633,208.1532"
        " L98.1748,196.7992"
        " C103.3865,194.5984 105.4135,190.8433 104.2558,185.5341"
        " C103.0981,180.2249 99.5078,177.4516 93.4848,177.2141 Z",
    ],
}

# Expand 2-element lists (legs/body/finish all the same) to 4 elements
for _k in ("Lower-Leg", "Upper-Leg"):
    _d = _D[_k]
    _D[_k] = [_d[0], _d[1], _d[1], _d[1]]

# Expand 3-element Torso (body/finish the same)
_D["Torso"] = [_D["Torso"][0], _D["Torso"][1], _D["Torso"][2], _D["Torso"][2]]

# Static paths (no animation)
_D_STATIC = {
    "Erg-Base": (
        "M73.9472656,254.173828 L271.727539,254.173828"
        " L292.887207,211.695801"
        " C297.111531,213.028787 300.613647,213.867492"
        "  303.393555,214.211914"
        " C306.173463,214.556336 309.440715,214.556336"
        "  313.195312,214.211914"
        " L283.90332,273.351074 L73.9472656,273.351074"
        " L73.9472656,254.173828 Z"
    ),
    "Foot": (
        "M172.900391,254.173828 L204.169922,254.173828"
        " C204.758411,251.100724 204.758411,248.766739"
        "  204.169922,247.171875"
        " C203.581432,245.577011 202.221081,244.224309"
        "  200.088867,243.11377"
        " L194,242.523437 L172.900391,254.173828 Z"
    ),
}

# Chain left-x per frame [catch, legs, body, finish]
_CHAIN_LX = [225.68457, 202.631348, 157.68457, 95.6845703]
# Fixed right-x and y coords of the chain rectangle
_CHAIN_RX = 279.684
_CHAIN_Y1, _CHAIN_Y2 = 176.437012, 185.931178
_CHAIN_Y3 = 186.17041

# Erg-Seat x per frame
_SEAT_X = [114, 84, 84, 84]

# Head image [x, y] per frame
_HEAD_XY = [(119, 59), (92, 51), (6, 53), (6, 53)]

# Head rotation (degrees, CCW-positive in SVG) per frame [catch, legs, body, finish]
_HEAD_ROT = [0, 0, -23, -23]
# Pivot point within the 110×110 head image (local coords — near the base/neck)
_HEAD_ROT_CX, _HEAD_ROT_CY = 55, 88

# ---------------------------------------------------------------------------
# Sweat droplet constants
# ---------------------------------------------------------------------------
# Droplets appear during the body-drive phase (keyTimes 0.2222→0.3333, ≈0.444 s)
# and fly off the back (left side) of the head.
# At body phase the head sits at SVG global (6, 53); the left/back edge is x≈6.
#
# Five keyframes: stationary+hidden → fade-in+fly → fade-out → invisible reset
_SWEAT_KT = "0; 0.2222; 0.2778; 0.3333; 1"

# Each row: (start_x, start_y, end_x, end_y, rx, ry, rotation_deg, peak_opacity, fill)
_SWEAT_DROPS = [
    (16, 66, -13, 41, 2.8, 4.8, -45, 0.90, "#3b82f6"),  # upper droplet, richer blue
    (12, 82, -16, 61, 2.2, 3.6, -40, 0.75, "#60a5fa"),  # lower droplet, lighter blue
]

# Letter [x, y] per frame  (tspan positions)
_LETTERS: dict[str, list[tuple[int, int]]] = {
    "N": [(249, 77), (159, 77), (129, 105), (126, 84)],
    "E": [(252, 130), (202, 130), (182, 130), (179, 130)],
    "R": [(281, 155), (241, 151), (230, 155), (230, 162)],
    "G": [(322, 131), (282, 131), (282, 131), (285, 123)],
}


# ---------------------------------------------------------------------------
# SMIL helpers
# ---------------------------------------------------------------------------


def _vals7(v: list) -> str:
    """Expand [v0,v1,v2,v3] → 'v0;v1;v2;v3;v2;v1;v0' (7-value stroke cycle)."""
    v0, v1, v2, v3 = v
    return "; ".join(str(x) for x in [v0, v1, v2, v3, v2, v1, v0])


def _animate_d(elem: str) -> str:
    vals = _D[elem]
    return (
        f'<animate attributeName="d" calcMode="spline" dur="{_DUR}"'
        f' repeatCount="indefinite"'
        f' keyTimes="{_KT}"'
        f' values="{_vals7(vals)}"'
        f' keySplines="{_KS}"/>'
    )


def _animate_attr(attr: str, vals: list) -> str:
    return (
        f'<animate attributeName="{attr}" calcMode="spline" dur="{_DUR}"'
        f' repeatCount="indefinite"'
        f' keyTimes="{_KT}"'
        f' values="{_vals7(vals)}"'
        f' keySplines="{_KS}"/>'
    )


def _chain_pts(lx: float) -> str:
    return (
        f"{_CHAIN_RX} {_CHAIN_Y1} {_CHAIN_RX} {_CHAIN_Y2}"
        f" {lx} {_CHAIN_Y3} {lx} {_CHAIN_Y1}"
    )


def _letter_translate(letter: str) -> str:
    """animateTransform that shifts the letter text element."""
    base_x, base_y = _LETTERS[letter][0]
    offsets = [(x - base_x, y - base_y) for x, y in _LETTERS[letter]]
    o0, o1, o2, o3 = offsets
    formatted = "; ".join(f"{dx},{dy}" for dx, dy in [o0, o1, o2, o3, o2, o1, o0])
    return (
        f'<animateTransform attributeName="transform" type="translate"'
        f' calcMode="spline" dur="{_DUR}" repeatCount="indefinite"'
        f' keyTimes="{_KT}" values="{formatted}"'
        f' keySplines="{_KS}"/>'
    )


def _animate_head_translate() -> str:
    """animateTransform (translate) that moves the head group to the correct position."""
    xy0, xy1, xy2, xy3 = _HEAD_XY
    frames = [xy0, xy1, xy2, xy3, xy2, xy1, xy0]
    formatted = "; ".join(f"{x},{y}" for x, y in frames)
    return (
        f'<animateTransform attributeName="transform" type="translate"'
        f' calcMode="spline" dur="{_DUR}" repeatCount="indefinite"'
        f' keyTimes="{_KT}" values="{formatted}"'
        f' keySplines="{_KS}"/>'
    )


def _animate_head_rotate() -> str:
    """animateTransform (rotate) for head tilt — -23° in body/finish phases."""
    cx, cy = _HEAD_ROT_CX, _HEAD_ROT_CY
    r0, r1, r2, r3 = _HEAD_ROT
    frames = [r0, r1, r2, r3, r2, r1, r0]
    formatted = "; ".join(f"{r} {cx} {cy}" for r in frames)
    return (
        f'<animateTransform attributeName="transform" type="rotate"'
        f' calcMode="spline" dur="{_DUR}" repeatCount="indefinite"'
        f' keyTimes="{_KT}" values="{formatted}"'
        f' keySplines="{_KS}"/>'
    )


def _sweat_drop_svg(sx, sy, ex, ey, rx, ry, rot, peak, color) -> str:
    """
    Return SVG for one animated sweat droplet.

    The droplet is invisible outside the body-drive phase.  It starts at
    (sx, sy), flies to (ex, ey) via the midpoint, fading in then out.
    Rendered as a rotated ellipse to suggest a flying teardrop.
    """
    mid_x = round((sx + ex) / 2, 1)
    mid_y = round((sy + ey) / 2, 1)
    pos_vals = f"{sx},{sy}; {sx},{sy}; {mid_x},{mid_y}; {ex},{ey}; {sx},{sy}"
    opa_vals = f"0; 0; {peak}; 0; 0"
    return (
        f'<g opacity="0">'
        f'<animate attributeName="opacity" calcMode="linear"'
        f' dur="{_DUR}" repeatCount="indefinite"'
        f' keyTimes="{_SWEAT_KT}" values="{opa_vals}"/>'
        f'<animateTransform attributeName="transform" type="translate"'
        f' calcMode="linear" dur="{_DUR}" repeatCount="indefinite"'
        f' keyTimes="{_SWEAT_KT}" values="{pos_vals}"/>'
        f'<ellipse cx="0" cy="0" rx="{rx}" ry="{ry}"'
        f' fill="{color}" transform="rotate({rot})"/>'
        f'</g>'
    )


# ---------------------------------------------------------------------------
# SVG generator
# ---------------------------------------------------------------------------


def build_svg(theme: str = "light") -> str:
    """
    Return a complete animated SVG string for the rowing logo.

    Parameters
    ----------
    theme : "light" | "dark"
        Selects fill colours for elements that differ between light and dark
        backgrounds.
    """
    leg_fill = "#000000" if theme == "light" else "#44BBCA"
    torso_fill = "#FFC200"
    arm_fill = "#FF00CA"
    erg_fill = "#C6C6C6" if theme == "light" else "#888888"
    seat_fill = "#5F6784"
    text_fill = "#000000" if theme == "light" else "#ffffff"
    spoke_stroke = "#888888"
    roller_fill = "#909090" if theme == "light" else "#555555"

    # ---------------------------------------------------------------------------
    # Flywheel geometry (in group-local coords, origin at translate(278,159))
    # The donut forms the "D" in ERG NERD — kept static.
    # Six spokes inside the ring rotate continuously.
    # ---------------------------------------------------------------------------
    _wheel_donut = (
        "M28,0 C43.463973,0 56,12.536027 56,28"
        " C56,43.463973 43.463973,56 28,56"
        " C12.536027,56 0,43.463973 0,28"
        " C0,12.536027 12.536027,0 28,0 Z"
        " M28,11.4074074"
        " C18.8361642,11.4074074 11.4074074,18.8361642 11.4074074,28"
        " C11.4074074,37.1638358 18.8361642,44.5925926 28,44.5925926"
        " C37.1638358,44.5925926 44.5925926,37.1638358 44.5925926,28"
        " C44.5925926,18.8361642 37.1638358,11.4074074 28,11.4074074 Z"
    )
    # Six spokes: inner ring (r≈11.4) to outer ring (r=28) at 60° intervals
    _spokes_svg = "\n".join(
        f'    <line x1="{28 + 11.4074*_c:.2f}" y1="{28 + 11.4074*_s:.2f}"'
        f' x2="{28 + 28*_c:.2f}" y2="{28 + 28*_s:.2f}"/>'
        for _i in range(6)
        for _c, _s in [
            (
                __import__("math").cos(__import__("math").radians(_i * 60)),
                __import__("math").sin(__import__("math").radians(_i * 60)),
            )
        ]
    )

    # ---------------------------------------------------------------------------
    # Chain geometry
    # The right end is fixed at the flywheel (x ≈ 279.684).
    # During drive the roller group translates leftward so links emerge from
    # the flywheel and travel toward the handle, then return on recovery.
    # ---------------------------------------------------------------------------
    _chain_cy = 181.18  # vertical center of chain
    _chain_clip_y, _chain_clip_h = 174.0, 14.0
    # 20 roller positions anchored from the flywheel end (403 → 232, step -9).
    # At catch (translate=0) only the rightmost ~6 fall inside the clip window.
    # At finish (translate=-130) all 20 are visible, emerging from the right.
    _rollers = [403 - 9 * _i for _i in range(20)]
    _rollers_svg = "\n  ".join(
        f'<circle cx="{_x}" cy="{_chain_cy:.2f}" r="3.2"/>' for _x in _rollers
    )
    # Roller-group translateX per frame: mirrors how far left the chain extends.
    # catch=0, legs=-23.053, body=-68.0, finish=-130.0  (then reverses)
    _chain_tx = [round(x - _CHAIN_LX[0], 3) for x in _CHAIN_LX]
    _chain_tx_vals = "; ".join(
        f"{v},0"
        for v in [
            _chain_tx[0],
            _chain_tx[1],
            _chain_tx[2],
            _chain_tx[3],
            _chain_tx[2],
            _chain_tx[1],
            _chain_tx[0],
        ]
    )
    # Chain clip rect animated values (mirrors the body-part keyTimes/splines)
    _clip_x_vals = "; ".join(
        str(round(x, 3))
        for x in [
            _CHAIN_LX[0],
            _CHAIN_LX[1],
            _CHAIN_LX[2],
            _CHAIN_LX[3],
            _CHAIN_LX[2],
            _CHAIN_LX[1],
            _CHAIN_LX[0],
        ]
    )
    _clip_w_vals = "; ".join(
        str(round(_CHAIN_RX - x, 3))
        for x in [
            _CHAIN_LX[0],
            _CHAIN_LX[1],
            _CHAIN_LX[2],
            _CHAIN_LX[3],
            _CHAIN_LX[2],
            _CHAIN_LX[1],
            _CHAIN_LX[0],
        ]
    )

    _sweat_svg = "\n  ".join(_sweat_drop_svg(*d) for d in _SWEAT_DROPS)

    svg = f"""<?xml version="1.0" encoding="UTF-8"?>
<svg xmlns="http://www.w3.org/2000/svg" xmlns:xlink="http://www.w3.org/1999/xlink"
     viewBox="-40 0 428 275" width="428" height="275">

  <defs>
    <!-- clip mask that grows as chain extends during drive -->
    <clipPath id="chain-clip">
      <rect x="{_CHAIN_LX[0]:.3f}" y="{_chain_clip_y}" width="{_CHAIN_RX:.3f}" height="{_chain_clip_h}">
        <!-- <animate attributeName="x" calcMode="spline" dur="{_DUR}"
            repeatCount="indefinite" keyTimes="{_KT}"
            values="{_clip_x_vals}" keySplines="{_KS}"/> -->
        <animate attributeName="width" calcMode="spline" dur="{_DUR}"
            repeatCount="indefinite" keyTimes="{_KT}"
            values="{_clip_w_vals}" keySplines="{_KS}"/>
      </rect>
    </clipPath>
  </defs>

  <!-- lower leg -->
  <path id="Lower-Leg" d="{_D['Lower-Leg'][0]}" fill="{leg_fill}">
    {_animate_d('Lower-Leg')}
  </path>

  <!-- foot (static) -->
  <path id="Foot" d="{_D_STATIC['Foot']}" fill="{leg_fill}"/>

  <!-- upper leg -->
  <path id="Upper-Leg" d="{_D['Upper-Leg'][0]}" fill="{leg_fill}">
    {_animate_d('Upper-Leg')}
  </path>

  <!-- torso -->
  <path id="Torso" d="{_D['Torso'][0]}" fill="{torso_fill}">
    {_animate_d('Torso')}
  </path>

  <!-- chain: background strip + rollers, clipped to visible length -->
  <!-- <rect x="90" y="177" width="192" height="8" rx="4"
        fill="{erg_fill}" clip-path="url(#chain-clip)"/>  -->
  <g id="Chain" clip-path="url(#chain-clip)" fill="{roller_fill}">
    <animateTransform attributeName="transform" type="translate"
        calcMode="spline" dur="{_DUR}" repeatCount="indefinite"
        keyTimes="{_KT}" values="{_chain_tx_vals}" keySplines="{_KS}"/>
    {_rollers_svg}
  </g>

  <!-- upper arm -->
  <path id="Upper-Arm" d="{_D['Upper-Arm'][0]}" fill="{arm_fill}">
    {_animate_d('Upper-Arm')}
  </path>

  <!-- lower arm -->
  <path id="Lower-Arm" d="{_D['Lower-Arm'][0]}" fill="{arm_fill}">
    {_animate_d('Lower-Arm')}
  </path>

  <!-- erg base covers lower body (rendered after body parts, same as source) -->
  <path id="Erg-Base" d="{_D_STATIC['Erg-Base']}" fill="{erg_fill}"/>

  <!-- erg seat -->
  <rect id="Erg-Seat" x="{_SEAT_X[0]}" y="243" width="43" height="18"
        fill="{seat_fill}">
    {_animate_attr('x', _SEAT_X)}
  </rect>

  <!-- fly wheel: donut + bar are static (they form the D in ERG NERD).
       Six spokes rotate behind the donut, visible through the transparent hole. -->
  <g id="Fly-Wheel" transform="translate(278,159)" fill="{text_fill}">
    <!-- spokes spin behind the donut ring -->
    <g id="Spokes" fill="none" stroke="{spoke_stroke}" stroke-width="1.5" stroke-linecap="round">
      <animateTransform attributeName="transform" type="rotate"
          from="0 28 28" to="360 28 28" dur="0.9s" repeatCount="indefinite"/>
      {_spokes_svg}
      <circle cx="28" cy="28" r="11.4074"/>
    </g>
    <!-- static donut ring (covers spoke ends; hole reveals spinning spokes) -->
    <path d="{_wheel_donut}"/>
    <!-- static vertical bar that forms the D letterform -->
    <rect x="8" y="0" width="15" height="56"/>
  </g>

  <!-- letters N E R G (animated via translate) -->
  <text font-family="Helvetica-Bold, Helvetica" font-size="65"
        font-weight="bold" letter-spacing="-4.0625" fill="{text_fill}">
    <tspan x="{_LETTERS['N'][0][0]}" y="{_LETTERS['N'][0][1]}">N</tspan>
    {_letter_translate('N')}
  </text>
  <text font-family="Helvetica-Bold, Helvetica" font-size="65"
        font-weight="bold" letter-spacing="-4.0625" fill="{text_fill}">
    <tspan x="{_LETTERS['E'][0][0]}" y="{_LETTERS['E'][0][1]}">E</tspan>
    {_letter_translate('E')}
  </text>
  <text font-family="Helvetica-Bold, Helvetica" font-size="65"
        font-weight="bold" letter-spacing="-4.0625" fill="{text_fill}">
    <tspan x="{_LETTERS['R'][0][0]}" y="{_LETTERS['R'][0][1]}">R</tspan>
    {_letter_translate('R')}
  </text>
  <text font-family="Helvetica-Bold, Helvetica" font-size="65"
        font-weight="bold" letter-spacing="-4.0625" fill="{text_fill}">
    <tspan x="{_LETTERS['G'][0][0]}" y="{_LETTERS['G'][0][1]}">G</tspan>
    {_letter_translate('G')}
  </text>

  <!-- head: outer group translates to position; inner group rotates around neck pivot -->
  <g id="HeadGroup">
    {_animate_head_translate()}
    <g id="HeadRotate">
      {_animate_head_rotate()}
      <image id="Head" x="0" y="0" width="110" height="110" xlink:href="{_HEAD_URI}"/>
    </g>
  </g>

  <!-- sweat droplets: body-drive phase only, fly off back (left side) of head -->
  {_sweat_svg}

</svg>"""
    return svg


# ---------------------------------------------------------------------------
# HyperDiv component
# ---------------------------------------------------------------------------


def ergnerd_animation(width: int = 20, theme: str | None = None) -> None:
    """
    Render the animated rowing logo as a HyperDiv image component.

    Parameters
    ----------
    width : int
        Width in HyperDiv spacing units (default 20 ≈ the login card width).
    theme : "light" | "dark" | None
        Override the theme.  If None, reads from hd.theme().
    """
    if theme is None:
        theme = "dark" if hd.theme().is_dark else "light"

    svg_str = build_svg(theme=theme)
    data_uri = "data:image/svg+xml;base64," + base64.b64encode(
        svg_str.encode("utf-8")
    ).decode("ascii")
    hd.image(src=data_uri, width=width)
