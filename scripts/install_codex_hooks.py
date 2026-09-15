#!/usr/bin/env python3
"""Register/remove Squid hooks without changing Codex's approval or trust policy."""
import argparse
import json
import os
import shlex
import sys
import tempfile
from pathlib import Path

STATUS = 'Squid: update approval indicator'
EVENTS = ('PermissionRequest', 'PreToolUse', 'PostToolUse', 'Stop',
          'Interrupt', 'SessionEnd', 'UserPromptSubmit')


def is_ours(handler):
    return (isinstance(handler, dict) and handler.get('statusMessage') == STATUS
            and any(Path(token).name == 'codex_pet_hook.py'
                    for token in shlex.split(handler.get('command', ''))))


def configure(path, remove=False):
    original = path.read_text() if path.exists() else '{}'
    data = json.loads(original)
    hooks = data.setdefault('hooks', {})
    # Validate before writing; preserve all unrelated definitions and settings.
    if not isinstance(hooks, dict):
        raise ValueError('hooks must be an object')
    for event, groups in list(hooks.items()):
        kept = []
        for group in groups:
            handlers = group['hooks']
            remaining = [h for h in handlers if not is_ours(h)]
            if remaining or remaining == handlers:
                kept.append({**group, 'hooks': remaining})
        if kept:
            hooks[event] = kept
        else:
            del hooks[event]
    if not remove:
        command = shlex.join([sys.executable,
                              str(Path(__file__).resolve().with_name('codex_pet_hook.py'))])
        for event in EVENTS:
            group = {'hooks': [{'type': 'command', 'command': command,
                                'timeout': 3, 'statusMessage': STATUS}]}
            if event == 'PreToolUse':
                group['matcher'] = '^request_user_input$'
            hooks.setdefault(event, []).append(group)
    updated = json.dumps(data, indent=2) + '\n'
    if updated == original:
        return
    path.parent.mkdir(parents=True, exist_ok=True)
    if path.exists():
        backup = path.with_name(path.name + '.squid-backup')
        if not backup.exists():
            backup.write_text(original)
    fd, name = tempfile.mkstemp(prefix='.squid-hooks-', dir=path.parent)
    try:
        with os.fdopen(fd, 'w') as stream:
            stream.write(updated)
        os.replace(name, path)
    finally:
        if os.path.exists(name):
            os.unlink(name)


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--hooks-file', type=Path,
                        default=Path(os.environ.get('CODEX_HOME', str(Path.home() / '.codex'))) / 'hooks.json')
    parser.add_argument('--remove', action='store_true')
    args = parser.parse_args()
    configure(args.hooks_file, args.remove)
    print('Squid Codex hooks removed.' if args.remove else
          'Squid Codex hooks registered. Open /hooks in Codex and review/trust the Squid hooks.')


if __name__ == '__main__':
    main()
