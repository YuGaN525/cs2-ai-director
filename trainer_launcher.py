"""Launch the bundled training workspace without requiring a second installation."""
from pathlib import Path
import subprocess
import sys


def default_entry_arguments(arguments):
    """The internal trainer asset shares the exact director binary and updater."""
    if (not arguments and getattr(sys, 'frozen', False)
            and Path(sys.executable).name.casefold() == 'yuzi_cs2_aidecisiontrainer.exe'):
        return ['--trainer']
    return arguments


def trainer_command():
    if getattr(sys, "frozen", False):
        return [sys.executable, "--trainer"]
    return [sys.executable, str(Path(__file__).with_name("cs2_ai_director.py")), "--trainer"]


def open_trainer(current=None):
    if current is not None and current.poll() is None:
        return current
    options = {"cwd": str(Path(sys.executable).parent if getattr(sys, "frozen", False)
                          else Path(__file__).parent)}
    if sys.platform == "win32":
        options["creationflags"] = subprocess.CREATE_NO_WINDOW | subprocess.BELOW_NORMAL_PRIORITY_CLASS
    return subprocess.Popen(trainer_command(), **options)
