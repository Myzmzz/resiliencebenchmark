from copy import deepcopy
import pytest
from .boundary import Denied, validate_cb
from .test_boundary import BOUND, cb


def test_exact_live_container_identity_is_in_scope():
    bound = {**deepcopy(BOUND), 'container_id': 'a'*64}
    obj = cb(); experiment = obj['spec']['experiments'][0]
    experiment['scope'] = 'container'
    experiment['matchers'] += [{'name': 'container-ids', 'value': [bound['container_id']]}, {'name': 'container-index', 'value': ['']}]
    assert validate_cb(obj, bound)['target_uid'] == BOUND['uid']
    experiment['matchers'][-2]['value'] = ['b'*64]
    with pytest.raises(Denied): validate_cb(obj, bound)


def test_container_identity_may_not_be_reused_after_restart():
    bound = {**deepcopy(BOUND), 'container_id': 'b'*64}
    obj = cb(); experiment = obj['spec']['experiments'][0]
    experiment['scope'] = 'container'
    experiment['matchers'] += [{'name': 'container-ids', 'value': ['a'*64]}]
    with pytest.raises(Denied): validate_cb(obj, bound)
