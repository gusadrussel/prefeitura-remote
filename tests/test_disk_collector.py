import unittest

from agent.collectors.disk import _windows_media_type, classify_rotational


class DiskClassificationTests(unittest.TestCase):
    def test_rotational_values_are_normalized(self):
        for value in (False, 0, "0", "false", "no"):
            with self.subTest(value=value):
                self.assertEqual(classify_rotational(value), "SSD")

        for value in (True, 1, "1", "true", "yes"):
            with self.subTest(value=value):
                self.assertEqual(classify_rotational(value), "HDD")

    def test_nvme_transport_takes_precedence(self):
        self.assertEqual(classify_rotational(True, name="nvme0n1"), "NVMe")
        self.assertEqual(classify_rotational(False, transport="nvme"), "NVMe")

    def test_unknown_values_stay_unknown(self):
        self.assertEqual(classify_rotational(None), "desconhecido")

    def test_windows_nvme_bus_type_is_preserved(self):
        self.assertEqual(_windows_media_type("SSD", "NVMe"), "NVMe")
        self.assertEqual(_windows_media_type("Fixed hard disk media"), "desconhecido")


if __name__ == "__main__":
    unittest.main()