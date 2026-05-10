# -*- coding: utf-8 -*-
"""Platform abstraction layer — multiplexer backends, terminal launchers, OS glue.

The package name shadows the Python stdlib `platform` module from the perspective of
relative imports inside this package, but absolute imports of `platform` from
elsewhere in `cpsm.*` still resolve to the stdlib module per Python 3 rules.
"""
