from __future__ import annotations

import asyncio
import time
import httpx
from .models import Market, utc_iso

GAMMA = "https://gamma-api.polymarket.com"
CLOB = "https://clob.polymarket.com"


class PublicAPI:
    def __init__(self):
        self.http = httpx.AsyncClient(timeout=15, headers={"User-Agent": "round-bot/0.1"})
        self.series = {}
        self.offset = 0.0
        self.clock_checked = 0.0
        self.clock_uncertainty = 999.0
        self.last_latency_ms = 0.0

    async def get(self, url, **params):
        for attempt in range(4):
            start = time.monotonic()
            try:
                r = await self.http.get(url, params=params)
                if r.status_code == 429 or r.status_code >= 500:
                    r.raise_for_status()
                r.raise_for_status()
                self.last_latency_ms = (time.monotonic() - start) * 1000
                return r.json()
            except (httpx.TransportError, httpx.HTTPStatusError) as exc:
                if isinstance(exc, httpx.HTTPStatusError) and exc.response.status_code < 500 and exc.response.status_code != 429:
                    raise
                if attempt == 3:
                    raise
                await asyncio.sleep(0.5 * 2 ** attempt)

    def now(self):
        return time.time() + self.offset

    async def sync_clock(self):
        samples = []
        for _ in range(3):
            start = time.time()
            server = float(await self.get(CLOB + "/time"))
            end = time.time()
            samples.append((end - start, start, end, server))
        _, start, end, server = min(samples, key=lambda sample: sample[0])
        self.offset = server - (start + end) / 2
        self.clock_uncertainty = (end - start) / 2 + 1  # 服务端只提供整秒
        self.clock_checked = time.time()
        return self.offset

    async def discover(self, rule, next_only=True):
        if rule.series_slug not in self.series:
            rows = await self.get(GAMMA + "/series", slug=rule.series_slug)
            exact = [r for r in rows if r.get("slug") == rule.series_slug]
            if len(exact) != 1:
                raise ValueError(f"未找到唯一系列 {rule.series_slug}")
            self.series[rule.series_slug] = exact[0]["id"]
        now = self.now()
        found = []
        invalid = 0
        # 按结束时间排序，分页查真实系列；不使用 Unix 对齐猜 token 或市场时间。
        for page in range(10):
            events = await self.get(GAMMA + "/events", series_id=self.series[rule.series_slug],
                                    closed="false", end_date_min=utc_iso(now),
                                    order="endDate", ascending="true", limit=100, offset=100 * page)
            for ev in events:
                for raw in ev.get("markets", []):
                    try:
                        market = Market.parse(raw, ev, rule)
                    except (ValueError, KeyError, TypeError):
                        invalid += 1
                        continue
                    if market.end > now and (not next_only or market.start > now):
                        found.append(market)
            if found or len(events) < 100:
                break
        if not found and invalid:
            raise ValueError("系列中没有通过周期和交易规格校验的未来市场")
        return min(found, key=lambda m: m.start) if found else None

    async def market(self, slug):
        rows = await self.get(GAMMA + "/markets", slug=slug)
        exact = [r for r in rows if r.get("slug") == slug]
        if not exact:
            rows = await self.get(GAMMA + "/markets", slug=slug, closed="true")
            exact = [r for r in rows if r.get("slug") == slug]
        if len(exact) != 1:
            raise ValueError("市场查询未返回唯一匹配")
        return exact[0]

    async def book(self, token):
        return await self.get(CLOB + "/book", token_id=token)

    async def geoblock(self):
        return await self.get("https://polymarket.com/api/geoblock")

    async def redemption_status(self, transaction_id):
        from urllib.parse import quote
        from polymarket.models.clob.relayer import GaslessTransaction
        # 与锁定 SDK 0.18.0 的 fetch_gasless_transaction 使用相同官方只读路由。
        raw = await self.get("https://relayer-v2.polymarket.com/v1/account/transactions/" + quote(transaction_id, safe=""))
        tx = GaslessTransaction.parse_response(raw)
        return {"state": str(tx.state), "tx_hash": str(tx.transaction_hash or "")}

    async def receipt(self, tx_hash):
        import os
        rpc = os.getenv("POLYGON_RPC_URL")
        if not rpc or not tx_hash:
            return None
        response = await self.http.post(rpc, json={"jsonrpc":"2.0", "id":1, "method":"eth_getTransactionReceipt", "params":[tx_hash]})
        response.raise_for_status()
        raw = response.json()
        if "error" in raw:
            raise ValueError("链上回执查询失败")
        return raw.get("result")

    async def close(self):
        await self.http.aclose()
