"""Lucky wheel: prize table and the spinning-wheel GIF."""
import colorsys
import math
import random
from io import BytesIO

from PIL import Image, ImageDraw, ImageFilter

from stat_card import font as card_font

# (key, label, weight %, kind, value)
#   kind "coins": value = credits won
#   kind "again": value = number of free extra spins
#   kind "style": value = card style key (if already owned you get STYLE_REFUND credits)
# Expected return ≈ 22.3 coins per spin (a paid spin costs 25), counting 彩虹 as 250
PRIZES = [
    ("net",     "收網",   24.5, "coins", 0),
    ("c10",     "10 金幣",  24, "coins", 10),
    ("c100",    "100 金幣",  4, "coins", 100),
    ("c15",     "15 金幣",  16, "coins", 15),
    ("rainbow", "彩虹卡面", 0.5, "style", "rainbow"),  # super jackpot
    ("c25",     "25 金幣",  14, "coins", 25),
    ("c50",     "50 金幣",   8, "coins", 50),
    ("again",   "再轉一次",  6, "again", 1),
    ("jackpot", "250 金幣",  1, "coins", 250),
    ("again2",  "再轉2次",   2, "again", 2),
]
STYLE_REFUND = 250

# how each prize looks on the wheel: (big text, small text, segment colour)
LOOK = {
    "net":     ("收網", "",        (62, 66, 96)),
    "c10":     ("10",   "金幣",    (88, 101, 242)),
    "c100":    ("100",  "金幣",    (229, 72, 77)),
    "c15":     ("15",   "金幣",    (150, 90, 225)),
    "rainbow": ("彩虹", "限定卡面", None),            # drawn as a rainbow
    "c25":     ("25",   "金幣",    (48, 164, 108)),
    "c50":     ("50",   "金幣",    (247, 107, 21)),
    "again":   ("再轉", "一次",    (18, 165, 148)),
    "jackpot": ("250",  "JACKPOT", (255, 178, 36)),
    "again2":  ("再轉", "2次",     (220, 70, 140)),
}
GOLD = ((255, 226, 140), (214, 152, 40))

CW, CH = 520, 620        # GIF size
CX, CY = 260, 330        # wheel centre
R = 200                  # wheel radius (disc only)
RIM = 22                 # rim thickness
FRAMES = 50
FRAME_MS = 45
HOLD_MS = 6000
SS = 3                   # supersampling for smooth edges


def roll():
    """Pick a prize index using the weights."""
    return random.choices(range(len(PRIZES)), weights=[p[2] for p in PRIZES])[0]


def font(weight, size):
    # stat_card fonts are scaled by 2; ask for half size to get `size` px
    return card_font(weight, max(1, round(size / 2)))


def lerp(c1, c2, t):
    return tuple(round(a + (b - a) * t) for a, b in zip(c1, c2))


# ---------- Static parts (drawn once) ----------

def draw_disc():
    """The spinning disc: segments, shading and labels. Segment 0 starts under the pointer."""
    big = R * 2 * SS
    disc = Image.new("RGBA", (big, big), (0, 0, 0, 0))
    d = ImageDraw.Draw(disc)
    n = len(PRIZES)
    step = 360 / n

    for i, (key, *_r) in enumerate(PRIZES):
        start = -90 - step / 2 + i * step
        colour = LOOK[key][2]
        if colour is None:  # rainbow: sweep the hue across the segment
            for k in range(int(step * 2)):
                hue = k / (step * 2)
                rgb = tuple(round(c * 255) for c in colorsys.hsv_to_rgb(hue, 0.75, 1.0))
                d.pieslice((0, 0, big - 1, big - 1), start + k / 2, start + (k + 1) / 2 + 0.6, fill=rgb)
        else:
            d.pieslice((0, 0, big - 1, big - 1), start, start + step, fill=colour)

    # shading: darker towards the middle, so the disc looks curved
    shade = Image.new("RGBA", (big, big), (0, 0, 0, 0))
    sd = ImageDraw.Draw(shade)
    for k in range(60):
        r = big / 2 * (1 - k / 60)
        a = round(70 * (k / 60) ** 1.6)
        sd.ellipse((big / 2 - r, big / 2 - r, big / 2 + r, big / 2 + r), fill=(0, 0, 0, a))
    disc.alpha_composite(shade.filter(ImageFilter.GaussianBlur(4 * SS)))

    # thin dividers
    d = ImageDraw.Draw(disc)
    for i in range(n):
        a = math.radians(-90 - step / 2 + i * step)
        d.line((big / 2, big / 2, big / 2 + big / 2 * math.cos(a), big / 2 + big / 2 * math.sin(a)),
               fill=(255, 255, 255, 90), width=2 * SS)

    # labels: upright when the segment is under the pointer
    for i, (key, *_r) in enumerate(PRIZES):
        main, sub, _ = LOOK[key]
        angle = i * step  # clockwise from top
        main_size = 36 if not main.isdigit() else 44          # Chinese words are wider
        sub_size = {"JACKPOT": 14, "限定卡面": 16}.get(sub, 18)
        for text, size, radius, weight in ((main, main_size, 0.70, "bold"), (sub, sub_size, 0.50, "bold")):
            if not text:
                continue
            f = font(weight, size * SS)
            tile = Image.new("RGBA", (int(f.size * len(text) * 1.3) + 40, int(f.size * 1.8)), (0, 0, 0, 0))
            ImageDraw.Draw(tile).text((tile.width / 2, tile.height / 2), text, font=f, fill=(255, 255, 255),
                                      anchor="mm", stroke_width=max(2, size // 9) * SS // 2,
                                      stroke_fill=(15, 17, 26, 200))
            tile = tile.rotate(-angle, expand=True, resample=Image.BICUBIC)
            r = big / 2 * (radius if sub else 0.62)
            x = big / 2 + r * math.sin(math.radians(angle))
            y = big / 2 - r * math.cos(math.radians(angle))
            disc.alpha_composite(tile, (round(x - tile.width / 2), round(y - tile.height / 2)))

    return disc.resize((R * 2, R * 2), Image.LANCZOS)


def draw_background():
    bg = Image.new("RGBA", (CW, CH), (0, 0, 0, 255))
    d = ImageDraw.Draw(bg)
    for y in range(CH):
        d.line([(0, y), (CW, y)], fill=lerp((18, 20, 32), (30, 30, 52), y / CH))

    glow = Image.new("RGBA", (CW, CH), (0, 0, 0, 0))
    gd = ImageDraw.Draw(glow)
    gd.ellipse((CW - 230, -120, CW + 130, 240), fill=(200, 110, 255, 70))
    gd.ellipse((-160, CH - 260, 220, CH + 120), fill=(100, 120, 255, 60))
    gd.ellipse((CX - R - 30, CY - R - 30, CX + R + 30, CY + R + 30), fill=(255, 190, 80, 40))
    bg.alpha_composite(glow.filter(ImageFilter.GaussianBlur(60)))

    # title
    d = ImageDraw.Draw(bg)
    d.text((CW / 2, 44), "幸運轉盤", font=font("bold", 34), fill=(255, 236, 190), anchor="mm")

    # soft shadow under the wheel
    shadow = Image.new("RGBA", (CW, CH), (0, 0, 0, 0))
    ImageDraw.Draw(shadow).ellipse((CX - R - RIM, CY - R - RIM + 14, CX + R + RIM, CY + R + RIM + 14),
                                   fill=(0, 0, 0, 150))
    bg.alpha_composite(shadow.filter(ImageFilter.GaussianBlur(16)))
    return bg


def draw_rim():
    """Gold rim (doesn't spin), without the bulbs."""
    size = (R + RIM) * 2
    big = size * SS
    rim = Image.new("RGBA", (big, big), (0, 0, 0, 0))
    # vertical gold gradient, cut into a ring
    grad = Image.new("RGBA", (big, big))
    gd = ImageDraw.Draw(grad)
    for y in range(big):
        t = y / big
        gd.line([(0, y), (big, y)], fill=lerp(GOLD[0], GOLD[1], t) if t < 0.5 else lerp(GOLD[1], GOLD[0], (t - 0.5) * 0.8))
    mask = Image.new("L", (big, big), 0)
    md = ImageDraw.Draw(mask)
    md.ellipse((0, 0, big - 1, big - 1), fill=255)
    inner = RIM * SS
    md.ellipse((inner, inner, big - 1 - inner, big - 1 - inner), fill=0)
    rim.paste(grad, (0, 0), mask)
    d = ImageDraw.Draw(rim)
    d.ellipse((0, 0, big - 1, big - 1), outline=(120, 80, 20), width=2 * SS)
    d.ellipse((inner, inner, big - 1 - inner, big - 1 - inner), outline=(120, 80, 20), width=2 * SS)
    return rim.resize((size, size), Image.LANCZOS)


def draw_pointer():
    w, h = 56, 64
    big = Image.new("RGBA", (w * SS, h * SS), (0, 0, 0, 0))
    d = ImageDraw.Draw(big)
    pts = [(4 * SS, 4 * SS), ((w - 4) * SS, 4 * SS), (w / 2 * SS, (h - 4) * SS)]
    d.polygon(pts, fill=(120, 80, 20))
    inner = [(10 * SS, 9 * SS), ((w - 10) * SS, 9 * SS), (w / 2 * SS, (h - 14) * SS)]
    d.polygon(inner, fill=GOLD[0])
    d.polygon([(w / 2 * SS, 9 * SS), ((w - 10) * SS, 9 * SS), (w / 2 * SS, (h - 14) * SS)], fill=GOLD[1])
    d.ellipse(((w / 2 - 7) * SS, 12 * SS, (w / 2 + 7) * SS, 26 * SS), fill=(255, 250, 235))
    return big.resize((w, h), Image.LANCZOS)


def draw_hub():
    size = 92
    big = size * SS
    hub = Image.new("RGBA", (big, big), (0, 0, 0, 0))
    d = ImageDraw.Draw(hub)
    d.ellipse((0, 0, big - 1, big - 1), fill=(120, 80, 20))
    for k in range(40):  # gold dome
        r = big / 2 - 3 * SS - k * (big / 2 - 3 * SS) / 40
        d.ellipse((big / 2 - r, big / 2 - r - k * 0.6, big / 2 + r, big / 2 + r - k * 0.6),
                  fill=lerp(GOLD[1], GOLD[0], k / 40))
    d.text((big / 2, big / 2), "轉", font=font("bold", 36 * SS), fill=(110, 70, 10), anchor="mm")
    return hub.resize((size, size), Image.LANCZOS)


_CACHE = {}


def parts():
    if not _CACHE:
        _CACHE.update(disc=draw_disc(), bg=draw_background(), rim=draw_rim(),
                      pointer=draw_pointer(), hub=draw_hub())
    return _CACHE


# ---------- Frames ----------

BULBS = 24
CELEBRATE_MS = 50
# extra animation frames after the wheel stops, per prize
CELEBRATE_FRAMES = {"net": 14, "c10": 0, "c15": 0, "c25": 0, "c50": 16, "c100": 18, "again": 16,
                    "again2": 16, "jackpot": 34, "rainbow": 34}
BIG_WINS = ("jackpot", "rainbow")


def draw_bulbs(img, phase, all_on=False, chase=False):
    d = ImageDraw.Draw(img)
    rr = R + RIM / 2
    for b in range(BULBS):
        a = math.radians(b * 360 / BULBS)
        x, y = CX + rr * math.sin(a), CY - rr * math.cos(a)
        if chase:                      # one bright band racing round the rim
            on = (b - phase * 2) % 6 in (0, 1)
        else:
            on = all_on or (b + phase) % 2 == 0
        if on:
            d.ellipse((x - 7, y - 7, x + 7, y + 7), fill=(255, 240, 180, 90))
            d.ellipse((x - 4.5, y - 4.5, x + 4.5, y + 4.5), fill=(255, 252, 230))
        else:
            d.ellipse((x - 4, y - 4, x + 4, y + 4), fill=(150, 105, 40))


def frame(angle, phase, all_on=False, chase=False, behind=None, disc=None):
    """One picture of the wheel. `behind` is drawn between the background and the wheel.
    `disc` is an already-rotated disc, to skip rotating it again for a wheel that has stopped."""
    p = parts()
    img = p["bg"].copy()
    if behind is not None:
        img.alpha_composite(behind)
    if disc is None:
        disc = p["disc"].rotate(angle, resample=Image.BICUBIC)
    img.alpha_composite(disc, (CX - R, CY - R))
    img.alpha_composite(p["rim"], (CX - R - RIM, CY - R - RIM))
    draw_bulbs(img, phase, all_on, chase)
    img.alpha_composite(p["hub"], (CX - p["hub"].width // 2, CY - p["hub"].height // 2))
    img.alpha_composite(p["pointer"], (CX - p["pointer"].width // 2, CY - R - RIM - 22))
    return img


# ---------- Effects ----------

def highlight_top(img, strength=1.0):
    """Glow over the winning segment (the one under the pointer)."""
    step = 360 / len(PRIZES)
    layer = Image.new("RGBA", img.size, (0, 0, 0, 0))
    ImageDraw.Draw(layer).pieslice((CX - R, CY - R, CX + R, CY + R), -90 - step / 2, -90 + step / 2,
                                   fill=(255, 255, 255, round(45 * strength)),
                                   outline=(255, 245, 200, round(255 * strength)), width=4)
    img.alpha_composite(layer)


_RAY_GLOW = {}


def ray_glow(rainbow):
    """Soft glow behind the wheel; the same on every frame, so it's drawn once."""
    if rainbow not in _RAY_GLOW:
        glow = Image.new("RGBA", (CW, CH), (0, 0, 0, 0))
        ImageDraw.Draw(glow).ellipse((CX - R - 60, CY - R - 60, CX + R + 60, CY + R + 60),
                                     fill=(255, 230, 150, 120) if not rainbow else (255, 255, 255, 90))
        _RAY_GLOW[rainbow] = glow.filter(ImageFilter.GaussianBlur(30))
    return _RAY_GLOW[rainbow]


def soft(layer, radius):
    """Blur at half size, then scale back up: looks the same for soft glows and is ~4x cheaper."""
    small = layer.resize((layer.width // 2, layer.height // 2), Image.BILINEAR)
    return small.filter(ImageFilter.GaussianBlur(radius / 2)).resize(layer.size, Image.BILINEAR)


def rays(t, rainbow):
    """Light rays spinning behind the wheel."""
    layer = Image.new("RGBA", (CW, CH), (0, 0, 0, 0))
    d = ImageDraw.Draw(layer)
    n, reach = 16, 420
    for k in range(n):
        a0 = k * 360 / n + t * 40
        if rainbow:
            rgb = tuple(round(c * 255) for c in colorsys.hsv_to_rgb((k / n + t * 0.3) % 1, 0.7, 1.0))
        else:
            rgb = (255, 215, 110) if k % 2 == 0 else (255, 170, 50)
        d.pieslice((CX - reach, CY - reach, CX + reach, CY + reach), a0, a0 + 360 / n / 2, fill=(*rgb, 110))
    layer = soft(layer, 3)
    layer.alpha_composite(ray_glow(rainbow))
    return layer


def flash(img, alpha, colour=(255, 245, 210)):
    if alpha > 0:
        img.alpha_composite(Image.new("RGBA", img.size, (*colour, round(alpha))))


def tint(img, hue, alpha):
    rgb = tuple(round(c * 255) for c in colorsys.hsv_to_rgb(hue % 1, 0.8, 1.0))
    img.alpha_composite(Image.new("RGBA", img.size, (*rgb, alpha)))


def make_particles(seed, count, kind):
    rnd = random.Random(seed)
    out = []
    for _ in range(count):
        out.append(dict(
            x=rnd.uniform(10, CW - 10), y=rnd.uniform(-CH * 0.9, -10),
            vy=rnd.uniform(9, 17), vx=rnd.uniform(-1.5, 1.5),
            spin=rnd.uniform(0, math.pi), vs=rnd.uniform(0.25, 0.5),
            size=rnd.uniform(9, 14) if kind == "coin" else rnd.uniform(3, 5),
            colour=rnd.choice([(255, 90, 90), (255, 200, 60), (100, 220, 120), (80, 170, 255), (200, 110, 255)]),
        ))
    return out


def draw_coins(img, coins, k):
    d = ImageDraw.Draw(img)
    for c in coins:
        x, y = c["x"] + c["vx"] * k, c["y"] + c["vy"] * k
        if y < -20 or y > CH + 20:
            continue
        r = c["size"]
        w = max(2.0, r * abs(math.cos(c["spin"] + c["vs"] * k)))  # flipping coin
        d.ellipse((x - w, y - r, x + w, y + r), fill=(214, 152, 40), outline=(130, 85, 15), width=2)
        if w > 5:
            d.ellipse((x - w * 0.6, y - r * 0.6, x + w * 0.6, y + r * 0.6), fill=(255, 216, 110))
            d.ellipse((x - w * 0.25, y - r * 0.45, x + w * 0.05, y - r * 0.1), fill=(255, 246, 210))


def draw_confetti(img, pieces, k):
    d = ImageDraw.Draw(img)
    for c in pieces:
        x = c["x"] + c["vx"] * k + 6 * math.sin(c["spin"] + k * 0.4)
        y = c["y"] + c["vy"] * 0.8 * k
        if y < -20 or y > CH + 20:
            continue
        w, h = c["size"], c["size"] * 1.8 * abs(math.cos(c["spin"] + c["vs"] * k)) + 1
        a = c["spin"] + c["vs"] * k
        ca, sa = math.cos(a), math.sin(a)
        pts = [(x + dx * ca - dy * sa, y + dx * sa + dy * ca) for dx, dy in ((-w, -h), (w, -h), (w, h), (-w, h))]
        d.polygon(pts, fill=c["colour"])


def star(d, x, y, r, colour):
    pts = []
    for i in range(8):
        rr = r if i % 2 == 0 else r * 0.28
        a = math.radians(i * 45)
        pts.append((x + rr * math.sin(a), y - rr * math.cos(a)))
    d.polygon(pts, fill=colour)


def sparkles(img, k, seed):
    rnd = random.Random(seed)
    d = ImageDraw.Draw(img)
    for i in range(14):
        a = rnd.uniform(0, 2 * math.pi)
        dist = rnd.uniform(R + RIM + 12, R + RIM + 55)
        x, y = CX + dist * math.sin(a), CY - dist * math.cos(a)
        pulse = max(0.0, math.sin(k * 0.55 + i * 1.3))
        if pulse > 0.05:
            star(d, x, y, 4 + 10 * pulse, (255, 248, 210))


def spider_web(img, strength):
    """Net drawn over the wheel for 收網, plus darkening."""
    flash(img, 120 * strength, colour=(8, 8, 14))
    layer = Image.new("RGBA", img.size, (0, 0, 0, 0))
    d = ImageDraw.Draw(layer)
    a_col = (235, 235, 245, round(210 * strength))
    spokes = 12
    for s_ in range(spokes):
        a = math.radians(s_ * 360 / spokes + 7)
        d.line((CX, CY, CX + (R + RIM + 10) * math.sin(a), CY - (R + RIM + 10) * math.cos(a)), fill=a_col, width=2)
    for ring in range(1, 7):
        rr = ring * (R + RIM) / 6
        pts = []
        for s_ in range(spokes + 1):
            a = math.radians(s_ * 360 / spokes + 7)
            sag = 0.9 if s_ % 2 else 1.0
            pts.append((CX + rr * sag * math.sin(a), CY - rr * sag * math.cos(a)))
        d.line(pts, fill=a_col, width=2)
    img.alpha_composite(layer)


def again_effect(img, k):
    pulse = (math.sin(k * 0.6) + 1) / 2
    layer = Image.new("RGBA", img.size, (0, 0, 0, 0))
    d = ImageDraw.Draw(layer)
    rr = R + RIM + 8 + 6 * pulse
    d.ellipse((CX - rr, CY - rr, CX + rr, CY + rr), outline=(60, 220, 255, round(140 + 100 * pulse)), width=6)
    img.alpha_composite(soft(layer, 3))
    # circular arrow spinning around the hub
    d = ImageDraw.Draw(img)
    ar = 62
    start = k * 22
    d.arc((CX - ar, CY - ar, CX + ar, CY + ar), start, start + 280, fill=(80, 230, 255), width=7)
    tip = math.radians(start + 280)
    tx, ty = CX + ar * math.cos(tip), CY + ar * math.sin(tip)
    ang = tip + math.pi / 2
    d.polygon([(tx + 14 * math.cos(ang), ty + 14 * math.sin(ang)),
               (tx + 9 * math.cos(ang + 2.3), ty + 9 * math.sin(ang + 2.3)),
               (tx + 9 * math.cos(ang - 2.3), ty + 9 * math.sin(ang - 2.3))], fill=(80, 230, 255))


def banner(img, key, label, shine=None):
    d = ImageDraw.Draw(img)
    big_win = key in BIG_WINS
    if key == "net":
        text, fill = "被收網了！", (62, 66, 96)
    elif key == "rainbow":
        text, fill = "超級 JACKPOT！彩虹卡面", None
    elif key == "jackpot":
        text, fill = "JACKPOT！250 金幣", None
    elif key in ("again", "again2"):
        text, fill = f"{label}！", LOOK[key][2]
    else:
        text, fill = f"獲得 {label}！", LOOK[key][2]

    x0, y0, x1, y1 = 70, CH - 72, CW - 70, CH - 18
    pill = Image.new("RGBA", img.size, (0, 0, 0, 0))
    pd = ImageDraw.Draw(pill)
    if key == "rainbow":
        for x in range(x0, x1):
            rgb = tuple(round(c * 255) for c in colorsys.hsv_to_rgb((x - x0) / (x1 - x0), 0.7, 1.0))
            pd.line([(x, y0), (x, y1)], fill=rgb)
    elif key == "jackpot":
        for y in range(y0, y1):
            pd.line([(x0, y), (x1, y)], fill=lerp(GOLD[0], GOLD[1], (y - y0) / (y1 - y0)))
    else:
        pd.rectangle((x0, y0, x1, y1), fill=fill)
    if shine is not None:  # bright band sweeping across the banner
        sx = x0 - 80 + (x1 - x0 + 160) * shine
        pd.polygon([(sx, y0), (sx + 40, y0), (sx + 10, y1), (sx - 30, y1)], fill=(255, 255, 255, 170))
    mask = Image.new("L", img.size, 0)
    ImageDraw.Draw(mask).rounded_rectangle((x0, y0, x1, y1), radius=27, fill=255)
    clipped = Image.new("RGBA", img.size, (0, 0, 0, 0))
    clipped.paste(pill, (0, 0), mask)
    img.alpha_composite(clipped)
    d.rounded_rectangle((x0, y0, x1, y1), radius=27, outline=(255, 245, 210) if big_win else (255, 255, 255), width=3)
    d.text((CW / 2, (y0 + y1) / 2), text, font=font("bold", 28), fill=(255, 255, 255),
           anchor="mm", stroke_width=3, stroke_fill=(20, 18, 30))


# ---------- The GIF ----------

def landing_angle(prize_index):
    """Where to stop. Sometimes stop right next to a jackpot segment (a near miss)."""
    n = len(PRIZES)
    step = 360 / n
    big = {i for i, p in enumerate(PRIZES) if p[0] in BIG_WINS}
    jitter = random.uniform(-step * 0.33, step * 0.33)
    near = False
    if prize_index not in big and random.random() < 0.6:
        if (prize_index - 1) % n in big:      # just rolled past the jackpot
            jitter, near = -step * random.uniform(0.30, 0.38), True
        elif (prize_index + 1) % n in big:    # stopped one step short of it
            jitter, near = step * random.uniform(0.30, 0.38), True
    return 360 * 5 + prize_index * step + jitter, near


def render_spin_gif(prize_index):
    """Animated GIF: spin, stop on prize_index, then a celebration that fits the prize."""
    final, near_miss = landing_angle(prize_index)
    key, label = PRIZES[prize_index][0], PRIZES[prize_index][1]
    ease_power = 4 if near_miss else 3   # near misses crawl more slowly at the end

    count = CELEBRATE_FRAMES.get(key, 0)
    coins = make_particles(7, 46, "coin") if key == "jackpot" else []
    confetti = make_particles(11, 110, "confetti") if key == "rainbow" else []
    still = parts()["disc"].rotate(final, resample=Image.BICUBIC)  # the wheel has stopped: rotate it once
    total = FRAMES + max(count, 1)

    def draw(f):
        if f < FRAMES:  # spinning
            t = f / (FRAMES - 1)
            return frame(final * (1 - (1 - t) ** ease_power), phase=f // 3)
        k = f - FRAMES  # stopped: celebration
        t = k / max(1, count - 1)
        if key in BIG_WINS:
            img = frame(final, phase=k, chase=True, behind=rays(t, rainbow=(key == "rainbow")), disc=still)
            highlight_top(img, 0.6 + 0.4 * abs(math.sin(k * 0.5)))
            if key == "jackpot":
                draw_coins(img, coins, k)
            else:
                tint(img, k * 0.06, 28)
                draw_confetti(img, confetti, k)
            flash(img, 200 * max(0.0, 1 - k / 5))
            banner(img, key, label, shine=(k % 17) / 16)
        elif key in ("c50", "c100"):
            img = frame(final, phase=0, all_on=(k // 2) % 2 == 0, disc=still)
            highlight_top(img, 1.0 if (k // 2) % 2 == 0 else 0.35)
            sparkles(img, k, seed=prize_index)
            banner(img, key, label)
        elif key in ("again", "again2"):
            img = frame(final, phase=0, all_on=True, disc=still)
            highlight_top(img)
            again_effect(img, k)
            banner(img, key, label)
        elif key == "net":
            img = frame(final, phase=0, all_on=False, disc=still)
            spider_web(img, min(1.0, (k + 1) / 8))
            banner(img, key, label)
        else:  # small prizes: just light up the segment
            img = frame(final, phase=0, all_on=True, disc=still)
            highlight_top(img)
            banner(img, key, label)
        return img

    # shared palette built from a few sample frames keeps colours steady
    sample_ids = [0, FRAMES // 2, FRAMES, total - 1]
    samples = {f: draw(f) for f in set(sample_ids)}
    sheet = Image.new("RGB", (CW * len(sample_ids), CH))
    for i, f in enumerate(sample_ids):
        sheet.paste(samples[f].convert("RGB"), (CW * i, 0))
    palette_src = sheet.quantize(colors=255, method=Image.MEDIANCUT)

    # turn each frame into the small palette version straight away, so full-colour frames
    # don't pile up in memory (matters on a 1 GB server)
    pal = []
    for f in range(total):
        img = samples.pop(f, None) or draw(f)
        pal.append(img.convert("RGB").quantize(palette=palette_src, dither=Image.NONE))
    durations = [FRAME_MS] * FRAMES + [CELEBRATE_MS] * (total - FRAMES)
    durations[-1] = HOLD_MS  # stay on the final picture

    buf = BytesIO()
    pal[0].save(buf, "GIF", save_all=True, append_images=pal[1:], duration=durations,
                optimize=False, disposal=1)  # no loop: plays once
    buf.seek(0)
    return buf
