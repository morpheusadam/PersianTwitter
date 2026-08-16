"""جمع‌آوری اخبار تکنولوژی و امنیت، رتبه‌بندی، خلاصه به فارسی، ارسال به تلگرام."""

import argparse
import logging
import os
import sys
import time
from datetime import datetime, timedelta, timezone
from pathlib import Path

import httpx
import yaml

from . import images, scoring, sources, telegram, translate
from .models import Item
from .state import State

ROOT = Path(__file__).resolve().parent.parent
FALLBACK_IMAGE = ROOT / "assets" / "fallback.jpg"

log = logging.getLogger("bot")


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument(
        "--dry-run",
        action="store_true",
        help="چیزی نفرست و state را ذخیره نکن؛ فقط نشان بده چه پستی می‌رفت.",
    )
    parser.add_argument(
        "--explain",
        action="store_true",
        help="جدول امتیازها را چاپ کن تا ببینی الگوریتم چه چیزی را چرا انتخاب کرد.",
    )
    parser.add_argument("--config", default=ROOT / "config.yaml", type=Path)
    parser.add_argument("--state", default=ROOT / "state.json", type=Path)
    args = parser.parse_args()

    logging.basicConfig(
        level=logging.INFO,
        format="%(asctime)s  %(levelname)-7s %(message)s",
        datefmt="%H:%M:%S",
    )

    load_dotenv(ROOT / ".env")

    config = yaml.safe_load(args.config.read_text(encoding="utf-8"))
    settings = config.get("settings", {})

    token = os.environ.get("TELEGRAM_BOT_TOKEN", "")
    channel = os.environ.get("TELEGRAM_CHANNEL", "")
    gemini_key = os.environ.get("GEMINI_API_KEY", "")

    missing = [
        name
        for name, value in [
            ("TELEGRAM_BOT_TOKEN", token),
            ("TELEGRAM_CHANNEL", channel),
            ("GEMINI_API_KEY", gemini_key),
        ]
        if not value
    ]
    if missing and not args.dry_run:
        log.error("متغیرهای محیطی تنظیم نشده: %s", ", ".join(missing))
        return 1

    state = State(args.state)
    if state.is_first_run:
        log.info("اولین اجرا — فقط بهترین‌ها می‌روند، بقیه seen علامت می‌خورند.")

    with httpx.Client(timeout=30, follow_redirects=True) as client:
        items = collect(config.get("sources", []), client)
        chosen = select(items, state, settings, explain=args.explain)

        if not chosen:
            log.info("چیز جدیدی نبود.")
        else:
            enrich_images(chosen, settings, client)
            publish(chosen, state, settings, client, args, token, channel, gemini_key)

    # در اولین اجرا هرچه پست نشد را seen می‌کنیم تا اجرای بعد سیل راه نیفتد.
    if state.is_first_run:
        for item in items:
            state.mark(item.uid)

    if args.dry_run:
        log.info("dry-run — state ذخیره نشد.")
    else:
        state.save()

    return 0


def load_dotenv(path: Path) -> None:
    """یک .env ساده. متغیرهایی که از قبل ست شده‌اند را دست نمی‌زند."""
    if not path.exists():
        return
    for line in path.read_text(encoding="utf-8").splitlines():
        line = line.strip()
        if not line or line.startswith("#") or "=" not in line:
            continue
        key, _, value = line.partition("=")
        os.environ.setdefault(key.strip(), value.strip().strip("\"'"))


def collect(source_configs: list[dict], client: httpx.Client) -> list[Item]:
    """همه‌ی منابع را می‌خواند. مرگ یک منبع نباید کل اجرا را زمین بزند."""
    items: list[Item] = []
    for source in source_configs:
        label = source.get("label") or source.get("handle") or source.get("url")
        try:
            got = sources.fetch(source, client)
        except Exception as exc:
            log.warning("منبع %s خوانده نشد: %s", label, exc)
            continue
        log.info("%-20s %d آیتم", label, len(got))
        items.extend(got)
    return items


def select(items: list[Item], state: State, settings: dict, explain: bool = False) -> list[Item]:
    """بهترین آیتم‌ها را با الگوریتم رتبه‌بندی انتخاب می‌کند.

    دو استخر جدا: منابعی که عدد تعامل دارند با امتیاز ویروسی، و فیدهای خبری با
    اعتبار و تازگی. سهم هرکدام با viral_share تعیین می‌شود. اگر یک استخر به
    سهمش نرسد، سهم استفاده‌نشده به آن یکی می‌رسد.
    """
    now = datetime.now(timezone.utc)
    viral_cutoff = now - timedelta(hours=settings.get("max_age_hours", 12))
    editorial_cutoff = now - timedelta(hours=settings.get("editorial_max_age_hours", 48))
    min_text = settings.get("min_text_length", 80)
    min_ranked_text = settings.get("min_ranked_text_length", 30)

    fresh = [
        item
        for item in items
        if not state.has(item.uid)
        and item.published >= (viral_cutoff if item.ranked else editorial_cutoff)
        and len(item.text) >= (min_ranked_text if item.ranked else min_text)
    ]

    ranked = scoring.rank(fresh)
    viral = [i for i in ranked if i.ranked]
    editorial = [i for i in ranked if not i.ranked]

    total = settings.get("max_posts_per_run", 1)
    chosen = _fill(viral, editorial, total, settings.get("viral_share", 0.6), state)

    log.info(
        "%d تازه (%d ویروسی، %d خبری) → %d انتخاب",
        len(fresh),
        len(viral),
        len(editorial),
        len(chosen),
    )
    if explain:
        _explain(chosen)

    # انتخاب بر اساس امتیاز، ولی ارسال به ترتیب زمانی.
    chosen.sort(key=lambda i: i.published)
    return chosen


def _fill(
    viral: list[Item],
    editorial: list[Item],
    total: int,
    viral_share: float,
    state: State,
) -> list[Item]:
    """جای خالی‌ها را طوری پر می‌کند که نسبت ۶۰/۴۰ در طول زمان حفظ شود.

    وقتی هر اجرا فقط یک پست می‌فرستد، تقسیم سهمیه داخل خودِ اجرا معنا ندارد؛
    ceil(1 × 0.6) = 1 یعنی استخر خبری هرگز نوبت نمی‌گیرد. پس در هر نوبت از
    استخری برمی‌داریم که نسبت به سهمش عقب‌تر است، بر اساس شمارش تجمعی state.
    اگر آن استخر خالی بود، آن یکی جایش را می‌گیرد.
    """
    counts = dict(state.posted)
    chosen: list[Item] = []
    pools = {"viral": list(viral), "editorial": list(editorial)}

    for _ in range(total):
        so_far = counts.get("viral", 0) + counts.get("editorial", 0)
        wants_viral = counts.get("viral", 0) < viral_share * (so_far + 1)
        order = ["viral", "editorial"] if wants_viral else ["editorial", "viral"]

        for pool in order:
            if pools[pool]:
                chosen.append(pools[pool].pop(0))
                counts[pool] = counts.get(pool, 0) + 1
                break
        else:
            break  # هر دو استخر خالی

    return chosen


def _explain(chosen: list[Item]) -> None:
    print("\n" + "─" * 78)
    print(f"{'score':>8}  {'source':<18}  detail")
    print("─" * 78)
    for item in chosen:
        detail = "  ".join(f"{k}={v}" for k, v in item.breakdown.items())
        print(f"{item.score:8.4f}  {item.label:<18}  {detail}")
        print(f"{'':8}  {'':<18}  {item.text[:70]}")
    print("─" * 78 + "\n")


def enrich_images(items: list[Item], settings: dict, client: httpx.Client) -> None:
    """هر آیتم را به یک تصویر معتبر می‌رساند، وگرنه به عکس پیش‌فرض می‌افتد."""
    scrape = settings.get("scrape_images", True)
    for item in items:
        images.resolve(item, client, enabled=scrape)


def publish(
    items: list[Item],
    state: State,
    settings: dict,
    client: httpx.Client,
    args,
    token: str,
    channel: str,
    gemini_key: str,
) -> None:
    model = settings.get("gemini_model", "gemini-2.5-flash")
    delay = settings.get("seconds_between_posts", 4)

    # در dry-run بدون کلید هم باید بشود شکل خروجی را دید.
    translating = bool(gemini_key)
    if not translating:
        log.warning("GEMINI_API_KEY نیست — متن اصلی انگلیسی نشان داده می‌شود.")

    for index, item in enumerate(items):
        if not translating:
            persian = item.text
        else:
            try:
                persian = translate.summarize(item.text, gemini_key, model, client)
            except translate.TranslationError as exc:
                # نه mark می‌کنیم نه می‌فرستیم — اجرای بعدی دوباره تلاش می‌کند.
                log.warning("ترجمه شکست خورد (%s): %s", item.label, exc)
                continue

        if persian is None:
            log.info("رد شد توسط مدل: %s — %s", item.label, item.text[:60])
            state.mark(item.uid)
            continue

        if args.dry_run:
            print("\n" + "─" * 60)
            print(f"[عکس] {item.image_url or 'assets/fallback.jpg (پیش‌فرض)'}")
            print(telegram.build_message(item.label, persian, telegram.MAX_CAPTION))
            buttons = telegram.build_keyboard(item)["inline_keyboard"]
            print("[دکمه‌ها] " + " | ".join(b["text"] for row in buttons for b in row))
            state.mark(item.uid)
            state.count_post("viral" if item.ranked else "editorial")
            continue

        try:
            telegram.publish(item, persian, token, channel, client, FALLBACK_IMAGE)
        except telegram.TelegramError as exc:
            log.error("ارسال نشد: %s", exc)
            continue

        log.info("ارسال شد: %-18s %s", item.label, "🖼" if item.image_url else "")
        state.mark(item.uid)
        state.count_post("viral" if item.ranked else "editorial")

        if index < len(items) - 1:
            time.sleep(delay)


if __name__ == "__main__":
    sys.exit(main())
