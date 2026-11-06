"""冒烟测试：确认集成结构符合 HA 要求。."""

import json
from pathlib import Path

COMPONENT_DIR = (
    Path(__file__).parent.parent / "custom_components" / "himedia_player"
)
MANIFEST_PATH = COMPONENT_DIR / "manifest.json"


def test_manifest_matches_folder() -> None:
    """Manifest 的 domain 必须与目录名一致，且带有版本号。."""
    manifest = json.loads(MANIFEST_PATH.read_text(encoding="utf-8"))

    assert manifest["domain"] == COMPONENT_DIR.name
    assert manifest["version"]
    assert manifest["config_flow"] is True
    assert "pyserial>=3.5" in manifest["requirements"]
