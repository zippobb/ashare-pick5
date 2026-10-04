# -*- coding: utf-8 -*-
"""把渲染好的看板推回 GitHub（走 REST API，零依赖、不需要 git）。

用法：
    export GH_TOKEN=github_pat_xxx
    python3 publish_pages.py --dir daily/20261008 --html index.html --date 20261008

会提交两个文件：
    index.html                      （GitHub Pages 根页面）
    daily/<date>/narrative.json     （当日文���留档）
"""
import os
import sys
import json
import base64
import argparse
import urllib.request
import urllib.error

API = 'https://api.github.com/repos/%s/contents/%s'


def req(url, token, data=None, method=None):
    body = json.dumps(data).encode() if data is not None else None
    r = urllib.request.Request(
        url, data=body, method=method or ('PUT' if data is not None else 'GET'),
        headers={'Authorization': 'Bearer ' + token,
                 'Accept': 'application/vnd.github+json',
                 'Content-Type': 'application/json',
                 'User-Agent': 'pick5-publisher'})
    return json.loads(urllib.request.urlopen(r, timeout=120).read())


def put_file(owner_repo, branch, path, local, token, message):
    if not os.path.exists(local):
        print('SKIP %s (本地不存在: %s)' % (path, local))
        return False
    raw = open(local, 'rb').read()
    url = API % (owner_repo, path)
    sha = None
    try:
        meta = req(url + '?ref=' + branch, token)
        sha = meta.get('sha')
    except urllib.error.HTTPError as e:
        if e.code != 404:
            print('  取 sha 失败 %s: HTTP %s' % (path, e.code))
    data = {'message': message, 'branch': branch,
            'content': base64.b64encode(raw).decode()}
    if sha:
        data['sha'] = sha
    try:
        d = req(url, token, data, 'PUT')
        print('OK %s (%d bytes) commit=%s' % (path, len(raw),
                                              (d.get('commit') or {}).get('sha', '')[:7]))
        return True
    except urllib.error.HTTPError as e:
        print('ERR %s HTTP %s %s' % (path, e.code, e.read().decode()[:200]))
        return False


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument('--repo', default='zippobb/ashare-pick5')
    ap.add_argument('--branch', default='main')
    ap.add_argument('--dir', required=True, help='当日数据目录（narrative.json 在其中）')
    ap.add_argument('--html', required=True, help='渲染出的 index.html 路径')
    ap.add_argument('--date', required=True, help='trade_date，如 20261008')
    a = ap.parse_args()

    token = os.environ.get('GH_TOKEN', '').strip()
    if not token or token.startswith('github_pat_xxx') or token.startswith('<github_pat'):
        sys.exit('GH_TOKEN 未配置或仍是占位符，无法推送')

    msg = 'dashboard %s' % a.date
    ok1 = put_file(a.repo, a.branch, 'index.html', a.html, token, msg)
    ok2 = put_file(a.repo, a.branch, 'daily/%s/narrative.json' % a.date,
                   os.path.join(a.dir, 'narrative.json'), token, msg)
    print('PUSHED' if (ok1 and ok2) else 'PARTIAL_OR_FAILED')


if __name__ == '__main__':
    main()
