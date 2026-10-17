import json
import re
from datetime import datetime, timezone
from pathlib import Path

from holophyte.agents.agent_output import count

SESSION_ID = re.compile(r'[A-Za-z0-9][A-Za-z0-9_-]{0,199}\Z')


def locate(kind, session_id, scratch_root):
    """Find a session under one allowed root; never follow an escaping link."""
    if not isinstance(session_id, str) or not SESSION_ID.fullmatch(session_id):
        return None
    root = Path(scratch_root).resolve()
    patterns = {'codex': f'**/*-{session_id}.jsonl',
                'devin': f'**/{session_id}/**/*.json',
                'claude': f'**/{session_id}.jsonl'}
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
    try:
        value = json.loads(text)
    except (ValueError, TypeError):
        return {}
    return value if isinstance(value, dict) else {}


def command_text(item):
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


def entry(speaker, text, record=None, tool=None, call=None, failed=False, summary=None):
    timestamp = (record or {}).get('timestamp')
    try:
        at = datetime.fromisoformat(timestamp.replace('Z', '+00:00'))
        at_ms = int(at.replace(tzinfo=at.tzinfo or timezone.utc).timestamp() * 1000)
    except (AttributeError, ValueError, TypeError, OverflowError):
        at_ms = None
    return dict(speaker=speaker, text=text, at_ms=at_ms, tool=tool, call=call,
                failed=failed or speaker == 'error', summary=summary)


def result_failed(output):
    if isinstance(output, str):
        output = decoded(output)
    if isinstance(output, list):
        return any(result_failed(part.get('text')) for part in output
                   if isinstance(part, dict))
    return (isinstance(output, dict) and type(output.get('exit_code')) is int
            and output['exit_code'] != 0)


def codex_summary(item, text):
    name = item.get('name')
    if name in ('exec', 'exec_command', 'shell', 'shell_command'):
        return text.split('\n', 1)[0]
    args = item.get('arguments', {})
    if isinstance(args, str):
        args = decoded(args)
    return args.get('description') or name if isinstance(args, dict) else name


def codex_entry(record, calls):
    """Response items are canonical; event_msg mirrors are skipped on purpose."""
    item = record.get('payload')
    if record.get('type') != 'response_item' or not isinstance(item, dict):
        return []
    kind = item.get('type')
    if kind == 'message' and item.get('role') in ('user', 'assistant'):
        content = item.get('content', [])
        if not isinstance(content, list):
            return []
        return [entry(item['role'], part['text'], record) for part in content
                if isinstance(part, dict) and part.get('type') in
                ('input_text', 'output_text') and isinstance(part.get('text'), str)]
    if kind in ('function_call', 'custom_tool_call'):
        text = command_text(item)
        if text is not None and isinstance(item.get('call_id'), str):
            calls.add(item['call_id'])
            return [entry('command', text, record, item.get('name'), item['call_id'],
                          summary=codex_summary(item, text))]
    if kind in ('function_call_output', 'custom_tool_call_output'):
        if isinstance(item.get('call_id'), str) and item['call_id'] in calls:
            text = tool_text(item.get('output'))
            return ([entry('tool', text, record, call=item['call_id'],
                           failed=result_failed(item.get('output')))]
                    if text is not None else [])
    return []


def devin_commands(step):
    entries, known = [], set()
    calls = step.get('tool_calls', [])
    for call in calls if isinstance(calls, list) else []:
        if not isinstance(call, dict) or call.get('function_name') != 'exec':
            continue
        args = call.get('arguments', {})
        text = args.get('command') if isinstance(args, dict) else None
        call_id = call.get('tool_call_id')
        if isinstance(text, str) and isinstance(call_id, str):
            entries.append(entry('command', text, tool='exec', call=call_id,
                                 summary=text.split('\n', 1)[0]))
            known.add(call_id)
    observation = step.get('observation', {})
    results = observation.get('results', []) if isinstance(observation, dict) else []
    for result in results if isinstance(results, list) else []:
        if not isinstance(result, dict):
            continue
        call_id, content = result.get('source_call_id'), result.get('content')
        if isinstance(call_id, str) and call_id in known and isinstance(content, str):
            entries.append(entry('tool', content, call=call_id))
    return entries


def devin_entries(document):
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
            entries.append(entry(speaker, message))
        if source == 'agent':
            entries.extend(devin_commands(step))
    return entries


def claude_summary(name, args, record):
    if name == 'Bash':
        return str(args.get('command', '')).split('\n', 1)[0]
    if name in ('Read', 'Edit', 'MultiEdit', 'Write', 'NotebookEdit'):
        path = args.get('file_path', args.get('notebook_path', ''))
        cwd = record.get('cwd')
        if not isinstance(path, str):
            return ''
        return (path.removeprefix(cwd.rstrip('/') + '/')
                if isinstance(cwd, str) else path)
    return args.get('description') or name


def claude_content(content):
    if isinstance(content, str):
        return content
    if isinstance(content, list):
        return '\n'.join(block['text'] for block in content
                         if isinstance(block, dict) and block.get('type') == 'text'
                         and isinstance(block.get('text'), str))
    return ''


def claude_block(block, record):
    role = record['type']
    kind = block.get('type')
    if kind == 'text' and isinstance(block.get('text'), str):
        speaker = 'error' if record.get('isApiErrorMessage') else role
        return entry(speaker, block['text'], record)
    if role == 'user' and kind == 'tool_result':
        return entry('tool', claude_content(block.get('content')), record,
                     call=block.get('tool_use_id'),
                     failed=block.get('is_error') is True)
    if role == 'assistant' and kind == 'tool_use':
        args = block.get('input', {})
        if not isinstance(args, dict):
            return None
        name = block.get('name')
        text = args.get('command', '') if name == 'Bash' else json.dumps(args, indent=2)
        return entry('command', text, record, name, block.get('id'),
                     summary=claude_summary(name, args, record))
    return None


def claude_entries(lines):
    entries = []
    for line in lines:
        record = decoded(line)
        if record.get('isSidechain'):
            continue
        role = record.get('type')
        if role == 'system' and record.get('subtype') == 'api_error':
            error = record.get('error', {})
            if not isinstance(error, dict):
                continue
            text = error.get('formatted') or error.get('message')
            if not isinstance(text, str):
                continue
            if 'retryAttempt' in record and 'maxRetries' in record:
                text += f" · retry {record['retryAttempt']} of {record['maxRetries']}"
            entries.append(entry('error', text, record))
        elif role in ('user', 'assistant'):
            message = record.get('message', {})
            content = message.get('content', []) if isinstance(message, dict) else []
            if isinstance(content, str):
                content = [dict(type='text', text=content)]
            for block in content if isinstance(content, list) else []:
                result = (claude_block(block, record)
                          if isinstance(block, dict) else None)
                if result is not None:
                    entries.append(result)
    return entries


def render(path):
    """A malformed line is skipped: the agent may still be writing the last."""
    path = Path(path)
    with path.open(encoding='utf-8') as stream:
        if path.suffix == '.json':
            return devin_entries(decoded(stream.read()))
        entries, calls = [], set()
        for line in stream:
            record = decoded(line)
            if record.get('type') in ('user', 'assistant', 'system'):
                entries.extend(claude_entries([line]))
            else:
                entries.extend(codex_entry(record, calls))
        return entries


def follows_turn(marks, at, trails):
    _, role, route = marks[at]
    before = at > 0 and marks[at - 1] == ('agent_turn', role, route)
    after = at + 1 < len(marks) and marks[at + 1] == ('agent_turn', role, route)
    if before != after:
        trails[route] = before
    return before and trails.get(route, True)


def turns(events):
    """A session beside one turn pairs with it; between two, the route's habit."""
    rows = [(seq, kind, decoded(payload), ended_ms)
            for seq, kind, payload, ended_ms in events]
    rows = [row for row in rows if row[2].get('role') in
            ('implement', 'review', 'adjudicate', 'write', 'trim')]
    marks = [(kind, data.get('role'), data.get('route')) for _, kind, data, _ in rows]
    result, pending, latest, trails = [], {}, {}, {}
    for at, (seq, kind, data, ended_ms) in enumerate(rows):
        role, route = data.get('role'), data.get('route')
        key = role, route
        if kind == 'agent_session':
            session_id = data.get('session_id')
            if role == 'review':
                pending[key] = session_id
            elif role == 'implement':
                latest[key] = session_id
                if follows_turn(marks, at, trails):
                    result[-1]['session_id'] = session_id
                else:
                    pending[key] = session_id
        elif kind == 'agent_turn':
            label = data.get('label')
            usage = data.get('usage')
            usage = usage if isinstance(usage, dict) else {}
            cost = usage.get('cost_usd')
            status = data.get('exit_status')
            result.append(dict(id=seq, role=role, route=route,
                               label=label if isinstance(label, str) else None,
                               seconds=data.get('seconds'), ended_ms=ended_ms,
                               exit_status=status if type(status) is int else None,
                               timed_out=data.get('timed_out') is True,
                               input_tokens=count(usage.get('input_tokens')),
                               output_tokens=count(usage.get('output_tokens')),
                               cost_usd=cost if type(cost) in (int, float)
                               and cost >= 0 else None,
                               session_id=pending.pop(key, latest.get(key))))
    return result
