from __future__ import annotations

from schemas.intents import (
    CapabilityCatalog,
    CapabilityParameterSpec,
    CapabilitySpec,
    VerificationRecipeSpec,
)


def default_capability_catalog() -> CapabilityCatalog:
    return CapabilityCatalog(
        version="phase7-capability-registry-v1",
        capabilities=[
            CapabilitySpec(
                capability_family="rollout.undo_deployment",
                description="Roll back a namespaced Deployment to its previous ReplicaSet.",
                layer="bounded_reversible",
                blast_radius_prior=0.3,
                reversible=True,
                required_parameters=[
                    CapabilityParameterSpec(name="namespace", description="Target namespace."),
                    CapabilityParameterSpec(name="deployment", description="Deployment to roll back."),
                ],
                allowed_read_tools=["get_deployment_context", "get_rollout_status"],
                mutation_tool="rollout_undo_deployment",
                verification_recipe=VerificationRecipeSpec(
                    check_types=["kubernetes_rollout_status", "prometheus_readiness_positive"],
                    success_criteria=[
                        "rollout reports succeeded",
                        "ready replicas become positive",
                    ],
                ),
                escalation_fallback="Escalate if rollback does not restore healthy ready replicas.",
                supported_slices=["crashloop", "bad_config"],
            ),
            CapabilitySpec(
                capability_family="rollout.restart_deployment",
                description="Restart a namespaced Deployment to clear transient stateless issues.",
                layer="bounded_reversible",
                blast_radius_prior=0.25,
                reversible=True,
                required_parameters=[
                    CapabilityParameterSpec(name="namespace", description="Target namespace."),
                    CapabilityParameterSpec(name="deployment", description="Deployment to restart."),
                ],
                allowed_read_tools=["get_deployment_context", "get_rollout_status"],
                mutation_tool="rollout_restart_deployment",
                verification_recipe=VerificationRecipeSpec(
                    check_types=["kubernetes_rollout_status", "prometheus_readiness_positive"],
                    success_criteria=[
                        "rollout reports succeeded",
                        "ready replicas recover without requiring rollback",
                    ],
                ),
                escalation_fallback="Escalate or roll back if restart does not stabilize the workload.",
                supported_slices=["crashloop", "transient_workload_fault"],
            ),
            CapabilitySpec(
                capability_family="scale.deployment",
                description=(
                    "Scale a namespaced stateless Deployment within a bounded replica range "
                    "when recovery depends on restoring a safe ready replica count."
                ),
                layer="bounded_reversible",
                blast_radius_prior=0.2,
                reversible=True,
                required_parameters=[
                    CapabilityParameterSpec(name="namespace", description="Target namespace."),
                    CapabilityParameterSpec(name="deployment", description="Deployment to scale."),
                    CapabilityParameterSpec(name="replicas", description="Bounded replica target."),
                ],
                allowed_read_tools=["get_deployment_context", "get_rollout_status"],
                mutation_tool="scale_deployment",
                verification_recipe=VerificationRecipeSpec(
                    check_types=["prometheus_ready_count_at_least"],
                    success_criteria=["ready replica count reaches the approved bounded target"],
                ),
                escalation_fallback="Escalate if scaling does not restore safe readiness.",
                supported_slices=["dynamic_capacity", "readiness_shortfall"],
            ),
            CapabilitySpec(
                capability_family="pod.delete_stateless_pod",
                description=(
                    "Delete a single unhealthy stateless Pod so its Deployment can replace it "
                    "without changing rollout history."
                ),
                layer="bounded_reversible",
                blast_radius_prior=0.15,
                reversible=True,
                required_parameters=[
                    CapabilityParameterSpec(name="namespace", description="Target namespace."),
                    CapabilityParameterSpec(name="pod", description="Pod to delete."),
                    CapabilityParameterSpec(name="deployment", description="Owning Deployment for verification."),
                ],
                allowed_read_tools=["get_pod_context", "get_deployment_context"],
                mutation_tool="delete_pod",
                verification_recipe=VerificationRecipeSpec(
                    check_types=["kubernetes_resource_absent", "prometheus_ready_count_at_least"],
                    success_criteria=[
                        "the unhealthy pod disappears",
                        "deployment readiness is preserved or restored",
                    ],
                ),
                escalation_fallback="Escalate if the pod belongs to a stateful or otherwise non-bounded workload.",
                supported_slices=["isolated_pod_fault", "transient_workload_fault"],
            ),
            CapabilitySpec(
                capability_family="chaos.delete_stresschaos",
                description="Delete a namespaced StressChaos object that is causing synthetic CPU saturation.",
                layer="bounded_reversible",
                blast_radius_prior=0.2,
                reversible=True,
                required_parameters=[
                    CapabilityParameterSpec(name="namespace", description="Target namespace."),
                    CapabilityParameterSpec(name="name", description="StressChaos resource name."),
                ],
                allowed_read_tools=["get_stresschaos"],
                mutation_tool="delete_stresschaos",
                verification_recipe=VerificationRecipeSpec(
                    check_types=["kubernetes_resource_absent", "prometheus_cpu_below_threshold"],
                    success_criteria=[
                        "chaos object is absent",
                        "cpu usage falls below the bounded threshold",
                    ],
                ),
                escalation_fallback="Escalate if CPU saturation persists after chaos removal.",
                supported_slices=["cpu_saturation"],
            ),
            CapabilitySpec(
                capability_family="chaos.delete_networkchaos",
                description="Delete a namespaced NetworkChaos object that is causing a synthetic dependency partition.",
                layer="bounded_reversible",
                blast_radius_prior=0.2,
                reversible=True,
                required_parameters=[
                    CapabilityParameterSpec(name="namespace", description="Target namespace."),
                    CapabilityParameterSpec(name="name", description="NetworkChaos resource name."),
                ],
                allowed_read_tools=["get_networkchaos"],
                mutation_tool="delete_networkchaos",
                verification_recipe=VerificationRecipeSpec(
                    check_types=[
                        "kubernetes_resource_absent",
                        "prometheus_network_receive_above_threshold",
                    ],
                    success_criteria=[
                        "chaos object is absent",
                        "dependency traffic recovers above the bounded threshold",
                    ],
                ),
                escalation_fallback="Escalate if dependency traffic does not recover after chaos removal.",
                supported_slices=["network_partition"],
            ),
            CapabilitySpec(
                capability_family="escalate.human_review",
                description="Escalate to a human operator when bounded automated recovery is not clearly justified.",
                layer="escalate_only",
                blast_radius_prior=0.0,
                reversible=True,
                required_parameters=[
                    CapabilityParameterSpec(name="reason", description="Why bounded automation is not justified."),
                ],
                allowed_read_tools=[],
                mutation_tool=None,
                verification_recipe=VerificationRecipeSpec(
                    check_types=[],
                    success_criteria=["human operator takes ownership of the next action"],
                ),
                escalation_fallback="Escalation is the terminal safe fallback.",
                supported_slices=[
                    "crashloop",
                    "cpu_saturation",
                    "bad_config",
                    "network_partition",
                    "dynamic_capacity",
                    "isolated_pod_fault",
                ],
            ),
        ],
    )
