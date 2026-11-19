from dataclasses import dataclass, field
from pathlib import Path

try:
    from hermes_yaml import safe_load
except ImportError:  # outside Hermes
    from yaml import safe_load

FLOOR = 64_000
SECTION = "polytropos-router"


@dataclass(frozen=True)
class Upstream:
    name: str
    base_url: str
    api_key_env: str = ""
    local: bool = False
    runtime: str = ""
    context_window: int = 0


@dataclass(frozen=True)
class Slot:
    upstream: str
    model: str


@dataclass(frozen=True)
class RouterConfig:
    upstreams: dict[str, Upstream]
    routes: dict[str, tuple[Slot, ...]]
    classifier: Slot | None
    classifier_max_tokens: int = 16
    classifier_timeout_s: float = 8.0
    default_route: str = "complex"
    channel_hints: dict[str, str] = field(default_factory=dict)
    save_traces: bool = False
    cloud_window_default: int = 128_000


_AUTO = Slot("freellmapi", "auto")
DEFAULT_CONFIG = RouterConfig(
    upstreams={"freellmapi": Upstream("freellmapi", "http://127.0.0.1:3001/v1", api_key_env="FREELLMAPI_API_KEY")},
    routes={"simple": (_AUTO,), "complex": (_AUTO,)},
    classifier=_AUTO,
)


class ConfigError(ValueError):
    pass


def parse_slot(text: str) -> Slot:
    upstream, sep, model = str(text).partition("/")
    if not (sep and upstream and model):
        raise ValueError(f"expected 'upstream/model', got {text!r}")
    return Slot(upstream, model)


def _slot(text, path: str, upstreams: dict) -> Slot:
    try:
        slot = parse_slot(text)
    except ValueError as e:
        raise ConfigError(f"{path}: {e}") from None
    if slot.upstream not in upstreams:
        raise ConfigError(f"{path}: unknown upstream {slot.upstream!r}")
    return slot


def _mapping(value, path: str) -> dict:
    if not isinstance(value, dict):
        raise ConfigError(f"{path}: expected a mapping")
    return value


def _typed(value, kind: type, path: str):
    ok = isinstance(value, (int, float)) if kind is float else isinstance(value, kind)
    if not ok or isinstance(value, bool) and kind is not bool:
        raise ConfigError(f"{path}: expected {kind.__name__}")
    return value


def parse_config(raw: dict) -> RouterConfig:
    raw = _mapping(raw, "config")
    upstreams = {}
    for name, body in _mapping(raw.get("upstreams"), "upstreams").items():
        path = f"upstreams.{name}"
        body = _mapping(body, path)
        if not body.get("base_url"):
            raise ConfigError(f"{path}.base_url: required")
        window = _typed(body.get("context_window", 0), int, f"{path}.context_window")
        if window < 0:
            raise ConfigError(f"{path}.context_window: must not be negative")
        upstreams[name] = Upstream(
            name, str(body["base_url"]), str(body.get("api_key_env", "")),
            _typed(body.get("local", False), bool, f"{path}.local"),
            str(body.get("runtime", "")), window,
        )
    routes = {}
    for tier, slots in _mapping(raw.get("routes"), "routes").items():
        if not isinstance(slots, list) or not slots:
            raise ConfigError(f"routes.{tier}: expected a non-empty list")
        routes[tier] = tuple(_slot(s, f"routes.{tier}[{i}]", upstreams) for i, s in enumerate(slots))
    default_route = raw.get("default_route", "complex")
    if default_route not in routes:
        raise ConfigError(f"default_route: no route named {default_route!r}")
    hints = _mapping(raw.get("channel_hints", {}), "channel_hints")
    for platform, tier in hints.items():
        if tier not in routes:
            raise ConfigError(f"channel_hints.{platform}: no route named {tier!r}")
    classifier = raw.get("classifier")
    return RouterConfig(
        upstreams=upstreams,
        routes=routes,
        classifier=_slot(classifier, "classifier", upstreams) if classifier else None,
        classifier_max_tokens=_typed(raw.get("classifier_max_tokens", 16), int, "classifier_max_tokens"),
        classifier_timeout_s=float(_typed(raw.get("classifier_timeout_s", 8.0), float, "classifier_timeout_s")),
        default_route=default_route,
        channel_hints=dict(hints),
        save_traces=_typed(raw.get("save_traces", False), bool, "save_traces"),
        cloud_window_default=_typed(raw.get("cloud_window_default", 128_000), int, "cloud_window_default"),
    )


def load_config(hermes_home: Path) -> tuple[RouterConfig, list[str]]:
    path = Path(hermes_home) / "config.yaml"
    try:
        data = safe_load(path.read_text()) or {}
        section = data["plugins"]["entries"][SECTION]
    except (OSError, KeyError, TypeError):
        return DEFAULT_CONFIG, []
    except Exception as e:  # unparsable YAML must not stop Hermes
        return DEFAULT_CONFIG, [f"config.yaml: {e}"]
    try:
        return parse_config(section), []
    except ConfigError as e:
        return DEFAULT_CONFIG, [str(e)]


def window(cfg: RouterConfig, slot: Slot) -> int:
    up = cfg.upstreams[slot.upstream]
    return up.context_window or (0 if up.local else cfg.cloud_window_default)


def _cloud_windows(cfg: RouterConfig) -> list[tuple[Slot, int]]:
    return [(s, window(cfg, s)) for slots in cfg.routes.values() for s in slots
            if not cfg.upstreams[s.upstream].local]


def reported_context(cfg: RouterConfig) -> int:
    return max(FLOOR, min((w for _, w in _cloud_windows(cfg)), default=FLOOR))


def cloud_slots_below_floor(cfg: RouterConfig) -> list[Slot]:
    return list(dict.fromkeys(s for s, w in _cloud_windows(cfg) if w < FLOOR))
