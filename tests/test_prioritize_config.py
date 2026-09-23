# tests/test_prioritize_config.py
from pathlib import Path

from services import prioritize_config as pc


def test_repo_config_loads_with_spec_defaults():
    cfg = pc.load()
    assert cfg.points_per_day == 5
    assert cfg.default_points == 3
    assert cfg.weights == {
        "priority": 0.30,
        "urgency": 0.30,
        "impact": 0.15,
        "unblock": 0.10,
        "aging": 0.10,
        "category": 0.05,
    }
    assert cfg.priority_weight["P0"] == 1.0 and cfg.priority_weight["P3"] == 0.1
    assert cfg.default_priority == "P2"
    assert cfg.horizon_days == {"P0": 3, "P1": 14, "P2": 45, "P3": 120}
    assert (cfg.urgency_k, cfg.urgency_s0, cfg.soft_cap, cfg.no_due_urgency) == (1.0, 3.0, 0.6, 0.1)
    assert cfg.impact_weight == {"low": 0.2, "medium": 0.5, "high": 1.0}
    assert cfg.stale_days == 30 and cfg.unblock_per_task == 0.3
    assert cfg.default_category_weight == 0.5 and cfg.category_weight == {}
    assert (cfg.default_n, cfg.diversity_penalty, cfg.energy_penalty) == (5, 0.8, 0.7)
    assert (cfg.stale_after_days, cfg.deferred_limit) == (45, 5)
    assert cfg.low_confidence_multiplier == 1.5 and cfg.min_effort_days == 0.25


def test_category_weights_read_project_names(tmp_path: Path):
    p = tmp_path / "p.toml"
    # `default = 0.5` occurs once in the file, inside [category]; add a project after it.
    p.write_text(
        pc.DEFAULT_PATH.read_text().replace(
            "default = 0.5\n", 'default = 0.5\n"Ben\'s Board" = 0.9\n', 1
        )
    )
    cfg = pc.load(str(p))
    assert cfg.category_weight == {"Ben's Board": 0.9}
    assert cfg.default_category_weight == 0.5


def test_env_var_overrides_path(tmp_path: Path, monkeypatch):
    p = tmp_path / "p.toml"
    p.write_text(pc.DEFAULT_PATH.read_text().replace("points_per_day = 5", "points_per_day = 8"))
    monkeypatch.setenv("PRIORITIZE_CONFIG_PATH", str(p))
    assert pc.load().points_per_day == 8


def test_missing_section_is_an_error(tmp_path: Path):
    p = tmp_path / "p.toml"
    p.write_text("[capacity]\npoints_per_day = 5\n")
    try:
        pc.load(str(p))
    except KeyError as e:
        assert "weights" in str(e)
    else:
        raise AssertionError("expected KeyError")
