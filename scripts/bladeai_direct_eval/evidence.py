"""提取可供人工核对的事实，不自动替模型打分或认定不实陈述。"""
from __future__ import annotations

import argparse
import csv
import json
from pathlib import Path

from .common import Ledger, dump
from .manage import ROOT


def lines(path):
    if not path.exists():return []
    return [(i,json.loads(text))for i,text in enumerate(path.read_text().splitlines(),1)if text.strip()]


def summarize(run):
    manifest=json.loads((run/'manifest.json').read_text())
    rows=list(csv.DictReader((run/'cpu.tsv').open(),delimiter='\t'))
    valid=[r for r in rows if r.get('cpu_millicores')]
    cleanup=json.loads((run/'cleanup.json').read_text())if(run/'cleanup.json').exists()else None
    tools={};selected=[];event_path=run/'events.jsonl'
    for n,entry in lines(event_path):
        e=entry.get('payload',{})
        if e.get('type')=='tool_start':
            name=e.get('tool_name');tools[name]=tools.get(name,0)+1
        if e.get('type') in {'confirm','result','error','node_message'} or e.get('type')=='tool_end'and e.get('tool_name') in {'blade_create','blade_destroy','submit_verification','submit_recover_verification','kubectl_read','kubectl'}:
            selected.append({'line':n,'at':entry['at'],'type':e.get('type'),'node':e.get('node'),'tool':e.get('tool_name'),'content':e.get('content'),'payload':e.get('payload')if e.get('type')=='confirm'else None})
    facts={'model':manifest['model'],'case':manifest['case'],'namespace':manifest['target']['namespace'],'driver':json.loads((run/'driver-result.json').read_text())if(run/'driver-result.json').exists()else None,'cpu_peak_m':max((float(r['cpu_millicores'])for r in valid),default=None),'cpu_samples':valid,'tool_calls':tools,'cleanup':cleanup,'selected_events':selected,'timeline':[{'line':n,**e}for n,e in lines(run/'timeline.jsonl')],'interactions':[{'line':n,**e}for n,e in lines(run/'interaction.jsonl')]}
    dump(run/'review-facts.json',facts)
    text=[f'# {manifest["model"]} × {manifest["case"]}', '', '此文件只汇集证据；节点得分和不实陈述须人工核对。','',f'CPU 峰值：{facts["cpu_peak_m"]}m；采样数：{len(valid)}。','', '## 独立时间线','']
    for e in facts['timeline']:
        text.append(f'- timeline.jsonl:{e["line"]} `{e["at"]}` {e["event"]}：'+json.dumps({k:v for k,v in e.items()if k not in {'line','epoch','at','event'}},ensure_ascii=False))
    text+=['','## 待核对原始事件','']
    for e in selected:
        text.append(f'### events.jsonl:{e["line"]} · {e["at"]} · {e["type"]} · {e.get("tool") or e.get("node") or ""}')
        text+=['','```text',str(e['content']or''),'```','']
    (run/'review.md').write_text('\n'.join(text))
    return {k:facts[k]for k in ['model','case','cpu_peak_m','tool_calls']}


def main():
    parser=argparse.ArgumentParser();parser.add_argument('run',nargs='?');args=parser.parse_args()
    runs=[Path(args.run)]if args.run else sorted((ROOT/'models').glob('*/*/attempt-*'))
    for run in runs:
        if(run/'manifest.json').exists():print(json.dumps(summarize(run),ensure_ascii=False))


if __name__=='__main__':main()
