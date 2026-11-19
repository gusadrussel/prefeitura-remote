import tempfile
import unittest
import uuid
from pathlib import Path
from unittest.mock import patch
from types import SimpleNamespace
from agent.agent import run
from agent.agent import _enroll_with_retry
import urllib.error

from agent.agent import _validate_server_url, get_agent_id
from agent.config import load_config
from agent.agent import enroll_agent, reconnect_delay


class AgentIdentityAndTransportTests(unittest.TestCase):
    def test_enrollment_retries_transient_errors_but_never_conflicts(self):
        transient = RuntimeError("synthetic unavailable")
        transient.__cause__ = urllib.error.HTTPError("https://synthetic.invalid",503,"Unavailable",None,None)
        with patch("agent.agent.enroll_agent",side_effect=[transient,"synthetic-credential"]) as enroll,patch("agent.agent.time.sleep"):
            self.assertEqual(_enroll_with_retry("https://synthetic.invalid","id","synthetic"),"synthetic-credential")
            self.assertEqual(enroll.call_count,2)
        conflict = RuntimeError("synthetic conflict")
        conflict.__cause__ = urllib.error.HTTPError("https://synthetic.invalid",409,"Conflict",None,None)
        with patch("agent.agent.enroll_agent",side_effect=conflict) as enroll,patch("agent.agent.time.sleep") as sleep:
            with self.assertRaises(RuntimeError):
                _enroll_with_retry("https://synthetic.invalid","id","synthetic")
            self.assertEqual(enroll.call_count,1)
            sleep.assert_not_called()

    def test_periodic_inventory_failure_is_retried_without_killing_continuous_agent(self):
        config = SimpleNamespace(server_url="https://synthetic.invalid",token="synthetic",individual_credential=True,
                                 interval_seconds=300,machine_id_file=Path("not-used"),enrollment_token=None)
        with patch("agent.agent.load_config",return_value=config),patch("agent.agent.get_agent_id",return_value="synthetic"), \
             patch("agent.agent.collect_inventory",return_value={"system":{"hostname":"SYNTHETIC"}}) as collect, \
             patch("agent.agent.ClientApp"),patch("agent.agent.time.sleep"),patch("agent.agent.time.monotonic",return_value=1000) as clock, \
             patch("agent.agent.send_inventory",side_effect=[None,RuntimeError("synthetic offline"),None]) as send:
            def websocket(*args,**kwargs):
                if send.call_count == 1:
                    clock.return_value = 1400
                    args[4]()
                raise KeyboardInterrupt()
            with patch("agent.agent.run_websocket_session",side_effect=websocket):
                with self.assertRaises(KeyboardInterrupt):
                    run()
            self.assertEqual(send.call_count,3)
            self.assertEqual(collect.call_count,2)

    def test_continuous_agent_recovers_when_first_inventory_send_fails(self):
        config = SimpleNamespace(server_url="https://synthetic.invalid",token="synthetic",individual_credential=True,
                                 interval_seconds=300,machine_id_file=Path("not-used"),enrollment_token=None)
        with patch("agent.agent.load_config",return_value=config),patch("agent.agent.get_agent_id",return_value="synthetic"), \
             patch("agent.agent.collect_inventory",return_value={"system":{"hostname":"SYNTHETIC"}}) as collect, \
             patch("agent.agent.ClientApp") as ui,patch("agent.agent.time.sleep"), \
             patch("agent.agent.send_inventory",side_effect=[RuntimeError("simulated offline"),None]) as send, \
             patch("agent.agent.run_websocket_session",side_effect=KeyboardInterrupt):
            with self.assertRaises(KeyboardInterrupt):
                run()
            self.assertEqual(send.call_count,2)
            self.assertEqual(collect.call_count,1)
            ui.return_value.stop.assert_called_once()

    def setUp(self):
        directory = tempfile.TemporaryDirectory()
        self.addCleanup(directory.cleanup)
        config_patch = patch("agent.config.default_config_file", return_value=Path(directory.name) / "config.json")
        config_patch.start()
        self.addCleanup(config_patch.stop)

    def test_agent_id_is_created_once_and_persisted(self):
        with tempfile.TemporaryDirectory() as directory:
            path = Path(directory) / "state" / "agent_id"
            first_id = get_agent_id(path)
            second_id = get_agent_id(path)
            self.assertEqual(first_id, second_id)
            self.assertEqual(str(uuid.UUID(first_id)), first_id)

    def test_transport_requires_tls_outside_loopback(self):
        self.assertEqual(_validate_server_url("https://central.example.gov"), "https://central.example.gov")
        self.assertEqual(_validate_server_url("http://localhost:8000"), "http://localhost:8000")
        for url in ("http://central.example.gov", "ftp://central.example.gov"):
            with self.subTest(url=url), self.assertRaises(ValueError):
                _validate_server_url(url)

    def test_individual_credential_takes_precedence_over_global_legacy_token(self):
        environment = {
            "PREFEITURA_SERVER_URL": "https://central.example.gov",
            "PREFEITURA_AGENT_TOKEN": "legacy-global-value",
            "PREFEITURA_AGENT_CREDENTIAL": "per-agent-random-value",
        }
        with patch.dict("os.environ", environment, clear=True):
            config = load_config()
        self.assertEqual(config.token, "per-agent-random-value")
        self.assertTrue(config.individual_credential)

        with patch.dict(
            "os.environ",
            {key: value for key, value in environment.items() if key != "PREFEITURA_AGENT_CREDENTIAL"},
            clear=True,
        ):
            config = load_config()
        self.assertEqual(config.token, "legacy-global-value")
        self.assertFalse(config.individual_credential)

    def test_enrollment_token_overrides_stale_legacy_global_token(self):
        with patch.dict(
            "os.environ",
            {
                "PREFEITURA_SERVER_URL": "http://127.0.0.1:8000",
                "PREFEITURA_ENROLLMENT_TOKEN": "bootstrap-value",
            },
            clear=True,
        ), patch("agent.config.default_config_file") as default_config:
            config_path = Path(tempfile.gettempdir()) / "prefeitura-remote-test-config.json"
            config_path.write_text(
                '{"server_url":"http://127.0.0.1:8000","token":"stale-legacy-token"}',
                encoding="utf-8",
            )
            default_config.return_value = config_path
            try:
                config = load_config()
            finally:
                config_path.unlink(missing_ok=True)

        self.assertIsNone(config.token)
        self.assertFalse(config.individual_credential)
        self.assertEqual(config.enrollment_token, "bootstrap-value")

    def test_reconnect_delay_is_bounded(self):
        self.assertEqual(reconnect_delay(0), 5)
        self.assertEqual(reconnect_delay(1), 10)
        self.assertEqual(reconnect_delay(5), 64 if False else 60)
        self.assertEqual(reconnect_delay(99), 60)

    def test_enrollment_request_returns_individual_credential(self):
        response = type("Response", (), {
            "__enter__": lambda self: self,
            "__exit__": lambda self, *args: None,
            "read": lambda self: b'{"credential":"per-agent-secret"}',
        })()
        with patch("agent.agent.open_agent_request", return_value=response) as urlopen:
            credential = enroll_agent(
                "https://central.example.gov",
                "agent-1",
                "bootstrap-secret",
            )
        self.assertEqual(credential, "per-agent-secret")
        request = urlopen.call_args.args[0]
        self.assertEqual(request.get_header("X-prefeitura-enrollment"), "bootstrap-secret")
        self.assertEqual(request.get_header("X-prefeitura-agent-id"), "agent-1")


if __name__ == "__main__":
    unittest.main()
