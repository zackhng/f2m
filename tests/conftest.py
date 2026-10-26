from __future__ import annotations

import sys
from pathlib import Path

import pytest

sys.path.insert(0, str(Path(__file__).parent))

from make_fixtures import build_all  # noqa: E402

from f2m import Options, Pipeline  # noqa: E402


@pytest.fixture(scope="session")
def files(tmp_path_factory) -> dict[str, Path]:
    return build_all(tmp_path_factory.mktemp("fixtures"))


@pytest.fixture
def md(files, tmp_path):
    """Convert a fixture by key and return its Markdown (images written to tmp_path)."""

    def run(key: str, **opts) -> str:
        res = Pipeline(options=Options(**opts)).convert_file(files[key], tmp_path)
        assert res.ok, res.error
        return res.markdown

    return run
