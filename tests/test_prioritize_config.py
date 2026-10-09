# tests/test_prioritize_config.py
from pathlib import Path

import pytest

from services import prioritize_config as pc


def test_repo_config_loads_with_spec_defaults():
    cfg = pc.load()
    assert cfg.points_per_day == 5
    assert cfg.default_points == 3
    assert cfg.weights == {
        "priority": 0.20,
        "urgency": 0.24,
        "impact": 0.12,
        "unblock": 0.08,
        "aging": 0.04,
        "category": 0.12,
        "necessity": 0.20,
    }
    assert sum(cfg.weights.values()) == pytest.approx(1.0)
    assert cfg.priority_weight["P0"] == 1.0 and cfg.priority_weight["P3"] == 0.1
    assert cfg.default_priority == "P2"
    assert cfg.horizon_days == {"P0": 3, "P1": 14, "P2": 45, "P3": 120}
    assert (cfg.urgency_k, cfg.urgency_s0, cfg.no_due_urgency) == (1.0, 3.0, 0.1)
    assert (cfg.soft_cap_inferred, cfg.soft_cap_horizon) == (0.6, 0.4)
    assert cfg.impact_weight == {"low": 0.2, "medium": 0.5, "high": 1.0}
    assert cfg.stale_days == 30 and cfg.unblock_per_task == 0.3
    assert cfg.default_category_weight == 0.5
    assert cfg.category_weight == {"Consulting": 1.0, "Ben's Board": 0.7}
    assert (cfg.default_n, cfg.diversity_penalty, cfg.energy_penalty) == (5, 0.95, 0.7)
    assert cfg.excluded_projects == ("Inbox",)
    assert cfg.hard_due_window_days == 1
    assert (cfg.starvation_boost_per_day, cfg.starvation_max_boost) == (0.1, 0.5)
    assert (cfg.stale_after_days, cfg.deferred_limit) == (45, 5)
    assert cfg.low_confidence_multiplier == 1.5 and cfg.min_effort_days == 0.25


def test_category_weights_read_project_names(tmp_path: Path):
    p = tmp_path / "p.toml"
    # `default = 0.5` occurs once in the file, inside [category]; add a project after it.
    p.write_text(
        pc.DEFAULT_PATH.read_text().replace(
            "default = 0.5\n", 'default = 0.5\n"Family Board" = 0.9\n', 1
        )
    )
    cfg = pc.load(str(p))
    assert cfg.category_weight["Family Board"] == 0.9
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


def test_fingerprint_tracks_the_file_bytes(tmp_path):
    src = Path(pc.DEFAULT_PATH).read_bytes()
    a = tmp_path / "a.toml"
    a.write_bytes(src)
    b = tmp_path / "b.toml"
    b.write_bytes(src + b"\n# retuned\n")
    fa, fb = pc.load(str(a)).fingerprint, pc.load(str(b)).fingerprint
    assert len(fa) == 12 and fa == pc.load(str(a)).fingerprint
    assert fa != fb


def test_weights_still_sum_to_one_with_necessity():
    cfg = pc.load()
    assert abs(sum(cfg.weights.values()) - 1.0) < 1e-9
    assert cfg.weights["necessity"] == 0.20


def test_necessity_and_strategy_sections():
    cfg = pc.load()
    assert cfg.necessity_mode == "flag"
    assert cfg.necessity_role == {"path": 1.0, "derisk": 0.9, "support": 0.5}
    assert cfg.necessity_unattached == 0.2
    assert cfg.below_the_line_boost == 1.15
    assert cfg.strategy_stale_after_days == 90
    assert cfg.lag_flat_periods == 2
    assert cfg.suppression_settle_days == 30
    assert cfg.signal_debounce_days == 3
    assert cfg.min_tagged_for_signals == 5


def test_bad_mode_is_rejected(tmp_path):
    text = pc.DEFAULT_PATH.read_text().replace('mode = "flag"', 'mode = "yolo"')
    p = tmp_path / "p.toml"
    p.write_text(text)
    with pytest.raises(ValueError, match="necessity.mode"):
        pc.load(str(p))
