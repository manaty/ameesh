# SPDX-License-Identifier: AGPL-3.0-only
"""H5: bind the single pinned handoff factory to the runner.

The factory path is derived from this source tree, never from a message, a
job, a document or an environment variable: an agent may not choose the code
a controller runs. The runner binds the factory once, before ``readiness`` or
``advance_host`` use it, on its own controller connection; the companion's
durable reads then share the controller's committed view. An absent source, an
unsafe file or a refused enrollment leaves the binding empty and names the
same prerequisites (``helper:L232b``, ``requirements:L232c``) in both paths; it
never fabricates an adapter or a ready host.
"""
from __future__ import annotations

import importlib.util
import os
from pathlib import Path
import stat

#: fixed relative source of the factory, from the repository root
FACTORY_RELATIVE = Path("deploy") / "mise-a-jour" / "fabrique-handoff.py"
#: named prerequisites recorded by the controller when the factory refuses
HELPER_GAP = "helper:L232b"
REQUIREMENTS_GAP = "requirements:L232c"
#: our import name; a document or a message never chooses it
MODULE_NAME = "ameesh_handoff_factory"


class FactoryUnavailable(ValueError):
    """A stable category; never a document body, a path or a credential."""

    def __init__(self, gap: str = HELPER_GAP):
        self.gap = gap
        super().__init__(gap)


def source_path() -> Path:
    """The one factory source, relative to this installed tree."""
    return Path(__file__).resolve().parents[2] / FACTORY_RELATIVE


def _trusted(path: Path) -> None:
    """Refuse a replaced or writable source before loading it.

    The installed tree is root-owned and 0644 (H7); like the pinned companion,
    a group- or world-writable source is refused. Executing the factory is
    controller code, so its own file is checked here before import.
    """
    try:
        info = path.lstat()
    except OSError:
        raise FactoryUnavailable(HELPER_GAP) from None
    if (not stat.S_ISREG(info.st_mode) or info.st_uid not in (os.geteuid(), 0)
            or info.st_mode & 0o022):
        raise FactoryUnavailable(HELPER_GAP)


def _module(path: Path):
    spec = importlib.util.spec_from_file_location(MODULE_NAME, path)
    if spec is None or spec.loader is None:
        raise FactoryUnavailable(HELPER_GAP)
    module = importlib.util.module_from_spec(spec)
    try:
        spec.loader.exec_module(module)
    except Exception:
        raise FactoryUnavailable(HELPER_GAP) from None
    return module


def load(db):
    """Load the pinned factory and its enrolled adapter, or refuse by name.

    ``db`` is the controller's dedicated connection: the companion performs
    its durable reads there, never on an agent's connection. The factory owns
    the document and companion checks; this loader only pins the source and
    maps every refusal to a stable category.
    """
    if db is None:
        raise FactoryUnavailable(HELPER_GAP)
    path = source_path()
    _trusted(path)
    factory_class = getattr(_module(path), "HandoffFactory", None)
    if not callable(factory_class):
        raise FactoryUnavailable(HELPER_GAP)
    try:
        factory = factory_class(db)
    except Exception:
        raise FactoryUnavailable(HELPER_GAP) from None
    if (getattr(factory, "adapter", None) is None
            or not callable(getattr(factory, "requirements_for", None))):
        raise FactoryUnavailable(HELPER_GAP)
    return factory
