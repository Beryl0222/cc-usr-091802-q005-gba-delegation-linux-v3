"""核对基础服务和湾区活动样例。"""

import json
import unittest
from pathlib import Path

from service import SERVICE_ID, health_payload


class BaselineContractTest(unittest.TestCase):
    def test_service_identity(self):
        self.assertEqual(health_payload()["service"], SERVICE_ID)

    def test_fixture_has_two_day_schedule(self):
        data = json.loads(Path("fixtures/sample.json").read_text(encoding="utf-8"))
        self.assertEqual(len(data["days"]), 2)
        self.assertEqual(len(data["teams"]), 4)


if __name__ == "__main__":
    unittest.main()
