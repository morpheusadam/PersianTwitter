"""ترجمه و خلاصه‌سازی فارسی با Gemini free tier."""

import logging
import time

import httpx

log = logging.getLogger(__name__)

# 503 یعنی مدل شلوغ است و چند ثانیه بعد جواب می‌دهد. 429 فرق دارد: سهمیه‌ی
# روزانه‌ی آن مدل تمام شده و صبرکردن فایده ندارد.
BUSY = {500, 503}
EXHAUSTED = 429
ATTEMPTS = 3

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

این کانال فقط پنج موضوع دارد:
تکنولوژی · برنامه‌نویسی · هوش مصنوعی · هک و امنیت سایبری · شبکه و اینترنت

اگر موضوع *اصلی* متن یکی از این پنج نیست، فقط بنویس SKIP و هیچ چیز دیگر.
«موضوع اصلی» یعنی خبر درباره‌ی خودِ آن است، نه اینکه گذری به یک دستگاه یا
نرم‌افزار اشاره کرده باشد.

اینها SKIP می‌شوند حتی اگر جالب باشند:
- حادثه‌ی صنعتی، نیروگاه، انرژی، خودرو، هوافضا، بدون جنبه‌ی نرم‌افزاری یا امنیتی
- پزشکی، زیست‌شناسی، اقلیم، فضا، فیزیک، ریاضیات محض
- سیاست، اقتصاد کلان، جنگ، جرم و جنایت عادی
- سرگرمی، فیلم، موسیقی، ورزش، بازی بدون بحث فنی
- تاریخ، فلسفه، هنر، یادداشت شخصی
- تبلیغ، کد تخفیف، معرفی محصول برای فروش، درخواست دنبال‌کردن

متن:
---
{text}
---"""


class TranslationError(RuntimeError):
    pass


def summarize(text: str, api_key: str, models: list[str], client: httpx.Client) -> str | None:
    """خلاصه‌ی فارسی برمی‌گرداند، یا None اگر مدل تشخیص داد ارزش پست‌کردن ندارد.

    سهمیه‌ی رایگان Gemini «به ازای هر مدل در روز» است، نه کل حساب. پس وقتی یک
    مدل سهمیه‌اش تمام می‌شود سراغ مدل بعدی می‌رویم و بودجه‌ی روزانه چند برابر
    می‌شود بدون اینکه هزینه‌ای اضافه شود.
    """
    payload = {
        "contents": [{"parts": [{"text": PROMPT.format(text=text[:4000])}]}],
        "generationConfig": {"temperature": 0.3, "maxOutputTokens": 8192},
    }

    errors = []
    for model in models:
        resp = _ask(model, payload, api_key, client)
        if resp is None:
            errors.append(f"{model}: شلوغ")
            continue
        if resp.status_code == EXHAUSTED:
            log.info("سهمیه‌ی %s تمام شد، مدل بعدی", model)
            errors.append(f"{model}: سهمیه تمام")
            continue
        if resp.status_code != 200:
            errors.append(f"{model}: {resp.status_code} {resp.text[:120]}")
            continue
        return _extract(resp, model)

    raise TranslationError(" | ".join(errors) or "هیچ مدلی امتحان نشد")


def _ask(model, payload, api_key, client) -> httpx.Response | None:
    """None یعنی مدل بعد از چند تلاش هنوز شلوغ بود."""
    for attempt in range(1, ATTEMPTS + 1):
        resp = client.post(
            ENDPOINT.format(model=model),
            params={"key": api_key},
            json=payload,
            timeout=90,
        )
        if resp.status_code not in BUSY:
            return resp
        if attempt < ATTEMPTS:
            backoff = 2**attempt
            log.info("%s شلوغ است — %d ثانیه صبر", model, backoff)
            time.sleep(backoff)
    return None


def _extract(resp: httpx.Response, model: str) -> str | None:
    candidates = resp.json().get("candidates") or []
    if not candidates:
        raise TranslationError(f"پاسخ Gemini خالی بود: {resp.text[:300]}")

    parts = candidates[0].get("content", {}).get("parts") or []
    out = "".join(p.get("text", "") for p in parts).strip()

    if not out or out.upper().startswith("SKIP"):
        return None
    log.info("خلاصه با %s", model)
    return out
