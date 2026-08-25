"""جمع‌آوری اخبار تکنولوژی و امنیت، رتبه‌بندی، خلاصه به فارسی، ارسال به تلگرام."""

import argparse
import html
import logging
import os
import sys
import time
from datetime import datetime, timedelta, timezone
from pathlib import Path

import httpx
import yaml

from . import article, cluster, images, scoring, sources, telegram, translate
from .models import Item
from .state import State, now_utc

ROOT = Path(__file__).resolve().parent.parent
FALLBACK_IMAGE = ROOT / "assets" / "fallback.png"

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
    # httpx هر درخواست را با URL کامل لاگ می‌کند و کلید Gemini در query string
    # است. در Actions ماسک می‌شود ولی در اجرای محلی روی ترمینال می‌افتد، و لاگ
    # هر فید هم چیزی به آدم نمی‌گوید.
    logging.getLogger("httpx").setLevel(logging.WARNING)

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
            publish(chosen, state, settings, client, args, token, channel, gemini_key)

        maybe_digest(state, settings, client, args, token, channel)

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

    def within_window(item: Item) -> bool:
        if item.max_age_hours:
            return item.published >= now - timedelta(hours=item.max_age_hours)
        return item.published >= (viral_cutoff if item.ranked else editorial_cutoff)

    fresh = [
        item
        for item in items
        if not state.has(item.uid)
        and within_window(item)
        and len(item.text) >= (min_ranked_text if item.ranked else min_text)
    ]
    fresh = _drop_repeats(fresh, state.recent_stories(now))

    ranked = scoring.rank(fresh, state)
    viral = [i for i in ranked if i.ranked]
    editorial = [i for i in ranked if not i.ranked]

    # چند گزینه‌ی یدکی هم برمی‌داریم: اگر ترجمه‌ی اولی شکست بخورد اجرا بی‌نتیجه
    # نماند. publish به‌محض رسیدن به سهمیه می‌ایستد.
    total = settings.get("max_posts_per_run", 1)
    wanted = total + settings.get("spare_candidates", 3)
    chosen = _fill(viral, editorial, wanted, settings.get("viral_share", 0.5), state)

    log.info(
        "%d ماجرا (%d ویروسی، %d خبری) → %d انتخاب",
        len(ranked),
        len(viral),
        len(editorial),
        len(chosen),
    )
    if explain:
        _explain(chosen)

    # انتخاب بر اساس امتیاز، ولی ارسال به ترتیب زمانی.
    chosen.sort(key=lambda i: i.published)
    return chosen


def _drop_repeats(items: list[Item], stories: list[set[str]]) -> list[Item]:
    """خبرهایی که همان ماجرای قبلاً پست‌شده‌اند را کنار می‌گذارد.

    اینجا همان جایی است که تکرار واقعاً متوقف می‌شود. خوشه‌بندی فقط *داخل* یک
    اجرا کار می‌کند: هفت رسانه‌ای که همزمان در فید هستند یک پست می‌شوند. ولی
    پوشش یک خبر بزرگ ساعت‌ها ادامه دارد و رسانه‌ی هشتم دو ساعت بعد می‌رسد،
    وقتی خوشه‌ی قبلی دیگر وجود ندارد و URLش هم در seen نیست. تنها چیزی که از
    آن اجرا باقی مانده امضای ماجراست، و مقایسه با همان، نسخه‌های دیرهنگام را
    می‌گیرد.
    """
    kept: list[Item] = []
    dropped = 0

    for item in items:
        item.signature = cluster.signature_of(item.text)
        if cluster.best_match(item.signature, stories) >= cluster.SIMILARITY:
            dropped += 1
            continue
        kept.append(item)

    if dropped:
        log.info("%d خبر تکراری از ماجراهای پست‌شده کنار رفت", dropped)
    return kept


def _archive(state: State, item: Item) -> None:
    """آیتم و همه‌ی نسخه‌های دیگرش را seen می‌کند.

    نشان‌کردن تنها همان یکی که پست شد کافی نیست: پنج رسانه‌ی دیگرِ همان خوشه
    URL جدا دارند و اجرای بعد دوباره بالا می‌آیند.
    """
    state.mark(item.uid)
    for uid in item.members:
        state.mark(uid)


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


def maybe_digest(state, settings, client, args, token, channel) -> None:
    """جمعه‌ها داغ‌ترین‌های هفته را در یک پست می‌گذارد.

    این نوع پست بیشترین forward را می‌گیرد، و forward تنها رشد ارگانیک واقعی
    تلگرام است. امتیازها را همان الگوریتم رتبه‌بندی داده، پس چیز تازه‌ای حساب
    نمی‌شود؛ فقط بهترین‌های ثبت‌شده دوباره مرتب می‌شوند.
    """
    if not settings.get("weekly_digest", True):
        return

    now = datetime.now(timezone.utc)
    today = now.date().isoformat()
    if state.last_digest == today:
        return
    if now.weekday() != settings.get("digest_weekday", 4):
        return
    if now.hour < settings.get("digest_hour_utc", 14):
        return

    rows = sorted(state.week(now), key=lambda r: r.get("score", 0), reverse=True)
    top = rows[: settings.get("digest_size", 5)]
    if len(top) < 3:
        log.info("خلاصه‌ی هفتگی نرفت: فقط %d پست در هفته", len(top))
        return

    lines = ["<b>🔥 داغ‌ترین‌های هفته</b>", ""]
    for index, row in enumerate(top, 1):
        title = html.escape(row.get("title", "")[:150])
        source = html.escape(row.get("source", ""))
        url = html.escape(row.get("url", ""), quote=True)
        lines.append(f'{index}. <a href="{url}">{title}</a>')
        lines.append(f"    <i>{source}</i>")
        lines.append("")
    handle = channel if channel.startswith("@") else ""
    if handle:
        lines.append(html.escape(handle))
    text = "\n".join(lines)

    if args.dry_run:
        print("\n" + "=" * 60)
        print(text)
        return

    try:
        telegram.send_digest(text, token, channel, client)
    except telegram.TelegramError as exc:
        log.error("خلاصه‌ی هفتگی نرفت: %s", exc)
        return

    state.last_digest = today
    log.info("خلاصه‌ی هفتگی ارسال شد (%d خبر)", len(top))


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
    models = settings.get("gemini_models") or ["gemini-2.5-flash"]
    delay = settings.get("seconds_between_posts", 4)
    target = settings.get("max_posts_per_run", 1)
    scrape_text = settings.get("scrape_article_text", True)
    sent = 0

    # در dry-run بدون کلید هم باید بشود شکل خروجی را دید.
    translating = bool(gemini_key)
    if not translating:
        log.warning("GEMINI_API_KEY نیست — متن اصلی انگلیسی نشان داده می‌شود.")

    for item in items:
        if sent >= target:
            break

        # کاندیداهای یدکی قبل از ارسالِ اولی انتخاب شده‌اند، پس ممکن است دو
        # تایشان یک ماجرا باشند که خوشه‌بندی از هم جدایشان کرده. حالا که
        # ماجرای پست‌شده ثبت شده، دوباره می‌سنجیم.
        if cluster.best_match(item.signature, state.recent_stories(now_utc())) >= cluster.SIMILARITY:
            log.info("رد شد، همان ماجرای پست‌شده: %s — %s", item.label, item.text[:60])
            _archive(state, item)
            continue

        if not translating:
            title, persian, tag = "", item.text, ""
        else:
            try:
                # متن کامل مقاله را می‌گیریم؛ تیتر تنها برای خلاصه‌ی ۲۵۰
                # کلمه‌ای کافی نیست و مدل مجبور می‌شد تیتر را بازنویسی کند.
                full = article.fetch(item.url, client) if scrape_text else None
                source_text = "\n\n".join(part for part in (item.text, full) if part)
                result = translate.summarize(source_text, gemini_key, models, client)
                title, persian, tag = result if result else ("", None, "")
            except translate.TranslationError as exc:
                # نه mark می‌کنیم نه می‌فرستیم — اجرای بعدی دوباره تلاش می‌کند.
                log.warning("ترجمه شکست خورد (%s): %s", item.label, exc)
                continue

        if persian is None:
            log.info("رد شد توسط مدل: %s — %s", item.label, item.text[:60])
            # مدل خودِ ماجرا را نامناسب دیده، نه فقط این روایت را؛ پس نسخه‌های
            # دیگرش هم نباید اجرای بعد دوباره ترجمه شوند.
            _archive(state, item)
            continue

        # عکس را همین‌جا حل می‌کنیم نه زودتر: گزینه‌های یدکی معمولاً استفاده
        # نمی‌شوند و گرفتن عکسشان فقط درخواست هدررفته بود.
        images.resolve(item, client, enabled=settings.get("scrape_images", True))

        if args.dry_run:
            print("\n" + "─" * 60)
            print(f"[عکس] {item.image_url or 'assets/fallback.png (پیش‌فرض)'}")
            print(telegram.build_message(item, title, persian, tag, channel))
            buttons = telegram.build_keyboard(item)["inline_keyboard"]
            print("[دکمه‌ها] " + " | ".join(b["text"] for row in buttons for b in row))
            _archive(state, item)
            state.remember_story(
                item.story_signature or item.signature, datetime.now(timezone.utc)
            )
            state.count_post("viral" if item.ranked else "editorial", item.label)
            sent += 1
            continue

        try:
            telegram.publish(item, title, persian, token, channel, client, tag)
        except telegram.TelegramError as exc:
            log.error("ارسال نشد: %s", exc)
            continue

        now = datetime.now(timezone.utc)
        log.info(
            "ارسال شد: %-18s %s  (%d نسخه‌ی دیگر از همین ماجرا بایگانی شد)",
            item.label,
            "🖼" if item.image_url else " ",
            max(len(item.members) - 1, 0),
        )
        _archive(state, item)
        state.count_post("viral" if item.ranked else "editorial", item.label)
        state.remember(item, title or persian, now)
        state.remember_story(item.story_signature or item.signature, now)
        sent += 1

        if sent < target:
            time.sleep(delay)


if __name__ == "__main__":
    sys.exit(main())
