"""Follow the Omarchy theme.

Omarchy writes the active palette to
``~/.local/state/omarchy/current/theme/colors.toml`` whenever a theme is set.
This reads it, styles the window from it, and watches the file so that changing
theme re-skins Omaform while it is open. Without the file, on another desktop,
the stock libadwaita look stays, which is the right fallback rather than an
invented palette of our own.

The same file and the same approach Compy uses, deliberately: two applications
from the same desk should not drift apart on how they follow the theme.
"""

from __future__ import annotations

import os
import re
from dataclasses import dataclass
from pathlib import Path

from gi.repository import Gdk, Gio, Gtk


def colors_path() -> Path:
    override = os.environ.get("OMAFORM_THEME_COLORS")
    if override:
        return Path(override)
    return Path.home() / ".local/state/omarchy/current/theme/colors.toml"


def _hex(text: str) -> tuple[float, float, float] | None:
    raw = text.strip().lstrip("#")
    if len(raw) != 6:
        return None
    try:
        value = int(raw, 16)
    except ValueError:
        return None
    return ((value >> 16 & 255) / 255, (value >> 8 & 255) / 255, (value & 255) / 255)


@dataclass(frozen=True)
class Palette:
    dark: bool
    background: str
    dark_background: str
    darker_background: str
    lighter_background: str
    foreground: str
    dark_foreground: str
    bright_foreground: str
    accent: str
    selection: str
    muted: str
    red: str
    green: str


def parse(text: str) -> Palette | None:
    """The flat ``key = "value"`` lines of colors.toml; nothing else matters."""
    found: dict[str, str] = {}
    for line in text.splitlines():
        line = line.strip()
        if line.startswith("#") or "=" not in line:
            continue
        key, _, value = line.partition("=")
        value = value.strip()
        if value.startswith('"'):
            value = value[1:].split('"')[0]
        else:
            # An unquoted value ends at a comment, except that a bare hex
            # colour opens with the same character a comment does. Omarchy
            # quotes its values, but a hand-edited file may well not.
            bare = re.match(r"#[0-9a-fA-F]{6}\b", value)
            value = bare.group(0) if bare else value.split("#")[0].strip()
        found[key.strip()] = value

    def get(key: str, *fallbacks: str) -> str | None:
        for name in (key, *fallbacks):
            value = found.get(name)
            if value and _hex(value):
                return value
        return None

    background = get("background")
    foreground = get("foreground")
    if not background or not foreground:
        return None

    mode = found.get("mode")
    if mode:
        dark = mode != "light"
    else:
        r, g, b = _hex(background)  # type: ignore[misc]
        dark = 0.299 * r + 0.587 * g + 0.114 * b < 0.5

    return Palette(
        dark=dark,
        background=background,
        dark_background=get("dark_background", "background") or background,
        darker_background=get("darker_background", "dark_background") or background,
        lighter_background=get("lighter_background", "selection") or background,
        foreground=foreground,
        dark_foreground=get("dark_foreground", "muted") or foreground,
        bright_foreground=get("bright_foreground", "light_foreground") or foreground,
        accent=get("accent", "blue") or foreground,
        selection=get("selection", "accent") or background,
        muted=get("muted", "dark_foreground") or foreground,
        red=get("red", "bright_red") or foreground,
        green=get("green", "bright_green") or foreground,
    )


# Omarchy's own face, everywhere it draws: the bar, the terminal, the menus.
# A monospace UI is unusual and it is the point, because it is what makes an
# application look like it belongs on this desktop rather than visiting it.
FONT_STACK = '"JetBrainsMono Nerd Font", "JetBrains Mono", "Symbols Nerd Font", monospace'


def base_css() -> str:
    """Type, shape and density. Applied whether or not a palette was found.

    Modelled on omarchy.org, whose radius tokens are all literally 0px: every
    corner is square, cards are a flat surface with a hairline edge, buttons
    are outlined and medium weight, and nothing is set in uppercase. Section
    titles are plain text above their list, never boxed. Nothing here sets a
    colour, so a machine with no Omarchy theme keeps the shape in its own
    palette.
    """
    return f"""
    .omaform, .omaform * {{
        font-family: {FONT_STACK};
        font-size: 13px;
        border-radius: 0;
        box-shadow: none;
        text-shadow: none;
    }}
    .omaform headerbar {{
        min-height: 46px;
        padding: 0 8px;
    }}
    /* A group's title is a line of text, not a header bar. Normal case,
       medium weight, tight tracking, the way the site sets its headings. */
    .omaform label.heading {{
        font-size: 14px;
        font-weight: 600;
        letter-spacing: -0.01em;
        margin-bottom: 2px;
    }}
    .omaform preferencesgroup > box > box:first-child {{
        background: none;
        border: none;
        padding: 0 2px 6px 2px;
    }}
    .omaform viewswitcher button label {{
        font-size: 13px;
        font-weight: 500;
    }}
    .omaform row {{
        min-height: 40px;
        padding: 2px 6px;
    }}
    .omaform button {{
        padding: 6px 14px;
        font-weight: 500;
        min-height: 30px;
    }}
    .omaform button.pill {{ padding: 8px 22px; }}
    /* Scrollbars as omarchy.org draws them: a thin square bar, no track,
       no border, no arrows, no shading at the ends of the scroll. */
    .omaform scrollbar, .omaform scrollbar trough {{
        background: transparent; border: none; box-shadow: none;
    }}
    .omaform scrollbar slider {{
        min-width: 6px; min-height: 6px; margin: 0;
        border: none; border-radius: 0; box-shadow: none; outline: none;
    }}
    .omaform scrollbar.overlay-indicator:not(.hovering) slider {{
        min-width: 4px; min-height: 4px;
    }}
    .omaform scrollbar button {{ min-width: 0; min-height: 0; padding: 0; opacity: 0; }}
    .omaform scrolledwindow undershoot, .omaform scrolledwindow overshoot {{
        background: none; box-shadow: none;
    }}
    """


def css(p: Palette) -> str:
    """Colour, from the Omarchy palette.

    Only chrome. Values a form will actually receive are shown in the
    foreground colour and never in the accent, so that a theme with a loud
    accent cannot make a wrong value look like a confirmed one.
    """
    return f"""
    window.omaform, .omaform-page, .omaform preferencespage, .omaform viewstack {{
        background: {p.background};
        color: {p.foreground};
    }}
    .omaform headerbar {{
        background: {p.background};
        color: {p.bright_foreground};
        border-bottom: 1px solid {p.lighter_background};
    }}
    /* Tabs: plain text, the active one underlined in the accent. */
    .omaform viewswitcher button {{
        background: transparent;
        color: {p.dark_foreground};
        border: none;
        border-bottom: 2px solid transparent;
    }}
    .omaform viewswitcher button:hover {{ color: {p.foreground}; background: transparent; }}
    .omaform viewswitcher button:checked {{
        background: transparent;
        color: {p.bright_foreground};
        border-bottom: 2px solid {p.accent};
    }}
    /* The list is the card. Its header, styled above, is not. */
    .omaform listbox.boxed-list, .omaform .boxed-list, .omaform .card {{
        background: {p.dark_background};
        border: 1px solid {p.lighter_background};
    }}
    .omaform row, .omaform .activatable {{
        background: transparent;
        color: {p.foreground};
        border-bottom: 1px solid {p.lighter_background};
    }}
    .omaform row:last-child {{ border-bottom: none; }}
    .omaform row:hover {{ background: {p.lighter_background}; }}
    .omaform row:selected {{ background: {p.selection}; }}
    .omaform label.heading {{ color: {p.bright_foreground}; }}
    .omaform preferencesgroup > box > box:first-child label:not(.heading),
    .omaform .dim-label, .omaform .subtitle, .omaform row subtitle {{
        color: {p.dark_foreground};
    }}
    .omaform .title-1, .omaform .title-2, .omaform .title-4, .omaform row title {{
        color: {p.bright_foreground};
    }}
    /* Secondary buttons: surface with a strong hairline, like "See it in
       action". Primary: filled accent with dark text, like "Get Omarchy". */
    .omaform button {{
        background: {p.dark_background};
        color: {p.foreground};
        border: 1px solid {p.muted};
    }}
    .omaform button:hover {{
        background: {p.lighter_background};
        color: {p.bright_foreground};
    }}
    .omaform button.suggested-action {{
        background: {p.accent};
        color: {p.darker_background};
        border-color: {p.accent};
        font-weight: 600;
    }}
    .omaform button.suggested-action:hover {{ background: {p.accent}; opacity: 0.92; }}
    .omaform button.destructive-action {{
        background: transparent;
        color: {p.red};
        border-color: {p.red};
    }}
    .omaform button.destructive-action:hover {{
        background: {p.red};
        color: {p.darker_background};
    }}
    .omaform button:disabled {{ opacity: 0.5; }}
    .omaform entry, .omaform entry text, .omaform .entry-row {{
        background: {p.darker_background};
        color: {p.bright_foreground};
        border: 1px solid {p.lighter_background};
    }}
    .omaform entry:focus-within {{ border-color: {p.accent}; }}
    .omaform row:focus-visible {{
        outline: 2px solid {p.accent};
        outline-offset: -2px;
    }}
    /* Paper, not theme: a signature is judged against the white page it will
       sit on, and against a dark surface black ink would vanish. */
    .omaform .omaform-signature-preview, .omaform .omaform-signature-pad {{
        background: #ffffff;
        border: 1px solid {p.lighter_background};
    }}
    .omaform .omaform-signature-preview {{ padding: 6px 10px; }}
    .omaform .omaform-page-card {{ padding: 6px; border: 1px solid transparent; }}
    .omaform .omaform-page-thumb {{
        background: #ffffff;
        border: 1px solid {p.lighter_background};
    }}
    .omaform .omaform-page-card.omaform-drop {{ border: 2px solid {p.accent}; }}
    .omaform .omaform-page-card.omaform-dragging {{ opacity: 0.35; }}
    .omaform button.omaform-small {{ min-width: 24px; min-height: 24px; padding: 2px; }}
    .omaform .omaform-reading {{ color: {p.accent}; }}
    .omaform .omaform-filled {{ color: {p.green}; }}
    .omaform .omaform-skipped {{ color: {p.dark_foreground}; }}
    .omaform .omaform-locked {{ color: {p.accent}; }}
    .omaform scrollbar slider {{ background: {p.muted}; }}
    .omaform scrollbar slider:hover, .omaform scrollbar slider:active {{
        background: {p.foreground};
    }}
    .omaform toast {{
        background: {p.lighter_background};
        color: {p.bright_foreground};
        border: 1px solid {p.muted};
    }}
    """


class Theme:
    """Applies the palette and keeps following it while the app runs."""

    def __init__(self) -> None:
        self.provider = Gtk.CssProvider()
        self.palette: Palette | None = None
        self._monitor: Gio.FileMonitor | None = None

    def attach(self, display: Gdk.Display) -> None:
        Gtk.StyleContext.add_provider_for_display(
            display, self.provider, Gtk.STYLE_PROVIDER_PRIORITY_APPLICATION)
        self.reload()
        self._watch()

    def reload(self) -> None:
        path = colors_path()
        try:
            self.palette = parse(path.read_text())
        except OSError:
            self.palette = None
        # Shape and type always; colour only when Omarchy supplies a palette.
        self.provider.load_from_string(
            base_css() + (css(self.palette) if self.palette else ""))

    def _watch(self) -> None:
        path = colors_path()
        try:
            # The theme directory is a symlink Omarchy re-points, so watching the
            # file alone would stop working the first time the theme changes.
            self._monitor = Gio.File.new_for_path(str(path.parent)).monitor_directory(
                Gio.FileMonitorFlags.WATCH_MOVES, None)
        except Exception:
            return
        self._monitor.connect("changed", lambda *_: self.reload())
