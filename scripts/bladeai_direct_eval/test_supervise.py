from .common import CASES, MODELS
from .supervise import choose_work


def test_free_slot_prefers_its_original_model():
    assert choose_work(2, {}, []) == (MODELS[1], 'L0')


def test_finished_model_slot_shares_remaining_work_without_duplicate_cells():
    done = {(m, c) for m in MODELS[1:] for c in CASES}
    done.update((MODELS[0], c) for c in CASES[:7])
    active = [{'model': MODELS[0], 'case': 'D1'}]
    assert choose_work(2, done, active) == (MODELS[0], 'D2')
    active.append({'model': MODELS[0], 'case': 'D2'})
    assert choose_work(3, done, active) == (MODELS[0], 'D3')


def test_all_reserved_or_finished_cells_leave_slot_idle():
    done = {(m, c) for m in MODELS for c in CASES}
    done.remove((MODELS[3], 'D8-B'))
    assert choose_work(1, done, [{'model': MODELS[3], 'case': 'D8-B'}]) is None
