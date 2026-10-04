# 每日选股数据管线（线上版）

把原先跑在本机的「拉数 → 复核 → 打分」搬到 ECS，看板仍由本地 WorkBuddy 生成发布。

## 目录

| 文件 | 作用 |
| --- | --- |
| `pipeline.py` | 主程序：交易日校验 → 同步上游仓 → 等 manifest → 复核 → 扣非核查 → v3 打分 → Top5 → LLM 点评 → 存档 |
| `config.json` | 运行配置（由 `config.example.json` 复制后改） |
| `market_calendar.json` | 休市日历，非交易日直接跳过 |
| `run_daily.sh` | cron 入口，写日志到 `logs/YYYYMMDD.log` |
| `aliyun_api.py` | 极简阿里云 OpenAPI 客户端，用于云助手下发命令（本机操作 ECS 用） |
| `fetch_remote.sh` | 本地侧从 ECS 拉结果 |

零第三方依赖，Python 3 标准库即可运行。

## 两种取数模式

1. **ECS 本地源（推荐）**：填 `local_bundle_path` + `local_manifest_path`，直接读上游输出文件，全程不联网拉 GitHub，`repo_url` 留空。
2. **GitHub 私有仓**：留空 local_* 字段、填 `repo_url`（含 PAT），走 `git pull` 增量同步。

## ECS 部署步骤

```bash
mkdir -p /root/ashare_pipeline && cd /root/ashare_pipeline
# 1) 上传 pipeline.py / config.json / market_calendar.json / run_daily.sh
# 2) 建 env.sh（chmod 600），写入敏感变量：
#      export DEEPSEEK_API_KEY=sk-xxxx
# 3) 首次手动跑一遍验证
FORCE_DATE=20260930 python3 pipeline.py
# 4) 配 cron（服务器时区若为 UTC，需换算：18:40 CST = 10:40 UTC）
(crontab -l 2>/dev/null; echo '40 18 * * 1-5 /root/ashare_pipeline/run_daily.sh') | crontab -
```

## 输出

`out/<YYYYMMDD>/`：`verify.json` `scored.json` `dropped.json` `picks.json` `comment.json`，
`out/latest` 为指向最新日期目录的软链。

## 硬规则（与本地一致）

1. `manifest.trade_date` 必须等于目标交易日，否则不产出结果（最多等 90 分钟）。
2. 非交易日（周末 + `market_calendar.json` 休市日）静默退出。
3. 排除：北交所、ST/\*ST、总市值 < 30 亿、成交额 < 3 亿。
4. 扣非净利 / 归母 < 60% 标记「利润失真」，交由 LLM 降权或否决。
