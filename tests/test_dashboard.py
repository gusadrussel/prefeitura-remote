import unittest
from pathlib import Path


DASHBOARD_SCRIPT = Path(__file__).resolve().parents[1] / "dashboard" / "js" / "dashboard.js"


class DashboardFullscreenTests(unittest.TestCase):
    def test_fullscreen_state_is_independent_from_remote_control(self):
        source = DASHBOARD_SCRIPT.read_text(encoding="utf-8")
        handler_start = source.index("async function toggleScreenFullscreen()")
        handler_end = source.index("async function startRemoteControl()", handler_start)
        fullscreen_handler = source[handler_start:handler_end]

        self.assertNotIn("controlState", fullscreen_handler)
        self.assertIn("viewer.requestFullscreen()", fullscreen_handler)
        self.assertIn("document.exitFullscreen()", fullscreen_handler)
        self.assertIn("data-screen-action=\"fullscreen\"", source)
        self.assertIn('addEventListener("click", toggleScreenFullscreen)', source)
        self.assertIn("console.error(\"[screen-fullscreen] rejected\"", fullscreen_handler)
        self.assertIn("document.fullscreenEnabled", fullscreen_handler)
        self.assertIn('console.info("[screen-fullscreen] changed"', source)
        self.assertLess(
            fullscreen_handler.index("viewer.requestFullscreen()"),
            fullscreen_handler.index("catch (reason)"),
        )
        self.assertIn('fullscreenStatus.textContent = isFullscreen ? "Tela cheia ativa" : "Janela"', source)
        self.assertIn('idle: "Controle desligado"', source)
        self.assertIn('button.disabled = screenState !== "streaming" && !isFullscreen', source)

    def test_control_start_rejection_remains_visible_and_retryable(self):
        source = DASHBOARD_SCRIPT.read_text(encoding="utf-8")
        self.assertIn('controlErrorMessage = message.reason || "screen_resolution_mismatch"', source)
        self.assertIn('data-control-error role="status"', source)
        self.assertIn('["idle", "error"].includes(controlState)', source)


if __name__ == "__main__":
    unittest.main()
