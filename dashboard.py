"""
dashboard.py — Live Dash / Plotly dashboard for Ballom_FYR.

Reads JSON state files from C:/Ballom_FYR/state/<mode>/ and displays:
  * Account balance, realized / unrealized P&L
  * Open positions table
  * SHA signal strength, power, list per symbol
  * RSI overbought/oversold state per symbol
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
from dash.dependencies import Input, Output, State
import plotly.graph_objects as go

from constants import (
    STATE_DIR_DEMO,
    STATE_DIR_LIVE,
    STATE_DIR_BASE,
    DASHBOARD_PORT,
    DASHBOARD_REFRESH_MS,
    SHA_LENGTH,
    SHA_TREND_LENGTH,
    SYMBOLS_JSON,
    get_state_dir,
)


# ======================================================================
#  PREMIUM COLOR PALETTE
# ======================================================================

COLORS = {
    # Backgrounds — deep void black inspired by Ballom's dark aura
    "bg":             "#050810",
    "bg_secondary":   "#0a0f1c",
    "card":           "rgba(12, 16, 32, 0.88)",
    "card_solid":     "#0c1020",
    "card_border":    "rgba(90, 60, 180, 0.14)",
    # Text
    "text":           "#e4e8f4",
    "text_secondary": "#9da8c4",
    "text_dim":       "#4e5878",
    "text_muted":     "#363f58",
    # Accents — purple energy orbs
    "accent":         "#7c3aed",
    "accent_glow":    "rgba(124, 58, 237, 0.30)",
    "accent_soft":    "rgba(124, 58, 237, 0.14)",
    # Signals — teal wings / crimson orbs / amber heart
    "positive":       "#00c4a0",
    "positive_soft":  "rgba(0, 196, 160, 0.12)",
    "positive_glow":  "rgba(0, 196, 160, 0.3)",
    "negative":       "#ff4444",
    "negative_soft":  "rgba(255, 68, 68, 0.14)",
    "negative_glow":  "rgba(255, 68, 68, 0.35)",
    "warning":        "#ff8c42",
    "neutral":        "#4e5878",
    # UI
    "divider":        "rgba(90, 60, 180, 0.10)",
    "chart_grid":     "rgba(90, 60, 180, 0.08)",
    "gradient_start": "#7c3aed",
    "gradient_end":   "#00c4a0",
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
        "strategy_log_dir": d / "strategy_log",
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


def _read_strategy_log_for_date(paths: dict, target_date: str) -> list:
    """Read strategy log entries for a specific date.

    Tries date-partitioned file first (strategy_log/YYYY-MM-DD.json),
    falls back to legacy strategy_log.json filtered by timestamp.
    """
    log_dir = paths.get("strategy_log_dir")
    if log_dir and Path(log_dir).is_dir():
        date_file = Path(log_dir) / f"{target_date}.json"
        if date_file.exists():
            data = _read(date_file)
            return data if isinstance(data, list) else []

    # Fallback: read legacy single file, filter by timestamp date
    data = _read(paths["strategy_log"])
    if isinstance(data, list):
        return [e for e in data
                if e.get("date", "").startswith(target_date)
                or e.get("timestamp", "").startswith(target_date)]
    return []


def _normalize_history_dates(history_data: list) -> list:
    """Fix history entries where the 'date' field doesn't match the timestamp.

    This corrects data from before the _append_history fix, where entries
    recorded across midnight got the wrong date.
    """
    for entry in history_data:
        ts = entry.get("timestamp", "")
        if len(ts) >= 10:
            ts_date = ts[:10]
            entry_date = entry.get("date", "")
            if entry_date and entry_date != ts_date:
                entry["date"] = ts_date
    return history_data


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


def _gap_badge(label: str, gap_pct: float, color: str) -> html.Div:
    """Compact GAP% badge for one leg (CE / PE / IDX).

    Colors the value based on GAP_RANGE_LOW / GAP_RANGE_HIGH:
        - Below LOW  → cyan  (gap is narrow — SHAs are close)
        - Within range → amber (gap is healthy)
        - Above HIGH → red   (gap is over-extended)
    """
    from constants import GAP_RANGE_LOW, GAP_RANGE_HIGH

    abs_gap = abs(gap_pct)
    if abs_gap < GAP_RANGE_LOW:
        gap_color = "#00bcd4"  # cyan — narrow gap
        gap_label = "NARROW"
    elif abs_gap > GAP_RANGE_HIGH:
        gap_color = "#e74c3c"  # red — over-extended
        gap_label = "WIDE"
    else:
        gap_color = "#f39c12"  # amber — in range
        gap_label = "IN RANGE"

    sign = "+" if gap_pct > 0 else ""
    return html.Div(style={
        "background": "rgba(0,0,0,0.2)",
        "borderRadius": "8px",
        "padding": "8px 10px",
        "textAlign": "center",
        "border": f"1px solid {color}33",
    }, children=[
        html.Div(label, style={
            "fontSize": "0.6rem", "fontWeight": "700",
            "color": color, "letterSpacing": "0.5px", "marginBottom": "4px"}),
        html.Div(f"{sign}{gap_pct:.2f}%", style={
            "fontSize": "1.1rem", "fontWeight": "800",
            "color": gap_color, "fontFamily": "'JetBrains Mono', monospace"}),
        html.Div(gap_label, style={
            "fontSize": "0.5rem", "fontWeight": "600",
            "color": gap_color, "letterSpacing": "0.5px",
            "marginTop": "2px", "opacity": "0.8"}),
    ])


def _relationship_badge(label: str, rel_data: dict, color: str) -> html.Div:
    """Compact badge showing SHA relationship status for one leg (CE / PE / IDX).

    Statuses:
        DIVERGING  — Signal & Trend SHAs are moving apart
        CONVERGING — Signal & Trend SHAs are moving closer
        PARALLEL   — SHAs moving in same direction at similar rate
        CLOSE      — SHAs are nearly overlapping
        UNKNOWN    — Insufficient data
    """
    status = rel_data.get("status", "UNKNOWN")
    strength = rel_data.get("strength", 0.0)
    avg_gap = rel_data.get("avg_gap", 0.0)
    delta = rel_data.get("delta", 0.0)

    # Status styling
    STATUS_STYLES = {
        "DIVERGING":  {"color": "#e74c3c", "icon": "\u2197\u2199", "bg": "rgba(231,76,60,0.10)"},
        "CONVERGING": {"color": "#00bcd4", "icon": "\u2198\u2197", "bg": "rgba(0,188,212,0.10)"},
        "PARALLEL":   {"color": "#f39c12", "icon": "\u2192\u2192", "bg": "rgba(243,156,18,0.10)"},
        "CLOSE":      {"color": "#00d2a0", "icon": "\u2248",  "bg": "rgba(0,210,160,0.10)"},
        "UNKNOWN":    {"color": "#4e5878", "icon": "\u2014",  "bg": "rgba(78,88,120,0.08)"},
    }
    st = STATUS_STYLES.get(status, STATUS_STYLES["UNKNOWN"])

    # Strength bar (3 segments)
    bar_segments = []
    for i in range(3):
        threshold = (i + 1) / 3
        filled = strength >= threshold
        bar_segments.append(
            html.Span(style={
                "display": "inline-block", "width": "14px", "height": "4px",
                "borderRadius": "2px", "marginRight": "2px",
                "background": st["color"] if filled else "rgba(255,255,255,0.08)",
                "opacity": "1" if filled else "0.3",
            })
        )

    sign = "+" if delta > 0 else ""

    return html.Div(style={
        "background": st["bg"],
        "borderRadius": "8px",
        "padding": "8px 10px",
        "textAlign": "center",
        "border": f"1px solid {color}33",
    }, children=[
        html.Div(label, style={
            "fontSize": "0.6rem", "fontWeight": "700",
            "color": color, "letterSpacing": "0.5px", "marginBottom": "4px"}),
        html.Div(f"{st['icon']}", style={
            "fontSize": "1rem", "marginBottom": "2px"}),
        html.Div(status, style={
            "fontSize": "0.7rem", "fontWeight": "800",
            "color": st["color"], "fontFamily": "'JetBrains Mono', monospace",
            "letterSpacing": "0.5px"}),
        html.Div(bar_segments, style={
            "display": "flex", "justifyContent": "center",
            "marginTop": "4px", "marginBottom": "2px"}),
        html.Div(f"\u0394 {sign}{delta:.2f}%", style={
            "fontSize": "0.5rem", "fontWeight": "600",
            "color": COLORS["text_dim"], "fontFamily": "'JetBrains Mono', monospace",
            "marginTop": "2px"}),
    ])


def _combined_analysis_badge(label: str, gap_pct: float, rel_data: dict, color: str) -> html.Div:
    """Combined GAP% + SHA Relationship badge for one leg (CE / PE / IDX)."""
    from constants import GAP_RANGE_LOW, GAP_RANGE_HIGH

    # GAP% coloring
    abs_gap = abs(gap_pct)
    if abs_gap < GAP_RANGE_LOW:
        gap_color = "#00bcd4"
        gap_label = "NARROW"
    elif abs_gap > GAP_RANGE_HIGH:
        gap_color = "#e74c3c"
        gap_label = "WIDE"
    else:
        gap_color = "#f39c12"
        gap_label = "IN RANGE"
    sign = "+" if gap_pct > 0 else ""

    # Relationship
    status = rel_data.get("status", "UNKNOWN") if isinstance(rel_data, dict) else "UNKNOWN"
    strength = rel_data.get("strength", 0.0) if isinstance(rel_data, dict) else 0.0
    delta = rel_data.get("delta", 0.0) if isinstance(rel_data, dict) else 0.0

    STATUS_STYLES = {
        "DIVERGING":  {"color": "#e74c3c", "icon": "\u2197\u2199"},
        "CONVERGING": {"color": "#00bcd4", "icon": "\u2198\u2197"},
        "PARALLEL":   {"color": "#f39c12", "icon": "\u2192\u2192"},
        "CLOSE":      {"color": "#00d2a0", "icon": "\u2248"},
        "UNKNOWN":    {"color": "#4e5878", "icon": "\u2014"},
    }
    st = STATUS_STYLES.get(status, STATUS_STYLES["UNKNOWN"])

    # Strength bar (3 segments)
    bar_segments = []
    for i in range(3):
        threshold = (i + 1) / 3
        filled = strength >= threshold
        bar_segments.append(
            html.Span(style={
                "display": "inline-block", "width": "14px", "height": "4px",
                "borderRadius": "2px", "marginRight": "2px",
                "background": st["color"] if filled else "rgba(255,255,255,0.08)",
                "opacity": "1" if filled else "0.3",
            })
        )

    rel_sign = "+" if delta > 0 else ""

    return html.Div(style={
        "background": "rgba(0,0,0,0.2)",
        "borderRadius": "8px",
        "padding": "10px 10px",
        "textAlign": "center",
        "border": f"1px solid {color}33",
    }, children=[
        # Label
        html.Div(label, style={
            "fontSize": "0.6rem", "fontWeight": "700",
            "color": color, "letterSpacing": "0.5px", "marginBottom": "6px"}),
        # GAP% value
        html.Div(f"{sign}{gap_pct:.2f}%", style={
            "fontSize": "1.1rem", "fontWeight": "800",
            "color": gap_color, "fontFamily": "'JetBrains Mono', monospace"}),
        html.Div(gap_label, style={
            "fontSize": "0.5rem", "fontWeight": "600",
            "color": gap_color, "letterSpacing": "0.5px",
            "marginTop": "2px", "opacity": "0.8"}),
        # Divider
        html.Hr(style={
            "border": "none",
            "borderTop": f"1px solid {COLORS['divider']}",
            "margin": "6px 0"}),
        # Relationship icon + status
        html.Div(f"{st['icon']}", style={
            "fontSize": "0.9rem", "marginBottom": "2px"}),
        html.Div(status, style={
            "fontSize": "0.65rem", "fontWeight": "800",
            "color": st["color"], "fontFamily": "'JetBrains Mono', monospace",
            "letterSpacing": "0.5px"}),
        html.Div(bar_segments, style={
            "display": "flex", "justifyContent": "center",
            "marginTop": "4px", "marginBottom": "2px"}),
        html.Div(f"\u0394 {rel_sign}{delta:.2f}%", style={
            "fontSize": "0.5rem", "fontWeight": "600",
            "color": COLORS["text_dim"], "fontFamily": "'JetBrains Mono', monospace",
            "marginTop": "2px"}),
    ])


def _rsi_badge(label: str, rsi_value, color: str) -> html.Div:
    """RSI badge showing overbought/oversold/neutral state for one leg."""
    from constants import RSI_OVERSOLD, RSI_OVERBOUGHT

    if rsi_value is None:
        rsi_display = "\u2014"
        rsi_color = COLORS["text_dim"]
        rsi_label = "NO DATA"
        rsi_bg = "rgba(78,88,120,0.08)"
    else:
        rsi_val = float(rsi_value)
        rsi_display = f"{rsi_val:.1f}"
        if rsi_val >= RSI_OVERBOUGHT:
            rsi_color = "#e74c3c"
            rsi_label = "OVERBOUGHT"
            rsi_bg = "rgba(231,76,60,0.10)"
        elif rsi_val <= RSI_OVERSOLD:
            rsi_color = "#00d2a0"
            rsi_label = "OVERSOLD"
            rsi_bg = "rgba(0,210,160,0.10)"
        else:
            rsi_color = "#f39c12"
            rsi_label = "NEUTRAL"
            rsi_bg = "rgba(243,156,18,0.08)"

    return html.Div(style={
        "background": rsi_bg,
        "borderRadius": "8px",
        "padding": "8px 10px",
        "textAlign": "center",
        "border": f"1px solid {color}33",
    }, children=[
        html.Div(label, style={
            "fontSize": "0.6rem", "fontWeight": "700",
            "color": color, "letterSpacing": "0.5px", "marginBottom": "4px"}),
        html.Div(rsi_display, style={
            "fontSize": "1.1rem", "fontWeight": "800",
            "color": rsi_color, "fontFamily": "'JetBrains Mono', monospace"}),
        html.Div(rsi_label, style={
            "fontSize": "0.5rem", "fontWeight": "600",
            "color": rsi_color, "letterSpacing": "0.5px",
            "marginTop": "2px", "opacity": "0.8"}),
    ])


def _signal_row(label: str, icon: str, color: str,
                power: int, lst: list) -> html.Div:
    """One compact row for CE / PE / IDX -- 3-column grid."""
    return html.Div(
        style={
            "display": "grid",
            "gridTemplateColumns": "64px 1fr 1fr",
            "gap": "8px", "alignItems": "center",
            "padding": "8px 0",
        },
        children=[
            html.Span(f"{icon} {label}", style={
                "fontWeight": "700", "fontSize": "0.82rem", "color": color,
            }),
            _power_bar(power),
            _list_dots(lst),
        ],
    )




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

    # ── LIVE mode: show selected date (last 7 days supported) ───────────
    if mode == "live":
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
        if not today_data:
            fig = go.Figure()
            fig.update_layout(**empty_layout)
            fig.add_annotation(text=f"No data for {chart_date}", showarrow=False,
                               font=dict(size=14, color=COLORS["text_dim"]),
                               xref="paper", yref="paper", x=0.5, y=0.5)
            return fig
        showing_past = chart_date != today
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
    assets_folder="asset",
)

# Custom HTML with premium Google Fonts, animations, scrollbar
app.index_string = """<!DOCTYPE html>
<html>
<head>
{%metas%}
<title>{%title%}</title>
<link rel="icon" type="image/png" href="/assets/ballom.png">
{%css%}
<link rel="preconnect" href="https://fonts.googleapis.com">
<link rel="preconnect" href="https://fonts.gstatic.com" crossorigin>
<link href="https://fonts.googleapis.com/css2?family=Inter:wght@300;400;500;600;700;800;900&family=JetBrains+Mono:wght@400;500;600;700&display=swap" rel="stylesheet">
<style>
    *, *::before, *::after { box-sizing: border-box; }
    body {
        margin: 0; padding: 0; background: #050810;
        font-family: 'Inter', -apple-system, BlinkMacSystemFont, 'Segoe UI', sans-serif;
        -webkit-font-smoothing: antialiased;
        -moz-osx-font-smoothing: grayscale;
    }
    ._dash-loading-callback, .dash-loading, ._dash-loading,
    div._dash-loading-callback--is-loading { visibility: hidden !important; }
    ::-webkit-scrollbar { width: 6px; height: 6px; }
    ::-webkit-scrollbar-track { background: transparent; }
    ::-webkit-scrollbar-thumb { background: rgba(124, 58, 237, 0.25); border-radius: 10px; }
    ::-webkit-scrollbar-thumb:hover { background: rgba(124, 58, 237, 0.45); }

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
        background: linear-gradient(90deg, #7c3aed, #00c4a0, #ff8c42, #ff4444, #7c3aed);
        background-size: 300% auto;
        animation: shimmer 6s linear infinite;
    }
    ::selection { background: rgba(124, 58, 237, 0.3); color: #e4e8f4; }
    .plotly .hoverlayer .hovertext { font-family: 'Inter', sans-serif !important; }

    /* SHA signal cards — collapsible sections */
    .signal-card-collapse > summary { list-style: none; }
    .signal-card-collapse > summary::-webkit-details-marker { display: none; }
    .signal-card-collapse > summary::marker { display: none; content: ''; }

    .dash-spreadsheet-container .dash-spreadsheet-inner th {
        font-family: 'Inter', sans-serif !important; letter-spacing: 0.5px !important;
    }
    .dash-spreadsheet-container .dash-spreadsheet-inner td {
        font-family: 'JetBrains Mono', monospace !important;
    }
    .Select-control { background: #0c1020 !important; border-color: rgba(90,60,180,0.2) !important; border-radius: 10px !important; }
    .Select-menu-outer { background: #0c1020 !important; border-color: rgba(90,60,180,0.2) !important; border-radius: 10px !important; }
    .Select-option.is-focused { background: rgba(124,58,237,0.15) !important; }
    .Select-value-label { color: #e4e8f4 !important; }
    /* Dash dropdown — dark theme for date selector */
    #profit-date-selector,
    #profit-date-selector * { box-sizing: border-box; }
    #profit-date-selector .Select-control,
    #profit-date-selector > div { background: #0a0f1c !important; border-color: rgba(90,60,180,0.25) !important; }
    #profit-date-selector .Select-value-label,
    #profit-date-selector .Select-placeholder,
    #profit-date-selector span[class*="value"],
    #profit-date-selector div[class*="singleValue"],
    #profit-date-selector div[class*="SingleValue"],
    #profit-date-selector div[class*="placeholder"] { color: #e4e8f4 !important; font-weight: 600 !important; font-size: 13px !important; }
    #profit-date-selector .Select-input > input,
    #profit-date-selector input { color: #e4e8f4 !important; }
    #profit-date-selector .Select-menu-outer,
    #profit-date-selector div[class*="menu"] { background: #0a0f1c !important; border-color: rgba(90,60,180,0.25) !important; z-index: 99999 !important; position: absolute !important; }
    #profit-date-selector { position: relative !important; z-index: 99999 !important; }
    #profit-date-selector .Select-option,
    #profit-date-selector div[class*="option"] { color: #e4e8f4 !important; background: transparent !important; }
    #profit-date-selector .Select-option.is-focused,
    #profit-date-selector div[class*="option"]:hover { background: rgba(124,58,237,0.25) !important; }
    #profit-date-selector .Select-arrow { border-color: #9da8c4 transparent transparent !important; }
    #profit-date-selector svg { fill: #9da8c4 !important; }
    /* Force the outer wrapper to also be dark */
    #profit-date-selector { background: #0a0f1c !important; border-radius: 10px !important; }
    /* iOS-style mode toggle */
    .mode-toggle-track {
        position: relative;
        width: 140px; height: 36px;
        background: rgba(10, 15, 28, 0.9);
        border-radius: 18px;
        border: 1px solid rgba(90,60,180,0.15);
        cursor: pointer;
        display: flex; align-items: center;
        padding: 3px;
        transition: background 0.3s ease;
        box-shadow: inset 0 1px 4px rgba(0,0,0,0.4);
    }
    .mode-toggle-track:hover {
        border-color: rgba(124,58,237,0.3);
    }
    .mode-toggle-knob {
        position: absolute;
        width: 66px; height: 30px;
        border-radius: 15px;
        transition: left 0.3s cubic-bezier(0.4, 0, 0.2, 1), background 0.3s ease;
        top: 3px;
        z-index: 1;
    }
    .mode-toggle-knob.demo {
        left: 3px;
        background: linear-gradient(135deg, #7c3aed, #5b21b6);
        box-shadow: 0 2px 10px rgba(124,58,237,0.4);
    }
    .mode-toggle-knob.live {
        left: 71px;
        background: linear-gradient(135deg, #ff4444, #dc2626);
        box-shadow: 0 2px 10px rgba(255,68,68,0.4);
    }
    .mode-toggle-label {
        flex: 1; text-align: center;
        font-size: 11px; font-weight: 700;
        letter-spacing: 0.8px;
        z-index: 2; position: relative;
        transition: color 0.3s ease;
        user-select: none;
        line-height: 30px;
    }
    .mode-toggle-label.active { color: #fff; }
    .mode-toggle-label.inactive { color: #4e5878; }
    /* Hide the backing RadioItems completely */
    #mode-selector { display: none !important; }

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
        background: rgba(124, 58, 237, 0.06);
    }
    .log-detail-popup {
        display: none;
        position: absolute;
        left: 0;
        top: 100%;
        width: 100%;
        max-height: 380px;
        overflow-y: auto;
        background: #0c1020;
        border: 1px solid rgba(124, 58, 237, 0.30);
        border-radius: 14px;
        padding: 16px 18px;
        box-shadow: 0 8px 40px rgba(0,0,0,0.55), 0 0 20px rgba(124,58,237,0.15);
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
        color: #7c3aed; text-transform: uppercase; margin-bottom: 10px;
        border-bottom: 1px solid rgba(90,60,180,0.15); padding-bottom: 8px;
    }
    .log-detail-popup .popup-row {
        display: flex; justify-content: space-between; align-items: flex-start;
        padding: 5px 0; border-bottom: 1px solid rgba(90,60,180,0.06);
    }
    .log-detail-popup .popup-row:last-child { border-bottom: none; }
    .log-detail-popup .popup-label {
        font-size: 0.65rem; font-weight: 600; color: #4e5878;
        letter-spacing: 0.5px; text-transform: uppercase; min-width: 70px;
        flex-shrink: 0;
    }
    .log-detail-popup .popup-value {
        font-size: 0.75rem; color: #e4e8f4;
        font-family: 'JetBrains Mono', monospace;
        text-align: right; word-break: break-all; max-width: 230px;
    }
    .log-detail-popup .popup-details-block {
        margin-top: 8px; padding: 10px 12px;
        background: rgba(5, 8, 16, 0.6); border-radius: 8px;
        font-size: 0.7rem; color: #9da8c4; line-height: 1.55;
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
                "background": "rgba(5, 8, 16, 0.95)",
                "backdropFilter": "blur(20px)",
                "WebkitBackdropFilter": "blur(20px)",
                "borderBottom": f"1px solid {COLORS['divider']}",
                "position": "relative",
                "zIndex": "100000",
            },
            children=[
                # Logo
                html.Div(
                    style={"display": "flex", "alignItems": "center"},
                    children=[
                        html.Img(src="/assets/ballom.png", style={
                            "width": "40px", "height": "40px", "borderRadius": "10px",
                            "objectFit": "cover",
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
                # Center -- mode selector (prominent) + status badges
                html.Div(
                    style={"display": "flex", "alignItems": "center", "gap": "14px"},
                    children=[
                        # iOS-style toggle
                        html.Div(
                            id="mode-toggle-track",
                            className="mode-toggle-track",
                            n_clicks=0,
                            children=[
                                html.Div(id="mode-toggle-knob",
                                         className=f"mode-toggle-knob {ACTIVE_MODE}"),
                                html.Span("DEMO", id="mode-label-demo",
                                          className=f"mode-toggle-label {'active' if ACTIVE_MODE == 'demo' else 'inactive'}"),
                                html.Span("LIVE", id="mode-label-live",
                                          className=f"mode-toggle-label {'active' if ACTIVE_MODE == 'live' else 'inactive'}"),
                            ],
                        ),
                        # Hidden RadioItems to keep callback contract
                        dcc.RadioItems(
                            id="mode-selector",
                            options=[
                                {"label": "DEMO", "value": "demo"},
                                {"label": "LIVE", "value": "live"},
                            ],
                            value=ACTIVE_MODE,
                        ),
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
                            html.Span("\U0001f4b9", style={"fontSize": "16px"}),
                            html.Span("Traded Positions", style={
                                "fontSize": "15px", "fontWeight": "600",
                                "color": COLORS["text"], "letterSpacing": "0.3px"}),
                        ]),
                        html.Div(id="traded-positions-container"),
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

# Clientside callback: iOS toggle click → update hidden RadioItems + knob/labels
app.clientside_callback(
    """
    function(n_clicks, current_value) {
        if (!n_clicks) return [current_value, 'mode-toggle-knob ' + current_value,
            current_value === 'demo' ? 'mode-toggle-label active' : 'mode-toggle-label inactive',
            current_value === 'live' ? 'mode-toggle-label active' : 'mode-toggle-label inactive'];
        var new_val = current_value === 'demo' ? 'live' : 'demo';
        return [new_val, 'mode-toggle-knob ' + new_val,
            new_val === 'demo' ? 'mode-toggle-label active' : 'mode-toggle-label inactive',
            new_val === 'live' ? 'mode-toggle-label active' : 'mode-toggle-label inactive'];
    }
    """,
    [Output("mode-selector", "value"),
     Output("mode-toggle-knob", "className"),
     Output("mode-label-demo", "className"),
     Output("mode-label-live", "className")],
    Input("mode-toggle-track", "n_clicks"),
    [dash.dependencies.State("mode-selector", "value")],
    prevent_initial_call=True,
)

@app.callback(
    [
        Output("app-status-badge", "children"),
        Output("app-mode-badge", "children"),
        Output("last-updated", "children"),
        Output("kpi-row", "children"),
        Output("positions-table-container", "children"),
        Output("traded-positions-container", "children"),
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
    tracker_data = _read(paths["position_tracker"])
    history_raw = _read(paths["profit_history"])
    history_data = history_raw if isinstance(history_raw, list) else []

    # Normalize history dates: fix entries where timestamp date != date field
    # (caused by app running across midnight before the _append_history fix)
    history_data = _normalize_history_dates(history_data)

    # Strategy log: ALWAYS show today's live events regardless of chart date
    _log_date = datetime.now().strftime("%Y-%m-%d")
    log_data = _read_strategy_log_for_date(paths, _log_date)

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

    # Positions table — split into open (netQty != 0) and traded/closed (netQty == 0)
    all_positions = pos_data.get("positions", [])
    open_positions = [p for p in all_positions if p.get("netQty", 0) != 0]

    # Traded positions: pair ENTRY → CLOSE from profit_history for the selected date
    _traded_date = selected_chart_date or datetime.now().strftime("%Y-%m-%d")
    _day_entries = [
        e for e in history_data
        if e.get("date") == _traded_date and e.get("action") in ("ENTRY", "CLOSE")
    ]
    # Build trade cycles by pairing each CLOSE with its most recent unmatched ENTRY
    _pending_entries: dict[str, list] = {}   # symbol → [entry, ...]
    traded_positions: list[dict] = []
    for ev in _day_entries:
        sym = ev.get("symbol", "")
        if ev["action"] == "ENTRY":
            _pending_entries.setdefault(sym, []).append(ev)
        elif ev["action"] == "CLOSE":
            entry_ev = None
            if sym in _pending_entries and _pending_entries[sym]:
                entry_ev = _pending_entries[sym].pop(0)
            open_time = entry_ev.get("timestamp", "\u2014") if entry_ev else "\u2014"
            entry_avg = entry_ev.get("avg_price", 0.0) if entry_ev else 0.0
            traded_positions.append({
                "symbol": sym,
                "open_time": open_time,
                "close_time": ev.get("timestamp", "\u2014"),
                "qty": ev.get("qty", 0),
                "avg_price": round(entry_avg, 2) if entry_avg else "\u2014",
                "ltp": round(ev.get("ltp", 0.0), 2) or "\u2014",
                "booked_profit": round(ev.get("effective_pl", 0.0), 2),
            })

    traded_positions.sort(key=lambda x: x.get("close_time", ""), reverse=True)

    _traded_cols_def = [
        {"name": "Symbol", "id": "symbol"},
        {"name": "Open Time", "id": "open_time"},
        {"name": "Close Time", "id": "close_time"},
        {"name": "Qty", "id": "qty"},
        {"name": "Avg Price", "id": "avg_price"},
        {"name": "LTP", "id": "ltp"},
        {"name": "Booked P&L", "id": "booked_profit"},
    ]

    _pos_cols_def = [
        {"name": "Symbol", "id": "symbol"},
        {"name": "Qty", "id": "netQty"},
        {"name": "Avg Price", "id": "netAvg"},
        {"name": "LTP", "id": "ltp"},
        {"name": "Realized P&L", "id": "realized_profit"},
        {"name": "Unrealized P&L", "id": "unrealized_profit"},
        {"name": "Product", "id": "productType"},
    ]
    _pos_table_style = {"overflowX": "auto", "borderRadius": "12px"}
    _pos_header_style = {
        "backgroundColor": COLORS["card_solid"],
        "color": COLORS["text_dim"],
        "fontWeight": "600", "border": "none",
        "fontSize": "11px", "letterSpacing": "0.8px",
        "textTransform": "uppercase", "padding": "12px 16px",
        "borderBottom": f"1px solid {COLORS['divider']}",
    }
    _pos_cell_style = {
        "backgroundColor": COLORS["card_solid"],
        "color": COLORS["text"],
        "border": f"1px solid {COLORS['divider']}",
        "padding": "12px 16px", "fontSize": "0.85rem",
        "fontFamily": "'JetBrains Mono', monospace",
    }
    _pos_cond_style = [
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
    ]

    cols = ["symbol", "netQty", "netAvg", "ltp", "realized_profit", "unrealized_profit", "productType"]

    if open_positions:
        rows = [{c: p.get(c, "") for c in cols} for p in open_positions]
        pos_table = dash_table.DataTable(
            data=rows, columns=_pos_cols_def,
            style_table=_pos_table_style,
            style_header=_pos_header_style,
            style_cell=_pos_cell_style,
            style_data_conditional=_pos_cond_style,
        )
        pos_table = html.Div(pos_table, style={
            **CARD_STYLE, "padding": "0", "overflow": "hidden"})
    else:
        pos_table = html.Div(children=[
            html.Div("\U0001f4ed", style={"fontSize": "28px", "marginBottom": "8px", "opacity": "0.5"}),
            html.Div("No open positions", style={"fontSize": "13px", "color": COLORS["text_dim"]}),
        ], style={**CARD_STYLE, "textAlign": "center", "padding": "32px"})

    if traded_positions:
        traded_table = dash_table.DataTable(
            data=traded_positions, columns=_traded_cols_def,
            style_table={**_pos_table_style, "maxHeight": "260px", "overflowY": "auto"},
            fixed_rows={"headers": True},
            style_header={**_pos_header_style, "position": "sticky", "top": 0, "zIndex": 1},
            style_cell=_pos_cell_style,
            style_data_conditional=[
                {"if": {"filter_query": "{booked_profit} > 0",
                        "column_id": "booked_profit"},
                 "color": COLORS["positive"], "fontWeight": "bold"},
                {"if": {"filter_query": "{booked_profit} < 0",
                        "column_id": "booked_profit"},
                 "color": COLORS["negative"], "fontWeight": "bold"},
                {"if": {"state": "active"},
                 "backgroundColor": COLORS["accent_soft"],
                 "border": f"1px solid {COLORS['accent']}"},
            ],
        )
        traded_table = html.Div(traded_table, style={
            **CARD_STYLE, "padding": "0", "overflow": "hidden"})
    else:
        traded_table = html.Div(children=[
            html.Div("\U0001f4ed", style={"fontSize": "28px", "marginBottom": "8px", "opacity": "0.5"}),
            html.Div(f"No traded positions for {_traded_date}", style={"fontSize": "13px", "color": COLORS["text_dim"]}),
        ], style={**CARD_STYLE, "textAlign": "center", "padding": "32px"})

    # Signal cards — show exactly the symbols defined in symbols.json
    _allowed_idx = []
    _allowed_com = []
    try:
        _sym_cfg = json.loads(SYMBOLS_JSON.read_text())
        _allowed_idx = [e["symbol"] for e in _sym_cfg.get("indices", [])]
        _allowed_com = [e["symbol"] for e in _sym_cfg.get("commodities", [])
                        if e.get("enabled", True)]
    except Exception:
        pass  # fallback: show whatever is in sig_data

    _allowed_syms = set(_allowed_idx + _allowed_com) if (_allowed_idx or _allowed_com) else None

    signal_cards = []
    if not isinstance(sig_data, dict):
        sig_data = {}

    # Filter out stale symbols not in symbols.json
    if _allowed_syms is not None:
        sig_data = {k: v for k, v in sig_data.items() if k in _allowed_syms}
        # Ensure every allowed symbol has an entry (placeholder if missing)
        for sym in _allowed_idx:
            if sym not in sig_data:
                sig_data[sym] = {"market_type": "INDEX"}
        for sym in _allowed_com:
            if sym not in sig_data:
                sig_data[sym] = {"market_type": "COMMODITY"}

    if sig_data:
        # Sort: indices first, then commodities
        # Infer from market_type field, or from ce_symbol exchange prefix
        # NSE:/NFO: = index, MCX: = commodity
        def _sort_key(item):
            _sig = item[1]
            mt = _sig.get("market_type", "")
            if not mt:
                ce_sym = _sig.get("ce_symbol", "")
                mt = "COMMODITY" if ce_sym.startswith("MCX:") else "INDEX"
            return (0 if mt == "INDEX" else 1, item[0])

        for sym_key, sig in sorted(sig_data.items(), key=_sort_key):
            idx_trend = sig.get("idx_trend", "\u2014")
            is_bull = idx_trend == "BULLISH"
            trend_color = COLORS["positive"] if is_bull else COLORS["negative"]
            trend_bg = "rgba(0,210,160,0.08)" if is_bull else "rgba(255,107,107,0.08)"
            trend_glow = COLORS["positive_glow"] if is_bull else COLORS["negative_glow"]

            # CE/PE pair label for card title
            _ce_sym_raw = sig.get("ce_symbol", "")
            _pe_sym_raw = sig.get("pe_symbol", "")
            _ce_short = _ce_sym_raw.split(":")[-1] if _ce_sym_raw else ""
            _pe_short = _pe_sym_raw.split(":")[-1] if _pe_sym_raw else ""

            ce = sig.get("ce", {})
            pe = sig.get("pe", {})
            idx = sig.get("idx", {})

            # Trend SHA data
            ce_t = sig.get("ce_trend", {})
            pe_t = sig.get("pe_trend", {})
            idx_t = sig.get("idx_trend_sha", {})

            # GAP% data
            ce_gap_data = sig.get("ce_gap", {})
            pe_gap_data = sig.get("pe_gap", {})
            idx_gap_data = sig.get("idx_gap", {})
            ce_gap_pct = ce_gap_data.get("gap_pct", 0.0) if isinstance(ce_gap_data, dict) else 0.0
            pe_gap_pct = pe_gap_data.get("gap_pct", 0.0) if isinstance(pe_gap_data, dict) else 0.0
            idx_gap_pct = idx_gap_data.get("gap_pct", 0.0) if isinstance(idx_gap_data, dict) else 0.0

            # SHA Relationship data
            ce_rel = sig.get("ce_relationship", {})
            pe_rel = sig.get("pe_relationship", {})
            idx_rel = sig.get("idx_relationship", {})

            col_hdr = {"fontSize": "0.65rem", "color": COLORS["text_dim"],
                       "fontWeight": "600", "letterSpacing": "0.5px"}
            row_divider = html.Hr(style={
                "border": "none",
                "borderTop": f"1px solid {COLORS['divider']}",
                "margin": "0"})

            card = html.Details(
                open=True,
                className="signal-card-collapse",
                style={
                    "background": COLORS["card_solid"],
                    "borderRadius": "14px", "marginBottom": "16px",
                    "overflow": "hidden",
                    "border": f"1px solid {COLORS['card_border']}",
                    "boxShadow": f"0 4px 20px rgba(0,0,0,0.2), 0 0 30px {trend_glow}",
                    "transition": "all 0.3s ease",
                },
                children=[
                    html.Summary(style={
                        "display": "flex", "justifyContent": "space-between",
                        "alignItems": "center", "padding": "12px 18px",
                        "background": trend_bg,
                        "borderBottom": f"2px solid {trend_color}",
                        "cursor": "pointer",
                        "listStyle": "none",
                        "WebkitAppearance": "none",
                        "outline": "none",
                        "userSelect": "none",
                    }, children=[
                        html.Div(style={
                            "display": "flex", "flexDirection": "column",
                        }, children=[
                            html.Span(sym_key, style={
                                "fontWeight": "700", "fontSize": "0.95rem",
                                "color": COLORS["text"], "letterSpacing": "1px"}),
                            html.Span(
                                f"CE: {_ce_short}  /  PE: {_pe_short}" if _ce_short else "Awaiting pair",
                                style={
                                    "fontSize": "0.6rem", "fontWeight": "500",
                                    "color": COLORS["text_dim"],
                                    "letterSpacing": "0.3px", "marginTop": "2px",
                                    "fontFamily": "'JetBrains Mono', monospace",
                                }),
                        ]),
                        html.Span(
                            ("📈 " if is_bull else "📉 ") + idx_trend,
                            style={"color": trend_color, "fontWeight": "700",
                                   "fontSize": "0.8rem",
                                   "background": COLORS["card_solid"],
                                   "padding": "3px 14px", "borderRadius": "20px",
                                   "boxShadow": f"0 0 12px {trend_glow}"}),
                    ]),
                    # ── Signal SHA section ─────────────────────────────────
                    html.Div(style={
                        "padding": "10px 18px 0",
                    }, children=[
                        html.Span(f"\U0001f4ca SIGNAL SHA ({SHA_LENGTH})", style={
                            "fontSize": "0.72rem", "fontWeight": "700",
                            "color": COLORS["text_dim"], "letterSpacing": "0.5px"}),
                    ]),
                    html.Div(style={
                        "display": "grid", "gridTemplateColumns": "64px 1fr 1fr",
                        "gap": "8px", "padding": "6px 18px 0",
                    }, children=[
                        html.Span(""),
                        html.Span("POWER", style=col_hdr),
                        html.Span("CANDLES", style=col_hdr),
                    ]),
                    html.Div(style={"padding": "0 18px 12px"}, children=[
                        _signal_row("CE", "\U0001f535", "#5dade2",
                                    ce.get("power", 0), ce.get("list", [])),
                        row_divider,
                        _signal_row("PE", "\U0001f534", "#ff6b6b",
                                    pe.get("power", 0), pe.get("list", [])),
                        row_divider,
                        _signal_row("IDX", "\U0001f4ca", "#ffd93d",
                                    idx.get("power", 0), idx.get("list", [])),
                    ]),
                    # ── Trend SHA section ─────────────────────────────────
                    html.Div(style={
                        "padding": "0 18px",
                        "borderTop": f"1px solid {COLORS['divider']}",
                    }, children=[
                        html.Div(style={
                            "display": "flex", "justifyContent": "space-between",
                            "alignItems": "center", "padding": "10px 0 0",
                        }, children=[
                            html.Span(f"\U0001f4c8 TREND SHA ({SHA_TREND_LENGTH})", style={
                                "fontSize": "0.72rem", "fontWeight": "700",
                                "color": COLORS["text_dim"], "letterSpacing": "0.5px"}),
                        ]),
                        html.Div(style={
                            "display": "grid", "gridTemplateColumns": "64px 1fr 1fr",
                            "gap": "8px", "padding": "6px 0 0",
                        }, children=[
                            html.Span(""),
                            html.Span("POWER", style=col_hdr),
                            html.Span("CANDLES", style=col_hdr),
                        ]),
                        html.Div(style={"padding": "0 0 10px"}, children=[
                            _signal_row("CE", "\U0001f535", "#5dade2",
                                        ce_t.get("power", 0), ce_t.get("list", [])),
                            row_divider,
                            _signal_row("PE", "\U0001f534", "#ff6b6b",
                                        pe_t.get("power", 0), pe_t.get("list", [])),
                            row_divider,
                            _signal_row("IDX", "\U0001f4ca", "#ffd93d",
                                        idx_t.get("power", 0), idx_t.get("list", [])),
                        ]),
                    ]),
                    # ── SHA Analysis (Gap% + Relationship combined) ───────
                    html.Div(style={
                        "padding": "10px 18px 12px",
                        "borderTop": f"1px solid {COLORS['divider']}",
                    }, children=[
                        html.Span("\U0001f50d SHA ANALYSIS  (Signal \u2194 Trend)", style={
                            "fontSize": "0.72rem", "fontWeight": "700",
                            "color": COLORS["text_dim"], "letterSpacing": "0.5px",
                            "display": "block", "marginBottom": "8px"}),
                        html.Div(style={
                            "display": "grid", "gridTemplateColumns": "1fr 1fr 1fr",
                            "gap": "8px",
                        }, children=[
                            _combined_analysis_badge("CE", ce_gap_pct, ce_rel, "#5dade2"),
                            _combined_analysis_badge("PE", pe_gap_pct, pe_rel, "#ff6b6b"),
                            _combined_analysis_badge("IDX", idx_gap_pct, idx_rel, "#ffd93d"),
                        ]),
                    ]),
                    # ── RSI section ───────────────────────────────────────
                    html.Div(style={
                        "padding": "10px 18px 12px",
                        "borderTop": f"1px solid {COLORS['divider']}",
                    }, children=[
                        html.Span("\U0001f4c9 RSI (Relative Strength Index)", style={
                            "fontSize": "0.72rem", "fontWeight": "700",
                            "color": COLORS["text_dim"], "letterSpacing": "0.5px",
                            "display": "block", "marginBottom": "8px"}),
                        # RSI 1min row
                        html.Span("1 min", style={
                            "fontSize": "0.62rem", "fontWeight": "600",
                            "color": COLORS["text_muted"], "letterSpacing": "0.3px",
                            "display": "block", "marginBottom": "4px"}),
                        html.Div(style={
                            "display": "grid", "gridTemplateColumns": "1fr 1fr 1fr",
                            "gap": "8px",
                        }, children=[
                            _rsi_badge("CE", sig.get("ce_rsi", None), "#5dade2"),
                            _rsi_badge("PE", sig.get("pe_rsi", None), "#ff6b6b"),
                            _rsi_badge("IDX", sig.get("idx_rsi", None), "#ffd93d"),
                        ]),
                        # RSI 5min row
                        html.Span("5 min", style={
                            "fontSize": "0.62rem", "fontWeight": "600",
                            "color": COLORS["text_muted"], "letterSpacing": "0.3px",
                            "display": "block", "marginTop": "10px", "marginBottom": "4px"}),
                        html.Div(style={
                            "display": "grid", "gridTemplateColumns": "1fr 1fr",
                            "gap": "8px",
                        }, children=[
                            _rsi_badge("CE", sig.get("ce_rsi_5m", None), "#5dade2"),
                            _rsi_badge("PE", sig.get("pe_rsi_5m", None), "#ff6b6b"),
                        ]),
                    ]),

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
                        # Row 2: RSI zones
                        html.Div(style={
                            "display": "flex", "flexWrap": "wrap",
                            "gap": "12px", "alignItems": "center",
                        }, children=[
                            html.Div(style={"display": "inline-flex", "gap": "5px", "alignItems": "center"}, children=[
                                html.Span("RSI:", style={"fontSize": "0.6rem", "fontWeight": "600",
                                                          "color": COLORS["text_dim"]}),
                                html.Span(style={"display": "inline-block", "width": "8px", "height": "8px",
                                                 "borderRadius": "50%", "background": "#e74c3c", "marginRight": "1px"}),
                                html.Span("Overbought (\u226570)", style={"fontSize": "0.55rem", "color": COLORS["text_dim"],
                                                          "marginRight": "4px"}),
                                html.Span(style={"display": "inline-block", "width": "8px", "height": "8px",
                                                 "borderRadius": "50%", "background": "#00d2a0", "marginRight": "1px"}),
                                html.Span("Oversold (\u226430)", style={"fontSize": "0.55rem", "color": COLORS["text_dim"],
                                                          "marginRight": "4px"}),
                                html.Span(style={"display": "inline-block", "width": "8px", "height": "8px",
                                                 "borderRadius": "50%", "background": "#f39c12", "marginRight": "1px"}),
                                html.Span("Neutral", style={"fontSize": "0.55rem", "color": COLORS["text_dim"]}),
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
            html.Div(f"No strategy events for {_log_date}", style={"fontSize": "13px"}),
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
        # Live mode: show last 7 days from profit_history
        available_dates = _get_available_chart_dates(history_data)
        today = datetime.now().strftime("%Y-%m-%d")
        date_options = []
        for d in available_dates:
            if d == today:
                label = f"Today ({d})"
            else:
                label = d
            date_options.append({"label": label, "value": d})

        if selected_chart_date and selected_chart_date in available_dates:
            date_value = selected_chart_date
        elif available_dates:
            date_value = available_dates[0]
        else:
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
        kpi_cards, pos_table, traded_table, signal_cards, log_entries,
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
