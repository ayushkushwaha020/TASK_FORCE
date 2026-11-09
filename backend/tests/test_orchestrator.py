import os
import unittest
from unittest.mock import patch

from app.orchestrator import create_plan
from app.schemas import PlanRequest


class OrchestratorTests(unittest.IsolatedAsyncioTestCase):
    async def test_demo_plan_always_includes_independent_qa(self):
        with patch.dict(os.environ, {"AI_API_KEY": "", "AI_MODEL": ""}):
            plan = await create_plan(PlanRequest(
                instruction="Build a small website for my college club."
            ))
        self.assertEqual(plan.execution_mode, "demo")
        self.assertGreaterEqual(len(plan.agents), 3)
        self.assertLessEqual(len(plan.agents), 20)
        self.assertTrue(any("qa" in agent.role.lower() or "verif" in agent.role.lower() for agent in plan.agents))
        self.assertEqual(plan.qa_status, "pending_execution")
        self.assertIn("No live AI agents", plan.notice)

    async def test_research_request_is_classified(self):
        with patch.dict(os.environ, {"AI_API_KEY": "", "AI_MODEL": ""}):
            plan = await create_plan(PlanRequest(
                instruction="Research and compare multi-agent AI orchestration frameworks."
            ))
        self.assertEqual(plan.category, "research")
        self.assertTrue(any("Research" in agent.role for agent in plan.agents))


if __name__ == "__main__":
    unittest.main()
