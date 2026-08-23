"""Rendering: judgment decides, code renders.

``kernel`` is markdown composition with zero dex vocabulary; ``surfaces``
names the reports and validates their payloads loudly. Never hand-draw a
report — there is a surface for it.
"""

from .surfaces import PayloadError, render

__all__ = ["PayloadError", "render"]
