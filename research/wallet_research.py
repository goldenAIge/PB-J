#!/usr/bin/env python3
"""
Standalone Polymarket wallet research: fetch public Data API + optional Gamma tags,
print a terminal summary. Does not import project agent code.

Usage:
  python research/wallet_research.py
  python research/wallet_research.py 0xabc... 0xdef...
  python research/wallet_research.py "Label:0xabc..."
  python research/wallet_research.py --no-gamma   # skip Gamma (faster, heuristic categories only)

Limits (Polymarket API): activity offset is capped (max offset 3000). Use GET /closed-positions
for historical realized P/L (paginated, limit 50 per page). Do NOT use
/positions?closed=true for win rate — it mirrors the open book, not settled history.
"""

from __future__ import annotations

import argparse
import json
import re
import sys
from pathlib import Path
from collections import defaultdict
from dataclasses import dataclass, field
from datetime import datetime, timezone
from typing import Any

import requests

DATA_API = "https://data-api.polymarket.com"
GAMMA_API = "https://gamma-api.polymarket.com"
SCRIPT_DIR = Path(__file__).resolve().parent
DEFAULT_REPORT_FILE = SCRIPT_DIR / "wallet_report.txt"
DEFAULT_WALLETS: list[tuple[str, str]] = [
    (
        "SeniorLaghetto",
        "0x05ab749a8554fb7c852238c271d384bae6798145",
    ),
]

SESSION = requests.Session()


class TeeStdout:
    """Mirror stdout to multiple text streams (terminal + report file)."""

    def __init__(self, *files: Any) -> None:
        self.files = files

    def write(self, s: str) -> int:
        for f in self.files:
            f.write(s)
            f.flush()
        return len(s)

    def flush(self) -> None:
        for f in self.files:
            f.flush()


# EVM address after optional "label:" (label may start with 0x or contain extra colons).
_CLI_ADDR_RE = re.compile(r"^0x[0-9a-fA-F]{40}$")


def parse_wallet_cli_arg(raw: str, idx: int) -> tuple[str, str]:
    """
    Accept: '0xabc...40hex...' | 'Name:0xabc...' | '0xSomethingElse:0xabc...'
    (last segment must be a full 0x address.)
    """
    w = raw.strip()
    default_lab = f"wallet_{idx + 1}"
    if _CLI_ADDR_RE.fullmatch(w):
        return default_lab, w.lower()
    if ":" in w:
        left, right = w.rsplit(":", 1)
        right = right.strip()
        if _CLI_ADDR_RE.fullmatch(right):
            return (left.strip() or default_lab, right.lower())
    return default_lab, w
SESSION.headers.update(
    {
        "Accept": "application/json",
        "User-Agent": "PB&J-wallet-research/1.0",
    }
)


def _print_raw_debug(label: str, text: str, max_len: int = 20000) -> None:
    snippet = text if len(text) <= max_len else text[:max_len] + "\n... [truncated]"
    print(f"\n--- DEBUG RAW ({label}) ---\n{snippet}\n--- END RAW ---\n")


def fetch_json(
    url: str,
    params: dict[str, Any] | None = None,
    timeout: float = 60.0,
) -> Any:
    """GET JSON; on failure prints raw body and re-raises."""
    try:
        r = SESSION.get(url, params=params or {}, timeout=timeout)
        text = r.text
        if r.status_code != 200:
            _print_raw_debug(f"HTTP {r.status_code} {url}", text)
            r.raise_for_status()
        try:
            return json.loads(text)
        except json.JSONDecodeError as e:
            _print_raw_debug(f"JSON decode {url}", text)
            raise e
    except requests.RequestException as e:
        print(f"Request error: {e}", file=sys.stderr)
        raise


def normalize_address(addr: str) -> str:
    a = addr.strip()
    if not a.startswith("0x"):
        a = "0x" + a
    return a.lower()


def paginate_open_positions(user: str) -> list[dict[str, Any]]:
    """Fetch all pages of GET /positions (current open book only)."""
    out: list[dict[str, Any]] = []
    offset = 0
    page_size = 500
    while True:
        params: dict[str, Any] = {
            "user": user,
            "limit": page_size,
            "offset": offset,
        }
        data = fetch_json(f"{DATA_API}/positions", params=params)
        if not isinstance(data, list):
            _print_raw_debug("positions unexpected type", repr(data)[:20000])
            raise ValueError("positions: expected JSON array")
        if not data:
            break
        out.extend(data)
        if len(data) < page_size:
            break
        offset += page_size
    return out


def paginate_closed_positions(user: str) -> list[dict[str, Any]]:
    """
    Historical closed markets via GET /closed-positions (not /positions?closed=true).

    Docs: limit max 50, offset up to 100000; sortBy REALIZEDPNL, TIMESTAMP, etc.
    """
    out: list[dict[str, Any]] = []
    offset = 0
    page_size = 50
    while True:
        params: dict[str, Any] = {
            "user": user,
            "limit": page_size,
            "offset": offset,
            "sortBy": "TIMESTAMP",
            "sortDirection": "DESC",
        }
        data = fetch_json(f"{DATA_API}/closed-positions", params=params)
        if not isinstance(data, list):
            _print_raw_debug("closed-positions unexpected type", repr(data)[:20000])
            raise ValueError("closed-positions: expected JSON array")
        if not data:
            break
        out.extend(data)
        if len(data) < page_size:
            break
        offset += page_size
    return out


def parse_activity_payload(data: Any) -> tuple[list[dict[str, Any]], str | None]:
    """
    Returns (items, next_cursor_or_offset_hint).
    Supports plain list or wrapped shapes with cursor / next_cursor.
    """
    if isinstance(data, list):
        return data, None
    if isinstance(data, dict):
        items = (
            data.get("data")
            or data.get("activities")
            or data.get("results")
            or data.get("items")
        )
        if isinstance(items, list):
            cur = (
                data.get("cursor")
                or data.get("next_cursor")
                or data.get("nextCursor")
            )
            return items, cur if isinstance(cur, str) else None
    _print_raw_debug("activity unexpected shape", json.dumps(data, indent=2)[:20000])
    raise ValueError("activity: unhandled JSON shape")


# Polymarket Data API rejects activity requests when offset exceeds this bound.
MAX_ACTIVITY_OFFSET = 3000


def paginate_activity(user: str, limit: int = 500) -> tuple[list[dict[str, Any]], str | None]:
    """
    Paginate GET /activity using offset until a short page is returned.
    Polymarket caps historical offset (max 3000); older history is not returned.

    If the API returns a wrapped body with cursor / next_cursor, also pull that page
    (best-effort). Returns (rows, truncation_note_or_none).
    """
    all_rows: list[dict[str, Any]] = []
    offset = 0
    seen_cursor_pages: set[str] = set()
    truncation: str | None = None

    while True:
        params: dict[str, Any] = {"user": user, "limit": limit, "offset": offset}
        r = SESSION.get(f"{DATA_API}/activity", params=params, timeout=60.0)
        text = r.text
        if r.status_code == 400 and "max historical activity offset" in text:
            truncation = (
                "Activity history stopped at API offset cap (older trades not returned). "
                f"Last offset attempted: {offset}."
            )
            print(f"Note: {truncation}", file=sys.stderr)
            break
        if r.status_code != 200:
            _print_raw_debug(f"HTTP {r.status_code} {DATA_API}/activity", text)
            r.raise_for_status()
        try:
            data = json.loads(text)
        except json.JSONDecodeError as e:
            _print_raw_debug("activity JSON decode", text)
            raise e

        items, cursor = parse_activity_payload(data)
        if not items:
            break
        all_rows.extend(items)

        if cursor and cursor not in seen_cursor_pages:
            seen_cursor_pages.add(cursor)
            r2 = SESSION.get(
                f"{DATA_API}/activity",
                params={"user": user, "limit": limit, "cursor": cursor},
                timeout=60.0,
            )
            if r2.status_code == 200:
                try:
                    data_c = json.loads(r2.text)
                except json.JSONDecodeError:
                    _print_raw_debug("activity cursor page JSON", r2.text)
                else:
                    extra, _ = parse_activity_payload(data_c)
                    all_rows.extend(extra)

        if len(items) < limit:
            break
        next_offset = offset + len(items)
        if next_offset > MAX_ACTIVITY_OFFSET:
            truncation = (
                f"Activity capped at API max offset ({MAX_ACTIVITY_OFFSET}); "
                "older trades are not available via offset pagination."
            )
            break
        offset = next_offset

    return all_rows, truncation


def open_book_pnl(p: dict[str, Any]) -> float:
    """Mark-to-market + realized on current /positions row (open book)."""
    cash = float(p.get("cashPnl") or 0)
    realized = float(p.get("realizedPnl") or 0)
    return cash + realized


def closed_realized_pnl(p: dict[str, Any]) -> float:
    """Realized P/L on a fully closed position (/closed-positions)."""
    return float(p.get("realizedPnl") or 0)


def fetch_portfolio_value_usdc(user: str) -> float | None:
    """Optional: GET /value — total position value (not P/L)."""
    try:
        data = fetch_json(f"{DATA_API}/value", params={"user": user})
        if isinstance(data, list) and data and isinstance(data[0], dict):
            v = data[0].get("value")
            return float(v) if v is not None else None
    except Exception:
        return None
    return None


def parse_end_date(s: str | None) -> datetime | None:
    if not s or not str(s).strip():
        return None
    raw = str(s).strip()
    if len(raw) >= 10 and raw[4] == "-" and raw[7] == "-":
        try:
            return datetime.strptime(raw[:10], "%Y-%m-%d").replace(tzinfo=timezone.utc)
        except ValueError:
            pass
    try:
        return datetime.fromisoformat(raw.replace("Z", "+00:00"))
    except ValueError:
        return None


# --- Category: Gamma tags + keyword fallback ---

TAG_SLUG_TO_BUCKET: dict[str, str] = {
    "politics": "politics",
    "us-politics": "politics",
    "world": "politics",
    "geopolitics": "politics",
    "crypto": "crypto",
    "bitcoin": "crypto",
    "ethereum": "crypto",
    "tech": "tech",
    "ai": "tech",
    "science": "tech",
    "sports": "sports",
    "nba": "sports",
    "nfl": "sports",
    "mlb": "sports",
    "soccer": "sports",
    "culture": "culture",
    "pop-culture": "culture",
    "entertainment": "culture",
}


def keyword_bucket(text: str) -> str:
    t = text.lower()
    checks: list[tuple[str, tuple[str, ...]]] = [
        ("crypto", ("bitcoin", "ethereum", "crypto", "token", "fdv", "airdrop", "solana", "defi")),
        ("sports", ("nba", "nfl", "mlb", "nhl", "ufc", "super bowl", "world cup", "premier league")),
        ("tech", ("gpt", " openai", "ai ", "tech", "apple", "google", "tesla")),
        ("politics", ("trump", "biden", "election", "senate", "congress", "president", "gop", "democrat")),
        ("culture", ("oscar", "grammy", "movie", "celebrity", "album")),
    ]
    for bucket, keys in checks:
        if any(k in t for k in keys):
            return bucket
    return "other"


@dataclass
class GammaCache:
    slug_tags: dict[str, list[dict[str, Any]]] = field(default_factory=dict)

    def tags_for_event_slug(self, slug: str) -> list[dict[str, Any]]:
        if not slug or not str(slug).strip():
            return []
        if slug in self.slug_tags:
            return self.slug_tags[slug]
        try:
            data = fetch_json(f"{GAMMA_API}/events", params={"slug": slug})
        except Exception:
            self.slug_tags[slug] = []
            return []
        if not isinstance(data, list) or not data:
            self.slug_tags[slug] = []
            return []
        ev = data[0]
        tags = ev.get("tags") or []
        self.slug_tags[slug] = tags if isinstance(tags, list) else []
        return self.slug_tags[slug]


def bucket_from_tags(tags: list[dict[str, Any]]) -> str | None:
    labels_and_slugs: list[str] = []
    for t in tags:
        if isinstance(t, dict):
            if t.get("slug"):
                labels_and_slugs.append(str(t["slug"]).lower())
            if t.get("label"):
                labels_and_slugs.append(str(t["label"]).lower())
    for s in labels_and_slugs:
        if s in TAG_SLUG_TO_BUCKET:
            return TAG_SLUG_TO_BUCKET[s]
    for s in labels_and_slugs:
        for key, bucket in TAG_SLUG_TO_BUCKET.items():
            if key in s or s in key:
                return bucket
    return None


def classify_market(
    event_slug: str | None,
    title: str | None,
    gamma: GammaCache | None,
) -> str:
    text = f"{event_slug or ''} {title or ''}"
    if gamma and event_slug:
        tags = gamma.tags_for_event_slug(event_slug)
        b = bucket_from_tags(tags)
        if b:
            return b
    return keyword_bucket(text)


@dataclass
class WalletReport:
    label: str
    address: str
    open_positions: list[dict[str, Any]]
    closed_positions: list[dict[str, Any]]
    activity: list[dict[str, Any]]
    activity_truncation: str | None = None


def load_wallet(label: str, address: str) -> WalletReport:
    addr = normalize_address(address)
    open_p = paginate_open_positions(addr)
    closed_p = paginate_closed_positions(addr)
    act, trunc_note = paginate_activity(addr, limit=500)

    return WalletReport(
        label=label,
        address=addr,
        open_positions=open_p,
        closed_positions=closed_p,
        activity=act,
        activity_truncation=trunc_note,
    )


def analyze_and_print(
    report: WalletReport,
    use_gamma: bool,
    material_loss_usd: float,
) -> None:
    gamma = GammaCache() if use_gamma else None

    trades = [a for a in report.activity if a.get("type") == "TRADE"]
    total_trades = len(trades)

    # USDC size per trade
    usdc_sizes: list[float] = []
    for t in trades:
        u = t.get("usdcSize")
        if u is not None:
            try:
                usdc_sizes.append(float(u))
            except (TypeError, ValueError):
                pass
        else:
            try:
                sz = float(t.get("size") or 0)
                pr = float(t.get("price") or 0)
                usdc_sizes.append(sz * pr)
            except (TypeError, ValueError):
                pass
    avg_trade_usdc = sum(usdc_sizes) / len(usdc_sizes) if usdc_sizes else 0.0

    open_rows = report.open_positions
    open_initial_vals: list[float] = []
    for p in open_rows:
        try:
            open_initial_vals.append(float(p.get("initialValue") or 0))
        except (TypeError, ValueError):
            pass
    avg_open_position_usdc = (
        sum(open_initial_vals) / len(open_initial_vals)
        if open_initial_vals
        else 0.0
    )

    # Historical closes: GET /closed-positions (realizedPnl only on settled positions)
    closed = report.closed_positions
    rp_list = [closed_realized_pnl(p) for p in closed]
    n_win = sum(1 for x in rp_list if x > 0)
    n_loss = sum(1 for x in rp_list if x < 0)
    n_flat = sum(1 for x in rp_list if x == 0)
    wins_amt = [x for x in rp_list if x > 0]
    losses_amt = [x for x in rp_list if x < 0]

    win_rate_all = (100.0 * n_win / (n_win + n_loss)) if (n_win + n_loss) else 0.0

    # "Material loss" denominator (matches many dashboards, e.g. ~81% vs counting tiny losses)
    mat_loss = sum(
        1 for x in rp_list if x <= -float(material_loss_usd)
    )
    scratch_loss = sum(1 for x in rp_list if -float(material_loss_usd) < x < 0)
    win_rate_material = (100.0 * n_win / (n_win + mat_loss)) if (n_win + mat_loss) else 0.0

    sum_closed_realized = sum(rp_list)
    open_pnls = [open_book_pnl(p) for p in open_rows]
    sum_open_book = sum(open_pnls)
    total_pnl_estimate = sum_closed_realized + sum_open_book

    pv = fetch_portfolio_value_usdc(report.address)

    # Category: trade counts from activity
    trade_cat_counts: dict[str, int] = defaultdict(int)
    for t in trades:
        cat = classify_market(
            t.get("eventSlug") or None,
            t.get("title"),
            gamma,
        )
        trade_cat_counts[cat] += 1

    # Category: closed positions (historical)
    pos_cat_counts: dict[str, int] = defaultdict(int)
    pos_wins_by_cat: dict[str, int] = defaultdict(int)
    pos_loss_by_cat: dict[str, int] = defaultdict(int)
    for p in closed:
        cat = classify_market(p.get("eventSlug") or None, p.get("title"), gamma)
        pos_cat_counts[cat] += 1
        r = closed_realized_pnl(p)
        if r > 0:
            pos_wins_by_cat[cat] += 1
        elif r < 0:
            pos_loss_by_cat[cat] += 1

    # Entry timing: first BUY per (conditionId, asset) vs endDate
    end_by_condition: dict[str, datetime | None] = {}
    for p in report.open_positions + closed:
        cid = p.get("conditionId")
        if cid and cid not in end_by_condition:
            end_by_condition[cid] = parse_end_date(p.get("endDate"))

    first_buy: dict[tuple[str, str], int] = {}
    for t in sorted(trades, key=lambda x: int(x.get("timestamp") or 0)):
        if (t.get("side") or "").upper() != "BUY":
            continue
        cid = t.get("conditionId") or ""
        aid = str(t.get("asset") or "")
        key = (cid, aid)
        ts = int(t.get("timestamp") or 0)
        if key not in first_buy:
            first_buy[key] = ts

    days_list: list[int] = []
    for (cid, _aid), ts in first_buy.items():
        end = end_by_condition.get(cid)
        if not end:
            continue
        trade_dt = datetime.fromtimestamp(ts, tz=timezone.utc)
        delta = (end - trade_dt).days
        if delta >= 0:
            days_list.append(delta)

    avg_days_before = sum(days_list) / len(days_list) if days_list else 0.0

    ts_all = [int(x.get("timestamp") or 0) for x in report.activity if x.get("timestamp")]
    last_ts = max(ts_all) if ts_all else 0
    last_dt = (
        datetime.fromtimestamp(last_ts, tz=timezone.utc).strftime("%Y-%m-%d %H:%M UTC")
        if last_ts
        else "n/a"
    )

    ranked = sorted(
        [p for p in closed if closed_realized_pnl(p) > 0],
        key=closed_realized_pnl,
        reverse=True,
    )[:5]

    # --- Print ---
    print("=" * 72)
    print(f"Wallet: {report.label}")
    print(f"Address: {report.address}")
    print("=" * 72)

    print(
        "\nData sources: GET /positions (open book), GET /closed-positions (settled"
        "\nhistory), GET /activity (fills). Do not use /positions?closed=true for"
        "\nsettled stats — it duplicates the open snapshot.\n"
    )

    print(f"Activity rows fetched: {len(report.activity)}")
    if report.activity_truncation:
        print(f"Warning: {report.activity_truncation}")
    print(f"Total TRADE events (activity): {total_trades}")
    print(f"Open position rows (/positions): {len(report.open_positions)}")
    print(f"Closed positions (/closed-positions): {len(closed)}")

    print("\n--- P/L (USDC) ---")
    print(f"  Sum realized P/L (all /closed-positions): ${sum_closed_realized:,.2f}")
    print(
        f"  Open book P/L (sum of cashPnl+realizedPnl on /positions): ${sum_open_book:,.2f}"
    )
    print(
        f"  Combined (realized from history + current book): ${total_pnl_estimate:,.2f}"
    )
    if pv is not None:
        print(
            f"  Cross-check: GET /value (position value, not P/L): ${pv:,.2f}"
        )

    print("\n--- Win rate (settled positions from /closed-positions) ---")
    print(
        f"  Wins (realizedPnl > 0): {n_win} | Losses (< 0): {n_loss} | Flat (0): {n_flat}"
    )
    print(
        f"  Win rate (wins / wins+losses, excludes flat): {win_rate_all:.1f}%"
    )
    print(
        f"  Win rate (wins / wins + material losses only, loss <= -${material_loss_usd:.0f}): "
        f"{win_rate_material:.1f}%"
    )
    print(
        f"     Material losses: {mat_loss} | Small losses (scratch, > -${material_loss_usd:.0f}): {scratch_loss}"
    )

    print("\n--- Trade sizing (TRADE activity fills) ---")
    print(f"Average trade notional (USDC): ${avg_trade_usdc:,.2f}")

    print("\n--- Open position sizing (current book /positions) ---")
    print(f"Open positions count: {len(open_rows)}")
    print(
        f"Average initial notional per open position (initialValue, USDC): "
        f"${avg_open_position_usdc:,.2f}"
    )

    print("\n--- P/L breakdown (closed history, realizedPnl per position) ---")
    print(f"  Gross wins (sum of positive): ${sum(wins_amt):,.2f}")
    print(f"  Gross losses (sum of negative): ${sum(losses_amt):,.2f}")
    print(f"  Best closed position: ${max(rp_list) if rp_list else 0:,.2f}")
    print(f"  Worst closed position: ${min(rp_list) if rp_list else 0:,.2f}")

    print("\n--- Category: trade counts (TRADE events) ---")
    for cat in sorted(trade_cat_counts.keys(), key=lambda c: -trade_cat_counts[c]):
        print(f"  {cat}: {trade_cat_counts[cat]} trades")

    print("\n--- Category: settled positions (/closed-positions) ---")
    cats = set(pos_cat_counts.keys()) | set(pos_wins_by_cat.keys()) | set(
        pos_loss_by_cat.keys()
    )
    for cat in sorted(cats, key=lambda c: -pos_cat_counts.get(c, 0)):
        pc = pos_cat_counts.get(cat, 0)
        w = pos_wins_by_cat.get(cat, 0)
        l = pos_loss_by_cat.get(cat, 0)
        denom = w + l
        wr = (100.0 * w / denom) if denom else 0.0
        print(f"  {cat}: positions={pc} | wins={w} losses={l} | win rate {wr:.1f}%")

    print("\n--- Entry timing (first BUY vs market endDate) ---")
    print(f"Samples with known endDate: {len(days_list)}")
    print(f"Average days before resolution at first BUY: {avg_days_before:.1f} days")

    print("\n--- CURRENT OPEN POSITIONS (GET /positions) ---")
    if not open_rows:
        print("  (none — no open positions in API snapshot)")
    else:
        sorted_open = sorted(
            open_rows,
            key=lambda p: (p.get("title") or "").lower(),
        )
        for i, p in enumerate(sorted_open, 1):
            title = (p.get("title") or "(no title)").replace("\n", " ").strip()
            outc = p.get("outcome") or ""
            try:
                apx = float(p.get("avgPrice") or 0)
                cpx = float(p.get("curPrice") or 0)
                sz = float(p.get("size") or 0)
                init_v = float(p.get("initialValue") or 0)
                cur_v = float(p.get("currentValue") or 0)
            except (TypeError, ValueError):
                apx = cpx = sz = init_v = cur_v = 0.0
            slug = p.get("eventSlug") or ""
            print(f"  {i}. {title}")
            print(
                f"      outcome={outc} | entry (avgPrice)={apx:.4f} | "
                f"curPrice={cpx:.4f} | size (shares)={sz:,.4f}"
            )
            print(
                f"      initialValue=${init_v:,.2f} | currentValue=${cur_v:,.2f} | "
                f"slug={slug}"
            )

    print("\n--- Recency ---")
    print(f"Most recent activity timestamp: {last_dt}")

    print("\n--- Top 5 closed positions by realized P/L ---")
    if not ranked:
        print("  (none positive)")
    else:
        for i, p in enumerate(ranked, 1):
            net = closed_realized_pnl(p)
            title = (p.get("title") or "")[:70]
            print(
                f"  {i}. ${net:,.2f} — {title}\n"
                f"      slug={p.get('eventSlug')} | outcome={p.get('outcome')}"
            )

    print("\n" + "=" * 72 + "\n")


def parse_args() -> argparse.Namespace:
    ap = argparse.ArgumentParser(description="Polymarket wallet research (Data API)")
    ap.add_argument(
        "wallets",
        nargs="*",
        help="Hex addresses (optional). Default: built-in test wallet.",
    )
    ap.add_argument(
        "--no-gamma",
        action="store_true",
        help="Skip Gamma API; use keyword buckets only for categories.",
    )
    ap.add_argument(
        "--material-loss-usd",
        type=float,
        default=50.0,
        help=(
            "For 'material loss' win rate: count a loss only if realizedPnl <= -this "
            "amount (default 50). Matches many dashboards that ignore scratch losses."
        ),
    )
    ap.add_argument(
        "--report-file",
        type=str,
        default=str(DEFAULT_REPORT_FILE),
        help=(
            "Write the full terminal report to this UTF-8 file "
            f"(default: {DEFAULT_REPORT_FILE.name} next to this script)."
        ),
    )
    ap.add_argument(
        "--no-report-file",
        action="store_true",
        help="Do not write the report file (terminal only).",
    )
    return ap.parse_args()


def main() -> None:
    args = parse_args()
    use_gamma = not args.no_gamma

    pairs: list[tuple[str, str]] = []
    if args.wallets:
        for i, w in enumerate(args.wallets):
            pairs.append(parse_wallet_cli_arg(w, i))
    else:
        pairs = list(DEFAULT_WALLETS)

    report_path: Path | None = None
    report_f = None
    old_stdout = sys.stdout
    if not args.no_report_file:
        report_path = Path(args.report_file).expanduser()
        if not report_path.is_absolute():
            report_path = (Path.cwd() / report_path).resolve()
        report_path.parent.mkdir(parents=True, exist_ok=True)
        report_f = open(report_path, "w", encoding="utf-8")
        sys.stdout = TeeStdout(old_stdout, report_f)
        print(f"Report also saved to: {report_path}\n", file=old_stdout)
        print(f"Full report file: {report_path}\n")

    try:
        for label, addr in pairs:
            try:
                rep = load_wallet(label, addr)
                analyze_and_print(
                    rep,
                    use_gamma=use_gamma,
                    material_loss_usd=args.material_loss_usd,
                )
            except Exception as e:
                print(f"\nERROR analyzing {label} ({addr}): {e}\n", file=sys.stderr)
                sys.exit(1)
    finally:
        if report_f is not None:
            sys.stdout = old_stdout
            report_f.close()


if __name__ == "__main__":
    main()
