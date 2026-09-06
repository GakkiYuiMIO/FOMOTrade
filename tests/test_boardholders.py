"""
🏅 盈利榜持有人 —— 取值层(src/boardholders.py)。

============ 夹具是真实响应 ============
tests/fixtures/fomo_leaderboard_24h.json        2026-09-05 实拉的 24h 榜 **150 行**
tests/fixtures/fomo_top_holders_robinhood_meme.json   totalHolders=15305 / 返回 97
tests/fixtures/fomo_top_holders_solana_stonk.json     7375 / 96
tests/fixtures/fomo_top_holders_bsc_xaut.json         1365 / 99
tests/fixtures/fomo_top_holders_base_eval.json        296 / 78  ← 命中的两人是 #124 / #134
tests/fixtures/fomo_top_holders_robinhood_cleat.json  87 / 87   ← **精确**口径的样本
tests/fixtures/fomo_top_holders_monad_gmonad.json     1 / 1     ← 精确但零命中(整块消失)
tests/fixtures/fomo_top_holders_ethereum_asteroid.json 1405 / 97
tests/fixtures/fomo_top_holders_empty.json            0 / 0     ← 空响应
⚠️ 夹具值一个字节没改,只删掉了 profilePictureLink / coverPhotoLink / thumbhash /
   coverPhotoThumbhash / description / topHoldings 这几个与本功能无关的大字段
   (不删的话光榜单一个文件就 270KB)。**本模块读的每一个键都原样保留着。**

============ 断言写死字面量 ============
⚠️ 不从 src.boardholders / src.formatter import 任何阈值 / 常量 / 正则来断言自己 ——
   人数、名次、条数、措辞全部手抄在这个文件里。
"""
# ruff: noqa: N802
from __future__ import annotations

import ast
import json
import pathlib

import pytest

from src import boardholders as bh
from src.auth import AuthError

FIX = pathlib.Path(__file__).parent / "fixtures"


def load(name: str):
    return json.loads((FIX / name).read_text(encoding="utf-8"))


BOARD_RAW = load("fomo_leaderboard_24h.json")


@pytest.fixture
def board():
    return bh.parse_board(BOARD_RAW)


# ============================================================
# 假 client:记下每一次调用的**全部**参数
# ============================================================
class FakeClient:
    """
    ⚠️ 它记的是「上层到底传了什么」—— networkId 是不是数字、limit 是不是 150、
       auth_invalidate 是不是 False,全靠这里留证。
    """

    def __init__(self, board_payload=None, holders=None, board_error=None,
                 holders_error=None):
        self._board = BOARD_RAW if board_payload is None else board_payload
        self._holders = holders or {}
        self._board_error = board_error
        self._holders_error = holders_error
        self.board_calls: list[tuple] = []
        self.holder_calls: list[tuple] = []

    def get_leaderboard(self, period="24h", limit=20, *, auth_invalidate=True):
        self.board_calls.append((period, limit, auth_invalidate))
        if self._board_error is not None:
            raise self._board_error
        return self._board

    def get_top_holders(self, token_address, network_id, *, auth_invalidate=True):
        self.holder_calls.append((token_address, network_id, auth_invalidate))
        if self._holders_error is not None:
            raise self._holders_error
        return self._holders.get(token_address, {})

    def close(self):
        pass


def make(client, **kw):
    """每条用例一份**独立**的榜单缓存 —— 绝不碰进程级那把单例。"""
    kw.setdefault("board_cache", bh._BoardCache())
    return bh.BoardHoldersLookup(client, **kw)


CA_MEME = "0x385f4f8ae47651ce5f58f5265395a669f8281e18"
CA_EVAL = "0x10f52295e129817e8e4b28da0dab067321ec2222"
CA_CLEAT = "0x12d4e7c85e66b5260a9a4251620a63e08482ca1e"
CA_GMONAD = "0xfbc1f84ea41c0cf8a850a7f2b148ea8f43fb8f9e"
CA_STONK = "6GmAFSYs4gk3FDao5FzzySQpPZaWsa4rUJHacpMpUNgx"


# ============================================================
# 榜单解析
# ============================================================
class Test榜单解析:
    def test_一次拿到一百五十行(self):
        """⚠️ 服务端真实上限就是 150。夹具是实拉的那一份,150 行一个不少。"""
        assert len(BOARD_RAW) == 150

    def test_排名是返回顺序的一基下标(self, board):
        """
        ⚠️⚠️ 接口**没有** rank 字段。第一名的名次必须是 1(不是 0),
           最后一名是 150 —— 改成 0-based 这两条一起红。
        """
        first = board[str(BOARD_RAW[0]["id"])]
        last = board[str(BOARD_RAW[-1]["id"])]
        assert first.rank == 1
        assert last.rank == 150
        assert first.handle == "unipcs"
        assert first.followers == 542169
        assert first.pnl24h == 6002861.530720012

    def test_第二十八名与第一百二十四名各是谁(self, board):
        """⚠️ 中间与**第 100 名之后**各钉一个:夹在 100 时后者根本不存在。"""
        assert board["d676a05f-0b3e-57d1-86dc-d6986927c6c8"].rank == 28
        assert board["d676a05f-0b3e-57d1-86dc-d6986927c6c8"].handle == "Aurelius0121"
        by_rank = {r.rank: r.handle for r in board.values()}
        assert by_rank[124] == "BertLuvv"
        assert by_rank[134] == "EricCryptoman"
        assert by_rank[144] == "deliveryydriver"

    def test_解析不出来的那一项照样吃掉一个名次(self):
        """
        ⚠️⚠️ 名次是**下标**。少算一项 = 它后面所有人的名次整体前移一位,
           而那是一个不会报错、只会印错数字的失效。
        """
        rows = [{"id": "a"}, {"no-id": 1}, None, {"id": "b"}]
        got = bh.parse_board(rows)
        assert got["a"].rank == 1
        assert got["b"].rank == 4

    def test_不是列表就当空榜(self):
        assert bh.parse_board(None) == {}
        assert bh.parse_board({"responseObject": []}) == {}

    def test_粉丝为零是真实值不是缺失(self):
        got = bh.parse_board([{"id": "a", "followers": 0, "pnl24h": 0}])
        assert got["a"].followers == 0
        assert got["a"].pnl24h == 0.0

    def test_粉丝为负当缺失(self):
        got = bh.parse_board([{"id": "a", "followers": -3}])
        assert got["a"].followers is None


# ============================================================
# 交集 —— 两套口径
# ============================================================
class Test交集与口径:
    def test_下界口径_MEME(self, board):
        """robinhood 的 MEME:15305 个持有人只拿到 97 条 → **下界**。"""
        blk = bh.match_block(board, load("fomo_top_holders_robinhood_meme.json"), 150)
        assert blk.covered == 97
        assert blk.total == 15305
        assert blk.exact is False
        assert [r[0] for r in blk.rows] == [1, 28, 41, 46, 48, 52, 121, 130, 143, 147]
        assert blk.rows[1][:2] == (28, "Aurelius0121")

    def test_精确口径_CLEAT(self, board):
        """robinhood 的 CLEAT:87 个持有人全部拿到 → **精确**。"""
        blk = bh.match_block(board, load("fomo_top_holders_robinhood_cleat.json"), 150)
        assert (blk.covered, blk.total, blk.exact) == (87, 87, True)
        assert blk.rows == ((144, "deliveryydriver", 10609159.41, 1051,
                             167937.1535139446),)

    def test_夹在一百名时看不见的那两个人(self, board):
        """
        ⚠️⚠️ 这条是「limit 放到 150」的**独占**证据:base 的 EVAL 命中的两个人
           排在 **#124 / #134**,榜只拉 100 行时一个都不在。
        """
        blk = bh.match_block(board, load("fomo_top_holders_base_eval.json"), 150)
        assert [r[0] for r in blk.rows] == [124, 134]
        assert min(r[0] for r in blk.rows) > 100

    def test_零命中整块消失(self, board):
        """monad 的 Gmonad:1/1 精确,但榜上一个人都没有 → **None**,绝不是"0 人"。"""
        blk = bh.match_block(board, load("fomo_top_holders_monad_gmonad.json"), 150)
        assert blk is None

    def test_空响应整块消失(self, board):
        assert bh.match_block(board, load("fomo_top_holders_empty.json"), 150) is None

    def test_不是字典的响应整块消失(self, board):
        for bad in (None, [], "x", 3):
            assert bh.match_block(board, bad, 150) is None

    def test_三条链各自都能对上(self, board):
        for name, hits in (("solana_stonk", 13), ("bsc_xaut", 6),
                           ("ethereum_asteroid", 1)):
            blk = bh.match_block(board, load(f"fomo_top_holders_{name}.json"), 150)
            assert len(blk.rows) == hits, name

    def test_按_uuid_对齐而不是按_handle(self, board):
        """
        ⚠️⚠️ handle 随时可改、大小写还不稳定。这里造一个"handle 一模一样、
           但 id 不是榜上那个"的持有人:必须**认不出来**。
        """
        data = {"totalHolders": 1,
                "topHolders": [{"humanAmount": 1.0,
                                "user": {"id": "not-on-the-board",
                                         "userHandle": "unipcs"}}]}
        assert bh.match_block(board, data, 150) is None

    def test_同一个人出现两次只算一次(self, board):
        uid = str(BOARD_RAW[0]["id"])
        data = {"totalHolders": 2,
                "topHolders": [{"humanAmount": 5.0, "user": {"id": uid}},
                               {"humanAmount": 1.0, "user": {"id": uid}}]}
        blk = bh.match_block(board, data, 150)
        assert len(blk.rows) == 1
        assert blk.rows[0][2] == 5.0        # 取头一条(按持仓市值降序的第一笔)

    def test_自报总数比明细还少时以明细为准且不敢称精确(self, board):
        uid = str(BOARD_RAW[0]["id"])
        data = {"totalHolders": 1,
                "topHolders": [{"humanAmount": 5.0, "user": {"id": uid}},
                               {"humanAmount": 1.0, "user": {"id": "x"}}]}
        blk = bh.match_block(board, data, 150)
        assert blk.covered == 2
        assert blk.total == 2
        assert blk.exact is False           # ⚠️ 抬上来的总数绝不换精确文案

    def test_总数缺失时不敢称精确(self, board):
        uid = str(BOARD_RAW[0]["id"])
        data = {"topHolders": [{"humanAmount": 5.0, "user": {"id": uid}}]}
        blk = bh.match_block(board, data, 150)
        assert blk.total is None
        assert blk.exact is False


# ============================================================
# ⚠️⚠️ 绝不拿"这个币上的盈亏"冒充"全平台 24h"
# ============================================================
class Test绝不混用两种盈亏:
    def test_榜单的盈亏与持有人行的盈亏是两个数(self, board):
        """
        实测:robinhood 的 MEME 第一大持有人(榜上 #28 Aurelius0121)
        **全平台 24h 是 +$579,472**,而他在这个币上的 pnl 是 **-$123,875** ——
        连符号都是反的。取出来的必须是前者。
        """
        raw = load("fomo_top_holders_robinhood_meme.json")
        first = raw["topHolders"][0]
        assert first["user"]["userHandle"] == "Aurelius0121"
        assert first["pnl"] < 0                        # 这个币上是亏的
        blk = bh.match_block(board, raw, 150)
        row = next(r for r in blk.rows if r[1] == "Aurelius0121")
        assert row[4] == 579472.4723991062             # 全平台 24h,来自榜单
        assert row[4] != first["pnl"]
        assert row[4] != first["unrealizedPnl"]
        assert row[4] != first["realizedPnl"]

    def test_持有人对象里根本没有全平台盈亏字段(self):
        """⚠️ 这是"只能从榜单取"这个结论的事实依据,写成断言免得下次有人猜。"""
        raw = load("fomo_top_holders_robinhood_meme.json")
        user = raw["topHolders"][0]["user"]
        for key in ("pnl24h", "pnl7d", "pnl30d", "totalPnL"):
            assert key not in user

    def test_源码里根本没读过币上盈亏那三个键(self):
        """
        ⚠️⚠️ AST 扫描:本模块的**任何** `.get("x")` / `["x"]` 都不许是
           pnl / unrealizedPnl / realizedPnl / costBasis。
           把 pnl24h 的取值换成 holder 的 pnl,这条当场红 —— 而且它连
           "改了但夹具碰巧对不上"这种运气都不给。
        ⚠️ 只看**代码里真正用作键**的字面量,不看注释与文档字符串。
        """
        src = (pathlib.Path(bh.__file__)).read_text(encoding="utf-8")
        tree = ast.parse(src)
        keys = set()
        for node in ast.walk(tree):
            if (isinstance(node, ast.Call) and isinstance(node.func, ast.Attribute)
                    and node.func.attr == "get" and node.args
                    and isinstance(node.args[0], ast.Constant)
                    and isinstance(node.args[0].value, str)):
                keys.add(node.args[0].value)
            if (isinstance(node, ast.Subscript)
                    and isinstance(node.slice, ast.Constant)
                    and isinstance(node.slice.value, str)):
                keys.add(node.slice.value)
        assert keys & {"pnl", "unrealizedPnl", "realizedPnl", "costBasis"} == set()
        # 反向:确实读了这几个该读的键(否则上面那条是空转)
        assert {"topHolders", "totalHolders", "humanAmount", "id",
                "userHandle", "followers"} <= keys

    def test_周期与盈亏字段成对(self):
        """⚠️ 24h 榜里只有 pnl24h。改周期不改字段 = 整列 None、静默消失。"""
        assert bh.BOARD_PERIOD == "24h"
        assert bh.BOARD_PNL_FIELD == "pnl24h"
        assert all("pnl24h" in r for r in BOARD_RAW)


# ============================================================
# 请求形态
# ============================================================
class Test请求形态:
    def test_榜单一次拉一百五十行(self):
        c = FakeClient(holders={CA_MEME: load("fomo_top_holders_robinhood_meme.json")})
        make(c).lookup([("robinhood", CA_MEME)])
        assert c.board_calls == [("24h", 150, False)]

    def test_持有人的链id必须是数字(self):
        """⚠️⚠️ 传链名服务端直接 400(`Expected number, received nan`)。"""
        c = FakeClient(holders={CA_MEME: load("fomo_top_holders_robinhood_meme.json")})
        make(c).lookup([("robinhood", CA_MEME)])
        addr, net, auth = c.holder_calls[0]
        assert net == 4663
        assert isinstance(net, int) and not isinstance(net, bool)
        assert addr == CA_MEME
        assert auth is False

    def test_四条链的链id各是哪个数字(self):
        c = FakeClient()
        make(c).lookup([("robinhood", CA_MEME), ("solana", CA_STONK),
                        ("bsc", "0x21caef8a43163eea865baee23b9c2e327696a3bf"),
                        ("base", CA_EVAL)])
        assert [n for _, n, _ in c.holder_calls] == [4663, 1399811149, 56, 8453]

    def test_两个请求都不许处置登录态(self):
        """⚠️⚠️ auth_invalidate=False:401/403 绝不去 invalidate 全进程共用的令牌。"""
        c = FakeClient(holders={CA_MEME: load("fomo_top_holders_robinhood_meme.json")})
        make(c).lookup([("robinhood", CA_MEME)])
        assert all(a is False for *_, a in c.board_calls)
        assert all(a is False for *_, a in c.holder_calls)

    def test_认不出的链一个请求都不发(self):
        c = FakeClient()
        assert make(c).lookup([("dogecoin", CA_MEME), ("", CA_MEME),
                               ("solana", "")]) == {}
        assert c.board_calls == [] and c.holder_calls == []

    def test_没有任何币时一个请求都不发(self):
        c = FakeClient()
        assert make(c).lookup([]) == {}
        assert c.board_calls == []

    def test_同一个币在一批里只问一次(self):
        c = FakeClient(holders={CA_MEME: load("fomo_top_holders_robinhood_meme.json")})
        # ⚠️ 第二个是 checksum 混合大小写的同一个 EVM 地址(归一化后是同一个键)
        make(c).lookup([("robinhood", CA_MEME),
                        ("robinhood", "0x" + CA_MEME[2:].upper())])
        assert len(c.holder_calls) == 1


# ============================================================
# 缓存与预算
# ============================================================
class Test缓存与预算:
    def test_榜单缓存命中时零请求(self):
        """⚠️ 榜单与币无关:第二个币**不该**再拉一次榜。"""
        c = FakeClient(holders={CA_MEME: load("fomo_top_holders_robinhood_meme.json"),
                                CA_EVAL: load("fomo_top_holders_base_eval.json")})
        lk = make(c)
        lk.lookup([("robinhood", CA_MEME)])
        lk.lookup([("base", CA_EVAL)])
        assert len(c.board_calls) == 1
        assert len(c.holder_calls) == 2

    def test_榜单缓存跨实例共用(self):
        """poller 与 pumpfun 各一个 lookup,但榜是**进程级**的 —— 只该拉一次。"""
        shared = bh._BoardCache()
        c = FakeClient(holders={CA_MEME: load("fomo_top_holders_robinhood_meme.json")})
        for _ in range(2):
            bh.BoardHoldersLookup(c, board_cache=shared).lookup(
                [("robinhood", CA_MEME)])
        assert len(c.board_calls) == 1

    def test_榜单缓存过期后重拉(self):
        now = [1000.0]
        cache = bh._BoardCache(ttl=300.0, clock=lambda: now[0])
        c = FakeClient(holders={CA_MEME: load("fomo_top_holders_robinhood_meme.json")})
        lk = bh.BoardHoldersLookup(c, board_cache=cache)
        lk.lookup([("robinhood", CA_MEME)])
        now[0] += 299.0
        lk.lookup([("solana", CA_STONK)])
        assert len(c.board_calls) == 1
        now[0] += 2.0
        lk.lookup([("base", CA_EVAL)])
        assert len(c.board_calls) == 2

    def test_榜单失败后短时间内不再重试(self):
        now = [1000.0]
        cache = bh._BoardCache(error_ttl=60.0, clock=lambda: now[0])
        c = FakeClient(board_error=RuntimeError("上游 500"))
        lk = bh.BoardHoldersLookup(c, board_cache=cache)
        assert lk.lookup([("robinhood", CA_MEME)]) == {}
        now[0] += 59.0
        lk.lookup([("solana", CA_STONK)])
        assert len(c.board_calls) == 1
        now[0] += 2.0
        lk.lookup([("base", CA_EVAL)])
        assert len(c.board_calls) == 2

    def test_每_tick_的次数上限(self):
        """⚠️ 8 个币、上限 3 → 只发 3 个持有人请求,后面那些这一块不显示。"""
        c = FakeClient()
        lk = make(c, per_round=3)
        lk.begin_round()
        lk.lookup([("robinhood", f"0x{i:040x}") for i in range(8)])
        assert len(c.holder_calls) == 3

    def test_次数上限每轮重置(self):
        c = FakeClient()
        lk = make(c, per_round=2)
        for _ in range(2):
            lk.begin_round()
            lk.lookup([("robinhood", f"0x{i:040x}") for i in range(5)])
        assert len(c.holder_calls) == 4

    def test_墙钟闸(self):
        """⚠️ 每次外呼假装花 3 秒,墙钟 6 秒 → 榜单 1 次 + 持有人最多 1 次就停。"""
        ticks = iter([0.0, 3.0, 3.0, 6.0, 6.0, 9.0, 9.0, 12.0, 12.0, 15.0])
        c = FakeClient()
        lk = make(c, wall_clock_sec=6.0, per_round=99,
                  clock=lambda: next(ticks, 99.0))
        lk.begin_round()
        lk.lookup([("robinhood", f"0x{i:040x}") for i in range(5)])
        assert len(c.holder_calls) == 1

    def test_拉榜的耗时也计入墙钟(self):
        """
        ⚠️⚠️ 拉榜同样挂在 tick 的墙钟上(而且它可能在**等另一个 job 的那把锁**)。
           不把它记进账,墙钟闸对"榜慢了"这种情况就完全失效。
        """
        ticks = iter([0.0, 7.0])          # 光拉榜就花了 7 秒 > 墙钟 6 秒
        c = FakeClient(holders={CA_MEME: load("fomo_top_holders_robinhood_meme.json")})
        lk = make(c, wall_clock_sec=6.0, per_round=99,
                  clock=lambda: next(ticks, 99.0))
        lk.begin_round()
        assert lk.lookup([("robinhood", CA_MEME)]) == {}
        assert len(c.board_calls) == 1
        assert c.holder_calls == [], "墙钟已经用光,后面一个持有人请求都不该发"

    def test_持有人结果进内存缓存后不再重复请求(self):
        c = FakeClient(holders={CA_MEME: load("fomo_top_holders_robinhood_meme.json")})
        lk = make(c)
        lk.begin_round()
        lk.lookup([("robinhood", CA_MEME)])
        lk.begin_round()
        out = lk.lookup([("robinhood", CA_MEME)])
        assert len(c.holder_calls) == 1
        assert len(out[("robinhood", CA_MEME)].rows) == 10

    def test_零命中也进缓存不重复请求(self):
        c = FakeClient(holders={CA_GMONAD: load("fomo_top_holders_monad_gmonad.json")})
        lk = make(c)
        for _ in range(2):
            lk.begin_round()
            assert lk.lookup([("monad", CA_GMONAD)]) == {}
        assert len(c.holder_calls) == 1

    def test_只读缓存路径一个请求都不发(self):
        """转入推送走这条:命中就有,不命中就没有,**绝不新开请求**。"""
        c = FakeClient(holders={CA_MEME: load("fomo_top_holders_robinhood_meme.json")})
        lk = make(c)
        assert lk.cached("robinhood", CA_MEME) is None
        assert c.holder_calls == [] and c.board_calls == []
        lk.lookup([("robinhood", CA_MEME)])
        assert lk.cached("robinhood", "0x" + CA_MEME[2:].upper()) is not None
        assert len(c.holder_calls) == 1

    # ------------------------------------------------------------------
    # ⚠️⚠️ 上面那几条闸门的用例都**自己注入了参数**(per_round=3 / wall_clock_sec=6.0
    #    / _BoardCache(error_ttl=...)),于是它们只证明了"闸门这套机制是通的",
    #    **完全没有钉住生产默认值**。变异跑当场抓到:把 _HOLDERS_PER_ROUND 改成
    #    100000、_ROUND_WALL_CLOCK_SEC 改成 100000.0、_BOARD_ERROR_TTL_SEC 改成 0.0,
    #    全量 4578 条**一条都不红**(实测)—— 也就是说线上的闸门可以被悄悄拿掉。
    #    下面三条用**生产默认值**(一个参数都不传)重做一遍。
    # ------------------------------------------------------------------
    def test_不传参数时次数上限就是生产那个值(self):
        """⚠️ 十个币进来,只该发 6 个持有人请求(第 7 个起本轮不显示)。"""
        c = FakeClient()
        lk = bh.BoardHoldersLookup(c, board_cache=bh._BoardCache())   # 只换缓存,闸门用默认
        lk.begin_round()
        lk.lookup([("robinhood", f"0x{i:040x}") for i in range(1, 11)])
        assert len(c.holder_calls) == 6

    def test_不传参数时墙钟闸就是生产那个值(self):
        """
        ⚠️ 每次外呼假装花 2 秒:拉榜 2 秒 + 两个持有人请求 4 秒 = 6 秒,到顶。
           墙钟默认值被改大 → 十个币全查完 → 这条红。
        """
        ticks = iter([float(2 * i) for i in range(40)])
        c = FakeClient()
        lk = bh.BoardHoldersLookup(c, board_cache=bh._BoardCache(),
                                   clock=lambda: next(ticks, 999.0))
        lk.begin_round()
        lk.lookup([("robinhood", f"0x{i:040x}") for i in range(1, 11)])
        assert len(c.holder_calls) == 2

    def test_不传参数时榜单失败的负缓存是分钟量级(self):
        """
        ⚠️ 拉榜失败后**半分钟内**绝不重试:一次上游抖动不该让后面每一条推送
           都再去试一次(那正是上游抖动时最不该做的事)。
           负缓存 TTL 被改成 0 → 第二次 lookup 又拉一次 → 这条红。
        """
        now = [1000.0]
        c = FakeClient(board_error=RuntimeError("上游 500"))
        cache = bh._BoardCache(clock=lambda: now[0])          # ttl / error_ttl 都用默认
        lk = bh.BoardHoldersLookup(c, board_cache=cache)
        lk.lookup([("robinhood", CA_MEME)])
        now[0] += 30.0
        lk.lookup([("solana", CA_STONK)])
        assert len(c.board_calls) == 1

    def test_不传参数时榜单成功的缓存是分钟量级(self):
        """⚠️ 同上的正向:成功之后几分钟内不该再拉榜(榜与币无关)。"""
        now = [1000.0]
        c = FakeClient(holders={CA_MEME: load("fomo_top_holders_robinhood_meme.json")})
        cache = bh._BoardCache(clock=lambda: now[0])
        lk = bh.BoardHoldersLookup(c, board_cache=cache)
        lk.lookup([("robinhood", CA_MEME)])
        now[0] += 120.0
        lk.lookup([("solana", CA_STONK)])
        assert len(c.board_calls) == 1

    def test_只读缓存路径脏输入也不抛(self):
        lk = make(FakeClient())
        assert lk.cached(None, None) is None
        assert lk.cached("robinhood", "") is None


# ============================================================
# 失败一律只让这一块消失
# ============================================================
class Test各类失败:
    @pytest.mark.parametrize("err", [
        TimeoutError("超时"),
        RuntimeError("HTTP 400"),
        ValueError("JSON 坏了"),
        AuthError("HTTP 401"),
    ], ids=["超时", "4xx", "JSON坏", "401"])
    def test_榜单失败这一块整块消失(self, err):
        c = FakeClient(board_error=err)
        assert make(c).lookup([("robinhood", CA_MEME)]) == {}

    @pytest.mark.parametrize("err", [
        TimeoutError("超时"),
        RuntimeError("HTTP 500"),
        AuthError("HTTP 403"),
    ], ids=["超时", "5xx", "403"])
    def test_持有人失败这一块整块消失(self, err):
        c = FakeClient(holders_error=err)
        assert make(c).lookup([("robinhood", CA_MEME)]) == {}

    def test_一个币失败不影响另一个币(self):
        """⚠️ 独立性:MEME 那个币的请求炸了,EVAL 那个照样出。"""
        good = load("fomo_top_holders_base_eval.json")

        class Half(FakeClient):
            def get_top_holders(self, token_address, network_id, *,
                                auth_invalidate=True):
                self.holder_calls.append((token_address, network_id, auth_invalidate))
                if token_address == CA_MEME:
                    raise RuntimeError("这个币炸了")
                return good

        c = Half()
        out = make(c).lookup([("robinhood", CA_MEME), ("base", CA_EVAL)])
        assert ("robinhood", CA_MEME) not in out
        assert [r[0] for r in out[("base", CA_EVAL)].rows] == [124, 134]

    def test_榜单返回垃圾结构时这一块消失(self):
        for junk in (None, {}, "nope", [1, 2, 3]):
            c = FakeClient(board_payload=junk)
            assert make(c).lookup([("robinhood", CA_MEME)]) == {}

    def test_持有人返回垃圾结构时这一块消失(self):
        for junk in (None, [], "nope", {"topHolders": "x"}):
            c = FakeClient(holders={CA_MEME: junk})
            assert make(c).lookup([("robinhood", CA_MEME)]) == {}

    def test_client_建不出来时这一块消失(self):
        lk = bh.BoardHoldersLookup(None, board_cache=bh._BoardCache())
        assert lk.lookup([("robinhood", CA_MEME)]) == {}

    def test_对外方法一个都不抛(self):
        """⚠️ 连 lookup 内部彻底崩掉也只降级 —— 它跑在发送循环里。"""
        class Boom(FakeClient):
            def get_leaderboard(self, *a, **kw):
                raise KeyboardInterrupt  # 连非 Exception 之外的都试一下形状

        lk = make(FakeClient())
        lk._lookup = lambda pairs: (_ for _ in ()).throw(RuntimeError("内部炸了"))
        assert lk.lookup([("robinhood", CA_MEME)]) == {}
        lk._cache_get = lambda nk: (_ for _ in ()).throw(RuntimeError("炸"))
        assert lk.cached("robinhood", CA_MEME) is None


# ============================================================
# render_args:交给渲染层的那两个参数
# ============================================================
class Test交给渲染层的参数:
    def test_零命中不产生任何键(self):
        blk = bh.BoardBlock(rows=(), covered=97, total=15305, exact=False,
                            board_size=150)
        assert blk.render_args() == {}

    def test_人数说的是命中总数而不是列出来的行数(self, board):
        """⚠️⚠️ 行被截断到 3 条,但「N 人」必须还是 10。"""
        blk = bh.match_block(board, load("fomo_top_holders_robinhood_meme.json"), 150)
        args = blk.render_args()
        assert len(args["board_holders"]) == 3
        assert args["board_scope"] == (10, 97, 15305, False, 150)

    def test_精确样本的口径(self, board):
        blk = bh.match_block(board, load("fomo_top_holders_robinhood_cleat.json"), 150)
        assert blk.render_args()["board_scope"] == (1, 87, 87, True, 150)


# ============================================================
# 真实的 400 / 401 报文(2026-09-06 亲手打出来的)
# ============================================================
# ⚠️ 这两份夹具是**真实响应**,不是编的:
#   400 —— 故意把 networkId 传成链名 "robinhood";
#   401 —— 用一个伪造令牌打榜单(全程走假的 token provider,不碰真实登录态)。
class Test真实的错误报文:
    def test_四百的报文说的就是链id不是数字(self):
        """
        ⚠️⚠️ 这条把"networkId 必须是数字"从一句注释变成一份**证据**:
           服务端的原话是 `Expected number, received nan`。
        """
        body = load("fomo_top_holders_400_chainname.json")
        assert body["statusCode"] == 400
        assert body["success"] is False
        assert body["message"] == (
            "Invalid input: query.tokens.0.networkId - Expected number, received nan")
        assert body["responseObject"]["validationErrors"][0]["field"] == \
            "query.tokens.0.networkId"

    def test_四百的报文走生产解析路径也只让这一块消失(self):
        """⚠️ client 会把 400 抛成 FomoAPIError;本模块吞掉它,这一块整块不出现。"""
        from src.client import FomoAPIError

        body = load("fomo_top_holders_400_chainname.json")
        c = FakeClient(holders_error=FomoAPIError(
            f"/hodlers/top HTTP 400: {json.dumps(body)[:300]}"))
        assert make(c).lookup([("robinhood", CA_MEME)]) == {}

    def test_四百一的报文(self):
        body = load("fomo_leaderboard_401.json")
        assert body["statusCode"] == 401
        assert body["message"] == "Unexpected error in JWT authentication middleware"
        # ⚠️ 注意它的 responseObject 是**空数组**而不是对象 —— 直接喂给解析层
        #    会得到一个"空榜",而不是一个异常。所以判据必须是 HTTP 状态码,
        #    绝不能靠"解析出来是不是空"来判鉴权失败。
        assert body["responseObject"] == []
        assert bh.parse_board(body["responseObject"]) == {}

    def test_四百一走生产路径这一块消失且推送不受影响(self):
        """⚠️⚠️ client 对 401 抛的是 AuthError,而 poller 对 AuthError 的处置是**停机**。"""
        c = FakeClient(board_error=AuthError(
            "/v2/leaderboard/24h 鉴权失败(HTTP 401);本次调用不处置登录态"))
        assert make(c).lookup([("robinhood", CA_MEME)]) == {}
        assert c.holder_calls == [], "榜都没拿到,就不该再去问持有人"
