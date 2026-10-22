"""Full-screen overlay to drag a box around the Roblox menu.

Shows a frozen screenshot of the chosen display; the box is converted to
that screenshot's real pixels, so Windows display scaling doesn't matter.
"""
from PIL import Image
from PySide6.QtCore import QRect, Qt, Signal
from PySide6.QtGui import QColor, QFont, QGuiApplication, QImage, QPainter, QPen, QPixmap
from PySide6.QtWidgets import QWidget


def grab_monitor(index: int):
    import mss
    with mss.mss() as sct:
        mons = sct.monitors
        mon = mons[index] if index < len(mons) else mons[1]
        shot = sct.grab(mon)
        return Image.frombytes("RGB", shot.size, shot.rgb), mon


def qt_screen_for(mon: dict):
    """The QScreen showing this mss monitor (match physical size, then x order)."""
    screens = QGuiApplication.screens()
    def phys(s):
        g, r = s.geometry(), s.devicePixelRatio()
        return round(g.width() * r), round(g.height() * r)
    same = [s for s in screens if phys(s) == (mon["width"], mon["height"])] or screens
    same.sort(key=lambda s: abs(s.geometry().x() * s.devicePixelRatio() - mon["left"]))
    return same[0]


class RegionSelector(QWidget):
    """Emits picked({"left","top","width","height"}) in monitor pixels, or cancelled()."""
    picked = Signal(dict)
    cancelled = Signal()

    def __init__(self, monitor_index: int, current=None):
        super().__init__(None, Qt.FramelessWindowHint | Qt.WindowStaysOnTopHint | Qt.Tool)
        self.setAttribute(Qt.WA_DeleteOnClose)
        self.setCursor(Qt.CrossCursor)
        self.img, self.mon = grab_monitor(monitor_index)
        data = self.img.tobytes("raw", "RGB")
        qimg = QImage(data, self.img.width, self.img.height, 3 * self.img.width,
                      QImage.Format_RGB888).copy()
        self.pix = QPixmap.fromImage(qimg)
        self.start = self.end = None
        self.current = current         # existing box (image pixels), shown until you drag
        screen = qt_screen_for(self.mon)
        self.setGeometry(screen.geometry())

    def _scale(self):
        w, h = max(1, self.width()), max(1, self.height())
        return self.img.width / w, self.img.height / h

    def showEvent(self, e):
        self.showFullScreen()
        self.raise_()
        self.activateWindow()
        self.setFocus()
        try:
            self.grabKeyboard()          # so Esc works even if Windows won't focus us
        except Exception:
            pass

    def closeEvent(self, e):
        try:
            self.releaseKeyboard()
        except Exception:
            pass
        super().closeEvent(e)

    def mousePressEvent(self, e):
        if e.button() == Qt.LeftButton:
            self.current = None
            self.start = self.end = e.position().toPoint()
            self.update()
        elif e.button() == Qt.RightButton:
            self._cancel()

    def mouseMoveEvent(self, e):
        if self.start is not None and e.buttons() & Qt.LeftButton:
            self.end = e.position().toPoint()
            self.update()

    def mouseReleaseEvent(self, e):
        if e.button() == Qt.LeftButton and self.start is not None:
            self.end = e.position().toPoint()
            self.update()
            self._confirm()              # letting go saves; a plain click is ignored

    def keyPressEvent(self, e):
        if e.key() == Qt.Key_Escape:
            self._cancel()
        elif e.key() in (Qt.Key_Return, Qt.Key_Enter):
            self._confirm()

    def mouseDoubleClickEvent(self, e):
        self._confirm()

    def _rect(self):
        if self.start is None and self.current:     # convert at today's size
            sx, sy = self._scale()
            c = self.current
            return QRect(int(c["left"] / sx), int(c["top"] / sy),
                         int(c["width"] / sx), int(c["height"] / sy))
        if self.start is None or self.end is None:
            return None
        return QRect(self.start, self.end).normalized()

    def _confirm(self):
        r = self._rect()
        if r is None or r.width() < 20 or r.height() < 20:
            self.start = self.end = None
            self.update()
            return
        sx, sy = self._scale()
        box = {"left": int(r.left() * sx), "top": int(r.top() * sy),
               "width": int(r.width() * sx), "height": int(r.height() * sy)}
        self.picked.emit(box)
        self.close()

    def _cancel(self):
        self.cancelled.emit()
        self.close()

    def paintEvent(self, e):
        p = QPainter(self)
        p.drawPixmap(self.rect(), self.pix)
        p.fillRect(self.rect(), QColor(0, 0, 0, 140))
        r = self._rect()
        if r is not None and r.width() > 0 and r.height() > 0:
            sx, sy = self._scale()
            src = QRect(int(r.left() * sx), int(r.top() * sy),
                        int(r.width() * sx), int(r.height() * sy))
            p.drawPixmap(r, self.pix, src)            # undimmed inside the box
            p.setPen(QPen(QColor(90, 170, 255), 2))
            p.drawRect(r)
            p.setFont(QFont("Segoe UI", 10))
            p.setPen(QColor(255, 255, 255))
            p.drawText(r.left(), max(16, r.top() - 6),
                       f"{src.width()} × {src.height()}")
        # instructions
        p.setFont(QFont("Segoe UI", 12, QFont.DemiBold))
        msg = ("Drag a box around the Roblox menu's player list (a little extra is fine)\n"
               "It saves when you let go   ·   Esc or right-click = cancel")
        fm = p.fontMetrics()
        tw = max(fm.horizontalAdvance(l) for l in msg.splitlines()) + 40
        th = fm.height() * 2 + 20
        box = QRect((self.width() - tw) // 2, 24, tw, th)
        p.fillRect(box, QColor(20, 22, 28, 230))
        p.setPen(QColor(235, 238, 245))
        p.drawText(box, Qt.AlignCenter, msg)
        p.end()
