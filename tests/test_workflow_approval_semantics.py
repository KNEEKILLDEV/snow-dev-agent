import unittest

from agent.orchestrator import enforce_workflow_step_contract, workflow_plan_has_suspicious_approval_structure
from validation.script_validator import validate_workflow_plan


REQUIREMENT = (
    'Build a workflow which Requires at least 2 approvals from the Cyber Security group '
    'for any external access request to move the item to "Approved."'
)


def build_workflow_plan(step_three_table="sysapproval_approver"):
    return {
        "name": "External Access Request Approval",
        "table": "sc_req_item",
        "description": (
            "Manages the approval process for external access requests, requiring at least "
            "two approvals from the Cyber Security group."
        ),
        "workflow_definition": {
            "workflow_kind": "approval",
            "approval_threshold": 2,
            "approval_group": "Cyber Security",
            "approval_subject": "External Access Request",
        },
        "workflow_steps": [
            {
                "step_key": "initialize_request",
                "artifact_type": "business_rule",
                "name": "Initialize External Access Request",
                "table": "sc_req_item",
                "when": "before",
                "insert": True,
                "update": False,
                "order": 1,
                "description": "Prepare the item",
                "purpose": "Prepare",
            },
            {
                "step_key": "request_approvals",
                "artifact_type": "business_rule",
                "name": "Request Cyber Security Approvals",
                "table": "sc_req_item",
                "when": "after",
                "insert": True,
                "update": False,
                "order": 2,
                "description": "Request approvals",
                "purpose": "Request",
            },
            {
                "step_key": "monitor_approval_status",
                "artifact_type": "business_rule",
                "name": "Monitor Cyber Security Approval Status",
                "table": step_three_table,
                "when": "after",
                "insert": False,
                "update": True,
                "order": 3,
                "description": "Monitor approvals",
                "purpose": "Monitor",
            },
            {
                "step_key": "finalize_request_outcome",
                "artifact_type": "business_rule",
                "name": "Finalize External Access Request Outcome",
                "table": "sc_req_item",
                "when": "after",
                "insert": False,
                "update": True,
                "order": 4,
                "description": "Finalize outcome",
                "purpose": "Finalize",
            },
        ],
    }


class WorkflowApprovalSemanticsTests(unittest.TestCase):
    def test_rejects_monitor_step_on_request_table(self):
        bad_plan = build_workflow_plan(step_three_table="sc_req_item")

        validation = validate_workflow_plan(bad_plan)

        self.assertFalse(validation["valid"])
        self.assertTrue(
            any(
                "approval monitoring steps must target sysapproval_approver" in issue
                for issue in validation["issues"]
            )
        )
        self.assertTrue(
            workflow_plan_has_suspicious_approval_structure(
                bad_plan,
                REQUIREMENT,
                "",
                "sc_req_item",
            )
        )

    def test_accepts_and_canonicalizes_monitor_step(self):
        good_plan = build_workflow_plan()

        validation = validate_workflow_plan(good_plan)

        self.assertTrue(validation["valid"])
        self.assertFalse(
            workflow_plan_has_suspicious_approval_structure(
                good_plan,
                REQUIREMENT,
                "",
                "sc_req_item",
            )
        )

        contracted = enforce_workflow_step_contract(
            {
                "step_key": "monitor_approval_status",
                "artifact_type": "business_rule",
                "name": "Monitor Cyber Security Approval Status",
                "table": "sc_req_item",
                "when": "after",
                "insert": False,
                "update": True,
                "order": 3,
            },
            workflow_kind="approval",
            target_table="sc_req_item",
        )

        self.assertEqual(contracted["table"], "sysapproval_approver")
        self.assertEqual(contracted["when"], "after")


if __name__ == "__main__":
    unittest.main()
