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
from urllib.parse import urljoin, urlparse

import httpx

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

_META = re.compile(
    r"<meta[^>]+(?:property|name)=[\"'](og:image(?::url)?|twitter:image(?::src)?)[\"'][^>]*>",
    re.IGNORECASE,
)
_CONTENT = re.compile(r"content=[\"']([^\"']+)[\"']", re.IGNORECASE)
_IMG = re.compile(r"<img\b[^>]*>", re.IGNORECASE)
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
    if item.image_url and _usable(item.image_url, client):
        return
    item.image_url = None

    if not enabled or not item.url.startswith("http"):
        return

    html = _fetch(item.url, client)
    if not html:
        return

    for candidate in _candidates(html, item.url):
        if _usable(candidate, client):
            item.image_url = candidate
            return

    log.info("تصویری پیدا نشد، عکس پیش‌فرض می‌رود: %s", item.url[:70])


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


def _candidates(html: str, base: str) -> list[str]:
    """اول متا تگ‌ها، بعد تصاویر داخل متن. ترتیب همان ترتیب امتحان‌کردن است."""
    found: list[str] = []

    for tag in _META.finditer(html):
        content = _CONTENT.search(tag.group(0))
        if content:
            found.append(content.group(1))

    for tag in _IMG.finditer(html):
        src = _SRC.search(tag.group(0))
        if not src:
            continue
        # srcset چند اندازه با ویرگول می‌دهد؛ اولی کافی است.
        found.append(src.group(1).split(",")[0].strip().split(" ")[0])

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
        if _JUNK.search(url) or path.endswith(_BAD_EXT):
            continue
        out.append(url)
    return out[:8]


def _usable(url: str, client: httpx.Client) -> bool:
    """تصویر واقعاً وجود دارد، تصویر است، و برای تلگرام زیادی بزرگ نیست."""
    try:
        resp = client.head(url, headers=BROWSER, timeout=12, follow_redirects=True)
        if resp.status_code >= 400:
            # خیلی از CDN ها HEAD را رد می‌کنند ولی GET را می‌دهند.
            resp = client.get(url, headers=BROWSER, timeout=12, follow_redirects=True)
        if resp.status_code >= 400:
            return False

        kind = resp.headers.get("content-type", "")
        if not kind.startswith("image/") or kind.startswith("image/svg"):
            return False

        length = resp.headers.get("content-length")
        return not (length and int(length) > MAX_BYTES)
    except Exception:
        return False
