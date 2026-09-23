"""
main.py — Project entry point.

Configures logging for the whole application, then asks whether
you want to start the CLI or the GUI, and launches accordingly.

Usage:
    python main.py            # interactive prompt
    python main.py --cli      # skip prompt, go straight to CLI
    python main.py --gui       # skip prompt, go straight to GUI
    python main.py --debug    # enable DEBUG-level logging (any mode)
"""

import sys
import logging
import argparse


# -----------------------------------
# LOGGING SETUP
# -----------------------------------
# This is the ONE place in the project that calls basicConfig.
# Every other module (backends.py, etc.) just does:
#   logger = logging.getLogger(__name__)
# and this config flows to them automatically.

def setup_logging(debug: bool = False) -> None:
    level = logging.DEBUG if debug else logging.INFO

    logging.basicConfig(
        level=level,
        format="%(asctime)s [%(name)-12s] %(levelname)-8s %(message)s",
        datefmt="%Y-%m-%d %H:%M:%S",
    )

    # Silence overly verbose third-party loggers even in debug mode.
    logging.getLogger("urllib3").setLevel(logging.WARNING)
    logging.getLogger("httpx").setLevel(logging.WARNING)
    logging.getLogger("yfinance").setLevel(logging.WARNING)
    logging.getLogger("peewee").setLevel(logging.WARNING)

    # Silence other modules if not necessary. For example, if you find backends.py too noisy:
    logging.getLogger("backends").setLevel(logging.WARNING)


# -----------------------------------
# MODE SELECTION
# -----------------------------------

def ask_mode() -> str:
    """Prompt the user to choose CLI or GUI. Returns 'cli' or 'gui'."""
    print()
    print("  How do you want to start?")
    print("  [1] CLI  — terminal interface")
    print("  [2] GUI   — graphical / web interface")
    print()

    while True:
        choice = input("  Enter 1 or 2: ").strip()
        if choice == "1":
            return "cli"
        if choice == "2":
            return "gui"
        print("  Please enter 1 or 2.")


def launch_cli() -> None:
    logging.getLogger(__name__).info("Starting CLI mode")
    from cli import main  # adjust import to match your cli.py structure
    main()


def launch_gui() -> None:
    logging.getLogger(__name__).info("Starting GUI mode")
    from gui import main  # adjust import to match your gui.py structure
    main()


# -----------------------------------
# ENTRY POINT
# -----------------------------------

def main() -> None:
    parser = argparse.ArgumentParser(description="Project launcher")
    group = parser.add_mutually_exclusive_group()
    group.add_argument("--cli", action="store_true", help="Launch CLI directly")
    group.add_argument("--gui",  action="store_true", help="Launch GUI directly")
    parser.add_argument("--debug", action="store_true", help="Enable DEBUG logging")
    args = parser.parse_args()

    setup_logging(debug=args.debug)
    log = logging.getLogger(__name__)
    log.debug("Logging initialised")

    # Determine mode
    if args.cli:
        mode = "cli"
    elif args.gui:
        mode = "gui"
    else:
        mode = ask_mode()

    # Launch
    if mode == "cli":
        launch_cli()
    else:
        launch_gui()


if __name__ == "__main__":
    main()