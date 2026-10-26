import { useEffect, useMemo, useState } from 'react'
import { Spin, Typography } from 'antd'
import { CheckOutlined, CopyOutlined, LinkOutlined } from '@ant-design/icons'
import { Link, useNavigate, useParams } from 'react-router-dom'
import ReactMarkdown from 'react-markdown'
import remarkGfm from 'remark-gfm'
import { api } from '../api.js'
import { copyText } from '../clipboard.js'
import SourceCard from './SourceCard.jsx'

const { Text } = Typography

const fmtDate = (v) => {
  const d = new Date(v || Date.now())
  if (Number.isNaN(d.getTime())) return ''
  return d.toLocaleDateString('zh-CN', { year: 'numeric', month: 'long', day: 'numeric' })
}

/** 答案 Markdown：[n] 引用标注转可点击角标，滚动到本页对应引用条目（与聊天页一致） */
function AnswerMarkdown({ text, citationCount, idPrefix }) {
  const md = useMemo(() => {
    if (!citationCount) return text
    const inRange = (n) => Number(n) >= 1 && Number(n) <= citationCount
    const moved = text.replace(/((?:\[\d{1,2}\])+)([。．！？；…]+)/g, (all, marks, punct) =>
      [...marks.matchAll(/\d+/g)].every((m) => inRange(m[0])) ? `${punct}${marks}` : all)
    return moved.replace(/\[(\d{1,2})\]/g, (all, n) => (inRange(n) ? `[${n}](#cite-${n})` : all))
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
              document.getElementById(`${idPrefix}-cite-${citeMatch[1]}`)
                ?.scrollIntoView({ behavior: 'smooth', block: 'center' })
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

/** 公开分享页：只读展示会话快照（无需登录），底部引导回主站提问 */
export default function SharePage() {
  const { shareId } = useParams()
  const navigate = useNavigate()
  const [data, setData] = useState(null)
  const [error, setError] = useState('')
  const [copied, setCopied] = useState(false)

  useEffect(() => {
    let alive = true
    setData(null)
    setError('')
    api.getShare(shareId)
      .then((res) => { if (alive) setData(res) })
      .catch((e) => { if (alive) setError(e.message || '链接已失效') })
    return () => { alive = false }
  }, [shareId])

  const sessions = data?.sessions || []

  // 标题随分享内容变化，离开时还原站点默认标题
  useEffect(() => {
    const fallback = document.title
    return () => { document.title = fallback }
  }, [])
  useEffect(() => {
    if (data?.title) document.title = `${data.title} · 戒色知识库`
  }, [data])

  const copyLink = async () => {
    const ok = await copyText(window.location.href)
    if (ok) {
      setCopied(true)
      setTimeout(() => setCopied(false), 1800)
    }
  }

  const openSource = (c) => {
    if (!c?.doc_id) return
    const params = new URLSearchParams({ from: 'share' })
    if (c.para_ids?.length) {
      params.set('para', c.para_ids[0])
      params.set('hl', c.para_ids.join(','))
    }
    navigate(`/reader/${c.doc_id}?${params}`)
  }

  return (
    <div className="share-page">
      <header className="share-page-head">
        <Link to="/" className="share-page-brand">
          <img src="/logo-seal.png" alt="" />
          <span>戒色知识库</span>
        </Link>
        <div className="share-page-head-actions">
          <button type="button" className="share-page-ghost" onClick={copyLink}>
            {copied ? <><CheckOutlined /> 已复制</> : <><CopyOutlined /> 复制链接</>}
          </button>
          <Link to="/" className="share-page-cta">我也要提问</Link>
        </div>
      </header>

      <main className="share-page-body">
        {!data && !error && (
          <div className="share-page-status"><Spin /> <span>正在加载分享内容…</span></div>
        )}

        {error && (
          <div className="share-page-status">
            <div className="share-page-status-title">
              <LinkOutlined /> {error}
            </div>
            <div className="share-page-status-sub">链接可能已过期，或被作者撤销。</div>
            <Link to="/" className="share-page-cta">去戒色知识库提问</Link>
          </div>
        )}

        {data && (
          <>
            <div className="share-page-title">{data.title}</div>
            <div className="share-page-meta">
              {fmtDate(data.createdAt)}
              {typeof data.viewCount === 'number' ? ` · ${data.viewCount} 次浏览` : ''}
            </div>

            {sessions.map((s, si) => (
              <section key={si} className="share-page-session">
                {sessions.length > 1 && (
                  <div className="share-page-session-title">{s.title || `对话 ${si + 1}`}</div>
                )}
                {(s.messages || []).map((m, i) => {
                  const idPrefix = `s${si}m${i}`
                  if (m.role === 'user') {
                    return <div key={i} className="msg-row user"><div className="bubble user">{m.content}</div></div>
                  }
                  return (
                    <div key={i} className="msg-row ai">
                      <div className="ai-msg">
                        <AnswerMarkdown
                          text={m.content}
                          citationCount={m.citations?.length || 0}
                          idPrefix={idPrefix}
                        />
                        {m.query && m.query !== s.messages[i - 1]?.content && (
                          <Text type="secondary" className="query-trace">本条查询：{m.query}</Text>
                        )}
                        {m.citations?.length > 0 && (
                          <div className="citations">
                            <Text type="secondary" className="citations-title">
                              参考来源（点击条目跳转原文）
                            </Text>
                            {m.citations.map((c, j) => (
                              <SourceCard
                                key={c.chunk_id || j}
                                index={j + 1}
                                citation={c}
                                onOpenSource={openSource}
                                idPrefix={`${idPrefix}-`}
                              />
                            ))}
                          </div>
                        )}
                      </div>
                    </div>
                  )
                })}
              </section>
            ))}

            <div className="share-page-foot">
              内容由 AI 生成，仅供参考 · 戒色知识库
              <div className="share-page-foot-cta">
                <Link to="/" className="share-page-cta">我也要提问</Link>
              </div>
            </div>
          </>
        )}
      </main>
    </div>
  )
}
