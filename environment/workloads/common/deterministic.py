"""Deterministic scheduling helpers shared by benchmark Locust profiles."""

from __future__ import annotations

import hashlib
import uuid
from typing import Iterable, Sequence, TypeVar


T = TypeVar("T")


def exact_percent_schedule(seed: int, weighted_flows: Iterable[tuple[str, int]]) -> tuple[str, ...]:
    if not isinstance(seed, int) or isinstance(seed, bool) or seed <= 0:
        raise ValueError("seed must be a positive integer")
    schedule: list[str] = []
    seen: set[str] = set()
    for flow, weight in weighted_flows:
        if not isinstance(flow, str) or not flow or flow in seen:
            raise ValueError("flow ids must be unique non-empty strings")
        if not isinstance(weight, int) or isinstance(weight, bool) or weight <= 0:
            raise ValueError("flow weights must be positive integers")
        seen.add(flow)
        schedule.extend([flow] * weight)
    if len(schedule) != 100:
        raise ValueError("flow weights must sum to 100")
    for index in range(len(schedule) - 1, 0, -1):
        digest = hashlib.sha256(f"{seed}:{index}".encode("utf-8")).digest()
        selected = int.from_bytes(digest[:8], "big") % (index + 1)
        schedule[index], schedule[selected] = schedule[selected], schedule[index]
    return tuple(schedule)


def deterministic_index(seed: int, user_index: int, iteration: int, label: str, size: int) -> int:
    if size <= 0:
        raise ValueError("cannot choose from an empty sequence")
    payload = f"{seed}:{user_index}:{iteration}:{label}".encode("utf-8")
    return int.from_bytes(hashlib.sha256(payload).digest()[:8], "big") % size


def deterministic_choice(values: Sequence[T], seed: int, user_index: int, iteration: int, label: str) -> T:
    return values[deterministic_index(seed, user_index, iteration, label, len(values))]


def deterministic_uuid(seed: int, user_index: int, iteration: int, label: str) -> str:
    digest = bytearray(hashlib.sha256(f"{seed}:{user_index}:{iteration}:{label}".encode("utf-8")).digest()[:16])
    digest[6] = (digest[6] & 0x0F) | 0x40
    digest[8] = (digest[8] & 0x3F) | 0x80
    return str(uuid.UUID(bytes=bytes(digest)))
