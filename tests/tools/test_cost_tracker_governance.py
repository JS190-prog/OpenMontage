from __future__ import annotations

import json
import sys
import tempfile
import unittest
from pathlib import Path

ROOT = Path(__file__).resolve().parents[2]
sys.path.insert(0, str(ROOT))

from lib.config_model import BudgetConfig, BudgetMode
from tools.cost_tracker import CostTracker


class CostTrackerGovernanceTests(unittest.TestCase):
    def test_invalid_budget_settings_are_rejected_before_log_creation(self) -> None:
        cases = [
            (field, value)
            for field in ("total_usd", "reserve_pct", "single_action_approval_usd")
            for value in (-1, float("nan"), float("inf"), float("-inf"), True)
        ] + [("reserve_pct", 1.01)]
        constructor_fields = {"total_usd": "budget_total_usd"}
        with tempfile.TemporaryDirectory() as temp_dir:
            log_path = Path(temp_dir) / "unused" / "cost_log.json"
            for field, value in cases:
                with self.subTest(field=field, value=value):
                    with self.assertRaises(ValueError):
                        BudgetConfig(**{field: value})
                    with self.assertRaises(ValueError):
                        CostTracker(
                            **{constructor_fields.get(field, field): value},
                            cost_log_path=log_path,
                        )
                    self.assertFalse(log_path.parent.exists())

    def test_zero_costs_and_reserve_boundaries_remain_valid(self) -> None:
        for reserve_pct in (0, 1):
            for approval_required in (False, True):
                with self.subTest(reserve_pct=reserve_pct, approval=approval_required):
                    config = BudgetConfig(
                        total_usd=0,
                        reserve_pct=reserve_pct,
                        single_action_approval_usd=0,
                        require_approval_for_new_paid_tool=approval_required,
                    )
                    tracker = CostTracker(
                        budget_total_usd=config.total_usd,
                        reserve_pct=config.reserve_pct,
                        single_action_approval_usd=config.single_action_approval_usd,
                        require_approval_for_new_paid_tool=approval_required,
                        mode=BudgetMode.CAP,
                    )
                    entry_id = tracker.estimate("free", "generate", 0)
                    tracker.reserve(entry_id)
                    tracker.reconcile(entry_id, 0)
                    self.assertEqual(tracker.budget_remaining_usd, 0)
                    self.assertIs(tracker.require_approval_for_new_paid_tool, approval_required)

    def test_invalid_estimates_leave_entries_and_log_unchanged(self) -> None:
        with tempfile.TemporaryDirectory() as temp_dir:
            log_path = Path(temp_dir) / "cost_log.json"
            tracker = CostTracker(cost_log_path=log_path)
            tracker.estimate("paid_video", "generate", 0.1)
            before_entries = json.dumps(tracker.entries, sort_keys=True)
            before_log = log_path.read_bytes()
            for value in (-100, float("nan"), float("inf"), float("-inf"), True):
                with self.subTest(value=value):
                    with self.assertRaises(ValueError):
                        tracker.estimate("paid_video", "generate", value)
                    self.assertEqual(json.dumps(tracker.entries, sort_keys=True), before_entries)
                    self.assertEqual(log_path.read_bytes(), before_log)

    def test_invalid_entry_costs_cannot_bypass_reservation_guards(self) -> None:
        with tempfile.TemporaryDirectory() as temp_dir:
            log_path = Path(temp_dir) / "cost_log.json"
            tracker = CostTracker(mode=BudgetMode.CAP, cost_log_path=log_path)
            entry_id = tracker.estimate("paid_video", "generate", 0.1)
            before_log = log_path.read_bytes()
            entry = tracker.entries[0]
            original = dict(entry)
            for field in ("estimated_usd", "reserved_usd", "actual_usd"):
                for value in (-100, float("nan"), float("inf"), float("-inf"), True):
                    with self.subTest(field=field, value=value):
                        entry.clear()
                        entry.update(original)
                        entry[field] = value
                        before_entries = json.dumps(tracker.entries, sort_keys=True)
                        with self.assertRaises(ValueError):
                            tracker.reserve(entry_id)
                        self.assertEqual(json.dumps(tracker.entries, sort_keys=True), before_entries)
                        self.assertEqual(log_path.read_bytes(), before_log)

    def test_invalid_actual_costs_leave_reservation_and_log_unchanged(self) -> None:
        with tempfile.TemporaryDirectory() as temp_dir:
            log_path = Path(temp_dir) / "cost_log.json"
            tracker = CostTracker(mode=BudgetMode.OBSERVE, cost_log_path=log_path)
            entry_id = tracker.estimate("paid_video", "generate", 0.1)
            tracker.reserve(entry_id)
            before_entries = json.dumps(tracker.entries, sort_keys=True)
            before_log = log_path.read_bytes()
            for value in (-100, float("nan"), float("inf"), float("-inf"), True):
                with self.subTest(value=value):
                    with self.assertRaises(ValueError):
                        tracker.reconcile(entry_id, value)
                    self.assertEqual(json.dumps(tracker.entries, sort_keys=True), before_entries)
                    self.assertEqual(log_path.read_bytes(), before_log)

    def test_reconcile_rejects_corrupted_existing_costs_without_writing(self) -> None:
        with tempfile.TemporaryDirectory() as temp_dir:
            log_path = Path(temp_dir) / "cost_log.json"
            tracker = CostTracker(mode=BudgetMode.OBSERVE, cost_log_path=log_path)
            entry_id = tracker.estimate("paid_video", "generate", 0.1)
            tracker.reserve(entry_id)
            before_log = log_path.read_bytes()
            tracker.entries[0]["estimated_usd"] = float("nan")
            before_entries = json.dumps(tracker.entries, sort_keys=True)
            with self.assertRaises(ValueError):
                tracker.reconcile(entry_id, 0.05)
            self.assertEqual(json.dumps(tracker.entries, sort_keys=True), before_entries)
            self.assertEqual(log_path.read_bytes(), before_log)

    def test_invalid_persisted_costs_are_rejected_without_rewriting_the_log(self) -> None:
        with tempfile.TemporaryDirectory() as temp_dir:
            log_path = Path(temp_dir) / "cost_log.json"
            tracker = CostTracker(mode=BudgetMode.OBSERVE, cost_log_path=log_path)
            entry_id = tracker.estimate("paid_video", "generate", 0.1)
            tracker.reserve(entry_id)
            valid_log = log_path.read_text()
            fields = (
                "budget_total_usd", "budget_reserved_usd", "budget_spent_usd",
                "estimated_usd", "reserved_usd", "actual_usd",
            )
            for field in fields:
                for value in (-100, float("nan"), float("inf"), float("-inf"), True):
                    with self.subTest(field=field, value=value):
                        data = json.loads(valid_log)
                        target = data if field.startswith("budget_") else data["entries"][0]
                        target[field] = value
                        log_path.write_text(json.dumps(data))
                        before_log = log_path.read_bytes()
                        with self.assertRaises(ValueError):
                            CostTracker(cost_log_path=log_path)
                        self.assertEqual(log_path.read_bytes(), before_log)

    def test_warn_mode_marks_over_budget_reservation(self) -> None:
        with self.subTest("warning is recorded and persisted"):
            import tempfile
            from pathlib import Path

            with tempfile.TemporaryDirectory() as temp_dir:
                log_path = Path(temp_dir) / "cost_log.json"
                tracker = CostTracker(
                    budget_total_usd=1.0,
                    reserve_pct=0.0,
                    single_action_approval_usd=99.0,
                    require_approval_for_new_paid_tool=False,
                    mode=BudgetMode.WARN,
                    cost_log_path=log_path,
                )
                entry_id = tracker.estimate("paid_video", "generate", 2.0)

                tracker.reserve(entry_id)

                entry = tracker.entries[0]
                self.assertEqual(entry["status"], "reserved")
                self.assertEqual(entry["reserved_usd"], 2.0)
                self.assertTrue(entry["budget_warning"])
                self.assertIn("exceeds usable budget", entry["budget_warning_message"])
                persisted = json.loads(log_path.read_text())
                self.assertTrue(persisted["entries"][0]["budget_warning"])

    def test_approved_tools_persist_across_tracker_restarts(self) -> None:
        import tempfile
        from pathlib import Path

        with tempfile.TemporaryDirectory() as temp_dir:
            log_path = Path(temp_dir) / "cost_log.json"
            tracker = CostTracker(cost_log_path=log_path)
            tracker.approve_tool("paid_video")

            restarted = CostTracker(cost_log_path=log_path)

            entry_id = restarted.estimate("paid_video", "generate", 0.01)
            restarted.reserve(entry_id)
            self.assertEqual(restarted.entries[-1]["status"], "reserved")


if __name__ == "__main__":
    unittest.main()
