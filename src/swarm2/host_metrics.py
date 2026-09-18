"""Small host metrics snapshot; no process or input collection."""

from collections.abc import Mapping
from dataclasses import dataclass
import json
import math
import os
from .file_io import open_regular_read, sync_directory
from pathlib import Path
import re
import shutil
import stat
import subprocess
import tempfile

from .configuration import configuration_directory
from .runtime import host_command_environment


GPU_PREFERENCE_SCHEMA = "swarm2.mc7.host-metrics"
GPU_PREFERENCE_VERSION = 1
MAX_GPU_PREFERENCE_BYTES = 4096


class MetricsUnavailable(RuntimeError):
    pass


class GpuSourcePreferenceError(ValueError):
    pass


@dataclass(frozen=True)
class GpuMetrics:
    percent: float | None = None
    temperature_c: float | None = None
    source: str | None = None


@dataclass(frozen=True)
class GpuSource:
    """One selectable adapter, identified independently of DRM card order."""

    identity: str
    label: str
    primary: bool = False
    load_available: bool = False
    temperature_available: bool = False


@dataclass(frozen=True)
class _GpuRecord:
    metrics: GpuMetrics
    primary: bool = False


@dataclass(frozen=True)
class HostMetrics:
    cpu_percent: float
    ram_percent: float
    gpu_percent: float | None = None
    cpu_temperature_c: float | None = None
    gpu_temperature_c: float | None = None


def _number(value, label, maximum):
    if (isinstance(value, bool) or not isinstance(value, (int, float))
            or not math.isfinite(value) or not 0 <= value <= maximum):
        raise MetricsUnavailable(f"The operating system returned an invalid {label}")
    return float(value)


def _percentage(value, label):
    return _number(value, f"{label} percentage", 100)


def _temperature(value, label):
    return _number(value, f"{label} temperature", 0xFFFF)


def _temperatures(provider, chip_markers) -> list[float]:
    getter = getattr(provider, "sensors_temperatures", None)
    if not callable(getter):
        return []
    try:
        groups = getter(fahrenheit=False)
    except (OSError, RuntimeError, TypeError, ValueError):
        return []
    if not isinstance(groups, Mapping):
        return []
    values = []
    for chip, entries in groups.items():
        if not isinstance(chip, str) or not any(marker in chip.lower() for marker in chip_markers):
            continue
        try:
            iterator = iter(entries)
        except TypeError:
            continue
        for entry in iterator:
            try:
                values.append(_temperature(entry.current, chip))
            except (AttributeError, MetricsUnavailable):
                continue
    return values


def _read_number(path: Path, *, scale: float = 1.0) -> float | None:
    try:
        return float(path.read_text(encoding="ascii").strip()) / scale
    except (OSError, UnicodeError, ValueError):
        return None


def _pci_slot(value: str) -> str | None:
    match = re.fullmatch(
        r"(?:[0-9a-fA-F]{4})?([0-9a-fA-F]{4}):([0-9a-fA-F]{2}):"
        r"([0-9a-fA-F]{2})\.([0-7])", value.strip())
    if match is None:
        return None
    domain, bus, device, function = match.groups()
    return f"pci:{domain.lower()}:{bus.lower()}:{device.lower()}.{function}"


def _linux_gpu_identity(device: Path, resolved: Path) -> str:
    try:
        for line in (device / "uevent").read_text(encoding="ascii").splitlines():
            if line.startswith("PCI_SLOT_NAME="):
                slot = _pci_slot(line.partition("=")[2])
                if slot is not None:
                    return slot
    except (OSError, UnicodeError):
        pass
    slot = _pci_slot(resolved.name)
    return slot if slot is not None else "sysfs:" + str(resolved)


def _linux_gpu_records(root: Path = Path("/sys/class/drm")) -> dict[str, _GpuRecord]:
    """Return every distinct DRM adapter under its stable device identity."""

    records: dict[str, _GpuRecord] = {}
    devices: set[Path] = set()
    try:
        cards = sorted(path for path in root.glob("card*")
                       if re.fullmatch(r"card\d+", path.name))
    except OSError:
        cards = []
    for card in cards:
        device = card / "device"
        try:
            identity = device.resolve()
        except OSError:
            identity = device
        if identity in devices:
            continue
        devices.add(identity)
        load = _read_number(device / "gpu_busy_percent")
        valid_load = None
        if load is not None:
            try:
                valid_load = _percentage(load, "GPU")
            except MetricsUnavailable:
                pass
        temperatures: list[float] = []
        try:
            temperature_files = sorted((device / "hwmon").glob("hwmon*/temp1_input"))
        except OSError:
            temperature_files = []
        for path in temperature_files:
            temperature = _read_number(path, scale=1000)
            if temperature is not None:
                try:
                    temperatures.append(_temperature(temperature, "GPU"))
                except MetricsUnavailable:
                    pass
        source = _linux_gpu_identity(device, identity)
        metrics = GpuMetrics(valid_load, max(temperatures, default=None), source)
        # Select from the complete adapter inventory. Readability must never
        # make a secondary card become primary from one sample to the next.
        records[source] = _GpuRecord(
            metrics, _read_number(device / "boot_vga") == 1)
    return records


def _automatic_linux_gpu(records: Mapping[str, _GpuRecord]) -> GpuMetrics:
    """Choose a DRM adapter only when its system role is unambiguous."""

    records = list(records.values())
    primary = [record.metrics for record in records if record.primary]
    if len(primary) == 1:
        return primary[0]
    if len(records) == 1:
        return records[0].metrics
    # Automatic mode needs a unique system primary. The explicit source API
    # below lets a user resolve an otherwise ambiguous multi-adapter system.
    return GpuMetrics()


def _linux_gpu_metrics(root: Path = Path("/sys/class/drm")) -> GpuMetrics:
    """Read one stable DRM device, preferring the unique boot VGA adapter."""

    return _automatic_linux_gpu(_linux_gpu_records(root))


def _nvidia_gpu_records() -> dict[str, GpuMetrics]:
    executable = shutil.which("nvidia-smi")
    if executable is None:
        return {}
    try:
        result = subprocess.run(
            [executable, "--query-gpu=pci.bus_id,utilization.gpu,temperature.gpu",
             "--format=csv,noheader,nounits"],
            capture_output=True, text=True, timeout=1, check=False,
            env=host_command_environment({"LC_ALL": "C", "LANG": "C"}),
        )
    except (OSError, subprocess.SubprocessError):
        return {}
    if result.returncode:
        return {}
    records: dict[str, GpuMetrics] = {}
    for line in result.stdout.splitlines():
        fields = [field.strip() for field in line.split(",")]
        if len(fields) != 3:
            continue
        source = _pci_slot(fields[0])
        if source is None or source in records:
            continue
        load = temperature = None
        try:
            load = _percentage(float(fields[1]), "GPU")
        except (ValueError, MetricsUnavailable):
            pass
        try:
            temperature = _temperature(float(fields[2]), "GPU")
        except (ValueError, MetricsUnavailable):
            pass
        if load is not None or temperature is not None:
            records[source] = GpuMetrics(load, temperature, source)
    return records


def _coerce_gpu_metrics(value) -> GpuMetrics:
    if isinstance(value, GpuMetrics):
        result = value
    elif isinstance(value, Mapping):
        result = GpuMetrics(value.get("percent"), value.get("temperature_c"),
                            value.get("source"))
    elif isinstance(value, (tuple, list)) and len(value) == 2:
        result = GpuMetrics(*value)
    else:
        return GpuMetrics()
    try:
        percent = None if result.percent is None else _percentage(result.percent, "GPU")
    except MetricsUnavailable:
        percent = None
    try:
        temperature = (None if result.temperature_c is None
                       else _temperature(result.temperature_c, "GPU"))
    except MetricsUnavailable:
        temperature = None
    source = result.source if isinstance(result.source, str) and result.source else None
    return GpuMetrics(percent, temperature, source)


def _merge_gpu_metrics(preferred: GpuMetrics, fallback: GpuMetrics) -> GpuMetrics:
    """Fill unreadable fields only when both providers describe one adapter."""

    if preferred.source != fallback.source:
        return preferred
    return GpuMetrics(
        preferred.percent if preferred.percent is not None else fallback.percent,
        preferred.temperature_c if preferred.temperature_c is not None
        else fallback.temperature_c,
        preferred.source,
    )


def _normalize_gpu_source(value, *, error_type=MetricsUnavailable) -> str | None:
    if value is None:
        return None
    if not isinstance(value, str) or not value:
        raise error_type("GPU source must be Automatic or a stable adapter identity")
    pci = re.fullmatch(
        r"pci:([0-9a-f]{4}):([0-9a-f]{2}):([0-9a-f]{2})\.([0-7])",
        value, re.IGNORECASE)
    if pci is not None:
        domain, bus, device, function = pci.groups()
        return f"pci:{domain.lower()}:{bus.lower()}:{device.lower()}.{function}"
    if (value.startswith("sysfs:/") and len(value) <= 4096
            and all(character.isprintable() and character not in "\r\n" for character in value)):
        return value
    raise error_type("GPU source is not a stable PCI or sysfs adapter identity")


def _gpu_source_label(identity: str) -> str:
    if identity.startswith("pci:"):
        return "PCI " + identity.removeprefix("pci:")
    return "DRM " + Path(identity.removeprefix("sysfs:")).name


def available_gpu_sources() -> tuple[GpuSource, ...]:
    """Enumerate selectable adapters without choosing one or opening USB."""

    linux = _linux_gpu_records()
    nvidia = _nvidia_gpu_records()
    identities = sorted(set(linux) | set(nvidia))
    sources = []
    for identity in identities:
        record = linux.get(identity)
        metrics = record.metrics if record is not None else nvidia[identity]
        if record is not None and identity in nvidia:
            metrics = _merge_gpu_metrics(metrics, nvidia[identity])
        sources.append(GpuSource(
            identity=identity,
            label=_gpu_source_label(identity),
            primary=bool(record and record.primary),
            load_available=metrics.percent is not None,
            temperature_available=metrics.temperature_c is not None,
        ))
    return tuple(sources)


def _sample_gpu(provider=None, *, source: str | None = None) -> GpuMetrics:
    selected = _normalize_gpu_source(source)
    if provider is not None:
        try:
            value = provider() if callable(provider) else provider.sample()
        except (AttributeError, OSError, RuntimeError, TypeError, ValueError):
            return GpuMetrics()
        result = _coerce_gpu_metrics(value)
        if selected is not None and result.source != selected:
            return GpuMetrics(source=selected)
        return result

    nvidia = _nvidia_gpu_records()
    if selected is not None:
        linux_records = _linux_gpu_records()
        record = linux_records.get(selected)
        linux = record.metrics if record is not None else None
        vendor = nvidia.get(selected)
        if linux is not None and vendor is not None:
            return _merge_gpu_metrics(linux, vendor)
        if linux is not None:
            return linux
        if vendor is not None:
            return vendor
        # Never replace a saved, missing source with a different adapter.
        return GpuMetrics(source=selected)

    linux_records = _linux_gpu_records()
    primary = [record.metrics for record in linux_records.values()
               if record.primary]
    if len(primary) == 1:
        vendor = nvidia.get(primary[0].source)
        return (primary[0] if vendor is None
                else _merge_gpu_metrics(primary[0], vendor))
    # Without one system primary, choose automatically only when the complete
    # DRM/vendor inventory contains a single identity.
    identities = set(linux_records) | set(nvidia)
    if len(identities) != 1:
        return GpuMetrics()
    identity = next(iter(identities))
    record, vendor = linux_records.get(identity), nvidia.get(identity)
    if record is None:
        return vendor
    return (record.metrics if vendor is None
            else _merge_gpu_metrics(record.metrics, vendor))


def sample_metrics(*, provider=None, gpu_provider=None,
                   gpu_source: str | None = None) -> HostMetrics:
    """Measure aggregate CPU, RAM and optional temperature/GPU telemetry.

    Run in a worker: an explicit interval produces a valid first CPU reading
    even when each job uses a different thread. CPU and RAM are required.
    Temperature and GPU sources are best effort and remain ``None`` when the
    operating system or hardware does not expose them without privileges.
    """
    injected_system_provider = provider is not None
    if provider is None:
        try:
            import psutil as provider
        except ImportError as error:
            raise MetricsUnavailable(
                "Install the GUI dependencies, including psutil, to read host usage") from error
    try:
        cpu = _percentage(provider.cpu_percent(interval=0.1), "CPU")
        ram = _percentage(provider.virtual_memory().percent, "RAM")
    except (AttributeError, OSError, RuntimeError, TypeError, ValueError) as error:
        if isinstance(error, MetricsUnavailable):
            raise
        raise MetricsUnavailable(f"Host CPU/RAM usage is unavailable: {error}") from error

    cpu_temperatures = _temperatures(
        provider, ("coretemp", "k10temp", "zenpower", "cpu_thermal", "cpu-thermal"))
    # Injected system fixtures stay isolated unless a GPU provider is supplied.
    # Validate an explicit identity, but never reach into the real host while
    # the caller is controlling the system provider.
    gpu = (_sample_gpu(gpu_provider, source=gpu_source) if gpu_provider is not None
           else GpuMetrics(source=_normalize_gpu_source(gpu_source))
           if injected_system_provider
           else _sample_gpu(source=gpu_source))
    return HostMetrics(cpu, ram, gpu.percent,
                       max(cpu_temperatures, default=None), gpu.temperature_c)


def gpu_source_preference_path() -> Path:
    return configuration_directory() / "host-metrics" / "gpu-source.json"


def _preference_pairs(pairs):
    result = {}
    for key, value in pairs:
        if key in result:
            raise GpuSourcePreferenceError(
                f"GPU source preference contains duplicate field {key!r}")
        result[key] = value
    return result


class GpuSourcePreferenceStore:
    """Bounded atomic storage for the host-only GPU source selection."""

    def __init__(self, path: str | Path | None = None):
        self.path = Path(path) if path is not None else gpu_source_preference_path()

    def load(self) -> str | None:
        try:
            descriptor = open_regular_read(self.path)
            with os.fdopen(descriptor, "rb") as stream:
                if not stat.S_ISREG(os.fstat(stream.fileno()).st_mode):
                    raise GpuSourcePreferenceError(
                        "GPU source preference must be a regular JSON file")
                raw = stream.read(MAX_GPU_PREFERENCE_BYTES + 1)
        except FileNotFoundError:
            return None
        except OSError as error:
            raise GpuSourcePreferenceError(
                "GPU source preference could not be read safely") from error
        if len(raw) > MAX_GPU_PREFERENCE_BYTES:
            raise GpuSourcePreferenceError("GPU source preference is too large")
        try:
            value = json.loads(raw.decode("utf-8"), object_pairs_hook=_preference_pairs)
        except (UnicodeError, json.JSONDecodeError, RecursionError, ValueError) as error:
            if isinstance(error, GpuSourcePreferenceError):
                raise
            raise GpuSourcePreferenceError(
                "GPU source preference must contain valid UTF-8 JSON") from error
        if (not isinstance(value, dict)
                or set(value) != {"schema", "version", "gpu_source"}
                or value["schema"] != GPU_PREFERENCE_SCHEMA
                or type(value["version"]) is not int
                or value["version"] != GPU_PREFERENCE_VERSION):
            raise GpuSourcePreferenceError("GPU source preference has an unsupported format")
        return _normalize_gpu_source(
            value["gpu_source"], error_type=GpuSourcePreferenceError)

    def save(self, source: str | None) -> Path:
        source = _normalize_gpu_source(source, error_type=GpuSourcePreferenceError)
        raw = (json.dumps({
            "schema": GPU_PREFERENCE_SCHEMA,
            "version": GPU_PREFERENCE_VERSION,
            "gpu_source": source,
        }, indent=2, sort_keys=True) + "\n").encode("utf-8")
        if len(raw) > MAX_GPU_PREFERENCE_BYTES:
            raise GpuSourcePreferenceError("GPU source preference is too large")
        self.path.parent.mkdir(mode=0o700, parents=True, exist_ok=True)
        temporary = None
        try:
            descriptor, temporary = tempfile.mkstemp(
                prefix=f".{self.path.name}.", suffix=".tmp", dir=self.path.parent)
            with os.fdopen(descriptor, "wb") as stream:
                stream.write(raw)
                stream.flush()
                os.fsync(stream.fileno())
            os.replace(temporary, self.path)
            temporary = None
            sync_directory(self.path.parent)
        except OSError as error:
            raise GpuSourcePreferenceError(
                "GPU source preference could not be saved atomically") from error
        finally:
            if temporary is not None:
                Path(temporary).unlink(missing_ok=True)
        return self.path
