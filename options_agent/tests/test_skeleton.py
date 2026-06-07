import importlib

PACKAGES = [
    "options_agent",
    "options_agent.contracts",
    "options_agent.data",
    "options_agent.data.providers",
    "options_agent.context",
    "options_agent.agent",
    "options_agent.risk",
    "options_agent.execution",
    "options_agent.monitor",
    "options_agent.state",
    "options_agent.obs",
]


def test_all_packages_importable() -> None:
    for package in PACKAGES:
        assert importlib.import_module(package) is not None
