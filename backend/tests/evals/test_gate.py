"""M1 PR 门禁：10 条 golden 全量 + 5 指标断言，真调小模型。

红线（审批召回、SOP 顺序）失败即拦合并。跑完经 conftest 落基线报告。
"""

from __future__ import annotations

import pytest

from .assertions import evaluate
from .harness import REPORT, run_scenario
from .scenarios import GATE_SCENARIOS


@pytest.mark.gate
@pytest.mark.parametrize("scenario", GATE_SCENARIOS, ids=[s.id for s in GATE_SCENARIOS])
async def test_gate_scenario(scenario, monkeypatch, tmp_path):
    traj, settings = await run_scenario(scenario, monkeypatch, tmp_path)
    results = evaluate(
        traj, settings, scenario.expect, scenario.thread_id, scenario.required_tools
    )
    REPORT.append((scenario.id, settings.openai_model, results))

    failures = [f"{name}: {detail}" for name, passed, detail in results if not passed]
    assert not failures, f"场景 {scenario.id} 断言失败:\n" + "\n".join(failures)
