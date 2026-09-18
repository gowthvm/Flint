"""Flint visual styles. build_style(theme) composes the reference-matched
QSS from a palette; themes: dark (default), light, high-contrast."""

_PALETTES = {
    "dark": {
        "bg": "#0a0a0a",
        "card": "#121212",
        "border": "#2a2a2a",
        "text": "#ffffff",
        "muted": "#a8a8a8",  # matches flint-web --muted (AA on all surfaces)
        "faded": "#6f6f6f",  # matches flint-web --faded
        "track": "#1b1b1b",
        "primary": "#ffffff",
        "onPrimary": "#000000",
        "error": "#ff4444",
        "success": "#2ecc71",
        "warning": "#ffb300",
        "menuHover": "#2d2d2d",
        # Gradient stops
        "winTop": "#0c0c0c",
        "winBottom": "#090909",
        "surfaceTop": "#141414",
        "surfaceBottom": "#0f0f0f",
        "hoverTop": "#222222",
        "hoverBottom": "#191919",
        "activeTop": "#222222",
        "activeBottom": "#1a1a1a",
        "pressedTop": "#111111",
        "pressedBottom": "#0d0d0d",
        "primaryTop": "#ffffff",
        "primaryBottom": "#e6e6e6",
        "primaryHoverTop": "#f4f4f4",
        "primaryHoverBottom": "#dedede",
        "primaryPressedTop": "#e4e4e4",
        "primaryPressedBottom": "#d7d7d7",
        "dangerHover": "#e63c3c",
        "dangerPressed": "#cc3333",
        "borderHighlight": "rgba(255, 255, 255, 0.04)",
    },
    "light": {
        "bg": "#f2f2f2",
        "card": "#ffffff",
        "border": "#d0d0d0",
        "text": "#111111",
        "muted": "#5a5a5a",
        "faded": "#757575",
        "track": "#e4e4e4",
        "primary": "#111111",
        "onPrimary": "#ffffff",
        "error": "#c62828",
        "success": "#1e8e3e",
        "warning": "#b26a00",
        "menuHover": "#eaeaea",
        # Gradient stops
        "winTop": "#f4f4f4",
        "winBottom": "#ebebeb",
        "surfaceTop": "#ffffff",
        "surfaceBottom": "#f8f8f8",
        "hoverTop": "#eaeaea",
        "hoverBottom": "#e0e0e0",
        "activeTop": "#e0e0e0",
        "activeBottom": "#d6d6d6",
        "pressedTop": "#d8d8d8",
        "pressedBottom": "#d0d0d0",
        "primaryTop": "#333333",
        "primaryBottom": "#111111",
        "primaryHoverTop": "#444444",
        "primaryHoverBottom": "#222222",
        "primaryPressedTop": "#222222",
        "primaryPressedBottom": "#111111",
        "dangerHover": "#d32f2f",
        "dangerPressed": "#b71c1c",
        "borderHighlight": "rgba(0, 0, 0, 0.06)",
    },
    "high-contrast": {
        "bg": "#000000",
        "card": "#000000",
        "border": "#ffffff",
        "text": "#ffffff",
        "muted": "#d0d0d0",
        "faded": "#a0a0a0",
        "track": "#3d3d3d",
        "primary": "#ffffff",
        "onPrimary": "#000000",
        "error": "#ff5555",
        "success": "#4cd964",
        "warning": "#ffd54f",
        "menuHover": "#1c1c1c",
        # Gradient stops
        "winTop": "#000000",
        "winBottom": "#000000",
        "surfaceTop": "#111111",
        "surfaceBottom": "#0a0a0a",
        "hoverTop": "#222222",
        "hoverBottom": "#1a1a1a",
        "activeTop": "#333333",
        "activeBottom": "#282828",
        "pressedTop": "#0a0a0a",
        "pressedBottom": "#050505",
        "primaryTop": "#ffffff",
        "primaryBottom": "#e6e6e6",
        "primaryHoverTop": "#f4f4f4",
        "primaryHoverBottom": "#dedede",
        "primaryPressedTop": "#e4e4e4",
        "primaryPressedBottom": "#d7d7d7",
        "dangerHover": "#ff6666",
        "dangerPressed": "#cc3333",
        "borderHighlight": "rgba(255, 255, 255, 0.08)",
    },
}

# Design tokens (used by Python UI components)
DESIGN_TOKENS = {
    "space_xs": 4,
    "space_sm": 8,
    "space_md": 12,
    "space_lg": 16,
    "space_xl": 20,
    "radius_sm": 4,
    "radius_md": 6,
    "radius_lg": 8,
    "radius_btn": 7,
    "icon_small": 16,
    "icon_medium": 22,
    "icon_large": 38,
    "toggle_w": 28,
    "toggle_h": 16,
    "toggle_knob": 12,
    "btn_pad_v": 9,
    "btn_pad_h": 16,
    "btn_primary_pad": 10,
    "progress_h": 4,
    "font_xs": 10,
    "font_sm": 11,
    "font_base": 13,
    "font_md": 14,
    "font_lg": 16,
    "font_xl": 22,
    "button_height": 36,
    "chamfer": 8,  # signature cut-corner motif size (px)
}

# Machine-voice font stack used by readout widgets ($font_mono in QSS)
FONT_MONO = '"Cascadia Mono", "Cascadia Code", "Consolas", "Courier New"'

def px(n: int) -> str:
    return f"{n}px"


_QSS_TEMPLATE = """
QMainWindow {
    background: qlineargradient(
        spread:pad,
        x1:0, y1:0,
        x2:0, y2:1,
        stop:0 @winTop,
        stop:1 @winBottom
    );
}

QWidget {
    color: @text;
    font-family: "Segoe UI Variable", "Segoe UI";
    font-size: $font_base;
}

QWidget:focus {
    outline: none;
}

QWidget#sidebar {
    background: qlineargradient(
        spread:pad,
        x1:0, y1:0,
        x2:1, y2:0,
        stop:0 @winTop,
        stop:1 @winBottom
    );
    border-right: 1px solid @border;
}

QFrame {
    background: qlineargradient(
        spread:pad,
        x1:0, y1:0,
        x2:0, y2:1,
        stop:0 @surfaceTop,
        stop:1 @surfaceBottom
    );
    border: 1px solid @border;
    border-radius: 10px;
}

QWidget#fixedStrip {
    background: @bg;
    border: none;
}

QLabel {
    background: transparent;
    border: none;
    color: @muted;
    font-size: $font_sm;
}

QLabel[colorRole="muted"] {
    color: @muted;
    font-size: $font_sm;
}

QLabel[colorRole="label"] {
    color: @faded;
    font-size: $font_sm;
}

QFrame#helpTip {
    background: @card;
    border: 1px solid @border;
    border-radius: $radius_lg;
}

QLabel#helpTipLabel {
    background: transparent;
    border: none;
    color: @text;
    font-size: $font_sm;
}

QProgressBar {
    background: @track;
    border: none;
    border-radius: 3px;
    min-height: $progress_h;
    max-height: $progress_h;
    font-size: 1px;
    color: transparent;
}

QProgressBar:focus {
    border: none;
    outline: none;
}

QProgressBar::chunk {
    background: @primary;
    border: none;
    border-radius: 3px;
}

QPushButton {
    background: qlineargradient(
        spread:pad,
        x1:0, y1:0,
        x2:0, y2:1,
        stop:0 @surfaceTop,
        stop:1 @surfaceBottom
    );
    border: 1px solid @border;
    border-radius: $radius_btn;
    color: @muted;
    font-size: $font_base;
    padding: $btn_pad_v $btn_pad_h;
}

QPushButton:hover {
    background: qlineargradient(
        spread:pad,
        x1:0, y1:0,
        x2:0, y2:1,
        stop:0 @hoverTop,
        stop:1 @hoverBottom
    );
    border-color: @faded;
    color: @text;
}

QPushButton:pressed {
    background: qlineargradient(
        spread:pad,
        x1:0, y1:0,
        x2:0, y2:1,
        stop:0 @pressedTop,
        stop:1 @pressedBottom
    );
    border-color: @faded;
}

QPushButton:disabled {
    color: @faded;
    background: @card;
    border-color: @card;
}

QPushButton#primary {
    background: qlineargradient(
        spread:pad,
        x1:0, y1:0,
        x2:0, y2:1,
        stop:0 @primaryTop,
        stop:1 @primaryBottom
    );
    border: none;
    color: @onPrimary;
    font-weight: 500;
    padding: $btn_primary_pad;
}

QPushButton#primary:disabled {
    background: @track;
    color: @faded;
}

QPushButton#primary:hover {
    background: qlineargradient(
        spread:pad,
        x1:0, y1:0,
        x2:0, y2:1,
        stop:0 @primaryHoverTop,
        stop:1 @primaryHoverBottom
    );
    color: @onPrimary;
}

QPushButton#primary:pressed {
    background: qlineargradient(
        spread:pad,
        x1:0, y1:0,
        x2:0, y2:1,
        stop:0 @primaryPressedTop,
        stop:1 @primaryPressedBottom
    );
    color: @onPrimary;
}

QPushButton:focus {
    border-color: @muted;
}

QPushButton#primary:focus {
    border: 1px solid @muted;
}

QPushButton#danger {
    background: @error;
    border: none;
    color: @onPrimary;
    font-weight: 500;
    padding: $btn_primary_pad;
}

QPushButton#danger:hover {
    background: @dangerHover;
    color: @onPrimary;
}

QPushButton#danger:pressed {
    background: @dangerPressed;
    color: @onPrimary;
}

QPushButton#danger:focus {
    border: 1px solid @muted;
}

QPushButton#ghost {
    background: transparent;
    border: 1px solid @border;
    color: @muted;
}

QPushButton#ghost:hover {
    background: @track;
    color: @text;
}

QPushButton#ghost:pressed {
    background: @border;
    color: @text;
}

QPushButton#helpBtn {
    background: @track;
    border: 1px solid @border;
    border-radius: 9px;
    color: @muted;
    font-size: 10px;
    font-weight: 600;
    padding: 0;
}

QPushButton#helpBtn:hover {
    color: @text;
    border-color: @faded;
}

QComboBox {
    background: qlineargradient(
        spread:pad,
        x1:0, y1:0,
        x2:0, y2:1,
        stop:0 @surfaceTop,
        stop:1 @surfaceBottom
    );
    border: 1px solid @border;
    border-radius: $radius_md;
    color: @text;
    font-size: $font_base;
    padding: 4px 10px;
    min-height: 22px;
}

QComboBox:hover {
    border-color: @faded;
    background: qlineargradient(
        spread:pad,
        x1:0, y1:0,
        x2:0, y2:1,
        stop:0 @hoverTop,
        stop:1 @hoverBottom
    );
}

QComboBox:focus {
    border: 1px solid @muted;
}

QComboBox:pressed {
    background: qlineargradient(
        spread:pad,
        x1:0, y1:0,
        x2:0, y2:1,
        stop:0 @pressedTop,
        stop:1 @pressedBottom
    );
}

QComboBox:disabled {
    color: @faded;
}

QComboBox::drop-down {
    border: none;
    width: 22px;
}

QComboBox::drop-down:hover {
    border-left: 1px solid @border;
}

QComboBox::down-arrow {
    image: none;
    border-left: 3px solid transparent;
    border-right: 3px solid transparent;
    border-top: 5px solid @muted;
    margin-right: 6px;
}

QComboBox QAbstractItemView {
    background: @card;
    border: 1px solid @border;
    color: @text;
    selection-background-color: @track;
    selection-color: @text;
    outline: none;
}

QAbstractItemView::item:selected,
QAbstractItemView::item:selected:focus {
    background: qlineargradient(
        spread:pad,
        x1:0, y1:0,
        x2:0, y2:1,
        stop:0 @activeTop,
        stop:1 @activeBottom
    );
    color: @text;
    border: none;
}

QListWidget {
    background: transparent;
    border: none;
    font-size: $font_base;
    outline: none;
    padding: $space_md $space_sm;
}

QListWidget::item {
    border-radius: $radius_md;
    color: @muted;
    margin-bottom: 1px;
    padding: $space_sm $space_md;
}

QListWidget::item:hover {
    background: qlineargradient(
        spread:pad,
        x1:0, y1:0,
        x2:0, y2:1,
        stop:0 @hoverTop,
        stop:1 @hoverBottom
    );
    color: @text;
}

QListWidget::item:selected {
    background: qlineargradient(
        spread:pad,
        x1:0, y1:0,
        x2:0, y2:1,
        stop:0 @activeTop,
        stop:1 @activeBottom
    );
    color: @text;
}

QListWidget::item:selected:!active {
    background: qlineargradient(
        spread:pad,
        x1:0, y1:0,
        x2:0, y2:1,
        stop:0 @activeTop,
        stop:1 @activeBottom
    );
    color: @text;
}

QFrame#vdiv, QFrame#hdiv {
    background: @border;
    border: none;
}

QLabel#logoMark {
    background: @primary;
    border-radius: 6px;
    color: @onPrimary;
    font-size: 12px;
}

QLabel#logoName {
    color: @text;
    font-size: 14px;
    font-weight: 500;
}

QLabel#capLabel {
    color: @faded;
    font-family: $font_mono;
    font-size: 10px;
    font-weight: 600;
    letter-spacing: 0.08em;
    padding-bottom: 2px;
}

QFrame#navItem {
    background: transparent;
    border: 1px solid transparent;
    border-radius: 6px;
}

QFrame#navItem:hover {
    background: qlineargradient(
        spread:pad,
        x1:0, y1:0,
        x2:0, y2:1,
        stop:0 @hoverTop,
        stop:1 @hoverBottom
    );
    border: 1px solid @border;
}

QFrame#navItem[on="true"] {
    background: qlineargradient(
        spread:pad,
        x1:0, y1:0,
        x2:0, y2:1,
        stop:0 @activeTop,
        stop:1 @activeBottom
    );
    border: 1px solid @border;
}

QFrame#navItem:focus {
    border: 1px solid @muted;
}

QLabel#navText {
    color: @muted;
    font-size: 13px;
}

QLabel#navText[on="true"] {
    color: @text;
}

QLabel#badge {
    background: @border;
    border-radius: 10px;
    color: @muted;
    font-size: 10px;
    padding: 2px 6px;
}

QLabel#badgeOn {
    background: @primary;
    border-radius: 10px;
    color: @onPrimary;
    font-size: 10px;
    padding: 2px 6px;
}

QFrame#driveChip {
    background: qlineargradient(
        spread:pad,
        x1:0, y1:0,
        x2:0, y2:1,
        stop:0 @surfaceTop,
        stop:1 @surfaceBottom
    );
    border: 1px solid @border;
    border-radius: $radius_md;
}

QFrame#driveChip:hover {
    background: qlineargradient(
        spread:pad,
        x1:0, y1:0,
        x2:0, y2:1,
        stop:0 @hoverTop,
        stop:1 @hoverBottom
    );
}

QFrame#driveCard {
    background: qlineargradient(
        spread:pad,
        x1:0, y1:0,
        x2:0, y2:1,
        stop:0 @surfaceTop,
        stop:1 @surfaceBottom
    );
    border: 1px solid @border;
    border-radius: 8px;
}

QFrame#driveCard:hover {
    border-color: @muted;
}

QFrame#driveChip:focus {
    border-color: @muted;
}

QWidget#toggleSwitch:focus QLabel#toggleTrack {
    border: 1px solid @muted;
}

QLabel#doneSummary {
    color: @muted;
    font-size: 11px;
}

QLabel#verifyHint {
    color: @muted;
    font-size: 11px;
}

QLabel#dropError {
    color: @error;
    font-size: 11px;
}

QLabel#dot {
    background: @primary;
    border-radius: 3px;
}

QLabel#dot[dim="true"] {
    background: @faded;
}

QLabel#driveName {
    color: @text;
    font-size: 13px;
    font-weight: 500;
}

QLabel#driveName[dim="true"] {
    color: @faded;
}

QLabel#driveSub {
    color: @muted;
    font-size: 11px;
}

QLabel#driveSub[dim="true"] {
    color: @faded;
}

QPushButton#iconBtn {
    background: qlineargradient(
        spread:pad,
        x1:0, y1:0,
        x2:0, y2:1,
        stop:0 @surfaceTop,
        stop:1 @surfaceBottom
    );
    border: 1px solid @border;
    border-radius: 6px;
    color: @muted;
    font-size: 14px;
    padding: 0;
}

QPushButton#iconBtn:hover {
    background: qlineargradient(
        spread:pad,
        x1:0, y1:0,
        x2:0, y2:1,
        stop:0 @hoverTop,
        stop:1 @hoverBottom
    );
    border-color: @faded;
    color: @text;
}

QPushButton#iconBtn:pressed {
    background: qlineargradient(
        spread:pad,
        x1:0, y1:0,
        x2:0, y2:1,
        stop:0 @pressedTop,
        stop:1 @pressedBottom
    );
    color: @text;
}

QLabel#title {
    color: @text;
    font-size: 15px;
    font-weight: 600;
}

QLabel#subtitle {
    color: @faded;
    font-size: 11px;
}

QFrame#isoDropZone {
    background: qlineargradient(
        spread:pad,
        x1:0, y1:0,
        x2:0, y2:1,
        stop:0 @surfaceTop,
        stop:1 @surfaceBottom
    );
    border: 1px dashed @border;
    border-radius: $radius_lg;
    padding: $space_xl;
}

QFrame#isoDropZone[dragging="true"] {
    border: 2px solid @primary;
    background: @track;
}

QFrame#isoDropZone[loaded="true"] {
    border: 1px solid @border;
}

QFrame#isoDropZone:focus {
    border-color: @muted;
}

QLabel#emptyIsoIcon {
    color: @muted;
    font-size: $font_xl;
}

QLabel#emptyIsoText {
    color: @muted;
    font-size: $font_base;
}

QLabel#isoIcon {
    background: @card;
    border: 1px solid @border;
    border-radius: $radius_btn;
    color: @text;
    font-size: $font_lg;
}

QLabel#isoName {
    color: @text;
    font-size: $font_base;
    font-weight: 500;
}

QLabel#isoMeta {
    color: @faded;
    font-size: $font_sm;
}

QLabel#isoMeta[error="true"] {
    color: @error;
}

QLabel#isoCheck {
    color: @text;
    font-size: $font_lg;
}

QPushButton#isoClear {
    background: transparent;
    border: 1px solid transparent;
    border-radius: $radius_sm;
    color: @muted;
    font-size: $font_sm;
    padding: 0;
}

QPushButton#isoClear:hover {
    background: @track;
    border-color: @border;
    color: @error;
}

QPushButton#isoClear:pressed {
    background: @border;
}

QFrame#block {
    background: qlineargradient(
        spread:pad,
        x1:0, y1:0,
        x2:0, y2:1,
        stop:0 @surfaceTop,
        stop:1 @surfaceBottom
    );
    border: 1px solid @border;
    border-top: 1px solid @borderHighlight;
    border-radius: $radius_lg;
}

QFrame#recessed {
    background: qlineargradient(
        spread:pad,
        x1:0, y1:0,
        x2:0, y2:1,
        stop:0 @surfaceTop,
        stop:1 @surfaceBottom
    );
    border: 1px solid @border;
    border-radius: $radius_md;
}

QLabel#chip {
    background: @card;
    border: 1px solid @border;
    border-radius: $radius_sm;
    color: @text;
    font-size: $font_sm;
    padding: 3px 8px;
}

QLabel#chipOn {
    background: @primary;
    border: 1px solid @primary;
    border-radius: $radius_sm;
    color: @onPrimary;
    font-size: $font_sm;
    font-weight: 500;
    padding: 3px 8px;
}

QLabel#chipOk {
    background: @card;
    border: 1px solid @border;
    border-radius: $radius_sm;
    color: @success;
    font-size: $font_sm;
    padding: 3px 8px;
}

QPushButton#seg {
    background: transparent;
    border: 1px solid @border;
    border-radius: 5px;
    color: @muted;
    font-size: 12px;
    padding: 7px 0;
}

QPushButton#seg:hover {
    background: @track;
    color: @text;
}

QPushButton#seg:focus,
QPushButton#segOn:focus {
    border-color: @muted;
}

QPushButton#segOn {
    background: @primary;
    border: 1px solid @primary;
    border-radius: 5px;
    color: @onPrimary;
    font-size: 12px;
    font-weight: 500;
    padding: 7px 0;
}

QPushButton#segOn:hover {
    background: @muted;
}

QFrame#progressArea {
    background: qlineargradient(
        spread:pad,
        x1:0, y1:0,
        x2:0, y2:1,
        stop:0 @surfaceTop,
        stop:1 @surfaceBottom
    );
    border: 1px solid @border;
    border-top: 1px solid @borderHighlight;
    border-radius: $radius_lg;
}

QLabel#progTitle {
    color: @text;
    font-size: $font_sm;
    font-weight: 500;
}

QLabel#progPct {
    color: @text;
    font-family: $font_mono;
    font-size: $font_md;
    font-weight: 500;
}

QLabel#statCap {
    color: @muted;
    font-size: $font_xs;
}

QLabel#statVal {
    color: @faded;
    font-family: $font_mono;
    font-size: $font_sm;
}

QLabel#progError {
    color: @error;
}

QLabel#progError[level="warning"] {
    color: @warning;
}

QLabel#verifyLabel {
    color: @faded;
    font-size: 11px;
}

QWidget#toggleTrack {
    background: @primary;
    border-radius: 8px;
}

QWidget#toggleTrack:hover {
    background: @text;
}

QWidget#toggleTrack[on="false"] {
    background: @track;
    border: 1px solid @border;
}

QWidget#toggleTrack[on="false"]:hover {
    background: @card;
    border-color: @faded;
}

QWidget#toggleTrack:disabled {
    opacity: 0.4;
}

QWidget#toggleTrack:disabled[on="false"] {
    opacity: 0.3;
}

QLabel#toggleKnob {
    background: @onPrimary;
    border-radius: 6px;
}

QWidget#toggleTrack[on="false"] QLabel#toggleKnob {
    background: @muted;
}

QRadioButton {
    color: @text;
    font-size: $font_base;
    spacing: 8px;
}

QRadioButton::indicator {
    width: 14px;
    height: 14px;
    border-radius: 7px;
    border: 1px solid @border;
    background: @card;
}

QRadioButton::indicator:hover {
    border-color: @faded;
}

QRadioButton::indicator:checked {
    background: @primary;
    border: none;
}

QScrollArea {
    background: transparent;
    border: none;
}

QScrollArea > QWidget > QWidget {
    background: transparent;
}

QScrollBar:vertical {
    background: transparent;
    width: 8px;
    margin: 0;
}

QScrollBar::handle:vertical {
    background: @border;
    border-radius: 4px;
    min-height: 30px;
}

QScrollBar::handle:vertical:hover {
    background: @muted;
}

QScrollBar::add-line:vertical, QScrollBar::sub-line:vertical {
    height: 0;
}

QScrollBar::add-page:vertical, QScrollBar::sub-page:vertical {
    background: transparent;
}

QMenu {
    background: qlineargradient(
        spread:pad,
        x1:0, y1:0,
        x2:0, y2:1,
        stop:0 @surfaceTop,
        stop:1 @surfaceBottom
    );
    border: 1px solid @border;
    color: @text;
    padding: $space_xs;
}

QMenu::item {
    border-radius: $radius_md;
    padding: $space_sm $space_xl $space_sm $space_md;
}

QMenu::item:selected {
    background: qlineargradient(
        spread:pad,
        x1:0, y1:0,
        x2:0, y2:1,
        stop:0 @activeTop,
        stop:1 @activeBottom
    );
}

QMenu::item:disabled {
    color: @faded;
}

QMenu::separator {
    height: 1px;
    background: @border;
    margin: 5px 8px;
}

QLineEdit {
    background: qlineargradient(
        spread:pad,
        x1:0, y1:0,
        x2:0, y2:1,
        stop:0 @surfaceTop,
        stop:1 @surfaceBottom
    );
    border: 1px solid @border;
    border-radius: $radius_btn;
    color: @text;
    padding: $space_sm $space_md;
    font-size: $font_sm;
}

QLineEdit#shaInput {
    font-family: $font_mono;
    font-size: $font_sm;
}

QLineEdit:focus {
    border: 1px solid @muted;
}

QDialog#flintDialog {
    background: @card;
    border: 1px solid @border;
    border-radius: 12px;
}

QDialog#flintDialog QLabel#flintDialogIcon {
    background: transparent;
    border: none;
    font-size: 24px;
    font-weight: 600;
}

QDialog#flintDialog QLabel#flintDialogIcon[dialogRole="success"] {
    color: @success;
}

QDialog#flintDialog QLabel#flintDialogIcon[dialogRole="warning"] {
    color: @warning;
}

QDialog#flintDialog QLabel#flintDialogIcon[dialogRole="error"] {
    color: @error;
}

QDialog#flintDialog QLabel#flintDialogIcon[dialogRole="info"] {
    color: @muted;
}

QDialog#flintDialog QLabel#flintDialogTitle {
    background: transparent;
    color: @text;
    font-size: $font_md;
    font-weight: 600;
}

QDialog#flintDialog QLabel#flintDialogMessage {
    background: transparent;
    color: @muted;
    font-size: $font_sm;
}

QDialog#flintDialog QCheckBox {
    color: @muted;
    font-size: $font_sm;
    spacing: 6px;
}

QDialog#flintDialog QCheckBox::indicator {
    width: 14px;
    height: 14px;
    border: 1px solid @border;
    border-radius: 4px;
    background: @card;
}

QDialog#flintDialog QCheckBox::indicator:hover {
    border-color: @faded;
}

QDialog#flintDialog QCheckBox::indicator:checked {
    background: @primary;
    border-color: @primary;
}

QDialog#flintDialog QCheckBox::indicator:focus {
    border-color: @muted;
}
"""


_CURRENT_THEME = "dark"


def palette_for(theme: str | None = None) -> dict[str, str]:
    """Return the palette for a theme; defaults to the currently active one."""
    return _PALETTES.get(theme or _CURRENT_THEME, _PALETTES["dark"])


def build_style(theme: str = "dark") -> str:
    global _CURRENT_THEME
    _CURRENT_THEME = theme
    palette = _PALETTES.get(theme, _PALETTES["dark"])
    qss = _QSS_TEMPLATE
    for key, value in palette.items():
        qss = qss.replace(f"@{key}", value)
    # replace design token placeholders like $space_md with px values
    for key, val in DESIGN_TOKENS.items():
        qss = qss.replace(f"${key}", px(val))
    qss = qss.replace("$font_mono", FONT_MONO)
    return qss


APP_STYLE: str = build_style("dark")