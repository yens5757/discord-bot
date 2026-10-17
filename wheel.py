"""Lucky wheel: prize table and the spinning-wheel GIF."""
import math
import random
from io import BytesIO

from PIL import Image, ImageDraw

from stat_card import font as card_font

# (key, label on the wheel, weight %, kind, value)
#   kind "coins": value = credits won
#   kind "again": a free extra spin
#   kind "style": value = card style key (if already owned you get STYLE_REFUND credits)
PRIZES = [
    ("net",     "收網",     30, "coins", 0),
    ("c10",     "10 金幣",  25, "coins", 10),
    ("c100",    "100 金幣",  5, "coins", 100),
    ("c25",     "25 金幣",  20, "coins", 25),
    ("rainbow", "彩虹卡面",  1, "style", "rainbow"),
    ("c50",     "50 金幣",  10, "coins", 50),
    ("again",   "再轉一次",  8, "again", 0),
    ("jackpot", "250 金幣",  1, "coins", 250),
]
STYLE_REFUND = 150

SEG_COLORS = [
    (72, 84, 120), (88, 101, 242), (240, 84, 92), (67, 181, 121),
    (190, 100, 255), (255, 160, 70), (40, 170, 220), (255, 196, 60),
]

SIZE = 480               # GIF width
WHEEL = 420              # wheel diameter
FRAMES = 56
FRAME_MS = 40
HOLD_MS = 5000


def roll():
    """Pick a prize index using the weights."""
    return random.choices(range(len(PRIZES)), weights=[p[2] for p in PRIZES])[0]


def font(weight, size):
    # stat_card fonts are scaled by 2; ask for half size to get `size` px
    return card_font(weight, max(1, size // 2))


def draw_wheel():
    """The wheel at rest, segment 0 under the pointer (top)."""
    big = WHEEL * 3
    img = Image.new("RGBA", (big, big), (0, 0, 0, 0))
    d = ImageDraw.Draw(img)
    n = len(PRIZES)
    step = 360 / n
    for i in range(n):
        start = -90 - step / 2 + i * step
        d.pieslice((0, 0, big - 1, big - 1), start, start + step,
                   fill=SEG_COLORS[i % len(SEG_COLORS)], outline=(20, 22, 30), width=6)
    d.ellipse((0, 0, big - 1, big - 1), outline=(250, 250, 255), width=18)

    # labels, written outwards along each segment
    f = font("bold", 66)
    for i, (_, label, *_rest) in enumerate(PRIZES):
        angle = i * step  # clockwise from top
        text = Image.new("RGBA", (big // 2, 120), (0, 0, 0, 0))
        ImageDraw.Draw(text).text((big // 4, 60), label, font=f, fill=(255, 255, 255),
                                  anchor="mm", stroke_width=4, stroke_fill=(20, 22, 30))
        text = text.rotate(90 - angle, expand=True, resample=Image.BICUBIC)
        r = big * 0.31
        cx = big / 2 + r * math.sin(math.radians(angle))
        cy = big / 2 - r * math.cos(math.radians(angle))
        img.alpha_composite(text, (round(cx - text.width / 2), round(cy - text.height / 2)))
    return img.resize((WHEEL, WHEEL), Image.LANCZOS)


_WHEEL = None


def wheel_image():
    global _WHEEL
    if _WHEEL is None:
        _WHEEL = draw_wheel()
    return _WHEEL


def background():
    bg = Image.new("RGBA", (SIZE, SIZE + 70), (22, 24, 34, 255))
    d = ImageDraw.Draw(bg)
    for y in range(bg.height):
        t = y / bg.height
        d.line([(0, y), (SIZE, y)], fill=(round(22 + 12 * t), round(24 + 10 * t), round(34 + 22 * t)))
    return bg


def overlay(frame):
    """Pointer and hub drawn on top of the wheel."""
    d = ImageDraw.Draw(frame)
    cx, top = SIZE // 2, (SIZE - WHEEL) // 2
    d.polygon([(cx - 22, top - 14), (cx + 22, top - 14), (cx, top + 34)],
              fill=(255, 255, 255), outline=(20, 22, 30))
    cy = SIZE // 2
    d.ellipse((cx - 34, cy - 34, cx + 34, cy + 34), fill=(250, 250, 255), outline=(20, 22, 30), width=3)
    d.text((cx, cy), "轉", font=font("bold", 30), fill=(30, 32, 44), anchor="mm")


def render_spin_gif(prize_index):
    """Animated GIF of the wheel spinning and stopping on prize_index."""
    n = len(PRIZES)
    step = 360 / n
    final = 360 * 5 + prize_index * step + random.uniform(-step * 0.35, step * 0.35)
    wheel = wheel_image()
    bg = background()
    offset = ((SIZE - WHEEL) // 2, (SIZE - WHEEL) // 2)

    frames = []
    for f in range(FRAMES):
        t = f / (FRAMES - 1)
        angle = final * (1 - (1 - t) ** 3)  # ease-out: fast, then slowing down
        frame = bg.copy()
        frame.alpha_composite(wheel.rotate(angle, resample=Image.BICUBIC), offset)
        overlay(frame)
        frames.append(frame)

    # last frame: result banner
    last = frames[-1].copy()
    d = ImageDraw.Draw(last)
    label = PRIZES[prize_index][1]
    banner = "被收網了！" if PRIZES[prize_index][0] == "net" else f"獲得 {label}！"
    d.rounded_rectangle((60, SIZE - 2, SIZE - 60, SIZE + 56), radius=29,
                        fill=SEG_COLORS[prize_index % len(SEG_COLORS)], outline=(250, 250, 255), width=3)
    d.text((SIZE // 2, SIZE + 27), banner, font=font("bold", 30), fill=(255, 255, 255),
           anchor="mm", stroke_width=2, stroke_fill=(20, 22, 30))
    frames.append(last)

    pal = [fr.convert("RGB").quantize(colors=128, method=Image.MEDIANCUT) for fr in frames]
    durations = [FRAME_MS] * FRAMES + [HOLD_MS]
    buf = BytesIO()
    pal[0].save(buf, "GIF", save_all=True, append_images=pal[1:], duration=durations,
                optimize=False, disposal=1)  # no loop: plays once
    buf.seek(0)
    return buf
