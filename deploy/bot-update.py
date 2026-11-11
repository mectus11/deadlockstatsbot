#!/usr/bin/env python3
"""bot-update — safe one-command update for the bot on the OVH game panel.

The panel stores the id of the container it created and only ever starts/restarts *that*
container. Recreating the bot by hand therefore leaves the panel pointing at a dead id, which
is what makes a plain `docker run` such a minefield. This script does the whole dance:

  1. pull the published image
  2. recreate the container from its own current configuration (nothing is hardcoded, so the
     settings cannot drift)
  3. verify the new build actually started
  4. re-point the panel's database at the new container id
  5. tag the outgoing image `<repo>:rollback` so --rollback stays instant

The old container is removed instead of being kept: the panel resolves a server's state from the
`gamepanel.serverId` label, so a second container carrying that label (e.g. `<name>-prev`) makes
its status sync report the server as stopped while the bot is in fact running.

Usage (on the VPS, as root):

    sudo bot-update --dry-run     # show what would happen, change nothing
    sudo bot-update               # update now
    sudo bot-update --wait 600    # poll until a new build lands, then update
    sudo bot-update --rollback    # go back to the previous build

Exit codes: 0 success or already up to date, 1 failure (the previous build is restored).
"""

from __future__ import annotations

import argparse
import json
import os
import sqlite3
import subprocess
import sys
import time
from datetime import datetime

try:  # Unix only; guarded so --help still works when the file is opened on Windows
    import fcntl
except ImportError:  # pragma: no cover
    fcntl = None

DEFAULT_IMAGE = "ghcr.io/mectus11/deadlockstatsbot:latest"
DEFAULT_PANEL_DB = "/opt/gamepanel/data/game-panel.db"
DEFAULT_PANEL_BACKEND = "gamepanel-backend-1"
LOCK_PATH = "/var/lock/bot-update.lock"
LOG_PATH = "/var/log/bot-update.log"
# Older versions kept the outgoing container as `<name>-prev` (and `<name>-failed` after a failed
# rollback). Those names are only used now to clean up leftovers.
PREV_SUFFIX = "-prev"
FAILED_SUFFIX = "-failed"
# Image tag holding the build we last moved away from, so --rollback can recreate it.
ROLLBACK_TAG = "rollback"
SECRET_HINTS = ("TOKEN", "KEY", "SECRET", "PASSWORD")
# Environment that describes the image itself. The new image must win for these, otherwise the
# recreated container keeps reporting the old commit (BUILD_SHA) or an outdated interpreter PATH.
IMAGE_OWNED_ENV = {"BUILD_SHA", "PATH", "LANG", "GPG_KEY", "PYTHON_VERSION", "PYTHON_SHA256"}


class Fail(Exception):
    """Anything that should stop the update and roll back."""


# --- logging ---------------------------------------------------------------
def log(message: str) -> None:
    line = f"{datetime.now():%Y-%m-%d %H:%M:%S}  {message}"
    print(line, flush=True)
    try:
        with open(LOG_PATH, "a", encoding="utf-8") as fh:
            fh.write(line + "\n")
    except OSError:
        pass  # not root, or /var/log not writable: stdout is enough


# --- docker plumbing -------------------------------------------------------
def _run(args: list[str], check: bool = True) -> subprocess.CompletedProcess:
    result = subprocess.run(args, text=True, capture_output=True)
    if check and result.returncode != 0:
        detail = (result.stderr or result.stdout or "").strip()
        raise Fail(f"{args[0]} {args[1] if len(args) > 1 else ''} failed: {detail}")
    return result


def docker(*args, check: bool = True) -> str:
    return _run(["docker", *[str(a) for a in args]], check=check).stdout.strip()


def docker_json(*args) -> list:
    return json.loads(docker(*args) or "[]")


def container_logs(name: str, tail: int = 80) -> str:
    result = _run(["docker", "logs", "--tail", str(tail), name], check=False)
    return (result.stdout or "") + (result.stderr or "")


def exists(name: str) -> bool:
    return _run(["docker", "inspect", name], check=False).returncode == 0


def exists_image(ref: str) -> bool:
    return _run(["docker", "image", "inspect", ref], check=False).returncode == 0


def inspect(name: str) -> dict:
    data = docker_json("inspect", name)
    if not data:
        raise Fail(f"container {name} not found")
    return data[0]


def repo_of(image_ref: str) -> str:
    """'ghcr.io/you/bot:latest' -> 'ghcr.io/you/bot' (registry ports handled)."""
    if "@" in image_ref:
        return image_ref.split("@", 1)[0]
    head, sep, tail = image_ref.rpartition(":")
    return head if sep and "/" not in tail else image_ref


def rollback_ref(image_ref: str) -> str:
    """'ghcr.io/you/bot:latest' -> 'ghcr.io/you/bot:rollback' (the build --rollback restores)."""
    return f"{repo_of(image_ref)}:{ROLLBACK_TAG}"


def parse_env(items: list[str] | None) -> dict[str, str]:
    out: dict[str, str] = {}
    for entry in items or []:
        key, _, value = entry.partition("=")
        out[key] = value
    return out


def image_env(image_ref: str) -> dict[str, str]:
    return parse_env(docker_json("image", "inspect", image_ref)[0]["Config"].get("Env"))


def image_id(image_ref: str) -> str:
    return docker_json("image", "inspect", image_ref)[0]["Id"]


def build_sha(image_ref: str) -> str | None:
    return image_env(image_ref).get("BUILD_SHA")


# --- finding the bot -------------------------------------------------------
def image_config(image_id: str) -> dict:
    if not image_id:
        return {}
    try:
        return docker_json("image", "inspect", image_id)[0]
    except Fail:
        return {}


def is_bot_container(name: str, repo: str) -> bool:
    """Does this container run an image from `repo`?

    `docker ps` only shows one image name per container, and a pull moves the `:latest` tag to
    the newer build, leaving the old image untagged — the container then displays a bare image id
    (e.g. `a493537c9976`). The container's creation reference and the image's own tags/digests
    still name the repo, so those are checked first. If the pull untagged the old image so
    thoroughly that no reference is left, the image's own BUILD_SHA (set by the Dockerfile)
    identifies it.
    """
    info = inspect(name)
    config = image_config(info.get("Image") or "")
    refs = [(info.get("Config") or {}).get("Image") or ""]
    refs += list(config.get("RepoTags") or [])
    refs += list(config.get("RepoDigests") or [])
    if any(repo_of(ref) == repo for ref in refs if ref):
        return True
    return "BUILD_SHA" in parse_env((config.get("Config") or {}).get("Env"))


def find_bot_container(image_ref: str, override: str | None) -> str:
    if override:
        if not exists(override):
            raise Fail(f"--container {override} does not exist")
        return override

    repo = repo_of(image_ref)
    rows = []
    for name in docker("ps", "-a", "--format", "{{.Names}}").splitlines():
        name = name.strip()
        if not name or name.endswith((PREV_SUFFIX, FAILED_SUFFIX)):
            continue
        try:
            if is_bot_container(name, repo):
                rows.append(name)
        except Fail:
            continue  # removed between the listing and the inspect

    if not rows:
        raise Fail(f"no container is using {repo}; pass --container <name>")
    if len(rows) > 1:
        raise Fail("several containers use that image, pick one with --container: "
                   + ", ".join(sorted(rows)))
    return rows[0]


# --- rebuilding the run command from the live container --------------------
def run_args(info: dict, image_ref: str, name: str, defaults: dict[str, str]) -> list[str]:
    """Recreate exactly the container we have, only swapping the image.

    Everything comes from `docker inspect`, so a change made by hand in the panel (memory
    limit, extra env var, another volume) is preserved instead of silently dropped.
    `defaults` is the new image's own environment, used to tell an override from a default.
    """
    config, host = info["Config"], info["HostConfig"]
    args = ["-d", "--name", name]

    if config.get("Hostname"):
        args += ["--hostname", config["Hostname"]]

    args += ["--restart", (host.get("RestartPolicy") or {}).get("Name") or "unless-stopped"]

    short_id = info["Id"][:12]
    for network, data in (info.get("NetworkSettings") or {}).get("Networks", {}).items():
        args += ["--network", network]
        for alias in (data or {}).get("Aliases") or []:
            # docker re-adds the container name and id itself; only keep the extra aliases
            if alias in (name, short_id, info["Id"]):
                continue
            args += ["--network-alias", alias]

    for key, value in (config.get("Labels") or {}).items():
        args += ["--label", f"{key}={value}"]

    if host.get("Memory"):
        args += ["--memory", str(host["Memory"])]
    if host.get("MemorySwap"):
        args += ["--memory-swap", str(host["MemorySwap"])]
    if host.get("NanoCpus"):
        args += ["--cpus", f"{host['NanoCpus'] / 1e9:g}"]

    for bind in host.get("Binds") or []:
        args += ["-v", bind]

    # Keep the container's env, but let the new image supply anything it defines itself with the
    # same value (PYTHONUNBUFFERED, CACHE_DIR...). Overrides still survive because their value
    # differs from the image default; build/base-image keys never survive.
    for key, value in parse_env(config.get("Env")).items():
        if key in IMAGE_OWNED_ENV:
            continue
        if defaults.get(key) != value:
            args += ["-e", f"{key}={value}"]

    if config.get("User"):
        args += ["--user", config["User"]]

    entrypoint = config.get("Entrypoint")
    command = list(config.get("Cmd") or [])
    if entrypoint:
        if isinstance(entrypoint, str):
            entrypoint = [entrypoint]
        args += ["--entrypoint", entrypoint[0]]
        command = list(entrypoint[1:]) + command

    args += [image_ref, *command]
    return args


def mask(args: list[str]) -> list[str]:
    """Hide secret values when printing a command (dry run, logs)."""
    out, hide_next = [], False
    for arg in args:
        if hide_next:
            key, _, value = arg.partition("=")
            out.append(f"{key}=***" if value and any(h in key.upper() for h in SECRET_HINTS) else arg)
            hide_next = False
        else:
            out.append(arg)
            hide_next = arg == "-e"
    return out


# --- panel database --------------------------------------------------------
def patch_panel_db(db_path: str, old_id: str, new_id: str) -> list[tuple[str, str, int]]:
    """Replace the container id the panel remembers with the one that actually exists."""
    old_short, new_short = old_id[:12], new_id[:12]
    con = sqlite3.connect(db_path)
    try:
        cur = con.cursor()
        tables = [row[0] for row in cur.execute(
            "SELECT name FROM sqlite_master WHERE type='table' AND name NOT LIKE 'sqlite_%'")]
        hits: list[tuple[str, str, int]] = []
        for table in tables:
            columns = [row[1] for row in cur.execute(f'PRAGMA table_info("{table}")')]
            for column in columns:
                try:
                    count = cur.execute(
                        f'SELECT COUNT(*) FROM "{table}" WHERE CAST("{column}" AS TEXT) LIKE ?',
                        (f"%{old_short}%",)).fetchone()[0]
                except sqlite3.Error:
                    continue
                if not count:
                    continue
                # full id first, then the short form, so a full id can't become a hybrid
                cur.execute(
                    f'UPDATE "{table}" SET "{column}" = '
                    f'REPLACE(REPLACE(CAST("{column}" AS TEXT), ?, ?), ?, ?) '
                    f'WHERE CAST("{column}" AS TEXT) LIKE ?',
                    (old_id, new_id, old_short, new_short, f"%{old_short}%"))
                hits.append((table, column, count))
        con.commit()
        return hits
    finally:
        con.close()


def stale_references(db_path: str, container_id: str) -> int:
    short = container_id[:12]
    con = sqlite3.connect(f"file:{db_path}?mode=ro", uri=True)
    try:
        cur = con.cursor()
        total = 0
        # materialise the list: running the inner queries on the same cursor would otherwise
        # cut the outer iteration short after the first table
        tables = [row[0] for row in cur.execute("SELECT name FROM sqlite_master WHERE type='table'")]
        for table in tables:
            for column in [row[1] for row in cur.execute(f'PRAGMA table_info("{table}")')]:
                try:
                    total += cur.execute(
                        f'SELECT COUNT(*) FROM "{table}" WHERE CAST("{column}" AS TEXT) LIKE ?',
                        (f"%{short}%",)).fetchone()[0]
                except sqlite3.Error:
                    continue
        return total
    finally:
        con.close()


def snapshot(db_path: str) -> str:
    """Consistent copy via SQLite's own backup API (safe while the panel is running)."""
    target = f"{db_path}.bak-{datetime.now():%Y%m%d-%H%M%S}"
    source = sqlite3.connect(f"file:{db_path}?mode=ro", uri=True)
    dest = sqlite3.connect(target)
    try:
        source.backup(dest)
    finally:
        dest.close()
        source.close()
    return target


def find_panel_backend(preferred: str) -> str | None:
    if exists(preferred):
        return preferred
    for line in docker("ps", "--format", "{{.Names}}\t{{.Image}}").splitlines():
        name, _, image = line.partition("\t")
        if "gamepanel-backend" in image:
            return name
    return None


# --- health ----------------------------------------------------------------
def wait_healthy(name: str, expected_sha: str | None, expected_image: str | None,
                 timeout: int) -> None:
    deadline = time.time() + timeout
    state: dict = {}
    while time.time() < deadline:
        try:
            state = inspect(name)["State"]
        except Fail:
            time.sleep(2)
            continue
        if state.get("Running"):
            break
        time.sleep(2)
    else:
        raise Fail(f"{name} did not start within {timeout}s (last state: {state.get('Status')})")

    time.sleep(3)  # let an immediate crash surface before declaring victory
    state = inspect(name)["State"]
    if not state.get("Running"):
        raise Fail(f"{name} exited right after starting (exit {state.get('ExitCode')})")

    # only meaningful for a freshly created container: a restored one keeps its old restart count
    if expected_sha and state.get("RestartCount"):
        raise Fail(f"{name} restarted {state['RestartCount']} time(s) right after starting "
                   f"(last exit {state.get('ExitCode')})")

    info = inspect(name)
    if expected_image and info.get("Image") != expected_image:
        raise Fail(f"{name} is running image {info.get('Image')}, expected {expected_image}")

    if expected_sha and expected_sha != "dev":
        logs = container_logs(name)
        if f"build {expected_sha}" not in logs:
            tail = "\n".join(logs.strip().splitlines()[-15:])
            raise Fail(f"logs do not show 'build {expected_sha}'. Last lines:\n{tail}")


# --- the two operations ----------------------------------------------------
def do_update(args) -> int:
    name = find_bot_container(args.image, args.container)
    current = inspect(name)
    old_id = current["Id"]
    old_image = current["Image"]
    old_sha = parse_env(current["Config"].get("Env")).get("BUILD_SHA") or "unknown"
    log(f"bot container: {name} ({old_id[:12]}, build {old_sha[:12]})")

    # 1. fetch the image, optionally waiting for a build that isn't published yet
    deadline = time.time() + args.wait
    while True:
        log(f"pulling {args.image}")
        docker("pull", args.image)
        new_sha = build_sha(args.image) or "dev"
        if new_sha != old_sha or args.wait <= 0:
            break
        if time.time() >= deadline:
            log(f"no new build after {args.wait}s (still {old_sha[:12]}); "
                "has the GitHub Action finished?")
            return 0
        log(f"still {old_sha[:12]}, retrying in 15s")
        time.sleep(15)

    new_image_id = image_id(args.image)
    if new_sha == old_sha:
        log(f"already up to date (build {old_sha[:12]})")
        return 0

    log(f"new build: {new_sha[:12]}")
    new_args = run_args(current, args.image, name, image_env(args.image))
    rollback = rollback_ref(args.image)

    # 2. dry run: show the plan, touch nothing
    if args.dry_run:
        log("dry run — would run:")
        print("  docker run " + " ".join(mask(new_args)))
        log(f"dry run — would keep the current image as {rollback} for --rollback")
        if args.panel_db and os.path.exists(args.panel_db):
            log(f"dry run — would re-point {args.panel_db}: {old_id[:12]} -> "
                "the new container's id (assigned when it is created)")
        log("dry run — nothing changed")
        return 0

    # 3. keep the outgoing build reachable for --rollback. The container itself is removed: the
    # panel finds a server's container by its `gamepanel.serverId` label, and a second labelled
    # container left behind makes its status sync report the server as stopped.
    docker("tag", old_image, rollback)
    log(f"previous build kept as {rollback} ({old_image[:12]})")

    # 4. swap the containers. The panel backend is stopped for the whole swap: if it were left
    # running it could restart the old container by id while we are replacing it, ending up with
    # two bots connected to Discord at once.
    backend = _panel_backend(args)
    _stop_backend(backend)
    try:
        _remove_legacy(name)
        log("starting the new container")
        docker("stop", name)
        docker("rm", name)
        docker("run", *new_args)
        new_id = inspect(name)["Id"]
        wait_healthy(name, new_sha, new_image_id, args.timeout)
        log(f"new container is healthy: {name} ({new_id[:12]})")

        # 5. point the panel at the new id, only now that the bot is proven working. If this
        # fails the container swap is undone too, so the panel's stored id stays valid.
        if args.panel_db:
            _patch_panel(args, old_id, new_id)
    except Exception:
        log("update failed — rolling back")
        _restore(name, old_image, current, old_sha, args.timeout)
        raise
    finally:
        _start_backend(backend)

    log(f"done: {name} now runs build {new_sha[:12]}")
    log("rollback with: sudo bot-update --rollback")
    return 0


def _remove_legacy(name: str) -> None:
    """Delete containers left behind by older versions of this script.

    They carry the same `gamepanel.serverId` label as the live container, so the panel's status
    sync can pick the exited one and show the server as stopped although the bot is running.
    """
    for legacy in (name + PREV_SUFFIX, name + FAILED_SUFFIX):
        if exists(legacy):
            docker("rm", "-f", legacy)
            log(f"removed leftover {legacy} (it would confuse the panel's status sync)")


def _restore(name: str, image_ref: str, template: dict, expected_sha: str | None = None,
             timeout: int = 45) -> None:
    """Recreate the bot on `image_ref` from the configuration captured before the swap."""
    docker("rm", "-f", name, check=False)
    docker("run", *run_args(template, image_ref, name, image_env(image_ref)))
    if expected_sha in (None, "unknown", "dev"):
        expected_sha = None
    wait_healthy(name, expected_sha, image_id(image_ref), timeout)
    log(f"restored {name} on {image_ref}")


def _panel_backend(args) -> str | None:
    if not args.panel_db:
        return None
    backend = find_panel_backend(args.panel_backend)
    if backend is None:
        log(f"warning: no {args.panel_backend} container found; the panel is not being stopped")
    return backend


def _stop_backend(backend: str | None) -> None:
    if backend:
        log(f"stopping {backend} so it cannot restart the old container")
        docker("stop", backend)


def _start_backend(backend: str | None) -> None:
    if backend:
        docker("start", backend)
        log(f"{backend} restarted")


def _patch_panel(args, old_id: str, new_id: str) -> None:
    db = args.panel_db
    if not os.path.exists(db):
        log(f"warning: {db} not found, panel not updated (use --no-panel to silence)")
        return

    backup = snapshot(db)
    log(f"panel database backed up to {backup}")

    hits = patch_panel_db(db, old_id, new_id)
    for table, column, count in hits:
        log(f"panel: {table}.{column} updated ({count} row(s))")
    if not hits:
        log("panel: no stored reference to the old container id was found")

    left = stale_references(db, old_id)
    if left:
        log(f"warning: {left} stale reference(s) to {old_id[:12]} remain in the panel database")
    else:
        log("panel database is clean")


def do_rollback(args) -> int:
    name = find_bot_container(args.image, args.container)
    target = rollback_ref(args.image)
    if not exists_image(target):
        raise Fail(f"nothing to roll back to ({target} does not exist); "
                   "run sudo bot-update once to establish it")

    target_id = image_id(target)
    target_sha = build_sha(target) or "unknown"
    current = inspect(name)
    current_id, current_image = current["Id"], current["Image"]
    log(f"rolling {name} back to {target_id[:12]} (build {target_sha[:12]})")
    new_args = run_args(current, target, name, image_env(target))

    if args.dry_run:
        log("dry run — would run:")
        print("  docker run " + " ".join(mask(new_args)))
        log("dry run — nothing changed")
        return 0

    backend = _panel_backend(args)
    _stop_backend(backend)
    try:
        _remove_legacy(name)
        docker("stop", name)
        docker("rm", name)
        docker("run", *new_args)
        wait_healthy(name, None if target_sha == "unknown" else target_sha,
                     target_id, args.timeout)
        # the build we just left becomes the rollback target, so --rollback can toggle back
        docker("tag", current_image, target)
        if args.panel_db:
            _patch_panel(args, current_id, target_id)
    except Exception:
        log("rollback target failed to start — restoring the current build")
        _restore(name, current_image, current, timeout=args.timeout)
        raise
    finally:
        _start_backend(backend)

    log(f"rolled back to build {target_sha[:12]}")
    return 0


# --- entry point -----------------------------------------------------------
def parse_args(argv: list[str] | None = None) -> argparse.Namespace:
    parser = argparse.ArgumentParser(
        prog="bot-update",
        description="Pull, recreate and re-register the bot on the OVH game panel.")
    parser.add_argument("--image", default=DEFAULT_IMAGE, help=f"image to deploy (default: {DEFAULT_IMAGE})")
    parser.add_argument("--container", help="container name, if auto-detection is ambiguous")
    parser.add_argument("--panel-db", default=DEFAULT_PANEL_DB,
                        help=f"panel SQLite database (default: {DEFAULT_PANEL_DB})")
    parser.add_argument("--panel-backend", default=DEFAULT_PANEL_BACKEND,
                        help=f"panel backend container (default: {DEFAULT_PANEL_BACKEND})")
    parser.add_argument("--no-panel", dest="panel_db", action="store_const", const=None,
                        help="do not touch the panel database")
    parser.add_argument("--wait", type=int, default=0, metavar="SECONDS",
                        help="poll the registry until a new build appears (0 = do not wait)")
    parser.add_argument("--timeout", type=int, default=45, metavar="SECONDS",
                        help="how long to wait for the new container to become healthy")
    parser.add_argument("--dry-run", action="store_true", help="show the plan, change nothing")
    parser.add_argument("--rollback", action="store_true",
                        help="recreate the bot from the previous build")
    return parser.parse_args(argv)


def main(argv: list[str] | None = None) -> int:
    args = parse_args(argv)

    if hasattr(os, "geteuid") and os.geteuid() != 0:
        print("bot-update must run as root (docker + the panel database): try sudo bot-update",
              file=sys.stderr)
        return 1

    lock_handle = None
    try:
        if fcntl is not None:
            lock_handle = open(LOCK_PATH, "w")
            fcntl.flock(lock_handle, fcntl.LOCK_EX | fcntl.LOCK_NB)
        return do_rollback(args) if args.rollback else do_update(args)
    except BlockingIOError:
        print("another bot-update is already running", file=sys.stderr)
        return 1
    except Fail as exc:
        log(f"error: {exc}")
        return 1
    except KeyboardInterrupt:
        log("interrupted")
        return 1
    finally:
        if lock_handle is not None:
            lock_handle.close()


if __name__ == "__main__":
    sys.exit(main())
