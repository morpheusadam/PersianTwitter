"""نگهداری آیتم‌های دیده‌شده و شمارش سهم استخرها، در یک فایل JSON."""

import json
from pathlib import Path

# فقط این تعداد uid آخر را نگه می‌داریم تا فایل بی‌نهایت رشد نکند.
MAX_SEEN = 800


class State:
    def __init__(self, path: Path):
        self.path = path
        self.is_first_run = not path.exists()
        self.seen: list[str] = []
        self._seen_set: set[str] = set()
        # سهم ۶۰/۴۰ بین دو استخر وقتی هر اجرا فقط یک پست می‌فرستد داخل خودِ اجرا
        # قابل اعمال نیست، پس در طول زمان نگه داشته می‌شود.
        self.posted: dict[str, int] = {"viral": 0, "editorial": 0}

        if not self.is_first_run:
            data = json.loads(path.read_text(encoding="utf-8"))
            self.seen = data.get("seen", [])
            self._seen_set = set(self.seen)
            self.posted.update(data.get("posted", {}))

    def has(self, uid: str) -> bool:
        return uid in self._seen_set

    def mark(self, uid: str) -> None:
        if uid in self._seen_set:
            return
        self._seen_set.add(uid)
        self.seen.append(uid)

    def count_post(self, pool: str) -> None:
        self.posted[pool] = self.posted.get(pool, 0) + 1

    def save(self) -> None:
        self.seen = self.seen[-MAX_SEEN:]
        self._seen_set = set(self.seen)
        self.path.write_text(
            json.dumps(
                {"seen": self.seen, "posted": self.posted},
                ensure_ascii=False,
                indent=1,
            ),
            encoding="utf-8",
        )
