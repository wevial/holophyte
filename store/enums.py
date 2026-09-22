"""Store vocabularies and their SQL spelling; standard library only."""
from enum import Enum


class AutonomyProfile(str, Enum):
    PERSONAL = 'personal'
    SHARED_LOW_RISK = 'shared_low_risk'
    PRODUCTION = 'production'


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


class RunOutcome(str, Enum):
    MERGED = 'merged'
    KILLED = 'killed'
    ABANDONED = 'abandoned'
    FAILED = 'failed'
    REJECTED = 'rejected'


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
    ('tickets', 'status'): TicketStatus,
    ('tickets', 'affinity'): Affinity,
    ('runs', 'phase'): RunPhase,
    ('runs', 'outcome'): RunOutcome,
    ('runs', 'outcomeClass'): OutcomeClass,
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
