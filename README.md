# Wishclaim · 礼物愿望认领

发布 → 认领锁定（互斥+TTL）→ 核销/释放。

| 服务 | 端口 |
| --- | --- |
| 前端 | 5200 |
| API | 10200 |

```bash
docker compose up --build
pytest backend/app/tests
```

0-1：`wish_comment` / `secret_santa` / `price_cap`。

认领链路：`engines/claim_lock.py` 纯判定（零库），`engines/claim_store.py` 持久化 claimer/expires_at，路由只编排、不再解析到期时间。过期临界拍板：判定已通过但写锁瞬间时钟或库内 expires_at 已过期 → 整单失败保持原锁（409），墙卡认领人 / 详情到期 / 我的认领三路同一口径。前端认领入口只打 `POST /api/wishes/{id}/claim`。
