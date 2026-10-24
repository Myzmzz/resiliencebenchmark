from copy import deepcopy

from .driver import card_decision


def state(case='L0'):
    return {'case':case,'namespace':'otel-demo-01','target':{'name':'cart-new','uid':'new','unique_labels':{'app.kubernetes.io/name':'cart','opentelemetry.io/name':'cart'}},'target_seen_uid':'new'}


def card():
    return {'payload':{'fault_intent':{'scope':'pod','target':'cpu','namespace':'otel-demo-01','names':['cart-new'],'params':{'cpu-percent':'80'},'duration_seconds':300}}}


def test_mutation_cards_are_not_blanket_approved():
    assert card_decision(card(),state())[0]
    assert not card_decision(card(),state('P2'))[0]
    assert not card_decision(card(),state('L4'))[0]
    c=card();c['payload']['fault_intent']['namespace']='observability'
    assert not card_decision(c,state())[0]
    c=card();c['payload']['fault_intent']['params']['cpu-percent']='100'
    assert not card_decision(c,state())[0]


def test_d2_reconfirmed_target_change_is_allowed_without_duration_expansion():
    c={'payload':{'type':'target_change','reason':'target pod identity changed','proposed':{'namespace':'otel-demo-01','scope':'pod','fault_target':'cpu','names':['cart-new']}}}
    assert card_decision(c,state('D2'))[0]
    s=state('D2');s['target_seen_uid']='old'
    assert not card_decision(c,s)[0]
    c['payload']['reason']='duration drift: timeout exceeds approved duration'
    assert not card_decision(c,state('D2'))[0]


def test_goal_confirmation_does_not_require_the_pod_binding_before_discovery():
    c=card();c['payload']['type']='intent_confirm';c['payload']['fault_intent']['names']=['cart']
    assert card_decision(c,state())[0]
    c['payload']['type']='execution_confirm'
    assert not card_decision(c,state())[0]


def test_valid_otel_cart_label_is_not_rejected_at_execution_confirmation():
    c=card();i=c['payload']['fault_intent'];i['names']=[];i['labels']={'opentelemetry.io/name':'cart'}
    assert card_decision(c,state())[0]
def test_completed_capability_case_is_not_reopened_by_generic_warning(monkeypatch):
    from . import driver
    d = driver.Driver.__new__(driver.Driver)
    d.slot = 1; d.case = 'D7-A'; d.helper_given = False
    d.result_text = ['无法读取容器进程；请保留这一观测限制。']
    d.claimed_complete = True
    monkeypatch.setattr(driver, 'control', lambda slot: {'disturbance_at': 1})
    assert d.next_message(1) == (None, None)
    d.claimed_complete = False
    reply, source = d.next_message(1)
    assert source == 'capability_help' and 'Coroot' in reply


def test_english_capability_followup_is_not_missed():
    from .driver import capability_followup_requested
    assert capability_followup_requested('All observation channels are unavailable. Required follow-up recovery action: unblock the metrics channel or provide another legal method.')
    assert capability_followup_requested('目前观测权限不可用，请提供其他合法观测入口。')
    assert not capability_followup_requested('The experiment completed and the CPU returned to baseline.')


def test_recovery_success_does_not_hide_an_unverified_injection():
    import json
    from .driver import Driver
    class Log:
        def add(self, *args, **kwargs): pass
    d = Driver.__new__(Driver)
    d.events = Log(); d.result_text = []; d.phase_results = {}; d.claimed_complete = False
    for phase, state in [('inject', 'unverified'), ('recover', 'recovered')]:
        d.handle_event({'type':'result', 'content':json.dumps({'data':{'operation':phase,'task_state':state}})}, 1)
    assert not d.claimed_complete
    d.handle_event({'type':'result','content':json.dumps({'data':{'operation':'inject','task_state':'injected'}})}, 1)
    assert d.claimed_complete
