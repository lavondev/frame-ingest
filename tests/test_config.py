from pathlib import Path

import pytest

from frame_ingest.config import ConfigError, load_config, load_pricing, resolve_settings
from frame_ingest.models import JobSettings, ModelOverrides


def cfg(tmp_path: Path, env: dict[str, str] | None = None, yaml_text: str | None = None):
    root = tmp_path / "r"
    root.mkdir(exist_ok=True)
    if yaml_text is not None:
        (root / "config.yaml").write_text(yaml_text)
    return load_config(root, env=env or {})


def test_defaults_work_out_of_the_box(tmp_path: Path) -> None:
    c = cfg(tmp_path)
    assert c.models.vision and c.models.transcribe and c.models.synthesize
    assert c.frame_cap == 100 and c.home == tmp_path / "r"


def test_precedence_default_yaml_env_job(tmp_path: Path) -> None:
    yaml_text = "frame_cap: 60\nmodels:\n  vision: yaml-vision\n  correct: yaml-correct\n"
    c = cfg(tmp_path, yaml_text=yaml_text)
    assert c.frame_cap == 60 and c.models.vision == "yaml-vision"  # yaml > default
    c = cfg(
        tmp_path,
        env={"FRAME_INGEST_FRAME_CAP": "40", "FRAME_INGEST_MODEL_VISION": "env-vision"},
        yaml_text=yaml_text,
    )
    assert c.frame_cap == 40 and c.models.vision == "env-vision"  # env > yaml
    assert c.models.correct == "yaml-correct"  # untouched keys keep lower layers
    job = JobSettings(frame_cap=10, models=ModelOverrides(vision="job-vision"))
    r = resolve_settings(c, job)
    assert r.frame_cap == 10 and r.vision_model == "job-vision"  # job > env
    assert r.correct_model == "yaml-correct"
    assert resolve_settings(c).frame_cap == 40


def test_base_url_specific_beats_general(tmp_path: Path) -> None:
    c = cfg(
        tmp_path,
        env={
            "OPENAI_BASE_URL": "http://general/v1",
            "FRAME_INGEST_VISION_BASE_URL": "http://vision/v1",
        },
    )
    assert c.base_urls.vision == "http://vision/v1"
    assert c.base_urls.text == "http://general/v1" and c.base_urls.transcribe == "http://general/v1"


def test_env_prefixes_other_than_frame_ingest_are_ignored(tmp_path: Path) -> None:
    c = cfg(tmp_path, env={"OTHER_FRAME_CAP": "41", "LEGACY_FRAME_CAP": "42"})
    assert c.frame_cap == 100


def test_home_comes_from_env_when_not_given(tmp_path: Path) -> None:
    home = tmp_path / "custom-home"
    c = load_config(env={"FRAME_INGEST_HOME": str(home)})
    assert c.home == home and c.jobs_dir == home / "jobs"


def test_dotenv_files_are_never_read(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    root = tmp_path / "r"
    root.mkdir()
    dotenv = "OPENAI_API_KEY=sk-from-dotenv-123456\nFRAME_INGEST_FRAME_CAP=33\n"
    (root / ".env").write_text(dotenv)
    (tmp_path / ".env").write_text(dotenv)
    monkeypatch.chdir(tmp_path)
    c = load_config(root, env={})
    assert c.key_for("vision") is None
    assert c.frame_cap == 100


def test_role_specific_key_beats_general(tmp_path: Path) -> None:
    c = cfg(
        tmp_path,
        env={"OPENAI_API_KEY": "sk-general-1234567", "FRAME_INGEST_VISION_API_KEY": "sk-vis-12345"},
    )
    assert c.key_for("vision") == "sk-vis-12345" and c.key_for("text") == "sk-general-1234567"


def test_secret_never_serialised(tmp_path: Path) -> None:
    c = cfg(tmp_path, env={"OPENAI_API_KEY": "sk-super-secret-value-1"})
    assert c.key_for("text") == "sk-super-secret-value-1"
    assert "sk-super" not in c.model_dump_json()
    assert "sk-super" not in repr(c)
    assert "sk-super" not in str(c.model_dump())


def test_invalid_config_has_clear_message(tmp_path: Path) -> None:
    with pytest.raises(ConfigError, match="frame_cap"):
        cfg(tmp_path, yaml_text="frame_cap: 99999\n")
    with pytest.raises(ConfigError, match="not valid YAML"):
        cfg(tmp_path, yaml_text="models: [unclosed\n")
    with pytest.raises(ConfigError):
        cfg(tmp_path, yaml_text="frame_capp: 3\n")  # typo'd key is rejected, not ignored


def test_pricing_is_unconfigured_until_a_file_is_added(tmp_path: Path) -> None:
    assert load_pricing(tmp_path).configured is False
    example = Path(__file__).resolve().parents[1] / "examples" / "pricing.example.yaml"
    assert load_pricing(example.parent).configured is False  # no pricing.yaml there
    (tmp_path / "pricing.yaml").write_text(example.read_text())
    assert load_pricing(tmp_path).configured is False  # placeholders only
    (tmp_path / "pricing.yaml").write_text(
        "text_models:\n  m:\n    input_per_mtok: 1\n    output_per_mtok: 2\n"
    )
    assert load_pricing(tmp_path).configured is True
