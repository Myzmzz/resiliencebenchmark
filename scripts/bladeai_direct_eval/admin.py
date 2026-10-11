"""仅外部采集/扰动/兜底进程使用；绝不挂入被测容器。"""
import base64
import json
import os
from pathlib import Path
import ssl
import tempfile

import httpx
import yaml

from .common import KUBE


class AdminAPI:
    def __init__(self):
        config = yaml.safe_load(Path(KUBE).read_text())
        context = next(x['context'] for x in config['contexts'] if x['name'] == config['current-context'])
        cluster = next(x['cluster'] for x in config['clusters'] if x['name'] == context['cluster'])
        user = next(x['user'] for x in config['users'] if x['name'] == context['user'])
        self.temp = tempfile.TemporaryDirectory(prefix='bladeai-observer-tls-')
        paths = []
        for key in ['client-certificate-data', 'client-key-data']:
            path = Path(self.temp.name) / key
            fd = os.open(path, os.O_CREAT | os.O_WRONLY, 0o600)
            with os.fdopen(fd, 'wb') as f:
                f.write(base64.b64decode(user[key]))
            paths.append(str(path))
        context = ssl.SSLContext(ssl.PROTOCOL_TLS_CLIENT)
        context.check_hostname = False
        context.verify_mode = ssl.CERT_NONE
        context.load_cert_chain(*paths)
        self.client = httpx.Client(base_url=cluster['server'], verify=context, trust_env=False, timeout=httpx.Timeout(12, connect=5))

    def get(self, path, params=None):
        r = self.client.get(path, params=params)
        r.raise_for_status()
        return r.json()

    def delete_owned(self, path, uid):
        r = self.client.request('DELETE', path, json={'apiVersion': 'v1', 'kind': 'DeleteOptions', 'preconditions': {'uid': uid}})
        if r.status_code not in {200, 202, 404}:
            r.raise_for_status()
        return {'status': r.status_code, 'body': r.json()}

    def close(self):
        self.client.close()
        self.temp.cleanup()
