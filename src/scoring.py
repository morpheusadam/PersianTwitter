"""رتبه‌بندی آیتم‌ها بر اساس سرعت رشد تعامل.

سه ایده‌ی جاافتاده را ترکیب می‌کند:

  1. Hacker News — score = (P-1) / (T+2)^1.8
     زوال زمانی با نمای gravity. چون نمای زمان بزرگ‌تر از نمای امتیاز است،
     هیچ آیتمی برای همیشه بالا نمی‌ماند.

  2. Reddit hot — log10(votes)
     بازده نزولی: ده رأی اول به‌اندازه‌ی صد رأی بعدی ارزش دارد. جلوی این را
     می‌گیرد که یک پست با ۵۰۰۰ لایک تا ابد همه‌چیز را خفه کند.

  3. velocity normalization
     پژوهش‌های viral detection می‌گویند نرخ انباشت تعامل در ساعت اول قوی‌ترین
     پیش‌بینی‌کننده‌ی ویروسی‌شدن است، و باید نسبت به baseline همان منبع سنجیده
     شود نه به عدد مطلق. یک اکانت با ۵۰۰ فالوور که ۵۰ لایک می‌گیرد داغ‌تر از
     اکانتی است با ۵۰۰هزار فالوور که ۲۰۰ لایک می‌گیرد.

فرمول نهایی:

    quality  = log10(1 + engagement)
    velocity = engagement / (age_hours + 0.5)
    ratio    = velocity / baseline_of_source
    score    = quality × (1 + log10(1 + ratio)) / (age_hours + 2) ^ 1.8

baseline میانه‌ی سرعت همان منبع در همین اجراست، پس هیچ عدد جادویی دستی در کد
نیست — هر منبع خودش کالیبره می‌شود و با تغییر محبوبیتش هم جابه‌جا می‌شود.
"""

import logging
import math
import statistics
from collections import defaultdict
from datetime import datetime, timezone

from .models import Item

log = logging.getLogger(__name__)

# نمای زوال زمانی. همان مقدار پیش‌فرض Hacker News.
GRAVITY = 1.8

# جلوی تقسیم بر صفر و انفجار امتیاز پست‌های چندثانیه‌ای را می‌گیرد.
VELOCITY_OFFSET_HOURS = 0.5

# وزن‌های تعامل. بازنشر و نقل‌قول یعنی کسی محتوا را به مخاطب خودش رسانده،
# که سیگنال ویروسی‌شدن است؛ لایک فقط مصرف منفعل است.
WEIGHTS = {
    "like": 1,
    "repost": 3,
    "quote": 3,
    "reply": 2,
    "point": 1,
    "comment": 2,
}


def weigh(**signals: int) -> int:
    """مثلاً weigh(like=100, repost=20) → 160"""
    return sum(WEIGHTS[name] * (count or 0) for name, count in signals.items())


def rank(items: list[Item], now: datetime | None = None) -> list[Item]:
    """به هر آیتم امتیاز می‌دهد و از پرامتیاز به کم‌امتیاز مرتب برمی‌گرداند."""
    now = now or datetime.now(timezone.utc)
    baselines = _baselines(items, now)

    for item in items:
        if item.ranked:
            _score_viral(item, baselines.get(item.label, 0.0), now)
        else:
            _score_editorial(item, now)

    return sorted(items, key=lambda i: i.score, reverse=True)


def _baselines(items: list[Item], now: datetime) -> dict[str, float]:
    """میانه‌ی سرعت هر منبع — نقطه‌ی مرجعِ «عادی» برای آن منبع."""
    by_source: dict[str, list[float]] = defaultdict(list)
    for item in items:
        if item.ranked:
            by_source[item.label].append(_velocity(item, now))

    return {
        label: statistics.median(speeds)
        for label, speeds in by_source.items()
        if speeds
    }


def _velocity(item: Item, now: datetime) -> float:
    return item.engagement / (item.age_hours(now) + VELOCITY_OFFSET_HOURS)


def _score_viral(item: Item, baseline: float, now: datetime) -> None:
    age = item.age_hours(now)
    velocity = _velocity(item, now)

    quality = math.log10(1 + item.engagement)

    # baseline صفر یعنی کل منبع در این اجرا ساکت بوده؛ آن‌وقت نسبت بی‌معناست
    # و فقط روی کیفیت خام تکیه می‌کنیم.
    ratio = velocity / baseline if baseline > 0 else 0.0
    boost = 1 + math.log10(1 + ratio)

    decay = (age + 2) ** GRAVITY
    item.score = quality * boost / decay
    item.breakdown = {
        "engagement": item.engagement,
        "age_h": round(age, 1),
        "velocity": round(velocity, 1),
        "baseline": round(baseline, 1),
        "boost": round(boost, 2),
    }


def _score_editorial(item: Item, now: datetime) -> None:
    """فیدهای خبری عدد تعاملی ندارند، پس فقط تازگی و اعتبار منبع می‌ماند.

    عمداً روی همان مقیاس viral نرمال نمی‌شود — این دو در استخرهای جدا رقابت
    می‌کنند (به select در main نگاه کنید). ساختن یک عدد engagement قلابی برای
    این‌ها فقط رتبه‌بندی را دروغین می‌کرد.
    """
    age = item.age_hours(now)
    item.score = item.authority / (age + 2) ** GRAVITY
    item.breakdown = {"authority": item.authority, "age_h": round(age, 1)}
