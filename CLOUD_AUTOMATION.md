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

云端要把渲染好的 `index.html` 推回仓库，必须有一枚 token。**推荐 fine-grained PAT**（最小权限，随时可删）：

- https://github.com/settings/personal-access-tokens → Generate new token
- Resource owner: `zippobb` → **Only select repositories: `ashare-pick5`**
- Permissions → Contents: **Read and write**（只勾这一项）
- 生成后把 `github_pat_xxx` 填到第 2 节 prompt 的 `GH_TOKEN=` 处

> 不要用本机 `~/.git-credentials` 里那枚 `gho_` classic token 写进云端 prompt——它是全作用域的，泄露即等于账号权限外泄。

## 2. 云端自动化 prompt（整段复制）

排期：每工作日 19:30（Asia/Shanghai）。

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

**硬性校验：`trade_date` 必须等于今天。不等就立刻中止并汇报
「上游未交付当日数据（trade_date=xxx，今日=yyy）」，不要发布、不要沿用旧数据。**

### 第 2 步 · 读数据（理解字段）
- `picks.json`：Top5 数组。字段 `code/name/seg/close/chg/total/sp(位置安全35)/sf(基本面30)/sb(逻辑纯度20)/st(技术15)/tech/sh/tsc/vp/mf/mrs/dma(距MA20的ATR倍数)/atr(ATR14占股价%)/j/jprev/rsi/cl/cvw/vr/amtr/r5/r20/totalcap(亿)/amt(成交额亿)/roe/rev/np/kf_yi/kf_ratio/kf_flag/high_knife/theme/biz/fact`
- `market.json`：市场快照（`market_environment.major_indices`、涨跌家数、站上MA20比例、两市成交额、phase/posture、上一/下一交易日）
- `scored.json`：全部候选项（同结构，用于页面候选池表格）
- `dropped.json`：被剔除项及原因
- `verify.json`：逐标的行情/财务复核明细

约束（写文案时必须遵守）：
- 范围：沪市主板 + 深市主板 + 创业板 + 科创板，**不含北交所**
- 已排除：ST/*ST、总市值 < 30 亿
- **不设成交额门槛**（用户 2026-10-04 明确取消），成交额只在页面单列"流动性参考"，不参与打分
- `kf_ratio`（扣非/归母）< 0.6 → 利润失真，必须显著提示或降权
- `dma` ≥ 2.0 → 高位飞刀，必须提示
- 用户实际只买 1–2 只：**只买1只取第1名，买2只取第1+2名**
- 用户偏激进（可满仓、单票 50%+），不要为了分散而分散，排序要诚实反映确定性差异

### 第 3 步 · 写文案 narrative.json
基于真实数据撰写，**不允许出现数据里没有的数字**。格式为：

```json
{
  "alert_title": "一句话当日市场判断",
  "alert_bullets": ["3~5 条要点，每条一句话，含具体数字"],
  "picks": {
    "688313": {
      "logic": "核心买入逻辑 2~4 句，必须引用真实字段（如 距MA20 0.9 ATR、ROE 15.2%、扣非占比 92%、5日涨幅 +8.3%）",
      "risk": "主要风险 1~3 句",
      "falsify": "证伪条件，1 句（省略则用脚本默认值：收盘有效跌破 MA20）",
      "tags_extra": ["可选：额外警示标签，如 利润失真"]
    }
  }
}
```
五个标的一定都要写（key 用 `code` 原值）。

### 第 4 步 · 渲染 + 发布
在工作目录执行（Python 3，无第三方依赖）：
```
python3 render_dashboard.py --data <当日数据目录> --narrative narrative.json --out index.html
```
然后把 `index.html` 推回仓库 main 分支根目录，**commit message 写 `dashboard <trade_date>`**：
```
git clone https://x-access-token:$GH_TOKEN@github.com/zippobb/ashare-pick5.git repo
cd repo && cp <index.html> index.html
git -c user.email=cloud@workbuddy -c user.name=pick5-cloud add index.html
git -c user.email=cloud@workbuddy -c user.name=pick5-cloud commit -m "dashboard <trade_date>"
git push origin main
```
同时把 `narrative.json` 放到 `daily/<trade_date>/narrative.json` 一并提交（便于留档）。

推完等约 60 秒，用 curl 校验（**必须带缓存位，否则命中 CDN 旧版**）：
`https://zippobb.github.io/ashare-pick5/?v=<trade_date>` 返回 200 且内容里能看到当日的 trade_date。

### 第 5 步 · 汇报（只在成功或异常时输出，休市日静默）
简短给出：交易日、Top5（名称/代码/总分/收盘涨跌）、一句话市场判断、看板链接
https://zippobb.github.io/ashare-pick5/ 、以及风险提示（扣非失真/高位飞刀/低成交额）。
若失败，说明失败在哪一步和具体报错。

---

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
