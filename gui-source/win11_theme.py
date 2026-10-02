"""Windows 11 inspired light presentation; standard library + Tk only.

Keeps native ttk controls and keyboard behavior. No model, network, disk or
Windows service operations. Rounded backgrounds are generated in memory.
"""
from __future__ import annotations

import base64
import struct
import zlib
import tkinter as tk
from tkinter import font, ttk


COLORS = {
    "shell": "#f3f3f3", "surface": "#ffffff", "text": "#1a1a1a",
    "muted": "#626262", "line": "#dedede", "accent": "#005fb8",
    "hover": "#f6f6f6", "selected": "#e9f2fc", "disabled": "#9b9b9b",
}


def _rounded_png(fill: str, outline: str, radius: int, scale: float,
                 opaque_outer: str | None = None) -> bytes:
    """Small antialiased nine-slice; no Pillow dependency in the application."""
    radius = max(3, round(radius * scale))
    size = radius * 2 + 8
    border = max(1, round(scale))
    fg = tuple(bytes.fromhex(fill.lstrip("#")))
    edge = tuple(bytes.fromhex(outline.lstrip("#")))
    outer = (tuple(bytes.fromhex(opaque_outer.lstrip("#")))
             if opaque_outer else None)
    rows = bytearray()

    def inside(x: float, y: float, inset: int = 0) -> bool:
        r = max(0, radius - inset)
        cx = min(max(x, inset + r), size - inset - r)
        cy = min(max(y, inset + r), size - inset - r)
        return (x - cx) ** 2 + (y - cy) ** 2 <= r * r

    for y in range(size):
        rows.append(0)
        for x in range(size):
            covered = interior = 0
            for dy in (0.125, 0.375, 0.625, 0.875):
                for dx in (0.125, 0.375, 0.625, 0.875):
                    if inside(x + dx, y + dy):
                        covered += 1
                        interior += int(inside(x + dx, y + dy, border))
            shape = tuple(round((fg[i] * interior + edge[i] * (covered - interior)) /
                                covered) if covered else 0 for i in range(3))
            if outer:
                rgb = tuple(round((shape[i] * covered + outer[i] * (16 - covered)) / 16)
                            for i in range(3))
                rows.extend((*rgb, 255))
            else:
                rows.extend((*shape, round(255 * covered / 16)))

    def chunk(kind: bytes, payload: bytes) -> bytes:
        return (struct.pack("!I", len(payload)) + kind + payload +
                struct.pack("!I", zlib.crc32(kind + payload) & 0xffffffff))

    return (b"\x89PNG\r\n\x1a\n" +
            chunk(b"IHDR", struct.pack("!2I5B", size, size, 8, 6, 0, 0, 0)) +
            chunk(b"IDAT", zlib.compress(bytes(rows))) + chunk(b"IEND", b""))


def apply(root: tk.Misc) -> dict[str, str]:
    style = ttk.Style(root)
    if "ornith-fluent" not in style.theme_names():
        style.theme_create("ornith-fluent", parent="clam")
    style.theme_use("ornith-fluent")
    families = set(font.families(root))
    family = next((n for n in ("Segoe UI Variable Text", "Segoe UI", "Noto Sans CJK TC",
                               "Noto Sans", "DejaVu Sans") if n in families), "TkDefaultFont")
    root.option_add("*TCombobox*Listbox.font", (family, 10))
    for name in ("TkDefaultFont", "TkTextFont", "TkMenuFont", "TkHeadingFont"):
        font.nametofont(name, root=root).configure(family=family, size=10)
    root.configure(background=COLORS["shell"])
    style.configure(".", font=(family, 10), background=COLORS["surface"],
                    foreground=COLORS["text"], bordercolor=COLORS["line"],
                    focuscolor=COLORS["accent"])
    style.configure("TFrame", background=COLORS["surface"])
    style.configure("Shell.TFrame", background=COLORS["shell"])
    style.configure("TLabel", font=(family, 10), background=COLORS["surface"])
    for name, size, weight, color, background in (
        ("Title", 26, "bold", "text", "surface"),
        ("PageTitle", 20, "bold", "text", "surface"),
        ("Subtitle", 11, "normal", "muted", "surface"),
        ("Section", 10, "bold", "text", "surface"),
        ("Muted", 9, "normal", "muted", "surface"),
        ("Shell", 10, "normal", "text", "shell"),
        ("Brand.Shell", 17, "bold", "text", "shell"),
        ("Muted.Shell", 9, "normal", "muted", "shell"),
    ):
        style.configure(name + ".TLabel", font=(family, size, weight),
                        foreground=COLORS[color], background=COLORS[background])

    scale = max(1.0, float(root.tk.call("tk", "scaling")) / (96 / 72))
    images = []

    def background(name: str, states: list[tuple[str, str, str]], radius: int = 4,
                   button: bool = True, outer: str = "surface",
                   opaque_outer: str | None = None) -> None:
        assets = []
        for state, fill, outline in states:
            photo = tk.PhotoImage(master=root, data=base64.b64encode(
                _rounded_png(fill, outline, radius, scale, opaque_outer)))
            images.append(photo)
            assets.append((state, photo))
        element = "Ornith." + name
        if element not in style.element_names():
            style.element_create(element, "image", assets[0][1],
                                 *((s, p) for s, p in assets[1:]),
                                 border=round(radius * scale) + 2, sticky="nsew")
        if button:
            style.layout(name, [(element, {"sticky": "nsew", "children": [
                ("Button.padding", {"sticky": "nsew", "children": [
                    ("Button.label", {"sticky": "nsew"})]})]})])
        else:
            style.layout(name, [(element, {"sticky": "nsew"})])
        style.configure(name, background=COLORS[outer])

    background("TButton", [("", "#ffffff", "#d3d3d3"),
                            ("disabled", "#f5f5f5", "#e5e5e5"),
                            ("pressed", "#eeeeee", "#cccccc"),
                            ("focus", "#ffffff", "#005fb8"),
                            ("active", "#f6f6f6", "#c5c5c5")])
    style.configure("TButton", padding=(14, 8), anchor="center", font=(family, 10))
    style.map("TButton", foreground=[("disabled", "#9b9b9b")])
    background("Primary.TButton", [("", "#005fb8", "#0053a0"),
                                    ("disabled", "#ededed", "#e5e5e5"),
                                    ("pressed", "#004f99", "#004f99"),
                                    ("focus", "#005fb8", "#1a1a1a"),
                                    ("active", "#0868c0", "#005fb8")])
    style.configure("Primary.TButton", foreground="white", padding=(20, 9))
    style.map("Primary.TButton", foreground=[("disabled", "#9b9b9b"), ("!disabled", "white")])
    background("Selected.Chip.TButton", [("", "#e9f2fc", "#005fb8"),
                                          ("focus", "#e9f2fc", "#1a1a1a"),
                                          ("active", "#ddeafb", "#005fb8")])
    style.configure("Chip.TButton", padding=(12, 8), font=(family, 9))
    style.configure("Selected.Chip.TButton", padding=(12, 8), foreground="#005fb8",
                    font=(family, 9))
    for name in ("Source.TButton", "Quiet.Source.TButton", "Footer.TButton"):
        style.configure(name, padding=(10, 6), font=(family, 9))
    background("Nav.TButton", [("", "#f3f3f3", "#f3f3f3"),
                                ("disabled", "#f3f3f3", "#f3f3f3"),
                                ("pressed", "#dedede", "#dedede"),
                                ("focus", "#e9f2fc", "#005fb8"),
                                ("active", "#e9e9e9", "#e9e9e9")], outer="shell")
    style.configure("Nav.TButton", anchor="w", padding=(14, 10))
    background("Selected.Nav.TButton", [("", "#e4ebf2", "#e4ebf2"),
                                         ("focus", "#e4ebf2", "#005fb8"),
                                         ("active", "#dbe5ef", "#dbe5ef")], outer="shell")
    style.configure("Selected.Nav.TButton", foreground="#005fb8", anchor="w", padding=(14, 10))
    # This frame spans most of the window.  Let ttk paint the flat white
    # surface directly instead of stretching a generated image on every
    # redraw; the latter is disproportionately expensive in packaged Tk.
    style.configure("Card.TFrame", background=COLORS["surface"],
                    borderwidth=0, relief="flat")
    style.configure("TEntry", padding=(12, 9), fieldbackground="white",
                    bordercolor="#aaaaaa", lightcolor="white", darkcolor="white")
    style.map("TEntry", bordercolor=[("focus", "#005fb8")])
    style.configure("TCombobox", padding=(8, 6), arrowsize=12, bordercolor="#d3d3d3")
    style.map("TCombobox", fieldbackground=[("readonly", "white")],
              foreground=[("readonly", "#1a1a1a")],
              selectbackground=[("readonly", "white")],
              selectforeground=[("readonly", "#1a1a1a")])
    style.configure("Vertical.TScrollbar", arrowsize=12, width=10,
                    background="#bdbdbd", troughcolor="white", borderwidth=0)
    style.configure("TPanedwindow", background=COLORS["shell"])
    style.configure("Sash", sashthickness=12, gripcount=0)
    style.configure("TProgressbar", background=COLORS["accent"], troughcolor="#e8e8e8")
    # Tk images must outlive the style elements; keep each generation alive.
    root._ornith_theme_images = getattr(root, "_ornith_theme_images", []) + images
    return dict(COLORS, font=family)
