# 云端自动化剧本（看板生成 + 发布）

> 终态：**本机彻底不用开机**。
> ECS 出数据 → GitHub → 云端（WorkBuddy）写文案 + 渲染 → 推回 GitHub → GitHub Pages 自动发布。

## 0. 固定信息（不要改）

| 项 | 值 |
| --- | --- |
| 结果仓库 | `zippobb/ashare-pick5`（**公开**） |
| RAW 前缀 | `https://raw.githubusercontent.com/zippobb/ashare-pick5/main` |
| 看板永久链接 | **https://zippobb.github.io/ashare-pick5/** |
| 数据指针 | `<RAW>/latest.json` → `{trade_date, generated_at, path}` |
| 当日数据 | `<RAW>/daily/<YYYYMMDD>/{picks,scored,market,dropped,verify}.json` |
| 渲染脚本 | `<RAW>/render/render_dashboard.py` + `<RAW>/render/template.css` |
| 已验证 | 云端 zsh + Python 3.11.1 + api.github.com 200 + raw.githubusercontent.com 200 |

ECS 侧：`/opt/ashare-pick5`，systemd timer `ashare-pick5.timer`，周一至周五 **18:40 CST** 出数并推送。
云端自动化建议排 **工作日 19:30 CST**。

## 1. 需要你准备的一件事：写入用的 GitHub Token

云端要把渲染好的 `index.html` 推回仓库，必须有一枚 token。**用 fine-grained PAT**（最小权限，随时可删）。

### 一键预填链接（推荐，官方支持 URL 参数预填）

直接点这个，名称/描述/所有者/有效期/权限都帮你填好了：

```
https://github.com/settings/personal-access-tokens/new?name=pick5-cloud-publish&description=Push+dashboard+index.html+to+ashare-pick5&target_name=zippobb&expires_in=365&contents=write
```

进去后**只有一步要手动做**：Repository access 选 **Only select repositories** → 下拉里选 `ashare-pick5`。
（官方 URL 参数不支持预选仓库，只能手动选）
然后点 **Generate token**，复制生成的 `github_pat_xxx`（只显示一次）。

### 手动路径（备选，官方文档原文步骤）

右上头像 → **Settings** → 左侧 **Developer settings** → 左侧 Personal access tokens → **Fine-grained tokens** → **Generate new token**

| 字段 | 填什么 |
| --- | --- |
| Token name | `pick5-cloud-publish` |
| Expiration | 365 天（可自定义；不建议 No expiration） |
| Resource owner | `zippobb` |
| Repository access | **Only select repositories** → 选 `ashare-pick5` |
| Permissions | 展开 **Repository permissions** → **Contents: Read and write**（只勾这一项；`Metadata: read` 会自动带上） |

点 Generate token，复制 `github_pat_xxx`。

### 填回 prompt

把 token 填到本节下面 prompt 的 `GH_TOKEN=` 处。
**云端小程序里的自动化 prompt 也要同步替换**——桌面端改不到云端任务。

> ⚠️ 不要用本机 `~/.git-credentials` 里那枚 `gho_` classic token 写进云端 prompt——它是全作用域的，
> 泄露即等于整个 GitHub 账号权限外泄。fine-grained token 只作用于 `ashare-pick5` 一个仓库。
> 参考：https://docs.github.com/en/authentication/keeping-your-account-and-data-secure/managing-your-personal-access-tokens

## 2. 云端自动化 prompt（整段复制）

排期：每工作日 19:30（Asia/Shanghai）。

> **2026-11-24 更新三点**（改的是 prompt，不是脚本逻辑）：
> ① 第 1 步与示例补拉 `data/char_latest.json`、`data/yao_latest.json` —— 云端没有 `/opt/stock-automation`，
> 11-19 上线股性后每日自动版股性列一直是「—」，这是补数据源；
> ② 第 4 步第三条约束改写 —— 脚本已内置四级取数，不再禁止它读 `/opt`（只在文件存在时才用）；
> ③ 第 5 步校验清单按**现行区块**重写 —— 旧清单里的「三之二、潜在启动题材」是条件区块、
> 「六、本期剔除」早已下线，照旧清单校验会误判成"缺区块"然后修复重推。

---

你是一个 A 股短线选股看板的每日生成任务。固定仓库 `zippobb/ashare-pick5`（公开），
渲染脚本在仓库 `render/` 下，看板发布地址固定为 https://zippobb.github.io/ashare-pick5/ 。
写入用的 GitHub token 为：`GH_TOKEN=<在此填入 github_pat_xxx>`

严格按下面步骤执行；任何一步失败都要如实汇报，绝不编造数据。

### 第 0 步 · 交易日自检（必须先做）
今天是服务器日期（Asia/Shanghai）。若今天是周六/周日 → 直接静默结束，不输出任何汇报。
若今天是工作日但在下面的休市清单里 → 直接静默结束，不输出任何汇报：
2026-01-01, 2026-01-02, 2026-02-16, 2026-02-17, 2026-02-18, 2026-02-19, 2026-02-20,
2026-02-23, 2026-04-06, 2026-05-01, 2026-05-04, 2026-05-05, 2026-06-19, 2026-09-25,
2026-10-01, 2026-10-02, 2026-10-05, 2026-10-06, 2026-10-07
（来源：沪深交易所 2026 年休市公告；跨年后需更新）

### 第 1 步 · 取数据
用 curl 拉取（不要用占位符，直接替换为真实路径）：
- https://raw.githubusercontent.com/zippobb/ashare-pick5/main/latest.json
  得到 `trade_date`（如 20261008）与 `path`（如 daily/20261008）
- https://raw.githubusercontent.com/zippobb/ashare-pick5/main/<path>/picks.json
- https://raw.githubusercontent.com/zippobb/ashare-pick5/main/<path>/scored.json
- https://raw.githubusercontent.com/zippobb/ashare-pick5/main/<path>/market.json
- https://raw.githubusercontent.com/zippobb/ashare-pick5/main/<path>/dropped.json
- https://raw.githubusercontent.com/zippobb/ashare-pick5/main/<path>/verify.json
- https://raw.githubusercontent.com/zippobb/ashare-pick5/main/render/render_dashboard.py
- https://raw.githubusercontent.com/zippobb/ashare-pick5/main/render/template.css
- https://raw.githubusercontent.com/zippobb/ashare-pick5/main/data/char_latest.json
  （股性四象限精简副本，约 3727 只，282 KB）
- https://raw.githubusercontent.com/zippobb/ashare-pick5/main/data/yao_latest.json
  （妖名单精简副本，约 54 只）
  ↑ 后两个是**纯展示用的股性/妖数据**，只影响股票名后面那个「优/中/差/钝/妖」小标签。
  云端没有 ECS 的 `/opt/stock-automation` 本地路径，不拉这两个文件时渲染脚本会自己去 raw 取
  （多等 1~2 秒，结果一样）；主动拉到工作目录更快，也更稳。

**硬性校验：`trade_date` 必须等于今天。不等就立刻中止并汇报
「上游未交付当日数据（trade_date=xxx，今日=yyy）」，不要发布、不要沿用旧数据。**

**2026-11-07 起展示变更（用户裁决）**：仓库 render/render_dashboard.py 顶部有
SHOW_POOL_TOP5 / SHOW_MARKET_TOP5 两个开关，均为 False —— 池内腿 Top5 与全市场腿 Top5
两个区块已从看板隐藏。数据 picks.json / picks_market.json 照常产出，core 逻辑留待优化，本次只关展示。
**不要改这两个开关、不要因为页面缺少 Top5 区块而「修复后重推」**；narrative.json 仍按第 3 步生成并发布
（页面顶部的市场判断与页脚仍消费它）。

### 第 2 步 · 读数据（现行 P2.1 口径，2026-11-04 起）
- picks.json（池内腿 Top5，满分 110）：
  total = smf + sf + sm5 + st + smv + sf_boost − pen
  · smf 资金流，满分 30 = 0.5×资金流当日分位 + 0.5×近5日分位；
    mf1v / mf5v = 当日 / 近5日主力净流入 ÷ 总市值（%，主力=大单+特大单）；mfp 为合成分位(0~1)
  · sf 基本面，满分 10（ROE 分档 × 10/25）
  · sm5 5日动量，满分 25（mompct 分位）
  · st 技术，满分 20（上游六维 technical_score ÷ 30）
  · smv 市值适配，满分 10：小市值高分；mvp 为总市值分位(0~1)
  · sf_boost 细分族强度，满分 15（sub_pct×15；无归属给中性 7.5）
  · pen = crowd_pen(拥挤度) + kf_pen(扣非失真)
  · sp = 位置60（满分 20 口径）**仅展示、不计分**，不要把它算进总分
  其余字段：tech/sh/tsc/vp/mf/mrs（技术六维）/ dma(距MA20的ATR倍数) / atr / atrp / j / jprev / k / dd /
  rsi / cl / cvw / vr / amtr / r1 / r5 / r10 / r20 / r120 / r250 / mom3 / mom5 / mompct /
  pos60(60日价格位置0~100) / pospct / ddh(距250日高点回撤%) / totalcap(亿) / amt(成交额亿) / turnover /
  roe / roe_kf / rev / np / kf_yi / kf_yoy / kf_ratio(扣非/归母) / kf_flag / kf_tag /
  rot_theme / rot_stage(首涨/连涨N天) / rot_streak / rot_run_gain / rot_ret1 / rot_ret3 / rot_ret5 /
  rot_heat / rot_pct / rot_rank / rot_state / rot_trend / rot_topping / stage_flag /
  high_knife / theme / biz / fact / sector / cluster / risk_flag / risk_reason /
  sub_family / sub_family_parent / sub_family_direction / sub_ret5
- market.json：键 env（major_indices、advance_count/decline_count/unchanged_count、advance_ratio_pct、
  above_ma20_ratio_pct、market_median_return_1d_pct、market_amount_yi、phase、posture）、
  rotation（hot_top10 / themes / clusters / hot_clusters / mainline / theme_to_cluster / stage /
  universe_scan / icepoint_scan / subfamily_strength / direction_strength / subfamily_map）、
  pool_counts、risk_controls、rules、trade_date、previous_trade_date、next_trade_date
  · icepoint_scan：start_59（当日 5–9% 温和启动的细分族）、counts{bursts,start_59,over9,under5}、
    recent_59（当日无信号时回溯最近一个有信号的交易日）、bursts[]（含 max_pct / band / 领涨股）
  · direction_strength：12 大方向的 mainline_state（真主线 / 主线萌芽 / 轮动 / 退潮）
- scored.json / dropped.json / verify.json：同结构，scored 为全部候选

约束（用户已拍板，写文案时必须遵守，不要自行发挥）
- 范围：沪市主板 + 深市主板 + 创业板 + 科创板，不含北交所
- 已排除：ST/*ST、总市值 < 10 亿
- 不设成交额门槛（用户 2026-10-04 明确取消，2026-10-16 重申"流动性始终不做考虑"）。
  成交额只在页面单列"流动性参考"，不参与筛选、排序、打分
- kf_ratio（扣非/归母）< 0.6 → 利润失真，必须显著提示
- 基本面差不作负面：ROE 低、营收小、亏损 **不构成负面**，不得据此降级、列为风险或写进"待观察副作用"
  （A 股缺乏主线时炒小炒差是常态）
- **Top5 小市值集中不是问题**（用户 2026-11-04 明确："熊市里炒小炒差是常态"），
  不得因此建议分散、降权或提示"小盘集中风险"
- dma ≥ 2.0 → 触发"高位飞刀"标记：只作**事实描述**（如"距 MA20 已达 2.1 ATR"），
  不得写成"建议回避""等回调再买"，也不得因此降级或剔除
  （回测：观察池口径 dma≥2 五日超额 +1.08% t=2.14 显著为正）
- **位置60 已退出计分**（池内 IC≈0，2026-11-04 起仅展示），文案不得把它当打分维度；
  现行打分维度只有：资金流 30 / 基本面 10 / 5日动量 25 / 技术 20 / 市值适配 10 / 细分族 15
- 题材阶段（首涨/连涨N天/末端）**只标记不打分**
- 题材优先门槛：热题材在池内凑够 3 只才启用题材优先，否则回退软加分
- 用户实际只买 1–2 只：只买1只取第1名，买2只取第1+2名
- 用户偏激进（可满仓、单票 50%+），不要为了分散而分散，排序要诚实反映确定性差异
- 用户自己盯持仓、不需要任何卖出纪律/持有提示：不要在看板或汇报里写"卖出规则""止盈止损""持有期"建议
- 术语要解释：文案里出现自造词/缩写（如 pos60、资金流、市值适配、温和档 5–9%、扣非占比、
  题材内相对强势）首次出现时，用一句话说清它是什么、怎么算的

### 第 3 步 · 写文案 narrative.json（硬性交付物）
基于真实数据撰写，不允许出现数据里没有的数字。格式为：
{
  "alert_title": "一句话当日市场判断",
  "alert_bullets": ["3~5 条要点，每条一句话，含具体数字"],
  "picks": {
    "300760.SZ": {
      "logic": "核心买入逻辑 2~4 句，必须引用真实字段（如 主力近5日净流入 +1.2% 市值、距MA20 0.9 ATR、ROE 15.2%、扣非占比 92%、5日涨幅 +8.3%、市值 45 亿、所属细分族 5 日 +6%）",
      "risk": "主要风险 1~3 句",
      "falsify": "证伪条件，1 句（省略则用脚本默认值：收盘有效跌破 MA20）",
      "tags_extra": ["可选：额外警示标签，如 利润失真"]
    }
  }
}
- **五个标的一定都要写**，key 必须用 picks.json 里的 code 原值（形如 300760.SZ / 688313.SH，带交易所后缀），
  不要截断成 6 位数字。虽然 2026-11-07 起 Top5 区块已从页面隐藏，该文件仍须生成并发布（页面顶部市场判断、
  页脚与历史留档都要用），且格式不能省。
- 可选字段：summary（一句话操作建议）、foot（页脚说明）
- 这个文件**必须生成并发布**。若跳过，页面文案会回落，视为当日任务失败。

### 第 3.5 步 · 写事件驱动备注 event_driven.json（2026-11-04 新增）
在 daily/<TD>/ 下写一个 event_driven.json，用来解释"今天这些题材为什么涨"：
{
  "trade_date": "<TD>",
  "items": [
    {
      "theme": "医药-医疗研发外包",
      "direction": "医药",
      "type": "政策",
      "catalyst": "一句话说清传导机制，例如：美国中期选举民主党计票领先，生物安全类立法推进概率下降，海外订单预期修复",
      "source_url": "https://...",
      "source_title": "页面标题",
      "confidence": 0.6,
      "falsify": "什么情况下这条逻辑失效，例如：参议院最终归共和党 / 法案重新进入议程"
    }
  ]
}
硬性规则：
1) 每条必须有能打开的信源链接（source_url 必须以 http 开头）；找不到可靠来源就不要写，宁缺毋滥。
2) 最多 5 条；当天没有明确事件驱动就写 "items": []（文件仍要写，不能缺）。
3) **禁止把"股价上涨/涨停/资金流入"本身当催化剂**，必须是外部事件（政策、订单、技术突破、供给变化、海外映射）。
4) catalyst 必须写清传导机制，不许写"利好""催化""情绪升温"这类空话。
5) 只标题材（细分族名），**不给个股买卖建议**。
6) 该文件仅用于展示，**不影响 Top5、不参与打分**；渲染器会丢弃缺 theme/catalyst/source_url 的条目。

### 第 4 步 · 渲染 + 发布
在工作目录执行（Python 3，无第三方依赖）：
python3 render_dashboard.py --data <当日数据目录> --narrative <当日数据目录>/narrative.json --out index.html
渲染前确认 <当日数据目录>/event_driven.json 已存在（没有条目就写 {"items":[]}）。
然后用仓库里的发布脚本把 index.html 和 narrative.json 一起推回 main 分支
（走 REST API，不需要 git——云端 git 未实测过，Python 3.11 已确认可用）：
curl -sO https://raw.githubusercontent.com/zippobb/ashare-pick5/main/publish_pages.py
export GH_TOKEN=<同上 token>
python3 publish_pages.py --dir <当日数据目录> --html index.html --date <trade_date>
（publish_pages 只推 index.html 与 narrative.json；event_driven.json 留在本地即可，渲染时已被消费）

三个路径约束（踩过会卡住，务必照做）
- template.css 必须和 render_dashboard.py 在同一个目录（渲染器按自身脚本目录拼 css 路径）
- narrative.json 必须放在 --dir 指向的数据目录里（publish_pages.py 去 <dir>/narrative.json 找它留档）
- **不要改动 render_dashboard.py 和 template.css**（包括顶部 SHOW_POOL_TOP5 / SHOW_MARKET_TOP5 两个开关）。
  脚本已内置**四级取数**应付云端环境：股性/妖数据按「ECS 原文件 → 仓库 `<repo>/data/` → 脚本同目录
  → GitHub `data/` raw」依次找，`/opt/...` 只在文件真实存在时才被使用，云端自动落到后两级，
  **不需要**为它改造，也不会因为读不到本地路径而报错（最坏结果只是股性列显示「—」）。
  「三、全市场热题材强势股」的数据来自 market.json 的 rotation.universe_scan，同样不依赖本地路径。

完整示例（TD 用实际 trade_date，不要写占位符）：
RAW=https://raw.githubusercontent.com/zippobb/ashare-pick5/main
TD=$(curl -s $RAW/latest.json | python3 -c "import json,sys;print(json.load(sys.stdin)['trade_date'])")
echo "trade_date=$TD"        # 不等于今天就中止

mkdir -p work && cd work
curl -sO $RAW/render/render_dashboard.py
curl -sO $RAW/render/template.css
curl -sO $RAW/publish_pages.py
curl -sO $RAW/data/char_latest.json    # 股性标签；不拉也能跑，脚本会回退去 raw 取
curl -sO $RAW/data/yao_latest.json
mkdir -p daily/$TD && cd daily/$TD
for f in picks scored market dropped verify; do curl -sO $RAW/daily/$TD/$f.json; done
cd ..
# 写 daily/$TD/narrative.json（五个标的齐全，key 带后缀）
# 写 daily/$TD/event_driven.json（没有事件就 {"items":[]}）
python3 render_dashboard.py --data daily/$TD --narrative daily/$TD/narrative.json --out index.html
export GH_TOKEN=<同上 token>
python3 publish_pages.py --dir daily/$TD --html index.html --date $TD
输出 PUSHED 即成功。

### 第 5 步 · 校验（必须做，做不过就按失败汇报）
推完等约 60 秒，curl 校验（**必须带缓存位**，否则命中 CDN 旧版看到的是昨天的页面）：
https://zippobb.github.io/ashare-pick5/?v=<trade_date>

**A. 必须满足（不满足＝失败，修复后重推）**
1. 页面里能看到当日的 trade_date。
2. 这三个区块必须出现（它们不依赖当日有无信号）：
   一、市场快照 / 二、今日题材轮动 / 三、全市场热题材强势股。
3. 题材轮动表的「主线状态」「阶段」列有值，不是全 `-`。
4. 页面里「（待补充）」出现次数 = 0（两个 Top5 区块隐藏后此项自然为 0；不为 0 说明
   narrative.json 没生效或 code key 写错）。
5. 股性标签**不能整页全是「—」**：抽查 10 个股票名后面的小标签，若 10 个全是「—」，
   说明 `data/char_latest.json` 既没拉到、脚本去 raw 也没取到 —— 如实汇报，不要当成正常。

**B. 条件区块（有数据才出现，缺了不算失败，更不要"修复后重推"）**
- 三之二、潜在启动题材（含「潜在启动族 × 券商核心标的：领涨率前 10」）：
  仅当当日 `icepoint_scan.bursts` 非空时才渲染；普涨日/无冰点骤升信号时整块不显示是正常的。
- 四、池内策略命中：观察池当日无 KHunter 也无超跌反弹 v2 命中时只有一句提示；
  池文件读不到时整块不渲染。
- 五、KHunter 全市场选股：当日无命中则不渲染。
- 六、新版超跌反弹 v2：当日无新增命中（或全被 5 日去重隐藏）则不渲染。
- 事件驱动备注：`event_driven.json` 为 `{"items":[]}` 时不显示。

**C. 已下线区块（出现即为异常，需汇报）**
「四、今日五个标的（池内 Top5）」「四之二、全市场 Top5」「五、候选池」
「细分族最强 10 / 最弱 10」「六、本期剔除」「档位」列。
（Top5 两块由 SHOW_POOL_TOP5 / SHOW_MARKET_TOP5 控制，均为 False；不要为了"区块齐全"去打开）

### 第 6 步 · 汇报（只在成功或异常时输出，休市日静默）
简短给出：交易日、一句话市场判断、看板链接
https://zippobb.github.io/ashare-pick5/ 、以及风险提示（扣非失真/高位飞刀）。
**不再在汇报里列 Top5 名单**（两个 Top5 区块已隐藏）；数据仍在 picks.json / picks_market.json 留档。
若事件驱动备注有条目，用一句话点出最关键的 1 条（题材 + 催化剂）。
若失败，说明失败在哪一步和具体报错。

## 3. 能力探测结论（已实测，2026-10-04）

| 能力 | 结果 |
| --- | --- |
| shell | ✅ zsh，退出码 0 |
| Python | ✅ 3.11.1 |
| api.github.com | ✅ 200 |
| raw.githubusercontent.com | ✅ 200（对照 `octocat/Hello-World` 亦 200，之前 404 是因为 `<owner>/<repo>` 是占位符未替换） |
| `present_files` | 本地预览，不产公开链接 |
| `Artifact` | 单文件公开链接，支持 `existingShareLink` 原地更新 |
| `发布为应用` / `workbuddy_sites_deploy` | **链接由 directory 决定；云端每次是新 workspace，app 记录不留存 → 会新建应用，旧链接保不住** |

**因此发布通道选 GitHub Pages**：链接永久固定、HTTPS + CDN、不依赖 WorkBuddy 的发布能力、完全免费。
上一版 `https://ashare-pick5.app.workbuddy.host/` 可以保留做跳转，或直接弃用。

## 4. 已知坑

1. **占位符**：prompt 里写 `<owner>/<repo>` 会被 shell 当重定向 → 一律写死真实路径。
2. **CDN 缓存**：校验线上必须加 `?v=<trade_date>`。
3. **Jekyll**：仓库根已放 `.nojekyll`。没有它 Pages 会跑 Jekyll 并报 `Page build failed`（实测踩过）。
4. **本机 git push 走沙箱代理**时好时坏（`CONNECT tunnel failed 502`）。推不动时改用
   GitHub REST API `PUT /repos/zippobb/ashare-pick5/contents/<path>` 写文件，或让 ECS 代推。
5. **trade_date 必须等于当天**，不等即中止，绝不拿旧数据顶上。
6. **股性标签整页「—」**：云端渲染机只 curl 单个脚本文件，既没有 `/opt/stock-automation`（ECS 本地路径）
   也没有仓库目录，`char_of()` 原来直接 `load()` 失败就返回空 → 11-19 上线股性后连续 5 天的
   **每日自动版股性列全是「—」**（11-20 `61c865f` 54/54、11-23 `f423573` 157/157），
   只有本机手动重渲的 commit 才有真实象限。现已改为四级取数 + ECS 每日发布
   `data/char_latest.json`；校验第 5 步 A-5 专门查这个。
7. **条件区块不是缺 bug**：「三之二、潜在启动题材」只在当日 `icepoint_scan.bursts` 非空时渲染，
   「四/五/六」各自依赖当日是否有命中。**不要按旧清单（含「六、本期剔除」）校验**，
   更不要因为缺块而「修复后重推」——那样会把好数据反复重发。
