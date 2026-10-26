"""戒色吧 AI 自动回复机器人：拉取贴吧新帖 → 选帖 → 调用本机问答接口生成回答 → 回帖。

基于 aiotieba（需 ==4.7.1，v4.8 起官方因封号风险过高移除了 add_post 回帖接口）。
问答复用后端 /api/chat 完整链路（术语归一化 / 意图识别 / 检索 / LLM 生成），
因此运行前需先启动后端服务。意图为闲聊（chitchat）或检索无命中的帖子自动跳过。

用法（backend 目录下）：
    python scripts/tieba_bot.py --scan        # 只扫描：打印将回复哪些帖子（不调问答、不发）
    python scripts/tieba_bot.py --once        # 单次运行，dry-run：生成回复仅打印，不实际回帖
    python scripts/tieba_bot.py --once --post --tid 1234567890  # 只回复指定 tid（需 TIEBA_BDUSS）
    python scripts/tieba_bot.py               # 常驻轮询（默认 dry-run，--post 才真实回帖）
    python scripts/tieba_bot.py --whoami      # 验证 BDUSS 是否可用（打印账号名，不回帖）
    python scripts/tieba_bot.py --list        # 查看处理记录
    python scripts/tieba_bot.py --reset       # 清空处理记录（重新处理已见帖子）

配置（backend/.env，均可缺省）：
    TIEBA_BDUSS=xxx                  # 账号 BDUSS Cookie（浏览器登录贴吧后 F12 复制）；不配则强制 dry-run
    TIEBA_FORUM=戒色                  # 吧名
    TIEBA_API_BASE=http://127.0.0.1:8000
    TIEBA_POLL_INTERVAL=900          # 轮询间隔（秒）
    TIEBA_MAX_REPLIES_PER_RUN=2      # 单轮最多回帖数（含 dry-run 生成数）
    TIEBA_REPLY_INTERVAL=120         # 两次回帖最小间隔（秒），降低风控风险
    TIEBA_DAILY_MAX=20               # 每日真实回帖上限（0=不限）
    TIEBA_MAX_THREAD_AGE_HOURS=24    # 只处理最近 N 小时内活跃的新帖
    TIEBA_MAX_REPLY_NUM=200          # 回复数超过该值的帖子跳过（热帖跟风易被删）
    TIEBA_MIN_QUESTION_LEN=8         # 标题+正文过短跳过（签到/灌水类）
    TIEBA_MAX_QUESTION_LEN=1500      # 送入问答接口的问题截断长度
    TIEBA_AUTHOR_CONTEXT=1           # 综合作者历史发帖生成个性化背景（默认开，需已配置 LLM）
    TIEBA_AUTHOR_PROFILE_TTL_DAYS=7  # 作者画像缓存天数（0=每次重新生成）
    TIEBA_SHARE_LINK=1               # 回帖末尾附分享链接（完整回答 + 引用出处，前端 /#/s/{id} 只读页）
    TIEBA_SHARE_BASE=                # 分享链接的站点根地址，须是读者能访问的公网地址（缺省= TIEBA_API_BASE）
    TIEBA_SHARE_DEVICE=tieba-bot     # 分享快照的游客设备标识（以后可据此撤销分享）
    TIEBA_MAX_ANSWER_LEN=1000        # 回复正文截断长度
    TIEBA_INCLUDE_KEYWORDS=          # 逗号分隔；非空时仅处理含关键词的帖子（空=交给意图识别过滤）
    TIEBA_EXCLUDE_KEYWORDS=广告,加群,兼职,推广,网赚   # 命中即跳过
    TIEBA_REPLY_PREFIX=【AI戒色助手】
    TIEBA_REPLY_SUFFIX=（以上内容由 AI 基于《戒为良药》《戒者录》自动生成，仅供参考）

注意：百度对自动化发帖风控较严（aiotieba 官方也因此移除发布接口），
真实回帖请保持低频（默认参数已压到很低），并优先用 --scan / dry-run 观察。
"""
from __future__ import annotations

import argparse
import asyncio
import json
import re
import sqlite3
import sys
import warnings
from datetime import datetime, timedelta
from pathlib import Path

import aiotieba
import httpx
from pydantic_settings import BaseSettings, SettingsConfigDict

warnings.filterwarnings("ignore", category=DeprecationWarning, module="aiotieba")

BACKEND_DIR = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(BACKEND_DIR))
from app.generator import Generator  # noqa: E402  作者画像摘要复用后端 LLM 配置


class BotSettings(BaseSettings):
    model_config = SettingsConfigDict(
        env_file=str(BACKEND_DIR / ".env"),
        env_file_encoding="utf-8",
        extra="ignore",
    )

    tieba_bduss: str = ""
    tieba_forum: str = "戒色"
    tieba_api_base: str = "http://127.0.0.1:8000"
    tieba_poll_interval: int = 900
    tieba_max_replies_per_run: int = 2
    tieba_reply_interval: int = 120
    tieba_daily_max: int = 20
    tieba_max_thread_age_hours: int = 24
    tieba_max_reply_num: int = 200
    tieba_min_question_len: int = 8
    tieba_max_question_len: int = 1500
    tieba_max_answer_len: int = 1000
    tieba_include_keywords: str = ""
    tieba_exclude_keywords: str = "广告,加群,兼职,推广,网赚"
    tieba_author_context: bool = True
    tieba_author_profile_ttl_days: int = 7
    tieba_distinct_authors: bool = True   # 单轮内每个作者只回一帖
    tieba_share_link: bool = True
    tieba_share_base: str = ""
    tieba_share_device: str = "tieba-bot"
    tieba_reply_prefix: str = "【AI戒色助手】"
    tieba_reply_suffix: str = "（以上内容由 AI 基于《戒为良药》《戒者录》自动生成，仅供参考）"
    tieba_state_db: str = str(BACKEND_DIR / "data" / "tieba_bot.db")


def log(msg: str) -> None:
    print(f"[{datetime.now():%m-%d %H:%M:%S}] [tieba-bot] {msg}")


class StateDB:
    """本地处理状态（SQLite）：已处理帖子去重 + 每日回帖计数 + 回复留痕可审计。"""

    def __init__(self, path: str) -> None:
        Path(path).parent.mkdir(parents=True, exist_ok=True)
        self.conn = sqlite3.connect(path)
        self.conn.execute(
            """
            CREATE TABLE IF NOT EXISTS tieba_replies (
                tid INTEGER PRIMARY KEY,
                title TEXT DEFAULT '',
                question TEXT DEFAULT '',
                answer TEXT DEFAULT '',
                status TEXT NOT NULL,              -- replied / dry_run / skipped / failed
                reason TEXT DEFAULT '',
                intent TEXT DEFAULT '',
                reply_num INTEGER DEFAULT 0,
                created_at TEXT NOT NULL
            )
            """
        )
        self.conn.execute(
            """
            CREATE TABLE IF NOT EXISTS author_profiles (
                author_id INTEGER PRIMARY KEY,
                summary TEXT NOT NULL,
                updated_at TEXT NOT NULL
            )
            """
        )
        try:  # 老库迁移：记录分享 id，便于日后撤销/审计
            self.conn.execute("ALTER TABLE tieba_replies ADD COLUMN share_id TEXT DEFAULT ''")
        except sqlite3.OperationalError:
            pass
        self.conn.commit()

    def get_author_profile(self, author_id: int, ttl_days: int) -> str | None:
        """命中缓存返回画像；过期/不存在返回 None。"""
        row = self.conn.execute(
            "SELECT summary, updated_at FROM author_profiles WHERE author_id=?", (author_id,)
        ).fetchone()
        if not row:
            return None
        try:
            fresh = datetime.now() - datetime.fromisoformat(row[1]) < timedelta(days=ttl_days)
        except ValueError:
            fresh = False
        return row[0] if fresh else None

    def save_author_profile(self, author_id: int, summary: str) -> None:
        self.conn.execute(
            "INSERT OR REPLACE INTO author_profiles (author_id, summary, updated_at) VALUES (?,?,?)",
            (author_id, summary, datetime.now().isoformat(timespec="seconds")),
        )
        self.conn.commit()

    def processed(self) -> set[int]:
        """已处理过的帖子（failed 除外，允许下轮重试）。dry_run 也算已处理：
        避免从 dry-run 切到真实模式后对同一批旧帖集中补发。"""
        rows = self.conn.execute(
            "SELECT tid FROM tieba_replies WHERE status != 'failed'"
        ).fetchall()
        return {r[0] for r in rows}

    def today_replied(self) -> int:
        today = datetime.now().strftime("%Y-%m-%d")
        return self.conn.execute(
            "SELECT COUNT(*) FROM tieba_replies WHERE status='replied' AND substr(created_at,1,10)=?",
            (today,),
        ).fetchone()[0]

    def record(self, tid: int, *, title: str = "", question: str = "", answer: str = "",
               status: str, reason: str = "", intent: str = "", reply_num: int = 0,
               share_id: str = "") -> None:
        self.conn.execute(
            "INSERT OR REPLACE INTO tieba_replies"
            " (tid, title, question, answer, status, reason, intent, reply_num, share_id, created_at)"
            " VALUES (?,?,?,?,?,?,?,?,?,?)",
            (tid, title[:200], question[:1000], answer[:3000], status, reason[:200],
             intent, reply_num, share_id, datetime.now().isoformat(timespec="seconds")),
        )
        self.conn.commit()

    def list_recent(self, limit: int = 30) -> list[tuple]:
        return self.conn.execute(
            "SELECT created_at, status, tid, substr(title,1,30), substr(reason,1,40), intent"
            " FROM tieba_replies ORDER BY created_at DESC LIMIT ?",
            (limit,),
        ).fetchall()

    def reset(self) -> None:
        self.conn.execute("DELETE FROM tieba_replies")
        self.conn.commit()


def _split_keywords(raw: str) -> list[str]:
    return [k.strip().lower() for k in raw.split(",") if k.strip()]


def clean_answer(text: str) -> str:
    """LLM 回答 → 贴吧纯文本：去掉 [n] 引用标记与 Markdown 修饰（贴吧不渲染）。"""
    text = re.sub(r"\[\d{1,2}\]", "", text)
    text = re.sub(r"[ \t]+([。，、；：！？])", r"\1", text)   # 去引用标记后残留的空格
    text = re.sub(r"\[([^\]]*)\]\([^)]*\)", r"\1", text)     # [文字](链接) → 文字
    text = re.sub(r"\*\*([^*]+)\*\*", r"\1", text)           # 粗体
    text = re.sub(r"^#{1,6}\s*", "", text, flags=re.M)       # 标题
    text = re.sub(r"^-{3,}\s*$", "", text, flags=re.M)       # 分割线
    return re.sub(r"\n{3,}", "\n\n", text).strip()


AUTHOR_PROMPT = """你是戒色吧的资深戒友。根据这位戒友近期的公开发帖记录，提炼一份不超过 150 字的背景画像，供回复时参考：
1) 当前戒色进度（打卡天数、第几次重来）；
2) 主要症状或困扰（频遗、脱发、前列腺炎、社恐、焦虑等）；
3) 反复破戒的模式或诱因（如有）；
4) 已尝试过的方法（中药、锻炼、断念等）。
只写记录中能确认的信息，没有就不写；不评价、不编造。直接输出画像文本。"""

# PC 回复接口跨吧返回，只保留与戒色话题相关的文本（否则游戏/无关发言会污染画像）
AUTHOR_REPLY_HINTS = ("戒", "破", "遗", "欲", "撸", "意淫", "手淫", "师兄", "打卡", "症状", "养生")


class TiebaBot:
    def __init__(self, settings: BotSettings, state: StateDB, post: bool) -> None:
        self.s = settings
        self.state = state
        self.post = post
        # 真实回帖必须有 BDUSS；否则强制 dry-run
        self.dry_run = not (post and settings.tieba_bduss)
        self.include_kw = _split_keywords(settings.tieba_include_keywords)
        self.exclude_kw = _split_keywords(settings.tieba_exclude_keywords)
        self.self_uid: int | None = None
        self.http: httpx.AsyncClient | None = None
        self.gen: Generator | None = Generator() if settings.tieba_author_context else None

    def _can_post_now(self) -> bool:
        if self.dry_run:
            return False
        if self.s.tieba_daily_max <= 0:
            return True
        return self.state.today_replied() < self.s.tieba_daily_max

    # ---------------- 贴吧侧 ----------------

    def _skip_reason(self, thread) -> str:
        """返回跳过原因；空串表示可回复。"""
        s = self.s
        if thread.is_top:
            return "置顶帖"
        if self.self_uid and thread.author_id == self.self_uid:
            return "自己的帖子"
        last = thread.last_time
        last_dt = datetime.fromtimestamp(last) if isinstance(last, (int, float)) else last.replace(tzinfo=None)
        age_h = (datetime.now() - last_dt).total_seconds() / 3600
        if age_h > s.tieba_max_thread_age_hours:
            return f"帖子过旧({age_h:.0f}h)"
        if thread.reply_num > s.tieba_max_reply_num:
            return f"热帖(reply={thread.reply_num})"
        title = (thread.title or "").strip()
        text = (thread.text or "").strip()
        question = f"{title}\n{text}".strip()
        if len(question) < s.tieba_min_question_len:
            return "正文过短"
        q = question.lower()
        if any(k in q for k in self.exclude_kw):
            return "命中排除词"
        if self.include_kw and not any(k in q for k in self.include_kw):
            return "未命中包含词"
        return ""

    def _pick_candidates(self, threads) -> tuple[list, list[tuple]]:
        """从帖子列表筛出候选（保持原有顺序，最新的在前），返回 (候选, [(摘要, 跳过原因)])。"""
        done = self.state.processed()
        candidates, excluded = [], []
        chosen_authors: set[int] = set()
        for t in threads:
            title = (t.title or t.text or "").strip()[:40] or f"tid={t.tid}"
            reason = self._skip_reason(t)
            if reason:
                excluded.append((title, reason))
            elif t.tid in done:
                excluded.append((title, "已处理"))
            elif self.s.tieba_distinct_authors and t.author_id in chosen_authors:
                excluded.append((title, "同作者本轮已选"))
            else:
                candidates.append(t)
                chosen_authors.add(t.author_id)
        return candidates, excluded

    # ---------------- 问答侧 ----------------

    async def _ask_qa(self, question: str, user_context: str = "") -> dict | None:
        """调用本机 /api/chat（SSE 流式），聚合回答与意图/引用信息。"""
        out: dict = {"answer": [], "intent": "", "citations": 0, "citations_data": []}
        event = ""
        try:
            async with self.http.stream(
                "POST", f"{self.s.tieba_api_base}/api/chat",
                headers={"X-Client": "tieba-bot"},
                json={"message": question, **({"user_context": user_context} if user_context else {})},
            ) as resp:
                resp.raise_for_status()
                async for line in resp.aiter_lines():
                    if line.startswith("event:"):
                        event = line[6:].strip()
                    elif line.startswith("data:"):
                        data = json.loads(line[5:].strip())
                        if event == "meta":
                            out["intent"] = (data.get("intent") or {}).get("code", "")
                        elif event == "citations":
                            out["citations_data"] = data if isinstance(data, list) else []
                            out["citations"] = len(out["citations_data"])
                        elif event == "answer":
                            out["answer"].append(data.get("delta", ""))
                        elif event == "done":
                            break
        except Exception as e:  # noqa: BLE001
            log(f"问答接口调用失败（后端未启动？）: {e!r}")
            return None
        out["answer"] = "".join(out["answer"]).strip()
        return out

    def _format_reply(self, answer: str, share_url: str = "") -> str:
        s = self.s
        answer = clean_answer(answer)
        if len(answer) > s.tieba_max_answer_len:
            answer = answer[:s.tieba_max_answer_len].rstrip() + "……"
        text = f"{s.tieba_reply_prefix}{answer}\n{s.tieba_reply_suffix}"
        if share_url:
            text += f"\n完整回答与出处：{share_url}"
        return text

    async def _create_share(self, title: str, question: str, answer: str, citations: list) -> str:
        """把本轮问答存为公开只读快照，返回前端分享页链接（失败返回空串，不影响回帖）。"""
        if not self.s.tieba_share_link or not self.http:
            return ""
        base = (self.s.tieba_share_base or self.s.tieba_api_base).rstrip("/")
        payload = {
            "title": f"戒色吧问答｜{title}"[:80],
            "device_id": self.s.tieba_share_device,
            "sessions": [{
                "title": title[:80] or "戒色吧问答",
                "created_at": int(datetime.now().timestamp() * 1000),
                "messages": [
                    {"role": "user", "content": question},
                    {"role": "assistant", "content": answer, "query": question,
                     "citations": citations or []},
                ],
            }],
        }
        try:
            resp = await self.http.post(f"{self.s.tieba_api_base}/api/shares", json=payload)
            resp.raise_for_status()
            share_id = (resp.json() or {}).get("id", "")
        except Exception as e:  # noqa: BLE001
            log(f"  分享链接创建失败（本条回复将不带链接）: {e!r}")
            return ""
        return f"{base}/#/s/{share_id}" if share_id else ""

    # ---------------- 作者背景（历史发帖画像） ----------------

    async def _fetch_author_history(self, client: aiotieba.Client, uid: int, exclude_tid: int) -> str:
        """拉取作者近期主题帖与回复（均公开可见、无需登录），拼成供 LLM 摘要的文本。"""
        lines: list[str] = []

        def _date(ts) -> str:
            try:
                return datetime.fromtimestamp(ts).strftime("%Y-%m-%d")
            except (TypeError, ValueError, OSError):
                return "?"

        try:
            threads = await client.get_user_threads(uid)
            n = 0
            for t in threads:
                if t.tid == exclude_tid:
                    continue
                if t.fname and t.fname != self.s.tieba_forum:   # 只看目标吧内的发言
                    continue
                title = (t.title or "").strip()
                text = (t.text or "").strip()
                if title and text.startswith(title):
                    text = text[len(title):]
                lines.append(f"[主题 {_date(t.create_time)}] {title}｜{text[:120]}")
                n += 1
                if n >= 15:
                    break
        except Exception as e:  # noqa: BLE001  隐私设置/接口异常 → 按无历史处理
            log(f"  拉取作者主题帖失败: {e!r}")

        try:
            posts = await client.get_user_posts_pc(uid)
            for p in list(posts)[:8]:
                text = (getattr(p, "text", "") or "").strip()
                if text and any(k in text for k in AUTHOR_REPLY_HINTS):
                    lines.append(f"[回复 {_date(getattr(p, 'create_time', None))}] {text[:80]}")
        except Exception as e:  # noqa: BLE001
            log(f"  拉取作者回复失败: {e!r}")

        return "\n".join(lines)[:3000]

    async def _author_profile(self, client: aiotieba.Client, uid: int, exclude_tid: int) -> str:
        """作者历史发帖 → 150 字画像（缓存 N 天）；LLM 未配置或拉取失败则返回空串。"""
        if not uid or self.gen is None or not self.gen.enabled:
            return ""
        cached = self.state.get_author_profile(uid, self.s.tieba_author_profile_ttl_days)
        if cached is not None:
            return cached
        history = await self._fetch_author_history(client, uid, exclude_tid)
        if not history:
            return ""
        try:
            summary = await self.gen.complete(
                [
                    {"role": "system", "content": AUTHOR_PROMPT},
                    {"role": "user", "content": history},
                ],
                temperature=0.2, max_tokens=300,
            )
        except Exception as e:  # noqa: BLE001
            log(f"  作者画像生成失败: {e!r}")
            return ""
        summary = (summary or "").strip()[:400]
        if summary:
            self.state.save_author_profile(uid, summary)
        return summary

    # ---------------- 主流程 ----------------

    async def run_once(self, client: aiotieba.Client, only_tids: set[int] | None = None) -> None:
        # 发帖模式下先取自己的 uid：跳过自己发的帖子（含介绍帖），避免自问自答
        if not self.dry_run and self.self_uid is None:
            me = await client.get_self_info()
            self.self_uid = getattr(me, "user_id", None)
        threads = await client.get_threads(
            self.s.tieba_forum, rn=100, sort=aiotieba.ThreadSortType.CREATE,
        )
        if only_tids:
            threads = [t for t in threads if t.tid in only_tids]
            missing = only_tids - {t.tid for t in threads}
            if missing:
                log(f"指定 tid={sorted(missing)} 不在最新 100 帖内（可能已沉或 tid 有误）")
            if not threads:
                return
        candidates, excluded = self._pick_candidates(threads)
        for title, reason in excluded[:8]:
            log(f"  跳过「{title}」: {reason}")
        if len(excluded) > 8:
            log(f"  ...其余 {len(excluded) - 8} 条同样跳过")
        if not candidates:
            log("本轮没有可回复的新帖")
            return

        daily_left = self.s.tieba_daily_max - self.state.today_replied()
        if not self.dry_run and self.s.tieba_daily_max > 0 and daily_left <= 0:
            log(f"已达每日回帖上限（{self.s.tieba_daily_max}），本轮仅生成不回帖")
        budget = min(self.s.tieba_max_replies_per_run, len(candidates))
        log(f"候选 {len(candidates)} 帖，本轮处理前 {budget} 帖（mode={'POST' if not self.dry_run else 'DRY-RUN'}）")

        replied = 0
        batch = candidates[:budget]
        for idx, t in enumerate(batch):
            title = (t.title or t.text or "").strip()[:40]
            t_title = (t.title or "").strip()
            t_text = (t.text or "").strip()
            # thread.text 通常自带标题，避免重复拼接
            question = t_text if (t_title and t_text.startswith(t_title)) else f"{t_title}\n{t_text}".strip()
            if len(question) > self.s.tieba_max_question_len:
                question = question[:self.s.tieba_max_question_len]
            author_ctx = await self._author_profile(client, t.author_id, t.tid)
            if author_ctx:
                log(f"  作者背景(uid={t.author_id}): {author_ctx[:100]}")
            qa = await self._ask_qa(question, author_ctx)
            # LLM 偶发失败会回退成「原文摘录」文案：等几秒重试一次，仍失败才记 failed
            if qa is not None and qa["answer"].startswith("（") and "最相关的原文摘录" in qa["answer"]:
                log("  LLM 生成失败（摘录回退），5 秒后重试一次 ...")
                await asyncio.sleep(5)
                qa = await self._ask_qa(question, author_ctx)
            if qa is None:
                self.state.record(t.tid, title=title, status="failed", reason="qa_error", reply_num=t.reply_num)
                await asyncio.sleep(10)   # 失败退避，避免连续撞限流
                continue

            intent = qa["intent"]
            if intent == "chitchat":
                self.state.record(t.tid, title=title, status="skipped",
                                  reason="意图=闲聊/无关", intent=intent, reply_num=t.reply_num)
                log(f"  跳过「{title}」: 意图识别为闲聊/无关")
                continue
            if qa["citations"] == 0:
                self.state.record(t.tid, title=title, status="skipped",
                                  reason="知识库无相关内容", intent=intent, reply_num=t.reply_num)
                log(f"  跳过「{title}」: 检索无相关摘录")
                continue
            if not qa["answer"]:
                self.state.record(t.tid, title=title, status="failed",
                                  reason="回答为空", intent=intent, reply_num=t.reply_num)
                continue
            if qa["answer"].startswith("（") and "最相关的原文摘录" in qa["answer"]:
                # 生成器的摘录回退文案（LLM 调用失败时），对贴吧读者无意义 → 记 failed 下轮重试
                self.state.record(t.tid, title=title, question=question, status="failed",
                                  reason="LLM生成失败(摘录回退)", intent=intent, reply_num=t.reply_num)
                log(f"  跳过「{title}」: LLM 生成失败（摘录回退），下轮重试")
                await asyncio.sleep(10)   # 失败退避，避免连续撞限流
                continue

            share_url = ""
            if self.s.tieba_share_link:
                share_url = await self._create_share(
                    title, question, qa["answer"], qa.get("citations_data", []),
                )
                if share_url:
                    log(f"  分享链接: {share_url}")
            reply = self._format_reply(qa["answer"], share_url)
            if self.dry_run:
                self.state.record(t.tid, title=title, question=question, answer=qa["answer"],
                                  status="dry_run", intent=intent, reply_num=t.reply_num,
                                  share_id=share_url.rsplit("/", 1)[-1] if share_url else "")
                log(f"  [dry-run] 回复「{title}」:\n{'-' * 46}\n{reply}\n{'-' * 46}")
            elif self._can_post_now():
                log(f"  即将回帖「{title}」:\n{'-' * 46}\n{reply[:400]}{'…' if len(reply) > 400 else ''}\n{'-' * 46}")
                res = await client.add_post(self.s.tieba_forum, t.tid, reply)
                err = getattr(res, "err", None)
                if err:
                    self.state.record(t.tid, title=title, question=question, answer=qa["answer"],
                                      status="failed", reason=f"add_post: {err}", intent=intent,
                                      reply_num=t.reply_num,
                                      share_id=share_url.rsplit("/", 1)[-1] if share_url else "")
                    log(f"  回帖失败「{title}」: {err}")
                else:
                    self.state.record(t.tid, title=title, question=question, answer=qa["answer"],
                                      status="replied", intent=intent, reply_num=t.reply_num,
                                      share_id=share_url.rsplit("/", 1)[-1] if share_url else "")
                    replied += 1
                    log(f"  已回帖「{title}」(tid={t.tid})")
            else:
                self.state.record(t.tid, title=title, question=question, answer=qa["answer"],
                                  status="skipped", reason="每日回帖上限", intent=intent,
                                  reply_num=t.reply_num,
                                  share_id=share_url.rsplit("/", 1)[-1] if share_url else "")
                log(f"  跳过「{title}」: 每日回帖上限")
            if replied and self.s.tieba_reply_interval > 0 and idx + 1 < len(batch):
                await asyncio.sleep(self.s.tieba_reply_interval)
        log(f"本轮结束：真实回帖 {replied} 条，累计今日 {self.state.today_replied()} 条")

    async def check_login(self) -> None:
        """--whoami：校验 BDUSS 是否有效，打印账号名与 uid（不发任何内容）。"""
        if not self.s.tieba_bduss:
            raise SystemExit("未配置 TIEBA_BDUSS：请先按 README 获取并写入 backend/.env")
        async with aiotieba.Client(self.s.tieba_bduss) as client:
            user = await client.get_self_info()
            name = getattr(user, "user_name", None) or getattr(user, "show_name", None)
            log(f"登录成功：{name}（uid={getattr(user, 'user_id', None)}）")

    async def run_forever(self) -> None:
        bduss = self.s.tieba_bduss if not self.dry_run else ""
        async with aiotieba.Client(bduss) as client:
            if not self.dry_run:
                user = await client.get_self_info()
                self.self_uid = getattr(user, "user_id", None)
                log(f"回帖账号: {getattr(user, 'user_name', None) or getattr(user, 'show_name', self.self_uid)}")
            while True:
                try:
                    await self.run_once(client)
                except Exception as e:  # noqa: BLE001
                    log(f"本轮运行出错: {e!r}")
                log(f"休眠 {self.s.tieba_poll_interval}s ...")
                await asyncio.sleep(self.s.tieba_poll_interval)

    async def run_scan(self) -> None:
        """--scan：只看选帖结果，不调问答、不发帖、不写状态。"""
        async with aiotieba.Client() as client:
            threads = await client.get_threads(
                self.s.tieba_forum, rn=100, sort=aiotieba.ThreadSortType.CREATE,
            )
            candidates, excluded = self._pick_candidates(threads)
            log(f"共拉取 {len(threads)} 帖：候选 {len(candidates)}，跳过 {len(excluded)}")
            for t in candidates:
                question = f"{(t.title or '').strip()}\n{(t.text or '').strip()}".strip()
                log(f"  [候选] tid={t.tid} reply={t.reply_num} 「{(t.title or t.text or '').strip()[:40]}」")
                log(f"         提问: {question[:100].replace(chr(10), ' / ')}")
            for title, reason in excluded:
                log(f"  [跳过] 「{title}」: {reason}")


async def main() -> None:
    parser = argparse.ArgumentParser(description="戒色吧 AI 自动回复机器人")
    parser.add_argument("--scan", action="store_true", help="只扫描候选帖子，不调问答不发帖")
    parser.add_argument("--once", action="store_true", help="单次运行后退出")
    parser.add_argument("--post", action="store_true", help="真实回帖（缺省 dry-run；需 TIEBA_BDUSS）")
    parser.add_argument("--whoami", action="store_true", help="校验 BDUSS 并打印账号名（不发帖）")
    parser.add_argument("--tid", help="只处理指定帖子 tid，多个用逗号分隔（测试用）")
    parser.add_argument("--list", action="store_true", help="查看处理记录")
    parser.add_argument("--reset", action="store_true", help="清空处理记录")
    args = parser.parse_args()

    settings = BotSettings()
    state = StateDB(settings.tieba_state_db)

    if args.reset:
        state.reset()
        log("处理记录已清空")
        return
    if args.list:
        for row in state.list_recent():
            log(f"  {row[0]} [{row[1]:>7}] tid={row[2]} 「{row[3]}」 intent={row[5]} {row[4]}")
        return
    if args.post and not settings.tieba_bduss:
        raise SystemExit("真实回帖需要在 backend/.env 配置 TIEBA_BDUSS（不配则只能 dry-run）")

    bot = TiebaBot(settings, state, post=args.post)
    mode = "DRY-RUN" if bot.dry_run else "POST"
    log(f"启动：吧={settings.tieba_forum} 模式={mode} 状态库={settings.tieba_state_db}")
    async with httpx.AsyncClient(timeout=httpx.Timeout(300.0, connect=10.0)) as http:
        bot.http = http
        if args.whoami:
            await bot.check_login()
        elif args.scan:
            await bot.run_scan()
        elif args.once:
            async with aiotieba.Client("" if bot.dry_run else settings.tieba_bduss) as client:
                tids = {int(x) for x in str(args.tid or "").replace("，", ",").split(",") if x.strip().isdigit()}
                await bot.run_once(client, only_tids=tids or None)
        else:
            await bot.run_forever()


if __name__ == "__main__":
    try:
        asyncio.run(main())
    except KeyboardInterrupt:
        print("\n[tieba-bot] 已停止")
