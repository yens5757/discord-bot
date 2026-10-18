"""League of Legends champion cards: names, splash art and icons from Riot's Data Dragon.

Everything is downloaded once and kept in champ_data/, so it also works if Riot is unreachable later.
Art is used under Riot's "Legal Jibber Jabber" policy for free fan projects.
"""
import colorsys
import json
import urllib.request
from functools import lru_cache
from pathlib import Path

from PIL import Image

DATA_DIR = Path(__file__).with_name("champ_data")
DDRAGON = "https://ddragon.leagueoflegends.com"
PREFIX = "champ:"             # card style key for a champion, e.g. "champ:Darius"

CHAMPIONS = {}                # champion id -> {"name": "達瑞斯", "title": "諾克薩斯之力"}
VERSION = None                # Data Dragon patch the data came from

# hand-tuned framing where the default puts the face in the wrong place: id -> (x, y) of the face, 0-1
FOCUS = {}
DEFAULT_FOCUS = (0.52, 0.27)


def fetch(url):
    request = urllib.request.Request(url, headers={"User-Agent": "discord-bot (fan project)"})
    with urllib.request.urlopen(request, timeout=30) as response:
        return response.read()


def load_list():
    """Champion names and titles in Traditional Chinese. Falls back to the saved copy if offline."""
    global VERSION
    saved = DATA_DIR / "champions.json"
    try:
        VERSION = json.loads(fetch(f"{DDRAGON}/api/versions.json"))[0]
        data = json.loads(fetch(f"{DDRAGON}/cdn/{VERSION}/data/zh_TW/champion.json"))["data"]
        champs = {cid: {"name": c["name"], "title": c["title"]} for cid, c in data.items()}
        DATA_DIR.mkdir(exist_ok=True)
        saved.write_text(json.dumps({"version": VERSION, "champions": champs}, ensure_ascii=False), "utf-8")
    except Exception as e:
        if not saved.exists():
            print(f"Couldn't load champions: {e!r}")
            return
        cached = json.loads(saved.read_text("utf-8"))
        VERSION, champs = cached["version"], cached["champions"]
    CHAMPIONS.clear()
    CHAMPIONS.update(sorted(champs.items(), key=lambda kv: kv[1]["name"]))


def art_path(cid):
    return DATA_DIR / "art" / f"{cid}.jpg"


def icon_path(cid):
    return DATA_DIR / "icons" / f"{cid}.png"


ART_NAME = {"Fiddlesticks": "FiddleSticks"}  # Riot spells a few art files differently


def download(cid):
    """Splash art and icon for one champion, if not on disk yet. Returns False if something is missing."""
    art = ART_NAME.get(cid, cid)
    wanted = (
        (art_path(cid), [f"{DDRAGON}/cdn/img/champion/centered/{art}_0.jpg",
                         f"{DDRAGON}/cdn/img/champion/splash/{art}_0.jpg"]),
        (icon_path(cid), [f"{DDRAGON}/cdn/{VERSION}/img/champion/{cid}.png"]),
    )
    ok = True
    for path, urls in wanted:
        if path.exists():
            continue
        for url in urls:
            try:
                data = fetch(url)
            except Exception:
                continue
            path.parent.mkdir(parents=True, exist_ok=True)
            tmp = path.with_suffix(".tmp")
            tmp.write_bytes(data)
            tmp.replace(path)
            break
        else:
            ok = False
    return ok


def download_all():
    missing = [cid for cid in CHAMPIONS if not download(cid)]
    if missing:
        print(f"Couldn't download art for: {', '.join(missing)}")
    print(f"Champion art ready ({len(CHAMPIONS) - len(missing)}/{len(CHAMPIONS)})")


# ---------- card styles ----------

def is_champion(style):
    return style.startswith(PREFIX)


def style_key(cid):
    return PREFIX + cid


def champion_id(style):
    return style[len(PREFIX):]


def label(style):
    """Display name: 達瑞斯 for a champion card."""
    c = CHAMPIONS.get(champion_id(style))
    return c["name"] if c else champion_id(style)


def hsv(h, s, v):
    return tuple(round(c * 255) for c in colorsys.hsv_to_rgb(h % 1, min(max(s, 0), 1), min(max(v, 0), 1)))


def main_colours(path):
    """The splash's most eye-catching colour, plus a second one with a different hue."""
    small = Image.open(path).convert("RGB").resize((96, 54))
    q = small.quantize(colors=10, method=Image.MEDIANCUT)
    pal = q.getpalette()
    scored = []
    for count, i in q.getcolors():
        h, s, v = colorsys.rgb_to_hsv(*(c / 255 for c in pal[i * 3:i * 3 + 3]))
        scored.append((count * s ** 1.5 * v ** 0.6, h, s, v))
    scored.sort(reverse=True)
    _, h1, s1, _ = scored[0]
    second = next((x for x in scored[1:] if min(abs(x[1] - h1), 1 - abs(x[1] - h1)) > 0.12 and x[2] > 0.25), None)
    h2, s2 = (second[1], second[2]) if second else (h1 + 0.09, s1)
    return (h1, max(s1, 0.55)), (h2, max(s2, 0.5))


@lru_cache(maxsize=48)
def theme(style):
    """Card colours for a champion, picked from their splash art."""
    cid = champion_id(style)
    download(cid)
    art = art_path(cid) if art_path(cid).exists() else None
    (h, s), (h2, s2) = main_colours(art) if art else ((0.11, 0.7), (0.0, 0.6))  # no art: gold and red
    accent, second = hsv(h, s, 0.92), hsv(h2, s2, 0.9)
    info = CHAMPIONS.get(cid, {"name": cid, "title": ""})
    return dict(
        bg=(hsv(h, s * 0.7, 0.07), hsv(h, s * 0.75, 0.16)), glows=(accent, second), glow_alpha=60,
        panel=hsv(h, 0.3, 0.13), edge=hsv(h, 0.4, 0.32), track=hsv(h, 0.32, 0.25), shadow=120,
        text=(246, 244, 242), muted=hsv(h, 0.16, 0.74),
        accent=(hsv(h, s * 0.45, 1.0), accent), mix=accent, flex=second,
        fx="champion", art=art, focus=FOCUS.get(cid, DEFAULT_FOCUS),
        champ_name=info["name"], champ_title=info["title"],
    )
