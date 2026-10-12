'''
State machine for tracking a single job application through its full lifecycle.

States
------
DISCOVERED      Job card was found in the search results.
MATCHED         Passed all pre-click filters (blacklist, bad words, experience, sponsorship).
PREPARING       Easy-Apply modal is open; resume uploaded; questions being answered.
NEEDS_INPUT     Bot is blocked on one or more unanswered questions.
AWAITING_APPROVAL   pause_before_submit dialog is shown to the user.
SUBMITTED       Application was submitted (or the external link was saved).
VERIFIED        Submitted record was written to the CSV without errors.

Error states (terminal, non-retryable within a single run)
-----------------------------------------------------------
SKIPPED         Intentionally not applied (blacklisted, over-experience, dry-run, etc.).
FAILED          An unexpected exception aborted the attempt.

Allowed transitions
-------------------
DISCOVERED  -> MATCHED | SKIPPED | FAILED
MATCHED     -> PREPARING | SKIPPED | FAILED
PREPARING   -> NEEDS_INPUT | AWAITING_APPROVAL | SUBMITTED | FAILED
NEEDS_INPUT -> PREPARING | SKIPPED | FAILED   (re-enters PREPARING after manual help)
AWAITING_APPROVAL -> SUBMITTED | SKIPPED | FAILED
SUBMITTED   -> VERIFIED | FAILED
VERIFIED    -> (terminal - nothing further)
SKIPPED     -> (terminal)
FAILED      -> (terminal)
'''

from __future__ import annotations

import time
import logging
from enum import Enum, auto
from dataclasses import dataclass, field

logger = logging.getLogger("auto_job_applier")


# ---------------------------------------------------------------------------
# State definition
# ---------------------------------------------------------------------------

class JobState(Enum):
    # Progress states
    DISCOVERED        = auto()
    MATCHED           = auto()
    PREPARING         = auto()
    NEEDS_INPUT       = auto()
    AWAITING_APPROVAL = auto()
    SUBMITTED         = auto()
    VERIFIED          = auto()
    # Terminal error states
    SKIPPED           = auto()
    FAILED            = auto()


# ---------------------------------------------------------------------------
# Allowed transitions table
# ---------------------------------------------------------------------------

_TRANSITIONS: dict[JobState, frozenset[JobState]] = {
    JobState.DISCOVERED:        frozenset({JobState.MATCHED,           JobState.SKIPPED, JobState.FAILED}),
    JobState.MATCHED:           frozenset({JobState.PREPARING,         JobState.SKIPPED, JobState.FAILED}),
    JobState.PREPARING:         frozenset({JobState.NEEDS_INPUT,
                                           JobState.AWAITING_APPROVAL,
                                           JobState.SUBMITTED,         JobState.SKIPPED, JobState.FAILED}),
    JobState.NEEDS_INPUT:       frozenset({JobState.PREPARING,         JobState.SKIPPED, JobState.FAILED}),
    JobState.AWAITING_APPROVAL: frozenset({JobState.SUBMITTED,         JobState.SKIPPED, JobState.FAILED}),
    JobState.SUBMITTED:         frozenset({JobState.VERIFIED,                            JobState.FAILED}),
    # Terminal states - no further transitions allowed
    JobState.VERIFIED:          frozenset(),
    JobState.SKIPPED:           frozenset(),
    JobState.FAILED:            frozenset(),
}

# Human-readable short labels for logging
_STATE_LABEL: dict[JobState, str] = {
    JobState.DISCOVERED:        "DISCOVERED",
    JobState.MATCHED:           "MATCHED",
    JobState.PREPARING:         "PREPARING",
    JobState.NEEDS_INPUT:       "NEEDS_INPUT",
    JobState.AWAITING_APPROVAL: "AWAITING_APPROVAL",
    JobState.SUBMITTED:         "SUBMITTED",
    JobState.VERIFIED:          "VERIFIED",
    JobState.SKIPPED:           "SKIPPED",
    JobState.FAILED:            "FAILED",
}


# ---------------------------------------------------------------------------
# Immutable snapshot of a single transition
# ---------------------------------------------------------------------------

@dataclass(frozen=True)
class StateEvent:
    from_state: JobState
    to_state:   JobState
    reason:     str
    timestamp:  float = field(default_factory=time.monotonic)


# ---------------------------------------------------------------------------
# Per-job state machine
# ---------------------------------------------------------------------------

class JobStateMachine:
    '''
    Tracks the full lifecycle of a single job application.

    Usage
    -----
    sm = JobStateMachine(job_id="12345", title="SWE", company="Acme")
    sm.transition(JobState.MATCHED, "passed all pre-click filters")
    sm.transition(JobState.PREPARING, "Easy Apply modal opened")
    sm.transition(JobState.SUBMITTED, "submit button clicked")
    sm.transition(JobState.VERIFIED, "record written to CSV")
    '''

    def __init__(self, job_id: str, title: str = "Unknown", company: str = "Unknown") -> None:
        self.job_id:  str = job_id
        self.title:   str = title
        self.company: str = company

        self._state:   JobState          = JobState.DISCOVERED
        self._history: list[StateEvent]  = []
        self._metadata: dict[str, object] = {}

        logger.info("[SM] %s | %s @ %s -> %s",
                    job_id, title, company, _STATE_LABEL[self._state])

    # ------------------------------------------------------------------
    # Core transition
    # ------------------------------------------------------------------

    def transition(self, new_state: JobState, reason: str = "") -> "JobStateMachine":
        '''
        Move to `new_state`, recording the event in the history.
        Raises `InvalidTransitionError` for illegal moves.
        Returns `self` so calls can be chained.
        '''
        if new_state not in _TRANSITIONS[self._state]:
            raise InvalidTransitionError(
                "[SM] {}: {} -> {} is not a valid transition. Allowed: {}".format(
                    self.job_id,
                    _STATE_LABEL[self._state],
                    _STATE_LABEL[new_state],
                    [_STATE_LABEL[s] for s in _TRANSITIONS[self._state]],
                )
            )
        event = StateEvent(from_state=self._state, to_state=new_state, reason=reason)
        self._history.append(event)
        self._state = new_state
        logger.info("[SM] %s | %s -> %s%s",
                    self.job_id,
                    _STATE_LABEL[event.from_state],
                    _STATE_LABEL[new_state],
                    " ({})".format(reason) if reason else "")
        return self

    # ------------------------------------------------------------------
    # Convenience transition helpers (match the caller's vocabulary)
    # ------------------------------------------------------------------

    def mark_matched(self, reason: str = "passed all pre-click filters") -> "JobStateMachine":
        return self.transition(JobState.MATCHED, reason)

    def mark_preparing(self, reason: str = "Easy Apply modal opened") -> "JobStateMachine":
        return self.transition(JobState.PREPARING, reason)

    def mark_needs_input(self, unanswered: set[str] | None = None) -> "JobStateMachine":
        detail = ""
        if unanswered:
            detail = "; ".join(sorted(unanswered)[:5])   # cap log length
            if len(unanswered) > 5:
                detail += " (+{} more)".format(len(unanswered) - 5)
        return self.transition(JobState.NEEDS_INPUT,
                               "unanswered: {}".format(detail) if detail else "unanswered questions")

    def mark_awaiting_approval(self) -> "JobStateMachine":
        return self.transition(JobState.AWAITING_APPROVAL, "pause_before_submit shown to user")

    def mark_submitted(self, application_link: str = "") -> "JobStateMachine":
        return self.transition(JobState.SUBMITTED,
                               "link={}".format(application_link) if application_link else "submitted")

    def mark_verified(self) -> "JobStateMachine":
        return self.transition(JobState.VERIFIED, "record written to CSV")

    def mark_skipped(self, reason: str = "") -> "JobStateMachine":
        return self.transition(JobState.SKIPPED, reason)

    def mark_failed(self, reason: str = "") -> "JobStateMachine":
        return self.transition(JobState.FAILED, reason)

    # ------------------------------------------------------------------
    # Metadata store - attach arbitrary info without polluting the state
    # ------------------------------------------------------------------

    def set(self, key: str, value: object) -> None:
        '''Attach metadata (resume path, questions, HR name, ...) to this machine.'''
        self._metadata[key] = value

    def get(self, key: str, default: object = None) -> object:
        return self._metadata.get(key, default)

    # ------------------------------------------------------------------
    # Queries
    # ------------------------------------------------------------------

    @property
    def state(self) -> JobState:
        return self._state

    @property
    def is_terminal(self) -> bool:
        return self._state in {JobState.VERIFIED, JobState.SKIPPED, JobState.FAILED}

    @property
    def is_success(self) -> bool:
        return self._state == JobState.VERIFIED

    @property
    def history(self) -> list[StateEvent]:
        return list(self._history)   # defensive copy

    def elapsed_seconds(self) -> float:
        '''Wall-clock seconds from DISCOVERED until now (or until terminal state).'''
        if not self._history:
            return 0.0
        start = self._history[0].timestamp
        end   = self._history[-1].timestamp if self.is_terminal else time.monotonic()
        return end - start

    def summary(self) -> str:
        '''One-line summary suitable for end-of-run logs.'''
        trail = " -> ".join(_STATE_LABEL[e.from_state] for e in self._history)
        if self._history:
            trail += " -> {}".format(_STATE_LABEL[self._state])
        else:
            trail = _STATE_LABEL[self._state]
        return "[SM] {} | {} @ {} | {} | {:.1f}s".format(
            self.job_id, self.title, self.company, trail, self.elapsed_seconds()
        )

    def __repr__(self) -> str:
        return "JobStateMachine(job_id={!r}, state={}, title={!r})".format(
            self.job_id, _STATE_LABEL[self._state], self.title
        )


# ---------------------------------------------------------------------------
# Custom exception
# ---------------------------------------------------------------------------

class InvalidTransitionError(RuntimeError):
    '''Raised when a caller tries an illegal state transition.'''
