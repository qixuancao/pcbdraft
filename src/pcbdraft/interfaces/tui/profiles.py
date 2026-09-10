"""Native profile data helpers for internal consumers.

Profiles already inside PCBDraft's runtime root remain readable. Historical
wrapper scripts, service/PID state and external application homes are not adopted.
The public CLI has no profile administration command; inherited destructive
administration and service hooks are retired.
"""

import re
from dataclasses import dataclass
from pathlib import Path

from pcbdraft.core.runtime_environment import (
    get_default_runtime_root,
    get_runtime_home,
)
from pcbdraft.interfaces.tui.update_cmd import unsupported_lifecycle

_PROFILE_ID_RE = re.compile(r"^[a-z0-9][a-z0-9_-]{0,63}$")
_BUNDLED_SKILLS_OPT_OUT = ".no-bundled-skills"


def _get_default_runtime_home() -> Path:
    return get_default_runtime_root()


def _get_profiles_root() -> Path:
    return _get_default_runtime_home() / "profiles"


def _get_active_profile_path() -> Path:
    return _get_default_runtime_home() / "active_profile"


def _get_wrapper_dir() -> Path:
    return _get_default_runtime_home() / "bin"


def normalize_profile_name(name: str) -> str:
    return name.strip().lower()


def validate_profile_name(name: str) -> None:
    """Validate a stored profile identifier without applying creation policy."""
    if not _PROFILE_ID_RE.fullmatch(name):
        raise ValueError("profile name must match [a-z0-9][a-z0-9_-]{0,63}")


def validate_new_profile_name(name: str) -> None:
    """Apply reserved-name policy only when validating a new profile or alias."""
    validate_profile_name(name)
    if name in {"pcbdraft", "bin", "profiles", "test", "python", "python3"}:
        raise ValueError(f"reserved profile name: {name}")


validate_alias_name = validate_new_profile_name


def get_profile_dir(name: str) -> Path:
    name = normalize_profile_name(name)
    validate_profile_name(name)
    if name == "default":
        return _get_default_runtime_home()
    root = _get_profiles_root()
    candidate = root / name
    # Profiles are data inside the product store, not links to another app.
    if root.is_symlink() or candidate.is_symlink():
        raise ValueError("profile directory must not be a symbolic link")
    candidate.resolve().relative_to(root.resolve())
    return candidate


def profile_exists(name: str) -> bool:
    try:
        return get_profile_dir(name).is_dir()
    except ValueError:
        return False


def resolve_profile_env(profile_name: str) -> str:
    path = get_profile_dir(profile_name)
    if not path.is_dir():
        raise FileNotFoundError(f"PCBDraft profile does not exist: {profile_name}")
    return str(path)


def get_active_profile() -> str:
    """Read existing sticky metadata, without applying it to public CLI argv."""
    try:
        name = _get_active_profile_path().read_text(encoding="utf-8").strip()
        validate_profile_name(name)
        return name if profile_exists(name) else "default"
    except (OSError, UnicodeError, ValueError):
        return "default"


def get_active_profile_name() -> str:
    current = get_runtime_home().resolve()
    if current == _get_default_runtime_home().resolve():
        return "default"
    try:
        relative = current.relative_to(_get_profiles_root().resolve())
        if len(relative.parts) == 1:
            validate_profile_name(relative.name)
            return relative.name
    except ValueError:
        pass
    return "custom"


def _read_yaml(path: Path) -> dict:
    if path.is_symlink():
        return {}
    try:
        import yaml

        raw = yaml.safe_load(path.read_text(encoding="utf-8"))
        return raw if isinstance(raw, dict) else {}
    except (OSError, UnicodeError, ValueError):
        return {}
    except yaml.YAMLError:
        return {}


def _read_config_model(profile_dir: Path) -> tuple[str | None, str | None]:
    """Read one profile's model/provider without loading active-profile defaults.

    Shared by profile summaries and the describer. Missing, malformed and
    symlinked config files remain untouched and yield unset values.
    """
    model = _read_yaml(profile_dir / "config.yaml").get("model")
    if isinstance(model, str):
        return model, None
    if not isinstance(model, dict):
        return None, None
    name = model.get("default") or model.get("model")
    provider = model.get("provider")
    return (
        name if isinstance(name, str) else None,
        provider if isinstance(provider, str) else None,
    )


def read_profile_meta(profile_dir: Path) -> dict:
    data = _read_yaml(profile_dir / "profile.yaml")
    return {
        "description": str(data.get("description") or "").strip(),
        "description_auto": bool(data.get("description_auto", False)),
    }


def write_profile_meta(
    profile_dir: Path,
    *,
    description: str | None = None,
    description_auto: bool | None = None,
) -> None:
    """Keep metadata editing for internal profile-description consumers."""
    if not profile_dir.is_dir():
        raise FileNotFoundError(f"profile directory does not exist: {profile_dir}")
    root = _get_default_runtime_home().resolve()
    relative = profile_dir.resolve().relative_to(root)
    if relative.parts and (len(relative.parts) != 2 or relative.parts[0] != "profiles"):
        raise ValueError("not a PCBDraft profile directory")
    path = profile_dir / "profile.yaml"
    if profile_dir.is_symlink() or path.is_symlink():
        raise ValueError("profile metadata must not be a symbolic link")
    data = _read_yaml(path)
    if description is not None:
        data["description"] = description.strip()
    if description_auto is not None:
        data["description_auto"] = bool(description_auto)
    from pcbdraft.core.runtime_utils import atomic_yaml_write

    atomic_yaml_write(path, data, sort_keys=False)


@dataclass
class ProfileInfo:
    name: str
    path: Path
    is_default: bool
    gateway_running: bool = False
    model: str | None = None
    provider: str | None = None
    has_env: bool = False
    skill_count: int = 0
    alias_path: Path | None = None
    alias_name: str | None = None
    distribution_name: str | None = None
    distribution_version: str | None = None
    distribution_source: str | None = None
    description: str = ""
    description_auto: bool = False


def list_profiles() -> list[ProfileInfo]:
    root = _get_default_runtime_home()
    candidates = [("default", root)] if root.is_dir() else []
    profiles_root = _get_profiles_root()
    if profiles_root.is_dir() and not profiles_root.is_symlink():
        for path in sorted(profiles_root.iterdir()):
            if path.name != "default" and profile_exists(path.name):
                candidates.append((path.name, path))
    result = []
    for name, path in candidates:
        model_name, provider = _read_config_model(path)
        meta = read_profile_meta(path)
        dist = _read_yaml(path / "distribution.yaml")
        result.append(
            ProfileInfo(
                name=name,
                path=path,
                is_default=name == "default",
                model=model_name,
                provider=provider,
                has_env=(path / ".env").is_file(),
                distribution_name=dist.get("name"),
                distribution_version=dist.get("version"),
                distribution_source=dist.get("source"),
                **meta,
            )
        )
    return result


def has_bundled_skills_opt_out(profile_dir: Path) -> bool:
    return (profile_dir / _BUNDLED_SKILLS_OPT_OUT).is_file()


def build_alias_map() -> dict[str, str]:
    return {}


def find_alias_for_profile(profile_name: str) -> None:
    return None


def check_alias_collision(name: str) -> str:
    return "Profile launcher aliases are unsupported; use `pcbdraft --help`."


def _check_gateway_running(profile_dir: Path) -> bool:
    return False


def _profile_bound_backend_pids(canon: str, profile_dir: Path) -> list[int]:
    return []


def backfill_profile_envs(quiet: bool = False) -> list[str]:
    return []


create_wrapper_script = unsupported_lifecycle
remove_wrapper_script = unsupported_lifecycle
create_profile = unsupported_lifecycle
delete_profile = unsupported_lifecycle
rename_profile = unsupported_lifecycle
set_active_profile = unsupported_lifecycle
export_profile = unsupported_lifecycle
import_profile = unsupported_lifecycle
seed_profile_skills = unsupported_lifecycle
profiles_to_serve = unsupported_lifecycle
_stop_profile_backends = unsupported_lifecycle
_stop_gateway_process = unsupported_lifecycle
_cleanup_gateway_service = unsupported_lifecycle
_maybe_register_gateway_service = unsupported_lifecycle
_maybe_unregister_gateway_service = unsupported_lifecycle
