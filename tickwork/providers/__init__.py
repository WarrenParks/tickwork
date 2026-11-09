"""Provider implementations for :mod:`tickwork.replay`.

Each provider is a stdlib-only adapter speaking one API shape, imported
here so a CLI can map a ``--provider <name>`` flag to an adapter without
the replay core knowing about transports.
"""

from tickwork.providers.openai_compat import OpenAICompatProvider

__all__ = ["OpenAICompatProvider"]
