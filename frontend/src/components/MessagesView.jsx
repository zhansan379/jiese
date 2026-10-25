import { useEffect, useMemo, useRef, useState } from 'react'
import { Button, Spin, Tooltip, Typography } from 'antd'
import {
  ArrowDownOutlined, CheckOutlined, CopyOutlined, DislikeFilled, DislikeOutlined,
  DownOutlined, LikeFilled, LikeOutlined, LoadingOutlined, ShareAltOutlined, UpOutlined,
} from '@ant-design/icons'
import { AnimatePresence, motion } from 'framer-motion'
import ReactMarkdown from 'react-markdown'
import remarkGfm from 'remark-gfm'
import SourceCard from './SourceCard.jsx'

const { Text } = Typography

const PRESET_QUESTIONS = [
  '滑精很频繁怎么办',
  '一到周末就忍不住破戒',
  '断念口诀具体怎么用',
  '社恐能恢复吗',
  '戒了很久突然破了，很绝望',
  '脱发还能长回来吗',
]

/** 答案 Markdown 渲染：[n] 引用标注转为可点击角标，平滑滚动到对应引用卡片 */
function AnswerMarkdown({ text, citationCount }) {
  const md = useMemo(() => {
    if (!citationCount) return text
    const inRange = (n) => Number(n) >= 1 && Number(n) <= citationCount
    // 角标统一放句末标点之后：“……频繁[2]。” → “……频繁。[2]”
    const moved = text.replace(/((?:\[\d{1,2}\])+)([。．！？；…]+)/g, (all, marks, punct) =>
      [...marks.matchAll(/\d+/g)].every((m) => inRange(m[0])) ? `${punct}${marks}` : all)
    return moved.replace(/\[(\d{1,2})\]/g, (all, n) =>
      inRange(n) ? `[${n}](#cite-${n})` : all)
  }, [text, citationCount])

  return (
    <div className="answer-md">
      <ReactMarkdown
        remarkPlugins={[remarkGfm]}
        components={{
          a: ({ href, children }) => {
            const citeMatch = typeof href === 'string' ? href.match(/^#cite-(\d+)$/) : null
            if (!citeMatch) return <a href={href} target="_blank" rel="noreferrer">{children}</a>
            const jump = (e) => {
              e.preventDefault()
              // 直接驱动滚动容器（scrollIntoView 在 .main-scroll 内会静默失效）
              const el = document.getElementById(`cite-${citeMatch[1]}`)
              const sc = el?.closest('.main-scroll')
              if (el && sc) {
                const r = el.getBoundingClientRect()
                const sr = sc.getBoundingClientRect()
                sc.scrollTo({ top: sc.scrollTop + r.top - sr.top - 80, behavior: 'smooth' })
              }
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
                    ? <AnswerMarkdown text={m.content} citationCount={m.citations?.length || 0} />
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
    </div>
  )
}
