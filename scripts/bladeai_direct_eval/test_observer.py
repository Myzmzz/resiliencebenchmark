from .observer import cleanup_deadline,Observer,healthy_application_rate


def test_grace_starts_from_running_not_earlier_cr_creation():
    assert cleanup_deadline(100,106,300,300)==706
    assert cleanup_deadline(100,None,300,300)==700


def test_application_success_is_not_confused_with_missing_trace_series():
    o=Observer.__new__(Observer)
    good={'cart_api_requests':{'data':{'result':[{'metric':{'status':'200'},'value':[1,'10']}]}}}
    o.baseline_business=good;o.latest_business=good
    assert o.business_recovered()
    o.latest_business={'cart_api_requests':{'data':{'result':[]}}}
    assert not o.business_recovered()
    o.latest_business={'cart_api_requests':{'data':{'result':[{'metric':{'status':'500'},'value':[1,'10']}]}}}
    assert not o.business_recovered()


def test_preinjection_missing_sample_does_not_erase_healthy_business_baseline():
    class API:
        def __init__(self): self.missing = False
        def get(self, path, params):
            if self.missing: return {'data': {'result': []}}
            return {'data': {'result': [{'metric': {'status': '200'}, 'value': [1, '10']}]}}
    class Log:
        def add(self, *args, **kwargs): pass
    o = Observer.__new__(Observer)
    o.api = API(); o.business = Log(); o.baseline_business = None
    state = {'namespace': 'otel-demo-03', 'target': {'uid': 'bound'}, 'owned': {}}
    o.business_sample(state)
    o.api.missing = True
    o.business_sample(state)
    assert healthy_application_rate(o.baseline_business) == 10
    assert not o.business_recovered()
    o.api.missing = False
    state['owned'] = {'fault': {}}
    o.business_sample(state)
    assert o.business_recovered()


def test_nonfinite_business_rate_is_not_recovery_evidence():
    for value in ('NaN', 'inf', '-1', '0'):
        assert healthy_application_rate({'cart_api_requests': {'data': {'result': [
            {'metric': {'status': '200'}, 'value': [1, value]}
        ]}}}) is None
