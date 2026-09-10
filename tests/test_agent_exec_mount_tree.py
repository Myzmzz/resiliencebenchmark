"""C ABI and ordering tests; real filesystem isolation is qualified on Linux."""
import ctypes
from types import SimpleNamespace

import pytest

from harness.agent_exec import server


class NativeFunction:
    def __init__(self, function):
        self.function = function

    def __call__(self, *args):
        return self.function(*args)


@pytest.mark.parametrize("result", [0, -1])
def test_recursive_mount_attributes_fail_closed_and_set_only_read_only(result):
    def call(fd, path, flags, attributes, size):
        values = ctypes.cast(attributes, ctypes.POINTER(server._MountAttributes)).contents
        assert (fd, path, flags, size) == (-100, b"/", 0x8000, 32)
        assert (values.attr_set, values.attr_clr, values.propagation, values.userns_fd) == (1, 0, 0, 0)
        return result
    libc = SimpleNamespace(mount_setattr=NativeFunction(call))
    if result:
        with pytest.raises(OSError, match="recursively read-only"):
            server._make_mount_tree_read_only(libc)
    else:
        server._make_mount_tree_read_only(libc)


def test_sandbox_freezes_tree_before_opening_only_its_temporary_mount(monkeypatch, tmp_path):
    events = []
    libc = SimpleNamespace(
        unshare=NativeFunction(lambda flags: events.append(("unshare", flags)) or 0),
        mount=NativeFunction(lambda source, target, fs, flags, data: events.append(("mount", source, target, flags)) or 0),
    )
    monkeypatch.setattr(server.ctypes, "CDLL", lambda *_a, **_k: libc)
    monkeypatch.setattr(server, "_make_mount_tree_read_only", lambda value: events.append(("readonly_tree", value)))
    server._isolate_sandbox_namespaces(tmp_path)
    assert events[0] == ("unshare", 0x00020000 | 0x40000000)
    assert events[1] == ("mount", None, b"/", 16384 | (1 << 18))
    assert events[2] == ("readonly_tree", libc)
    assert events[3] == ("mount", bytes(tmp_path), bytes(tmp_path), 4096)
    assert events[4] == ("mount", None, bytes(tmp_path), 4096 | 32)
