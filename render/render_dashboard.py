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
import csv
import argparse

BASE = os.path.dirname(os.path.abspath(__file__))
WEEK = ['周一', '周二', '周三', '周四', '周五', '周六', '周日']

# 2026-11-07 用户裁决：池内腿 / 全市场腿 Top5 均先从看板隐藏。
# 数据（picks.json / picks_market.json）照常产出，core 逻辑留待优化，仅关闭展示。
# 恢复展示时把对应开关改回 True 即可，不需要改其它代码。
SHOW_POOL_TOP5 = False
SHOW_MARKET_TOP5 = False
# 2026-11-17 用户指令：不再做池内选股，池内只做策略命中标记（见 render_pool_hits）。


# ---------- 工具 ----------
UNIVERSE_SCAN_JSON = '/opt/stock-automation/scanner/config/theme_universe_scan_latest.json'   # 全市场题材扫描（独立数据源，不进 Top5）
ICEPOINT_SCAN_JSON = '/opt/stock-automation/scanner/config/icepoint_scan_latest.json'          # 冰点骤升 + 券商核心标的（观察区，不进 Top5）
SUBFAMILY_STRENGTH_JSON = '/opt/stock-automation/scanner/config/subfamily_strength_latest.json'   # 细分族强度（已参与全市场腿打分）
DIRECTION_STRENGTH_JSON = '/opt/stock-automation/scanner/config/direction_strength.json'   # 大方向主线状态（2026-11-01 新增）
SUBFAMILY_MAP_JSON = '/opt/stock-automation/scanner/config/subfamily_map.json'   # 细分族映射（股票代码 -> 细分族名）
CHAR_QUAD_JSON = '/opt/stock-automation/scanner/config/stock_char_quadrant.json'
YAO_JSON = '/opt/stock-automation/scanner/config/stock_yao.json'   # 妖＝无族+250日内曾暴涨（2026-11-21 用户修正：短期指'窗口内曾出现'）   # 股性四象限（族内 500 日，纯标签，2026-11-19 用户要求上板）
POOL_CSV = '/opt/stock-automation/scanner/config/observation_pool.csv'   # 观察池（自选 + AI 推荐）；2026-11-17 起只做策略命中标记、不再做池内排名


def load(p, default=None):
    if p and os.path.exists(p):
        return json.load(open(p, encoding='utf-8'))
    return default if default is not None else {}


def esc(s):
    return html.escape(str(s), quote=False)


_SF_MAP = None
_SF_MAP_ROT = None   # 由 render 主函数从 market.json 的 rotation 注入（云端无 ECS 路径）


def subfamily_of(code):
    # 返回股票代码所属细分族名（单值映射），无归属返回 None
    global _SF_MAP
    if _SF_MAP is None:
        _SF_MAP = {}
        _sf = _SF_MAP_ROT or load(SUBFAMILY_MAP_JSON) or {}
        for _sb in (_sf.get('subfamilies') or []):
            for _c in (_sb.get('stocks') or []):
                _SF_MAP[_c] = _sb.get('sub')
    return _SF_MAP.get(code)


_CHAR_MAP = None


def char_of(code):
    """股性四象限标签（族内 500 日）。缓存一次，按 6 位代码或 ts_code 查。"""
    global _CHAR_MAP
    if _CHAR_MAP is None:
        _CHAR_MAP = {}
        d = load(CHAR_QUAD_JSON) or {}
        for r in (d.get('stocks') or []):
            _c = r.get('code') or ''
            _CHAR_MAP[_c] = r
            if '.' in _c:
                _CHAR_MAP[_c.split('.')[0]] = r
    return _CHAR_MAP.get(code) or _CHAR_MAP.get(_ts_code(code))


_CHAR_CLS = {'优': 'cq-you', '中': 'cq-mid', '差': 'cq-bad', '钝': 'cq-dull', '妖': 'cq-yao'}

_YAO_MAP = None


def yao_of(code):
    """无族股「妖」标记：无族 + r20>=20% 或 r60>=50%。"""
    global _YAO_MAP
    if _YAO_MAP is None:
        _YAO_MAP = {}
        for _r in ((load(YAO_JSON) or {}).get('stocks') or []):
            _c = _r.get('code') or ''
            _YAO_MAP[_c] = _r
            if '.' in _c:
                _YAO_MAP[_c.split('.')[0]] = _r
    return _YAO_MAP.get(code) or _YAO_MAP.get(_ts_code(code))


def char_tag(code):
    """股性标签：优=跟涨不跟跌 / 中=跟涨也跟跌 / 差=跟跌不跟涨 / 钝=都不跟。纯展示。"""
    r = char_of(code)
    y = yao_of(code)
    if not r and y:
        return ('<span class="tag cq cq-yao" title="妖：无族 + 250日内曾出现20日涨%+.1f%%、跑赢市场%+.1f%%（%s，行业=%s）'
                '——纯资金炒作、无板块归属，不进打分">妖</span>'
                % (y.get('peak_r20') or 0, y.get('peak_ex20') or 0,
                   y.get('peak_date') or '-', esc(y.get('industry_l3') or '-')))
    if not r:
        return '<span class="tag cq cq-na" title="无股性标签（未评级或不在族表）">—</span>'

    q = r.get('quad') or ''
    if q.startswith('弱相关'):
        cls, label = 'cq-na', '不评'
        tip = '族相关性 %.2f < 0.40，族归属可疑或独立行情，不评级' % (r.get('corr') or 0)
    else:
        cls, label = _CHAR_CLS.get(q, 'cq-na'), q
        tip = ('股性四象限（族内 500 日）：优=跟涨不跟跌，中=跟涨也跟跌，差=跟跌不跟涨，钝=都不跟；'
               'up_beta=%.2f dn_beta=%.2f 跟随度=%.2f 族相关=%.2f 前后半一致=%s'
               % (r.get('up_beta') or 0, r.get('dn_beta') or 0, r.get('e') or 0,
                  r.get('corr') or 0, '是' if r.get('stable') else '否'))
    return '<span class="tag cq %s" title="%s">%s</span>' % (cls, esc(tip), esc(label))


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
    _sf_name = subfamily_of(r.get('code'))
    t = ['<span class="tag">%s</span>' % (esc(_sf_name) if _sf_name
                                         else (esc(r['theme']) if r.get('theme')
                                               else '行业：%s' % esc(r.get('sector') or '-')))]
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
    b1 = (bar('资金流', r.get('smf', 0), 30, True) + bar('基本面', r.get('sf', 0), 10, True)
          + bar('5日动量', r.get('sm5', 0), 25, True) + bar('技术', r.get('st', 0), 20, True)
          + bar('市值适配', r.get('smv', 0), 10, True)
          + bar('细分族强度', r.get('sf_boost') or 0, 15, True)
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
    _gross = ((r.get('smf') or 0) + (r.get('sf') or 0) + (r.get('sm5') or 0)
              + (r.get('st') or 0) + (r.get('smv') or 0) + (r.get('sf_boost') or 0))
    _sfn = r.get('sub_family')
    crowd = ('<div class="note">总分 = 五维 %s + 细分族 %s = %s%s</div>'
             % (fnum(_gross - (r.get('sf_boost') or 0), 1), fnum(r.get('sf_boost') or 0, 1),
                fnum(_gross, 1), (' − ' + '、'.join(pens)) if pens else ''))
    _mf1v, _mf5v = r.get('mf1v'), r.get('mf5v')
    if _mf1v is not None or _mf5v is not None:
        crowd += ('<div class="note">主力资金：当日净流入 %s%% 市值 / 近 5 日 %s%%（大单+特大单净额 ÷ 总市值）；'
                  '位置60 = %s（<b>仅展示，2026-11-04 起不计分</b>，池内 IC≈0）</div>'
                  % (fnum(_mf1v, 3), fnum(_mf5v, 3), fnum(r.get('sp'), 1)))
    else:
        crowd += ('<div class="note">主力资金：<b>本股无资金流数据</b>，资金流项按中性 12.5 分计；'
                  '位置60 = %s（仅展示，不计分）</div>' % fnum(r.get('sp'), 1))
    if _sfn:
        crowd += ('<div class="note">所属细分族：<b>%s</b>（%s／%s），5 日 %s</div>'
                  % (esc(_sfn), esc(r.get('sub_family_parent') or '-'),
                     esc(r.get('sub_family_direction') or '-'),
                     pct_txt(r.get('sub_ret5')) if r.get('sub_ret5') is not None else '—'))
    else:
        crowd += '<div class="note">该股不在细分族体系内（未覆盖 36%），细分族项按中性 7.5 分计（15×0.5），不奖不罚</div>'
    # 2026-11-05：表内顺序按「综合评分 + 题材热度加分」排，需把排序分一并显示，否则分数看起来乱序
    _tot = r.get('total') or 0
    _rot = r.get('srot') or 0
    _trot = r.get('total_rot')
    _sort_note = ''
    if _trot is not None and _rot:
        _sort_note = ('<span class="note" style="margin-left:8px">排序分 %s = 综合 %s + 题材热度 %s</span>'
                      % (fnum(_trot, 1), fnum(_tot, 1), fnum(_rot, 1)))
    body = ('<div class="pick-bd"><div class="grid2"><div>'
            '<h3>综合评分（五维 100 + 细分族 10） <b class="mono %st">%s</b>%s</h3>'
            '<div class="bars">%s</div>%s</div>'
            '<div><h3>六维技术评分（上游口径）</h3><div class="bars">%s</div></div></div>'
            '%s%s%s</div>'
            % (rate_cls(min(100, 100.0 * _tot / 110.0)),
               fnum(_tot, 1), _sort_note, b1, crowd, b2, sec, op, fals))
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



def sub_stage(sub):
    """细分族阶段推断：首涨/连涨/末端/退潮，用 ret1/ret5/ret20 关系。"""
    r1 = sub.get('ret1') or 0
    r5 = sub.get('ret5') or 0
    r20 = sub.get('ret20') or 0
    if r5 < 0:
        return '退潮' if r20 < 0 else '回落'
    if r20 >= 10 and r1 < 0:
        return '末端'
    if r1 >= 0 and r20 < 0:
        return '首涨'
    if r5 >= 5:
        return '连涨'
    return '震荡'


def render_events(ev):
    """事件驱动题材备注（AI 生成，仅供展示，不进打分）。

    数据来源：云端自动化每日用检索引擎生成的 data_dir/event_driven.json。
    硬校验：theme + catalyst + source_url(http) 三者缺一即丢弃该条，防止幻觉条目上板。
    """
    items = []
    for x in (ev.get('items') or []):
        if not isinstance(x, dict):
            continue
        theme = (x.get('theme') or '').strip()
        cat = (x.get('catalyst') or '').strip()
        url = (x.get('source_url') or '').strip()
        if not theme or not cat or not url.startswith('http'):
            continue
        items.append({**x, 'theme': theme, 'catalyst': cat, 'source_url': url})
    if not items:
        return ''
    rows = []
    for x in items[:6]:
        conf = x.get('confidence')
        conf_txt = ('%.0f%%' % (float(conf) * 100)) if isinstance(conf, (int, float)) else '-'
        rows.append('<tr><td>' + esc(x['theme']) + '</td>'
                    '<td>' + esc(x.get('direction') or '-') + '</td>'
                    '<td>' + esc(x.get('type') or '事件') + '</td>'
                    '<td>' + esc(x['catalyst']) + '</td>'
                    '<td class="r mono">' + conf_txt + '</td>'
                    '<td><a href="' + esc(x['source_url']) + '" target="_blank" rel="noopener">'
                    + esc(x.get('source_title') or '信源') + '</a></td>'
                    '<td>' + esc(x.get('falsify') or '-') + '</td></tr>')
    out = ['<div class="sub" style="margin:14px 0 6px"><b>事件驱动备注（AI 检索生成，仅参考）</b>'
           + str(len(items)) + ' 条</div>',
           '<table><tr><th>题材</th><th>方向</th><th>类型</th><th>驱动逻辑</th>'
           '<th class="r">置信度</th><th>信源</th><th>证伪条件</th></tr>'
           + ''.join(rows) + '</table>',
           '<div class="note"><b>怎么用：</b>这一块是每日生成看板时由 AI 检索公开信源补充的'
           '<b>题材催化剂</b>，用来解释"为什么涨"，<b>不参与打分、不改变 Top5</b>。'
           '每条都必须有可点开的信源；没有信源的题材不会被列出。'
           '置信度是 AI 自评，仅供参考；证伪条件一旦出现即视为该逻辑失效。</div>']
    return '\n'.join(out)


def render_start59(ice, subs_all):
    """题材启动：5–9% 温和启动（§6.19 最优档），替代原「细分族最强10/最弱10」。

    口径：冰点骤升（前日上涨占比<20% + 当日>50% + 当日均涨>0）
          且 当日族内最高涨幅 落在 5%~9%。
    当日无信号时回溯最近一个有信号的交易日（看板兜底）。
    """
    if not ice:
        return ''
    today = ice.get('start_59')
    if today is None:
        # 兼容旧版 icepoint_scan（无 start_59 字段）：从 bursts 里按 band 现算
        today = [b for b in (ice.get('bursts') or []) if b.get('band') == '5to9']
    fallback = [] if today else (ice.get('recent_59') or [])
    rows_src = today or fallback
    lsd = ice.get('last_start_date') or ''
    cnt = ice.get('counts') or {}
    if not rows_src and not ice.get('trade_date'):
        return ''
    ret5 = {}
    for sb in (subs_all or []):
        if sb.get('sub') is not None:
            ret5[sb['sub']] = sb.get('ret5')

    def _row(b):
        r5 = ret5.get(b.get('sub'))
        return ('<tr><td>' + esc(b.get('sub')) + '</td>'
                '<td>' + esc(b.get('direction') or '') + '</td>'
                '<td class="r mono">' + str(b.get('n_members') or 0) + '</td>'
                '<td class="r mono">' + ('%.0f%%' % ((b.get('prev_up_share') or 0) * 100)) + '</td>'
                '<td class="r mono">' + ('%.0f%%' % ((b.get('cur_up_share') or 0) * 100)) + '</td>'
                '<td class="r mono ' + pct_cls(b.get('cur_mean_ret')) + '">'
                + pct_txt(b.get('cur_mean_ret')) + '</td>'
                '<td class="r mono ' + pct_cls(b.get('max_pct')) + '">'
                + pct_txt(b.get('max_pct')) + '</td>'
                '<td>' + esc((b.get('max_name') or '') + ' ' + (b.get('max_code') or '')) + '</td>'
                '<td class="r mono ' + pct_cls(r5) + '">' + pct_txt(r5) + '</td></tr>')

    head = ('<table><tr><th>细分族</th><th>大方向</th><th class="r">成员</th>'
            '<th class="r">前日占比</th><th class="r">当日占比</th>'
            '<th class="r">当日均涨</th><th class="r">族内最高</th>'
            '<th>领涨股</th><th class="r">族5日</th></tr>')

    out = []
    if rows_src:
        if today:
            out.append('<div class="sub" style="margin:12px 0 6px">'
                       '<b>题材启动 · 温和档（5–9%）' + str(len(rows_src)) + ' 个</b></div>')
        else:
            snap = (lsd[:4] + '-' + lsd[4:6] + '-' + lsd[6:8]) if len(lsd) == 8 else (lsd or '-')
            out.append('<div class="sub" style="margin:12px 0 6px;background:var(--warnbg);'
                       'border-left:4px solid var(--warn);border-radius:6px;padding:9px 12px">'
                       '<b>题材启动 · 温和档</b>｜今日无信号，以下为 <b>' + esc(snap) + '</b> 快照'
                       '<span style="display:block;margin-top:3px;font-size:12px;color:var(--warn)">'
                       '表内「前日占比 / 当日占比 / 当日均涨 / 族内最高 / 领涨股」均为 '
                       + esc(snap) + ' 数据；「族5日」为今日滚动值。</span></div>')
        out.append(head + ''.join(_row(b) for b in rows_src[:15]) + '</table>')
        if len(rows_src) > 15:
            out.append('<div class="note">仅显示当日均涨幅前 15 个，共 ' + str(len(rows_src))
                       + ' 个。</div>')
    else:
        out.append('<div class="sub" style="margin:12px 0 6px">'
                   '<b>题材启动 · 温和档（5–9%）</b>：今日与近 5 日均无符合的细分族</div>')
    out.append('<div class="note"><b>为什么只列 5–9%：</b>「启动」= 冰点骤升（前一日上涨占比'
               '&lt;20%、当日&gt;50% 且均涨转正），再要求<b>当日族内最高涨幅落在 5%–9%</b>。'
               '回测 6192 个启动事件：这一档后续最好（5 日超额 +0.65%、10 日 +1.34%、胜率 60.2%），'
               '温和上涨说明资金在吸筹、还有空间；涨停级（&ge;15%）反而最差（10 日仅 +0.20%），'
               '当日已把情绪打满。<b>本区只观察，不进 Top5、不参与打分。</b>'
               '今日分布：冰点骤升 ' + str(cnt.get('bursts', 0)) + ' 个（5–9% 温和 '
               + str(cnt.get('start_59', 0)) + ' / &ge;9% 过热 ' + str(cnt.get('over9', 0))
               + ' / &lt;5% 弱势 ' + str(cnt.get('under5', 0)) + '）。</div>')
    return '\n'.join(out)


def render_rotation(rot, ice=None):
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
    sfs = rot.get('subfamily_strength') or {}
    subs_all = sfs.get('subfamilies') or []
    # 大方向主线状态（读独立文件 direction_strength.json）
    dstate = {}
    try:
        _ds = rot.get('direction_strength') or load(DIRECTION_STRENGTH_JSON) or {}
        for _d in (_ds.get('directions') or []):
            dstate[_d.get('direction')] = _d.get('mainline_state') or '轮动'
    except Exception:
        pass

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

    trt = []
    for h in topping:
        trt.append('<tr><td>' + esc(h.get('theme')) + '</td>'
                   '<td class="r mono">' + str(h.get('up_streak') or 0) + '</td>'
                   '<td class="r mono ' + pct_cls(h.get('run_gain')) + '">' + pct_txt(h.get('run_gain')) + '</td>'
                   '<td class="r mono ' + pct_cls(h.get('dist_ma20')) + '">' + pct_txt(h.get('dist_ma20')) + '</td>'
                   '<td class="r mono">' + str(h.get('n_pool') or 0) + '</td></tr>')

    # 12 大方向状态（从细分族强度按成员数加权聚合，替代原"六个候选方向主线状态"）
    watch_rows = []
    if subs_all:
        ddir = {}
        for sb in subs_all:
            k = sb.get('direction') or '未归类'
            v = ddir.setdefault(k, {'n': 0, 'mem': 0, 'r5': 0.0, 'r20': 0.0, 'up': 0.0, 'lu': 0})
            m = sb.get('n_members') or 0
            v['n'] += 1
            v['mem'] += m
            v['r5'] += (sb.get('ret5') or 0) * m
            v['r20'] += (sb.get('ret20') or 0) * m
            v['up'] += (sb.get('up_share') or 0) * m
            v['lu'] += (sb.get('lu5') or 0)
        wrows = []
        for k, v in ddir.items():
            mem = v['mem'] if v['mem'] else 1
            wrows.append((k, v['n'], v['mem'], v['r5'] / mem, v['r20'] / mem,
                          v['up'] / mem, v['lu']))
        wrows.sort(key=lambda x: -x[3])
        for k, nf, mem, r5, r20, up, lu in wrows:
            st = dstate.get(k, '轮动')
            st_cls = 'tag b' if st in ('真主线', '主线萌芽') else ('tag gy' if st == '退潮' else '')
            st_html = ('<span class="' + st_cls + '">' + esc(st) + '</span>') if st_cls else esc(st)
            watch_rows.append('<tr><td>' + esc(k) + '</td>'
                              '<td>' + st_html + '</td>'
                              '<td class="r mono">' + str(nf) + '</td>'
                              '<td class="r mono">' + str(mem) + '</td>'
                              '<td class="r mono ' + pct_cls(r5) + '">' + pct_txt(r5) + '</td>'
                              '<td class="r mono ' + pct_cls(r20) + '">' + pct_txt(r20) + '</td>'
                              '<td class="r mono">' + ('%.0f%%' % up) + '</td>'
                              '<td class="r mono">' + str(lu) + '</td></tr>')

    out = []
    out.append('<h2>二、今日题材轮动</h2>')
    out.append('<div class="snap">' + ''.join(bx) + '</div>')
    if watch_rows:
        out.append('<div class="sub" style="margin:10px 0 4px"><b>12 大方向状态</b>'
                   '（细分族收益按成员数加权，5 日强弱排序）</div>')
        out.append('<table><tr><th>大方向</th><th>主线状态</th>'
                   '<th class="r">细分族</th><th class="r">成员</th>'
                   '<th class="r">5日</th><th class="r">20日</th>'
                   '<th class="r">上涨占比</th><th class="r">5日涨停</th></tr>'
                   + ''.join(watch_rows) + '</table>')
    _start_html = render_start59(ice, subs_all)
    if _start_html:
        out.append(_start_html)
    if trt:
        out.append('<div class="sub" style="margin:14px 0 4px">'
                   '<b>上涨趋势末端</b>（连涨 &ge;' + str(rot.get('top_streak') or 4)
                   + ' 天，' + esc(mstate) + '中容易买在高点）</div>')
        out.append('<table><tr><th>题材</th><th class="r">连涨</th>'
                   '<th class="r">本轮涨幅</th><th class="r">距MA20</th>'
                   '<th class="r">池内</th></tr>' + ''.join(trt) + '</table>')
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
    # 股票代码 -> 细分族名（单值映射；无归属的股票回退原 theme）
    sf_map = {}
    _sf = load(SUBFAMILY_MAP_JSON) or {}
    for _sb in (_sf.get('subfamilies') or []):
        for _c in (_sb.get('stocks') or []):
            sf_map[_c] = _sb.get('sub')
    rows = []
    for x in rec[:10]:
        rows.append('<tr><td class="mono">' + esc(x.get('code')) + '</td>'
                    '<td>' + esc(x.get('name')) + '</td>'
                    '<td>' + char_tag(x.get('code')) + '</td>'
                    '<td>' + ('<span class="tag gy">池内</span>'
                              if x.get('in_pool') else '<span class="tag w">池外</span>') + '</td>'
                    '<td>' + esc(sf_map.get(x.get('code')) or x.get('theme') or '') + '</td>'
                    '<td class="r mono ' + pct_cls(x.get('rs')) + '">' + pct_txt(x.get('rs')) + '</td>'
                    '<td class="r mono ' + pct_cls(x.get('mom20')) + '">' + pct_txt(x.get('mom20')) + '</td>'
                    '<td class="r mono">' + fnum(x.get('elast'), 0) + '</td>'
                    '<td>' + esc(x.get('quality')) + '</td></tr>')
    out = []
    out.append('<h2>三、全市场热题材强势股（仅供观察，不进 Top5）</h2>')
    out.append('<div class="sub" style="margin:16px 0 6px">'
               '<b>热题材内值得关注的全市场个股</b>（★推荐 %d 只，显示前 10；'
               '仅供观察，<b>不自动入池</b>）</div>' % len(rec))
    out.append('<table><tr><th>代码</th><th>名称</th><th>股性</th><th>在池</th><th>题材</th>'
               '<th class="r">相对强势</th><th class="r">20日%</th>'
               '<th class="r">弹性</th><th>质量</th></tr>' + ''.join(rows) + '</table>')
    out.append('<div class="note">全市场 %d 只 &rarr; 剔ST %d &rarr; %d 只；'
               '取「首涨/连涨%d~%d天」的热题材，按<b>题材内相对强势</b>每题材挑 %d 只。'
               '&#9733; = 弹性&ge;18 且非亏损。题材标签为机器派生，可能偏离主业，'
               '是否入池需人工核实。</div>'
               % (uni.get('base') or 0, uni.get('st_removed') or 0, uni.get('final') or 0,
                  us.get('stage_filter', {}).get('early_min', 1),
                  us.get('stage_filter', {}).get('early_max', 3), us.get('per_theme') or 5))
    out.append('<div class="note"><b>本区块与观察池无关：</b>'
               '池内自 2026-11-17 起不再排名，本区直接扫全市场热题材，'
               '不受观察池限制。<b>不参与任何排名、不自动入池</b>，只作为人工换池的候选清单。'
               '“池外”即当前不在观察池内的标的，才是值得考虑补进池子的。</div>')
    return '\n'.join(out)


def _burst_members(b, limit=2):
    """潜在启动题材的族内个股：取 top 字段涨幅前 limit 只（默认 2），否则退回族内最高那一只。"""
    tops = (b.get('top') or [])[:limit]
    if not tops:
        if b.get('max_code'):
            tops = [{'code': b.get('max_code'), 'name': b.get('max_name'),
                     'pct': b.get('max_pct')}]
        else:
            return '—'
    parts = []
    for x in tops:
        code = str(x.get('code') or '').split('.')[0]
        nm = esc(x.get('name') or '')
        pv = x.get('pct')
        try:
            pv = float(pv)
            ptxt = ('%+.2f%%' % pv)
        except Exception:
            ptxt = '—'
        cell = '<b>%s</b> <span class="mono" style="color:var(--muted)">%s</span> ' \
               '<span class="mono %s">%s</span>' % (nm, esc(code), pct_cls(pv), ptxt)
        parts.append(cell)
    return '<br>'.join(parts)


def render_icepoint(ice):
    """冰点骤升 + 券商核心标的（观察区，不改 Top5）。"""
    if not ice:
        return ''
    bursts = ice.get('bursts') or []
    core = ice.get('core_in_burst') or []
    if not bursts and not core:
        return ''
    out = []
    out.append('<h2>三之二、潜在启动题材（全档对照，仅供观察）</h2>')
    out.append('<div class="sub" style="margin:16px 0 6px">'
               '<b>当日「潜在启动」细分族 %d 个</b>（前日上涨占比&lt;20%% → 当日&gt;50%% 且均涨转正，'
               '细分族粒度、等权个股口径；'
               '<b>5&ndash;9%% 温和档是回测最优档</b>，已在第二部分单独列出）。'
               '普涨日该信号会满屏飘红，仅供观察，<b>不改 Top5、不参与打分</b>。</div>' % len(bursts))
    if bursts:
        tr = []
        for b in bursts[:20]:
            cls = pct_cls(b.get('cur_mean_ret'))
            txt = pct_txt(b.get('cur_mean_ret'))
            tr.append('<tr><td>' + esc(b.get('sub')) + '</td>'
                      '<td>' + esc(b.get('direction') or '') + '</td>'
                      '<td class="r mono">' + str(b.get('n_members')) + '</td>'
                      '<td class="r mono">' + ('%.0f%%' % (b.get('prev_up_share', 0) * 100)) + '</td>'
                      '<td class="r mono">' + ('%.0f%%' % (b.get('cur_up_share', 0) * 100)) + '</td>'
                      '<td class="r mono ' + cls + '">' + txt + '</td>'
                      '<td class="r mono ' + pct_cls(b.get('max_pct')) + '">'
                      + pct_txt(b.get('max_pct')) + '</td>'
                      '<td>' + _burst_members(b) + '</td></tr>')
        out.append('<table class="cmp"><tr><th>细分族</th><th>大方向</th><th class="r">成员</th>'
                   '<th class="r">前日占比</th><th class="r">当日占比</th>'
                   '<th class="r">当日均涨</th><th class="r">族内最高</th>'
                   '<th>族内可选个股（涨幅前 2）</th></tr>' + ''.join(tr) + '</table>')
        if len(bursts) > 20:
            out.append('<div class="note">仅显示涨幅前 20 个，共 %d 个。</div>' % len(bursts))
    if core:
        # 按细分族聚合：领涨率 = 族内领涨核心数 ÷ 族内核心标的总数（2026-11-23 起改为族级口径）
        fams = {}
        for c in core:
            k = c.get('sub') or c.get('family') or '-'
            d = fams.get(k)
            if d is None:
                d = fams[k] = {'sub': k, 'dir': c.get('direction') or '',
                               'n': 0, 'led': 0, 'ln': [], 'lg': []}
            d['n'] += 1
            if c.get('leading'):
                d['led'] += 1
                d['ln'].append((c.get('name'), c.get('pct')))
            else:
                d['lg'].append((c.get('name'), c.get('pct')))
        rows_f = list(fams.values())
        for d in rows_f:
            d['rate'] = d['led'] * 100.0 / d['n'] if d['n'] else 0.0
        bmean = {b.get('sub'): b.get('cur_mean_ret') for b in bursts}
        rows_f.sort(key=lambda d: (-d['rate'], -d['n'],
                                   -sum((p or 0) for _n, p in d['ln']), d['sub']))
        top_f = rows_f[:10]
        tot_n = sum(d['n'] for d in rows_f)
        tot_led = sum(d['led'] for d in rows_f)
        n_thin = sum(1 for d in top_f if d['n'] <= 2)

        def _nm(lst, color):
            if not lst:
                return '<span style="color:#b6bcc6">—</span>'
            return '<span style="color:%s">%s</span>' % (
                color, '&ensp;'.join(
                    esc(n) + ('(%+.1f)' % p if p is not None else '') for n, p in lst))

        def _bar(rate):
            if rate >= 50:
                col = '#d32029'
            elif rate >= 25:
                col = '#d97706'
            elif rate > 0:
                col = '#eab308'
            else:
                col = '#d1d5db'
            return ('<span style="display:inline-block;width:46px;height:6px;background:#eef1f5;'
                    'border-radius:3px;overflow:hidden;vertical-align:middle">'
                    '<i style="display:block;height:100%%;width:%.1f%%;background:%s"></i></span>'
                    % (max(rate, 1.5), col))

        out.append('<div class="sub" style="margin:16px 0 6px">'
                   '<b>潜在启动细分族 × 券商核心标的：领涨率前 10</b>'
                   '（核心标的共 %d 只、分布在 %d 个触发族；'
                   '<b>领涨率</b> = 族内当日跑赢族均值的核心数 ÷ 族内核心标的总数，'
                   '用于验证板块是否真启动，不是买入推荐）</div>'
                   % (len(core), len(rows_f)))
        tr = []
        for i, d in enumerate(top_f, 1):
            fm = bmean.get(d['sub'])
            tr.append('<tr><td class="mono r" style="color:#b6bcc6">' + str(i) + '</td>'
                      '<td><b>' + esc(d['sub']) + '</b></td>'
                      '<td class="sub">' + esc(d['dir']) + '</td>'
                      '<td class="r mono">' + str(d['n']) + '</td>'
                      '<td class="r mono">' + str(d['led']) + '</td>'
                      '<td class="r mono">' + _bar(d['rate'])
                      + ' <b>%.0f%%</b></td>' % d['rate']
                      + '<td class="r mono ' + pct_cls(fm) + '">' + pct_txt(fm, 1) + '</td>'
                      '<td style="font-size:12px;line-height:1.7">'
                      + _nm(d['ln'], '#b91c1c') + '<br>' + _nm(d['lg'], '#8b929e') + '</td></tr>')
        out.append('<table><tr><th class="r">#</th><th>细分族</th><th>大方向</th>'
                   '<th class="r">核心数</th><th class="r">领涨</th><th class="r">领涨率</th>'
                   '<th class="r">族均涨</th><th>领涨（红）/ 未领涨（灰）・括号内为当日涨幅</th></tr>'
                   + ''.join(tr) + '</table>')
        out.append('<div class="note">全部 %d 族合计 %d/%d = %.1f%%。'
                   '前 10 名里有 %d 个族分母只有 1&ndash;2 只，比例天然只有 0%%/50%%/100%%，'
                   '信息量低；领涨只是「跑赢族均值」的相对概念，不代表绝对收益。</div>'
                   % (len(rows_f), tot_led, tot_n, tot_led * 100.0 / tot_n if tot_n else 0, n_thin))
    return '\n'.join(out)




def render_market_picks(picks_market):
    """全市场腿 Top5（2026-11-01 新选股：信号日 MA5向上+收红，RSI跳升排序）。"""
    if not picks_market:
        return ''
    tr = []
    for i, r in enumerate(picks_market, 1):
        trend = 'RSI6跳升+%s · 量比%s%s' % (
            fnum(r.get('rsi_jump')), fnum(r.get('vr'), 2),
            ' · MA5↑收红' if (r.get('ma5_up') and r.get('red')) else '')
        tr.append('<tr><td class="mono">%d</td><td>%s</td><td class="mono">%s</td>'
                  '<td>%s</td><td class="r mono">%.0f</td>'
                  '<td>%s</td><td class="r mono %s">%s</td>'
                  '<td class="r mono">%s</td><td class="r mono">%s</td></tr>'
                  % (i, esc(r.get('name')), esc(r.get('code')),
                     esc(subfamily_of(r.get('code')) or r.get('rot_theme') or r.get('industry') or '-'),
                     r.get('pos60') or 0, trend,
                     pct_cls(r.get('pct_chg')), pct_txt(r.get('pct_chg')),
                     fnum(r.get('rsi6'), 1),
                     fnum(r.get('pos20'), 0)))
    note = ('<h2>四之二、全市场 Top5（新选股）</h2>'
            '<div class="note">全市场腿使用<b>新选股逻辑</b>：信号日「MA5向上 + 收红」确认，'
            '候选=RSI6跳升15~25 + RSI6&lt;70 + 涨幅2%~涨停 + 量比≥1.5 + 收盘&gt;MA5 + 非跳空'
            ' + pos60&lt;60 + pos20&lt;80，新池=市值≥30亿/剔ST/剔主板120日无涨停，'
            '按 RSI 跳升越大越优先。回测超额 +2.35%、胜率 53.8%、抽稀 t 6.53。</div>'
            '<table><tr><th>#</th><th>名称</th><th>代码</th><th>题材</th>'
            '<th class="r">位置60</th><th>信号</th><th class="r">涨幅</th>'
            '<th class="r">RSI6</th><th class="r">位置20</th></tr>')
    return note + ''.join(tr) + '</table>'




def _ts_code(c):
    """6 位代码补交易所后缀；已带后缀原样返回。"""
    c = str(c or '').strip()
    if not c:
        return ''
    if '.' in c:
        return c
    if c.startswith('6'):
        return c + '.SH'
    if c.startswith(('4', '8')):
        return c + '.BJ'
    return c + '.SZ'


def load_pool():
    """观察池 -> {ts_code: name}。读不到返回空 dict（页面降级为无池区块）。"""
    if not os.path.exists(POOL_CSV):
        return {}
    out = {}
    try:
        with open(POOL_CSV, encoding='utf-8-sig') as f:
            for r in csv.DictReader(f):
                c = _ts_code(r.get('ts_code'))
                if c:
                    out[c] = (r.get('name') or '').strip()
    except Exception:
        return {}
    return out


def render_pool_hits(market, pool):
    """池内策略命中：只列观察池中被 KHunter / 超跌反弹 v2 命中的个股。"""
    kh = market.get('khunter') or {}
    ov = market.get('oversold_v2') or {}
    if not pool:
        # 池文件读不到（换机渲染 / 路径缺失）时不渲染空壳，避免误导为"池内零命中"
        return ''
    kh_rows = [r for r in (kh.get('rows') or [])
               if _ts_code(r.get('ts_code') or r.get('code')) in pool]
    # 「池内命中」看的是当日是否命中，不受 v2 去重影响 → 用 rows_all（gate 后全量）
    ov_src = ov.get('rows_all') or ov.get('rows') or []
    ov_rows = [r for r in ov_src
               if _ts_code(r.get('ts_code') or r.get('code')) in pool]
    head = ('<h2>四、池内策略命中（KHunter / 超跌反弹）</h2>'
            '<div class="sub" style="margin:10px 0 8px">'
            '<b>观察池自 2026-11-17 起不再做池内排名</b>，池内个股由本人自行观察；'
            '本区块只做一件事——把观察池里<b>被策略命中的个股单独列出来</b>。'
            '观察池共 <b>%d</b> 只，其中命中 KHunter <b>%d</b> 只、'
            '超跌反弹 v2 <b>%d</b> 只。命中只代表当日形态符合该策略条件，'
            '<b>不构成推荐、不参与打分</b>。</div>' % (len(pool), len(kh_rows), len(ov_rows)))
    out = [head]
    if not kh_rows and not ov_rows:
        out.append('<div class="note">今日观察池内无 KHunter / 超跌反弹命中。</div>')
        return '\n'.join(out)
    if kh_rows:
        kh_rows = sorted(kh_rows, key=lambda r: -float(r.get('chg') or 0))
        tr = []
        for r in kh_rows:
            src = r.get('theme_src') or ''
            label = (r.get('theme') or '—') + ('（行业）' if src in ('行业兜底', '生效行业') else '')
            tr.append('<tr><td class="mono">%s</td><td>%s</td><td>%s</td><td>%s</td><td>%s</td>'
                      '<td>%s</td><td class="r mono">%s</td>'
                      '<td class="r mono %s">%s</td><td class="r mono">%s</td><td>%s</td></tr>'
                      % (esc(_ts_code(r.get('ts_code') or r.get('code'))[:6]),
                         esc(r.get('name')), char_tag(r.get('ts_code') or r.get('code')),
                         esc(r.get('strategy')), esc(r.get('status') or ''),
                         esc(label), esc(r.get('close')),
                         pct_cls(r.get('chg')), pct_txt(r.get('chg')),
                         esc(r.get('mv')), esc(r.get('reason') or '')))
        out.append('<div class="sub" style="margin:14px 0 6px"><b>KHunter 命中（%d 只）</b>'
                   '（按当日涨幅降序；状态＝该策略在 11-11 人工裁决中的档位）</div>' % len(kh_rows))
        out.append('<table><tr><th>代码</th><th>名称</th><th>股性</th><th>策略</th><th>状态</th><th>题材</th>'
                   '<th class="r">收盘</th><th class="r">当日%%</th><th class="r">市值亿</th>'
                   '<th>命中依据</th></tr>%s</table>' % ''.join(tr))
    else:
        out.append('<div class="note">今日观察池内无 KHunter 命中。</div>')
    if ov_rows:
        tr = []
        for r in ov_rows:
            src = r.get('theme_src') or ''
            label = (r.get('theme') or '—') + ('（行业）' if src in ('行业兜底', '生效行业') else '')
            tr.append('<tr><td class="mono">%s</td><td>%s</td><td>%s</td><td>%s</td>'
                      '<td class="r mono">%s</td><td class="r mono %s">%s</td>'
                      '<td class="r mono">%s</td></tr>'
                      % (esc(_ts_code(r.get('ts_code') or r.get('code'))[:6]),
                         esc(r.get('name')), char_tag(r.get('ts_code') or r.get('code')),
                         esc(label), esc(r.get('close')),
                         pct_cls(r.get('chg')), pct_txt(r.get('chg')), esc(r.get('mv'))))
        out.append('<div class="sub" style="margin:14px 0 6px"><b>超跌反弹 v2 命中（%d 只）</b>'
                   '（C1 腰斩 + C3 反弹上限 + KDJ 存在型底背离，'
                   '再取 AMP20 / VOL20 / 成交额分位 各最低 50%% 的交集）</div>' % len(ov_rows))
        out.append('<table><tr><th>代码</th><th>名称</th><th>股性</th><th>题材</th><th class="r">收盘</th>'
                   '<th class="r">当日%%</th><th class="r">市值亿</th></tr>%s</table>' % ''.join(tr))
    if not ov_rows:
        out.append('<div class="note">今日观察池内无超跌反弹 v2 命中。</div>')
    both = ({_ts_code(r.get('ts_code') or r.get('code')) for r in kh_rows}
            & {_ts_code(r.get('ts_code') or r.get('code')) for r in ov_rows})
    if both:
        out.append('<div class="note"><b>两套策略同时命中：</b>%s</div>'
                   % '、'.join(sorted(c[:6] for c in both)))
    return '\n'.join(out)


def render_khunter(kh):
    """KHunter 7 策略选股（观察区块）。kh 来自 market.json 的 khunter 字段。"""
    if not kh or not kh.get('rows'):
        return ''
    rows = kh.get('rows') or []
    _KH_STRATEGY_ORDER = {s: i for i, s in enumerate((
        '主升低吸策略', '多金叉共振', '次新腰斩策略', '涨停回马枪策略',
        '阻力位突破策略', '启明星策略', '超跌反弹'))}
    rows = sorted(rows, key=lambda r: _KH_STRATEGY_ORDER.get(r.get('strategy'), 99))
    tr = []
    for r in rows:
        theme = r.get('theme') or '—'
        src = r.get('theme_src') or '无'
        label = theme + '（行业）' if src in ('行业兜底', '生效行业') else theme
        td = '<td title="题材来源：%s">%s</td>' % (esc(src), esc(label))
        tr.append('<tr><td class="mono">%s</td><td>%s</td><td>%s</td>%s<td>%s</td>'
                  '<td class="r mono">%s</td><td class="r mono">%+.2f</td><td class="r mono">%s</td></tr>'
                  % (esc(r.get('code')), esc(r.get('name')), char_tag(r.get('code')), td,
                     esc(r.get('strategy')), esc(r.get('close')),
                     float(r.get('chg') or 0), esc(r.get('mv'))))
    tr = ''.join(tr)
    dd = kh.get('dedup') or {}
    note = ('<div class="note">7 个未放弃策略（保留 2 / 观察 3 / 优化 2），'
            '排除 2560战法、仙人指路、多方炮、金三角、龙头。'
            '<b>多金叉共振仅首次命中展示</b>：同一票若上一交易日已命中则今日不展示'
            '（本日隐藏 %s 条）。仅作形态观察，<b>不进 Top5、不参与打分、不构成推荐</b>；'
            '已剔 ST/*ST、退市、北交所、当日停牌、上市不足 60 根 K 线。'
            '<b>题材列</b>＝个股主题材，优先人工复核 / 东财概念，缺失时用细分族、'
            '行业兜底（标「（行业）」）；悬停可看来源，仅展示不参与筛选。'
            '<b>排序</b>：按策略固定顺序（保留→观察→优化）分组，组内保持原始顺序。</div>'
            % esc(dd.get('hidden', 0)))
    return ('<h2>五、KHunter 全市场选股（观察，%d 只）</h2>' % len(rows) + note +
            '<table><tr><th>代码</th><th>名称</th><th>股性</th><th>题材</th><th>策略</th>'
            '<th class="r">收盘</th><th class="r">当日涨跌%%</th><th class="r">市值亿</th></tr>'
            '%s</table>' % tr)


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


def render_oversold_v2(ov):
    """新版超跌反弹 v2（看板最下方主要展示策略）。ov 来自 market.json 的 oversold_v2 字段。"""
    if not ov or not ov.get('rows'):
        return ''
    rows = ov.get('rows') or []
    tr = []
    for r in rows:
        theme = r.get('theme') or '—'
        src = r.get('theme_src') or '无'
        label = theme + '（行业）' if src in ('行业兜底', '生效行业') else theme
        td = '<td title="题材来源：%s">%s</td>' % (esc(src), esc(label))
        tr.append('<tr><td class="mono">%s</td><td>%s</td><td>%s</td>%s'
                  '<td class="r mono">%s</td><td class="r mono">%+.2f</td>'
                  '<td class="r mono">%s</td></tr>'
                  % (esc(r.get('code')), esc(r.get('name')), char_tag(r.get('code')), td,
                     esc(r.get('close')), float(r.get('chg') or 0), esc(r.get('mv'))))
    note = ('<div class="note"><b>规则</b>（全部只用 T 日及之前数据，无前视）：'
            '① <b>C1 腰斩</b>＝收盘价 ≤ 近 120 日最高价 × 50%；'
            '② <b>C3 反弹上限</b>＝较近 100 日最低价反弹 ≤ 30%；'
            '③ <b>KDJ 存在型底背离</b>＝近 5 日内出现过「当日最低价创新低、但 J 值高于此前的某一个低点日」'
            '（J＝3K−2D，K/D 为 KDJ(9,3,3) 的 1/3 指数平滑）；'
            '④ <b>三重 gate</b>＝在当日全部 B 信号票内，'
            '<b>AMP20</b>（过去 20 日平均振幅，即 (最高−最低)/昨收 的 20 日均值）、'
            '<b>VOL20</b>（过去 20 日日收益率的标准差）、'
            '<b>当日成交额分位</b>（当日成交额在全市场横截面的百分位，数值越小越冷门）'
            '三项各取最低的 50%，取交集。<br>'
            '表格按成交额分位升序；已剔 ST/*ST、退市、北交所、当日停牌、上市不足 120 根 K 线。<br>'
            '本策略已通过项目完整七关检验（用户口径：交易成本不计入），作为看板主要展示策略；'
            '<b>不进 Top5、不参与打分、不构成推荐</b>。</div>')
    dd = ov.get('dedup') or {}
    hid = dd.get('hidden') or 0
    if dd:
        win = dd.get('window') or 1
        title = ('六、新版超跌反弹 v2（全市场，主要展示策略，今日新增 %d 只 · %s）'
                 % (len(rows), esc(ov.get('trade_date') or '')))
        note = note.replace('</div>', '⑥ <b>去重</b>＝近 <b>%d 个交易日</b>内命中过该策略的票，'
                            '今天不再展示（<b>一个窗口内只出现第一次</b>，走出窗口后再触发才算新首次）；'
                            '今日另有 <b>%d 只</b>已隐藏，gate 后共 %d 只。</div>'
                            % (win, hid, dd.get('before') or len(rows)))
    else:
        title = ('六、新版超跌反弹 v2（全市场，主要展示策略，%d 只 · %s）'
                 % (len(rows), esc(ov.get('trade_date') or '')))
    return ('<h2>%s</h2>' % title + note +
            '<table><tr><th>代码</th><th>名称</th><th>股性</th><th>题材</th>'
            '<th class="r">收盘</th><th class="r">当日涨跌%%</th><th class="r">市值亿</th></tr>'
            '%s</table>' % ''.join(tr))




def render_foot(narr):
    return ('<div class="disc">%s<br><br>本页由数据管线自动生成，'
            '仅供本人交易参考，不构成投资建议。'
            '<b>观察池自 2026-11-17 起不再做池内排名</b>：池内个股只做两件事——'
            '标出 KHunter / 超跌反弹是否命中，以及列示潜在启动题材的族内可选个股。'
            '成交额不参与任何打分、不影响任何排序。逻辑纯度仅作展示、不计分。<br>'
            '<b>位置60</b>（60日价格位置）=（今收 − 60日最低）÷（60日最高 − 60日最低）×100：'
            '100 = 创 60 日新高，0 = 60 日最低。它衡量股价在近 60 日箱体里的相对位置，'
            '不是「最近涨了多少」。<br>'
            '<b>股性标签</b>（族内 500 日）：<b>优</b>=跟涨不跟跌、<b>中</b>=跟涨也跟跌、'
            '<b>差</b>=跟跌不跟涨、<b>钝</b>=都不跟（族涨 1%% 个股涨幅不足 0.8%% 视为不跟）；'
            '族相关性 &lt;0.40 标「不评」。<b>纯标签：不参与打分、不影响排序、不构成推荐</b>；'
            '其中「涨多还是跌多」这一维前后半窗口一致率仅 13~17%%（约等于随机），'
            '只作当期状态参考，不作长期特性。<br>'
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

    global _SF_MAP_ROT
    _SF_MAP_ROT = (market.get('rotation') or {}).get('subfamily_map')
    uscan = ((market.get('rotation') or {}).get('universe_scan')
             or load(UNIVERSE_SCAN_JSON))
    ice = (market.get('rotation') or {}).get('icepoint_scan') or load(ICEPOINT_SCAN_JSON)
    # 潜在启动扫描的 config 版本比 market.json 内嵌版更新（含族内个股 top 字段），
    # 交易日一致时优先用 config，避免渲染出缺列的旧结构。
    _ice_fresh = load(ICEPOINT_SCAN_JSON)
    if _ice_fresh and str(_ice_fresh.get('trade_date') or '') == str(td):
        ice = _ice_fresh
    sfs = (market.get('rotation') or {}).get('subfamily_strength') or load(SUBFAMILY_STRENGTH_JSON)
    ev = load(os.path.join(data_dir, 'event_driven.json'), {})
    body = [render_head(td, market, gen), render_alert(narr), render_snap(market.get('env') or {}),
            render_rotation(market.get('rotation') or {}, ice or {}),
            render_events(ev or {}),
            render_universe_scan(uscan or {}),
            render_icepoint(ice or {}),
            render_pool_hits(market, load_pool()),
            '<h2>四、今日五个标的（按确定性排名）</h2>',
            '<div class="sub" style="margin-bottom:12px">排名即优先级。'
            '<b>只买 1 只 → 取第 1 名；买 2 只 → 取第 1 + 2 名。</b><br>'
            '<b>排序键 = 综合评分 + 题材热度加分</b>（热题材在池内凑不够 3 只时启用，满分 30，'
            '按所选题材热度分位给分）。因此<b>综合评分高但题材偏冷的票会排到后面</b>，'
            '两者不一致时以排序分为准。<br>'
            '评分框架（2026-11-04 P2.1）：<b>资金流 30</b> + 基本面 10 + <b>5日动量 25</b> + '
            '<b>技术 20</b> + 市值适配 10 + <b>细分族强度 15</b> = 110（不含流动性维度）。<br>'
            '<b>资金流</b> = 近 5 日主力（大单+特大单）净流入 ÷ 总市值，在候选集内取分位；'
            '<b>市值适配</b> = 总市值在候选集内的分位取倒数（小市值高分）；'
            '<b>位置60</b> = 近 60 日高低区间中的相对位置，<b>已退出计分、仅作展示</b>。</div>']
    if not SHOW_POOL_TOP5:
        # 列表末尾两项即「四、今日五个标的」标题与评分框架说明
        assert body[-2].startswith('<h2>四、今日五个标的') and body[-1].startswith('<div class="sub"'), \
            'body 结构变化，需同步更新隐藏逻辑'
        del body[-2:]
    if SHOW_POOL_TOP5:
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
    if SHOW_MARKET_TOP5:
        body.append(render_market_picks(picks_market))
    body.append(render_khunter(market.get('khunter')))
    body.append(render_oversold_v2(market.get('oversold_v2')))
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
