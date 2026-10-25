const tocCache = new Map()
const bookCache = new Map()

const TOKEN_KEY = 'jiese_token'
const DEVICE_KEY = 'jiese_device'

export const getToken = () => localStorage.getItem(TOKEN_KEY) || ''
export const setToken = (t) => t ? localStorage.setItem(TOKEN_KEY, t) : localStorage.removeItem(TOKEN_KEY)

/** 游客设备标识：点赞点踩等匿名记录的主体 */
export function getDeviceId() {
  let id = localStorage.getItem(DEVICE_KEY)
  if (!id) {
    id = `d_${Date.now()}_${Math.random().toString(36).slice(2, 10)}`
    localStorage.setItem(DEVICE_KEY, id)
  }
  return id
}

function authHeaders() {
  const t = getToken()
  return t ? { Authorization: `Bearer ${t}` } : {}
}

async function get(path) {
  const resp = await fetch(path, { headers: authHeaders() })
  if (!resp.ok) {
    let detail = ''
    try { detail = (await resp.json()).detail || '' } catch { /* ignore */ }
    throw new Error(detail || `请求失败 ${resp.status}`)
  }
  return resp.json()
}

async function post(path, body, method = 'POST') {
  const resp = await fetch(path, {
    method,
    headers: { 'Content-Type': 'application/json', ...authHeaders() },
    body: JSON.stringify(body),
  })
  if (!resp.ok) {
    let detail = ''
    try { detail = (await resp.json()).detail || '' } catch { /* ignore */ }
    throw new Error(detail || `请求失败 ${resp.status}`)
  }
  return resp.json()
}

export const api = {
  get,

  // ---- 管理后台 ----
  adminIntentNodes: () => get('/api/admin/intent-nodes'),
  adminAddIntentNode: (node) => post('/api/admin/intent-nodes', node),
  adminUpdateIntentNode: (code, patch) => post(`/api/admin/intent-nodes/${code}`, patch, 'PATCH'),
  adminDelIntentNode: (code) => post(`/api/admin/intent-nodes/${code}`, {}, 'DELETE'),
  adminUsers: () => get('/api/admin/users'),
  adminDeleteUser: (id) => post(`/api/admin/users/${id}`, {}, 'DELETE'),

  health: () => get('/api/health'),
  books: () => get('/api/books'),
  doc: (docId) => get(`/api/docs/${docId}`),

  async toc(bookId) {
    if (!tocCache.has(bookId)) {
      tocCache.set(bookId, await get(`/api/books/${bookId}/toc`))
    }
    return tocCache.get(bookId)
  },
  async book(bookId) {
    if (!bookCache.size) {
      for (const b of await get('/api/books')) bookCache.set(b.id, b)
    }
    return bookCache.get(bookId)
  },

  /** SSE 流式问答：onEvent(event, data) 依次收到 meta/citations/thinking/answer/done */
  async chatStream(message, history, options, onEvent) {
    const resp = await fetch('/api/chat', {
      method: 'POST',
      headers: { 'Content-Type': 'application/json' },
      body: JSON.stringify({
        message,
        history,
        deep_thinking: Boolean(options?.deepThinking),
        smart_search: Boolean(options?.smartSearch),
        book_id: options?.bookId || null,
        session_id: options?.sessionId || null,   // 会话记忆：服务端据此加载持久化摘要
      }),
    })
    if (!resp.ok) throw new Error(`请求失败 ${resp.status}`)

    const reader = resp.body.getReader()
    const decoder = new TextDecoder()
    let buf = ''
    for (;;) {
      const { done, value } = await reader.read()
      if (done) break
      buf += decoder.decode(value, { stream: true })
      let idx
      while ((idx = buf.indexOf('\n\n')) >= 0) {
        const block = buf.slice(0, idx)
        buf = buf.slice(idx + 2)
        let event = 'message'
        const dataLines = []
        for (const line of block.split('\n')) {
          if (line.startsWith('event: ')) event = line.slice(7).trim()
          else if (line.startsWith('data: ')) dataLines.push(line.slice(6))
        }
        if (dataLines.length) {
          try { onEvent(event, JSON.parse(dataLines.join('\n'))) } catch { /* 跳过坏块 */ }
        }
      }
    }
  },
  search: (query, k) => post('/api/search', { query, k }),

  // ---------------- 账号 ----------------
  register: (username, password, email) =>
    post('/api/auth/register', {
      username, password,
      email: email || null,
      device_id: getDeviceId(),   // 注册时认领本机游客会话
    }),
  login: (username, password) =>
    post('/api/auth/login', { username, password, device_id: getDeviceId() }),
  logout: () => post('/api/auth/logout', {}),
  me: () => get('/api/auth/me'),

  // ---------------- 会话云同步（登录按账号、游客按设备） ----------------
  loadSessions: () =>
    get(`/api/sessions${getToken() ? '' : `?device_id=${getDeviceId()}`}`),
  saveSession: (s) => post(
    `/api/sessions/${s.id}${getToken() ? '' : `?device_id=${getDeviceId()}`}`,
    {
      id: s.id,
      title: s.title,
      created_at: s.createdAt,
      messages: s.messages.map((m) => ({
        role: m.role,
        content: m.content,
        thinking: m.thinking || null,
        query: m.query || null,
        citations: m.citations || [],
        thinking_seconds: m.thinkingSeconds ?? null,
      })),
    },
    'PUT',
  ),
  deleteSession: (id) =>
    post(`/api/sessions/${id}${getToken() ? '' : `?device_id=${getDeviceId()}`}`, {}, 'DELETE'),

  // ---------------- 点赞点踩（游客也可用） ----------------
  feedback: (sessionId, msgIndex, rating) =>
    post('/api/feedback', {
      session_id: sessionId,
      msg_index: msgIndex,
      rating,
      device_id: getToken() ? null : getDeviceId(),
    }),
  getFeedback: (sessionId) =>
    get(`/api/feedback?session_id=${encodeURIComponent(sessionId)}${getToken() ? '' : `&device_id=${getDeviceId()}`}`),
}
