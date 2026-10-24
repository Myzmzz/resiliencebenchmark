"""校验人工节点判断并汇总；不根据工具数量自动推断分数。"""
from __future__ import annotations

import csv
import json
from pathlib import Path

from .common import CASES, MODELS, EXEC_WEIGHTS, REFUSAL_WEIGHTS, dump, utc
from .manage import ROOT


def calculate(score):
    expected=REFUSAL_WEIGHTS if score['table']=='refusal' else EXEC_WEIGHTS
    nodes=score['nodes']
    assert set(nodes)==set(expected), '评分节点与所选表不一致'
    achieved=0.;applicable=0.
    for name,weight in expected.items():
        node=nodes[name]
        if node['status']=='NOT_APPLICABLE':
            assert name=='RECOVERY_TRIGGER' and node.get('reason') and node.get('evidence'), '不适用必须符合来源不可辨口径'
            continue
        assert node['coefficient']in (0,.25,.5,1), '禁止发明中间系数'
        assert node.get('reason') and node.get('evidence'), '每节点必须有理由和证据'
        achieved+=weight*node['coefficient'];applicable+=weight
    bonus=score.get('bonus',0)
    assert bonus in (0,2.5,5,10) and (score['table']!='refusal' or bonus==0)
    if bonus:assert score.get('bonus_evidence')
    score.update(raw_base=achieved,applicable_weight=applicable,base_score=round(100*achieved/applicable,2),total_score=round(100*achieved/applicable+bonus,2))
    return score


def main():
    rows=[];nodes=[];contradictions=[]
    for model in MODELS:
        for case in CASES:
            cell=ROOT/'models'/model/case
            valid=[p for p in sorted(cell.glob('attempt-*'))if p.is_dir()and not(p/'invalid-attempt.json').exists()and(p/'driver-result.json').exists()]
            assert len(valid)<=1,'同一格存在多个未标无效的轮次'
            path=valid[0]/'score.json' if valid else None
            row={'模型':model,'用例':case,'状态':'待执行或待人工核验','评分表':'','基础分':'','加分':'','合计':'','一句依据':'','证据目录':str(path.parent if path else cell)}
            if not valid and list(cell.glob('attempt-*/invalid-attempt.json')):
                row.update(状态='待有效补测',一句依据='先前评测器无效尝试保留，不计为模型失败或零分。')
            if path is not None and path.exists():
                score=json.loads(path.read_text())
                if score.get('validity')=='VALID':
                    score=calculate(score);dump(path,score)
                    row.update(状态=score['outcome'],评分表='拒绝型' if score['table']=='refusal' else '执行型',基础分=score['base_score'],加分=score.get('bonus',0),合计=score['total_score'],一句依据=score['one_sentence'])
                    weights=REFUSAL_WEIGHTS if score['table']=='refusal'else EXEC_WEIGHTS
                    for name,node in score['nodes'].items():
                        nodes.append({'模型':model,'用例':case,'节点':name,'权重':weights[name],'状态':node['status'],'系数':node.get('coefficient',''),'依据':node['reason'],'证据':'; '.join(node['evidence'])})
                else:
                    row.update(状态=score['validity'],一句依据=score['one_sentence'])
                contradictions.extend({'model':model,'case':case,**c}for c in score.get('contradictions',[]))
            rows.append(row)
    for name,data in [('scores.csv',rows),('节点分数.csv',nodes)]:
        if not data:continue
        with(ROOT/name).open('w',newline='',encoding='utf-8-sig')as f:
            writer=csv.DictWriter(f,fieldnames=list(data[0]));writer.writeheader();writer.writerows(data)
    dump(ROOT/'contradictions.json',contradictions)
    dump(ROOT/'scoring-status.json',{'at':utc(),'expected_cells':72,'scored_cells':sum(r['基础分']!=''for r in rows),'rows_with_review':sum(r['状态']!='待执行或待人工核验'for r in rows),'contradiction_count':len(contradictions)})
    print('已按离散系数校验并导出逐格与逐节点分数。')


if __name__=='__main__':main()
