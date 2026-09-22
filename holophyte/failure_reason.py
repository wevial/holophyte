"""Bounded failure sentences and the structured facts behind them."""
import json


def line(value):
    return ' '.join(str(value).split())


class Reason(str):
    """A normal reason string carrying lossless facts to close-out."""

    def __new__(cls, text, facts, failure_kind="unclassified"):
        value = super().__new__(cls, line(text)[:400])
        value.facts = facts
        value.failure_kind = failure_kind
        return value


def compose(kind, **facts):
    """One line, at most 400 characters; full facts remain in the event."""
    if kind == 'verify':
        text = (f"verify failed: command {facts.get('command_index', '?')} "
                f"[{line(facts.get('command', ''))[:120]}], "
                f"exit {facts.get('exit_status', 'unknown')}; "
                f"{line(facts.get('last_output_line', '(no output)'))}")
    elif kind == 'adjudication':
        criteria = '; '.join(
            f"criterion {c['number']} {c['status']}: {line(c['text'])[:65]}"
            for c in facts['criteria'])
        text = (f"terminal adjudication: {facts['decision']}; "
                f"{criteria or 'no criterion detail supplied'}")
    elif kind == 'fix_round':
        state = 'timed out' if facts['timed_out'] else 'made no progress'
        text = (f"fix round {state}; {facts['open_count']} findings open; "
                f"first: {facts['first_title']}")
    else:
        raise ValueError(f'unknown failure kind: {kind}')
    if facts.get('context'):
        text += f"; {facts['context']}"
    failure_kind = {'verify': 'verify', 'adjudication': 'unclassified',
                    'fix_round': ('budget' if facts.get('timed_out')
                                  else 'fix_no_progress')}[kind]
    return Reason(text, {'kind': kind, **facts}, failure_kind)


def verify(output, command, context):
    """Use gate facts, retaining a useful fallback for contract-only failures."""
    facts = getattr(output, 'failure', None) or {
        'command_index': None, 'command': command or '', 'exit_status': None,
        'last_output_line': str(output).splitlines()[-1] if output else '(no output)',
    }
    return compose('verify', **facts, context=context)


def adjudication(reply, criteria, decision, context):
    from holophyte.review import criteria_block

    block = criteria_block(reply)
    failed = [{'number': n, 'status': block.get(n, ('unwitnessed', ''))[0],
               'text': text}
              for n, text in enumerate(criteria, 1)
              if block.get(n, ('unwitnessed', ''))[0] != 'met']
    return compose('adjudication', decision=decision, criteria=failed,
                   context=context)


def fix_round(findings, timed_out, context):
    titles = [f.get('title') or (f.get('message', '').splitlines() or ['(untitled)'])[0]
              for f in findings if not f.get('evidence_only')]
    return compose('fix_round', timed_out=timed_out, open_count=len(titles),
                   first_title=titles[0] if titles else '(none recorded)',
                   context=context)


def record(conn, run_id, reason):
    """The same string the failure card reads, with JSON facts beside it."""
    import store

    if conn is not None and getattr(reason, 'facts', None):
        store.record_event(conn, run_id, 'failure', str(reason),
                           level='detail', payload=json.dumps(reason.facts))
