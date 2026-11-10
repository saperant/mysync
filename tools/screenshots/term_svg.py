"""A pyte screen as an SVG in a terminal window frame (like Textual's screenshots)."""
import re
from html import escape

NAMED = {"black": "#000000", "red": "#cd3131", "green": "#0dbc79", "brown": "#e5e510", "yellow": "#e5e510",
         "blue": "#2472c8", "magenta": "#bc3fbc", "cyan": "#11a8cd", "white": "#e5e5e5",
         "brightblack": "#666666", "brightred": "#f14c4c", "brightgreen": "#23d18b", "brightbrown": "#f5f543",
         "brightyellow": "#f5f543", "brightblue": "#3b8eea", "brightmagenta": "#d670d6", "brightcyan": "#29b8db",
         "brightwhite": "#ffffff"}
FG, BG = "#e0e0e0", "#1e1e1e"
CW, CH, PAD, TOP = 12.2, 24.4, 10, 40


def color(c, default):
    if c == "default":
        return default
    if c in NAMED:
        return NAMED[c]
    if len(c) == 6:
        return "#" + c
    return default


def render(screen, title: str) -> str:
    cols, rows = screen.columns, screen.lines
    W, H = PAD * 2 + cols * CW, TOP + PAD + rows * CH
    out = [f'<svg xmlns="http://www.w3.org/2000/svg" viewBox="0 0 {W:.0f} {H:.0f}" font-family="Fira Code, DejaVu Sans Mono, monospace" font-size="20">',
           f'<rect width="{W:.0f}" height="{H:.0f}" rx="8" fill="#292929" stroke="#444"/>',
           '<circle cx="20" cy="20" r="7" fill="#ff5f57"/><circle cx="42" cy="20" r="7" fill="#febc2e"/><circle cx="64" cy="20" r="7" fill="#28c840"/>',
           f'<text x="{W / 2:.0f}" y="26" fill="#c5c8c6" text-anchor="middle" font-family="sans-serif" font-size="17" font-weight="bold">{escape(title)}</text>',
           f'<rect x="{PAD}" y="{TOP}" width="{cols * CW:.1f}" height="{rows * CH:.1f}" fill="{BG}"/>']
    for y in range(rows):
        line = screen.buffer[y]
        x = 0
        while x < cols:
            ch = line[x]
            fg, bg = color(ch.fg, FG), color(ch.bg, BG)
            if ch.reverse:
                fg, bg = (bg if ch.bg != "default" else BG), (fg if ch.fg != "default" else FG)
            key = (fg, bg, ch.bold, ch.italics, ch.underscore)
            start, text = x, ""
            while x < cols:
                c = line[x]
                f2, b2 = color(c.fg, FG), color(c.bg, BG)
                if c.reverse:
                    f2, b2 = (b2 if c.bg != "default" else BG), (f2 if c.fg != "default" else FG)
                if (f2, b2, c.bold, c.italics, c.underscore) != key:
                    break
                text += c.data or " "
                x += 1
            px, py = PAD + start * CW, TOP + y * CH
            n = x - start
            if bg != BG:
                out.append(f'<rect x="{px:.1f}" y="{py:.1f}" width="{n * CW + 0.6:.1f}" height="{CH + 0.6:.1f}" fill="{bg}"/>')
            attrs = ' font-weight="bold"' if key[2] else ""
            attrs += ' font-style="italic"' if key[3] else ""
            attrs += ' text-decoration="underline"' if key[4] else ""
            # Each chunk without spaces at its own column: SVG renderers collapse spaces.
            for m in re.finditer(r"\S+(?: \S+)*", text):
                chunk, cx = m.group(), PAD + (start + m.start()) * CW
                out.append(f'<text x="{cx:.1f}" y="{py + CH * 0.76:.1f}" fill="{fg}" textLength="{len(chunk) * CW:.1f}" '
                           f'lengthAdjust="spacingAndGlyphs"{attrs}>{escape(chunk)}</text>')
    out.append("</svg>")
    return "\n".join(out)
