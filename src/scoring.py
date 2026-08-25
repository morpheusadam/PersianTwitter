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

from . import cluster
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

# باند نرمِ زیر آستانه‌ی خوشه‌بندی. هرچه از `cluster.SIMILARITY` رد شود در
# select کاملاً حذف می‌شود و اصلاً به رتبه‌بندی نمی‌رسد؛ ولی خبری که *شبیه*
# ماجرای تازه‌پست‌شده است بدون اینکه قطعاً همان باشد — مثلاً خبر بعدی از همان
# رویداد — نباید حذف شود، فقط نباید جلوی بقیه را هم بگیرد.
ECHO_FLOOR = 0.28
ECHO_PENALTY = 0.25

# «چند رسانه‌ی مستقل نوشته‌اند» در برابر «چقدر تعامل گرفته».
#
# اولین نسخه log2 بود و خیلی کند رشد می‌کرد: یک منبع ۰.۸ می‌گرفت و سه منبع
# ۱.۶، یعنی فقط دو برابر، در حالی که یک بحث داغ HN به‌تنهایی heat=3.1 می‌گرفت.
# نتیجه این بود که خوشه‌های چندمنبعی هیچ‌وقت برنده نمی‌شدند و کل خوشه‌بندی
# بی‌اثر می‌ماند. نمای ۰.۸ زیرخطی است، پس هنوز بازده نزولی دارد، ولی فاصله‌ی
# یک‌منبعی تا شش‌منبعی را واقعی می‌کند:
#
#   ۱ منبع  → 1.2      ۳ منبع → 2.9      ۶ منبع → 5.0
# پاداش به ازای هر رسانه‌ی *اضافه*، نه به ازای تعداد کل.
#
# نسخه‌های قبلی روی خود `sources` کار می‌کردند و مشکلشان این بود که پایه را هم
# بالا می‌بردند: وقتی یک‌منبعی ۱.۷۶ می‌گیرد، سه‌منبعی باید خیلی بالاتر برود تا
# اختلاف سن را جبران کند. اینجا پایه ثابت است و فقط منبع دوم به بعد پاداش
# می‌گیرند، پس اختلاف واقعی می‌شود:
#
#   ۱ منبع → 0.80      ۳ منبع → 3.05      ۶ منبع → 5.90   (با authority=0.8)
CORROBORATION_BONUS = 1.5
CORROBORATION_EXPONENT = 0.9

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
    """هر ماجرا را امتیاز می‌دهد و نماینده‌هایش را مرتب برمی‌گرداند.

    ورودی، خبرهای خام است. خروجی، یک آیتم به ازای هر ماجرا — چون وقتی هفت سایت
    یک خبر را نوشته‌اند، آن هفت‌تا یک چیزند و باید یک بار پست شوند، نه هفت بار.
    """
    now = now or datetime.now(timezone.utc)

    speeds = {item.uid: _velocity(item, state, now) for item in items if item.ranked}
    baselines = _baselines(items, speeds)
    recent = list(getattr(state, "recent_labels", []) or [])
    echoes = state.recent_stories(now) if hasattr(state, "recent_stories") else []

    leads: list[Item] = []
    for story in cluster.build(items):
        lead = story.lead()
        # نماینده باید بداند نماینده‌ی چه کسانی است: وقتی پست شد، همه‌ی این
        # uidها با هم بایگانی می‌شوند.
        lead.members = [i.uid for i in story.items]
        lead.story_signature = set(story.signature)
        if not lead.signature:
            lead.signature = cluster.signature_of(lead.text)
        _score(lead, story, speeds.get(lead.uid), baselines.get(lead.label, 0.0), now)
        _apply_diversity(lead, recent)
        _apply_echo(lead, echoes)
        leads.append(lead)

    if state is not None:
        for item in items:
            if item.ranked:
                state.record(item.uid, item.engagement, now)

    return sorted(leads, key=lambda i: i.score, reverse=True)


def _score(lead: Item, story, velocity, baseline: float, now: datetime) -> None:
    """امتیاز یک ماجرا از سه سیگنال مستقل ساخته می‌شود.

    corroboration — چند رسانه‌ی مستقل نوشته‌اند، وزن‌دار با اعتبارشان. این همان
                    چیزی است که «مهم» را از «فقط منتشر شده» جدا می‌کند.
    heat          — تعامل واقعی، آنجا که عددی در کار است (HN، Lobsters، Bluesky).
    pickup        — سرعت پخش: چند رسانه در چند ساعت. این «جنجالی» را می‌سنجد،
                    چون خبری که در دو ساعت به پنج سایت رسیده با خبری که در دو
                    روز به پنج سایت رسیده یکی نیست.

    و در آخر همان زوال زمانی Hacker News، تا هیچ ماجرایی برای همیشه بالا نماند.
    """
    # دو زمان متفاوت، و همین تفاوت کل مسئله را حل می‌کند.
    #
    # پوشش چندرسانه‌ای ذاتاً چند ساعت طول می‌کشد. اگر زوال را از لحظه‌ی شکستن
    # ماجرا حساب کنیم، تا وقتی سومین رسانه بنویسد (age+2)^1.8 امتیاز را صدها
    # برابر کوچک کرده و خوشه‌ی چندمنبعی هیچ‌وقت برنده نمی‌شود — دقیقاً همان
    # چیزی که در تست دیده شد: corrob بالا ولی رتبه‌ی ۲۰۰.
    #
    # پس زوال از آخرین پوشش حساب می‌شود: ماجرایی که رسانه‌ها هنوز دارند رویش
    # می‌نویسند زنده است. و span، یعنی فاصله‌ی اولین تا آخرین پوشش، سرعت پخش
    # را می‌دهد؛ پنج رسانه در دو ساعت جنجالی است، پنج رسانه در دو روز نه.
    age = max((now - story.latest()).total_seconds() / 3600, 0.05)
    span = max((story.latest() - story.broke()).total_seconds() / 3600, 0.0)
    sources = story.sources

    extra = max(sources - 1, 0) ** CORROBORATION_EXPONENT
    corroboration = story.authority * (1 + CORROBORATION_BONUS * extra)

    heat = 0.0
    if lead.ranked or story.engagement:
        speed = velocity if velocity is not None else 0.0
        ratio = speed / baseline if baseline > 0 else 0.0
        heat = math.log10(1 + story.engagement) * (1 + math.log10(1 + ratio))

    pickup = sources / (span + 1)

    lead.score = (corroboration + heat) * (1 + math.log10(1 + pickup)) / (age + 2) ** GRAVITY
    lead.breakdown.update(
        {
            "sources": sources,
            "age_h": round(age, 1),
            "span_h": round(span, 1),
            "corrob": round(corroboration, 2),
            "heat": round(heat, 2),
            "pickup": round(pickup, 2),
        }
    )


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


def _apply_echo(item: Item, echoes: list[set[str]]) -> None:
    """ماجراهایی که تازه پست شده‌اند را ته صف می‌فرستد.

    فیلتر قطعی در `main.select` است و هرچه از آستانه رد شود اصلاً به اینجا
    نمی‌رسد. این باند نرمِ زیر آستانه است: خبر مشکوک حذف نمی‌شود ولی باید از
    یک ماجرای واقعاً تازه عقب بیفتد.
    """
    if not echoes or not item.signature:
        return
    score = cluster.best_match(item.signature, echoes)
    if score < ECHO_FLOOR:
        return
    item.score *= ECHO_PENALTY
    item.breakdown["echo"] = round(score, 2)


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
