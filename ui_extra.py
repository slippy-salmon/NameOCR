"""Small UI pieces: info tips, labelled sliders, and the How it works chart."""
from PySide6.QtCore import QPoint, QRectF, QSize, Qt, Signal
from PySide6.QtGui import QColor, QFont, QFontMetrics, QPainter, QPainterPath, QPen
from PySide6.QtWidgets import (QDialog, QHBoxLayout, QLabel, QPushButton,
                               QScrollArea, QSlider, QToolTip, QVBoxLayout, QWidget)


# ------------------------------------------------------------------ helpers
class InfoIcon(QLabel):
    """A small ⓘ that shows its explanation as soon as the mouse is over it
    (or when clicked), instead of waiting for the usual tooltip delay."""

    def __init__(self, text: str):
        super().__init__("ⓘ")
        self.tip = f"<qt>{text}</qt>"
        self.setStyleSheet("color:#7d8fa8; font-size:14px; padding:0 2px;")
        self.setCursor(Qt.ArrowCursor)
        self.setMouseTracking(True)

    def _show(self):
        QToolTip.showText(self.mapToGlobal(QPoint(self.width() + 4, self.height() // 2)),
                          self.tip, self)

    def enterEvent(self, e):
        self.setStyleSheet("color:#aac4ea; font-size:14px; padding:0 2px;")
        self._show()

    def leaveEvent(self, e):
        self.setStyleSheet("color:#7d8fa8; font-size:14px; padding:0 2px;")
        QToolTip.hideText()

    def mousePressEvent(self, e):
        self._show()


def info(text: str) -> QLabel:
    return InfoIcon(text)


def with_info(widget, text: str) -> QWidget:
    w = QWidget()
    h = QHBoxLayout(w)
    h.setContentsMargins(0, 0, 0, 0)
    h.addWidget(widget, 1)
    if text:
        h.addWidget(info(text), 0, Qt.AlignVCenter)
    return w


class SliderRow(QWidget):
    """Slider with its current value shown beside it."""
    changed = Signal(int)

    def __init__(self, lo, hi, value, fmt="{v}", step=1):
        super().__init__()
        self.fmt, self.step = fmt, step
        h = QHBoxLayout(self)
        h.setContentsMargins(0, 0, 0, 0)
        self.s = QSlider(Qt.Horizontal)
        self.s.setRange(lo // step, hi // step)
        self.s.setPageStep(1)
        self.lab = QLabel()
        self.lab.setMinimumWidth(118)
        h.addWidget(self.s, 1)
        h.addWidget(self.lab)
        self.s.valueChanged.connect(self._moved)
        self.setValue(value)

    def _moved(self, raw):
        v = raw * self.step
        self.lab.setText(self.fmt(v) if callable(self.fmt) else self.fmt.format(v=v))
        self.changed.emit(v)

    def value(self) -> int:
        return self.s.value() * self.step

    def setValue(self, v):
        self.s.setValue(int(round(v / self.step)))
        self._moved(self.s.value())


# ------------------------------------------------------------------ chart
STEPS = [
    ("Capture", "📷",
     "While you scroll the People list, the app screenshots your capture area "
     "about 30 times a second.",
     "Settings › Tuning › Capture rate"),
    ("Find the rows", "▤",
     "Each player row is found by Roblox's exact row colour and the even spacing "
     "between rows, even when the game behind the menu is a similar colour. "
     "Half-visible rows at the top and bottom are skipped.",
     "Row colour match"),
    ("Follow each player", "⇅",
     "Every row gets a fingerprint (its name text, plus the profile picture once "
     "it has loaded), so the same player is recognised in every frame as the list "
     "moves. Scrolling past rows too fast is flagged so you can scroll back.",
     "Tracking strictness"),
    ("Junk filter", "🧹",
     "Anything that isn't the People list is dropped: \"rows\" that show up somewhere "
     "else (like a loading screen) and one-frame flickers.",
     "Junk filter (can be loosened or turned off)"),
    ("Read the names", "🔤",
     "The sharpest image of each @username is read several ways with Tesseract "
     "(different sizes and contrast), and the display name is read too.",
     "Readings per name"),
    ("Check with Roblox", "✔",
     None,       # drawn as its own little flow
     "Display-name match, lookalike spellings, profile pictures"),
    ("Group check", "👥",
     "Each account's groups are fetched once. Players not in the selected group are "
     "marked, and switching the group dropdown re-checks the list instantly.",
     "Main window › Group"),
    ("Your list", "📋",
     "Click a name to leave it out (or put it back), double-click a username in the "
     "table to fix it, then Copy puts the kept names on your clipboard, "
     "space-separated.",
     None),
]

VERIFY_CHAIN = [("Look up every reading", "#2b3f5c"),
                ("Display name matches the row?", "#2b3f5c"),
                ("Try lookalike spellings", "#2b3f5c"),
                ("Compare profile pictures", "#2b3f5c")]
OUTCOMES = [("Verified", "#e6e9f0", "#2a3a2c"), ("Needs a look", "#f5b942", "#3a3220"),
            ("Not in group", "#f07575", "#3a2224"), ("Left out", "#9aa0aa", "#363a43"),
            ("Edited by you", "#7cc4ff", "#1f2d3a")]


class FlowChart(QWidget):
    W = 660

    def __init__(self):
        super().__init__()
        self.body = QFont(self.font().family(), 10)
        self.title = QFont(self.font().family(), 11, QFont.DemiBold)
        self.small = QFont(self.font().family(), 9)
        self.setFixedWidth(self.W)
        self.setFixedHeight(self._layout(dry=True))

    def sizeHint(self):
        return QSize(self.W, self.height())

    def paintEvent(self, e):
        self._layout(dry=False)

    def _text_h(self, font, text, width):
        fm = QFontMetrics(font)
        return fm.boundingRect(0, 0, width, 10000, Qt.TextWordWrap, text).height()

    def _layout(self, dry):
        p = None if dry else QPainter(self)
        if p:
            p.setRenderHint(QPainter.Antialiasing)
            p.fillRect(self.rect(), QColor("#1b1d22"))
        x0, bw = 70, self.W - 90
        tw = bw - 28
        y = 16
        for i, (title, icon, text, tune) in enumerate(STEPS):
            # box height
            h = 14 + QFontMetrics(self.title).height() + 6
            if text:
                h += self._text_h(self.body, text, tw)
            else:
                h += 92 + 40          # verify mini-flow + outcomes
            if tune:
                h += 6 + QFontMetrics(self.small).height()
            h += 12
            if p:
                # number badge + connector
                cx, cy = 34, y + 22
                if i < len(STEPS) - 1:
                    p.setPen(QPen(QColor("#3d4350"), 2))
                    p.drawLine(cx, cy + 16, cx, y + h + 18)
                p.setPen(Qt.NoPen)
                p.setBrush(QColor("#3d6fd6"))
                p.drawEllipse(QRectF(cx - 15, cy - 15, 30, 30))
                p.setPen(QColor("white"))
                p.setFont(self.title)
                p.drawText(QRectF(cx - 15, cy - 15, 30, 30), Qt.AlignCenter, str(i + 1))
                # box
                path = QPainterPath()
                path.addRoundedRect(QRectF(x0, y, bw, h), 10, 10)
                p.fillPath(path, QColor("#232730"))
                p.setBrush(Qt.NoBrush)
                p.setPen(QPen(QColor("#323845"), 1))
                p.drawPath(path)
                # arrow into next box
                if i < len(STEPS) - 1:
                    ax, ay = x0 + bw / 2, y + h
                    p.setPen(QPen(QColor("#5a6475"), 2))
                    p.drawLine(int(ax), int(ay + 2), int(ax), int(ay + 14))
                    p.setBrush(QColor("#5a6475"))
                    tri = QPainterPath()
                    tri.moveTo(ax - 5, ay + 10)
                    tri.lineTo(ax + 5, ay + 10)
                    tri.lineTo(ax, ay + 16)
                    tri.closeSubpath()
                    p.fillPath(tri, QColor("#5a6475"))
                # title
                p.setPen(QColor("#e6e9f0"))
                p.setFont(self.title)
                p.drawText(QRectF(x0 + 14, y + 10, tw, 22), Qt.AlignLeft | Qt.AlignVCenter,
                           f"{icon}  {title}")
            ty = y + 14 + QFontMetrics(self.title).height() + 6
            if text:
                th = self._text_h(self.body, text, tw)
                if p:
                    p.setFont(self.body)
                    p.setPen(QColor("#c9cdd6"))
                    p.drawText(QRectF(x0 + 14, ty, tw, th), Qt.TextWordWrap, text)
                ty += th
            else:
                if p:
                    self._verify_flow(p, x0 + 14, ty, tw)
                ty += 92 + 40
            if tune and p:
                p.setFont(self.small)
                p.setPen(QColor("#7d8fa8"))
                p.drawText(QRectF(x0 + 14, ty + 4, tw, 18), Qt.AlignLeft,
                           f"⚙ Tune: {tune}")
            y += h + 22
        if p:
            p.end()
        return y

    @staticmethod
    def _arrow(p, x1, y1, x2, y2):
        col = QColor("#9aa3b2")
        p.setPen(QPen(col, 2))
        dx, dy = x2 - x1, y2 - y1
        n = max(1.0, (dx * dx + dy * dy) ** 0.5)
        ux, uy = dx / n, dy / n
        p.drawLine(int(x1), int(y1), int(x2 - ux * 5), int(y2 - uy * 5))
        tri = QPainterPath()
        tri.moveTo(x2, y2)
        tri.lineTo(x2 - ux * 7 - uy * 4, y2 - uy * 7 + ux * 4)
        tri.lineTo(x2 - ux * 7 + uy * 4, y2 - uy * 7 - ux * 4)
        tri.closeSubpath()
        p.fillPath(tri, col)

    def _verify_flow(self, p, x, y, w):
        # four steps in a 2x2 snake with arrows, then the outcomes
        cw, ch, gap = (w - 30) / 2, 34, 30
        pos = [(x, y), (x + cw + gap, y), (x + cw + gap, y + ch + 16), (x, y + ch + 16)]
        p.setFont(self.small)
        for k, ((label, col), (bx, by)) in enumerate(zip(VERIFY_CHAIN, pos)):
            path = QPainterPath()
            path.addRoundedRect(QRectF(bx, by, cw, ch), 8, 8)
            p.fillPath(path, QColor(col))
            p.setPen(QColor("#e6e9f0"))
            p.drawText(QRectF(bx + 6, by, cw - 12, ch), Qt.AlignCenter | Qt.TextWordWrap,
                       f"{k + 1}. {label}")
        # 1 -> 2 (right), 2 -> 3 (down), 3 -> 4 (left)
        my, my2 = y + ch / 2, y + ch + 16 + ch / 2
        self._arrow(p, x + cw + 4, my, x + cw + gap - 4, my)
        self._arrow(p, x + cw + gap + cw / 2, y + ch + 1, x + cw + gap + cw / 2, y + ch + 15)
        self._arrow(p, x + cw + gap - 4, my2, x + cw + 4, my2)
        p.setPen(QColor("#9aa3b2"))
        p.drawText(QRectF(x, y + 2 * ch + 22, w, 18), Qt.AlignLeft,
                   "Stops as soon as one account clearly fits. Anything still unsure → Needs a look.")
        # outcome chips
        cx, cy = x, y + 2 * ch + 46
        for label, fg, bg in OUTCOMES:
            tw = QFontMetrics(self.small).horizontalAdvance(label) + 18
            path = QPainterPath()
            path.addRoundedRect(QRectF(cx, cy, tw, 22), 11, 11)
            p.fillPath(path, QColor(bg))
            p.setPen(QColor(fg))
            p.drawText(QRectF(cx, cy, tw, 22), Qt.AlignCenter, label)
            cx += tw + 8


class HowItWorks(QDialog):
    def __init__(self, parent=None):
        super().__init__(parent)
        self.setWindowTitle("How NameOCR works")
        lay = QVBoxLayout(self)
        intro = QLabel("From the moment you press the scan key to the list you copy. "
                       "Each step can be adjusted in Settings › Tuning; hover the ⓘ "
                       "icons there for details.")
        intro.setWordWrap(True)
        intro.setStyleSheet("color:#aab0bb")
        lay.addWidget(intro)
        sc = QScrollArea()
        sc.setWidget(FlowChart())
        sc.setWidgetResizable(False)
        sc.setAlignment(Qt.AlignHCenter)
        sc.setStyleSheet("QScrollArea { border:none; background:#1b1d22; }")
        lay.addWidget(sc, 1)
        close = QPushButton("Close")
        close.clicked.connect(self.accept)
        row = QHBoxLayout()
        row.addStretch(1)
        row.addWidget(close)
        lay.addLayout(row)
        self.resize(FlowChart.W + 50, 760)
