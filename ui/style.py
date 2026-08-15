"""Flint visual styles. build_style(theme) composes the reference-matched
QSS from a palette; themes: dark (default), light, high-contrast."""

_PALETTES = {
    "dark": {
        "bg": "#0a0a0a",
        "card": "#0e0e0e",
        "border": "#1f1f1f",
        "text": "#ffffff",
        "muted": "#707070",
        "faded": "#525252",
        "track": "#1a1a1a",
        "primary": "#ffffff",
        "onPrimary": "#000000",
        "error": "#ff4444",
        "success": "#2ecc71",
        "warning": "#ffb300",
        "menuHover": "#262626",
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
    "progress_h": 6,
    "font_xs": 10,
    "font_sm": 11,
    "font_base": 13,
    "font_md": 14,
    "font_lg": 16,
    "font_xl": 22,
    "button_height": 36,
}

def px(n: int) -> str:
    return f"{n}px"


_QSS_TEMPLATE = """
QMainWindow {
    background: @bg;
}

QWidget {
    color: @text;
    font-family: "Segoe UI Variable", "Segoe UI";
    font-size: $font_base;
}

QFrame {
    background: @card;
    border: 1px solid @border;
    border-radius: 8px;
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

QProgressBar::chunk {
    background: @primary;
    border-radius: 3px;
}

QPushButton {
    background: transparent;
    border: 1px solid @border;
    border-radius: $radius_btn;
    color: @muted;
    font-size: $font_base;
    padding: $btn_pad_v $btn_pad_h;
}

QPushButton:hover {
    background: @track;
    color: @text;
}

QPushButton:pressed {
    background: @card;
}

QPushButton:disabled {
    color: @faded;
}

QPushButton#primary {
    background: @primary;
    border: none;
    color: @onPrimary;
    font-weight: 500;
    padding: $btn_primary_pad;
}

QPushButton#primary:disabled {
    background: @track;
    color: @faded;
}

QPushButton#primary:hover, QPushButton#primary:pressed {
    background: @primary;
    color: @onPrimary;
}

QPushButton:focus {
    border: 1px solid @muted;
}

QPushButton#primary:focus {
    border: 1px solid @text;
}

QPushButton#danger {
    background: @error;
    border: none;
    color: @onPrimary;
    font-weight: 500;
    padding: $btn_primary_pad;
}

QPushButton#danger:hover, QPushButton#danger:pressed {
    background: @error;
    color: @onPrimary;
}

QPushButton#danger:focus {
    border: 1px solid @text;
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
    background: @track;
    border: 1px solid @border;
    border-radius: $radius_md;
    color: @text;
    font-size: $font_base;
    padding: 4px 10px;
    min-height: 22px;
}

QComboBox:hover {
    border-color: @faded;
}

QComboBox:focus {
    border: 1px solid @muted;
}

QComboBox:disabled {
    color: @faded;
}

QComboBox::drop-down {
    border: none;
    width: 22px;
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
}

QListWidget {
    background: transparent;
    border: none;
    font-size: $font_base;
    outline: 0;
    padding: $space_md $space_sm;
}

QListWidget::item {
    border-radius: $radius_md;
    color: @muted;
    margin-bottom: 1px;
    padding: $space_sm $space_md;
}

QListWidget::item:hover {
    background: @card;
    color: @text;
}

QListWidget::item:selected {
    background: @track;
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
    font-size: 10px;
    font-weight: 600;
    padding-bottom: 2px;
}

QFrame#navItem {
    background: transparent;
    border: none;
    border-radius: 6px;
}

QFrame#navItem:hover {
    background: @track;
}

QFrame#navItem[on="true"] {
    background: @track;
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
    background: @card;
    border: 1px solid @border;
    border-radius: $radius_md;
    padding: $space_sm $space_md;
}

QFrame#driveChip:hover {
    background: @track;
}

QFrame#driveCard {
    background: @card;
    border: 1px solid @border;
    border-radius: 8px;
    padding: 0px;
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
    font-size: 12px;
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
    background: @card;
    border: 1px solid @border;
    border-radius: 6px;
    color: @muted;
    font-size: 14px;
    padding: 0;
}

QPushButton#iconBtn:hover {
    background: @track;
    color: @text;
}

QLabel#title {
    color: @text;
    font-size: 14px;
    font-weight: 500;
}

QLabel#subtitle {
    color: @faded;
    font-size: 11px;
}

QFrame#isoDropZone {
    background: @card;
    border: 1px dashed @border;
    border-radius: $radius_lg;
    padding: $space_xl;
}

QFrame#isoDropZone[loaded="true"] {
    border: 1px solid @border;
}

QFrame#isoDropZone:focus {
    border-color: @primary;
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
    background: @card;
    border: 1px solid @border;
    border-radius: $radius_lg;
    padding: $space_md $space_lg;
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
    border: 1px solid @primary;
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

QFrame#progressArea {
    background: @card;
    border: 1px solid @border;
    border-radius: $radius_lg;
    padding: $space_lg $space_md;
}

QLabel#progTitle {
    color: @text;
    font-size: 13px;
    font-weight: 500;
}

QLabel#progPct {
    color: @text;
    font-size: $font_xl;
    font-weight: 500;
}

QLabel#statCap {
    color: @muted;
    font-size: $font_xs;
}

QLabel#statVal {
    color: @faded;
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

QWidget#toggleTrack[on="false"] {
    background: @track;
    border: 1px solid @border;
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
    width: $space_sm;
    margin: 0;
}

QScrollBar::handle:vertical {
    background: @border;
    border-radius: 4px;
    min-height: 30px;
}

QScrollBar::add-line:vertical, QScrollBar::sub-line:vertical {
    height: 0;
}

QScrollBar::add-page:vertical, QScrollBar::sub-page:vertical {
    background: transparent;
}

QMenu {
    background: @card;
    border: 1px solid @border;
    color: @text;
    padding: $space_xs;
}

QMenu::item {
    border-radius: $radius_md;
    padding: $space_sm $space_xl $space_sm $space_md;
}

QMenu::item:selected {
    background: @menuHover;
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
    background: @card;
    border: 1px solid @border;
    border-radius: $radius_btn;
    color: @text;
    padding: $space_sm $space_md;
    font-size: $font_sm;
}

QLineEdit#shaInput {
    font-family: "Cascadia Mono", "Consolas", "Courier New";
    font-size: $font_sm;
}

QLineEdit:focus {
    border: 1px solid @primary;
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

QDialog#flintDialog QCheckBox::indicator:checked {
    background: @primary;
    border-color: @primary;
}

QDialog#flintDialog QCheckBox::indicator:focus {
    border-color: @muted;
}
"""


def build_style(theme: str = "dark") -> str:
    palette = _PALETTES.get(theme, _PALETTES["dark"])
    qss = _QSS_TEMPLATE
    for key, value in palette.items():
        qss = qss.replace(f"@{key}", value)
    # replace design token placeholders like $space_md with px values
    for key, val in DESIGN_TOKENS.items():
        qss = qss.replace(f"${key}", px(val))
    return qss


APP_STYLE: str = build_style("dark")