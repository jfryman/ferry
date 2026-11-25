"""What the engine needs from a code host. GitLab today; GitHub or a webhook service later.

Keep this small: every method here is one more thing each backend must implement.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from typing import Protocol


class NotFound(LookupError):
    """The code host says the object doesn't exist (HTTP 404/410)."""


def is_not_found(error: BaseException) -> bool:
    """Only a definite "doesn't exist" from the code host. Timeouts, 5xx, rate limits and auth
    errors are not: callers that would give something away on "gone" must fail closed on those."""
    return isinstance(error, NotFound) or getattr(error, "status", None) in (404, 410)


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

    def behind_by(self, cr: ChangeRequest) -> int:
        """Commits on cr's target branch that cr's head doesn't contain (0: up to date). Raises when
        the code host can't say; callers fail closed."""
        ...

    def contributors(self, cr: ChangeRequest) -> set[str]:
        """Usernames who authored, committed or pushed commits in cr, as far as the code host tells.
        Their approvals don't count (#25). Raises when it can't tell; callers fail closed."""
        ...

    def set_status(self, sha: str, name: str, state: str, description: str, url: str | None = None) -> None:
        """Commit status on `sha` (#29). state: pending | running | success | failure."""
        ...

    def changed_files(self, cr_id: int) -> list[str]: ...

    def approvers(self, cr_id: int) -> list[str]: ...

    def list_notes(self, cr_id: int) -> list[Note]: ...

    def post_note(self, cr_id: int, body: str) -> Note: ...

    def edit_note(self, cr_id: int, note_id: int, body: str) -> Note:
        """Replace the body of a note ferry posted (#89). Raises if the code host refuses or the note
        is gone; callers fall back to post_note."""
        ...

    def react(self, cr_id: int, note_id: int, emoji: str = "eyes") -> None:
        """Best effort: mark someone's comment as seen (#89). Callers ignore failures."""
        ...

    def bot_username(self) -> str: ...

    def can_apply(self, username: str) -> bool:
        """Whether this user may trigger applies (e.g. Developer+ on the project)."""
        ...

    def change_requests_for_commit(self, sha: str) -> list[int]: ...
