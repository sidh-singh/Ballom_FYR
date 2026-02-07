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
        "app_status":     d / "app_status.json",
        "signal_state":   d / "signal_state.json",
        "position_state": d / "position_state.json",
        "account_state":  d / "account_state.json",
        "strategy_log":   d / "strategy_log.json",
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


def _crossover_badge(val: int) -> str:
    """Return emoji + label for a crossover value."""
    mapping = {
        3: "🔥 Strong Bull (+3)",
        2: "⚡ Mid Bull (+2)",
        1: "💨 Weak Bull (+1)",
        -1: "💨 Weak Bear (-1)",
        -2: "⚡ Mid Bear (-2)",
        -3: "🔥 Strong Bear (-3)",
    }
    return mapping.get(val, str(val))


def _power_bar(power: int, max_power: int = 7) -> go.Figure:
    """Horizontal bar gauge for power (0-7)."""
    color = "#18bc9c" if power >= 5 else "#f39c12" if power >= 3 else "#e74c3c"
    fig = go.Figure(go.Bar(
        x=[power], y=[""], orientation="h",
        marker_color=color, text=[f"{power}/{max_power}"], textposition="auto",
    ))
    fig.update_layout(
        xaxis=dict(range=[0, max_power], showticklabels=False, showgrid=False),
        yaxis=dict(showticklabels=False),
        margin=dict(l=0, r=0, t=0, b=0),
        height=30,
        paper_bgcolor="rgba(0,0,0,0)",
        plot_bgcolor="rgba(0,0,0,0)",
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
            trend_color = ACCENT if idx_trend == "BULLISH" else RED

            ce = sig.get("ce", {})
            pe = sig.get("pe", {})
            idx = sig.get("idx", {})

            ce_cross_val = ce.get("crossover", [0])[0] if ce.get("crossover") else 0
            pe_cross_val = pe.get("crossover", [0])[0] if pe.get("crossover") else 0
            idx_cross_val = idx.get("crossover", [0])[0] if idx.get("crossover") else 0

            card = html.Div(
                style={
                    "background": CARD_BG, "borderRadius": "12px", "padding": "16px",
                    "marginBottom": "14px", "borderLeft": f"4px solid {trend_color}",
                },
                children=[
                    html.Div(
                        style={"display": "flex", "justifyContent": "space-between", "alignItems": "center"},
                        children=[
                            html.H5(sym_key, style={"margin": 0}),
                            html.Span(
                                f"{'📈' if idx_trend == 'BULLISH' else '📉'} {idx_trend}",
                                style={"color": trend_color, "fontWeight": "bold", "fontSize": "0.9rem"},
                            ),
                        ],
                    ),
                    html.P(f"Updated: {sig.get('timestamp', '—')}",
                           style={"color": "#666", "fontSize": "0.75rem", "margin": "4px 0 12px"}),

                    # CE row
                    html.Div(
                        style={"display": "flex", "gap": "16px", "marginBottom": "8px", "alignItems": "center"},
                        children=[
                            html.Span("🔵 CE", style={"fontWeight": "bold", "width": "50px"}),
                            html.Span(f"Power: {ce.get('power', 0)}/7", style={"width": "90px"}),
                            html.Span(
                                f"List: {''.join(str(x) for x in ce.get('list', [])[:7])}",
                                style={"fontFamily": "monospace", "width": "120px"},
                            ),
                            html.Span(
                                _crossover_badge(ce_cross_val),
                                style={"fontSize": "0.85rem"},
                            ),
                        ],
                    ),
                    # PE row
                    html.Div(
                        style={"display": "flex", "gap": "16px", "marginBottom": "8px", "alignItems": "center"},
                        children=[
                            html.Span("🔴 PE", style={"fontWeight": "bold", "width": "50px"}),
                            html.Span(f"Power: {pe.get('power', 0)}/7", style={"width": "90px"}),
                            html.Span(
                                f"List: {''.join(str(x) for x in pe.get('list', [])[:7])}",
                                style={"fontFamily": "monospace", "width": "120px"},
                            ),
                            html.Span(
                                _crossover_badge(pe_cross_val),
                                style={"fontSize": "0.85rem"},
                            ),
                        ],
                    ),
                    # Index row
                    html.Div(
                        style={"display": "flex", "gap": "16px", "alignItems": "center"},
                        children=[
                            html.Span("📊 IDX", style={"fontWeight": "bold", "width": "50px"}),
                            html.Span(f"Power: {idx.get('power', 0)}/7", style={"width": "90px"}),
                            html.Span(
                                f"List: {''.join(str(x) for x in idx.get('list', [])[:7])}",
                                style={"fontFamily": "monospace", "width": "120px"},
                            ),
                            html.Span(
                                _crossover_badge(idx_cross_val),
                                style={"fontSize": "0.85rem"},
                            ),
                        ],
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

    # ── strategy log ──────────────────────────────────────────────────────
    log_entries = []
    if isinstance(log_data, list):
        for entry in reversed(log_data[-50:]):
            action = entry.get("action", "")
            color = ACCENT if "BUY" in action else RED if "SELL" in action else YELLOW

            log_entries.append(html.Div(
                style={
                    "borderBottom": "1px solid #2c2c3e",
                    "padding": "8px 0",
                    "fontSize": "0.8rem",
                },
                children=[
                    html.Div(
                        style={"display": "flex", "justifyContent": "space-between"},
                        children=[
                            html.Span(
                                f"{entry.get('leg', '')} {action}",
                                style={"color": color, "fontWeight": "600"},
                            ),
                            html.Span(
                                entry.get("timestamp", ""),
                                style={"color": "#666", "fontSize": "0.7rem"},
                            ),
                        ],
                    ),
                    html.Div(
                        f"{entry.get('symbol', '')} | qty={entry.get('qty', 0)} | "
                        f"P&L=₹{entry.get('pl', 0):,.2f}",
                        style={"color": "#aaa", "fontSize": "0.75rem"},
                    ),
                    html.Div(
                        entry.get("details", ""),
                        style={"color": "#777", "fontSize": "0.72rem", "fontStyle": "italic"},
                    ) if entry.get("details") else None,
                ],
            ))

    if not log_entries:
        log_entries = [html.Div("No strategy events yet", style={"color": "#666", "textAlign": "center"})]

    return (
        status_text,
        mode_text,
        f"Last updated: {last_ts}",
        kpi_cards,
        pos_table,
        signal_cards,
        log_entries,
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
