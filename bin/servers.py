#!/usr/bin/env python3
"""List and stop local servers (listening TCP sockets) for the Omarchy bar.

Stdlib only. Reads /proc directly so it needs no root and no ss/lsof.

  servers.py list            JSON: {"servers": [...], "system": [...]}
  servers.py stop <pid> [start-ticks]
                             SIGTERM the server and its supervisors, SIGKILL
                             after a grace period; start-ticks guards pid reuse
  servers.py stop-container <id>   docker stop <id>
"""

import ipaddress
import json
import os
import re
import shutil
import signal
import subprocess
import sys
import time

PROC = os.environ.get("SERVERS_PROC", "/proc")
HOME = os.path.expanduser("~")
LISTEN = "0A"
STOP_GRACE_SEC = 3.0

# (pattern on the full command line, label, nerd-font icon). First match wins.
KNOWN = [
    (r"\bvite\b", "Vite dev server", "󰹭"),
    (r"\bnext[- ](dev|start|server)\b|next-router-worker|/next/dist/", "Next.js", "󰰓"),
    (r"\bnuxt\b", "Nuxt", "󱄆"),
    (r"\bastro\b", "Astro", "󰑣"),
    (r"\bsvelte-kit\b|\bsveltekit\b", "SvelteKit", "󰚗"),
    (r"\bremix\b", "Remix", "󰜫"),
    (r"webpack(-dev-server)?|\breact-scripts\b", "Webpack dev server", "󰜫"),
    (r"\bparcel\b", "Parcel", "󰏗"),
    (r"\bstorybook\b", "Storybook", "󰂺"),
    (r"\bexpo\b|\bmetro\b", "Expo / Metro", "󰄜"),
    (r"\bhugo\b.*\bserver\b|\bhugo server\b", "Hugo server", "󰉋"),
    (r"\bjekyll\b", "Jekyll", "󰉋"),
    (r"\beleventy\b|@11ty", "Eleventy", "󰉋"),
    (r"\brails\b|\bpuma\b", "Rails", "󰴭"),
    (r"manage\.py\s+runserver", "Django", "󰌠"),
    (r"\buvicorn\b", "Uvicorn (ASGI)", "󰌠"),
    (r"\bgunicorn\b", "Gunicorn", "󰌠"),
    (r"\bflask\b", "Flask", "󰌠"),
    (r"\bfastapi\b", "FastAPI", "󰌠"),
    (r"\bstreamlit\b", "Streamlit", "󰌠"),
    (r"\bjupyter", "Jupyter", "󰠮"),
    (r"http\.server", "Python http.server", "󰌠"),
    (r"\bphp\b.*\s-S\s", "PHP built-in server", "󰌟"),
    (r"\bollama\b", "Ollama", "󰚩"),
    (r"\bpostgres\b|\bpostmaster\b", "PostgreSQL", "󰆼"),
    (r"\bmysqld\b|\bmariadbd\b", "MySQL / MariaDB", "󰆼"),
    (r"\bredis-server\b", "Redis", "󰆼"),
    (r"\bmongod\b", "MongoDB", "󰆼"),
    (r"\bnginx\b", "nginx", "󰖟"),
    (r"\bcaddy\b", "Caddy", "󰖟"),
    (r"\bt3code\b", "T3 Code", "󰚩"),
    (r"\bcodex\b", "Codex", "󰚩"),
    (r"\bclaude\b", "Claude Code", "󰚩"),
]
# Generic runtimes, matched on the executable name only so a path or argument
# that merely mentions "node" or "go" does not mislabel the process.
RUNTIMES = [
    (r"^(node|nodejs|npm|npx|pnpm|yarn)$", "Node.js", "󰎙"),
    (r"^bun$", "Bun", "󰎙"),
    (r"^deno$", "Deno", "󰎙"),
    (r"^python[0-9.]*$", "Python", "󰌠"),
    (r"^ruby[0-9.]*$", "Ruby", "󰴭"),
    (r"^java$", "Java", "󰬷"),
    (r"^php[0-9.]*$", "PHP", "󰌟"),
]
_KNOWN = [(re.compile(p), label, icon) for p, label, icon in KNOWN]
_RUNTIMES = [(re.compile(p), label, icon) for p, label, icon in RUNTIMES]
DEFAULT_ICON = "󰒋"


def classify(cmdline, comm=""):
    """Return (label, icon) for a process command line."""
    text = cmdline or comm or ""
    for rx, label, icon in _KNOWN:
        if rx.search(text):
            return label, icon
    words = text.split()
    exe = os.path.basename(words[0]) if words else ""
    for name in (exe, comm):
        for rx, label, icon in _RUNTIMES:
            if name and rx.match(name):
                script = script_name(words[1:])
                return (label + " · " + script if script else label), icon
    return (comm or exe or "Server"), DEFAULT_ICON


GENERIC_SCRIPTS = {"server", "app", "main", "index", "run", "serve", "start", "dev", "cli", "__main__"}


def script_name(args):
    """First non-flag argument as a short name, e.g. "todo-api/server.py"."""
    skip_next = False
    for arg in args:
        if skip_next:
            skip_next = False
            continue
        if arg in ("-m",):
            skip_next = False
            continue
        if arg in ("-c", "-e", "--eval"):
            return ""
        if arg.startswith("-"):
            continue
        base = os.path.basename(arg.rstrip("/"))
        stem = base.rsplit(".", 1)[0]
        parent = os.path.basename(os.path.dirname(arg.rstrip("/")))
        if stem in GENERIC_SCRIPTS and parent:
            return parent + "/" + base
        return base
    return ""


def _hex_ip(hexaddr):
    if len(hexaddr) == 8:  # IPv4, little-endian
        b = bytes.fromhex(hexaddr)[::-1]
        return ".".join(str(x) for x in b)
    # IPv6: four little-endian 32-bit words
    raw = bytes.fromhex(hexaddr)
    words = b"".join(raw[i:i + 4][::-1] for i in range(0, 16, 4))
    groups = [words[i:i + 2].hex() for i in range(0, 16, 2)]
    if groups[:6] == ["0000"] * 5 + ["ffff"]:  # v4-mapped
        return ".".join(str(x) for x in words[12:])
    return str(ipaddress.IPv6Address(words))


def parse_proc_net(text, family):
    """Parse /proc/net/tcp{,6} text into listening socket dicts."""
    out = []
    for line in text.splitlines()[1:]:
        parts = line.split()
        if len(parts) < 10 or parts[3] != LISTEN:
            continue
        addr_hex, port_hex = parts[1].split(":")
        out.append({
            "address": _hex_ip(addr_hex),
            "port": int(port_hex, 16),
            "uid": int(parts[7]),
            "inode": parts[9],
            "family": family,
        })
    return out


def read_listeners():
    socks = []
    for name, fam in (("tcp", "ipv4"), ("tcp6", "ipv6")):
        try:
            with open(os.path.join(PROC, "net", name)) as fh:
                socks += parse_proc_net(fh.read(), fam)
        except OSError:
            pass
    return socks


def inode_to_pid(wanted):
    """Map socket inodes -> pid by scanning /proc/<pid>/fd (own processes only)."""
    found = {}
    if not wanted:
        return found
    for entry in os.listdir(PROC):
        if not entry.isdigit():
            continue
        fd_dir = os.path.join(PROC, entry, "fd")
        try:
            fds = os.listdir(fd_dir)
        except OSError:
            continue
        for fd in fds:
            try:
                target = os.readlink(os.path.join(fd_dir, fd))
            except OSError:
                continue
            if target.startswith("socket:["):
                inode = target[8:-1]
                if inode in wanted and inode not in found:
                    found[inode] = int(entry)
    return found


def _read(path, binary=False):
    try:
        with open(path, "rb" if binary else "r") as fh:
            return fh.read()
    except OSError:
        return b"" if binary else ""


def _boot_time():
    for line in _read(os.path.join(PROC, "stat")).splitlines():
        if line.startswith("btime "):
            return int(line.split()[1])
    return 0


def proc_stat(pid):
    """state/ppid/pgid/sid/start ticks from /proc/<pid>/stat, or None."""
    stat = _read(os.path.join(PROC, str(pid), "stat"))
    if not stat:
        return None
    # comm may contain spaces or parens; everything after the last ")" is fixed.
    fields = stat.rsplit(")", 1)[-1].split()
    try:
        return {"state": fields[0], "ppid": int(fields[1]), "pgid": int(fields[2]),
                "sid": int(fields[3]), "ticks": int(fields[19])}
    except (IndexError, ValueError):
        return None


def process_info(pid, boot_time=None, clk_tck=None):
    base = os.path.join(PROC, str(pid))
    raw = _read(os.path.join(base, "cmdline"), binary=True)
    args = [a.decode(errors="replace") for a in raw.split(b"\0") if a]
    comm = _read(os.path.join(base, "comm")).strip()
    try:
        cwd = os.readlink(os.path.join(base, "cwd"))
    except OSError:
        cwd = ""
    started = ticks = 0
    st = proc_stat(pid)
    if st:
        ticks = st["ticks"]
        bt = boot_time if boot_time is not None else _boot_time()
        hz = clk_tck or os.sysconf("SC_CLK_TCK")
        started = bt + ticks // hz
    return {"pid": pid, "comm": comm, "args": args, "cwd": cwd,
            "started": started, "ticks": ticks}


def pretty_path(path):
    if not path:
        return ""
    if path == HOME:
        return "~"
    if path.startswith(HOME + "/"):
        return "~" + path[len(HOME):]
    return path


def short_command(args, limit=90):
    if not args:
        return ""
    parts = [os.path.basename(args[0])] + [
        pretty_path(a) if a.startswith("/") else a for a in args[1:]
    ]
    cmd = " ".join(parts)
    return cmd if len(cmd) <= limit else cmd[: limit - 1] + "…"


def is_exposed(addresses):
    return any(a in ("0.0.0.0", "::") or not (a.startswith("127.") or a == "::1")
               for a in addresses)


def docker_containers():
    """Published host port -> container dict. Empty when docker is unavailable."""
    if not shutil.which("docker"):
        return {}
    try:
        res = subprocess.run(
            ["docker", "ps", "--format", "{{json .}}"],
            capture_output=True, text=True, timeout=2,
        )
    except (OSError, subprocess.TimeoutExpired):
        return {}
    if res.returncode != 0:
        return {}
    by_port = {}
    for line in res.stdout.splitlines():
        try:
            c = json.loads(line)
        except ValueError:
            continue
        for port in published_ports(c.get("Ports", "")):
            by_port[port] = c
    return by_port


def published_ports(ports):
    """Host ports from a `docker ps` Ports column, ranges included."""
    out = []
    for m in re.finditer(r":(\d+)(?:-(\d+))?->", ports or ""):
        first = int(m.group(1))
        last = int(m.group(2) or first)
        out += [p for p in range(first, min(last, first + 1024) + 1) if p not in out]
    return out


def collect(uid=None, now=None):
    uid = os.getuid() if uid is None else uid
    now = time.time() if now is None else now
    socks = read_listeners()
    pids = inode_to_pid({s["inode"] for s in socks})
    boot, hz = _boot_time(), os.sysconf("SC_CLK_TCK")
    containers = docker_containers()

    groups, system, seen_containers = {}, {}, {}
    for s in socks:
        pid = pids.get(s["inode"])
        container = containers.get(s["port"])
        if container and pid is None:
            cid = container.get("ID", "")
            g = seen_containers.setdefault(cid, {
                "key": "container:" + cid,
                "kind": "container",
                "id": cid,
                "label": container.get("Names", "container"),
                "icon": "󰡨",
                "detail": "Docker · " + container.get("Image", ""),
                "command": container.get("Command", "").strip('"'),
                "cwd": "",
                "uptime": container.get("RunningFor", ""),
                "ports": [], "addresses": [],
                "stoppable": True,
            })
        elif pid is not None:
            g = groups.get(pid)
            if g is None:
                info = process_info(pid, boot, hz)
                cmdline = " ".join(info["args"])
                label, icon = classify(cmdline, info["comm"])
                g = groups[pid] = {
                    "key": "pid:%d" % pid,
                    "kind": "process",
                    "pid": pid,
                    # Identifies this exact process so `stop` can't hit a
                    # recycled pid.
                    "startTicks": info["ticks"],
                    "label": label,
                    "icon": icon,
                    "name": info["comm"],
                    "detail": pretty_path(info["cwd"]),
                    "command": short_command(info["args"]),
                    "cwd": pretty_path(info["cwd"]),
                    "uptime": human_duration(now - info["started"]) if info["started"] else "",
                    "ports": [], "addresses": [],
                    "stoppable": s["uid"] == uid,
                    "alsoStops": [_comm(p) for p in stop_targets(pid)[1:]] if s["uid"] == uid else [],
                }
        else:
            g = system.setdefault(s["port"], {
                "key": "port:%d" % s["port"],
                "kind": "system",
                "label": WELL_KNOWN_PORTS.get(s["port"], "System service"),
                "icon": "󰒋",
                "detail": "owned by " + user_name(s["uid"]),
                "command": "",
                "cwd": "",
                "uptime": "",
                "ports": [], "addresses": [],
                "stoppable": False,
            })
        if s["port"] not in g["ports"]:
            g["ports"].append(s["port"])
        if s["address"] not in g["addresses"]:
            g["addresses"].append(s["address"])

    def finish(items):
        for g in items:
            g["ports"].sort()
            g["exposed"] = is_exposed(g["addresses"])
            g["url"] = "http://localhost:%d" % g["ports"][0] if g["ports"] else ""
        return sorted(items, key=lambda g: g["ports"][0] if g["ports"] else 0)

    return {
        "servers": finish(list(groups.values()) + list(seen_containers.values())),
        "system": finish(list(system.values())),
    }


WELL_KNOWN_PORTS = {
    22: "SSH", 25: "SMTP", 53: "DNS resolver", 80: "HTTP", 111: "rpcbind",
    443: "HTTPS", 631: "CUPS printing", 5353: "mDNS", 3306: "MySQL",
    5432: "PostgreSQL", 6379: "Redis", 27017: "MongoDB", 11434: "Ollama",
}


def user_name(uid):
    try:
        import pwd
        return pwd.getpwuid(uid).pw_name
    except (KeyError, ImportError):
        return str(uid)


def human_duration(seconds):
    seconds = max(0, int(seconds))
    if seconds < 60:
        return "%ds" % seconds
    minutes = seconds // 60
    if minutes < 60:
        return "%dm" % minutes
    hours, minutes = divmod(minutes, 60)
    if hours < 24:
        return "%dh %dm" % (hours, minutes) if minutes else "%dh" % hours
    days, hours = divmod(hours, 24)
    return "%dd %dh" % (days, hours) if hours else "%dd" % days


SHELLS = {"bash", "zsh", "fish", "sh", "dash", "ksh", "tcsh", "csh", "nu", "xonsh", "elvish"}


def _comm(pid):
    return _read(os.path.join(PROC, str(pid), "comm")).strip()


def _uid_of(pid):
    try:
        return os.stat(os.path.join(PROC, str(pid))).st_uid
    except OSError:
        return -1


# Parents that exist to run and respawn a dev server. Stop climbs only into
# these, so it can never take out the editor, agent or app that launched it.
SUPERVISORS = {
    "npm", "npx", "pnpm", "pnpx", "yarn", "bun", "bunx", "nodemon", "watchfiles",
    "cargo-watch", "air", "reflex", "entr", "overmind", "foreman", "honcho",
    "concurrently", "turbo", "nx", "vite", "next", "nuxt", "astro",
}
# Never treated as part of a server's job even when the label would match.
APP_LABELS = {"T3 Code", "Codex", "Claude Code", "Ollama"}


def _argv(pid):
    raw = _read(os.path.join(PROC, str(pid), "cmdline"), binary=True)
    return [a.decode(errors="replace") for a in raw.split(b"\0") if a]


def _tool_name(word):
    base = os.path.basename(word or "")
    for suffix in (".js", ".cjs", ".mjs", "-cli"):
        if base.endswith(suffix):
            base = base[: -len(suffix)]
    return base


def is_supervisor(pid, listener_label):
    comm = _comm(pid)
    argv = _argv(pid)
    names = {comm.split(" ")[0], _tool_name(argv[0] if argv else "")}
    # `node /usr/lib/node_modules/npm/bin/npm-cli.js run dev`
    if len(argv) > 1 and _tool_name(argv[0]) in ("node", "python", "python3", "bun", "deno"):
        names.add(_tool_name(argv[1]))
    if names & SUPERVISORS:
        return True
    # A parent running the same framework as the listener, e.g. the
    # `next dev` process that owns a `next-server` worker.
    label = classify(" ".join(argv), comm)[0]
    return (label == listener_label and label not in APP_LABELS
            and any(label == known for _, known, _ in _KNOWN))


def is_wrapper_shell(pid):
    argv = _argv(pid)
    return bool(argv) and _comm(pid) in SHELLS and "-c" in argv[1:]


def stop_targets(pid, max_depth=6):
    """The listener plus the supervisors that would respawn it.

    Dev servers often run under a parent that restarts them (npm run dev,
    nodemon, next dev). Stopping only the listener would let it come back, so
    climb into parents that are recognised supervisors, passing through
    `sh -c` wrappers only when a supervisor sits above them. The climb stops
    at anything else (your shell, an editor, an AI agent, the desktop), at
    other process groups, other users and pid 1. Returned listener-first.
    """
    st = proc_stat(pid)
    if not st:
        return [pid]
    me, uid = os.getpid(), _uid_of(pid)
    label = classify(" ".join(_argv(pid)), _comm(pid))[0]
    chain, pending, cur = [pid], [], st
    for _ in range(max_depth):
        ppid = cur["ppid"]
        parent = proc_stat(ppid) if ppid > 1 else None
        if (not parent or parent["pgid"] != st["pgid"] or ppid == me
                or _uid_of(ppid) != uid):
            break
        if is_supervisor(ppid, label):
            chain += pending + [ppid]
            pending = []
        elif is_wrapper_shell(ppid):
            pending.append(ppid)
        else:
            break
        cur = parent
    return chain


def _wait_exit(fds, timeout):
    """Wait until every pidfd reports exit; return the fds still alive."""
    import select
    pending = set(fds)
    deadline = time.time() + timeout
    poller = select.poll()
    for fd in pending:
        poller.register(fd, select.POLLIN)
    while pending:
        remaining = deadline - time.time()
        if remaining <= 0:
            break
        for fd, _ in poller.poll(remaining * 1000):
            pending.discard(fd)
            poller.unregister(fd)
    return pending


def _send(fd, sig):
    try:
        signal.pidfd_send_signal(fd, sig)
    except ProcessLookupError:
        pass


def stop_pid(pid, start_ticks=None, grace=STOP_GRACE_SEC):
    """SIGTERM the server (and its supervisors), SIGKILL after `grace` seconds.

    Signals go through pidfds, so once a process is verified (start time
    matches what `list` reported) a recycled pid can never be hit.
    """
    if pid <= 1 or pid == os.getpid():
        return {"ok": False, "error": "refusing to stop pid %d" % pid}
    fds = {}
    try:
        for target in stop_targets(pid):
            try:
                fd = os.pidfd_open(target)
            except ProcessLookupError:
                if target == pid:
                    return {"ok": True, "signal": "none", "message": "already stopped"}
                continue
            st = proc_stat(target)
            if target == pid and start_ticks is not None and (not st or st["ticks"] != start_ticks):
                os.close(fd)
                return {"ok": True, "signal": "none", "message": "already stopped"}
            fds[fd] = target
        # Supervisors first so they don't respawn the listener.
        for fd in reversed(list(fds)):
            try:
                signal.pidfd_send_signal(fd, signal.SIGTERM)
            except ProcessLookupError:
                pass
            except PermissionError:
                if fds[fd] == pid:
                    return {"ok": False, "error": "permission denied (pid %d is not yours)" % pid}
        alive = _wait_exit(list(fds), grace)
        if not alive:
            return {"ok": True, "signal": "TERM", "pids": sorted(fds.values())}
        for fd in alive:
            _send(fd, signal.SIGKILL)
        alive = _wait_exit(list(alive), 2.0)
        return {"ok": not alive, "signal": "KILL", "pids": sorted(fds.values())}
    finally:
        for fd in fds:
            os.close(fd)


def stop_container(cid):
    if not re.fullmatch(r"[0-9a-fA-F]{6,64}", cid or ""):
        return {"ok": False, "error": "bad container id"}
    try:
        res = subprocess.run(["docker", "stop", cid], capture_output=True, text=True, timeout=30)
    except (OSError, subprocess.TimeoutExpired) as exc:
        return {"ok": False, "error": str(exc)}
    return {"ok": res.returncode == 0, "error": res.stderr.strip()}


def main(argv):
    cmd = argv[1] if len(argv) > 1 else "list"
    if cmd == "list":
        result = collect()
    elif cmd == "stop" and len(argv) > 2 and argv[2].isdigit():
        ticks = int(argv[3]) if len(argv) > 3 and argv[3].isdigit() else None
        result = stop_pid(int(argv[2]), ticks)
    elif cmd == "stop-container" and len(argv) > 2:
        result = stop_container(argv[2])
    else:
        sys.stderr.write(__doc__)
        return 2
    print(json.dumps(result))
    return 0 if result.get("ok", True) else 1


if __name__ == "__main__":
    sys.exit(main(sys.argv))
