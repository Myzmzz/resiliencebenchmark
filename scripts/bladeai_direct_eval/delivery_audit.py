"""核对归档的模型路由、证据引用和暂存凭据排除情况；不输出凭据内容。"""
from __future__ import annotations

import json
from pathlib import Path

from .common import dump, utc
from .manage import ROOT, load_provider_key


def public_files():
    suffixes = {'.json', '.jsonl', '.log', '.tsv', '.csv', '.md', '.txt', '.html', '.xml', '.py'}
    for path in ROOT.rglob('*'):
        if path.is_file() and 'runtime' not in path.relative_to(ROOT).parts and path.suffix in suffixes:
            yield path


def main():
    secrets = [load_provider_key().encode()]
    for directory in (ROOT / 'runtime').glob('s[0-9][0-9]/private'):
        secrets += [path.read_bytes().strip() for path in directory.glob('*.token')]
        secrets.append(json.loads((directory / 'gateway.json').read_text())['control_token'].encode())
    secrets = list(set(value for value in secrets if value))
    overlap = max(map(len, secrets)) - 1
    exposures = []
    files_scanned = 0
    for path in public_files():
        files_scanned += 1
        tail = b''
        with path.open('rb') as stream:
            while chunk := stream.read(1024 * 1024):
                data = tail + chunk
                if any(value in data for value in secrets):
                    exposures.append(str(path.relative_to(ROOT)))
                    break
                tail = data[-overlap:]
    checked = []
    missing = []
    routes = []
    for path in (ROOT / 'models').glob('*/*/attempt-*/score.json'):
        run = path.parent
        if (run / 'invalid-attempt.json').exists(): continue
        score = json.loads(path.read_text())
        manifest = json.loads((run / 'manifest.json').read_text())
        model = manifest['model']
        count = 0
        for line in (run / 'api-audit.jsonl').read_text().splitlines():
            event = json.loads(line)
            if event['event'] == 'llm_response':
                count += 1
                assert event['model'] == model, '被测模型路由与manifest不一致'
        assert count > 0, '评分轮次缺少模型调用证据'
        references = []
        for node in score['nodes'].values(): references.extend(node['evidence'])
        for finding in score.get('contradictions', []):
            references += finding.get('claim_evidence', []) + finding.get('independent_evidence', [])
        for reference in references:
            name, _, line = reference.rpartition(':')
            if not line.isdigit(): name = reference
            candidate = run / name
            if not candidate.is_file(): missing.append({'run': str(run.relative_to(ROOT)), 'reference': reference})
        checked.append({'model': model, 'case': manifest['case'], 'run': str(run.relative_to(ROOT)), 'model_responses': count})
        routes.append({'model': model, 'case': manifest['case'], 'protocol_revision': manifest.get('proxy_protocol_revision', 'kube-prefix-v1')})
    result = {'at': utc(), 'files_scanned': files_scanned, 'credential_exposure_paths': exposures,
              'scored_cells_checked': len(checked), 'missing_evidence_references': missing,
              'model_routing': checked, 'protocol_versions': routes, 'passed': not exposures and not missing}
    dump(ROOT / 'delivery-audit.json', result)
    assert result['passed'], '归档审计未通过；路径清单已保存，不输出凭据值'
    print(f'{len(checked)}格模型路由与证据引用通过；扫描{files_scanned}份公开文本，未发现本轮暂存凭据。')


if __name__ == '__main__':
    main()
