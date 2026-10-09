"""Store vocabularies and their SQL spelling; standard library only."""
from enum import Enum


class AutonomyProfile(str, Enum):
    PERSONAL = 'personal'
    SHARED_LOW_RISK = 'shared_low_risk'
    PRODUCTION = 'production'


class ProjectAdmission(str, Enum):
    ENABLED = 'enabled'
    HELD = 'held'
    DISABLED = 'disabled'


class TicketStatus(str, Enum):
    NEEDS_SPEC = 'needs_spec'
    READY = 'ready'
    IN_FLIGHT = 'in_flight'
    BLOCKED_ON_DEPS = 'blocked_on_deps'
    BLOCKED_ON_OPERATOR = 'blocked_on_operator'
    MERGED = 'merged'
    ABANDONED = 'abandoned'


class BoardColumn(str, Enum):
    BACKLOG = 'backlog'
    READY = 'ready'
    CANCELED = 'canceled'


class Affinity(str, Enum):
    ANY = 'any'
    GUI = 'gui'
    HEADLESS = 'headless'


class RunPhase(str, Enum):
    CLAIMED = 'claimed'
    WORKING = 'working'
    VERIFYING = 'verifying'
    REVIEWING = 'reviewing'
    ADDRESSING = 'addressing'
    MERGE_GATE = 'merge_gate'
    AWAITING_MERGE_APPROVAL = 'awaiting_merge_approval'
    MERGING = 'merging'
    SQUASHING = 'squashing'
    DONE = 'done'
    BLOCKED_ON_OPERATOR = 'blocked_on_operator'
    FAILED = 'failed'
    KILLED = 'killed'
    REJECTED = 'rejected'
    PAUSED = 'paused'


class ParkKind(str, Enum):
    PULL_REQUEST = 'pull_request'
    PULL_REQUEST_CLOSED = 'pull_request_closed'
    THREAD = 'thread'
    FIX_DECLINED = 'fix_declined'
    MERGE_LOCK = 'merge_lock'
    QUESTION = 'question'
    NOT_REPRODUCED = 'not_reproduced'
    CI = 'ci'


class RunOutcome(str, Enum):
    MERGED = 'merged'
    KILLED = 'killed'
    ABANDONED = 'abandoned'
    FAILED = 'failed'
    REJECTED = 'rejected'
    PAUSED = 'paused'


class FailureKind(str, Enum):
    VERIFY = 'verify'
    REVIEW_ROUTE = 'review_route'
    FIX_NO_PROGRESS = 'fix_no_progress'
    NO_COMMITS = 'no_commits'
    BUDGET = 'budget'
    MERGE_LOCK = 'merge_lock'
    INFRA = 'infra'
    SWEPT = 'swept'
    UNCLASSIFIED = 'unclassified'


class OutcomeClass(str, Enum):
    WORK = 'work'
    INFRA = 'infra'


# RunPhase's values under its own nullable SQL spelling.
ResumePhase = Enum("ResumePhase", {e.name: e.value for e in RunPhase}, type=str)


class ReviewVerdict(str, Enum):
    PASS = 'pass'
    CHANGES_REQUESTED = 'changes_requested'
    ERROR = 'error'


class EventLevel(str, Enum):
    NARRATIVE = 'narrative'
    DETAIL = 'detail'


class LedgerKind(str, Enum):
    MERGE = 'merge'
    FAILURE = 'failure'
    ROUND = 'round'
    ADJUDICATION = 'adjudication'
    INTERVENTION = 'intervention'
    NOTE = 'note'


class LedgerSource(str, Enum):
    LOOP = 'loop'
    OPERATOR = 'operator'


class InterventionSource(str, Enum):
    SUPERVISOR = 'supervisor'
    HUMAN = 'human'
    FACTORY = 'factory'


class InterventionTrigger(str, Enum):
    TIME_BOX = 'time_box'
    OFF_CRITERIA = 'off_criteria'
    LOOPING = 'looping'
    REVIEW_STUCK = 'review_stuck'
    LINEAR_CANCELLED = 'linear_cancelled'
    LINEAR_COMPLETED = 'linear_completed'
    MANUAL = 'manual'
    BOARD_CANCELLED = 'board_cancelled'


class InterventionAction(str, Enum):
    REDIRECT = 'redirect'
    KILL = 'kill'
    EXTEND_TIME_BOX = 'extend_time_box'
    RESUME = 'resume'
    CLOSE_OUT = 'close_out'
    REQUEUE = 'requeue'
    APPROVE = 'approve'
    REPOINT = 'repoint'
    BABYSIT = 'babysit'
    RECONCILE = 'reconcile'
    RESTART_SUPERVISOR = 'restart_supervisor'
    LAUNCH_LOOP = 'launch_loop'
    LAUNCH_BACKOFF = 'launch_backoff'
    ROUTE_FALLBACK = 'route_fallback'
    CONFIG_EDIT = 'config_edit'
    OPERATOR_NOTE = 'operator_note'
    MIGRATE = 'migrate'
    HOLD = 'hold'
    RELEASE_HOLD = 'release_hold'
    REGISTER_PROJECT = 'register_project'
    DISABLE = 'disable'
    PAUSE = 'pause'
    ABORT = 'abort'
    ABORT_CLOSE = 'abort_close'
    APPROVE_STORY = 'approve_story'
    DECIDE = 'decide'


class GapLayer(str, Enum):
    IMPOSSIBLE = 'impossible'
    STATIC = 'static'
    WITNESS = 'witness'
    GUIDANCE = 'guidance'
    REVIEW = 'review'
    NONE = 'none'


class StoryState(str, Enum):
    PLANNED = 'planned'
    APPROVED = 'approved'
    PARKED = 'parked'
    CLOSED = 'closed'
    ABANDONED = 'abandoned'


class ChildRole(str, Enum):
    COMPLETES = 'completes'
    ADVANCES = 'advances'
    SCAFFOLDING = 'scaffolding'


class DecisionKind(str, Enum):
    UNMET = 'unmet'
    REGRESSED = 'regressed'
    PLAN_DRIFT = 'plan_drift'


class WitnessVerdict(str, Enum):
    ABSENT = 'absent'
    RED = 'red'
    GREEN = 'green'
    ERROR = 'error'


class RedKind(str, Enum):
    ASSERT = 'assert'
    EXCEPTION = 'exception'


class WitnessVerifier(str, Enum):
    BASELINE = 'baseline'
    LOOP = 'loop'
    OPERATOR = 'operator'


class GapFinder(str, Enum):
    OPERATOR = 'operator'
    WITNESS = 'witness'


class FollowUpKind(str, Enum):
    FEATURE = 'feature'
    GUARDRAIL = 'guardrail'


class ProposalState(str, Enum):
    PROPOSED = 'proposed'
    ACCEPTED = 'accepted'
    REJECTED = 'rejected'
    SUPERSEDED = 'superseded'


# Line breaks are part of the existing sqlite_master SQL contract.
_WRAPPING = {
    TicketStatus: {4: 26},
    RunPhase: {4: 25, 7: 25, 11: 25},
    ResumePhase: {4: 34, 6: 34, 8: 34, 11: 34},
    LedgerKind: {4: 24},
    InterventionTrigger: {3: 29, 5: 29},
    InterventionAction: {4: 28, 8: 28, 11: 28, 14: 28},
}

CONSTRAINED_COLUMNS = {
    ('projects', 'autonomyProfile'): AutonomyProfile,
    ('projects', 'admission'): ProjectAdmission,
    ('tickets', 'status'): TicketStatus,
    ('tickets', 'affinity'): Affinity,
    ('tickets', 'boardColumn'): BoardColumn,
    ('runs', 'phase'): RunPhase,
    ('runs', 'parkKind'): ParkKind,
    ('runs', 'outcome'): RunOutcome,
    ('runs', 'outcomeClass'): OutcomeClass,
    ('runs', 'failureKind'): FailureKind,
    ('runs', 'resumePhase'): ResumePhase,
    ('reviewRounds', 'verdict'): ReviewVerdict,
    ('runEvents', 'level'): EventLevel,
    ('ledger', 'kind'): LedgerKind,
    ('ledger', 'source'): LedgerSource,
    ('ticketRevisions', 'boardColumn'): BoardColumn,
    ('interventions', 'source'): InterventionSource,
    ('interventions', 'trigger'): InterventionTrigger,
    ('interventions', 'action'): InterventionAction,
    ('gapLayers', 'layer'): GapLayer,
    ('gapLayers', 'foundBy'): GapFinder,
    ('stories', 'state'): StoryState,
    ('storyChildren', 'role'): ChildRole,
    ('witnessResults', 'verdict'): WitnessVerdict,
    ('witnessResults', 'redKind'): RedKind,
    ('witnessResults', 'verifier'): WitnessVerifier,
    ('storyDecisions', 'kind'): DecisionKind,
    ('followUps', 'kind'): FollowUpKind,
    ('storyProposals', 'state'): ProposalState,
}


def check_clause(column, enum):
    """Render a CHECK with the existing quoting, nullability and whitespace."""
    column = f'"{column}"' if column in {"trigger", "action"} else column
    wrapping = _WRAPPING.get(enum, {})
    values = ""
    for index, member in enumerate(enum):
        if index:
            values += ",\n" + " " * wrapping[index] if index in wrapping else ", "
        values += "'" + member.value.replace("'", "''") + "'"
    nullable = (f"{column} IS NULL\n               OR "
                if enum in {RunOutcome, ResumePhase} else "")
    return f"CHECK ({nullable}{column} IN ({values}))"


RESUMABLE_WORK_PHASES = frozenset(
    {RunPhase.WORKING.value, RunPhase.VERIFYING.value, RunPhase.REVIEWING.value,
     RunPhase.ADDRESSING.value}
)

# Rendered in the README too; squashing is declared but unused by --no-ff merges.
RUN_PHASE_TRANSITIONS = {
    # `-> merge_gate`: an approved candidate's run reuses its worktree.
    RunPhase.CLAIMED.value: frozenset({
        RunPhase.WORKING.value, RunPhase.MERGE_GATE.value,
        RunPhase.AWAITING_MERGE_APPROVAL.value,
        RunPhase.FAILED.value, RunPhase.KILLED.value}),
    RunPhase.WORKING.value: frozenset({
        RunPhase.VERIFYING.value, RunPhase.FAILED.value,
        RunPhase.KILLED.value}),
    RunPhase.VERIFYING.value: frozenset({
        RunPhase.REVIEWING.value, RunPhase.FAILED.value,
        RunPhase.AWAITING_MERGE_APPROVAL.value,
        RunPhase.KILLED.value}),
    RunPhase.REVIEWING.value: frozenset({
        RunPhase.ADDRESSING.value, RunPhase.MERGE_GATE.value,
        RunPhase.VERIFYING.value, RunPhase.AWAITING_MERGE_APPROVAL.value,
        RunPhase.FAILED.value, RunPhase.KILLED.value}),
    RunPhase.ADDRESSING.value: frozenset({
        RunPhase.VERIFYING.value, RunPhase.FAILED.value,
        RunPhase.KILLED.value}),
    # `-> done` here and below: a person merged the pull request on GitHub.
    RunPhase.MERGE_GATE.value: frozenset({
        RunPhase.MERGING.value, RunPhase.DONE.value,
        RunPhase.VERIFYING.value,
        RunPhase.AWAITING_MERGE_APPROVAL.value, RunPhase.FAILED.value,
        RunPhase.KILLED.value, RunPhase.REJECTED.value}),
    RunPhase.AWAITING_MERGE_APPROVAL.value: frozenset({
        RunPhase.DONE.value,
        RunPhase.FAILED.value, RunPhase.KILLED.value, RunPhase.REJECTED.value}),
    RunPhase.MERGING.value: frozenset({
        RunPhase.DONE.value, RunPhase.FAILED.value,
        RunPhase.VERIFYING.value, RunPhase.MERGE_GATE.value,
        RunPhase.AWAITING_MERGE_APPROVAL.value,
        RunPhase.BLOCKED_ON_OPERATOR.value,
        RunPhase.KILLED.value}),
    RunPhase.SQUASHING.value: frozenset(),
    RunPhase.DONE.value: frozenset(),
    RunPhase.FAILED.value: RESUMABLE_WORK_PHASES,
    RunPhase.BLOCKED_ON_OPERATOR.value: frozenset({RunPhase.WORKING.value}),
    RunPhase.KILLED.value: frozenset(),
    RunPhase.REJECTED.value: frozenset(),
}
for _phase in ("claimed", "working", "verifying", "reviewing", "addressing",
               "merge_gate", "merging", "squashing", "awaiting_merge_approval"):
    RUN_PHASE_TRANSITIONS[_phase] |= {"paused"}
RUN_PHASE_TRANSITIONS["paused"] = frozenset({
    "working", "verifying", "reviewing", "addressing", "merge_gate", "merging"})
assert set(RUN_PHASE_TRANSITIONS) == {e.value for e in RunPhase}
