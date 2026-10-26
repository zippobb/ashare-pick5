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
UNIVERSE_SCAN_JSON = '/opt/stock-automation/scanner/config/theme_universe_scan_latest.json'   # 全市场题材扫描（独立数据源，不进 Top5）


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
    chips = [
        ('交易日', '%s（%s）' % (d, weekday(td))),
        ('trade_date', td),
        ('生成时间', '%s CST' % (gen_at or '-')),
        ('上一交易日', prev or '-'),
        ('下一交易日', nxt or '-'),
    ]
    chip_html = ''.join(
        '<span class="chip%s">%s <b>%s</b></span>' % (' ok' if k == 'trade_date' else '', esc(k), esc(v))
        for k, v in chips)
    return (
        '<div class="hero">\n'
        '<h1>A股短线选股看板<span class="date">%s（%s）</span></h1>\n'
        '<div class="sub">数据源：ECS 线上管线 · stock-key10 bundle · 已核对为当日最新交付</div>\n'
        '<div class="hero-meta">%s</div>\n</div>\n' % (d, weekday(td), chip_html))


def datetime_fmt(td):
    return '%s-%s-%s' % (td[:4], td[4:6], td[6:8])


def theme_cell(r):
    mark = ' <span class="tag w" title="%s">待修正</span>' % esc(r.get('theme_reason') or '') \
        if r.get('theme_review') else ''
    if r.get('theme'):
        return esc(r['theme']) + mark
    return '<span class="tag gy">未入题材表</span>%s' % esc(r.get('sector') or '-')


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
    t = ['<span class="tag">%s</span>' % (esc(r['theme']) if r.get('theme')
                                         else '行业：%s' % esc(r.get('sector') or '-'))]
    if r.get('theme_review'):
        t.append('<span class="tag w">题材待修正：%s</span>' % esc(r.get('theme_reason') or ''))
    biz = r.get('biz') or ''
    if '待核验' in biz or not biz:
        t.append('<span class="tag gy">题材待核验</span>')
    else:
        t.append('<span class="tag b">%s</span>' % esc(str(biz)[:8]))
    kr = r.get('kf_ratio')
    ky = r.get('kf_yoy')
    if kr is not None and r.get('np') and r['np'] > 0:
        tip = ('扣非同比 %s%% · 扣非ROE %s%%'
               % (fnum(ky, 1) if ky is not None else 'NA', fnum(r.get('roe_kf'), 2)))
        if kr >= 0.6:
            t.append('<span class="tag b" title="%s">扣非占比 %.0f%%</span>' % (esc(tip), kr * 100))
        else:
            t.append('<span class="tag w" title="%s">一次性收益抬高利润 −%d（%s）</span>'
                     % (esc(tip), r.get('kf_pen') or 0,
                        '主业改善' if (ky is not None and ky > 0) else '主业下滑'))
    if r.get('risk_flag'):
        t.append('<span class="tag w" title="%s">观察池标记：%s（不影响评分）</span>'
                 % (esc(r.get('risk_reason') or ''), esc(r['risk_flag'])))
    if r.get('high_knife'):
        t.append('<span class="tag w" title="股价高出20日均线≥2个ATR（ATR=日均真实波幅，'
                 '衡量这只票一天的正常波动）。均线是成本中枢，离得越远，回落修复的空间越大。">'
                 '高位飞刀</span>')
    if r.get('crowd_pen'):
        t.append('<span class="tag w" title="年涨幅 %s%% · ATR %s%% · 距MA20 %s">拥挤扣分 -%d（%s）</span>'
                 % (fnum(r.get('r250'), 0), fnum(r.get('atrp'), 1), fnum(r.get('dma')),
                    r['crowd_pen'], esc(r.get('crowd_tag') or '')))
    if r.get('cluster'):
        t.append('<span class="tag b">产业链：%s</span>' % esc(r['cluster']))
    if r.get('rot_state') and r.get('rot_state') != '-':
        t.append('<span class="tag %s" title="题材轮动口径：%s｜今日%+.2f%% 3日%+.2f%%">题材轮动：%s</span>'
                 % (STATE_CLS.get(r.get('rot_state'), 'gy'), esc(r.get('rot_theme') or ''),
                    r.get('rot_ret1') or 0, r.get('rot_ret3') or 0, esc(r['rot_state'])))
    _stg = stage_txt(r)
    if _stg:
        t.append('<span class="tag %s" title="题材阶段：%s｜连涨 %s 天｜本轮 %+.1f%%｜距MA20 %+.1f%%｜60日位 %.0f%%｜趋势 %s">题材阶段：%s</span>'
                 % ('w' if r.get('rot_topping') else
                    ('b' if r.get('rot_stage') == '首涨' else 'gy'),
                    _stg, r.get('rot_streak') or 0, r.get('rot_run_gain') or 0,
                    r.get('rot_dist_ma20') or 0, (r.get('rot_pos60') or 0) * 100,
                    esc(r.get('rot_trend') or '-'), esc(_stg)))
    if r.get('stage_flag'):
        t.append('<span class="tag w" title="%s">末端风险</span>' % esc(r['stage_flag']))
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
    b1 = (bar('位置60', r.get('sp', 0), 20, True) + bar('基本面', r.get('sf', 0), 30, True)
          + bar('逻辑纯度', r.get('sb', 0), 20, True) + bar('技术', r.get('st', 0), 30, True)
          + (bar('题材内相对强势', r.get('srs', 0), 30, True) if r.get('srs') else ''))
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
    pens = []
    if r.get('crowd_pen'):
        pens.append('拥挤度 −%d' % r['crowd_pen'])
    if r.get('kf_pen'):
        pens.append('利润质量 −%d' % r['kf_pen'])
    crowd = ('<div class="note">总分 = 四维合计 %s − %s</div>'
             % (fnum((r.get('sp') or 0) + (r.get('sf') or 0) + (r.get('sb') or 0) + (r.get('st') or 0), 1),
                '、'.join(pens))) if pens else ''
    body = ('<div class="pick-bd"><div class="grid2"><div>'
            '<h3>四维综合评分 <b class="mono %st">%s</b></h3><div class="bars">%s</div>%s</div>'
            '<div><h3>六维技术评分（上游口径）</h3><div class="bars">%s</div></div></div>'
            '%s%s%s</div>'
            % (rate_cls(min(100, (r.get('total') or 0))), fnum(r.get('total'), 1), b1, crowd, b2, sec, op, fals))
    return '<div class="pick">%s%s</div>\n' % (hd, body)


ROT_RENDER_MARK_20261015 = True
STATE_CLS = {'\u52a0\u901f': 'b', '\u542f\u52a8': 'b', '\u5ef6\u7eed': '',
             '\u8f6c\u8dcc': 'w', '\u9000\u6f6e': 'gy', '\u9707\u8361': 'gy', '-': 'gy'}
STATE_TIP = {
    '\u52a0\u901f': '\u4eca\u65e5\u6da8\u5e45\u6392\u540d\u524d 20%\u4e14\u52a0\u901f\u5ea6\u6392\u540d\u524d 20%\uff1a\u6da8\u52bf\u6b63\u5728\u52a0\u5feb',
    '\u542f\u52a8': '\u4eca\u65e5\u5f3a\u4f46\u8fd1 3 \u65e5\u6392\u540d\u4e0d\u9ad8\uff1a\u521a\u521a\u542f\u52a8',
    '\u5ef6\u7eed': '\u4eca\u65e5\u4e0e\u8fd1 3 \u65e5\u5747\u5c45\u524d 40%\uff1a\u6da8\u52bf\u5ef6\u7eed',
    '\u8f6c\u8dcc': '\u8fd1 5 \u65e5\u4ecd\u6da8\u4f46\u4eca\u65e5\u8f6c\u8dcc\uff1a\u9996\u6b21\u56de\u8c03\uff0c\u56de\u6d4b\u4e2d\u5e76\u975e\u7acb\u5373\u89c1\u9876',
    '\u9000\u6f6e': '\u8fd1 3 \u65e5\u6392\u540d\u57ab\u5e95 20%\uff1a\u8f6e\u52a8\u5df2\u8d70\u5b8c',
    '\u9707\u8361': '\u65e0\u660e\u786e\u8f6e\u52a8\u4fe1\u53f7',
}


def rot_state_tag(st):
    st = st or '-'
    return '<span class="tag %s" title="%s">%s</span>' % (
        STATE_CLS.get(st, 'gy'), esc(STATE_TIP.get(st, '')), esc(st))


STAGE_CLS = {'首涨': 'b', '回落': 'gy', '退潮': 'gy', '转跌': 'w'}


def stage_txt(r):
    """题材阶段短文字：首涨 / 连涨N天 / 连涨N天·末端。"""
    stg = r.get('rot_stage')
    if not stg or stg == '-':
        return ''
    if stg.endswith('末端') or r.get('rot_topping'):
        return stg + ' ⚠'
    return stg


def _g(r, *keys, **kw):
    """兼容两套字段名：题材快照用 stage/topping/up_streak/run_gain，个股用 rot_* 前缀。"""
    d = kw.get('default')
    for k in keys:
        v = r.get(k)
        if v not in (None, '', 0) or (d is None and v == 0):
            return v
    return d


def rot_stage_tag(r):
    # 题材快照（hot_top10）= stage/topping/up_streak/run_gain；个股候选 = rot_* 前缀
    stg = _g(r, 'rot_stage', 'stage', default='-') or '-'
    if stg == '-':
        return '<span class="tag gy">-</span>'
    streak = _g(r, 'rot_streak', 'up_streak', default=0) or 0
    run = _g(r, 'rot_run_gain', 'run_gain', default=0) or 0
    if _g(r, 'rot_topping', 'topping', 'topping_risky', default=False):
        return ('<span class="tag w" title="连涨 %s 天，本轮 +%s%%；'
                '震荡/下跌行情中趋势末端板块容易买在高点">%s ⚠</span>'
                % (streak, fnum(run, 1), esc(stg)))
    key = ('b' if stg == '首涨' else ('gy' if stg in ('回落', '退潮', '转跌') else ''))
    tip = '连涨 %s 天，本轮 %s%%' % (streak, fnum(run, 1))
    return '<span class="tag %s" title="%s">%s</span>' % (key, esc(tip), esc(stg))


def render_rotation(rot):
    """今日题材轮动（精简版）。"""
    if not rot or not rot.get('trade_date'):
        return ''
    mode = rot.get('mode') or 'OFF'
    hot = ((rot.get('hot_clusters') or rot.get('hot_top10')) or [])[:8]
    using_clusters = bool(rot.get('hot_clusters'))
    cold = (rot.get('cold_bottom5') or [])[:5]
    topping = (rot.get('topping') or [])[:6]
    mstate = rot.get('market_state') or '-'
    mainline = rot.get('mainline') or {}
    focus_names = set((f or {}).get('theme') for f in (rot.get('focus') or []))

    boxes = [
        ('市场状态', mstate, '近20日 %s%%' % fnum(rot.get('market_ret20'), 1)),
        ('上涨题材', '%s / %s' % (rot.get('n_up_themes') or 0, rot.get('n_themes') or 0),
         '全市场等权 %s' % pct_txt(rot.get('market_ret1'))),
        ('本期模式', '题材优先' if mode == 'THEME' else ('软加分' if mode == 'SOFT' else '回退纯 D'),
         ('热题材候选 ' + str(rot.get('n_cand', 0)) + ' 只') if mode == 'THEME'
         else ('0~30 分题材加分' if mode == 'SOFT' else '热题材在池内不足 3 只')),
        ('主线判定', mainline.get('market_mainline_state') or '-', '价格与广度，前向观察'),
    ]
    bx = []
    for k, v, d in boxes:
        bx.append('<div class="sbox"><div class="k">' + esc(k) + '</div>'
                  '<div class="v ' + pct_cls(v if isinstance(v, (int, float)) else None) + '">'
                  + esc(v) + '</div><div class="d">' + esc(d) + '</div></div>')

    tr = []
    for ix, h in enumerate(hot, 1):
        hname = h.get('theme') or h.get('cluster')
        nm = esc(hname)
        if hname in focus_names:
            nm = '<b>' + nm + '</b> <span class="tag b">本期主攻</span>'
        tip = '加速度 %s%% · 上涨家数 %s%% · 原始题材 %s' % (
            fnum(h.get('acc'), 2), fnum((h.get('up1') or 0) * 100, 0),
            esc('/'.join(h.get('raw_themes') or [hname])))
        tr.append('<tr><td class="mono">' + str(ix) + '</td><td title="' + tip + '">' + nm + '</td>'
                  '<td>' + rot_stage_tag(h) + '</td>'
                  '<td class="r mono ' + pct_cls(h.get('ret1')) + '">' + pct_txt(h.get('ret1')) + '</td>'
                  '<td class="r mono ' + pct_cls(h.get('ret3')) + '">' + pct_txt(h.get('ret3')) + '</td>'
                  '<td class="r mono ' + pct_cls(h.get('dist_ma20')) + '">' + pct_txt(h.get('dist_ma20')) + '</td>'
                  '<td class="r mono">' + str(h.get('n_members') if using_clusters else (h.get('n_pool') or 0)) + '</td></tr>')

    trt = []
    for h in topping:
        trt.append('<tr><td>' + esc(h.get('theme')) + '</td>'
                   '<td class="r mono">' + str(h.get('up_streak') or 0) + '</td>'
                   '<td class="r mono ' + pct_cls(h.get('run_gain')) + '">' + pct_txt(h.get('run_gain')) + '</td>'
                   '<td class="r mono ' + pct_cls(h.get('dist_ma20')) + '">' + pct_txt(h.get('dist_ma20')) + '</td>'
                   '<td class="r mono">' + str(h.get('n_pool') or 0) + '</td></tr>')

    trc = []
    for c in cold:
        trc.append('<tr><td>' + esc(c.get('theme')) + '</td>'
                   '<td class="r mono ' + pct_cls(c.get('ret3')) + '">' + pct_txt(c.get('ret3')) + '</td>'
                   '<td class="r mono ' + pct_cls(c.get('dist_ma20')) + '">' + pct_txt(c.get('dist_ma20')) + '</td>'
                   '<td class="r mono">' + str(c.get('n_pool') or 0) + '</td></tr>')

    watch_rows = []
    clusters = rot.get('clusters') or {}
    for label, w in (mainline.get('watchlist') or {}).items():
        best = clusters.get(w.get('best_cluster')) or {}
        watch_rows.append('<tr><td>' + esc(label) + '</td><td>' + esc(w.get('state') or '-') + '</td>'
                          '<td>' + esc(w.get('best_cluster') or '-') + '</td>'
                          '<td class="r mono ' + pct_cls(best.get('excess5')) + '">' + pct_txt(best.get('excess5')) + '</td>'
                          '<td class="r mono">' + fnum((best.get('above_ma20_share') or 0) * 100, 0) + '%</td>'
                          '<td class="r mono">' + fnum((best.get('near_high20_share') or 0) * 100, 0) + '%</td>'
                          '<td class="r mono">' + pct_txt(best.get('leader_ret10')) + '</td></tr>')

    out = []
    out.append('<h2>二、今日题材轮动</h2>')
    out.append('<div class="snap">' + ''.join(bx) + '</div>')
    if watch_rows:
        out.append('<div class="sub" style="margin:10px 0 4px"><b>六个候选方向的主线状态</b>（每日固定口径落盘，供前向验证）</div>')
        out.append('<table><tr><th>方向</th><th>状态</th><th>代表集群</th>'
                   '<th class="r">5日超额</th><th class="r">MA20上</th>'
                   '<th class="r">近20日高</th><th class="r">龙头10日</th></tr>' + ''.join(watch_rows) + '</table>')
    out.append('<table><tr><th>#</th><th>题材集群</th><th>阶段</th>'
               '<th class="r">今日</th><th class="r">3日</th>'
               '<th class="r">距MA20</th><th class="r">' + ('成员' if using_clusters else '池内') + '</th></tr>' + ''.join(tr) + '</table>')
    if trt:
        out.append('<div class="sub" style="margin:14px 0 4px">'
                   '<b>上涨趋势末端</b>（连涨 &ge;' + str(rot.get('top_streak') or 4)
                   + ' 天，' + esc(mstate) + '中容易买在高点）</div>')
        out.append('<table><tr><th>题材</th><th class="r">连涨</th>'
                   '<th class="r">本轮涨幅</th><th class="r">距MA20</th>'
                   '<th class="r">池内</th></tr>' + ''.join(trt) + '</table>')
    out.append('<div class="sub" style="margin:14px 0 4px"><b>退潮题材</b>（轮动已走完，回避）</div>')
    out.append('<table><tr><th>题材</th><th class="r">3日</th>'
               '<th class="r">距MA20</th><th class="r">池内</th></tr>' + ''.join(trc) + '</table>')
    out.append('<div class="note"><b>怎么读：</b>「阶段」= 首涨 / 连涨第 N 天 / 末端（连涨&ge;4 天）；'
               '热度用<b>全市场</b>成员算，不设成交额门槛；高相关题材已按集群去重，鼠标悬停可看原始题材。'
               '主线状态由相对收益、上涨持续性、MA20广度、近20日新高扩散和龙头强度共同判定，暂只观察不改权重。'
               '三级级联 RET1&times;3 &rarr; ACC&times;5 &rarr; RET10&times;5，'
               '池内凑不出 3 只或普跌日（全市场&le;-1%）则回退纯 D。'
               '回测：题材优先 +1.08%/期 vs 纯 D +0.47%/期；'
               '题材内相对强势 +1.34%/期，题材内随机仅 +0.39%（不显著）——'
               '<b>题材只负责缩小范围，alpha 来自筛完之后怎么选</b>。</div>')
    return '\n'.join(out)


def render_universe_scan(us):
    """全市场题材扫描：只显示 ★推荐的前 10 只，7 列。仅供观察，不自动入池。"""
    if not us or not us.get('candidates'):
        return ''
    uni = us.get('universe') or {}
    rec = [x for x in us['candidates'] if x.get('recommend')]
    if not rec:
        return ''
    rows = []
    for x in rec[:10]:
        rows.append('<tr><td class="mono">' + esc(x.get('code')) + '</td>'
                    '<td>' + esc(x.get('name')) + '</td>'
                    '<td>' + ('<span class="tag gy">池内</span>'
                              if x.get('in_pool') else '<span class="tag w">池外</span>') + '</td>'
                    '<td>' + esc(x.get('theme')) + '</td>'
                    '<td class="r mono ' + pct_cls(x.get('rs')) + '">' + pct_txt(x.get('rs')) + '</td>'
                    '<td class="r mono ' + pct_cls(x.get('mom20')) + '">' + pct_txt(x.get('mom20')) + '</td>'
                    '<td class="r mono">' + fnum(x.get('elast'), 0) + '</td>'
                    '<td>' + esc(x.get('quality')) + '</td></tr>')
    out = []
    out.append('<h2>三、全市场热题材强势股（仅供观察，不进 Top5）</h2>')
    out.append('<div class="sub" style="margin:16px 0 6px">'
               '<b>热题材内值得关注的全市场个股</b>（★推荐 %d 只，显示前 10；'
               '仅供观察，<b>不自动入池</b>）</div>' % len(rec))
    out.append('<table><tr><th>代码</th><th>名称</th><th>在池</th><th>题材</th>'
               '<th class="r">相对强势</th><th class="r">20日%</th>'
               '<th class="r">弹性</th><th>质量</th></tr>' + ''.join(rows) + '</table>')
    out.append('<div class="note">全市场 %d 只 &rarr; 剔ST %d &rarr; %d 只；'
               '取「首涨/连涨%d~%d天」的热题材，按<b>题材内相对强势</b>每题材挑 %d 只。'
               '&#9733; = 弹性&ge;18 且非亏损。题材标签为机器派生，可能偏离主业，'
               '是否入池需人工核实。</div>'
               % (uni.get('base') or 0, uni.get('st_removed') or 0, uni.get('final') or 0,
                  us.get('stage_filter', {}).get('early_min', 1),
                  us.get('stage_filter', {}).get('early_max', 3), us.get('per_theme') or 5))
    out.append('<div class="note"><b>与上面的 Top5 完全独立：</b>'
               'Top5 只从观察池 202 只里产生；本区是全市场热题材里相对强势的个股，'
               '<b>不参与 Top5 排名、不自动入池</b>，只作为人工换池的候选清单。'
               '“池外”即当前不在观察池内的标的，才是值得考虑补进池子的。</div>')
    return '\n'.join(out)


def render_market_picks(picks_market):
    """全市场腿 Top5（趋势型因子，独立于池内 v5 四维）。精简表，因子口径不同不混排。"""
    if not picks_market:
        return ''
    tr = []
    for i, r in enumerate(picks_market, 1):
        trend = '20日%s%% · 连阳%d天%s' % (
            fnum(r.get('mom20')), r.get('streak') or 0,
            ' · 均线多头' if r.get('bull') else '')
        tr.append('<tr><td class="mono">%d</td><td>%s</td><td class="mono">%s</td>'
                  '<td>%s</td><td class="r mono">%.0f</td>'
                  '<td class="r mono">%s</td><td class="r mono %s">%s</td>'
                  '<td class="r mono">%s</td><td class="r mono">%s</td></tr>'
                  % (i, esc(r.get('name')), esc(r.get('code')),
                     esc(r.get('rot_theme') or r.get('industry') or '-'),
                     r.get('pos60') or 0, trend,
                     pct_cls(r.get('mom20')), pct_txt(r.get('mom20')),
                     ('-%d' % r.get('crowd_pen')) if r.get('crowd_pen') else '—',
                     fnum(r.get('total'), 1)))
    return ('<h2>四之二、全市场 Top5（趋势型，为通用化验证）</h2>'
            '<div class="note">全市场腿使用<b>独立趋势型因子</b>（位置60 + 20日/5日涨幅分位 + 连阳 + 均线多头），'
            '基本面只作门槛不作打分；权重为占位，与备忘2/8回测一起定参。'
            '<b>本区为模型通用化前置验证，非池内实盘建议。</b></div>'
            '<table><tr><th>#</th><th>名称</th><th>代码</th><th>题材</th>'
            '<th class="r">位置60</th><th>趋势</th><th class="r">20日</th>'
            '<th class="r">拥挤</th><th class="r">总分</th></tr>%s</table>' % ''.join(tr))


def render_pool(rows, topn=20):
    shown = rows[:topn] if (topn and len(rows) > topn) else rows
    tr = []
    for i, r in enumerate(shown, 1):
        cap = ' <span class="tag gy">限</span>' if r.get('capped_by') else ''
        flag = (' <span class="tag w" title="%s">%s</span>' % (esc(r.get('risk_reason') or ''),
                                                              esc(r.get('risk_flag')))
                if r.get('risk_flag') else '')
        tr.append('<tr><td class="mono">%d</td><td>%s</td><td class="mono">%s</td>'
                  '<td>%s</td><td class="r mono">%s</td><td class="r mono %s">%s</td>'
                  '<td class="r mono">%s</td><td class="r mono">%s</td>'
                  '<td class="r mono">%s</td><td>%s</td><td>%s%s</td><td>%s</td></tr>'
                  % (i, esc(r.get('name')), esc(r.get('code')), esc(r.get('seg') or ''),
                     fnum(r.get('total'), 1), pct_cls(r.get('chg')), pct_txt(r.get('chg')),
                     fnum(r.get('dma')), fnum(r.get('totalcap'), 1),
                     ('-%d' % (r.get('crowd_pen', 0) + r.get('kf_pen', 0)))
                     if (r.get('crowd_pen') or r.get('kf_pen')) else '—',
                     theme_cell(r), rot_state_tag(r.get('rot_state')) + ' ' + rot_stage_tag(r),
                     cap, flag))
    n = sum(1 for r in rows if not r.get('theme'))
    notes = []
    if n:
        notes.append('<div class="note">题材取自上游人工维护的题材映射表，非算法推断。'
                     '标「未入题材表」的 %d 只不在表内，此处显示行业分类代替；'
                     '其逻辑纯度维度按最低档计分，排名已相应偏低。</div>' % n)
    m = sum(1 for r in rows if r.get('risk_flag'))
    if m:
        notes.append('<div class="note">规则1（2026-10-12 生效）：观察池内标的不再因利润质量'
                     '被硬剔除，改打「观察池标记」，%d 只带标记。标记仅提示，'
                     '不参与评分，对总分与排名无加减影响。</div>' % m)
    _more = ('<div class="note">共 %d 只参与打分，此处按总分降序显示前 %d 只。</div>'
             % (len(rows), len(shown))) if len(shown) < len(rows) else ''
    return ('<h2>五、候选池（共 %d 只，按总分降序）</h2>'
            '<table><tr><th>#</th><th>名称</th><th>代码</th><th>板块</th><th class="r">总分</th>'
            '<th class="r">涨跌</th><th class="r">距MA20</th>'
            '<th class="r">市值亿</th><th>题材</th><th>轮动状态</th><th>标记</th></tr>%s</table>%s'
            % (len(rows), ''.join(tr), ''.join(notes) + _more))


def render_dropped(dropped):
    if not dropped:
        return ('<h2>六、本期剔除</h2><div class="note">本期无标的被剔除。'
                '硬约束 = ST / *ST、总市值 &lt; 30 亿、弹性分 &lt; 18（低弹性票涨不动，不进候选）。'
                '规则1（2026-10-12）后利润质量只作标记、不再剔除。</div>')
    tr = ''.join('<tr><td class="mono">%s</td><td>%s</td><td>%s</td><td>%s</td></tr>'
                 % (esc(d.get('code')), esc(d.get('name')), esc(d.get('seg') or ''),
                    esc(d.get('reason'))) for d in dropped)
    return ('<h2>六、本期剔除（%d 只）</h2>' % len(dropped) +
            '<div class="note">硬约束 = ST / *ST、总市值 &lt; 30 亿、'
            '弹性分 &lt; 18（低弹性票涨不动，不进候选）。'
            '规则1（2026-10-12）后利润质量只作标记、不再剔除。</div>'
            '<table><tr><th>代码</th><th>名称</th><th>板块</th>'
            '<th>原因</th></tr>%s</table>' % tr)


def render_liquidity(rows):
    top = sorted(rows, key=lambda r: (r.get('amt') or 0))[:10]
    tr = ''.join('<tr><td>%s</td><td class="mono">%s</td><td class="r mono">%s</td>'
                 '<td class="r mono">%s</td><td class="r mono">%s</td></tr>'
                 % (esc(r.get('name')), esc(r.get('code')), fnum(r.get('amt')),
                    fnum(r.get('turnover')), fnum(r.get('totalcap'), 1)) for r in top)
    return ('<h2>六、流动性参考（成交额最低 10 只，仅自查用，不参与打分）</h2>'
            '<table><tr><th>名称</th><th>代码</th><th class="r">成交额亿</th>'
            '<th class="r">换手%%</th><th class="r">市值亿</th></tr>%s</table>' % tr)


def render_foot(narr):
    return ('<div class="disc">%s<br><br>本页由数据管线自动生成，'
            '仅供本人交易参考，不构成投资建议。评分框架 v5：位置60 20 + 基本面 30 + 逻辑纯度 20 + 技术 30'
            ' + 题材内相对强势 30（仅题材优先模式生效），不含流动性维度；成交额不参与打分、不影响排名。<br>'
            '<b>位置60</b>（60日价格位置）=（今收 − 60日最低）÷（60日最高 − 60日最低）×100：'
            '100 = 创 60 日新高，0 = 60 日最低。它衡量股价在近 60 日箱体里的相对位置，'
            '不是「最近涨了多少」。<br>'
            '公式与阈值为实验参数，未做历史回测，不保证未来有效。</div>'
            % (narr.get('foot') or ''))


def render(data_dir, narrative_path, out_path):
    market = load(os.path.join(data_dir, 'market.json'))
    picks = load(os.path.join(data_dir, 'picks.json'), [])
    scored = load(os.path.join(data_dir, 'scored.json'), [])
    dropped = load(os.path.join(data_dir, 'dropped.json'), [])
    picks_market = load(os.path.join(data_dir, 'picks_market.json'), [])
    narr = load(narrative_path)
    css = open(os.path.join(BASE, 'template.css'), encoding='utf-8').read()

    td = market.get('trade_date') or os.path.basename(data_dir.rstrip('/\\'))
    gen = narr.get('generated_at_label') or (market.get('generated_at') or '')[:16].replace('T', ' ')

    uscan = ((market.get('rotation') or {}).get('universe_scan')
             or load(UNIVERSE_SCAN_JSON))
    body = [render_head(td, market, gen), render_alert(narr), render_snap(market.get('env') or {}),
            render_rotation(market.get('rotation') or {}),
            render_universe_scan(uscan or {}),
            '<h2>四、今日五个标的（按确定性排名）</h2>',
            '<div class="sub" style="margin-bottom:12px">排名即优先级。'
            '<b>只买 1 只 → 取第 1 名；买 2 只 → 取第 1 + 2 名。</b>'
            '评分框架 v5：位置60 20 + 基本面 30 + 逻辑纯度 20 + 技术 30 = 100（不含流动性维度）。'
            '位置60 = 股价在近 60 日高低区间中的相对位置，100 表示创 60 日新高，0 表示 60 日最低。</div>']
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
    body.append(render_market_picks(picks_market))
    body.append(render_pool(scored))
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
