# -*- coding: utf-8 -*-
"""看板渲染器：把线上管线输出的 JSON + 一份文案(narrative.json) 渲染成单文件 HTML。

设计目的：ECS 只产出数据，看板的分析文案由 WorkBuddy（云/本）撰写，本脚本负责排版。
零第三方依赖。

用法：
    python3 render_dashboard.py --data out/20260930 --narrative out/20260930/narrative.json \
        --out ../dashboard/index.html
"""
import os
import sys
import json
import html
import argparse

BASE = os.path.dirname(os.path.abspath(__file__))
WEEK = ['周一', '周二', '周三', '周四', '周五', '周六', '周日']


# ---------- 工具 ----------
def load(p, default=None):
    if p and os.path.exists(p):
        return json.load(open(p, encoding='utf-8'))
    return default if default is not None else {}


def esc(s):
    return html.escape(str(s), quote=False)


def fnum(v, nd=2):
    return '-' if v is None else ('%.*f' % (nd, v))


def pct_cls(v):
    return 'flat' if v is None or v == 0 else ('up' if v > 0 else 'down')


def pct_txt(v, nd=2, sign=True):
    if v is None:
        return '-'
    s = '%.*f%%' % (nd, v)
    return ('+' if sign and v > 0 else '') + s


def rate_cls(rate):
    if rate >= 85:
        return 'q1'
    if rate >= 70:
        return 'q2'
    if rate >= 50:
        return 'q3'
    return 'q4'


def bar(label, val, maxv, big=False):
    rate = 0 if not maxv else max(0, min(100, val / maxv * 100))
    q = rate_cls(rate)
    over = ' 超额' if val > maxv + 0.05 else ''
    return ('<div class="bar%s"><span class="lb">%s</span>'
            '<span class="tr"><span class="fl %s" style="width:%.1f%%"></span></span>'
            '<span class="vl mono">%s / %g%s</span></div>'
            % (' big' if big else '', esc(label), q, rate, fnum(val, 1), maxv, over))


def weekday(d):
    import datetime
    return WEEK[datetime.datetime.strptime(d, '%Y%m%d').weekday()]


# ---------- 各区块 ----------
def render_head(td, market, gen_at):
    prev = market.get('previous_trade_date')
    nxt = market.get('next_trade_date')
    d = datetime_fmt(td)
    return (
        '<h1>A股短线选股看板 <span class="sub">· %s（%s）</span></h1>\n'
        '<div class="sub">数据源：stock-key10-bridge <span class="mono">trade_date=%s</span>'
        '（manifest 已核对，为当日最新交付） · 生成时间 %s CST · 上一交易日 %s，下一交易日 %s</div>'
        % (d, weekday(td), esc(td), esc(gen_at), esc(prev or '-'), esc(nxt or '-')))


def datetime_fmt(td):
    return '%s-%s-%s' % (td[:4], td[4:6], td[6:8])


def render_alert(narr):
    if not narr.get('alert_title') and not narr.get('alert_bullets'):
        return ''
    lis = ''.join('<li>%s</li>' % b for b in narr.get('alert_bullets') or [])
    if not lis:
        lis = '<li>%s</li>' % esc(narr.get('alert_title', ''))
    return ('<div class="alert"><div class="t">%s</div><ul>%s</ul></div>'
            % (esc(narr.get('alert_title', '提示')), lis))


def render_snap(env):
    boxes = []
    for ix in env.get('major_indices') or []:
        r1 = ix.get('return_1d_pct')
        cls = pct_cls(r1)
        boxes.append(
            '<div class="sbox"><div class="k">%s</div>'
            '<div class="v mono %s">%s</div>'
            '<div class="d mono">MA20 %s · 5日 <span class="%s">%s</span> · %s</div></div>'
            % (esc(ix.get('name')), cls, fnum(ix.get('close')), fnum(ix.get('ma20')),
               pct_cls(ix.get('return_5d_pct')), pct_txt(ix.get('return_5d_pct')),
               esc(ix.get('trend') or '')))
    adv, dec, unc = env.get('advance_count'), env.get('decline_count'), env.get('unchanged_count')
    boxes.append(
        '<div class="sbox"><div class="k">涨 / 跌 / 平</div>'
        '<div class="v mono"><span class="up">%s</span> / <span class="down">%s</span> / '
        '<span class="flat">%s</span></div><div class="d mono">上涨占比 %s</div></div>'
        % (adv, dec, unc, fnum(env.get('advance_ratio_pct')) + '%'))
    med = env.get('market_median_return_1d_pct')
    boxes.append(
        '<div class="sbox"><div class="k">个股当日中位数</div>'
        '<div class="v mono %s">%s</div><div class="d">%s</div></div>'
        % (pct_cls(med), pct_txt(med), '赚钱效应为负' if (med or 0) < 0 else '赚钱效应为正'))
    am = env.get('above_ma20_ratio_pct')
    boxes.append(
        '<div class="sbox"><div class="k">站上 MA20 比例</div>'
        '<div class="v mono %s">%s%%</div><div class="d">%s</div></div>'
        % ('down' if (am or 0) < 50 else 'up', fnum(am), '不足半数' if (am or 0) < 50 else '过半'))
    boxes.append(
        '<div class="sbox"><div class="k">两市成交额</div>'
        '<div class="v mono">%s 亿</div><div class="d">市场量能</div></div>'
        % fnum(env.get('market_amount_yi'), 0))
    boxes.append(
        '<div class="sbox"><div class="k">市场阶段 / 姿态</div>'
        '<div class="v" style="color:var(--warn)">%s / %s</div>'
        '<div class="d">仓位上限 100%%，单票可至 50%%+</div></div>'
        % (esc(env.get('phase') or '-'), esc(env.get('posture') or '-')))
    return '<h2>一、市场快照</h2>\n<div class="snap">%s</div>' % ''.join(boxes)


def pick_tags(r, extra):
    t = ['<span class="tag">%s</span>' % esc(r.get('theme') or '-')]
    biz = r.get('biz') or ''
    if '待核验' in biz or not biz:
        t.append('<span class="tag gy">题材待核验</span>')
    else:
        t.append('<span class="tag b">%s</span>' % esc(str(biz)[:8]))
    kr = r.get('kf_ratio')
    if kr is not None and r.get('np') and r['np'] > 0:
        t.append('<span class="tag %s">扣非 %.0f%%</span>' % ('b' if kr >= 0.6 else 'w', kr * 100))
    if r.get('high_knife'):
        t.append('<span class="tag w">高位飞刀</span>')
    for e in extra or []:
        t.append('<span class="tag w">%s</span>' % esc(e))
    return ''.join(t)


def pick_ops(r):
    close = r.get('close') or 0
    atrp = r.get('atr') or 0
    atr = close * atrp / 100.0
    ma20 = close - (r.get('dma') or 0) * atr
    return {
        'ma20': fnum(ma20), 'atr': fnum(atr) + ' (%.1f%%)' % atrp,
        'buy_lo': fnum(close - 0.4 * atr), 'buy_hi': fnum(close + 0.3 * atr),
        'stop': fnum(close - 1.2 * atr),
        'stop_pct': pct_txt(-1.2 * atrp if close else 0),
        't1': fnum(close * 1.06), 't2': fnum(close * 1.12),
    }


def render_pick(r, i, narr):
    nd = (narr.get('picks') or {}).get(r['code']) or {}
    chg = r.get('chg')
    ops = pick_ops(r)
    hd = (
        '<div class="pick-hd"><span class="rank%s">%d</span>'
        '<span class="pname">%s</span><span class="pcode mono">%s · %s</span>%s'
        '<span style="margin-left:auto" class="mono">收 %s <span class="%s">%s</span></span></div>'
        % (' g' if i == 1 else '', i, esc(r.get('name')), esc(r.get('code')),
           esc(r.get('seg') or '-'), pick_tags(r, nd.get('tags_extra')),
           fnum(r.get('close')), pct_cls(chg), pct_txt(chg)))
    b1 = (bar('位置安全', r.get('sp', 0), 35, True) + bar('基本面', r.get('sf', 0), 30, True)
          + bar('逻辑纯度', r.get('sb', 0), 20, True) + bar('技术', r.get('st', 0), 15, True))
    b2 = (bar('技术总分', r.get('tech', 0), 30) + bar('结构健康', r.get('sh') or 0, 3)
          + bar('趋势结构', r.get('tsc') or 0, 8) + bar('量价', r.get('vp', 0), 6)
          + bar('市场契合', r.get('mf', 0), 5) + bar('相对强度', r.get('mrs', 0), 5))
    sec = (
        '<div class="sec"><div class="hd">核心买入逻辑</div><p>%s</p></div>'
        '<div class="sec"><div class="hd">主要风险</div><p>%s</p></div>'
        % (nd.get('logic') or esc(r.get('fact') or '（待补充）'),
           nd.get('risk') or '（待补充）'))
    op = ('<div class="ops">'
          '<div class="op"><div class="k">MA20</div><div class="v mono">%s</div></div>'
          '<div class="op"><div class="k">ATR14</div><div class="v mono">%s</div></div>'
          '<div class="op"><div class="k">买入区间</div><div class="v mono">%s – %s</div></div>'
          '<div class="op"><div class="k">止损位</div><div class="v mono down">%s (%s)</div></div>'
          '<div class="op"><div class="k">目标位 1</div><div class="v mono up">%s (+6.0%%)</div></div>'
          '<div class="op"><div class="k">目标位 2</div><div class="v mono up">%s (+12.0%%)</div></div>'
          '</div>' % (ops['ma20'], ops['atr'], ops['buy_lo'], ops['buy_hi'],
                      ops['stop'], ops['stop_pct'], ops['t1'], ops['t2']))
    fals = ('<div class="falsify"><b>证伪条件：</b>%s</div>'
            % (nd.get('falsify') or ('收盘有效跌破 <b>%s</b>，或次日放量下跌且收盘价低于 VWAP 1%% 以上'
                                     '——任一出现即离场。' % ops['stop'])))
    body = ('<div class="pick-bd"><div class="grid2"><div>'
            '<h3>四维综合评分 <b class="mono %st">%s</b></h3><div class="bars">%s</div></div>'
            '<div><h3>六维技术评分（上游口径）</h3><div class="bars">%s</div></div></div>'
            '%s%s%s</div>'
            % (rate_cls(min(100, (r.get('total') or 0))), fnum(r.get('total'), 1), b1, b2, sec, op, fals))
    return '<div class="pick">%s%s</div>\n' % (hd, body)


def render_pool(rows):
    tr = []
    for i, r in enumerate(rows, 1):
        tr.append('<tr><td class="mono">%d</td><td>%s</td><td class="mono">%s</td>'
                  '<td>%s</td><td class="r mono">%s</td><td class="r mono %s">%s</td>'
                  '<td class="r mono">%s</td><td class="r mono">%s</td><td class="r mono">%s</td>'
                  '<td>%s</td></tr>'
                  % (i, esc(r.get('name')), esc(r.get('code')), esc(r.get('seg') or ''),
                     fnum(r.get('total'), 1), pct_cls(r.get('chg')), pct_txt(r.get('chg')),
                     fnum(r.get('dma')), fnum(r.get('amt')), fnum(r.get('totalcap'), 1),
                     esc(r.get('theme') or '')))
    return ('<h2>三、候选池全量（%d 只，按总分降序）</h2>'
            '<table><tr><th>#</th><th>名称</th><th>代码</th><th>板块</th><th class="r">总分</th>'
            '<th class="r">涨跌</th><th class="r">距MA20</th><th class="r">成交额亿</th>'
            '<th class="r">市值亿</th><th>题材</th></tr>%s</table>' % (len(rows), ''.join(tr)))


def render_dropped(dropped):
    if not dropped:
        return '<div class="note">本期无硬约束剔除标的（用户已取消成交额门槛）。</div>'
    tr = ''.join('<tr><td class="mono">%s</td><td>%s</td><td>%s</td><td>%s</td></tr>'
                 % (esc(d.get('code')), esc(d.get('name')), esc(d.get('seg') or ''),
                    esc(d.get('reason'))) for d in dropped)
    return ('<h2>四、硬约束剔除</h2><table><tr><th>代码</th><th>名称</th><th>板块</th>'
            '<th>原因</th></tr>%s</table>' % tr)


def render_liquidity(rows):
    top = sorted(rows, key=lambda r: (r.get('amt') or 0))[:10]
    tr = ''.join('<tr><td>%s</td><td class="mono">%s</td><td class="r mono">%s</td>'
                 '<td class="r mono">%s</td><td class="r mono">%s</td></tr>'
                 % (esc(r.get('name')), esc(r.get('code')), fnum(r.get('amt')),
                    fnum(r.get('turnover')), fnum(r.get('totalcap'), 1)) for r in top)
    return ('<h2>五、流动性参考（成交额最低 10 只，仅自查用，不参与打分）</h2>'
            '<table><tr><th>名称</th><th>代码</th><th class="r">成交额亿</th>'
            '<th class="r">换手%%</th><th class="r">市值亿</th></tr>%s</table>' % tr)


def render_foot(narr):
    return ('<div class="disc">%s<br><br>本页由数据管线自动生成，'
            '仅供本人交易参考，不构成投资建议。评分框架 v3：位置安全 35 + 基本面 30 + 逻辑纯度 20 + 技术 15，'
            '不含流动性维度；成交额不参与打分、不影响排名。</div>'
            % (narr.get('foot') or ''))


def render(data_dir, narrative_path, out_path):
    market = load(os.path.join(data_dir, 'market.json'))
    picks = load(os.path.join(data_dir, 'picks.json'), [])
    scored = load(os.path.join(data_dir, 'scored.json'), [])
    dropped = load(os.path.join(data_dir, 'dropped.json'), [])
    narr = load(narrative_path)
    css = open(os.path.join(BASE, 'template.css'), encoding='utf-8').read()

    td = market.get('trade_date') or os.path.basename(data_dir.rstrip('/\\'))
    gen = narr.get('generated_at_label') or (market.get('generated_at') or '')[:16].replace('T', ' ')

    body = [render_head(td, market, gen), render_alert(narr), render_snap(market.get('env') or {}),
            '<h2>二、今日五个标的（按确定性排名）</h2>',
            '<div class="sub" style="margin-bottom:12px">排名即优先级。'
            '<b>只买 1 只 → 取第 1 名；买 2 只 → 取第 1 + 2 名。</b>'
            '评分框架 v3：位置安全 35 + 基本面 30 + 逻辑纯度 20 + 技术 15 = 100（不含流动性维度）。</div>']
    body.append(
        '<div class="legend"><b>色条图例（按得分率着色，与涨跌的红/绿配色无关）：</b>'
        '<span><i class="qd" style="background:#0f8a4a"></i>优 · ≥85%</span>'
        '<span><i class="qd" style="background:#65a30d"></i>良 · 70–85%</span>'
        '<span><i class="qd" style="background:#d97706"></i>中 · 50–70%</span>'
        '<span><i class="qd" style="background:#9ca3af"></i>弱 · &lt;50%</span>'
        '<span style="color:var(--muted)">条宽＝得分率，颜色＝强弱档位；粗条为四维，细条为六维技术分。</span></div>')
    for i, r in enumerate(picks, 1):
        body.append(render_pick(r, i, narr))
    if narr.get('summary'):
        body.append('<div class="note"><b>操作提示：</b>%s</div>' % narr['summary'])
    body.append(render_pool(scored))
    body.append(render_liquidity(scored))
    body.append(render_dropped(dropped))
    body.append(render_foot(narr))

    doc = ('<!DOCTYPE html>\n<html lang="zh-CN">\n<head>\n<meta charset="UTF-8">\n'
           '<meta name="viewport" content="width=device-width, initial-scale=1">\n'
           '<title>A股短线选股看板 · %s</title>\n<style>\n%s\n</style>\n</head>\n<body>\n'
           '<div class="wrap">\n%s\n</div>\n</body>\n</html>\n'
           % (datetime_fmt(td), css, '\n'.join(body)))
    os.makedirs(os.path.dirname(os.path.abspath(out_path)) or '.', exist_ok=True)
    open(out_path, 'w', encoding='utf-8').write(doc)
    print('已生成 %s (%d 字节)' % (out_path, len(doc.encode('utf-8'))))
    return out_path


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument('--data', required=True, help='out/<YYYYMMDD> 目录')
    ap.add_argument('--narrative', required=True, help='文案 narrative.json')
    ap.add_argument('--out', required=True, help='输出 HTML 路径')
    a = ap.parse_args()
    render(a.data, a.narrative, a.out)
    return 0


if __name__ == '__main__':
    sys.exit(main())
