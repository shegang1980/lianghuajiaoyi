"""
股票复盘实时面板 - 后端服务
数据源: 新浪财经(实时行情) + 腾讯财经(K线/分时) + AKShare(板块/全市场)
运行: python3 server.py  ->  http://localhost:5000
"""
import re
import json
import time
import requests
import pandas as pd
from flask import Flask, jsonify, request, send_from_directory
from concurrent.futures import ThreadPoolExecutor

app = Flask(__name__, static_folder="static", static_url_path="")

SINA_HDR = {
    "Referer": "https://finance.sina.com.cn",
    "User-Agent": "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36",
}
TENCENT_HDR = {"User-Agent": "Mozilla/5.0"}

CACHE = {}  # 简单内存缓存 {key: (ts, data)}


def cache_get(key, ttl=60):
    item = CACHE.get(key)
    if item and time.time() - item[0] < ttl:
        return item[1]
    return None


def cache_set(key, data):
    CACHE[key] = (time.time(), data)


# ============ 代码解析 ============
def parse_code(raw):
    """把用户输入解析成 (sina_code, tencent_code, market)
    支持: 600519 / sh600519 / sz000001 / 00700 / hk00700 / AAPL / usAAPL
    """
    raw = str(raw).strip().lower()
    # 美股: 字母开头
    if re.match(r"^[a-z]", raw):
        sym = raw.replace("us", "", 1).upper()
        # 尝试推断交易所后缀
        return f"gb_{sym.lower()}", f"us{sym}.OQ", "us"
    # 港股: 5位数字 或 hk开头
    if raw.startswith("hk") or (raw.isdigit() and len(raw) == 5):
        code = raw.replace("hk", "")
        return f"rt_hk{code}", f"hk{code}", "hk"
    # A股: 6位数字 或 sh/sz/bj 开头
    if raw.startswith(("sh", "sz", "bj")):
        return raw, raw, "a"
    if raw.isdigit() and len(raw) == 6:
        prefix = "sh" if raw.startswith(("6", "9")) else ("bj" if raw.startswith(("8", "4")) else "sz")
        return f"{prefix}{raw}", f"{prefix}{raw}", "a"
    return raw, raw, "a"


# ============ 实时行情 (新浪) ============
def sina_quote(sina_code):
    """请求新浪实时行情, 返回解析后的 dict"""
    url = f"https://hq.sinajs.cn/list={sina_code}"
    try:
        r = requests.get(url, headers=SINA_HDR, timeout=10)
        r.encoding = "gbk"
        text = r.text
        m = re.search(r'="([^"]*)"', text)
        if not m:
            return None
        parts = m.group(1).split(",")
        return parts
    except Exception:
        return None


@app.route("/api/quote")
def api_quote():
    """实时行情. codes 逗号分隔"""
    codes = request.args.get("codes", "")
    if not codes:
        return jsonify({"error": "missing codes"}), 400
    result = {}
    for c in codes.split(","):
        sina_code, _, market = parse_code(c)
        parts = sina_quote(sina_code)
        if not parts:
            result[c] = {"error": "no data", "market": market}
            continue
        data = {"market": market, "code": c}
        if market == "a":
            # 名称,今开,昨收,当前价,最高,最低,买一,卖一,成交量,成交额,...
            data.update({
                "name": parts[0], "open": parts[1], "prev_close": parts[2],
                "price": parts[3], "high": parts[4], "low": parts[5],
                "bid": parts[6], "ask": parts[7],
                "volume": parts[8], "amount": parts[9],
                "date": parts[30] if len(parts) > 30 else "",
                "time": parts[31] if len(parts) > 31 else "",
            })
        elif market == "hk":
            # 英文名,中文名,昨收,今开,最高,最低,当前价,涨跌额,涨跌幅,买一,卖一,成交额,成交量,...
            data.update({
                "name": parts[1], "prev_close": parts[2], "open": parts[3],
                "high": parts[4], "low": parts[5], "price": parts[6],
                "change": parts[7], "change_pct": parts[8],
                "bid": parts[9], "ask": parts[10], "amount": parts[11],
                "volume": parts[12], "date": parts[17] if len(parts) > 17 else "",
                "time": parts[18] if len(parts) > 18 else "",
            })
        elif market == "us":
            # 名称,当前价,涨跌幅,时间,涨跌额,今开,最高,最低,52周高,52周低,成交量,成交额,市值,市盈率,...
            data.update({
                "name": parts[0], "price": parts[1], "change_pct": parts[2],
                "time": parts[3], "change": parts[4], "open": parts[5],
                "high": parts[6], "low": parts[7],
                "week52_high": parts[8], "week52_low": parts[9],
                "volume": parts[10], "amount": parts[11], "mktcap": parts[12],
                "pe": parts[13],
            })
        result[c] = data
    return jsonify(result)


# ============ K线 (腾讯) ============
@app.route("/api/kline")
def api_kline():
    """K线. code, period=day/week/month/5/15/30/60, count=120"""
    code = request.args.get("code", "")
    period = request.args.get("period", "day")
    count = int(request.args.get("count", "120"))
    _, tc, market = parse_code(code)
    period_map = {"day": "day", "week": "week", "month": "month",
                  "5": "5", "15": "15", "30": "30", "60": "60"}
    p = period_map.get(period, "day")
    url = f"https://web.ifzq.gtimg.cn/appstock/app/fqkline/get?param={tc},{p},,,{count},qfq"
    try:
        r = requests.get(url, headers=TENCENT_HDR, timeout=15)
        js = r.json()
        d = js.get("data", {}).get(tc, {})
        # qfqday / day / qfq5 / 5 ...
        key = f"qfq{p}" if f"qfq{p}" in d else p
        klines = d.get(key, [])
        # 有些返回 qfqday, 有些 day
        if not klines and p == "day":
            klines = d.get("day", [])
        qt = d.get("qt", {}).get(tc, [])
        out = []
        for k in klines:
            # [date, open, close, high, low, volume]
            out.append({"date": k[0], "open": k[1], "close": k[2],
                        "high": k[3], "low": k[4], "volume": k[5]})
        info = {}
        if qt and len(qt) > 5:
            info = {"name": qt[1], "code": qt[2], "price": qt[3], "prev": qt[4]}
        return jsonify({"market": market, "kline": out, "info": info})
    except Exception as e:
        return jsonify({"error": str(e)}), 500


# ============ 分时 (腾讯) ============
@app.route("/api/minute")
def api_minute():
    """分时图. code"""
    code = request.args.get("code", "")
    _, tc, market = parse_code(code)
    url = f"https://web.ifzq.gtimg.cn/appstock/app/minute/query?code={tc}"
    try:
        r = requests.get(url, headers=TENCENT_HDR, timeout=15)
        js = r.json()
        d = js.get("data", {}).get(tc, {}).get("data", {})
        rows = d.get("data", [])
        points = []
        for row in rows:
            parts = row.split()
            if len(parts) >= 3:
                points.append({"time": parts[0], "price": parts[1],
                               "volume": parts[2]})
        return jsonify({"market": market, "points": points, "raw": d})
    except Exception as e:
        return jsonify({"error": str(e)}), 500


# ============ 全市场行情 (AKShare 新浪源) ============
@app.route("/api/market")
def api_market():
    """市场列表. type=a/hk, sort=涨幅榜/跌幅榜/成交额"""
    mtype = request.args.get("type", "a")
    sort = request.args.get("sort", "涨幅榜")
    limit = int(request.args.get("limit", "50"))
    key = f"market_{mtype}_{sort}_{limit}"
    cached = cache_get(key, ttl=120)
    if cached is not None:
        return jsonify(cached)
    try:
        import akshare as ak
        if mtype == "a":
            df = ak.stock_zh_a_spot()
            if sort == "amount":
                sort_col = "成交额"
            else:
                sort_col = "涨跌幅"
        else:
            df = ak.stock_hk_spot()
            sort_col = "涨跌幅"
        asc = sort == "down"
        df = df.sort_values(by=sort_col, ascending=asc).head(limit)
        # 统一字段
        cols_map = {}
        if mtype == "a":
            cols_map = {"代码": "code", "名称": "name", "最新价": "price",
                        "涨跌额": "change", "涨跌幅": "change_pct",
                        "成交量": "volume", "成交额": "amount"}
        else:
            cols_map = {"代码": "code", "中文名称": "name", "最新价": "price",
                        "涨跌额": "change", "涨跌幅": "change_pct",
                        "成交量": "volume", "成交额": "amount"}
        out = []
        for _, row in df.iterrows():
            item = {}
            for cn, en in cols_map.items():
                if cn in df.columns:
                    v = row[cn]
                    item[en] = float(v) if isinstance(v, (int, float)) else v
            out.append(item)
        cache_set(key, out)
        return jsonify(out)
    except Exception as e:
        return jsonify({"error": str(e)}), 500


# ============ 板块数据 (AKShare) ============
@app.route("/api/sectors")
def api_sectors():
    cached = cache_get("sectors", ttl=180)
    if cached is not None:
        return jsonify(cached)
    try:
        import akshare as ak
        df = ak.stock_sector_spot()
        out = []
        for _, row in df.iterrows():
            out.append({
                "name": row.get("板块", ""),
                "change_pct": float(row.get("涨跌幅", 0) or 0),
                "amount": float(row.get("总成交额", 0) or 0),
                "top_stock": row.get("股票名称", ""),
                "top_pct": float(row.get("个股-涨跌幅", 0) or 0),
                "top_code": row.get("股票代码", ""),
            })
        out.sort(key=lambda x: x["change_pct"], reverse=True)
        cache_set("sectors", out)
        return jsonify(out)
    except Exception as e:
        return jsonify({"error": str(e)}), 500


# ============ 大盘指数 ============
@app.route("/api/indices")
def api_indices():
    """主要指数实时"""
    idx_map = {
        "sh000001": "上证指数", "sz399001": "深证成指", "sz399006": "创业板指",
        "sh000300": "沪深300", "sh000016": "上证50", "sz399005": "中小板指",
        "rt_hkHSI": "恒生指数", "rt_hkHSCEI": "国企指数",
        "gb_dji": "道琼斯", "gb_ixic": "纳斯达克", "gb_inx": "标普500",
    }
    result = {}
    for code, name in idx_map.items():
        parts = sina_quote(code)
        if not parts:
            continue
        if code.startswith("sh") or code.startswith("sz"):
            result[code] = {"name": name, "price": parts[3], "change": parts[31] if len(parts) > 31 else "",
                            "change_pct": parts[32] if len(parts) > 32 else "", "volume": parts[8], "amount": parts[9]}
        elif "hkHSI" in code or "hkHSCEI" in code:
            result[code] = {"name": name, "price": parts[6], "change": parts[7], "change_pct": parts[8]}
        else:
            result[code] = {"name": name, "price": parts[1], "change": parts[4], "change_pct": parts[2]}
    return jsonify(result)


# ============ 搜索 ============
@app.route("/api/search")
def api_search():
    """简单搜索, 返回匹配的热门标的"""
    q = request.args.get("q", "").strip().lower()
    if not q:
        return jsonify([])
    popular = [
        # A股
        {"code": "600519", "name": "贵州茅台", "market": "a"},
        {"code": "000858", "name": "五粮液", "market": "a"},
        {"code": "601318", "name": "中国平安", "market": "a"},
        {"code": "000001", "name": "平安银行", "market": "a"},
        {"code": "300750", "name": "宁德时代", "market": "a"},
        {"code": "002594", "name": "比亚迪", "market": "a"},
        {"code": "600036", "name": "招商银行", "market": "a"},
        {"code": "601899", "name": "紫金矿业", "market": "a"},
        {"code": "000725", "name": "京东方A", "market": "a"},
        {"code": "600900", "name": "长江电力", "market": "a"},
        # 港股
        {"code": "00700", "name": "腾讯控股", "market": "hk"},
        {"code": "09988", "name": "阿里巴巴-W", "market": "hk"},
        {"code": "09618", "name": "京东集团-SW", "market": "hk"},
        {"code": "03690", "name": "美团-W", "market": "hk"},
        {"code": "00941", "name": "中国移动", "market": "hk"},
        # 美股
        {"code": "AAPL", "name": "苹果", "market": "us"},
        {"code": "TSLA", "name": "特斯拉", "market": "us"},
        {"code": "NVDA", "name": "英伟达", "market": "us"},
        {"code": "MSFT", "name": "微软", "market": "us"},
        {"code": "GOOGL", "name": "谷歌", "market": "us"},
        {"code": "AMZN", "name": "亚马逊", "market": "us"},
        {"code": "META", "name": "Meta", "market": "us"},
        {"code": "BABA", "name": "阿里巴巴", "market": "us"},
    ]
    res = [s for s in popular if q in s["code"].lower() or q in s["name"].lower()]
    return jsonify(res)


@app.route("/")
def index():
    return send_from_directory("static", "index.html")


if __name__ == "__main__":
    print("🚀 股票复盘实时面板启动: http://localhost:5000")
    app.run(host="0.0.0.0", port=5000, debug=False, threaded=True)
