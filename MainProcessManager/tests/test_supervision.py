import json
import os
import runpy
import sys
import unittest
from contextlib import ExitStack, closing, redirect_stdout
from dataclasses import replace
from io import StringIO
from pathlib import Path
from tempfile import TemporaryDirectory
from unittest.mock import patch

from MainProcessManager.__main__ import _dispatch
from MainProcessManager.control import ROOT, ControlStore, control_lock
from MainProcessManager.control_pipe import ControlPipe
from MainProcessManager.external import FRP_BACKEND, FRP_PROXY, LAN_BACKEND, LAN_PROXY
from MainProcessManager.processes import Supervisor
from MainProcessManager.services import CORE, RELOAD_APPS, Service, project_services
from server.CASserver.store import CASStore
CLIENT_FILE = ROOT / "server" / "UnifiedInterfaceManagement" / "request_auth.py"
LISTENER_COMMAND = (
    sys.executable, "-B", "-c",
    "import signal, socket, sys, time\n"
    "signal.signal(signal.SIGBREAK, lambda *_: sys.exit(0))\n"
    "with socket.socket() as listener:\n"
    "    listener.bind(('127.0.0.1', 0))\n"
    "    listener.listen()\n"
    "    while True:\n"
    "        time.sleep(0.05)\n",
)


class SupervisorTests(unittest.TestCase):
    @patch.dict(os.environ, {"CAMPUS_DIGEST_DIR": "", "CAMPUS_RUMOR_DIR": "", "CAMPUS_RUMOR_PYTHON": ""})
    def test_package_registration_can_use_local_deliveries_and_digest_cache(self):
        root = Path(self.enterContext(TemporaryDirectory())) / "Desktop" / "Projects" / "Campus"
        initial = project_services(root)
        self.assertEqual(initial["campus-digest"].argv[-1], "--cached")
        self.assertFalse(initial["campus-rumor"].start_all)
        self.assertFalse(initial["campus-digest"].start_all)

        for marker in (root / "server/MainAgent/.venv/Scripts/python.exe",
                       root / "DataBase/CampusDigest/digest.sqlite3"):
            marker.parent.mkdir(parents=True, exist_ok=True)
            marker.touch()
        self.assertTrue(project_services(root)["campus-digest"].start_all)

        digest = root / "server" / "CampusDigest" / ".runtime" / "delivery"
        rumor = root / "server" / "CampusRumor" / ".runtime" / "delivery"
        for marker in (digest / "server/winctl.py", digest / "runtime/python/python.exe",
                       rumor / "app/main.py", rumor.parent / "python/python.exe"):
            marker.parent.mkdir(parents=True, exist_ok=True)
            marker.touch()

        restored = project_services(root)
        self.assertEqual(Path(restored["campus-digest"].argv[-1]), digest)
        self.assertEqual(Path(restored["campus-rumor"].argv[-1]), rumor)
        self.assertTrue(restored["campus-rumor"].start_all)

    def test_cas_is_supervised_and_managed_in_the_private_state_directory(self):
        with TemporaryDirectory() as temporary, closing(ControlStore(Path(temporary))) as store:
            with closing(Supervisor(store, ROOT)) as supervisor, redirect_stdout(StringIO()):
                spec = supervisor.services["cas-server"]
                with patch.object(Service, "command", lambda service: [sys.executable, *service.argv[1:]]):
                    argv, env = supervisor._command_and_env(spec)
                self.assertEqual(argv[-2:], ["-m", "server.CASserver"])
                self.assertIn("cas-server", CORE)
                self.assertIn("cas-server", RELOAD_APPS)
                self.assertIn("cas-server", supervisor.services["user-website"].depends_on)
                with patch("MainProcessManager.__main__._secret", side_effect=["local-cas1", "local-cas1"]):
                    _dispatch("cas", "add 001090", "developer", store, supervisor)
                account, = CASStore(Path(env["CAS_DATA_DIR"])).accounts()
                self.assertEqual((account["username"], account["enabled"]), ("001090", 1))
                _dispatch("cas", "set-number 001090 staff-001090", "developer", store, supervisor)
                _dispatch("cas", "disable staff-001090", "developer", store, supervisor)
                self.assertEqual(CASStore(Path(env["CAS_DATA_DIR"])).accounts()[0]["enabled"], 0)
                self.assertIn(("cas.set-number", "staff-001090"), [tuple(row[2:]) for row in store.recent_events()])
                directory = CASStore(Path(env["CAS_DATA_DIR"]))
                with patch("MainProcessManager.__main__._secret", return_value="cas-admin-initial9"):
                    _dispatch("cas", "admin passwd", "developer", store, supervisor)
                session, _ = directory.administrator_login("admin", "cas-admin-initial9", "")
                self.assertTrue(directory.administrator_session_active(session))
                with patch("MainProcessManager.__main__._secret", side_effect=["cas-admin-new9", "cas-admin-new9"]):
                    _dispatch("cas", "admin passwd", "developer", store, supervisor)
                self.assertFalse(directory.administrator_session_active(session))
                self.assertIsNotNone(directory.administrator_login("admin", "cas-admin-new9", "")[0])

    def test_lan_website_is_saved_and_started_only_on_request(self):
        with TemporaryDirectory() as temporary, closing(ControlStore(Path(temporary))) as store:
            directory = Path(temporary)
            website = Service("user-website", directory, LISTENER_COMMAND, listener=True)
            services = {website.name: website}
            with closing(Supervisor(store, ROOT, services=services)) as supervisor, redirect_stdout(StringIO()):
                _dispatch("external", "configure website 192.168.42.10 9443 9083", "developer", store, supervisor)
                proxy = supervisor.services[LAN_PROXY]
                supervisor.services[LAN_PROXY] = replace(proxy, cwd=directory, argv=LISTENER_COMMAND)
                _, backend_env = supervisor._command_and_env(supervisor.services[LAN_BACKEND])
                self.assertEqual(backend_env["USER_WEB_HOST"], "127.0.0.1")
                self.assertEqual(backend_env["USER_WEB_PORT"], "9083")
                self.assertEqual(backend_env["USER_WEB_PUBLIC_URL"], "https://192.168.42.10:9443")
                self.assertEqual(backend_env["USER_WEB_CAS_PUBLIC_URL"], "https://192.168.42.10:8444")
                self.assertEqual(dict(proxy.env)["MPM_LAN_CAS_PORT"], "8444")

                _dispatch("start", "all", "developer", store, supervisor)
                before = supervisor.status()
                self.assertEqual(before["user-website"][0], "ready")
                self.assertEqual(before[LAN_PROXY], ("stopped", None))
                self.assertEqual(before[LAN_BACKEND], ("stopped", None))
                with self.assertRaisesRegex(ValueError, "不能作为自定义进程的依赖"):
                    supervisor.register("developer", "extension", {
                        "cwd": temporary, "argv": list(LISTENER_COMMAND), "depends_on": [LAN_PROXY],
                    })

                _dispatch("external", "start website", "developer", store, supervisor)
                published = supervisor.status()
                self.assertEqual(published["user-website"], before["user-website"])
                self.assertEqual(published[LAN_BACKEND][0], "ready")
                self.assertEqual(published[LAN_PROXY][0], "ready")
                with self.assertRaisesRegex(ValueError, "先运行 external stop website"):
                    _dispatch("external", "configure website 192.168.42.11", "developer", store, supervisor)

                self.assertEqual(supervisor.reload("developer")[LAN_PROXY], "ready")
                reloaded = supervisor.status()
                self.assertNotEqual(reloaded[LAN_PROXY][1], published[LAN_PROXY][1])
                _dispatch("external", "stop website", "developer", store, supervisor)
                stopped = supervisor.status()
                self.assertEqual(stopped["user-website"], reloaded["user-website"])
                self.assertIsNone(stopped[LAN_BACKEND][1])
                self.assertIsNone(stopped[LAN_PROXY][1])
                supervisor.stop_all("developer")

            with closing(Supervisor(store, ROOT, services=services)) as restored:
                self.assertEqual(restored.external_website.origin, "https://192.168.42.10:9443")
                self.assertEqual(restored.status(LAN_PROXY)[LAN_PROXY], ("stopped", None))

    def test_frp_config_has_two_targets_and_preserves_the_lan_cas_mapping(self):
        with TemporaryDirectory() as temporary, closing(ControlStore(Path(temporary))) as store:
            with closing(Supervisor(store, ROOT)) as supervisor, redirect_stdout(StringIO()):
                _dispatch("external", "configure website 192.168.42.10", "developer", store, supervisor)
                _dispatch("external", "configure frp https://agent.example.com https://cas.example.com",
                          "developer", store, supervisor)
                with (patch.object(Service, "command", lambda service: [sys.executable, *service.argv[1:]]),
                      patch.dict(os.environ, {"CLOUD_DATABASE_URL": "postgresql://fixture"})):
                    _, agent_env = supervisor._command_and_env(supervisor.services["main-agent"])
                    _, cas_env = supervisor._command_and_env(supervisor.services["cas-server"])
                sites = {"https://192.168.42.10:8443": "https://192.168.42.10:8444",
                         "https://agent.example.com": "https://cas.example.com"}
                self.assertEqual(json.loads(agent_env["CLOUD_CAS_PUBLIC_SITES"]), sites)
                self.assertEqual(json.loads(cas_env["CAS_PUBLIC_SITES"]), sites)
                proxy = supervisor.services[FRP_PROXY]
                self.assertEqual(dict(proxy.env)["MPM_FRP_WEBSITE_PORT"], "8483")
                self.assertEqual(dict(proxy.env)["MPM_FRP_CAS_PORT"], "8484")
                self.assertEqual(dict(supervisor.services[FRP_BACKEND].env)["USER_WEB_PUBLIC_URL"], "https://agent.example.com")
                self.assertFalse(proxy.start_all)
                self.assertFalse(supervisor.services[FRP_BACKEND].start_all)
            with closing(Supervisor(store, ROOT)) as restored:
                self.assertEqual(restored.require_external_frp().cas_origin, "https://cas.example.com")

    def test_invalid_addresses_and_failed_lan_start_leave_no_listener(self):
        with TemporaryDirectory() as temporary, closing(ControlStore(Path(temporary))) as store:
            directory = Path(temporary)
            website = Service("user-website", directory, LISTENER_COMMAND, listener=True)
            with closing(Supervisor(store, ROOT, services={website.name: website})) as supervisor:
                for address in ("0.0.0.0", "127.0.0.1", "8.8.8.8", "192.168.1.2:8443"):
                    with self.subTest(address=address), self.assertRaisesRegex(ValueError, "内网 IPv4"):
                        _dispatch("external", f"configure website {address}", "developer", store, supervisor)
                with self.assertRaisesRegex(ValueError, "80 是 Caddy 的 HTTP 端口"):
                    _dispatch("external", "configure website 10.0.0.8 80", "developer", store, supervisor)
                self.assertIsNone(store.external_config("website"))
                with redirect_stdout(StringIO()):
                    _dispatch("external", "configure website 10.0.0.8", "developer", store, supervisor)
                supervisor.services[LAN_PROXY] = replace(
                    supervisor.services[LAN_PROXY], cwd=directory, argv=(sys.executable, "-B", "-c", "raise SystemExit(2)"),
                )
                with self.assertRaisesRegex(RuntimeError, "退出码 2"):
                    supervisor.start_external_website("developer")
                self.assertIsNone(supervisor.status(LAN_PROXY)[LAN_PROXY][1])
                self.assertIsNone(supervisor.status(LAN_BACKEND)[LAN_BACKEND][1])

    def test_reload_restarts_only_running_apps_and_dependents_in_dependency_order(self):
        with TemporaryDirectory() as temporary, closing(ControlStore(Path(temporary))) as store:
            directory = Path(temporary)
            graph = {
                "llmserver": (),
                "mcpserver": (),
                "unified-interface": ("llmserver",),
                "user-website": ("unified-interface",),
                "extension": ("user-website",),
            }
            services = {
                name: Service(name, directory, LISTENER_COMMAND, depends, listener=True)
                for name, depends in graph.items()
            }
            with closing(Supervisor(store, ROOT, services=services)) as supervisor, ExitStack() as cleanup:
                cleanup.callback(supervisor.stop_all, "developer")
                self.assertEqual(supervisor.reload("developer"), {})
                supervisor.start("developer", "extension")
                # Simulate an upstream process exiting and being restarted after its dependents.
                supervisor._job("unified-interface").stop()
                supervisor.start("developer", "unified-interface")
                before = supervisor.status()
                output = StringIO()
                with redirect_stdout(output):
                    _dispatch("reload", "", "developer", store, supervisor)
                after = supervisor.status()
                self.assertEqual(before["llmserver"], after["llmserver"])
                self.assertEqual(after["mcpserver"], ("stopped", None))
                affected = ("unified-interface", "user-website", "extension")
                for name in affected:
                    self.assertEqual(after[name][0], "ready")
                    self.assertNotEqual(before[name][1], after[name][1])
                    self.assertIn(f"{name}  ready", output.getvalue())
                actions = [(record[2], record[3]) for record in reversed(store.recent_events())]
                self.assertEqual(actions[-7:], [
                    ("process.reload", "apps"),
                    *(("process.stop", name) for name in reversed(affected)),
                    *(("process.start", name) for name in affected),
                ])

    def test_reload_restores_stopped_dependents_if_a_stop_fails(self):
        with TemporaryDirectory() as temporary, closing(ControlStore(Path(temporary))) as store:
            directory = Path(temporary)
            services = {
                "unified-interface": Service("unified-interface", directory, LISTENER_COMMAND, listener=True),
                "user-website": Service("user-website", directory, LISTENER_COMMAND, ("unified-interface",), listener=True),
            }
            with closing(Supervisor(store, ROOT, services=services)) as supervisor, ExitStack() as cleanup:
                cleanup.callback(supervisor.stop_all, "developer")
                supervisor.start("developer", "user-website")
                before = supervisor.status()
                original_stop = supervisor.stop

                def stop(actor, name):
                    if name == "unified-interface":
                        raise RuntimeError("模拟停止失败")
                    original_stop(actor, name)

                with patch.object(supervisor, "stop", side_effect=stop):
                    with self.assertRaisesRegex(RuntimeError, "unified-interface 停止失败"):
                        supervisor.reload("developer")
                after = supervisor.status()
                self.assertEqual(after["unified-interface"], before["unified-interface"])
                self.assertEqual(after["user-website"][0], "ready")
                self.assertNotEqual(after["user-website"][1], before["user-website"][1])

    def test_reload_retries_failed_apps_and_blocked_dependents(self):
        with TemporaryDirectory() as temporary, closing(ControlStore(Path(temporary))) as store:
            directory = Path(temporary)
            marker = directory / "fail-on-reload"
            listener = (*LISTENER_COMMAND[:-1],
                        "from pathlib import Path\nif Path('fail-on-reload').exists():\n    raise SystemExit(1)\n"
                        + LISTENER_COMMAND[-1])
            services = {
                "main-agent": Service("main-agent", directory, listener, listener=True,
                                      env=(("CLOUD_DATABASE_URL", "fixture"),)),
                "teacher-credentials": Service("teacher-credentials", directory, listener, listener=True),
                "user-website": Service("user-website", directory, LISTENER_COMMAND, ("main-agent",), listener=True),
                "mcpserver": Service("mcpserver", directory, LISTENER_COMMAND, listener=True),
            }
            with closing(Supervisor(store, ROOT, services=services)) as supervisor, ExitStack() as cleanup:
                cleanup.callback(supervisor.stop_all, "developer")
                supervisor.start("developer", "teacher-credentials")
                supervisor.start("developer", "user-website")
                marker.touch()
                with self.assertRaisesRegex(RuntimeError, "user-website 等待依赖进程恢复"):
                    supervisor.reload("developer")
                self.assertEqual(supervisor.status("main-agent")["main-agent"], ("exited", None))
                self.assertEqual(supervisor.status("user-website")["user-website"], ("stopped", None))

                marker.unlink()
                self.assertEqual(supervisor.reload("developer"), {
                    "main-agent": "ready", "teacher-credentials": "ready", "user-website": "ready",
                })
                self.assertEqual(supervisor.status("mcpserver")["mcpserver"], ("stopped", None))

                marker.touch()
                with self.assertRaises(RuntimeError):
                    supervisor.reload("developer")
                supervisor.stop("developer", "main-agent")
                marker.unlink()
                self.assertEqual(supervisor.reload("developer"), {"teacher-credentials": "ready"})
                self.assertEqual(supervisor.status("user-website")["user-website"], ("stopped", None))

    def test_local_account_has_one_owner_and_a_password_hash(self):
        with TemporaryDirectory() as temporary:
            directory = Path(temporary)
            with control_lock(directory), closing(ControlStore(directory)) as store:
                with self.assertRaises(RuntimeError), control_lock(directory):
                    self.fail("unexpected second supervisor")
                original = "first local test password"
                replacement = "second local test password"
                store.initialize("developer", original)
                with self.assertRaises(ValueError):
                    store.initialize("developer2", replacement)
                with self.assertRaises(ValueError):
                    store.authenticate("developer", "wrong password")
                self.assertEqual(store.authenticate("developer", original), "developer")
                store.change_password("developer", original, replacement)
                self.assertEqual(store.authenticate("developer", replacement), "developer")
                self.assertNotIn(replacement.encode(), store.path.read_bytes())

    def test_process_registration_is_restored_and_preserves_dependencies(self):
        with TemporaryDirectory() as temporary, closing(ControlStore(Path(temporary))) as store:
            with closing(Supervisor(store, ROOT, services={})) as supervisor:
                definition = {"cwd": temporary, "argv": [sys.executable, "-V"]}
                with self.assertRaises(ValueError):
                    supervisor.register("developer", "consumer", {**definition, "depends_on": ["producer"]})
                supervisor.register("developer", "producer", definition)
                supervisor.register("developer", "consumer", {**definition, "depends_on": ["producer"]})
                with self.assertRaises(ValueError):
                    supervisor.unregister("developer", "producer")
            with closing(Supervisor(store, ROOT, services={})) as restored:
                self.assertEqual(restored.services["consumer"].depends_on, ("producer",))
                restored.unregister("developer", "consumer")
                restored.unregister("developer", "producer")
                self.assertFalse(store.registrations())

    def test_each_pipe_request_authenticates_a_supervised_process(self):
        with TemporaryDirectory() as temporary, closing(ControlStore(Path(temporary))) as store:
            directory = Path(temporary)
            driver = directory / "agent.py"
            driver.write_text(
                "import json, os, runpy, sys\n"
                "authorize = runpy.run_path(sys.argv[1])['authorize_request']\n"
                "address, pid = os.environ['MPM_AUTH_PIPE'], int(os.environ['MPM_AUTH_PID'])\n"
                "results = [authorize(address, pid, 'GET', '/v1/instances') for _ in range(2)]\n"
                "print(json.dumps(results))\n",
                encoding="ascii",
            )
            command = (sys.executable, "-B", str(driver), str(CLIENT_FILE))
            service = Service("main-agent", directory, command)
            with closing(Supervisor(store, ROOT, services={"main-agent": service})) as supervisor:
                with ControlPipe(supervisor.owns_interface_caller) as pipe:
                    supervisor.control_pipe = pipe.address
                    caller = runpy.run_path(str(CLIENT_FILE))["authorize_request"]
                    self.assertFalse(caller(pipe.address, os.getpid(), "GET", "/v1/instances"))
                    with patch.dict(os.environ, CLOUD_DATABASE_URL="unused"):
                        supervisor.start("developer", "main-agent")
                    job = supervisor._job("main-agent")
                    if job is not None:
                        self.assertEqual(job.wait_root(5), 0)
                    self.assertEqual(json.loads(supervisor.log_path("main-agent").read_text()), [True, True])
                    self.assertFalse(caller(pipe.address, os.getpid(), "GET", "/v1/instances"))


if __name__ == "__main__":
    unittest.main()
