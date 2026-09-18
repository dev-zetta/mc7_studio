"""Host-owned countdown state for MC7 LCD timer tiles.

This module is independent from USB discovery and process control. A caller
supplies a serialized transport plus a guard that verifies the active profile
and layout around each transient A3 batch.
"""

from dataclasses import dataclass, field
import math
from queue import Empty, Full, Queue
import re
from threading import BoundedSemaphore, Event, Thread
import time

from .countdown_commands import (
    COUNTDOWN_MAX_SECONDS,
    CountdownDisplayUpdate,
    build_countdown_reports,
    decode_countdown_press,
)
from .general_media_commands import (
    GeneralMediaAction,
    GeneralMediaDisplayState,
    GeneralMediaSlotForm,
    build_general_media_report,
    decode_general_media_press,
)
from .host_action_commands import decode_host_action_touch
from .host_actions import HostActionBinding, execute_host_action
from .host_media import (
    DISPATCH_TIMEOUT_SECONDS,
    MediaAction,
    MediaControlError,
    MediaDispatch,
    MediaPlaybackState,
    create_host_media_provider,
)
from .obs_actions import (
    ObsLaunchBinding,
    ObsScreenshotBinding,
    ObsStudioModeBinding,
    execute_obs_launch,
    execute_obs_screenshot,
    execute_obs_studio_mode,
)
from .obs_commands import (
    decode_obs_launch_touch,
    decode_obs_screenshot_touch,
    decode_obs_studio_mode_touch,
)
from .obs_studio_mode_commands import (
    ObsStudioModeDisplayUpdate,
    build_obs_studio_mode_reports,
)
from .obs_websocket import (
    ObsStudioModeToggleResult,
    get_obs_studio_mode_enabled,
)
from .transport import DeviceError


MAX_QUEUED_MEDIA_ACTIONS = 4
MEDIA_REFRESH_INTERVAL_SECONDS = 2.0
# One action and its following state read each have their own aggregate provider
# deadline and run serially in the same worker.
MEDIA_WORKER_STOP_TIMEOUT_SECONDS = DISPATCH_TIMEOUT_SECONDS * 2 + 0.25
_MEDIA_WORKER_STOP = object()


@dataclass
class _QueuedMediaAction:
    action: MediaAction
    page_index: int
    slot_index: int
    guard_ready: Event = field(default_factory=Event)
    approved: bool = False


@dataclass(frozen=True)
class _QueuedMediaRefresh:
    reason: str

    def __post_init__(self) -> None:
        if self.reason not in ("initial", "poll"):
            raise ValueError("Media refresh reason is invalid")


@dataclass(frozen=True)
class _MediaWorkerResult:
    task: object
    dispatch: object | None = None
    state: object | None = None
    error: Exception | None = None
    state_error: Exception | None = None


@dataclass(frozen=True)
class CountdownBinding:
    timer_id: str
    page_index: int
    slot_index: int
    duration_seconds: int

    def __post_init__(self) -> None:
        if (not isinstance(self.timer_id, str) or len(self.timer_id) > 64
                or re.fullmatch(r"[A-Za-z0-9-]+", self.timer_id) is None):
            raise ValueError(
                "Countdown timer ID must be 1..64 letters, digits, or hyphens")
        if type(self.page_index) is not int or not 0 <= self.page_index <= 2:
            raise ValueError("Countdown page must be 0..2")
        if type(self.slot_index) is not int or not 0 <= self.slot_index <= 3:
            raise ValueError("Countdown slot must be 0..3")
        if (type(self.duration_seconds) is not int
                or not 1 <= self.duration_seconds <= COUNTDOWN_MAX_SECONDS):
            raise ValueError(f"Countdown duration must be 1..{COUNTDOWN_MAX_SECONDS} seconds")


@dataclass(frozen=True)
class GeneralMediaBinding:
    page_index: int
    slot_index: int

    def __post_init__(self) -> None:
        if type(self.page_index) is not int or not 0 <= self.page_index <= 2:
            raise ValueError("General Media page must be 0..2")
        # The app-1024 tile is three cells wide, so it can start only in the
        # first or second logical cell of a four-cell page.
        if type(self.slot_index) is not int or not 0 <= self.slot_index <= 1:
            raise ValueError("General Media tile must start in slot 0 or 1")


_MEDIA_ACTIONS = {
    GeneralMediaAction.SHUFFLE: MediaAction.SHUFFLE,
    GeneralMediaAction.NEXT: MediaAction.NEXT,
    GeneralMediaAction.PLAY_PAUSE: MediaAction.PLAY_PAUSE,
    GeneralMediaAction.PREVIOUS: MediaAction.PREVIOUS,
    GeneralMediaAction.REPEAT: MediaAction.REPEAT,
}


class LcdActionRuntime:
    """Dispatch host actions and countdowns through one LCD event owner."""

    def __init__(self, countdown_bindings, host_action_bindings, transport,
                 emit=lambda value: None, *, general_media_bindings=(),
                 obs_launch_bindings=(), obs_screenshot_bindings=(),
                 obs_studio_mode_bindings=(),
                 guard=lambda: None, clock=time.monotonic,
                 launcher=execute_host_action, obs_launcher=execute_obs_launch,
                 obs_screenshot_executor=execute_obs_screenshot,
                 obs_studio_mode_executor=execute_obs_studio_mode,
                 obs_studio_mode_state_reader=get_obs_studio_mode_enabled,
                 media_provider=None):
        self.bindings = self.countdown_bindings = tuple(countdown_bindings)
        self.host_action_bindings = tuple(host_action_bindings)
        self.general_media_bindings = tuple(general_media_bindings)
        self.obs_launch_bindings = tuple(obs_launch_bindings)
        self.obs_screenshot_bindings = tuple(obs_screenshot_bindings)
        self.obs_studio_mode_bindings = tuple(obs_studio_mode_bindings)
        if any(not isinstance(item, CountdownBinding)
               for item in self.countdown_bindings):
            raise ValueError("Countdown bindings must use CountdownBinding")
        if any(not isinstance(item, HostActionBinding)
               for item in self.host_action_bindings):
            raise ValueError("Host actions must use HostActionBinding")
        if any(not isinstance(item, GeneralMediaBinding)
               for item in self.general_media_bindings):
            raise ValueError("General Media bindings must use GeneralMediaBinding")
        if any(not isinstance(item, ObsLaunchBinding)
               for item in self.obs_launch_bindings):
            raise ValueError("Launch OBS bindings must use ObsLaunchBinding")
        if any(not isinstance(item, ObsScreenshotBinding)
               for item in self.obs_screenshot_bindings):
            raise ValueError(
                "OBS Screenshot bindings must use ObsScreenshotBinding"
            )
        if any(not isinstance(item, ObsStudioModeBinding)
               for item in self.obs_studio_mode_bindings):
            raise ValueError(
                "OBS Studio Mode bindings must use ObsStudioModeBinding"
            )
        if (not self.countdown_bindings and not self.host_action_bindings
                and not self.general_media_bindings
                and not self.obs_launch_bindings
                and not self.obs_screenshot_bindings
                and not self.obs_studio_mode_bindings):
            raise ValueError("At least one LCD action binding is required")
        positions = [(item.page_index, item.slot_index)
                     for item in (*self.countdown_bindings,
                                  *self.host_action_bindings,
                                  *self.obs_launch_bindings,
                                  *self.obs_screenshot_bindings,
                                  *self.obs_studio_mode_bindings)]
        positions.extend(
            (item.page_index, item.slot_index + offset)
            for item in self.general_media_bindings for offset in range(3))
        if len(set(positions)) != len(positions):
            raise ValueError("Each LCD action position must be configured once")
        durations = {}
        for item in self.countdown_bindings:
            item.__post_init__()
            old = durations.setdefault(item.timer_id, item.duration_seconds)
            if old != item.duration_seconds:
                raise ValueError("One countdown timer ID must use one duration")
        for item in self.host_action_bindings:
            item.__post_init__()
        for item in self.general_media_bindings:
            item.__post_init__()
        for item in self.obs_launch_bindings:
            item.__post_init__()
        for item in self.obs_screenshot_bindings:
            item.__post_init__()
        for item in self.obs_studio_mode_bindings:
            item.__post_init__()
        self.transport, self.emit, self.guard, self.clock = transport, emit, guard, clock
        self.launcher = launcher
        self.obs_launcher = obs_launcher
        self.obs_screenshot_executor = obs_screenshot_executor
        if not callable(self.obs_screenshot_executor):
            raise ValueError("OBS Screenshot executor must be callable")
        self.obs_studio_mode_executor = obs_studio_mode_executor
        self.obs_studio_mode_state_reader = obs_studio_mode_state_reader
        if (
            not callable(self.obs_studio_mode_executor)
            or not callable(self.obs_studio_mode_state_reader)
        ):
            raise ValueError("OBS Studio Mode providers must be callable")
        self.media_provider = (
            create_host_media_provider()
            if self.general_media_bindings and media_provider is None
            else media_provider
        )
        if (self.general_media_bindings
                and (not callable(getattr(self.media_provider, "perform", None))
                     or not callable(getattr(
                         self.media_provider, "read_state", None)))):
            raise ValueError("General Media bindings require a host media provider")
        self._by_position = {(item.page_index, item.slot_index): item
                             for item in self.countdown_bindings}
        self._host_by_position = {(item.page_index, item.slot_index): item
                                  for item in self.host_action_bindings}
        self._obs_by_position = {(item.page_index, item.slot_index): item
                                 for item in self.obs_launch_bindings}
        self._obs_screenshot_by_position = {
            (item.page_index, item.slot_index): item
            for item in self.obs_screenshot_bindings
        }
        self._obs_studio_mode_by_position = {
            (item.page_index, item.slot_index): item
            for item in self.obs_studio_mode_bindings
        }
        self._media_positions = {
            (item.page_index, item.slot_index) for item in self.general_media_bindings
        }
        self._by_timer = {
            timer_id: tuple(item for item in self.countdown_bindings
                            if item.timer_id == timer_id)
            for timer_id in durations
        }
        self._durations = durations
        self._deadlines: dict[str, float] = {}
        self._last_remaining: dict[str, int] = {}
        self.started = False
        self.display_may_be_stale = False
        self._pressed_host_positions: set[tuple[int, int]] = set()
        self._media_tasks = None
        self._media_guard_requests = None
        self._media_results = None
        self._media_stop_requested = None
        self._media_worker = None
        self._media_accepting = False
        self._media_action_slots = None
        self._media_refresh_pending = False
        self._next_media_refresh_at = None
        self._last_media_display_state = None

    @property
    def running_timer_ids(self) -> tuple[str, ...]:
        return tuple(sorted(self._deadlines))

    def _media_worker_main(self, tasks, guard_requests, results, stop_requested):
        while True:
            task = tasks.get()
            if task is _MEDIA_WORKER_STOP:
                return
            if isinstance(task, _QueuedMediaAction):
                self._media_action_slots.release()
            if stop_requested.is_set():
                continue
            if isinstance(task, _QueuedMediaAction):
                guard_requests.put(task)
                task.guard_ready.wait()
                if not task.approved or stop_requested.is_set():
                    continue
                try:
                    dispatch = self.media_provider.perform(task.action)
                except Exception as error:
                    result = _MediaWorkerResult(task, error=error)
                else:
                    try:
                        state = self.media_provider.read_state()
                    except Exception as error:
                        result = _MediaWorkerResult(
                            task, dispatch=dispatch, state_error=error)
                    else:
                        result = _MediaWorkerResult(
                            task, dispatch=dispatch, state=state)
            elif isinstance(task, _QueuedMediaRefresh):
                try:
                    state = self.media_provider.read_state()
                except Exception as error:
                    result = _MediaWorkerResult(task, error=error)
                else:
                    result = _MediaWorkerResult(task, state=state)
            else:  # pragma: no cover - tasks are private and closed over
                result = _MediaWorkerResult(
                    task, error=TypeError("Invalid host media worker task"))
            results.put(result)

    def _start_media_worker(self):
        if not self.general_media_bindings:
            return
        # Four action slots preserve the public action backlog. The queue has
        # one extra cell so one coalesced refresh never consumes that backlog.
        tasks = Queue(maxsize=MAX_QUEUED_MEDIA_ACTIONS + 1)
        guard_requests = Queue(maxsize=1)
        results = Queue(maxsize=1)
        stop_requested = Event()
        worker = Thread(
            target=self._media_worker_main,
            args=(tasks, guard_requests, results, stop_requested),
            name="swarm2-media-dispatch",
            daemon=True,
        )
        self._media_tasks = tasks
        self._media_guard_requests = guard_requests
        self._media_results = results
        self._media_stop_requested = stop_requested
        self._media_worker = worker
        self._media_accepting = True
        self._media_action_slots = BoundedSemaphore(MAX_QUEUED_MEDIA_ACTIONS)
        self._media_refresh_pending = False
        try:
            worker.start()
        except RuntimeError as error:
            self._media_accepting = False
            self._media_tasks = None
            self._media_guard_requests = None
            self._media_results = None
            self._media_stop_requested = None
            self._media_worker = None
            self._media_action_slots = None
            raise DeviceError("The host media worker could not be started") from error

    def _queue_media_refresh(self, reason):
        if (not self._media_accepting or self._media_tasks is None
                or self._media_refresh_pending):
            return False
        try:
            self._media_tasks.put_nowait(_QueuedMediaRefresh(reason))
        except Full:  # pragma: no cover - one refresh has a reserved queue cell
            return False
        self._media_refresh_pending = True
        return True

    def _begin_media_shutdown(self):
        worker = self._media_worker
        if worker is None:
            return
        self._media_accepting = False
        self._media_stop_requested.set()
        while True:
            try:
                task = self._media_tasks.get_nowait()
            except Empty:
                break
            if isinstance(task, _QueuedMediaAction):
                self._media_action_slots.release()
            elif isinstance(task, _QueuedMediaRefresh):
                self._media_refresh_pending = False
        try:
            self._media_tasks.put_nowait(_MEDIA_WORKER_STOP)
        except Full:  # pragma: no cover - the queue was drained above
            pass
        self._cancel_media_guard_requests()

    def _cancel_media_guard_requests(self):
        if self._media_guard_requests is None:
            return
        while True:
            try:
                task = self._media_guard_requests.get_nowait()
            except Empty:
                return
            task.approved = False
            task.guard_ready.set()

    def _emit_media_worker_result(self, result):
        task = result.task
        if isinstance(task, _QueuedMediaRefresh):
            self._media_refresh_pending = False
            self._next_media_refresh_at = self.clock() + MEDIA_REFRESH_INTERVAL_SECONDS
            if result.error is not None:
                if isinstance(result.error, MediaControlError):
                    return
                self._begin_media_shutdown()
                raise DeviceError(
                    "Host media provider failed unexpectedly") from result.error
            self._apply_media_state(
                result.state,
                slot_form=GeneralMediaSlotForm.SCAN_INDEX,
                force=task.reason == "initial",
            )
            return
        if result.error is not None:
            if not isinstance(result.error, MediaControlError):
                self._begin_media_shutdown()
                raise DeviceError(
                    "Host media provider failed unexpectedly") from result.error
            detail = str(result.error)
            if (not detail or len(detail) > 4096 or not detail.isprintable()):
                self._begin_media_shutdown()
                raise DeviceError(
                    "Host media provider returned an invalid error") from result.error
            self.emit({
                "type": "media_action", "event": "failed",
                "action": task.action.value,
                "page_index": task.page_index,
                "slot_index": task.slot_index,
                "error": detail,
            })
            return
        dispatched = result.dispatch
        if not isinstance(dispatched, MediaDispatch) or dispatched.action != task.action:
            self._begin_media_shutdown()
            raise DeviceError("Host media provider returned an invalid dispatch")
        if result.state_error is not None:
            if not isinstance(result.state_error, MediaControlError):
                self._begin_media_shutdown()
                raise DeviceError(
                    "Host media provider failed unexpectedly") from result.state_error
        else:
            if (not isinstance(result.state, MediaPlaybackState)
                    or result.state.backend != dispatched.backend
                    or result.state.player_id != dispatched.player_id):
                self._begin_media_shutdown()
                raise DeviceError(
                    "Host media provider returned an invalid state")
            self._apply_media_state(
                result.state,
                slot_form=GeneralMediaSlotForm.TOUCH_COORDINATE,
                force=True,
            )
        self._next_media_refresh_at = self.clock() + MEDIA_REFRESH_INTERVAL_SECONDS
        self.emit({
            "type": "media_action", "event": "dispatched",
            "action": task.action.value,
            "page_index": task.page_index,
            "slot_index": task.slot_index,
        })

    def _apply_media_state(self, state, *, slot_form, force):
        if not isinstance(state, MediaPlaybackState):
            self._begin_media_shutdown()
            raise DeviceError("Host media provider returned an invalid state")
        try:
            state.__post_init__()
            display_state = GeneralMediaDisplayState(
                repeat_active=state.repeat_active,
                playing=state.playing,
                shuffle_active=state.shuffle_active,
            )
        except (TypeError, ValueError) as error:
            self._begin_media_shutdown()
            raise DeviceError(
                "Host media provider returned an invalid state") from error
        if not force and display_state == self._last_media_display_state:
            return
        for binding in self.general_media_bindings:
            report = build_general_media_report(
                display_state,
                page_index=binding.page_index,
                logical_slot=binding.slot_index,
                slot_form=slot_form,
            )
            self.guard()
            self.display_may_be_stale = True
            self.transport.send(report)
        self.guard()
        self._last_media_display_state = display_state

    def _service_media_worker(self):
        if self._media_worker is None:
            return
        while True:
            try:
                result = self._media_results.get_nowait()
            except Empty:
                break
            self._emit_media_worker_result(result)
        try:
            task = self._media_guard_requests.get_nowait()
        except Empty:
            return
        if self._media_stop_requested.is_set():
            task.approved = False
            task.guard_ready.set()
            return
        try:
            self.guard()
        except Exception:
            task.approved = False
            task.guard_ready.set()
            self._begin_media_shutdown()
            raise
        task.approved = True
        task.guard_ready.set()

    def _stop_media_worker(self):
        worker = self._media_worker
        if worker is None:
            return
        self._begin_media_shutdown()
        deadline = time.monotonic() + MEDIA_WORKER_STOP_TIMEOUT_SECONDS
        while worker.is_alive():
            self._cancel_media_guard_requests()
            self._service_media_worker()
            # Servicing a completed result can include guarded USB work. The
            # worker may consume its stop token during that work, so check
            # liveness again before applying the worker deadline.
            if not worker.is_alive():
                break
            remaining = deadline - time.monotonic()
            if remaining <= 0:
                raise DeviceError("The host media worker did not stop in time")
            worker.join(min(0.02, remaining))
        self._service_media_worker()
        self._cancel_media_guard_requests()
        self._media_tasks = None
        self._media_guard_requests = None
        self._media_results = None
        self._media_stop_requested = None
        self._media_worker = None
        self._media_accepting = False
        self._media_action_slots = None
        self._media_refresh_pending = False
        self._next_media_refresh_at = None

    def _updates(self, timer_id, *, state, remaining, sync=False):
        duration = self._durations[timer_id]
        factory = CountdownDisplayUpdate.sync if sync else CountdownDisplayUpdate.live
        return [factory(item.page_index, item.slot_index, state=state,
                        total_seconds=duration, remaining_seconds=remaining)
                for item in self._by_timer[timer_id]]

    def _send_updates(self, updates):
        reports = build_countdown_reports(updates)
        if not reports:
            return
        self.guard()
        for index, report in enumerate(reports):
            if index:
                self.guard()
            # From this point the transfer may have reached the mouse even if
            # acknowledgement or the following guard fails.
            self.display_may_be_stale = True
            self.transport.send(report)
        self.guard()

    def _send_obs_studio_mode_state(self, enabled):
        if type(enabled) is not bool:
            raise DeviceError("OBS Studio Mode provider returned an invalid state")
        reports = build_obs_studio_mode_reports([
            ObsStudioModeDisplayUpdate(
                binding.page_index,
                binding.slot_index,
                enabled,
            )
            for binding in self.obs_studio_mode_bindings
        ])
        if not reports:
            return
        self.guard()
        for index, report in enumerate(reports):
            if index:
                self.guard()
            # A3 has no state readback. Treat a failed transfer or following
            # guard as terminal because the display may already have changed.
            self.display_may_be_stale = True
            self.transport.send(report)
        self.guard()

    def start(self):
        if self.started:
            raise DeviceError("Countdown runtime is already started")
        updates = []
        for timer_id in sorted(self._by_timer):
            duration = self._durations[timer_id]
            updates.extend(self._updates(timer_id, state=0, remaining=duration, sync=True))
        if updates:
            self._send_updates(updates)
        else:
            # A host-only listener still validates the active profile, layout,
            # and any applicable command-0x29 definitions before it is ready.
            self.guard()
        if self.obs_studio_mode_bindings:
            state = self.obs_studio_mode_state_reader()
            self._send_obs_studio_mode_state(state)
        self._start_media_worker()
        if self.general_media_bindings:
            self._next_media_refresh_at = self.clock() + MEDIA_REFRESH_INTERVAL_SECONDS
            self._queue_media_refresh("initial")
        self.started = True
        ready = {"type": "ready", "timers": len(self._by_timer),
                 "positions": len(self.countdown_bindings)}
        host_action_count = (
            len(self.host_action_bindings)
            + len(self.obs_launch_bindings)
            + len(self.obs_screenshot_bindings)
            + len(self.obs_studio_mode_bindings)
        )
        if host_action_count:
            ready["host_actions"] = host_action_count
        if self.general_media_bindings:
            ready["media_panels"] = len(self.general_media_bindings)
        self.emit(ready)

    def observe(self, event: bytes, *, now=None) -> bool:
        if not self.started:
            raise DeviceError("Start the LCD action runtime before handling events")
        media_press = decode_general_media_press(event)
        if media_press is not None:
            position = (media_press.page_index, media_press.logical_slot)
            if position not in self._media_positions:
                return False
            action = _MEDIA_ACTIONS[media_press.action]
            try:
                if (not self._media_accepting or self._media_tasks is None
                        or self._media_action_slots is None
                        or not self._media_action_slots.acquire(blocking=False)):
                    raise Full
                try:
                    self._media_tasks.put_nowait(_QueuedMediaAction(
                        action, media_press.page_index, media_press.logical_slot))
                except Exception:
                    self._media_action_slots.release()
                    raise
            except Full:
                self.emit({
                    "type": "media_action", "event": "failed",
                    "action": action.value,
                    "page_index": media_press.page_index,
                    "slot_index": media_press.logical_slot,
                    "error": "Host media action queue is full",
                })
                return True
            return True
        host_touch = decode_host_action_touch(event)
        if host_touch is not None:
            position = (host_touch.page_index, host_touch.slot_index)
            binding = self._host_by_position.get(position)
            if binding is None or binding.widget_key != host_touch.widget_key:
                return False
            if not host_touch.pressed:
                self._pressed_host_positions.discard(position)
                return True
            if position in self._pressed_host_positions:
                return True
            self.guard()
            self._pressed_host_positions.add(position)
            try:
                self.launcher(binding)
            except Exception:
                self._pressed_host_positions.discard(position)
                raise
            self.emit({
                "type": "host_action", "event": "opened",
                "widget": binding.widget_key,
                "page_index": binding.page_index,
                "slot_index": binding.slot_index,
            })
            return True
        obs_touch = decode_obs_launch_touch(event)
        if obs_touch is not None:
            position = (obs_touch.page_index, obs_touch.slot_index)
            binding = self._obs_by_position.get(position)
            if binding is None:
                return False
            if not obs_touch.pressed:
                self._pressed_host_positions.discard(position)
                return True
            if position in self._pressed_host_positions:
                return True
            self.guard()
            self._pressed_host_positions.add(position)
            try:
                self.obs_launcher(binding)
            except DeviceError as error:
                detail = str(error)
                if (not detail or len(detail) > 4096
                        or not detail.isprintable()):
                    self._pressed_host_positions.discard(position)
                    raise DeviceError(
                        "Launch OBS returned an invalid error") from error
                self.emit({
                    "type": "host_action", "event": "failed",
                    "widget": "launch_obs",
                    "page_index": binding.page_index,
                    "slot_index": binding.slot_index,
                    "error": detail,
                })
                return True
            except Exception:
                self._pressed_host_positions.discard(position)
                raise
            self.emit({
                "type": "host_action", "event": "opened",
                "widget": "launch_obs",
                "page_index": binding.page_index,
                "slot_index": binding.slot_index,
            })
            return True
        obs_screenshot_touch = decode_obs_screenshot_touch(event)
        if obs_screenshot_touch is not None:
            position = (
                obs_screenshot_touch.page_index,
                obs_screenshot_touch.slot_index,
            )
            binding = self._obs_screenshot_by_position.get(position)
            if binding is None:
                return False
            if not obs_screenshot_touch.pressed:
                self._pressed_host_positions.discard(position)
                return True
            if position in self._pressed_host_positions:
                return True
            self.guard()
            self._pressed_host_positions.add(position)
            try:
                self.obs_screenshot_executor(binding)
            except DeviceError as error:
                detail = str(error)
                if (
                    not detail
                    or len(detail) > 4096
                    or not detail.isprintable()
                ):
                    self._pressed_host_positions.discard(position)
                    raise DeviceError(
                        "OBS Screenshot returned an invalid error"
                    ) from error
                self.emit({
                    "type": "host_action",
                    "event": "failed",
                    "widget": "obs_screenshot",
                    "page_index": binding.page_index,
                    "slot_index": binding.slot_index,
                    "error": detail,
                })
                return True
            except Exception:
                self._pressed_host_positions.discard(position)
                raise
            self.emit({
                "type": "host_action",
                "event": "triggered",
                "widget": "obs_screenshot",
                "page_index": binding.page_index,
                "slot_index": binding.slot_index,
            })
            return True
        obs_studio_mode_touch = decode_obs_studio_mode_touch(event)
        if obs_studio_mode_touch is not None:
            position = (
                obs_studio_mode_touch.page_index,
                obs_studio_mode_touch.slot_index,
            )
            binding = self._obs_studio_mode_by_position.get(position)
            if binding is None:
                return False
            if not obs_studio_mode_touch.pressed:
                self._pressed_host_positions.discard(position)
                return True
            if position in self._pressed_host_positions:
                return True
            self.guard()
            self._pressed_host_positions.add(position)
            try:
                result = self.obs_studio_mode_executor(binding)
            except DeviceError as error:
                detail = str(error)
                if (
                    not detail
                    or len(detail) > 4096
                    or not detail.isprintable()
                ):
                    self._pressed_host_positions.discard(position)
                    raise DeviceError(
                        "OBS Studio Mode returned an invalid error"
                    ) from error
                self.emit({
                    "type": "host_action",
                    "event": "failed",
                    "widget": "obs_studio_mode",
                    "page_index": binding.page_index,
                    "slot_index": binding.slot_index,
                    "error": detail,
                })
                return True
            except Exception:
                self._pressed_host_positions.discard(position)
                raise
            if not isinstance(result, ObsStudioModeToggleResult):
                self._pressed_host_positions.discard(position)
                raise DeviceError(
                    "OBS Studio Mode returned an invalid result"
                )
            try:
                result.__post_init__()
            except ValueError as error:
                self._pressed_host_positions.discard(position)
                raise DeviceError(
                    "OBS Studio Mode returned an invalid result"
                ) from error
            self._send_obs_studio_mode_state(result.new_enabled)
            self.emit({
                "type": "host_action",
                "event": "toggled",
                "widget": "obs_studio_mode",
                "page_index": binding.page_index,
                "slot_index": binding.slot_index,
                "old_enabled": result.old_enabled,
                "new_enabled": result.new_enabled,
            })
            return True
        touch = decode_countdown_press(event)
        if touch is None:
            return False
        binding = self._by_position.get((touch.page_index, touch.slot_index))
        if binding is None:
            return False
        timestamp = self.clock() if now is None else now
        if binding.timer_id in self._deadlines:
            del self._deadlines[binding.timer_id]
            self._last_remaining.pop(binding.timer_id, None)
            self._send_updates(self._updates(
                binding.timer_id, state=2,
                remaining=0))
            self.emit({"type": "timer", "event": "stopped",
                       "timer_id": binding.timer_id,
                       "remaining_seconds": 0})
        else:
            duration = self._durations[binding.timer_id]
            self._deadlines[binding.timer_id] = timestamp + duration
            self._last_remaining[binding.timer_id] = duration
            self._send_updates(self._updates(
                binding.timer_id, state=0, remaining=duration))
            self.emit({"type": "timer", "event": "started",
                       "timer_id": binding.timer_id,
                       "remaining_seconds": duration})
        return True

    def advance(self, *, now=None) -> int:
        """Send changed whole-second values; return the number of timer updates."""
        if not self.started:
            raise DeviceError("Start the countdown runtime before advancing it")
        timestamp = self.clock() if now is None else now
        changed = 0
        for timer_id in tuple(sorted(self._deadlines)):
            remaining = max(0, math.ceil(self._deadlines[timer_id] - timestamp))
            if remaining == self._last_remaining[timer_id]:
                continue
            self._last_remaining[timer_id] = remaining
            self._send_updates(self._updates(timer_id, state=1, remaining=remaining))
            changed += 1
            if remaining == 0:
                # The native helper publishes the zero tick, removes the
                # running entry, then publishes state 2 to reset the tile.
                del self._deadlines[timer_id]
                del self._last_remaining[timer_id]
                self._send_updates(self._updates(timer_id, state=2, remaining=0))
                self.emit({"type": "timer", "event": "completed",
                           "timer_id": timer_id, "remaining_seconds": 0})
            else:
                self.emit({"type": "timer", "event": "tick",
                           "timer_id": timer_id,
                           "remaining_seconds": remaining})
        self._service_media_worker()
        if (self.general_media_bindings
                and self._next_media_refresh_at is not None
                and timestamp >= self._next_media_refresh_at
                and self._queue_media_refresh("poll")):
            self._next_media_refresh_at = (
                timestamp + MEDIA_REFRESH_INTERVAL_SECONDS)
        return changed

    def stop(self):
        if not self.started:
            return
        self._stop_media_worker()
        if (not self._deadlines
                and (self.host_action_bindings or self.general_media_bindings
                     or self.obs_launch_bindings
                     or self.obs_screenshot_bindings
                     or self.obs_studio_mode_bindings)):
            # No countdown reset will perform a final state check for a
            # host-only or currently idle combined listener.
            self.guard()
        for timer_id in tuple(sorted(self._deadlines)):
            self._send_updates(self._updates(timer_id, state=2, remaining=0))
        self._deadlines.clear()
        self._last_remaining.clear()
        self._pressed_host_positions.clear()
        self._last_media_display_state = None
        self.started = False
        self.display_may_be_stale = False
        self.emit({"type": "stopped"})


class CountdownRuntime(LcdActionRuntime):
    """Compatibility wrapper for the original countdown-only runtime API."""

    def __init__(self, bindings, transport, emit=lambda value: None, *,
                 guard=lambda: None, clock=time.monotonic):
        if not bindings:
            raise ValueError("At least one countdown binding is required")
        super().__init__(bindings, (), transport, emit, guard=guard, clock=clock)
