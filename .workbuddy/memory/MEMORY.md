# LOF套利监测系统 - 项目约定

## 项目结构
```
LOFnotice/
├── main.py              # FastAPI后端（端口8877）
├── fetcher.py           # 数据获取模块
├── jisilu.py            # 集思录数据抓取（agent-browser）
├── requirements.txt     # Python依赖
├── static/
│   └── index.html       # 前端页面
├── .jisilu_data.json    # 集思录缓存数据
└── .workbuddy/memory/   # 项目记忆
```

## 环境配置
- Python: `C:/Users/Administrator/.workbuddy/binaries/python/envs/lofnotice/Scripts/python.exe`
- 默认使用模拟数据（`LOF_USE_MOCK=1`）
- 切换真实数据：`LOF_USE_MOCK=0`

## 启动命令
```bash
cd /h/trybuddy/LOFnotice
C:/Users/Administrator/.workbuddy/binaries/python/envs/lofnotice/Scripts/python.exe main.py
```

## 数据流
1. fetcher.py 从东方财富/集思录/AKShare/天天基金获取数据
2. 集思录用 agent-browser 无头浏览器登录（账号 xiaoyucun9999），数据缓存在 .jisilu_data.json
3. main.py FastAPI提供REST API
4. index.html前端轮询API刷新展示
5. 缓存5分钟，避免频繁请求外部API

## 集思录集成
- 接口：`https://www.jisilu.cn/data/lof/index_lof_list/`（股票LOF标签页）
- 登录方式：agent-browser CLI 无头浏览器（Playwright 不可用，沙箱限制）
- apply_status 覆盖东财的"未知"：开放申购/暂停申购/限大额 等具体值
- 数据刷新：需在 Bash 中运行 agent-browser 命令登录并抓取
- 已知局限：当前仅抓取"股票LOF"标签页，QDII/指数LOF需单独抓取

## 套利筛选条件（2026-07-03 重大修复）

### 溢价率计算（核心变更 2026-07-03）
- **之前（有Bug）**: 溢价率 = (场内价 - 昨天已公布净值dwjz) / dwjz
  - 问题：申购按当天收盘净值执行，但用昨天净值算溢价，估值滞后导致虚高
  - 验证：7/3修复前显示3只溢价>3%，修复后=0只（大盘涨了，真实溢价被吃掉）
- **现在（修复后）**: 溢价率 = (场内价 - 盘中估算净值est_nav) / est_nav ★
  - 天天基金 gsz 字段 = est_nav，基于持仓成分股实时涨跌估算当日净值
  - 过滤时优先使用 premium_rt_est，无est_nav时降级使用 premium_rt_verified(dwjz)
  - 前端展示同时显示两者：实时溢价(est)和昨天溢价(dwjz)做对比
- 溢价阈值：绝对值 > 3%
- **有色/资源类LOF（名称含"有色""资源""大宗商品""煤炭"等）溢价阈值提升至 > 5%**
- **成交额 ≥ 1000万元**（社区共识底线）
- **基金规模 ≥ 2亿元**（防清盘风险，从东财基金详情页HTML解析）
- 申购状态非"暂停申购"
- 日限额 >= 100元或无限额
- **auto_buy 买入使用 est_nav 估算成本（buy_nav=est_nav），成交额阈值已修正为 >=1000万**

## 数据源优先级（2026-07-01 更新）
1. 东方财富(curl) — 主力，字段最全
2. 集思录API — 备用1，需登录
3. 腾讯财经(qt.gtimg.cn) — 备用2，免代理
4. **新浪财经(hq.sinajs.cn) — 备用3，新增，免代理，社区常用**
5. AKShare — 备用4
6. 本地集思录缓存 — 兜底

## 基金规模获取（2026-07-01 新增，07-29 增强）
- 从东财基金详情页HTML解析：`规模</a>：XX.XX亿元（YYYY-MM-DD）`
- 每批次并发5只，最多检查前30只（按成交额排序）
- 规模未知的不做过滤（不误杀），仅对已知规模应用≥2亿限制
- **07-29新增**：东财缺失的用新浪f_接口返回的规模(亿)兜底（fund_size_sina）

## 净值核实多源架构（2026-07-29 重大修复）
- **天天基金fundgz估值接口已下线**（返回404页面，非限流）→ 7/21起0/60失败的根因
- verify_navs_batch 三级降级：探测fundgz(1只探针) → 存活则逐只(含est_nav盘中估值) → 失败的用新浪 `hq.sinajs.cn/list=f_<code>` 批量兜底（50只/请求，GBK，需Referer: finance.sina.com.cn）
- 新浪f_返回：`名称,单位净值,累计净值,前日净值,净值日期,规模(亿)`，无盘中估值
- est_nav兜底：enrich_with_lof8 用 lof8.cn 的 estNav 补盘中估算净值+premium_rt_est
- **潜伏bug修复**：filter_arbitrage_opportunities 的 `**item` 曾放字典末尾，原始premium_rt(0)覆盖计算值 → 已移到最前

## 已确认的案例
- 160644 港美互联网LOF: 2026-05-27鹏华公告, 5月28日起暂停申购 → 套利通道关闭
- 160140 美国REIT精选LOF（模拟）: 曾有暂停，后发恢复申购公告 → resume信号

## 公告风险等级
- `safe`: 公告无异常
- `warning`: 有限额调整，需确认
- `danger`: 有暂停/停牌公告 → 过滤
- `resume`: 检测到恢复申购/复牌 → 保留并提示手动确认

## 用户偏好
- 只监控溢价套利（目前）
- 需要网页展示
- 后续需要公告解析确认限购变化
- **每日报告中不显示"已实现收益率"行**：因为投入资金滚动复用后分母失真，该指标无意义（2026-06-26确认）

## 盈亏跟踪系统 (trade_tracker.py)
- 文件：`.trades.json`（JSON持久化）
- 逻辑：每日检测到符合条件（溢价>3%+开放申购+成交>1万）→ 自动模拟买入￥5000
- 费用：申购费0.15% + 卖出佣金0.01%
- T+2日到账后可卖，以实时价格计算浮盈亏
- API端点：`GET /api/trades`(持仓)、`GET /api/trades/sell/{code}`(卖出)、`POST /api/trades/clear`(清空)
- 同一天同一基金不重复买入

## 申购状态解析（已修复 2026-06-16）
- 东财API不返回申购状态 → 从基金详情页HTML解析
- HTML字段名："交易状态"（不是"申购状态"）
- 格式："交易状态：暂停申购 （单日累计购买上限10.00元） 开放赎回"
- 标准化：开放申购/暂停申购/封闭期(定开)/限大额
- 备选：集思录 apply_status（需手动更新.jisilu_data.json）

## 卖出价格获取（已修复 2026-06-22）
- **问题**: sell接口只在当日screened数据中查找价格，T+2到期的持仓若已不在筛选列表中则无法卖出
- **修复**: main.py sell_position增加腾讯财经API兜底 → `_fetch_price_from_tencent(code)` 
- 先查缓存opportunities，找不到则调用qt.gtimg.cn获取实时价

## 每日自动化惯例（2026-06-22确认）
- 每日14:40定时执行：刷新数据 → 自动买入所有可申购品种 → 自动卖出T+2到期品种
- 买入/卖出均在 main.py refresh_cache() 中自动完成，无需手动调用
- 买入后输出：[买入] + 品种详情；T+2到期自动：[卖出] + 盈亏
- 同一天同一基金不重复买入；不在当日数据中的持仓从腾讯财经获取价格卖出

## 东财API问题（2026-06-20诊断）
- **根因**: Python OpenSSL TLS指纹被东财服务器拦截；系统代理封堵push2.eastmoney.com
- **修复**: 新增腾讯财经API(qt.gtimg.cn)作为实时行情主源，免代理直连
- **数据优先级**: 东财(curl) → 集思录API → 腾讯财经(新) → AKShare → 本地缓存
- **注**: 周六非交易日，数据为周五收盘价+最新净值，属正常

## 日报输出格式（2026-07-08 确认）
- **文字叙事为主，少用表格**
- 免责声明放报告开头第一行
- 结构：数据概况 → 筛选结果 → 落选品种分析（原因解释）→ 交易情况 → 累计统计 → 一句话总结
- 风格：平实口语化，像在跟朋友说今天盯盘看到了什么
- **所有基金必须标注代码**：首次提及写"名称（代码）"，后续可用简称但同样带代码

## 自动卖出BUG（已修复 2026-06-25）
- **问题**: refresh_cache 中 auto-sell 逻辑在 `if result.get("data"):` 块内
- 当 result["data"] 为空（0机会）时，T+2到期的持仓不会被自动卖出
- 影响：6/24 16笔到期持仓未自动平仓
- **已修复**: auto-sell 已移到 `if result.get("data"):` 块外面，独立执行
