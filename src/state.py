"""نگهداری لیست آیتم‌های دیده‌شده در یک فایل JSON."""

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

        if not self.is_first_run:
            data = json.loads(path.read_text(encoding="utf-8"))
            self.seen = data.get("seen", [])
            self._seen_set = set(self.seen)

    def has(self, uid: str) -> bool:
        return uid in self._seen_set

    def mark(self, uid: str) -> None:
        if uid in self._seen_set:
            return
        self._seen_set.add(uid)
        self.seen.append(uid)

    def save(self) -> None:
        self.seen = self.seen[-MAX_SEEN:]
        self._seen_set = set(self.seen)
        self.path.write_text(
            json.dumps({"seen": self.seen}, ensure_ascii=False, indent=1),
            encoding="utf-8",
        )
