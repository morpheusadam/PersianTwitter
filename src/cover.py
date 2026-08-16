"""ساخت کاور شیشه‌ای برای هر پست.

Bot API هیچ کنترلی روی ظاهر دکمه‌ها نمی‌دهد، ولی تصویر پست کاملاً دست ماست.
پس ظاهر شیشه‌ای را همان‌جا می‌سازیم: عکس مقاله را بلور و تیره می‌کنیم و رویش
یک کارت نیمه‌شفاف با تیتر فارسی می‌گذاریم.

فارسی در Pillow دو مرحله لازم دارد که هیچ‌کدام خودکار نیست: reshape برای وصل
کردن حروف به هم، و bidi برای چیدن راست‌به‌چپ. بدون این دو، متن حروف جدا و
برعکس درمی‌آید.
"""

import logging
from io import BytesIO
from pathlib import Path

import arabic_reshaper
from bidi.algorithm import get_display
from PIL import Image, ImageDraw, ImageFilter, ImageFont

log = logging.getLogger(__name__)

ASSETS = Path(__file__).resolve().parent.parent / "assets"
FONT_BOLD = ASSETS / "Vazirmatn-Bold.ttf"
FONT_BODY = ASSETS / "Vazirmatn-Medium.ttf"

WIDTH, HEIGHT = 1280, 720
MARGIN = 72
BLUR = 28
CARD_RADIUS = 40

# شیشه = پرکردن کم‌رنگ + لبه‌ی روشن‌تر. تلگرام JPEG می‌گیرد پس شفافیت واقعی
# نداریم و باید روی خودِ پس‌زمینه‌ی بلورشده ترکیب شود.
CARD_FILL = (255, 255, 255, 38)
CARD_EDGE = (255, 255, 255, 92)
SCRIM = (8, 12, 20, 130)


def build(item, persian: str, client, fallback: Path | None) -> bytes | None:
    """کاور آماده‌ی ارسال برای یک آیتم. None یعنی بی‌خیال شو و مسیر عادی برو."""
    source = None
    if item.image_url:
        source = _load(item.image_url, client)
    if source is None and fallback and fallback.exists():
        try:
            source = Image.open(fallback)
        except Exception as exc:
            log.warning("عکس پیش‌فرض باز نشد: %s", exc)

    # روی کارت فقط جمله‌ی اول می‌نشیند؛ خلاصه‌ی کامل در caption می‌ماند.
    headline = persian.split(". ")[0].strip().rstrip(".")

    try:
        return render(source, item.label, headline or item.label)
    except Exception as exc:
        log.warning("کاور ساخته نشد: %s", exc)
        return None


def _load(url: str, client) -> Image.Image | None:
    try:
        resp = client.get(url, timeout=20)
        resp.raise_for_status()
        return Image.open(BytesIO(resp.content))
    except Exception as exc:
        log.debug("عکس دانلود نشد (%s): %s", url[:60], exc)
        return None


def render(image: Image.Image | None, label: str, title: str) -> bytes:
    """کاور را می‌سازد و بایت‌های JPEG برمی‌گرداند."""
    base = _backdrop(image)
    card = Image.new("RGBA", base.size, (0, 0, 0, 0))
    draw = ImageDraw.Draw(card)

    title_font = ImageFont.truetype(str(FONT_BOLD), 52)
    label_font = ImageFont.truetype(str(FONT_BODY), 30)

    lines = _wrap(_shape(title), title_font, WIDTH - 2 * MARGIN - 96, draw, limit=4)
    line_height = 76
    text_block = len(lines) * line_height

    card_h = text_block + 150
    top = HEIGHT - card_h - MARGIN
    box = (MARGIN, top, WIDTH - MARGIN, HEIGHT - MARGIN)

    draw.rounded_rectangle(box, CARD_RADIUS, fill=CARD_FILL, outline=CARD_EDGE, width=2)

    # لبه‌ی بالایی کمی روشن‌تر: همان براقی‌ای که شیشه را شیشه نشان می‌دهد. یک
    # کمان نازک است، نه یک نوار پر، وگرنه شبیه ایراد رندر می‌شود.
    draw.arc(
        (box[0] + 6, box[1] + 4, box[2] - 6, box[1] + CARD_RADIUS * 2),
        start=185,
        end=355,
        fill=(255, 255, 255, 70),
        width=2,
    )

    right = box[2] - 48
    draw.text((right, box[1] + 34), _shape(label), font=label_font,
              fill=(150, 210, 255, 255), anchor="ra")

    y = box[1] + 96
    for line in lines:
        draw.text((right, y), line, font=title_font, fill=(255, 255, 255, 255), anchor="ra")
        y += line_height

    out = Image.alpha_composite(base, card).convert("RGB")
    buffer = BytesIO()
    out.save(buffer, "JPEG", quality=88, optimize=True)
    return buffer.getvalue()


def _backdrop(image: Image.Image | None) -> Image.Image:
    """عکس مقاله را پر می‌کند، بلور می‌زند و تیره می‌کند تا متن خوانا شود."""
    if image is None:
        canvas = _mesh()
    else:
        source = image.convert("RGB")
        # cover: نسبت را حفظ کن و اضافه را ببر، تا کشیده نشود.
        scale = max(WIDTH / source.width, HEIGHT / source.height)
        size = (max(1, int(source.width * scale)), max(1, int(source.height * scale)))
        source = source.resize(size, Image.LANCZOS)
        left = (source.width - WIDTH) // 2
        top = (source.height - HEIGHT) // 2
        canvas = source.crop((left, top, left + WIDTH, top + HEIGHT)).convert("RGBA")
        canvas = canvas.filter(ImageFilter.GaussianBlur(BLUR))

    scrim = Image.new("RGBA", canvas.size, SCRIM)
    return Image.alpha_composite(canvas, scrim)


def _mesh() -> Image.Image:
    """پس‌زمینه‌ی گرادیانی برای وقتی مقاله هیچ عکسی ندارد.

    رنگ صاف در کانال شبیه پستِ خراب دیده می‌شود. چند لکه‌ی رنگی بلورشده کافی
    است تا عمدی به نظر برسد.
    """
    canvas = Image.new("RGBA", (WIDTH, HEIGHT), (13, 17, 28, 255))
    blobs = ImageDraw.Draw(canvas)
    for cx, cy, radius, color in [
        (200, 140, 320, (56, 92, 190, 255)),
        (1080, 210, 300, (120, 58, 168, 255)),
        (760, 620, 340, (24, 110, 140, 255)),
        (330, 560, 240, (150, 58, 110, 255)),
    ]:
        blobs.ellipse((cx - radius, cy - radius, cx + radius, cy + radius), fill=color)
    return canvas.filter(ImageFilter.GaussianBlur(150))


def _shape(text: str) -> str:
    """فارسی را برای Pillow آماده می‌کند: اتصال حروف، بعد ترتیب راست‌به‌چپ."""
    return get_display(arabic_reshaper.reshape(text))


def _wrap(text: str, font, max_width: int, draw, limit: int) -> list[str]:
    """شکستن خط روی متن shape‌شده.

    چون متن قبلاً بازچینی شده، ترتیب کلمات معکوس است؛ پس از ته می‌سازیم و آخر
    برمی‌گردانیم تا خطوط به ترتیب درست خوانده شوند.
    """
    words = text.split()
    lines: list[str] = []
    current: list[str] = []

    for word in words:
        trial = " ".join(current + [word])
        if draw.textlength(trial, font=font) <= max_width or not current:
            current.append(word)
        else:
            lines.append(" ".join(current))
            current = [word]
    if current:
        lines.append(" ".join(current))

    lines.reverse()
    if len(lines) > limit:
        lines = lines[-limit:]
        lines[0] = "…" + lines[0]
    return lines
