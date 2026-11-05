"""Simple SVG charts (no external libraries): lines with range bands and reference lines."""
from __future__ import annotations

import html

from .i18n import L


def line_chart(series: list[dict], w=900, h=260, y_label="", y_min=None, y_max=None, hlines=(), x_labels=None) -> str:
    """series: [{name, points: [(x, y, lo, hi)], cls}] – x as a number (e.g. days), lo/hi optional (band)."""
    pts_all = [p for s in series for p in s["points"] if p[1] is not None]
    if not pts_all:
        return f"<p class='muted'>{L('No data for this chart yet.', 'Noch keine Daten für dieses Diagramm.')}</p>"
    xs = [p[0] for p in pts_all]
    ys = [p[1] for p in pts_all] + [p[2] for p in pts_all if len(p) > 2 and p[2] is not None] + \
         [p[3] for p in pts_all if len(p) > 3 and p[3] is not None] + [v for v, _ in hlines]
    x0, x1 = min(xs), max(xs)
    if x1 == x0:
        x1 = x0 + 1
    lo = min(ys) if y_min is None else y_min
    hi = max(ys) if y_max is None else y_max
    if hi == lo:
        hi = lo + 1
    pad_l, pad_r, pad_t, pad_b = 46, 12, 12, 26
    X = lambda x: pad_l + (x - x0) / (x1 - x0) * (w - pad_l - pad_r)  # noqa: E731
    Y = lambda y: pad_t + (hi - y) / (hi - lo) * (h - pad_t - pad_b)  # noqa: E731
    out = [f"<svg viewBox='0 0 {w} {h}' class='chart' role='img' aria-label='{html.escape(y_label)}'>"]
    for i in range(5):
        v = lo + (hi - lo) * i / 4
        out.append(f"<line x1='{pad_l}' x2='{w - pad_r}' y1='{Y(v):.1f}' y2='{Y(v):.1f}' class='grid'/>"
                   f"<text x='{pad_l - 6}' y='{Y(v) + 4:.1f}' text-anchor='end'>{v:.{0 if hi - lo > 8 else 1}f}</text>")
    for v, label in hlines:
        out.append(f"<line x1='{pad_l}' x2='{w - pad_r}' y1='{Y(v):.1f}' y2='{Y(v):.1f}' class='ref'/>"
                   f"<text x='{w - pad_r - 4}' y='{Y(v) - 4:.1f}' text-anchor='end' class='reftxt'>{html.escape(label)}</text>")
    for k, s in enumerate(series):
        cls = s.get("cls", f"s{k + 1}")
        pts = [p for p in s["points"] if p[1] is not None]
        band = [p for p in pts if len(p) > 3 and p[2] is not None and p[3] is not None]
        if len(band) >= 2:
            poly = " ".join(f"{X(p[0]):.1f},{Y(p[3]):.1f}" for p in band) + " " + \
                   " ".join(f"{X(p[0]):.1f},{Y(p[2]):.1f}" for p in reversed(band))
            out.append(f"<polygon points='{poly}' class='band {cls}'/>")
        for p in band:
            out.append(f"<line x1='{X(p[0]):.1f}' x2='{X(p[0]):.1f}' y1='{Y(p[2]):.1f}' y2='{Y(p[3]):.1f}' class='err {cls}'/>")
        if len(pts) >= 2 and s.get("line", True):
            out.append(f"<polyline points='{' '.join(f'{X(p[0]):.1f},{Y(p[1]):.1f}' for p in pts)}' class='ln {cls}'/>")
        if s.get("dots", True):
            for p in pts:
                out.append(f"<circle cx='{X(p[0]):.1f}' cy='{Y(p[1]):.1f}' r='3' class='dot {cls}'/>")
    for x, label in (x_labels or []):
        if x0 <= x <= x1:
            out.append(f"<text x='{X(x):.1f}' y='{h - 6}' text-anchor='middle'>{html.escape(label)}</text>")
    legend = " ".join(f"<tspan class='lg s{k + 1} {s.get('cls', '')}'>● {html.escape(s['name'])}</tspan>"
                      for k, s in enumerate(series))
    out.append(f"<text x='{pad_l + 4}' y='{pad_t + 10}'>{legend}</text>")
    out.append("</svg>")
    return "".join(out)


CHART_CSS = """
svg.chart{width:100%;height:auto;display:block}
svg.chart text{fill:var(--muted,#888);font-size:11px}
svg.chart .grid{stroke:var(--line,#ddd);stroke-width:1}
svg.chart .ref{stroke:var(--bad,#d03b3b);stroke-dasharray:5 4;stroke-width:1.2}
svg.chart .reftxt{fill:var(--bad,#d03b3b)}
svg.chart .ln{fill:none;stroke-width:2}
svg.chart .band{opacity:.16;stroke:none}
svg.chart .err{stroke-width:1.2;opacity:.6}
svg.chart .s1{stroke:var(--series-1,#2a78d6);fill:var(--series-1,#2a78d6)}
svg.chart .ln.s1,svg.chart .ln.s2,svg.chart .ln.s3,svg.chart .ln.s4{fill:none}
svg.chart .s2{stroke:var(--series-2,#eb6834);fill:var(--series-2,#eb6834)}
svg.chart .s3{stroke:var(--series-3,#1baf7a);fill:var(--series-3,#1baf7a)}
svg.chart .s4{stroke:var(--series-4,#eda100);fill:var(--series-4,#eda100)}
svg.chart tspan.lg.s1{fill:var(--series-1,#2a78d6)}svg.chart tspan.lg.s2{fill:var(--series-2,#eb6834)}
svg.chart tspan.lg.s3{fill:var(--series-3,#1baf7a)}svg.chart tspan.lg.s4{fill:var(--series-4,#eda100)}
"""
