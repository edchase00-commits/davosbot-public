"""Run the reviewed release watch against the unchanged, pinned Mini runtime."""

import hashlib
import importlib
import importlib.util
from pathlib import Path
import subprocess
import sys


RUNTIME_ROOT = Path("/Users/<you>/projects/davosbot")
RUNTIME_REVISION = "6a1f3b814383c116b4db4677610b6830613819b4"
WATCH_FILES = {
    "davosbot/platinum_emerald_watch.py": "53ab6e8a60fba829b9ef88c309df1ba2346f5c7628a08c7ef8f5d8b4c81e5393",
    "scripts/check_platinum_emerald.py": "5dbeca12cf1e25f311c3b6630e9862e8550de2a2b0814318a9b2dca65cfb25ca",
}


class BootstrapError(RuntimeError):
    pass


def _verify_runtime(root, revision):
    def git(*args):
        return subprocess.run(
            ["/usr/bin/git", "-C", str(root), *args], check=True,
            capture_output=True, text=True, timeout=15,
        ).stdout.strip()
    if git("rev-parse", "HEAD") != revision:
        raise BootstrapError("runtime_revision_mismatch")
    if git("status", "--porcelain", "--untracked-files=no"):
        raise BootstrapError("runtime_tracked_files_changed")


def _load_file(name, path):
    spec = importlib.util.spec_from_file_location(name, path)
    module = importlib.util.module_from_spec(spec)
    sys.modules[name] = module
    spec.loader.exec_module(module)
    return module


def _load_entrypoint(runtime_root, artifact_root, revision, hashes):
    """Internal arguments permit isolated fixture tests, not CLI overrides."""
    sys.dont_write_bytecode = True
    runtime_root, artifact_root = runtime_root.resolve(), artifact_root.resolve()
    _verify_runtime(runtime_root, revision)
    for relative, expected in hashes.items():
        path = artifact_root / relative
        if path.is_symlink() or hashlib.sha256(path.read_bytes()).hexdigest() != expected:
            raise BootstrapError("watch_artifact_mismatch")
    if any(name == "davosbot" or name.startswith("davosbot.") for name in sys.modules):
        raise BootstrapError("fresh_interpreter_required")

    sys.path.insert(0, str(runtime_root))
    package = importlib.import_module("davosbot")
    expected_package = runtime_root / "davosbot"
    if [Path(path).resolve() for path in package.__path__] != [expected_package]:
        raise BootstrapError("runtime_package_path_mismatch")
    for name in ("__init__", "config", "runtime_locks", "db", "reminder_tools"):
        module = package if name == "__init__" else importlib.import_module("davosbot." + name)
        if Path(module.__file__).resolve() != expected_package / (name + ".py"):
            raise BootstrapError("runtime_module_origin_mismatch")
    if Path(sys.modules["davosbot.config"].PROJECT_ROOT).resolve() != runtime_root:
        raise BootstrapError("runtime_project_root_mismatch")

    watch = _load_file("davosbot.platinum_emerald_watch", artifact_root / "davosbot/platinum_emerald_watch.py")
    package.platinum_emerald_watch = watch
    saved_path = sys.path[:]
    try:
        entrypoint = _load_file("_standalone_platinum_entrypoint", artifact_root / "scripts/check_platinum_emerald.py")
    finally:
        # The original CLI prepends its root. Never retain an external package overlay.
        sys.path[:] = saved_path
    _verify_runtime(runtime_root, revision)
    return entrypoint


def main(argv=None):
    if sys.platform != "darwin" or not sys.flags.isolated:
        print("isolated_mini_interpreter_required", file=sys.stderr)
        return 2
    try:
        entrypoint = _load_entrypoint(RUNTIME_ROOT, Path(__file__).resolve().parents[1],
                                     RUNTIME_REVISION, WATCH_FILES)
        return entrypoint.main(argv)
    except BootstrapError as exc:
        print(str(exc), file=sys.stderr)
    except Exception:
        print("standalone_watch_failed", file=sys.stderr)
    return 2


if __name__ == "__main__":
    raise SystemExit(main())
