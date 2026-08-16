"""جمع‌آوری پست‌های تکنولوژی، خلاصه به فارسی، ارسال به کانال تلگرام."""

import argparse
import logging
import os
import sys
import time
from datetime import datetime, timedelta, timezone
from pathlib import Path

import httpx
import yaml

from . import sources, telegram, translate
from .models import Item
from .state import State

ROOT = Path(__file__).resolve().parent.parent

log = logging.getLogger("bot")


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument(
        "--dry-run",
        action="store_true",
        help="چیزی نفرست و state را ذخیره نکن؛ فقط نشان بده چه پستی می‌رفت.",
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
        log.info("اولین اجرا — فقط جدیدترین پست‌ها می‌روند، بقیه seen علامت می‌خورند.")

    with httpx.Client(timeout=30, follow_redirects=True) as client:
        items = collect(config.get("sources", []), client)
        fresh = select(items, state, settings)

        if not fresh:
            log.info("چیز جدیدی نبود.")
        else:
            publish(fresh, state, settings, client, args, token, channel, gemini_key)

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
        label = source.get("label", source.get("handle") or source.get("url"))
        try:
            got = sources.fetch(source, client)
        except Exception as exc:
            log.warning("منبع %s خوانده نشد: %s", label, exc)
            continue
        log.info("%-18s %d آیتم", label, len(got))
        items.extend(got)
    return items


def select(items: list[Item], state: State, settings: dict) -> list[Item]:
    """آیتم‌های تازه و به‌دردبخور، مرتب‌شده از قدیم به جدید."""
    cutoff = datetime.now(timezone.utc) - timedelta(hours=settings.get("max_age_hours", 24))
    min_length = settings.get("min_text_length", 80)

    candidates = [
        item
        for item in items
        if not state.has(item.uid) and item.published >= cutoff and len(item.text) >= min_length
    ]

    # جدیدترین‌ها را انتخاب کن، ولی به ترتیب زمانی پست کن.
    candidates.sort(key=lambda i: i.published, reverse=True)
    chosen = candidates[: settings.get("max_posts_per_run", 6)]
    chosen.reverse()

    log.info("%d آیتم تازه، %d تا برای این اجرا", len(candidates), len(chosen))
    return chosen


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

        message = telegram.build_message(item.label, persian, item.url)

        if args.dry_run:
            print("\n" + "─" * 60)
            print(message)
            state.mark(item.uid)
            continue

        try:
            telegram.send(message, token, channel, client)
        except telegram.TelegramError as exc:
            log.error("ارسال نشد: %s", exc)
            continue

        log.info("ارسال شد: %s", item.label)
        state.mark(item.uid)

        if index < len(items) - 1:
            time.sleep(delay)


if __name__ == "__main__":
    sys.exit(main())
