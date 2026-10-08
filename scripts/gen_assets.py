#!/usr/bin/env python3
"""Generate the README visual assets (logo, banner, pipeline) as themed SVGs.

One palette dict per theme, one template per asset, so dark and light variants
can never drift apart. No dependencies. Usage: gen_assets.py <output-dir>
"""

import sys
from pathlib import Path

THEMES = {
    "dark": dict(
        bg0="#0B1020",
        bg1="#161E3A",
        ink="#F2F4FA",
        muted="#9AA3BD",
        line="#2A3355",
        card="#1A2240",
        amber="#FF9F4A",
        teal="#35D6C0",
    ),
    "light": dict(
        bg0="#FFFDF8",
        bg1="#F3EDDF",
        ink="#121829",
        muted="#5B6479",
        line="#D9D2C0",
        card="#FFFFFF",
        amber="#E8731A",
        teal="#0E9F8E",
    ),
}

MONO = "ui-monospace,SFMono-Regular,Menlo,Consolas,'Liberation Mono',monospace"
SANS = "-apple-system,BlinkMacSystemFont,'Segoe UI',Helvetica,Arial,sans-serif"


def mark(x, y, scale, p):
    """The logo mark: viewfinder corners, a play triangle that resolves into text lines."""
    return f"""<g transform="translate({x} {y}) scale({scale})" fill="none" stroke-linecap="round" stroke-linejoin="round">
    <path d="M10 30V14Q10 10 14 10H30M90 30V14Q90 10 86 10H70M10 70V86Q10 90 14 90H30M90 70V86Q90 90 86 90H70" stroke="{p["amber"]}" stroke-width="6"/>
    <path d="M28 38L46 50L28 62Z" fill="{p["teal"]}" stroke="{p["teal"]}" stroke-width="3"/>
    <path d="M56 38H80M56 50H74M56 62H78" stroke="{p["muted"]}" stroke-width="5"/>
  </g>"""


def svg(w, h, body, title, p):
    return f"""<?xml version="1.0" encoding="UTF-8"?>
<svg xmlns="http://www.w3.org/2000/svg" width="{w}" height="{h}" viewBox="0 0 {w} {h}" role="img" aria-label="{title}">
  <title>{title}</title>
{body}
</svg>
"""


def logo(p):
    body = f'  <rect width="100" height="100" rx="22" fill="{p["bg0"]}"/>\n  ' + mark(0, 0, 1, p)
    return svg(100, 100, body, "frame-ingest logo", p)


def banner(p):
    w, h = 1200, 300
    perfs = "".join(
        f'<rect x="{x}" y="14" width="18" height="10" rx="3"/><rect x="{x}" y="{h - 24}" width="18" height="10" rx="3"/>'
        for x in range(16, w, 32)
    )
    card = f"""
  <g transform="translate(812 62)">
    <rect width="348" height="176" rx="16" fill="{p["card"]}" stroke="{p["line"]}" stroke-width="1.5"/>
    <circle cx="22" cy="22" r="5" fill="{p["amber"]}"/><circle cx="40" cy="22" r="5" fill="{p["teal"]}"/><circle cx="58" cy="22" r="5" fill="{p["line"]}"/>
    <text x="24" y="62" font-family="{MONO}" font-size="15" font-weight="700" fill="{p["amber"]}">ch-01</text>
    <rect x="24" y="74" width="230" height="8" rx="4" fill="{p["line"]}"/>
    <rect x="24" y="92" width="180" height="8" rx="4" fill="{p["line"]}"/>
    <text x="24" y="124" font-family="{MONO}" font-size="15" font-weight="700" fill="{p["teal"]}">t-000125</text>
    <rect x="24" y="136" width="250" height="8" rx="4" fill="{p["line"]}"/>
    <text x="24" y="166" font-family="{MONO}" font-size="10.5" fill="{p["muted"]}">audio yes · transcript asr · frames 8/8 analysed</text>
  </g>"""
    body = f"""  <defs>
    <linearGradient id="g" x1="0" y1="0" x2="1" y2="1"><stop offset="0" stop-color="{p["bg0"]}"/><stop offset="1" stop-color="{p["bg1"]}"/></linearGradient>
  </defs>
  <rect width="{w}" height="{h}" rx="20" fill="url(#g)"/>
  <g fill="{p["line"]}" opacity="0.7">{perfs}</g>
  {mark(56, 82, 1.45, p)}
  <text x="226" y="148" font-family="{MONO}" font-size="62" font-weight="700" fill="{p["ink"]}">frame<tspan fill="{p["amber"]}">-ingest</tspan></text>
  <text x="228" y="192" font-family="{SANS}" font-size="27" font-weight="600" fill="{p["ink"]}">Video in. Citable Markdown out.</text>
  <text x="228" y="228" font-family="{MONO}" font-size="14.5" fill="{p["muted"]}">transcript · chapters · summaries · glossary · entities</text>{card}"""
    return svg(w, h, body, "frame-ingest: video in, citable Markdown out", p)


def arrow(x1, y1, x2, y2, p, dashed=False):
    d = ' stroke-dasharray="5 5"' if dashed else ""
    return f'  <path d="M{x1} {y1}L{x2} {y2}" stroke="{p["muted"]}" stroke-width="2" fill="none" marker-end="url(#ah)"{d}/>'


def flow(p):
    """Data flow in four steps: where the video's data goes, and who touches it."""
    w, h = 1040, 190
    bw, bh, y = 210, 92, 24
    step = (w - 40 - bw) / 3
    xs = [20 + i * step for i in range(4)]
    A, T, N = p["amber"], p["teal"], p["muted"]
    spec = [
        ("video", "a file or a URL", "", N),
        ("ingest", "runs on your machine", "transcript + key frames", A),
        ("write", "your agent's model", "reads frames, fixes the text", T),
        ("finish", "checks and builds", "citable Markdown + JSON", A),
    ]
    boxes, arrows = [], []
    for x, (title, l1, l2, c) in zip(xs, spec, strict=True):
        boxes.append(f"""  <g transform="translate({x:.1f} {y})">
    <rect width="{bw}" height="{bh}" rx="16" fill="{p["card"]}" stroke="{c}" stroke-width="2.5"/>
    <text x="{bw / 2}" y="38" text-anchor="middle" font-family="{MONO}" font-size="22" font-weight="700" fill="{c}">{title}</text>
    <text x="{bw / 2}" y="62" text-anchor="middle" font-family="{SANS}" font-size="13.5" fill="{p["ink"]}">{l1}</text>
    <text x="{bw / 2}" y="80" text-anchor="middle" font-family="{SANS}" font-size="13.5" fill="{p["muted"]}">{l2}</text>
  </g>""")
    for i in range(3):
        arrows.append(arrow(xs[i] + bw + 6, y + bh / 2, xs[i + 1] - 6, y + bh / 2, p))
    legend = f"""  <g font-family="{SANS}" font-size="13.5" fill="{p["muted"]}">
    <rect x="20" y="150" width="14" height="14" rx="4" fill="none" stroke="{A}" stroke-width="2.5"/><text x="42" y="162">plain code, runs locally</text>
    <rect x="260" y="150" width="14" height="14" rx="4" fill="none" stroke="{T}" stroke-width="2.5"/><text x="282" y="162">the model already running your agent</text>
  </g>"""
    body = f"""  <defs>
    <marker id="ah" viewBox="0 0 10 10" refX="8" refY="5" markerWidth="7" markerHeight="7" orient="auto-start-reverse"><path d="M0 0L10 5L0 10Z" fill="{p["muted"]}"/></marker>
  </defs>
{chr(10).join(boxes)}
{chr(10).join(arrows)}
{legend}"""
    return svg(
        w,
        h,
        body,
        "Data flow: video, ingest on your machine, your model writes, finish builds the document",
        p,
    )


def main(out):
    out = Path(out)
    out.mkdir(parents=True, exist_ok=True)
    for name, p in THEMES.items():
        (out / f"banner-{name}.svg").write_text(banner(p), encoding="utf-8")
        (out / f"flow-{name}.svg").write_text(flow(p), encoding="utf-8")
    (out / "logo.svg").write_text(logo(THEMES["dark"]), encoding="utf-8")
    print("wrote", *sorted(f.name for f in out.glob("*.svg")))


if __name__ == "__main__":
    main(sys.argv[1] if len(sys.argv) > 1 else "docs/assets")
