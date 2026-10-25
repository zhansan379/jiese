import { useCallback, useEffect, useRef, useState } from 'react'
import { Select, App as AntdApp } from 'antd'
import { MenuOutlined, PlusOutlined } from '@ant-design/icons'
import Sidebar from './components/Sidebar.jsx'
import MessagesView from './components/MessagesView.jsx'
import InputBar from './components/InputBar.jsx'
import ShelfView from './components/ShelfView.jsx'
import ReaderView from './components/ReaderView.jsx'
import AdminView from './components/AdminView.jsx'
import AuthModal from './components/AuthModal.jsx'
import ShareModal from './components/ShareModal.jsx'
import { api, getToken, setToken } from './api.js'

const STORAGE_KEY = 'jiese_sessions_v1'

const newSession = () => ({
  id: `s_${Date.now()}_${Math.random().toString(36).slice(2, 8)}`,
  title: '新对话',
  createdAt: Date.now(),
  messages: [],
})

function loadSessions() {
  try {
    const raw = JSON.parse(localStorage.getItem(STORAGE_KEY) || '[]')
    if (Array.isArray(raw) && raw.length) return raw
  } catch { /* ignore */ }
  return [newSession()]
}

/** 登录时合并本地与服务端会话：按 id 去重，保留消息更多的一侧 */
function mergeSessions(local, server) {
  const byId = new Map(server.map((s) => [s.id, s]))
  for (const s of local) {
    const ex = byId.get(s.id)
    if (!ex || (s.messages?.length || 0) > (ex.messages?.length || 0)) byId.set(s.id, s)
  }
  return [...byId.values()].sort((a, b) => (b.createdAt || 0) - (a.createdAt || 0))
}

export default function App() {
  const { message: toast } = AntdApp.useApp()
  const [sessions, setSessions] = useState(loadSessions)
  const [currentId, setCurrentId] = useState(() => sessions[0].id)
  // view: {type:'chat'} | {type:'shelf'} | {type:'reader', docId, focusParaId, highlightIds, returnToChat}
  const [view, setView] = useState({ type: 'chat' })
  const [loading, setLoading] = useState(false)
  const [health, setHealth] = useState(null)
  // 移动端抽屉侧栏开关（桌面端不生效）
  const [drawerOpen, setDrawerOpen] = useState(false)
  // 检索范围：null=全部书籍 | 'jwly' | 'jzl'
  const [bookFilter, setBookFilter] = useState(null)
  // 账号：null = 游客（云端按设备标识同步）；user = 已登录（按账号同步，跨设备）
  const [user, setUser] = useState(null)
  const [authOpen, setAuthOpen] = useState(false)
  // 分享弹窗：open + 预选中的会话 id
  const [share, setShare] = useState({ open: false, sessionId: null })

  const savedSnapshots = useRef(new Map())   // sessionId -> 已同步到服务端的 JSON 快照

  const current = sessions.find((s) => s.id === currentId) || sessions[0]

  useEffect(() => { api.health().then(setHealth).catch(() => {}) }, [])

  // 启动时：有 token 恢复登录态；无论游客/登录都拉取云端会话与本机合并（游客按设备标识）
  useEffect(() => {
    let me = null
    ;(async () => {
      if (getToken()) {
        try {
          me = await api.me()
        } catch {
          setToken('')   // token 失效，回落游客模式
        }
      }
      const server = await api.loadSessions().catch(() => [])
      setSessions((local) => {
        const merged = mergeSessions(local, server)
        return merged.length ? merged : [newSession()]
      })
      if (me) setUser(me)
    })()
  }, [])

  // 本地始终镜像一份（浏览器离线兜底）
  useEffect(() => {
    try { localStorage.setItem(STORAGE_KEY, JSON.stringify(sessions)) } catch { /* ignore */ }
  }, [sessions])

  // 会话防抖增量同步（登录按账号、游客按设备）：只上传内容有变化的会话
  useEffect(() => {
    const timer = setTimeout(() => {
      for (const s of sessions) {
        const snap = JSON.stringify(s)
        if (savedSnapshots.current.get(s.id) === snap) continue
        savedSnapshots.current.set(s.id, snap)
        api.saveSession(s).catch(() => { savedSnapshots.current.delete(s.id) })
      }
    }, 600)
    return () => clearTimeout(timer)
  }, [sessions])

  // 打开会话时拉取该会话的点赞/点踩状态（游客按设备标识）
  useEffect(() => {
    if (!currentId) return
    let alive = true
    api.getFeedback(currentId).then(({ ratings }) => {
      if (!alive || !Object.keys(ratings).length) return
      setSessions((ss) => ss.map((s) => s.id !== currentId ? s : ({
        ...s,
        messages: s.messages.map((m, i) =>
          m.role === 'assistant' && ratings[i] !== undefined ? { ...m, rating: ratings[i] } : m),
      }))
    )}).catch(() => {})
    return () => { alive = false }
  }, [currentId, user])

  const patchSession = (id, patcher) =>
    setSessions((ss) => ss.map((s) => (s.id === id ? patcher(s) : s)))

  const newChat = () => {
    const empty = sessions.find((s) => s.messages.length === 0)
    if (empty) {
      setCurrentId(empty.id)
    } else {
      const s = newSession()
      setSessions((ss) => [s, ...ss])
      setCurrentId(s.id)
    }
    setView({ type: 'chat' })
    setDrawerOpen(false)
  }

  const openSession = (id) => {
    setCurrentId(id)
    setView({ type: 'chat' })
    setDrawerOpen(false)
  }

  const openShelf = () => {
    setView({ type: 'shelf' })
    setDrawerOpen(false)
  }

  const deleteSession = (id) => {
    api.deleteSession(id).catch(() => {})   // 游客/登录都同步删除云端记录
    setSessions((ss) => {
      const rest = ss.filter((s) => s.id !== id)
      if (!rest.length) {
        const s = newSession()
        setCurrentId(s.id)
        return [s]
      }
      if (id === currentId) setCurrentId(rest[0].id)
      return rest
    })
  }

  /** 登录成功：合并本地/云端会话并上传本地新增部分 */
  const onAuthed = async ({ token, user: u }) => {
    setToken(token)
    const server = await api.loadSessions().catch(() => [])
    setSessions((local) => {
      const merged = mergeSessions(local, server)
      const result = merged.length ? merged : [newSession()]
      // 本地比云端新的会话立即上传（同步 effect 只比较内容快照，这里预热快照避免重复上传）
      for (const s of result) {
        const snap = JSON.stringify(s)
        savedSnapshots.current.set(s.id, snap)
        const sv = server.find((x) => x.id === s.id)
        if (!sv || (s.messages?.length || 0) > (sv.messages?.length || 0)) {
          api.saveSession(s).catch(() => { savedSnapshots.current.delete(s.id) })
        }
      }
      return result
    })
    setUser(u)
    setAuthOpen(false)
    toast.success(`欢迎，${u.username}`)
  }

  const logout = async () => {
    try { await api.logout() } catch { /* token 可能已失效 */ }
    setToken('')
    setUser(null)
    toast.success('已退出登录，对话保留在本机')
  }

  /** 点赞/点踩：rating=1|-1，再点一次取消（0） */
  const rateMessage = (sid, idx, rating) => {
    const s = sessions.find((x) => x.id === sid)
    const m = s?.messages[idx]
    if (!m) return
    const next = m.rating === rating ? 0 : rating
    patchSession(sid, (ss) => ({
      ...ss,
      messages: ss.messages.map((mm, i) => (i === idx ? { ...mm, rating: next || undefined } : mm)),
    }))
    api.feedback(sid, idx, next).catch(() => {})
  }

  const send = useCallback(async (text, opts = {}) => {
    const options = { ...opts, bookId: bookFilter }
    const content = text.trim()
    if (!content || loading) return
    const session = sessions.find((s) => s.id === currentId)
    const history = (session?.messages || []).slice(-8).map((m) => ({ role: m.role, content: m.content }))
    patchSession(currentId, (s) => ({
      ...s,
      title: s.messages.length === 0 ? content.slice(0, 18) : s.title,
      messages: [...s.messages, { role: 'user', content }],
    }))
    setLoading(true)
    // 流式占位消息：thinking/answer 边收边更新
    patchSession(currentId, (s) => ({
      ...s,
      messages: [...s.messages, {
        role: 'assistant',
        content: '',
        thinking: '',
        citations: [],
        query: content,
        streaming: true,
        thinkingStart: options.deepThinking ? Date.now() : null,
        thinkingSeconds: null,
      }],
    }))
    try {
      let firstAnswerAt = null
      let thinkingStart = null
      await api.chatStream(content, history, options, (event, data) => {
        patchSession(currentId, (s) => {
          const ms = s.messages.slice()
          const m = { ...ms[ms.length - 1] }
          if (event === 'meta') {
            m.query = data.query
            m.mode = data.mode
          } else if (event === 'citations') {
            m.citations = data
          } else if (event === 'thinking') {
            if (thinkingStart === null) thinkingStart = Date.now()
            m.thinking += data.delta
          } else if (event === 'answer') {
            if (firstAnswerAt === null) {
              firstAnswerAt = Date.now()
              if (thinkingStart !== null) {
                m.thinkingSeconds = Math.max(1, Math.round((firstAnswerAt - thinkingStart) / 1000))
              }
            }
            m.content += data.delta
          } else if (event === 'done') {
            m.streaming = false
            if (thinkingStart !== null && m.thinkingSeconds === null) {
              m.thinkingSeconds = Math.max(1, Math.round((Date.now() - thinkingStart) / 1000))
            }
          }
          ms[ms.length - 1] = m
          return { ...s, messages: ms }
        })
      })
    } catch (e) {
      toast.error(`问答失败：${e.message}`)
      patchSession(currentId, (s) => {
        const ms = s.messages.slice()
        const last = ms[ms.length - 1]
        if (last?.role === 'assistant' && !last.content) {
          return { ...s, messages: ms.slice(0, -1) }  // 空回复撤回，用户消息保留
        }
        ms[ms.length - 1] = { ...last, streaming: false }
        return { ...s, messages: ms }
      })
    } finally {
      setLoading(false)
    }
  }, [sessions, currentId, loading])

  const openSource = (c) => {
    if (!c?.doc_id) return
    setView({
      type: 'reader',
      docId: c.doc_id,
      focusParaId: c.para_ids?.[0] || null,
      highlightIds: c.para_ids || [],
      returnToChat: true,
    })
  }

  const openBook = async (bookId) => {
    const toc = await api.toc(bookId)
    const first = toc.find((e) => e.para_id) || toc[0]
    setView({ type: 'reader', docId: first.doc_id, focusParaId: null, highlightIds: [], returnToChat: false })
  }

  return (
    <div className="app-shell">
      <Sidebar
        sessions={sessions}
        currentId={currentId}
        view={view}
        health={health}
        user={user}
        mobileOpen={drawerOpen}
        onClose={() => setDrawerOpen(false)}
        onNewChat={newChat}
        onOpenSession={openSession}
        onDeleteSession={deleteSession}
        onOpenShelf={openShelf}
        onOpenAdmin={() => { setView({ type: 'admin' }); setDrawerOpen(false) }}
        onOpenAuth={() => setAuthOpen(true)}
        onLogout={logout}
      />

      <main className="main-area">
        {view.type === 'chat' && (
          <>
            <header className="main-header">
              <button
                type="button" className="icon-btn" aria-label="打开菜单"
                onClick={() => setDrawerOpen(true)}
              >
                <MenuOutlined />
              </button>
              <div className="main-title">{current?.title || '新对话'}</div>
              <div className="main-header-side">
                <Select
                  size="small"
                  variant="borderless"
                  value={bookFilter}
                  onChange={setBookFilter}
                  className="book-filter"
                  options={[
                    { value: null, label: '全部书籍' },
                    { value: 'jwly', label: '戒为良药' },
                    { value: 'jzl', label: '戒者录' },
                  ]}
                />
                <button
                  type="button" className="icon-btn" aria-label="开启新对话"
                  onClick={newChat}
                >
                  <PlusOutlined />
                </button>
              </div>
            </header>
            <div className="main-scroll">
              <MessagesView
                session={current}
                loading={loading}
                onOpenSource={openSource}
                onPreset={(q) => send(q)}
                onFeedback={rateMessage}
                onShare={(sid) => setShare({ open: true, sessionId: sid })}
              />
            </div>
            <InputBar sending={loading} onSend={send} />
          </>
        )}

        {view.type === 'shelf' && (
          <>
            <header className="main-header">
              <button
                type="button" className="icon-btn" aria-label="打开菜单"
                onClick={() => setDrawerOpen(true)}
              >
                <MenuOutlined />
              </button>
              <div className="main-title">书架</div>
              <div className="main-header-side" />
            </header>
            <div className="main-scroll">
              <ShelfView onOpenBook={openBook} />
            </div>
          </>
        )}

        {view.type === 'reader' && (
          <ReaderView
            docId={view.docId}
            focusParaId={view.focusParaId}
            highlightIds={view.highlightIds}
            returnToChat={view.returnToChat}
            onBack={() => setView(view.returnToChat ? { type: 'chat' } : { type: 'shelf' })}
            onNavigate={(docId, focusParaId) =>
              setView((v) => ({ ...v, docId, focusParaId, highlightIds: [] }))}
          />
        )}
        {view.type === 'admin' && (
          <AdminView me={user} onBack={() => setView({ type: 'chat' })} />
        )}
      </main>

      <AuthModal open={authOpen} onClose={() => setAuthOpen(false)} onAuthed={onAuthed} />
      <ShareModal
        open={share.open}
        sessions={sessions}
        defaultSessionId={share.sessionId}
        onClose={() => setShare({ open: false, sessionId: null })}
      />
    </div>
  )
}
