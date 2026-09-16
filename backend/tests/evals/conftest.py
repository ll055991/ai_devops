"""evals conftest：session 结束把基线报告写到 .scratch/m1-evals/baseline.md。"""

from __future__ import annotations

from datetime import datetime
from pathlib import Path


def pytest_sessionfinish(session, exitstatus):
    from .harness import REPORT

    if not REPORT:
        return
    out = Path(__file__).parents[3] / ".scratch" / "m1-evals" / "baseline.md"
    out.parent.mkdir(parents=True, exist_ok=True)

    total = sum(len(r[2]) for r in REPORT)
    failed = sum(1 for _, _, rs in REPORT for _, p, _ in rs if not p)
    lines = [
        "# Evals 基线报告（M1）",
        "",
        f"- 时间：{datetime.now().isoformat(timespec='seconds')}",
        f"- 模型：{REPORT[0][1]}",
        f"- 场景：{len(REPORT)}，断言：{total}，失败：{failed}",
        "",
        "| 场景 | 指标 | 通过 | 说明 |",
        "|---|---|---|---|",
    ]
    for scenario_id, _, results in REPORT:
        for name, passed, detail in results:
            lines.append(f"| {scenario_id} | {name} | {'✅' if passed else '❌'} | {detail} |")
    out.write_text("\n".join(lines) + "\n", encoding="utf-8")
