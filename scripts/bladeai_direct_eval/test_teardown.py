from pathlib import Path

import pytest

from .teardown import assert_instance_mounts


def mounts(prefix=''):
    return {'Mounts': [
        {'Type': 'bind', 'Source': prefix + '/Users/owner/run/s01/private', 'Destination': '/private'},
        {'Type': 'bind', 'Source': prefix + '/Users/owner/run/s01/data', 'Destination': '/data'},
        {'Type': 'volume', 'Name': 'owned-sockets', 'Destination': '/sockets'},
    ]}


@pytest.mark.parametrize('prefix', ['', '/host_mnt'])
def test_owns_host_and_docker_vm_bind_representations(prefix):
    assert_instance_mounts(mounts(prefix), Path('/Users/owner/run/s01'), 'owned-sockets')


@pytest.mark.parametrize('change', ['other_directory', 'misleading_prefix', 'other_volume'])
def test_rejects_foreign_resources(change):
    info = mounts('/host_mnt')
    if change == 'other_directory':
        info['Mounts'][0]['Source'] = '/host_mnt/Users/other/run/s01/private'
    elif change == 'misleading_prefix':
        info['Mounts'][0]['Source'] = '/host_mnt_extra/Users/owner/run/s01/private'
    else:
        info['Mounts'][2]['Name'] = 'shared-sockets'
    with pytest.raises(AssertionError):
        assert_instance_mounts(info, Path('/Users/owner/run/s01'), 'owned-sockets')
