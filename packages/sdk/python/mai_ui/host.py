"""Embedding-host diagnostics, deliberately separate from the agent vocabulary."""
from types import MappingProxyType
from .types import Capability


class HostDiagnostics:
    def __init__(self, session):
        self._session = session

    async def capabilities(self):
        """Negotiated provider readiness; never captures UI or emits output."""
        return MappingProxyType({
            name: Capability(**value)
            for name, value in self._session._description.get("capabilities", {}).items()
        })

    def observation(self, observation):
        """Inspect captured structured state without reads or baseline changes."""
        from .runtime import Observation, MuError
        from .types import ObservationDiagnostics
        if not isinstance(observation, Observation) or observation._ui._session is not self._session:
            raise MuError("INVALID_ARGUMENT", "foreign observation")
        return ObservationDiagnostics(
            tree=observation._tree, ax_text=observation._ax_text,
            delta=observation._delta, indices=observation._indices,
        )
