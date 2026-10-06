import asyncio
import unittest
from unittest.mock import patch

from server.services.agent_connections import AgentConnectionRegistry
from server.services.screen_streams import ScreenStreamRegistry, ScreenTicketCapacityError


class ScreenStreamRegistryTests(unittest.TestCase):
    def test_viewer_tickets_are_single_use_and_bound_to_agent_and_computer(self):
        registry = ScreenStreamRegistry()
        ticket, _ = registry.issue_ticket("agent-1", 7)
        self.assertIsNone(registry.consume_ticket(ticket, 8))
        self.assertIsNone(registry.consume_ticket(ticket, 7))

        ticket, _ = registry.issue_ticket("agent-1", 7)
        self.assertEqual(registry.consume_ticket(ticket, 7), "agent-1")
        self.assertIsNone(registry.consume_ticket(ticket, 7))

    def test_expired_tickets_are_removed_when_issuing_new_tickets(self):
        registry = ScreenStreamRegistry()
        with patch("server.services.screen_streams.time.monotonic") as monotonic:
            monotonic.return_value = 10
            expired_ticket, _ = registry.issue_ticket("agent-expired", 7, ttl_seconds=1)
            monotonic.return_value = 12
            fresh_ticket, _ = registry.issue_ticket("agent-fresh", 8)

            self.assertEqual(len(registry._tickets), 1)
            self.assertNotIn(expired_ticket, registry._tickets)
            self.assertEqual(registry.consume_ticket(fresh_ticket, 8), "agent-fresh")
            self.assertEqual(registry._tickets, {})

    def test_expired_ticket_is_rejected_and_removed_when_consumed(self):
        registry = ScreenStreamRegistry()
        with patch("server.services.screen_streams.time.monotonic") as monotonic:
            monotonic.return_value = 20
            ticket, _ = registry.issue_ticket("agent-1", 7, ttl_seconds=1)
            monotonic.return_value = 21

            self.assertIsNone(registry.consume_ticket(ticket, 7))
            self.assertEqual(registry._tickets, {})

    def test_capacity_rejects_without_evicting_valid_tickets(self):
        registry = ScreenStreamRegistry()
        with patch("server.services.screen_streams.MAX_PENDING_SCREEN_TICKETS", 2):
            first_ticket, _ = registry.issue_ticket("agent-1", 7)
            second_ticket, _ = registry.issue_ticket("agent-2", 8)
            with self.assertRaises(ScreenTicketCapacityError):
                registry.issue_ticket("agent-3", 9)

            self.assertEqual(len(registry._tickets), 2)
            self.assertEqual(registry.consume_ticket(first_ticket, 7), "agent-1")
            self.assertEqual(registry.consume_ticket(second_ticket, 8), "agent-2")

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
        registry.agent_started(
            "agent-1",
            {
                "type": "screen.start_ack",
                "accepted": True,
                "screen_width": 1920,
                "screen_height": 1080,
            },
        )
        self.assertTrue(registry.detach_and_stop_agent(viewer, connections))
        self.assertTrue(viewer.disconnected.is_set())
        self.assertFalse(registry.publish_frame("agent-1", {"type": "screen.frame"}))
        self.assertEqual(connection.outbound.get_nowait(), {"type": "screen.stop"})

    def test_control_session_requires_active_stream_and_audits_start_and_end(self):
        registry = ScreenStreamRegistry()
        viewer = registry.attach("agent-1", 7)
        self.assertFalse(registry.request_control_start(viewer))
        self.assertTrue(registry.request_start(viewer))
        registry.agent_started(
            "agent-1",
            {
                "type": "screen.start_ack",
                "accepted": True,
                "screen_width": 1920,
                "screen_height": 1080,
            },
        )
        viewer.control_queue.get_nowait()

        with self.assertLogs("prefeitura_remote.audit", level="INFO") as audit:
            self.assertTrue(registry.request_control_start(viewer))
            self.assertTrue(
                registry.agent_control_started(
                    "agent-1",
                    {
                        "type": "control.start_ack",
                        "accepted": True,
                        "screen_width": 1920,
                        "screen_height": 1080,
                    },
                )
            )
            self.assertEqual(viewer.control_state, "active")
            viewer.control_queue.get_nowait()
            self.assertTrue(registry.request_control_stop(viewer))
            self.assertTrue(
                registry.agent_control_stopped(
                    "agent-1", {"type": "control.stop_ack", "stopped": True}
                )
            )
            self.assertEqual(viewer.control_state, "idle")

        self.assertIn("remote_control_started technician=shared-dashboard-token", audit.output[0])
        self.assertIn("remote_control_ended technician=shared-dashboard-token", audit.output[1])


if __name__ == "__main__":
    unittest.main()