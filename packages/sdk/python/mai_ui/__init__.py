"""mai-ui: portable Python UI vocabulary, native Rust core, pluggable execution."""

from .types import *
from .runtime import (
    connect,
    Connection,
    Session,
    Device,
    App,
    Display,
    Window,
    UI,
    Observation,
    PointRef,
    MuError,
    ShellError,
    Shell,
)
from .host import HostDiagnostics
from .adapters import Adapter, register_adapter

from .unified import Runtime

__version__ = "0.3.0"
