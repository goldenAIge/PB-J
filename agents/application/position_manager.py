"""
Position Manager — live arcade dashboard for Polymarket positions.

Streams positions from data-api.polymarket.com in a Rich Live display,
auto-refreshing every 30 seconds. Accepts commands while the display
updates: `close 1,3,5`, `close all`, `r` refresh, `d` toggle dry-run,
`q` quit. Closes go through Polymarket.execute_limit_sell() at best bid.

Usage:
    python -m agents.application.position_manager
    python -m agents.application.position_manager --dry-run
"""

import os
import sys
import json
import time
import select
import termios
import tty
import logging
import argparse
from contextlib import contextmanager
from datetime import date, datetime
from pathlib import Path
from typing import Optional

import httpx
from dotenv import load_dotenv
from rich.console import Console, Group
from rich.live import Live
from rich.table import Table
from rich.panel import Panel
from rich.text import Text
from rich.box import DOUBLE, HEAVY, ROUNDED, SIMPLE, SIMPLE_HEAVY

from agents.polymarket.polymarket import Polymarket

load_dotenv()

DATA_API = "https://data-api.polymarket.com"
PROJECT_ROOT = Path(__file__).resolve().parents[2]
LOG_DIR = PROJECT_ROOT / "logs"
LOG_DIR.mkdir(parents=True, exist_ok=True)
LOG_FILE = LOG_DIR / "position_manager.log"
DAILY_SNAPSHOT_FILE = PROJECT_ROOT / "agents" / "data" / "position_manager_daily.json"

logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s - %(levelname)s - %(message)s",
    handlers=[logging.FileHandler(LOG_FILE)],
)
logger = logging.getLogger(__name__)

console = Console()

# --- Clean cyan / deep navy palette ---
BG         = "#000811"   # dashboard background
HEADER_BG  = "#000d1a"   # header + footer bar background
PINK       = "#FF006E"
ORANGE     = "#FF6600"
ACCENT     = "#FFE600"
CYAN       = "#00ccff"
GREEN      = "#00FF88"
RED        = PINK
DIMGREY    = "grey42"

# Cyan approximations for alpha tints (no true alpha in terminal).
CYAN_66    = "#0088b3"   # ≈ #00ccff66
CYAN_55    = "#00749a"   # ≈ #00ccff55
CYAN_44    = "#005a80"   # ≈ #00ccff44
CYAN_33    = "#00425e"   # ≈ #00ccff33
CYAN_22    = "#002b3d"   # ≈ #00ccff22
WHITE_22   = "#333333"   # ≈ #ffffff22

# Row backgrounds
ROW_BG_WIN  = "on #0d1100"   # win: dark orange tint
ROW_BG_LOSS = "on #1a0010"   # loss: dark pink tint

# Per-card backgrounds (card bodies, not dashboard)
CARD_BG_POS = "#0d0d00"
CARD_BG_VAL = "#001122"
CARD_BG_OPN = "#1a0010"
CARD_BG_TDY = "#001a0a"
CARD_BG_BAL = "#1a0800"

PANEL_BG   = f"on {BG}"
HEADER_STY = f"on {HEADER_BG}"


def resolve_wallet_address() -> str:
    addr = os.getenv("POLYMARKET_ADDRESS")
    if addr:
        return addr
    pk = os.getenv("POLYGON_WALLET_PRIVATE_KEY")
    if pk:
        from eth_account import Account
        return Account.from_key(pk).address
    raise RuntimeError("Set POLYMARKET_ADDRESS (or POLYGON_WALLET_PRIVATE_KEY) in .env")


def fetch_positions(address: str) -> list[dict]:
    r = httpx.get(f"{DATA_API}/positions", params={"user": address}, timeout=15)
    r.raise_for_status()
    data = r.json()
    return [p for p in data if float(p.get("size") or 0) > 0]


def days_until(end_date_str: str) -> Optional[int]:
    if not end_date_str:
        return None
    try:
        s = end_date_str.split("T")[0]
        end = datetime.strptime(s, "%Y-%m-%d").date()
        return (end - date.today()).days
    except Exception:
        return None


def _read_daily_snapshot() -> dict:
    try:
        return json.loads(DAILY_SNAPSHOT_FILE.read_text())
    except (FileNotFoundError, json.JSONDecodeError):
        return {}


def _write_daily_snapshot(snap: dict) -> None:
    DAILY_SNAPSHOT_FILE.parent.mkdir(parents=True, exist_ok=True)
    DAILY_SNAPSHOT_FILE.write_text(json.dumps(snap, indent=2))


def daily_realized_pnl(positions: list[dict]) -> float:
    # Daily realized = change in cumulative realizedPnl across currently-held
    # positions since first render of the UTC day. Fully-closed positions drop
    # off /positions, so their contribution is not captured here.
    cumulative = sum(float(p.get("realizedPnl") or 0) for p in positions)
    today = date.today().isoformat()
    snap = _read_daily_snapshot()
    if snap.get("date") != today:
        snap = {"date": today, "baseline": cumulative}
        _write_daily_snapshot(snap)
    return cumulative - float(snap.get("baseline", cumulative))


# --- Arcade UI components ---

def _addr_short(address: str) -> str:
    """0xD55a914800616d6a44E93327e164ac828CF146be → 0xD55a...46be"""
    if not address:
        return "0x????...????"
    return f"{address[:6]}...{address[-4:]}"


def build_header_bar(address: str, dry_run: bool) -> Panel:
    left = Text.from_markup(
        f"[{CYAN_55}]POLYBOT & JELLY  ·  {_addr_short(address)}[/]"
    )
    if dry_run:
        right = Text.from_markup(
            f"[bold {ACCENT}]● DRY-RUN[/]   [{WHITE_22}]LIVE[/]"
        )
    else:
        right = Text.from_markup(
            f"[{WHITE_22}]DRY-RUN[/]   [bold {GREEN}]● LIVE[/]"
        )
    grid = Table.grid(expand=True, padding=(0, 1))
    grid.add_column(justify="left", ratio=1)
    grid.add_column(justify="right", ratio=1)
    grid.add_row(left, right)
    return Panel(grid, border_style=CYAN_44, box=HEAVY, padding=(0, 1),
                 style=HEADER_STY)


def build_title() -> Group:
    """Exact 14-line title: scanlines, fading star rulers, thick rulers,
    PB&J, cyan double underline (narrow bright + wider dim), subtitle."""
    blank = Text(" ", style=PANEL_BG)

    # Alpha-blend approximations on #000811 background
    cyan_18      = "#001a27"   # ≈ #00ccff18
    pink_33      = "#330624"   # ≈ #FF006E33
    pink_44      = "#44062a"   # ≈ #FF006E44
    pink_66      = "#660536"   # ≈ #FF006E66
    cyan_thin_33 = "#002f41"   # ≈ #00ccff33

    scanline = Text("░" * 120, style=f"{cyan_18} {PANEL_BG}", justify="center")

    def make_star_ruler() -> Text:
        t = Text(justify="center")
        dashes = "─" * 36
        t.append(dashes, style=f"{pink_33} {PANEL_BG}")
        t.append(" ✦ ", style=f"bold {pink_44} {PANEL_BG}")
        t.append(dashes, style=f"{pink_33} {PANEL_BG}")
        return t

    thick_ruler = Text("━" * 88, style=f"{pink_66} {PANEL_BG}", justify="center")

    # Line 6: bold hot-pink PB&J, wide letter spacing via multiple spaces
    title_line = Text(
        "P              B              &              J",
        style=f"bold {PINK} {PANEL_BG}",
        justify="center",
    )

    # Double underline: bright solid at 55% width, dimmer at 72% width
    cyan_underline_narrow = Text("━" * 83, style=f"bold {CYAN} {PANEL_BG}", justify="center")
    cyan_underline_wide = Text("─" * 108, style=f"{cyan_thin_33} {PANEL_BG}", justify="center")

    subtitle = Text(
        "P  O  S  I  T  I  O  N     M  A  N  A  G  E  R",
        style=f"bold {CYAN_66} {PANEL_BG}",
        justify="center",
    )

    return Group(
        blank,                    # 1
        scanline,                 # 2
        make_star_ruler(),        # 3
        thick_ruler,              # 4
        blank,                    # 5
        title_line,               # 6
        blank,                    # 7
        thick_ruler,              # 8
        make_star_ruler(),        # 9
        scanline,                 # 10
        blank,                    # 11
        cyan_underline_narrow,    # 12
        cyan_underline_wide,      # 13
        subtitle,                 # 14
    )


_RAINBOW_COLORS = [
    "#FF006E", "#FF4400", "#FF6600", "#FF8800", "#FFE600",
    "#00FF88", "#00ccff", "#0044ff", "#8800ff", "#FF006E",
]


def build_rainbow_stripe(cells_per_band: int = 12) -> Group:
    """Full-width rainbow stripe, 3 rows tall, 10 color stops."""
    rows = []
    for _ in range(3):
        row = Text(justify="center")
        for color in _RAINBOW_COLORS:
            row.append("█" * cells_per_band, style=f"bold {color} {PANEL_BG}")
        rows.append(row)
    return Group(*rows)


_TURBO_STOPS = [
    (0.00, (0xFF, 0x00, 0x6E)),  # pink
    (0.34, (0xFF, 0x66, 0x00)),  # orange
    (0.67, (0xFF, 0xE6, 0x00)),  # yellow
    (1.00, (0x00, 0xCC, 0xFF)),  # cyan
]


def _grad_color(pos: float) -> str:
    """4-stop pink → orange → yellow → green gradient at position 0..1."""
    pos = max(0.0, min(1.0, pos))
    for i in range(len(_TURBO_STOPS) - 1):
        p0, c0 = _TURBO_STOPS[i]
        p1, c1 = _TURBO_STOPS[i + 1]
        if p0 <= pos <= p1:
            t = (pos - p0) / (p1 - p0) if p1 > p0 else 0.0
            r = int(c0[0] + (c1[0] - c0[0]) * t)
            g = int(c0[1] + (c1[1] - c0[1]) * t)
            b = int(c0[2] + (c1[2] - c0[2]) * t)
            return f"#{r:02x}{g:02x}{b:02x}"
    return "#888888"


def build_turbo_bar(countdown: float, width: int = 28) -> Text:
    """Depleting refresh bar — pink → orange → yellow → cyan gradient."""
    frac = max(0.0, min(1.0, countdown / REFRESH_INTERVAL_S))
    filled = int(frac * width + 0.5)
    bar = Text()
    for i in range(width):
        pos = i / max(1, width - 1)
        color = _grad_color(pos)
        if i < filled:
            bar.append("█", style=color)
        else:
            bar.append("░", style=CYAN_22)
    return bar


def _metric_card(label: str, value: str, border: str, value_style: str,
                 bg: str = BG) -> Panel:
    # Label in small spaced caps, value big and bold on a chunkier card body
    # painted with its own deep-tinted background. Extra blank padding lines
    # above and below the value make the number feel larger.
    spaced_label = " ".join(label)
    card_style = f"on {bg}"
    blank = Text(" ", style=card_style)
    body = Group(
        Text(spaced_label, style=f"bold {CYAN_44} {card_style}", justify="center"),
        blank, blank,
        Text(value, style=f"{value_style} {card_style}", justify="center"),
        blank, blank,
    )
    return Panel(body, border_style=border, box=HEAVY,
                 padding=(1, 1), style=card_style)


def _fmt_pnl(amount: float) -> str:
    sign = "+" if amount >= 0 else "-"
    return f"{sign}${abs(amount):,.2f}"


def build_metrics_row(positions: list[dict], daily_realized: float, balance: float) -> Table:
    total_value = sum(float(p.get("currentValue") or 0) for p in positions)
    unrealized = sum(float(p.get("cashPnl") or 0) for p in positions)

    open_color = RED if unrealized < 0 else (GREEN if unrealized > 0 else PINK)

    cards = [
        _metric_card("POSITIONS",    f"{len(positions)}",       ACCENT, f"bold {ACCENT}", CARD_BG_POS),
        _metric_card("VALUE",        f"${total_value:,.2f}",    CYAN,   f"bold {CYAN}",   CARD_BG_VAL),
        _metric_card("OPEN P&L",     _fmt_pnl(unrealized),      PINK,   f"bold {open_color}", CARD_BG_OPN),
        _metric_card("TODAY P&L",    _fmt_pnl(daily_realized),  GREEN,  f"bold {GREEN}",  CARD_BG_TDY),
        _metric_card("USDC BALANCE", f"${balance:,.2f}",        ORANGE, f"bold {ORANGE}", CARD_BG_BAL),
    ]
    grid = Table.grid(expand=True, padding=(0, 1))
    for _ in cards:
        grid.add_column(ratio=1)
    grid.add_row(*cards)
    return grid


def build_positions_table(positions: list[dict]) -> Panel:
    banner = Text.from_markup(
        f"[bold {CYAN}]▼ ▼ ▼   PORTFOLIO   ▼ ▼ ▼[/]",
        justify="center",
    )

    if not positions:
        body = Group(banner, Text(""),
                     Text("— NO ACTIVE POSITIONS —",
                          style=f"bold {DIMGREY}", justify="center"))
        return Panel(body, border_style=CYAN_33, box=HEAVY, padding=(0, 1),
                     style=PANEL_BG)

    table = Table(box=SIMPLE_HEAVY, header_style=f"bold {CYAN_44}", expand=True,
                  pad_edge=False, show_edge=False, border_style=CYAN_22,
                  padding=(1, 1))
    table.add_column("#",        width=5,  justify="left",   no_wrap=True)
    table.add_column("MARKET",   ratio=5,  overflow="fold")
    table.add_column("SIDE",     width=6,  justify="center", no_wrap=True)
    table.add_column("COST",     width=10, justify="right",  no_wrap=True)
    table.add_column("VALUE",    width=10, justify="right",  no_wrap=True)
    table.add_column("P&L ($)",  width=12, justify="right",  no_wrap=True)
    table.add_column("P&L (%)",  width=10, justify="right",  no_wrap=True)
    table.add_column("EXPIRES",  width=10, justify="right",  no_wrap=True)

    for i, p in enumerate(positions, start=1):
        title = p.get("title") or "(unknown)"
        outcome = (p.get("outcome") or "").upper() or "—"
        cost = float(p.get("initialValue") or 0)
        value = float(p.get("currentValue") or 0)
        pnl = float(p.get("cashPnl") or 0)
        pct = float(p.get("percentPnl") or 0)
        redeemable = bool(p.get("redeemable"))

        winning = pnl > 0
        losing = pnl < 0
        row_bg = ROW_BG_WIN if winning else (ROW_BG_LOSS if losing else PANEL_BG)
        border_color = ORANGE if winning else (WHITE_22 if losing else CYAN_22)

        num_cell = Text.from_markup(f"[bold {border_color}]▌[/] [bold white]{i}[/]")
        market_cell = Text(title, style="white", overflow="fold")
        side_color = GREEN if outcome == "YES" else (PINK if outcome == "NO" else DIMGREY)
        side_cell = Text(outcome, style=f"bold {side_color}")
        cost_cell = Text(f"${cost:,.2f}", style=DIMGREY)
        value_cell = Text(f"${value:,.2f}", style=f"bold {CYAN}")

        pnl_color = GREEN if winning else (PINK if losing else "white")
        pnl_dollar = Text(_fmt_pnl(pnl), style=f"bold {pnl_color}")
        pnl_pct = Text(f"{pct:+.1f}%", style=f"bold {pnl_color}")

        days = days_until(p.get("endDate", ""))
        if redeemable:
            exp_cell = Text("REDEEM", style=f"bold {PINK}")
        elif days is None:
            exp_cell = Text("—", style=DIMGREY)
        elif days <= 0:
            exp_cell = Text("TODAY ▲", style=f"bold {ACCENT}")
        elif days <= 7:
            exp_cell = Text(f"{days}d ▲", style=f"bold {ACCENT}")
        else:
            exp_cell = Text(f"{days}d", style=DIMGREY)

        table.add_row(num_cell, market_cell, side_cell,
                      cost_cell, value_cell, pnl_dollar, pnl_pct, exp_cell,
                      style=row_bg)

    body = Group(banner, Text(""), table)
    return Panel(body, border_style=CYAN_33, box=HEAVY, padding=(0, 1),
                 style=PANEL_BG)


def build_footer(
    cmd: str,
    status: str,
    status_style: str,
    dry_run: bool,
    seconds_since_refresh: float,
    seconds_to_next_refresh: float,
    confirm_pending: bool,
    last_error: Optional[str],
    last_refresh_epoch: float,
) -> Panel:
    prompt_prefix = f"[bold {ACCENT}][y/n][/] " if confirm_pending else ""
    cursor = "[bold white on white] [/]"
    prompt_line = Text.from_markup(
        f"{prompt_prefix}[bold {ACCENT}]▶[/] {cmd}{cursor}"
    )

    lines = [prompt_line]
    if status:
        lines.append(Text.from_markup(f"[{status_style}]{status}[/]"))
    if last_error:
        lines.append(Text.from_markup(f"[bold {RED}]ERR[/] [white]{last_error}[/]"))

    cmd_bar = (
        f"[bold {PINK}]▶[/] [{PINK}]CLOSE \\[#][/]   "
        f"[bold {ORANGE}]▶[/] [{ORANGE}]CLOSE ALL[/]   "
        f"[bold {ACCENT}]▶[/] [{ACCENT}]R=REFRESH[/]   "
        f"[bold {CYAN}]▶[/] [{CYAN}]D=TOGGLE DRY-RUN[/]   "
        f"[bold {GREEN}]▶[/] [{GREEN}]Q=QUIT[/]"
    )

    countdown = max(0, seconds_to_next_refresh)
    if last_refresh_epoch > 0:
        stamp = time.strftime("%H:%M:%S", time.localtime(last_refresh_epoch))
    else:
        stamp = "--:--:--"

    bar_only = build_turbo_bar(countdown)
    refresh_tail = Text.from_markup(
        f"   [{CYAN_66}]↻ {stamp} · {countdown:3.0f}s[/]"
    )
    refresh_line = Text.assemble(bar_only, refresh_tail)

    grid = Table.grid(expand=True, padding=(0, 1))
    grid.add_column(justify="left", ratio=3)
    grid.add_column(justify="right", ratio=2)
    grid.add_row(Text.from_markup(cmd_bar), refresh_line)

    body = Group(*lines, Text(""), grid)
    border = CYAN_44 if not confirm_pending else PINK
    title_markup = f"[bold {PINK}]CONFIRM (y/n)[/]" if confirm_pending else None
    return Panel(body, title=title_markup, border_style=border, box=HEAVY,
                 padding=(0, 1), style=HEADER_STY)


def build_dashboard(
    address: str,
    positions: list[dict],
    daily_realized: float,
    balance: float,
    cmd_buffer: str,
    status: str,
    status_style: str,
    dry_run: bool,
    seconds_since_refresh: float,
    seconds_to_next_refresh: float,
    confirm_pending: bool,
    last_error: Optional[str],
    last_refresh_epoch: float,
) -> Group:
    """Compose the full live dashboard inside a double-line hot-pink frame."""
    inner = Group(
        build_header_bar(address, dry_run),
        build_title(),
        build_metrics_row(positions, daily_realized, balance),
        build_positions_table(positions),
        build_rainbow_stripe(),
        build_footer(
            cmd_buffer, status, status_style, dry_run,
            seconds_since_refresh, seconds_to_next_refresh,
            confirm_pending, last_error, last_refresh_epoch,
        ),
    )
    return Group(Panel(inner, border_style=CYAN, box=DOUBLE,
                       padding=(0, 1), style=PANEL_BG))


def parse_close_command(cmd: str, n: int) -> list[int]:
    parts = cmd.strip().split(maxsplit=1)
    if len(parts) < 2:
        return []
    arg = parts[1].strip().lower()
    if arg in ("all", "*"):
        return list(range(1, n + 1))
    indices: list[int] = []
    for tok in arg.replace(";", ",").replace(" ", ",").split(","):
        tok = tok.strip()
        if not tok:
            continue
        try:
            idx = int(tok)
        except ValueError:
            raise ValueError(f"not an integer: {tok!r}")
        if idx < 1 or idx > n:
            raise ValueError(f"out of range: {idx} (valid 1–{n})")
        if idx not in indices:
            indices.append(idx)
    return indices


def close_position(poly: Polymarket, pos: dict, dry_run: bool) -> dict:
    """Close one position by placing a GTC limit sell at best bid."""
    token_id = pos.get("asset", "")
    size = float(pos.get("size") or 0)
    title = pos.get("title", "")
    avg = float(pos.get("avgPrice") or 0)

    if not token_id or size <= 0:
        raise ValueError("missing token_id or zero size")

    book = poly.client.get_order_book(token_id)
    if not book.bids:
        raise RuntimeError("no bids on order book")
    best_bid = max(float(b.price) for b in book.bids)
    price = round(best_bid, 3)
    expected_pnl = (price - avg) * size

    payload = {
        "title": title,
        "asset": token_id,
        "size": size,
        "price": price,
        "avg_price": avg,
        "expected_pnl": round(expected_pnl, 4),
        "dry_run": dry_run,
    }

    if dry_run:
        logger.info(f"[DRY RUN] close {size:.4f} @ ${price:.3f} (avg ${avg:.3f}, est P&L ${expected_pnl:+.2f}) :: {title}")
        return {"status": "DRY_RUN", **payload}

    poly.ensure_sell_approval()
    resp = poly.execute_limit_sell(token_id=token_id, price=price, size=size)
    order_id = resp.get("orderID") if isinstance(resp, dict) else None
    logger.info(
        f"CLOSE {size:.4f} @ ${price:.3f} (avg ${avg:.3f}, est P&L ${expected_pnl:+.2f}) "
        f"order={order_id} :: {title}"
    )
    return {"status": "PLACED", "order_id": order_id, **payload}


# --- Non-blocking TTY input for Live dashboard ---

@contextmanager
def _cbreak_mode():
    fd = sys.stdin.fileno()
    old = termios.tcgetattr(fd)
    try:
        tty.setcbreak(fd)
        yield
    finally:
        termios.tcsetattr(fd, termios.TCSADRAIN, old)


def _read_key(timeout: float) -> Optional[str]:
    r, _, _ = select.select([sys.stdin], [], [], timeout)
    if not r:
        return None
    try:
        return sys.stdin.read(1)
    except Exception:
        return None


def _drain_escape() -> None:
    """Swallow trailing bytes of an ANSI escape sequence (arrow keys, etc.)."""
    while _read_key(0.002) is not None:
        pass


# --- Data loader ---

def _load_snapshot(address: str, poly: Polymarket) -> tuple[list[dict], float, float, Optional[str]]:
    """Fetch positions + balance + daily realized. Returns (positions, daily, balance, err)."""
    err: Optional[str] = None
    try:
        positions = fetch_positions(address)
    except Exception as e:
        logger.error(f"fetch_positions failed: {e}")
        return [], 0.0, 0.0, f"positions: {e}"
    try:
        balance = poly.get_usdc_balance()
    except Exception as e:
        logger.warning(f"balance fetch failed: {e}")
        balance = 0.0
        err = f"balance: {e}"
    daily = daily_realized_pnl(positions)
    return positions, daily, balance, err


# --- Live dashboard loop ---

REFRESH_INTERVAL_S = 30.0
TICK_S = 0.2


def run_dashboard(address: str, poly: Polymarket, dry_run: bool) -> None:
    if not sys.stdin.isatty():
        console.print("[red]The live dashboard requires an interactive TTY.[/red]")
        sys.exit(1)

    positions, daily, balance, data_err = _load_snapshot(address, poly)
    last_refresh = time.time()

    state = {
        "dry_run": dry_run,
        "cmd_buffer": "",
        "status": "",
        "status_style": "dim",
        "confirm_indices": [],
        "last_error": data_err,
    }

    def make_dashboard() -> Group:
        now = time.time()
        since = now - last_refresh
        until = REFRESH_INTERVAL_S - since
        return build_dashboard(
            address, positions, daily, balance,
            state["cmd_buffer"], state["status"], state["status_style"],
            state["dry_run"], since, until,
            bool(state["confirm_indices"]), state["last_error"],
            last_refresh,
        )

    with _cbreak_mode(), Live(make_dashboard(), console=console,
                              refresh_per_second=8, screen=True,
                              transient=False) as live:
        while True:
            key = _read_key(TICK_S)

            if key is not None:
                if key in ("\x03", "\x04"):  # Ctrl-C / Ctrl-D
                    return
                if key == "\x1b":
                    _drain_escape()
                    if state["confirm_indices"]:
                        state["confirm_indices"] = []
                        state["status"], state["status_style"] = "confirmation cancelled", DIMGREY
                    else:
                        state["cmd_buffer"] = ""
                elif state["confirm_indices"]:
                    lower = key.lower()
                    if lower == "y":
                        idxs = state["confirm_indices"]
                        state["confirm_indices"] = []
                        state["status"] = f"closing {len(idxs)} position(s)…"
                        state["status_style"] = ACCENT
                        live.update(make_dashboard())
                        results = _execute_closes(poly, positions, idxs, state["dry_run"])
                        ok = sum(1 for r in results if r["ok"])
                        bad = len(results) - ok
                        tag = "dry-run" if state["dry_run"] else "placed"
                        parts = [f"{ok} {tag}"]
                        if bad:
                            parts.append(f"{bad} failed")
                        state["status"] = " · ".join(parts) + " — " + "; ".join(
                            _format_close_result(r) for r in results
                        )
                        state["status_style"] = GREEN if bad == 0 else RED
                        last_refresh = 0
                    else:
                        state["confirm_indices"] = []
                        state["status"], state["status_style"] = "cancelled", DIMGREY
                elif key in ("\r", "\n"):
                    last_refresh = _handle_enter(state, positions, last_refresh)
                    if state.get("_exit"):
                        return
                elif key in ("\x7f", "\x08"):
                    state["cmd_buffer"] = state["cmd_buffer"][:-1]
                elif not state["cmd_buffer"] and key.lower() in ("q", "r", "d"):
                    # Single-key shortcuts only when the buffer is empty
                    k = key.lower()
                    if k == "q":
                        return
                    if k == "r":
                        last_refresh = 0
                        state["status"], state["status_style"] = "refreshing…", DIMGREY
                    elif k == "d":
                        state["dry_run"] = not state["dry_run"]
                        new_mode = "DRY-RUN" if state["dry_run"] else "LIVE"
                        state["status"] = f"mode toggled → {new_mode}"
                        state["status_style"] = ACCENT if state["dry_run"] else GREEN
                        logger.info(f"Mode toggle: dry_run={state['dry_run']}")
                elif key.isprintable():
                    state["cmd_buffer"] += key
                    if state["status"] and state["status_style"] != ACCENT:
                        state["status"], state["status_style"] = "", DIMGREY

            # Auto-refresh tick
            now = time.time()
            if now - last_refresh >= REFRESH_INTERVAL_S:
                positions, daily, balance, data_err = _load_snapshot(address, poly)
                last_refresh = now
                state["last_error"] = data_err

            live.update(make_dashboard())


def _handle_enter(state: dict, positions: list[dict], last_refresh: float) -> float:
    """Process Enter on the current cmd_buffer. Returns (possibly reset) last_refresh."""
    cmd = state["cmd_buffer"].strip().lower()
    state["cmd_buffer"] = ""
    state["last_error"] = None
    if cmd in ("q", "quit", "exit"):
        state["_exit"] = True
        return last_refresh
    if cmd in ("", "r", "refresh"):
        state["status"], state["status_style"] = "refreshing…", DIMGREY
        return 0.0
    if cmd in ("d", "toggle"):
        state["dry_run"] = not state["dry_run"]
        new_mode = "DRY-RUN" if state["dry_run"] else "LIVE"
        state["status"] = f"mode toggled → {new_mode}"
        state["status_style"] = ACCENT if state["dry_run"] else GREEN
        logger.info(f"Mode toggle: dry_run={state['dry_run']}")
        return last_refresh
    if cmd.startswith("close"):
        if not positions:
            state["status"], state["status_style"] = "no positions to close", DIMGREY
            return last_refresh
        try:
            idxs = parse_close_command(cmd, len(positions))
        except ValueError as e:
            state["status"], state["status_style"] = f"invalid: {e}", RED
            return last_refresh
        if idxs:
            state["confirm_indices"] = idxs
            titles = ", ".join(f"#{i}" for i in idxs)
            state["status"] = f"close {titles}? press y to confirm, n/Esc to cancel"
            state["status_style"] = ACCENT
        else:
            state["status"] = "specify positions: close 1,3,5 or close all"
            state["status_style"] = RED
        return last_refresh
    state["status"], state["status_style"] = f"unknown command: {cmd}", RED
    return last_refresh


def _execute_closes(poly: Polymarket, positions: list[dict],
                    indices: list[int], dry_run: bool) -> list[dict]:
    out: list[dict] = []
    for idx in indices:
        pos = positions[idx - 1]
        title = (pos.get("title") or "")[:40]
        try:
            r = close_position(poly, pos, dry_run=dry_run)
            out.append({"idx": idx, "ok": True, "title": title, **r})
        except Exception as e:
            logger.error(f"close failed #{idx} {title}: {e}")
            out.append({"idx": idx, "ok": False, "title": title, "error": str(e)})
    return out


def _format_close_result(r: dict) -> str:
    if not r["ok"]:
        return f"#{r['idx']} FAILED ({r.get('error', '?')[:40]})"
    tag = "DRY" if r.get("status") == "DRY_RUN" else "PLACED"
    return f"#{r['idx']} {tag} {r['size']:.2f}@${r['price']:.3f} (est ${r['expected_pnl']:+.2f})"


def main() -> None:
    ap = argparse.ArgumentParser(description="Polymarket Position Manager (live dashboard)")
    ap.add_argument("--dry-run", action="store_true", help="Preview without placing orders")
    args = ap.parse_args()

    try:
        address = resolve_wallet_address()
    except RuntimeError as e:
        console.print(f"[red]{e}[/red]")
        sys.exit(1)

    logger.info(f"Session start | wallet={address} | dry_run={args.dry_run}")
    poly = Polymarket()

    try:
        run_dashboard(address, poly, dry_run=args.dry_run)
    except KeyboardInterrupt:
        pass
    finally:
        logger.info("Session end")


if __name__ == "__main__":
    main()
