"""Core: resource model, observation delivery, staleness (alias of mai_ui.unified)."""
from mai_ui import unified as _unified

globals().update({k: v for k, v in vars(_unified).items() if not k.startswith("_")})
