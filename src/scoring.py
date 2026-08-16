"""رتبه‌بندی آیتم‌ها بر اساس سرعت رشد تعامل.

چهار ایده روی هم:

  1. Hacker News — score = (P-1) / (T+2)^1.8
     زوال زمانی با نمای gravity. چون نمای زمان بزرگ‌تر از نمای امتیاز است،
     هیچ آیتمی برای همیشه بالا نمی‌ماند.

  2. Reddit hot — log10(votes)
     بازده نزولی: ده رأی اول به‌اندازه‌ی صد رأی بعدی ارزش دارد. جلوی این را
     می‌گیرد که یک پست با ۵۰۰۰ لایک تا ابد همه‌چیز را خفه کند.

  3. velocity اندازه‌گیری‌شده، نه میانگین عمر
     پژوهش viral detection می‌گوید نرخ *تغییر* تعامل مهم است. تقسیم تعامل کل
     بر سن، میانگین از لحظه‌ی انتشار می‌دهد و پستی که سه ساعت پیش منفجر شده و
     حالا مرده را با پستی که همین حالا در حال رشد است یکسان می‌بیند. پس تعامل
     هر آیتم بین اجراها ذخیره می‌شود و رشد واقعی بین دو نقطه حساب می‌شود.

  4. نرمال‌سازی نسبت به baseline همان منبع
     یک اکانت با ۵۰۰ فالوور که ۵۰ لایک می‌گیرد داغ‌تر از اکانتی است با ۵۰۰هزار
     فالوور که ۲۰۰ لایک می‌گیرد.

فرمول:

    quality  = log10(1 + engagement)
    velocity = رشد بین دو اجرا، یا اگر سابقه نداریم engagement/(age+0.5)
    ratio    = velocity / baseline_of_source
    score    = quality × (1 + log10(1 + ratio)) / (age_hours + 2) ^ 1.8

baseline میانه‌ی سرعت همان منبع در همین اجراست، پس هیچ عدد جادویی دستی در کد
نیست — هر منبع خودش کالیبره می‌شود.
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

# فاصله‌ی کمتر از این بین دو اجرا برای سنجش رشد قابل اتکا نیست: نویز شمارش
# تلگرام و تأخیر cron از خودِ رشد بزرگ‌تر می‌شود.
MIN_SNAPSHOT_GAP_HOURS = 0.25

# اگر آخرین پست از همین منبع بود، امتیازش را ضربدر این کن. کانالی که پنج پست
# پشت هم از یک منبع می‌گذارد یکنواخت به نظر می‌رسد.
REPEAT_PENALTY = 0.55

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


def rank(items: list[Item], state=None, now: datetime | None = None) -> list[Item]:
    """به هر آیتم امتیاز می‌دهد و از پرامتیاز به کم‌امتیاز مرتب برمی‌گرداند."""
    now = now or datetime.now(timezone.utc)

    speeds = {item.uid: _velocity(item, state, now) for item in items if item.ranked}
    baselines = _baselines(items, speeds)
    recent = list(getattr(state, "recent_labels", []) or [])

    for item in items:
        if item.ranked:
            _score_viral(item, speeds[item.uid], baselines.get(item.label, 0.0), now)
        else:
            _score_editorial(item, now)
        _apply_diversity(item, recent)

    if state is not None:
        for item in items:
            if item.ranked:
                state.record(item.uid, item.engagement, now)

    return sorted(items, key=lambda i: i.score, reverse=True)


def _baselines(items: list[Item], speeds: dict[str, float]) -> dict[str, float]:
    """میانه‌ی سرعت هر منبع — نقطه‌ی مرجعِ «عادی» برای آن منبع."""
    by_source: dict[str, list[float]] = defaultdict(list)
    for item in items:
        if item.ranked:
            by_source[item.label].append(speeds[item.uid])

    return {
        label: statistics.median(values)
        for label, values in by_source.items()
        if values
    }


def _velocity(item: Item, state, now: datetime) -> float:
    """رشد تعامل در ساعت.

    اگر این آیتم را در اجرای قبل دیده باشیم، رشد واقعی بین آن لحظه و حالا را
    می‌دهد. بدون سابقه، به میانگین از لحظه‌ی انتشار برمی‌گردیم.
    """
    previous = state.previous(item.uid) if state is not None else None
    if previous:
        before, when = previous
        gap = (now - when).total_seconds() / 3600
        if gap >= MIN_SNAPSHOT_GAP_HOURS:
            growth = item.engagement - before
            # شمارنده‌ها گاهی پایین می‌آیند (حذف لایک). منفی یعنی مرده، نه منفی.
            item.breakdown["growth"] = growth
            return max(growth, 0) / gap

    return item.engagement / (item.age_hours(now) + VELOCITY_OFFSET_HOURS)


def _score_viral(item: Item, velocity: float, baseline: float, now: datetime) -> None:
    age = item.age_hours(now)
    quality = math.log10(1 + item.engagement)

    # baseline صفر یعنی کل منبع در این اجرا ساکت بوده؛ آن‌وقت نسبت بی‌معناست
    # و فقط روی کیفیت خام تکیه می‌کنیم.
    ratio = velocity / baseline if baseline > 0 else 0.0
    boost = 1 + math.log10(1 + ratio)

    item.score = quality * boost / (age + 2) ** GRAVITY
    item.breakdown.update(
        {
            "engagement": item.engagement,
            "age_h": round(age, 1),
            "velocity": round(velocity, 1),
            "baseline": round(baseline, 1),
            "boost": round(boost, 2),
        }
    )


def _score_editorial(item: Item, now: datetime) -> None:
    """فیدهای خبری عدد تعاملی ندارند، پس فقط تازگی و اعتبار منبع می‌ماند.

    عمداً روی همان مقیاس viral نرمال نمی‌شود — این دو در استخرهای جدا رقابت
    می‌کنند. ساختن یک عدد engagement قلابی برای این‌ها رتبه‌بندی را دروغین
    می‌کرد.
    """
    age = item.age_hours(now)
    item.score = item.authority / (age + 2) ** GRAVITY
    item.breakdown.update({"authority": item.authority, "age_h": round(age, 1)})


def _apply_diversity(item: Item, recent: list[str]) -> None:
    """هرچه این منبع در پست‌های اخیر بیشتر آمده، امتیازش را بیشتر کم کن."""
    if not recent:
        return
    repeats = recent.count(item.label)
    if not repeats:
        return
    penalty = REPEAT_PENALTY**repeats
    item.score *= penalty
    item.breakdown["diversity"] = round(penalty, 2)
