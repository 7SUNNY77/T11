import json
import sys
from pathlib import Path

from mas_drift.agents.policy_fake import PolicyFake
from mas_drift.infra.config import DEFAULT_CONFIG, ProviderConfig
from mas_drift.infra.llm import build_llm
from mas_drift.runner import cli

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "scripts"))


def test_finalize_offline_on_fake(tmp_path):
    import finalize

    cfg = DEFAULT_CONFIG.read_text(encoding="utf-8").replace(
        'runs_dir = "runs"', f'runs_dir = "{(tmp_path / "runs").as_posix()}"')
    path = tmp_path / "c.toml"
    path.write_text(cfg, encoding="utf-8")
    llm = build_llm(ProviderConfig(name="fake", kind="fake", model="policy-fake", cache=False), None, raw=PolicyFake())
    cli.main(["--config", str(path), "--provider", "fake", "--n-tasks", "3", "--max-runs", "25"],
             llm=llm, log=lambda s: None)
    out = tmp_path / "rep"
    code = finalize.main(["--config", str(path), "--experiment", "fake", "--provider", "fake", "--offline",
                          "--out", str(out)])
    assert code == 0
    md = (out / "summary.md").read_text(encoding="utf-8")
    assert "**Объём: 2 полных блоков**" in md and "Сетка не завершена" in md
    assert "`reports/results_used.csv`" in md and "Вердиктов нет." in md
    used = (out / "results_used.csv").read_text(encoding="utf-8").splitlines()
    assert len(used) == 1 + 20  # 2 полных блока × 10 условий
    m = json.loads((out / "manifest.json").read_text(encoding="utf-8"))
    assert m["agents"]["model"] == "policy-fake" and m["prompts"] == {"agents": "p1", "judge": "j1"}
    assert m["data"]["world_json"]["sha256"] and "\\" not in m["data"]["world_json"]["path"]
    assert m["git"]["commit"] and m["analysis"]["complete_blocks"] == 2
    assert "api_key" not in json.dumps(m)
    assert (out / "success_by_condition.png").exists()
