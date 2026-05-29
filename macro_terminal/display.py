"""Bloomberg-style terminal rendering."""

from __future__ import annotations

from macro_terminal.models import TerminalSnapshot

try:
    from rich import box
    from rich.console import Console, Group
    from rich.layout import Layout
    from rich.panel import Panel
    from rich.table import Table
    from rich.text import Text

    RICH_AVAILABLE = True
except ImportError:
    RICH_AVAILABLE = False


def render_plain(snapshot: TerminalSnapshot) -> str:
    lines = [
        f"MACRO TERMINAL | {snapshot.timestamp_et} | 15m {snapshot.candle_15m_window} tick {snapshot.tick_in_candle + 1}/2",
        "",
    ]
    ticker = snapshot.macro_ticker
    if ticker:
        lines.append(
            "TICKER  "
            f"XAU {ticker.gold_spot or 'n/a'}  "
            f"DXY {ticker.dxy or 'n/a'} ({ticker.dxy_change_pct or 0:+.2f}%)  "
            f"10Y {ticker.treasury_10y or 'n/a'} ({ticker.treasury_10y_bps_1d or 0:+.1f}bps)  "
            f"Real {ticker.real_yield or 'n/a'}  BE {ticker.breakeven or 'n/a'}"
        )
    regime = snapshot.regime
    if regime:
        lines.extend([
            "",
            f"REGIME  {regime.primary_regime} | gold {regime.gold_bias} | clarity {regime.clarity}/10",
            f"        {regime.narrative}",
        ])
    lines.extend([
        "",
        f"PRIORITY STORE ({snapshot.queue_depth} active | {snapshot.expired_count} expired)",
        f"  P1={snapshot.priority_store.get('P1', 0)}  "
        f"P2={snapshot.priority_store.get('P2', 0)}  "
        f"P3={snapshot.priority_store.get('P3', 0)}",
        "-" * 72,
    ])
    for idx, item in enumerate(snapshot.top_items, start=1):
        lines.append(
            f"{idx:02d} {item['priority_band']} {item['priority_score']:>5.1f} "
            f"[{item['gold_bias'][:4]}] {item['title'][:64]}"
        )
    if snapshot.errors:
        lines.extend(["", "WARNINGS", *snapshot.errors])
    return "\n".join(lines)


def render_rich(snapshot: TerminalSnapshot) -> Group:
    if not RICH_AVAILABLE:
        raise RuntimeError("rich is not installed")

    header = Text.assemble(
        ("GOLD MACRO TERMINAL", "bold yellow"),
        "  |  ",
        (snapshot.timestamp_et, "cyan"),
        "  |  ",
        (f"15m {snapshot.candle_15m_window}", "magenta"),
        "  tick ",
        (f"{snapshot.tick_in_candle + 1}/2", "bold"),
        "  |  store ",
        (f"P1:{snapshot.priority_store.get('P1', 0)} P2:{snapshot.priority_store.get('P2', 0)} P3:{snapshot.priority_store.get('P3', 0)}", "bold green"),
        "  exp ",
        (str(snapshot.expired_count), "dim"),
    )

    ticker = snapshot.macro_ticker
    ticker_table = Table.grid(expand=True)
    ticker_table.add_column(justify="left")
    ticker_table.add_column(justify="left")
    ticker_table.add_column(justify="left")
    ticker_table.add_column(justify="left")
    if ticker:
        ticker_table.add_row(
            f"[bold]XAU[/] {ticker.gold_spot or 'n/a'}",
            f"[bold]DXY[/] {ticker.dxy or 'n/a'} ({ticker.dxy_change_pct or 0:+.2f}%)",
            f"[bold]10Y[/] {ticker.treasury_10y or 'n/a'} ({ticker.treasury_10y_bps_1d or 0:+.1f}bps)",
            f"[bold]Real[/] {ticker.real_yield or 'n/a'}  [bold]BE[/] {ticker.breakeven or 'n/a'}",
        )

    regime = snapshot.regime
    regime_text = Text()
    if regime:
        regime_text.append(f"{regime.primary_regime} ", style="bold yellow")
        regime_text.append(f"| gold {regime.gold_bias} | clarity {regime.clarity}/10\n")
        regime_text.append(regime.narrative, style="dim")
        if regime.confluence_label:
            regime_text.append(
                f"\nConfluence: {regime.confluence_label} ({regime.confluence_score:+.1f})"
                if regime.confluence_score is not None
                else f"\nConfluence: {regime.confluence_label}",
                style="cyan",
            )

    queue_table = Table(box=box.SIMPLE_HEAVY, expand=True, show_lines=False)
    queue_table.add_column("#", style="dim", width=3)
    queue_table.add_column("Pri", width=4)
    queue_table.add_column("Scr", justify="right", width=5)
    queue_table.add_column("Bias", width=8)
    queue_table.add_column("Cat", width=10)
    queue_table.add_column("Headline", ratio=1)

    band_style = {"P1": "bold red", "P2": "bold yellow", "P3": "cyan", "expired": "dim"}
    bias_style = {
        "BULLISH": "green",
        "BEARISH": "red",
        "TWO_WAY": "yellow",
        "NEUTRAL": "white",
        "UNKNOWN": "dim",
    }

    for idx, item in enumerate(snapshot.top_items, start=1):
        queue_table.add_row(
            str(idx),
            Text(item.get("priority_band", "P3"), style=band_style.get(item.get("priority_band"), "white")),
            f"{item.get('priority_score', 0):.1f}",
            Text(item.get("gold_bias", "?")[:8], style=bias_style.get(item.get("gold_bias"), "white")),
            item.get("category", ""),
            item.get("title", "")[:90],
        )

    panels = [
        Layout(Panel(header, border_style="yellow"), size=3),
        Layout(Panel(ticker_table, title="Market Ticker", border_style="blue"), size=5),
        Layout(Panel(regime_text, title="Macro Regime", border_style="green"), size=7),
        Layout(Panel(queue_table, title="Priority Store (P1 → P2 → P3)", border_style="magenta"), ratio=3),
    ]
    if snapshot.errors:
        warn = Text("\n".join(snapshot.errors), style="bold red")
        panels.append(
            Layout(Panel(warn, title="Warnings", border_style="red"), size=min(3 + len(snapshot.errors), 8))
        )

    layout = Layout()
    layout.split_column(*panels)
    return Group(layout)


def print_snapshot(snapshot: TerminalSnapshot, *, use_rich: bool = True) -> None:
    if use_rich and RICH_AVAILABLE:
        console = Console()
        console.print(render_rich(snapshot))
        return
    print(render_plain(snapshot))
