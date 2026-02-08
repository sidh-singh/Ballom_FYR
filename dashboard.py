"""
dashboard.py — Live Dash / Plotly dashboard for Ballom_FYR.

Reads JSON state files from C:/Ballom_FYR/state/<mode>/ and displays:
  • Account balance, realized / unrealized P&L
  • Open positions table
  • SHA signal strength, power, list, crossover per symbol
  • Strategy decision log (rolling)

Launch:
    python dashboard.py               → auto-detect mode, http://127.0.0.1:8050
    python dashboard.py demo          → force demo mode
    python dashboard.py live          → force live mode
    python dashboard.py demo 8060     → demo mode on port 8060
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


# ═══════════════════════════════════════════════════════════════════════════════
#  MODE DETECTION
# ═══════════════════════════════════════════════════════════════════════════════

def _detect_active_mode() -> str:
    """
    Auto-detect which mode is running by comparing timestamps in
    app_status.json for demo vs live.  Returns the more recently updated one.
    Falls back to 'demo' if neither exists.
    """
    demo_file = STATE_DIR_DEMO / "app_status.json"
    live_file = STATE_DIR_LIVE / "app_status.json"
    demo_ts = demo_file.stat().st_mtime if demo_file.exists() else 0
    live_ts = live_file.stat().st_mtime if live_file.exists() else 0
    if live_ts > demo_ts:
        return "live"
    return "demo"


def _resolve_state_paths(mode: str) -> dict:
    """Return a dict of state-file Path objects for the given mode."""
    d = get_state_dir(mode)
    return {
        "app_status":        d / "app_status.json",
        "signal_state":      d / "signal_state.json",
        "position_state":    d / "position_state.json",
        "account_state":     d / "account_state.json",
        "strategy_log":      d / "strategy_log.json",
        "position_tracker":  d / "position_tracker.json",
        "profit_history":    d / "profit_history.json",
    }


# ── resolve mode from CLI or auto-detect ──────────────────────────────────────
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


# ═══════════════════════════════════════════════════════════════════════════════
#  HELPERS
# ═══════════════════════════════════════════════════════════════════════════════

def _read(path: Path):
    if not path.exists():
        return {}
    try:
        with open(path, "r") as f:
            return json.load(f)
    except Exception:
        return {}


def _kpi_card(title: str, value: str, color: str = "#18bc9c") -> html.Div:
    """Return a styled KPI card element."""
    return html.Div(
        children=[
            html.H6(title, style={"margin": 0, "color": "#999", "fontSize": "0.85rem"}),
            html.H3(value, style={"margin": 0, "color": color, "fontWeight": "700"}),
        ],
        style={
            "background": "#1e1e2f",
            "borderRadius": "12px",
            "padding": "18px 22px",
            "flex": "1",
            "minWidth": "180px",
            "boxShadow": "0 2px 8px rgba(0,0,0,0.25)",
        },
    )


def _crossover_dots(cross_list: list, max_items: int = 7) -> html.Div:
    """Render a crossover list as 7 rectangular bar segments (like Power).

    Each value in cross_list is in {-3, -2, -1, 0, 1, 2, 3}.
    Positive → green shades  (1 = light, 2 = medium, 3 = dark)
    Negative → red shades    (−1 = light, −2 = medium, −3 = dark)
    Zero     → dim placeholder
    """
    green_shades = {1: "#82e0aa", 2: "#27ae60", 3: "#0d6b3a"}  # light, med, dark
    red_shades   = {1: "#f1948a", 2: "#e74c3c", 3: "#922b21"}

    bars = []
    for i in range(max_items):
        v = cross_list[i] if i < len(cross_list) else 0
        if v > 0:
            c = green_shades.get(min(abs(v), 3), "#82e0aa")
        elif v < 0:
            c = red_shades.get(min(abs(v), 3), "#f1948a")
        else:
            c = "#2c2c3e"
        bars.append(html.Span(style={
            "display": "inline-block", "width": "8px", "height": "16px",
            "borderRadius": "2px", "background": c,
            "marginRight": "2px",
        }))

    return html.Div(bars, style={"display": "inline-flex", "alignItems": "center"})


def _power_bar(power: int, max_power: int = 7) -> html.Div:
    """Compact inline power gauge with colored segments."""
    dots = []
    for i in range(max_power):
        if i < power:
            c = "#18bc9c" if power >= 5 else "#f39c12" if power >= 3 else "#e74c3c"
        else:
            c = "#2c2c3e"
        dots.append(html.Span(style={
            "display": "inline-block", "width": "8px", "height": "16px",
            "borderRadius": "2px", "background": c, "marginRight": "2px",
        }))
    return html.Div(
        children=[*dots, html.Span(f" {power}", style={
            "fontSize": "0.75rem", "fontWeight": "700", "marginLeft": "4px",
            "color": "#18bc9c" if power >= 5 else "#f39c12" if power >= 3 else "#e74c3c",
        })],
        style={"display": "inline-flex", "alignItems": "center"},
    )


def _list_dots(lst: list, max_items: int = 7) -> html.Div:
    """Render the bullish/bearish list as colored circle dots."""
    dots = []
    for i, v in enumerate(lst[:max_items]):
        is_bull = v == 1
        c = "#18bc9c" if is_bull else "#e74c3c"
        opacity = 1.0 - (i * 0.08)
        dots.append(html.Span(style={
            "display": "inline-block", "width": "10px", "height": "10px",
            "borderRadius": "50%", "background": c,
            "marginRight": "3px", "opacity": str(opacity),
        }))
    return html.Div(dots, style={"display": "inline-flex", "alignItems": "center"})


def _signal_row(label: str, icon: str, color: str,
                power: int, lst: list, cross_list: list) -> html.Div:
    """One compact row for CE / PE / IDX in the signal card."""
    return html.Div(
        style={
            "display": "grid",
            "gridTemplateColumns": "60px 1fr 1fr 1fr",
            "gap": "8px", "alignItems": "center",
            "padding": "6px 0",
        },
        children=[
            html.Span(f"{icon} {label}", style={
                "fontWeight": "700", "fontSize": "0.8rem", "color": color,
            }),
            _power_bar(power),
            _list_dots(lst),
            _crossover_dots(cross_list),
        ],
    )


def _action_badge(action: str) -> html.Span:
    """Compact colored pill badge for a strategy action."""
    act_upper = action.upper()
    if "MARTINGALE" in act_upper:
        bg, fg = "#9b59b6", "#f0e6f6"
        icon = "⚡"
    elif "EXIT" in act_upper or "CLOSE" in act_upper:
        if "PROFIT" in act_upper:
            bg, fg = "#18bc9c", "#0d2f25"
            icon = "💰"
        elif "ADVERSE" in act_upper:
            bg, fg = "#e67e22", "#3a2412"
            icon = "⚠️"
        else:
            bg, fg = "#3498db", "#12283a"
            icon = "🔄"
    elif "BUY" in act_upper:
        bg, fg = "#27ae60", "#122a1c"
        icon = "🟢"
    elif "SELL" in act_upper:
        bg, fg = "#e74c3c", "#3a1212"
        icon = "🔴"
    elif "ANALYSIS" in act_upper or "EVAL" in act_upper:
        bg, fg = "#34495e", "#bdc3c7"
        icon = "🔍"
    elif "BLOCKED" in act_upper or "BRAKE" in act_upper:
        bg, fg = "#7f8c8d", "#ecf0f1"
        icon = "🚫"
    else:
        bg, fg = "#2c3e50", "#bdc3c7"
        icon = "📌"
    return html.Span(f"{icon} {action}", style={
        "background": bg, "color": fg,
        "padding": "2px 8px", "borderRadius": "8px",
        "fontSize": "0.7rem", "fontWeight": "600",
        "whiteSpace": "nowrap",
    })


def _build_profit_chart(history_data: list) -> go.Figure:
    """Build a Plotly line chart of effective P&L over time per symbol."""
    empty_layout = dict(
        template="plotly_dark",
        paper_bgcolor="#121225",
        plot_bgcolor="#1e1e2f",
        height=300,
        margin=dict(l=40, r=20, t=10, b=40),
    )

    if not history_data:
        fig = go.Figure()
        fig.update_layout(**empty_layout)
        fig.add_annotation(text="No profit data yet", showarrow=False,
                           font=dict(size=14, color="#666"),
                           xref="paper", yref="paper", x=0.5, y=0.5)
        return fig

    today = datetime.now().strftime("%Y-%m-%d")
    today_data = [
        e for e in history_data
        if e.get("date") == today
        and e.get("action") in ("SNAPSHOT", "CLOSE", "MARTINGALE")
    ]

    if not today_data:
        fig = go.Figure()
        fig.update_layout(**empty_layout)
        fig.add_annotation(text="No data for today yet", showarrow=False,
                           font=dict(size=14, color="#666"),
                           xref="paper", yref="paper", x=0.5, y=0.5)
        return fig

    # Group by symbol
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
    colors = ["#18bc9c", "#e74c3c", "#f39c12", "#3498db", "#9b59b6", "#1abc9c"]

    for i, (sym, data) in enumerate(symbols.items()):
        color = colors[i % len(colors)]
        short_name = sym.split(":")[-1] if ":" in sym else sym

        fig.add_trace(go.Scatter(
            x=data["x"], y=data["y"],
            mode="lines",
            name=short_name,
            line=dict(color=color, width=2),
        ))

        if data["close_x"]:
            fig.add_trace(go.Scatter(
                x=data["close_x"], y=data["close_y"],
                mode="markers",
                name=f"{short_name} ★ close",
                marker=dict(color=color, size=10, symbol="star"),
                showlegend=False,
            ))

        if data["mg_x"]:
            fig.add_trace(go.Scatter(
                x=data["mg_x"], y=data["mg_y"],
                mode="markers",
                name=f"{short_name} ◆ martingale",
                marker=dict(color="#9b59b6", size=9, symbol="diamond"),
                showlegend=False,
            ))

    fig.add_hline(y=0, line_dash="dash", line_color="#666", opacity=0.5)
    fig.update_layout(
        **empty_layout,
        xaxis=dict(title="Time", showgrid=True, gridcolor="#2c2c3e"),
        yaxis=dict(title="Effective P&L (₹)", showgrid=True, gridcolor="#2c2c3e"),
        legend=dict(orientation="h", y=-0.25),
    )
    return fig


# ═══════════════════════════════════════════════════════════════════════════════
#  DASH APP
# ═══════════════════════════════════════════════════════════════════════════════

app = dash.Dash(
    __name__,
    title="Ballom FYR — Dashboard",
    update_title=None,
)

# ── colour theme ───────────────────────────────────────────────────────────────
BG = "#121225"
CARD_BG = "#1e1e2f"
TEXT = "#ecf0f1"
ACCENT = "#18bc9c"
RED = "#e74c3c"
YELLOW = "#f39c12"

app.layout = html.Div(
    style={
        "fontFamily": "'Segoe UI', Roboto, sans-serif",
        "backgroundColor": BG,
        "color": TEXT,
        "minHeight": "100vh",
        "padding": "20px 30px",
    },
    children=[
        # ── header ────────────────────────────────────────────────────────
        html.Div(
            style={"display": "flex", "alignItems": "center", "gap": "16px", "marginBottom": "10px"},
            children=[
                html.H1("📊 Ballom FYR", style={"margin": 0, "fontSize": "1.8rem"}),
                html.Div(id="app-status-badge", style={
                    "background": ACCENT, "padding": "4px 14px", "borderRadius": "20px",
                    "fontSize": "0.8rem", "fontWeight": "600",
                }),
                html.Div(id="app-mode-badge", style={
                    "background": YELLOW, "color": "#000", "padding": "4px 14px",
                    "borderRadius": "20px", "fontSize": "0.8rem", "fontWeight": "600",
                }),
                # ── mode selector ────────────────────────────────────────
                dcc.Dropdown(
                    id="mode-selector",
                    options=[
                        {"label": "🎯 DEMO", "value": "demo"},
                        {"label": "⚠️  LIVE", "value": "live"},
                    ],
                    value=ACTIVE_MODE,
                    clearable=False,
                    style={
                        "width": "140px", "fontSize": "0.85rem",
                        "backgroundColor": CARD_BG, "color": "#000",
                    },
                ),
            ],
        ),
        html.P(id="last-updated", style={"color": "#666", "fontSize": "0.8rem", "marginBottom": "20px"}),

        # ── KPI row ───────────────────────────────────────────────────────
        html.Div(
            id="kpi-row",
            style={"display": "flex", "gap": "16px", "flexWrap": "wrap", "marginBottom": "24px"},
        ),

        # ── daily trading stats ───────────────────────────────────────────
        html.Div(
            id="daily-stats-row",
            style={"display": "flex", "gap": "16px", "flexWrap": "wrap", "marginBottom": "24px"},
        ),

        # ── profit history chart ──────────────────────────────────────────
        html.Div(
            style={
                "background": "#1e1e2f", "borderRadius": "12px",
                "padding": "16px", "marginBottom": "24px",
            },
            children=[
                html.H4("📈 Profit History", style={"marginBottom": "10px"}),
                dcc.Graph(id="profit-chart", config={"displayModeBar": False}),
            ],
        ),

        # ── two-column layout ─────────────────────────────────────────────
        html.Div(
            style={"display": "flex", "gap": "20px", "flexWrap": "wrap"},
            children=[
                # LEFT: positions + signals
                html.Div(
                    style={"flex": "2", "minWidth": "500px"},
                    children=[
                        # Open Positions
                        html.H4("📋 Open Positions", style={"marginBottom": "10px"}),
                        html.Div(id="positions-table-container"),

                        # SHA Signals
                        html.H4("🎯 SHA Signal Analysis", style={"marginTop": "24px", "marginBottom": "10px"}),
                        html.Div(id="signal-cards-container"),
                    ],
                ),

                # RIGHT: strategy log
                html.Div(
                    style={"flex": "1", "minWidth": "350px"},
                    children=[
                        html.H4("📝 Strategy Log", style={"marginBottom": "10px"}),
                        html.Div(
                            id="strategy-log-container",
                            style={
                                "background": CARD_BG,
                                "borderRadius": "12px",
                                "padding": "14px",
                                "maxHeight": "600px",
                                "overflowY": "auto",
                            },
                        ),
                    ],
                ),
            ],
        ),

        # ── auto-refresh timer ────────────────────────────────────────────
        dcc.Interval(id="refresh-timer", interval=DASHBOARD_REFRESH_MS, n_intervals=0),
    ],
)


# ═══════════════════════════════════════════════════════════════════════════════
#  CALLBACKS
# ═══════════════════════════════════════════════════════════════════════════════

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
    ],
    [Input("refresh-timer", "n_intervals"),
     Input("mode-selector", "value")],
)
def refresh_dashboard(_n, selected_mode):
    # Resolve state file paths based on the dropdown selection
    paths = _resolve_state_paths(selected_mode or ACTIVE_MODE)

    app_data = _read(paths["app_status"])
    acct_data = _read(paths["account_state"])
    pos_data = _read(paths["position_state"])
    sig_data = _read(paths["signal_state"])
    log_data = _read(paths["strategy_log"])
    tracker_data = _read(paths["position_tracker"])
    history_raw = _read(paths["profit_history"])
    history_data = history_raw if isinstance(history_raw, list) else []

    # ── header badges ─────────────────────────────────────────────────────
    status_text = app_data.get("status", "offline").upper()
    mode_text = (selected_mode or ACTIVE_MODE).upper()
    last_ts = acct_data.get("timestamp", app_data.get("timestamp", "—"))

    # ── KPI cards ─────────────────────────────────────────────────────────
    balance = acct_data.get("balance", 0)
    realized = acct_data.get("realized_pnl", 0)
    unrealized = acct_data.get("unrealized_pnl", 0)
    total_pnl = acct_data.get("total_pnl", 0)
    win_rate = acct_data.get("win_rate", 0)
    total_trades = acct_data.get("total_trades", 0)

    pnl_color = ACCENT if total_pnl >= 0 else RED
    real_color = ACCENT if realized >= 0 else RED
    unreal_color = ACCENT if unrealized >= 0 else RED

    kpi_cards = [
        _kpi_card("Balance", f"₹{balance:,.2f}"),
        _kpi_card("Realized P&L", f"₹{realized:,.2f}", real_color),
        _kpi_card("Unrealized P&L", f"₹{unrealized:,.2f}", unreal_color),
        _kpi_card("Total P&L", f"₹{total_pnl:,.2f}", pnl_color),
        _kpi_card("Win Rate", f"{win_rate:.1f}%", ACCENT if win_rate > 50 else RED),
        _kpi_card("Total Trades", str(total_trades)),
    ]

    # ── positions table ───────────────────────────────────────────────────
    positions = pos_data.get("positions", [])
    if positions:
        cols = ["symbol", "netQty", "netAvg", "ltp", "unrealized_profit", "productType"]
        rows = [{c: p.get(c, "") for c in cols} for p in positions]
        pos_table = dash_table.DataTable(
            data=rows,
            columns=[
                {"name": "Symbol", "id": "symbol"},
                {"name": "Qty", "id": "netQty"},
                {"name": "Avg Price", "id": "netAvg"},
                {"name": "LTP", "id": "ltp"},
                {"name": "Unrealized P&L", "id": "unrealized_profit"},
                {"name": "Product", "id": "productType"},
            ],
            style_table={"overflowX": "auto"},
            style_header={
                "backgroundColor": "#2c2c3e", "color": TEXT,
                "fontWeight": "600", "border": "none",
            },
            style_cell={
                "backgroundColor": CARD_BG, "color": TEXT,
                "border": "1px solid #2c2c3e", "padding": "10px",
                "fontSize": "0.85rem",
            },
            style_data_conditional=[
                {"if": {"filter_query": "{unrealized_profit} > 0", "column_id": "unrealized_profit"},
                 "color": ACCENT, "fontWeight": "bold"},
                {"if": {"filter_query": "{unrealized_profit} < 0", "column_id": "unrealized_profit"},
                 "color": RED, "fontWeight": "bold"},
            ],
        )
    else:
        pos_table = html.Div(
            "No open positions",
            style={"background": CARD_BG, "borderRadius": "12px", "padding": "18px",
                   "color": "#666", "textAlign": "center"},
        )

    # ── signal cards ──────────────────────────────────────────────────────
    signal_cards = []
    if isinstance(sig_data, dict):
        for sym_key, sig in sig_data.items():
            idx_trend = sig.get("idx_trend", "—")
            is_bull = idx_trend == "BULLISH"
            trend_color = ACCENT if is_bull else RED
            trend_bg = "#0d2f25" if is_bull else "#3a1212"

            ce = sig.get("ce", {})
            pe = sig.get("pe", {})
            idx = sig.get("idx", {})

            ce_cross = ce.get("crossover", [])
            pe_cross = pe.get("crossover", [])
            idx_cross = idx.get("crossover", [])

            card = html.Div(
                style={
                    "background": CARD_BG, "borderRadius": "12px",
                    "marginBottom": "14px", "overflow": "hidden",
                },
                children=[
                    # ── header bar ────────────────────────────────────────
                    html.Div(
                        style={
                            "display": "flex", "justifyContent": "space-between",
                            "alignItems": "center", "padding": "10px 16px",
                            "background": trend_bg,
                            "borderBottom": f"2px solid {trend_color}",
                        },
                        children=[
                            html.Span(sym_key, style={
                                "fontWeight": "700", "fontSize": "0.95rem",
                            }),
                            html.Span(
                                f"{'📈' if is_bull else '📉'} {idx_trend}",
                                style={
                                    "color": trend_color, "fontWeight": "700",
                                    "fontSize": "0.8rem",
                                    "background": CARD_BG,
                                    "padding": "2px 10px", "borderRadius": "10px",
                                },
                            ),
                        ],
                    ),
                    # ── column headers ────────────────────────────────────
                    html.Div(
                        style={
                            "display": "grid",
                            "gridTemplateColumns": "60px 1fr 1fr 1fr",
                            "gap": "8px", "padding": "8px 16px 0",
                        },
                        children=[
                            html.Span("", style={"fontSize": "0.65rem"}),
                            html.Span("POWER", style={
                                "fontSize": "0.65rem", "color": "#666",
                                "fontWeight": "600", "letterSpacing": "0.5px",
                            }),
                            html.Span("CANDLES", style={
                                "fontSize": "0.65rem", "color": "#666",
                                "fontWeight": "600", "letterSpacing": "0.5px",
                            }),
                            html.Span("CROSSOVER", style={
                                "fontSize": "0.65rem", "color": "#666",
                                "fontWeight": "600", "letterSpacing": "0.5px",
                            }),
                        ],
                    ),
                    # ── signal rows ───────────────────────────────────────
                    html.Div(style={"padding": "0 16px 10px"}, children=[
                        _signal_row("CE",  "🔵", "#5dade2",
                                    ce.get("power", 0),
                                    ce.get("list", []),
                                    ce_cross),
                        html.Hr(style={
                            "border": "none", "borderTop": "1px solid #2c2c3e",
                            "margin": "0",
                        }),
                        _signal_row("PE",  "🔴", "#e74c3c",
                                    pe.get("power", 0),
                                    pe.get("list", []),
                                    pe_cross),
                        html.Hr(style={
                            "border": "none", "borderTop": "1px solid #2c2c3e",
                            "margin": "0",
                        }),
                        _signal_row("IDX", "📊", "#f39c12",
                                    idx.get("power", 0),
                                    idx.get("list", []),
                                    idx_cross),
                    ]),
                    # ── footer timestamp ──────────────────────────────────
                    html.Div(
                        sig.get("timestamp", "—"),
                        style={
                            "fontSize": "0.65rem", "color": "#555",
                            "padding": "4px 16px 8px", "textAlign": "right",
                        },
                    ),
                ],
            )
            signal_cards.append(card)

    if not signal_cards:
        signal_cards = [html.Div(
            "No signal data yet",
            style={"background": CARD_BG, "borderRadius": "12px", "padding": "18px",
                   "color": "#666", "textAlign": "center"},
        )]

    # ── strategy log (timeline view) ─────────────────────────────────────
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
            details = entry.get("details", "")

            # Skip ANALYSIS entries for a cleaner log
            if action == "ANALYSIS":
                continue

            # P&L indicator
            pl_children = []
            if pl_val != 0:
                pl_col = ACCENT if pl_val > 0 else RED
                pl_children = [html.Span(
                    f"₹{pl_val:,.0f}",
                    style={"color": pl_col, "fontWeight": "700", "fontSize": "0.75rem"},
                )]

            row = html.Div(
                style={
                    "display": "grid",
                    "gridTemplateColumns": "52px 1fr auto",
                    "gap": "8px", "alignItems": "start",
                    "padding": "7px 0",
                    "borderBottom": "1px solid #1a1a2e",
                },
                children=[
                    # time column
                    html.Span(ts_short, style={
                        "color": "#555", "fontSize": "0.68rem",
                        "fontFamily": "monospace", "paddingTop": "2px",
                    }),
                    # main content
                    html.Div(children=[
                        html.Div(
                            style={"display": "flex", "gap": "6px",
                                   "alignItems": "center", "flexWrap": "wrap"},
                            children=[
                                _action_badge(action),
                                html.Span(leg, style={
                                    "color": "#5dade2" if leg == "CE" else "#e74c3c" if leg == "PE" else "#f39c12",
                                    "fontWeight": "700", "fontSize": "0.72rem",
                                }) if leg and leg not in ("EVAL", "CHECK") else None,
                                html.Span(sym_short, style={
                                    "color": "#888", "fontSize": "0.7rem",
                                }) if sym_short else None,
                            ],
                        ),
                        html.Span(
                            f"qty: {qty_val}" if qty_val else "",
                            style={"color": "#666", "fontSize": "0.68rem"},
                        ) if qty_val else None,
                    ]),
                    # P&L column
                    html.Div(children=pl_children,
                             style={"textAlign": "right", "minWidth": "50px"}),
                ],
            )
            log_entries.append(row)

    if not log_entries:
        log_entries = [html.Div("No strategy events yet",
                                style={"color": "#666", "textAlign": "center",
                                       "padding": "20px"})]

    # ── daily trading stats (from position tracker) ───────────────────────
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
    booked_color = ACCENT if daily_booked >= 0 else RED

    daily_stats_cards = [
        _kpi_card("Today's Booked Profit", f"₹{daily_booked:,.2f}", booked_color),
        _kpi_card("Avg Profit / Close", f"₹{daily_avg:,.2f}",
                  ACCENT if daily_avg > 0 else RED),
        _kpi_card("Closes Today", str(daily_closes)),
        _kpi_card("Martingale Adds", str(daily_martingales),
                  "#9b59b6" if daily_martingales > 0 else "#666"),
    ]

    # ── profit history line chart ─────────────────────────────────────────
    profit_fig = _build_profit_chart(history_data)

    return (
        status_text,
        mode_text,
        f"Last updated: {last_ts}",
        kpi_cards,
        pos_table,
        signal_cards,
        log_entries,
        daily_stats_cards,
        profit_fig,
    )


# ═══════════════════════════════════════════════════════════════════════════════
#  MAIN
# ═══════════════════════════════════════════════════════════════════════════════

if __name__ == "__main__":
    # Ensure both state directories exist so dashboard doesn't crash on first load
    STATE_DIR_DEMO.mkdir(parents=True, exist_ok=True)
    STATE_DIR_LIVE.mkdir(parents=True, exist_ok=True)

    print(f"📊 Ballom FYR Dashboard starting on http://127.0.0.1:{_port_arg}")
    print(f"   Monitoring mode: {ACTIVE_MODE.upper()}")
    print(f"   State dir: {get_state_dir(ACTIVE_MODE)}")
    print(f"   (Use the dropdown to switch between DEMO / LIVE)\n")

    app.run(debug=False, host="0.0.0.0", port=_port_arg)
