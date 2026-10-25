"""生成：LLM 模式（OpenAI 兼容协议）+ 摘录模式（未配置 LLM 时的开箱即用回退）。"""
from __future__ import annotations

import re

from openai import AsyncOpenAI

from .config import settings
from .retriever import Citation

SYSTEM_PROMPT = """你是一个基于《戒为良药》与《戒者录》两本书的戒色知识助手。请严格遵守：

1. 仅依据下面给出的编号摘录回答问题，不要编造书中没有的内容。
2. 回答中引用某条摘录时，在对应句子末尾标注 [n]（如"断念口诀是核心工具[1]"）。
3. 多条摘录观点一致时归纳陈述；观点不同或矛盾时，分开陈述并注明各自来源，不要强行统一。
4. 摘录是书中作者的断言与经验分享，忠实转述其观点即可，注明"书中认为/飞翔经验/某戒友经验"，不要将其表述为医学诊断或普遍事实。
5. 用简体中文，条理清晰，先直接回答问题，再展开细节。"""


class Generator:
    def __init__(self) -> None:
        self.client = AsyncOpenAI(
            base_url=settings.openai_base_url,
            api_key=settings.openai_api_key,
            timeout=60,
        ) if settings.llm_enabled else None

    @property
    def enabled(self) -> bool:
        return self.client is not None

    @property
    def _extra_body(self) -> dict | None:
        """Qwen3 系列显式关闭原生思考模式（本项目的深度思考走 prompt 级指令）。"""
        model = settings.llm_model
        if "Qwen3" in model and "Thinking" not in model:
            return {"enable_thinking": False}
        return None

    async def complete(self, messages: list[dict], temperature: float, max_tokens: int) -> str:
        """单次非流式调用（查询改写/会话摘要等辅助任务共用）。失败抛异常由调用方兜底。"""
        resp = await self.client.chat.completions.create(
            model=settings.llm_model,
            messages=messages,
            temperature=temperature,
            max_tokens=max_tokens,
            extra_body=self._extra_body,
        )
        return self._clean(resp.choices[0].message.content or "")

    @staticmethod
    def _clean(text: str) -> str:
        """防御性清洗：剥离可能泄漏的思考过程标签。"""
        return re.sub(r"<think>.*?</think>\s*", "", text, flags=re.S).strip()

    async def rewrite_query(self, message: str, history: list[dict], summary: str | None = None) -> str:
        """多轮对话查询改写：持久化摘要 + 最近历史 + 当前问题 → 独立完整查询。

        摘要覆盖保留窗口之外的长程上下文（ragent 的「摘要 + 最近 N 轮」组合），
        使远距离指代（如多轮之前提过的症状/方法）也能被改写进检索查询。
        """
        if not history or not self.enabled:
            return message
        try:
            lines = "\n".join(f"{m['role']}: {m['content']}" for m in history[-6:])
            context = f"对话历史：\n{lines}\n\n最新问题：{message}"
            if summary and summary.strip():
                context = f"对话背景摘要（更早的对话）：\n{summary.strip()}\n\n{context}"
            return await self.complete(
                [
                    {"role": "system", "content": "把用户的最新问题结合对话历史改写为一个独立、完整的检索查询。只输出改写后的查询本身，不要解释。"},
                    {"role": "user", "content": context},
                ],
                temperature=0.0,
                max_tokens=200,
            ) or message
        except Exception as e:  # noqa: BLE001
            print(f"[generator] 查询改写失败，使用原始问题: {e}")
            return message

    async def stream_generate(
        self, query: str, citations: list[Citation], deep_thinking: bool = False,
        memory_messages: list[dict] | None = None, intent_prompt: str = "",
    ):
        """流式生成：异步产出 ("thinking"|"answer", delta)。

        深度思考 → 开启 Qwen3 原生思维链（reasoning_content 先流式产出）+ 分点分析指令；
        普通模式 → 关闭原生思考，仅流式 answer。LLM 不可用/失败 → 单块摘录回退。
        memory_messages：会话记忆注入（摘要 system + 最近几轮原文，来自 memory.py）。
        """
        if not self.enabled:
            yield "answer", self._extractive(query, citations, deep_thinking)
            return

        system = SYSTEM_PROMPT
        if deep_thinking:
            system += "\n6. 本次开启「深度思考」：请进行更系统的分析——先给结论，再分点展开（机理、可执行方法、常见误区、案例佐证），尽量覆盖所有相关摘录，明确区分书中共识与个别戒友经验，结尾给出简明的行动建议。"
        if intent_prompt:
            system += f"\n7. 本次回答遵循以下风格指引：{intent_prompt}"
        excerpts = "\n\n".join(
            f"[{i}] 〔{c.breadcrumb}｜{c.doc_type}{('｜' + c.author) if c.author else ''}〕\n{c.text}"
            for i, c in enumerate(citations, 1)
        )

        extra = dict(self._extra_body or {})
        extra["enable_thinking"] = bool(deep_thinking)  # 深度思考 = 原生思维链
        try:
            stream = await self.client.chat.completions.create(
                model=settings.llm_model,
                messages=[
                    {"role": "system", "content": system},
                    *(memory_messages or []),
                    {"role": "user", "content": f"问题：{query}\n\n参考摘录：\n{excerpts}"},
                ],
                temperature=0.3,
                max_tokens=2400 if deep_thinking else 1200,
                stream=True,
                extra_body=extra,
            )
            async for chunk in stream:
                delta = chunk.choices[0].delta if chunk.choices else None
                if delta is None:
                    continue
                reasoning = getattr(delta, "reasoning_content", None)
                if reasoning:
                    yield "thinking", reasoning
                content = getattr(delta, "content", None)
                if content:
                    # 逐块原样透传：任何清洗（含 strip）都会剥掉块边界的换行，
                    # 导致 Markdown 的换行/标题/分割线全部失效
                    yield "answer", content
        except Exception as e:  # noqa: BLE001
            print(f"[generator] 流式生成失败，回退摘录模式: {e}")
            yield "answer", self._extractive(query, citations, deep_thinking)

    @staticmethod
    def _extractive(query: str, citations: list[Citation], deep_thinking: bool = False) -> str:
        if not citations:
            return "未在两本书中找到与该问题直接相关的内容，请尝试换个问法（例如提及具体症状或方法名称）。"
        mode = "已深度检索，" if deep_thinking else ""
        return f"（{mode}以下为与「{query}」最相关的原文摘录，点击下方引用可查看原文出处）"
