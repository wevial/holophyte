"""Locate opted-in session files and render only known transcript records."""
import json
import re
from pathlib import Path

SESSION_ID = re.compile(r'[A-Za-z0-9][A-Za-z0-9_-]{0,199}\Z')


def locate(kind, session_id, scratch_root):
    """Find a session under one allowed root; never follow an escaping link.

    Codex roots are sessions directories. Devin exports live anywhere below
    a directory named for the session, allowing the operator's per-run layout.
    """
    if not isinstance(session_id, str) or not SESSION_ID.fullmatch(session_id):
        return None
    root = Path(scratch_root).resolve()
    patterns = {'codex': f'**/*-{session_id}.jsonl',
                'devin': f'**/{session_id}/**/*.json'}
    if kind not in patterns:
        return None
    for candidate in sorted(root.glob(patterns[kind])):
        try:
            resolved = candidate.resolve(strict=True)
            if resolved.is_relative_to(root) and resolved.is_file():
                return resolved
        except (OSError, RuntimeError):
            continue
    return None


def decoded(text):
    """A JSON object, or an empty one for unknown/malformed input."""
    try:
        value = json.loads(text)
    except (ValueError, TypeError):
        return {}
    return value if isinstance(value, dict) else {}


def command_text(item):
    """Known shell calls and Codex's executable tool-orchestration input."""
    name = item.get('name')
    if name in ('exec', 'apply_patch'):
        text = item.get('input')
        return text if isinstance(text, str) else None
    args = item.get('arguments', {})
    if isinstance(args, str):
        args = decoded(args)
    if not isinstance(args, dict):
        return None
    if name in ('exec_command', 'shell', 'shell_command'):
        command = args.get('cmd', args.get('command'))
        if isinstance(command, list) and all(isinstance(x, str) for x in command):
            return ' '.join(command)
        return command if isinstance(command, str) else None
    if name == 'wait':
        return f"wait for cell {args.get('cell_id', '')}"
    if name == 'write_stdin':
        return f"stdin: {args.get('chars', '')}"
    return None


def tool_text(output):
    """Plain tool text or the known structured shell result, never raw JSON."""
    if isinstance(output, list):
        texts = [tool_text(part.get('text')) for part in output
                 if isinstance(part, dict) and part.get('type') in
                 ('text', 'input_text', 'output_text')]
        return '\n'.join(text for text in texts if text is not None) or None
    if isinstance(output, str):
        if not output.lstrip().startswith(('{', '[')):
            return output
        output = decoded(output)
    if not isinstance(output, dict) or not isinstance(output.get('output'), str):
        return None
    text = output['output']
    status = output.get('exit_code')
    return text + (f'\nExit code: {status}' if isinstance(status, int) else '')


def codex_entry(record, calls):
    """Response items are canonical; event_msg mirrors are intentionally skipped."""
    item = record.get('payload')
    if record.get('type') != 'response_item' or not isinstance(item, dict):
        return []
    kind = item.get('type')
    if kind == 'message' and item.get('role') in ('user', 'assistant'):
        content = item.get('content', [])
        if not isinstance(content, list):
            return []
        return [(item['role'], part['text']) for part in content
                if isinstance(part, dict) and part.get('type') in
                ('input_text', 'output_text') and isinstance(part.get('text'), str)]
    if kind in ('function_call', 'custom_tool_call'):
        text = command_text(item)
        if text is not None and isinstance(item.get('call_id'), str):
            calls.add(item['call_id'])
            return [('command', text)]
    if kind in ('function_call_output', 'custom_tool_call_output'):
        if isinstance(item.get('call_id'), str) and item['call_id'] in calls:
            text = tool_text(item.get('output'))
            return [('tool', text)] if text is not None else []
    return []


def devin_commands(step):
    """Only render observations paired with a recognized command call."""
    entries, known = [], set()
    calls = step.get('tool_calls', [])
    for call in calls if isinstance(calls, list) else []:
        if not isinstance(call, dict) or call.get('function_name') != 'exec':
            continue
        args = call.get('arguments', {})
        text = args.get('command') if isinstance(args, dict) else None
        call_id = call.get('tool_call_id')
        if isinstance(text, str) and isinstance(call_id, str):
            entries.append(('command', text))
            known.add(call_id)
    observation = step.get('observation', {})
    results = observation.get('results', []) if isinstance(observation, dict) else []
    for result in results if isinstance(results, list) else []:
        if not isinstance(result, dict):
            continue
        call_id, content = result.get('source_call_id'), result.get('content')
        if isinstance(call_id, str) and call_id in known and isinstance(content, str):
            entries.append(('tool', content))
    return entries


def devin_entries(document):
    """Devin export steps carry commands and observations on the agent step."""
    entries = []
    steps = document.get('steps', [])
    if not isinstance(steps, list):
        return entries
    for step in steps:
        if not isinstance(step, dict):
            continue
        source, message = step.get('source'), step.get('message')
        speaker = ('user' if source == 'user' else
                   'assistant' if source == 'agent' else None)
        if speaker and isinstance(message, str) and message:
            entries.append((speaker, message))
        if source == 'agent':
            entries.extend(devin_commands(step))
    return entries


def render(path):
    """Read a Codex JSONL rollout or a Devin JSON export as (speaker, text).

    Unknown records and malformed lines are skipped, including a partial last
    line while an agent is still writing. No metadata is ever rendered raw.
    """
    path = Path(path)
    with path.open(encoding='utf-8') as stream:
        if path.suffix == '.json':
            return devin_entries(decoded(stream.read()))
        entries, calls = [], set()
        for line in stream:
            entries.extend(codex_entry(decoded(line), calls))
        return entries


def turns(events):
    """Join telemetry in sequence order, respecting each producer's ordering.

    Review session events precede their turn; implement session events follow
    it. A missing session never inherits a previous turn's handle; adjudicate
    and write turns are listed with their recorded label but no session.
    """
    result, pending = [], {}
    for seq, kind, payload in events:
        data = decoded(payload)
        role, route = data.get('role'), data.get('route')
        if role not in ('implement', 'review', 'adjudicate', 'write'):
            continue
        key = role, route
        if kind == 'agent_session':
            if role not in ('implement', 'review'):
                continue
            if role == 'implement':
                if result and (result[-1]['role'], result[-1]['route']) == key:
                    result[-1]['session_id'] = data.get('session_id')
            else:
                pending[key] = data.get('session_id')
        elif kind == 'agent_turn':
            label = data.get('label')
            result.append(dict(id=seq, role=role, route=route,
                               label=label if isinstance(label, str) else None,
                               seconds=data.get('seconds'),
                               session_id=pending.pop(key, None)))
    return result
