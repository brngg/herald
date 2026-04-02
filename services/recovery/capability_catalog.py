from __future__ import annotations

from schemas.intents import CapabilityCatalog


def default_capability_catalog() -> CapabilityCatalog:
    return CapabilityCatalog(
        version="phase2-shadow-v1",
        capabilities=[
            {
                "operation_family": "rollout.undo_deployment",
                "description": "Roll back a namespaced Deployment to its previous ReplicaSet.",
                "bounded_v1_action_type": "rollout_undo_deployment",
                "reversible": True,
                "supported_slices": ["crashloop", "bad_config"],
            },
            {
                "operation_family": "rollout.restart_deployment",
                "description": "Restart a namespaced Deployment to clear transient stateless issues.",
                "bounded_v1_action_type": "rollout_restart_deployment",
                "reversible": True,
                "supported_slices": ["crashloop"],
            },
            {
                "operation_family": "scale.deployment",
                "description": (
                    "Scale a namespaced stateless Deployment within a bounded replica range "
                    "when recovery depends on restoring a safe ready replica count."
                ),
                "bounded_v1_action_type": "scale_deployment",
                "reversible": True,
                "supported_slices": [
                    "dynamic_capacity",
                    "readiness_shortfall",
                ],
            },
            {
                "operation_family": "chaos.delete_stresschaos",
                "description": "Delete a namespaced StressChaos object that is causing synthetic CPU saturation.",
                "bounded_v1_action_type": "delete_stresschaos",
                "reversible": True,
                "supported_slices": ["cpu_saturation"],
            },
            {
                "operation_family": "chaos.delete_networkchaos",
                "description": "Delete a namespaced NetworkChaos object that is causing a synthetic dependency partition.",
                "bounded_v1_action_type": "delete_networkchaos",
                "reversible": True,
                "supported_slices": ["network_partition"],
            },
            {
                "operation_family": "escalate.human_review",
                "description": "Escalate to a human operator when bounded automated recovery is not clearly justified.",
                "bounded_v1_action_type": "escalate",
                "reversible": True,
                "supported_slices": [
                    "crashloop",
                    "cpu_saturation",
                    "bad_config",
                    "network_partition",
                ],
            },
        ],
    )
