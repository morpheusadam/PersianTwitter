"""حافظه‌ی بین اجراها: چه چیزی پست شده، سهم هر استخر، و رشد تعامل آیتم‌ها."""

import json
from datetime import datetime, timedelta, timezone
from pathlib import Path

# فقط این تعداد uid آخر را نگه می‌داریم تا فایل بی‌نهایت رشد نکند. حالا که هر
# پست کل خوشه‌اش را بایگانی می‌کند (نه فقط خودش را)، این عدد چند برابر سریع‌تر
# پر می‌شود، پس سقف بالاتر رفت تا پنجره‌ی seen از پنجره‌ی حافظه‌ی ماجرا کوتاه‌تر
# نشود.
MAX_SEEN = 3000

# عکس‌های تعامل. هر اجرا حدود صد آیتم رتبه‌دار می‌بیند و هر آیتم بعد از خروج از
# پنجره‌ی سنی بی‌فایده می‌شود، پس این سقف چند اجرا تاریخچه می‌دهد.
MAX_SNAPSHOTS = 600

# برای جلوگیری از پشت‌سرهم آمدن یک منبع، فقط همین تعداد پست آخر مهم است.
RECENT_LABELS = 5

# امضای ماجراهای پست‌شده. سقفش سخاوتمند است چون هر ردیف فقط چند ده توکن است.
MAX_STORIES = 300

# چقدر یک ماجرا در حافظه بماند. پوشش چندرسانه‌ای یک خبر بزرگ تا دو روز کش
# می‌آید — رسانه‌های روسی و آسیایی معمولاً یک روز دیرتر می‌نویسند — پس پنجره
# باید از خودِ موج پوشش بلندتر باشد وگرنه دنباله‌اش رد می‌شود.
STORY_MEMORY_HOURS = 72


class State:
    def __init__(self, path: Path):
        self.path = path
        self.is_first_run = not path.exists()
        self.seen: list[str] = []
        self._seen_set: set[str] = set()
        # سهم ۵۰/۵۰ بین دو استخر وقتی هر اجرا فقط یک پست می‌فرستد داخل خودِ اجرا
        # قابل اعمال نیست، پس در طول زمان نگه داشته می‌شود.
        self.posted: dict[str, int] = {"viral": 0, "editorial": 0}
        # uid → [engagement, timestamp]. برای سنجش رشد بین دو اجرا.
        self.snapshots: dict[str, list] = {}
        # برچسب منابع آخرین پست‌ها، از قدیم به جدید.
        self.recent_labels: list[str] = []
        # پست‌های هفته، برای ساختن خلاصه‌ی هفتگی.
        self.history: list[dict] = []
        # تاریخ آخرین خلاصه‌ی هفتگی، تا دو بار در یک روز نرود.
        self.last_digest: str = ""
        # امضای ماجراهایی که پست شده‌اند، جدا از uid مقاله‌ها.
        self.stories: list[dict] = []

        if not self.is_first_run:
            data = json.loads(path.read_text(encoding="utf-8"))
            self.seen = data.get("seen", [])
            self._seen_set = set(self.seen)
            self.posted.update(data.get("posted", {}))
            self.snapshots = data.get("snapshots", {})
            self.recent_labels = data.get("recent_labels", [])
            self.history = data.get("history", [])
            self.last_digest = data.get("last_digest", "")
            self.stories = data.get("stories", [])

    def has(self, uid: str) -> bool:
        return uid in self._seen_set

    def mark(self, uid: str) -> None:
        if uid in self._seen_set:
            return
        self._seen_set.add(uid)
        self.seen.append(uid)

    def count_post(self, pool: str, label: str) -> None:
        self.posted[pool] = self.posted.get(pool, 0) + 1
        self.recent_labels.append(label)
        self.recent_labels = self.recent_labels[-RECENT_LABELS:]

    def remember_story(self, signature: set[str], now: datetime) -> None:
        """امضای ماجرایی که پست شد.

        `seen` فقط URL مقاله را می‌گیرد، و این برای خبری که ده رسانه پوشش
        می‌دهند کافی نیست: نسخه‌ی رسانه‌ی بعدی URL دیگری دارد و از فیلتر رد
        می‌شود. امضای ماجرا همان چیزی است که همه‌ی آن نسخه‌ها در آن مشترک‌اند.
        """
        if not signature:
            return
        self.stories.append({"sig": sorted(signature), "at": now.isoformat()})

    def recent_stories(self, now: datetime, hours: int = STORY_MEMORY_HOURS) -> list[set[str]]:
        """ماجراهایی که در پنجره‌ی حافظه پست شده‌اند."""
        cutoff = now - timedelta(hours=hours)
        live = []
        for row in self.stories:
            at = _parse(row.get("at"))
            if at and at >= cutoff:
                live.append(set(row.get("sig", [])))
        return live

    def remember(self, item, persian: str, now: datetime) -> None:
        """پست را برای خلاصه‌ی هفتگی نگه می‌دارد."""
        self.history.append(
            {
                "title": persian.split(". ")[0].strip().rstrip("."),
                "url": item.url,
                "source": item.publisher or item.label,
                "score": round(item.score, 5),
                "at": now.isoformat(),
            }
        )

    def week(self, now: datetime, days: int = 7) -> list[dict]:
        """پست‌های این هفته، و هرچه قدیمی‌تر بود را دور می‌ریزد."""
        cutoff = now - timedelta(days=days)
        self.history = [
            row
            for row in self.history
            if _parse(row.get("at")) and _parse(row["at"]) >= cutoff
        ]
        return self.history

    def previous(self, uid: str) -> tuple[int, datetime] | None:
        """تعامل و زمانِ آخرین باری که این آیتم را دیدیم."""
        row = self.snapshots.get(uid)
        if not row:
            return None
        try:
            return int(row[0]), datetime.fromisoformat(row[1])
        except (ValueError, IndexError, TypeError):
            return None

    def record(self, uid: str, engagement: int, now: datetime) -> None:
        self.snapshots[uid] = [engagement, now.isoformat()]

    def save(self) -> None:
        self.seen = self.seen[-MAX_SEEN:]
        self._seen_set = set(self.seen)
        self.stories = self.stories[-MAX_STORIES:]

        # قدیمی‌ترین عکس‌ها اول حذف می‌شوند؛ آن‌ها همان‌هایی هستند که از پنجره‌ی
        # سنی خارج شده‌اند و دیگر امتیاز نمی‌گیرند.
        if len(self.snapshots) > MAX_SNAPSHOTS:
            ordered = sorted(self.snapshots.items(), key=lambda kv: kv[1][1])
            self.snapshots = dict(ordered[-MAX_SNAPSHOTS:])

        self.path.write_text(
            json.dumps(
                {
                    "seen": self.seen,
                    "posted": self.posted,
                    "recent_labels": self.recent_labels,
                    "last_digest": self.last_digest,
                    "history": self.history,
                    "stories": self.stories,
                    "snapshots": self.snapshots,
                },
                ensure_ascii=False,
                indent=1,
            ),
            encoding="utf-8",
        )


def _parse(value) -> datetime | None:
    try:
        return datetime.fromisoformat(value)
    except (TypeError, ValueError):
        return None


def now_utc() -> datetime:
    return datetime.now(timezone.utc)
