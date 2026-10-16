"""Private fixture helpers. Never prebound or documented as agent targeting APIs."""
import asyncio
import time
from mai_ui import MuError


def node(observation, **fields):
    """Select fixture metadata exactly, rejecting ambiguity (no selector objects)."""
    nodes = [n for n in observation._tree.nodes
             if all(getattr(n, k) == v for k, v in fields.items())]
    if len(nodes) != 1:
        raise MuError('AMBIGUOUS_TARGET' if nodes else 'TARGET_NOT_FOUND')
    return observation._ref(nodes[0].id)


async def wait_for(ui, predicate, *, timeout_ms=5000):
    deadline = time.monotonic() + timeout_ms / 1000
    while True:
        remaining = deadline - time.monotonic()
        if remaining <= 0:
            raise MuError('CONDITION_TIMEOUT')
        observation = await ui.observe(timeout_ms=max(1, int(remaining * 1000)))
        if predicate(observation):
            return observation
        await asyncio.sleep(min(.075, max(0, deadline - time.monotonic())))
