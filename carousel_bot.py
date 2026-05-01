#!/usr/bin/env python3
"""
Instagram Carousel Generator Bot
Telegram бот для создания каруселей для Instagram
Использует OpenRouter API для AI-текста и AI-фонов, если ключ задан
"""

import os, io, re, json, zipfile, asyncio, logging, tempfile, base64, hashlib
from typing import Optional
from dataclasses import dataclass, field
from PIL import Image, ImageDraw, ImageFont, ImageChops
import openai
from telegram import Update, InlineKeyboardButton, InlineKeyboardMarkup
from telegram.ext import (
    Application, CommandHandler, MessageHandler, CallbackQueryHandler,
    ConversationHandler, filters, ContextTypes
)
from reportlab.pdfgen import canvas as pdf_canvas
import aiohttp

logging.basicConfig(
    format="%(asctime)s | %(levelname)s | %(message)s",
    level=logging.INFO
)
logger = logging.getLogger(__name__)

# ─── Конфигурация ─────────────────────────────────────────────────────────────
def env_value(*names: str) -> str:
    for name in names:
        value = os.getenv(name)
        if value and value.strip():
            return value.strip().strip('"').strip("'")
    return ""

def env_present(name: str) -> str:
    value = os.getenv(name)
    return "yes" if value and value.strip() else "no"

TELEGRAM_TOKEN      = env_value("TELEGRAM_TOKEN", "BOT_TOKEN", "TELEGRAM_BOT_TOKEN")
OPENROUTER_API_KEY  = env_value("OPENROUTER_API_KEY", "OPENAI_API_KEY")
OPENROUTER_BASE_URL = "https://openrouter.ai/api/v1"
TEXT_MODEL          = env_value("TEXT_MODEL") or "anthropic/claude-sonnet-4.5"
IMAGE_MODEL         = env_value("IMAGE_MODEL") or "google/gemini-2.5-flash-image"
BOT_TITLE           = env_value("BOT_TITLE") or "Carousel Bot"

SLIDE_W, SLIDE_H = 1080, 1350

# ─── Состояния диалога ────────────────────────────────────────────────────────
(
    MAIN_MENU,
    CHOOSE_SLIDES,
    CHOOSE_CONTENT,
    ENTER_TOPIC,
    ENTER_SLIDE_TEXT,
    CHOOSE_BG,
    CHOOSE_TEMPLATE,
    ENTER_BG_PROMPT,
    WAIT_BG_PHOTO,
    CHOOSE_COLOR,
    CHOOSE_STYLE,
    CONFIRM,
    CHOOSE_FORMAT,
) = range(13)

# ─── Дизайн-шаблоны ───────────────────────────────────────────────────────────
TEMPLATES = {
    "purple":   {"name": "💜 Purple Dream",  "c1": (75, 0, 130),    "c2": (138, 43, 226),  "tc": "#FFFFFF"},
    "ocean":    {"name": "🌊 Ocean Blue",     "c1": (0, 119, 182),   "c2": (0, 180, 216),   "tc": "#FFFFFF"},
    "sunset":   {"name": "🌅 Sunset",         "c1": (255, 94, 58),   "c2": (255, 182, 104), "tc": "#FFFFFF"},
    "forest":   {"name": "🌿 Forest",         "c1": (34, 85, 34),    "c2": (0, 168, 107),   "tc": "#FFFFFF"},
    "dark":     {"name": "🖤 Minimal Dark",   "c1": (20, 20, 20),    "c2": (50, 50, 50),    "tc": "#FFFFFF"},
    "rose":     {"name": "🌸 Rose Gold",      "c1": (183, 110, 121), "c2": (245, 192, 176), "tc": "#FFFFFF"},
    "midnight": {"name": "🌙 Midnight",       "c1": (15, 12, 41),    "c2": (48, 43, 99),    "tc": "#E0E0FF"},
    "citrus":   {"name": "🍋 Citrus",         "c1": (247, 183, 51),  "c2": (244, 92, 67),   "tc": "#FFFFFF"},
}

COLOR_PRESETS = {
    "white":  {"name": "⬜ Белый",       "bg": (255, 255, 255), "tc": "#333333"},
    "black":  {"name": "⬛ Чёрный",      "bg": (0, 0, 0),       "tc": "#FFFFFF"},
    "beige":  {"name": "🟫 Бежевый",     "bg": (245, 235, 220), "tc": "#333333"},
    "navy":   {"name": "🔵 Тёмно-синий", "bg": (15, 30, 80),    "tc": "#FFFFFF"},
    "custom": {"name": "🎨 Свой HEX",    "bg": None,            "tc": "#FFFFFF"},
}

STYLE_PRESETS = {
    "modern": {
        "name": "⚡ Modern",
        "desc": "контрастные карточки, крупный гротеск",
        "title": 76,
        "body": 54,
        "cta": 68,
        "panel": "dark_cover",
        "radius": 34,
        "align": "left",
    },
    "editorial": {
        "name": "📰 Editorial",
        "desc": "журнальный стиль, светлые панели",
        "title": 70,
        "body": 48,
        "cta": 62,
        "panel": "light",
        "radius": 12,
        "align": "left",
    },
    "bold": {
        "name": "🔥 Bold",
        "desc": "максимально крупный жирный текст",
        "title": 86,
        "body": 60,
        "cta": 76,
        "panel": "solid_dark",
        "radius": 0,
        "align": "center",
    },
    "soft": {
        "name": "🌿 Soft",
        "desc": "мягкие карточки и спокойная типографика",
        "title": 68,
        "body": 48,
        "cta": 60,
        "panel": "frosted",
        "radius": 42,
        "align": "left",
    },
    "minimal": {
        "name": "◻️ Minimal",
        "desc": "чистая верстка, меньше декора",
        "title": 72,
        "body": 50,
        "cta": 64,
        "panel": "minimal",
        "radius": 6,
        "align": "left",
    },
}

# ─── Сессия пользователя ──────────────────────────────────────────────────────
@dataclass
class Session:
    slides_count:   int            = 5
    content_type:   str            = "manual"
    topic:          str            = ""
    slide_texts:    list           = field(default_factory=list)
    current_slide:  int            = 0
    bg_type:        str            = "template"
    bg_template:    str            = "purple"
    bg_prompt:      str            = ""
    bg_photo:       Optional[bytes] = None
    bg_photos:      list           = field(default_factory=list)
    current_bg_slide: int          = 0
    bg_c1:          tuple          = (75, 0, 130)
    bg_c2:          Optional[tuple] = (138, 43, 226)
    text_color:     str            = "#FFFFFF"
    style:          str            = "modern"
    export_format:  str            = "both"

_sessions: dict[int, Session] = {}

def sess(uid: int) -> Session:
    if uid not in _sessions:
        _sessions[uid] = Session()
    return _sessions[uid]

# ─── Шрифты ───────────────────────────────────────────────────────────────────
_FONT_CACHE: dict = {}
_FONT_DIR = os.path.join(tempfile.gettempdir(), "carousel_fonts")

def _ensure_fonts() -> dict:
    os.makedirs(_FONT_DIR, exist_ok=True)
    paths = {
        "regular": os.path.join(_FONT_DIR, "Roboto-Regular.ttf"),
        "bold":    os.path.join(_FONT_DIR, "Roboto-Bold.ttf"),
    }
    if os.getenv("DOWNLOAD_FONTS", "0") != "1":
        return paths

    # Зеркала шрифтов (несколько на случай недоступности)
    urls = {
        "regular": [
            "https://fonts.gstatic.com/s/roboto/v47/KFOMCnqEu92Fr1ME7kSn66aGLdTylUAMQXC89YmC2DY.ttf",
            "https://raw.githubusercontent.com/googlefonts/roboto/main/src/hinted/Roboto-Regular.ttf",
        ],
        "bold": [
            "https://fonts.gstatic.com/s/roboto/v47/KFOMCnqEu92Fr1ME7kSn66aGLdTylUAMQXC89YmC2DY.ttf",
            "https://raw.githubusercontent.com/googlefonts/roboto/main/src/hinted/Roboto-Bold.ttf",
        ],
    }
    import urllib.request
    for key, path in paths.items():
        if not os.path.exists(path):
            for url in urls[key]:
                try:
                    logger.info(f"Загружаю шрифт {key} с {url[:50]}...")
                    with urllib.request.urlopen(url, timeout=8) as resp:
                        with open(path, "wb") as out:
                            out.write(resp.read())
                    logger.info(f"Шрифт {key} загружен успешно")
                    break
                except Exception as e:
                    logger.warning(f"Не удалось загрузить шрифт {key}: {e}")
    return paths

_font_paths = _ensure_fonts()

# Системные fallback-шрифты (приоритет — самые распространённые)
_SYS_FONTS = {
    True: [
        "/usr/share/fonts/truetype/liberation/LiberationSans-Bold.ttf",
        "/usr/share/fonts/truetype/crosextra/Carlito-Bold.ttf",
        "/usr/share/fonts/truetype/dejavu/DejaVuSans-Bold.ttf",
        "/usr/share/fonts/truetype/ubuntu/Ubuntu-B.ttf",
        "/System/Library/Fonts/Helvetica.ttc",
        "/Library/Fonts/Arial Bold.ttf",
        "C:/Windows/Fonts/arialbd.ttf",
    ],
    False: [
        "/usr/share/fonts/truetype/liberation/LiberationSans-Regular.ttf",
        "/usr/share/fonts/truetype/crosextra/Carlito-Regular.ttf",
        "/usr/share/fonts/truetype/dejavu/DejaVuSans.ttf",
        "/usr/share/fonts/truetype/ubuntu/Ubuntu-R.ttf",
        "/System/Library/Fonts/Helvetica.ttc",
        "/Library/Fonts/Arial.ttf",
        "C:/Windows/Fonts/arial.ttf",
    ],
}

def get_font(size: int, bold: bool = False) -> ImageFont.FreeTypeFont:
    key = (size, bold)
    if key in _FONT_CACHE:
        return _FONT_CACHE[key]
    candidates = [_font_paths["bold"] if bold else _font_paths["regular"]] + _SYS_FONTS[bold]
    for path in candidates:
        if path and os.path.exists(path):
            try:
                f = ImageFont.truetype(path, size)
                _FONT_CACHE[key] = f
                return f
            except Exception:
                pass
    f = ImageFont.load_default()
    _FONT_CACHE[key] = f
    return f

# ─── Утилиты изображений ──────────────────────────────────────────────────────
def make_gradient(w: int, h: int, c1: tuple, c2: tuple) -> Image.Image:
    try:
        import numpy as np
        arr = np.zeros((h, w, 3), dtype=np.uint8)
        for i in range(3):
            arr[:, :, i] = np.linspace(c1[i], c2[i], h, dtype=np.uint8)[:, np.newaxis]
        return Image.fromarray(arr)
    except ImportError:
        img = Image.new("RGB", (w, h))
        draw = ImageDraw.Draw(img)
        for y in range(h):
            t = y / max(h - 1, 1)
            r = int(c1[0] + (c2[0] - c1[0]) * t)
            g = int(c1[1] + (c2[1] - c1[1]) * t)
            b = int(c1[2] + (c2[2] - c1[2]) * t)
            draw.line([(0, y), (w, y)], fill=(r, g, b))
        return img

def wrap_text(text: str, fnt, max_w: int, draw: ImageDraw.ImageDraw) -> list[str]:
    words = text.split()
    lines, cur = [], []
    for word in words:
        cur.append(word)
        line = " ".join(cur)
        bbox = draw.textbbox((0, 0), line, font=fnt)
        if bbox[2] - bbox[0] > max_w and len(cur) > 1:
            cur.pop()
            lines.append(" ".join(cur))
            cur = [word]
    if cur:
        lines.append(" ".join(cur))
    return lines or [""]

def text_size(draw: ImageDraw.ImageDraw, text: str, fnt) -> tuple[int, int]:
    bbox = draw.textbbox((0, 0), text, font=fnt)
    return bbox[2] - bbox[0], bbox[3] - bbox[1]

def fit_text(text: str, draw: ImageDraw.ImageDraw, max_w: int, max_h: int, start: int, minimum: int, bold: bool) -> tuple:
    for size in range(start, minimum - 1, -2):
        fnt = get_font(size, bold=bold)
        lines = wrap_text(text, fnt, max_w, draw)
        line_h = int(size * 1.18)
        block_h = line_h * len(lines)
        widest = max((text_size(draw, line, fnt)[0] for line in lines), default=0)
        if widest <= max_w and block_h <= max_h:
            return fnt, lines, line_h
    fnt = get_font(minimum, bold=bold)
    return fnt, wrap_text(text, fnt, max_w, draw), int(minimum * 1.18)

def draw_multiline(draw: ImageDraw.ImageDraw, lines: list[str], xy: tuple[int, int], fnt, line_h: int, fill, align: str = "left", width: int = 0):
    x, y = xy
    for line in lines:
        tw, _ = text_size(draw, line, fnt)
        tx = x
        if align == "center":
            tx = x + max((width - tw) // 2, 0)
        draw.text((tx, y), line, font=fnt, fill=fill)
        y += line_h

def style_conf(key: str) -> dict:
    return STYLE_PRESETS.get(key, STYLE_PRESETS["modern"])

def panel_fill(panel_type: str, first: bool) -> tuple:
    if panel_type == "light":
        return (255, 255, 255, 232)
    if panel_type == "frosted":
        return (255, 255, 255, 205)
    if panel_type == "minimal":
        return (255, 255, 255, 0) if first else (255, 255, 255, 226)
    if panel_type == "solid_dark":
        return (0, 0, 0, 210)
    return (0, 0, 0, 132) if first else (255, 255, 255, 218)

def text_palette(panel_type: str, first: bool):
    if panel_type in ("light", "frosted") or (panel_type == "minimal" and not first):
        return (16, 18, 24, 238), (40, 44, 54, 210)
    return (255, 255, 255, 255), (255, 255, 255, 178)

def trim_uniform_border(img: Image.Image) -> Image.Image:
    src = img.convert("RGB")
    corner = src.getpixel((0, 0))
    bg = Image.new("RGB", src.size, corner)
    diff = ImageChops.difference(src, bg).convert("L")
    mask = diff.point(lambda p: 255 if p > 22 else 0)
    bbox = mask.getbbox()
    if not bbox:
        return src
    x1, y1, x2, y2 = bbox
    w, h = src.size
    border_x = min(x1, w - x2)
    border_y = min(y1, h - y2)
    if border_x > w * 0.04 or border_y > h * 0.04:
        pad = 8
        return src.crop((max(0, x1 - pad), max(0, y1 - pad), min(w, x2 + pad), min(h, y2 + pad)))
    return src

def cover_resize(img: Image.Image, w: int, h: int) -> Image.Image:
    src = trim_uniform_border(img).convert("RGB")
    scale = max(w / src.width, h / src.height)
    nw, nh = int(src.width * scale), int(src.height * scale)
    resized = src.resize((nw, nh), Image.LANCZOS)
    left = max((nw - w) // 2, 0)
    top = max((nh - h) // 2, 0)
    return resized.crop((left, top, left + w, top + h))

def add_vignette(img: Image.Image) -> Image.Image:
    overlay = Image.new("RGBA", img.size, (0, 0, 0, 86))
    base = img.convert("RGBA")
    base = Image.alpha_composite(base, overlay)
    glow = Image.new("RGBA", img.size, (0, 0, 0, 0))
    gd = ImageDraw.Draw(glow)
    gd.rectangle((0, 0, SLIDE_W, int(SLIDE_H * 0.28)), fill=(0, 0, 0, 56))
    gd.rectangle((0, int(SLIDE_H * 0.68), SLIDE_W, SLIDE_H), fill=(0, 0, 0, 72))
    return Image.alpha_composite(base, glow).convert("RGB")

def hex_to_rgb(hex_color: str) -> tuple:
    h = hex_color.lstrip("#")
    return tuple(int(h[i:i+2], 16) for i in (0, 2, 4))

def has_ai() -> bool:
    return bool(OPENROUTER_API_KEY)

def seeded_palette(seed_text: str) -> tuple[tuple, tuple]:
    digest = hashlib.sha256(seed_text.encode("utf-8")).digest()
    c1 = (35 + digest[0] % 130, 35 + digest[1] % 130, 35 + digest[2] % 130)
    c2 = (90 + digest[3] % 140, 90 + digest[4] % 140, 90 + digest[5] % 140)
    return c1, c2

# ─── Создание слайда ──────────────────────────────────────────────────────────
def create_slide(
    text: str,
    num: int,
    total: int,
    bg_img: Optional[Image.Image],
    c1: tuple,
    c2: Optional[tuple],
    tc: str,
    style_key: str,
    is_first: bool,
    is_last: bool,
) -> Image.Image:
    if bg_img:
        base = add_vignette(cover_resize(bg_img, SLIDE_W, SLIDE_H))
    elif c2:
        base = make_gradient(SLIDE_W, SLIDE_H, c1, c2)
    else:
        base = Image.new("RGB", (SLIDE_W, SLIDE_H), c1)

    canvas = base.convert("RGBA")
    draw = ImageDraw.Draw(canvas)
    soft = (255, 255, 255, 210)
    accent = hex_to_rgb(tc)
    st = style_conf(style_key)
    panel_type = st["panel"]
    align = st["align"]
    radius = st["radius"]
    pad = 84

    # Тонкая система навигации вместо декоративных полос.
    progress_w = SLIDE_W - pad * 2
    progress_y = 62
    draw.rounded_rectangle((pad, progress_y, pad + progress_w, progress_y + 8), radius=4, fill=(255, 255, 255, 58))
    draw.rounded_rectangle((pad, progress_y, pad + int(progress_w * num / total), progress_y + 8), radius=4, fill=(*accent, 235))

    count_font = get_font(28, bold=True)
    count = f"{num:02d}/{total:02d}"
    cw, ch = text_size(draw, count, count_font)
    draw.rounded_rectangle((SLIDE_W - pad - cw - 34, SLIDE_H - 78, SLIDE_W - pad, SLIDE_H - 30), radius=24, fill=(0, 0, 0, 112))
    draw.text((SLIDE_W - pad - cw - 17, SLIDE_H - 70), count, font=count_font, fill=soft)

    if is_first:
        panel = (pad, 450, SLIDE_W - pad, 930)
        fill = panel_fill(panel_type, True)
        text_fill, muted = text_palette(panel_type, True)
        if fill[3] > 0:
            draw.rounded_rectangle(panel, radius=radius, fill=fill, outline=(255, 255, 255, 46), width=2)
        draw.rounded_rectangle((pad + 36, panel[1] + 40, pad + 126, panel[1] + 48), radius=4, fill=(*accent, 255))
        fnt, lines, lh = fit_text(text, draw, panel[2] - panel[0] - 80, 285, st["title"], 40, True)
        draw_multiline(draw, lines, (panel[0] + 40, panel[1] + 84), fnt, lh, text_fill, align=align, width=panel[2] - panel[0] - 80)
        hint_font = get_font(30, bold=True)
        draw.text((panel[0] + 40, panel[3] - 78), "Листай дальше", font=hint_font, fill=muted)
    elif is_last:
        panel = (pad, 410, SLIDE_W - pad, 955)
        fill = panel_fill(panel_type, False)
        text_fill, _ = text_palette(panel_type, False)
        if fill[3] > 0:
            draw.rounded_rectangle(panel, radius=radius, fill=fill)
        label_font = get_font(28, bold=True)
        draw.text((panel[0] + 46, panel[1] + 42), "CTA", font=label_font, fill=(*accent, 255))
        fnt, lines, lh = fit_text(text, draw, panel[2] - panel[0] - 92, 325, st["cta"], 36, True)
        draw_multiline(draw, lines, (panel[0] + 46, panel[1] + 102), fnt, lh, text_fill, align=align, width=panel[2] - panel[0] - 92)
    else:
        panel = (pad, 445, SLIDE_W - pad, 900)
        fill = panel_fill(panel_type, False)
        text_fill, _ = text_palette(panel_type, False)
        if fill[3] > 0:
            draw.rounded_rectangle(panel, radius=radius, fill=fill)
        label_font = get_font(28, bold=True)
        draw.text((panel[0] + 42, panel[1] + 38), f"Слайд {num}", font=label_font, fill=(*accent, 255))
        fnt, lines, lh = fit_text(text, draw, panel[2] - panel[0] - 84, 280, st["body"], 30, panel_type == "solid_dark")
        draw_multiline(draw, lines, (panel[0] + 42, panel[1] + 104), fnt, lh, text_fill, align=align, width=panel[2] - panel[0] - 84)

    return canvas.convert("RGB")

# ─── AI функции (OpenRouter) ──────────────────────────────────────────────────
def _or_client():
    return openai.AsyncOpenAI(
        api_key=OPENROUTER_API_KEY,
        base_url=OPENROUTER_BASE_URL,
    )

async def ai_generate_texts(topic: str, n: int) -> list[str]:
    if not has_ai():
        raise RuntimeError("OPENROUTER_API_KEY не задан")

    prompt = (
        "Ты senior-копирайтер для Instagram-каруселей. Нужна не вода, а готовые короткие слайды.\n"
        f'Тема: "{topic}"\n'
        f"Количество слайдов: {n}\n\n"
        "Правила:\n"
        "- Слайд 1: сильный hook, 5-9 слов, без канцелярита.\n"
        f"- Слайды 2-{n-1}: конкретная мысль или микро-инсайт, 65-115 символов.\n"
        f"- Слайд {n}: естественный CTA, 55-100 символов.\n"
        "- Не начинай каждый слайд одинаково.\n"
        "- Не используй общие фразы вроде 'позволяет обрабатывать огромные объемы данных'.\n"
        "- Пиши так, чтобы текст можно было сразу поставить на дизайн.\n\n"
        f'Ответь ТОЛЬКО JSON: {{"slides": ["...", "..."]}}\n'
        "Язык: русский."
    )
    r = await _or_client().chat.completions.create(
        model=TEXT_MODEL,
        messages=[{"role": "user", "content": prompt}],
        temperature=0.8,
        max_tokens=700,
        extra_headers={"HTTP-Referer": "https://t.me/carousel_bot", "X-Title": BOT_TITLE},
    )
    raw = r.choices[0].message.content or ""
    m = re.search(r"\{.*\}", raw, re.DOTALL)
    if m:
        try:
            data = json.loads(m.group())
            slides = data.get("slides", [])
            return [str(x).strip() for x in slides if str(x).strip()]
        except json.JSONDecodeError:
            logger.warning("AI вернул невалидный JSON: %s", raw[:300])
    return []

async def ai_generate_bg(prompt: str) -> Optional[bytes]:
    if not has_ai():
        return None

    full = (
        f"Full-bleed vertical 4:5 abstract editorial background for an Instagram carousel, {prompt}, "
        "no text, no typography, no frames, no borders, no mockup, no poster inside poster, "
        "clean edges, rich depth, enough negative space for text, high-end social media design"
    )
    try:
        r = await _or_client().chat.completions.create(
            model=IMAGE_MODEL,
            messages=[{"role": "user", "content": full}],
            modalities=["image", "text"],
            extra_headers={"HTTP-Referer": "https://t.me/carousel_bot", "X-Title": BOT_TITLE},
        )
        msg = r.choices[0].message
        images = getattr(msg, "images", None) or []
        if not images:
            return None
        first_image = images[0]
        if isinstance(first_image, dict):
            image_url = first_image.get("image_url", {}).get("url")
        else:
            image_url_obj = getattr(first_image, "image_url", None) or getattr(first_image, "imageUrl", None)
            image_url = getattr(image_url_obj, "url", None)
        if not image_url:
            return None
        if image_url.startswith("data:image"):
            _, payload = image_url.split(",", 1)
            return base64.b64decode(payload)
        async with aiohttp.ClientSession() as session:
            async with session.get(image_url) as resp:
                if resp.status == 200:
                    return await resp.read()
    except Exception as e:
        logger.warning(f"AI фон не получился: {e}")
    return None

def slide_bg_prompt(s: Session, text: str, index: int, total: int) -> str:
    topic = s.topic or (s.slide_texts[0] if s.slide_texts else "Instagram carousel")
    return (
        f"visual theme: {topic}; slide {index} of {total}; slide meaning: {text}; "
        "keep a coherent premium editorial style across the carousel, use related colors, "
        "different composition for this slide, abstract/metaphorical visual, no text"
    )

# ─── Сборка карусели ──────────────────────────────────────────────────────────
async def build_carousel(s: Session) -> tuple[bytes, bytes, bytes]:
    bg_img, c1, c2 = None, s.bg_c1, s.bg_c2
    slide_bg_imgs: list[Optional[Image.Image]] = []

    if s.bg_type == "template":
        t = TEMPLATES.get(s.bg_template, TEMPLATES["purple"])
        c1, c2, s.text_color = t["c1"], t["c2"], t["tc"]
    elif s.bg_type == "ai" and s.bg_prompt:
        data = await ai_generate_bg(s.bg_prompt)
        if data:
            bg_img = Image.open(io.BytesIO(data))
        else:
            c1, c2 = seeded_palette(s.bg_prompt)
            logger.warning("AI фон недоступен, использую уникальный градиент")
    elif s.bg_type == "ai_auto":
        c1, c2 = seeded_palette(" ".join(s.slide_texts) or s.topic)
        for i, text in enumerate(s.slide_texts):
            data = await ai_generate_bg(slide_bg_prompt(s, text, i + 1, len(s.slide_texts)))
            slide_bg_imgs.append(Image.open(io.BytesIO(data)) if data else None)
        if not any(slide_bg_imgs):
            logger.warning("AI-фоны по слайдам недоступны, использую единый градиент")
    elif s.bg_type == "photo" and s.bg_photo:
        bg_img = Image.open(io.BytesIO(s.bg_photo))
    elif s.bg_type == "photo_each" and s.bg_photos:
        for data in s.bg_photos:
            slide_bg_imgs.append(Image.open(io.BytesIO(data)) if data else None)
    # bg_type == "color" — c1/c2 уже установлены

    slides = [
        create_slide(
            text=text,
            num=i + 1,
            total=len(s.slide_texts),
            bg_img=slide_bg_imgs[i] if i < len(slide_bg_imgs) and slide_bg_imgs[i] else bg_img,
            c1=c1,
            c2=c2,
            tc=s.text_color,
            style_key=s.style,
            is_first=(i == 0),
            is_last=(i == len(s.slide_texts) - 1),
        )
        for i, text in enumerate(s.slide_texts)
    ]

    # ZIP с PNG слайдами
    zip_buf = io.BytesIO()
    with zipfile.ZipFile(zip_buf, "w", zipfile.ZIP_DEFLATED) as zf:
        for i, slide in enumerate(slides):
            buf = io.BytesIO()
            slide.save(buf, "PNG", optimize=True)
            zf.writestr(f"slide_{i+1:02d}.png", buf.getvalue())
    zip_buf.seek(0)

    preview_buf = io.BytesIO()
    slides[0].save(preview_buf, "JPEG", quality=90)
    preview_buf.seek(0)

    # PDF
    pdf_buf = io.BytesIO()
    c = pdf_canvas.Canvas(pdf_buf, pagesize=(SLIDE_W, SLIDE_H))
    for slide in slides:
        buf = io.BytesIO()
        slide.save(buf, "JPEG", quality=88)
        buf.seek(0)
        with tempfile.NamedTemporaryFile(suffix=".jpg", delete=False) as tmp:
            tmp.write(buf.read())
            tmp_path = tmp.name
        c.drawImage(tmp_path, 0, 0, SLIDE_W, SLIDE_H)
        c.showPage()
        os.unlink(tmp_path)
    c.save()
    pdf_buf.seek(0)

    return zip_buf.getvalue(), pdf_buf.getvalue(), preview_buf.getvalue()

# ─── Хелперы клавиатур ────────────────────────────────────────────────────────
def kb(*rows):
    return InlineKeyboardMarkup(list(rows))

def btn(text: str, data: str):
    return InlineKeyboardButton(text, callback_data=data)

def content_keyboard() -> InlineKeyboardMarkup:
    rows = [[btn("✍️ Введу текст вручную", "ct_manual")]]
    if has_ai():
        rows.append([btn("🤖 Сгенерировать через ИИ", "ct_ai")])
    return kb(*rows)

def bg_keyboard() -> InlineKeyboardMarkup:
    rows = [
        [btn("🖼 Готовые шаблоны (8 стилей)", "bg_template")],
        [btn("📸 Одно своё фото на все слайды", "bg_photo")],
        [btn("🧩 Свои фото по слайдам", "bg_photo_each")],
        [btn("🎨 Цвет / градиент", "bg_color")],
    ]
    if has_ai():
        rows.insert(1, [btn("🤖 ИИ-генерация фона", "bg_ai")])
    return kb(*rows)

def style_keyboard() -> InlineKeyboardMarkup:
    rows = [[btn(v["name"], f"style_{k}")] for k, v in STYLE_PRESETS.items()]
    return InlineKeyboardMarkup(rows)

async def show_style_menu_from_query(q) -> int:
    text = "✨ *Выбери стиль оформления:*\n\n" + "\n".join(
        f"{v['name']} — {v['desc']}" for v in STYLE_PRESETS.values()
    )
    await q.edit_message_text(text, parse_mode="Markdown", reply_markup=style_keyboard())
    return CHOOSE_STYLE

async def show_style_menu_from_msg(msg) -> int:
    text = "✨ *Выбери стиль оформления:*\n\n" + "\n".join(
        f"{v['name']} — {v['desc']}" for v in STYLE_PRESETS.values()
    )
    await msg.reply_text(text, parse_mode="Markdown", reply_markup=style_keyboard())
    return CHOOSE_STYLE

async def show_bg_menu_from_query(q) -> int:
    await q.edit_message_text(
        "🎨 *Выбери тип фона для всех слайдов:*",
        parse_mode="Markdown",
        reply_markup=bg_keyboard(),
    )
    return CHOOSE_BG

async def show_bg_menu_from_msg(msg) -> int:
    await msg.reply_text(
        "🎨 *Выбери тип фона для всех слайдов:*",
        parse_mode="Markdown",
        reply_markup=bg_keyboard(),
    )
    return CHOOSE_BG

# ─── Обработчики ──────────────────────────────────────────────────────────────
async def cmd_start(u: Update, ctx: ContextTypes.DEFAULT_TYPE) -> int:
    _sessions[u.effective_user.id] = Session()
    await u.message.reply_text(
        f"👋 Привет, *{u.effective_user.first_name}*!\n\n"
        "Я создаю *карусели для Instagram* 🎠\n\n"
        "✅ Текст вручную *или* через ИИ\n"
        "✅ Фоны: шаблоны, ИИ, своё фото, цвет\n"
        "✅ Выгрузка: *ZIP (PNG) + PDF*\n"
        "✅ Размер слайдов: *1080 × 1350 px* (4:5)\n\n"
        "Нажми ↓ чтобы начать!",
        parse_mode="Markdown",
        reply_markup=kb(
            [btn("🎨 Создать карусель", "start_create")],
            [btn("ℹ️ Как это работает", "how_it_works")],
        ),
    )
    return MAIN_MENU

async def cb_how(u: Update, ctx: ContextTypes.DEFAULT_TYPE) -> int:
    q = u.callback_query
    await q.answer()
    await q.edit_message_text(
        "📖 *Как это работает:*\n\n"
        "1️⃣ Выбери количество слайдов (2–10)\n"
        "2️⃣ Введи текст сам *или* дай тему — ИИ напишет\n"
        "3️⃣ Выбери фон:\n"
        "   • 8 готовых шаблонов\n"
        "   • ИИ-генерация по описанию или под каждый текст\n"
        "   • Загрузи одно фото или отдельные фото по слайдам\n"
        "   • Любой цвет или градиент\n"
        "4️⃣ Получи *ZIP* с PNG слайдами + *PDF*\n\n"
        "_Слайды: 1080×1350 px — вертикальный 4:5 формат для Instagram_",
        parse_mode="Markdown",
        reply_markup=kb([btn("🎨 Начать создание", "start_create")]),
    )
    return MAIN_MENU

async def cb_start_create(u: Update, ctx: ContextTypes.DEFAULT_TYPE) -> int:
    q = u.callback_query
    await q.answer()
    await q.edit_message_text(
        "📊 *Сколько слайдов в карусели?*",
        parse_mode="Markdown",
        reply_markup=kb(
            [btn("2", "sl_2"), btn("3", "sl_3"), btn("4", "sl_4"), btn("5", "sl_5")],
            [btn("6", "sl_6"), btn("7", "sl_7"), btn("8", "sl_8"), btn("10", "sl_10")],
        ),
    )
    return CHOOSE_SLIDES

async def cb_slides(u: Update, ctx: ContextTypes.DEFAULT_TYPE) -> int:
    q = u.callback_query
    await q.answer()
    n = int(q.data.split("_")[1])
    s = sess(q.from_user.id)
    s.slides_count = n
    s.slide_texts = []
    s.current_slide = 0
    await q.edit_message_text(
        f"✅ Слайдов: *{n}*\n\n📝 *Как заполнить контент?*",
        parse_mode="Markdown",
        reply_markup=kb(
            *content_keyboard().inline_keyboard,
        ),
    )
    return CHOOSE_CONTENT

async def cb_content_type(u: Update, ctx: ContextTypes.DEFAULT_TYPE) -> int:
    q = u.callback_query
    await q.answer()
    s = sess(q.from_user.id)
    s.content_type = q.data.split("_")[1]
    if s.content_type == "ai":
        if not has_ai():
            await q.edit_message_text(
                "ИИ-режим сейчас недоступен: не задан OPENROUTER_API_KEY.\n\n"
                "Можно продолжить вручную.",
                reply_markup=kb([btn("✍️ Ввести текст вручную", "ct_manual")]),
            )
            return CHOOSE_CONTENT
        await q.edit_message_text(
            "🤖 Введи *тему* карусели:\n\n"
            "_Примеры:_\n"
            "• _5 привычек успешных людей_\n"
            "• _Как похудеть без диет_\n"
            "• _Советы для начинающих фотографов_",
            parse_mode="Markdown",
        )
        return ENTER_TOPIC
    else:
        await q.edit_message_text(
            f"✍️ Введи текст для *слайда 1/{s.slides_count}*\n\n"
            "_Первый слайд — заголовок карусели_",
            parse_mode="Markdown",
        )
        return ENTER_SLIDE_TEXT

async def msg_topic(u: Update, ctx: ContextTypes.DEFAULT_TYPE) -> int:
    s = sess(u.effective_user.id)
    s.topic = u.message.text
    wait_msg = await u.message.reply_text("⏳ Генерирую тексты через ИИ...")
    try:
        texts = await ai_generate_texts(s.topic, s.slides_count)
        if texts and len(texts) >= s.slides_count:
            s.slide_texts = texts[: s.slides_count]
        else:
            raise ValueError(f"Получено {len(texts)} слайдов, нужно {s.slides_count}")
    except Exception as e:
        logger.error(f"Ошибка генерации текстов: {e}")
        s.slide_texts = (
            [s.topic]
            + [f"Пункт {i}" for i in range(1, s.slides_count - 1)]
            + ["Подпишись и сохрани! 🔔"]
        )
    preview = "\n\n".join(f"{i+1}. {t}" for i, t in enumerate(s.slide_texts))
    await wait_msg.edit_text(
        f"📝 Сгенерированные тексты:\n\n{preview}",
        reply_markup=kb(
            [btn("✅ Отлично, продолжить!", "texts_ok")],
            [btn("🔄 Перегенерировать", "regen")],
            [btn("✏️ Редактировать вручную", "edit_manual")],
        ),
    )
    return ENTER_TOPIC

async def cb_texts_ok(u: Update, ctx: ContextTypes.DEFAULT_TYPE) -> int:
    await u.callback_query.answer()
    return await show_bg_menu_from_query(u.callback_query)

async def cb_regen(u: Update, ctx: ContextTypes.DEFAULT_TYPE) -> int:
    q = u.callback_query
    await q.answer()
    s = sess(q.from_user.id)
    await q.edit_message_text("⏳ Перегенерирую...")
    try:
        texts = await ai_generate_texts(s.topic, s.slides_count)
        if texts and len(texts) >= s.slides_count:
            s.slide_texts = texts[: s.slides_count]
    except Exception as e:
        logger.error(f"Ошибка регенерации: {e}")
    preview = "\n\n".join(f"{i+1}. {t}" for i, t in enumerate(s.slide_texts))
    await q.edit_message_text(
        f"📝 Новые тексты:\n\n{preview}",
        reply_markup=kb(
            [btn("✅ Отлично!", "texts_ok")],
            [btn("🔄 Ещё раз", "regen")],
            [btn("✏️ Редактировать вручную", "edit_manual")],
        ),
    )
    return ENTER_TOPIC

async def cb_edit_manual(u: Update, ctx: ContextTypes.DEFAULT_TYPE) -> int:
    q = u.callback_query
    await q.answer()
    s = sess(q.from_user.id)
    s.slide_texts = []
    s.current_slide = 0
    await q.edit_message_text(
        f"✍️ Введи текст для *слайда 1/{s.slides_count}*",
        parse_mode="Markdown",
    )
    return ENTER_SLIDE_TEXT

async def msg_slide_text(u: Update, ctx: ContextTypes.DEFAULT_TYPE) -> int:
    s = sess(u.effective_user.id)
    s.slide_texts.append(u.message.text)
    s.current_slide += 1
    if s.current_slide < s.slides_count:
        hint = ""
        if s.current_slide == s.slides_count - 1:
            hint = "\n\n_Последний слайд — обычно CTA: «Подпишись», «Сохрани», «Напиши в Direct»_"
        await u.message.reply_text(
            f"✍️ Текст для *слайда {s.current_slide + 1}/{s.slides_count}*{hint}",
            parse_mode="Markdown",
        )
        return ENTER_SLIDE_TEXT
    return await show_bg_menu_from_msg(u.message)

async def cb_bg_type(u: Update, ctx: ContextTypes.DEFAULT_TYPE) -> int:
    q = u.callback_query
    await q.answer()
    s = sess(q.from_user.id)
    t = q.data.removeprefix("bg_")
    s.bg_type = t

    if t == "template":
        rows = []
        items = list(TEMPLATES.items())
        for i in range(0, len(items), 2):
            row = [btn(items[i][1]["name"], f"tpl_{items[i][0]}")]
            if i + 1 < len(items):
                row.append(btn(items[i + 1][1]["name"], f"tpl_{items[i+1][0]}"))
            rows.append(row)
        await q.edit_message_text(
            "🖼 *Выбери шаблон фона:*",
            parse_mode="Markdown",
            reply_markup=InlineKeyboardMarkup(rows),
        )
        return CHOOSE_TEMPLATE

    elif t == "ai":
        if not has_ai():
            await q.edit_message_text(
                "ИИ-фоны сейчас недоступны: не задан OPENROUTER_API_KEY.\n\n"
                "Выбери шаблон, свой фон или цвет.",
                reply_markup=bg_keyboard(),
            )
            return CHOOSE_BG
        await q.edit_message_text(
            "🤖 Опиши желаемый фон:\n\n"
            "_Примеры:_\n"
            "• _космос и звёзды, фиолетовый_\n"
            "• _абстрактные синие волны_\n"
            "• _минималистичная природа, зелёный_\n"
            "• _неоновый город в дождь_",
            parse_mode="Markdown",
            reply_markup=kb([btn("✨ Подобрать фоны под текст", "ai_auto_bg")]),
        )
        return ENTER_BG_PROMPT

    elif t == "photo":
        await q.edit_message_text(
            "📸 Отправь фото для фона\n\n"
            "_Будет автоматически подогнано под 1080×1350 px_",
            parse_mode="Markdown",
        )
        return WAIT_BG_PHOTO

    elif t == "photo_each":
        s.bg_photos = []
        s.current_bg_slide = 0
        await q.edit_message_text(
            f"🧩 Отправь фото для *слайда 1/{s.slides_count}*\n\n"
            "_Каждый фон будет подогнан под вертикальный формат 1080×1350 px_",
            parse_mode="Markdown",
        )
        return WAIT_BG_PHOTO

    elif t == "color":
        rows = [[btn(v["name"], f"clr_{k}")] for k, v in COLOR_PRESETS.items()]
        await q.edit_message_text(
            "🎨 *Выбери цветовую схему:*",
            parse_mode="Markdown",
            reply_markup=InlineKeyboardMarkup(rows),
        )
        return CHOOSE_COLOR

async def cb_template(u: Update, ctx: ContextTypes.DEFAULT_TYPE) -> int:
    q = u.callback_query
    await q.answer()
    s = sess(q.from_user.id)
    key = q.data.split("_", 1)[1]
    s.bg_template = key
    t = TEMPLATES[key]
    s.text_color = t["tc"]
    await q.edit_message_text(
        f"✅ Шаблон: *{t['name']}*\n\nТеперь выбери стиль оформления.",
        parse_mode="Markdown",
    )
    return await show_style_menu_from_query(q)

async def msg_bg_prompt(u: Update, ctx: ContextTypes.DEFAULT_TYPE) -> int:
    s = sess(u.effective_user.id)
    s.bg_prompt = u.message.text
    await u.message.reply_text(
        f"✅ Фон: *{s.bg_prompt}*\n\n"
        "⚠️ _ИИ генерирует фон во время создания, обычно это занимает 15–30 секунд_\n\n"
        "Теперь выбери стиль оформления.",
        parse_mode="Markdown",
    )
    return await show_style_menu_from_msg(u.message)

async def cb_ai_auto_bg(u: Update, ctx: ContextTypes.DEFAULT_TYPE) -> int:
    q = u.callback_query
    await q.answer()
    s = sess(q.from_user.id)
    s.bg_type = "ai_auto"
    s.bg_prompt = s.topic or (s.slide_texts[0] if s.slide_texts else "")
    await q.edit_message_text(
        "✅ Режим выбран: *ИИ подберёт отдельный фон под каждый слайд*.\n\n"
        "Я сохраню общий стиль карусели, но картинки будут разными по смыслу каждого слайда.\n"
        "Генерация может занять 1-3 минуты.\n\n"
        "Теперь выбери стиль оформления.",
        parse_mode="Markdown",
    )
    return await show_style_menu_from_query(q)

async def msg_bg_photo(u: Update, ctx: ContextTypes.DEFAULT_TYPE) -> int:
    s = sess(u.effective_user.id)
    photo = u.message.photo[-1]
    f = await ctx.bot.get_file(photo.file_id)
    data = bytes(await f.download_as_bytearray())

    if s.bg_type == "photo_each":
        s.bg_photos.append(data)
        s.current_bg_slide += 1
        if s.current_bg_slide < s.slides_count:
            await u.message.reply_text(
                f"✅ Фото для слайда {s.current_bg_slide} получено.\n\n"
                f"Теперь отправь фото для *слайда {s.current_bg_slide + 1}/{s.slides_count}*",
                parse_mode="Markdown",
            )
            return WAIT_BG_PHOTO
        await u.message.reply_text(
            "✅ Все фото по слайдам получены! Теперь выбери стиль оформления.",
        )
        return await show_style_menu_from_msg(u.message)

    s.bg_photo = data
    await u.message.reply_text(
        "✅ Фото получено! Теперь выбери стиль оформления.",
    )
    return await show_style_menu_from_msg(u.message)

async def cb_color(u: Update, ctx: ContextTypes.DEFAULT_TYPE) -> int:
    q = u.callback_query
    await q.answer()
    s = sess(q.from_user.id)
    key = q.data.split("_", 1)[1]
    if key == "custom":
        await q.edit_message_text(
            "🎨 Введи HEX-цвет:\n\n"
            "Один цвет: `#1A1A2E`\n"
            "Градиент (два цвета): `#1A1A2E, #16213E`",
            parse_mode="Markdown",
        )
        return CHOOSE_COLOR
    p = COLOR_PRESETS[key]
    s.bg_c1 = p["bg"]
    s.bg_c2 = None
    s.text_color = p["tc"]
    await q.edit_message_text(
        f"✅ Цвет: *{p['name']}*\n\nТеперь выбери стиль оформления.",
        parse_mode="Markdown",
    )
    return await show_style_menu_from_query(q)

async def msg_custom_color(u: Update, ctx: ContextTypes.DEFAULT_TYPE) -> int:
    s = sess(u.effective_user.id)
    txt = u.message.text.strip()

    def parse_hex(h: str) -> Optional[tuple]:
        h = h.strip().lstrip("#")
        if len(h) == 6:
            try:
                return tuple(int(h[i:i+2], 16) for i in (0, 2, 4))
            except ValueError:
                return None
        return None

    if "," in txt:
        parts = txt.split(",", 1)
        c1, c2 = parse_hex(parts[0]), parse_hex(parts[1])
        if c1 and c2:
            s.bg_c1, s.bg_c2 = c1, c2
        else:
            await u.message.reply_text(
                "❌ Неверный формат. Пример: `#FF5733, #C70039`", parse_mode="Markdown"
            )
            return CHOOSE_COLOR
    else:
        c = parse_hex(txt)
        if c:
            s.bg_c1, s.bg_c2 = c, None
        else:
            await u.message.reply_text(
                "❌ Неверный формат. Пример: `#FF5733`", parse_mode="Markdown"
            )
            return CHOOSE_COLOR

    r, g, b = s.bg_c1
    s.text_color = "#000000" if (r * 299 + g * 587 + b * 114) / 1000 > 128 else "#FFFFFF"
    await u.message.reply_text(
        "✅ Цвет выбран! Теперь выбери стиль оформления.",
    )
    return await show_style_menu_from_msg(u.message)

async def cb_style(u: Update, ctx: ContextTypes.DEFAULT_TYPE) -> int:
    q = u.callback_query
    await q.answer()
    s = sess(q.from_user.id)
    key = q.data.split("_", 1)[1]
    s.style = key if key in STYLE_PRESETS else "modern"
    preset = style_conf(s.style)
    await q.edit_message_text(
        f"✅ Стиль: *{preset['name']}*\n\nВсё готово. Нажми для генерации 🚀",
        parse_mode="Markdown",
        reply_markup=kb([btn("🚀 Генерировать карусель!", "gen")]),
    )
    return CONFIRM

async def cb_gen(u: Update, ctx: ContextTypes.DEFAULT_TYPE) -> int:
    q = u.callback_query
    await q.answer()
    s = sess(q.from_user.id)
    await q.edit_message_text(
        "⏳ *Генерирую карусель...*\n\n_Подожди 15–30 секунд_",
        parse_mode="Markdown",
    )
    if s.bg_type == "ai_auto":
        await q.edit_message_text(
            "⏳ *Генерирую карусель...*\n\n"
            "_Подбираю отдельный AI-фон под каждый слайд. Это может занять 1–3 минуты._",
            parse_mode="Markdown",
        )
    try:
        zip_bytes, pdf_bytes, preview_bytes = await build_carousel(s)
        ctx.user_data["zip"] = zip_bytes
        ctx.user_data["pdf"] = pdf_bytes
        ctx.user_data["preview"] = preview_bytes
        await q.edit_message_text("✅ Карусель готова! Отправляю предпросмотр обложки...")
        await ctx.bot.send_photo(
            q.message.chat_id,
            io.BytesIO(preview_bytes),
            caption=f"Предпросмотр обложки. Всего слайдов: {len(s.slide_texts)}",
        )
        await ctx.bot.send_message(
            q.message.chat_id,
            "В каком формате отправить готовую карусель?",
            parse_mode="Markdown",
            reply_markup=kb(
                [btn("📦 ZIP (PNG слайды)", "fmt_zip"), btn("📄 PDF", "fmt_pdf")],
                [btn("📦 + 📄 Оба формата", "fmt_both")],
                [btn("🔄 Создать новую", "start_create")],
            ),
        )
        return CHOOSE_FORMAT
    except Exception as e:
        logger.error(f"Ошибка генерации: {e}", exc_info=True)
        await q.edit_message_text(
            f"❌ Ошибка при генерации:\n`{str(e)[:200]}`\n\nПопробуй /start",
            parse_mode="Markdown",
        )
        return MAIN_MENU

async def cb_send_format(u: Update, ctx: ContextTypes.DEFAULT_TYPE) -> int:
    q = u.callback_query
    await q.answer()
    fmt = q.data.split("_")[1]
    zip_b = ctx.user_data.get("zip")
    pdf_b = ctx.user_data.get("pdf")
    cid = q.message.chat_id
    await q.edit_message_text("📤 Отправляю файлы...")
    if fmt in ("zip", "both") and zip_b:
        await ctx.bot.send_document(
            cid,
            io.BytesIO(zip_b),
            filename="instagram_carousel.zip",
            caption="📦 PNG слайды — загружай в Instagram по одному",
        )
    if fmt in ("pdf", "both") and pdf_b:
        await ctx.bot.send_document(
            cid,
            io.BytesIO(pdf_b),
            filename="instagram_carousel.pdf",
            caption="📄 PDF версия карусели",
        )
    await ctx.bot.send_message(
        cid,
        "✅ *Готово! Загружай в Instagram* 🚀\n\nСоздать ещё одну?",
        parse_mode="Markdown",
        reply_markup=kb(
            [btn("🎨 Создать ещё карусель", "start_create")],
            [btn("🏠 Главное меню", "main_menu")],
        ),
    )
    return MAIN_MENU

async def cb_main_menu(u: Update, ctx: ContextTypes.DEFAULT_TYPE) -> int:
    q = u.callback_query
    await q.answer()
    _sessions[q.from_user.id] = Session()
    await q.edit_message_text(
        "🏠 *Главное меню*",
        parse_mode="Markdown",
        reply_markup=kb(
            [btn("🎨 Создать карусель", "start_create")],
            [btn("ℹ️ Как это работает", "how_it_works")],
        ),
    )
    return MAIN_MENU

async def cmd_cancel(u: Update, ctx: ContextTypes.DEFAULT_TYPE) -> int:
    await u.message.reply_text("❌ Отменено. /start — начать заново.")
    return ConversationHandler.END

# ─── Запуск ───────────────────────────────────────────────────────────────────
def main():
    if not TELEGRAM_TOKEN:
        logger.error(
            "Telegram token env presence: TELEGRAM_TOKEN=%s BOT_TOKEN=%s TELEGRAM_BOT_TOKEN=%s",
            env_present("TELEGRAM_TOKEN"),
            env_present("BOT_TOKEN"),
            env_present("TELEGRAM_BOT_TOKEN"),
        )
        raise SystemExit("❌ Не задан TELEGRAM_TOKEN")
    if not OPENROUTER_API_KEY:
        logger.warning("OPENROUTER_API_KEY не задан: AI-текст и AI-фоны будут скрыты, ручной режим работает.")

    app = Application.builder().token(TELEGRAM_TOKEN).concurrent_updates(False).build()

    conv = ConversationHandler(
        entry_points=[CommandHandler("start", cmd_start)],
        states={
            MAIN_MENU: [
                CallbackQueryHandler(cb_start_create, pattern="^start_create$"),
                CallbackQueryHandler(cb_how, pattern="^how_it_works$"),
                CallbackQueryHandler(cb_main_menu, pattern="^main_menu$"),
            ],
            CHOOSE_SLIDES: [
                CallbackQueryHandler(cb_slides, pattern="^sl_"),
            ],
            CHOOSE_CONTENT: [
                CallbackQueryHandler(cb_content_type, pattern="^ct_"),
            ],
            ENTER_TOPIC: [
                MessageHandler(filters.TEXT & ~filters.COMMAND, msg_topic),
                CallbackQueryHandler(cb_texts_ok, pattern="^texts_ok$"),
                CallbackQueryHandler(cb_regen, pattern="^regen$"),
                CallbackQueryHandler(cb_edit_manual, pattern="^edit_manual$"),
            ],
            ENTER_SLIDE_TEXT: [
                MessageHandler(filters.TEXT & ~filters.COMMAND, msg_slide_text),
            ],
            CHOOSE_BG: [
                CallbackQueryHandler(cb_bg_type, pattern="^bg_"),
            ],
            CHOOSE_TEMPLATE: [
                CallbackQueryHandler(cb_template, pattern="^tpl_"),
            ],
            ENTER_BG_PROMPT: [
                CallbackQueryHandler(cb_ai_auto_bg, pattern="^ai_auto_bg$"),
                MessageHandler(filters.TEXT & ~filters.COMMAND, msg_bg_prompt),
            ],
            WAIT_BG_PHOTO: [
                MessageHandler(filters.PHOTO, msg_bg_photo),
            ],
            CHOOSE_COLOR: [
                CallbackQueryHandler(cb_color, pattern="^clr_"),
                MessageHandler(filters.TEXT & ~filters.COMMAND, msg_custom_color),
            ],
            CHOOSE_STYLE: [
                CallbackQueryHandler(cb_style, pattern="^style_"),
            ],
            CONFIRM: [
                CallbackQueryHandler(cb_gen, pattern="^gen$"),
                CallbackQueryHandler(cb_start_create, pattern="^start_create$"),
            ],
            CHOOSE_FORMAT: [
                CallbackQueryHandler(cb_send_format, pattern="^fmt_"),
                CallbackQueryHandler(cb_start_create, pattern="^start_create$"),
            ],
        },
        fallbacks=[
            CommandHandler("cancel", cmd_cancel),
            CommandHandler("start", cmd_start),
        ],
        per_user=True,
        per_chat=True,
        allow_reentry=True,
    )

    app.add_handler(conv)
    logger.info("🤖 Бот запущен! Нажми Ctrl+C для остановки.")
    app.run_polling(allowed_updates=Update.ALL_TYPES)

if __name__ == "__main__":
    main()
