"""استخراج متن کامل مقاله از صفحه‌ی وب.

فیدها معمولاً فقط تیتر و یک خلاصه‌ی بریده می‌دهند، و Hacker News و Lobsters
حتی همان را هم ندارند و فقط لینک‌اند. با آن ورودی، خلاصه‌ی فارسی چیزی جز
بازنویسی تیتر نمی‌شود.

trafilatura متن اصلی را از میان منو و تبلیغ و فوتر بیرون می‌کشد. انتخابش
به‌خاطر همین است که نگهداری می‌شود و روی سایت‌های خبری دقت خوبی دارد؛
newspaper3k ستاره‌ی بیشتری دارد ولی سال‌هاست رها شده.
"""

import logging

import httpx
import trafilatura

log = logging.getLogger(__name__)

BROWSER = {
    "User-Agent": (
        "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 "
        "(KHTML, like Gecko) Chrome/128.0.0.0 Safari/537.36"
    ),
    "Accept": "text/html,application/xhtml+xml,application/xml;q=0.9,*/*;q=0.8",
    "Accept-Language": "en-US,en;q=0.9",
}

# کمتر از این یعنی صفحه‌ی paywall یا چالش جاوااسکریپت گرفته‌ایم، نه مقاله.
MIN_USEFUL = 400

# سقف چیزی که به مدل می‌دهیم. مقاله‌های بلند اول‌شان مهم‌ترین بخش است.
MAX_CHARS = 8000


def fetch(url: str, client: httpx.Client) -> str | None:
    """متن مقاله، یا None اگر چیز به‌دردبخوری در نیامد."""
    if not url.startswith("http"):
        return None

    try:
        resp = client.get(url, headers=BROWSER, timeout=20, follow_redirects=True)
        resp.raise_for_status()
        if "html" not in resp.headers.get("content-type", ""):
            return None
    except Exception as exc:
        log.debug("مقاله گرفته نشد (%s): %s", url[:60], exc)
        return None

    try:
        text = trafilatura.extract(
            resp.text,
            include_comments=False,
            include_tables=False,
            no_fallback=False,
        )
    except Exception as exc:
        log.debug("استخراج متن شکست خورد (%s): %s", url[:60], exc)
        return None

    if not text or len(text) < MIN_USEFUL:
        return None
    return text[:MAX_CHARS]
