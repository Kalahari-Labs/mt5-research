"""Self-healing bridge ladder tests (executor plane, no MT5 required).

A scripted HTTP server plays every failure personality the real bridge has
shown in production: up-but-attach-dead with /reinit (current server),
up-but-attach-dead without /reinit (pre-self-heal zombie), and
up-but-terminal-logged-out. `ensure_bridge` must pick the right remedy for
each — reinit in place, kill-and-replace, or report the terminal — and must
never blind-spawn a duplicate that dies on a squatted port, which is the
production failure these tests exist to keep dead.
"""
import json
import os
import sys
import threading
import unittest
from functools import partial
from http.server import BaseHTTPRequestHandler, HTTPServer
from unittest import mock

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))  # repo root


def _import_bridge():
    """Import executor.bridge the way it runs: with `intel/` on the path.
    Appended (not inserted) so root modules keep precedence."""
    intel_dir = os.path.join(
        os.path.dirname(os.path.dirname(os.path.abspath(__file__))), "intel")
    if intel_dir not in sys.path:
        sys.path.append(intel_dir)
    import executor.bridge as bridge  # noqa: E402
    return bridge


class ScriptedBridge:
    """In-process HTTP server impersonating bridge_server.py states."""

    def __init__(self):
        self.state = {"ok": False, "login": None, "server": None,
                      "has_reinit": True, "reinit_fixes": False}
        outer = self

        class H(BaseHTTPRequestHandler):
            def log_message(self, *a):
                pass

            def _send(self, code, payload):
                raw = json.dumps(payload).encode()
                self.send_response(code)
                self.send_header("Content-Type", "application/json")
                self.send_header("Content-Length", str(len(raw)))
                self.end_headers()
                self.wfile.write(raw)

            def do_GET(self):
                if self.path == "/health":
                    s = outer.state
                    self._send(200, {"ok": s["ok"],
                                     "account": {"login": s["login"],
                                                 "server": s["server"],
                                                 "demo": bool(s["login"])},
                                     "writes_allowed": s["ok"]})
                else:
                    self._send(404, {"error": "unknown path %s" % self.path})

            def do_POST(self):
                s = outer.state
                if self.path == "/reinit" and s["has_reinit"]:
                    if s["reinit_fixes"]:
                        s["ok"], s["login"] = True, 336582315
                    self._send(200, {"ok": s["ok"], "login": s["login"],
                                     "server": s["server"]})
                else:
                    # exactly what an old bridge_server answers: 404 + JSON
                    self._send(404, {"error": "unknown path %s" % self.path})

        self.srv = HTTPServer(("127.0.0.1", 0), H)
        self.port = self.srv.server_address[1]
        threading.Thread(target=self.srv.serve_forever, daemon=True).start()

    def url(self) -> str:
        return "http://127.0.0.1:%d" % self.port

    def stop(self) -> None:
        self.srv.shutdown()
        self.srv.server_close()


class TestBridgeProbes(unittest.TestCase):
    def setUp(self):
        self.m = _import_bridge()
        self.fake = ScriptedBridge()
        self.addCleanup(self.fake.stop)

    def b(self):
        return self.m.Bridge(self.fake.url(), timeout=3)

    def test_reachable_but_not_alive_when_attach_dead(self):
        """ok:false must read as 'process up, unhealthy' — not 'no bridge'."""
        self.assertTrue(self.b().reachable())
        self.assertFalse(self.b().alive())

    def test_alive_when_ok(self):
        self.fake.state["ok"] = True
        self.assertTrue(self.b().alive())
        self.assertTrue(self.b().reachable())

    def test_reinit_on_old_server_has_no_login_key(self):
        """An old server 404s /reinit into {'error': ...}; the ladder keys on
        the absence of 'login' to decide the process must be replaced."""
        self.fake.state["has_reinit"] = False
        r = self.b().reinit()
        self.assertNotIn("login", r)

    def test_reinit_on_current_server_carries_login_key(self):
        r = self.b().reinit()
        self.assertIn("login", r)


class TestEnsureBridgeLadder(unittest.TestCase):
    def setUp(self):
        self.m = _import_bridge()
        self.fake = ScriptedBridge()
        self.addCleanup(self.fake.stop)
        self.calls = []
        real_bridge, url = self.m.Bridge, self.fake.url()
        for p in (
            mock.patch.object(self.m, "Bridge",
                              partial(real_bridge, url, timeout=3)),
            mock.patch.object(self.m, "start_terminal",
                              lambda: self.calls.append("terminal")),
            mock.patch.object(self.m, "start_bridge", self._fake_spawn),
            mock.patch.object(self.m, "kill_stale_bridge",
                              lambda: self.calls.append("kill")),
            mock.patch.object(self.m.config, "BRIDGE_SPAWN", True),
        ):
            p.start()
            self.addCleanup(p.stop)

    def _fake_spawn(self, *a, **k):
        self.calls.append("spawn")
        self.fake.state.update(ok=True, login=336582315)

    def test_reinit_heals_in_place_without_respawn(self):
        self.fake.state.update(has_reinit=True, reinit_fixes=True)
        b = self.m.ensure_bridge(timeout_sec=4)
        self.assertTrue(b.alive())
        self.assertNotIn("kill", self.calls)
        self.assertNotIn("spawn", self.calls)

    def test_zombie_without_reinit_is_killed_then_replaced(self):
        self.fake.state.update(has_reinit=False)
        b = self.m.ensure_bridge(timeout_sec=6)
        self.assertTrue(b.alive())
        self.assertIn("kill", self.calls)
        self.assertIn("spawn", self.calls)
        self.assertLess(self.calls.index("kill"), self.calls.index("spawn"),
                        "must free the port before spawning, or the spawn dies")

    def test_logged_out_terminal_is_reported_not_respawned(self):
        """Replacing the bridge cannot log the terminal in — the ladder must
        say so instead of churning processes."""
        self.fake.state.update(has_reinit=True, reinit_fixes=False)
        with self.assertRaises(self.m.BridgeError) as cm:
            self.m.ensure_bridge(timeout_sec=4)
        self.assertIn("logged out", str(cm.exception))
        self.assertNotIn("kill", self.calls)
        self.assertNotIn("spawn", self.calls)


if __name__ == "__main__":
    unittest.main()
