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


# Resume accepts the same vocabulary, with its own nullable SQL spelling.
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
    ('interventions', 'source'): InterventionSource,
    ('interventions', 'trigger'): InterventionTrigger,
    ('interventions', 'action'): InterventionAction,
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

# The phase writer's legal edges, also rendered in the README. Resume owns
# re-entry from a parked or failed run. Squashing is declared but unused by
# the --no-ff merge path, so it has no outgoing edge.
RUN_PHASE_TRANSITIONS = {
    # `claimed -> merge_gate` is the approved candidate's run: `--approve`
    # ended the parked run with `resumePhase = 'merge_gate'`, and the claim
    # that follows reuses its worktree and branch and goes straight to the
    # gate -- nothing to implement or review, the candidate already was.
    RunPhase.CLAIMED.value: frozenset({
        RunPhase.WORKING.value, RunPhase.MERGE_GATE.value,
        # _sync_branch_from_origin parks a diverged carried PR before the gate.
        RunPhase.AWAITING_MERGE_APPROVAL.value,
        RunPhase.FAILED.value, RunPhase.KILLED.value}),
    RunPhase.WORKING.value: frozenset({
        RunPhase.VERIFYING.value, RunPhase.FAILED.value,
        RunPhase.KILLED.value}),
    RunPhase.VERIFYING.value: frozenset({
        RunPhase.REVIEWING.value, RunPhase.FAILED.value,
        # _review_fix parks verification results for human approval.
        RunPhase.AWAITING_MERGE_APPROVAL.value,
        RunPhase.KILLED.value}),
    RunPhase.REVIEWING.value: frozenset({
        RunPhase.ADDRESSING.value, RunPhase.MERGE_GATE.value,
        # _review_fix retries one rejected fix, or parks its review verdict.
        RunPhase.VERIFYING.value, RunPhase.AWAITING_MERGE_APPROVAL.value,
        RunPhase.FAILED.value, RunPhase.KILLED.value}),
    RunPhase.ADDRESSING.value: frozenset({
        RunPhase.VERIFYING.value, RunPhase.FAILED.value,
        RunPhase.KILLED.value}),
    # `merge_gate -> done` is the pull request a person merged on GitHub
    # while the babysitter still watched it (KO-653): the run ends merged
    # with that merge commit, and records no `merging` step it never took.
    # The factory's own merges still go `merge_gate -> merging -> done`.
    RunPhase.MERGE_GATE.value: frozenset({
        RunPhase.MERGING.value, RunPhase.DONE.value,
        # The babysitter verifies each new fix before its covering review.
        RunPhase.VERIFYING.value,
        RunPhase.AWAITING_MERGE_APPROVAL.value, RunPhase.FAILED.value,
        RunPhase.KILLED.value, RunPhase.REJECTED.value}),
    # `awaiting_merge_approval -> done` is the pull request a person merged
    # on GitHub while the run waited for `--approve`: the loop's reconcile
    # ends the parked run merged with that merge commit (KO-359). The
    # operator's own `--approve` still ends it `failed` (abandoned) and lets
    # the next run merge.
    RunPhase.AWAITING_MERGE_APPROVAL.value: frozenset({
        RunPhase.DONE.value,
        RunPhase.FAILED.value, RunPhase.KILLED.value, RunPhase.REJECTED.value}),
    RunPhase.MERGING.value: frozenset({
        RunPhase.DONE.value, RunPhase.FAILED.value,
        # A refused PR merge retries conflict fixes and the merge gate.
        RunPhase.VERIFYING.value, RunPhase.MERGE_GATE.value,
        # _merge_pr parks a GitHub merge refusal on the open PR.
        RunPhase.AWAITING_MERGE_APPROVAL.value,
        # _run_after parks when a post-merge command fails.
        RunPhase.BLOCKED_ON_OPERATOR.value,
        RunPhase.KILLED.value}),
    RunPhase.SQUASHING.value: frozenset(),
    RunPhase.DONE.value: frozenset(),
    # `resume()`: a failed run re-enters its `resumePhase`, or `working`
    # when none was recorded; a `blocked_on_operator` run always re-enters
    # `working`.
    RunPhase.FAILED.value: RESUMABLE_WORK_PHASES,
    RunPhase.BLOCKED_ON_OPERATOR.value: frozenset({RunPhase.WORKING.value}),
    RunPhase.KILLED.value: frozenset(),
    RunPhase.REJECTED.value: frozenset(),
}
# KO-589: cooperative stop is legal from every live working boundary.
for _phase in ("claimed", "working", "verifying", "reviewing", "addressing",
               "merge_gate", "merging", "squashing", "awaiting_merge_approval"):
    RUN_PHASE_TRANSITIONS[_phase] |= {"paused"}
RUN_PHASE_TRANSITIONS["paused"] = frozenset({
    "working", "verifying", "reviewing", "addressing", "merge_gate", "merging"})
assert set(RUN_PHASE_TRANSITIONS) == {e.value for e in RunPhase}
