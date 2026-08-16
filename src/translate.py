"""ترجمه و خلاصه‌سازی فارسی با Gemini free tier."""

import logging
import time

import httpx

log = logging.getLogger(__name__)

# free tier مرتب 429 و 503 می‌دهد و هر دو گذرا هستند.
RETRY_ON = {429, 500, 503}
ATTEMPTS = 4

ENDPOINT = "https://generativelanguage.googleapis.com/v1beta/models/{model}:generateContent"

PROMPT = """\
متن زیر یک خبر یا پست تکنولوژی به زبان انگلیسی است.

آن را به یک خلاصه‌ی فارسی روان و طبیعی تبدیل کن، بین یک تا سه جمله.

قواعد:
- فارسی محاوره‌ای ننویس، ولی خشک و ماشینی هم نباشد.
- اصطلاحات فنی و نام محصولات و شرکت‌ها را به لاتین نگه دار.
- هیچ مقدمه یا توضیحی اضافه نکن. فقط خود خلاصه.
- ممکن است متن وسط جمله بریده شده باشد. فقط از همان چیزی که هست خلاصه بنویس و
  چیزی از خودت اضافه نکن.

فقط بنویس SKIP و هیچ چیز دیگر، اگر متن یکی از اینها بود:
- درباره‌ی تکنولوژی نیست (سرگرمی، فیلم، ورزش، سیاست، بازی بدون جنبه‌ی فنی)
- تبلیغ، کد تخفیف، یا معرفی محصول برای فروش است
- درخواست دنبال‌کردن یا محتوای بی‌ارزش خبری است

متن:
---
{text}
---"""


class TranslationError(RuntimeError):
    pass


def summarize(text: str, api_key: str, model: str, client: httpx.Client) -> str | None:
    """خلاصه‌ی فارسی برمی‌گرداند، یا None اگر مدل تشخیص داد ارزش پست‌کردن ندارد."""

    payload = {
        "contents": [{"parts": [{"text": PROMPT.format(text=text[:4000])}]}],
        "generationConfig": {"temperature": 0.3, "maxOutputTokens": 8192},
    }

    for attempt in range(1, ATTEMPTS + 1):
        resp = client.post(
            ENDPOINT.format(model=model),
            params={"key": api_key},
            json=payload,
            timeout=90,
        )
        if resp.status_code == 200:
            break
        if resp.status_code not in RETRY_ON or attempt == ATTEMPTS:
            raise TranslationError(f"Gemini {resp.status_code}: {resp.text[:300]}")

        backoff = 2**attempt
        log.info("Gemini %d — %d ثانیه صبر و تلاش دوباره", resp.status_code, backoff)
        time.sleep(backoff)

    candidates = resp.json().get("candidates") or []
    if not candidates:
        raise TranslationError(f"پاسخ Gemini خالی بود: {resp.text[:300]}")

    parts = candidates[0].get("content", {}).get("parts") or []
    out = "".join(p.get("text", "") for p in parts).strip()

    if not out or out.upper().startswith("SKIP"):
        return None
    return out
