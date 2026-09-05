"""SQLite 仓储。异步一套走到底，避免 bot 层（全异步）里混同步 IO。

DDL 幂等，所以启动时无脑跑一次 init_schema 就行，没有迁移框架的必要
—— 真到了要改列的那天，raw_text 还在，整库重解析比写 migration 快。
"""

from __future__ import annotations

import datetime as dt
from collections.abc import Iterable, Sequence
from pathlib import Path
from typing import Any, Self

import aiosqlite
import orjson

from ..domain.post import ParseStatus, Post

SCHEMA_PATH = Path(__file__).parent / "schema.sql"

_PRAGMAS = (
    "PRAGMA journal_mode=WAL",
    "PRAGMA synchronous=NORMAL",
    "PRAGMA busy_timeout=5000",
)

_COLS = (
    "channel_id", "message_id", "posted_at", "edited_at",
    "title_cn", "title_en", "search_blob",
    "episodes", "air_date", "air_weekday", "duration",
    "score", "score_text", "summary",
    "aliases_json", "staff_json", "links_json", "passwords_json",
    "extra_json", "parse_notes_json",
    "has_photo", "raw_text", "parse_status", "ingested_at",
)

_UPSERT = (
    f"INSERT INTO posts ({', '.join(_COLS)}) "
    f"VALUES ({', '.join('?' * len(_COLS))}) "
    "ON CONFLICT(channel_id, message_id) DO UPDATE SET "
    + ", ".join(f"{c}=excluded.{c}" for c in _COLS if c not in ("channel_id", "message_id"))
)


def _dumps(obj: Any) -> str:
    return orjson.dumps(obj).decode()


# 参与检索的状态：解析成功和部分成功都算真帖子，SKIPPED/FAILED 不进结果
_ACTIVE: tuple[str, str] = (str(ParseStatus.OK), str(ParseStatus.PARTIAL))


def _now() -> str:
    return dt.datetime.now(dt.UTC).isoformat(timespec="seconds")


def post_to_row(post: Post, ingested_at: str | None = None) -> tuple[Any, ...]:
    return (
        post.channel_id,
        post.message_id,
        post.posted_at.isoformat(),
        post.edited_at.isoformat() if post.edited_at else None,
        post.title_cn,
        post.title_en,
        post.search_title.lower(),   # 存小写，让英文名 LIKE 不区分大小写
        post.episodes,
        post.air_date,
        post.air_weekday,
        post.duration,
        post.score,
        post.score_text,
        post.summary,
        _dumps(post.aliases),
        _dumps(post.staff),
        _dumps(post.links),
        _dumps(post.passwords),
        _dumps(post.extra),
        _dumps(post.parse_notes),
        int(post.has_photo),
        post.raw_text,
        str(post.parse_status),
        ingested_at or _now(),
    )


def row_to_post(row: aiosqlite.Row) -> Post:
    return Post(
        channel_id=row["channel_id"],
        message_id=row["message_id"],
        posted_at=dt.datetime.fromisoformat(row["posted_at"]),
        edited_at=dt.datetime.fromisoformat(row["edited_at"]) if row["edited_at"] else None,
        title_cn=row["title_cn"],
        title_en=row["title_en"],
        aliases=orjson.loads(row["aliases_json"]),
        episodes=row["episodes"],
        air_date=row["air_date"],
        air_weekday=row["air_weekday"],
        duration=row["duration"],
        score=row["score"],
        score_text=row["score_text"],
        summary=row["summary"],
        staff=orjson.loads(row["staff_json"]),
        links=orjson.loads(row["links_json"]),
        passwords=orjson.loads(row["passwords_json"]),
        extra=orjson.loads(row["extra_json"]),
        parse_notes=orjson.loads(row["parse_notes_json"]),
        has_photo=bool(row["has_photo"]),
        raw_text=row["raw_text"],
        parse_status=ParseStatus(row["parse_status"]),
        tags=[],
        index_tags=[],
    )


class PostRepo:
    """帖子仓储。用 async with 管生命周期。"""

    def __init__(self, db_path: Path) -> None:
        self._path = Path(db_path)
        self._conn: aiosqlite.Connection | None = None

    # ------------------------------------------------------------ 生命周期
    @property
    def conn(self) -> aiosqlite.Connection:
        if self._conn is None:
            raise RuntimeError("PostRepo 未打开，先 await repo.open()")
        return self._conn

    async def open(self) -> Self:
        self._path.parent.mkdir(parents=True, exist_ok=True)
        self._conn = await aiosqlite.connect(self._path)
        self._conn.row_factory = aiosqlite.Row
        for pragma in _PRAGMAS:
            await self._conn.execute(pragma)
        return self

    async def close(self) -> None:
        if self._conn is not None:
            await self._conn.close()
            self._conn = None

    async def __aenter__(self) -> Self:
        return await self.open()

    async def __aexit__(self, *_exc: object) -> None:
        await self.close()

    async def init_schema(self) -> None:
        await self.conn.executescript(SCHEMA_PATH.read_text(encoding="utf-8"))
        await self.conn.commit()

    # ------------------------------------------------------------ 写入
    async def upsert_many(self, posts: Sequence[Post]) -> int:
        """幂等写入。帖子会被反复编辑（真实数据里 99.9% 都 edited 过），所以只能 upsert。"""
        if not posts:
            return 0
        stamp = _now()
        await self.conn.executemany(_UPSERT, [post_to_row(p, stamp) for p in posts])

        keys = [(p.channel_id, p.message_id) for p in posts]
        await self.conn.executemany(
            "DELETE FROM post_tags WHERE channel_id=? AND message_id=?", keys
        )
        rows = [
            (p.channel_id, p.message_id, kind, tag)
            for p in posts
            for kind, tags in (("tag", p.tags), ("index", p.index_tags))
            for tag in tags
        ]
        if rows:
            await self.conn.executemany(
                "INSERT OR IGNORE INTO post_tags (channel_id, message_id, kind, tag) "
                "VALUES (?,?,?,?)",
                rows,
            )
        await self.conn.commit()
        return len(posts)

    # ------------------------------------------------------------ 读取
    async def _hydrate(self, rows: Iterable[aiosqlite.Row]) -> list[Post]:
        """补上关联表里的 tags / index_tags，一次查询搞定，不做 N+1。"""
        posts = [row_to_post(r) for r in rows]
        if not posts:
            return posts
        keys = {(p.channel_id, p.message_id): p for p in posts}
        by_channel: dict[int, list[int]] = {}
        for cid, mid in keys:
            by_channel.setdefault(cid, []).append(mid)

        for cid, mids in by_channel.items():
            ph = ",".join("?" * len(mids))
            sql = (
                "SELECT message_id, kind, tag FROM post_tags "
                f"WHERE channel_id=? AND message_id IN ({ph})"
            )
            async with self.conn.execute(sql, (cid, *mids)) as cur:
                async for r in cur:
                    target = keys.get((cid, r["message_id"]))
                    if target is None:
                        continue
                    bucket = target.tags if r["kind"] == "tag" else target.index_tags
                    bucket.append(r["tag"])
        return posts

    async def get(self, channel_id: int, message_id: int) -> Post | None:
        async with self.conn.execute(
            "SELECT * FROM posts WHERE channel_id=? AND message_id=?",
            (channel_id, message_id),
        ) as cur:
            row = await cur.fetchone()
        if row is None:
            return None
        return (await self._hydrate([row]))[0]

    async def count(self, *, only_posts: bool = True) -> int:
        sql = "SELECT COUNT(*) FROM posts"
        params: tuple[Any, ...] = ()
        if only_posts:
            sql += " WHERE parse_status IN (?,?)"
            params = (ParseStatus.OK, ParseStatus.PARTIAL)
        async with self.conn.execute(sql, params) as cur:
            row = await cur.fetchone()
        return int(row[0]) if row else 0

    async def status_breakdown(self) -> dict[str, int]:
        async with self.conn.execute(
            "SELECT parse_status, COUNT(*) n FROM posts GROUP BY parse_status"
        ) as cur:
            return {r["parse_status"]: r["n"] async for r in cur}

    # ------------------------------------------------------------ 检索
    async def like_titles(self, keyword: str, limit: int) -> list[Post]:
        """标题/英文名/别名的子串匹配。实测 1639 行 0.5ms，不需要 FTS5。"""
        async with self.conn.execute(
            "SELECT * FROM posts WHERE parse_status IN (?,?) AND search_blob LIKE ? "
            "ORDER BY posted_at DESC LIMIT ?",
            (*_ACTIVE, f"%{keyword.lower()}%", limit),
        ) as cur:
            rows = await cur.fetchall()
        return await self._hydrate(rows)

    async def like_fulltext(self, keyword: str, limit: int) -> list[Post]:
        """扩到正文（简介、staff、标签）。标题搜不到时兜底，实测 3ms。"""
        async with self.conn.execute(
            "SELECT * FROM posts WHERE parse_status IN (?,?) "
            "AND (search_blob LIKE ? OR raw_text LIKE ?) "
            "ORDER BY posted_at DESC LIMIT ?",
            (*_ACTIVE, f"%{keyword.lower()}%", f"%{keyword}%", limit),
        ) as cur:
            rows = await cur.fetchall()
        return await self._hydrate(rows)

    async def all_titles(self) -> list[tuple[int, int, str]]:
        """(channel_id, message_id, 中文名) 全量，给 rapidfuzz 做错别字容错。

        故意只取 title_cn 而不是 search_blob：blob 拼上了英文名和别名后长达 80+ 字符，
        WRatio 对长串惩罚很重，"无值英雄" 会被 "英雄时代" 反超。实测过。
        """
        async with self.conn.execute(
            "SELECT channel_id, message_id, title_cn FROM posts "
            "WHERE parse_status IN (?,?) AND title_cn <> ''",
            _ACTIVE,
        ) as cur:
            return [(r[0], r[1], r[2]) async for r in cur]

    async def get_many(self, keys: Sequence[tuple[int, int]]) -> list[Post]:
        """按 (channel_id, message_id) 批量取，保持传入顺序。"""
        if not keys:
            return []
        by_channel: dict[int, list[int]] = {}
        for cid, mid in keys:
            by_channel.setdefault(cid, []).append(mid)
        found: dict[tuple[int, int], Post] = {}
        for cid, mids in by_channel.items():
            ph = ",".join("?" * len(mids))
            async with self.conn.execute(
                f"SELECT * FROM posts WHERE channel_id=? AND message_id IN ({ph})",
                (cid, *mids),
            ) as cur:
                rows = await cur.fetchall()
            for post in await self._hydrate(rows):
                found[post.key] = post
        return [found[k] for k in keys if k in found]

    async def by_tags(
        self,
        tags: Sequence[str],
        *,
        kind: str = "tag",
        require_all: bool = True,
        limit: int = 50,
    ) -> list[Post]:
        """标签筛选。require_all=True 是交集（#奇幻 且 #异世界）。"""
        if not tags:
            return []
        ph = ",".join("?" * len(tags))
        having = "HAVING COUNT(DISTINCT t.tag) = ?" if require_all else ""
        params: list[Any] = [*_ACTIVE, kind, *tags]
        if require_all:
            params.append(len(tags))
        params.append(limit)
        sql = (
            "SELECT p.* FROM posts p "
            "JOIN post_tags t ON t.channel_id = p.channel_id "
            "AND t.message_id = p.message_id "
            "WHERE p.parse_status IN (?,?) AND t.kind = ? "
            f"AND t.tag IN ({ph}) "
            "GROUP BY p.channel_id, p.message_id "
            f"{having} "
            "ORDER BY p.posted_at DESC LIMIT ?"
        )
        async with self.conn.execute(sql, params) as cur:
            rows = await cur.fetchall()
        return await self._hydrate(rows)

    async def tag_cloud(self, kind: str = "tag", limit: int = 100) -> list[tuple[str, int]]:
        async with self.conn.execute(
            "SELECT tag, COUNT(*) n FROM post_tags WHERE kind=? "
            "GROUP BY tag ORDER BY n DESC LIMIT ?",
            (kind, limit),
        ) as cur:
            return [(r[0], r[1]) async for r in cur]

    # ------------------------------------------------------------ 审计
    async def start_run(self, source: str, trace_id: str = "") -> int:
        cur = await self.conn.execute(
            "INSERT INTO ingest_runs (source, trace_id, started_at) VALUES (?,?,?)",
            (source, trace_id, _now()),
        )
        await self.conn.commit()
        return int(cur.lastrowid or 0)

    async def finish_run(self, run_id: int, stats: dict[str, int], note: str = "") -> None:
        await self.conn.execute(
            "UPDATE ingest_runs SET finished_at=?, total=?, ok=?, partial=?, "
            "failed=?, skipped=?, upserted=?, note=? WHERE id=?",
            (
                _now(),
                stats.get("total", 0),
                stats.get("ok", 0),
                stats.get("partial", 0),
                stats.get("failed", 0),
                stats.get("skipped", 0),
                stats.get("upserted", 0),
                note,
                run_id,
            ),
        )
        await self.conn.commit()

    # ------------------------------------------------------------ 同步水位
    async def mark_seen(self, channel_id: int, message_id: int, *, stored: bool) -> None:
        """记录同步看到了一条消息。

        `last_seen` 包含不入库的公告闲聊 —— 它回答「update 还在到达吗」。
        `last_stored` 才是索引的进度。两个分开记，否则「一个月没发新番」和
        「同步挂了」在数据上长得一模一样。

        用 MAX() 而不是直接赋值：编辑旧帖会带来一个较小的 message_id，
        直接赋值会让水位倒退。
        """
        now = _now()
        await self.conn.execute(
            "INSERT INTO sync_state (channel_id, last_seen_message_id, last_seen_at, "
            "  last_stored_message_id, last_stored_at, seen_count, stored_count) "
            "VALUES (?,?,?,?,?,1,?) "
            "ON CONFLICT(channel_id) DO UPDATE SET "
            "  last_seen_message_id = MAX(last_seen_message_id, excluded.last_seen_message_id), "
            "  last_seen_at         = excluded.last_seen_at, "
            "  last_stored_message_id = MAX(last_stored_message_id, "
            "                              excluded.last_stored_message_id), "
            "  last_stored_at       = CASE WHEN excluded.stored_count > 0 "
            "                              THEN excluded.last_stored_at "
            "                              ELSE last_stored_at END, "
            "  seen_count           = seen_count + 1, "
            "  stored_count         = stored_count + excluded.stored_count",
            (
                channel_id,
                message_id,
                now,
                message_id if stored else 0,
                now if stored else "",
                1 if stored else 0,
            ),
        )
        await self.conn.commit()

    async def sync_state(self, channel_id: int) -> dict[str, Any] | None:
        async with self.conn.execute(
            "SELECT * FROM sync_state WHERE channel_id=?", (channel_id,)
        ) as cur:
            row = await cur.fetchone()
        return dict(row) if row else None

    async def max_message_id(self, channel_id: int) -> int:
        """库里最大的 message_id。缺口检测的下界。"""
        async with self.conn.execute(
            "SELECT COALESCE(MAX(message_id), 0) FROM posts WHERE channel_id=?",
            (channel_id,),
        ) as cur:
            row = await cur.fetchone()
        return int(row[0]) if row else 0
