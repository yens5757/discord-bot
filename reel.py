"""英雄卡轉盤 animation: a slot-machine reel of champion icons that stops on the prize,
then fades into the champion's splash art."""
import random
from io import BytesIO

from PIL import Image, ImageDraw, ImageEnhance, ImageFilter

import champions
from stat_card import font as card_font

CW, CH = 520, 360        # GIF size
ICON, GAP = 100, 14      # reel icons
STEP = ICON + GAP
REEL_Y = 175             # centre line of the reel
TARGET = 42              # position of the prize in the strip
SPIN_FRAMES, FRAME_MS = 48, 40
REVEAL_FRAMES, REVEAL_MS = 14, 60
HOLD_MS = 6000
SECONDS = (SPIN_FRAMES * FRAME_MS + REVEAL_FRAMES * REVEAL_MS) / 1000  # until the result is showing
GOLD = (255, 214, 110)


def font(weight, size):
    # stat_card fonts are scaled by 2; ask for half size to get `size` px
    return card_font(weight, max(1, round(size / 2)))


def lerp(c1, c2, t):
    return tuple(round(a + (b - a) * t) for a, b in zip(c1, c2))


def background():
    bg = Image.new("RGBA", (CW, CH))
    d = ImageDraw.Draw(bg)
    for y in range(CH):
        d.line([(0, y), (CW, y)], fill=lerp((16, 14, 28), (34, 24, 46), y / CH))
    glow = Image.new("RGBA", (CW, CH), (0, 0, 0, 0))
    ImageDraw.Draw(glow).ellipse((CW / 2 - 200, REEL_Y - 90, CW / 2 + 200, REEL_Y + 90), fill=(255, 190, 80, 55))
    bg.alpha_composite(glow.filter(ImageFilter.GaussianBlur(40)))
    d = ImageDraw.Draw(bg)
    d.text((CW / 2, 46), "英雄卡轉盤", font=font("bold", 32), fill=(255, 236, 190), anchor="mm")
    d.rounded_rectangle((16, REEL_Y - 66, CW - 16, REEL_Y + 66), radius=18, fill=(10, 10, 18), outline=(120, 96, 50), width=2)
    return bg


def icon(cid):
    try:
        im = Image.open(champions.icon_path(cid)).convert("RGBA").resize((ICON, ICON), Image.LANCZOS)
    except OSError:
        im = Image.new("RGBA", (ICON, ICON), (60, 60, 80, 255))
    mask = Image.new("L", (ICON * 4, ICON * 4), 0)
    ImageDraw.Draw(mask).rounded_rectangle((0, 0, ICON * 4 - 1, ICON * 4 - 1), radius=56, fill=255)
    im.putalpha(mask.resize((ICON, ICON), Image.LANCZOS))
    return im


def reel_frame(bg, strip, icons, pos, selected=False):
    img = bg.copy()
    window = Image.new("RGBA", (CW - 36, 128), (0, 0, 0, 0))
    first = max(0, int((pos - CW / 2) // STEP) - 1)
    for i in range(first, min(len(strip), first + 7)):
        x = CW / 2 + i * STEP - pos - ICON / 2 - 18
        window.alpha_composite(icons[strip[i]], (round(x), 14))
    img.alpha_composite(window, (18, REEL_Y - 64))
    d = ImageDraw.Draw(img)
    # fade the reel at both ends
    for k in range(60):
        a = round(235 * (1 - k / 60) ** 1.5)
        d.line([(18 + k, REEL_Y - 64), (18 + k, REEL_Y + 64)], fill=(10, 10, 18, a))
        d.line([(CW - 19 - k, REEL_Y - 64), (CW - 19 - k, REEL_Y + 64)], fill=(10, 10, 18, a))
    # centre marker
    box = (CW / 2 - ICON / 2 - 6, REEL_Y - ICON / 2 - 6, CW / 2 + ICON / 2 + 6, REEL_Y + ICON / 2 + 6)
    d.rounded_rectangle(box, radius=16, outline=(255, 245, 210) if selected else GOLD, width=5 if selected else 3)
    d.polygon([(CW / 2 - 12, REEL_Y - 78), (CW / 2 + 12, REEL_Y - 78), (CW / 2, REEL_Y - 62)], fill=GOLD)
    d.polygon([(CW / 2 - 12, REEL_Y + 78), (CW / 2 + 12, REEL_Y + 78), (CW / 2, REEL_Y + 62)], fill=GOLD)
    return img


def reveal_frame(splash, cid, t):
    """Splash art fading in over the stopped reel, with the champion's name and title."""
    img = splash.copy()
    img.putalpha(round(255 * t))
    d = ImageDraw.Draw(img)
    info = champions.CHAMPIONS.get(cid, {"name": cid, "title": ""})
    a = round(255 * t)
    d.rounded_rectangle((60, CH - 96, CW - 60, CH - 22), radius=20, fill=(14, 12, 22, round(200 * t)),
                        outline=(*GOLD, a), width=3)
    d.text((CW / 2, CH - 70), info["name"], font=font("bold", 30), fill=(255, 255, 255, a), anchor="mm",
           stroke_width=2, stroke_fill=(20, 16, 30, a))
    d.text((CW / 2, CH - 38), info["title"], font=font("bold", 17), fill=(*GOLD, a), anchor="mm")
    return img


def splash_image(cid):
    try:
        src = Image.open(champions.art_path(cid)).convert("RGB")
    except OSError:
        return Image.new("RGBA", (CW, CH), (30, 26, 44, 255))
    fx, fy = champions.FOCUS.get(cid, champions.DEFAULT_FOCUS)
    cw = src.width * 0.62
    ch = cw * CH / CW
    left = min(max(fx * src.width - cw / 2, 0), src.width - cw)
    top = min(max(fy * src.height - ch * 0.32, 0), src.height - ch)
    pic = src.resize((CW, CH), Image.LANCZOS, box=(left, top, left + cw, top + ch))
    return ImageEnhance.Brightness(pic).enhance(0.9).convert("RGBA")


def render_reel_preview(pool):
    """Still picture of the reel for the /spin menu (PNG bytes): random icons, a ? in the middle."""
    rnd = random.Random(7)
    strip = [rnd.choice(pool) for _ in range(9)] if pool else ["?"] * 9
    icons = {c: icon(c) for c in set(strip)}
    img = reel_frame(background(), strip, icons, 4 * STEP)
    d = ImageDraw.Draw(img)
    d.rounded_rectangle((CW / 2 - ICON / 2, REEL_Y - ICON / 2, CW / 2 + ICON / 2, REEL_Y + ICON / 2), radius=14,
                        fill=(24, 20, 38))
    d.text((CW / 2, REEL_Y + 2), "?", font=font("bold", 64), fill=GOLD, anchor="mm")
    d.text((CW / 2, CH - 52), "必定抽到未擁有的英雄卡", font=font("bold", 22), fill=(255, 236, 190), anchor="mm")
    buf = BytesIO()
    img.convert("RGB").save(buf, "PNG", optimize=True)
    return buf.getvalue()


def render_reel_gif(cid, pool):
    """Animated GIF that lands on champion `cid`. `pool` = champion ids to fill the reel with."""
    champions.download(cid)
    strip = [random.choice(pool) for _ in range(TARGET + 4)]
    strip[TARGET] = cid
    icons = {c: icon(c) for c in set(strip)}
    bg = background()
    end = TARGET * STEP

    frames = []
    for f in range(SPIN_FRAMES):
        t = f / (SPIN_FRAMES - 1)
        frames.append(reel_frame(bg, strip, icons, end * (1 - (1 - t) ** 3.2)))
    stopped = reel_frame(bg, strip, icons, end, selected=True)
    splash = splash_image(cid)
    for k in range(REVEAL_FRAMES):
        img = stopped.copy()
        img.alpha_composite(reveal_frame(splash, cid, (k + 1) / REVEAL_FRAMES))
        frames.append(img)

    samples = [frames[0], frames[SPIN_FRAMES // 2], frames[-1]]
    sheet = Image.new("RGB", (CW * len(samples), CH))
    for i, fr in enumerate(samples):
        sheet.paste(fr.convert("RGB"), (CW * i, 0))
    palette = sheet.quantize(colors=255, method=Image.MEDIANCUT)
    pal = [fr.convert("RGB").quantize(palette=palette, dither=Image.NONE) for fr in frames]
    durations = [FRAME_MS] * SPIN_FRAMES + [REVEAL_MS] * REVEAL_FRAMES
    durations[-1] = HOLD_MS

    buf = BytesIO()
    pal[0].save(buf, "GIF", save_all=True, append_images=pal[1:], duration=durations, optimize=False, disposal=1)
    buf.seek(0)
    return buf
