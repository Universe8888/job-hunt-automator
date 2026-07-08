from __future__ import annotations

import importlib.util
import os
import sys
from pathlib import Path

import pytest


ROOT = Path(__file__).resolve().parents[1]
CONFIG = ROOT / "config.py"
TEMPLATE_CONFIG = ROOT / "config.example.py"


def _load_template_config_when_local_config_is_absent() -> None:
    if CONFIG.exists():
        return

    spec = importlib.util.spec_from_file_location("config", TEMPLATE_CONFIG)
    if spec is None or spec.loader is None:
        raise RuntimeError(f"Could not load template config from {TEMPLATE_CONFIG}")

    module = importlib.util.module_from_spec(spec)
    sys.modules["config"] = module
    spec.loader.exec_module(module)


def main() -> int:
    os.chdir(ROOT)
    if str(ROOT) not in sys.path:
        sys.path.insert(0, str(ROOT))
    _load_template_config_when_local_config_is_absent()
    return pytest.main([str(ROOT / "tests"), "-q"])


if __name__ == "__main__":
    raise SystemExit(main())
