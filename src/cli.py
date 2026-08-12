"""
命令行入口 —— --login / --probe / --check / --dry-run / --run / --init-db

各命令都返回 int 退出码,main() 里统一 sys.exit(),方便 PowerShell 里用 $LASTEXITCODE 判断。

⚠️ client / auth / poller / bot / apscheduler 全部**延迟导入**:
   --init-db 与 --check 必须在 playwright 没装好、登录态没建立的机器上也能跑通,
   否则"环境到底哪一步没配好"永远查不出来。

⚠️ --probe 是 Phase 0 的硬门槛(设计文档 §11 / §14)。
   §11 那 17 项字段假设全部是逆向前端 bundle 猜的,一项都没实测过 ——
   核对表没逐项打勾之前,normalize() 写出来的每一行都是在赌。
"""
from __future__ import annotations

import argparse
import json
import re
import sys
import threading
import time
import unicodedata
from contextlib import suppress
from datetime import UTC, datetime
from urllib.parse import quote

from loguru import logger

from src import store
from src.client import request_stop
from src.config import PROBE_DIR, PROFILE_DIR, SESSION_FILE, get_settings, mask
from src.logger import setup_logger
from src.models import (
    NETWORK_SLUG,
    QUOTE_TOKENS,
    known_networks,
    normalize_network,
    normalize_token_address,
    now_iso,
    to_iso,
)
from src.notifier import TelegramNotifier

# --handle 缺省时的兜底样本用户(设计文档示例里的名字)。
# 正常路径是从 /v2/leaderboard 自动挑一个 —— 硬编码的名字随时可能改名或销号。
DEFAULT_PROBE_HANDLE = "maxpain"

# 核对表的结论列取值
OK = "✅ 符合假设"
NG = "❌ 不符合"
WARN = "⚠️ 需人工核对"
NA = "— 本次无法验证"


# ============================================================
# 终端输出小工具
# ============================================================
def _out(line: str = "") -> None:
    """
    报表走 stdout 而不是 logger:logger 前缀(时间/级别/文件行号)会把对齐的表格撑烂。
    Windows 控制台可能是 GBK,emoji 直接抛 UnicodeEncodeError —— 兜一下,别让整个 probe 白跑。
    """
    # flush=True:报表走 stdout、日志走 stderr,不刷的话重定向到文件时两者顺序会乱成一团
    try:
        print(line, flush=True)
    except UnicodeEncodeError:
        print(line.encode("utf-8", errors="replace").decode("utf-8", errors="replace"), flush=True)


def _dw(s: str) -> int:
    """显示宽度:CJK 全角按 2 列算,否则中文表格必然错位"""
    return sum(2 if unicodedata.east_asian_width(c) in ("W", "F") else 1 for c in str(s))


def _fit(s, width: int) -> str:
    """按显示宽度截断并右侧补空格"""
    s = " ".join(str(s).split())  # 换行和连续空格会把表格撑烂
    if _dw(s) <= width:
        return s + " " * (width - _dw(s))
    out, used = "", 0
    for ch in s:
        cw = 2 if unicodedata.east_asian_width(ch) in ("W", "F") else 1
        if used + cw > width - 1:
            break
        out += ch
        used += cw
    return out + "…" + " " * max(0, width - used - 1)


# ============================================================
# probe:脱敏 + dump
# ============================================================
# ⚠️ 绝不能用"键名里含 token 就糊掉"这种模糊规则 ——
#    tokenAddress / tokenSymbol / tokenAmount 全都含 "token",糊掉它们 probe 就白跑了。
#    用"精确键名 + 后缀匹配":凭据字段是 xxxToken / xxxSecret 形态(以敏感词**结尾**),
#    而业务字段是 tokenXxx 形态(以敏感词**开头**),两者不会误伤。
_SECRET_KEYS = frozenset({
    "token", "authorization", "auth", "jwt", "bearer", "secret",
    "password", "passwd", "cookie", "cookies", "credentials",
})
_SECRET_SUFFIXES = ("token", "secret", "password", "apikey", "credential", "cookie")


def _is_secret_key(k) -> bool:
    n = str(k).lower().replace("_", "").replace("-", "")
    return n in _SECRET_KEYS or n.endswith(_SECRET_SUFFIXES)
# JWT 形态的值不管挂在哪个键上一律糊掉(Privy 的 access token 就长这样)
_JWT_RE = re.compile(r"^ey[A-Za-z0-9_-]{8,}\.[A-Za-z0-9_-]{8,}")


def _sanitize(obj, depth: int = 0):
    """dump 落盘前脱敏。data/fomo_probe/ 已在 .gitignore,但凭据永远不该落地成文件"""
    if depth > 12:
        return obj
    if isinstance(obj, dict):
        out = {}
        for k, v in obj.items():
            if _is_secret_key(k):
                out[k] = "***REDACTED***"
            else:
                out[k] = _sanitize(v, depth + 1)
        return out
    if isinstance(obj, list):
        return [_sanitize(v, depth + 1) for v in obj]
    if isinstance(obj, str) and _JWT_RE.match(obj):
        return f"{obj[:8]}***REDACTED_JWT***"
    return obj


def _dump(name: str, path: str, status, payload) -> None:
    """整份原始响应落盘。字段语义未实测,必须留原始数据以便离线回填"""
    PROBE_DIR.mkdir(parents=True, exist_ok=True)
    body = {"_endpoint": path, "_status": status, "_fetched_at": now_iso(), "data": _sanitize(payload)}
    f = PROBE_DIR / f"{name}.json"
    f.write_text(json.dumps(body, ensure_ascii=False, indent=2, default=str), encoding="utf-8")


# 各端点的响应头,probe #15/#16 要用。
# 单独放一个模块级累加器而不是塞进 samples:samples 的 (None, None) 默认值散落在
# 五六处 .get() 调用里,改成三元组会让每一处都得同步改,风险不划算。
_PROBE_HEADERS: dict[str, dict] = {}


def _probe_get(client, samples: dict, name: str, path: str):
    """打一个端点:落盘 + 记样本。单点失败不中断整轮 probe(除了鉴权失败)"""
    from src.auth import AuthError

    try:
        status, payload, headers = client.raw_get(path)
    except AuthError:
        raise  # 没登录就没必要继续打剩下 10 个端点了
    except Exception as e:  # noqa: BLE001
        logger.error("[{}] 请求异常 {} | {}", name, path, e)
        samples[name] = (None, None)
        return None, None
    _PROBE_HEADERS[name] = headers or {}
    _dump(name, path, status, payload)
    n = len(_as_list(payload))
    logger.info("[{}] {} → HTTP {} | 列表 {} 条 | dump: {}.json", name, path, status, n, name)
    samples[name] = (status, payload)
    return status, payload


# ============================================================
# probe:响应结构探查
# ============================================================
_LIST_KEYS = ("data", "items", "results", "records", "list", "docs", "rows",
              "swaps", "transfers", "balances", "users", "feed", "theses")


def _as_list(payload, depth: int = 0) -> list:
    """把"可能包了几层的列表响应"抠出来 —— 容器键名同样是猜的,多试几个"""
    if isinstance(payload, list):
        return payload
    if isinstance(payload, dict) and depth < 3:
        for k in _LIST_KEYS:
            v = payload.get(k)
            if isinstance(v, list):
                return v
        for v in payload.values():
            if isinstance(v, dict):
                inner = _as_list(v, depth + 1)
                if inner:
                    return inner
    return []


def _first(payload) -> dict:
    """取第一条记录;响应本身就是单个对象时返回它自己"""
    items = _as_list(payload)
    if items and isinstance(items[0], dict):
        return items[0]
    return payload if isinstance(payload, dict) else {}


def _deep_find(rec, *candidates: str, max_depth: int = 3) -> tuple[str | None, object]:
    """
    在记录里(含嵌套 dict / list[0])按候选键名找第一个非空字段,返回 (点分路径, 值)。

    ⚠️ 嵌套查找是必须的:swap 记录很可能长成 {"tokenOut": {"address":…, "networkId":…}},
       只在顶层找会得出"字段不存在"的错误结论,进而误判功能 A/B 不可用。
    键名比较忽略大小写与下划线(chainId / chain_id / ChainID 是同一个东西)。
    """
    if not isinstance(rec, dict):
        return None, None
    norm = {str(k).lower().replace("_", ""): k for k in rec}
    for c in candidates:
        real = norm.get(c.lower().replace("_", ""))
        if real is not None and rec[real] not in (None, "", [], {}):
            return real, rec[real]
    if max_depth <= 0:
        return None, None
    for k, v in rec.items():
        if isinstance(v, dict):
            p, val = _deep_find(v, *candidates, max_depth=max_depth - 1)
            if p:
                return f"{k}.{p}", val
        elif isinstance(v, list) and v and isinstance(v[0], dict):
            p, val = _deep_find(v[0], *candidates, max_depth=max_depth - 1)
            if p:
                return f"{k}[0].{p}", val
    return None, None


def _collect(payload, *candidates: str, limit: int = 50) -> list:
    """把列表响应里某个字段的所有取值收集起来(用于比较三个端点的 networkId 表示)"""
    vals = []
    for rec in _as_list(payload)[:limit]:
        _, v = _deep_find(rec, *candidates)
        if v is not None:
            vals.append(v)
    return vals


def _typed(v) -> str:
    """样本值 + 类型,#1 要判"数值还是字符串" """
    return f"{v!r}({type(v).__name__})"


# ============================================================
# probe:采集
# ============================================================
def _probe_fetch_all(client, handle: str | None) -> tuple[dict, dict]:
    """按设计文档 §2.1 的端点表逐个打,返回 (samples, ctx)"""
    samples: dict[str, tuple] = {}
    ctx: dict = {"handle": handle}

    # --- 排行榜:不依赖 handle,顺便在 --handle 缺省时挑个真实存在的样本用户 ---
    _, lb = _probe_get(client, samples, "leaderboard", "/v2/leaderboard?limit=20")
    if not handle:
        _, h = _deep_find(_first(lb), "handle", "userHandle", "username")
        handle = str(h) if h else DEFAULT_PROBE_HANDLE
        logger.info("未指定 --handle,取样本用户: {}", handle)
    ctx["handle"] = handle

    # --- handle → userId ---
    _, u = _probe_get(client, samples, "user_by_handle", f"/v2/users/userHandle/{quote(handle, safe='')}")
    _, uid = _deep_find(_first(u), "id", "userId", "_id", "uid")
    if not uid:
        logger.error("拿不到 userId,后续按用户维度的端点全部跳过 —— 先看 user_by_handle.json 的实际结构")
        return samples, ctx
    uid = str(uid)
    ctx["user_id"] = uid
    logger.info("样本 userId = {}", uid)

    _probe_get(client, samples, "user_detail", f"/v2/users/{quote(uid, safe='')}")

    # --- swaps:本项目最重要的端点,#1~#5 全靠它 ---
    _, swaps = _probe_get(client, samples, "swaps", f"/v2/users/{quote(uid, safe='')}/swaps")
    # #5 分页:同一批数据取两页,比对首条 id 是否不同 —— 分页不通则功能 A/B 永久降级
    _probe_get(client, samples, "swaps_page1", f"/v2/users/{quote(uid, safe='')}/swaps?limit=3")
    _probe_get(client, samples, "swaps_page2", f"/v2/users/{quote(uid, safe='')}/swaps?limit=3&offset=3")

    _, bal = _probe_get(client, samples, "balances", f"/v2/users/{quote(uid, safe='')}/balances")

    # --- 持仓聚合快照:#7 最高优先级探索项(能一次拿到 per-token 首次买入时间就不用翻页了) ---
    # timestamp 单位未知,秒和毫秒各打一次,谁返回 200 谁就是对的
    ms = int(datetime.now(UTC).timestamp() * 1000)
    st, _ = _probe_get(client, samples, "aggregated_snapshot_ms",
                       f"/v2/userTokens/aggregatedSnapshot?userId={quote(uid, safe='')}&timestamp={ms}")
    if st != 200:
        _probe_get(client, samples, "aggregated_snapshot_sec",
                   f"/v2/userTokens/aggregatedSnapshot?userId={quote(uid, safe='')}&timestamp={ms // 1000}")

    _probe_get(client, samples, "transfers", f"/v2/transfers/with/{quote(uid, safe='')}")

    # --- thesis:需要一个真实的 (tokenAddress, networkId) 样本 ---
    # ⚠️ networkId 必须用 API **原样返回**的值,不能用 normalize 之后的 ——
    #    归一化是给本地聚合键用的,查询参数得按人家的表示法来
    ca_path, ca = _deep_find(_first(bal) or _first(swaps), "tokenAddress", "address", "mint", "contractAddress")
    net_path, net_raw = _deep_find(_first(bal) or _first(swaps), "networkId", "chainId", "network", "chain")
    ctx["token_address"] = ca
    ctx["network_raw"] = net_raw
    ctx["ca_path"], ctx["net_path"] = ca_path, net_path
    if ca:
        q = f"/feed/token/thesis?tokenAddress={quote(str(ca), safe='')}&limit=100"
        if net_raw is not None:
            q += f"&networkId={quote(str(net_raw), safe='')}"
        _probe_get(client, samples, "thesis", q)
    else:
        logger.warning("没能从 balances/swaps 里取到样本 tokenAddress,thesis 端点跳过")

    _probe_get(client, samples, "feed", "/feed?limit=20")
    return samples, ctx


# ============================================================
# probe:核对表
# ============================================================
def _probe_report(samples: dict, ctx: dict) -> list[tuple]:
    """逐项生成设计文档 §11 的 17 行核对表:(编号, 待验证项, 实际字段名, 样本值, 结论)"""
    rows: list[tuple] = []

    def add(no, topic, path, value, verdict):
        rows.append((no, topic, path or "(未找到)", value if value not in (None, "") else "—", verdict))

    def payload(name):
        return samples.get(name, (None, None))[1]

    def status(name):
        return samples.get(name, (None, None))[0]

    swaps, bal, tr = payload("swaps"), payload("balances"), payload("transfers")
    s0, b0, t0 = _first(swaps), _first(bal), _first(tr)

    # ---- #1 swaps.networkId(唯一的硬阻断项)----
    p, v = _deep_find(s0, "networkId", "chainId", "network", "chain", "chainName")
    add(1, "swaps 有无 networkId / 类型", p, _typed(v) if p else None, OK if p else NG)

    # ---- #2 唯一 id + txHash ----
    pid, vid = _deep_find(s0, "id", "_id", "swapId", "uuid")
    ptx, vtx = _deep_find(s0, "txHash", "transactionHash", "signature", "hash", "tx")
    add(2, "swaps 稳定唯一 id",  pid, vid, OK if pid else WARN)
    add(2, "swaps txHash(拆单去重必需)", ptx, vtx, OK if ptx else NG)

    # ---- #3 时间字段与单位(最危险的一项)----
    pts, vts = _deep_find(s0, "timestamp", "blockTime", "createdAt", "time", "tradedAt", "date", "ts")
    iso, fb = to_iso(vts) if pts else (None, True)
    add(3, "swaps 时间字段 → 解析结果", pts, f"{vts!r} → {(iso or '解析失败')[:16]}" if pts else None,
        WARN if (pts and not fb) else NG)

    # ---- #4 买卖方向 ----
    pside, vside = _deep_find(s0, "side", "type", "direction", "action", "tradeType", "isBuy", "kind")
    pin, _ = _deep_find(s0, "tokenIn", "inputToken", "fromToken", "sellToken")
    pout, _ = _deep_find(s0, "tokenOut", "outputToken", "toToken", "buyToken")
    both = "双侧记录" if (pin and pout) else "单侧记录"
    add(4, f"swaps 买卖方向({both})", pside or f"{pin}/{pout}",
        vside or ("无 side 字段 → 只能按非计价币侧定方向" if (pin and pout) else None),
        OK if (pside or (pin and pout)) else NG)

    # ---- #5 分页 ----
    id_keys = (pid, "id", "_id", "txHash", "signature", "hash")
    a = _deep_find(_first(payload("swaps_page1")), *[k for k in id_keys if k])[1]
    b = _deep_find(_first(payload("swaps_page2")), *[k for k in id_keys if k])[1]
    n1, n2 = len(_as_list(payload("swaps_page1"))), len(_as_list(payload("swaps_page2")))
    if a and b and a != b:
        verdict, note = OK, f"limit=3 生效({n1}/{n2} 条),offset 翻页有效"
    elif a and b:
        verdict, note = NG, "offset 无效:两页首条相同 → 换 cursor/before 再试"
    else:
        verdict, note = WARN, f"页1={n1} 条 页2={n2} 条,首条 id 取不到,人工看 swaps_page*.json"
    add(5, "swaps 分页(limit/offset)", "limit&offset", note, verdict)

    # ---- #6 balances 字段齐全 ----
    pca, vca = _deep_find(b0, "tokenAddress", "address", "mint", "contractAddress")
    pnet, vnet = _deep_find(b0, "networkId", "chainId", "network", "chain")
    pusd, vusd = _deep_find(b0, "usdValue", "valueUsd", "usdAmount", "balanceUsd", "value")
    add(6, "balances tokenAddress", pca, vca, OK if pca else NG)
    add(6, "balances networkId", pnet, vnet, OK if pnet else NG)
    add(6, "balances usdValue(dust 判定)", pusd, vusd, OK if pusd else WARN)
    nets_b = {str(x) for x in _collect(bal, "networkId", "chainId", "network", "chain")}
    add(6, "balances 是否一次返回全部链", "networkId 取值集合", sorted(nets_b),
        OK if len(nets_b) > 1 else WARN)

    # ---- #7 aggregatedSnapshot 是否直接给 per-token 首次买入时间 ----
    snap = payload("aggregated_snapshot_ms") or payload("aggregated_snapshot_sec")
    sc = status("aggregated_snapshot_ms") or status("aggregated_snapshot_sec")
    pfb, vfb = _deep_find(_first(snap), "firstBuyAt", "firstBuy", "firstPurchaseAt", "firstBoughtAt", "firstTradeAt")
    ptb, vtb = _deep_find(_first(snap), "totalBought", "totalBuyUsd", "boughtUsd", "costBasis", "totalCost")
    add(7, "aggregatedSnapshot 首次买入时间", pfb, f"HTTP {sc} | {vfb}", OK if pfb else WARN)
    add(7, "aggregatedSnapshot 累计买入额", ptb, vtb, OK if ptb else WARN)

    # ---- #8 交易次数(Q3 单向否决票)----
    ptc, vtc = _deep_find(s0 or b0, "tradeCount", "txCount", "tradesCount", "numTrades", "transactionCount")
    add(8, "交易次数字段(Q3 否决票)", ptc, vtc, WARN if ptc else NA)

    # ---- #9 均价 / 成本价 ----
    pap, vap = _deep_find(b0 or _first(snap), "avgPrice", "averagePrice", "avgBuyPrice", "costBasis", "entryPrice")
    add(9, "per-token 均价(绝不本地推算)", pap, vap, OK if pap else NA)

    # ---- #10 三处 networkId 表示是否一致 ----
    nets_s = {str(x) for x in _collect(swaps, "networkId", "chainId", "network", "chain")}
    nets_t = {str(x) for x in _collect(tr, "networkId", "chainId", "network", "chain")}
    # ⚠️ 这里比的是**表示法**,不是"三处覆盖的链是否相同" ——
    #    balances 比 swaps 多几条链完全正常(持仓本来就比近期交易广),按集合相等判会一直报假警。
    #    真正的风险只有一个:同一条链在两处写法不同、且 alias 表没盖住 → 裂成两个聚合键,共识计数直接错。
    forms: dict[str, set[str]] = {}
    for group in (nets_s, nets_b, nets_t):
        for v in group:
            forms.setdefault(str(normalize_network(v)), set()).add(v)
    unknown = {k: sorted(v) for k, v in forms.items() if k not in set(known_networks().values())}
    multi = {k: sorted(v) for k, v in forms.items() if len(v) > 1}
    if unknown:
        note = f"未收录的链标识 {unknown} → 确认各指哪条链后补进 models._NETWORK_ALIASES"
        verdict = WARN
    else:
        note = f"swaps={sorted(nets_s)} balances={sorted(nets_b)} transfers={sorted(nets_t)}"
        note += f" | 同链多写法 {multi}(alias 已覆盖)" if multi else ""
        verdict = OK
    add(10, "三处 networkId 表示是否一致", "networkId", note, verdict)

    # ---- #11 transfers 方向 / 对手方 / 是否与 swaps 重合 ----
    pdir, vdir = _deep_find(t0, "direction", "type", "kind", "side")
    pcp, vcp = _deep_find(t0, "counterparty", "fromAddress", "toAddress", "from", "to", "peer")
    add(11, "transfers 方向字段", pdir, vdir, OK if pdir else NG)
    add(11, "transfers 对手方字段", pcp, vcp, OK if pcp else WARN)
    tx_s = {str(x) for x in _collect(swaps, "txHash", "transactionHash", "signature", "hash")}
    tx_t = {str(x) for x in _collect(tr, "txHash", "transactionHash", "signature", "hash")}
    dup = tx_s & tx_t
    add(11, "transfers 是否含 swap 产生的转账", "txHash 交集",
        f"{len(dup)} 条重合" + (f" 例:{sorted(dup)[0][:20]}…" if dup else ""),
        NG if dup else OK)

    # ---- #12 thesis 是否同时给 tokenAddress 与 networkId ----
    th0 = _first(payload("thesis"))
    pth_ca, vth_ca = _deep_find(th0, "tokenAddress", "address", "mint")
    pth_net, vth_net = _deep_find(th0, "networkId", "chainId", "network")
    pth_t, vth_t = _deep_find(th0, "createdAt", "timestamp", "time", "afterTime", "postedAt")
    add(12, "thesis tokenAddress", pth_ca, vth_ca, OK if pth_ca else WARN)
    add(12, "thesis networkId(缺则观点无共识)", pth_net, vth_net, OK if pth_net else NG)
    add(12, "thesis 时间字段(afterTime 单位)", pth_t,
        f"{vth_t!r} → {(to_iso(vth_t)[0] or '解析失败')[:16]}" if pth_t else None,
        WARN if pth_t else NA)

    # ---- #13 计价币真实 CA ----
    found, missing = _scan_quote_tokens(bal, swaps)
    # ⚠️ "不在白名单"不等于"要加进白名单":链上假 USDC 遍地,按 symbol 找出来的候选
    #    必须人工核对 CA 才能补,否则会把真币当计价币直接排除出功能 A/B。
    add(13, "计价币 CA 是否已在 QUOTE_TOKENS", "symbol+address",
        f"发现 {len(found)} 个,其中 {len(missing)} 个不在白名单(需人工核对真伪)",
        OK if not missing else WARN)

    # ---- #14~#17 ----
    # 延迟导入,与本文件其余 client 引用保持一致(避免模块级就把 client 拉起来)
    from src.client import SUPPORTED_CHAINS

    codes = {k: v[0] for k, v in samples.items() if v[0] is not None}
    add(14, "token 过期返回 401 还是 403", "(需等 token 过期后重跑)", f"本次状态码 {codes}", NA)
    add(15, "X-Supported-Chains 取值格式", "(client 内部请求头)",
        f"当前发送 {SUPPORTED_CHAINS!r};若 #10 出现未收录链标识,改 client.SUPPORTED_CHAINS 再重跑对比",
        WARN)
    # 响应头统一小写后再找,不同实现的大小写不一致
    _rl = sorted({
        k for h in _PROBE_HEADERS.values() for k in (h or {})
        if "ratelimit" in k.lower().replace("-", "") or k.lower() == "retry-after"
    })
    add(16, "有无 X-RateLimit-* 响应头", "(响应头)",
        f"发现 {_rl}(seeding 分页间隔按此调)" if _rl else "未发现限流头,分页间隔按保守值走",
        OK if _rl else WARN)
    first_ok = any(c == 200 for c in codes.values())
    add(17, "带 Bearer 后 Cloudflare 是否放行", "HTTP status", f"{codes}",
        OK if first_ok else NG)
    return rows


def _scan_quote_tokens(bal, swaps) -> tuple[list, list]:
    """
    #13:把响应里出现的计价币 (network, address, symbol) 抠出来,与 models.QUOTE_TOKENS 比对。

    ⚠️ 白名单必须按 (network_id, address) 填,**绝不能按 symbol** —— 链上假 USDC 遍地,
       按 symbol 判会把真币误当计价币直接排除。这里按 symbol 只是为了"找出候选给人工确认"。
    """
    quote_symbols = {"SOL", "WSOL", "ETH", "WETH", "BNB", "WBNB", "USDC", "USDT", "USDBC", "DAI"}
    found, missing = [], []
    for rec in (_as_list(bal) + _as_list(swaps))[:200]:
        _, sym = _deep_find(rec, "symbol", "tokenSymbol", "ticker")
        if not sym or str(sym).upper() not in quote_symbols:
            continue
        _, addr = _deep_find(rec, "tokenAddress", "address", "mint", "contractAddress")
        _, net = _deep_find(rec, "networkId", "chainId", "network", "chain")
        key = (normalize_network(net), normalize_token_address(addr))
        if not key[0] or not key[1]:
            continue
        item = (key[0], key[1], str(sym).upper())
        if item in found:
            continue
        found.append(item)
        if key not in QUOTE_TOKENS:
            missing.append(item)
    return found, missing


def _print_report(rows: list[tuple], samples: dict, ctx: dict) -> None:
    _out("")
    _out("=" * 118)
    _out(f"  FOMO API 字段核对表(设计文档 §11)  样本用户: {ctx.get('handle')}  userId: {ctx.get('user_id')}")
    _out("=" * 118)
    _out(f"{_fit('#', 4)}{_fit('待验证项', 38)}{_fit('实际字段名', 28)}{_fit('样本值', 34)}结论")
    _out("-" * 118)
    for no, topic, path, value, verdict in rows:
        _out(f"{_fit(no, 4)}{_fit(topic, 38)}{_fit(path, 28)}{_fit(value, 34)}{verdict}")
    _out("-" * 118)
    bad = [r for r in rows if r[4] == NG]
    warn = [r for r in rows if r[4] == WARN]
    _out(f"❌ 不符合 {len(bad)} 项 · ⚠️ 待人工核对 {len(warn)} 项 · 共 {len(rows)} 行")
    if bad:
        _out("❌ 以下项必须先解决,否则 normalize() 写出来就是错的:")
        for r in bad:
            _out(f"   #{r[0]} {r[1]} → {r[3]}")
    _out("")

    # ---- 计价币候选:直接给出可粘贴进 models.QUOTE_TOKENS 的行 ----
    found, missing = _scan_quote_tokens(samples.get("balances", (None, None))[1],
                                        samples.get("swaps", (None, None))[1])
    if found:
        _out("【#13 计价币候选】(核对无误后补进 models.QUOTE_TOKENS,注意按 CA 而非 symbol)")
        for net, ca, sym in found:
            flag = "  ← 白名单缺失" if (net, ca, sym) in missing else ""
            _out(f'    ("{net}", "{ca}"),   # {sym}{flag}')
        _out("")

    # ---- 样本记录:字段名核对最终还是得肉眼看一眼原始结构 ----
    for name in ("swaps", "balances", "transfers", "thesis"):
        rec = _first(samples.get(name, (None, None))[1])
        if not rec:
            continue
        _out(f"【{name} 首条记录】")
        txt = json.dumps(_sanitize(rec), ensure_ascii=False, indent=2, default=str)
        _out(txt[:1500] + ("\n  …(完整内容见 dump 文件)" if len(txt) > 1500 else ""))
        _out("")
    _out(f"原始响应已 dump 到: {PROBE_DIR}")
    _out("⚠️ #3(时间戳单位)必须用一笔已知时间的真实交易人工核对 —— 判错会导致'永远拉不到新数据'的静默失效")


# ============================================================
# 命令
# ============================================================
def cmd_init_db() -> int:
    """只建表。main() 里已经 init 过一次(幂等),这里只把结果亮出来供人工确认"""
    with store.get_conn() as conn:
        tables = [r[0] for r in conn.execute(
            "SELECT name FROM sqlite_master WHERE type = 'table' ORDER BY name"
        ).fetchall()]
    logger.info("✅ {} | 表: {}", store.DB_PATH, ", ".join(tables))
    return 0


def cmd_login(cdp_url: str | None = None) -> int:
    """有头浏览器登录,程序不经手密码"""
    from src.auth import interactive_login

    if cdp_url:
        logger.info("将 attach 到你自己启动的浏览器: {}", cdp_url)
    else:
        logger.info("即将打开浏览器,请在窗口里自行完成 FOMO 登录(本程序不经手你的账号密码)")
    try:
        ok = interactive_login(cdp_url=cdp_url)
    except Exception as e:  # noqa: BLE001
        logger.exception("登录流程异常: {}", e)
        return 1
    if ok:
        logger.info("✅ 登录态已保存: {}(该文件含 refresh token,已在 .gitignore)", SESSION_FILE)
        return 0
    logger.error("❌ 未获取到登录态,请重试 --login")
    return 1


def cmd_check() -> int:
    """连通性自检:配置 / 登录态 / Telegram / DB"""
    s = get_settings()
    problems: list[str] = []

    logger.info("=" * 60)
    logger.info("【连通性自检】")
    logger.info("=" * 60)

    # --- 1/4 配置 ---
    logger.info("--- 1/4 配置 ---")
    logger.info("client 实现   : {}", s.fomo_client_impl)
    logger.info("轮询间隔      : {}s · 推送间隔 {}s · 回填 {} 条",
                s.fomo_poll_interval_sec, s.fomo_send_interval_sec, s.fomo_backfill_max_items)
    logger.info("代理          : {}", s.fomo_proxy or "(未配置,国内大概率连不上 TG)")
    logger.info("Bot Token     : {}", mask(s.tg_token))
    logger.info("推送 chat_id  : {}", s.fomo_telegram_chat_id or "(未配置)")
    logger.info("命令 chat_id  : {}", s.admin_chat_id or "(未配置)")
    if not s.tg_enabled:
        problems.append("Telegram 未配置(FOMO_TELEGRAM_BOT_TOKEN / CHAT_ID)")
    if not s.admin_chat_id:
        # 没有 admin 就没有命令白名单,bot 层会拒绝启动 —— 这是安全策略不是 bug
        problems.append("admin_chat_id 为空,命令层将拒绝启动")

    # --- 2/4 登录态 ---
    logger.info("--- 2/4 FOMO 登录态 ---")
    if SESSION_FILE.exists():
        st = SESSION_FILE.stat()
        logger.info("✅ {} | {} 字节 | 修改于 {}", SESSION_FILE.name, st.st_size,
                    datetime.fromtimestamp(st.st_mtime, tz=UTC).isoformat(timespec="seconds"))
    else:
        problems.append("未登录:先跑 --login")
        logger.error("❌ 找不到 {},先跑 --login", SESSION_FILE)

    # 浏览器 profile 是**第二套凭据**,与上面那份 API token 完全独立:
    # 跟单的真实下单只认它,而全项目没有任何代码会自动续期它。
    # ⚠️ 只做不开浏览器的粗查 —— --check 要能在几秒内跑完。
    #    真要确认还登录着,跟单开自动之前会跑 executor.check_login()。
    from src.executor import profile_looks_present

    ok, why = profile_looks_present()
    if ok:
        logger.info("✅ 买入用的浏览器 profile:{}", why)
    else:
        # 不进 problems:没打算用跟单的人不该因为这个看到"自检未通过"
        logger.warning("⚠️ 买入用的浏览器 profile:{}(只影响跟单下单,不影响监控)", why)

    # --- 3/4 DB ---
    logger.info("--- 3/4 数据库 ---")
    try:
        store.init_db()
        with store.get_conn() as conn:
            active = len(store.list_active_users(conn))
            ready = len(store.ready_user_ids(conn))
        logger.info("✅ 建表成功 | 名单 {} 人 · 基线就绪 {} 人", active, ready)
    except Exception as e:  # noqa: BLE001
        logger.exception("❌ 数据库不可用: {}", e)
        problems.append(f"数据库不可用: {e}")

    # --- 4/4 Telegram ---
    logger.info("--- 4/4 Telegram 推送 ---")
    tg = TelegramNotifier()
    if tg.enabled:
        if tg.send(f"✅ FOMO 监控自检 · {now_iso()}"):
            logger.info("✅ 已推送测试消息,请看手机")
        else:
            problems.append("Telegram 推送失败(看上面的错误;国内通常是没配代理)")
    else:
        logger.warning("⚠️ Telegram 未配置,跳过")

    logger.info("=" * 60)
    if problems:
        for p in problems:
            logger.error("❌ {}", p)
        return 1
    logger.info("✅ 自检全部通过")
    return 0


def cmd_probe(handle: str | None) -> int:
    """Phase 0 硬门槛:打全部端点 + dump 原始 JSON + 打印字段核对表"""
    from src.auth import AuthError
    from src.client import build_client

    s = get_settings()
    logger.info("probe 开始 | client={} | dump 目录={}", s.fomo_client_impl, PROBE_DIR)
    try:
        client = build_client()
    except Exception as e:  # noqa: BLE001
        logger.exception("client 初始化失败: {}", e)
        return 1

    try:
        samples, ctx = _probe_fetch_all(client, handle)
    except AuthError as e:
        logger.error("❌ 未登录 / 登录态失效: {} —— 先跑 .\\bot.ps1 --login", e)
        return 2
    except Exception as e:  # noqa: BLE001
        logger.exception("probe 采集异常: {}", e)
        return 1

    if not samples:
        logger.error("❌ 一个端点都没打通,先看日志里的 HTTP 状态码")
        return 1

    try:
        rows = _probe_report(samples, ctx)
        _print_report(rows, samples, ctx)
    except Exception as e:  # noqa: BLE001
        # 核对表挂了不能让已经拿到的 dump 白费 —— 原始文件才是真正的产出
        logger.exception("核对表生成失败(dump 文件仍然可用): {}", e)
        return 1
    return 0


def cmd_dry_run() -> int:
    """跑一次 tick,只打印不推送"""
    from src.client import build_client
    from src.poller import Poller

    try:
        poller = Poller(build_client(), TelegramNotifier())
        n = poller.tick(dry_run=True)
    except Exception as e:  # noqa: BLE001
        logger.exception("dry-run 失败: {}", e)
        return 1
    logger.info("✅ dry-run 完成,本轮新事件 {} 条(未推送)", n)
    return 0


def cmd_run() -> int:
    """
    正式运行:poller 跑在主线程的 BlockingScheduler 上,bot 命令层跑 daemon 线程。

    ⚠️ max_instances=1 + coalesce=True 是必须的:一次 tick 卡住 60s 的话,
       默认配置会把积压的 3 次 tick 一起放出来并发拉取,直接把写锁和 API 限流一起打爆。
    """
    from apscheduler.schedulers.blocking import BlockingScheduler

    from src.auth import AuthError, RetryableAuthError
    from src.bot import CommandBot
    from src.client import build_client
    from src.poller import Poller

    s = get_settings()
    notifier = TelegramNotifier()
    try:
        client = build_client()
    except Exception as e:  # noqa: BLE001
        logger.exception("client 初始化失败: {}", e)
        return 1
    poller = Poller(client, notifier)

    stop_event = threading.Event()
    bot = CommandBot(client, notifier, poller=poller)
    threading.Thread(target=bot.run_forever, args=(stop_event,), name="fomo-bot", daemon=True).start()

    sched = BlockingScheduler(timezone="UTC")

    # 连续多少轮拿到"可重试的登录态错误"才真的当成失效。
    # 网络抖一下就停机是不可接受的:bot.ps1 没有守护进程,停了就一直停着。
    retry_tolerance = 5
    fail_streak = {"n": 0}

    slow_warned = {"done": False}

    def _tick_job() -> None:
        t0 = time.monotonic()
        try:
            n = poller.tick()
            # /status 要显示"最近一次 tick 时间"。Poller 契约里没有这个字段,
            # 由持有调度器的这里回填 —— 且**只记成功的 tick**:
            # 失败时保持旧值不动,/status 上那个不再前进的时间戳本身就是"管道坏了"的信号。
            poller.last_tick_at = now_iso()
            fail_streak["n"] = 0
            dt = time.monotonic() - t0
            if n:
                logger.info("tick 完成,新事件 {} 条 · 耗时 {:.0f}s", n, dt)
            # ⚠️ 一轮跑不完一个间隔时,APScheduler 只会甩一句
            #    "skipped: maximum number of running instances reached" ——
            #    看着像出错,其实只是 tick 连轴转(coalesce 保证不堆积、不丢数据)。
            #    这里给一句能照着做的话,且只说一次,免得刷屏。
            if dt > s.fomo_poll_interval_sec and not slow_warned["done"]:
                slow_warned["done"] = True
                logger.warning(
                    "单轮耗时 {:.0f}s > 轮询间隔 {}s —— tick 会连轴转。"
                    "数据不会丢(coalesce 已开),但日志里会一直刷 'skipped ... max instances'。"
                    "建议把 .env 的 FOMO_POLL_INTERVAL_SEC 调到 {} 以上后重启。"
                    "(基线全部建好之后耗时会明显下降,届时可以再调回来)",
                    dt, s.fomo_poll_interval_sec, int(dt * 1.5),
                )
        except RetryableAuthError as e:
            # 网络抖动 / 代理断流 / Privy 5xx —— 不是"你被登出了"。
            # ⚠️ 直接当成 AuthError 停机是这条链上最贵的误判:
            #    用户收到"请重新 --login",但 session 文件根本没坏,重登是白做的,
            #    而真实原因(一次网络抖动)被完全掩盖,监控在人工发现前一直停着。
            fail_streak["n"] += 1
            logger.warning("续期暂时失败({}/{} 轮),下一轮重试: {}",
                           fail_streak["n"], retry_tolerance, e)
            if fail_streak["n"] >= retry_tolerance:
                logger.error("连续 {} 轮续期失败,按登录态失效处理", retry_tolerance)
                notifier.send(
                    f"🔐 <b>FOMO 登录态可能失效</b>\n"
                    f"连续 {retry_tolerance} 轮续期失败,轮询已停止。\n"
                    f"先确认网络/代理正常;仍不行就执行 <code>.\\bot.ps1 --login</code> 后重启。"
                )
                sched.shutdown(wait=False)
        except AuthError as e:
            # 设计文档 §3.5:续期失败 → TG 告警 + 停止轮询,不空转刷日志
            logger.error("登录态失效,停止轮询: {}", e)
            notifier.send(
                "🔐 <b>FOMO 登录态失效</b>\n"
                "轮询已停止。请到服务器执行 <code>.\\bot.ps1 --login</code> 后重启进程。"
            )
            sched.shutdown(wait=False)  # wait=True 会在自己的 job 里死锁
        except Exception as e:  # noqa: BLE001
            # 单轮失败不退出:网络抖动是常态,下一轮自然重试
            logger.exception("tick 异常,下一轮继续: {}", e)

    sched.add_job(
        _tick_job, "interval",
        seconds=s.fomo_poll_interval_sec,
        id="fomo_tick",
        max_instances=1,
        coalesce=True,
        misfire_grace_time=s.fomo_poll_interval_sec,
        next_run_time=datetime.now(UTC),  # 别等第一个间隔,立刻跑一轮
    )

    logger.info("=" * 60)
    logger.info("FOMO 监控启动 | 轮询 {}s | client={} | Ctrl+C 退出",
                s.fomo_poll_interval_sec, s.fomo_client_impl)
    logger.info("=" * 60)
    try:
        sched.start()
    except (KeyboardInterrupt, SystemExit):
        logger.info("收到退出信号,正在停止…")
    finally:
        # ⚠️ 顺序要紧:**先喊停在途请求**,再关调度器。
        #    只关调度器的话,线程池里还排着几十个请求会一个个跑完(每个还带重试退避),
        #    Ctrl+C 要等十几秒才真的退出 —— 用户只能连按好几次。
        request_stop()
        stop_event.set()
        if sched.running:
            sched.shutdown(wait=False)
        # 观点线程池是常驻的,不关的话 atexit 会 join 它,而它可能卡在 HTTP 超时里
        with suppress(Exception):
            poller.close()
        with suppress(Exception):
            client.close()
    logger.info("已退出")
    return 0


# ============================================================
# 入口
# ============================================================
def cmd_capture_buy(ca: str, network: str, amount: float) -> int:
    """
    抓「点了成交之后页面会出现什么」的开发工具。

    📌 2026-08-12 的结论:**点下去就是成交,没有二次确认、没有滑块**
       (曾怀疑有个滑块要拖,用户真实买入 $1.90 验证过 —— 没有)。
       所以现在执行器不需要它;留着是为了 FOMO 改买入流程时能再抓一次。

    ⚠️ 本命令**从不点成交按钮**。它把页面开好、金额填好,然后停下来等**你**点;
       你点的那一刻它开始每 0.5s 采一次 DOM,把新出现的控件/弹窗结构和截图存下来。
       分工是刻意的:下单是你的动作,抓结构是程序的活。
    ⚠️ 采样要采**元素属性**(role / aria-valuenow / type=range / draggable / 位置尺寸),
       不能只截图 —— 光看图写不出定位器。
    """
    from src.auth import _DROP_DEFAULT_ARGS, _LOGIN_CHANNELS, _STEALTH_ARGS
    from src.executor import TOKEN_URL

    slug = NETWORK_SLUG.get((network or "").strip())
    if not slug:
        _out(f"❌ 不支持的链: {network}")
        return 1

    s = get_settings()
    out_dir = PROBE_DIR / "buyflow"
    out_dir.mkdir(parents=True, exist_ok=True)
    url = TOKEN_URL.format(slug=slug, ca=ca)

    # 采样脚本:把"可能是滑块/确认控件"的元素连同属性一起吐出来
    probe_js = """() => {
      const out = [];
      const sel = 'button,[role=slider],input[type=range],[draggable=true],'
                + '[role=dialog],[aria-valuenow],[class*=slid],[class*=Slid],'
                + '[class*=drag],[class*=Drag],[class*=confirm],[class*=Confirm]';
      document.querySelectorAll(sel).forEach(el => {
        const r = el.getBoundingClientRect();
        if (r.width < 4 || r.height < 4) return;
        out.push({
          tag: el.tagName.toLowerCase(),
          role: el.getAttribute('role') || '',
          type: el.getAttribute('type') || '',
          aria: el.getAttribute('aria-label') || '',
          valuenow: el.getAttribute('aria-valuenow') || '',
          valuemax: el.getAttribute('aria-valuemax') || '',
          draggable: el.getAttribute('draggable') || '',
          testid: el.getAttribute('data-testid') || '',
          cls: (el.className || '').toString().slice(0, 90),
          text: (el.innerText || '').trim().slice(0, 60),
          box: [Math.round(r.x), Math.round(r.y), Math.round(r.width), Math.round(r.height)],
        });
      });
      return out;
    }"""

    from playwright.sync_api import sync_playwright

    base: dict = {
        "user_data_dir": str(PROFILE_DIR), "headless": False, "locale": "en-US",
        "args": list(_STEALTH_ARGS), "ignore_default_args": list(_DROP_DEFAULT_ARGS),
    }
    if s.fomo_proxy:
        base["proxy"] = {"server": s.fomo_proxy}

    with sync_playwright() as p:
        ctx = None
        for ch in _LOGIN_CHANNELS:
            try:
                kw = dict(base)
                if ch:
                    kw["channel"] = ch
                ctx = p.chromium.launch_persistent_context(**kw)
                break
            except Exception as e:  # noqa: BLE001
                logger.debug("{} 启动失败: {}", ch, e)
        if ctx is None:
            _out("❌ 浏览器起不来(profile 可能正被另一个窗口占用,关掉再试)")
            return 1

        try:
            page = ctx.pages[0] if ctx.pages else ctx.new_page()
            page.set_default_timeout(45_000)
            page.goto(url, wait_until="domcontentloaded")
            page.wait_for_timeout(8000)

            amount_input = page.locator('input[placeholder="0"]').first
            if not amount_input.count():
                _out("❌ 找不到金额输入框,页面结构可能变了")
                return 1
            amount_input.fill(f"{amount:.2f}")
            page.wait_for_timeout(2000)

            before = {json.dumps(x, sort_keys=True) for x in page.evaluate(probe_js)}
            page.screenshot(path=str(out_dir / "0_before.png"))

            _out("=" * 64)
            _out(f"金额已填 ${amount:.2f}。**现在请你自己点那个成交按钮** —— 本程序不会点。")
            _out("点完之后如果出现滑块,把它拖到底完成成交;我会全程记录结构。")
            _out(f"我会盯 120 秒,结果存到 {out_dir}")
            _out("=" * 64)

            seen, shots = set(), 0
            for i in range(240):                     # 240 × 0.5s = 120s
                page.wait_for_timeout(500)
                try:
                    now = page.evaluate(probe_js)
                except Exception:  # noqa: BLE001
                    continue                          # 页面正在跳转,下一轮再采
                fresh = [x for x in now
                         if json.dumps(x, sort_keys=True) not in before
                         and json.dumps(x, sort_keys=True) not in seen]
                if not fresh:
                    continue
                for x in fresh:
                    seen.add(json.dumps(x, sort_keys=True))
                shots += 1
                page.screenshot(path=str(out_dir / f"{shots}_step.png"))
                _out(f"\n--- 第 {i * 0.5:.1f}s 出现 {len(fresh)} 个新元素 ---")
                for x in fresh:
                    bits = [f"<{x['tag']}>"]
                    for k in ("role", "type", "aria", "valuenow", "valuemax",
                              "draggable", "testid"):
                        if x[k]:
                            bits.append(f"{k}={x[k]!r}")
                    if x["text"]:
                        bits.append(f"text={x['text']!r}")
                    bits.append(f"box={x['box']}")
                    bits.append(f"cls={x['cls']!r}")
                    _out("  " + " ".join(bits))
                (out_dir / f"{shots}_step.json").write_text(
                    json.dumps(fresh, ensure_ascii=False, indent=2), encoding="utf-8")

            _out(f"\n✅ 记录结束,共 {shots} 次变化。截图与 JSON 在 {out_dir}")
            return 0
        finally:
            ctx.close()


def main() -> int:
    parser = argparse.ArgumentParser(
        prog="fomo",
        description="FOMO 平台指定用户监控 → Telegram 推送",
    )
    parser.add_argument("--login", action="store_true",
                        help="打开浏览器手动登录 FOMO,保存登录态(程序不经手密码)")
    parser.add_argument("--cdp", metavar="URL", default=None,
                        help="配合 --login:attach 到你自己启动的 Chrome(例 http://127.0.0.1:9222)。"
                             "Google 第三方登录拒绝自动化浏览器时用这个 —— "
                             "先用 --remote-debugging-port=9222 启动 Chrome")
    parser.add_argument("--probe", action="store_true",
                        help="打全部 API 端点 + dump 原始 JSON + 打印字段核对表(Phase 0 必跑)")
    parser.add_argument("--handle", type=str, default=None, metavar="HANDLE",
                        help="--probe 的样本用户(缺省时从排行榜自动挑一个)")
    parser.add_argument("--check", action="store_true",
                        help="连通性自检:配置 / 登录态 / Telegram / 数据库")
    parser.add_argument("--dry-run", action="store_true",
                        help="跑一次 tick,只打印不推送")
    parser.add_argument("--run", action="store_true",
                        help="正式运行:轮询 + Telegram 命令层")
    parser.add_argument("--init-db", action="store_true",
                        help="只建表,不做别的")
    parser.add_argument("--capture-buy", metavar="CA", default=None,
                        help="抓买入流程:开页面填好金额后**停下来等你自己点**,"
                             "把点击后出现的滑块/弹窗结构抓下来(本程序全程不点成交)")
    parser.add_argument("--amount", type=float, default=2.0,
                        help="--capture-buy 填多少金额(默认 2)")
    parser.add_argument("--network", type=str, default="solana",
                        help="--capture-buy 的链,默认 solana")
    args = parser.parse_args()

    setup_logger()
    store.init_db()

    if args.login:
        return cmd_login(args.cdp)
    if args.probe:
        return cmd_probe(args.handle)
    if args.check:
        return cmd_check()
    if args.init_db:
        return cmd_init_db()
    if args.capture_buy:
        return cmd_capture_buy(args.capture_buy, args.network, args.amount)
    if args.dry_run:
        return cmd_dry_run()
    if args.run:
        return cmd_run()

    parser.print_help()
    return 1


if __name__ == "__main__":
    sys.exit(main())
