from dataclasses import dataclass, field
from datetime import datetime, timezone


@dataclass
class Item:
    """یک آیتم از هر منبعی، بعد از نرمال‌سازی."""

    uid: str  # کلید یکتا برای dedup

    # نام منبع در config. کلید رتبه‌بندی هم هست: baseline سرعت بر اساس همین
    # محاسبه می‌شود، پس نباید به ازای هر خبر عوض شود.
    label: str

    text: str  # متن اصلی، انگلیسی
    url: str
    published: datetime  # همیشه timezone-aware

    # ناشر واقعی خبر، وقتی با منبع فرق دارد. Hacker News و Lobsters فقط لینک
    # جمع می‌کنند؛ خودِ خبر مال جایی مثل wptv.com است و سرتیتر پست باید آن را
    # بگوید، وگرنه انگار HN خبر را نوشته.
    publisher: str | None = None

    # منابعی مثل Bluesky و HN عدد تعامل می‌دهند و امتیاز ویروسی می‌گیرند.
    # فیدهای خبری هیچ عددی ندارند و در استخر editorial رتبه‌بندی می‌شوند.
    engagement: int = 0
    ranked: bool = False

    # اعتبار منبع، فقط برای استخر editorial. از config می‌آید.
    authority: float = 0.5

    # پنجره‌ی سنی اختصاصی. منابع حقوق دیجیتال هر چند روز یک بار می‌نویسند و با
    # سقف عمومی هیچ‌وقت نوبت نمی‌گیرند، ولی خبرشان هفته‌ی بعد هم تازه است.
    max_age_hours: float | None = None

    image_url: str | None = None
    # صفحه‌ی بحث، جدا از خود مقاله. HN و Lobsters هر دو را دارند.
    discussion_url: str | None = None
    score: float = 0.0
    breakdown: dict = field(default_factory=dict)

    # امضای خودِ این مقاله: توکن‌های لاتینی که آن را به هم‌نوعانش وصل می‌کند.
    # هرگز با امضای خوشه جایگزین نمی‌شود — مقایسه‌ی «آیا این خبر همان ماجرای
    # پست‌شده است؟» باید با امضای کوچکِ خودِ مقاله انجام شود، نه با اجتماع
    # بزرگِ خوشه، وگرنه مخرجِ شباهت بزرگ می‌شود و تطبیق از دست می‌رود.
    signature: set[str] = field(default_factory=set)
    # اجتماع امضای همه‌ی رسانه‌هایی که این ماجرا را پوشش داده‌اند. همین در
    # state ذخیره می‌شود، چون هرچه غنی‌تر باشد نسخه‌های بعدی را بهتر می‌گیرد.
    story_signature: set[str] = field(default_factory=set)
    # uid همه‌ی نسخه‌های دیگر همین ماجرا. وقتی این آیتم پست شد، آن‌ها هم باید
    # بایگانی شوند وگرنه اجرای بعدی همان خبر را از رسانه‌ی بعدی می‌فرستد.
    members: list[str] = field(default_factory=list)

    def age_hours(self, now: datetime | None = None) -> float:
        now = now or datetime.now(timezone.utc)
        return max((now - self.published).total_seconds() / 3600, 0.05)
