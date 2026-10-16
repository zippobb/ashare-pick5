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
            # 规则5（2026-10-13）：5日动量，供"位置维→动量维"（方案D）使用
            'mom5': round((c / cl[-6] - 1) * 100, 2) if len(cl) > 6 else None,
            # 规则6（2026-10-15）：3 日动量，供「题材内相对强势」打分
            'mom3': round((c / cl[-4] - 1) * 100, 2) if len(cl) > 4 else None,
            # 规则5（2026-10-13）：5日动量，供"位置维→动量维"（方案D）使用
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


# ---------- 3.1.5) 主业题材统一口径（人工规则 2026-10-12）----------
# 背景：机器派生题材取东方财富概念列表的第一个热词，由此出现偏离主业的标签：
#   贵州茅台 -> 味蕾经济   五粮液/汾酒/老窖 -> 味蕾经济
#   联影医疗 -> 脑机接口   迈瑞医疗 -> 精准诊断   福瑞医科 -> 创新医疗服务
#   片仔癀/云南白药 -> 特色药
# 规则：通达信细分行业（theme_classification.industry_l3）命中下表时，
#       题材强制统一为该主业词，并视为已人工确认（不再显示"题材待修正"）。
# 优先级：theme/theme_override.json（人工单票） > 本表（行业口径） > 机器派生。
# 新增行业只需往本表加一行；未来新入池的同行业标的自动生效。
INDUSTRY_THEME_NORMALIZE = {
    u"白酒\u2162": u"白酒",        # 白酒Ⅲ：以白酒生产为主业
    u"医疗设备": u"医疗器械",       # 医疗器械（设备）
    u"医疗耗材": u"医疗器械",       # 医疗器械（耗材）
    u"中药\u2162": u"中药",        # 中药Ⅲ：中药 / 中药材
}

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
               '&columns=SECUCODE,REPORT_DATE,KCFJCXSYJLR,KCFJCXSYJLRTZ,ROEKCJQ'
               '&filter=(SECUCODE%%3D%%22%s%%22)'
               '&sortColumns=REPORT_DATE&sortTypes=-1&pageSize=1&source=HSF10&client=PC' % (bare + '.' + mkt))
        t = http_get(url)
        if not t:
            continue
        try:
            rows = json.loads(t).get('result', {}).get('data') or []
            if rows and rows[0].get('KCFJCXSYJLR') is not None:
                kf[bare] = {'kf_yi': round(rows[0]['KCFJCXSYJLR'] / 1e8, 4),
                            # 扣非同比增速 + 扣非加权ROE：用于区分"主业在改善"与"主业在崩塌"
                            'kf_yoy': rows[0].get('KCFJCXSYJLRTZ'),
                            'roe_kf': rows[0].get('ROEKCJQ'),
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


# 全市场通达信细分行业字典（代码 -> 行业），仅在 industry_l3 缺失时兜底。
# 场景：人工229表条目（classification_status=user_confirmed）不带 industry_l3，
#       例如联影医疗 688271.SH 行业实为"医疗设备"，但表里只有 primary_theme=脑机接口。
INDUSTRY_L3_FALLBACK = {}


# ---------- 3.1.6) 股性/弹性门槛（2026-10-13）----------
# 复合弹性分 0~100 = 四个波动指标的归一化均值：
#   日均振幅(amp_mean) / 近60日单日涨幅>=5%天数占比 / 近20日波动率 / 近60日最大单日涨幅
# 数据：data/elasticity_latest.csv（由历史日线滚动 60 日预计算）
# 用途：只做候选池准入过滤，不参与打分权重（回测证明进权重会变差）
ELASTICITY = {}
ELAST_MIN = 18.0


def load_elasticity():
    """读预计算弹性指标，合成 0~100 分。任何异常都 fail open（返回空表）。"""
    import csv as _csv
    m = {}
    for p in ('data/elasticity_latest.csv', '/opt/ashare-pick5/data/elasticity_latest.csv'):
        try:
            if not os.path.exists(p):
                continue
            with open(p, encoding='utf-8-sig') as f:
                for r in _csv.DictReader(f):
                    c = (r.get('ts_code') or '').strip()
                    if not c:
                        continue
                    try:
                        amp = float(r['amp_mean'])
                        big = float(r['big_up_60'])
                        v20 = float(r['vol20'])
                        mg = float(r['max_gain_60'])
                    except (ValueError, TypeError, KeyError):
                        continue
                    m[c] = (min(1.0, amp / 10.0) + min(1.0, big / 25.0)
                            + min(1.0, v20 / 6.0) + min(1.0, max(0.0, mg) / 20.0)) / 4.0 * 100.0
            break
        except Exception as e:
            log(u'\u5f39\u6027\u5206\u8bfb\u53d6\u5931\u8d25 %s: %s' % (p, e))
    return m


def load_industry_map():
    """读 theme/theme_market_full.csv 建行业字典。任何异常都 fail open（返回空表）。"""
    import csv as _csv
    m = {}
    for p in ('theme/theme_market_full.csv', '/opt/ashare-pick5/theme/theme_market_full.csv'):
        try:
            if not os.path.exists(p):
                continue
            with open(p, encoding='utf-8-sig') as f:
                for row in _csv.DictReader(f):
                    code = (row.get(u'\u4ee3\u7801') or '').strip()
                    ind = (row.get(u'\u884c\u4e1a') or '').strip()
                    if code and ind:
                        m[code] = ind
            break
        except Exception as e:
            log(u'\u884c\u4e1a\u5b57\u5178\u8bfb\u53d6\u5931\u8d25 %s: %s' % (p, e))
    return m


def load_theme_effective():
    """个股 -> 重映射后的有效题材。

    来源 /opt/stock-automation/scanner/config/stock_theme_effective.json
    （由 ops 侧题材体检生成：666 个题材里 186 个可做轮动主线，其余为宽口径 /
      不联动 / 从不成为主线 / 追进去无超额 / 成员过少）。
    缺失时返回空表，题材逻辑静默退回原值，不影响主流程。
    """
    try:
        return json.load(open('/opt/stock-automation/scanner/config/stock_theme_effective.json', encoding='utf-8'))
    except Exception:
        return {}


EFF_THEME = load_theme_effective()


# ── 规则6（2026-10-15）：题材轮动参与每日选股 ──────────────────────────────
# 旧口径「题材等权 10 日动量」太滞后（题材涨势常只有 3~5 日）。341 天长样本
# （观察池口径配对 alpha，未来 5 日，对照同池随机 5 只）实测：
#   现行纯 D（不做轮动）          +0.47%/期  t=+1.11
#   RET1（昨日题材涨幅）×Top3    +1.07%/期  t=+2.55   ← 覆盖 48%
#   ACC（1日−前5日均值）×Top5    +0.90%/期  t=+2.30   ← 覆盖 62%
#   三级级联（RET1→ACC→RET10）   +1.08%/期  t=+2.74   ← 覆盖 90%，落地用这个
# 前半段/后半段、涨市/跌市、池内半池交叉检验均一致为正；跌市改善最大
# （纯 D −0.13% → 题材优先 +0.86%~+0.98%）。
# 题材热度必须用**全市场**成员算（池内每题材只有 1~2 只，噪音极大且偏小盘），
# 再用池内标的兑现。快照由 ops/theme_rotation.py 每日产出。
# ROTATION_MARK_20261015
ROT_JSON = '/opt/stock-automation/scanner/config/theme_rotation_latest.json'


def load_theme_rotation():
    """题材轮动快照。任何异常一律 fail open（返回空表 → 退回现行纯 D 选股）。"""
    try:
        return json.load(open(ROT_JSON, encoding='utf-8'))
    except Exception:
        return {}


ROT = load_theme_rotation()


# 题材内相对强势满分 30（个股 3 日涨幅 − 题材 3 日涨幅，候选集内百分位）
ROT_RS_MAX = 30.0
# 三级级联：(排序字段名, TopK) —— 字段见 theme_rotation.py 的 rank / rank_acc / rank_r10
ROT_STEPS = (('rank', 3), ('rank_acc', 5), ('rank_r10', 5))
ROT_MIN_CAND = 5
# 硬过滤未命中时的软加分满分（题材热度横截面分位 × 本值）。
# 341 天回测（观察池口径，w=30，热度=z(昨日涨幅)+z(加速度)+z(上涨家数占比)）：
#   纯 D +0.47%/期(t=1.11) → 软加分 +0.84%/期(t=2.33)。弱于硬过滤(+1.08%)但覆盖率 100%。
ROT_SOFT_W = 30.0
# 普跌日关闭：当日全市场等权跌幅达到此值时不启用题材轮动（退回纯 D）。
# 341 天回测：大跌日（≤−1%）软加分 −0.56%/期、硬过滤 −0.28%/期，均无预测力。
ROT_OFF_MKT = -1.0


def theme_priority(rows, rot, min_cand=ROT_MIN_CAND, steps=ROT_STEPS):
    """热题材优先：把候选限制在热题材内，并按「题材内相对强势」加分重排。

    返回 (mode, hot_themes, cand_codes)。
      mode = 'THEME'    命中热题材且池内候选 >= min_cand，rows 内已写好 srs/total_rot
            'FALLBACK' 三轮都没有足够候选，退回现行纯 D（不改动任何分数）
            'OFF'      无快照，同上
    """
    th = rot.get('themes') or {}
    if not th:
        return 'OFF', [], None
    by_th = {}
    for r in rows:
        by_th.setdefault(r.get('rot_theme') or '', []).append(r)
    for key, k in steps:
        order = sorted([t for t in th if (th[t] or {}).get(key)],
                       key=lambda t: th[t][key])
        hot = order[:k]
        cand = [r for t in hot for r in by_th.get(t, [])]
        if len(cand) < min_cand:
            continue
        # 题材内相对强势：个股近 3 日涨幅 − 所属题材近 3 日涨幅
        raw = {}
        for r in cand:
            t3 = (th.get(r.get('rot_theme')) or {}).get('ret3')
            m3 = r.get('mom3')
            if m3 is None or t3 is None:
                continue
            raw[r['code']] = float(m3) - float(t3)
        vals = sorted(raw.values())
        n = len(vals)
        for r in cand:
            if r['code'] in raw and n:
                v = raw[r['code']]
                lo = sum(1 for x in vals if x < v)
                eq = sum(1 for x in vals if x == v)
                r['srs'] = round(ROT_RS_MAX * (lo + 0.5 * eq) / n, 1)
            else:
                r['srs'] = round(ROT_RS_MAX * 0.5, 1)   # 缺数据按中位
            r['total_rot'] = round(r['total'] + r['srs'], 1)
        return 'THEME', hot, [r['code'] for r in cand]
    return 'FALLBACK', [], None


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
    # ── 规则5（2026-10-13）：位置维 → 5日动量维（方案 D）──
    # 341 天长样本（297 期有效，观察池口径配对 alpha）：D 是唯一显著方案 +0.92%/期（t=2.18）。
    #   上涨市 D(+1.26) ≈ A(+1.23) 都显著；下跌市 D(+0.44) 远好于 A(−0.24，全表最差)。
    # 回测口径 = 弹性门槛后候选池内的横截面百分位排名（越高越好），此处保持一致。
    # 注意：动量是追涨方向，与下方 crowd_penalty（高位高波动扣分）天然对冲，
    #       两者同时保留，可抑制纯追高，不要删掉拥挤度扣分。
    MOM_PCT = {}
    _elig = []
    for bare, code, name, pfx, mkt, seg, c in cands:
        q = tx.get(bare) or {}
        mc = q.get('totalcap_yi') or 0
        amt = q.get('amount_yi') or 0
        if q.get('is_st') or (mc and mc < 30) or (min_amt and amt and amt < min_amt):
            continue
        _el = ELASTICITY.get(code) or ELASTICITY.get(bare)
        if _el is not None and _el < ELAST_MIN:
            continue
        _v = ((kl or {}).get(bare) or {}).get('mom5')
        if _v is None:
            continue
        try:
            _elig.append((bare, float(_v)))
        except (TypeError, ValueError):
            pass
    _elig.sort(key=lambda kv: kv[1])
    _nn = len(_elig)
    for _i, (_b, _v) in enumerate(_elig):
        MOM_PCT[_b] = (_i + 1) / float(_nn) if _nn else 0.5
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
        if reason is None:
            _el = ELASTICITY.get(code) or ELASTICITY.get(bare)
            if _el is not None and _el < ELAST_MIN:
                reason = u'\u80a1\u6027\u4e0d\u8db3\uff1a\u5f39\u6027\u5206%.0f < %.0f' % (_el, ELAST_MIN)
        if reason:
            dropped.append({'code': code, 'name': name, 'seg': seg, 'reason': reason})
            continue

        tc = c.get('theme_classification') or {}
        biz = tc.get('business_realization')
        theme = tc.get('primary_theme')
        # ── 第 0 层：无效题材重映射（2026-10-14）──
        # 东财机器派生的主题材里大量是失真标签：宽口径 WIDE（"通信技术"338只跨74行业、
        # "电池技术"472只跨148行业）或不联动 LOOSE。scanner 侧 _theme_pick 只按
        # "成员>=100 + 泛化词表"粗筛，剩下的往往是次热的次要概念，仍然偏离主业
        # （实测：格力→液冷服务器、中国神华→铁路基建、海天/双汇→味蕾经济）。
        # 这里把机器派生值换成有效性表里重映射后的主业题材：
        # 格力→空调、中国神华→动力煤、海天→调味发酵品Ⅲ、宁德→锂电池。
        # 只对 machine_derived 生效；人工层（override / 229 表）优先级更高，不动。
        # 341 天回测：限定有效题材 + 单归属后，板块轮动配对 alpha
        # 由 +0.56%/期(t=1.43,不显著) 提升到 +1.18%/期(t=2.92)。
        if (tc.get('classification_status') or 'machine_derived') == 'machine_derived':
            _ef = EFF_THEME.get(code) or EFF_THEME.get(bare) or {}
            if _ef.get('primary_eff'):
                theme = _ef['primary_eff']
        fact = tc.get('fact_summary')
        need_review = bool(tc.get('needs_manual_review'))
        review_reason = tc.get('review_reason') or ''
        # 主业题材统一口径：行业命中规则表时强制使用主业词（见 INDUSTRY_THEME_NORMALIZE）
        _l3 = (tc.get('industry_l3') or INDUSTRY_L3_FALLBACK.get(code)
              or INDUSTRY_L3_FALLBACK.get(bare) or '')
        norm_theme = INDUSTRY_THEME_NORMALIZE.get(_l3)
        theme_src = tc.get('classification_status') or 'machine_derived'
        # ── 第 1 层：主业口径落地 ──
        # 行业命中规则表时强制使用主业词（见 INDUSTRY_THEME_NORMALIZE）。
        # 2026-10-13 修复：本层必须在 override 之前执行。否则 override 里
        # 只填了 biz/fact、theme 留空的标的（如片仔癀）会被空 theme 挡住，
        # 题材退回机器派生的错误热词（"特色药"）。
        if norm_theme:
            theme = norm_theme
            theme_src = 'industry_normalized'
            need_review, review_reason = False, ''
            # ── 逻辑分补档（2026-10-13）──
            # 题材由"主业行业"归一化而来，说明此时 题材 == 主业，该题材必然已有实际收入。
            # 机器侧是按错误题材判定的（如联影=脑机接口、片仔癀=特色药），
            # 它给的 biz 或空或"待核验"，系统性低估（s_biz 只拿 4/15 档 → 5.3/20）。
            # 此处统一补到最高档；仅覆盖"无信息 / 待核验"类低档，不覆盖已有实质档位。
            if biz is None or BIZ_SCORE.get(biz, 5) <= 5:
                biz = u'\u5df2\u6709\u6536\u5165\uff08\u636e\u6750\u6599\uff09'
        # ── 第 2 层：人工修正表，非空字段才覆盖（优先级最高）──
        ov = (ovr or {}).get(code) or (ovr or {}).get(bare) or {}
        if ov:
            if ov.get('theme'):
                theme = ov['theme']
            if ov.get('biz'):
                biz = ov['biz']
            if ov.get('fact'):
                fact = ov['fact']
            theme_src = 'manual_override'
            need_review, review_reason = False, ''
        # ── 第 2 层：人工修正表，非空字段才覆盖（优先级最高）──
        ov = (ovr or {}).get(code) or (ovr or {}).get(bare) or {}
        if ov:
            if ov.get('theme'):
                theme = ov['theme']
            if ov.get('biz'):
                biz = ov['biz']
            if ov.get('fact'):
                fact = ov['fact']
            theme_src = 'manual_override'
            need_review, review_reason = False, ''
        dma = c.get('distance_ma20_atr') or 0
        roe = f.get('roe')
        kfv = (kf.get(bare) or {}).get('kf_yi')
        kmeta = (kf.get(bare) or {})
        np_yi = f.get('netprofit_yi') or 0
        kf_ratio = round(kfv / np_yi, 3) if (kfv is not None and np_yi > 0) else None
        kf_yoy = kmeta.get('kf_yoy')
        roe_kf = kmeta.get('roe_kf')

        # ---- 利润质量三档判别（2026-10-09 用户质疑后重构）----
        # 旧规则：扣非/归母 < 60% 一票否决。
        #   缺陷：只看"比值"，不区分主业在改善还是在崩塌。
        #   实证（2026-10-08 电池题材扫描）：
        #     国轩高科 扣非比 8%，但扣非同比 +46.7%、归母同比 +278% —— 主业在改善，被误杀
        #     派能科技 扣非比 34%，扣非同比 +203.8% —— 扭亏上行，被误杀
        #     欣旺达   扣非比 16%，扣非同比 -83.3%、扣非ROE 0.39% —— 主业崩塌，该杀
        #   三者用旧规则是同一结论，显然不成立。
        # 新规则：以"主业是否在赚钱、趋势是向上还是向下"为本，比值为辅。
        # ── 规则1（2026-10-12 生效）：观察池内标的不做利润质量硬剔除，仅打标记 ──
        # 标记只用于提示与人工复核，不参与评分计算，对总分/排名/评分条无加减影响。
        # 保留的硬剔除只有：ST/*ST、总市值<30亿、成交额不足（见上方）。
        risk_flag, risk_reason = '', ''
        if kfv is None or np_yi <= 0:
            # 数据缺失或本身亏损
            if np_yi <= 0 and kfv is not None and kfv < 0:
                risk_flag = '归母与扣非双亏'
                risk_reason = '归母与扣非双亏（%.2f亿/%.2f亿）' % (np_yi, kfv)
        elif kfv <= 0:
            risk_flag = '扣非亏损'
            risk_reason = '扣非为负 %.2f亿，主营业务亏损' % kfv
        elif kf_yoy is not None and kf_yoy <= -50 and (roe_kf is not None and roe_kf < 1.0):
            # 主业崩塌：扣非绝对回报近零（扣非ROE<1%）且同比腰斩以上
            risk_flag = '主业崩塌'
            risk_reason = '主业崩塌：扣非同比 %.0f%% 且扣非ROE %.2f%%' % (kf_yoy, roe_kf)

        # 未达硬剔除 → 计算利润质量降权（从总分扣，不删标的）
        if kf_ratio is not None and kf_ratio < 0.6:
            if kf_yoy is not None and kf_yoy > 0:
                kf_pen, kf_tag = 8, '一次性收益抬高利润·主业改善'
            else:
                kf_pen, kf_tag = 12, '一次性收益抬高利润·主业下滑'
        elif np_yi <= 0:
            kf_pen, kf_tag = 12, '归母亏损'
        else:
            kf_pen, kf_tag = 0, ''

        # ── 规则2（2026-10-12）：位置 35→20、技术 15→30 ──
        # 依据：distance_ma20_atr 的 T+5 IC 仅 +0.025，位置维度缺乏预测力却占 35 分；
        # 回测 pick5 两维：现行(35/15) T+5 -3.17% → 调整后(20/30) -2.48%，
        # 跑赢候选池 43% → 52%。注意：此处"提技术"用的是重构后的六维分，
        # 若沿用旧 technical_score 反而恶化至 -3.47%。
        # 规则5（2026-10-13）：位置维改为 5 日动量百分位（满分 20）
        # 旧式：s_pos = max(0.0, 20.0 * (1 - dma / 3.0))  # 距MA20越近越好，T+5 IC 仅 +0.025
        s_pos = 20.0 * MOM_PCT.get(bare, 0.5)
        mom5 = ((kl or {}).get(bare) or {}).get('mom5')
        s_fun = roe_score(roe) * 30.0 / 25.0
        s_biz = BIZ_SCORE.get(biz, 4 if biz is None else 5) * 20.0 / 15.0
        s_tec = 30.0 * min(1.0, (c.get('technical_score') or 0) / 30.0)
        atr_pct = c.get('atr14_pct') or 0
        km = (kl or {}).get(bare) or {}
        cpen, ctag = crowd_penalty(km.get('r250'), atr_pct, dma)
        pen = cpen + kf_pen
        # ── 规则6：题材轮动字段（只写字段，不改分数；改分在 theme_priority）──
        _effc = EFF_THEME.get(code) or EFF_THEME.get(bare) or {}
        # 兜底顺序必须与 ops/theme_rotation.py 一致，否则个股查不到轮动状态：
        # theme_rot（有效概念/主业）> industry_l3（三级行业）> primary_eff > 展示用题材
        _rt = (_effc.get('theme_rot') or _effc.get('industry_l3')
               or _effc.get('primary_eff') or theme or '')
        _rd = (ROT.get('themes') or {}).get(_rt) or {}
        rot_theme = _rt
        rot_state = _rd.get('state') or '-'
        rot_rank = _rd.get('rank') or 0
        rot_ret1 = _rd.get('ret1')
        rot_ret3 = _rd.get('ret3')
        rot_ret5 = _rd.get('ret5')
        rot_pct = _rd.get('pct')
        # ── 规则7（2026-10-15）：题材阶段（首涨 / 连涨第N天 / 上涨趋势末端）──
        # 用户诉求：板块轮动速度极快，日报要能看出题材是首涨还是已连涨多日；
        # 震荡/下跌行情里，上涨趋势末端的板块容易买在高点。
        # 341 天回测（题材层面未来 5 日超额）：连涨≥4 天 → 全样本 −0.16%(t=−3.02)；
        # 下跌市 −0.88%(t=−3.93)、震荡市 −0.27%(t=−2.28)、上涨市中性(−0.01%)。
        # 但把"末端降级"加进选股框架后 alpha 无提升（软加分 0.86%→0.78%，硬过滤 1.01%→0.97%），
        # 故此处只标记、不扣分，与规则1 的 risk_flag 同一处理方式。
        rot_stage = _rd.get('stage') or '-'
        rot_streak = _rd.get('up_streak')
        rot_run_gain = _rd.get('run_gain')
        rot_pos60 = _rd.get('pos60')
        rot_dist_ma20 = _rd.get('dist_ma20')
        rot_trend = _rd.get('trend') or '-'
        rot_topping = bool(_rd.get('topping_risky'))
        stage_flag = ''
        if rot_topping:
            stage_flag = '题材末端·连涨%d天（本轮 +%.1f%%）' % (
                rot_streak or 0, rot_run_gain or 0.0)
        total = s_pos + s_fun + s_biz + s_tec - pen

        rows.append({
            'code': code, 'name': name, 'seg': seg,
            'close': q.get('close'), 'chg': q.get('chg_pct'), 'amt': amt,
            'turnover': q.get('turnover_pct'), 'totalcap': mc,
            'listing': (listing.get(bare) or {}).get('LISTING_DATE'),
            'dma': round(dma, 2), 'atr': round(c.get('atr14_pct') or 0, 2),
            'mom5': mom5, 'mompct': round(MOM_PCT.get(bare, 0.5), 3),
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
            'elast': round((ELASTICITY.get(code) or ELASTICITY.get(bare) or 0), 0),
            'theme': theme, 'biz': biz, 'fact': fact,
            'theme_src': theme_src,
            'theme_review': need_review, 'theme_reason': review_reason,
            'roe': roe, 'np': np_yi, 'rev': f.get('revenue_yi') or 0, 'report': f.get('report'),
            'gm': round(f.get('gross_margin') or 0, 1),
            'kf_yi': kfv, 'kf_ratio': kf_ratio, 'kf_yoy': kf_yoy, 'roe_kf': roe_kf,
            'kf_pen': kf_pen, 'kf_tag': kf_tag,
            'kf_flag': ('主业崩塌' if (kf_yoy is not None and kf_yoy <= -50 and roe_kf is not None
                                   and roe_kf < 1.0) else
                        ('扣非亏损' if (kfv is not None and kfv <= 0) else
                         ('利润失真' if (kf_ratio is not None and kf_ratio < 0.6) else
                          ('亏损' if np_yi <= 0 else 'OK')))),
            'risk_flag': risk_flag, 'risk_reason': risk_reason,
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
            'rot_theme': rot_theme, 'rot_state': rot_state, 'rot_rank': rot_rank,
            'rot_ret1': rot_ret1, 'rot_ret3': rot_ret3, 'rot_ret5': rot_ret5,
            'rot_pct': rot_pct, 'mom3': km.get('mom3'),
            'rot_heat_pct': _rd.get('heat_pct'), 'rot_heat': _rd.get('heat'),
            'rot_stage': rot_stage, 'rot_streak': rot_streak,
            'rot_run_gain': rot_run_gain, 'rot_pos60': rot_pos60,
            'rot_dist_ma20': rot_dist_ma20, 'rot_trend': rot_trend,
            'rot_topping': rot_topping, 'stage_flag': stage_flag,
            'srs': 0.0, 'srot': 0.0, 'total_rot': round(total, 1),
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
            '%d. %s(%s/%s) 总分%.1f[动量%.0f/基本%.0f/逻辑%.0f/技术%.0f] 收盘%.2f 涨%.2f%% '
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
        '打分框架：5日动量20（候选池内横截面百分位，越强越好）+ 基本面30（ROE分档）+ 逻辑纯度20（已有收入>有实证>待核验）+ 技术30（六维重构分），满分100；另扣拥挤度与利润质量分。\n'
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
    _imap = load_industry_map()
    INDUSTRY_L3_FALLBACK.clear()
    INDUSTRY_L3_FALLBACK.update(_imap)
    log('行业字典 %d 条' % len(INDUSTRY_L3_FALLBACK))
    _el = load_elasticity()
    ELASTICITY.clear()
    ELASTICITY.update(_el)
    log('弹性分 %d 条，候选门槛 %.0f' % (len(ELASTICITY), ELAST_MIN))
    global ROT
    ROT = load_theme_rotation()
    log('题材轮动快照 %s（%d 个题材）' % (ROT.get('trade_date') or '缺失', len(ROT.get('themes') or {})))
    rows, dropped = score(cands, tx, listing, fin, kf, cfg.get('min_amount_yi', 0), ovr, kl)
    log('打分完成 候选%d 剔除%d' % (len(rows), len(dropped)))

    # ── 规则6：题材轮动参与选股 ──
    rot_mode, rot_hot, rot_codes = 'OFF', [], None
    _mr1 = ROT.get('market_ret1')
    if str(ROT.get('trade_date')) != want:
        rot_mode = 'FALLBACK'
        log('题材轮动快照日期 %s != 交易日 %s，本次不启用题材轮动'
            % (ROT.get('trade_date'), want))
    elif _mr1 is not None and _mr1 <= ROT_OFF_MKT:
        rot_mode = 'FALLBACK'
        log('当日全市场等权 %+.2f%%（<= %.1f%%），普跌日题材轮动无效，本次回退纯 D'
            % (_mr1, ROT_OFF_MKT))
    else:
        rot_mode, rot_hot, rot_codes = theme_priority(rows, ROT)
        if rot_codes:
            _cs = set(rot_codes)
            head = sorted([r for r in rows if r['code'] in _cs], key=lambda r: -r['total_rot'])
            tail = sorted([r for r in rows if r['code'] not in _cs], key=lambda r: -r['total'])
            rows = head + tail
            log('题材优先命中：%s（候选 %d 只）' % (
                '/'.join('%s[%s]' % (t, (ROT['themes'][t] or {}).get('state')) for t in rot_hot),
                len(_cs)))
        else:
            # 软加分回退：热题材在池内凑不出足够候选时，仍让题材热度参与排序。
            # 每日实际候选只有 ~31 只（payload 口径），硬过滤命中率低，
            # 这一层保证「题材轮动每天都参与选股」，而不是长期空转。
            for r in rows:
                p = r.get('rot_heat_pct')
                r['srot'] = round(ROT_SOFT_W * (p if p is not None else 0.5), 1)
                r['total_rot'] = round(r['total'] + r['srot'], 1)
            rows = sorted(rows, key=lambda r: -r['total_rot'])
            rot_mode = 'SOFT'
            log('题材优先未命中（热题材在池内不足 %d 只），改用软加分（满分 %.0f）'
                % (ROT_MIN_CAND, ROT_SOFT_W))
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
        'rotation': {
            'mode': rot_mode,
            'trade_date': ROT.get('trade_date'),
            'indicator': ROT.get('indicator'),
            'market_ret1': ROT.get('market_ret1'), 'market_ret5': ROT.get('market_ret5'),
            'n_themes': ROT.get('n_themes'), 'state_count': ROT.get('state_count'),
            'hot_top10': ROT.get('hot_top10') or [],
            'cold_bottom5': ROT.get('cold_bottom5') or [],
            'focus': [dict(((ROT.get('themes') or {}).get(t) or {}), theme=t) for t in rot_hot],
            'n_cand': len(rot_codes) if rot_codes else 0,
            'off_mkt': ROT_OFF_MKT,
            'n_up_themes': ROT.get('n_up_themes'),
            'n_dn_themes': ROT.get('n_dn_themes'),
            'market_state': ROT.get('market_state'),
            'market_ret20': ROT.get('market_ret20'),
            'stage_count': ROT.get('stage_count'),
            'top_streak': ROT.get('top_streak'),
            'topping': sorted(
                [dict(v, theme=t) for t, v in (ROT.get('themes') or {}).items()
                 if v.get('topping_risky')],
                key=lambda x: -(x.get('up_streak') or 0))[:12],
        },
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
        print('%d. %-8s %-6s %5.1f | 动量%.0f 基本%.0f 逻辑%.0f 技术%.0f 拥挤-%d 质量-%d | 扣非%s | %s/%s' % (
            i, r['name'], r['code'], r['total'], r['sp'], r['sf'], r['sb'], r['st'],
            r['crowd_pen'], r['kf_pen'], r['kf_flag'], r['theme'], r['cluster'] or '未归类'))
    if rot_codes:
        print('题材优先模式：%s | 候选 %d 只' % (
            '/'.join('%s[%s]' % (t, (ROT['themes'][t] or {}).get('state')) for t in rot_hot),
            len(rot_codes)))
    for i, r in enumerate(picks, 1):
        _rs = (r.get('srs') or 0) if rot_mode == 'THEME' else (r.get('srot') or 0)
        print('   %d. %s %s +%.1f → 总分 %.1f（题材 %s · %s · %s）%s'
              % (i, r['name'], '题材内相对强势' if rot_mode == 'THEME' else '题材热度加分',
                 _rs, r.get('total_rot') or r['total'],
                 r.get('rot_theme') or '-', r.get('rot_state') or '-',
                 r.get('rot_stage') or '-',
                 ('  ⚠ ' + r['stage_flag']) if r.get('stage_flag') else ''))
    return 0


if __name__ == '__main__':
    try:
        sys.exit(main())
    except SystemExit:
        raise
    except Exception as e:
        log('FATAL %s' % e)
        sys.exit(1)
