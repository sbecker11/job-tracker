import importlib
import pkgutil

import pytest

import job_tracker.cli

CLI_MODULES = sorted(m.name for m in pkgutil.iter_modules(job_tracker.cli.__path__))


@pytest.mark.parametrize("name", CLI_MODULES)
def test_every_cli_module_imports(name):
    importlib.import_module(f"job_tracker.cli.{name}")
