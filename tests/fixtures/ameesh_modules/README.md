# Test module for external descriptors (#2847)

This directory is **test-only**. It is not packaged with ameesh and is never
installed in production.

`sample_module.py` is a minimal external descriptor (the five #1915/Q1 witness
types). The neighbouring `ameesh_sample_module-0.1.0.dist-info/` turns the
directory into a real distribution for `importlib.metadata`: adding this
directory to `sys.path` lets the tests exercise the actual
`ameesh.modules` entry-point discovery, not only a mocked one.

A production descriptor (for example ThePackengers' private one) is built and
installed from its own private repository the same way; its name and types are
chosen by its owner, and ameesh only provides the generic loading mechanism.
