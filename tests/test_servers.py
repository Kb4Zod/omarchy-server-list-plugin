import os
import socket
import subprocess
import sys
import time
import unittest

sys.path.insert(0, os.path.join(os.path.dirname(__file__), "..", "bin"))
import servers  # noqa: E402

PROC_NET_TCP = """\
  sl  local_address rem_address   st tx_queue rx_queue tr tm->when retrnsmt   uid  timeout inode
   0: 0100007F:0EBD 00000000:0000 0A 00000000:00000000 00:00000000 00000000  1000        0 1837383 1 0 100 0 0 10 0
   1: 00000000:1F90 00000000:0000 0A 00000000:00000000 00:00000000 00000000  1000        0 222 1 0 100 0 0 10 0
   2: 0100007F:0EBD 0100007F:D2A4 01 00000000:00000000 00:00000000 00000000  1000        0 333 1 0 100 0 0 10 0
"""

PROC_NET_TCP6 = """\
  sl  local_address                         remote_address                        st tx_queue rx_queue tr tm->when retrnsmt   uid  timeout inode
   0: 00000000000000000000000001000000:0277 00000000000000000000000000000000:0000 0A 00000000:00000000 00:00000000 00000000     0        0 444 1 0 100 0 0 10 0
   1: 00000000000000000000000000000000:1F40 00000000000000000000000000000000:0000 0A 00000000:00000000 00:00000000 00000000  1000        0 555 1 0 100 0 0 10 0
"""


def free_port():
    with socket.socket() as s:
        s.bind(("127.0.0.1", 0))
        return s.getsockname()[1]


def wait_for_port(port, timeout=5.0):
    deadline = time.time() + timeout
    while time.time() < deadline:
        try:
            with socket.create_connection(("127.0.0.1", port), timeout=0.2):
                return True
        except OSError:
            time.sleep(0.05)
    return False


class ParseTests(unittest.TestCase):
    def test_parses_only_listening_ipv4(self):
        socks = servers.parse_proc_net(PROC_NET_TCP, "ipv4")
        self.assertEqual([(s["address"], s["port"]) for s in socks],
                         [("127.0.0.1", 3773), ("0.0.0.0", 8080)])
        self.assertEqual(socks[0]["inode"], "1837383")
        self.assertEqual(socks[0]["uid"], 1000)

    def test_parses_ipv6(self):
        socks = servers.parse_proc_net(PROC_NET_TCP6, "ipv6")
        self.assertEqual([(s["address"], s["port"], s["uid"]) for s in socks],
                         [("::1", 631, 0), ("::", 8000, 1000)])

    def test_docker_published_ports(self):
        self.assertEqual(servers.published_ports("0.0.0.0:5432->5432/tcp, :::5432->5432/tcp"), [5432])
        self.assertEqual(servers.published_ports("0.0.0.0:8000-8002->8000-8002/tcp"), [8000, 8001, 8002])
        self.assertEqual(servers.published_ports("5432/tcp"), [])

    def test_exposed(self):
        self.assertFalse(servers.is_exposed(["127.0.0.1", "::1"]))
        self.assertTrue(servers.is_exposed(["0.0.0.0"]))
        self.assertTrue(servers.is_exposed(["::"]))
        self.assertTrue(servers.is_exposed(["192.168.1.20"]))


class ClassifyTests(unittest.TestCase):
    def label(self, cmd, comm=""):
        return servers.classify(cmd, comm)[0]

    def test_frameworks(self):
        self.assertEqual(self.label("node /home/u/app/node_modules/.bin/vite --port 5173"), "Vite dev server")
        self.assertEqual(self.label("next-server (v15.0.0)"), "Next.js")
        self.assertEqual(self.label("node /x/node_modules/next/dist/server/lib/start-server.js"), "Next.js")
        self.assertEqual(self.label("python3 -m http.server 8000"), "Python http.server")
        self.assertEqual(self.label("python manage.py runserver"), "Django")
        self.assertEqual(self.label("/usr/bin/hugo server -D"), "Hugo server")
        self.assertEqual(self.label("ollama serve"), "Ollama")

    def test_runtimes_use_script_name(self):
        self.assertEqual(self.label("python3 /home/u/todo-api/server.py"),
                         "Python · todo-api/server.py")
        self.assertEqual(self.label("node api.js"), "Node.js · api.js")
        self.assertEqual(self.label("python3 -c 'print(1)'"), "Python")

    def test_mentions_do_not_mislabel(self):
        # "node" or "next" in an argument must not win over the executable.
        self.assertEqual(self.label("python3 /home/u/node-tools/serve_it.py"), "Python · serve_it.py")
        self.assertEqual(self.label("python3 app.py --next"), "Python · app.py")

    def test_unknown_falls_back_to_comm(self):
        self.assertEqual(servers.classify("/opt/thing/bin/mystery --x", "mystery"),
                         ("mystery", servers.DEFAULT_ICON))


class FormatTests(unittest.TestCase):
    def test_duration(self):
        self.assertEqual(servers.human_duration(5), "5s")
        self.assertEqual(servers.human_duration(125), "2m")
        self.assertEqual(servers.human_duration(3600), "1h")
        self.assertEqual(servers.human_duration(3600 + 13 * 60), "1h 13m")
        self.assertEqual(servers.human_duration(86400 * 4 + 3600 * 4), "4d 4h")

    def test_pretty_path(self):
        self.assertEqual(servers.pretty_path(servers.HOME + "/Projects/x"), "~/Projects/x")
        self.assertEqual(servers.pretty_path("/srv/www"), "/srv/www")


class LiveTests(unittest.TestCase):
    """Spawns real throwaway servers and drives list + stop end to end."""

    def spawn(self, code, port):
        proc = subprocess.Popen([sys.executable, "-c", code, str(port)],
                                stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)
        self.addCleanup(lambda: (proc.poll() is None and proc.kill(), proc.wait()))
        self.assertTrue(wait_for_port(port), "test server never came up")
        return proc

    def find(self, port):
        for item in servers.collect()["servers"]:
            if port in item["ports"]:
                return item
        return None

    def test_lists_and_stops_server(self):
        # Not http.server: it does a reverse-DNS lookup on bind that can
        # stall for seconds on some networks.
        port = free_port()
        listener = (
            "import socket, sys, time\n"
            "s = socket.socket(); s.bind(('0.0.0.0', int(sys.argv[1]))); s.listen()\n"
            "while True: s.accept()[0].close()\n"
        )
        proc = self.spawn(listener, port)

        item = self.find(port)
        self.assertIsNotNone(item)
        self.assertEqual(item["pid"], proc.pid)
        self.assertEqual(item["label"], "Python")
        self.assertTrue(item["exposed"])
        self.assertTrue(item["stoppable"])
        self.assertEqual(item["url"], "http://localhost:%d" % port)

        result = servers.stop_pid(proc.pid, item["startTicks"])
        self.assertTrue(result["ok"])
        self.assertEqual(result["signal"], "TERM")
        proc.wait(timeout=2)
        self.assertIsNone(self.find(port))

    def test_escalates_to_sigkill(self):
        port = free_port()
        stubborn = (
            "import signal, socket, sys, time\n"
            "signal.signal(signal.SIGTERM, signal.SIG_IGN)\n"
            "s = socket.socket(); s.bind(('127.0.0.1', int(sys.argv[1]))); s.listen()\n"
            "time.sleep(60)\n"
        )
        proc = self.spawn(stubborn, port)
        self.assertIsNotNone(self.find(port))

        result = servers.stop_pid(proc.pid, grace=0.5)
        self.assertEqual(result["signal"], "KILL")
        self.assertTrue(result["ok"])

    def test_stale_start_ticks_never_signal(self):
        # Simulates pid reuse: the pid now belongs to a different process.
        port = free_port()
        listener = (
            "import socket, sys, time\n"
            "s = socket.socket(); s.bind(('127.0.0.1', int(sys.argv[1]))); s.listen()\n"
            "time.sleep(60)\n"
        )
        proc = self.spawn(listener, port)
        item = self.find(port)
        result = servers.stop_pid(proc.pid, item["startTicks"] + 1)
        self.assertEqual(result["signal"], "none")
        time.sleep(0.2)
        self.assertIsNone(proc.poll(), "process with mismatched start time was signalled")

    # --- supervisor handling -------------------------------------------------
    # Helper scripts run with a chosen argv[0] (like `exec -a`), so the tree
    # looks like the real thing: an agent, a `bash -c` wrapper, nodemon, a server.
    # A renamed interpreter can't find itself, so the real path is passed in.

    LISTENER = (
        "import socket, sys, time\n"
        "s = socket.socket(); s.setsockopt(socket.SOL_SOCKET, socket.SO_REUSEADDR, 1)\n"
        "s.bind(('127.0.0.1', int(sys.argv[1]))); s.listen(); time.sleep(60)\n"
    )
    RESPAWNER = (
        "import subprocess, sys, time\n"
        "while True:\n"
        "    subprocess.run(sys.argv[1:])\n"
        "    time.sleep(0.2)\n"
    )

    def script(self, name, code):
        import tempfile
        d = tempfile.mkdtemp(prefix="servers-test-")
        path = os.path.join(d, name)
        with open(path, "w") as fh:
            fh.write(code)
        return path

    def tree(self, argv, **kw):
        own_group = kw.get("process_group") == 0 or kw.get("start_new_session")
        proc = subprocess.Popen(argv, executable=kw.pop("executable", sys.executable),
                                stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL, **kw)

        def cleanup():
            if proc.poll() is None:
                # Only kill a whole group the child leads, never ours.
                if own_group:
                    os.killpg(proc.pid, 9)
                else:
                    proc.kill()
            proc.wait()
        self.addCleanup(cleanup)
        return proc

    def test_stops_supervisor_so_server_is_not_respawned(self):
        port = free_port()
        listener = self.script("server.py", self.LISTENER)
        respawner = self.script("nodemon.py", self.RESPAWNER)
        # Runs as its own job (process group), as an interactive shell would.
        sup = self.tree(["nodemon", respawner, sys.executable, listener, str(port)], process_group=0)
        self.assertTrue(wait_for_port(port))
        item = self.find(port)
        self.assertNotEqual(item["pid"], sup.pid)
        self.assertEqual(servers.stop_targets(item["pid"]), [item["pid"], sup.pid])
        self.assertEqual(item["alsoStops"], ["python3"])

        result = servers.stop_pid(item["pid"], item["startTicks"])
        self.assertTrue(result["ok"])
        sup.wait(timeout=3)
        time.sleep(0.5)
        self.assertIsNone(self.find(port), "server came back")

    def test_never_climbs_into_the_agent_that_started_it(self):
        # agent -> bash -c -> nodemon -> server, all one process group: the
        # shape you get when an AI agent or editor starts a dev server.
        port = free_port()
        listener = self.script("server.py", self.LISTENER)
        respawner = self.script("nodemon.py", self.RESPAWNER)
        agent_code = (
            "import subprocess, sys, time\n"
            "py = sys.argv[1]\n"
            "subprocess.Popen(['bash', '-c', 'exec -a nodemon \"$0\" \"$@\"; true',\n"
            "                  py, sys.argv[2], py, sys.argv[3], sys.argv[4]])\n"
            "time.sleep(60)\n"
        )
        agent_script = self.script("agent.py", agent_code)
        agent = self.tree(["fake-agent", agent_script, sys.executable, respawner, listener, str(port)],
                          process_group=0)
        self.assertTrue(wait_for_port(port))
        pid = self.find(port)["pid"]
        targets = servers.stop_targets(pid)
        self.assertNotIn(agent.pid, targets)
        self.assertEqual(len(targets), 2)  # server + nodemon (bash exec'd into nodemon)

        servers.stop_pid(pid, self.find(port)["startTicks"])
        time.sleep(0.5)
        self.assertIsNone(agent.poll(), "the agent was killed")
        self.assertIsNone(self.find(port), "server came back")

    def test_bash_c_wrapper_without_supervisor_is_spared(self):
        port = free_port()
        listener = self.script("server.py", self.LISTENER)
        sh = self.tree(["bash", "-c", '"$0" "$1" "$2"; true', sys.executable, listener, str(port)],
                       executable="bash", start_new_session=True)
        self.assertTrue(wait_for_port(port))
        pid = self.find(port)["pid"]
        self.assertEqual(servers.stop_targets(pid), [pid])

    def test_plain_parent_is_not_a_supervisor(self):
        # Parent is this test runner: an ordinary python process, not a supervisor.
        port = free_port()
        listener = self.script("server.py", self.LISTENER)
        proc = self.tree([sys.executable, listener, str(port)])
        self.assertTrue(wait_for_port(port))
        self.assertEqual(servers.stop_targets(proc.pid), [proc.pid])

    def test_refuses_bad_pids(self):
        self.assertFalse(servers.stop_pid(1)["ok"])
        self.assertFalse(servers.stop_pid(os.getpid())["ok"])
        self.assertFalse(servers.stop_container("; rm -rf /")["ok"])

    def test_cli_list_is_json(self):
        script = os.path.join(os.path.dirname(__file__), "..", "bin", "servers.py")
        out = subprocess.run([sys.executable, script, "list"], capture_output=True, text=True, check=True)
        import json
        data = json.loads(out.stdout)
        self.assertIn("servers", data)
        self.assertIn("system", data)


if __name__ == "__main__":
    unittest.main()
