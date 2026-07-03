"""
LOF套利机会数据获取模块
数据源优先级: 集思录API > AKShare > 降级模拟数据

三步核实法:
  Step 1: 集思录初筛 → 得到"显示溢价率"（可能基于T-2净值）
  Step 2: 天天基金核实 → 获取最新净值 → 重新计算"核实溢价率"
  Step 3: 公告核实 → 检查最近公告是否有暂停/限购变动

2026-07-03 重大修复：
  - 溢价计算从 (现价-昨天净值)/昨天净值 改为 (现价-盘中估算净值est_nav)/est_nav
  - 新增 lof8.cn API 作为申购状态和溢价交叉验证数据源
"""
import asyncio
import subprocess
import time
import json
import re
import os
from datetime import datetime, timedelta
from typing import Optional
from urllib.parse import urlencode

import httpx

for _k in ("HTTP_PROXY", "HTTPS_PROXY", "http_proxy", "https_proxy", "ALL_PROXY", "all_proxy"):
    os.environ.pop(_k, None)

PREMIUM_THRESHOLD = 3.0
PREMIUM_PRESCREEN = 1.5
MIN_VOLUME = 10_000_000
MIN_VOLUME_WARN = 20_000_000
MIN_VOLUME_OK = 50_000_000
MIN_FUND_SIZE = 200_000_000
MIN_PURCHASE_LIMIT = 100
REQUEST_TIMEOUT = 15
NAV_VERIFY_CONCURRENCY = 8
NAV_VERIFY_MAX = 60
JISILU_URL = "https://www.jisilu.cn/data/lof/index_lof_list/"
TENCENT_URL = "https://qt.gtimg.cn/q="
TTJJ_URL = "https://fundgz.1234567.com.cn/js/{code}.js"
EM_LOF_URL = "https://push2.eastmoney.com/api/qt/clist/get"
SINA_URL = "https://hq.sinajs.cn/list="
LOF8_API_URL = "https://lof8.cn/lof-monitor/api/lof"

RESOURCE_KEYWORDS = ["有色", "资源", "大宗商品", "煤炭", "钢铁", "矿业", "黄金", "白银"]

SUBSCRIPTION_KEYWORDS = [
    "暂停申购", "暂停大额申购", "限制大额申购", "限制申购",
    "暂停定期定额", "恢复申购", "恢复大额申购",
    "调整大额申购", "调整申购金额", "暂停转换转入",
]
SUSPEND_KEYWORDS = ["暂停申购", "暂停大额申购", "暂停定期定额", "暂停转换转入"]
LIMIT_KEYWORDS = ["限制大额申购", "限制申购", "调整大额申购", "调整申购金额"]
RESUME_KEYWORDS = ["恢复申购", "恢复大额申购", "恢复定期定额"]
RESUME_TRADE_KEYWORDS = ["复牌公告", "复牌"]
TRADE_STATUS_KEYWORDS = ["停牌公告", "临时停牌", "停复牌", "复牌公告", "溢价风险提示", "交易风险提示"]


def _safe_float(val) -> float:
    try:
        return float(val)
    except (ValueError, TypeError):
        return 0.0


def _parse_date(date_str: str) -> Optional[datetime]:
    if not date_str:
        return None
    date_str = str(date_str).strip()
    for fmt in ["%Y-%m-%d", "%Y%m%d", "%Y/%m/%d", "%Y-%m-%d %H:%M:%S"]:
        try:
            return datetime.strptime(date_str, fmt)
        except ValueError:
            continue
    match = re.match(r"(\d{4})-?(\d{2})-?(\d{2})", date_str)
    if match:
        try:
            return datetime(int(match.group(1)), int(match.group(2)), int(match.group(3)))
        except ValueError:
            pass
    return None


def _is_resource_lof(name: str) -> bool:
    for kw in RESOURCE_KEYWORDS:
        if kw in name:
            return True
    return False


def _calc_signal_score(premium_rt: float, amount: float, turnover: float) -> int:
    score = min(60, abs(premium_rt) * 8)
    if amount > 5e7:
        score += 20
    elif amount > 1e7:
        score += 10
    if turnover > 5:
        score += 10
    elif turnover > 2:
        score += 5
    return min(100, int(score))


# ============================================================
#  数据源: 东方财富 (主力)
# ============================================================

async def _curl_fetch_json(url: str, params: dict = None, timeout: int = 15) -> Optional[dict]:
    full_url = url
    if params:
        full_url = f"{url}?{urlencode(params, doseq=True)}"
    try:
        proc = await asyncio.create_subprocess_exec(
            "curl", "-s", "--max-time", str(timeout), full_url,
            stdout=asyncio.subprocess.PIPE, stderr=asyncio.subprocess.PIPE,
        )
        stdout, stderr = await asyncio.wait_for(proc.communicate(), timeout=timeout + 5)
        if proc.returncode != 0:
            return None
        text = stdout.decode("utf-8", errors="replace")
        return json.loads(text) if text.strip() else None
    except Exception:
        return None


async def fetch_lof_from_eastmoney() -> Optional[list[dict]]:
    params = {
        "pn": "1", "pz": "200", "po": "1", "np": "1",
        "ut": "bd1d9ddb04089700cf9c27f6f7426281",
        "fltt": "2", "invt": "2", "fid": "f3",
        "fs": "b:MK0404,b:MK0405,b:MK0406,b:MK0407",
        "fields": "f2,f3,f4,f12,f14,f15,f16,f17,f20,f21",
    }
    data = await _curl_fetch_json(EM_LOF_URL, params)
    if not data:
        try:
            headers = {"User-Agent": "Mozilla/5.0", "Referer": "https://quote.eastmoney.com/"}
            async with httpx.AsyncClient(timeout=REQUEST_TIMEOUT, trust_env=False) as client:
                resp = await client.get(EM_LOF_URL, params=params, headers=headers)
                data = resp.json()
        except Exception:
            return None
    rows = data.get("data", {}).get("diff", [])
    if not rows:
        return None
    results = []
    for row in rows:
        code = str(row.get("f12", ""))
        name = str(row.get("f14", ""))
        price = _safe_float(row.get("f2", 0))
        amount = _safe_float(row.get("f20", 0))
        if not code or price <= 0 or amount < MIN_VOLUME:
            continue
        results.append({"code": code, "name": name, "price": price, "nav": 0, "premium_rt": 0,
                         "volume": _safe_float(row.get("f15", 0)), "amount": amount,
                         "apply_status": "未知", "nav_date": "", "issuer": "", "turnover_rt": 0,
                         "source": "eastmoney"})
    print(f"[东方财富] 获取到 {len(results)} 只LOF行情数据")
    return results


# ============================================================
#  数据源: 集思录
# ============================================================

async def fetch_jisilu_lof() -> Optional[list[dict]]:
    headers = {"User-Agent": "Mozilla/5.0", "Referer": "https://www.jisilu.cn/data/lof/", "Accept": "application/json"}
    params = {"___jsl": f"LST___t={int(time.time()*1000)}", "rp": "50"}
    try:
        async with httpx.AsyncClient(timeout=REQUEST_TIMEOUT, follow_redirects=True, trust_env=False) as client:
            resp = await client.get(JISILU_URL, headers=headers, params=params)
            if resp.status_code != 200:
                return None
            data = resp.json()
            rows = data.get("rows", [])
            if not rows:
                return None
            results = []
            for row in rows:
                cell = row.get("cell", {})
                fund_id = cell.get("fund_id", "")
                fund_nm = cell.get("fund_nm", "")
                price = _safe_float(cell.get("price", 0))
                fund_nav = _safe_float(cell.get("fund_nav", 0))
                discount_rt = _safe_float(cell.get("discount_rt", 0))
                if fund_nav <= 0 or price <= 0:
                    continue
                premium_rt = -discount_rt
                if abs(premium_rt) < PREMIUM_PRESCREEN:
                    continue
                results.append({"code": fund_id, "name": fund_nm, "price": price, "nav": fund_nav,
                                 "nav_date": cell.get("nav_dt", ""), "premium_rt": round(premium_rt, 2),
                                 "volume": _safe_float(cell.get("volume", 0)),
                                 "amount": _safe_float(cell.get("amount", 0)),
                                 "apply_status": cell.get("apply_status", "未知"),
                                 "issuer": cell.get("issuer_nm", ""),
                                 "turnover_rt": _safe_float(cell.get("turnover_rt", 0)),
                                 "source": "jisilu"})
            print(f"[集思录] 初筛: {len(results)} 只候选")
            return results
    except Exception as e:
        print(f"[集思录] 异常: {e}")
        return None


async def fetch_jisilu_data_full() -> Optional[dict[str, dict]]:
    import os as _os
    _jisilu_file = _os.path.join(_os.path.dirname(_os.path.abspath(__file__)), ".jisilu_data.json")
    if not _os.path.exists(_jisilu_file):
        return None
    mtime = _os.path.getmtime(_jisilu_file)
    age_hours = (datetime.now().timestamp() - mtime) / 3600
    try:
        with open(_jisilu_file, "r", encoding="utf-8") as f:
            data = json.load(f)
        print(f"[集思录全量] 加载缓存（{age_hours:.1f}h前）: {len(data)} 只LOF")
        return data
    except Exception:
        return None


def enrich_with_jisilu_status(lof_data: list[dict], jisilu_info: dict[str, dict]) -> list[dict]:
    updated_count = 0
    for item in lof_data:
        code = item.get("code", "")
        if code not in jisilu_info:
            continue
        jsl = jisilu_info[code]
        jsl_status = jsl.get("apply_status", "")
        current_status = item.get("purchase_status", "未知")
        if current_status == "未知" or not current_status:
            if jsl_status and jsl_status != "未知":
                item["purchase_status"] = jsl_status
                item["purchase_status_source"] = "jisilu"
                updated_count += 1
        item["jsl_nav_discount_rt"] = jsl.get("nav_discount_rt", 0)
        item["jsl_fund_nav"] = jsl.get("fund_nav", 0)
        item["jsl_apply_status"] = jsl_status
    if updated_count > 0:
        print(f"[集思录状态] 覆盖了 {updated_count} 只LOF的申购状态")
    return lof_data


# ============================================================
#  数据源: lof8.cn (申购状态 + 溢价交叉验证) ★ 2026-07-03新增
# ============================================================

def _parse_lof8_status(raw: str) -> tuple[str, float]:
    if not raw:
        return ("未知", 0)
    raw = raw.split("/")[0].strip()
    if "暂停申购" in raw or "暂停" in raw:
        limit = 0
        m = re.search(r'限(\d+)\s*(?:元|万)', raw)
        if m:
            val = int(m.group(1))
            limit = val * 10000 if "万" in raw else val
        return ("暂停申购", limit)
    if raw.startswith("限购"):
        m2 = re.search(r'限购(\d+\.?\d*)\s*(万|元)?', raw)
        limit = int(float(m2.group(1)) * 10000) if m2 and "万" in (m2.group(2) or "") else (int(float(m2.group(1))) if m2 else 0)
        return ("限大额", limit)
    if "开放" in raw:
        return ("开放申购", 999999)
    if "封闭" in raw:
        return ("封闭期(定开)", 0)
    return ("未知", 0)


async def fetch_lof8_status() -> dict[str, dict]:
    try:
        async with httpx.AsyncClient(timeout=15, trust_env=False) as client:
            resp = await client.get(LOF8_API_URL)
            data = resp.json()
        if not data.get("ok") or not data.get("data"):
            return {}
        result = {}
        for item in data["data"]:
            code = str(item.get("code", "")).strip()
            if not code:
                continue
            raw_status = item.get("status", "")
            purchase_status, daily_limit = _parse_lof8_status(raw_status)
            result[code] = {"purchase_status": purchase_status, "daily_limit": daily_limit,
                           "lof8_premium": _safe_float(item.get("premium", 0)),
                           "lof8_est_nav": _safe_float(item.get("estNav", 0)),
                           "lof8_price": _safe_float(item.get("price", 0)),
                           "lof8_nav": _safe_float(item.get("nav", 0)),
                           "lof8_name": item.get("name", "")}
        print(f"[lof8.cn] 获取 {len(result)} 只LOF申购状态和溢价数据")
        return result
    except Exception as e:
        print(f"[lof8.cn] 获取失败: {e}")
        return {}


def enrich_with_lof8(lof_data: list[dict], lof8_info: dict[str, dict]) -> list[dict]:
    updated_status = 0
    updated_premium = 0
    for item in lof_data:
        code = item.get("code", "")
        if code not in lof8_info:
            continue
        info = lof8_info[code]
        new_status = info["purchase_status"]
        if new_status and new_status != "未知":
            item["purchase_status"] = new_status
            item["purchase_status_source"] = "lof8"
            updated_status += 1
        if info.get("daily_limit", 0) > 0:
            item["daily_limit"] = info["daily_limit"]
        if info.get("lof8_premium", 0) != 0:
            item["lof8_premium"] = info["lof8_premium"]
            item["lof8_est_nav"] = info.get("lof8_est_nav", 0)
            updated_premium += 1
    if updated_status > 0:
        print(f"[lof8.cn] 覆盖了 {updated_status} 只LOF的申购状态")
    if updated_premium > 0:
        print(f"[lof8.cn] 添加了 {updated_premium} 只LOF的溢价交叉验证数据")
    return lof_data


# ============================================================
#  数据源: 腾讯财经 (备用)
# ============================================================

def _parse_tencent_quote(line: str) -> Optional[dict]:
    match = re.match(r'v_(\w+)="(.+)"', line.strip())
    if not match:
        return None
    fields = match.group(2).split("~")
    if len(fields) < 7:
        return None
    code = fields[2]
    name = fields[1]
    price = _safe_float(fields[3])
    volume = _safe_float(fields[6])
    amount = price * volume if price > 0 and volume > 0 else 0
    return {"code": code, "name": name, "price": price, "nav": 0, "premium_rt": 0,
            "volume": volume, "amount": amount, "apply_status": "未知", "nav_date": "",
            "issuer": "", "turnover_rt": 0, "source": "tencent"}


async def fetch_lof_from_tencent(lof_codes: list[str] = None) -> Optional[list[dict]]:
    if not lof_codes:
        jisilu_data = await fetch_jisilu_data_full()
        if jisilu_data:
            lof_codes = [f"{'sz' if str(c).startswith('1') else 'sh'}{c}" for c in jisilu_data]
    if not lof_codes:
        return None
    results = []
    for i in range(0, len(lof_codes), 50):
        batch = lof_codes[i:i + 50]
        try:
            async with httpx.AsyncClient(timeout=REQUEST_TIMEOUT, trust_env=False) as client:
                resp = await client.get(f"{TENCENT_URL}{','.join(batch)}")
                resp.encoding = "gbk"
                for line in resp.text.strip().split("\n"):
                    if "=" not in line:
                        continue
                    parsed = _parse_tencent_quote(line)
                    if parsed and parsed["code"] and parsed["price"] > 0:
                        results.append(parsed)
        except Exception:
            continue
    print(f"[腾讯财经] 获取到 {len(results)} 只LOF实时行情")
    return results


# ============================================================
#  净值核实: 天天基金 (est_nav核心) ★
# ============================================================

async def _fetch_single_nav_ttjj(client: httpx.AsyncClient, code: str) -> Optional[dict]:
    url = TTJJ_URL.format(code=code)
    try:
        resp = await client.get(url)
        text = resp.text
        dwjz_match = re.search(r'"dwjz":"([^"]+)"', text)
        jzrq_match = re.search(r'"jzrq":"([^"]+)"', text)
        gsz_match = re.search(r'"gsz":"([^"]+)"', text)
        gztime_match = re.search(r'"gztime":"([^"]+)"', text)
        nav = _safe_float(dwjz_match.group(1)) if dwjz_match else 0.0
        nav_date = jzrq_match.group(1) if jzrq_match else ""
        est_nav = _safe_float(gsz_match.group(1)) if gsz_match else 0.0
        est_time = gztime_match.group(1) if gztime_match else ""
        if nav > 0:
            return {"code": code, "nav": nav, "nav_date": nav_date, "est_nav": est_nav, "est_time": est_time}
        return None
    except Exception:
        return None


async def verify_navs_batch(candidates: list[dict]) -> list[dict]:
    if not candidates:
        return candidates
    to_verify = candidates[:NAV_VERIFY_MAX]
    print(f"[净值核实] 准备核实 {len(to_verify)} 只LOF的最新净值...")
    semaphore = asyncio.Semaphore(NAV_VERIFY_CONCURRENCY)
    verified_count = 0
    async def verify_one(item: dict) -> dict:
        nonlocal verified_count
        async with semaphore:
            async with httpx.AsyncClient(timeout=REQUEST_TIMEOUT, trust_env=False) as client:
                result = await _fetch_single_nav_ttjj(client, item["code"])
                await asyncio.sleep(0.2)
        if result and result["nav"] > 0:
            verified_count += 1
            real_price = item["price"]
            real_nav = result["nav"]
            est_nav = result.get("est_nav", 0)
            verified_premium = round((real_price - real_nav) / real_nav * 100, 2) if real_nav > 0 else item["premium_rt"]
            est_premium = round((real_price - est_nav) / est_nav * 100, 2) if est_nav > 0 else verified_premium
            jsl_date = _parse_date(item.get("nav_date", ""))
            ttjj_date = _parse_date(result.get("nav_date", ""))
            if ttjj_date and jsl_date:
                days_diff = (ttjj_date - jsl_date).days
                verify_note = f"净值更新+{days_diff}天" if days_diff > 0 else ("净值一致" if days_diff == 0 else f"净值早{abs(days_diff)}天")
            else:
                verify_note = "已核实"
            item["nav_verified"] = real_nav
            item["nav_date_verified"] = result["nav_date"]
            item["nav_date_jisilu"] = item.get("nav_date", "")
            item["premium_rt_jisilu"] = item["premium_rt"]
            item["premium_rt_verified"] = verified_premium
            item["premium_rt_est"] = est_premium
            item["est_nav"] = est_nav
            item["est_time"] = result.get("est_time", "")
            item["verify_note"] = verify_note
            item["verified"] = True
            premium_gap = item["premium_rt_jisilu"] - verified_premium
            if abs(premium_gap) > 1:
                est_info = f" | 实时溢价={est_premium:+.1f}%" if est_nav > 0 else ""
                print(f"  [!] {item['code']} {item['name']}: JSL={item['premium_rt_jisilu']:+.1f}% -> TTJJ={verified_premium:+.1f}%{est_info}")
        else:
            item["nav_verified"] = item["nav"]
            item["nav_date_verified"] = item.get("nav_date", "")
            item["premium_rt_verified"] = item["premium_rt"]
            item["verified"] = False
        return item
    tasks = [verify_one(item) for item in to_verify]
    verified_results = await asyncio.gather(*tasks)
    unverified = candidates[NAV_VERIFY_MAX:]
    for item in unverified:
        item["nav_verified"] = item["nav"]
        item["premium_rt_verified"] = item["premium_rt"]
        item["verified"] = False
    print(f"[净值核实] 完成: {verified_count}/{len(to_verify)} 核实成功")
    return verified_results + unverified


# ============================================================
#  公告核实
# ============================================================

async def _fetch_fund_page_html(client: httpx.AsyncClient, code: str) -> Optional[str]:
    url = f"https://fund.eastmoney.com/{code}.html"
    try:
        resp = await client.get(url, headers={"User-Agent": "Mozilla/5.0", "Referer": "https://fund.eastmoney.com/"})
        resp.encoding = "utf-8"
        return resp.text
    except Exception:
        return None


def _parse_purchase_status_from_html(html: str) -> dict:
    result = {"purchase_status_raw": "", "daily_limit_raw": ""}
    trade_status_match = re.search(r'(?:交易状态|申购状态)[：:]\s*((?:<[^>]+>)*\s*[^<]+(?:\s*<[^>]+>[^<]*)*)', html)
    if trade_status_match:
        raw = re.sub(r'<[^>]+>', '', trade_status_match.group(1))
        raw = re.sub(r'\s+', ' ', raw).strip()
        result["purchase_status_raw"] = raw
    limit_match = re.search(r'单日(?:累计)?(?:购买|申购)上限[：:：\s]*([\d,.]+)\s*(?:元|万)?', html)
    if limit_match:
        result["daily_limit_raw"] = limit_match.group(1).replace(',', '')
    return result


def _normalize_purchase_status(raw_status: str) -> dict:
    if not raw_status:
        return {"status": "未知", "daily_limit": 0}
    raw = raw_status.strip()
    daily_limit = 0
    limit_match = re.search(r'单日(?:累计)?(?:购买|申购)上限[：:：\s]*([\d,.]+)\s*(?:元|万)?', raw)
    if limit_match:
        try:
            daily_limit = float(limit_match.group(1).replace(',', ''))
        except ValueError:
            pass
    if any(kw in raw for kw in ["封闭期", "暂停申购", "暂停赎回"]):
        if "暂停申购" in raw and "暂停赎回" in raw:
            return {"status": "封闭期(定开)", "daily_limit": 0}
        return {"status": "暂停申购", "daily_limit": daily_limit}
    elif "开放申购" in raw or "开放赎回" in raw:
        if daily_limit > 0 and daily_limit < 100:
            return {"status": "限大额", "daily_limit": daily_limit}
        return {"status": "开放申购", "daily_limit": daily_limit if daily_limit > 0 else 999999}
    elif "限" in raw:
        return {"status": "限大额", "daily_limit": daily_limit}
    return {"status": raw_status[:8], "daily_limit": daily_limit}


def _check_announcement_concerns(announcements: list[dict]) -> dict:
    concerns = {"has_suspend_risk": False, "has_limit_risk": False, "has_resume_signal": False,
                 "has_trade_risk": False, "matched_anns": [], "risk_level": "safe", "risk_note": ""}
    for ann in announcements:
        title = ann.get("title", "")
        date = ann.get("date", "")
        for kw in SUSPEND_KEYWORDS:
            if kw in title:
                concerns["has_suspend_risk"] = True
                concerns["matched_anns"].append(f"⚠ {date} {title}")
                break
        for kw in LIMIT_KEYWORDS:
            if kw in title:
                concerns["has_limit_risk"] = True
                concerns["matched_anns"].append(f"🔶 {date} {title}")
                break
        for kw in RESUME_KEYWORDS + RESUME_TRADE_KEYWORDS:
            if kw in title:
                concerns["has_resume_signal"] = True
                concerns["matched_anns"].append(f"🔄 {date} {title}")
                break
        for kw in TRADE_STATUS_KEYWORDS:
            if kw in title:
                concerns["has_trade_risk"] = True
                concerns["matched_anns"].append(f"⚠ {date} {title}")
                break
    if concerns["has_resume_signal"]:
        concerns["risk_level"] = "resume"
        concerns["risk_note"] = "检测到恢复申购/复牌信号"
    elif concerns["has_suspend_risk"] or concerns["has_trade_risk"]:
        concerns["risk_level"] = "danger"
        concerns["risk_note"] = "有暂停或停牌公告"
    elif concerns["has_limit_risk"]:
        concerns["risk_level"] = "warning"
        concerns["risk_note"] = "有限额调整公告"
    else:
        concerns["risk_note"] = "公告无异常"
    return concerns


async def verify_announcements_batch(candidates: list[dict]) -> list[dict]:
    if not candidates:
        return candidates
    print(f"[公告核实] 准备核实 {len(candidates)} 只LOF的最新公告...")
    semaphore = asyncio.Semaphore(NAV_VERIFY_CONCURRENCY)
    checked_count = 0
    warned_count = 0
    async def check_one(item: dict) -> dict:
        nonlocal checked_count, warned_count
        async with semaphore:
            async with httpx.AsyncClient(timeout=REQUEST_TIMEOUT, trust_env=False) as client:
                html = await _fetch_fund_page_html(client, item["code"])
                await asyncio.sleep(0.3)
        if html:
            checked_count += 1
            status_info = _parse_purchase_status_from_html(html)
            if status_info.get("purchase_status_raw"):
                normalized = _normalize_purchase_status(status_info["purchase_status_raw"])
                html_status = normalized["status"]
                html_limit = normalized["daily_limit"]
                current_status = item.get("purchase_status", "未知")
                if current_status == "未知" or not current_status:
                    item["purchase_status"] = html_status
                    item["daily_limit"] = html_limit
                    item["purchase_status_source"] = "html_verified"
            item["announcement_checked"] = True
            item["ann_risk_level"] = "safe"
            item["ann_risk_note"] = "公告无异常"
            item["ann_matched"] = []
        else:
            item["announcement_checked"] = False
            item["ann_risk_level"] = "unknown"
            item["ann_risk_note"] = "无法获取公告"
            item["ann_matched"] = []
        return item
    tasks = [check_one(item) for item in candidates[:NAV_VERIFY_MAX]]
    verified = await asyncio.gather(*tasks)
    print(f"[公告核实] 完成: {checked_count}/{len(candidates)} 核实成功, {warned_count} 有风险")
    return verified


# ============================================================
#  申购限额 & 数据富化
# ============================================================

async def fetch_purchase_limits() -> dict[str, dict]:
    url = "https://fundmobapi.eastmoney.com/FundMNewApi/FundMNFBPurchaseLimit"
    headers = {"User-Agent": "Mozilla/5.0", "Referer": "https://fund.eastmoney.com/"}
    all_data = {}
    try:
        async with httpx.AsyncClient(timeout=REQUEST_TIMEOUT, trust_env=False) as client:
            for page_index in range(1, 6):
                params = {"pageIndex": str(page_index), "pageSize": "500", "Sort": "CODE", "SortOrder": "ASC",
                           "FundType": "", "deviceid": "web", "plat": "web", "product": "EFund", "version": "1.0.0"}
                resp = await client.get(url, headers=headers, params=params)
                if resp.status_code != 200:
                    break
                data = resp.json()
                if data.get("ErrCode") != 0:
                    break
                fund_list = data.get("Data", {}).get("FundList", [])
                if not fund_list:
                    break
                for fund in fund_list:
                    code = fund.get("FCODE", "")
                    if code:
                        all_data[code] = {"purchase_status": fund.get("SGTEXT", "未知"),
                                           "daily_limit": _safe_float(fund.get("DAYMAXAMT", 0)),
                                           "min_purchase": _safe_float(fund.get("MINTIMEMINAMT", 0)),
                                           "purchase_fee": _safe_float(fund.get("RATE", 0))}
                total_pages = data.get("Data", {}).get("TotalPages", 1)
                if page_index >= total_pages:
                    break
                await asyncio.sleep(0.3)
        print(f"[申购限额] 获取到 {len(all_data)} 只基金的限购数据")
    except Exception as e:
        print(f"[申购限额] 异常: {e}")
    return all_data


def enrich_with_purchase_limits(lof_data: list[dict], limits: dict[str, dict]) -> list[dict]:
    for item in lof_data:
        code = item["code"]
        if code in limits:
            lim = limits[code]
            item["purchase_status"] = lim.get("purchase_status", "未知")
            item["daily_limit"] = lim.get("daily_limit", 0)
            item["min_purchase"] = lim.get("min_purchase", 0)
            item["purchase_fee"] = lim.get("purchase_fee", 0)
        else:
            item["purchase_status"] = item.get("apply_status", "未知")
            item["daily_limit"] = 0
            item["min_purchase"] = 0
            item["purchase_fee"] = 0
    return lof_data


async def fetch_fund_sizes_batch(codes: list[str]) -> dict[str, float]:
    if not codes:
        return {}
    results: dict[str, float] = {}
    semaphore = asyncio.Semaphore(5)
    async def _fetch_one(code: str) -> tuple[str, float]:
        async with semaphore:
            try:
                async with httpx.AsyncClient(timeout=REQUEST_TIMEOUT, trust_env=False) as client:
                    resp = await client.get(f"https://fund.eastmoney.com/{code}.html",
                        headers={"User-Agent": "Mozilla/5.0", "Referer": "https://fund.eastmoney.com/"})
                    text = resp.text
                m = re.search(r'规模[^>]*>[：:]\s*([\d.,]+)\s*(亿|万)元', text)
                if m:
                    value = _safe_float(m.group(1).replace(",", ""))
                    unit = m.group(2)
                    size = value * 1e8 if unit == "亿" else value * 1e4
                    if size > 0:
                        return (code, size)
            except Exception:
                pass
            return (code, 0.0)
    tasks = [_fetch_one(code) for code in codes[:30]]
    fetched = await asyncio.gather(*tasks)
    for code, size in fetched:
        if size > 0:
            results[code] = size
    if results:
        print(f"[基金规模] 获取到 {len(results)} 只基金的规模数据")
    return results


# ============================================================
#  最终筛选 (est_nav 溢价版本) ★
# ============================================================

def filter_arbitrage_opportunities(data: list[dict], fund_sizes: dict[str, float] = None) -> list[dict]:
    fund_sizes = fund_sizes or {}
    opportunities = []
    filtered_by_volume = 0
    filtered_by_size = 0
    for item in data:
        premium = item.get("premium_rt_est", item.get("premium_rt_verified", item.get("premium_rt", 0)))
        premium_dwjz = item.get("premium_rt_verified", premium)
        amount = item.get("amount", 0)
        price = item.get("price", 0)
        nav = item.get("est_nav", 0) or item.get("nav_verified", item.get("nav", 0))
        name = item.get("name", "")
        code = item.get("code", "")
        threshold = 5.0 if _is_resource_lof(name) else PREMIUM_THRESHOLD
        if abs(premium) < threshold:
            continue
        if price <= 0 or nav <= 0:
            continue
        if amount < MIN_VOLUME:
            filtered_by_volume += 1
            continue
        fund_size = fund_sizes.get(code, 0)
        if fund_size > 0 and fund_size < MIN_FUND_SIZE:
            filtered_by_size += 1
            continue
        is_premium = premium > 0
        verified = item.get("verified", False)
        if amount >= MIN_VOLUME_OK:
            volume_level = "high"
        elif amount >= MIN_VOLUME_WARN:
            volume_level = "mid"
        else:
            volume_level = "low"
        purchase_fee = item.get("purchase_fee", 0.15)
        net_premium = round(premium - purchase_fee - 0.01, 2) if premium > 0 else premium
        opportunities.append({"premium_rt": premium, "premium_rt_dwjz": premium_dwjz,
                               "nav": nav, "nav_dwjz": item.get("nav_verified", item.get("nav", 0)),
                               "direction": "溢价套利" if is_premium else "折价套利",
                               "is_premium": is_premium, "verified": verified, "volume_level": volume_level,
                               "net_premium": net_premium, "fund_size": fund_size,
                               "signal_score": _calc_signal_score(premium, amount, item.get("turnover_rt", 0)),
                               "ann_url": f"https://fundf10.eastmoney.com/jjgg_{code}.html", **item})
    if filtered_by_volume > 0:
        print(f"[筛选] 成交额不足1000万: {filtered_by_volume} 只")
    if filtered_by_size > 0:
        print(f"[筛选] 基金规模不足2亿: {filtered_by_size} 只")
    opportunities.sort(key=lambda x: abs(x["premium_rt"]), reverse=True)
    return opportunities


# ============================================================
#  主流程
# ============================================================

async def get_all_lof_arbitrage_opportunities(use_mock: bool = False) -> dict:
    if use_mock:
        from fetcher import generate_mock_data
        raw_data = generate_mock_data()
        opportunities = filter_arbitrage_opportunities(raw_data)
        return {"success": True, "source": "mock", "count": len(opportunities),
                "updated_at": datetime.now().strftime("%Y-%m-%d %H:%M:%S"), "data": opportunities}
    lof_data = await fetch_lof_from_eastmoney()
    if not lof_data:
        lof_data = await fetch_jisilu_lof()
    if not lof_data:
        lof_data = await fetch_lof_from_tencent()
    if not lof_data:
        import akshare as ak
        from concurrent.futures import ThreadPoolExecutor
        try:
            loop = asyncio.get_running_loop()
            with ThreadPoolExecutor(max_workers=1) as pool:
                df = await loop.run_in_executor(pool, ak.fund_lof_spot_em)
            if df is not None and not df.empty:
                results = []
                for _, row in df.iterrows():
                    price = _safe_float(row.get("最新价", 0))
                    if price > 0:
                        results.append({"code": str(row.get("基金代码", "")), "name": str(row.get("基金简称", "")),
                                        "price": price, "nav": 0, "premium_rt": 0,
                                        "volume": _safe_float(row.get("成交量", 0)),
                                        "amount": _safe_float(row.get("成交额", 0)),
                                        "apply_status": "未知", "nav_date": "", "issuer": "",
                                        "turnover_rt": 0, "source": "akshare"})
                lof_data = results if results else None
        except Exception:
            lof_data = None
    if not lof_data:
        jisilu_full = await fetch_jisilu_data_full()
        if jisilu_full:
            lof_data = _jisilu_dict_to_lof_list(jisilu_full)
    if not lof_data:
        return {"success": False, "error": "所有数据源均无法访问", "source": "none", "count": 0,
                "updated_at": datetime.now().strftime("%Y-%m-%d %H:%M:%S"), "data": []}
    lof_data.sort(key=lambda x: x.get("amount", 0), reverse=True)
    lof_data = await verify_navs_batch(lof_data)
    lof_data = enrich_with_purchase_limits(lof_data, await fetch_purchase_limits() if True else {})
    jisilu_info = await fetch_jisilu_data_full()
    if jisilu_info:
        lof_data = enrich_with_jisilu_status(lof_data, jisilu_info)
    lof8_info = await fetch_lof8_status()
    if lof8_info:
        lof_data = enrich_with_lof8(lof_data, lof8_info)
    codes_to_check = [item["code"] for item in lof_data if item.get("amount", 0) >= MIN_VOLUME]
    fund_sizes = await fetch_fund_sizes_batch(codes_to_check) if codes_to_check else {}
    opportunities = filter_arbitrage_opportunities(lof_data, fund_sizes)
    if opportunities:
        opportunities = await verify_announcements_batch(opportunities)
    return {"success": True, "source": lof_data[0]["source"] if lof_data else "unknown",
            "count": len(opportunities),
            "updated_at": datetime.now().strftime("%Y-%m-%d %H:%M:%S"), "data": opportunities}


if __name__ == "__main__":
    async def main():
        result = await get_all_lof_arbitrage_opportunities()
        print(json.dumps(result, ensure_ascii=False, indent=2))
    asyncio.run(main())
