from __future__ import annotations

from dataclasses import dataclass, field
from typing import Any, Literal


OperationFamily = Literal[
    "rollout.undo_deployment",
    "rollout.restart_deployment",
    "scale.deployment",
    "pod.delete_stateless_pod",
    "chaos.delete_stresschaos",
    "chaos.delete_networkchaos",
    "escalate.human_review",
]

VALID_OPERATION_FAMILIES: tuple[OperationFamily, ...] = (
    "rollout.undo_deployment",
    "rollout.restart_deployment",
    "scale.deployment",
    "pod.delete_stateless_pod",
    "chaos.delete_stresschaos",
    "chaos.delete_networkchaos",
    "escalate.human_review",
)


@dataclass(slots=True)
class ResourceTarget:
    namespace: str | None
    kind: str
    name: str | None
    selector: dict[str, str] | None = None

    def __post_init__(self) -> None:
        if self.namespace is not None:
            if not isinstance(self.namespace, str):
                raise TypeError("namespace must be a str or None")
            if not self.namespace:
                raise ValueError("namespace must be non-empty when provided")
        if not isinstance(self.kind, str):
            raise TypeError("kind must be a str")
        if not self.kind:
            raise ValueError("kind must be non-empty")
        if self.name is not None:
            if not isinstance(self.name, str):
                raise TypeError("name must be a str or None")
            if not self.name:
                raise ValueError("name must be non-empty when provided")
        if self.selector is not None:
            if not isinstance(self.selector, dict):
                raise TypeError("selector must be a dict[str, str] or None")
            for key, value in self.selector.items():
                if not isinstance(key, str) or not key:
                    raise TypeError("selector keys must be non-empty strings")
                if not isinstance(value, str) or not value:
                    raise TypeError("selector values must be non-empty strings")


@dataclass(slots=True)
class OperationIntent:
    intent_id: str
    intent: str
    operation_family: OperationFamily
    target: ResourceTarget
    arguments: dict[str, Any]
    reversible: bool
    confidence_score: float
    blast_radius_score: float
    requires_approval: bool
    verification_hints: dict[str, Any]
    rollback_hints: dict[str, Any]

    def __post_init__(self) -> None:
        if not isinstance(self.intent_id, str):
            raise TypeError("intent_id must be a str")
        if not self.intent_id:
            raise ValueError("intent_id must be non-empty")
        if not isinstance(self.intent, str):
            raise TypeError("intent must be a str")
        if not self.intent:
            raise ValueError("intent must be non-empty")
        if self.operation_family not in VALID_OPERATION_FAMILIES:
            raise ValueError(f"unsupported operation_family: {self.operation_family}")
        if not isinstance(self.target, ResourceTarget):
            raise TypeError("target must be a ResourceTarget")
        if not isinstance(self.arguments, dict):
            raise TypeError("arguments must be a dict")
        if not isinstance(self.reversible, bool):
            raise TypeError("reversible must be a bool")
        if not _is_number(self.confidence_score):
            raise TypeError("confidence_score must be a float-compatible number")
        if self.confidence_score < 0.0 or self.confidence_score > 1.0:
            raise ValueError("confidence_score must be in the range [0.0, 1.0]")
        if not _is_number(self.blast_radius_score):
            raise TypeError("blast_radius_score must be a float-compatible number")
        if self.blast_radius_score < 0.0 or self.blast_radius_score > 1.0:
            raise ValueError("blast_radius_score must be in the range [0.0, 1.0]")
        if not isinstance(self.requires_approval, bool):
            raise TypeError("requires_approval must be a bool")
        if not self.requires_approval:
            raise ValueError("requires_approval must remain true in Phase 2")
        if not isinstance(self.verification_hints, dict):
            raise TypeError("verification_hints must be a dict")
        if not isinstance(self.rollback_hints, dict):
            raise TypeError("rollback_hints must be a dict")
        self.confidence_score = float(self.confidence_score)
        self.blast_radius_score = float(self.blast_radius_score)


@dataclass(slots=True)
class ReasonerOutput:
    diagnosis_summary: str
    likely_causes: list[str] = field(default_factory=list)
    missing_information: list[str] = field(default_factory=list)
    intents: list[OperationIntent] = field(default_factory=list)

    def __post_init__(self) -> None:
        if not isinstance(self.diagnosis_summary, str):
            raise TypeError("diagnosis_summary must be a str")
        if not self.diagnosis_summary:
            raise ValueError("diagnosis_summary must be non-empty")
        _validate_string_list(self.likely_causes, field_name="likely_causes")
        _validate_string_list(self.missing_information, field_name="missing_information")
        if not isinstance(self.intents, list):
            raise TypeError("intents must be a list[OperationIntent]")
        for intent in self.intents:
            if not isinstance(intent, OperationIntent):
                raise TypeError("intents must contain only OperationIntent values")


@dataclass(slots=True)
class CapabilityParameterSpec:
    name: str
    description: str
    required: bool = True

    def __post_init__(self) -> None:
        if not isinstance(self.name, str):
            raise TypeError("name must be a str")
        if not self.name:
            raise ValueError("name must be non-empty")
        if not isinstance(self.description, str):
            raise TypeError("description must be a str")
        if not self.description:
            raise ValueError("description must be non-empty")
        if not isinstance(self.required, bool):
            raise TypeError("required must be a bool")


@dataclass(slots=True)
class VerificationRecipeSpec:
    check_types: list[str] = field(default_factory=list)
    success_criteria: list[str] = field(default_factory=list)

    def __post_init__(self) -> None:
        _validate_string_list(self.check_types, field_name="check_types")
        _validate_string_list(self.success_criteria, field_name="success_criteria")


@dataclass(slots=True)
class CapabilitySpec:
    capability_family: OperationFamily
    description: str
    layer: str
    blast_radius_prior: float
    reversible: bool
    required_parameters: list[CapabilityParameterSpec] = field(default_factory=list)
    allowed_read_tools: list[str] = field(default_factory=list)
    mutation_tool: str | None = None
    verification_recipe: VerificationRecipeSpec = field(default_factory=VerificationRecipeSpec)
    escalation_fallback: str = ""
    supported_slices: list[str] = field(default_factory=list)

    def __post_init__(self) -> None:
        if self.capability_family not in VALID_OPERATION_FAMILIES:
            raise ValueError(f"unsupported capability_family: {self.capability_family}")
        if not isinstance(self.description, str):
            raise TypeError("description must be a str")
        if not self.description:
            raise ValueError("description must be non-empty")
        if not isinstance(self.layer, str):
            raise TypeError("layer must be a str")
        if not self.layer:
            raise ValueError("layer must be non-empty")
        if not _is_number(self.blast_radius_prior):
            raise TypeError("blast_radius_prior must be a float-compatible number")
        if self.blast_radius_prior < 0.0 or self.blast_radius_prior > 1.0:
            raise ValueError("blast_radius_prior must be in the range [0.0, 1.0]")
        if not isinstance(self.reversible, bool):
            raise TypeError("reversible must be a bool")
        if not isinstance(self.required_parameters, list):
            raise TypeError("required_parameters must be a list[CapabilityParameterSpec]")
        normalized_parameters: list[CapabilityParameterSpec] = []
        for parameter in self.required_parameters:
            if isinstance(parameter, dict):
                parameter = capability_parameter_from_dict(parameter)
            if not isinstance(parameter, CapabilityParameterSpec):
                raise TypeError("required_parameters must contain only CapabilityParameterSpec values")
            normalized_parameters.append(parameter)
        self.required_parameters = normalized_parameters
        _validate_string_list(self.allowed_read_tools, field_name="allowed_read_tools")
        if self.mutation_tool is not None:
            if not isinstance(self.mutation_tool, str):
                raise TypeError("mutation_tool must be a str or None")
            if not self.mutation_tool:
                raise ValueError("mutation_tool must be non-empty when provided")
        if isinstance(self.verification_recipe, dict):
            self.verification_recipe = verification_recipe_from_dict(self.verification_recipe)
        if not isinstance(self.verification_recipe, VerificationRecipeSpec):
            raise TypeError("verification_recipe must be a VerificationRecipeSpec")
        if not isinstance(self.escalation_fallback, str):
            raise TypeError("escalation_fallback must be a str")
        if not self.escalation_fallback:
            raise ValueError("escalation_fallback must be non-empty")
        _validate_string_list(self.supported_slices, field_name="supported_slices")
        self.blast_radius_prior = float(self.blast_radius_prior)


@dataclass(slots=True)
class CapabilityCatalog:
    version: str
    capabilities: list[CapabilitySpec] = field(default_factory=list)

    def __post_init__(self) -> None:
        if not isinstance(self.version, str):
            raise TypeError("version must be a str")
        if not self.version:
            raise ValueError("version must be non-empty")
        if not isinstance(self.capabilities, list):
            raise TypeError("capabilities must be a list[CapabilitySpec]")
        normalized_capabilities: list[CapabilitySpec] = []
        for capability in self.capabilities:
            if isinstance(capability, dict):
                capability = capability_spec_from_dict(capability)
            if not isinstance(capability, CapabilitySpec):
                raise TypeError("capabilities must contain only CapabilitySpec values")
            normalized_capabilities.append(capability)
        self.capabilities = normalized_capabilities


def reasoner_output_from_dict(payload: dict[str, Any]) -> ReasonerOutput:
    if not isinstance(payload, dict):
        raise TypeError("ReasonerOutput payload must be a dict")

    intents_raw = payload.get("intents", [])
    if not isinstance(intents_raw, list):
        raise TypeError("ReasonerOutput intents must be a list")

    intents = [_operation_intent_from_dict(item) for item in intents_raw]
    likely_causes = _string_list(payload.get("likely_causes"), field_name="likely_causes")
    missing_information = _string_list(
        payload.get("missing_information"),
        field_name="missing_information",
    )

    return ReasonerOutput(
        diagnosis_summary=str(payload["diagnosis_summary"]),
        likely_causes=likely_causes,
        missing_information=missing_information,
        intents=intents,
    )


def capability_catalog_from_dict(payload: dict[str, Any]) -> CapabilityCatalog:
    if not isinstance(payload, dict):
        raise TypeError("CapabilityCatalog payload must be a dict")
    capabilities_raw = payload.get("capabilities", [])
    if not isinstance(capabilities_raw, list):
        raise TypeError("CapabilityCatalog capabilities must be a list")
    return CapabilityCatalog(
        version=str(payload["version"]),
        capabilities=[capability_spec_from_dict(item) for item in capabilities_raw],
    )


def _resource_target_from_dict(payload: dict[str, Any]) -> ResourceTarget:
    if not isinstance(payload, dict):
        raise TypeError("ResourceTarget payload must be a dict")

    namespace = payload.get("namespace")
    if namespace is not None:
        namespace = str(namespace)
    name = payload.get("name")
    if name is not None:
        name = str(name)

    selector_raw = payload.get("selector")
    selector: dict[str, str] | None = None
    if selector_raw is not None:
        if not isinstance(selector_raw, dict):
            raise TypeError("selector must be a dict[str, str] when provided")
        selector = {str(key): str(value) for key, value in selector_raw.items()}

    return ResourceTarget(
        namespace=namespace,
        kind=str(payload["kind"]),
        name=name,
        selector=selector,
    )


def _operation_intent_from_dict(payload: dict[str, Any]) -> OperationIntent:
    if not isinstance(payload, dict):
        raise TypeError("OperationIntent payload must be a dict")
    return OperationIntent(
        intent_id=str(payload["intent_id"]),
        intent=str(payload["intent"]),
        operation_family=payload["operation_family"],
        target=_resource_target_from_dict(payload["target"]),
        arguments=dict(payload.get("arguments", {})),
        reversible=bool(payload["reversible"]),
        confidence_score=float(payload["confidence_score"]),
        blast_radius_score=float(payload["blast_radius_score"]),
        requires_approval=bool(payload["requires_approval"]),
        verification_hints=dict(payload.get("verification_hints", {})),
        rollback_hints=dict(payload.get("rollback_hints", {})),
    )


def capability_parameter_from_dict(payload: dict[str, Any]) -> CapabilityParameterSpec:
    if not isinstance(payload, dict):
        raise TypeError("CapabilityParameterSpec payload must be a dict")
    return CapabilityParameterSpec(
        name=str(payload["name"]),
        description=str(payload["description"]),
        required=bool(payload.get("required", True)),
    )


def verification_recipe_from_dict(payload: dict[str, Any]) -> VerificationRecipeSpec:
    if not isinstance(payload, dict):
        raise TypeError("VerificationRecipeSpec payload must be a dict")
    return VerificationRecipeSpec(
        check_types=_string_list(payload.get("check_types"), field_name="check_types"),
        success_criteria=_string_list(
            payload.get("success_criteria"),
            field_name="success_criteria",
        ),
    )


def capability_spec_from_dict(payload: dict[str, Any]) -> CapabilitySpec:
    if not isinstance(payload, dict):
        raise TypeError("CapabilitySpec payload must be a dict")
    capability_family = payload.get("capability_family", payload.get("operation_family"))
    if capability_family is None:
        raise KeyError("CapabilitySpec payload must include capability_family")
    required_parameters = payload.get("required_parameters", [])
    if not isinstance(required_parameters, list):
        raise TypeError("CapabilitySpec required_parameters must be a list")
    verification_recipe = payload.get("verification_recipe", {})
    if not isinstance(verification_recipe, dict):
        raise TypeError("CapabilitySpec verification_recipe must be a dict")
    return CapabilitySpec(
        capability_family=capability_family,
        description=str(payload.get("description") or capability_family),
        layer=str(payload.get("layer") or "bounded"),
        blast_radius_prior=float(payload.get("blast_radius_prior", 0.2)),
        reversible=bool(payload.get("reversible", True)),
        required_parameters=[capability_parameter_from_dict(item) for item in required_parameters],
        allowed_read_tools=_string_list(
            payload.get("allowed_read_tools"),
            field_name="allowed_read_tools",
        ),
        mutation_tool=str(payload["mutation_tool"]) if payload.get("mutation_tool") is not None else None,
        verification_recipe=verification_recipe_from_dict(verification_recipe),
        escalation_fallback=str(payload.get("escalation_fallback") or "escalate.human_review"),
        supported_slices=_string_list(
            payload.get("supported_slices"),
            field_name="supported_slices",
        ),
    )


def _string_list(value: Any, *, field_name: str) -> list[str]:
    if value is None:
        return []
    if not isinstance(value, list):
        raise TypeError(f"{field_name} must be a list[str]")
    values = [str(item) for item in value]
    _validate_string_list(values, field_name=field_name)
    return values


def _validate_string_list(value: Any, *, field_name: str) -> None:
    if not isinstance(value, list):
        raise TypeError(f"{field_name} must be a list[str]")
    for item in value:
        if not isinstance(item, str):
            raise TypeError(f"{field_name} must contain only strings")
        if not item:
            raise ValueError(f"{field_name} must not contain empty strings")


def _is_number(value: Any) -> bool:
    return isinstance(value, (int, float)) and not isinstance(value, bool)
