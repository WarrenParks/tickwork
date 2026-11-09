"""The public tickwork engine: a generic, instance-neutral workflow engine.

It drives states, transitions and sweeps over a queue of work items. It is
developed in-tree but deliberately names nothing of the instance it runs
for: no persona, host, repository or person names appear here, and the
package imports only the standard library, PyYAML and other ``tickwork``
modules — everything
instance-specific is supplied by the caller.
"""

__all__: list[str] = []
