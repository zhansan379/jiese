import { useEffect, useMemo, useRef, useState } from 'react'
import { Button, Spin, Tooltip, Typography } from 'antd'
import {
  ArrowDownOutlined, CheckOutlined, CopyOutlined, DislikeFilled, DislikeOutlined,
  DownOutlined, LikeFilled, LikeOutlined, LoadingOutlined, ShareAltOutlined, UpOutlined,
} from '@ant-design/icons'
import { AnimatePresence, motion } from 'framer-motion'
import ReactMarkdown from 'react-markdown'
import remarkGfm from 'remark-gfm'
import ClaimDrawer from './ClaimDrawer.jsx'
import SourceCard from './SourceCard.jsx'

const { Text } = Typography

// ---------- 共识表述标注（答案内「飞翔及 N 位戒友均强调」→ 可点击胶囊） ----------

const _norm = (s) => (s || '').replace(/[^\u4e00-\u9fa5A-Za-z0-9]/g, '')

function _bigrams(s) {
  const t = _norm(s)
  const set = new Set()
  for (let i = 0; i < t.length - 1; i += 1) set.add(t.slice(i, i + 2))
  return set
}

/** 字面重合度（以较短一方归一，鼓励“断言包含在句中”的匹配） */
function _overlap(a, b) {
  const A = _bigrams(a)
  const B = _bigrams(b)
  if (!A.size || !B.size) return 0
  let n = 0
  A.forEach((g) => { if (B.has(g)) n += 1 })
  return n / Math.min(A.size, B.size)
}

/** 匹配上下文：向前取一段（断言通常写在“均强调”前一句）+ 向后取少量，便于字面裁决 */
function _contextAround(text, start, end) {
  return text.slice(Math.max(0, start - 180), Math.min(text.length, end + 60))
}

/** 本次答案可用的共识点（去重） */
function collectClaims(citations) {
  const byId = new Map()
  ;(citations || []).forEach((c) => {
    ;(c.claims || []).forEach((cl) => { if (!byId.has(cl.id)) byId.set(cl.id, cl) })
  })
  return [...byId.values()]
}

function _leadOf(claim) {
  const authors = claim.authors || []
  return authors.find((a) => a === '飞翔' || a === '前辈') || authors[0] || ''
}

/** 共识短语 → 具体共识点：领衔作者 + 支持数过滤，字面重合度裁决，多候选时取唯一最大支持度 */
function resolveClaim(claims, lead, countRaw, context) {
  if (!claims.length) return null
  let pool = claims
  if (lead) {
    const byLead = pool.filter((cl) => (cl.authors || []).some(
      (a) => a === lead || (a && a.startsWith(lead)),
    ))
    if (byLead.length) pool = byLead
  }
  let exact = []
  if (/^\d+$/.test(countRaw)) {
    exact = pool.filter((cl) => cl.support_authors === Number(countRaw))
  } else if (/^(数十|几十|多|[一二三四五六七八九十百]+)$/.test(countRaw)) {
    exact = pool.filter((cl) => (cl.support_authors || 0) >= 10)
  }
  if (exact.length === 1) return exact[0]
  const cands = exact.length ? exact : pool
  let best = null
  let bestScore = 0
  cands.forEach((cl) => {
    const s = _overlap(cl.text, context)
    if (s > bestScore) { best = cl; bestScore = s }
  })
  if (best && bestScore >= 0.06) return best
  if (cands.length > 1) {
    const maxSupport = Math.max(...cands.map((c) => c.support_authors || 0))
    const top = cands.filter((c) => c.support_authors === maxSupport)
    return top.length === 1 ? top[0] : null
  }
  return null
}

/**
 * 把答案里的共识表述替换为 Markdown 链接 [短语](claim://id)，由 a 渲染器变成可点击胶囊。
 * 只标注能解析到具体共识点的短语；解析不到保持原文。
 */
function annotateConsensus(text, citations) {
  const claims = collectClaims(citations)
  if (!claims.length || !text) return text
  const re = /(数十|几十|多|[一二三四五六七八九十百]{1,3}|\d{1,4})\s*位戒友\s*(?:均|都|一致)?(?:强调|认为)/g
  let out = ''
  let last = 0
  let m = re.exec(text)
  while (m !== null) {
    let start = m.index
    const before = text.slice(Math.max(0, start - 8), start)
    const lm = before.match(/([\u4e00-\u9fa5A-Za-z0-9_]{1,4})及\s*$/)
    const lead = lm ? lm[1] : ''
    if (lm) start -= lm[0].length
    const end = m.index + m[0].length
    const phrase = (lead ? `${lead}及` : '') + m[0]
    const claim = resolveClaim(claims, lead, m[1], _contextAround(text, start, end))
    out += text.slice(last, start)
    // 用内联锚点而非自定义协议：react-markdown 会清洗非白名单协议（claim:// 会被丢弃）
    out += claim ? `[${phrase}](#claim-${claim.id})` : text.slice(start, end)
    last = end
    re.lastIndex = end
    m = re.exec(text)
  }
  out += text.slice(last)
  return out
}

/**
 * 滚动到指定引用卡片（scrollIntoView 在 .main-scroll 内会静默失效，直接驱动滚动容器）。
 * smooth=false 用于“从抽屉跳转”：平滑动画期间的重渲染会被贴底逻辑打断，即时滚动更可靠。
 */
function jumpToCite(n, smooth = true) {
  const el = document.getElementById(`cite-${n}`)
  const sc = el?.closest('.main-scroll')
  if (el && sc) {
    const r = el.getBoundingClientRect()
    const sr = sc.getBoundingClientRect()
    sc.scrollTo({ top: sc.scrollTop + r.top - sr.top - 80, behavior: smooth ? 'smooth' : 'auto' })
  }
}

const PRESET_QUESTIONS = [
  '滑精很频繁怎么办',
  '一到周末就忍不住破戒',
  '断念口诀具体怎么用',
  '社恐能恢复吗',
  '戒了很久突然破了，很绝望',
  '脱发还能长回来吗',
]

/**
 * 答案 Markdown 渲染：
 * - [n] 引用标注 → 可点击角标（滚动到对应引用卡片）
 * - 共识表述（“飞翔及 10 位戒友均强调”）→ 可点击「共识」胶囊（打开证据抽屉）
 */
function AnswerMarkdown({ text, citations, citationCount, onOpenClaim }) {
  const md = useMemo(() => {
    let out = text
    if (citationCount) {
      const inRange = (n) => Number(n) >= 1 && Number(n) <= citationCount
      // 角标统一放句末标点之后：“……频繁[2]。” → “……频繁。[2]”
      const moved = out.replace(/((?:\[\d{1,2}\])+)([。．！？；…]+)/g, (all, marks, punct) =>
        [...marks.matchAll(/\d+/g)].every((m) => inRange(m[0])) ? `${punct}${marks}` : all)
      out = moved.replace(/\[(\d{1,2})\]/g, (all, n) =>
        inRange(n) ? `[${n}](#cite-${n})` : all)
    }
    return annotateConsensus(out, citations)
  }, [text, citations, citationCount])

  return (
    <div className="answer-md">
      <ReactMarkdown
        remarkPlugins={[remarkGfm]}
        components={{
          a: ({ href, children }) => {
            const claimMatch = typeof href === 'string' ? href.match(/^#claim-(\d+)$/) : null
            if (claimMatch) {
              return (
                <button
                  type="button"
                  className="consensus-chip"
                  title="查看该共识的证据原文"
                  onClick={(e) => { e.preventDefault(); onOpenClaim?.(Number(claimMatch[1])) }}
                >
                  <span className="consensus-chip-icon" aria-hidden>◆</span>
                  {children}
                </button>
              )
            }
            const citeMatch = typeof href === 'string' ? href.match(/^#cite-(\d+)$/) : null
            if (!citeMatch) return <a href={href} target="_blank" rel="noreferrer">{children}</a>
            const jump = (e) => {
              e.preventDefault()
              jumpToCite(citeMatch[1])
            }
            return <a href={href} className="cite-ref" onClick={jump}><sup>{children}</sup></a>
          },
        }}
      >
        {md}
      </ReactMarkdown>
    </div>
  )
}

/** AI 回答操作栏：复制 / 点赞 / 点踩 / 分享（点赞点踩互斥，再点取消；记录入库） */
function AnswerActions({ message, index, session, onFeedback, onShare }) {
  const [copied, setCopied] = useState(false)
  const rating = message.rating || 0

  const copy = async () => {
    try {
      await navigator.clipboard.writeText(message.content)
      setCopied(true)
      setTimeout(() => setCopied(false), 1500)
    } catch { /* 剪贴板不可用（如非安全上下文）时静默 */ }
  }

  return (
    <div className="msg-actions">
      <Tooltip title={copied ? '已复制' : '复制'} mouseEnterDelay={0.4}>
        <button type="button" className={`act-btn${copied ? ' ok' : ''}`} onClick={copy} aria-label="复制">
          {copied ? <CheckOutlined /> : <CopyOutlined />}
        </button>
      </Tooltip>
      <Tooltip title={rating === 1 ? '取消点赞' : '赞同'} mouseEnterDelay={0.4}>
        <button
          type="button" className={`act-btn${rating === 1 ? ' on' : ''}`} aria-label="点赞"
          onClick={() => onFeedback(session.id, index, 1)}
        >
          {rating === 1 ? <LikeFilled /> : <LikeOutlined />}
        </button>
      </Tooltip>
      <Tooltip title={rating === -1 ? '取消点踩' : '不赞同'} mouseEnterDelay={0.4}>
        <button
          type="button" className={`act-btn${rating === -1 ? ' on' : ''}`} aria-label="点踩"
          onClick={() => onFeedback(session.id, index, -1)}
        >
          {rating === -1 ? <DislikeFilled /> : <DislikeOutlined />}
        </button>
      </Tooltip>
      <Tooltip title="分享为图片" mouseEnterDelay={0.4}>
        <button
          type="button" className="act-btn" aria-label="分享"
          onClick={() => onShare(session.id)}
        >
          <ShareAltOutlined />
        </button>
      </Tooltip>
    </div>
  )
}

/** 深度思考面板：思考中实时展开灰字，首个答案块到达自动收起为“已深度思考（用时X秒）”，可点击回看 */
function ThinkingPanel({ message }) {
  const [open, setOpen] = useState(true)
  const live = message.streaming && !message.thinkingSeconds && message.thinkingStart

  useEffect(() => {
    if (message.streaming && message.thinkingSeconds) setOpen(false)
  }, [message.streaming, message.thinkingSeconds])
  useEffect(() => {
    if (!message.streaming) setOpen(false)
  }, [message.streaming])

  return (
    <div className="think-panel">
      <button type="button" className="think-header" onClick={() => setOpen((v) => !v)}>
        {live
          ? <><LoadingOutlined className="think-icon" /> 深度思考中…</>
          : <>已深度思考{message.thinkingSeconds ? `（用时 ${message.thinkingSeconds} 秒）` : ''}</>}
        {open ? <UpOutlined className="think-arrow" /> : <DownOutlined className="think-arrow" />}
      </button>
      {open && (
        <div className="think-body">
          {message.thinking || '…'}
          {live && <span className="think-cursor">▍</span>}
        </div>
      )}
    </div>
  )
}

/** 中间滚动内容区：当前会话的消息流（SSE 流式 + 思考面板 + Markdown） */
export default function MessagesView({ session, loading, onOpenSource, onPreset, onFeedback, onShare }) {
  const bottomRef = useRef(null)
  const stickRef = useRef(true)          // 是否贴底跟随（用户上滑后置 false）
  const lastJumpAt = useRef(0)           // "滚动到最新"动画窗口，期间不干预
  const lastProgTop = useRef(0)          // 最近一次程序贴底后的 scrollTop 基准
  const lastH = useRef(0)                // 上次观测的 scrollHeight（检测内容塌缩）
  const [showJump, setShowJump] = useState(false)
  const [claimView, setClaimView] = useState(null)   // { id, citations }：共识证据抽屉
  const messages = session?.messages || []

  useEffect(() => {
    const sc = bottomRef.current?.closest('.main-scroll')
    if (!sc) return
    if (Date.now() - lastJumpAt.current < 700) return   // 回底动画进行中

    // 内容塌缩（会话切换旧消息移除、思考面板收起、流式 Markdown 重排收缩）会让 scrollTop 被钳制
    // ——那不是用户上滑：大塌缩(>400px, 会话更替)回到新会话底部；小塌缩仅对齐基准、不打断阅读
    if (sc.scrollHeight < lastH.current - 40) {
      const bigSwap = lastH.current - sc.scrollHeight > 400
      if (stickRef.current || bigSwap) {
        stickRef.current = true
        setShowJump(false)
        sc.scrollTo({ top: sc.scrollHeight })
        lastProgTop.current = sc.scrollTop
      } else {
        lastProgTop.current = Math.min(lastProgTop.current, sc.scrollTop)
      }
      lastH.current = sc.scrollHeight
      return
    }
    lastH.current = sc.scrollHeight

    // 用户上滑必然改变 scrollTop；内容增长只增加 scrollHeight 不动 scrollTop。
    // 据此区分二者：scrollTop 比程序贴底基准低超过 40px ⇒ 用户主动上滑。
    const userScrolledUp = sc.scrollTop < lastProgTop.current - 40
    if (userScrolledUp) {
      stickRef.current = false
      setShowJump(true)
      return
    }
    if (!stickRef.current) return
    sc.scrollTo({ top: sc.scrollHeight })
    // 基准取滚动后实际到达的位置（被钳制即为真实值），防止陈旧几何量污染
    lastProgTop.current = sc.scrollTop
  })

  // 用户手动滚回底部 ⇒ 恢复跟随并隐藏按钮（document capture 委托，免疫元素替换/挂载时序）
  useEffect(() => {
    const onScroll = (e) => {
      const sc = e.target
      if (!(sc instanceof Element) || !sc.classList?.contains('main-scroll')) return
      if (!bottomRef.current) return
      if (Date.now() - lastJumpAt.current < 700) return
      const away = sc.scrollHeight - sc.scrollTop - sc.clientHeight > 80
      stickRef.current = !away
      setShowJump(away)
    }
    document.addEventListener('scroll', onScroll, { capture: true, passive: true })
    return () => document.removeEventListener('scroll', onScroll, { capture: true })
  }, [])

  const jumpToLatest = () => {
    lastJumpAt.current = Date.now()
    stickRef.current = true
    setShowJump(false)
    bottomRef.current?.closest('.main-scroll')?.scrollTo({ top: 1e9, behavior: 'smooth' })
  }

  return (
    <div className="msg-wrap">
      {messages.length === 0 && !loading && (
        <div className="chat-welcome">
          <img className="welcome-logo" src="/logo-seal.png" alt="" />
          <div className="welcome-title">你好，戒友</div>
          <div className="welcome-sub">我能帮什么忙？</div>
          <div className="preset-grid">
            {PRESET_QUESTIONS.map((q) => (
              <Button key={q} onClick={() => onPreset(q)} className="preset-btn">{q}</Button>
            ))}
          </div>
        </div>
      )}

      <AnimatePresence initial={false}>
        {messages.map((m, i) => (
          <motion.div
            key={i}
            initial={{ opacity: 0, y: 10 }}
            animate={{ opacity: 1, y: 0 }}
            transition={{ duration: 0.25 }}
            className={m.role === 'user' ? 'msg-row user' : 'msg-row ai'}
          >
            {m.role === 'user'
              ? <div className="bubble user">{m.content}</div>
              : (
                <div className="ai-msg">
                  {m.thinking ? <ThinkingPanel message={m} /> : null}
                  {m.content
                    ? (
                      <AnswerMarkdown
                        text={m.content}
                        citations={m.citations}
                        citationCount={m.citations?.length || 0}
                        onOpenClaim={(id) => setClaimView({ id, citations: m.citations })}
                      />
                    )
                    : null}
                  {m.streaming && !m.content && !m.thinking && (
                    <div className="ai-loading">
                      <Spin size="small" /> <Text type="secondary">正在检索两本书…</Text>
                    </div>
                  )}
                  {m.query && m.query !== messages[i - 1]?.content && (
                    <Text type="secondary" className="query-trace">本条查询：{m.query}</Text>
                  )}
                  {m.citations?.length > 0 && (
                    <div className="citations">
                      <Text type="secondary" className="citations-title">
                        参考来源（点击条目跳转原文预览）
                      </Text>
                      {m.citations.map((c, j) => (
                        <SourceCard key={c.chunk_id} index={j + 1} citation={c} onOpenSource={onOpenSource} />
                      ))}
                    </div>
                  )}
                  {!m.streaming && m.content && (
                    <AnswerActions message={m} index={i} session={session} onFeedback={onFeedback} onShare={onShare} />
                  )}
                </div>
              )}
          </motion.div>
        ))}
      </AnimatePresence>
      <div ref={bottomRef} />
      {showJump && (
        <button type="button" className="jump-latest" onClick={jumpToLatest}>
          <ArrowDownOutlined /> 滚动到最新
        </button>
      )}

      <ClaimDrawer
        claimId={claimView?.id}
        citations={claimView?.citations}
        onClose={() => setClaimView(null)}
        onOpenSource={onOpenSource}
        onJumpCite={(n) => {
          setClaimView(null)
          // 先停止“贴底跟随”：抽屉关闭会触发重渲染，否则自动贴底会把刚跳转的视图拉回底部
          stickRef.current = false
          setShowJump(true)
          // 等抽屉关闭动画（约 300ms）结束再滚动；即时滚动避免被贴底重渲染打断
          setTimeout(() => jumpToCite(n, false), 380)
        }}
      />
    </div>
  )
}
