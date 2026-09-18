"""Optional Qt desktop interface for the MC7 configurator."""


def main() -> int:
    """Start Qt, leaving command-line discovery usable without the GUI extra."""
    try:
        from .app import run
    except ImportError as error:
        if error.name and error.name.startswith("PySide6"):
            import sys
            print("The desktop interface needs PySide6. Install with: pip install '.[gui]'", file=sys.stderr)
            return 2
        raise
    return run()
