# 云端自动化剧本（复制粘贴到 WorkBuddy 小程序端的自动化 prompt）

> 目标：ECS 出数据 → GitHub → 云端 WorkBuddy 生成看板并发布。**本机不再需要开机。**
> 首次启用前建议先跑一次「能力探测」（见文末），确认云端具备 bash + 网络 + 发布能力。

## 一、仓库与路径约定

| 项 | 值 |
| --- | --- |
| 结果仓库 | 由 ECS 推送，含 `latest.json` 与 `daily/<YYYYMMDD>/*.json` |
| 脚本来源 | render_dashboard.py / template.css 存放在同一仓库的 `render/` 目录 |
| raw 地址 | `https://raw.githubusercontent.com/<owner>/<repo>/main/...`（私有仓需 `{ 'Authorization': 'token <PAT>' }`） |

## 二、自动化 prompt 全文

```
你是 A 股短线选股助手，负责把云端数据渲染成每日看板并发布。严格执行以下步骤：

【第 0 步 · 交易日校验】
今天是 {TODAY}。若为周六/周日，或属于以下 2026 年休市日清单，立即结束，不输出任何汇报、不发布任何内容：
2026-01-01,2026-01-02,2026-02-16,2026-02-17,2026-02-18,2026-02-19,2026-02-20,2026-02-23,
2026-04-06,2026-05-01,2026-05-04,2026-05-05,2026-06-19,2026-09-25,
2026-10-01,2026-10-02,2026-10-05,2026-10-06,2026-10-07

【第 1 步 · 取数】
REPO=<owner>/<repo>; TOKEN=<PAT，公开仓库可留空>
TD=$(date +%Y%m%d)
取 https://raw.githubusercontent.com/$REPO/main/latest.json，确认其 trade_date == $TD。
若不等：ECS 尚未产出，等待 10 分钟后重试一次；仍不等则结束并在汇报里说明"ECS 未产出"。
取得后下载以下文件到 ./$TD/：market.json picks.json scored.json dropped.json
（路径 daily/$TD/<文件名>）

【第 2 步 · 分析与文案】
下载 render/render_dashboard.py 与 render/template.css。
阅读 market.json / picks.json / scored.json，自己完成分析并写出 ./$TD/narrative.json，结构：
{
  "generated_at_label": "YYYY-MM-DD HH:MM",
  "alert_title": "⚠ 市场警戒：<阶段 / 姿态> —— <一句话>",
  "alert_bullets": ["基于市场快照的 2-4 条要点，可含 <b> 标签"],
  "picks": {"<code>": {"tags_extra": [], "logic": "<买入逻辑>", "risk": "<风险>", "falsify": "<证伪条件>"}},
  "summary": "<给用户的操作提示，含只买1只/买2只建议>"
}
硬性要求（必须遵守）：
1. 扣非净利 / 归母 < 60% 视为利润失真，必须降权或否决，并在 tags_extra 标注。
2. 距 MA20 ≥ 2.0 ATR 视为高位飞刀，明确提示、不得推荐。
3. 题材标签为"待核验"的，不得作为核心买入理由。
4. 成交额不参与打分——不要因为成交额小就把票往后排，但要在 risk 里提示进出需分批。
5. 分数高 ≠ 有短线动能，必须结合 J 值、量比、收盘位置(cls)、相对 5 日/20 日涨跌做二次判断。
6. 排名即优先级，可人工干预，但必须在 tags_extra 写明理由（如"人工上调 1 位"）。

【第 3 步 · 渲染】
python3 render_dashboard.py --data ./$TD --narrative ./$TD/narrative.json --out ./index.html
若渲染失败，把错误原文附在汇报里并结束，不要发布。

【第 4 步 · 发布】
调用 sites 发布能力发布当前目录（含 index.html）。
发布完成后，在汇报里给出：链接 + 一句班子构成建议（只买1只/买2只）+ 当日最高风险点。
汇报控制在 15 行以内。
```

## 三、首次能力探测（一次性任务）

新建一次性自动化，prompt 用下面这段，跑完把输出发我，据此判断方案是否成立：

```
依次执行并原样输出每一步的结果（成功/失败 + 原始输出前 20 行）：
1. uname -a 与 python3 -V（确认有无 shell 与 Python）
2. curl -s -o /dev/null -w "%{http_code}" https://api.github.com （确认能否联网访问 GitHub，期望 200）
3. curl -s https://raw.githubusercontent.com/<owner>/<repo>/main/latest.json （确认能否读到结果文件）
4. 列出当前会话可用的发布工具名称（是否有发布/部署类的工具 callable）
```

## 四、已知风险

1. **链接可能变化**：云端发布若新建了应用，旧链接 `ashare-pick5.app.workbuddy.host` 保不住。首次跑完要核对链接。
2. **私有仓需要 PAT**：写在 prompt 里（云端无本机凭据）。若不便，可把结果仓库设为公开，用 raw 免密读取。
3. **时区**：云端 cron 的时间基准需确认是 CST 还是 UTC，排期建议 19:30 CST（ECS 18:40 已产出）。
