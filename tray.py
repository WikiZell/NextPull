"""System tray icon for NextPull (pystray + Pillow). Optional: without them the app simply has no tray and closing the window quits."""
from __future__ import annotations

from typing import Any

try:
    import pystray
    from PIL import Image, ImageDraw
    HAVE_TRAY = True
except Exception:   # pragma: no cover - missing optional packages
    HAVE_TRAY = False


def make_icon(size: int = 64) -> "Image.Image":
    """A blue rounded square with a white download arrow, drawn in code so there is no image asset to ship."""
    image = Image.new("RGBA", (size, size), (0, 0, 0, 0))
    draw = ImageDraw.Draw(image)
    draw.rounded_rectangle((2, 2, size - 3, size - 3), radius=size // 5, fill=(50, 101, 206, 255))
    mid, top, tip, base = size // 2, size * 0.2, size * 0.66, size * 0.8
    draw.rectangle((mid - size * 0.07, top, mid + size * 0.07, tip - size * 0.12), fill="white")
    draw.polygon([(mid - size * 0.22, tip - size * 0.2), (mid + size * 0.22, tip - size * 0.2), (mid, tip + size * 0.05)], fill="white")
    draw.rectangle((size * 0.24, base - size * 0.06, size * 0.76, base), fill="white")
    return image


class Tray:
    def __init__(self, api: Any) -> None:
        self.api = api
        self.available = HAVE_TRAY
        self._icon: Any = None

    def start(self) -> None:
        if not self.available:
            return
        menu = pystray.Menu(
            pystray.MenuItem("Open NextPull", lambda *_: self.api.show_window(), default=True),
            pystray.MenuItem(lambda _item: self._status_text(), None, enabled=False),
            pystray.Menu.SEPARATOR,
            pystray.MenuItem("Quit", lambda *_: self.api.quit_app()),
        )
        self._icon = pystray.Icon("NextPull", make_icon(), "NextPull", menu)
        self._icon.run_detached()

    def _status_text(self) -> str:
        try:
            status = self.api._engine.status()
        except Exception:
            return "NextPull"
        if status["running"] and status["current"]:
            return f"Downloading: {status['current']['job_name']}"
        upcoming = sorted(job["next_run"] for job in status["jobs"] if job["next_run"])
        return f"Next run: {upcoming[0].replace('T', ' ')[:16]}" if upcoming else "No scheduled jobs"

    def notify(self, title: str, message: str) -> None:
        if self._icon is not None:
            try:
                self._icon.notify(message[:240], title[:60])
            except Exception:
                pass

    def stop(self) -> None:
        if self._icon is not None:
            try:
                self._icon.stop()
            except Exception:
                pass
