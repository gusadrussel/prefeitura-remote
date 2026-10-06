import unittest

from server.services.agent_connections import AgentConnectionRegistry


class AgentConnectionRegistryTests(unittest.TestCase):
    def test_connection_lifecycle_tracks_heartbeat_and_disconnect(self):
        registry = AgentConnectionRegistry()
        connection = registry.connect("agent-1", 42)
        self.assertTrue(connection.connected)
        self.assertEqual(connection.agent_id, "agent-1")
        self.assertEqual(connection.computer_id, 42)
        initial_heartbeat = connection.last_heartbeat

        self.assertIs(registry.heartbeat("agent-1", connection.connection_id), connection)
        self.assertGreaterEqual(connection.last_heartbeat, initial_heartbeat)
        self.assertIs(registry.disconnect("agent-1", connection.connection_id), connection)
        self.assertFalse(registry.get("agent-1").connected)
        self.assertIsNone(registry.heartbeat("agent-1", connection.connection_id))

    def test_stale_connection_cannot_update_replacement_session(self):
        registry = AgentConnectionRegistry()
        first = registry.connect("agent-1", 42)
        second = registry.connect("agent-1", 42)
        self.assertIsNone(registry.disconnect("agent-1", first.connection_id))
        self.assertTrue(registry.get("agent-1").connected)
        self.assertEqual(registry.get("agent-1").connection_id, second.connection_id)

    def test_urgent_command_replaces_queued_controls(self):
        registry = AgentConnectionRegistry()
        connection = registry.connect("agent-1", 42)
        for index in range(connection.outbound.maxsize):
            self.assertTrue(registry.send("agent-1", {"type": "control.input", "index": index}))

        self.assertTrue(
            registry.send_urgent(
                "agent-1", {"type": "session.stop", "reason": "authorization_revoked"}
            )
        )
        self.assertEqual(
            connection.outbound.get_nowait(),
            {"type": "session.stop", "reason": "authorization_revoked"},
        )
        self.assertTrue(connection.outbound.empty())


if __name__ == "__main__":
    unittest.main()