"""ferry: Atlantis-style plan/apply/drift for Ansible.

The engine (ferry.engine) is independent of where it runs. GitLab CI is one driver (ferry.cli); a
webhook service is another (ferry.webhook). VCS access goes through ferry.vcs.base.VCSBackend and
locking through ferry.locks.LockStore, so new backends slot in without touching the engine.
"""

__version__ = "0.3.0"
