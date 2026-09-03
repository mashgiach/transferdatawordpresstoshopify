"""Small layout helpers shared by the UI pages."""

from __future__ import annotations

from typing import Any, Iterable, Optional

from PyQt5.QtCore import Qt
from PyQt5.QtWidgets import QFormLayout, QHBoxLayout, QVBoxLayout, QWidget
from qfluentwidgets import (
    BodyLabel,
    CaptionLabel,
    CheckBox,
    ComboBox,
    HeaderCardWidget,
    LineEdit,
    PasswordLineEdit,
    ScrollArea,
    SpinBox,
    TitleLabel,
)


class VCard(HeaderCardWidget):
    """Titled card that stacks its contents vertically.

    HeaderCardWidget's own viewLayout is horizontal, which silently lays
    stacked rows out side by side — everything goes through `body` instead.
    """

    def __init__(self, title: str, parent: Optional[QWidget] = None):
        super().__init__(parent)
        self.setTitle(title)
        self.body = QVBoxLayout()
        self.body.setContentsMargins(0, 0, 0, 0)
        self.body.setSpacing(12)
        self.viewLayout.addLayout(self.body)

    def add_widget(self, widget: QWidget) -> QWidget:
        self.body.addWidget(widget)
        return widget

    def add_layout(self, layout) -> Any:
        self.body.addLayout(layout)
        return layout


class FormCard(VCard):
    """A titled card holding a two-column form."""

    def __init__(self, title: str, parent: Optional[QWidget] = None):
        super().__init__(title, parent)
        self.form = QFormLayout()
        self.form.setLabelAlignment(Qt.AlignLeft | Qt.AlignVCenter)
        self.form.setFormAlignment(Qt.AlignLeft | Qt.AlignTop)
        self.form.setHorizontalSpacing(18)
        self.form.setVerticalSpacing(12)
        self.body.addLayout(self.form)

    def add(self, label: str, widget: QWidget, hint: str = "") -> QWidget:
        if hint:
            box = QWidget(self)
            layout = QVBoxLayout(box)
            layout.setContentsMargins(0, 0, 0, 0)
            layout.setSpacing(2)
            layout.addWidget(widget)
            caption = CaptionLabel(hint, box)
            caption.setWordWrap(True)
            caption.setTextColor("#7a7a7a", "#9a9a9a")
            layout.addWidget(caption)
            self.form.addRow(BodyLabel(label, self), box)
        else:
            self.form.addRow(BodyLabel(label, self), widget)
        return widget

    def add_row(self, widget: QWidget) -> QWidget:
        self.form.addRow(widget)
        return widget


def line_edit(placeholder: str = "", text: str = "", password: bool = False) -> LineEdit:
    widget = PasswordLineEdit() if password else LineEdit()
    widget.setPlaceholderText(placeholder)
    widget.setText(text or "")
    widget.setClearButtonEnabled(True)
    widget.setMinimumWidth(360)
    return widget


def combo(items: Iterable[str], current: str = "") -> ComboBox:
    widget = ComboBox()
    items = list(items)
    widget.addItems(items)
    if current in items:
        widget.setCurrentIndex(items.index(current))
    widget.setMinimumWidth(240)
    return widget


def spin(minimum: int, maximum: int, value: int) -> SpinBox:
    widget = SpinBox()
    widget.setRange(minimum, maximum)
    widget.setValue(value)
    widget.setMinimumWidth(160)
    return widget


def checkbox(text: str, checked: bool) -> CheckBox:
    widget = CheckBox(text)
    widget.setChecked(bool(checked))
    return widget


def row(*widgets: QWidget, stretch_last: bool = False, spacing: int = 10) -> QWidget:
    box = QWidget()
    layout = QHBoxLayout(box)
    layout.setContentsMargins(0, 0, 0, 0)
    layout.setSpacing(spacing)
    for widget in widgets:
        layout.addWidget(widget)
    if not stretch_last:
        layout.addStretch(1)
    return box


class PageBase(ScrollArea):
    """Scrollable page with a title and a vertical stack of cards."""

    def __init__(self, object_name: str, title: str, subtitle: str = "", parent: Optional[QWidget] = None):
        super().__init__(parent)
        self.setObjectName(object_name)
        self.view = QWidget(self)
        self.view.setObjectName(object_name + "View")
        self.vbox = QVBoxLayout(self.view)
        self.vbox.setContentsMargins(28, 20, 28, 24)
        self.vbox.setSpacing(16)
        self.vbox.setAlignment(Qt.AlignTop)

        self.titleLabel = TitleLabel(title, self.view)
        self.vbox.addWidget(self.titleLabel)
        if subtitle:
            caption = BodyLabel(subtitle, self.view)
            caption.setWordWrap(True)
            caption.setTextColor("#666666", "#a0a0a0")
            self.vbox.addWidget(caption)

        self.setWidget(self.view)
        self.setWidgetResizable(True)
        self.enableTransparentBackground()

    def add_card(self, card: QWidget) -> QWidget:
        self.vbox.addWidget(card)
        return card
