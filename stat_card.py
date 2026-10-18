"""Draws the /stat profile card (and the /shop preview) as PNG images."""
import colorsys
import math
import random
from functools import lru_cache
from io import BytesIO
from pathlib import Path

from PIL import Image, ImageDraw, ImageEnhance, ImageFilter, ImageFont

S = 2                    # render at 2x so the card looks sharp in Discord
W, H = 900, 500          # card size before scaling

WIN = (67, 201, 121)
LOSS = (240, 84, 92)
COIN = (255, 196, 60)

# ---------- Card styles ----------
# key: (display name, price, colours). price None = can't be bought (wheel only)
STYLES = {
    "dark": ("暗夜", 0, dict(
        bg=((20, 23, 36), (30, 33, 52)), glows=((220, 120, 255), (122, 134, 255)), glow_alpha=55,
        panel=(36, 40, 62), edge=(52, 57, 86), track=(55, 60, 90), shadow=90,
        text=(240, 242, 248), muted=(150, 156, 180),
        accent=((122, 134, 255), (220, 120, 255)), mix=(110, 124, 255), flex=(255, 160, 70),
    )),
    "light": ("晨光", 100, dict(
        bg=((232, 237, 246), (248, 249, 252)), glows=((255, 190, 220), (150, 170, 255)), glow_alpha=70,
        panel=(255, 255, 255), edge=(224, 228, 236), track=(229, 232, 240), shadow=35,
        text=(28, 32, 40), muted=(110, 116, 130),
        accent=((88, 101, 242), (160, 90, 240)), mix=(88, 101, 242), flex=(245, 140, 40),
    )),
    "sakura": ("櫻花", 300, dict(
        bg=((255, 232, 240), (255, 246, 249)), glows=((255, 160, 195), (255, 205, 160)), glow_alpha=80,
        panel=(255, 252, 253), edge=(246, 210, 224), track=(250, 222, 232), shadow=30,
        text=(92, 40, 62), muted=(172, 118, 140),
        accent=((255, 100, 160), (255, 155, 115)), mix=(240, 95, 150), flex=(255, 145, 105),
    )),
    "crimson": ("暗紅", 300, dict(
        bg=((24, 10, 14), (40, 14, 22)), glows=((210, 30, 60), (140, 20, 45)), glow_alpha=75,
        panel=(46, 18, 27), edge=(86, 32, 46), track=(72, 30, 42), shadow=110,
        text=(252, 236, 238), muted=(196, 150, 160),
        accent=((255, 85, 95), (255, 170, 120)), mix=(255, 80, 95), flex=(255, 165, 105), fx="embers",
    )),
    "neon": ("霓虹", 500, dict(
        bg=((6, 6, 14), (14, 9, 26)), glows=((255, 0, 200), (0, 230, 255)), glow_alpha=75,
        panel=(15, 14, 30), edge=(70, 45, 130), track=(34, 30, 62), shadow=120,
        text=(240, 250, 255), muted=(140, 150, 195),
        accent=((0, 235, 255), (255, 60, 220)), mix=(0, 225, 255), flex=(255, 60, 220), fx="neon",
    )),
    "gold": ("黃金", 800, dict(
        bg=((14, 12, 8), (28, 23, 14)), glows=((235, 185, 60), (180, 130, 40)), glow_alpha=65,
        panel=(33, 29, 19), edge=(96, 78, 38), track=(62, 54, 33), shadow=120,
        text=(252, 242, 218), muted=(192, 172, 132),
        accent=((255, 222, 130), (205, 150, 45)), mix=(242, 202, 92), flex=(222, 160, 62), fx="gold",
    )),
    "rainbow": ("彩虹", None, dict(
        bg=((14, 12, 26), (24, 20, 40)), glows=((255, 90, 170), (70, 190, 255)), glow_alpha=70,
        panel=(28, 25, 46), edge=(72, 62, 112), track=(48, 44, 78), shadow=110,
        text=(248, 246, 255), muted=(170, 162, 205),
        accent=((255, 85, 85), (255, 185, 60), (110, 220, 100), (70, 170, 255), (190, 100, 255)),
        mix=(70, 170, 255), flex=(255, 140, 70), fx="rainbow",
    )),
}

# ---------- Fonts with Chinese support, tried in order: (path, index inside .ttc) ----------
HERE = Path(__file__).parent
FONTS = {
    "bold": [
        (HERE / "fonts" / "bold.ttf", 0),
        (Path("C:/Windows/Fonts/msjhbd.ttc"), 0),                          # Windows
        (Path("/System/Library/Fonts/PingFang.ttc"), 0),                   # macOS
        (Path("/usr/share/fonts/opentype/noto/NotoSansCJK-Bold.ttc"), 3),  # Linux
        (Path("/usr/share/fonts/noto-cjk/NotoSansCJK-Bold.ttc"), 3),
    ],
    "regular": [
        (HERE / "fonts" / "regular.ttf", 0),
        (Path("C:/Windows/Fonts/msjh.ttc"), 0),
        (Path("/System/Library/Fonts/PingFang.ttc"), 0),
        (Path("/usr/share/fonts/opentype/noto/NotoSansCJK-Regular.ttc"), 3),
        (Path("/usr/share/fonts/noto-cjk/NotoSansCJK-Regular.ttc"), 3),
    ],
}


@lru_cache(maxsize=None)
def font(weight, size):
    for path, index in FONTS[weight]:
        if path.exists():
            return ImageFont.truetype(str(path), size * S, index=index)
    return ImageFont.load_default(size * S)


def s(*values):
    """Scale coordinates."""
    return tuple(round(v * S) for v in values)


def lerp(c1, c2, t):
    return tuple(round(a + (b - a) * t) for a, b in zip(c1, c2))


def grad_color(colors, t):
    """Colour at position t (0–1) along a gradient with any number of stops."""
    t = min(max(t, 0), 1) * (len(colors) - 1)
    i = min(int(t), len(colors) - 2)
    return lerp(colors[i], colors[i + 1], t - i)


# ---------- Drawing helpers ----------

def glow(img, center, radius, color, alpha, blur=90):
    layer = Image.new("RGBA", img.size, (0, 0, 0, 0))
    cx, cy = center
    ImageDraw.Draw(layer).ellipse(s(cx - radius, cy - radius, cx + radius, cy + radius), fill=(*color, alpha))
    img.alpha_composite(layer.filter(ImageFilter.GaussianBlur(blur * S)))


def panel(img, box, t, radius=18):
    x0, y0, x1, y1 = box
    shadow = Image.new("RGBA", img.size, (0, 0, 0, 0))
    ImageDraw.Draw(shadow).rounded_rectangle(
        s(x0, y0 + 6, x1, y1 + 6), radius=radius * S, fill=(0, 0, 0, t["shadow"])
    )
    img.alpha_composite(shadow.filter(ImageFilter.GaussianBlur(12 * S)))
    ImageDraw.Draw(img).rounded_rectangle(s(*box), radius=radius * S, fill=t["panel"], outline=t["edge"], width=S)


def smooth_ring(size, width, track, arc_color, fraction):
    """Anti-aliased donut ring (drawn at 4x then shrunk)."""
    big = size * 4
    ring = Image.new("RGBA", (big, big), (0, 0, 0, 0))
    d = ImageDraw.Draw(ring)
    pad = width * 2
    box = (pad, pad, big - pad, big - pad)
    d.ellipse(box, outline=track, width=width * 4)
    if fraction > 0:
        d.arc(box, -90, -90 + 360 * fraction, fill=arc_color, width=width * 4)
    return ring.resize((size, size), Image.LANCZOS)


def gradient_text(img, xy, text, f, colors, anchor):
    mask = Image.new("L", img.size, 0)
    ImageDraw.Draw(mask).text(xy, text, font=f, fill=255, anchor=anchor)
    x0, _, x1, _ = mask.getbbox() or (0, 0, 1, 1)
    grad = Image.new("RGBA", img.size)
    gd = ImageDraw.Draw(grad)
    for x in range(x0, x1 + 1):
        gd.line([(x, 0), (x, img.size[1])], fill=grad_color(colors, (x - x0) / max(1, x1 - x0)))
    img.paste(grad, (0, 0), mask)


def fit_text(draw, text, weight, size, min_size, max_width):
    """Shrink the font (then cut with …) until the text fits."""
    while size > min_size and draw.textlength(text, font=font(weight, size)) > max_width * S:
        size -= 2
    f = font(weight, size)
    if draw.textlength(text, font=f) > max_width * S:
        while text and draw.textlength(text + "…", font=f) > max_width * S:
            text = text[:-1]
        text += "…"
    return text, f


def circle_mask(px):
    big = Image.new("L", (px * 4, px * 4), 0)
    ImageDraw.Draw(big).ellipse((0, 0, px * 4 - 1, px * 4 - 1), fill=255)
    return big.resize((px, px), Image.LANCZOS)


def avatar_with_ring(avatar_bytes, size, t, ring=5):
    px, inner = size * S, (size - ring * 2 - 4) * S
    av = None
    if avatar_bytes:
        try:
            av = Image.open(BytesIO(avatar_bytes)).convert("RGBA").resize((inner, inner), Image.LANCZOS)
        except Exception:
            av = None
    if av is None:
        av = Image.new("RGBA", (inner, inner), (*t["track"], 255))

    ring_img = Image.new("RGBA", (px, px))
    rd = ImageDraw.Draw(ring_img)
    for y in range(px):
        rd.line([(0, y), (px, y)], fill=grad_color(t["accent"], y / px))
    ring_img.putalpha(circle_mask(px))

    gap_px = inner + 4 * S
    gap = Image.new("RGBA", (gap_px, gap_px), (*t["bg"][0], 255))
    gap.putalpha(circle_mask(gap_px))
    offset = (px - inner) // 2
    ring_img.alpha_composite(gap, (offset - 2 * S, offset - 2 * S))
    ring_img.paste(av, (offset, offset), circle_mask(inner))
    return ring_img


def mode_panel(img, x, y, w, h, title, color, games, wins, t):
    panel(img, (x, y, x + w, y + h), t)
    draw = ImageDraw.Draw(img)

    draw.rounded_rectangle(s(x + 26, y + 26, x + 32, y + 54), radius=3 * S, fill=color)
    draw.text(s(x + 44, y + 40), title, font=font("bold", 28), fill=t["text"], anchor="lm")

    ring_size = 150
    fraction = wins / games if games else 0
    ring = smooth_ring(ring_size * S, 14 * S // 2, t["track"], color, fraction)
    rx, ry = x + 30, y + 80
    img.alpha_composite(ring, s(rx, ry))
    cx, cy = rx + ring_size / 2, ry + ring_size / 2
    rate = f"{fraction:.0%}" if games else "—"
    draw.text(s(cx, cy - 6), rate, font=font("bold", 34), fill=t["text"], anchor="mm")
    draw.text(s(cx, cy + 26), "勝率", font=font("regular", 18), fill=t["muted"], anchor="mm")

    col, right = x + 215, x + w - 30
    rows = (("勝場", wins, WIN), ("敗場", games - wins, LOSS))
    for i, (label, value, c) in enumerate(rows):
        mid = y + 122 + i * 66
        draw.ellipse(s(col, mid - 6, col + 12, mid + 6), fill=c)
        draw.text(s(col + 24, mid), label, font=font("regular", 24), fill=t["muted"], anchor="lm")
        draw.text(s(right, mid), str(value), font=font("bold", 40), fill=t["text"], anchor="rm")
        if i == 0:
            draw.line(s(col, mid + 33, right, mid + 33), fill=t["edge"], width=S)


# ---------- Premium effects (gold / rainbow / neon / crimson) ----------

GOLD_FOIL = [(255, 242, 190), (198, 142, 42), (255, 228, 145), (146, 96, 22), (255, 238, 175)]
NEON_CYAN, NEON_PINK = (0, 235, 255), (255, 60, 220)
BADGES = {"gold": "黃金", "rainbow": "限定", "neon": "霓虹"}


def hue(t, sat=0.75, val=1.0):
    return tuple(round(c * 255) for c in colorsys.hsv_to_rgb(t % 1, sat, val))


def gold_at(t):
    return grad_color(GOLD_FOIL, t)


def rainbow_at(t):
    return hue(t)


def diagonal_gradient(size, colour_at, repeats=1.0):
    """Full-size image coloured along the top-left → bottom-right diagonal."""
    w, h = size
    strip = Image.new("RGB", (w + h, 1))
    px = strip.load()
    for i in range(w + h):
        px[i, 0] = colour_at((i / (w + h) * repeats) % 1)
    out = Image.new("RGB", size)
    for y in range(h):
        out.paste(strip.crop((y, 0, y + w, 1)), (0, y))
    return out


def gradient_outline(img, box, colour_at, width, radius, repeats=1.0):
    mask = Image.new("L", img.size, 0)
    ImageDraw.Draw(mask).rounded_rectangle(s(*box), radius=radius * S, outline=255, width=round(width * S))
    img.paste(diagonal_gradient(img.size, colour_at, repeats), (0, 0), mask)


def neon_outline(img, box, colour, radius, width=2.5):
    layer = Image.new("RGBA", img.size, (0, 0, 0, 0))
    ImageDraw.Draw(layer).rounded_rectangle(s(*box), radius=radius * S, outline=(*colour, 255), width=round(4 * S))
    blurred = layer.filter(ImageFilter.GaussianBlur(7 * S))
    img.alpha_composite(blurred)
    img.alpha_composite(blurred)
    bright = lerp(colour, (255, 255, 255), 0.55)
    ImageDraw.Draw(img).rounded_rectangle(s(*box), radius=radius * S, outline=bright, width=round(width * S))


def glow_text(img, xy, text, f, colour, anchor, blur=8):
    layer = Image.new("RGBA", img.size, (0, 0, 0, 0))
    ImageDraw.Draw(layer).text(xy, text, font=f, fill=(*colour, 255), anchor=anchor)
    blurred = layer.filter(ImageFilter.GaussianBlur(blur * S))
    img.alpha_composite(blurred)
    img.alpha_composite(blurred)


def sparkle(d, x, y, r, colour):
    pts = []
    for i in range(8):
        rr = r if i % 2 == 0 else r * 0.25
        a = math.radians(i * 45)
        pts.append((x + rr * math.sin(a), y - rr * math.cos(a)))
    d.polygon([s(px, py) for px, py in pts], fill=colour)


def sparkle_field(img, seed, colours, count=26):
    rnd = random.Random(seed)
    layer = Image.new("RGBA", img.size, (0, 0, 0, 0))
    d = ImageDraw.Draw(layer)
    spots = [(rnd.uniform(14, W - 14), rnd.uniform(10, 196)) for _ in range(count)]
    spots += [(rnd.uniform(14, W - 14), rnd.uniform(470, H - 6)) for _ in range(count // 3)]
    keep_clear = [(24, 0, 196, 196),            # avatar
                  (196, 40, 640, 150),          # name + badge
                  (W - 330, 24, W - 24, 190)]   # ELO + credits
    for x, y in spots:
        if any(x0 - 8 < x < x1 + 8 and y0 - 8 < y < y1 + 8 for x0, y0, x1, y1 in keep_clear):
            continue
        r = rnd.choice([2.5, 3, 4, 5, 7])
        c = rnd.choice(colours)
        sparkle(d, x, y, r, (*c, 230))
    glowy = layer.filter(ImageFilter.GaussianBlur(3 * S))
    img.alpha_composite(glowy)
    img.alpha_composite(layer)


def background_fx(img, fx):
    if fx == "gold":
        # soft diagonal sheen
        sheen = Image.new("RGBA", img.size, (0, 0, 0, 0))
        d = ImageDraw.Draw(sheen)
        for x0, wdt, a in ((120, 70, 26), (300, 30, 18), (620, 90, 22)):
            d.polygon([s(x0, 0), s(x0 + wdt, 0), s(x0 + wdt - 260, H), s(x0 - 260, H)], fill=(255, 225, 150, a))
        img.alpha_composite(sheen.filter(ImageFilter.GaussianBlur(14 * S)))
        # art-deco fans in two corners
        deco = Image.new("RGBA", img.size, (0, 0, 0, 0))
        d = ImageDraw.Draw(deco)
        for cx, cy, start in ((0, 0, 0), (W, H, 180)):
            for r in range(50, 190, 22):
                d.arc(s(cx - r, cy - r, cx + r, cy + r), start, start + 90, fill=(230, 180, 80, 70), width=S)
            for k in range(1, 6):
                a = math.radians(start + k * 15)
                d.line(s(cx + 50 * math.cos(a), cy + 50 * math.sin(a), cx + 180 * math.cos(a), cy + 180 * math.sin(a)),
                       fill=(230, 180, 80, 45), width=S)
        img.alpha_composite(deco)
    elif fx == "rainbow":
        holo = diagonal_gradient(img.size, lambda t: hue(t, 0.55, 1.0), repeats=1.6).convert("RGBA")
        holo.putalpha(48)
        img.alpha_composite(holo)
        shimmer = Image.new("RGBA", img.size, (0, 0, 0, 0))
        d = ImageDraw.Draw(shimmer)
        for x0, wdt in ((200, 60), (420, 25), (700, 80)):
            d.polygon([s(x0, 0), s(x0 + wdt, 0), s(x0 + wdt - 300, H), s(x0 - 300, H)], fill=(255, 255, 255, 30))
        img.alpha_composite(shimmer.filter(ImageFilter.GaussianBlur(16 * S)))
    elif fx == "neon":
        grid = Image.new("RGBA", img.size, (0, 0, 0, 0))
        d = ImageDraw.Draw(grid)
        for x in range(0, W + 1, 36):
            d.line(s(x, 0, x, H), fill=(*NEON_PINK, 22), width=S)
        for y in range(0, H + 1, 36):
            d.line(s(0, y, W, y), fill=(*NEON_CYAN, 18), width=S)
        img.alpha_composite(grid)
    elif fx == "embers":
        rnd = random.Random(5)
        layer = Image.new("RGBA", img.size, (0, 0, 0, 0))
        d = ImageDraw.Draw(layer)
        for _ in range(60):
            x, y = rnd.uniform(0, W), H - abs(rnd.gauss(0, 170))
            r = rnd.uniform(1.2, 3.2)
            c = rnd.choice([(255, 120, 40), (255, 170, 60), (255, 80, 60)])
            d.ellipse(s(x - r, y - r, x + r, y + r), fill=(*c, rnd.randint(120, 230)))
        img.alpha_composite(layer.filter(ImageFilter.GaussianBlur(2 * S)))
        img.alpha_composite(layer)


def crown(img, cx, top):
    """Small gold crown sitting on top of the avatar."""
    w, h = 54, 30
    pts = [(-w / 2, h), (-w / 2, 8), (-w / 4, 18), (0, 0), (w / 4, 18), (w / 2, 8), (w / 2, h)]
    shape = [s(cx + x, top + y) for x, y in pts]
    mask = Image.new("L", img.size, 0)
    ImageDraw.Draw(mask).polygon(shape, fill=255)
    grad = Image.new("RGB", img.size)
    gd = ImageDraw.Draw(grad)
    for y in range(round(top * S), round((top + h) * S) + 1):
        gd.line([(0, y), (img.size[0], y)], fill=gold_at((y / S - top) / h * 0.5))
    shadow = Image.new("RGBA", img.size, (0, 0, 0, 0))
    ImageDraw.Draw(shadow).polygon([s(cx + x, top + y + 3) for x, y in pts], fill=(0, 0, 0, 120))
    img.alpha_composite(shadow.filter(ImageFilter.GaussianBlur(3 * S)))
    img.paste(grad, (0, 0), mask)
    d = ImageDraw.Draw(img)
    d.line(shape + [shape[0]], fill=(120, 78, 15), width=round(1.5 * S))
    for x, y in ((-w / 2, 8), (0, 0), (w / 2, 8)):
        d.ellipse(s(cx + x - 4, top + y - 4, cx + x + 4, top + y + 4), fill=(255, 245, 210), outline=(120, 78, 15))
    d.ellipse(s(cx - 4, top + 20, cx + 4, top + 28), fill=(229, 72, 77))


def badge(img, x, y, fx):
    text = BADGES[fx]
    f = font("bold", 16)
    d = ImageDraw.Draw(img)
    tw = d.textlength(text, font=f) / S
    box = (x, y - 14, x + tw + 26, y + 14)
    fill_at = {"gold": gold_at, "rainbow": rainbow_at, "neon": lambda t: lerp(NEON_CYAN, NEON_PINK, t)}[fx]
    mask = Image.new("L", img.size, 0)
    ImageDraw.Draw(mask).rounded_rectangle(s(*box), radius=14 * S, fill=255)
    grad = Image.new("RGB", img.size)
    gd = ImageDraw.Draw(grad)
    for xx in range(round(box[0] * S), round(box[2] * S) + 1):
        gd.line([(xx, 0), (xx, img.size[1])], fill=fill_at((xx / S - box[0]) / (box[2] - box[0])))
    img.paste(grad, (0, 0), mask)
    text_col = (60, 38, 6) if fx == "gold" else (255, 255, 255)
    d.text(s(x + 13 + tw / 2, y), text, font=f, fill=text_col, anchor="mm",
           stroke_width=0 if fx == "gold" else S, stroke_fill=(30, 20, 40))


def foreground_fx(img, fx, panels):
    if fx == "gold":
        for box in panels:
            gradient_outline(img, box, gold_at, 2, 18, repeats=1.5)
        sparkle_field(img, 3, [(255, 236, 170), (255, 250, 230)])
        gradient_outline(img, (6, 6, W - 6, H - 6), gold_at, 5, 22, repeats=2)
        gradient_outline(img, (14, 14, W - 14, H - 14), gold_at, 1.2, 17, repeats=2)
    elif fx == "rainbow":
        for box in panels:
            gradient_outline(img, box, rainbow_at, 2, 18, repeats=1.2)
        sparkle_field(img, 9, [(255, 255, 255), (255, 220, 250), (210, 245, 255)])
        gradient_outline(img, (6, 6, W - 6, H - 6), rainbow_at, 6, 22, repeats=1.5)
    elif fx == "neon":
        neon_outline(img, panels[0], NEON_CYAN, 18)
        neon_outline(img, panels[1], NEON_PINK, 18)
        neon_outline(img, (8, 8, W - 8, H - 8), NEON_PINK, 22, width=2)
        # CRT scanlines
        lines = Image.new("RGBA", img.size, (0, 0, 0, 0))
        d = ImageDraw.Draw(lines)
        for y in range(0, H * S, 3 * S):
            d.line([(0, y), (W * S, y)], fill=(0, 0, 0, 22), width=S)
        img.alpha_composite(lines)
    elif fx == "embers":
        gradient_outline(img, (6, 6, W - 6, H - 6), lambda t: lerp((255, 90, 70), (140, 20, 40), abs(t * 2 - 1)), 3, 22)


# ---------- Champion cards (colours and art come from champions.theme) ----------

ART_BOX = (330, 0, W, 330)  # right side of the header; the panels cover the bottom part


def header_art(img, t, zoom=0.74, brightness=0.82):
    """Champion splash in the right of the header, fading out towards the name and the panels."""
    x0, y0, x1, y1 = ART_BOX
    bw, bh = (x1 - x0) * S, (y1 - y0) * S
    src = Image.open(t["art"]).convert("RGB")
    fx_, fy = t["focus"]
    cw = src.width * zoom
    ch = cw * bh / bw
    left = min(max(fx_ * src.width - cw * 0.5, 0), src.width - cw)
    top = min(max(fy * src.height - ch * 0.3, 0), src.height - ch)
    pic = src.resize((bw, bh), Image.LANCZOS, box=(left, top, left + cw, top + ch))
    pic = ImageEnhance.Brightness(pic).enhance(brightness).convert("RGBA")

    fade = Image.new("L", (bw, bh), 0)
    fd = ImageDraw.Draw(fade)
    for x in range(bw):  # fade in from the left, out above the panels
        fd.line([(x, 0), (x, bh)], fill=round(255 * min(1, x / (150 * S)) ** 1.4))
    bottom = Image.new("L", (bw, bh), 0)
    bd = ImageDraw.Draw(bottom)
    for y in range(bh):
        bd.line([(0, y), (bw, y)], fill=round(255 * min(1, max(0, (225 * S - y) / (45 * S)))))
    pic.putalpha(Image.composite(fade, Image.new("L", (bw, bh), 0), bottom))
    img.alpha_composite(pic, s(x0, y0))

    shade = Image.new("RGBA", img.size, (0, 0, 0, 0))  # keeps the ELO number readable
    ImageDraw.Draw(shade).ellipse(s(W - 300, 10, W + 20, 200), fill=(0, 0, 0, 165))
    img.alpha_composite(shade.filter(ImageFilter.GaussianBlur(28 * S)))


def champion_foreground(img, t, panels):
    edge_at = lambda k: grad_color([t["accent"][1], t["flex"], t["accent"][1]], k)  # noqa: E731
    for box in panels:
        gradient_outline(img, box, edge_at, 1.5, 18, repeats=1.2)
    gradient_outline(img, (6, 6, W - 6, H - 6), edge_at, 4, 22, repeats=1.5)

    d = ImageDraw.Draw(img)
    f = font("bold", 16)
    x, y = 206 + d.textlength("個人戰績", font=font("regular", 20)) / S + 12, 128
    tw = d.textlength(t["champ_name"], font=f) / S
    box = (x, y - 14, x + tw + 26, y + 14)
    d.rounded_rectangle(s(*box), radius=14 * S, fill=t["accent"][1])
    d.text(s(x + 13 + tw / 2, y), t["champ_name"], font=f, fill=(20, 18, 24), anchor="mm")
    d.text(s(W - 40, 482), t["champ_title"], font=font("bold", 14), fill=t["muted"], anchor="rm")


# ---------- Public functions ----------

def render_card_image(name, avatar_bytes, elo, credits, mix, flex, style="dark"):
    """Returns the card as a PIL image. mix / flex = (games, wins).
    style is a key of STYLES, or a ready-made colour dict (champion cards)."""
    t = style if isinstance(style, dict) else STYLES.get(style, STYLES["dark"])[2]
    fx = t.get("fx")
    img = Image.new("RGBA", s(W, H))
    bg = ImageDraw.Draw(img)
    for row in range(H * S):
        bg.line([(0, row), (W * S, row)], fill=lerp(*t["bg"], row / (H * S)))
    if fx == "champion":
        if t["art"]:
            header_art(img, t)
    else:
        background_fx(img, fx)
    glow(img, (W - 80, 40), 180, t["glows"][0], t["glow_alpha"])
    glow(img, (60, H), 200, t["glows"][1], t["glow_alpha"] - 10)

    # header: avatar (+ crown for gold)
    img.alpha_composite(avatar_with_ring(avatar_bytes, 136, t), s(40, 32))
    if fx == "gold":
        crown(img, 108, 17)
    draw = ImageDraw.Draw(img)

    # ELO and credits on the right
    elo_font = font("bold", 64)
    draw.text(s(W - 44, 42), "ELO 積分", font=font("bold", 20), fill=t["muted"], anchor="rt")
    if fx == "neon":
        glow_text(img, s(W - 44, 128), str(elo), elo_font, NEON_PINK, "rs")
    gradient_text(img, s(W - 44, 128), str(elo), elo_font, t["accent"], "rs")
    draw = ImageDraw.Draw(img)
    elo_w = draw.textlength(str(elo), font=elo_font) / S

    coin_text = f"{credits:,}"
    coin_font = font("bold", 22)
    tw = draw.textlength(coin_text, font=coin_font) / S
    label_w = draw.textlength("金幣", font=font("regular", 18)) / S
    pill_r = W - 44
    pill_l = pill_r - 14 - tw - 12 - label_w - 38
    draw.rounded_rectangle(s(pill_l, 144, pill_r, 178), radius=17 * S, fill=t["panel"], outline=t["edge"], width=S)
    draw.ellipse(s(pill_l + 10, 151, pill_l + 30, 171), fill=COIN)
    draw.text(s(pill_l + 38, 161), "金幣", font=font("regular", 18), fill=t["muted"], anchor="lm")
    draw.text(s(pill_r - 14, 161), coin_text, font=coin_font, fill=t["text"], anchor="rm")

    # name: gold foil / rainbow / neon glow on the premium styles
    name_max = W - 44 - max(elo_w, pill_r - pill_l) - 40 - 204
    name_text, name_font = fit_text(draw, name, "bold", 42, 24, name_max)
    if fx == "gold":
        gradient_text(img, s(204, 86), name_text, name_font, GOLD_FOIL[:3], "lm")
    elif fx == "rainbow":
        gradient_text(img, s(204, 86), name_text, name_font, [hue(k / 5) for k in range(6)], "lm")
    elif fx == "neon":
        glow_text(img, s(204, 86), name_text, name_font, NEON_CYAN, "lm")
        ImageDraw.Draw(img).text(s(204, 86), name_text, font=name_font, fill=(235, 255, 255), anchor="lm")
    else:
        draw.text(s(204, 86), name_text, font=name_font, fill=t["text"], anchor="lm")
    draw = ImageDraw.Draw(img)
    draw.text(s(206, 128), "個人戰績", font=font("regular", 20), fill=t["muted"], anchor="lm")
    if fx in BADGES:
        badge(img, 206 + draw.textlength("個人戰績", font=font("regular", 20)) / S + 12, 128, fx)

    # 10mix and Flex panels
    pw = (W - 40 * 2 - 24) / 2
    panels = [(40, 205, 40 + pw, 465), (40 + pw + 24, 205, 40 + 2 * pw + 24, 465)]
    mode_panel(img, 40, 205, pw, 260, "10mix", t["mix"], *mix, t)
    mode_panel(img, 40 + pw + 24, 205, pw, 260, "Flex", t["flex"], *flex, t)
    if fx == "champion":
        champion_foreground(img, t, panels)
    else:
        foreground_fx(img, fx, panels)
    return img


def to_png(img):
    buf = BytesIO()
    img.convert("RGB").save(buf, "PNG", optimize=True)
    buf.seek(0)
    return buf


def render_stat_card(name, avatar_bytes, elo, credits, mix, flex, style="dark"):
    """Returns the card as a PNG (BytesIO)."""
    return to_png(render_card_image(name, avatar_bytes, elo, credits, mix, flex, style))


def price_text(price):
    if price is None:
        return "轉盤限定"
    return "免費" if price == 0 else f"{price} 金幣"


@lru_cache(maxsize=1)
def _shop_preview_bytes():
    cols, thumb_w = 3, 560
    thumb_h = round(thumb_w * H / W)
    label_h, gap = 56, 24
    sheet_w = cols * thumb_w + (cols + 1) * gap
    rows = (len(STYLES) + cols - 1) // cols
    sheet_h = rows * (thumb_h + label_h) + (rows + 1) * gap
    sheet = Image.new("RGB", (sheet_w, sheet_h), (24, 26, 34))
    draw = ImageDraw.Draw(sheet)
    label_font = font("bold", 15)  # 30px on the sheet

    for i, (key, (label, price, _)) in enumerate(STYLES.items()):
        r, c = divmod(i, cols)
        x = gap + c * (thumb_w + gap)
        y = gap + r * (thumb_h + label_h + gap)
        card = render_card_image("預覽", None, 1200, 520, (20, 12), (10, 6), key)
        sheet.paste(card.convert("RGB").resize((thumb_w, thumb_h), Image.LANCZOS), (x, y))
        tag = f"{label}　{price_text(price)}"
        draw.text((x + thumb_w / 2, y + thumb_h + label_h / 2), tag, font=label_font,
                  fill=(235, 238, 245), anchor="mm")
    out = BytesIO()
    sheet.save(out, "PNG", optimize=True)
    return out.getvalue()


def render_shop_preview():
    """All card styles side by side, as a PNG (BytesIO). Cached after the first call."""
    return BytesIO(_shop_preview_bytes())
