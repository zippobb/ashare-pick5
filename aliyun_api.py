# -*- coding: utf-8 -*-
"""极简阿里云 ECS OpenAPI 客户端（POP RPC 签名 v1，零第三方依赖）。
仅实现本次迁移需要的三个接口：DescribeInstances / RunCommand / DescribeInvocationResults。
凭据读取 ~/.aliyun/config.json 的 current profile。
"""
import os
import sys
import json
import time
import hmac
import base64
import hashlib
import urllib.parse
import urllib.request

ENDPOINT = 'https://ecs.aliyuncs.com'


def pct(s):
    return urllib.parse.quote(str(s), safe='-_.~')


def load_profile(name=None):
    p = os.path.expanduser('~/.aliyun/config.json')
    cfg = json.load(open(p, encoding='utf-8'))
    cur = name or cfg.get('current', 'default')
    for prof in cfg.get('profiles', []):
        if prof.get('name') == cur:
            return prof
    raise SystemExit('未找到 profile: %s' % cur)


class ECS:
    def __init__(self, ak, sk, region):
        self.ak, self.sk, self.region = ak, sk, region

    def call(self, action, params=None, version='2014-05-26'):
        p = {
            'Action': action,
            'Version': version,
            'AccessKeyId': self.ak,
            'SignatureMethod': 'HMAC-SHA1',
            'SignatureVersion': '1.0',
            'SignatureNonce': '%d%d' % (time.time() * 1000, os.getpid() % 100000),
            'Timestamp': time.strftime('%Y-%m-%dT%H:%M:%SZ', time.gmtime()),
            'Format': 'JSON',
            'RegionId': self.region,
        }
        p.update(params or {})
        keys = sorted(p)
        canon = '&'.join('%s=%s' % (pct(k), pct(p[k])) for k in keys)
        sts = 'GET&%s&%s' % (pct('/'), pct(canon))
        sig = base64.b64encode(hmac.new((self.sk + '&').encode(), sts.encode(), hashlib.sha1).digest()).decode()
        query = canon + '&Signature=' + pct(sig)
        url = ENDPOINT + '/?' + query
        try:
            r = urllib.request.urlopen(url, timeout=30).read().decode('utf-8', 'ignore')
        except urllib.error.HTTPError as e:
            r = e.read().decode('utf-8', 'ignore')
        try:
            return json.loads(r)
        except Exception:
            return {'_raw': r}

    def find_instance_by_ip(self, ip):
        for region in [self.region, 'cn-hangzhou', 'cn-shanghai']:
            self.region = region
            r = self.call('DescribeInstances', {'PublicIpAddresses': json.dumps([ip]), 'PageSize': 10})
            inst = ((r.get('Instances') or {}).get('Instance') or [])
            if inst:
                return region, inst[0]
        return None, None

    def run_command(self, instance_id, cmd, timeout=120, region=None):
        if region:
            self.region = region
        r = self.call('RunCommand', {
            'InstanceId.1': instance_id,
            'Type': 'RunShellScript',
            'CommandContent': cmd,
            'Timeout': timeout,
            'ContentEncoding': 'PlainText',
        })
        return r.get('InvokeId'), r

    def wait_result(self, invoke_id, timeout=180, interval=4):
        t0 = time.time()
        while time.time() - t0 < timeout:
            r = self.call('DescribeInvocationResults', {
                'InvokeId': invoke_id,
                'ContentEncoding': 'PlainText',
            })
            res = ((r.get('Invocation') or {}).get('InvocationResults') or {}).get('InvocationResult') or []
            if res:
                st = res[0].get('InvocationStatus')
                if st in ('Success', 'Failed', 'Error', 'Timeout', 'Cancelled', 'Terminated', 'Stopped'):
                    return res[0]
            time.sleep(interval)
        return {'InvocationStatus': 'PollTimeout', '_last': r}


def main():
    prof = load_profile()
    ip = sys.argv[1] if len(sys.argv) > 1 else '47.103.42.149'
    ecs = ECS(prof['access_key_id'], prof['access_key_secret'], prof.get('region_id', 'cn-shanghai'))
    cmd = sys.argv[2] if len(sys.argv) > 2 else None
    region, inst = ecs.find_instance_by_ip(ip)
    if not inst:
        print(json.dumps({'error': 'instance not found', 'ip': ip}, ensure_ascii=False))
        return 1
    print(json.dumps({'region': region, 'InstanceId': inst.get('InstanceId'),
                      'InstanceName': inst.get('InstanceName'),
                      'OSName': inst.get('OSName'), 'Status': inst.get('Status'),
                      'PublicIp': (inst.get('PublicIpAddress') or {}).get('IpAddress')},
                     ensure_ascii=False, indent=1))
    if cmd:
        iid = inst['InstanceId']
        inv, raw = ecs.run_command(iid, cmd, region=region)
        print('InvokeId=%s' % inv)
        if not inv:
            print(json.dumps(raw, ensure_ascii=False, indent=1))
            return 1
        res = ecs.wait_result(inv)
        print('status=%s exitcode=%s' % (res.get('InvocationStatus'), res.get('ExitCode')))
        print(res.get('Output') or '')
        if res.get('ErrorInfo'):
            print('ErrorInfo: %s' % res['ErrorInfo'])
    return 0


if __name__ == '__main__':
    sys.exit(main())
