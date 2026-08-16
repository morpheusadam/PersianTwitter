"""پیدا کردن یک تصویر قابل استفاده برای هر پست.

ترتیب تلاش:

  1. تصویری که خود فید یا Bluesky داده
  2. og:image یا twitter:image صفحه‌ی مقاله
  3. تصاویر داخل خودِ متن مقاله
  4. هیچ‌کدام: تلگرام فایل پیش‌فرض assets/fallback.jpg را می‌گیرد

هر کاندیدا قبل از قبول‌شدن اعتبارسنجی می‌شود، چون یک URL شکسته باعث می‌شود
sendPhoto رد کند و پست بدون عکس برود.
"""

import html as html_lib
import logging
import re
from io import BytesIO
from urllib.parse import urljoin, urlparse

import httpx
from PIL import Image

from .models import Item

log = logging.getLogger(__name__)

# با User-Agent رباتی خیلی از سایت‌ها ۴۰۳ می‌دهند و آن‌وقت هیچ تصویری پیدا
# نمی‌شود. اینجا صفحه را مثل یک مرورگر می‌گیریم.
BROWSER = {
    "User-Agent": (
        "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 "
        "(KHTML, like Gecko) Chrome/128.0.0.0 Safari/537.36"
    ),
    "Accept": "text/html,application/xhtml+xml,application/xml;q=0.9,image/avif,image/webp,*/*;q=0.8",
    "Accept-Language": "en-US,en;q=0.9",
}

# سقف sendPhoto وقتی فایل را خودمان می‌فرستیم ۱۰ مگابایت است؛ با حاشیه.
MAX_BYTES = 5_000_000
HTML_LIMIT = 400_000

# کوچک‌تر از این در تلگرام یک مربع ریز می‌شود. آیکون ۵۶ پیکسلی سایت‌ها دقیقاً
# همین‌جا رد می‌شود و پست به عکس پیش‌فرض می‌افتد، که تمیزتر از لوگوی کش‌آمده است.
MIN_SIDE = 400

# وقتی چیزی در این حد پیدا شد دیگر دنبال بهتر نگرد؛ بقیه فقط درخواست اضافه‌اند.
GOOD_ENOUGH = 900

_META = re.compile(
    r"<meta[^>]+(?:property|name)=[\"'](og:image(?::url)?|twitter:image(?::src)?)[\"'][^>]*>",
    re.IGNORECASE,
)
_CONTENT = re.compile(r"content=[\"']([^\"']+)[\"']", re.IGNORECASE)
_IMG = re.compile(r"<img\b[^>]*>", re.IGNORECASE)
_LINK_ICON = re.compile(r"<link\b[^>]*rel=[\"'][^\"']*icon[^\"']*[\"'][^>]*>", re.IGNORECASE)
_HREF = re.compile(r"href=[\"']([^\"']+)[\"']", re.IGNORECASE)
_SRC = re.compile(r"\b(?:data-src|data-original|srcset|src)=[\"']([^\"']+)[\"']", re.IGNORECASE)

# آیکون، لوگو، آواتار، اسپریت و پیکسل ردیابی. اینها عکس خبر نیستند.
_JUNK = re.compile(
    r"(sprite|icon|logo|avatar|favicon|placeholder|pixel|spacer|blank|1x1|"
    r"badge|button|emoji|gravatar|advert|banner)",
    re.IGNORECASE,
)
_BAD_EXT = (".svg", ".gif", ".ico", ".webp?")


def resolve(item: Item, client: httpx.Client, enabled: bool = True) -> None:
    """image_url آیتم را روی یک تصویر معتبر تنظیم می‌کند، یا None می‌گذارد."""
    if item.image_url and _measure(item.image_url, client):
        return
    item.image_url = None

    if not enabled or not item.url.startswith("http"):
        return

    html = _fetch(item.url, client)
    if html:
        best = _pick(_candidates(html, item.url), client)
        if best:
            item.image_url = best
            return

    # مقاله هیچ عکسی نداشت: تصویر شاخص خودِ وب‌سایت را بردار.
    site = _site_image(item.url, client)
    if site:
        item.image_url = site
        return

    log.info("تصویری پیدا نشد، عکس پیش‌فرض می‌رود: %s", item.url[:70])


def _site_image(url: str, client: httpx.Client) -> str | None:
    """تصویر شاخص خودِ سایت، نه صفحه‌ی مقاله.

    اول ریشه‌ی سایت را می‌خوانیم چون og:image صفحه‌ی اصلی معمولاً کاور برند
    است. اگر نبود سراغ آیکون‌ها می‌رویم، و آخرش سرویس favicon گوگل که تقریباً
    برای هر دامنه‌ای چیزی برمی‌گرداند.
    """
    parts = urlparse(url)
    if not parts.netloc:
        return None
    root = f"{parts.scheme}://{parts.netloc}/"

    html = _fetch(root, client)
    if html:
        return _pick(_candidates(html, root, icons_ok=True), client)
    return None


def _fetch(url: str, client: httpx.Client) -> str | None:
    try:
        resp = client.get(url, headers=BROWSER, timeout=20)
        resp.raise_for_status()
        if "html" not in resp.headers.get("content-type", ""):
            return None
        return resp.text[:HTML_LIMIT]
    except Exception as exc:
        log.debug("صفحه خوانده نشد (%s): %s", url[:60], exc)
        return None


def _candidates(html: str, base: str, icons_ok: bool = False) -> list[str]:
    """اول متا تگ‌ها، بعد تصاویر داخل متن. ترتیب همان ترتیب امتحان‌کردن است.

    icons_ok برای صفحه‌ی اصلی سایت است: آنجا apple-touch-icon دقیقاً همان
    چیزی است که می‌خواهیم، در حالی که در صفحه‌ی مقاله فقط نویز است.
    """
    found: list[str] = []

    for tag in _META.finditer(html):
        content = _CONTENT.search(tag.group(0))
        if content:
            found.append(content.group(1))

    if icons_ok:
        for tag in _LINK_ICON.finditer(html):
            href = _HREF.search(tag.group(0))
            if href:
                found.append(href.group(1))

    for tag in _IMG.finditer(html):
        src = _SRC.search(tag.group(0))
        if not src:
            continue
        # srcset چند اندازه با ویرگول می‌دهد و از کوچک به بزرگ مرتب است؛
        # آخری بزرگ‌ترین است. قبلاً اولی برداشته می‌شد یعنی همیشه کوچک‌ترین.
        found.append(src.group(1).split(",")[-1].strip().split(" ")[0])

    seen: set[str] = set()
    out: list[str] = []
    for raw in found:
        # داخل HTML آدرس‌ها escape شده‌اند. بدون unescape، هر URL دارای پارامتر
        # با &amp; می‌ماند و تلگرام آن را ۴۰۴ می‌گیرد.
        url = urljoin(base, html_lib.unescape(raw.strip()))
        if not url.startswith("http") or url in seen:
            continue
        seen.add(url)
        path = urlparse(url).path.lower()
        # روی صفحه‌ی اصلی سایت دنبال همین آیکون‌ها هستیم، پس فیلتر «آشغال» را
        # آنجا اعمال نمی‌کنیم. ico همیشه رد می‌شود چون تلگرام قبولش نمی‌کند.
        if path.endswith(_BAD_EXT):
            continue
        if not icons_ok and _JUNK.search(url):
            continue
        out.append(url)
    return out[:8]


def _pick(candidates: list[str], client: httpx.Client) -> str | None:
    """بزرگ‌ترین تصویر قابل قبول. اولین گزینه لزوماً بهترین نیست."""
    best, best_area = None, 0
    for url in candidates:
        size = _measure(url, client)
        if size is None:
            continue
        width, height = size
        if min(width, height) < MIN_SIDE:
            continue
        area = width * height
        if area > best_area:
            best, best_area = url, area
        if min(width, height) >= GOOD_ENOUGH:
            break
    return best


def _measure(url: str, client: httpx.Client) -> tuple[int, int] | None:
    """ابعاد واقعی تصویر، یا None اگر قابل استفاده نبود.

    content-type به‌تنهایی کافی نیست: خیلی از سایت‌ها یک آیکون ۵۶ پیکسلی را هم
    image/png اعلام می‌کنند و آن در کانال افتضاح دیده می‌شود.
    """
    try:
        resp = client.get(url, headers=BROWSER, timeout=15, follow_redirects=True)
        if resp.status_code >= 400:
            return None

        kind = resp.headers.get("content-type", "")
        if not kind.startswith("image/") or kind.startswith("image/svg"):
            return None
        if len(resp.content) > MAX_BYTES:
            return None

        with Image.open(BytesIO(resp.content)) as image:
            return image.size
    except Exception:
        return None
