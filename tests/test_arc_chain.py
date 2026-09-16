"""
Arc 链(FOMO networkId 5042)接进来:别名 / 展示名 / 链接 slug / 数字链 ID / 计价币 /
请求头 / 老数据迁移 / pump.fun 链映射 / DexScreener 链片段。

⚠️ 断言一律**写死字面量**(5042 / "arc" / "Arc" / USDC 的 CA),绝不从被测模块 import 常量断言自己。
⚠️ 实测依据(2026-09-16,真实接口,全部离线不得复现,故记在这里):
   · filterTokens 查 ARCGUY:头里没有 5042 → HTTP 200 + **空数组**;补上 5042 → 正常返回
     (holders 263 / totalSupply 10 亿)。这正是 /chips 分母与持有人行的来源。
   · DexScreener:ARCGUY / CINU 的 chainId = "arc",池子**全部**对 USDC(0x3600…0000)计价。
   · fomo.family 代币页是单页应用(随便编的 slug 也回 200),slug 是用它的 og 卡片渲染器验的:
     /og/token/arc/<Arc 的币>/card.png 出真卡片,任何无效的「链+币」组合出的是同一张兜底图。
"""
# ruff: noqa: N802
from __future__ import annotations

import pytest

from src import bot as bot_mod
from src import client as client_mod
from src import pumpfun as pf
from src import store
from src.dexscreener import parse_pool_quotes
from src.formatter import render_pump_untracked
from src.models import (
    NETWORK_CHAIN_ID,
    NETWORK_DISPLAY,
    NETWORK_SLUG,
    is_quote_token,
    known_networks,
    normalize_network,
)
from tests.test_dexscreener import _pair

ARC_USDC = "0x3600000000000000000000000000000000000000"
ARCGUY = "0x3ca1fcfd26cefbb1339ce42cafe1a9dc6107fa12"


class Test链标识:
    @pytest.mark.parametrize("raw", ["5042", "arc", "ARC", " Arc "])
    def test_数字与名字都归一化成arc(self, raw):
        assert normalize_network(raw) == "arc"

    def test_核对表里也有(self):
        """--probe 的核对表读的是这份副本"""
        assert known_networks()["5042"] == "arc"

    def test_展示名(self):
        assert NETWORK_DISPLAY["arc"] == "Arc"

    def test_链接slug(self):
        assert NETWORK_SLUG["arc"] == "arc"

    def test_数字链id两处一致(self):
        """⚠️ 两张表分别给 boardholders / bot 用,写岔了 → 服务端 400 或查错链"""
        assert NETWORK_CHAIN_ID["arc"] == 5042
        assert bot_mod._NETWORK_RAW_ID["arc"] == "5042"

    def test_猜链顺序里有arc(self):
        """/ca 与 /chips 都靠它:不在里面,用户不显式指定链就永远找不到 Arc 的币"""
        assert "arc" in bot_mod.CA_EVM_GUESS_ORDER


class Test请求头:
    def test_supported_chains带上了5042(self):
        """
        ⚠️⚠️ 实测:不带 5042 时 Arc 的币返回 HTTP 200 + 空数组 —— 成功状态码配空数组,
           没有任何错误码、没有任何日志,/chips 的分母与持有人行静默取不到。
        """
        assert client_mod.SUPPORTED_CHAINS == "1,56,143,4663,5042,8453,1399811149"


class Test计价币:
    def test_arc上的usdc算计价币(self):
        assert is_quote_token("arc", ARC_USDC) is True

    def test_arc上的普通币不算(self):
        assert is_quote_token("arc", ARCGUY) is False

    def test_按链加地址判而不是按地址(self):
        """⚠️ 同一个地址串在别的链上不是计价币 —— 判据必须带链"""
        assert is_quote_token("base", ARC_USDC) is False


class Test老数据迁移:
    def test_库里已有的5042行会被改成arc(self, tmp_path):
        """
        ⚠️⚠️ 不迁移的话同一条链裂成两个聚合键("5042" 与 "arc" 各算一份):
           已经建过仓的币会被重新判成「首次建仓」,而徽章落库即冻结、错了就是永久的。
        """
        mp = pytest.MonkeyPatch()
        mp.setattr(store, "DB_PATH", tmp_path / "arc.db")
        try:
            store.init_db()
            with store.get_conn() as conn, store.tx(conn):
                conn.execute(
                    "INSERT INTO fomo_events (event_id, event_type, user_id, network_id,"
                    " token_address, event_ts, ingested_at, raw_json) VALUES (?,?,?,?,?,?,?,?)",
                    ("BUY:arc-1", "BUY", "u1", "5042", ARCGUY,
                     "2026-09-16T00:00:00+00:00", "2026-09-16T00:00:00+00:00", "{}"))
                conn.execute(
                    "INSERT INTO user_token_stats (user_id, network_id, token_address,"
                    " buy_count, updated_at) VALUES (?,?,?,?,?)",
                    ("u1", "5042", ARCGUY, 1, "2026-09-16T00:00:00+00:00"))
            store.init_db()                       # 幂等迁移:再跑一次就该改过来
            with store.get_conn() as conn:
                ev = conn.execute("SELECT network_id FROM fomo_events").fetchone()["network_id"]
                st = conn.execute("SELECT network_id FROM user_token_stats").fetchone()["network_id"]
            assert ev == "arc"
            assert st == "arc"
        finally:
            mp.undo()


class Test各平台的链映射:
    def test_pump的持仓行认得5042(self):
        pos = pf.parse_positions({"positions": [
            {"coinMint": ARCGUY, "chainId": 5042, "amountHeld": 1.0, "realizedPnlUsd": 0.0}]})
        assert pos[0].network_id == "arc"

    def test_dexscreener按arc这个chainId挑池子(self):
        """⚠️ 写错一个字母的后果:这条链每一条 pair 都被静默过滤掉,日志里一个字都没有"""
        p = _pair(ARCGUY, ARC_USDC, chain="arc")
        assert parse_pool_quotes([p], "arc", {ARCGUY}), "arc 的 chainId 不是实测过的那个"


class Test推送:
    def test_arc的推送有链名也有fomo链接(self):
        out = render_pump_untracked(username="bob", token_symbol="ARCGUY", coin_mint=ARCGUY,
                                    added_amount=10.0, amount_usd=60.0, network_id="arc")
        assert "🧬 Arc" in out, out
        assert f"https://fomo.family/tokens/arc/{ARCGUY}" in out, out

    def test_不给arc拼gmgn链接(self):
        """⚠️ GMGN 支持哪些链没实测过 —— 拼一个它不支持的链只会 404,错的链接比没有更糟"""
        out = render_pump_untracked(username="bob", token_symbol="ARCGUY", coin_mint=ARCGUY,
                                    added_amount=10.0, amount_usd=60.0, network_id="arc")
        assert "gmgn" not in out.lower(), out
