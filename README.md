# persian-tech-tube-bot

اخبار و پست‌های تکنولوژی را از Bluesky و RSS جمع می‌کند، با Gemini به فارسی خلاصه
می‌کند، و در کانال تلگرام می‌گذارد. هر ۳۰ دقیقه روی GitHub Actions اجرا می‌شود.

هزینه‌ی کل صفر است: Bluesky API باز است، Gemini free tier دارد، و GitHub Actions
برای repo عمومی دقیقه‌ی نامحدود می‌دهد.

## راه‌اندازی

**۱. کلید Gemini بگیرید** از [aistudio.google.com/apikey](https://aistudio.google.com/apikey).

**۲. کانال بسازید** و بات را به‌عنوان admin با دسترسی Post Messages اضافه کنید.

**۳. تست محلی:**

```bash
python -m venv .venv
.venv/Scripts/python.exe -m pip install -r requirements.txt
cp .env.example .env      # و پرش کنید
.venv/Scripts/python.exe -m src.main --dry-run
```

`--dry-run` چیزی نمی‌فرستد و `state.json` را دست نمی‌زند — فقط نشان می‌دهد چه
پستی می‌رفت. بدون `GEMINI_API_KEY` هم کار می‌کند و متن اصلی انگلیسی را نشان می‌دهد.

**۴. روی GitHub:** repo را push کنید و در Settings → Secrets → Actions این سه را
بسازید:

| Secret | مقدار |
|---|---|
| `TELEGRAM_BOT_TOKEN` | token از BotFather |
| `TELEGRAM_CHANNEL` | `@your_channel` |
| `GEMINI_API_KEY` | کلید Gemini |

بعد در تب Actions، workflow با نام `publish` را دستی `Run workflow` بزنید تا
مطمئن شوید کار می‌کند. از آن به بعد خودش هر ۳۰ دقیقه اجرا می‌شود.

## منابع

در [config.yaml](config.yaml) تعریف می‌شوند. دو نوع:

```yaml
- type: bluesky          # بدون کلید، بدون rate limit جدی
  handle: simonwillison.net
  label: Simon Willison

- type: rss              # هر فید RSS
  url: https://xcancel.com/OpenAI/rss
  label: OpenAI
```

برای توییتر از `xcancel.com/<user>/rss` استفاده کنید. instance های Nitter هر چند
ماه می‌میرند؛ اگر منبعی از کار افتاد فقط همان خط را عوض کنید — بقیه‌ی اجرا با
مرگ یک منبع متوقف نمی‌شود.

## چطور کار می‌کند

```
config.yaml → sources.py → فیلتر (تازه؟ قدیمی نیست؟ به‌اندازه بلند هست؟)
            → translate.py (Gemini؛ SKIP برای محتوای غیرتک یا تبلیغ)
            → telegram.py → کانال
            → state.json (uid های دیده‌شده، workflow خودش commit می‌کند)
```

`state.json` مانع تکرار پست است. اولین اجرا فقط جدیدترین‌ها را می‌فرستد و بقیه را
seen علامت می‌زند تا کانال با ۵۰ پست قدیمی پر نشود.

اگر ترجمه‌ی یک آیتم شکست بخورد، آن آیتم seen نمی‌شود و اجرای بعدی دوباره تلاش
می‌کند.

## تنظیمات

در بخش `settings` فایل [config.yaml](config.yaml):

| کلید | کار |
|---|---|
| `max_posts_per_run` | سقف پست در هر اجرا |
| `max_age_hours` | آیتم قدیمی‌تر از این نادیده گرفته می‌شود |
| `seconds_between_posts` | فاصله برای نخوردن به rate limit تلگرام |
| `min_text_length` | متن کوتاه‌تر از این احتمالاً ریپلای است، نه خبر |
| `gemini_model` | پیش‌فرض `gemini-2.5-flash` |

## نکته‌ی حقوقی

بات خلاصه‌ی چندجمله‌ای به‌همراه لینک منبع می‌فرستد، نه متن کامل — این کار در
محدوده‌ی نقل قول منصفانه است. اگر تغییرش دادید که متن کامل مقالات را بازنشر کند،
مسئله‌ی کپی‌رایت پیدا می‌کنید.
