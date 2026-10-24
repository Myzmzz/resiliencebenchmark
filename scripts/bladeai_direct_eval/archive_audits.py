"""从保留的槽位审计补齐各轮尾部，只追加、不移动既有引用行号。"""
import json

from .common import dump, utc
from .manage import ROOT


def main():
    index = {}
    for source in (ROOT / 'runtime').glob('s[0-9][0-9]/data/api-audit.jsonl'):
        slot = int(source.parent.parent.name[1:])
        for line in source.read_text().splitlines(keepends=True):
            if not line.endswith('\n'): continue
            event = json.loads(line)
            trial = event.get('trial_id')
            if trial: index.setdefault((slot, trial), []).append(line)
    updated = []
    for manifest_path in (ROOT / 'models').glob('*/*/attempt-*/manifest.json'):
        run = manifest_path.parent
        if not (run / 'cleanup.json').exists(): continue
        manifest = json.loads(manifest_path.read_text())
        all_lines = index.get((manifest['slot'], manifest['trial_id']), [])
        assert all_lines, '缺少本轮完整槽位审计'
        target = run / 'api-audit.jsonl'
        existing = target.read_text().splitlines(keepends=True) if target.exists() else []
        assert all_lines[:len(existing)] == existing, '原归档不是完整审计的前缀，不自动覆盖'
        tail = all_lines[len(existing):]
        if tail:
            with target.open('a') as out: out.write(''.join(tail))
        updated.append({'run': str(run.relative_to(ROOT)), 'original_lines': len(existing), 'appended_lines': len(tail), 'total_lines': len(all_lines)})
    dump(ROOT / 'api-audit-archive-reconciliation.json', {'at': utc(), 'existing_line_numbers_preserved': True,
                                                       'note': '尾部可能含实例停止后的外部代理重启记录；gateway_started不是模型动作。',
                                                       'runs': updated})
    print(f'已核对{len(updated)}轮审计，新增{sum(x["appended_lines"] for x in updated)}行，原引用行号保持不变。')


if __name__ == '__main__':
    main()
