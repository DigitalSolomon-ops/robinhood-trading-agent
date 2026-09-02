from __future__ import annotations

import html
import json
import os
import sqlite3
import uuid
from datetime import UTC, datetime
from pathlib import Path
from urllib.parse import parse_qs
from typing import Any

import yaml
from dotenv import load_dotenv
from fastapi import FastAPI, Form, Request
from fastapi.responses import FileResponse, HTMLResponse, RedirectResponse, Response

from .equity_runtime import equity_kill_switch
from .intelligence import (
    collect_intelligence as collect_intelligence_layer,
    export_intelligence_report as export_intelligence_report_layer,
    intelligence_status as intelligence_status_layer,
    score_all_symbols as score_all_symbols_layer,
)
from .intelligence.intelligence_store import IntelligenceStore
from .kill_switch import KillSwitch
from .live_broker import LiveBroker
from .logger import SQLiteLogger
from .market_hours import EASTERN, blocked_reason as market_blocked_reason
from .main import (
    ROOT,
    _best_price_from_payload,
    _manual_preview_signal,
    build_live_smoke_preview,
    build_parser,
    cancel_open_live_orders_dashboard,
    clear_stop_trading,
    create_stop_trading,
    export_live_audit,
    live_launch_readiness,
    load_settings,
    load_symbol_validation,
    make_client,
    reconcile_live_orders,
    return_to_paper_mode,
    risk_portfolio_for_mode,
    run_bounded_live_iteration,
    run_cycle,
    run_live_smoke_test,
    runtime_mode,
    select_account_number,
    symbol_lists,
    validate_symbols,
)
from .order_manager import OrderManager
from .paper_broker import PaperBroker
from .portfolio import Portfolio
from .risk_manager import RiskManager
from .shared_state import build_arm_store
from .web_security import install_security_middleware

SECRET_MARKERS = ("ROBINHOOD_API_KEY", "ROBINHOOD_PRIVATE_KEY", "PRIVATE_KEY", "API_KEY")

# --- Per-lane arm / disarm toggle -------------------------------------------
# The toggle delegates to the shared ArmStore, whose meaning is per lane: crypto
# and equities are ARMED when their stop file is ABSENT (disarm writes it), while
# the leveraged OPTIONS lane is POSITIVE and fail-closed -- ARMED only when its
# own ARM_STATE marker is PRESENT (arm writes that marker, a DIFFERENT file from
# the kill-switch stop file; the default with no marker is DISARMED). Disarming
# is one click (the safe direction); arming requires an explicit confirm. NOTE:
# this enables/halts a lane's loop -- it does NOT switch a lane from paper to
# LIVE, which stays a separate, higher gate.
TRADING_LANES = ("crypto", "equities", "options")


def _lane_stop_paths(root: Path, rules: dict[str, Any]) -> dict[str, tuple[str, Path]]:
    """Each lane's stop-file path, matching the kill-switch conventions:
    crypto = top-level kill_switch.stop_file (STOP_TRADING); equities/options =
    their own <lane>.kill_switch.stop_file (STOP_TRADING_EQUITIES/_OPTIONS)."""
    crypto = root / ((rules.get("kill_switch") or {}).get("stop_file", "STOP_TRADING"))
    equities = root / (((rules.get("equities") or {}).get("kill_switch") or {}).get("stop_file", "STOP_TRADING_EQUITIES"))
    options = root / (((rules.get("options") or {}).get("kill_switch") or {}).get("stop_file", "STOP_TRADING_OPTIONS"))
    return {"crypto": ("Crypto", crypto), "equities": ("Equities", equities), "options": ("Options", options)}


def _arm_panel_html(root: Path, rules: dict[str, Any]) -> str:
    """Render the per-lane arm/disarm panel: current posture + the one control."""
    rows: list[str] = []
    store = build_arm_store(root, rules)
    for lane, (label, _path) in _lane_stop_paths(root, rules).items():
        disarmed = not store.is_armed(lane)
        if disarmed:
            state, color = "DISARMED — halted", "#166534"
            control = (
                f'<form method="post" action="/kill/{lane}/arm" style="display:inline">'
                f'<label><input type="checkbox" name="confirm_arm"> confirm</label> '
                f'<button type="submit">Arm {html.escape(label)}</button></form>'
            )
        else:
            state, color = "ARMED — trading enabled", "#b91c1c"
            control = (
                f'<form method="post" action="/kill/{lane}/disarm" style="display:inline">'
                f'<button type="submit">Disarm {html.escape(label)}</button></form>'
            )
        rows.append(
            f'<tr><td><b>{html.escape(label)}</b></td>'
            f'<td style="color:{color};font-weight:600">{state}</td><td>{control}</td></tr>'
        )
    note = (
        "<p><b>This toggle enables or halts a lane's trading loop.</b> Disarming is one click "
        "(the safe direction). Arming requires the confirm box. Switching a lane from paper to "
        "<b>LIVE</b> is a separate, higher gate — this control does not do it.</p>"
    )
    return note + '<table cellpadding="8">' + "".join(rows) + "</table>"


def dashboard_app(root: Path = ROOT) -> FastAPI:
    app = FastAPI(title="Digital Solomon Crypto Agent")
    app.state.root = Path(root)
    app.state.smoke_previews = {}
    install_security_middleware(app)

    @app.get("/healthz")
    def healthz() -> dict[str, str]:
        """Load-balancer health check.

        Deliberately reads nothing: no database, no config file, and above all
        no Robinhood API call. The home page does all three, so pointing a
        health check at "/" would sign an authenticated broker request every
        few seconds from a single egress IP.
        """
        return {"status": "ok"}

    @app.get("/health")
    def health() -> dict[str, str]:
        # Cloud Run / GFE intercept the exact path /healthz before the container
        # sees it, so the LB + Cloud Run health check must target /health instead.
        # Same contract as /healthz: reads nothing, signs no broker request.
        return {"status": "ok"}

    @app.get("/", response_class=HTMLResponse)
    def home() -> str:
        return page("Safety Status", home_html(app.state.root))

    @app.get("/settings", response_class=HTMLResponse)
    def settings() -> str:
        return page("Settings", settings_html(app.state.root))

    @app.post("/settings", response_class=HTMLResponse)
    async def save_settings(request: Request) -> HTMLResponse:
        form = await parse_form(request)
        errors = save_dashboard_settings(app.state.root, form)
        if errors:
            return HTMLResponse(page("Settings", settings_html(app.state.root, errors)), status_code=400)
        return RedirectResponse("/settings?saved=1", status_code=303)

    @app.get("/strategy", response_class=HTMLResponse)
    def strategy() -> str:
        return page("Strategy", strategy_html(app.state.root))

    @app.get("/kill", response_class=HTMLResponse)
    def kill() -> str:
        return page("Kill Switch", kill_html(app.state.root))

    @app.post("/kill/stop")
    def kill_stop() -> RedirectResponse:
        rules, _ = load_dashboard_settings(app.state.root)
        KillSwitch(stop_file=str(app.state.root / rules.get("kill_switch", {}).get("stop_file", "STOP_TRADING"))).create_stop_file()
        SQLiteLogger(app.state.root / "data" / "trading_agent.db").log_decision(None, "dashboard_stop_trading", "STOP_TRADING created from dashboard")
        return RedirectResponse("/kill", status_code=303)

    @app.post("/kill/clear")
    async def kill_clear(request: Request) -> RedirectResponse:
        form = await parse_form(request)
        if form.get("confirm_clear") == "on":
            rules, _ = load_dashboard_settings(app.state.root)
            stop_path = app.state.root / rules.get("kill_switch", {}).get("stop_file", "STOP_TRADING")
            if stop_path.exists():
                stop_path.unlink()
            SQLiteLogger(app.state.root / "data" / "trading_agent.db").log_decision(None, "dashboard_clear_stop", "STOP_TRADING cleared from dashboard")
        return RedirectResponse("/kill", status_code=303)

    @app.get("/arm", response_class=HTMLResponse)
    def arm_panel() -> str:
        rules, _ = load_dashboard_settings(app.state.root)
        return page("Arm / Disarm", _arm_panel_html(app.state.root, rules))

    @app.post("/kill/{lane}/disarm")
    def lane_disarm(lane: str) -> RedirectResponse:
        # Disarm = HALT the lane. Safe direction, one click. Backend-agnostic
        # through the shared arm store: for crypto/equities this writes the stop
        # file, for the options lane it removes the positive ARM_STATE marker.
        rules, _ = load_dashboard_settings(app.state.root)
        if lane in _lane_stop_paths(app.state.root, rules):
            build_arm_store(app.state.root, rules).set_armed(lane, False, by="dashboard")
            SQLiteLogger(app.state.root / "data" / "trading_agent.db").log_decision(
                None, f"dashboard_disarm_{lane}", f"{lane} lane DISARMED from dashboard"
            )
        return RedirectResponse("/arm", status_code=303)

    @app.post("/kill/{lane}/arm")
    async def lane_arm(lane: str, request: Request) -> RedirectResponse:
        # Arm = ENABLE the lane. For crypto/equities this removes the stop file;
        # for the options lane it WRITES the positive ARM_STATE marker (a separate
        # file from the kill switch). Requires an explicit confirm; only a human
        # request through the (IAP-gated) dashboard reaches this route.
        form = await parse_form(request)
        rules, _ = load_dashboard_settings(app.state.root)
        if lane in _lane_stop_paths(app.state.root, rules) and form.get("confirm_arm") == "on":
            build_arm_store(app.state.root, rules).set_armed(lane, True, by="dashboard")
            SQLiteLogger(app.state.root / "data" / "trading_agent.db").log_decision(
                None, f"dashboard_arm_{lane}", f"{lane} lane ARMED from dashboard"
            )
        return RedirectResponse("/arm", status_code=303)

    @app.get("/preview", response_class=HTMLResponse)
    def preview() -> str:
        return page("Preview", preview_html())

    @app.post("/preview", response_class=HTMLResponse)
    async def preview_action(request: Request) -> str:
        form = await parse_form(request)
        action = str(form.get("action", ""))
        symbol = str(form.get("symbol", "BTC-USD")).upper()
        amount = float(form.get("amount_usd") or 1)
        result = run_preview_action(app.state.root, action, symbol, amount)
        return page("Preview", preview_html(result))

    @app.get("/logs", response_class=HTMLResponse)
    def logs() -> str:
        return page("Logs", logs_html(app.state.root))

    @app.get("/audit", response_class=HTMLResponse)
    def audit() -> str:
        return page("Audit", audit_html(app.state.root))

    @app.post("/audit", response_class=HTMLResponse)
    def audit_action() -> str:
        result = {"submitted": False, "status": "audit exported", "path": str(export_live_audit(limit=200, root=app.state.root))}
        return page("Audit", audit_html(app.state.root, result))

    @app.get("/symbols", response_class=HTMLResponse)
    def symbols() -> str:
        return page("Symbols", symbols_html(app.state.root))

    @app.post("/symbols", response_class=HTMLResponse)
    def validate_dashboard_symbols() -> str:
        try:
            result = validate_symbols(app.state.root)
        except SystemExit as exc:
            result = {"submitted": False, "status": "blocked", "error": str(exc)}
        return page("Symbols", symbols_html(app.state.root, result))

    @app.get("/intelligence", response_class=HTMLResponse)
    def intelligence() -> str:
        return page("Intelligence", intelligence_html(app.state.root))

    @app.post("/intelligence", response_class=HTMLResponse)
    async def intelligence_action(request: Request) -> str:
        form = await parse_form(request)
        result = run_intelligence_action(app.state.root, str(form.get("action", "")))
        return page("Intelligence", intelligence_html(app.state.root, result))

    @app.get("/equities", response_class=HTMLResponse)
    def equities() -> str:
        return page("Equities Lane", equities_html(app.state.root))

    @app.get("/scout-reports", response_class=HTMLResponse)
    def scout_reports(request: Request) -> str:
        # ANALYSIS/DISPLAY ONLY: renders the scouts' persisted daily plays.
        # No order path, no trading gate is reachable from here.
        day = request.query_params.get("day")
        return page("Scout Reports", scout_reports_html(app.state.root, day))

    @app.get("/calibration", response_class=HTMLResponse)
    def calibration() -> str:
        # ANALYSIS/DISPLAY ONLY: predicted-vs-realized accuracy over settled plays.
        return page("Scout Calibration", calibration_html(app.state.root))

    @app.get("/scout-recipients", response_class=HTMLResponse)
    def scout_recipients() -> str:
        return page("Report Recipients", scout_recipients_html(app.state.root))

    @app.post("/scout-recipients", response_class=HTMLResponse)
    async def scout_recipients_action(request: Request) -> str:
        form = await parse_form(request)
        result = run_recipient_action(app.state.root, form)
        return page("Report Recipients", scout_recipients_html(app.state.root, result))

    @app.get("/live-readiness", response_class=HTMLResponse)
    def live_readiness() -> str:
        return page("Live Readiness", live_readiness_html(app.state.root))

    @app.post("/live-readiness", response_class=HTMLResponse)
    async def live_readiness_action(request: Request) -> str:
        form = await parse_form(request)
        result = run_live_readiness_action(app.state.root, str(form.get("action", "")))
        return page("Live Readiness", live_readiness_html(app.state.root, result))

    @app.get("/live-control", response_class=HTMLResponse)
    def live_control() -> str:
        return page("Live Control Center", live_control_html(app.state.root, app.state.smoke_previews))

    @app.post("/live-control", response_class=HTMLResponse)
    async def live_control_action(request: Request) -> str:
        form = await parse_form(request)
        result = run_live_control_action(app.state.root, form, app.state.smoke_previews)
        return page("Live Control Center", live_control_html(app.state.root, app.state.smoke_previews, result))

    @app.get("/help", response_class=HTMLResponse)
    def help_page() -> str:
        return page("Instructions / Help", help_html())

    @app.get("/assets/logo.png")
    def logo() -> Response:
        logo_path = app.state.root / "assets" / "logo.png"
        if logo_path.exists():
            return FileResponse(logo_path, media_type="image/png")
        return Response(status_code=404)

    return app


async def parse_form(request: Request) -> dict[str, str]:
    body = (await request.body()).decode("utf-8")
    parsed = parse_qs(body, keep_blank_values=True)
    return {key: values[-1] if values else "" for key, values in parsed.items()}


def load_dashboard_settings(root: Path) -> tuple[dict[str, Any], dict[str, Any]]:
    load_dotenv(root / ".env", override=True)
    rules = read_yaml(root / "config" / "trading_rules.yaml")
    strategy = read_yaml(root / "config" / "strategy.yaml")
    return rules, strategy


def read_yaml(path: Path) -> dict[str, Any]:
    if not path.exists():
        return {}
    with path.open("r", encoding="utf-8") as handle:
        return yaml.safe_load(handle) or {}


def write_yaml(path: Path, data: dict[str, Any]) -> None:
    with path.open("w", encoding="utf-8") as handle:
        yaml.safe_dump(data, handle, sort_keys=False)


def read_env(path: Path) -> dict[str, str]:
    values: dict[str, str] = {}
    if not path.exists():
        return values
    for line in path.read_text(encoding="utf-8").splitlines():
        if not line or line.strip().startswith("#") or "=" not in line:
            continue
        key, value = line.split("=", 1)
        values[key.strip()] = value.strip()
    return values


def write_env_allowed(path: Path, updates: dict[str, str]) -> None:
    allowed = {"TRADING_MODE", "TRADING_ENABLED", "POLL_INTERVAL_SECONDS"}
    existing_lines = path.read_text(encoding="utf-8").splitlines() if path.exists() else []
    seen: set[str] = set()
    output: list[str] = []
    for line in existing_lines:
        if not line or line.strip().startswith("#") or "=" not in line:
            output.append(line)
            continue
        key, _ = line.split("=", 1)
        key = key.strip()
        if key in allowed and key in updates:
            output.append(f"{key}={updates[key]}")
            seen.add(key)
        else:
            output.append(line)
    for key, value in updates.items():
        if key in allowed and key not in seen:
            output.append(f"{key}={value}")
    path.write_text("\n".join(output) + "\n", encoding="utf-8")


def display_mode(mode: str) -> str:
    if mode == "paper":
        return "SAFE: Paper Mode"
    if mode in {"dry-run", "live-dry-run"}:
        return "CAUTION: Dry Run"
    if mode == "live":
        return "DANGER: Live Mode"
    return mode


def readiness_banner(mode: str) -> str:
    if mode == "live":
        return '<p class="danger">DANGER: LIVE MODE CAN PLACE REAL ROBINHOOD CRYPTO ORDERS</p>'
    if mode in {"dry-run", "live-dry-run"}:
        return '<p class="warn">CAUTION: DRY-RUN MODE - PREVIEWS ONLY</p>'
    return '<p class="safe">SAFE: PAPER MODE ONLY</p>'


def mode_from_form(value: str) -> str:
    return "live-dry-run" if value == "dry-run" else value


def form_from_mode(value: str) -> str:
    return "dry-run" if value == "live-dry-run" else value


def save_dashboard_settings(root: Path, form: dict[str, Any]) -> list[str]:
    errors: list[str] = []
    rules, strategy = load_dashboard_settings(root)
    env_mode = mode_from_form(str(form.get("trading_mode", "paper")))
    config_mode = mode_from_form(str(form.get("config_mode", env_mode)))
    live_selected = "live" in {env_mode, config_mode}
    if live_selected and form.get("live_confirm") != "on":
        return ["Live mode requires confirmation."]

    trading = rules.setdefault("trading", {})
    risk = rules.setdefault("risk", {})
    symbols_root = rules.setdefault("symbols", {})
    strategy_root = strategy.setdefault("strategy", {})
    profiles = strategy_profiles(strategy)
    selected_profile = str(form.get("active_strategy_profile", strategy_root.get("active_profile", ""))).strip()
    if selected_profile not in profiles:
        return [f"Unknown strategy profile: {selected_profile}"]
    trading["enabled"] = str(form.get("config_enabled", "false")).lower() == "true"
    trading["mode"] = config_mode
    trading["allowed_symbols"] = parse_symbols(str(form.get("allowed_symbols", "")))
    risk["max_trade_amount_usd"] = parse_float(form.get("max_trade_amount_usd"), "max_trade_amount_usd", errors)
    risk["max_daily_loss_usd"] = parse_float(form.get("max_daily_loss_usd"), "max_daily_loss_usd", errors)
    risk["max_trades_per_day"] = parse_int(form.get("max_trades_per_day"), "max_trades_per_day", errors)
    risk["max_open_positions"] = parse_int(form.get("max_open_positions", risk.get("max_open_positions", 10)), "max_open_positions", errors)
    risk["max_symbol_allocation_percent"] = parse_float(
        form.get("max_symbol_allocation_percent", risk.get("max_symbol_allocation_percent", 25)),
        "max_symbol_allocation_percent",
        errors,
    )
    risk["min_order_cooldown_seconds"] = parse_int(
        form.get("min_order_cooldown_seconds", risk.get("min_order_cooldown_seconds", 300)),
        "min_order_cooldown_seconds",
        errors,
    )
    risk["allow_margin"] = False
    risk["allow_shorting"] = False
    risk["allow_shorts"] = False
    risk["allow_position_scaling"] = False
    risk["require_live_order_reconciliation"] = True
    symbols_root["research_watchlist"] = parse_symbols(str(form.get("research_watchlist", ",".join(symbol_lists(rules)["research_watchlist"]))))
    symbols_root["paper_allowed_symbols"] = parse_symbols(str(form.get("paper_allowed_symbols", ",".join(symbol_lists(rules)["paper_allowed_symbols"]))))
    symbols_root["live_allowed_symbols"] = parse_symbols(str(form.get("live_allowed_symbols", ",".join(symbol_lists(rules)["live_allowed_symbols"]))))
    strategy_root["active_profile"] = selected_profile

    if live_selected:
        risk["max_trade_amount_usd"] = min(float(risk.get("max_trade_amount_usd") or 100), 100.0)
        risk["max_daily_loss_usd"] = min(float(risk.get("max_daily_loss_usd") or 100), 100.0)
        risk["max_trades_per_day"] = min(int(risk.get("max_trades_per_day") or 5), 5)
        risk["max_open_positions"] = min(int(risk.get("max_open_positions") or 10), 10)
        risk["max_symbol_allocation_percent"] = min(float(risk.get("max_symbol_allocation_percent") or 25), 25.0)
        risk["min_order_cooldown_seconds"] = max(int(risk.get("min_order_cooldown_seconds") or 300), 300)
        trading["allowed_symbols"] = symbols_root["live_allowed_symbols"]

    if errors:
        return errors

    write_env_allowed(
        root / ".env",
        {
            "TRADING_MODE": env_mode,
            "TRADING_ENABLED": "true" if str(form.get("env_enabled", "false")).lower() == "true" else "false",
            "POLL_INTERVAL_SECONDS": str(parse_int(form.get("poll_interval_seconds"), "poll_interval_seconds", errors) or 60),
        },
    )
    write_yaml(root / "config" / "trading_rules.yaml", rules)
    write_yaml(root / "config" / "strategy.yaml", strategy)
    SQLiteLogger(root / "data" / "trading_agent.db").log_decision(None, "dashboard_settings_saved", "settings saved from dashboard")
    return errors


def parse_float(value: Any, field: str, errors: list[str]) -> float:
    try:
        return float(value)
    except (TypeError, ValueError):
        errors.append(f"{field} must be numeric")
        return 0.0


def parse_int(value: Any, field: str, errors: list[str]) -> int:
    try:
        return int(value)
    except (TypeError, ValueError):
        errors.append(f"{field} must be an integer")
        return 0


def parse_symbols(value: str) -> list[str]:
    return [symbol.strip().upper() for symbol in value.replace("\n", ",").split(",") if symbol.strip()]


def strategy_profiles(strategy: dict[str, Any]) -> dict[str, dict[str, Any]]:
    profiles = strategy.get("profiles", {})
    if not isinstance(profiles, dict):
        return {}
    return {str(name): profile for name, profile in profiles.items() if isinstance(profile, dict)}


def active_profile_name(strategy: dict[str, Any]) -> str:
    strategy_root = strategy.get("strategy", {})
    return str(strategy_root.get("active_profile", ""))


def home_snapshot(root: Path) -> dict[str, Any]:
    rules, strategy = load_dashboard_settings(root)
    logger = SQLiteLogger(root / "data" / "trading_agent.db")
    paper_broker = PaperBroker(root / "data" / "paper_trades.db")
    kill = KillSwitch(stop_file=str(root / rules.get("kill_switch", {}).get("stop_file", "STOP_TRADING")))
    mode = runtime_mode(None, rules)
    summary = logger.get_daily_summary()
    external_positions = 0
    client = make_client()
    if client.has_credentials:
        try:
            account_payload = client.get_accounts()
            account_number = select_account_number(account_payload)
            holdings_payload = client.get_holdings(account_number)
            external_positions = Portfolio.from_robinhood(account_payload, holdings_payload).open_position_count
        except Exception as exc:
            logger.log_error("dashboard_external_positions", str(exc))
    return {
        "Current Mode": display_mode(mode),
        "Trading Enabled": os.getenv("TRADING_ENABLED", "false"),
        "Config Enabled": rules.get("trading", {}).get("enabled", False),
        "STOP_TRADING Exists": kill.stop_file_exists(),
        "Max Trade Amount": rules.get("risk", {}).get("max_trade_amount_usd"),
        "Max Daily Loss": rules.get("risk", {}).get("max_daily_loss_usd"),
        "Max Daily Trades": rules.get("risk", {}).get("max_trades_per_day"),
        "Max Symbol Allocation Percent": rules.get("risk", {}).get("max_symbol_allocation_percent"),
        "Order Cooldown Seconds": rules.get("risk", {}).get("min_order_cooldown_seconds"),
        "Allowed Symbols": ", ".join(rules.get("trading", {}).get("allowed_symbols", [])),
        "Active Strategy Profile": active_profile_name(strategy),
        "Open Paper Positions": paper_broker.get_portfolio().open_position_count,
        "External Robinhood Positions": external_positions,
        "Last Decision": logger.get_last_decision(),
        "Daily Trade Count": summary["trade_count"],
        "Daily Realized PnL": summary["realized_pnl"],
        "Daily Blocked Count": summary["blocked_count"],
        "Submitted Live Orders Today": submitted_live_orders_today(root),
    }


def submitted_live_orders_today(root: Path) -> int:
    db_path = root / "data" / "trading_agent.db"
    if not db_path.exists():
        return 0
    today = datetime.now(UTC).date().isoformat()
    with sqlite3.connect(db_path) as conn:
        try:
            decisions = conn.execute(
                """
                SELECT COUNT(*)
                FROM decisions
                WHERE substr(timestamp, 1, 10) = ?
                  AND action IN ('live_order_submitted', 'live_smoke_submitted', 'live_single_trade_submitted')
                """,
                (today,),
            ).fetchone()[0]
            orders = conn.execute(
                """
                SELECT COUNT(*)
                FROM orders
                WHERE substr(timestamp, 1, 10) = ?
                  AND status = 'submitted'
                """,
                (today,),
            ).fetchone()[0]
        except sqlite3.Error:
            return 0
    return int(decisions or 0) + int(orders or 0)


def paper_negative_positions(root: Path) -> dict[str, float]:
    portfolio = PaperBroker(root / "data" / "paper_trades.db").get_portfolio()
    return {
        symbol: float(position.quantity)
        for symbol, position in portfolio.positions.items()
        if float(position.quantity) < -1e-12
    }


def cli_command_exists(command_name: str) -> bool:
    parser = build_parser()
    subparsers_action = next(action for action in parser._actions if action.dest == "command")
    return command_name in subparsers_action.choices


def readiness_status(condition: bool, name: str, detail: str = "") -> dict[str, str]:
    return {"name": name, "status": "PASS" if condition else "FAIL", "detail": detail}


def readiness_warning(name: str, detail: str) -> dict[str, str]:
    return {"name": name, "status": "WARNING", "detail": detail}


def readiness_checks(root: Path, connection_result: dict[str, Any] | None = None) -> list[dict[str, str]]:
    rules, _ = load_dashboard_settings(root)
    env = read_env(root / ".env")
    trading = rules.get("trading", {})
    risk = rules.get("risk", {})
    kill = KillSwitch(stop_file=str(root / rules.get("kill_switch", {}).get("stop_file", "STOP_TRADING")))
    max_trade = float(risk.get("max_trade_amount_usd", 0) or 0)
    max_loss = float(risk.get("max_daily_loss_usd", 0) or 0)
    max_trades = int(risk.get("max_trades_per_day", 0) or 0)
    max_allocation = float(risk.get("max_symbol_allocation_percent", 0) or 0)
    cooldown = int(risk.get("min_order_cooldown_seconds", 0) or 0)
    lists = symbol_lists(rules)
    live_symbols = lists["live_allowed_symbols"]
    validation = load_symbol_validation(root)
    validated = [symbol for symbol in validation.get("available_for_live", []) if symbol in live_symbols]
    unavailable = [symbol for symbol in validation.get("unavailable", []) if symbol in live_symbols]
    unknown = [symbol for symbol in validation.get("unknown", []) if symbol in live_symbols]
    unvalidated = [symbol for symbol in live_symbols if symbol not in validated and symbol not in unavailable and symbol not in unknown]
    negative_positions = paper_negative_positions(root)
    submitted_live_count = submitted_live_orders_today(root)
    checks = [
        readiness_warning("pytest last known status", "Run pytest from PowerShell before any live attempt."),
        readiness_warning("Robinhood API connection passed", "Not checked from this page load."),
        readiness_status(env.get("TRADING_MODE") == "live", "TRADING_MODE value", env.get("TRADING_MODE", "missing")),
        readiness_status(env.get("TRADING_ENABLED", "").lower() == "true", "TRADING_ENABLED value", env.get("TRADING_ENABLED", "missing")),
        readiness_status(trading.get("mode") == "live", "config trading.mode", str(trading.get("mode"))),
        readiness_status(bool(trading.get("enabled", False)), "config trading.enabled", str(trading.get("enabled", False))),
        readiness_status(not kill.stop_file_exists(), "STOP_TRADING exists", str(kill.stop_file_exists())),
        readiness_status(max_trade <= 100, "max_trade_amount_usd <= 100", str(max_trade)),
        readiness_status(max_loss <= 100, "max_daily_loss_usd <= 100", str(max_loss)),
        readiness_status(max_trades <= 5, "max_trades_per_day <= 5", str(max_trades)),
        readiness_status(int(risk.get("max_open_positions", 0) or 0) <= 10, "max_open_positions <= 10", str(risk.get("max_open_positions"))),
        readiness_status(0 < max_allocation <= 25, "max_symbol_allocation_percent configured <= 25", str(max_allocation)),
        readiness_status(cooldown >= 300, "min_order_cooldown_seconds >= 300", str(cooldown)),
        readiness_status(bool(risk.get("require_live_order_reconciliation", False)), "require_live_order_reconciliation", str(risk.get("require_live_order_reconciliation", False))),
        readiness_status(not unvalidated, "unvalidated live symbols", ", ".join(unvalidated)),
        readiness_status(not unavailable, "unsupported live symbols", ", ".join(unavailable)),
        readiness_status(not unknown, "unknown live symbols", ", ".join(unknown)),
        readiness_status(bool(validated), "validated live symbols", ", ".join(validated)),
        readiness_status(not negative_positions, "no negative paper positions", json.dumps(negative_positions)),
        readiness_status(submitted_live_count == 0, "no submitted live orders today", str(submitted_live_count)),
        readiness_status(cli_command_exists("stop"), "kill switch command exists", "python -m src.main stop"),
        readiness_status(
            cli_command_exists("live-smoke-buy") and cli_command_exists("live-smoke-sell"),
            "live-smoke-buy/sell commands exist",
            "available" if cli_command_exists("live-smoke-buy") and cli_command_exists("live-smoke-sell") else "missing",
        ),
        readiness_status(True, "dashboard secrets hidden", "API keys, private keys, .env contents, and account numbers are not displayed."),
    ]
    if connection_result is not None:
        status = "PASS" if connection_result.get("ok") else "FAIL"
        checks[1] = {"name": "Robinhood API connection passed", "status": status, "detail": str(connection_result.get("detail", ""))}
    return checks


def run_preview_action(root: Path, action: str, symbol: str, amount_usd: float) -> dict[str, Any]:
    rules, strategy = load_dashboard_settings(root)
    logger = SQLiteLogger(root / "data" / "trading_agent.db")
    if action == "run-paper-once":
        run_cycle("paper", rules, strategy)
        return {"submitted": False, "action": action, "status": "paper cycle completed"}
    if action == "run-dry-once":
        run_cycle("live-dry-run", rules, strategy)
        return {"submitted": False, "action": action, "status": "dry-run cycle completed"}

    side = "sell" if action == "preview-sell" else "buy"
    client = make_client()
    if not client.has_credentials:
        return {"submitted": False, "risk_allowed": False, "risk_reasons": ["API credentials are missing"], "order_payload": None}
    market_payload = client.get_best_bid_ask(symbol)
    price = _best_price_from_payload(market_payload, symbol, side)
    if price is None:
        return {"submitted": False, "risk_allowed": False, "risk_reasons": ["market data unavailable"], "order_payload": None}
    paper_portfolio = PaperBroker(root / "data" / "paper_trades.db").get_portfolio()
    portfolio = risk_portfolio_for_mode("live-dry-run", rules, paper_portfolio, Portfolio(cash_usd=100000, positions=paper_portfolio.positions))
    signal = _manual_preview_signal(rules, symbol, side, f"dashboard_{action}")
    kill = KillSwitch(stop_file=str(root / rules.get("kill_switch", {}).get("stop_file", "STOP_TRADING")))
    risk = RiskManager(rules, kill)
    order = OrderManager(rules, risk, logger, PaperBroker(root / "data" / "paper_trades.db")).build_limit_order(signal, price, portfolio, amount_usd=amount_usd)
    decision = risk.evaluate(
        signal,
        "live-dry-run",
        float(order["notional"]),
        portfolio,
        logger.get_daily_summary(),
        client.has_credentials,
        False,
        float(order["quantity"]),
        current_price=price,
        last_order_timestamp=(logger.get_last_order(symbol) or {}).get("timestamp"),
    )
    payload = None
    if decision.allowed:
        payload = LiveBroker(client, dry_run=True).place_limit_order(order).get("order_payload")
        payload = scrub_secrets(payload)
    result = {
        "submitted": False,
        "risk_allowed": decision.allowed,
        "risk_reasons": decision.reasons,
        "payload_preview": payload,
    }
    logger.log_decision(symbol, "dashboard_preview", "dashboard preview generated without submitting", result)
    return result


def run_live_readiness_action(root: Path, action: str) -> dict[str, Any]:
    logger = SQLiteLogger(root / "data" / "trading_agent.db")
    if action == "test-connection":
        client = make_client()
        if not client.has_credentials:
            return {"submitted": False, "ok": False, "action": action, "detail": "API credentials are missing"}
        try:
            payload = client.get_accounts()
            results = payload.get("results", []) if isinstance(payload, dict) else []
            result = {
                "submitted": False,
                "ok": True,
                "action": action,
                "accounts_found": len(results),
                "buying_power_present": any(
                    isinstance(item, dict) and (item.get("buying_power") is not None or item.get("cash_available_for_trading") is not None)
                    for item in results
                ),
                "detail": "read-only account request succeeded",
            }
            logger.log_decision(None, "dashboard_test_connection", "read-only connection test succeeded", result)
            return scrub_secrets(result)
        except Exception as exc:
            result = {"submitted": False, "ok": False, "action": action, "detail": str(exc)}
            logger.log_error("dashboard_test_connection", str(exc))
            logger.log_decision(None, "dashboard_test_connection_failed", str(exc), result)
            return scrub_secrets(result)
    if action == "preview-buy":
        return run_preview_action(root, "preview-buy", "BTC-USD", 1)
    if action == "preview-sell":
        return run_preview_action(root, "preview-sell", "BTC-USD", 1)
    if action == "smoke-buy":
        return {
            "submitted": False,
            "action": action,
            "status": "manual_supervised_only",
            "command": "python -m src.main live-smoke-buy BTC-USD 1 --confirm-live-smoke",
            "typed_confirmation_required": "I UNDERSTAND THIS WILL PLACE A REAL $1 CRYPTO ORDER",
            "note": "The dashboard does not submit smoke orders or bypass typed confirmation.",
        }
    if action == "return-paper":
        rules, strategy = load_dashboard_settings(root)
        rules.setdefault("trading", {})["enabled"] = True
        rules.setdefault("trading", {})["mode"] = "paper"
        write_env_allowed(root / ".env", {"TRADING_MODE": "paper", "TRADING_ENABLED": "true"})
        write_yaml(root / "config" / "trading_rules.yaml", rules)
        write_yaml(root / "config" / "strategy.yaml", strategy)
        logger.log_decision(None, "dashboard_return_to_paper", "dashboard returned env and config to paper mode")
        return {"submitted": False, "action": action, "status": "paper mode restored"}
    if action == "reconcile-live-orders":
        try:
            client = make_client()
            if not client.has_credentials:
                raise SystemExit("API credentials are missing")
            account_payload = client.get_accounts()
            account_number = select_account_number(account_payload)
            orders_payload = client.get_orders(account_number)
            rows = orders_payload.get("results", []) if isinstance(orders_payload, dict) else []
            open_states = {"open", "queued", "new", "confirmed", "unconfirmed", "partially_filled"}
            result = {
                "submitted": False,
                "action": action,
                "orders_seen": len(rows),
                "open_live_orders": sum(1 for row in rows if str(row.get("state") or row.get("status") or "").lower() in open_states),
                "statuses": sorted({str(row.get("state") or row.get("status") or "unknown") for row in rows}),
            }
            logger.log_decision(None, "dashboard_live_order_reconciliation", "read-only dashboard live order reconciliation completed", result)
            return {**result, "action": action}
        except SystemExit as exc:
            return {"submitted": False, "action": action, "status": "blocked", "error": str(exc)}
        except Exception as exc:
            logger.log_error("dashboard_live_order_reconciliation", str(exc))
            return {"submitted": False, "action": action, "status": "blocked", "error": str(exc)}
    if action == "export-live-audit":
        path = export_live_audit(limit=200, root=root)
        return {"submitted": False, "action": action, "status": "audit exported", "path": str(path)}
    if action == "create-stop":
        rules, _ = load_dashboard_settings(root)
        KillSwitch(stop_file=str(root / rules.get("kill_switch", {}).get("stop_file", "STOP_TRADING"))).create_stop_file()
        logger.log_decision(None, "dashboard_stop_trading", "STOP_TRADING created from live readiness")
        return {"submitted": False, "action": action, "status": "STOP_TRADING created"}
    return {"submitted": False, "action": action, "status": "unknown action"}


def run_live_control_action(root: Path, form: dict[str, str], smoke_previews: dict[str, dict[str, Any]]) -> dict[str, Any]:
    action = form.get("action", "")
    if action == "readiness":
        return {"submitted": False, "action": action, "readiness": live_launch_readiness(root=root)}
    if action == "validate-symbols":
        try:
            return {"submitted": False, "action": action, "validation": validate_symbols(root)}
        except SystemExit as exc:
            return {"submitted": False, "action": action, "status": "blocked", "error": str(exc)}
    if action == "preview-smoke":
        symbol = form.get("symbol", "BTC-USD").upper()
        side = form.get("side", "buy")
        amount = float(form.get("amount_usd") or 1)
        result = build_live_smoke_preview(symbol, side, amount, root)
        preview_id = str(uuid.uuid4())
        smoke_previews[preview_id] = result
        return {**result, "action": action, "preview_id": preview_id}
    if action == "submit-smoke":
        preview_id = form.get("preview_id", "")
        preview = smoke_previews.get(preview_id)
        if not preview:
            return {"submitted": False, "action": action, "status": "blocked", "error": "Smoke test requires a preview first"}
        if form.get("understand_smoke") != "on" or form.get("confirm_smoke_submit") != "on":
            return {
                "submitted": False,
                "action": action,
                "status": "blocked",
                "error": "Smoke test requires both confirmation checkboxes",
            }
        try:
            return run_live_smoke_test(
                str(preview["symbol"]),
                str(preview["side"]),
                float(preview["amount_usd"]),
                form.get("understand_smoke") == "on",
                form.get("confirm_smoke_submit") == "on",
                preview,
                root,
            )
        except SystemExit as exc:
            return {"submitted": False, "action": action, "status": "blocked", "error": str(exc)}
    if action == "bounded-iteration":
        if form.get("confirm_bounded_iteration") != "on":
            return {
                "submitted": False,
                "action": action,
                "status": "blocked",
                "error": "Bounded live iteration requires confirmation checkbox",
            }
        try:
            return run_bounded_live_iteration(True, root=root)
        except SystemExit as exc:
            return {"submitted": False, "action": action, "status": "blocked", "error": str(exc)}
    if action == "return-paper":
        return return_to_paper_mode(root)
    if action == "create-stop":
        return create_stop_trading(root)
    if action == "clear-stop":
        try:
            return clear_stop_trading(form.get("confirm_clear_stop") == "on", root)
        except SystemExit as exc:
            return {"submitted": False, "action": action, "status": "blocked", "error": str(exc)}
    if action == "cancel-open":
        try:
            return cancel_open_live_orders_dashboard(form.get("confirm_cancel_open") == "on", root)
        except SystemExit as exc:
            return {"submitted": False, "action": action, "status": "blocked", "error": str(exc)}
    if action == "reconcile":
        try:
            return {**reconcile_live_orders(root), "action": action}
        except SystemExit as exc:
            return {"submitted": False, "action": action, "status": "blocked", "error": str(exc)}
    if action == "export-audit":
        return {"submitted": False, "action": action, "status": "audit exported", "path": str(export_live_audit(limit=200, root=root))}
    return {"submitted": False, "action": action, "status": "unknown action"}


def run_intelligence_action(root: Path, action: str) -> dict[str, Any]:
    rules, _ = load_dashboard_settings(root)
    symbols = symbol_lists(rules)["live_allowed_symbols"]
    validation = load_symbol_validation(root)
    validated_symbols = [symbol for symbol in validation.get("available_for_live", []) if symbol in symbols] or symbols
    if action == "collect":
        return collect_intelligence_layer(root, symbols)
    if action == "score-all":
        return score_all_symbols_layer(root, validated_symbols)
    if action == "export-report":
        path = export_intelligence_report_layer(root)
        return {"submitted": False, "status": "intelligence report exported", "path": str(path)}
    return {"submitted": False, "status": "unknown action", "action": action}


def scrub_secrets(value: Any) -> Any:
    if isinstance(value, dict):
        return {
            key: ("***" if any(marker in key.upper() for marker in SECRET_MARKERS) or key == "account_number_suffix" else scrub_secrets(item))
            for key, item in value.items()
        }
    if isinstance(value, list):
        return [scrub_secrets(item) for item in value]
    return value


def recent_rows(root: Path, db_name: str, table: str, limit: int = 25) -> list[dict[str, Any]]:
    db_path = root / "data" / db_name
    if not db_path.exists():
        return []
    with sqlite3.connect(db_path) as conn:
        conn.row_factory = sqlite3.Row
        try:
            rows = conn.execute(f"SELECT * FROM {table} ORDER BY id DESC LIMIT ?", (limit,)).fetchall()
        except sqlite3.Error:
            return []
    return [scrub_secrets(dict(row)) for row in rows]


def recent_equity_decisions(root: Path, limit: int = 25) -> list[dict[str, Any]]:
    """Decisions logged by the equities lane only, newest first.

    Every decision equity_runtime/RobinhoodEquityBroker log carries an
    action prefixed "equity_" (equity_signal_skipped, equity_order_refused,
    equity_paper_loop_completed, ...), so filtering on that prefix separates
    this lane's rationale from the crypto lane's decisions in the same table
    without needing a schema change.
    """
    db_path = root / "data" / "trading_agent.db"
    if not db_path.exists():
        return []
    with sqlite3.connect(db_path) as conn:
        conn.row_factory = sqlite3.Row
        try:
            rows = conn.execute(
                "SELECT * FROM decisions WHERE action LIKE 'equity_%' ORDER BY id DESC LIMIT ?",
                (limit,),
            ).fetchall()
        except sqlite3.Error:
            return []
    return [scrub_secrets(dict(row)) for row in rows]


def equity_price_sources(root: Path) -> dict[str, int]:
    """Row count per recorded price source in the equities candle ledger.

    Read straight off the ledger so this view states what the prices behind the
    simulated fills ACTUALLY were, rather than what the lane means them to be.
    """
    db_path = root / "data" / "equity_market_data.db"
    if not db_path.exists():
        return {}
    with sqlite3.connect(db_path) as conn:
        try:
            rows = conn.execute("SELECT source, COUNT(*) FROM equity_candles GROUP BY source").fetchall()
        except sqlite3.Error:
            return {}
    return {str(row[0]): int(row[1]) for row in sorted(rows)}


def equities_positions(root: Path) -> dict[str, Any]:
    """The equities paper broker's own ledger -- the lane's only fill source
    for this build (paper_broker simulating fills against REAL Massive
    historical daily bars replayed one per cycle, never a live execution
    quote; see docs/rh-equities-binding.md). This dashboard process holds no
    live Robinhood connector (the connector is session-bound to a Claude
    agent), so live positions cannot be read from here."""
    portfolio = PaperBroker(root / "data" / "equity_paper_trades.db").get_portfolio()
    return {
        "cash_usd": portfolio.cash_usd,
        "positions": {
            symbol: {"quantity": position.quantity, "average_price": position.average_price, "pnl": position.pnl}
            for symbol, position in portfolio.positions.items()
            if abs(position.quantity) > 1e-12
        },
    }


# --- Scout Reports + Report Recipients (ANALYSIS/DISPLAY + CONFIG ONLY) -------
#
# These read the scouts' persisted daily plays and manage the report
# distribution list. They reach GCS through the entry_alerts store, whose google
# import is LAZY and guarded, so a missing library/bucket degrades to the local
# data/entry_alerts/ fallback rather than crashing the tab. Nothing here places,
# reviews, or cancels an order, and no trading gate is reachable from here.


def _scout_store() -> Any:
    """Import the entry-alerts store lazily so a broken/absent optional
    dependency degrades the tab to an empty state instead of failing app import."""
    from .entry_alerts import store as scout_store  # noqa: PLC0415 (deliberate lazy import)

    return scout_store


def _entry_alerts_bucket() -> str | None:
    """Resolve the shared entry-alerts GCS bucket (env ENTRY_ALERTS_BUCKET, then
    config/entry_alerts.yaml gcs.bucket). None -> the local-file dev fallback."""
    try:
        from .entry_alerts.config import load_alerts_config, resolve_bucket  # noqa: PLC0415

        return resolve_bucket(load_alerts_config())
    except Exception:
        return None


def _fmt_level(value: Any) -> str:
    """A price level for the report table: numeric -> trimmed, else the raw
    string, else an em dash placeholder."""
    if value is None or value == "":
        return "—"
    try:
        return f"{float(value):g}"
    except (TypeError, ValueError):
        return str(value)


def _fmt_optional(value: Any) -> str:
    return "—" if value is None or value == "" else str(value)


def _fmt_return(value: Any) -> str:
    if value is None or value == "":
        return "—"
    try:
        return f"{float(value):+.1f}%"
    except (TypeError, ValueError):
        return str(value)


def _verdict_cell(verdict: str) -> str:
    label = verdict.strip().upper() or "PENDING"
    cls = {"WIN": "PASS", "LOSS": "FAIL", "OPEN": "WARNING"}.get(label, "")
    return f'<td class="{cls}">{escape(label)}</td>'


def scout_reports_html(root: Path, selected_day: str | None = None) -> str:
    bucket = _entry_alerts_bucket()
    try:
        store = _scout_store()
        days = store.list_report_days(14, bucket=bucket)
    except Exception:
        days, store = [], None

    intro = (
        '<p class="muted">Read-only view of the scouts\' persisted daily plays '
        "(options + small-cap). Every level is on the underlying. Plan-vs-actual "
        "columns light up automatically once the settlement engine records "
        "outcomes; until then they read <b>pending</b>. This page never trades.</p>"
    )

    if not days or store is None:
        source = "GCS bucket " + escape(bucket) if bucket else "local data/entry_alerts"
        return (
            intro
            + '<p class="warn">No scout report days found yet ('
            + source
            + "). The Options and Small-Cap scouts write a day file when they run; "
            "this tab will populate on the next scout run.</p>"
        )

    active = selected_day if selected_day in days else days[0]
    day_links = "".join(
        f"<a href='/scout-reports?day={escape(d)}' class='badge' "
        f"style=\"{'background:#2563eb;color:#fff;' if d == active else ''}text-decoration:none;\">{escape(d)}</a>"
        for d in days
    )

    try:
        raw = store.load_report_raw(active, bucket=bucket)
        plays = store.report_plays(raw)
        summary = store.report_summary(raw)
    except Exception:
        raw, plays, summary = None, [], {"plays": 0, "settled": 0}

    if summary.get("settled"):
        accuracy = f"accuracy: {summary.get('accuracy_pct')}% over {summary['settled']} settled play(s)"
    else:
        accuracy = "accuracy: pending first settled runs"
    summary_line = (
        f"<p><b>{escape(active)}</b> &middot; {summary.get('plays', 0)} play(s) "
        f"&middot; {escape(accuracy)}</p>"
    )

    if not plays:
        body_table = "<p>No plays recorded for this day.</p>"
    else:
        header = (
            "<thead><tr>"
            "<th>Rank</th><th>Source</th><th>Symbol</th><th>Direction</th>"
            "<th>Entry</th><th>Target (ceiling)</th><th>Stop (floor)</th>"
            "<th>Contract</th><th>Conviction</th>"
            "<th>Actual</th><th>Verdict</th><th>Return</th>"
            "</tr></thead>"
        )
        rows = []
        for index, rec in enumerate(plays, start=1):
            rank = rec.get("rank") or index
            outcome = store.play_outcome(rec)
            if outcome is None:
                actual_cell = "<td>pending</td>"
                verdict_cell = "<td>pending</td>"
                return_cell = "<td>pending</td>"
            else:
                actual_cell = f"<td>{escape(_fmt_optional(outcome.get('actual')))}</td>"
                verdict_cell = _verdict_cell(str(outcome.get("verdict", "")))
                return_cell = (
                    f"<td>{escape(_fmt_return(outcome.get('return_pct', outcome.get('return'))))}</td>"
                )
            rows.append(
                "<tr>"
                f"<td>{escape(rank)}</td>"
                f"<td>{escape(rec.get('source', '—'))}</td>"
                f"<td><b>{escape(rec.get('symbol', '—'))}</b></td>"
                f"<td>{escape(str(rec.get('direction', '')).upper() or '—')}</td>"
                f"<td>{escape(_fmt_level(rec.get('entry')))}</td>"
                f"<td>{escape(_fmt_level(rec.get('target')))}</td>"
                f"<td>{escape(_fmt_level(rec.get('stop')))}</td>"
                f"<td>{escape(_fmt_optional(rec.get('contract') or rec.get('contract_ticker')))}</td>"
                f"<td>{escape(_fmt_optional(rec.get('conviction')))}</td>"
                f"{actual_cell}{verdict_cell}{return_cell}"
                "</tr>"
            )
        body_table = f"<table>{header}<tbody>{''.join(rows)}</tbody></table>"

    return (
        intro
        + section("Available Report Days", "The most recent days (newest first) that have a persisted plays file. Click one to view that day's report.")
        + f"<p>{day_links}</p>"
        + section("Day Report", "That day's ranked plays with the PLAN levels (entry / target / stop). The Actual, Verdict, and Return columns are scaffolded for the later settlement engine and read 'pending' until it records outcomes.")
        + summary_line
        + body_table
    )


def _fmt_rate(value: Any) -> str:
    """A 0..1 rate rendered as a percentage, or an em dash when absent."""
    return f"{value * 100:.1f}%" if isinstance(value, (int, float)) else "—"


def calibration_html(root: Path) -> str:
    """Predicted-vs-realized accuracy over settled plays. ANALYSIS/DISPLAY ONLY --
    surfaces whether the scout's conviction ranks outcomes correctly and whether
    its backtested hit-rate matches reality. Never trades, never tunes weights."""
    bucket = _entry_alerts_bucket()
    try:
        from .scout_calibration.calibration import load_calibration

        report = load_calibration(days=60, bucket=bucket)
    except Exception as exc:  # a read/compute hiccup must not crash the tab
        return f'<p class="warn">Calibration is unavailable right now ({escape(str(exc))}).</p>'

    intro = (
        '<p class="muted">How trustworthy is the scout\'s own confidence? This compares '
        "what it FORECAST (conviction, backtested hit-rate) against what the settlement "
        "engine REALIZED (target hit before stop). It only surfaces the numbers &mdash; it "
        "never trades and does not auto-tune any weight.</p>"
    )

    settled = report.get("settled", 0)
    if not settled:
        openn = report.get("open", 0)
        return (
            intro
            + '<p class="warn">No settled plays yet'
            + (f" ({openn} still open)." if openn else ".")
            + " Calibration fills in automatically as the settlement engine records "
            "WIN/LOSS outcomes after each session closes.</p>"
        )

    drift = report.get("predicted_vs_realized_drift")
    drift_txt = f"{drift * 100:+.1f} pts" if isinstance(drift, (int, float)) else "—"
    drift_cls = "warn" if report.get("drift_flag") else "muted"
    overall = (
        f"<p><b>{escape(settled)}</b> settled play(s) &middot; "
        f"<b>{escape(report.get('wins', 0))}W / {escape(report.get('losses', 0))}L</b> &middot; "
        f"realized win rate <b>{_fmt_rate(report.get('realized_win_rate'))}</b> &middot; "
        f"avg return <b>{escape(_fmt_return(report.get('avg_return_pct')))}</b> &middot; "
        f"{escape(report.get('open', 0))} still open</p>"
        f"<p class='{drift_cls}'>Backtested hit-rate predicted "
        f"<b>{_fmt_rate(report.get('avg_predicted_hit_rate'))}</b> over "
        f"{escape(report.get('n_with_prediction', 0))} play(s) with a forecast; "
        f"realized minus predicted = <b>{escape(drift_txt)}</b>"
        + ("  &mdash; &#9888; DRIFT beyond threshold." if report.get("drift_flag") else "")
        + "</p>"
    )

    mono = report.get("conviction_monotonic")
    if mono is True:
        mono_line = '<p class="safe">Conviction ranks outcomes correctly: higher-conviction tiers realize higher win rates.</p>'
    elif mono is False:
        mono_line = '<p class="warn">&#9888; Conviction is NOT monotonic &mdash; a higher-conviction tier is realizing a LOWER win rate than a lower one. The ranking signal may need review.</p>'
    else:
        mono_line = '<p class="muted">Not enough settled plays per tier yet to judge whether conviction ranks outcomes correctly.</p>'

    tier_rows = "".join(
        "<tr>"
        f"<td>{escape(t['label'])}</td>"
        f"<td>{escape(t['n'])}</td>"
        f"<td>{escape(t['wins'])}</td>"
        f"<td>{_fmt_rate(t['win_rate'])}</td>"
        f"<td>{escape(_fmt_optional(round(t['avg_conviction'], 1) if t.get('avg_conviction') is not None else None))}</td>"
        f"<td>{escape(_fmt_return(t.get('avg_return_pct')))}</td>"
        "</tr>"
        for t in report.get("tiers", [])
    )
    tier_table = (
        "<table><thead><tr><th>Conviction tier</th><th>Settled</th><th>Wins</th>"
        "<th>Win rate</th><th>Avg conviction</th><th>Avg return</th></tr></thead>"
        f"<tbody>{tier_rows}</tbody></table>"
    )

    source_rows = "".join(
        f"<tr><td>{escape(src)}</td><td>{escape(s['n'])}</td>"
        f"<td>{escape(s['wins'])}</td><td>{_fmt_rate(s['win_rate'])}</td></tr>"
        for src, s in report.get("by_source", {}).items()
    )
    source_table = (
        "<table><thead><tr><th>Source</th><th>Settled</th><th>Wins</th><th>Win rate</th></tr></thead>"
        f"<tbody>{source_rows}</tbody></table>"
        if source_rows
        else "<p class='muted'>No per-source breakdown yet.</p>"
    )

    days = report.get("as_of_days", [])
    coverage = (
        f"<p class='muted'>Across {len(days)} report day(s)"
        + (f", {escape(days[-1])} &rarr; {escape(days[0])}" if days else "")
        + ".</p>"
    )

    return (
        intro
        + section("Overall", "Realized accuracy across every settled play, and how it compares to the scout's backtested forecast. A large gap (drift) with enough sample is flagged.")
        + overall
        + coverage
        + section("By conviction tier", "Does higher conviction actually win more? If the win rate does not rise with the tier, the conviction signal is miscalibrated.")
        + mono_line
        + tier_table
        + section("By source", "Realized win rate split by scout (options vs small-cap).")
        + source_table
    )


def run_recipient_action(root: Path, form: dict[str, str]) -> dict[str, Any]:
    """Add or remove one report recipient. Never raises: validation failures and
    backend hiccups come back as a message the tab renders."""
    action = str(form.get("action", "")).strip().lower()
    email = str(form.get("email", "")).strip()
    bucket = _entry_alerts_bucket()
    try:
        store = _scout_store()
    except Exception:
        return {"ok": False, "message": "Recipient store is unavailable."}
    try:
        if action == "add":
            ok, message = store.add_recipient(email, bucket=bucket)
        elif action == "remove":
            ok, message = store.remove_recipient(email, bucket=bucket)
        else:
            ok, message = False, "Unknown action."
    except Exception as exc:  # a backend write failure must not crash the tab
        return {"ok": False, "message": f"Could not update recipients: {exc}"}
    return {"ok": ok, "message": message}


def scout_recipients_html(root: Path, result: dict[str, Any] | None = None) -> str:
    bucket = _entry_alerts_bucket()
    try:
        store = _scout_store()
        recipients = store.load_recipients(bucket=bucket)
        available = True
    except Exception:
        recipients, available = [], False

    banner = ""
    if result and result.get("message"):
        cls = "safe" if result.get("ok") else "error"
        banner = f'<p class="{cls}">{escape(result["message"])}</p>'

    intro = (
        '<p class="muted">Everyone here receives the daily Options and Small-Cap '
        "Scout emails, in addition to the default operator address "
        f"(<b>{escape(store.DEFAULT_EMAIL) if available and hasattr(store, 'DEFAULT_EMAIL') else 'digitalsolomon.com@gmail.com'}</b>), "
        "which is always included. Analysis emails only; this list has no trading "
        "capability.</p>"
    )

    if not available:
        return intro + '<p class="warn">The recipient store is unavailable right now.</p>'

    where = "GCS bucket " + escape(bucket) if bucket else "local data/entry_alerts/recipients.json"
    if recipients:
        rows = "".join(
            "<tr>"
            f"<td>{escape(addr)}</td>"
            "<td><form method='post' style='margin:0'>"
            f"<input type='hidden' name='email' value='{escape(addr)}'>"
            "<button class='danger' name='action' value='remove'>Remove</button>"
            "</form></td></tr>"
            for addr in recipients
        )
        table = f"<table><thead><tr><th>Recipient</th><th></th></tr></thead><tbody>{rows}</tbody></table>"
    else:
        table = "<p>No additional recipients yet. The default operator address still receives every report.</p>"

    add_form = (
        "<form method='post'>"
        "<label>Add a recipient</label>"
        "<input name='email' type='email' placeholder='name@example.com'>"
        "<button name='action' value='add'>Add</button>"
        "</form>"
    )
    return (
        intro
        + banner
        + section("Current Recipients", "The addresses that receive the scout report emails alongside the default operator address. Remove one with its Remove button.")
        + table
        + section("Add Recipient", "Enter a valid email address and click Add. Invalid or duplicate addresses are rejected with a message.")
        + add_form
        + f"<p class='muted'>Recipients are stored in {where} as recipients.json.</p>"
    )


NAV_ITEMS = [
    ("/", "Status", "Safety Status"),
    ("/equities", "Equities Lane", "Equities Lane"),
    ("/scout-reports", "Scout Reports", "Scout Reports"),
    ("/calibration", "Calibration", "Scout Calibration"),
    ("/scout-recipients", "Report Recipients", "Report Recipients"),
    ("/live-control", "Live Control Center", "Live Control Center"),
    ("/live-readiness", "Live Readiness", "Live Readiness"),
    ("/settings", "Settings", "Settings"),
    ("/symbols", "Symbols", "Symbols"),
    ("/intelligence", "Intelligence", "Intelligence"),
    ("/strategy", "Strategy", "Strategy"),
    ("/kill", "Kill Switch", "Kill Switch"),
    ("/preview", "Preview", "Preview"),
    ("/logs", "Logs", "Logs"),
    ("/audit", "Audit", "Audit"),
    ("/help", "Help", "Instructions / Help"),
]


def info(text: str) -> str:
    """A small round 'i' button that reveals an expandable explanation panel."""
    return (
        "<details class='info'>"
        "<summary title='More info' aria-label='More info'>i</summary>"
        f"<div class='info-body'>{escape(text)}</div>"
        "</details>"
    )


def section(title: str, text: str = "") -> str:
    """A section heading with an optional inline info button."""
    return f"<h3 class='sec'>{escape(title)}{info(text) if text else ''}</h3>"


def nav_html(active_title: str) -> str:
    links = "".join(
        f"<a href='{href}' class='{'active' if page_title == active_title else ''}'>{escape(label)}</a>"
        for href, label, page_title in NAV_ITEMS
    )
    return f"<nav>{links}</nav>"


def page(title: str, body: str) -> str:
    return f"""
    <!doctype html>
    <html>
    <head>
      <meta charset="utf-8">
      <meta name="viewport" content="width=device-width, initial-scale=1">
      <title>Solomon Trader 007 - {escape(title)}</title>
      <link rel="icon" href="/assets/logo.png">
      <style>
        * {{ box-sizing: border-box; }}
        body {{ font-family: 'Segoe UI', Arial, sans-serif; margin: 0; background: #eef1f6; color: #14171f; }}
        header {{ background: #0b1220; color: white; padding: 16px 28px; position: sticky; top: 0; z-index: 30; box-shadow: 0 2px 10px rgba(0,0,0,.18); }}
        .brand {{ display: flex; align-items: center; gap: 14px; }}
        .brand .logo {{ width: 48px; height: 48px; object-fit: contain; background: #fff; border-radius: 11px; padding: 5px; flex: none; }}
        .brand h1 {{ margin: 0; font-size: 21px; letter-spacing: .3px; }}
        .brand .tag {{ display: block; color: #93a4bd; font-size: 12px; margin-top: 2px; }}
        nav {{ margin-top: 13px; display: flex; flex-wrap: wrap; gap: 6px; }}
        nav a {{ padding: 6px 12px; border-radius: 999px; color: #cdd8ea; font-size: 13px; text-decoration: none; transition: background .12s; }}
        nav a:hover {{ background: #1e293b; }}
        nav a.active {{ background: #2563eb; color: #fff; font-weight: 600; }}
        main {{ padding: 26px 28px 60px; max-width: 1180px; margin: 0 auto; }}
        main > h2 {{ margin: 4px 0 18px; font-size: 26px; }}
        h3.sec, main h3 {{ display: flex; align-items: center; font-size: 16px; margin: 26px 0 10px; }}
        table {{ border-collapse: collapse; width: 100%; background: white; margin: 12px 0 24px; border-radius: 10px; overflow: hidden; box-shadow: 0 1px 3px rgba(16,24,40,.06); }}
        th, td {{ border-bottom: 1px solid #e6eaf0; padding: 10px 12px; text-align: left; vertical-align: top; }}
        thead th {{ background: #f3f5f9; font-size: 13px; text-transform: uppercase; letter-spacing: .4px; color: #475467; }}
        tr:last-child td {{ border-bottom: none; }}
        label {{ display: block; margin: 14px 0 4px; font-weight: 600; }}
        input, select, textarea {{ padding: 9px 10px; width: 360px; max-width: 100%; border: 1px solid #cbd3e0; border-radius: 8px; font-size: 14px; }}
        button {{ padding: 9px 15px; margin: 8px 8px 8px 0; cursor: pointer; border: 1px solid #2563eb; background: #2563eb; color: #fff; border-radius: 8px; font-size: 14px; font-weight: 600; transition: filter .12s; }}
        button:hover {{ filter: brightness(1.08); }}
        button.danger, button.danger:hover {{ background: #dc2626; border-color: #dc2626; }}
        pre {{ background: #0b1220; color: #e5e7eb; padding: 14px; overflow: auto; border-radius: 10px; font-size: 13px; }}
        p.danger, .danger {{ color: #b91c1c; font-weight: 700; }}
        p.safe, .safe {{ color: #047857; font-weight: 700; }}
        p.warn, .warn {{ color: #b45309; font-weight: 700; }}
        p.safe, p.warn, p.danger {{ padding: 12px 16px; border-radius: 10px; }}
        p.safe {{ background: #ecfdf3; border: 1px solid #abefc6; }}
        p.warn {{ background: #fffaeb; border: 1px solid #fedf89; }}
        p.danger {{ background: #fef3f2; border: 1px solid #fda29b; }}
        .error {{ background: #fee2e2; border: 1px solid #fecaca; padding: 10px; border-radius: 8px; }}
        td.PASS, .PASS {{ color: #047857; font-weight: 700; }}
        td.FAIL, .FAIL {{ color: #b91c1c; font-weight: 700; }}
        td.WARNING, .WARNING {{ color: #b45309; font-weight: 700; }}
        .grid {{ display: grid; grid-template-columns: repeat(auto-fit, minmax(240px, 1fr)); gap: 14px; margin: 14px 0 24px; }}
        .card {{ background: white; border: 1px solid #e6eaf0; border-radius: 12px; padding: 15px; box-shadow: 0 1px 3px rgba(16,24,40,.06); }}
        .card h3 {{ margin: 0 0 8px; font-size: 14px; color: #475467; text-transform: uppercase; letter-spacing: .3px; display: flex; align-items: center; }}
        .card .val {{ font-size: 20px; font-weight: 700; word-break: break-word; }}
        .muted {{ color: #526070; font-size: 13px; }}
        .badge {{ display: inline-block; padding: 4px 9px; border-radius: 999px; background: #e5e7eb; margin: 2px; font-size: 12px; }}
        /* Info button + expandable explanation */
        .info {{ display: inline-block; position: relative; margin-left: 8px; vertical-align: middle; }}
        .info > summary {{ list-style: none; cursor: pointer; width: 18px; height: 18px; line-height: 18px; text-align: center; border-radius: 50%; background: #2563eb; color: #fff; font-size: 12px; font-weight: 700; font-family: Georgia, 'Times New Roman', serif; font-style: italic; user-select: none; }}
        .info > summary::-webkit-details-marker {{ display: none; }}
        .info[open] > summary {{ background: #1e40af; }}
        .info-body {{ position: absolute; z-index: 40; left: 0; top: 25px; width: 290px; background: #0f172a; color: #e7ecf4; padding: 11px 13px; border-radius: 9px; font-size: 13px; font-weight: 400; font-style: normal; line-height: 1.5; text-transform: none; letter-spacing: normal; box-shadow: 0 10px 28px rgba(0,0,0,.28); }}
        .info-body::before {{ content: ''; position: absolute; top: -6px; left: 6px; border: 6px solid transparent; border-top: 0; border-bottom-color: #0f172a; }}
      </style>
    </head>
    <body>
      <header>
        <div class="brand">
          <img class="logo" src="/assets/logo.png" alt="Solomon Trader 007 logo">
          <div>
            <h1>Solomon Trader 007</h1>
            <span class="tag">Rules-based Robinhood crypto agent &middot; DigitalSolomon</span>
          </div>
        </div>
        {nav_html(title)}
      </header>
      <main><h2>{escape(title)}</h2>{body}</main>
    </body>
    </html>
    """


def home_html(root: Path) -> str:
    snapshot = home_snapshot(root)
    rules, _ = load_dashboard_settings(root)
    mode = runtime_mode(None, rules)
    kill_active = bool(snapshot.get("STOP_TRADING Exists"))
    banner = '<p class="danger">BLOCKED: STOP_TRADING Is Active</p>' if kill_active else readiness_banner(mode)
    descriptions = {
        "Current Mode": "Paper Mode = simulated trades only, no money at risk. Dry Run = uses live Robinhood data and builds real order payloads but submits nothing. Bounded Live = places real orders, one iteration at a time, under strict risk caps.",
        "Trading Enabled": "The TRADING_ENABLED switch in your .env file. When 'false', the bot logs decisions and risk blocks but never submits an order, even in live mode.",
        "Config Enabled": "The trading.enabled flag in trading_rules.yaml. Both this and TRADING_ENABLED must be true (plus live mode) before any real order can be placed.",
        "STOP_TRADING Exists": "STOP_TRADING is the emergency brake. When this file exists, the risk manager blocks every new order before it is placed. Create it instantly from the Kill Switch tab.",
        "Max Trade Amount": "The largest dollar amount the bot may spend on a single order. Live saves are hard-capped at $100.",
        "Max Daily Loss": "If realized losses reach this amount in one day, trading stops for the rest of the day. Live saves are hard-capped at $100.",
        "Max Daily Trades": "The most live trades the bot can place in a single day. Live saves are hard-capped at 5.",
        "Max Symbol Allocation Percent": "The most of your capital that can sit in any one symbol at a time. Live saves are hard-capped at 25%.",
        "Order Cooldown Seconds": "The minimum wait between live orders, so the bot can't rapid-fire. Live saves enforce at least 300 seconds.",
        "Allowed Symbols": "The crypto pairs the bot is allowed to consider in the current mode. Live trading additionally requires each symbol to pass Robinhood validation.",
        "Active Strategy Profile": "The named rule set (from strategy.yaml) that decides buy/sell/hold signals. Change it on the Settings tab.",
        "Open Paper Positions": "How many simulated positions the paper broker currently holds.",
        "External Robinhood Positions": "Crypto positions already in your real Robinhood account, read live. These are shown for awareness; the bot does not manage them unless configured to.",
        "Last Decision": "The most recent action the engine logged - useful for confirming the bot is running and what it last did.",
        "Daily Trade Count": "Live trades placed so far today, measured against Max Daily Trades.",
        "Daily Realized PnL": "Profit or loss locked in from closed trades today.",
        "Daily Blocked Count": "How many proposed orders the risk manager blocked today - a high number means your rules are actively protecting you.",
        "Submitted Live Orders Today": "Real orders sent to Robinhood today. Should be 0 while you are still in paper/testing.",
    }
    cards = "".join(
        f"<div class='card'><h3>{escape(key)}{info(descriptions[key]) if key in descriptions else ''}</h3>"
        f"<div class='val'>{escape(value)}</div></div>"
        for key, value in snapshot.items()
    )
    legend = (
        "<p class='muted' style='margin:-6px 0 14px'>Tap any "
        "<span style=\"display:inline-block;width:16px;height:16px;line-height:16px;text-align:center;border-radius:50%;background:#2563eb;color:#fff;font-size:11px;font-style:italic;font-family:Georgia,serif\">i</span>"
        " for a plain-English explanation.</p>"
    )
    return f"{banner}{legend}<div class='grid'>{cards}</div>"


def settings_html(root: Path, errors: list[str] | None = None) -> str:
    rules, strategy = load_dashboard_settings(root)
    env = read_env(root / ".env")
    trading = rules.get("trading", {})
    risk = rules.get("risk", {})
    strategy_root = strategy.get("strategy", {})
    error_html = f"<div class='error'>{escape('; '.join(errors))}</div>" if errors else ""
    return f"""
    {error_html}
    <form method="post">
      <label>TRADING_MODE</label>
      {mode_select("trading_mode", form_from_mode(env.get("TRADING_MODE", "paper")))}
      <label>TRADING_ENABLED</label>
      {bool_select("env_enabled", env.get("TRADING_ENABLED", "false").lower() == "true")}
      <label>trading.enabled</label>
      {bool_select("config_enabled", bool(trading.get("enabled", False)))}
      <label>trading.mode</label>
      {mode_select("config_mode", form_from_mode(str(trading.get("mode", "paper"))))}
      <label>max_trade_amount_usd</label>
      <input name="max_trade_amount_usd" value="{escape(risk.get("max_trade_amount_usd", ""))}">
      <p class="muted">? The largest dollar amount the bot can use for a single order.</p>
      <label>max_daily_loss_usd</label>
      <input name="max_daily_loss_usd" value="{escape(risk.get("max_daily_loss_usd", ""))}">
      <p class="muted">? The maximum loss allowed in one day before trading stops.</p>
      <label>max_trades_per_day</label>
      <input name="max_trades_per_day" value="{escape(risk.get("max_trades_per_day", ""))}">
      <p class="muted">? The most live trades the bot can place in one day.</p>
      <label>max_open_positions</label>
      <input name="max_open_positions" value="{escape(risk.get("max_open_positions", ""))}">
      <p class="muted">? The maximum number of active positions the bot can hold at one time.</p>
      <label>max_symbol_allocation_percent</label>
      <input name="max_symbol_allocation_percent" value="{escape(risk.get("max_symbol_allocation_percent", ""))}">
      <p class="muted">? The maximum percentage of available capital that can be allocated to one symbol.</p>
      <label>min_order_cooldown_seconds</label>
      <input name="min_order_cooldown_seconds" value="{escape(risk.get("min_order_cooldown_seconds", ""))}">
      <p class="muted">? The minimum wait time between live orders.</p>
      <label>allowed_symbols</label>
      <textarea name="allowed_symbols">{escape(", ".join(trading.get("allowed_symbols", [])))}</textarea>
      <label>research_watchlist</label>
      <textarea name="research_watchlist">{escape(", ".join(symbol_lists(rules)["research_watchlist"]))}</textarea>
      <label>paper_allowed_symbols</label>
      <textarea name="paper_allowed_symbols">{escape(", ".join(symbol_lists(rules)["paper_allowed_symbols"]))}</textarea>
      <label>live_allowed_symbols</label>
      <textarea name="live_allowed_symbols">{escape(", ".join(symbol_lists(rules)["live_allowed_symbols"]))}</textarea>
      <label>Active Strategy Profile</label>
      {strategy_profile_select(strategy)}
      <p class="muted">This controls which trading rule set the bot uses to generate buy, sell, or hold signals.</p>
      {strategy_profile_summary(strategy)}
      <label>poll_interval_seconds</label>
      <input name="poll_interval_seconds" value="{escape(env.get("POLL_INTERVAL_SECONDS", "60"))}">
      <label><input type="checkbox" name="live_confirm" style="width:auto"> I understand live mode can place real Robinhood crypto orders.</label>
      <p class="warn">Live saves enforce max trade <= 100, max daily loss <= 100, max trades/day <= 5, max open positions <= 10, max allocation <= 25%, cooldown >= 300 seconds, no margin, no shorts, and validation required.</p>
      <button type="submit">Save Settings</button>
    </form>
    """


def kill_html(root: Path) -> str:
    rules, _ = load_dashboard_settings(root)
    stop_path = root / rules.get("kill_switch", {}).get("stop_file", "STOP_TRADING")
    return f"""
    <p>STOP_TRADING Exists: <strong>{escape(stop_path.exists())}</strong></p>
    <form method="post" action="/kill/stop"><button class="danger">STOP TRADING NOW</button></form>
    <form method="post" action="/kill/clear">
      <label><input type="checkbox" name="confirm_clear" style="width:auto"> Confirm clear stop file</label>
      <button>Clear Stop File</button>
    </form>
    <h3>Safety Actions</h3>
    <div class="grid">
      <div class="card"><h3>Return to Paper Mode</h3><p class="muted">Switches the app back to simulated paper trading so no new live orders can be placed.</p><form method="post" action="/live-control"><button name="action" value="return-paper">Return to Paper Mode</button></form></div>
      <div class="card"><h3>Cancel Open Live Orders</h3><p class="muted">Attempts to cancel open Robinhood crypto orders.</p><form method="post" action="/live-control"><label><input type="checkbox" name="confirm_cancel_open" style="width:auto"> I understand this will attempt to cancel open live Robinhood crypto orders.</label><button name="action" value="cancel-open">Cancel Open Live Orders</button></form></div>
      <div class="card"><h3>Reconcile Live Orders</h3><p class="muted">Checks Robinhood order history and compares it with local logs.</p><form method="post" action="/live-control"><button name="action" value="reconcile">Reconcile Live Orders</button></form></div>
      <div class="card"><h3>Export Live Audit</h3><p class="muted">Exports a record of live-readiness status, orders, decisions, and safety checks.</p><form method="post" action="/live-control"><button name="action" value="export-audit">Export Live Audit</button></form></div>
    </div>
    """


def symbols_html(root: Path, result: dict[str, Any] | None = None) -> str:
    rules, _ = load_dashboard_settings(root)
    lists = symbol_lists(rules)
    validation = load_symbol_validation(root)
    rows = {
        "Research Watchlist": "Symbols the bot can monitor for research. These do not automatically trade live.\n" + ", ".join(lists["research_watchlist"]),
        "Paper Allowed Symbols": "Symbols the bot can use for simulated paper trading.\n" + ", ".join(lists["paper_allowed_symbols"]),
        "Live Allowed Symbols": "Symbols the bot may trade with real money, but only after validation and risk checks.\n" + ", ".join(lists["live_allowed_symbols"]),
        "Validated Live Symbols": ", ".join(validation.get("available_for_live", [])),
        "Unsupported Symbols": ", ".join(validation.get("unavailable", [])),
        "Unknown Symbols": ", ".join(validation.get("unknown", [])),
    }
    table = "".join(f"<tr><th>{escape(key)}</th><td>{escape(value)}</td></tr>" for key, value in rows.items())
    badges = "".join(
        f"<span class='badge'>{escape(symbol)}: {escape('Validated' if symbol in validation.get('available_for_live', []) else 'Unsupported' if symbol in validation.get('unavailable', []) else 'Unknown' if symbol in validation.get('unknown', []) else 'Needs Validation')}</span>"
        for symbol in lists["live_allowed_symbols"]
    )
    result_html = f"<h3>Validation Result</h3><pre>{escape(json.dumps(scrub_secrets(result), indent=2))}</pre>" if result else ""
    return f"""
    <p class="warn">More live symbols means the bot can scan more real-money opportunities. Risk limits still apply.</p>
    <table>{table}</table>
    <h3>Live Symbol Status</h3>
    <p>{badges}</p>
    <form method="post">
      <button>Validate Symbols With Robinhood</button>
    </form>
    {result_html}
    """


def intelligence_html(root: Path, result: dict[str, Any] | None = None) -> str:
    status = intelligence_status_layer(root)
    store = IntelligenceStore(root / "data" / "intelligence.db")
    scores = store.latest_rows("intelligence_scores", 50)
    news = store.latest_rows("news_events", 20)
    errors = store.latest_errors(10)
    key_status = status.get("provider_key_status", {})
    allowed = [row.get("symbol") for row in scores if row.get("recommendation") == "allow"]
    blocked = [row.get("symbol") for row in scores if row.get("recommendation") == "block"]
    missing = sorted({item for row in scores for item in json.loads(row.get("missing_data") or "[]")})
    provider_rows = {
        "News Provider Status": status.get("news_provider_status"),
        "Macro Provider Status": status.get("macro_provider_status"),
        "Market Context Provider Status": status.get("market_context_provider_status"),
        "Last Successful Refresh": status.get("last_successful_refresh") or "Never",
        "Last Error": (status.get("last_error") or {}).get("message") or "None",
        "Minimum Confidence To Trade": status.get("minimum_confidence_to_trade"),
        "Macro Risk": (status.get("macro_risk") or {}).get("label"),
        "Macro Risk Score": (status.get("macro_risk") or {}).get("score"),
        "Crypto Market Context": status.get("market_context_status"),
        "Last Errors": len(errors),
    }
    provider_table = "".join(f"<tr><th>{escape(key)}</th><td>{escape(value)}</td></tr>" for key, value in provider_rows.items())
    provider_cards = "".join(
        f"<div class='card'><h3>{escape(title)}</h3><p>{escape(value)}</p></div>"
        for title, value in {
            "News Provider": status.get("news_provider_status"),
            "Macro Provider": status.get("macro_provider_status"),
            "Crypto Market Provider": status.get("market_context_provider_status"),
            "Last Successful Refresh": status.get("last_successful_refresh") or "Never",
            "Last Error": (status.get("last_error") or {}).get("message") or "None",
        }.items()
    )
    key_rows = {
        "CRYPTOPANIC_API_KEY": key_status.get("CRYPTOPANIC_API_KEY", "missing"),
        "COINGECKO_API_KEY": key_status.get("COINGECKO_API_KEY", "missing"),
        "FRED_API_KEY": key_status.get("FRED_API_KEY", "missing"),
    }
    key_table = "".join(f"<tr><th>{escape(key)}</th><td>{escape(value)}</td></tr>" for key, value in key_rows.items())
    score_rows = rows_table(
        [
            {
                "timestamp": row.get("timestamp"),
                "symbol": row.get("symbol"),
                "score": row.get("combined_intelligence_score"),
                "recommendation": row.get("recommendation"),
                "missing_data": row.get("missing_data"),
            }
            for row in scores
        ]
    )
    news_rows = rows_table(
        [
            {
                "timestamp": row.get("timestamp"),
                "symbol": row.get("symbol"),
                "sentiment": row.get("sentiment"),
                "title": row.get("title"),
            }
            for row in news
        ]
    )
    result_html = f"<h3>Action Result</h3><pre>{escape(json.dumps(scrub_secrets(result), indent=2))}</pre>" if result else ""
    return f"""
    <p class="warn">Market intelligence is a decision filter. It cannot submit orders or bypass the risk manager, STOP_TRADING, symbol validation, or bounded live limits.</p>
    <h3>Optional Intelligence API Keys</h3>
    <p class="muted">Add optional intelligence provider keys to the local .env file only. The dashboard shows only configured or missing and never displays key values.</p>
    <table>{key_table}</table>
    <h3>Provider Status</h3>
    <div class="grid">{provider_cards}</div>
    <table>{provider_table}</table>
    <form method="post">
      <button name="action" value="collect">Collect Intelligence Now</button>
      <button name="action" value="score-all">Score All Symbols</button>
      <button name="action" value="export-report">Export Intelligence Report</button>
    </form>
    {result_html}
    <div class="grid">
      <div class="card"><h3>Allowed by Intelligence</h3><p>{escape(", ".join([str(item) for item in allowed]) or "None yet")}</p></div>
      <div class="card"><h3>Blocked by Intelligence</h3><p>{escape(", ".join([str(item) for item in blocked]) or "None yet")}</p></div>
      <div class="card"><h3>Missing Data</h3><p>{escape(", ".join(missing) or "None")}</p></div>
    </div>
    <h3>Symbol Scores</h3>
    {score_rows}
    <h3>Recent News</h3>
    {news_rows}
    <h3>Recent Intelligence Errors</h3>
    {rows_table(errors)}
    """


def live_control_html(root: Path, smoke_previews: dict[str, dict[str, Any]], result: dict[str, Any] | None = None) -> str:
    rules, _ = load_dashboard_settings(root)
    risk = rules.get("risk", {})
    readiness = live_launch_readiness(root=root)
    intelligence = intelligence_status_layer(root)
    validation = load_symbol_validation(root)
    validated = validation.get("available_for_live", []) or ["BTC-USD"]
    stop_exists = KillSwitch(stop_file=str(root / rules.get("kill_switch", {}).get("stop_file", "STOP_TRADING"))).stop_file_exists()
    status_rows = {
        "Current Mode": runtime_mode(None, rules),
        "Trading Enabled": os.getenv("TRADING_ENABLED", "false"),
        "STOP_TRADING status": "active" if stop_exists else "clear",
        "Readiness status": "pass" if readiness.get("ready") else "blocked",
        "Validated live symbols": ", ".join(validation.get("available_for_live", [])),
        "Max trade amount": risk.get("max_trade_amount_usd"),
        "Max daily loss": risk.get("max_daily_loss_usd"),
        "Max trades per day": risk.get("max_trades_per_day"),
        "Max open positions": risk.get("max_open_positions"),
        "Max symbol allocation": risk.get("max_symbol_allocation_percent"),
        "Cooldown": risk.get("min_order_cooldown_seconds"),
        "Intelligence enabled": intelligence.get("enabled"),
        "Latest macro risk score": (intelligence.get("macro_risk") or {}).get("score"),
        "Latest market context score": intelligence.get("market_context_status"),
        "Minimum confidence to trade": intelligence.get("minimum_confidence_to_trade"),
    }
    status_table = "".join(f"<tr><th>{escape(k)}</th><td>{escape(v)}</td></tr>" for k, v in status_rows.items())
    options = "".join(f"<option value='{escape(symbol)}'>{escape(symbol)}</option>" for symbol in validated)
    result_html = f"<h3>Result</h3><pre>{escape(json.dumps(scrub_secrets(result), indent=2))}</pre>" if result else ""
    preview_id = result.get("preview_id") if result and result.get("action") == "preview-smoke" else ""
    submit_smoke = ""
    if preview_id and result.get("risk_allowed"):
        submit_smoke = f"""
        <form method="post">
          <input type="hidden" name="action" value="submit-smoke">
          <input type="hidden" name="preview_id" value="{escape(preview_id)}">
          <label><input type="checkbox" name="understand_smoke" style="width:auto"> I understand this smoke test can place a real Robinhood crypto order using the selected symbol and amount.</label>
          <label><input type="checkbox" name="confirm_smoke_submit" style="width:auto"> I confirm I have reviewed the order preview and want to submit this one live smoke-test order.</label>
          <button class="danger">Submit One Live Smoke Test Order</button>
        </form>
        """
    steps = [
        ("readiness", "Run readiness check", "Checks mode, STOP_TRADING, risk caps, validation, and local safety state."),
        ("validate-symbols", "Validate symbols", "Uses Robinhood read-only endpoints to confirm symbol availability and bid/ask data."),
        ("reconcile", "Review Robinhood/order reconciliation", "Checks open Robinhood crypto orders without submitting anything."),
        ("export-audit", "Export live audit", "Writes recent decisions, orders, risk blocks, and errors for review."),
    ]
    step_forms = "".join(
        f"<div class='card'><h3>{escape(label)}</h3><p class='muted'>{escape(text)}</p><form method='post'><button name='action' value='{escape(action)}'>{escape(label)}</button></form></div>"
        for action, label, text in steps
    )
    return f"""
    {section("Current Live Status", "The live-relevant state at a glance: mode, whether trading is enabled, STOP_TRADING status, readiness pass/blocked, validated symbols, your risk caps, and intelligence signals.")}
    <table>{status_table}</table>
    {section("Step-by-Step Live Flow", "The recommended order of operations before going live. Each button here is read-only or a preview - none of them place a real order.")}
    <div class="grid">{step_forms}</div>
    {section("Smoke Test From Dashboard", "A smoke test is one small supervised live order used to confirm the real order path works. It requires readiness to pass, a validated symbol, a generated preview, and both confirmation checkboxes before anything is submitted.")}
    <p class="warn">A smoke test is one supervised live order. It requires readiness, validated symbol, preview, and both confirmation checkboxes.</p>
    <form method="post">
      <input type="hidden" name="action" value="preview-smoke">
      <label>Symbol</label><select name="symbol">{options}</select>
      <label>Amount USD</label><input name="amount_usd" value="1">
      <label>Side</label><select name="side"><option value="buy">buy</option><option value="sell">sell</option></select>
      <button>Preview Smoke Test</button>
    </form>
    {submit_smoke}
    {section("Start Bounded Live Trading", "Runs exactly one live strategy-and-risk cycle. If (and only if) the strategy signals a trade and every risk check passes, it may place one real order - then it stops. It is not a continuous loop.")}
    <p class="warn">Runs one bounded live iteration only. It is not a forever loop.</p>
    <form method="post">
      <label><input type="checkbox" name="confirm_bounded_iteration" style="width:auto"> I understand this can place one real Robinhood crypto order if the strategy and risk checks approve it.</label>
      <button class="danger" name="action" value="bounded-iteration">Run One Bounded Live Iteration</button>
    </form>
    <h3>Return to Safety Controls</h3>
    <div class="grid">
      <div class="card"><h3>Return to Paper Mode</h3><p class="muted">Switches the app back to simulated paper trading so no new live orders can be placed.</p><form method="post"><button name="action" value="return-paper">Return to Paper Mode</button></form></div>
      <div class="card"><h3>Create STOP_TRADING</h3><p class="muted">Emergency brake. Blocks all new trading activity immediately.</p><form method="post"><button class="danger" name="action" value="create-stop">Create STOP_TRADING</button></form></div>
      <div class="card"><h3>Cancel Open Live Orders</h3><p class="muted">Attempts to cancel open Robinhood crypto orders.</p><form method="post"><label><input type="checkbox" name="confirm_cancel_open" style="width:auto"> I understand this will attempt to cancel open live Robinhood crypto orders.</label><button name="action" value="cancel-open">Cancel Open Live Orders</button></form></div>
      <div class="card"><h3>Reconcile Live Orders</h3><p class="muted">Checks Robinhood order history and compares it with local logs.</p><form method="post"><button name="action" value="reconcile">Reconcile Live Orders</button></form></div>
      <div class="card"><h3>Export Live Audit</h3><p class="muted">Exports a record of live-readiness status, orders, decisions, and safety checks.</p><form method="post"><button name="action" value="export-audit">Export Live Audit</button></form></div>
    </div>
    {result_html}
    """


def audit_html(root: Path, result: dict[str, Any] | None = None) -> str:
    logger = SQLiteLogger(root / "data" / "trading_agent.db")
    result_html = f"<h3>Result</h3><pre>{escape(json.dumps(scrub_secrets(result), indent=2))}</pre>" if result else ""
    sections = {
        "Last live readiness result": recent_rows(root, "trading_agent.db", "decisions", 50),
        "Recent risk blocks": recent_rows(root, "trading_agent.db", "risk_blocks", 25),
        "Recent orders": recent_rows(root, "trading_agent.db", "orders", 25),
    }
    counts = {
        "Submitted orders count": submitted_live_orders_today(root),
        "Blocked orders count": SQLiteLogger(root / "data" / "trading_agent.db").get_daily_summary()["blocked_count"],
        "Last decision": logger.get_last_decision(),
    }
    count_table = "".join(f"<tr><th>{escape(k)}</th><td>{escape(v)}</td></tr>" for k, v in counts.items())
    body = f"<table>{count_table}</table><form method='post'><button>Export Live Audit</button></form>{result_html}"
    body += "".join(f"<h3>{escape(title)}</h3>{rows_table(rows)}" for title, rows in sections.items())
    return body


def resolve_equity_account(root: Path) -> Any:
    """The resolved, pinned equities account -- or None.

    The dashboard process holds no Robinhood connector (execution is
    agent-hosted: the OAuth connector is bound to an agent session, not to this
    web process), so in normal operation there is nothing live to resolve and
    this returns None. The page then renders the config-declared EXPECTED
    identity. A harness that DOES hold a connector can monkeypatch this to
    return the live RobinhoodEquityClient, so the page shows the account that
    was ACTUALLY pinned and raises a DANGER banner if it does not match the
    expected ••2092 Agentic identity.
    """
    return None


def equities_html(root: Path) -> str:
    rules, _ = load_dashboard_settings(root)
    equities_config = rules.get("equities", {})
    kill = equity_kill_switch(rules, root)
    stop_exists = kill.stop_file_exists()
    trading_enabled = os.getenv("TRADING_ENABLED", "false").lower() == "true"
    allow_extended = bool(equities_config.get("allow_extended_hours", False))
    market_reason = market_blocked_reason(datetime.now(EASTERN), allow_extended_hours=allow_extended)
    crypto_stop_exists = KillSwitch(stop_file=str(root / rules.get("kill_switch", {}).get("stop_file", "STOP_TRADING"))).stop_file_exists()
    price_sources = equity_price_sources(root)

    # The out-of-band expected identity for the single agent-tradable account,
    # read from config (never a literal baked into this view).
    expected = equities_config.get("expected_account", {}) or {}
    expected_nickname = str(expected.get("nickname", "Agentic"))
    expected_suffix = str(expected.get("number_suffix", "2092"))
    off_limits_suffix = str(equities_config.get("off_limits_account_suffix", "2833"))

    # If a live client is resolvable, render the account it ACTUALLY pinned and
    # compare it to the expected identity; otherwise render the expected identity.
    resolved = resolve_equity_account(root)
    account_mismatch = False
    mismatch_detail = ""
    if resolved is not None:
        resolved_number = str(getattr(resolved, "account_number", "") or "")
        resolved_nickname = getattr(resolved, "nickname", None) or "(unknown)"
        account_mismatch = (resolved_nickname != expected_nickname) or (not resolved_number.endswith(expected_suffix))
        designated_nickname = resolved_nickname
        designated_number = ("••" + resolved_number[-4:]) if resolved_number else "(unknown)"
        if account_mismatch:
            mismatch_detail = (
                f"resolved account {designated_number} (nickname {designated_nickname}) does NOT match the "
                f"pinned Agentic ••{expected_suffix} identity"
            )
    else:
        designated_nickname = expected_nickname
        designated_number = "••" + expected_suffix

    if stop_exists:
        banner = '<p class="danger">BLOCKED: STOP_TRADING_EQUITIES Is Active - the equities lane will not place or simulate new orders</p>'
    elif account_mismatch:
        banner = (
            f'<p class="danger">DANGER: {escape(mismatch_detail)} - the equities lane must not trade until this is '
            'corrected; every order path is pinned to the wrong account</p>'
        )
    elif market_reason:
        banner = f'<p class="warn">CAUTION: {escape(market_reason)} - equity orders would be refused right now</p>'
    else:
        banner = '<p class="safe">SAFE: EQUITIES PAPER MODE - simulated fills only, no live Robinhood equity order has a path from this dashboard</p>'

    posture_rows = {
        "Lane": "Robinhood equities (rules-based, long-only)",
        "Execution surface": "Agent-hosted: the OAuth Robinhood MCP connector is session-bound to a Claude/harness agent session. This dashboard process holds no connector and cannot place or preview a live equity order.",
        "Current posture": "paper (simulated fills against real historical bars)" if not stop_exists else "halted",
        "Proving/backtest price source": (
            ", ".join(f"{name} ({count} rows)" for name, count in price_sources.items())
            or "none recorded yet (a counted proving run is priced on massive: real Massive historical daily bars)"
        ),
        "Execution price source": "the Robinhood OAuth connector's own quote, read at order time inside an agent session",
        "STOP_TRADING_EQUITIES exists": stop_exists,
        "TRADING_ENABLED (shared env flag)": trading_enabled,
        "Extended hours opt-in (config)": allow_extended,
        "Regular trading hours right now": "closed" if market_reason else "open",
        "Crypto lane STOP_TRADING (unrelated file, shown for awareness)": crypto_stop_exists,
    }
    posture_table = "".join(f"<tr><th>{escape(k)}</th><td>{escape(v)}</td></tr>" for k, v in posture_rows.items())

    resolution_label = (
        "resolved live from the connector (agent session)"
        if resolved is not None
        else "expected identity from config (no live client in this dashboard process)"
    )
    account_rows = {
        "Designated account nickname": designated_nickname,
        "Designated account number": designated_number,
        "Account identity source": resolution_label,
        "Matches expected Agentic ••" + expected_suffix + " identity": ("no - MISMATCH" if account_mismatch else "yes"),
        "Default account (off-limits to the agent)": f"••{off_limits_suffix} - never targeted by this lane",
        "Options level": "none (long-only equities, no options)",
        "Margin": "none - cash account only",
        "Live account balance": "read live via the connector inside an agent session only; not available from this dashboard process",
    }
    account_table = "".join(f"<tr><th>{escape(k)}</th><td>{escape(v)}</td></tr>" for k, v in account_rows.items())

    holdings = equities_positions(root)
    position_rows = holdings["positions"]
    if position_rows:
        positions_table = rows_table(
            [
                {"symbol": symbol, "quantity": row["quantity"], "average_price": row["average_price"], "pnl": row["pnl"]}
                for symbol, row in sorted(position_rows.items())
            ]
        )
    else:
        positions_table = "<p>No open equities paper positions.</p>"

    decisions_table = rows_table(recent_equity_decisions(root, 25))

    return f"""
    {banner}
    {section("Posture & Gates", "Everything that governs whether the equities lane can act: the lane's own kill switch (separate from the crypto lane's STOP_TRADING), the shared TRADING_ENABLED flag, market hours, and where live execution actually happens.")}
    <table>{posture_table}</table>
    {section("Agentic Account", "Robinhood confines all agent trading to exactly one designated account. The agent never targets the default account, whatever the rules would otherwise allow.")}
    <table>{account_table}</table>
    {section("Equities Positions (paper)", "Simulated fills from the local paper broker, priced against REAL Massive historical daily bars replayed one per cycle (not a live Robinhood execution quote). This is the lane's only fill source in this build.")}
    <p class="muted">Cash (paper): {escape(holdings["cash_usd"])}</p>
    {positions_table}
    {section("Recent Decisions & Rationale", "Every equities decision - a simulated fill, a skipped signal, or a refused order - with the human-readable reason the lane logged for it.")}
    {decisions_table}
    """


def strategy_html(root: Path) -> str:
    _, strategy = load_dashboard_settings(root)
    return strategy_profile_summary(strategy)


def help_html() -> str:
    sections = {
        "Trading Modes": [
            "Paper Mode: simulated trades only.",
            "Dry Run Mode: live Robinhood data and live-style payloads, but no real orders.",
            "Bounded Live Mode: real orders with strict limits and readiness gates.",
            "Smoke Test: one supervised live order after preview and checkbox confirmations.",
            "Live Loop: one bounded live iteration; not a forever loop.",
        ],
        "Safety Terms": [
            "STOP_TRADING: emergency brake that blocks new trading.",
            "Readiness Check: verifies mode, config, risk caps, symbol validation, and local safety state.",
            "Risk Limit: a hard cap that blocks trades outside approved bounds.",
            "Max Trade Amount: largest allowed dollar amount for one order.",
            "Max Daily Loss: loss threshold that stops trading for the day.",
            "Max Trades Per Day: maximum number of live trades in one day.",
            "Max Open Positions: maximum active positions at one time.",
            "Max Symbol Allocation: maximum percent of capital in one symbol.",
            "Cooldown: required waiting period between live orders.",
            "No Shorts, No Margin, No Position Scaling: safety rules that prevent extra exposure.",
        ],
        "Symbol Terms": [
            "Research Watchlist: symbols monitored for research only.",
            "Paper Allowed Symbols: symbols available for simulated trades.",
            "Live Allowed Symbols: symbols eligible for real-money trading after validation.",
            "Validated Symbols: Robinhood returned valid symbol and bid/ask data.",
            "Unsupported Symbols: unavailable, paused, delisted, or disabled symbols.",
            "Unknown Symbols: symbols that did not return enough data to validate.",
        ],
        "Strategy Terms": [
            "Active Strategy Profile: the rule set used to generate signals.",
            "Buy Signal: strategy says a buy may be considered.",
            "Sell Signal: strategy says an exit may be considered.",
            "Hold Signal: no action.",
            "EMA: moving average trend indicator.",
            "RSI: momentum/overbought-oversold indicator.",
            "Momentum: recent price direction.",
        ],
        "Button Definitions": [
            "Run Test Connection: read-only Robinhood connection check.",
            "Validate Symbols With Robinhood: read-only symbol and bid/ask validation.",
            "Preview Buy / Preview Sell: build a non-submitted order preview.",
            "Run Smoke Test: one supervised live order after preview and confirmations.",
            "Run One Bounded Live Iteration: one strategy/risk cycle, not a forever loop.",
            "Return to Paper Mode: switches back to simulated trading.",
            "Create STOP_TRADING: activates the emergency brake.",
            "Clear STOP_TRADING: removes the emergency brake after confirmation.",
            "Cancel Open Live Orders: attempts to cancel open Robinhood crypto orders.",
            "Reconcile Live Orders: read-only check of Robinhood orders.",
            "Export Live Audit: writes a review file with recent safety state.",
        ],
    }
    body = ""
    for title, items in sections.items():
        body += f"<h3>{escape(title)}</h3><ul>" + "".join(f"<li>{escape(item)}</li>" for item in items) + "</ul>"
    flow = [
        "Start in Paper Mode.",
        "Validate symbols.",
        "Run previews.",
        "Check Live Readiness.",
        "Run one smoke test.",
        "Reconcile live orders.",
        "Export live audit.",
        "Return to Paper Mode or run one bounded live iteration.",
        "Use STOP_TRADING if anything looks wrong.",
    ]
    body += "<h3>Recommended Operating Flow</h3><ol>" + "".join(f"<li>{escape(item)}</li>" for item in flow) + "</ol>"
    return body


def preview_html(result: dict[str, Any] | None = None) -> str:
    result_html = f"<h3>Result</h3><pre>{escape(json.dumps(scrub_secrets(result), indent=2))}</pre>" if result else ""
    return f"""
    <form method="post">
      <label>Symbol</label><input name="symbol" value="BTC-USD">
      <label>Amount USD</label><input name="amount_usd" value="1">
      <button name="action" value="preview-buy">Preview Buy</button>
      <button name="action" value="preview-sell">Preview Sell</button>
      <button name="action" value="run-paper-once">Run Paper Once</button>
      <button name="action" value="run-dry-once">Run Dry Once</button>
    </form>
    {result_html}
    """


def live_readiness_html(root: Path, result: dict[str, Any] | None = None) -> str:
    rules, _ = load_dashboard_settings(root)
    mode = runtime_mode(None, rules)
    connection_result = result if result and result.get("action") == "test-connection" else None
    validation = load_symbol_validation(root)
    lists = symbol_lists(rules)
    risk = rules.get("risk", {})
    intelligence = intelligence_status_layer(root)
    summary_rows = {
        "Live Symbol Count": len(lists["live_allowed_symbols"]),
        "Validated Live Symbols": ", ".join(validation.get("available_for_live", [])),
        "Unvalidated Live Symbols": ", ".join(
            symbol
            for symbol in lists["live_allowed_symbols"]
            if symbol not in set(validation.get("available_for_live", []))
            and symbol not in set(validation.get("unavailable", []))
            and symbol not in set(validation.get("unknown", []))
        ),
        "Unsupported Live Symbols": ", ".join(validation.get("unavailable", [])),
        "Max Trade Amount": risk.get("max_trade_amount_usd"),
        "Max Daily Loss": risk.get("max_daily_loss_usd"),
        "Max Trades Per Day": risk.get("max_trades_per_day"),
        "Max Open Positions": risk.get("max_open_positions"),
        "Max Symbol Allocation": risk.get("max_symbol_allocation_percent"),
        "Cooldown": risk.get("min_order_cooldown_seconds"),
        "Intelligence Enabled": intelligence.get("enabled"),
        "Latest Macro Risk Score": (intelligence.get("macro_risk") or {}).get("score"),
        "Latest Market Context Score": intelligence.get("market_context_status"),
        "Minimum Confidence To Trade": intelligence.get("minimum_confidence_to_trade"),
        "Symbols Blocked By Intelligence": ", ".join(
            str(row.get("symbol"))
            for row in IntelligenceStore(root / "data" / "intelligence.db").latest_rows("intelligence_scores", 50)
            if row.get("recommendation") == "block"
        ),
        "Symbols Allowed By Intelligence": ", ".join(
            str(row.get("symbol"))
            for row in IntelligenceStore(root / "data" / "intelligence.db").latest_rows("intelligence_scores", 50)
            if row.get("recommendation") == "allow"
        ),
    }
    summary = "".join(f"<tr><th>{escape(key)}</th><td>{escape(value)}</td></tr>" for key, value in summary_rows.items())
    result_html = f"<h3>Action Result</h3><pre>{escape(json.dumps(scrub_secrets(result), indent=2))}</pre>" if result else ""
    rows = "".join(
        f"<tr><th>{escape(check['name'])}</th><td class=\"{escape(check['status'])}\">{escape(check['status'])}</td><td>{escape(check['detail'])}</td></tr>"
        for check in readiness_checks(root, connection_result)
    )
    return f"""
    {readiness_banner(mode)}
    {section("Live Summary", "A snapshot of what live trading would do right now: how many symbols are live-eligible, which are validated, your current risk caps, and the intelligence-layer status. Nothing here places an order.")}
    <table>{summary}</table>
    {section("Checklist", "Every gate that must read PASS before live trading is possible. FAIL on the TRADING_MODE / TRADING_ENABLED / config rows is expected and correct while you are safely in paper mode. Even when all gates pass, placing a real order still requires a separate typed confirmation in the terminal.")}
    <table><thead><tr><th>Gate</th><th>Status</th><th>Detail</th></tr></thead><tbody>{rows}</tbody></table>
    {section("Actions", "Read-only and preview actions are safe to run any time. 'Run Supervised $1 Smoke Buy' only prints the manual terminal command - the dashboard cannot submit a real order or bypass typed confirmation.")}
    <form method="post">
      <button name="action" value="test-connection">Run Test Connection</button>
      <button name="action" value="preview-buy">Run Buy Preview ($1 BTC)</button>
      <button name="action" value="preview-sell">Run Sell Preview ($1 BTC)</button>
      <button name="action" value="smoke-buy">Run Supervised $1 Smoke Buy</button>
      <button name="action" value="reconcile-live-orders">Reconcile Live Orders</button>
      <button name="action" value="export-live-audit">Export Live Audit</button>
      <button name="action" value="return-paper">Return to Paper Mode</button>
      <button class="danger" name="action" value="create-stop">Create STOP_TRADING File</button>
    </form>
    <p class="warn">The smoke button prints manual instructions only. It cannot submit an order or bypass typed confirmation.</p>
    {result_html}
    """


def logs_html(root: Path) -> str:
    sections = [
        ("decisions", recent_rows(root, "trading_agent.db", "decisions")),
        ("orders", recent_rows(root, "trading_agent.db", "orders")),
        ("blocked trades", recent_rows(root, "trading_agent.db", "risk_blocks")),
        ("paper trades", recent_rows(root, "paper_trades.db", "paper_trades")),
        ("errors", recent_rows(root, "trading_agent.db", "errors")),
    ]
    return "".join(f"<h3>{escape(title)}</h3>{rows_table(rows)}" for title, rows in sections)


def rows_table(rows: list[dict[str, Any]]) -> str:
    if not rows:
        return "<p>No rows.</p>"
    keys = list(rows[0])
    header = "".join(f"<th>{escape(key)}</th>" for key in keys)
    body = "".join("<tr>" + "".join(f"<td>{escape(row.get(key, ''))}</td>" for key in keys) + "</tr>" for row in rows)
    return f"<table><thead><tr>{header}</tr></thead><tbody>{body}</tbody></table>"


def mode_select(name: str, selected: str) -> str:
    options = [("paper", "paper"), ("dry-run", "dry-run"), ("live", "live")]
    return "<select name=\"" + name + "\">" + "".join(
        f"<option value=\"{value}\" {'selected' if value == selected else ''}>{label}</option>" for value, label in options
    ) + "</select>"


def bool_select(name: str, selected: bool) -> str:
    return f"""
    <select name="{name}">
      <option value="true" {'selected' if selected else ''}>true</option>
      <option value="false" {'selected' if not selected else ''}>false</option>
    </select>
    """


def strategy_profile_select(strategy: dict[str, Any]) -> str:
    profiles = strategy_profiles(strategy)
    active = active_profile_name(strategy)
    if not profiles:
        return '<select name="active_strategy_profile"></select><p class="warn">No profiles configured.</p>'
    options = "".join(
        f'<option value="{escape(name)}" {"selected" if name == active else ""}>{escape(name)}</option>'
        for name in profiles
    )
    return f'<select name="active_strategy_profile">{options}</select>'


def strategy_profile_summary(strategy: dict[str, Any]) -> str:
    profiles = strategy_profiles(strategy)
    active = active_profile_name(strategy)
    profile = profiles.get(active) or {}
    buy_conditions = profile.get("buy_when", [])
    sell_conditions = profile.get("sell_when", [])
    rows = {
        "Profile Name": active or "Not configured",
        "Buy Conditions": ", ".join(buy_conditions) if buy_conditions else "Not configured",
        "Sell Conditions": ", ".join(sell_conditions) if sell_conditions else "Not configured",
        "Risk Level": profile.get("risk_level") or "Not configured",
        "Notes": profile.get("notes") or "Not configured",
    }
    body = "".join(f"<tr><th>{escape(key)}</th><td>{escape(value)}</td></tr>" for key, value in rows.items())
    return f"<h3>Strategy Description</h3><table>{body}</table>"


def escape(value: Any) -> str:
    return html.escape(str(value))
