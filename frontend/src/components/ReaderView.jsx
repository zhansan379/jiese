import { useEffect, useMemo, useRef, useState } from 'react'
import { useNavigate, useParams, useSearchParams } from 'react-router-dom'
import {
  Alert, Button, Drawer, Empty, Space, Spin, Tooltip, Typography,
} from 'antd'
import {
  ArrowLeftOutlined, LeftOutlined, MenuOutlined, RightOutlined,
} from '@ant-design/icons'
import { api } from '../api.js'

const { Title, Text } = Typography

const isSoftHeading = (t) =>
  t.length <= 20 && !/[。！？；，：…」』]$/.test(t) && !/^(答|分析|案例|（?\d|作者|【)/.test(t)

/** 目录列表（侧栏抽屉共用） */
function TocList({ toc, currentDocId, onNavigate }) {
  return (
    <div className="toc-list">
      {toc.map((e) => (
        <a
          key={e.id}
          className={`toc-item${e.doc_id === currentDocId ? ' active' : ''}`}
          style={{ paddingLeft: 8 + (e.level - 1) * 14 }}
          onClick={() => onNavigate(e.doc_id, e.para_id)}
          title={e.title}
        >
          {e.title}
        </a>
      ))}
    </div>
  )
}

/**
 * 阅读器（主内容区形态）：固定工具栏 + 独立滚动正文，路由 /reader/:docId。
 * search params：para=定位段落、hl=高亮段落（逗号分隔）、
 * from=chat|shelf|admin|explore（返回按钮去向）、back=知识探索/管理端来源路径
 * （from=admin|explore 时回到对应页签）。
 */
export default function ReaderView() {
  const { docId } = useParams()
  const [searchParams] = useSearchParams()
  const routerNavigate = useNavigate()
  const fromParam = searchParams.get('from')
  const from = ['chat', 'admin', 'explore'].includes(fromParam) ? fromParam : 'shelf'
  // 证据跳转的返回目标：仅接受应用内绝对路径（防外部/协议跳转）
  const backParam = searchParams.get('back') || ''
  const backTarget = backParam.startsWith('/') && !backParam.startsWith('//')
    ? backParam
    : from === 'explore' ? '/explore/claims' : '/admin/tree'
  const focusParaId = searchParams.get('para')
  const highlightIds = useMemo(
    () => (searchParams.get('hl') || '').split(',').filter(Boolean),
    [searchParams],
  )
  const [doc, setDoc] = useState(null)
  const [book, setBook] = useState(null)
  const [toc, setToc] = useState([])
  const [tocOpen, setTocOpen] = useState(false)
  const [error, setError] = useState(null)
  const scrollRef = useRef(null)

  useEffect(() => {
    let alive = true
    setError(null)
    setDoc(null)
    api.doc(docId)
      .then(async (d) => {
        if (!alive) return
        setDoc(d)
        const [b, t] = await Promise.all([api.book(d.book_id), api.toc(d.book_id)])
        if (!alive) return
        setBook(b)
        setToc(t)
      })
      .catch((e) => alive && setError(e.message))
    return () => { alive = false }
  }, [docId])

  // 跳转定位：直接驱动滚动容器并校验结果，避免渲染未稳定时 scrollIntoView 静默失效
  useEffect(() => {
    if (!doc || !focusParaId) return undefined
    let tries = 0
    let timer = null
    const scrollToFocus = () => {
      const sc = scrollRef.current
      const el = document.getElementById(focusParaId)
      if (!sc || !el) return
      const r = el.getBoundingClientRect()
      const sr = sc.getBoundingClientRect()
      const target = Math.max(0, sc.scrollTop + r.top - sr.top - (sc.clientHeight - r.height) / 2)
      sc.scrollTo({ top: target })
      if (Math.abs(sc.scrollTop - target) > 40 && ++tries < 6) {
        timer = setTimeout(scrollToFocus, 200)
      }
    }
    timer = setTimeout(scrollToFocus, 250)
    return () => clearTimeout(timer)
  }, [doc, focusParaId])

  const highlight = useMemo(() => new Set(highlightIds), [highlightIds])
  // 章节内跳转：push 新路由并保留 from，清掉上一章的高亮/定位
  const goChapter = (nextDocId, nextFocus) => {
    if (!nextDocId || nextDocId === docId) return
    const params = new URLSearchParams(searchParams)
    if (nextFocus) params.set('para', nextFocus)
    else params.delete('para')
    params.delete('hl')
    routerNavigate(`/reader/${nextDocId}?${params}`)
  }
  const onBack = () => routerNavigate(
    from === 'chat' ? '/' : from === 'shelf' ? '/shelf' : backTarget,
  )
  const backLabel = from === 'chat' ? '返回对话'
    : from === 'admin' ? '返回管理后台'
      : from === 'explore' ? '返回知识探索' : '返回书架'

  return (
    <div className="reader-embed">
      <header className="main-header reader-header">
        <Button icon={<ArrowLeftOutlined />} onClick={onBack}>
          <span className="btn-label">{backLabel}</span>
        </Button>
        <div className="reader-title">
          {book && <Text type="secondary" className="reader-book">{book.title}</Text>}
          <Text strong ellipsis={{ tooltip: doc?.title }} className="reader-chapter">
            {doc?.title || '加载中…'}
          </Text>
        </div>
        <Space>
          <Button icon={<MenuOutlined />} onClick={() => setTocOpen(true)}>
            <span className="btn-label">目录</span>
          </Button>
          <Tooltip title="上一页">
            <Button icon={<LeftOutlined />} disabled={!doc?.prev_id}
              onClick={() => goChapter(doc.prev_id)} />
          </Tooltip>
          <Tooltip title="下一页">
            <Button icon={<RightOutlined />} disabled={!doc?.next_id}
              onClick={() => goChapter(doc.next_id)} />
          </Tooltip>
        </Space>
      </header>

      <div className="main-scroll" ref={scrollRef}>
        <div className="reader-body">
          {error && <Alert type="error" showIcon message={error} />}
          {!doc && !error && <div className="page-center"><Spin /></div>}
          {doc && doc.paragraphs.length === 0 && <Empty description="本页无内容" />}
          {doc?.paragraphs.map((p) => {
            if (p.kind === 'heading') {
              return <Title key={p.id} id={p.id} level={4} className="reader-heading">{p.text}</Title>
            }
            const soft = isSoftHeading(p.text)
            const cls = ['reader-para', soft && 'reader-soft-head', highlight.has(p.id) && 'reader-highlight']
              .filter(Boolean).join(' ')
            return <p key={p.id} id={p.id} className={cls}>{p.text}</p>
          })}
          {doc && (
            <div className="reader-footer">
              <Button disabled={!doc.prev_id} onClick={() => goChapter(doc.prev_id)}>
                <LeftOutlined /> 上一页
              </Button>
              <Button disabled={!doc.next_id} onClick={() => goChapter(doc.next_id)}>
                下一页 <RightOutlined />
              </Button>
            </div>
          )}
        </div>
      </div>

      <Drawer
        title={`${book?.title || ''} · 目录`}
        placement="left"
        width="min(78vw, 340px)"
        open={tocOpen}
        onClose={() => setTocOpen(false)}
      >
        <TocList
          toc={toc}
          currentDocId={doc?.id}
          onNavigate={(d, p) => { setTocOpen(false); goChapter(d, p) }}
        />
      </Drawer>
    </div>
  )
}
