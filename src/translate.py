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
متن زیر یک خبر یا مقاله‌ی تکنولوژی به زبان انگلیسی است.

خروجی دقیقاً این ساختار را دارد:

خط اول: یکی از این پنج دسته، و بعد از آن صفر تا دو برچسب از فهرست دوم،
همه با فاصله. فقط همین کلمات، دقیقاً با همین املا. چیزی از خودت نساز.

دسته (حتماً یکی):
AI · SECURITY · PROGRAMMING · NETWORK · TECH

برچسب (اختیاری، حداکثر دو تا، فقط اگر واقعاً موضوع متن است):
LINUX WINDOWS ANDROID APPLE GOOGLE MICROSOFT OPENSOURCE PYTHON JAVASCRIPT
RUST MALWARE RANSOMWARE BREACH VULNERABILITY PHISHING CRYPTO CLOUD DATABASE
MOBILE HARDWARE CHIP STARTUP PRIVACY LLM CHATBOT BROWSER GAMEDEV DEVOPS

نمونه‌ی خط اول:  SECURITY RANSOMWARE WINDOWS

خط دوم: یک تیتر فارسی کوتاه، حداکثر دوازده کلمه، بدون نقطه‌ی پایانی.

بعد یک خط خالی، و بعد **یک پاراگراف**: حدود ۶۰ تا ۸۰ کلمه فارسی. بیشتر ننویس.

آن یک پاراگراف باید *توضیح* بدهد، نه اینکه تیتر را بازنویسی کند: چه اتفاقی
افتاده، و مهم‌ترین جزئیات مشخص مثل نام محصول، عدد، شماره‌ی CVE یا روش حمله.
جای همه‌ی جزئیات نیست؛ آن‌قدر بگو که خواننده بفهمد ماجرا چیست و تصمیم بگیرد
مقاله را باز کند یا نه.

قواعد:
- فارسی محاوره‌ای ننویس، ولی خشک و ماشینی هم نباشد.
- اصطلاحات فنی و نام محصولات و شرکت‌ها را به لاتین نگه دار.
- هیچ مقدمه‌ای مثل «در این خبر آمده است» ننویس. مستقیم برو سر اصل مطلب.
- فقط از چیزی که در متن هست بنویس. هیچ عدد، نام یا ادعایی از خودت اضافه نکن.
- اگر متن آن‌قدر محتوا نداشت، کوتاه‌تر بنویس. کش دادن ممنوع.

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
        # حالا متن کامل مقاله می‌آید نه فقط تیتر، پس سقف ورودی بالاتر است.
        "contents": [{"parts": [{"text": PROMPT.format(text=text[:8000])}]}],
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
    return _split(out)


# دسته‌ها ثابت‌اند تا هشتگ‌ها یکدست بمانند. جستجوی داخل تلگرام روی رشته‌ی دقیق
# کار می‌کند، پس هشتگی که هر بار کمی فرق کند بی‌فایده است.
TAGS = {
    "AI": "#هوش_مصنوعی",
    "SECURITY": "#امنیت",
    "PROGRAMMING": "#برنامه_نویسی",
    "NETWORK": "#شبکه",
    "TECH": "#تکنولوژی",
}

# برچسب‌های مشخص. کسی «#تکنولوژی» را جستجو نمی‌کند ولی «#باج_افزار» را می‌کند.
# واژگان بسته است تا املا هر بار یکی باشد؛ هشتگی که کمی فرق کند حجم جمع نمی‌کند.
EXTRA = {
    "LINUX": "#لینوکس",
    "WINDOWS": "#ویندوز",
    "ANDROID": "#اندروید",
    "APPLE": "#اپل",
    "GOOGLE": "#گوگل",
    "MICROSOFT": "#مایکروسافت",
    "OPENSOURCE": "#متن_باز",
    "PYTHON": "#پایتون",
    "JAVASCRIPT": "#جاوااسکریپت",
    "RUST": "#Rust",
    "MALWARE": "#بدافزار",
    "RANSOMWARE": "#باج_افزار",
    "BREACH": "#نشت_اطلاعات",
    "VULNERABILITY": "#آسیب_پذیری",
    "PHISHING": "#فیشینگ",
    "CRYPTO": "#رمزارز",
    "CLOUD": "#کلاد",
    "DATABASE": "#دیتابیس",
    "MOBILE": "#موبایل",
    "HARDWARE": "#سخت_افزار",
    "CHIP": "#تراشه",
    "STARTUP": "#استارتاپ",
    "PRIVACY": "#حریم_خصوصی",
    "LLM": "#مدل_زبانی",
    "CHATBOT": "#چت_بات",
    "BROWSER": "#مرورگر",
    "GAMEDEV": "#بازی_سازی",
    "DEVOPS": "#DevOps",
}

# بیشتر از این، پست اسپم به نظر می‌رسد و forward نمی‌شود.
MAX_TAGS = 3


def _split(out: str) -> tuple[str, str, str]:
    """خروجی مدل را به تیتر، متن و هشتگ می‌شکند.

    اگر مدل قالب را رعایت نکرد، بدترین حالت این است که تیتر خالی بماند و
    همه‌چیز متن شود؛ پست باز هم درست می‌رود.
    """
    lines = [line.strip() for line in out.splitlines()]
    tag = TAGS["TECH"]

    words = lines[0].replace("·", " ").replace(",", " ").split() if lines else []
    keys = [w.strip("#*").upper() for w in words]
    if keys and keys[0] in TAGS:
        picked = [TAGS[keys[0]]]
        for key in keys[1:]:
            if key in EXTRA and EXTRA[key] not in picked:
                picked.append(EXTRA[key])
        tag = "  ".join(picked[:MAX_TAGS])
        lines = lines[1:]

    while lines and not lines[0]:
        lines = lines[1:]
    if not lines:
        return "", out.strip(), tag

    title = lines[0].lstrip("#*• ").rstrip(".")
    body = "\n".join(lines[1:]).strip()
    if not body:
        return "", title, tag
    return title, body, tag
