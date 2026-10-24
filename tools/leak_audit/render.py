"""Renders the privacy report's raw logs to an SVG for the README, so the image is the data.

    python -m tools.leak_audit.render     # writes assets/screenshots/privacy-report.svg
"""

from __future__ import annotations

import json
from collections import Counter

from rich.console import Console
from rich.table import Table
from rich.text import Text

from tools.devnet import REPO
from tools.leak_audit.report import OUT_DIR, SCENARIOS

WALLETS = ["ZecPy", "zcash-devtool", "zingo-cli"]
STYLE = {"public": "green", "coarse": "cyan", "broadcast": "yellow", "identifying": "bold red"}


def main() -> None:
    logs = {w: [json.loads(line) for line in (OUT_DIR / f"{w}.jsonl").read_text().splitlines()] for w in WALLETS}

    table = Table(title="Identifying requests / all requests seen by the light server",
                  title_style="bold", show_lines=False, pad_edge=False)
    table.add_column("Scenario")
    for w in WALLETS:
        table.add_column(w, justify="center")
    for key, title in SCENARIOS:
        row = []
        for w in WALLETS:
            records = [r for r in logs[w] if r["scenario"] == key]
            bad = sum(r["label"] == "identifying" for r in records)
            row.append(Text(f"{bad} / {len(records)}", style="bold red" if bad else "green"))
        table.add_row(title, *row)
    totals = [sum(r["label"] == "identifying" for r in logs[w]) for w in WALLETS]
    table.add_row(Text("Total identifying", style="bold"),
                  *[Text(str(t), style="bold red" if t else "bold green") for t in totals])

    console = Console(record=True, width=96)
    console.print(table)
    console.print()
    console.print(Text("ZecPy, receive-a-payment scenario, as recorded by tools/leak_audit:", style="bold"))
    for r in [r for r in logs["ZecPy"] if r["scenario"] == "receive"]:
        line = Text("  ")
        line.append(f"{r['label']:<11}", style=STYLE[r["label"]])
        line.append(f" {r['method']:<20} {r['reason'][:58]}")
        console.print(line)
    counts = Counter(r["method"] for r in logs["zingo-cli"] if r["label"] == "identifying")
    console.print()
    console.print(Text("Same scenarios, zingo-cli, identifying requests by method: ", style="bold")
                  + Text(", ".join(f"{m} x{n}" for m, n in counts.most_common()), style="bold red"))

    out = REPO / "assets" / "screenshots" / "privacy-report.svg"
    out.parent.mkdir(parents=True, exist_ok=True)
    console.save_svg(str(out), title="python -m tools.leak_audit.report")
    print(f"wrote {out}")


if __name__ == "__main__":
    main()
