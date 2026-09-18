"""Local automatic-profile rules and deterministic application matching.

This module stores user-selected application identities and resolves a desired
onboard profile from a caller-provided application snapshot. It does not list
processes, watch windows, open a device, or switch a mouse profile by itself.
"""

from __future__ import annotations

from dataclasses import dataclass, field
import json
import os
from .file_io import open_regular_read, sync_directory
from pathlib import Path
import posixpath
import re
import stat
import tempfile
from typing import Any, Iterable
import uuid

from .configuration import configuration_directory


SCHEMA = "swarm2.mc7.automatic-profiles"
SCHEMA_VERSION = 1
MAX_RULE_FILE_BYTES = 64 * 1024
MAX_RULES = 128
MAX_APPLICATIONS = 4096
MATCH_KINDS = ("executable_path", "bundle_id")

_RULE_ID = re.compile(r"[A-Za-z0-9_-]{1,64}\Z")
_BUNDLE_ID = re.compile(
    r"[A-Za-z0-9][A-Za-z0-9-]*(?:\.[A-Za-z0-9][A-Za-z0-9-]*)+\Z"
)


class AutomaticProfileError(ValueError):
    """An automatic-profile rule file or application identity is invalid."""


@dataclass(frozen=True)
class ApplicationProfileRule:
    """One exact application identity associated with an onboard slot."""

    name: str
    profile_slot: int
    match_kind: str
    match_value: str
    enabled: bool = True
    rule_id: str = field(default_factory=lambda: uuid.uuid4().hex)

    def _raw_dict(self) -> dict[str, Any]:
        return {
            "id": self.rule_id,
            "name": self.name,
            "enabled": self.enabled,
            "profile_slot": self.profile_slot,
            "match": {"kind": self.match_kind, "value": self.match_value},
        }

    def normalized(self) -> ApplicationProfileRule:
        return _parse_rule(self._raw_dict(), 1)

    def to_dict(self) -> dict[str, Any]:
        return self.normalized()._raw_dict()


@dataclass(frozen=True)
class AutomaticProfileSettings:
    """Ordered matching rules plus the slot restored when no rule matches."""

    enabled: bool = False
    default_profile_slot: int = 1
    rules: tuple[ApplicationProfileRule, ...] = ()

    def _raw_dict(self) -> dict[str, Any]:
        if not isinstance(self.rules, tuple) or any(
            type(rule) is not ApplicationProfileRule for rule in self.rules
        ):
            raise AutomaticProfileError(
                "Automatic profile rules must be a tuple of ApplicationProfileRule values."
            )
        return {
            "schema": SCHEMA,
            "version": SCHEMA_VERSION,
            "enabled": self.enabled,
            "default_profile_slot": self.default_profile_slot,
            "rules": [rule._raw_dict() for rule in self.rules],
        }

    def normalized(self) -> AutomaticProfileSettings:
        return self.from_dict(self._raw_dict())

    def to_dict(self) -> dict[str, Any]:
        return self.normalized()._raw_dict()

    @classmethod
    def from_dict(cls, value: Any) -> AutomaticProfileSettings:
        value = _object(
            value,
            "Automatic profile settings",
            ("schema", "version", "enabled", "default_profile_slot", "rules"),
        )
        if value["schema"] != SCHEMA:
            raise AutomaticProfileError("This is not an MC7 automatic-profile rule file.")
        if type(value["version"]) is not int or value["version"] != SCHEMA_VERSION:
            raise AutomaticProfileError(
                "Unsupported automatic-profile version; this application "
                f"supports version {SCHEMA_VERSION}."
            )
        enabled = _boolean(value["enabled"], "Automatic switching enabled")
        default = _profile_slot(value["default_profile_slot"], "Default profile slot")
        raw_rules = value["rules"]
        if not isinstance(raw_rules, list) or len(raw_rules) > MAX_RULES:
            raise AutomaticProfileError(
                f"Rules must be a list containing at most {MAX_RULES} items."
            )

        rules = tuple(_parse_rule(rule, index) for index, rule in enumerate(raw_rules, 1))
        rule_ids = [rule.rule_id for rule in rules]
        if len(set(rule_ids)) != len(rule_ids):
            raise AutomaticProfileError("Every automatic-profile rule must have a unique ID.")
        identities = [(rule.match_kind, rule.match_value) for rule in rules]
        if len(set(identities)) != len(identities):
            raise AutomaticProfileError(
                "Each application identity can appear in only one automatic-profile rule."
            )
        return cls(enabled=enabled, default_profile_slot=default, rules=rules)


@dataclass(frozen=True)
class ApplicationIdentity:
    """Observed application identity; at least one exact identity is required."""

    executable_path: str | None = None
    bundle_id: str | None = None

    def normalized(self) -> ApplicationIdentity:
        if self.executable_path is None and self.bundle_id is None:
            raise AutomaticProfileError(
                "An observed application needs an executable path or bundle identifier."
            )
        executable = (
            None
            if self.executable_path is None
            else _executable_path(self.executable_path, "Observed executable path")
        )
        bundle = (
            None
            if self.bundle_id is None
            else _bundle_id(self.bundle_id, "Observed bundle identifier")
        )
        return ApplicationIdentity(executable_path=executable, bundle_id=bundle)


@dataclass(frozen=True)
class ProfileSelection:
    """Pure matching result for a future device-switch coordinator."""

    profile_slot: int
    source: str
    rule: ApplicationProfileRule | None = None
    application: ApplicationIdentity | None = None


def resolve_profile(
    settings: AutomaticProfileSettings,
    applications: Iterable[ApplicationIdentity],
) -> ProfileSelection | None:
    """Resolve the first matching rule, or the configured default profile.

    Rule order is explicit priority. Application order does not affect the
    chosen rule. Disabled settings return ``None`` so a caller cannot mistake a
    disabled feature for a request to switch to the default slot.
    """

    if type(settings) is not AutomaticProfileSettings:
        raise AutomaticProfileError("Choose validated automatic-profile settings.")
    normalized_settings = settings.normalized()
    if not normalized_settings.enabled:
        return None

    normalized_applications: set[ApplicationIdentity] = set()
    try:
        for index, application in enumerate(applications, 1):
            if index > MAX_APPLICATIONS:
                raise AutomaticProfileError(
                    f"Application snapshots can contain at most {MAX_APPLICATIONS} items."
                )
            if type(application) is not ApplicationIdentity:
                raise AutomaticProfileError(
                    "Application snapshots must contain ApplicationIdentity values."
                )
            normalized_applications.add(application.normalized())
    except TypeError as exc:
        raise AutomaticProfileError("Application snapshots must be iterable.") from exc

    ordered_applications = sorted(
        normalized_applications,
        key=lambda app: (app.executable_path or "", app.bundle_id or ""),
    )
    for rule in normalized_settings.rules:
        if not rule.enabled:
            continue
        for application in ordered_applications:
            if _matches(rule, application):
                return ProfileSelection(
                    profile_slot=rule.profile_slot,
                    source="application",
                    rule=rule,
                    application=application,
                )
    return ProfileSelection(
        profile_slot=normalized_settings.default_profile_slot,
        source="default",
    )


def automatic_profile_settings_path() -> Path:
    """Return a path separate from the preset store's root-level JSON files."""

    return configuration_directory() / "automatic-profiles" / "rules.json"


class AutomaticProfileStore:
    """Atomic, bounded storage for local automatic-profile settings."""

    def __init__(self, path: str | Path | None = None):
        self.path = Path(path) if path is not None else automatic_profile_settings_path()

    def load(self) -> AutomaticProfileSettings:
        try:
            descriptor = open_regular_read(self.path)
            with os.fdopen(descriptor, "rb") as stream:
                if not stat.S_ISREG(os.fstat(stream.fileno()).st_mode):
                    raise AutomaticProfileError(
                        "Automatic-profile settings must be a regular JSON file."
                    )
                raw = stream.read(MAX_RULE_FILE_BYTES + 1)
        except FileNotFoundError:
            return AutomaticProfileSettings()
        except OSError as exc:
            raise AutomaticProfileError(
                "Automatic-profile settings could not be read safely."
            ) from exc

        if len(raw) > MAX_RULE_FILE_BYTES:
            raise AutomaticProfileError(
                "Automatic-profile settings must be no larger than "
                f"{MAX_RULE_FILE_BYTES // 1024} KiB."
            )
        try:
            value = json.loads(
                raw.decode("utf-8"),
                object_pairs_hook=_json_pairs,
                parse_constant=_invalid_constant,
            )
        except (UnicodeError, json.JSONDecodeError, RecursionError, ValueError) as exc:
            if isinstance(exc, AutomaticProfileError):
                raise
            raise AutomaticProfileError(
                "Automatic-profile settings must contain valid, bounded UTF-8 JSON."
            ) from exc
        return AutomaticProfileSettings.from_dict(value)

    def save(self, settings: AutomaticProfileSettings) -> Path:
        if type(settings) is not AutomaticProfileSettings:
            raise AutomaticProfileError("Choose validated automatic-profile settings.")
        normalized = settings.normalized()
        raw = (
            json.dumps(normalized._raw_dict(), ensure_ascii=False, indent=2, sort_keys=True)
            + "\n"
        ).encode("utf-8")
        if len(raw) > MAX_RULE_FILE_BYTES:
            raise AutomaticProfileError(
                f"Automatic-profile settings exceed {MAX_RULE_FILE_BYTES // 1024} KiB."
            )
        self.path.parent.mkdir(mode=0o700, parents=True, exist_ok=True)
        temporary: str | None = None
        try:
            descriptor, temporary = tempfile.mkstemp(
                prefix=f".{self.path.name}.", suffix=".tmp", dir=self.path.parent
            )
            with os.fdopen(descriptor, "wb") as stream:
                stream.write(raw)
                stream.flush()
                os.fsync(stream.fileno())
            os.replace(temporary, self.path)
            temporary = None
            sync_directory(self.path.parent)
        except OSError as exc:
            raise AutomaticProfileError(
                "Automatic-profile settings could not be saved atomically."
            ) from exc
        finally:
            if temporary is not None:
                Path(temporary).unlink(missing_ok=True)
        return self.path


def _parse_rule(value: Any, index: int) -> ApplicationProfileRule:
    label = f"Rule {index}"
    value = _object(value, label, ("id", "name", "enabled", "profile_slot", "match"))
    rule_id = value["id"]
    if not isinstance(rule_id, str) or _RULE_ID.fullmatch(rule_id) is None:
        raise AutomaticProfileError(
            f"{label} ID must use 1 to 64 letters, numbers, underscores, or hyphens."
        )
    match = _object(value["match"], f"{label} match", ("kind", "value"))
    kind = match["kind"]
    if kind not in MATCH_KINDS or not isinstance(kind, str):
        raise AutomaticProfileError(
            f"{label} match kind must be one of: {', '.join(MATCH_KINDS)}."
        )
    match_value = (
        _executable_path(match["value"], f"{label} executable path")
        if kind == "executable_path"
        else _bundle_id(match["value"], f"{label} bundle identifier")
    )
    return ApplicationProfileRule(
        rule_id=rule_id,
        name=_text(value["name"], f"{label} name", 80),
        enabled=_boolean(value["enabled"], f"{label} enabled"),
        profile_slot=_profile_slot(value["profile_slot"], f"{label} profile slot"),
        match_kind=kind,
        match_value=match_value,
    )


def _matches(rule: ApplicationProfileRule, application: ApplicationIdentity) -> bool:
    if rule.match_kind == "executable_path":
        return application.executable_path == rule.match_value
    return application.bundle_id == rule.match_value


def _object(value: Any, label: str, keys: tuple[str, ...]) -> dict[str, Any]:
    if not isinstance(value, dict):
        raise AutomaticProfileError(f"{label} must be a JSON object.")
    if set(value) != set(keys):
        raise AutomaticProfileError(
            f"{label} has missing or unknown fields; expected: {', '.join(keys)}."
        )
    return value


def _text(value: Any, label: str, maximum: int) -> str:
    if not isinstance(value, str) or not value.strip() or len(value) > maximum:
        raise AutomaticProfileError(
            f"{label} must be a nonempty string of at most {maximum} characters."
        )
    if any(
        ord(character) < 32
        or ord(character) == 127
        or 0xD800 <= ord(character) <= 0xDFFF
        for character in value
    ):
        raise AutomaticProfileError(
            f"{label} must not contain control characters or invalid Unicode."
        )
    return value


def _profile_slot(value: Any, label: str) -> int:
    if type(value) is not int or not 1 <= value <= 5:
        raise AutomaticProfileError(f"{label} must be an integer from 1 to 5.")
    return value


def _boolean(value: Any, label: str) -> bool:
    if type(value) is not bool:
        raise AutomaticProfileError(f"{label} must be true or false.")
    return value


def _executable_path(value: Any, label: str) -> str:
    value = _text(value, label, 4096)
    windows = value.replace("\\", "/")
    if re.fullmatch(r"[A-Za-z]:/[^/].*", windows):
        normalized = posixpath.normpath(windows)
        if normalized != windows:
            raise AutomaticProfileError(
                f"{label} must not contain duplicate separators, dot segments, or a trailing slash."
            )
        return windows.casefold()
    if value == "/" or not value.startswith("/") or value.startswith("//"):
        raise AutomaticProfileError(f"{label} must be an absolute application path.")
    normalized = posixpath.normpath(value)
    if normalized != value:
        raise AutomaticProfileError(
            f"{label} must not contain duplicate separators, dot segments, or a trailing slash."
        )
    return value


def _bundle_id(value: Any, label: str) -> str:
    value = _text(value, label, 255)
    if _BUNDLE_ID.fullmatch(value) is None:
        raise AutomaticProfileError(
            f"{label} must be a reverse-domain identifier such as com.example.Game."
        )
    return value.casefold()


def _json_pairs(pairs: list[tuple[str, Any]]) -> dict[str, Any]:
    result: dict[str, Any] = {}
    for key, value in pairs:
        if key in result:
            raise AutomaticProfileError(
                f"Automatic-profile JSON contains the duplicate field {key!r}."
            )
        result[key] = value
    return result


def _invalid_constant(value: str) -> None:
    raise AutomaticProfileError(
        f"Automatic-profile JSON contains the invalid number {value}."
    )
