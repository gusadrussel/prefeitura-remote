import asyncio
import unittest

from server.services.agent_connections import AgentConnectionRegistry
from server.services.screen_streams import ScreenStreamRegistry


class ScreenStreamRegistryTests(unittest.TestCase):
    def test_viewer_tickets_are_single_use_and_bound_to_agent_and_computer(self):
        registry = ScreenStreamRegistry()
        ticket, _ = registry.issue_ticket("agent-1", 7)
        self.assertIsNone(registry.consume_ticket(ticket, 8))
        self.assertIsNone(registry.consume_ticket(ticket, 7))

        ticket, _ = registry.issue_ticket("agent-1", 7)
        self.assertEqual(registry.consume_ticket(ticket, 7), "agent-1")
        self.assertIsNone(registry.consume_ticket(ticket, 7))

    def test_frame_queue_is_bounded_and_keeps_latest_frame(self):
        registry = ScreenStreamRegistry()
        viewer = registry.attach("agent-1", 7)
        self.assertIsNotNone(viewer)
        self.assertTrue(registry.request_start(viewer))
        self.assertTrue(registry.agent_started("agent-1", {"type": "screen.start_ack", "accepted": True}))
        registry.publish_frame("agent-1", {"type": "screen.frame", "sequence": 1})
        registry.publish_frame("agent-1", {"type": "screen.frame", "sequence": 2})

        latest = asyncio.run(viewer.frame_queue.get())
        self.assertEqual(latest["sequence"], 2)
        self.assertEqual(viewer.frame_queue.maxsize, 1)
        self.assertEqual(viewer.dropped_frames, 1)

    def test_viewer_disconnect_enqueues_stop_for_active_agent(self):
        registry = ScreenStreamRegistry()
        connections = AgentConnectionRegistry()
        connection = connections.connect("agent-1", 7)
        viewer = registry.attach("agent-1", 7)
        registry.request_start(viewer)
        registry.agent_started("agent-1", {"type": "screen.start_ack", "accepted": True})
        self.assertTrue(registry.detach_and_stop_agent(viewer, connections))
        self.assertTrue(viewer.disconnected.is_set())
        self.assertFalse(registry.publish_frame("agent-1", {"type": "screen.frame"}))
        self.assertEqual(connection.outbound.get_nowait(), {"type": "screen.stop"})


if __name__ == "__main__":
    unittest.main()