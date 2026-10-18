"""Region looks for champion cards: every Runeterra region gets its own frame and decorations,
and a few champions get an extra touch of their own (SPECIAL)."""
import math
import random

from PIL import Image, ImageDraw, ImageFilter

from stat_card import H, S, W, diagonal_gradient, font, grad_color, gradient_outline, lerp, s, sparkle, star5

# areas decorations stay out of: avatar, name, ELO + coins
KEEP_CLEAR = [(24, 10, 200, 200), (196, 50, 560, 150), (W - 340, 20, W - 20, 192)]
ROLES = {"Fighter": "戰士", "Tank": "坦克", "Mage": "法師", "Assassin": "刺客", "Marksman": "射手", "Support": "輔助"}


def layer():
    return Image.new("RGBA", (W * S, H * S), (0, 0, 0, 0))


def add(img, lay, blur=0, glow=False):
    """Composite a decoration layer; glow=True adds a blurred copy underneath."""
    if glow:
        img.alpha_composite(lay.filter(ImageFilter.GaussianBlur(4 * S)))
    img.alpha_composite(lay.filter(ImageFilter.GaussianBlur(blur * S)) if blur else lay)


def spots(seed, count, bottom=0):
    """Random points in the header (clear of text) plus some in the strip under the panels."""
    rnd = random.Random(seed)
    out = []
    while len(out) < count:
        x, y = rnd.uniform(20, W - 20), rnd.uniform(14, 198)
        if not any(x0 - 6 < x < x1 + 6 and y0 - 6 < y < y1 + 6 for x0, y0, x1, y1 in KEEP_CLEAR):
            out.append((x, y))
    out += [(rnd.uniform(270, 630), rnd.uniform(470, 482)) for _ in range(bottom)]
    return out, rnd


def frame(img, colours, width=4, inner=None, glow=False):
    at = lambda t: grad_color(colours, t)  # noqa: E731
    if glow:
        lay = layer()
        ImageDraw.Draw(lay).rounded_rectangle(s(6, 6, W - 6, H - 6), radius=22 * S, outline=(*colours[0], 255),
                                              width=6 * S)
        img.alpha_composite(lay.filter(ImageFilter.GaussianBlur(8 * S)))
    gradient_outline(img, (6, 6, W - 6, H - 6), at, width, 22, repeats=1.5)
    if inner:
        gradient_outline(img, (14, 14, W - 14, H - 14), lambda t: grad_color(inner, t), 1.5, 17)


def panel_edges(img, panels, colours):
    for box in panels:
        gradient_outline(img, box, lambda t: grad_color(colours, t), 1.5, 18, repeats=1.2)


# ---------- regions ----------

def noxus_fg(img, panels):
    steel = [(235, 236, 242), (120, 122, 132), (210, 212, 220), (90, 92, 102), (230, 232, 238)]
    d = ImageDraw.Draw(img)
    for x0, y0, x1, _ in panels:  # blood-red strip on top of each panel
        d.rounded_rectangle(s(x0 + 18, y0, x1 - 18, y0 + 4), radius=2 * S, fill=(200, 22, 32))
    for inset, cut, width, colours in ((7, 26, 4, steel), (15, 20, 1.5, [(200, 22, 32), (110, 10, 16), (200, 22, 32)])):
        pts = [(inset + cut, inset), (W - inset - cut, inset), (W - inset, inset + cut), (W - inset, H - inset - cut),
               (W - inset - cut, H - inset), (inset + cut, H - inset), (inset, H - inset - cut), (inset, inset + cut)]
        mask = Image.new("L", img.size, 0)
        ImageDraw.Draw(mask).line([s(*p) for p in pts + pts[:2]], fill=255, width=round(width * S), joint="curve")
        img.paste(diagonal_gradient(img.size, lambda t: grad_color(colours, t), repeats=2), (0, 0), mask)
    spikes = layer()
    sd = ImageDraw.Draw(spikes)
    for cx, cy, dx, dy in ((7, 7, 1, 1), (W - 7, 7, -1, 1), (7, H - 7, 1, -1), (W - 7, H - 7, -1, -1)):
        sd.polygon([s(cx + dx * 26, cy), s(cx, cy + dy * 26), s(cx + dx * 4, cy + dy * 4)], fill=(170, 172, 182, 255))
    add(img, spikes)


def demacia_bg(img):
    beams = layer()
    bd = ImageDraw.Draw(beams)
    for x0, w, a in ((380, 60, 40), (470, 26, 30), (640, 80, 34)):
        bd.polygon([s(x0, 0), s(x0 + w, 0), s(x0 + w - 120, 205), s(x0 - 120, 205)], fill=(255, 250, 230, a))
    add(img, beams, blur=12)


def wing(d, cx, cy, flip):
    for k in range(6):  # a fan of feathers
        a = math.radians(200 + k * 14) if flip else math.radians(-20 - k * 14)
        length = 70 - k * 7
        x, y = cx + math.cos(a) * length, cy - abs(math.sin(a)) * length * 0.55
        mx, my = (cx + x) / 2, (cy + y) / 2
        nx, ny = -(y - cy) / length * 7, (x - cx) / length * 7
        d.polygon([s(cx, cy), s(mx + nx, my + ny), s(x, y), s(mx - nx, my - ny)],
                  fill=(255, 246, 220, 235 - k * 18))


def demacia_fg(img, panels):
    lay = layer()
    d = ImageDraw.Draw(lay)
    wing(d, W / 2 - 18, 10, flip=True)
    wing(d, W / 2 + 18, 10, flip=False)
    d.polygon([s(W / 2, -2), s(W / 2 + 11, 10), s(W / 2, 22), s(W / 2 - 11, 10)], fill=(230, 190, 90, 255))
    add(img, lay, glow=True)


def petal(d, x, y, r, angle, colour):
    pts = []
    for k in range(16):
        t = k / 16 * 2 * math.pi
        px, py = math.cos(t) * r, math.sin(t) * r * 0.55 * (1 - 0.35 * math.cos(t))
        ca, sa = math.cos(angle), math.sin(angle)
        pts.append(s(x + px * ca - py * sa, y + px * sa + py * ca))
    d.polygon(pts, fill=colour)


def ionia_bg(img):
    lay = layer()
    d = ImageDraw.Draw(lay)
    pts, rnd = spots(11, 16, bottom=5)
    for x, y in pts:
        petal(d, x, y, rnd.uniform(5, 9), rnd.uniform(0, math.pi), (255, rnd.randint(150, 200), 205, 215))
    add(img, lay, glow=True)


def ionia_fg(img, panels):
    lay = layer()
    d = ImageDraw.Draw(lay)
    rnd = random.Random(3)
    for _ in range(9):  # a drift of petals over the top-right corner
        petal(d, W - rnd.uniform(14, 120), rnd.uniform(4, 30), rnd.uniform(5, 8), rnd.uniform(0, 3), (255, 175, 205, 235))
    add(img, lay, glow=True)


def snowflake(d, x, y, r, colour):
    for k in range(6):
        a = math.radians(k * 60)
        ex, ey = x + math.cos(a) * r, y + math.sin(a) * r
        d.line([s(x, y), s(ex, ey)], fill=colour, width=max(1, round(S * 1.2)))
        for side in (-1, 1):
            b = a + side * math.radians(40)
            mx, my = x + math.cos(a) * r * 0.6, y + math.sin(a) * r * 0.6
            d.line([s(mx, my), s(mx + math.cos(b) * r * 0.35, my + math.sin(b) * r * 0.35)], fill=colour, width=S)


def freljord_bg(img):
    frost = layer()
    ImageDraw.Draw(frost).rectangle(s(0, 0, W, 26), fill=(220, 245, 255, 70))
    add(img, frost, blur=10)
    lay = layer()
    d = ImageDraw.Draw(lay)
    pts, rnd = spots(5, 14, bottom=4)
    for x, y in pts:
        snowflake(d, x, y, rnd.uniform(4, 9), (235, 250, 255, 200))
    add(img, lay, glow=True)


def freljord_fg(img, panels):
    lay = layer()
    d = ImageDraw.Draw(lay)
    rnd = random.Random(8)
    for x in list(range(30, 200, 17)) + list(range(W - 200, W - 20, 17)):  # icicles along the top
        h = rnd.uniform(8, 22)
        d.polygon([s(x - 5, 7), s(x + 5, 7), s(x + rnd.uniform(-1, 1), 7 + h)], fill=(215, 245, 255, 220))
    add(img, lay, glow=True)


def shadow_isles_bg(img):
    mist = layer()
    md = ImageDraw.Draw(mist)
    rnd = random.Random(4)
    for _ in range(9):
        x, y = rnd.uniform(0, W), rnd.choice([rnd.uniform(-20, 40), rnd.uniform(H - 50, H + 20)])
        md.ellipse(s(x - 110, y - 26, x + 110, y + 26), fill=(60, 230, 180, 46))
    add(img, mist, blur=18)
    wisps = layer()
    wd = ImageDraw.Draw(wisps)
    pts, rnd = spots(9, 10, bottom=4)
    for x, y in pts:
        for k in range(5):  # glowing orb with a fading tail
            r = 3.2 - k * 0.5
            wd.ellipse(s(x - r, y + k * 4 - r, x + r, y + k * 4 + r), fill=(140, 255, 220, 230 - k * 45))
    add(img, wisps, glow=True)


def gear(d, cx, cy, r, teeth, colour):
    pts = []
    for k in range(teeth * 2):
        a = math.radians(k * 180 / teeth)
        rr = r if k % 2 == 0 else r * 0.8
        for da in (-0.12, 0.12):
            pts.append(s(cx + math.cos(a + da) * rr, cy + math.sin(a + da) * rr))
    d.polygon(pts, fill=colour)
    d.ellipse(s(cx - r * 0.35, cy - r * 0.35, cx + r * 0.35, cy + r * 0.35), fill=(0, 0, 0, 0))


def piltover_bg(img):
    lay = layer()
    d = ImageDraw.Draw(lay)
    size = 22
    hw, hh = size * math.sqrt(3), size * 1.5
    for row in range(0, 10):
        for col in range(10, int(W / hw) + 2):
            cx, cy = col * hw + (hw / 2 if row % 2 else 0), row * hh
            d.polygon([s(cx + size * math.cos(math.radians(60 * k + 30)), cy + size * math.sin(math.radians(60 * k + 30)))
                       for k in range(6)], outline=(90, 200, 255, 26), width=S)
    add(img, lay)


def piltover_fg(img, panels):
    lay = layer()
    d = ImageDraw.Draw(lay)
    gear(d, W - 8, 8, 30, 10, (205, 160, 80, 235))
    gear(d, W - 46, 4, 16, 8, (175, 130, 60, 235))
    gear(d, 8, 8, 24, 9, (205, 160, 80, 235))
    add(img, lay)


def zaun_bg(img):
    glow = layer()
    ImageDraw.Draw(glow).rectangle(s(0, H - 40, W, H), fill=(120, 255, 90, 60))
    add(img, glow, blur=16)
    lay = layer()
    d = ImageDraw.Draw(lay)
    pts, rnd = spots(6, 18, bottom=6)
    for x, y in pts:
        r = rnd.uniform(2.5, 7)
        d.ellipse(s(x - r, y - r, x + r, y + r), outline=(150, 255, 110, 210), width=S)
        d.ellipse(s(x - r * 0.45, y - r * 0.6, x - r * 0.05, y - r * 0.2), fill=(220, 255, 200, 200))
    add(img, lay, glow=True)


def zaun_fg(img, panels):
    lay = layer()
    d = ImageDraw.Draw(lay)
    rnd = random.Random(2)
    for x in (60, 140, 300, 520, 610, 760, 840):  # toxic drips from the top edge
        length = rnd.uniform(5, 14)
        d.rectangle(s(x - 2, 6, x + 2, 6 + length), fill=(130, 255, 90, 230))
        d.ellipse(s(x - 4.5, 4 + length, x + 4.5, 13 + length), fill=(130, 255, 90, 230))
    add(img, lay, glow=True)


def shurima_bg(img):
    rays = layer()
    rd = ImageDraw.Draw(rays)
    cx, cy = 600, -30
    for k in range(14):
        a = math.radians(20 + k * 10)
        rd.polygon([s(cx, cy), s(cx + math.cos(a - 0.03) * 380, cy + math.sin(a - 0.03) * 380),
                    s(cx + math.cos(a + 0.03) * 380, cy + math.sin(a + 0.03) * 380)], fill=(255, 210, 110, 34))
    add(img, rays, blur=4)
    disc = layer()
    ImageDraw.Draw(disc).ellipse(s(cx - 70, cy - 70, cx + 70, cy + 70), fill=(255, 220, 130, 110))
    add(img, disc, blur=14)


def shurima_fg(img, panels):
    lay = layer()
    d = ImageDraw.Draw(lay)
    colour = (230, 190, 100, 200)
    for k, x in enumerate(range(250, 640, 22)):  # a band of glyphs under the panels
        y = 478
        kind = k % 4
        if kind == 0:
            d.polygon([s(x - 6, y + 6), s(x + 6, y + 6), s(x, y - 6)], outline=colour, width=S)
        elif kind == 1:
            d.ellipse(s(x - 5, y - 5, x + 5, y + 5), outline=colour, width=S)
        elif kind == 2:
            d.line([s(x, y - 7), s(x, y + 7)], fill=colour, width=S)
            d.line([s(x - 5, y - 2), s(x + 5, y - 2)], fill=colour, width=S)
        else:
            d.rectangle(s(x - 5, y - 5, x + 5, y + 5), outline=colour, width=S)
    add(img, lay)


def targon_bg(img):
    lay = layer()
    d = ImageDraw.Draw(lay)
    pts, rnd = spots(13, 40, bottom=8)
    for x, y in pts:
        r = rnd.choice([0.8, 1, 1.2, 1.6])
        d.ellipse(s(x - r, y - r, x + r, y + r), fill=(235, 230, 255, rnd.randint(140, 240)))
    for chain in ([(395, 30), (430, 22), (468, 38), (505, 26), (540, 44)], [(600, 182), (640, 170), (668, 186), (700, 176)]):
        d.line([s(*p) for p in chain], fill=(200, 190, 255, 120), width=S)
        for x, y in chain:
            sparkle(d, x, y, 4, (245, 240, 255, 240))
    add(img, lay, glow=True)


def targon_fg(img, panels):
    lay = layer()
    d = ImageDraw.Draw(lay)
    d.ellipse(s(436, 8, 472, 44), fill=(235, 228, 255, 235))  # crescent moon on the top edge
    d.ellipse(s(444, 2, 480, 38), fill=(0, 0, 0, 0))
    add(img, lay, glow=True)


def bilgewater_bg(img):
    lay = layer()
    d = ImageDraw.Draw(lay)
    for row, (alpha, amp, y0) in enumerate(((70, 4, H - 34), (110, 5, H - 24), (150, 4, H - 14))):
        pts = [s(x, y0 + math.sin(x / 26 + row) * amp) for x in range(0, W + 6, 6)]
        d.polygon(pts + [s(W, H), s(0, H)], fill=(40, 160, 170, alpha))
    add(img, lay, blur=1)


def bilgewater_fg(img, panels):
    lay = layer()
    d = ImageDraw.Draw(lay)
    for x in range(30, W - 20, 14):  # rope along the top edge
        d.ellipse(s(x - 6, 3, x + 6, 11), fill=(196, 150, 96, 255), outline=(110, 76, 40, 255), width=S)
    add(img, lay)


def leaf(d, x, y, length, angle, colour):
    ca, sa = math.cos(angle), math.sin(angle)
    pts = []
    for k in range(13):
        t = k / 12
        pts.append((t * length, math.sin(t * math.pi) * length * 0.28))
    pts += [(t, -w) for t, w in reversed(pts[1:-1])]
    d.polygon([s(x + px * ca - py * sa, y + px * sa + py * ca) for px, py in pts], fill=colour)
    d.line([s(x, y), s(x + length * ca, y + length * sa)], fill=(20, 70, 40, 200), width=S)


def ixtal_fg(img, panels):
    lay = layer()
    d = ImageDraw.Draw(lay)
    rnd = random.Random(7)
    for cx, cy, base in ((W - 10, 10, 2.0), (10, 10, 0.1)):
        for k in range(6):
            leaf(d, cx, cy, rnd.uniform(34, 52), base + k * 0.28, (60 + k * 12, 180 + k * 8, 110, 235))
    add(img, lay, glow=True)


def crack(d, x, y, dx, dy, rnd, steps, width):
    for _ in range(steps):
        nx, ny = x + dx * rnd.uniform(10, 24) + rnd.uniform(-8, 8), y + dy * rnd.uniform(10, 24) + rnd.uniform(-8, 8)
        d.line([s(x, y), s(nx, ny)], fill=(235, 120, 255, 240), width=round(width * S))
        if rnd.random() < 0.35:  # side branch
            crack(d, nx, ny, dx + rnd.uniform(-0.8, 0.8), dy + rnd.uniform(-0.8, 0.8), rnd, 2, width * 0.6)
        x, y = nx, ny


def void_bg(img):
    haze = layer()
    hd = ImageDraw.Draw(haze)
    for cx, cy in ((-20, H + 10), (W + 20, H + 10), (W / 2 + 60, -30)):
        hd.ellipse(s(cx - 140, cy - 70, cx + 140, cy + 70), fill=(150, 40, 220, 110))
    add(img, haze, blur=20)
    lay = layer()
    d = ImageDraw.Draw(lay)
    rnd = random.Random(12)
    for x, y, dx, dy in ((610, 0, 0.3, 1), (14, 205, 1, -0.4), (W - 14, 205, -1, -0.3), (W / 2, 470, 1, 0.1)):
        crack(d, x, y, dx, dy, rnd, 7, 2)
    add(img, lay, glow=True)


def bandle_bg(img):
    lay = layer()
    d = ImageDraw.Draw(lay)
    pts, rnd = spots(21, 14, bottom=5)
    for x, y in pts:
        colour = rnd.choice([(255, 220, 90, 230), (255, 160, 200, 230), (140, 220, 255, 230)])
        star5(d, x, y, rnd.uniform(4, 7), colour)
    add(img, lay, glow=True)


def rune_circle(d, cx, cy, r, colour):
    d.ellipse(s(cx - r, cy - r, cx + r, cy + r), outline=colour, width=S)
    d.ellipse(s(cx - r * 0.82, cy - r * 0.82, cx + r * 0.82, cy + r * 0.82), outline=colour, width=S)
    for k in range(24):
        a = math.radians(k * 15)
        d.line([s(cx + math.cos(a) * r * 0.84, cy + math.sin(a) * r * 0.84),
                s(cx + math.cos(a) * r * (0.94 if k % 3 else 0.99), cy + math.sin(a) * r * (0.94 if k % 3 else 0.99))],
               fill=colour, width=S)
    pts = [s(cx + math.cos(math.radians(k * 72 - 90)) * r * 0.8, cy + math.sin(math.radians(k * 72 - 90)) * r * 0.8)
           for k in (0, 2, 4, 1, 3, 0)]
    d.line(pts, fill=colour, width=S)


def runeterra_bg(img):
    lay = layer()
    rune_circle(ImageDraw.Draw(lay), 610, 105, 118, (150, 200, 255, 70))
    add(img, lay, glow=True)


# name, frame colours, inner frame line, panel edges, background (after the art), foreground, frame glow
REGIONS = {
    "noxus": ("諾克薩斯", None, None, [(200, 30, 36), (90, 90, 100), (200, 30, 36)], None, noxus_fg, False),
    "demacia": ("蒂瑪西亞", [(255, 255, 255), (225, 185, 90), (255, 250, 235)], [(60, 110, 220), (130, 170, 255)],
                [(240, 236, 225), (220, 180, 90)], demacia_bg, demacia_fg, False),
    "ionia": ("愛歐尼亞", [(255, 170, 200), (220, 50, 80), (255, 200, 215)], None,
              [(255, 160, 190), (210, 60, 90)], ionia_bg, ionia_fg, False),
    "freljord": ("弗雷爾卓德", [(255, 255, 255), (120, 210, 255), (60, 120, 190)], [(200, 240, 255), (255, 255, 255)],
                 [(170, 225, 255), (90, 150, 210)], freljord_bg, freljord_fg, False),
    "shadow-isles": ("闇影島", [(90, 255, 200), (20, 120, 100), (90, 255, 200)], None,
                     [(70, 220, 170), (20, 90, 80)], shadow_isles_bg, None, True),
    "piltover": ("皮爾托福", [(240, 200, 120), (160, 110, 50), (240, 200, 120)], [(80, 210, 255), (170, 235, 255)],
                 [(220, 180, 100), (80, 200, 255)], piltover_bg, piltover_fg, False),
    "zaun": ("佐恩", [(140, 255, 90), (150, 70, 200), (140, 255, 90)], None,
             [(130, 240, 90), (150, 80, 200)], zaun_bg, zaun_fg, True),
    "shurima": ("蘇瑞瑪", [(255, 225, 140), (200, 140, 40), (255, 225, 140)], [(40, 200, 190), (120, 230, 220)],
                [(240, 200, 110), (40, 190, 180)], shurima_bg, shurima_fg, False),
    "mount-targon": ("巨石峰", [(240, 240, 255), (150, 120, 230), (240, 240, 255)], None,
                     [(200, 190, 255), (120, 100, 210)], targon_bg, targon_fg, False),
    "bilgewater": ("比爾吉沃特", [(60, 180, 190), (200, 110, 50), (60, 180, 190)], None,
                   [(60, 170, 180), (190, 110, 60)], bilgewater_bg, bilgewater_fg, False),
    "ixtal": ("伊克斯塔爾", [(110, 230, 150), (30, 130, 90), (230, 200, 110)], None,
              [(100, 210, 140), (220, 190, 100)], None, ixtal_fg, False),
    "void": ("虛空", [(230, 100, 255), (90, 20, 160), (230, 100, 255)], None,
             [(210, 90, 255), (110, 30, 180)], void_bg, None, True),
    "bandle-city": ("班德爾城", [(255, 220, 90), (255, 140, 60), (255, 220, 90)], [(255, 255, 255), (255, 235, 160)],
                    [(255, 210, 90), (255, 150, 80)], bandle_bg, None, False),
    "runeterra": ("符文大地", [(150, 200, 255), (220, 180, 90), (150, 200, 255)], None,
                  [(140, 190, 255), (210, 170, 90)], runeterra_bg, None, False),
}


# ---------- champion specials ----------

def darius_special(img):
    lay = layer()
    d = ImageDraw.Draw(lay)
    for i in range(5):  # Hemorrhage: 5 stacks
        x, y, r = W / 2 - 44 + i * 22, 473, 4.5
        d.ellipse(s(x - r, y - r * 0.2, x + r, y + r * 1.8), fill=(200, 22, 32, 255))
        d.polygon([s(x - r * 0.92, y + r * 0.5), s(x, y - r * 1.6), s(x + r * 0.92, y + r * 0.5)], fill=(200, 22, 32, 255))
    add(img, lay, glow=True)


SPECIAL = {"Darius": darius_special}


# ---------- used by stat_card ----------

def background(img, t):
    draw_bg = REGIONS.get(t.get("region"), REGIONS["runeterra"])[4]
    if draw_bg:
        draw_bg(img)


def foreground(img, t, panels):
    name, colours, inner, edges, _, draw_fg, glow = REGIONS.get(t.get("region"), REGIONS["runeterra"])
    panel_edges(img, panels, edges)
    if colours:
        frame(img, colours, inner=inner, glow=glow)
    if draw_fg:
        draw_fg(img, panels)
    if t.get("champ_id") in SPECIAL:
        SPECIAL[t["champ_id"]](img)

    d = ImageDraw.Draw(img)
    roles = "／".join(ROLES.get(r, r) for r in t.get("roles", []))
    d.text(s(40, 476), f"{name}　{roles}".strip(), font=font("bold", 14), fill=lerp(edges[0], (255, 255, 255), 0.25),
           anchor="lm")
