"""In-memory application of complete node-operation websocket messages."""

from __future__ import annotations

from dataclasses import dataclass

from node_api.operation_service import (
    NodeOperationStreamEvent,
    NodeOperationStreamEventKind,
    NodeOperationView,
)


@dataclass(frozen=True, slots=True)
class ModWebNodeOperationSnapshot:
    """The live, non-persistent operation view currently reported by one node."""

    node_name: str
    operations: tuple[NodeOperationView, ...]

    def __post_init__(self) -> None:
        if not self.node_name.strip():
            raise ValueError("Operation snapshot node name must not be blank.")

    @classmethod
    def apply_event(
        cls,
        current: "ModWebNodeOperationSnapshot | None",
        event: NodeOperationStreamEvent,
    ) -> "ModWebNodeOperationSnapshot":
        """Apply a complete event without inventing client-side patch semantics."""

        if (
            current is not None
            and current.node_name.casefold() != event.node_name.casefold()
        ):
            raise ValueError("Cannot merge operation streams for different nodes.")
        if event.kind is NodeOperationStreamEventKind.SNAPSHOT:
            return cls(node_name=event.node_name, operations=event.operations)
        if current is None:
            raise ValueError(
                "Operation stream updates require an authoritative snapshot first."
            )
        operation = event.operation
        if operation is None:
            raise ValueError(
                "Operation stream update is missing its complete operation."
            )
        operations_by_id = {
            candidate.record.operation_id: candidate for candidate in current.operations
        }
        if event.kind is NodeOperationStreamEventKind.REMOVED:
            operations_by_id.pop(operation.record.operation_id, None)
        else:
            operations_by_id[operation.record.operation_id] = operation
        return cls(
            node_name=event.node_name,
            operations=tuple(
                sorted(
                    operations_by_id.values(),
                    key=lambda candidate: (
                        candidate.record.created_at_unix_ms,
                        candidate.record.operation_id,
                    ),
                    reverse=True,
                )
            ),
        )


__all__: tuple[str, ...] = ("ModWebNodeOperationSnapshot",)
