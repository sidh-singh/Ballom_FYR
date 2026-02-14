"""
dashboard.py — Live Dash / Plotly dashboard for Ballom_FYR.

Reads JSON state files from C:/Ballom_FYR/state/<mode>/ and displays:
  * Account balance, realized / unrealized P&L
  * Open positions table
  * SHA signal strength, power, list, crossover per symbol
  * Strategy decision log (rolling)

Launch:
    python dashboard.py               -> auto-detect mode, http://127.0.0.1:8050
    python dashboard.py demo          -> force demo mode
    python dashboard.py live          -> force live mode
    python dashboard.py demo 8060     -> demo mode on port 8060
"""

from __future__ import annotations

import json
import sys
from datetime import datetime
from pathlib import Path

import dash
from dash import dcc, html, dash_table
from dash.dependencies import Input, Output
import plotly.graph_objects as go

from constants import (
    STATE_DIR_DEMO,
    STATE_DIR_LIVE,
    STATE_DIR_BASE,
    DASHBOARD_PORT,
    DASHBOARD_REFRESH_MS,
    get_state_dir,
)


# ======================================================================
#  PREMIUM COLOR PALETTE
# ======================================================================

COLORS = {
    # Backgrounds
    "bg":             "#0a0e1a",
    "bg_secondary":   "#0f1423",
    "card":           "rgba(17, 22, 40, 0.85)",
    "card_solid":     "#111628",
    "card_border":    "rgba(99, 115, 171, 0.12)",
    # Text
    "text":           "#e8ecf4",
    "text_secondary": "#a3adc4",
    "text_dim":       "#5a6580",
    "text_muted":     "#3d4660",
    # Accents
    "accent":         "#7c6cf0",
    "accent_glow":    "rgba(124, 108, 240, 0.25)",
    "accent_soft":    "rgba(124, 108, 240, 0.12)",
    # Signals
    "positive":       "#00d2a0",
    "positive_soft":  "rgba(0, 210, 160, 0.12)",
    "positive_glow":  "rgba(0, 210, 160, 0.3)",
    "negative":       "#ff6b6b",
    "negative_soft":  "rgba(255, 107, 107, 0.12)",
    "negative_glow":  "rgba(255, 107, 107, 0.3)",
    "warning":        "#ffd93d",
    "neutral":        "#5a6580",
    # UI
    "divider":        "rgba(99, 115, 171, 0.1)",
    "chart_grid":     "rgba(99, 115, 171, 0.08)",
    "gradient_start": "#7c6cf0",
    "gradient_end":   "#00d2a0",
}

# Legacy aliases used in callback
BG      = COLORS["bg"]
CARD_BG = COLORS["card_solid"]
TEXT    = COLORS["text"]
ACCENT  = COLORS["positive"]
RED     = COLORS["negative"]
YELLOW  = COLORS["warning"]

# Shared glassmorphism card style
CARD_STYLE = {
    "background": COLORS["card"],
    "backdropFilter": "blur(20px)",
    "WebkitBackdropFilter": "blur(20px)",
    "border": f"1px solid {COLORS['card_border']}",
    "borderRadius": "16px",
    "padding": "20px 24px",
    "transition": "all 0.3s cubic-bezier(0.4, 0, 0.2, 1)",
}


# ======================================================================
#  MODE DETECTION
# ======================================================================

def _detect_active_mode() -> str:
    demo_file = STATE_DIR_DEMO / "app_status.json"
    live_file = STATE_DIR_LIVE / "app_status.json"
    demo_ts = demo_file.stat().st_mtime if demo_file.exists() else 0
    live_ts = live_file.stat().st_mtime if live_file.exists() else 0
    if live_ts > demo_ts:
        return "live"
    return "demo"


def _resolve_state_paths(mode: str) -> dict:
    d = get_state_dir(mode)
    return {
        "app_status":       d / "app_status.json",
        "signal_state":     d / "signal_state.json",
        "position_state":   d / "position_state.json",
        "account_state":    d / "account_state.json",
        "strategy_log":     d / "strategy_log.json",
        "position_tracker": d / "position_tracker.json",
        "profit_history":   d / "profit_history.json",
    }


_cli_args = sys.argv[1:]
_mode_arg = None
_port_arg = DASHBOARD_PORT

for arg in _cli_args:
    if arg.lower() in ("demo", "live"):
        _mode_arg = arg.lower()
    elif arg.isdigit():
        _port_arg = int(arg)

ACTIVE_MODE = _mode_arg or _detect_active_mode()
STATE_PATHS = _resolve_state_paths(ACTIVE_MODE)


# ======================================================================
#  HELPERS
# ======================================================================

def _read(path: Path):
    if not path.exists():
        return {}
    try:
        with open(path, "r") as f:
            return json.load(f)
    except Exception:
        return {}


def _kpi_card(title: str, value: str, color: str = None,
              icon: str = "", sub: str = "") -> html.Div:
    """Premium KPI card with glassmorphism and subtle glow."""
    color = color or COLORS["positive"]
    if color == COLORS["positive"]:
        glow = COLORS["positive_glow"]
        border_accent = "rgba(0, 210, 160, 0.25)"
    elif color == COLORS["negative"]:
        glow = COLORS["negative_glow"]
        border_accent = "rgba(255, 107, 107, 0.25)"
    elif color == COLORS["accent"]:
        glow = COLORS["accent_glow"]
        border_accent = "rgba(124, 108, 240, 0.25)"
    elif color == "#9b59b6":
        glow = "rgba(155, 89, 182, 0.3)"
        border_accent = "rgba(155, 89, 182, 0.25)"
    else:
        glow = COLORS["accent_glow"]
        border_accent = COLORS["card_border"]

    return html.Div(
        children=[
            html.Div(
                style={"display": "flex", "alignItems": "center", "marginBottom": "8px"},
                children=[
                    html.Span(icon, style={
                        "fontSize": "12px", "marginRight": "6px", "opacity": "0.7",
                    }) if icon else None,
                    html.Span(title, style={
                        "fontSize": "10px", "color": COLORS["text_dim"],
                        "textTransform": "uppercase", "letterSpacing": "1.2px",
                        "fontWeight": "600",
                    }),
                ],
            ),
            html.H3(value, style={
                "margin": 0, "color": color, "fontWeight": "700",
                "fontSize": "1.25rem",
                "fontFamily": "'JetBrains Mono', 'SF Mono', monospace",
                "letterSpacing": "-0.3px",
                "lineHeight": "1.2",
            }),
            html.Div(sub, style={
                "fontSize": "10px", "color": COLORS["text_dim"],
                "marginTop": "4px",
            }) if sub else None,
        ],
        style={
            "background": COLORS["card"],
            "backdropFilter": "blur(20px)",
            "WebkitBackdropFilter": "blur(20px)",
            "border": f"1px solid {border_accent}",
            "borderRadius": "14px",
            "padding": "16px 20px",
            "flex": "1",
            "minWidth": "155px",
            "boxShadow": f"0 4px 20px rgba(0,0,0,0.3), 0 0 30px {glow}",
            "transition": "all 0.3s cubic-bezier(0.4, 0, 0.2, 1)",
        },
    )


def _crossover_dots(cross_list: list, max_items: int = 7) -> html.Div:
    """Crossover as circle dots with green/red intensity shading + fade."""
    green_map = {1: "#4de8c8", 2: "#00d2a0", 3: "#009d7a"}
    red_map   = {1: "#ee7b6e", 2: "#e74c3c", 3: "#c0392b"}
    dots = []
    for i in range(max_items):
        v = cross_list[i] if i < len(cross_list) else 0
        if v > 0:
            c = green_map.get(min(abs(v), 3), "#4de8c8")
        elif v < 0:
            c = red_map.get(min(abs(v), 3), "#ee7b6e")
        else:
            c = "rgba(255,255,255,0.06)"
        opacity = max(0.35, 1.0 - (i * 0.09))
        dots.append(html.Span(style={
            "display": "inline-block", "width": "10px", "height": "10px",
            "borderRadius": "50%", "background": c,
            "marginRight": "3px", "opacity": str(opacity),
        }))
    return html.Div(dots, style={"display": "inline-flex", "alignItems": "center"})


def _cross_power_bar(cross_list: list, max_items: int = 7) -> html.Div:
    """Power bar for crossover -- segments colored per value intensity."""
    green_map = {1: "#4de8c8", 2: "#00d2a0", 3: "#009d7a"}
    red_map   = {1: "#ee7b6e", 2: "#e74c3c", 3: "#c0392b"}
    bull_count = sum(1 for v in cross_list[:max_items] if v > 0)
    segs = []
    for i in range(max_items):
        if i < len(cross_list):
            v = cross_list[i]
            if v > 0:
                c = green_map.get(v, "#81c784")
            elif v < 0:
                c = red_map.get(abs(v), "#e57373")
            else:
                c = "rgba(255,255,255,0.06)"
        else:
            c = "rgba(255,255,255,0.06)"
        segs.append(html.Span(style={
            "display": "inline-block", "width": "8px", "height": "16px",
            "borderRadius": "3px", "background": c, "marginRight": "2px",
        }))
    p_color = "#00d2a0" if bull_count >= 5 else "#f39c12" if bull_count >= 3 else "#e74c3c"
    return html.Div([
        *segs,
        html.Span(f" {bull_count}", style={
            "fontSize": "0.75rem", "fontWeight": "700", "marginLeft": "4px",
            "color": p_color if bull_count > 0 else COLORS["text_dim"],
            "fontFamily": "'JetBrains Mono', monospace",
        }),
    ], style={"display": "inline-flex", "alignItems": "center"})


def _power_bar(power: int, max_power: int = 7) -> html.Div:
    """Compact inline power gauge with colored segments."""
    dots = []
    for i in range(max_power):
        if i < power:
            c = "#00d2a0" if power >= 5 else "#f39c12" if power >= 3 else "#e74c3c"
        else:
            c = "rgba(255,255,255,0.06)"
        dots.append(html.Span(style={
            "display": "inline-block", "width": "8px", "height": "16px",
            "borderRadius": "3px", "background": c, "marginRight": "2px",
        }))
    p_color = "#00d2a0" if power >= 5 else "#f39c12" if power >= 3 else "#e74c3c"
    return html.Div(
        children=[*dots, html.Span(f" {power}", style={
            "fontSize": "0.75rem", "fontWeight": "700", "marginLeft": "4px",
            "color": p_color if power > 0 else COLORS["text_dim"],
            "fontFamily": "'JetBrains Mono', monospace",
        })],
        style={"display": "inline-flex", "alignItems": "center"},
    )


def _list_dots(lst: list, max_items: int = 7) -> html.Div:
    """Bullish/bearish list as colored circle dots with fade."""
    dots = []
    for i, v in enumerate(lst[:max_items]):
        c = "#00d2a0" if v == 1 else "#e74c3c"
        opacity = max(0.35, 1.0 - (i * 0.09))
        dots.append(html.Span(style={
            "display": "inline-block", "width": "10px", "height": "10px",
            "borderRadius": "50%", "background": c,
            "marginRight": "3px", "opacity": str(opacity),
        }))
    return html.Div(dots, style={"display": "inline-flex", "alignItems": "center"})


def _signal_row(label: str, icon: str, color: str,
                power: int, lst: list, cross_list: list) -> html.Div:
    """One compact row for CE / PE / IDX -- 4-column grid."""
    return html.Div(
        style={
            "display": "grid",
            "gridTemplateColumns": "64px 1fr 1fr 1fr",
            "gap": "8px", "alignItems": "center",
            "padding": "8px 0",
        },
        children=[
            html.Span(f"{icon} {label}", style={
                "fontWeight": "700", "fontSize": "0.82rem", "color": color,
            }),
            _power_bar(power),
            _list_dots(lst),
            _cross_power_bar(cross_list),
        ],
    )


def _sha_debug_table(label: str, color: str, sha_list: list) -> html.Div:
    """Compact SHA OHLC diagnostic table for one leg (CE / PE / IDX).
    *sha_list* is a list of dicts: [{ts, O, H, L, C, dir}, ...] most-recent first.
    """
    if not sha_list:
        return html.Div()

    mono = "'JetBrains Mono', monospace"
    hdr_style = {"fontSize": "0.58rem", "fontWeight": "700",
                 "color": COLORS["text_dim"], "padding": "2px 6px",
                 "letterSpacing": "0.5px"}
    cell_style = {"fontSize": "0.62rem", "fontFamily": mono,
                  "color": COLORS["text_secondary"], "padding": "1px 6px"}

    rows = [
        html.Tr([
            html.Th("#", style=hdr_style),
            html.Th("TIME", style=hdr_style),
            html.Th("OPEN", style=hdr_style),
            html.Th("HIGH", style=hdr_style),
            html.Th("LOW", style=hdr_style),
            html.Th("CLOSE", style=hdr_style),
            html.Th("", style=hdr_style),
        ])
    ]
    for i, c in enumerate(sha_list):
        is_bull = c.get("dir") == "BULL"
        dot_col = "#00d2a0" if is_bull else "#e74c3c"
        ts_short = c.get("ts", "")[-8:]  # just HH:MM:SS
        rows.append(html.Tr([
            html.Td(f"-{i+1}", style={**cell_style, "color": COLORS["text_muted"]}),
            html.Td(ts_short, style=cell_style),
            html.Td(c.get("O", ""), style=cell_style),
            html.Td(c.get("H", ""), style=cell_style),
            html.Td(c.get("L", ""), style=cell_style),
            html.Td(c.get("C", ""), style=cell_style),
            html.Td(html.Span(style={
                "display": "inline-block", "width": "7px", "height": "7px",
                "borderRadius": "50%", "background": dot_col,
            })),
        ]))

    return html.Div(style={"marginBottom": "6px"}, children=[
        html.Span(f"{label} SHA OHLC", style={
            "fontSize": "0.6rem", "fontWeight": "700", "color": color,
            "letterSpacing": "0.5px", "marginBottom": "2px", "display": "block"}),
        html.Table(rows, style={
            "width": "100%", "borderCollapse": "collapse",
            "background": "rgba(0,0,0,0.15)", "borderRadius": "6px"}),
    ])


def _popup_row(label: str, value: str) -> html.Div:
    """Single key-value row inside the detail popup."""
    return html.Div(className="popup-row", children=[
        html.Span(label, className="popup-label"),
        html.Span(value or "—", className="popup-value"),
    ])


def _action_badge(action: str) -> html.Span:
    """Premium colored pill badge with glow for a strategy action."""
    act_upper = action.upper()
    if "MARTINGALE" in act_upper:
        bg, fg, glow = "#9b59b6", "#f0e6f6", "rgba(155, 89, 182, 0.3)"
        icon = "\u26a1"
    elif "EXIT" in act_upper or "CLOSE" in act_upper or "ALL_CLOSED" in act_upper:
        if "REJECTED" in act_upper:
            bg, fg, glow = "#c0392b", "#f0e6e6", "rgba(192, 57, 43, 0.3)"
            icon = "\U0001f6a8"
        elif "CONFIRMED" in act_upper:
            bg, fg, glow = "#27ae60", "#e6f0ea", "rgba(39, 174, 96, 0.3)"
            icon = "\u2705"
        elif "SENT" in act_upper or "RETRY" in act_upper:
            bg, fg, glow = "#f39c12", "#3a2e12", "rgba(243, 156, 18, 0.3)"
            icon = "\u23f3"
        elif "PROFIT" in act_upper:
            bg, fg, glow = "#00d2a0", "#0d2f25", COLORS["positive_glow"]
            icon = "\U0001f4b0"
        elif "ADVERSE" in act_upper:
            bg, fg, glow = "#e67e22", "#3a2412", "rgba(230, 126, 34, 0.3)"
            icon = "\u26a0\ufe0f"
        elif "TREND_FLIP" in act_upper:
            bg, fg, glow = "#e74c3c", "#f0e6e6", "rgba(231, 76, 60, 0.3)"
            icon = "\U0001f504"
        else:
            bg, fg, glow = "#3498db", "#12283a", "rgba(52, 152, 219, 0.3)"
            icon = "\U0001f504"
    elif "BUY" in act_upper:
        bg, fg, glow = "#00d2a0", "#0d2f25", COLORS["positive_glow"]
        icon = "\U0001f7e2"
    elif "SELL" in act_upper:
        bg, fg, glow = "#ff6b6b", "#3a1212", COLORS["negative_glow"]
        icon = "\U0001f534"
    elif "SKIP" in act_upper:
        bg, fg, glow = "#e67e22", "#3a2412", "rgba(230, 126, 34, 0.3)"
        icon = "\u23ed\ufe0f"
    elif "NO_PAIRS" in act_upper:
        bg, fg, glow = "#c0392b", "#f0e6e6", "rgba(192, 57, 43, 0.3)"
        icon = "\u274c"
    elif "FAIL" in act_upper or "ERROR" in act_upper:
        bg, fg, glow = "#c0392b", "#f0e6e6", "rgba(192, 57, 43, 0.3)"
        icon = "\U0001f6a8"
    elif "LOADED" in act_upper or "FOUND" in act_upper:
        bg, fg, glow = "#27ae60", "#e6f0ea", "rgba(39, 174, 96, 0.3)"
        icon = "\u2705"
    elif "ANALYSIS" in act_upper or "EVAL" in act_upper:
        bg, fg, glow = "#34495e", "#bdc3c7", "rgba(52, 73, 94, 0.3)"
        icon = "\U0001f50d"
    elif "BLOCKED" in act_upper or "BRAKE" in act_upper:
        bg, fg, glow = "#7f8c8d", "#ecf0f1", "rgba(127, 140, 141, 0.3)"
        icon = "\U0001f6ab"
    elif "LOCKED" in act_upper or "CLEARED" in act_upper or "OVERNIGHT" in act_upper:
        bg, fg, glow = "#2980b9", "#e6f0f6", "rgba(41, 128, 185, 0.3)"
        icon = "\U0001f512" if "LOCKED" in act_upper else "\U0001f513"
    else:
        bg, fg, glow = "#2c3e50", "#bdc3c7", "rgba(44, 62, 80, 0.3)"
        icon = "\U0001f4cc"

    return html.Span(f"{icon} {action}", style={
        "background": f"linear-gradient(135deg, {bg}, {bg}dd)",
        "color": fg,
        "padding": "3px 12px", "borderRadius": "20px",
        "fontSize": "0.72rem", "fontWeight": "700",
        "whiteSpace": "nowrap",
        "letterSpacing": "0.5px",
        "boxShadow": f"0 0 12px {glow}, 0 2px 6px rgba(0,0,0,0.25)",
        "textShadow": "0 1px 2px rgba(0,0,0,0.2)",
        "display": "inline-block",
    })


def _get_available_chart_dates(history_data: list) -> list[str]:
    """Return up to 7 most recent dates that have chart-worthy data."""
    chart_actions = ("SNAPSHOT", "CLOSE", "MARTINGALE", "ENTRY")
    dates = sorted(
        {e.get("date") for e in history_data
         if e.get("date") and e.get("action") in chart_actions},
        reverse=True,
    )
    return dates[:7]


def _load_demo_trade_history() -> list:
    """
    Load append-only demo trade history and convert to profit_history
    format so the chart can display demo trading days even when
    profit_history.json was wiped by a day-change reset.

    Each DemoTrade has: trade_id, symbol, side, qty, price,
    product_type, timestamp, order_type (ENTRY/EXIT/PARTIAL_EXIT), pnl.
    """
    demo_history_file = Path("C:/Ballom_FYR/demo/demo_trade_history.json")
    if not demo_history_file.exists():
        return []
    try:
        with open(demo_history_file, "r") as f:
            trades = json.load(f)
    except Exception:
        return []
    if not isinstance(trades, list):
        return []

    # Convert demo trades to chart-compatible entries
    entries = []
    cumulative_pnl: dict = {}  # symbol -> running pnl
    for t in trades:
        sym = t.get("symbol", "")
        ts = t.get("timestamp", "")
        trade_date = ts[:10] if len(ts) >= 10 else ""
        pnl = t.get("pnl", 0.0)
        order_type = t.get("order_type", "")

        if not sym or not trade_date:
            continue

        # Track cumulative P&L per symbol per day
        day_key = f"{sym}_{trade_date}"
        cumulative_pnl[day_key] = cumulative_pnl.get(day_key, 0.0) + pnl

        if order_type == "ENTRY":
            action = "ENTRY"
        elif order_type in ("EXIT", "PARTIAL_EXIT"):
            action = "CLOSE"
        else:
            action = "SNAPSHOT"

        # Format timestamp for chart x-axis
        display_ts = ts[:19].replace("T", " ") if "T" in ts else ts[:19]

        entries.append({
            "timestamp": display_ts,
            "date": trade_date,
            "symbol": sym,
            "effective_pl": round(cumulative_pnl[day_key], 2),
            "api_total_pl": round(cumulative_pnl[day_key], 2),
            "booked_profit": 0.0,
            "qty": t.get("qty", 0),
            "action": action,
        })
    return entries


def _build_profit_chart(
    history_data: list,
    selected_date: str | None = None,
    mode: str = "demo",
) -> go.Figure:
    """Build a premium Plotly line chart of effective P&L for one day."""
    empty_layout = dict(
        template="plotly_dark",
        paper_bgcolor="rgba(0,0,0,0)",
        plot_bgcolor="rgba(0,0,0,0)",
        height=320,
        margin=dict(l=50, r=20, t=10, b=40),
        font=dict(
            color=COLORS["text_secondary"],
            size=11,
            family="'Inter', sans-serif",
        ),
    )

    if not history_data:
        fig = go.Figure()
        fig.update_layout(**empty_layout)
        fig.add_annotation(text="No profit data yet", showarrow=False,
                           font=dict(size=14, color=COLORS["text_dim"]),
                           xref="paper", yref="paper", x=0.5, y=0.5)
        return fig

    chart_actions = ("SNAPSHOT", "CLOSE", "MARTINGALE", "ENTRY")
    today = datetime.now().strftime("%Y-%m-%d")

    # ── LIVE mode: today only, no date selection ──────────────────────────
    if mode == "live":
        today_data = [
            e for e in history_data
            if e.get("date") == today
            and e.get("action") in chart_actions
        ]
        if not today_data:
            fig = go.Figure()
            fig.update_layout(**empty_layout)
            fig.add_annotation(text="No data for today yet", showarrow=False,
                               font=dict(size=14, color=COLORS["text_dim"]),
                               xref="paper", yref="paper", x=0.5, y=0.5)
            return fig
        chart_date = today
        showing_past = False
    else:
        # ── DEMO mode: try profit_history, fallback to demo_trade_history ─
        # Merge demo trade history as fallback for dates missing from
        # profit_history (which used to be wiped on day change).
        demo_fallback = _load_demo_trade_history()
        # Dates already covered by profit_history
        existing_dates = {e.get("date") for e in history_data
                          if e.get("date") and e.get("action") in chart_actions}
        # Only add fallback entries for dates NOT already in profit_history
        for entry in demo_fallback:
            if entry.get("date") not in existing_dates:
                history_data.append(entry)

        # Use the selected date, or default to today / most recent
        available = _get_available_chart_dates(history_data)
        if selected_date and selected_date in available:
            chart_date = selected_date
        elif today in available:
            chart_date = today
        elif available:
            chart_date = available[0]
        else:
            chart_date = None

        if not chart_date:
            fig = go.Figure()
            fig.update_layout(**empty_layout)
            fig.add_annotation(text="No data for today yet", showarrow=False,
                               font=dict(size=14, color=COLORS["text_dim"]),
                               xref="paper", yref="paper", x=0.5, y=0.5)
            return fig

        today_data = [
            e for e in history_data
            if e.get("date") == chart_date
            and e.get("action") in chart_actions
        ]
        showing_past = chart_date != today

    symbols: dict = {}
    for entry in today_data:
        sym = entry.get("symbol", "")
        if sym not in symbols:
            symbols[sym] = {
                "x": [], "y": [],
                "close_x": [], "close_y": [],
                "mg_x": [], "mg_y": [],
            }
        symbols[sym]["x"].append(entry["timestamp"])
        symbols[sym]["y"].append(entry.get("effective_pl", 0))
        if entry["action"] == "CLOSE":
            symbols[sym]["close_x"].append(entry["timestamp"])
            symbols[sym]["close_y"].append(entry.get("effective_pl", 0))
        elif entry["action"] == "MARTINGALE":
            symbols[sym]["mg_x"].append(entry["timestamp"])
            symbols[sym]["mg_y"].append(entry.get("effective_pl", 0))

    fig = go.Figure()
    chart_colors = ["#00d2a0", "#ff6b6b", "#ffd93d", "#7c6cf0", "#5dade2", "#e74c3c"]

    for i, (sym, data) in enumerate(symbols.items()):
        color = chart_colors[i % len(chart_colors)]
        short_name = sym.split(":")[-1] if ":" in sym else sym
        r, g, b = int(color[1:3], 16), int(color[3:5], 16), int(color[5:7], 16)
        fill_color = f"rgba({r},{g},{b},0.06)"

        # Build custom data for rich hover
        actions = []
        for ts in data["x"]:
            for e in today_data:
                if e["timestamp"] == ts and e.get("symbol") == sym:
                    actions.append(e.get("action", "—"))
                    break
            else:
                actions.append("—")

        fig.add_trace(go.Scatter(
            x=data["x"], y=data["y"],
            mode="lines+markers", name=short_name,
            line=dict(color=color, width=2.5, shape="spline"),
            marker=dict(size=4, color=color, opacity=0.6),
            fill="tozeroy", fillcolor=fill_color,
            customdata=actions,
            hovertemplate=(
                "<b>%{fullData.name}</b><br>"
                "Time: %{x}<br>"
                "P&L: ₹%{y:,.2f}<br>"
                "Action: %{customdata}"
                "<extra></extra>"
            ),
        ))
        if data["close_x"]:
            fig.add_trace(go.Scatter(
                x=data["close_x"], y=data["close_y"],
                mode="markers", name=f"{short_name} close",
                marker=dict(color=color, size=10, symbol="star",
                            line=dict(width=2, color=COLORS["bg"])),
                hovertemplate=(
                    "<b>⭐ CLOSE — %{fullData.name}</b><br>"
                    "Time: %{x}<br>"
                    "Booked P&L: ₹%{y:,.2f}"
                    "<extra></extra>"
                ),
                showlegend=False,
            ))
        if data["mg_x"]:
            fig.add_trace(go.Scatter(
                x=data["mg_x"], y=data["mg_y"],
                mode="markers", name=f"{short_name} martingale",
                marker=dict(color="#9b59b6", size=9, symbol="diamond",
                            line=dict(width=2, color=COLORS["bg"])),
                hovertemplate=(
                    "<b>⚡ MARTINGALE — %{fullData.name}</b><br>"
                    "Time: %{x}<br>"
                    "P&L at entry: ₹%{y:,.2f}"
                    "<extra></extra>"
                ),
                showlegend=False,
            ))

    fig.add_hline(y=0, line_dash="dot", line_color=COLORS["text_muted"], opacity=0.5)

    # Show date label when displaying a past day's data
    if showing_past:
        fig.add_annotation(
            text=f"Showing: {chart_date}",
            showarrow=False,
            font=dict(size=11, color=COLORS["accent"]),
            xref="paper", yref="paper", x=0.0, y=1.05,
            xanchor="left",
        )

    fig.update_layout(
        **empty_layout,
        hovermode="closest",
        hoverlabel=dict(
            bgcolor="rgba(17, 22, 40, 0.95)",
            bordercolor=COLORS["accent"],
            font=dict(family="'Inter', sans-serif", size=12,
                      color=COLORS["text"]),
        ),
        xaxis=dict(
            title="Time", showgrid=False,
            tickfont=dict(size=10, color=COLORS["text_dim"]),
            rangeslider=dict(visible=True, bgcolor=COLORS["bg_secondary"],
                             bordercolor=COLORS["card_border"], thickness=0.06),
            showspikes=True, spikemode="across", spikesnap="cursor",
            spikecolor=COLORS["accent"], spikethickness=1, spikedash="dot",
        ),
        yaxis=dict(
            title="P&L (₹)", showgrid=True,
            gridcolor=COLORS["chart_grid"], gridwidth=0.5,
            zeroline=True, zerolinecolor=COLORS["text_muted"],
            zerolinewidth=0.5, tickprefix="₹",
            tickfont=dict(size=10, color=COLORS["text_dim"]),
            fixedrange=False,
            showspikes=True, spikemode="across", spikesnap="cursor",
            spikecolor=COLORS["accent"], spikethickness=1, spikedash="dot",
        ),
        legend=dict(orientation="h", y=-0.25,
                    font=dict(size=10, color=COLORS["text_dim"])),
        dragmode="zoom",
    )
    return fig


# ======================================================================
#  DASH APP
# ======================================================================

app = dash.Dash(
    __name__,
    title="Ballom FYR \u2014 Dashboard",
    update_title=None,
    suppress_callback_exceptions=True,
)

# Custom HTML with premium Google Fonts, animations, scrollbar
app.index_string = """<!DOCTYPE html>
<html>
<head>
{%metas%}
<title>{%title%}</title>
{%favicon%}
{%css%}
<link rel="preconnect" href="https://fonts.googleapis.com">
<link rel="preconnect" href="https://fonts.gstatic.com" crossorigin>
<link href="https://fonts.googleapis.com/css2?family=Inter:wght@300;400;500;600;700;800;900&family=JetBrains+Mono:wght@400;500;600;700&display=swap" rel="stylesheet">
<style>
    *, *::before, *::after { box-sizing: border-box; }
    body {
        margin: 0; padding: 0; background: #0a0e1a;
        font-family: 'Inter', -apple-system, BlinkMacSystemFont, 'Segoe UI', sans-serif;
        -webkit-font-smoothing: antialiased;
        -moz-osx-font-smoothing: grayscale;
    }
    ._dash-loading-callback, .dash-loading, ._dash-loading,
    div._dash-loading-callback--is-loading { visibility: hidden !important; }
    ::-webkit-scrollbar { width: 6px; height: 6px; }
    ::-webkit-scrollbar-track { background: transparent; }
    ::-webkit-scrollbar-thumb { background: rgba(124, 108, 240, 0.25); border-radius: 10px; }
    ::-webkit-scrollbar-thumb:hover { background: rgba(124, 108, 240, 0.45); }

    @keyframes fadeIn {
        from { opacity: 0; transform: translateY(8px); }
        to   { opacity: 1; transform: translateY(0); }
    }
    @keyframes shimmer {
        0%   { background-position: -200% 0; }
        100% { background-position: 200% 0; }
    }
    @keyframes liveDot {
        0%, 100% { opacity: 0.5; transform: scale(0.9); }
        50%      { opacity: 1;   transform: scale(1.15); }
    }
    .fade-in { animation: fadeIn 0.4s cubic-bezier(0.4, 0, 0.2, 1); }
    .gradient-bar {
        height: 3px;
        background: linear-gradient(90deg, #7c6cf0, #00d2a0, #ffd93d, #ff6b6b, #7c6cf0);
        background-size: 300% auto;
        animation: shimmer 6s linear infinite;
    }
    ::selection { background: rgba(124, 108, 240, 0.3); color: #e8ecf4; }
    .plotly .hoverlayer .hovertext { font-family: 'Inter', sans-serif !important; }

    .dash-spreadsheet-container .dash-spreadsheet-inner th {
        font-family: 'Inter', sans-serif !important; letter-spacing: 0.5px !important;
    }
    .dash-spreadsheet-container .dash-spreadsheet-inner td {
        font-family: 'JetBrains Mono', monospace !important;
    }
    .Select-control { background: #111628 !important; border-color: rgba(99,115,171,0.2) !important; border-radius: 10px !important; }
    .Select-menu-outer { background: #111628 !important; border-color: rgba(99,115,171,0.2) !important; border-radius: 10px !important; }
    .Select-option.is-focused { background: rgba(124,108,240,0.15) !important; }
    .Select-value-label { color: #e8ecf4 !important; }
    /* Dash dropdown — dark theme for date selector */
    #profit-date-selector,
    #profit-date-selector * { box-sizing: border-box; }
    #profit-date-selector .Select-control,
    #profit-date-selector > div { background: #0f1423 !important; border-color: rgba(99,115,171,0.25) !important; }
    #profit-date-selector .Select-value-label,
    #profit-date-selector .Select-placeholder,
    #profit-date-selector span[class*="value"],
    #profit-date-selector div[class*="singleValue"],
    #profit-date-selector div[class*="SingleValue"],
    #profit-date-selector div[class*="placeholder"] { color: #e8ecf4 !important; font-weight: 600 !important; font-size: 13px !important; }
    #profit-date-selector .Select-input > input,
    #profit-date-selector input { color: #e8ecf4 !important; }
    #profit-date-selector .Select-menu-outer,
    #profit-date-selector div[class*="menu"] { background: #0f1423 !important; border-color: rgba(99,115,171,0.25) !important; }
    #profit-date-selector .Select-option,
    #profit-date-selector div[class*="option"] { color: #e8ecf4 !important; background: transparent !important; }
    #profit-date-selector .Select-option.is-focused,
    #profit-date-selector div[class*="option"]:hover { background: rgba(124,108,240,0.25) !important; }
    #profit-date-selector .Select-arrow { border-color: #a3adc4 transparent transparent !important; }
    #profit-date-selector svg { fill: #a3adc4 !important; }
    /* Force the outer wrapper to also be dark */
    #profit-date-selector { background: #0f1423 !important; border-radius: 10px !important; }

    /* Strategy Log — detail popup on hover / tap */
    .log-entry-wrapper {
        position: relative;
        cursor: pointer;
        outline: none;
        border-radius: 8px;
        transition: background 0.2s ease;
    }
    .log-entry-wrapper:hover,
    .log-entry-wrapper:focus-within {
        background: rgba(124, 108, 240, 0.06);
    }
    .log-detail-popup {
        display: none;
        position: absolute;
        left: 0;
        top: 100%;
        width: 100%;
        max-height: 380px;
        overflow-y: auto;
        background: #141929;
        border: 1px solid rgba(124, 108, 240, 0.30);
        border-radius: 14px;
        padding: 16px 18px;
        box-shadow: 0 8px 40px rgba(0,0,0,0.55), 0 0 20px rgba(124,108,240,0.15);
        z-index: 9999;
        font-family: 'Inter', sans-serif;
        animation: popIn 0.18s cubic-bezier(0.4, 0, 0.2, 1);
    }
    /* Desktop: show on hover */
    .log-entry-wrapper:hover .log-detail-popup {
        display: block;
    }
    /* Touch/mobile: show on tap (focus) */
    .log-entry-wrapper:focus-within .log-detail-popup {
        display: block;
    }
    @media (max-width: 900px) {
        .log-detail-popup {
            width: 100%;
        }
    }
    @keyframes popIn {
        from { opacity: 0; transform: translateX(8px) scale(0.97); }
        to   { opacity: 1; transform: translateX(0) scale(1); }
    }
    .log-detail-popup .popup-header {
        font-size: 0.72rem; font-weight: 700; letter-spacing: 1.5px;
        color: #7c6cf0; text-transform: uppercase; margin-bottom: 10px;
        border-bottom: 1px solid rgba(99,115,171,0.15); padding-bottom: 8px;
    }
    .log-detail-popup .popup-row {
        display: flex; justify-content: space-between; align-items: flex-start;
        padding: 5px 0; border-bottom: 1px solid rgba(99,115,171,0.06);
    }
    .log-detail-popup .popup-row:last-child { border-bottom: none; }
    .log-detail-popup .popup-label {
        font-size: 0.65rem; font-weight: 600; color: #5a6580;
        letter-spacing: 0.5px; text-transform: uppercase; min-width: 70px;
        flex-shrink: 0;
    }
    .log-detail-popup .popup-value {
        font-size: 0.75rem; color: #e8ecf4;
        font-family: 'JetBrains Mono', monospace;
        text-align: right; word-break: break-all; max-width: 230px;
    }
    .log-detail-popup .popup-details-block {
        margin-top: 8px; padding: 10px 12px;
        background: rgba(10, 14, 26, 0.6); border-radius: 8px;
        font-size: 0.7rem; color: #a3adc4; line-height: 1.55;
        font-family: 'JetBrains Mono', monospace;
        word-break: break-word; white-space: pre-wrap;
    }
</style>
</head>
<body>
{%app_entry%}
<footer>{%config%}{%scripts%}{%renderer%}</footer>
</body>
</html>
"""


app.layout = html.Div(
    style={
        "fontFamily": "'Inter', -apple-system, BlinkMacSystemFont, 'Segoe UI', sans-serif",
        "backgroundColor": COLORS["bg"],
        "color": COLORS["text"],
        "minHeight": "100vh",
    },
    children=[
        # Animated gradient top accent bar
        html.Div(className="gradient-bar"),

        # Glassmorphism Header
        html.Div(
            style={
                "display": "flex", "justifyContent": "space-between",
                "alignItems": "center", "padding": "16px 36px",
                "background": "rgba(10, 14, 26, 0.95)",
                "backdropFilter": "blur(20px)",
                "WebkitBackdropFilter": "blur(20px)",
                "borderBottom": f"1px solid {COLORS['divider']}",
            },
            children=[
                # Logo
                html.Div(
                    style={"display": "flex", "alignItems": "center"},
                    children=[
                        html.Div(style={
                            "width": "36px", "height": "36px", "borderRadius": "10px",
                            "background": f"linear-gradient(135deg, {COLORS['gradient_start']}, {COLORS['gradient_end']})",
                            "boxShadow": f"0 4px 18px {COLORS['accent_glow']}",
                            "marginRight": "16px",
                        }),
                        html.Div([
                            html.Span("BALLOM FYR", style={
                                "fontSize": "18px", "fontWeight": "800",
                                "letterSpacing": "3px",
                                "background": f"linear-gradient(135deg, {COLORS['text']}, {COLORS['accent']})",
                                "WebkitBackgroundClip": "text",
                                "WebkitTextFillColor": "transparent",
                            }),
                            html.Div("Trading Dashboard", style={
                                "fontSize": "10px", "color": COLORS["text_dim"],
                                "letterSpacing": "2px", "textTransform": "uppercase",
                                "marginTop": "1px",
                            }),
                        ]),
                    ],
                ),
                # Center -- status badges + selector
                html.Div(
                    style={"display": "flex", "alignItems": "center", "gap": "12px"},
                    children=[
                        html.Div(style={
                            "width": "8px", "height": "8px", "borderRadius": "50%",
                            "background": COLORS["positive"],
                            "boxShadow": f"0 0 10px {COLORS['positive_glow']}",
                            "animation": "liveDot 2s ease-in-out infinite",
                        }),
                        html.Div(id="app-status-badge", style={
                            "background": f"linear-gradient(135deg, {COLORS['positive']}, {COLORS['positive']}dd)",
                            "color": "#fff", "padding": "5px 18px", "borderRadius": "24px",
                            "fontSize": "11px", "fontWeight": "700", "letterSpacing": "1px",
                            "boxShadow": f"0 0 16px {COLORS['positive_glow']}, 0 2px 8px rgba(0,0,0,0.3)",
                        }),
                        html.Div(id="app-mode-badge", style={
                            "background": f"linear-gradient(135deg, {COLORS['warning']}, {COLORS['warning']}dd)",
                            "color": "#000", "padding": "5px 18px", "borderRadius": "24px",
                            "fontSize": "11px", "fontWeight": "700", "letterSpacing": "1px",
                            "boxShadow": "0 0 16px rgba(255,217,61,0.25), 0 2px 8px rgba(0,0,0,0.3)",
                        }),
                        dcc.Dropdown(
                            id="mode-selector",
                            options=[
                                {"label": "\U0001f3af DEMO", "value": "demo"},
                                {"label": "\u26a0\ufe0f  LIVE", "value": "live"},
                            ],
                            value=ACTIVE_MODE, clearable=False,
                            style={"width": "140px", "fontSize": "0.82rem"},
                        ),
                    ],
                ),
                # Right -- timestamp
                html.Div(id="last-updated", style={
                    "fontSize": "11px", "color": COLORS["text_dim"],
                    "fontFamily": "'JetBrains Mono', monospace", "fontWeight": "400",
                }),
            ],
        ),

        # Main content
        html.Div(
            style={"padding": "28px 36px 48px 36px", "maxWidth": "1400px", "margin": "0 auto"},
            className="fade-in",
            children=[
                html.Div(id="kpi-row", style={
                    "display": "flex", "gap": "14px", "flexWrap": "wrap", "marginBottom": "24px"}),
                html.Div(id="daily-stats-row", style={
                    "display": "flex", "gap": "14px", "flexWrap": "wrap", "marginBottom": "24px"}),

                # Profit history chart
                html.Div(style={**CARD_STYLE, "marginBottom": "24px"}, children=[
                    html.Div(style={"display": "flex", "alignItems": "center",
                                    "justifyContent": "space-between",
                                    "marginBottom": "12px"}, children=[
                        html.Div(style={"display": "flex", "alignItems": "center",
                                        "gap": "8px"}, children=[
                            html.Span("\U0001f4c8", style={"fontSize": "16px"}),
                            html.Span("Profit History", style={
                                "fontSize": "15px", "fontWeight": "600",
                                "color": COLORS["text"], "letterSpacing": "0.3px"}),
                        ]),
                        dcc.Dropdown(
                            id="profit-date-selector",
                            placeholder="Select date…",
                            clearable=False,
                            style={
                                "width": "180px",
                                "backgroundColor": COLORS["bg_secondary"],
                                "color": COLORS["text"],
                                "border": f"1px solid {COLORS['card_border']}",
                                "borderRadius": "10px",
                                "fontSize": "13px",
                            },
                        ),
                    ]),
                    dcc.Graph(id="profit-chart", config={
                        "displayModeBar": True,
                        "displaylogo": False,
                        "scrollZoom": True,
                        "modeBarButtonsToRemove": ["lasso2d", "select2d"],
                    }),
                ]),

                # Two-column layout
                html.Div(style={"display": "flex", "gap": "24px", "flexWrap": "wrap"}, children=[
                    # LEFT: positions + signals
                    html.Div(style={"flex": "2", "minWidth": "500px"}, children=[
                        html.Div(style={"display": "flex", "alignItems": "center",
                                        "gap": "8px", "marginBottom": "12px"}, children=[
                            html.Span("\U0001f4cb", style={"fontSize": "16px"}),
                            html.Span("Open Positions", style={
                                "fontSize": "15px", "fontWeight": "600",
                                "color": COLORS["text"], "letterSpacing": "0.3px"}),
                        ]),
                        html.Div(id="positions-table-container"),
                        html.Div(style={"display": "flex", "alignItems": "center",
                                        "gap": "8px", "marginTop": "28px", "marginBottom": "12px"}, children=[
                            html.Span("\U0001f3af", style={"fontSize": "16px"}),
                            html.Span("SHA Signal Analysis", style={
                                "fontSize": "15px", "fontWeight": "600",
                                "color": COLORS["text"], "letterSpacing": "0.3px"}),
                        ]),
                        html.Div(id="signal-cards-container"),
                    ]),
                    # RIGHT: strategy log
                    html.Div(style={"flex": "1", "minWidth": "380px"}, children=[
                        html.Div(style={"display": "flex", "alignItems": "center",
                                        "gap": "8px", "marginBottom": "12px"}, children=[
                            html.Span("\U0001f4dd", style={"fontSize": "16px"}),
                            html.Span("Strategy Log", style={
                                "fontSize": "15px", "fontWeight": "600",
                                "color": COLORS["text"], "letterSpacing": "0.3px"}),
                        ]),
                        html.Div(id="strategy-log-container", style={
                            **CARD_STYLE, "maxHeight": "640px", "overflowY": "auto"}),
                    ]),
                ]),
            ],
        ),

        # Footer
        html.Div(style={"padding": "0 36px 20px"}, children=[
            html.Div(style={
                "height": "1px",
                "background": f"linear-gradient(90deg, transparent, {COLORS['divider']}, transparent)",
                "marginBottom": "16px",
            }),
            html.Div("Ballom FYR Trading System", style={
                "textAlign": "center", "fontSize": "10px",
                "color": COLORS["text_muted"],
                "letterSpacing": "2px", "textTransform": "uppercase",
            }),
        ]),

        dcc.Interval(id="refresh-timer", interval=DASHBOARD_REFRESH_MS, n_intervals=0),
    ],
)


# ======================================================================
#  CALLBACKS
# ======================================================================

@app.callback(
    [
        Output("app-status-badge", "children"),
        Output("app-mode-badge", "children"),
        Output("last-updated", "children"),
        Output("kpi-row", "children"),
        Output("positions-table-container", "children"),
        Output("signal-cards-container", "children"),
        Output("strategy-log-container", "children"),
        Output("daily-stats-row", "children"),
        Output("profit-chart", "figure"),
        Output("profit-date-selector", "options"),
        Output("profit-date-selector", "value"),
    ],
    [Input("refresh-timer", "n_intervals"),
     Input("mode-selector", "value"),
     Input("profit-date-selector", "value")],
)
def refresh_dashboard(_n, selected_mode, selected_chart_date):
    paths = _resolve_state_paths(selected_mode or ACTIVE_MODE)

    app_data = _read(paths["app_status"])
    acct_data = _read(paths["account_state"])
    pos_data = _read(paths["position_state"])
    sig_data = _read(paths["signal_state"])
    log_data = _read(paths["strategy_log"])
    tracker_data = _read(paths["position_tracker"])
    history_raw = _read(paths["profit_history"])
    history_data = history_raw if isinstance(history_raw, list) else []

    # Header badges
    status_text = app_data.get("status", "offline").upper()
    mode_text = (selected_mode or ACTIVE_MODE).upper()
    last_ts = acct_data.get("timestamp", app_data.get("timestamp", "\u2014"))

    # KPI cards
    balance = acct_data.get("balance", 0)
    realized = acct_data.get("realized_pnl", 0)
    unrealized = acct_data.get("unrealized_pnl", 0)
    total_pnl = acct_data.get("total_pnl", 0)
    win_rate = acct_data.get("win_rate", 0)
    total_trades = acct_data.get("total_trades", 0)

    pnl_color = COLORS["positive"] if total_pnl >= 0 else COLORS["negative"]
    real_color = COLORS["positive"] if realized >= 0 else COLORS["negative"]
    unreal_color = COLORS["positive"] if unrealized >= 0 else COLORS["negative"]

    kpi_cards = [
        _kpi_card("Balance", f"\u20b9{balance:,.2f}", COLORS["accent"], icon="\U0001f48e"),
        _kpi_card("Realized P&L", f"\u20b9{realized:,.2f}", real_color, icon="\u2705"),
        _kpi_card("Unrealized P&L", f"\u20b9{unrealized:,.2f}", unreal_color, icon="\U0001f4ca"),
        _kpi_card("Total P&L", f"\u20b9{total_pnl:,.2f}", pnl_color, icon="\U0001f4b0"),
        _kpi_card("Win Rate", f"{win_rate:.1f}%",
                  COLORS["positive"] if win_rate > 50 else COLORS["negative"],
                  icon="\U0001f3af"),
        _kpi_card("Total Trades", str(total_trades), COLORS["accent"], icon="\U0001f4c8"),
    ]

    # Positions table
    positions = pos_data.get("positions", [])
    if positions:
        cols = ["symbol", "netQty", "netAvg", "ltp", "realized_profit", "unrealized_profit", "productType"]
        rows = [{c: p.get(c, "") for c in cols} for p in positions]
        pos_table = dash_table.DataTable(
            data=rows,
            columns=[
                {"name": "Symbol", "id": "symbol"},
                {"name": "Qty", "id": "netQty"},
                {"name": "Avg Price", "id": "netAvg"},
                {"name": "LTP", "id": "ltp"},
                {"name": "Realized P&L", "id": "realized_profit"},
                {"name": "Unrealized P&L", "id": "unrealized_profit"},
                {"name": "Product", "id": "productType"},
            ],
            style_table={"overflowX": "auto", "borderRadius": "12px"},
            style_header={
                "backgroundColor": COLORS["card_solid"],
                "color": COLORS["text_dim"],
                "fontWeight": "600", "border": "none",
                "fontSize": "11px", "letterSpacing": "0.8px",
                "textTransform": "uppercase", "padding": "12px 16px",
                "borderBottom": f"1px solid {COLORS['divider']}",
            },
            style_cell={
                "backgroundColor": COLORS["card_solid"],
                "color": COLORS["text"],
                "border": f"1px solid {COLORS['divider']}",
                "padding": "12px 16px", "fontSize": "0.85rem",
                "fontFamily": "'JetBrains Mono', monospace",
            },
            style_data_conditional=[
                {"if": {"filter_query": "{realized_profit} > 0",
                        "column_id": "realized_profit"},
                 "color": COLORS["positive"], "fontWeight": "bold"},
                {"if": {"filter_query": "{realized_profit} < 0",
                        "column_id": "realized_profit"},
                 "color": COLORS["negative"], "fontWeight": "bold"},
                {"if": {"filter_query": "{unrealized_profit} > 0",
                        "column_id": "unrealized_profit"},
                 "color": COLORS["positive"], "fontWeight": "bold"},
                {"if": {"filter_query": "{unrealized_profit} < 0",
                        "column_id": "unrealized_profit"},
                 "color": COLORS["negative"], "fontWeight": "bold"},
                {"if": {"state": "active"},
                 "backgroundColor": COLORS["accent_soft"],
                 "border": f"1px solid {COLORS['accent']}"},
            ],
        )
        pos_table = html.Div(pos_table, style={
            **CARD_STYLE, "padding": "0", "overflow": "hidden"})
    else:
        pos_table = html.Div(children=[
            html.Div("\U0001f4ed", style={"fontSize": "28px", "marginBottom": "8px", "opacity": "0.5"}),
            html.Div("No open positions", style={"fontSize": "13px", "color": COLORS["text_dim"]}),
        ], style={**CARD_STYLE, "textAlign": "center", "padding": "32px"})

    # Signal cards
    signal_cards = []
    if isinstance(sig_data, dict):
        for sym_key, sig in sig_data.items():
            idx_trend = sig.get("idx_trend", "\u2014")
            is_bull = idx_trend == "BULLISH"
            trend_color = COLORS["positive"] if is_bull else COLORS["negative"]
            trend_bg = "rgba(0,210,160,0.08)" if is_bull else "rgba(255,107,107,0.08)"
            trend_glow = COLORS["positive_glow"] if is_bull else COLORS["negative_glow"]

            ce = sig.get("ce", {})
            pe = sig.get("pe", {})
            idx = sig.get("idx", {})
            ce_cross = ce.get("crossover", [])
            pe_cross = pe.get("crossover", [])
            idx_cross = idx.get("crossover", [])

            col_hdr = {"fontSize": "0.65rem", "color": COLORS["text_dim"],
                       "fontWeight": "600", "letterSpacing": "0.5px"}
            row_divider = html.Hr(style={
                "border": "none",
                "borderTop": f"1px solid {COLORS['divider']}",
                "margin": "0"})

            card = html.Div(
                style={
                    "background": COLORS["card_solid"],
                    "borderRadius": "14px", "marginBottom": "16px",
                    "overflow": "hidden",
                    "border": f"1px solid {COLORS['card_border']}",
                    "boxShadow": f"0 4px 20px rgba(0,0,0,0.2), 0 0 30px {trend_glow}",
                    "transition": "all 0.3s ease",
                },
                children=[
                    html.Div(style={
                        "display": "flex", "justifyContent": "space-between",
                        "alignItems": "center", "padding": "12px 18px",
                        "background": trend_bg,
                        "borderBottom": f"2px solid {trend_color}",
                    }, children=[
                        html.Span(sym_key, style={
                            "fontWeight": "700", "fontSize": "0.95rem",
                            "color": COLORS["text"], "letterSpacing": "1px"}),
                        html.Span(
                            ("📈 " if is_bull else "📉 ") + idx_trend,
                            style={"color": trend_color, "fontWeight": "700",
                                   "fontSize": "0.8rem",
                                   "background": COLORS["card_solid"],
                                   "padding": "3px 14px", "borderRadius": "20px",
                                   "boxShadow": f"0 0 12px {trend_glow}"}),
                    ]),
                    html.Div(style={
                        "display": "grid", "gridTemplateColumns": "64px 1fr 1fr 1fr",
                        "gap": "8px", "padding": "10px 18px 0",
                    }, children=[
                        html.Span(""),
                        html.Span("POWER", style=col_hdr),
                        html.Span("CANDLES", style=col_hdr),
                        html.Span("CROSSOVER", style=col_hdr),
                    ]),
                    html.Div(style={"padding": "0 18px 12px"}, children=[
                        _signal_row("CE", "\U0001f535", "#5dade2",
                                    ce.get("power", 0), ce.get("list", []), ce_cross),
                        row_divider,
                        _signal_row("PE", "\U0001f534", "#ff6b6b",
                                    pe.get("power", 0), pe.get("list", []), pe_cross),
                        row_divider,
                        _signal_row("IDX", "\U0001f4ca", "#ffd93d",
                                    idx.get("power", 0), idx.get("list", []), idx_cross),
                    ]),
                    # SHA OHLC diagnostic tables (compare with TradingView)
                    html.Details(
                        open=False,
                        style={"padding": "0 18px 8px"},
                        children=[
                            html.Summary("🔍 SHA Debug OHLC", style={
                                "fontSize": "0.65rem", "color": COLORS["text_dim"],
                                "fontWeight": "700", "cursor": "pointer",
                                "letterSpacing": "0.5px", "marginBottom": "6px",
                                "listStylePosition": "inside",
                            }),
                            _sha_debug_table("CE", "#5dade2", ce.get("sha", [])),
                            _sha_debug_table("PE", "#ff6b6b", pe.get("sha", [])),
                            _sha_debug_table("IDX", "#ffd93d", idx.get("sha", [])),
                        ],
                    ),
                    # Legend — complete reference for all signal indicators
                    html.Div(style={
                        "padding": "10px 18px 12px",
                        "borderTop": f"1px solid {COLORS['divider']}",
                        "display": "flex", "flexDirection": "column",
                        "gap": "6px",
                    }, children=[
                        html.Span("LEGEND", style={
                            "fontSize": "0.6rem", "color": COLORS["text_dim"],
                            "fontWeight": "700", "letterSpacing": "1px", "marginBottom": "2px"}),
                        # Row 1: Candle direction + Power bar
                        html.Div(style={
                            "display": "flex", "flexWrap": "wrap",
                            "gap": "12px", "alignItems": "center",
                        }, children=[
                            html.Div(style={"display": "inline-flex", "gap": "5px", "alignItems": "center"}, children=[
                                html.Span("Candles:", style={"fontSize": "0.6rem", "fontWeight": "600",
                                                              "color": COLORS["text_dim"]}),
                                html.Span(style={"display": "inline-block", "width": "8px", "height": "8px",
                                                 "borderRadius": "50%", "background": "#00d2a0", "marginRight": "2px"}),
                                html.Span("Bull", style={"fontSize": "0.6rem", "color": COLORS["text_secondary"],
                                                          "marginRight": "4px"}),
                                html.Span(style={"display": "inline-block", "width": "8px", "height": "8px",
                                                 "borderRadius": "50%", "background": "#e74c3c", "marginRight": "2px"}),
                                html.Span("Bear", style={"fontSize": "0.6rem", "color": COLORS["text_secondary"]}),
                            ]),
                            html.Span("\u2502", style={"color": COLORS["text_muted"], "fontSize": "0.7rem"}),
                            html.Div(style={"display": "inline-flex", "gap": "5px", "alignItems": "center"}, children=[
                                html.Span("Power:", style={"fontSize": "0.6rem", "fontWeight": "600",
                                                            "color": COLORS["text_dim"]}),
                                html.Span(style={"display": "inline-block", "width": "6px", "height": "12px",
                                                 "borderRadius": "2px", "background": "#e74c3c", "marginRight": "1px"}),
                                html.Span("<3", style={"fontSize": "0.55rem", "color": COLORS["text_dim"],
                                                        "marginRight": "4px"}),
                                html.Span(style={"display": "inline-block", "width": "6px", "height": "12px",
                                                 "borderRadius": "2px", "background": "#f39c12", "marginRight": "1px"}),
                                html.Span("\u22653", style={"fontSize": "0.55rem", "color": COLORS["text_dim"],
                                                             "marginRight": "4px"}),
                                html.Span(style={"display": "inline-block", "width": "6px", "height": "12px",
                                                 "borderRadius": "2px", "background": "#00d2a0", "marginRight": "1px"}),
                                html.Span("\u22655", style={"fontSize": "0.55rem", "color": COLORS["text_dim"]}),
                            ]),
                        ]),
                        # Row 2: Crossover — Bullish + Bearish strength
                        html.Div(style={
                            "display": "flex", "flexWrap": "wrap",
                            "gap": "12px", "alignItems": "center",
                        }, children=[
                            html.Div(style={"display": "inline-flex", "gap": "5px", "alignItems": "center"}, children=[
                                html.Span("Crossover Bull:", style={"fontSize": "0.6rem", "fontWeight": "600",
                                                                     "color": COLORS["text_dim"]}),
                                html.Span(style={"display": "inline-block", "width": "8px", "height": "8px",
                                                 "borderRadius": "50%", "background": "#4de8c8", "marginRight": "1px"}),
                                html.Span("Weak", style={"fontSize": "0.55rem", "color": COLORS["text_dim"],
                                                          "marginRight": "4px"}),
                                html.Span(style={"display": "inline-block", "width": "8px", "height": "8px",
                                                 "borderRadius": "50%", "background": "#00d2a0", "marginRight": "1px"}),
                                html.Span("Mid", style={"fontSize": "0.55rem", "color": COLORS["text_dim"],
                                                         "marginRight": "4px"}),
                                html.Span(style={"display": "inline-block", "width": "8px", "height": "8px",
                                                 "borderRadius": "50%", "background": "#009d7a", "marginRight": "1px"}),
                                html.Span("Strong", style={"fontSize": "0.55rem", "color": COLORS["text_dim"]}),
                            ]),
                            html.Span("\u2502", style={"color": COLORS["text_muted"], "fontSize": "0.7rem"}),
                            html.Div(style={"display": "inline-flex", "gap": "5px", "alignItems": "center"}, children=[
                                html.Span("Crossover Bear:", style={"fontSize": "0.6rem", "fontWeight": "600",
                                                                     "color": COLORS["text_dim"]}),
                                html.Span(style={"display": "inline-block", "width": "8px", "height": "8px",
                                                 "borderRadius": "50%", "background": "#ee7b6e", "marginRight": "1px"}),
                                html.Span("Weak", style={"fontSize": "0.55rem", "color": COLORS["text_dim"],
                                                          "marginRight": "4px"}),
                                html.Span(style={"display": "inline-block", "width": "8px", "height": "8px",
                                                 "borderRadius": "50%", "background": "#e74c3c", "marginRight": "1px"}),
                                html.Span("Mid", style={"fontSize": "0.55rem", "color": COLORS["text_dim"],
                                                         "marginRight": "4px"}),
                                html.Span(style={"display": "inline-block", "width": "8px", "height": "8px",
                                                 "borderRadius": "50%", "background": "#c0392b", "marginRight": "1px"}),
                                html.Span("Strong", style={"fontSize": "0.55rem", "color": COLORS["text_dim"]}),
                            ]),
                        ]),
                    ]),
                    html.Div(sig.get("timestamp", "\u2014"), style={
                        "fontSize": "0.65rem", "color": COLORS["text_muted"],
                        "padding": "4px 18px 10px", "textAlign": "right",
                        "fontFamily": "'JetBrains Mono', monospace"}),
                ],
            )
            signal_cards.append(card)

    if not signal_cards:
        signal_cards = [html.Div(children=[
            html.Div("\u26a1", style={"fontSize": "28px", "marginBottom": "8px", "opacity": "0.5"}),
            html.Div("No signal data yet", style={"fontSize": "13px", "color": COLORS["text_dim"]}),
        ], style={**CARD_STYLE, "textAlign": "center", "padding": "32px"})]

    # Strategy log
    log_entries = []
    if isinstance(log_data, list):
        for entry in reversed(log_data[-50:]):
            action = entry.get("action", "")
            leg = entry.get("leg", "")
            sym_raw = entry.get("symbol", "")
            sym_short = sym_raw.split(":")[-1] if ":" in sym_raw else sym_raw
            ts_raw = entry.get("timestamp", "")
            ts_short = ts_raw.split(" ")[-1] if " " in ts_raw else ts_raw
            qty_val = entry.get("qty", 0)
            pl_val = entry.get("pl", 0)

            if action == "ANALYSIS":
                continue

            pl_children = []
            if pl_val != 0:
                pl_col = COLORS["positive"] if pl_val > 0 else COLORS["negative"]
                pl_children = [html.Span(
                    f"\u20b9{pl_val:,.0f}",
                    style={"color": pl_col, "fontWeight": "700", "fontSize": "0.78rem",
                           "fontFamily": "'JetBrains Mono', monospace"})]

            leg_color = "#5dade2" if leg == "CE" else "#ff6b6b" if leg == "PE" else "#ffd93d"
            details_text = entry.get("details", "")

            # ── build detail popup ────────────────────────────────────
            popup_pl_col = COLORS["positive"] if pl_val >= 0 else COLORS["negative"]
            popup_rows = [
                html.Div(className="popup-header", children="📋 Event Details"),
                _popup_row("Timestamp", ts_raw),
                _popup_row("Symbol", sym_raw),
                _popup_row("Action", action),
                _popup_row("Leg", leg or "—"),
            ]
            if qty_val:
                popup_rows.append(_popup_row("Quantity", str(qty_val)))
            if pl_val != 0:
                popup_rows.append(
                    html.Div(className="popup-row", children=[
                        html.Span("P&L", className="popup-label"),
                        html.Span(f"₹{pl_val:,.2f}", className="popup-value",
                                  style={"color": popup_pl_col, "fontWeight": "700"}),
                    ])
                )
            if details_text:
                popup_rows.append(
                    html.Div(className="popup-details-block", children=details_text)
                )

            detail_popup = html.Div(className="log-detail-popup", children=popup_rows)

            # ── the visible row ───────────────────────────────────────
            row_inner = html.Div(style={
                "display": "grid", "gridTemplateColumns": "56px 1fr auto",
                "gap": "8px", "alignItems": "start", "padding": "9px 0",
                "borderBottom": f"1px solid {COLORS['divider']}",
            }, children=[
                html.Span(ts_short, style={
                    "color": COLORS["text_muted"], "fontSize": "0.7rem",
                    "fontFamily": "'JetBrains Mono', monospace", "paddingTop": "2px"}),
                html.Div(children=[
                    html.Div(style={"display": "flex", "gap": "6px",
                                    "alignItems": "center", "flexWrap": "wrap"}, children=[
                        _action_badge(action),
                        html.Span(leg, style={"color": leg_color, "fontWeight": "700",
                                              "fontSize": "0.74rem"})
                            if leg and leg not in ("EVAL", "CHECK") else None,
                        html.Span(sym_short, style={"color": COLORS["text_dim"],
                                                    "fontSize": "0.72rem",
                                                    "fontFamily": "'JetBrains Mono', monospace"})
                            if sym_short else None,
                    ]),
                    html.Span(details_text, style={
                        "color": COLORS["text_muted"], "fontSize": "0.62rem",
                        "fontFamily": "'JetBrains Mono', monospace",
                        "display": "block", "marginTop": "2px",
                        "overflow": "hidden", "textOverflow": "ellipsis",
                        "whiteSpace": "nowrap", "maxWidth": "350px"})
                        if details_text else None,
                    html.Span(f"qty: {qty_val}" if qty_val else "", style={
                        "color": COLORS["text_muted"], "fontSize": "0.68rem",
                        "fontFamily": "'JetBrains Mono', monospace"})
                        if qty_val else None,
                ]),
                html.Div(children=pl_children, style={"textAlign": "right", "minWidth": "55px"}),
            ])

            # Wrapper: focusable for touch, hoverable for desktop
            row = html.Div(
                className="log-entry-wrapper",
                tabIndex=0,
                children=[row_inner, detail_popup],
            )
            log_entries.append(row)

    if not log_entries:
        log_entries = [html.Div(children=[
            html.Div("\U0001f4dd", style={"fontSize": "24px", "marginBottom": "8px", "opacity": "0.5"}),
            html.Div("No strategy events yet", style={"fontSize": "13px"}),
        ], style={"color": COLORS["text_dim"], "textAlign": "center", "padding": "32px"})]

    # Daily trading stats
    daily_closes = 0
    daily_booked = 0.0
    daily_martingales = 0
    if isinstance(tracker_data, dict):
        for _k, _v in tracker_data.items():
            if _k.startswith("_") or not isinstance(_v, dict):
                continue
            daily_closes += _v.get("close_count", 0)
            daily_booked += _v.get("total_profit_closed", 0.0)
            daily_martingales += _v.get("martingale_count", 0)
    daily_avg = daily_booked / max(daily_closes, 1)
    booked_color = COLORS["positive"] if daily_booked >= 0 else COLORS["negative"]

    daily_stats_cards = [
        _kpi_card("Today's Booked Profit", f"\u20b9{daily_booked:,.2f}", booked_color, icon="\U0001f3e6"),
        _kpi_card("Avg Profit / Close", f"\u20b9{daily_avg:,.2f}",
                  COLORS["positive"] if daily_avg > 0 else COLORS["negative"], icon="\U0001f4c9"),
        _kpi_card("Closes Today", str(daily_closes), COLORS["accent"], icon="\u2705"),
        _kpi_card("Martingale Adds", str(daily_martingales),
                  "#9b59b6" if daily_martingales > 0 else COLORS["text_dim"], icon="\u26a1"),
    ]

    profit_fig = _build_profit_chart(history_data, selected_chart_date,
                                      mode=selected_mode or ACTIVE_MODE)

    # Build date dropdown options
    current_mode = selected_mode or ACTIVE_MODE
    if current_mode == "live":
        # Live mode: no date selector, today only
        date_options = []
        date_value = None
    else:
        # Demo mode: show last 7 days with data (including demo trade history fallback)
        demo_fallback = _load_demo_trade_history()
        chart_actions = ("SNAPSHOT", "CLOSE", "MARTINGALE", "ENTRY")
        existing_dates = {e.get("date") for e in history_data
                          if e.get("date") and e.get("action") in chart_actions}
        merged = list(history_data)
        for entry in demo_fallback:
            if entry.get("date") not in existing_dates:
                merged.append(entry)
        available_dates = _get_available_chart_dates(merged)
        today = datetime.now().strftime("%Y-%m-%d")
        date_options = []
        for d in available_dates:
            if d == today:
                label = f"Today ({d})"
            else:
                label = d
            date_options.append({"label": label, "value": d})

        # Keep current selection if still valid; otherwise default to first
        if selected_chart_date and selected_chart_date in available_dates:
            date_value = selected_chart_date
        elif available_dates:
            date_value = available_dates[0]
        else:
            date_value = None

    return (
        status_text, mode_text, f"Last updated: {last_ts}",
        kpi_cards, pos_table, signal_cards, log_entries,
        daily_stats_cards, profit_fig, date_options, date_value,
    )


# ======================================================================
#  MAIN
# ======================================================================

if __name__ == "__main__":
    STATE_DIR_DEMO.mkdir(parents=True, exist_ok=True)
    STATE_DIR_LIVE.mkdir(parents=True, exist_ok=True)

    print(f"\U0001f4ca Ballom FYR Dashboard starting on http://127.0.0.1:{_port_arg}")
    print(f"   Monitoring mode: {ACTIVE_MODE.upper()}")
    print(f"   State dir: {get_state_dir(ACTIVE_MODE)}")
    print(f"   (Use the dropdown to switch between DEMO / LIVE)\n")

    app.run(debug=False, host="0.0.0.0", port=_port_arg)
