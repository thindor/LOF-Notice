# Changelog

## 2026-07-29

### 重大修复：净值核实多源架构（新浪基金兜底）

**背景**：天天基金 fundgz 估值接口于 2026-07 下线（返回 404 页面，非限流），导致 7/21 起净值核实连续多日 0/60 全部失败，系统无法计算溢价、无法产生任何交易信号。

- `verify_navs_batch` 重构为三级降级：
  1. 先用 1 只探针探测天天基金 fundgz 接口是否存活
  2. 存活则走原逐只核实路径（含 est_nav 盘中估值）
  3. 失败的批量切换新浪基金 `hq.sinajs.cn/list=f_<code>` 接口兜底（50只/请求，GBK编码，需 Referer: finance.sina.com.cn）
- 新浪 f_ 接口返回单位净值/净值日期/规模，无盘中估值 → `enrich_with_lof8` 新增用 lof8.cn 的 estNav 补盘中估算净值和 premium_rt_est
- 基金规模增强：东财详情页缺失的用新浪返回的规模(亿)兜底（`fund_size_sina`）
- `auto_buy_opportunities` 收紧：只在有实时估值溢价（premium_rt_est）时才触发买入，避免用昨日净值溢价误买
- 修复潜伏 bug：`filter_arbitrage_opportunities` 中 `**item` 展开位置导致原始 premium_rt(0) 覆盖计算值

**验证**：7/29 15:46 刷新，净值核实 60/60 全部成功（连续一周 0/60 后首次满血恢复）。

**改动文件：**
- `fetcher.py` — 新增 `_fetch_navs_from_sina`、`_apply_nav_result`，重构 `verify_navs_batch`，增强 `enrich_with_lof8`
- `trade_tracker.py` — `auto_buy_opportunities` 溢价来源收紧为 premium_rt_est

## 2026-07-03

### 重大修复：溢价率计算改用盘中估算净值

- 溢价率从「场内价 vs 昨日已公布净值」改为「场内价 vs 盘中估算净值(est_nav)」，消除估值滞后导致的虚高溢价
- 接入 lof8.cn 数据源做申购状态覆盖和溢价交叉验证
- 有色/资源类 LOF 溢价阈值提升至 5%

## 2026-06-24

### 新增：LOF溢价详情快速跳转

- 表格中每只LOF的代码列改为可点击链接，点击直接跳转东方财富基金详情页查看溢价情况
- 操作列的链接文案从"Link"改为"公告"，语义更清晰

**改动文件：**
- `static/index.html` — 代码列加 `<a>` 链接到 `https://fundf10.eastmoney.com/{code}.html`
