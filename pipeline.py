# -*- coding: utf-8 -*-
"""A股每日选股数据管线（线上版，零第三方依赖，python3 直接跑）。

流程：交易日校验 -> 同步上游数据仓 -> 等 manifest 就绪 -> 行情/财务复核
      -> 扣非利润质量核查 -> v3 四维打分 -> Top5 -> LLM 点评 -> 存档

退出码：0=成功或休市跳过，1=失败，2=数据未就绪（上游未推送）
"""
import os
import sys
import json
import time
import shutil
import urllib.request
import urllib.error
from datetime import datetime, timedelta, timezone

CST = timezone(timedelta(hours=8))
UA = {'User-Agent': 'Mozilla/5.0 (Windows NT 10.0; Win64; x64)'}
BASE = os.path.dirname(os.path.abspath(__file__))

# ---------- 配置 ----------
DEFAULT_CFG = {
    "repo_url": "",                      # 上游私有仓 https 地址；ECS 上若已有本地源数据则留空
    "repo_dir": "repo",
    # ECS 本地源数据（推荐）：直接读上游输出的这两个文件，完全不联网
    "local_bundle_path": "",
    "local_manifest_path": "",
    "out_dir": "out",
    # 成交额下限（亿元）。用户 2026-10-04 决定：不设门槛，默认 0
    "min_amount_yi": 0,
    # 同一产业链在 Top5 里最多几只（用户 2026-10-08 决定：2）
    "cluster_cap": 2,
    # 结果推送到 GitHub（云端自动化再从这里取数生成看板）
    "push": {
        "enabled": False,
        "repo_url": "",                  # 例：https://<token>@github.com/zippobb/ashare-pick5.git
        "repo_dir": "results_repo",
        "branch": "main",
        "subdir": "daily"                # 存到 <repo>/<subdir>/<YYYYMMDD>/
    },
    "bundle_rel": "latest/key10_bundle_latest.json",
    "manifest_rel": "latest/manifest.json",
    "wait_manifest_minutes": 90,         # 上游未推送时的最大等待时长
    "wait_interval_seconds": 300,
    "llm": {
        "enabled": True,
        "provider": "deepseek",
        "base_url": "https://api.deepseek.com/v1",
        "model": "deepseek-chat",
        "api_key_env": "DEEPSEEK_API_KEY",
        "timeout": 180,
        "max_tokens": 2500
    }
}


def load_cfg():
    p = os.path.join(BASE, 'config.json')
    cfg = dict(DEFAULT_CFG)
    if os.path.exists(p):
        cfg.update(json.load(open(p, encoding='utf-8')))
        if 'llm' in cfg and isinstance(cfg['llm'], dict):
            llm = dict(DEFAULT_CFG['llm'])
            llm.update({k: v for k, v in cfg['llm'].items() if v is not None})
            cfg['llm'] = llm
    return cfg


def log(msg):
    print('[%s] %s' % (datetime.now(CST).strftime('%Y-%m-%d %H:%M:%S'), msg), flush=True)


def abspath(p):
    return p if os.path.isabs(p) else os.path.join(BASE, p)


def http_get(url, headers=None, decode='utf-8', retries=3, timeout=25):
    h = dict(UA)
    h.update(headers or {})
    for i in range(retries):
        try:
            req = urllib.request.Request(url, headers=h)
            return urllib.request.urlopen(req, timeout=timeout).read().decode(decode, 'ignore')
        except Exception as e:
            if i == retries - 1:
                log('HTTP FAIL %s -> %s' % (url[:100], e))
                return None
            time.sleep(1.5 * (i + 1))


# ---------- 0) 交易日 ----------
def load_calendar():
    p = abspath('market_calendar.json')
    cal = json.load(open(p, encoding='utf-8'))
    return set(cal.get('closed_days', []))


def is_trading_day(d, closed):
    if d.weekday() >= 5:
        return False, '周末'
    ds = d.strftime('%Y-%m-%d')
    if ds in closed:
        return False, '休市日(%s)' % ds
    return True, ''


# ---------- 1) 同步上游 ----------
def sync_repo(cfg):
    repo = abspath(cfg['repo_dir'])
    url = cfg.get('repo_url') or ''
    if os.path.isdir(os.path.join(repo, '.git')):
        rc = os.system("cd '%s' && git pull --quiet --ff-only" % repo)
        if rc != 0:
            log('git pull 失败(rc=%d)，尝试 fetch+reset' % rc)
            os.system("cd '%s' && git fetch --quiet && git reset --quiet --hard FETCH_HEAD" % repo)
    elif url:
        os.system("git clone --depth 1 '%s' '%s'" % (url, repo))
    else:
        raise SystemExit('repo_dir 不存在且未配置 repo_url')
    return repo


def read_manifest(repo, cfg):
    """优先读 manifest；ECS 上通常没有 manifest 文件，则回退到 bundle 自带的 trade_date。"""
    for p in (cfg.get('local_manifest_path') or '', os.path.join(repo, cfg['manifest_rel'])):
        if p and os.path.exists(p):
            return json.load(open(p, encoding='utf-8'))
    b = read_bundle(repo, cfg)
    if b and b.get('trade_date'):
        return {'trade_date': b['trade_date'], '_source': 'bundle'}
    return None


def read_bundle(repo, cfg):
    for p in (cfg.get('local_bundle_path') or '', os.path.join(repo, cfg['bundle_rel'])):
        if p and os.path.exists(p):
            return json.load(open(p, encoding='utf-8'))
    return None


# ---------- 2) 复核：行情 + 上市日期 + 财务 + 扣非 ----------
def build_candidates(bundle):
    out = []
    for g in ['official_review_candidates', 'external_review_candidates']:
        for c in bundle.get(g) or []:
            seg = c.get('segment')
            if seg in ('北交所', '北证A股', '北证'):
                continue
            code = c['ts_code']
            bare, mkt = code.split('.')
            out.append((bare, code, c.get('name'), 'sh' if mkt == 'SH' else 'sz', mkt, seg, c))
    return out


def fetch_quotes(cands):
    tx = {}
    codes = [p + c for c, _, _, p, _, _, _ in cands]
    for i in range(0, len(codes), 8):
        txt = http_get('https://qt.gtimg.cn/q=' + ','.join(codes[i:i + 8]), decode='gbk')
        if not txt:
            continue
        for line in txt.strip().split('\n'):
            if '="' not in line:
                continue
            key = line.split('=')[0].replace('v_', '').strip()
            f = line.split('="')[1].rstrip('";').split('~')
            if len(f) < 45:
                continue
            name = f[1]
            tx[key[2:]] = {
                'name_tx': name,
                'close': float(f[3] or 0),
                'prev_close': float(f[4] or 0),
                'open': float(f[5] or 0),
                'amount_yi': round(float(f[37]) / 1e4, 3) if f[37] else None,
                'time': f[30],
                'chg_pct': float(f[32] or 0),
                'high': float(f[33] or 0),
                'low': float(f[34] or 0),
                'turnover_pct': float(f[38] or 0) if f[38] else None,
                'pe_tx': f[39],
                'amplitude': f[43],
                'floatcap_yi': float(f[44] or 0) if f[44] else None,
                'totalcap_yi': float(f[45] or 0) if f[45] else None,
                'pb': f[46],
                'is_st': ('ST' in name),
            }
        time.sleep(0.4)
    return tx


EM = 'https://datacenter-web.eastmoney.com/api/data/v1/get'


def fetch_listing(cands):
    listing = {}
    for bare, code, name, pfx, mkt, seg, _ in cands:
        url = (EM + '?reportName=RPT_F10_BASIC_ORGINFO'
               '&columns=SECUCODE,SECURITY_CODE,SECURITY_NAME_ABBR,LISTING_DATE,EM2016'
               '&filter=(SECUCODE%%3D%%22%s%%22)&pageSize=1&source=HSF10&client=PC' % (bare + '.' + mkt))
        t = http_get(url, headers={'Referer': 'https://emweb.securities.eastmoney.com/'})
        if not t:
            continue
        try:
            rows = json.loads(t)['result']['data']
            if rows:
                listing[bare] = {'LISTING_DATE': (rows[0].get('LISTING_DATE') or '')[:10],
                                 'industry': rows[0].get('EM2016')}
        except Exception:
            pass
    return listing


def fetch_fin(cands):
    fin = {}
    for bare, code, name, pfx, mkt, seg, _ in cands:
        url = (EM + '?reportName=RPT_LICO_FN_CPD'
               '&columns=SECUCODE,REPORTDATE,TOTAL_OPERATE_INCOME,PARENT_NETPROFIT,'
               'WEIGHTAVG_ROE,YSTZ,BASIC_EPS,XSMLL&filter=(SECUCODE%%3D%%22%s%%22)'
               '&sortColumns=REPORTDATE&sortTypes=-1&pageSize=1&source=WEB&client=WEB' % (bare + '.' + mkt))
        t = http_get(url)
        if not t:
            continue
        try:
            rows = json.loads(t).get('result', {}).get('data') or []
            if rows:
                r = rows[0]
                fin[bare] = {
                    'report': (r.get('REPORTDATE') or '')[:10],
                    'revenue_yi': round((r.get('TOTAL_OPERATE_INCOME') or 0) / 1e8, 2),
                    'netprofit_yi': round((r.get('PARENT_NETPROFIT') or 0) / 1e8, 2),
                    'roe': r.get('WEIGHTAVG_ROE'),
                    'eps': r.get('BASIC_EPS'),
                    'gross_margin': r.get('XSMLL'),
                }
        except Exception:
            pass
    return fin


KLINE_API = 'https://web.ifzq.gtimg.cn/appstock/app/fqkline/get'


def fetch_kline_metrics(cands, trade_date, need=260):
    """拉前复权日线，算拥挤度三指标：250日涨幅 / 120日涨幅 / 距年内高点回撤。

    trade_date 形如 20261008；返回值 {bare: {r250, r120, ddh, bars}}，失败则缺项（降级为不扣分）。
    """
    upto = '%s-%s-%s' % (trade_date[:4], trade_date[4:6], trade_date[6:8])
    out = {}
    for bare, code, name, pfx, mkt, seg, _ in cands:
        sym = pfx + bare
        u = '%s?param=%s,day,,,%d,qfq' % (KLINE_API, sym, need)
        txt = http_get(u, retries=3, timeout=30)
        if not txt:
            continue
        try:
            d = json.loads(txt)['data'][sym]
            rows = d.get('qfqday') or d.get('day') or []
        except Exception:
            continue
        rows = [r for r in rows if r[0] <= upto]
        if len(rows) < 30:
            continue
        cl = [float(r[2]) for r in rows]
        hi = [float(r[3]) for r in rows]
        c = cl[-1]
        out[bare] = {
            'r250': round((c / cl[0] - 1) * 100, 1) if len(cl) > 2 else None,
            'r120': round((c / cl[-121] - 1) * 100, 1) if len(cl) > 121 else None,
            'ddh': round((1 - c / max(hi[-250:])) * 100, 1),
            'bars': len(rows),
        }
        time.sleep(0.25)
    return out


# ---------- 3.1) 拥挤度扣分（2026-10-08 实盘复盘后新增） ----------
# 教训：9/30 的 Top1 仕佳光子 10/8 单日 -17.4%，而它当时 dma=0.04（贴均线）被判"位置最安全"。
# 根因：dma 的单位是 ATR，跨股票不可比；ATR 6% 的票"贴均线"蕴含的绝对下行远大于 ATR 2% 的票。
# 且当年涨幅巨大、筹码高度集中时，赛道拥挤度本身就是风险源。
CROWD_RULES = [
    # (年涨幅下限, ATR%下限, dma上限(可空), 扣分, 标签)
    (200.0, 4.5, None, 12, '高位高波动'),
    (100.0, 5.0, None, 8, '高位高波动'),
    (60.0, 4.5, 0.5, 5, '贴均线筹码集中'),
    (60.0, 3.5, 0.3, 3, '贴均线筹码集中'),
]


def crowd_penalty(r250, atr_pct, dma):
    """返回 (扣分, 标签)。数据缺失时返回 (0, '数据不足')。"""
    if r250 is None or not atr_pct:
        return 0, '数据不足'
    best, tag = 0, ''
    for r_lim, a_lim, d_lim, pen, t in CROWD_RULES:
        if r250 >= r_lim and atr_pct >= a_lim and (d_lim is None or dma <= d_lim):
            if pen > best:
                best, tag = pen, t
    return best, (tag or '正常')


# ---------- 3.2) 产业链集中度上限 ----------
# 教训：9/30 的 Top5 里 4 只在同一条 AI 算力 β 上，等同单票重仓，今日同跌。
# 注意：顺序敏感，'光芯片'含'芯片'，必须排在'半导体'之前
CLUSTERS = [
    ('光通信', ['光芯片', '光模块', 'CPO', '光通信', '光缆', '激光器', '光器件', '光纤', '光互连']),
    ('PCB覆铜板', ['覆铜板', 'PCB', '铜箔', '载板', '线路板']),
    ('算力IDC', ['算力', 'IDC', '数据中心', '服务器', '液冷', '交换机', '电源模块',
                 '铜连接', '高速铜缆']),
    ('半导体', ['存储', '芯片', '半导体', '晶圆', '封测', '光刻', 'MCU', '模拟芯片',
                '功率半导体', 'EDA', '硅片', '电子特气']),
    ('医药', ['创新药', 'CXO', '原料药', '生物制药', '医药', '医疗器械', '疫苗', '中药',
              '诊断', '重组蛋白', '合成生物', '精准医疗']),
    ('新能源', ['锂电', '固态电池', '光伏', '储能', '风电', '电解液', '电池', '隔膜', '正极', '负极']),
    ('汽车', ['汽车', '智能驾驶', '汽零', '一体化压铸', '汽车电子', '轮胎', '座椅']),
    ('资源周期', ['有色', '煤炭', '油气', '石油', '航运', '化工', '钢铁', '稀土', '锂矿', '黄金', '农药']),
    ('消费', ['白酒', '食品', '家电', '纺织', '零售', '宠物', '消费', '日化', '农业', '养殖']),
    ('金融地产', ['银行', '券商', '保险', '地产', '金融']),
    ('机器人军工', ['机器人', '减速器', '丝杠', '军工', '航空', '航天', '卫星', '低空']),
    ('软件传媒', ['软件', '信创', '传媒', '游戏', 'AI应用', '数据要素']),
]


def cluster_of(theme, sector):
    s = '%s %s' % (theme or '', sector or '')
    for name, kws in CLUSTERS:
        for k in kws:
            if k in s:
                return name
    return ''


def pick_top5(rows, cap=2):
    """按分数取 5 只，同一产业链最多 cap 只；被挤掉的记 cluster_cap 供页面展示。"""
    picks, cnt, capped = [], {}, []
    for r in rows:
        if len(picks) >= 5:
            break
        cl = r.get('cluster') or ''
        if cl and cnt.get(cl, 0) >= cap:
            r['capped_by'] = cl
            capped.append({'code': r['code'], 'name': r['name'], 'cluster': cl, 'total': r['total']})
            continue
        picks.append(r)
        if cl:
            cnt[cl] = cnt.get(cl, 0) + 1
    return picks, capped


def fetch_kf(cands):
    """扣非净利（亿元），用于利润质量核查。"""
    kf = {}
    for bare, code, name, pfx, mkt, seg, _ in cands:
        url = (EM + '?reportName=RPT_F10_FINANCE_MAINFINADATA'
               '&columns=SECUCODE,REPORT_DATE,KCFJCXSYJLR'
               '&filter=(SECUCODE%%3D%%22%s%%22)'
               '&sortColumns=REPORT_DATE&sortTypes=-1&pageSize=1&source=HSF10&client=PC' % (bare + '.' + mkt))
        t = http_get(url)
        if not t:
            continue
        try:
            rows = json.loads(t).get('result', {}).get('data') or []
            if rows and rows[0].get('KCFJCXSYJLR') is not None:
                kf[bare] = {'kf_yi': round(rows[0]['KCFJCXSYJLR'] / 1e8, 4),
                            'report': (rows[0].get('REPORT_DATE') or '')[:10]}
        except Exception:
            pass
        time.sleep(0.3)
    return kf


# ---------- 3) 打分 ----------
BIZ_SCORE = {
    '已有收入（据材料）': 15,
    '已有载板业务；ABF认证/爬坡（据材料）': 15,
    '已有硅片业务；分项贡献待核验': 12,
    '小批量（据材料），相关利润待核验': 12,
    '已签合同；履约收入待核验': 10,
    '低收入贡献（原材料口径，待核验）': 5,
    '相关资产及收入归属待核验': 3,
    '待核验': 5,
}


def roe_score(r):
    if r is None:
        return 5
    if r < 0:
        return 0
    if r < 3:
        return 10
    if r < 6:
        return 16
    if r < 10:
        return 20
    return 25


def load_theme_override(cfg):
    """人工修正表：{code: {theme, biz, fact}}，优先级最高。"""
    for p in (cfg.get('theme_override_path') or '', 'theme/theme_override.json'):
        if p and os.path.exists(p):
            try:
                return json.load(open(p, encoding='utf-8'))
            except Exception as e:
                log('题材修正表读取失败 %s: %s' % (p, e))
    return {}


def score(cands, tx, listing, fin, kf, min_amt=0, ovr=None, kl=None):
    rows, dropped = [], []
    for bare, code, name, pfx, mkt, seg, c in cands:
        q = tx.get(bare) or {}
        f = fin.get(bare) or {}
        mc = q.get('totalcap_yi') or 0
        amt = q.get('amount_yi') or 0
        reason = None
        if q.get('is_st'):
            reason = 'ST/*ST'
        elif mc and mc < 30:
            reason = '总市值%.1f亿 < 30亿' % mc
        elif min_amt and amt and amt < min_amt:
            reason = '成交额%.2f亿 < %s亿' % (amt, min_amt)
        if reason:
            dropped.append({'code': code, 'name': name, 'seg': seg, 'reason': reason})
            continue

        tc = c.get('theme_classification') or {}
        biz = tc.get('business_realization')
        theme = tc.get('primary_theme')
        fact = tc.get('fact_summary')
        need_review = bool(tc.get('needs_manual_review'))
        review_reason = tc.get('review_reason') or ''
        ov = (ovr or {}).get(code) or (ovr or {}).get(bare) or {}
        if ov:
            if ov.get('theme'):
                theme = ov['theme']
            if ov.get('biz'):
                biz = ov['biz']
            if ov.get('fact'):
                fact = ov['fact']
            need_review, review_reason = False, ''
        dma = c.get('distance_ma20_atr') or 0
        roe = f.get('roe')
        kfv = (kf.get(bare) or {}).get('kf_yi')
        np_yi = f.get('netprofit_yi') or 0
        kf_ratio = round(kfv / np_yi, 3) if (kfv is not None and np_yi > 0) else None
        # 用户长期规则：扣非/归母 < 60% 视为利润失真，一票否决（此前只标记未执行）
        if kf_ratio is not None and kf_ratio < 0.6:
            dropped.append({'code': code, 'name': name, 'seg': seg,
                            'reason': '扣非/归母 %.1f%% < 60%% 利润失真' % (kf_ratio * 100)})
            continue

        s_pos = max(0.0, 35.0 * (1 - dma / 3.0))
        s_fun = roe_score(roe) * 30.0 / 25.0
        s_biz = BIZ_SCORE.get(biz, 4 if biz is None else 5) * 20.0 / 15.0
        s_tec = 15.0 * min(1.0, (c.get('technical_score') or 0) / 30.0)
        atr_pct = c.get('atr14_pct') or 0
        km = (kl or {}).get(bare) or {}
        cpen, ctag = crowd_penalty(km.get('r250'), atr_pct, dma)
        total = s_pos + s_fun + s_biz + s_tec - cpen

        rows.append({
            'code': code, 'name': name, 'seg': seg,
            'close': q.get('close'), 'chg': q.get('chg_pct'), 'amt': amt,
            'turnover': q.get('turnover_pct'), 'totalcap': mc,
            'listing': (listing.get(bare) or {}).get('LISTING_DATE'),
            'dma': round(dma, 2), 'atr': round(c.get('atr14_pct') or 0, 2),
            'tech': round(c.get('technical_score') or 0, 2),
            'sh': c.get('structure_health_score'), 'tsc': c.get('trend_structure_score'),
            'vp': round(c.get('volume_price_score') or 0, 2),
            'mf': round(c.get('market_fit_score') or 0, 2),
            'mrs': round(c.get('momentum_relative_strength_score') or 0, 2),
            'j': round(c.get('kdj_j') or 0, 1), 'jprev': round(c.get('kdj_j_prev') or 0, 1),
            'k': round(c.get('kdj_k') or 0, 1), 'dd': round(c.get('kdj_d') or 0, 1),
            'xst': c.get('kdj_cross_state'), 'xage': c.get('kdj_cross_age_days'),
            'rsi': round(c.get('rsi14') or 0, 1),
            'vr': round(c.get('volume_ratio_1d_prior_10d') or 0, 2),
            'amtr': round(c.get('amount_ratio_1d_20d') or 0, 2),
            'cl': round(c.get('close_location') or 0, 2),
            'cvw': round(c.get('close_vs_vwap_pct') or 0, 2),
            'r1': round(c.get('return_1d_pct') or 0, 2), 'r5': round(c.get('return_5d_pct') or 0, 2),
            'r10': round(c.get('return_10d_pct') or 0, 2), 'r20': round(c.get('return_20d_pct') or 0, 2),
            'theme': theme, 'biz': biz, 'fact': fact,
            'theme_src': ('manual_override' if ov else tc.get('classification_status')),
            'theme_review': need_review, 'theme_reason': review_reason,
            'roe': roe, 'np': np_yi, 'rev': f.get('revenue_yi') or 0, 'report': f.get('report'),
            'gm': round(f.get('gross_margin') or 0, 1),
            'kf_yi': kfv, 'kf_ratio': kf_ratio,
            'kf_flag': ('利润失真' if (kf_ratio is not None and kf_ratio < 0.6) else
                        ('亏损' if np_yi <= 0 else 'OK')),
            'high_knife': bool(dma >= 2.0),
            'atrp': round(atr_pct, 2),
            'r250': km.get('r250'), 'r120': km.get('r120'), 'ddh': km.get('ddh'),
            'crowd_pen': cpen, 'crowd_tag': ctag,
            'cluster': cluster_of(theme, c.get('sector')), 'capped_by': '',
            'sector': c.get('sector'), 'secrank': c.get('sector_rank'),
            'setup': c.get('setup_type'),
            'quant_rank': c.get('quant_rank'), 'quant_score': round(c.get('quant_score') or 0, 2),
            'sp': round(s_pos, 1), 'sf': round(s_fun, 1), 'sb': round(s_biz, 1), 'st': round(s_tec, 1),
            'total': round(total, 1),
        })
    rows.sort(key=lambda r: -r['total'])
    return rows, dropped


# ---------- 4) LLM 点评 ----------
def llm_comment(cfg, rows, trade_date, dropped):
    llm = cfg['llm']
    if not llm.get('enabled'):
        return {'enabled': False, 'text': ''}
    key = os.environ.get(llm.get('api_key_env', 'DEEPSEEK_API_KEY'), '')
    if not key:
        return {'enabled': False, 'error': 'missing api key env %s' % llm.get('api_key_env')}

    top = rows[:8]
    lines = []
    for i, r in enumerate(top, 1):
        lines.append(
            '%d. %s(%s/%s) 总分%.1f[位置%.0f/基本%.0f/逻辑%.0f/技术%.0f] 收盘%.2f 涨%.2f%% '
            '距MA20=%.2fATR 市值%.0f亿 ROE=%s 营收%.2f亿 归母%.2f亿 扣非%.2f亿(占比%s) '
            'J=%.1f RSI=%.1f 量比%.2f 5日%.1f%% 20日%.1f%% 题材=%s 落地=%s 事实=%s'
            % (i, r['name'], r['code'], r['seg'], r['total'], r['sp'], r['sf'], r['sb'], r['st'],
               r['close'] or 0, r['chg'] or 0, r['dma'], r['totalcap'] or 0, r['roe'],
               r['rev'], r['np'], r['kf_yi'] if r['kf_yi'] is not None else -1,
               ('%.0f%%' % (r['kf_ratio'] * 100)) if r['kf_ratio'] is not None else 'NA',
               r['j'], r['rsi'], r['vr'], r['r5'], r['r20'], r['theme'], r['biz'],
               (r['fact'] or '')[:80]))
    pool = '候选池 %d 只，剔除 %d 只。剔除原因：%s' % (
        len(rows), len(dropped), '；'.join(sorted({d['reason'].split('（')[0] for d in dropped}) or ['无']))

    prompt = (
        '你是A股短线选股助理。以下是 %s 收盘后由一个量化打分系统产出的候选池与打分明细。\n'
        '打分框架：位置安全35（距MA20的ATR倍数越低越好）+ 基本面30（ROE分档）+ 逻辑纯度20（已有收入>有实证>待核验）+ 技术15，满分100。\n'
        '用户约束：短线持仓；沪市主板+深市主板+创业板+科创板；不吃流动性折价（资金量小）；实际只买1-2只。\n'
        '请输出严格 JSON（不要 markdown 代码块），结构：\n'
        '{"market_view":"<80字内市场判断>","top_rationale":[{"code":"","name":"","reason":"<60字，说明为什么排这个位置>","risk":"<40字主要风险>"}],'
        '"manual_veto":[{"code":"","name":"","why":"<人工否决或降权的理由，没有则空数组>"}],'
        '"summary":"<60字给用户的操作提示>"}\n'
        '要求：\n'
        '1) 扣非净利/归母 <60% 的标的视为利润失真，必须降权或否决，并在 manual_veto 说明；\n'
        '2) 距MA20 >=2.0 ATR 视为高位飞刀，明确提示；\n'
        '3) 分数高不等于有短线动能，成交额过小（<5亿）需在 risk 中提示；\n'
        '4) top_rationale 只给前5名，按确定性排序。\n\n'
        '%s\n\n%s' % (trade_date, pool, '\n'.join(lines)))

    body = json.dumps({
        'model': llm['model'],
        'messages': [{'role': 'user', 'content': prompt}],
        'max_tokens': llm.get('max_tokens', 2500),
        'temperature': 0.3,
    }, ensure_ascii=False).encode('utf-8')
    req = urllib.request.Request(
        llm['base_url'].rstrip('/') + '/chat/completions',
        data=body,
        headers={'Content-Type': 'application/json', 'Authorization': 'Bearer ' + key})
    try:
        raw = urllib.request.urlopen(req, timeout=llm.get('timeout', 180)).read().decode('utf-8', 'ignore')
        content = json.loads(raw)['choices'][0]['message']['content']
    except Exception as e:
        return {'enabled': True, 'error': str(e), 'text': ''}
    try:
        s = content.strip()
        if s.startswith('```'):
            s = s.strip('`').split('\n', 1)[1].rsplit('```', 1)[0]
        return {'enabled': True, 'json': json.loads(s), 'text': content}
    except Exception as e:
        return {'enabled': True, 'parse_error': str(e), 'text': content}


# ---------- 5) 存档 ----------
def archive(cfg, trade_date, payload):
    out_root = abspath(cfg['out_dir'])
    day_dir = os.path.join(out_root, trade_date)
    os.makedirs(day_dir, exist_ok=True)
    for k in ('verify', 'scored', 'dropped', 'picks', 'market', 'comment'):
        if k in payload:
            json.dump(payload[k], open(os.path.join(day_dir, k + '.json'), 'w', encoding='utf-8'),
                      ensure_ascii=False, indent=1)
    latest = os.path.join(out_root, 'latest')
    if os.path.islink(latest) or os.path.exists(latest):
        os.remove(latest) if os.path.islink(latest) else None
    try:
        if os.path.exists(latest):
            os.remove(latest)
        os.symlink(day_dir, latest)
    except Exception:
        pass
    json.dump({'trade_date': trade_date, 'generated_at': datetime.now(CST).isoformat()},
              open(os.path.join(day_dir, '_meta.json'), 'w', encoding='utf-8'), ensure_ascii=False, indent=1)
    return day_dir


def push_results(cfg, trade_date, day_dir):
    """把当日结果推到 GitHub，供云端自动化取数生成看板。"""
    push = cfg.get('push') or {}
    if not push.get('enabled'):
        return False
    url = push.get('repo_url', '')
    if not url:
        log('push 已启用但未配置 repo_url')
        return False
    repo = abspath(push.get('repo_dir', 'results_repo'))
    branch = push.get('branch', 'main')
    if os.path.isdir(os.path.join(repo, '.git')):
        os.system("cd '%s' && git pull --quiet --ff-only" % repo)
    else:
        os.system("git clone --depth 1 -b %s '%s' '%s'" % (branch, url, repo))

    sub = os.path.join(repo, push.get('subdir', 'daily'), trade_date)
    os.makedirs(sub, exist_ok=True)
    for fn in os.listdir(day_dir):
        if fn.endswith('.json'):
            shutil.copyfile(os.path.join(day_dir, fn), os.path.join(sub, fn))
    # 供云端发现最新一期，不用遍历目录
    json.dump({'trade_date': trade_date,
               'generated_at': datetime.now(CST).isoformat(),
               'path': '%s/%s' % (push.get('subdir', 'daily'), trade_date)},
              open(os.path.join(repo, 'latest.json'), 'w', encoding='utf-8'),
              ensure_ascii=False, indent=1)
    json.dump({'trade_date': trade_date, 'path': '%s/%s' % (push.get('subdir', 'daily'), trade_date)},
              open(os.path.join(repo, push.get('subdir', 'daily'), 'latest.json'), 'w', encoding='utf-8'),
              ensure_ascii=False, indent=1)

    msg = 'pick5 %s' % trade_date
    rc = os.system("cd '%s' && git add -A && git commit -q -m '%s' && git push -q origin %s"
                   % (repo, msg, branch))
    log('推送 GitHub %s' % ('成功' if rc == 0 else '失败 rc=%d' % rc))
    return rc == 0


# ---------- main ----------
def main():
    cfg = load_cfg()
    now = datetime.now(CST)
    target = os.environ.get('FORCE_DATE') or now.strftime('%Y-%m-%d')
    want = target.replace('-', '')

    closed = load_calendar()
    if not os.environ.get('FORCE_DATE'):
        ok, why = is_trading_day(now, closed)
        if not ok:
            log('非交易日(%s)，跳过' % why)
            return 0
    else:
        log('FORCE_DATE=%s，跳过休市校验' % target)

    use_local = bool(cfg.get('local_bundle_path'))   # 配了本地 bundle 就不联网拉 GitHub
    if use_local:
        log('走 ECS 本地源数据: %s' % cfg['local_bundle_path'])
        repo = abspath(cfg['repo_dir'])
    else:
        repo = sync_repo(cfg)
        log('上游同步完成: %s' % repo)

    # 等 manifest 就绪（上游一般由 18:20 的 cron 触发，18:21 左右产出）
    deadline = time.time() + cfg.get('wait_manifest_minutes', 90) * 60
    mf = read_manifest(repo, cfg)
    while True:
        mf = read_manifest(repo, cfg)
        if mf and str(mf.get('trade_date')) == want:
            break
        if time.time() > deadline:
            log('manifest 仍未就绪（期望 trade_date=%s，当前=%s），放弃' % (
                want, (mf or {}).get('trade_date')))
            return 2
        log('manifest 未就绪，%ds 后重试' % cfg.get('wait_interval_seconds', 300))
        time.sleep(cfg.get('wait_interval_seconds', 300))
        if not use_local:
            sync_repo(cfg)
    log('manifest 就绪 trade_date=%s' % mf['trade_date'])

    bundle = read_bundle(repo, cfg)
    if not bundle:
        log('bundle 未找到')
        return 2
    cands = build_candidates(bundle)
    log('候选 %d 只（已剔北交所）' % len(cands))

    tx = fetch_quotes(cands)
    miss = [c[1] for c in cands if c[0] not in tx]
    if miss:
        log('行情缺失 %d 只: %s' % (len(miss), ','.join(miss[:8])))
    listing = fetch_listing(cands)
    fin = fetch_fin(cands)
    kf = fetch_kf(cands)
    log('复核完成 行情%d 上市日期%d 财务%d 扣非%d' % (len(tx), len(listing), len(fin), len(kf)))

    verify = {}
    for bare, code, name, pfx, mkt, seg, c in cands:
        verify[bare] = {'name': name, 'mkt': pfx, 'seg': seg,
                        'tx': tx.get(bare), 'listing': listing.get(bare),
                        'fin': fin.get(bare), 'kf': kf.get(bare)}

    kl = fetch_kline_metrics(cands, want)
    log('K线就绪 %d/%d 只' % (len(kl), len(cands)))

    ovr = load_theme_override(cfg)
    log('题材人工修正表 %d 条' % len(ovr))
    rows, dropped = score(cands, tx, listing, fin, kf, cfg.get('min_amount_yi', 0), ovr, kl)
    log('打分完成 候选%d 剔除%d' % (len(rows), len(dropped)))
    hit = [r for r in rows[:12] if r['crowd_pen'] > 0]
    if hit:
        log('拥挤度扣分: %s' % ', '.join('%s-%d(%s)' % (r['name'], r['crowd_pen'], r['crowd_tag'])
                                         for r in hit))

    bad = [r for r in rows[:10] if r['kf_flag'] == '利润失真']
    if bad:
        log('利润失真标记: %s' % ', '.join('%s(%.0f%%)' % (r['name'], r['kf_ratio'] * 100) for r in bad))

    comment = llm_comment(cfg, rows, want, dropped)
    log('LLM 点评: %s' % ('ok' if comment.get('json') else comment.get('error', 'skipped')))

    picks, capped = pick_top5(rows, cap=cfg.get('cluster_cap', 2))
    if capped:
        log('产业链上限挤出: %s' % ', '.join('%s(%s)' % (x['name'], x['cluster']) for x in capped))
    market = {
        'trade_date': want,
        'previous_trade_date': bundle.get('previous_trade_date'),
        'next_trade_date': bundle.get('next_trade_date'),
        'generated_at': bundle.get('generated_at'),
        'stage': bundle.get('stage'),
        'rules': bundle.get('rules'),
        'env': bundle.get('market_environment') or {},
        'pool_counts': {'official': len(bundle.get('official_review_candidates') or []),
                        'external': len(bundle.get('external_review_candidates') or []),
                        'scored': len(rows), 'dropped': len(dropped)},
        'risk_controls': {'cluster_cap': cfg.get('cluster_cap', 2), 'cluster_capped': capped,
                          'crowd_rule': '年涨幅+ATR%+贴均线，实验参数未回测'},
    }
    day_dir = archive(cfg, want, {'verify': verify, 'scored': rows,
                                  'dropped': dropped, 'picks': picks,
                                  'market': market, 'comment': comment})
    log('存档 -> %s' % day_dir)
    push_results(cfg, want, day_dir)

    print('\n=== Top5 (%s) ===' % want)
    for i, r in enumerate(picks, 1):
        print('%d. %-8s %-6s %5.1f | 位置%.0f 基本%.0f 逻辑%.0f 技术%.0f 拥挤-%d | 扣非%s | %s/%s' % (
            i, r['name'], r['code'], r['total'], r['sp'], r['sf'], r['sb'], r['st'],
            r['crowd_pen'], r['kf_flag'], r['theme'], r['cluster'] or '未归类'))
    return 0


if __name__ == '__main__':
    try:
        sys.exit(main())
    except SystemExit:
        raise
    except Exception as e:
        log('FATAL %s' % e)
        sys.exit(1)
