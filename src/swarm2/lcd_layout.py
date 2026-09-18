"""Rearrange supported onboard pages without separating wide widgets."""

from copy import deepcopy

from .configuration import Configuration, ConfigurationError
from .lcd_commands import LCD_HOST_ACTION_WIDGETS, LCD_WIDGETS


def move_page(pages: list, source: int, destination: int, *,
              key_bindings: list | None = None,
              macro_bindings: list | None = None,
              macros: list | None = None,
              timer_bindings: list | None = None,
              countdown_timers: list | None = None,
              host_action_bindings: list | None = None,
              host_action_icon_bindings: list | None = None) -> list:
    """Return a reordered draft; unknown position-dependent widgets stay put."""
    draft = Configuration()
    draft.display.pages = deepcopy(pages)
    if key_bindings is not None:
        draft.display.key_bindings = deepcopy(key_bindings)
    if macro_bindings is not None:
        draft.display.macro_bindings = deepcopy(macro_bindings)
    if macros is not None:
        draft.macros = deepcopy(macros)
    if timer_bindings is not None:
        draft.display.timer_bindings = deepcopy(timer_bindings)
    if countdown_timers is not None:
        draft.countdown_timers = deepcopy(countdown_timers)
    if host_action_bindings is not None:
        draft.display.host_action_bindings = deepcopy(host_action_bindings)
    if host_action_icon_bindings is not None:
        draft.display.host_action_icon_bindings = deepcopy(
            host_action_icon_bindings)
    draft.validate()
    for index in (source, destination):
        if type(index) is not int or not 0 <= index < len(pages):
            raise ConfigurationError("Choose an existing LCD page to move")
    if source != destination:
        first, last = min(source, destination), max(source, destination) + 1
        for page in pages[first:last]:
            if any(key is not None and key not in LCD_WIDGETS for key in page):
                raise ConfigurationError("Pages with unrecognized widgets cannot be moved; their position may be part of the widget configuration")
        for page_index in range(first, last):
            for cell, key in enumerate(pages[page_index]):
                if key == "macro":
                    resolved = (
                        macro_bindings is not None
                        and page_index < len(macro_bindings)
                        and cell < len(macro_bindings[page_index])
                        and macro_bindings[page_index][cell] is not None
                    )
                    if not resolved:
                        raise ConfigurationError(
                            "Pages with an unreadable LCD macro cannot be moved; its raw slot is tied to this position")
                elif key in LCD_HOST_ACTION_WIDGETS:
                    resolved = (
                        host_action_bindings is not None
                        and page_index < len(host_action_bindings)
                        and cell < len(host_action_bindings[page_index])
                        and host_action_bindings[page_index][cell] is not None
                    )
                    if not resolved:
                        raise ConfigurationError(
                            "Pages with an unresolved host action cannot be moved; its raw slot is tied to this position")
        if key_bindings is not None and any(
                isinstance(binding, str) and binding.startswith("device:")
                for row in key_bindings[first:last] for binding in row):
            raise ConfigurationError("Pages with unrecognized key data cannot be moved; its position is part of the device setting")
    result = deepcopy(pages)
    result.insert(destination, result.pop(source))
    return result
