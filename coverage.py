"""
ARIA Prompt Coverage — CLI runner.

Usage:
    python coverage.py                    # all domains
    python coverage.py customer_segments  # single domain
    python coverage.py --threshold 0.60   # stricter coverage bar
"""

from __future__ import annotations

import sys
from pathlib import Path

from rich.console import Console
from rich.table import Table
from rich.panel import Panel
from rich.rule import Rule
from rich.text import Text

from modules.dksm.prompt_coverage import PromptCoverageAnalyzer, DomainCoverageReport

console = Console()


def _coverage_color(pct: float) -> str:
    if pct >= 80:
        return "bold green"
    if pct >= 50:
        return "bold yellow"
    return "bold red"


def print_domain_report(r: DomainCoverageReport) -> None:
    color = _coverage_color(r.coverage_pct)

    # ── summary panel ─────────────────────────────────────────────────────────
    summary = (
        f"[bold]Domain:[/bold]        {r.display_name}\n"
        f"[bold]Probe questions:[/bold]  {r.total_probes}\n"
        f"[bold]Edge cases:[/bold]       {r.total_edge_cases}\n\n"
        f"[bold]Coverage:[/bold]  [{color}]{r.coverage_pct:.1f}%[/{color}]"
        f"  ({r.covered_count} covered / {r.uncovered_count} gaps)\n\n"
        f"[bold green]Fully covered entities:[/bold green]  "
        f"{', '.join(r.entities_fully_covered) or '[dim]none[/dim]'}\n"
        f"[bold red]Entities with gaps:[/bold red]      "
        f"{', '.join(r.entities_with_gaps) or '[dim]none[/dim]'}"
    )
    console.print(Panel(summary, title=f"[bold]{r.display_name}[/bold] — Prompt Coverage"))

    if not r.edge_cases:
        console.print("[dim]  No Gold-layer entities found.[/dim]\n")
        return

    # ── edge case table ───────────────────────────────────────────────────────
    table = Table(show_lines=True, title="Edge Case Coverage Detail")
    table.add_column("Entity", width=22)
    table.add_column("Case Type", width=14)
    table.add_column("Score", width=7, justify="right")
    table.add_column("Covered", width=9, justify="center")
    table.add_column("Best Matching Probe (truncated)", width=55)

    for c in r.edge_cases:
        cov_text = "[green]✓[/green]" if c.covered else "[red]✗[/red]"
        score_color = "green" if c.covered else ("yellow" if c.best_score >= 0.40 else "red")
        probe_text = (c.best_probe[:80] + "…") if c.best_probe and len(c.best_probe) > 80 else (c.best_probe or "—")
        table.add_row(
            c.entity,
            c.case_type,
            f"[{score_color}]{c.best_score:.2f}[/{score_color}]",
            cov_text,
            f"[dim]{probe_text}[/dim]",
        )

    console.print(table)

    # ── suggested probes ──────────────────────────────────────────────────────
    if r.suggested_probes:
        console.print(Rule(f"[bold red]{r.uncovered_count} Suggested Probes to Add[/bold red]"))
        for i, probe in enumerate(r.suggested_probes, 1):
            console.print(f"  [dim]{i:2d}.[/dim] {probe}")
    console.print()


def print_summary_table(reports: list[DomainCoverageReport]) -> None:
    table = Table(title="ARIA Prompt Coverage — All Domains", show_lines=True)
    table.add_column("Domain", width=22)
    table.add_column("Probes", width=7, justify="right")
    table.add_column("Edge Cases", width=11, justify="right")
    table.add_column("Covered", width=8, justify="right")
    table.add_column("Gaps", width=6, justify="right")
    table.add_column("Coverage %", width=12, justify="right")
    table.add_column("Entities w/ Gaps", width=35)

    for r in reports:
        color = _coverage_color(r.coverage_pct)
        table.add_row(
            r.display_name,
            str(r.total_probes),
            str(r.total_edge_cases),
            str(r.covered_count),
            str(r.uncovered_count),
            f"[{color}]{r.coverage_pct:.1f}%[/{color}]",
            ", ".join(r.entities_with_gaps[:4]) + ("…" if len(r.entities_with_gaps) > 4 else ""),
        )

    console.print(table)

    total_cases = sum(r.total_edge_cases for r in reports)
    total_covered = sum(r.covered_count for r in reports)
    overall = round(total_covered / total_cases * 100, 1) if total_cases else 0.0
    color = _coverage_color(overall)
    console.print(
        f"\n[bold]Overall coverage:[/bold] [{color}]{overall:.1f}%[/{color}]"
        f"  ({total_covered}/{total_cases} edge cases)\n"
    )


def main():
    args = sys.argv[1:]
    threshold = 0.55
    domain_filter = None

    for arg in args:
        if arg.startswith("--threshold="):
            threshold = float(arg.split("=")[1])
        elif not arg.startswith("--"):
            domain_filter = arg

    import os
    os.chdir(Path(__file__).parent)

    analyzer = PromptCoverageAnalyzer(threshold=threshold)

    if domain_filter:
        console.print(f"\n[dim]Analyzing domain: {domain_filter}[/dim]\n")
        report = analyzer.analyze_domain(domain_filter)
        print_domain_report(report)
    else:
        console.print("\n[dim]Analyzing all domains...[/dim]\n")
        reports = analyzer.analyze_all()
        print_summary_table(reports)
        console.print(Rule("Per-Domain Detail"))
        for r in reports:
            print_domain_report(r)


if __name__ == "__main__":
    main()
