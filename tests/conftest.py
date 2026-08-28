"""tests/conftest.py - hermetic test isolation for the whole suite.

SIMULATION.md Round 2, finding C: most of test_housekeeping_flow.py called
load_settings()/_settings() without isolating AGENT_REPO_ROOT (only 3 of the
file's tests did), so those tests read *this working copy's own*
config/hotel.yaml, config/agent.yaml and data/imports/*.csv instead of a
fixed fixture set. The moment a hotel follows the README's own guided setup
and fills those in, previously-green tests fail with counts that have
nothing to do with anything the hotel changed - and one test
(test_apply_triage_run_writes_every_ticket_and_never_touches_a_low_priority_clock)
left a real-looking "sent" message in the hotel's own data/exports/, purely
from running `make test`, because core/adapters/messaging_mock.py's outbox
path is `sub_data_dir("exports")` - repo_root()-relative, and nothing
pointed AGENT_REPO_ROOT anywhere else for that test.

core.config.repo_root() - and everything derived from it: data_dir(),
sub_data_dir() (data/agent.db, data/exports/*, data/imports/*, data/logs/*),
config_path() (config/hotel.yaml vs config/hotel.example.yaml),
core.templates.prompts_dir()/load_knowledge() (prompts/*.md, knowledge/*.md),
core.llm's mock-provider fixture lookup (fixtures/expected/<task>/*.json),
and this repo's own tools/store_ext.py CSV-import loaders - all honour the
AGENT_REPO_ROOT environment variable (see core/config.py:repo_root()).

The autouse fixture below points AGENT_REPO_ROOT, for every test in this
suite automatically, at a fresh tmp copy of just the parts a test needs
(prompts/, knowledge/, fixtures/, config/*.example.yaml) - never this
working copy's own config/hotel.yaml, config/agent.yaml, or data/. A test
that needs its own isolated repo root for a reason of its own (to hand-write
a CSV under data/imports/, for example) can still call
`monkeypatch.setenv("AGENT_REPO_ROOT", ...)` inside the test body - pytest
hands every fixture and the test function of one test call the same
`monkeypatch` instance, so a later call simply overrides this fixture's
setting, and both are undone together at teardown. See
test_vip_names_resolves_vip_via_the_csv_adapters_guests_file for exactly
that pattern.

No test may write outside its own tmp_path. This fixture is what makes that
true even for tests that never mention tmp_path themselves.
"""

from __future__ import annotations

import shutil
import sys
from pathlib import Path

import pytest

REPO_ROOT = Path(__file__).resolve().parent.parent
if str(REPO_ROOT) not in sys.path:
    sys.path.insert(0, str(REPO_ROOT))

#: copied wholesale into every test's clean repo root
_COPY_DIRS = ("prompts", "knowledge", "fixtures")
#: config/ is special-cased: only the shipped examples travel, never a
#: hotel's own filled-in config/hotel.yaml or config/agent.yaml (gitignored,
#: and exactly the files this fixture must keep out of the test suite).
_CONFIG_EXAMPLES_GLOB = "*.example.yaml"


@pytest.fixture(autouse=True)
def _hermetic_agent_repo_root(tmp_path, monkeypatch):
    """Give every test its own throwaway AGENT_REPO_ROOT. See module docstring."""
    root = tmp_path / "agent-repo-root"
    for name in _COPY_DIRS:
        src = REPO_ROOT / name
        if src.exists():
            shutil.copytree(src, root / name)
    config_dst = root / "config"
    config_dst.mkdir(parents=True, exist_ok=True)
    for f in (REPO_ROOT / "config").glob(_CONFIG_EXAMPLES_GLOB):
        shutil.copy2(f, config_dst / f.name)
    (root / "data").mkdir(parents=True, exist_ok=True)

    monkeypatch.setenv("AGENT_REPO_ROOT", str(root))
    # A stray AGENT_CONFIG_DIR in the shell environment would otherwise beat
    # the isolation above (core/config.py:config_path() checks it first).
    monkeypatch.delenv("AGENT_CONFIG_DIR", raising=False)
    return root
