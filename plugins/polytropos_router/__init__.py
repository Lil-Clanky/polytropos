"""Hermes model-provider plugin: registers the router as the provider ``polytropos-router``."""

import logging
from pathlib import Path

from ._shared import paths
from .client import RouterClient
from .config import load_config, reported_context

logger = logging.getLogger("hermes_plugins.polytropos_router")
REGISTERED: list = []  # profiles registered while Hermes is absent (tests)

try:
    from providers import register_provider
    from providers.base import ProviderProfile
except ImportError:
    class ProviderProfile:  # stand-in so the package imports without Hermes
        def __init__(self, **fields):
            self.__dict__.update(fields)

    register_provider = REGISTERED.append


def _home() -> Path:
    try:
        from hermes_constants import get_hermes_home  # lazy: follows the active profile
    except ImportError:
        return paths.hermes_home()
    return get_hermes_home()


def _config():
    cfg, problems = load_config(_home())
    for problem in problems:
        logger.warning("router config: %s", problem)
    return cfg


class PolytroposRouterProfile(ProviderProfile):
    def create_client(self, **client_kwargs) -> RouterClient:
        extra = {}
        try:
            from agent.secret_scope import get_secret  # the active profile's secret, as Hermes resolves it
            extra["env_lookup"] = get_secret
        except ImportError:
            pass
        return RouterClient(_config(), api_key=client_kwargs.get("api_key"),
                            trace_path=paths.state_dir() / "router-traces.jsonl", **extra)

    def get_model_context_length(self, model: str) -> int:
        return reported_context(_config())

    def fetch_models(self, *, api_key=None, base_url=None, timeout=8.0) -> list[str]:
        return ["auto"]


register_provider(PolytroposRouterProfile(
    name="polytropos-router", display_name="Polytropos router",
    description="Routes each turn to a local or free cloud model", base_url="polytropos://router",
    env_vars=("FREELLMAPI_API_KEY",), fallback_models=("auto",), supports_streaming=True,
    supports_health_check=False, supports_model_listing=False))
