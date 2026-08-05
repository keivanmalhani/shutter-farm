"""shutter-farm: the deployment story for a local-first toolchain.

Local-first is a promise about where the data goes, not a claim that the
software cannot be operated properly. This is the same pipeline as a
container: point it at a media volume, and it discovers work, dispatches
to shutter-cull or shutter-select, keeps a ledger so scheduled runs are
idempotent and resumable, and emits structured logs and Prometheus metrics
so an unattended batch is observable.

The data still never leaves the volume you mounted.
"""

__version__ = "0.1.0"
