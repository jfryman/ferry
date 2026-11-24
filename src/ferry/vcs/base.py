"""What the engine needs from a code host. GitLab today; GitHub or a webhook service later.

Keep this small: every method here is one more thing each backend must implement.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from typing import Protocol


@dataclass
class ChangeRequest:
    """A merge request / pull request."""

    id: int  # MR iid / PR number
    head_sha: str
    source_branch: str
    target_branch: str
    author: str
    state: str  # opened | merged | closed
    draft: bool = False
    has_conflicts: bool = False
    discussions_resolved: bool = True
    merge_status: str = "mergeable"  # backend-specific detail, shown in refusals
    blocked_reasons: list[str] = field(default_factory=list)  # backend-computed merge blockers
    web_url: str = ""

    @property
    def open(self) -> bool:
        return self.state == "opened"


@dataclass
class Note:
    id: int
    author: str
    body: str
    created_at: str = ""


class VCSBackend(Protocol):
    def get_change_request(self, cr_id: int) -> ChangeRequest: ...

    def merge_rule_blockers(self, cr: ChangeRequest) -> list[str]:
        """Reasons the code host's own merge rules (branch protection, rulesets, approval rules) would
        refuse to merge `cr` now, beyond `cr.blocked_reasons`. Asked only on apply; fail closed."""
        ...

    def changed_files(self, cr_id: int) -> list[str]: ...

    def approvers(self, cr_id: int) -> list[str]: ...

    def list_notes(self, cr_id: int) -> list[Note]: ...

    def post_note(self, cr_id: int, body: str) -> Note: ...

    def bot_username(self) -> str: ...

    def can_apply(self, username: str) -> bool:
        """Whether this user may trigger applies (e.g. Developer+ on the project)."""
        ...

    def change_requests_for_commit(self, sha: str) -> list[int]: ...
