from __future__ import annotations

from schemas.intents import OperationIntent
from schemas.remediation import RemediationAction


def intent_to_v1_remediation(intent: OperationIntent) -> RemediationAction | None:
    namespace = intent.target.namespace or "default"
    name = intent.target.name or ""

    if intent.operation_family == "rollout.undo_deployment" and name:
        action_id = "rollout_undo_cartservice" if name == "cartservice" else "rollout_undo_frontend_bad_config"
        return RemediationAction(
            action_id=action_id,
            action_type="rollout_undo_deployment",
            description=f"Roll back {name} Deployment to the previous ReplicaSet.",
            confidence_score=intent.confidence_score,
            blast_radius_score=intent.blast_radius_score,
            requires_approval=intent.requires_approval,
            parameters={"namespace": namespace, "deployment": name},
        )

    if intent.operation_family == "rollout.restart_deployment" and name:
        return RemediationAction(
            action_id=f"restart_{name}",
            action_type="rollout_restart_deployment",
            description=f"Restart {name} Deployment to clear transient stateless issues.",
            confidence_score=intent.confidence_score,
            blast_radius_score=intent.blast_radius_score,
            requires_approval=intent.requires_approval,
            parameters={"namespace": namespace, "deployment": name},
        )

    if intent.operation_family == "scale.deployment" and name:
        replicas = int(intent.arguments.get("replicas", 1) or 1)
        return RemediationAction(
            action_id=f"scale_{name}_{replicas}",
            action_type="scale_deployment",
            description=f"Scale Deployment {name} in namespace {namespace} to {replicas} replicas.",
            confidence_score=intent.confidence_score,
            blast_radius_score=intent.blast_radius_score,
            requires_approval=intent.requires_approval,
            parameters={"namespace": namespace, "deployment": name, "replicas": replicas},
        )

    if intent.operation_family == "chaos.delete_stresschaos" and name:
        return RemediationAction(
            action_id="delete_frontend_cpu_stresschaos",
            action_type="delete_stresschaos",
            description="Delete the active frontend CPU StressChaos object to remove synthetic saturation.",
            confidence_score=intent.confidence_score,
            blast_radius_score=intent.blast_radius_score,
            requires_approval=intent.requires_approval,
            parameters={"namespace": namespace, "name": name},
        )

    if intent.operation_family == "chaos.delete_networkchaos" and name:
        return RemediationAction(
            action_id="delete_frontend_cartservice_network_partition",
            action_type="delete_networkchaos",
            description="Delete the active frontend-to-cartservice NetworkChaos partition object.",
            confidence_score=intent.confidence_score,
            blast_radius_score=intent.blast_radius_score,
            requires_approval=intent.requires_approval,
            parameters={"namespace": namespace, "name": name, "deployment": "cartservice"},
        )

    if intent.operation_family == "escalate.human_review":
        reason = str(intent.arguments.get("reason") or intent.intent)
        target_name = intent.target.name or "incident"
        return RemediationAction(
            action_id=f"escalate_{target_name.replace('-', '_')}",
            action_type="escalate",
            description="Escalate the incident for deeper human investigation.",
            confidence_score=intent.confidence_score,
            blast_radius_score=intent.blast_radius_score,
            requires_approval=intent.requires_approval,
            parameters={"reason": reason},
        )

    return None
