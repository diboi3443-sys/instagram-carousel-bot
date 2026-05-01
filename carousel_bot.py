#!/usr/bin/env python3
"""
Instagram Carousel Generator Bot
Telegram бот для создания каруселей для Instagram
Использует OpenRouter API для AI-текста и AI-фонов, если ключ задан
"""

import os, io, re, json, zipfile, asyncio, logging, tempfile, base64, hashlib
from typing import Optional
from dataclasses import dataclass, field
from PIL import Image, ImageDraw, ImageFont
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
TELEGRAM_TOKEN      = os.getenv("TELEGRAM_TOKEN", "")
OPENROUTER_API_KEY  = os.getenv("OPENROUTER_API_KEY", "")
OPENROUTER_BASE_URL = "https://openrouter.ai/api/v1"
TEXT_MODEL          = os.getenv("TEXT_MODEL", "openai/gpt-4o-mini")
IMAGE_MODEL         = os.getenv("IMAGE_MODEL", "google/gemini-2.5-flash-image")
BOT_TITLE           = os.getenv("BOT_TITLE", "Carousel Bot")

SLIDE_W, SLIDE_H = 1080, 1080

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
    CONFIRM,
    CHOOSE_FORMAT,
) = range(12)

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
    bg_c1:          tuple          = (75, 0, 130)
    bg_c2:          Optional[tuple] = (138, 43, 226)
    text_color:     str            = "#FFFFFF"
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
    is_first: bool,
    is_last: bool,
) -> Image.Image:
    # Фон
    if bg_img:
        base = bg_img.copy().convert("RGBA").resize((SLIDE_W, SLIDE_H), Image.LANCZOS)
        overlay = Image.new("RGBA", (SLIDE_W, SLIDE_H), (0, 0, 0, 110))
        base = Image.alpha_composite(base, overlay).convert("RGB")
    elif c2:
        base = make_gradient(SLIDE_W, SLIDE_H, c1, c2)
    else:
        base = Image.new("RGB", (SLIDE_W, SLIDE_H), c1)

    draw = ImageDraw.Draw(base)
    rgb = hex_to_rgb(tc)
    pad = 90
    cw = SLIDE_W - pad * 2

    # Декоративные линии
    draw.rectangle([(pad, 58), (SLIDE_W - pad, 64)], fill=(*rgb, 160))
    draw.rectangle([(pad, SLIDE_H - 64), (SLIDE_W - pad, SLIDE_H - 58)], fill=(*rgb, 160))

    # Номер слайда
    cf = get_font(28)
    ct = f"{num}/{total}"
    cb = draw.textbbox((0, 0), ct, font=cf)
    draw.text((SLIDE_W - pad - (cb[2] - cb[0]), SLIDE_H - 52), ct, font=cf, fill=(*rgb, 180))

    def draw_centered_text(fnt, lines, lh, y_start):
        y = y_start
        for line in lines:
            lb = draw.textbbox((0, 0), line, font=fnt)
            x = (SLIDE_W - (lb[2] - lb[0])) // 2
            # Тень
            draw.text((x + 3, y + 3), line, font=fnt, fill=(0, 0, 0, 100))
            draw.text((x, y), line, font=fnt, fill=rgb)
            y += lh

    if is_first:
        fnt = get_font(66, bold=True)
        lh = 82
        lines = wrap_text(text, fnt, cw, draw)
        th = len(lines) * lh
        draw_centered_text(fnt, lines, lh, (SLIDE_H - th) // 2 - 30)
        # Подсказка "листай"
        hf = get_font(30)
        ht = "Листай →"
        hb = draw.textbbox((0, 0), ht, font=hf)
        draw.text(((SLIDE_W - (hb[2] - hb[0])) // 2, SLIDE_H - 115), ht, font=hf, fill=(*rgb, 160))
    elif is_last:
        fnt = get_font(58, bold=True)
        lh = 74
        lines = wrap_text(text, fnt, cw, draw)
        th = len(lines) * lh
        draw_centered_text(fnt, lines, lh, (SLIDE_H - th) // 2)
    else:
        fnt = get_font(44)
        lh = 62
        lines = wrap_text(text, fnt, cw, draw)
        th = len(lines) * lh
        draw_centered_text(fnt, lines, lh, (SLIDE_H - th) // 2)

    return base

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
        f'Создай контент для Instagram карусели на тему: "{topic}"\n'
        f"Количество слайдов: {n}\n\n"
        f"- Слайд 1: цепляющий заголовок (≤60 символов)\n"
        f"- Слайды 2–{n-1}: одна мысль/факт на слайд (≤120 символов каждый)\n"
        f"- Слайд {n}: призыв к действию (CTA)\n\n"
        f'Ответь ТОЛЬКО JSON: {{"slides": ["...", "..."]}}\n'
        f"Пиши по-русски, живо и вовлекающе."
    )
    r = await _or_client().chat.completions.create(
        model=TEXT_MODEL,
        messages=[{"role": "user", "content": prompt}],
        temperature=0.8,
        max_tokens=700,
        extra_headers={"HTTP-Referer": "https://t.me/carousel_bot", "X-Title": "Carousel Bot"},
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
        f"Abstract artistic background for Instagram post, {prompt}, "
        "no text, no letters, square format, high quality, aesthetic, vibrant colors"
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

# ─── Сборка карусели ──────────────────────────────────────────────────────────
async def build_carousel(s: Session) -> tuple[bytes, bytes, bytes]:
    bg_img, c1, c2 = None, s.bg_c1, s.bg_c2

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
    elif s.bg_type == "photo" and s.bg_photo:
        bg_img = Image.open(io.BytesIO(s.bg_photo))
    # bg_type == "color" — c1/c2 уже установлены

    slides = [
        create_slide(
            text=text,
            num=i + 1,
            total=len(s.slide_texts),
            bg_img=bg_img,
            c1=c1,
            c2=c2,
            tc=s.text_color,
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
        [btn("📸 Загрузить своё фото", "bg_photo")],
        [btn("🎨 Цвет / градиент", "bg_color")],
    ]
    if has_ai():
        rows.insert(1, [btn("🤖 ИИ-генерация фона", "bg_ai")])
    return kb(*rows)

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
        "✅ Размер слайдов: *1080 × 1080 px*\n\n"
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
        "   • ИИ-генерация по описанию\n"
        "   • Загрузи своё фото\n"
        "   • Любой цвет или градиент\n"
        "4️⃣ Получи *ZIP* с PNG слайдами + *PDF*\n\n"
        "_Слайды: 1080×1080 px — идеальный формат для Instagram_",
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
    t = q.data.split("_")[1]
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
        )
        return ENTER_BG_PROMPT

    elif t == "photo":
        await q.edit_message_text(
            "📸 Отправь фото для фона\n\n"
            "_Будет автоматически подогнано под 1080×1080 px_",
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
        f"✅ Шаблон: *{t['name']}*\n\nВсё готово! Нажми для генерации 🚀",
        parse_mode="Markdown",
        reply_markup=kb([btn("🚀 Генерировать карусель!", "gen")]),
    )
    return CONFIRM

async def msg_bg_prompt(u: Update, ctx: ContextTypes.DEFAULT_TYPE) -> int:
    s = sess(u.effective_user.id)
    s.bg_prompt = u.message.text
    await u.message.reply_text(
        f"✅ Фон: *{s.bg_prompt}*\n\n"
        "⚠️ _ИИ генерирует фон во время создания, обычно это занимает 15–30 секунд_",
        parse_mode="Markdown",
        reply_markup=kb([btn("🚀 Генерировать карусель!", "gen")]),
    )
    return CONFIRM

async def msg_bg_photo(u: Update, ctx: ContextTypes.DEFAULT_TYPE) -> int:
    s = sess(u.effective_user.id)
    photo = u.message.photo[-1]
    f = await ctx.bot.get_file(photo.file_id)
    s.bg_photo = bytes(await f.download_as_bytearray())
    await u.message.reply_text(
        "✅ Фото получено! Готов к генерации.",
        reply_markup=kb([btn("🚀 Генерировать карусель!", "gen")]),
    )
    return CONFIRM

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
        f"✅ Цвет: *{p['name']}*",
        parse_mode="Markdown",
        reply_markup=kb([btn("🚀 Генерировать карусель!", "gen")]),
    )
    return CONFIRM

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
        "✅ Цвет выбран!",
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
                MessageHandler(filters.TEXT & ~filters.COMMAND, msg_bg_prompt),
            ],
            WAIT_BG_PHOTO: [
                MessageHandler(filters.PHOTO, msg_bg_photo),
            ],
            CHOOSE_COLOR: [
                CallbackQueryHandler(cb_color, pattern="^clr_"),
                MessageHandler(filters.TEXT & ~filters.COMMAND, msg_custom_color),
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
