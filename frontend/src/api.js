import axios from 'axios'

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

// 统一 HTTP 实例：请求自动附带登录态，响应直接解包 data，错误统一转成 Error(detail)
const http = axios.create()

http.interceptors.request.use((config) => {
  const t = getToken()
  if (t) config.headers.Authorization = `Bearer ${t}`
  return config
})

http.interceptors.response.use(
  (resp) => resp.data,
  (err) => {
    const status = err.response?.status
    const detail = err.response?.data?.detail
    return Promise.reject(new Error(detail || `请求失败${status ? ` ${status}` : ''}`))
  },
)

async function get(path) {
  return http.get(path)
}

function put(path, body) {
  return post(path, body, 'PUT')
}

function post(path, body, method = 'POST') {
  return http.request({ method, url: path, data: body })
}

export const api = {
  get,

  // ---- 管理后台 ----
  adminIntentTreeEnabled: () => get('/api/admin/intent-tree-switch'),
  adminSetIntentTreeEnabled: (enabled) => put('/api/admin/intent-tree-switch', { enabled }),
  adminIntentNodes: () => get('/api/admin/intent-nodes'),
  adminAddIntentNode: (node) => post('/api/admin/intent-nodes', node),
  adminUpdateIntentNode: (code, patch) => post(`/api/admin/intent-nodes/${code}`, patch, 'PATCH'),
  adminDelIntentNode: (code) => post(`/api/admin/intent-nodes/${code}`, {}, 'DELETE'),
  adminMoveIntentNode: (code, parent_code, after_code) =>
    post(`/api/admin/intent-nodes/${code}/move`, { parent_code, after_code }),
  adminUsers: () => get('/api/admin/users'),
  adminDeleteUser: (id) => post(`/api/admin/users/${id}`, {}, 'DELETE'),

  // ---- 查询词映射（关键词映射，参考 ragent QueryTermMapping） ----
  adminTermMappings: (params = {}) =>
    get(`/api/admin/term-mappings?page=${params.page || 1}&size=${params.size || 20}`
      + `&keyword=${encodeURIComponent(params.keyword || '')}`),
  adminTermMappingPreview: (text) =>
    get(`/api/admin/term-mappings/preview?text=${encodeURIComponent(text)}`),
  adminAddTermMapping: (m) => post('/api/admin/term-mappings', m),
  adminUpdateTermMapping: (id, patch) => put(`/api/admin/term-mappings/${id}`, patch),
  adminDelTermMapping: (id) => post(`/api/admin/term-mappings/${id}`, {}, 'DELETE'),

  // ---- 链路追踪（参考 ragent RagTraceController） ----
  adminTraceRuns: (params = {}) =>
    get(`/api/admin/traces/runs?page=${params.page || 1}&size=${params.size || 20}`
      + `&status=${params.status || ''}&keyword=${encodeURIComponent(params.keyword || '')}`
      + `&source=${params.source || ''}&user=${encodeURIComponent(params.user || '')}`
      + `&hide_future=${params.hide_future === false ? 'false' : 'true'}`),
  adminTraceDetail: (traceId) => get(`/api/admin/traces/runs/${traceId}`),

  // ---- 术语与关系（实体标注 + 受限关系） ----
  adminTerms: (params = {}) =>
    get(`/api/admin/terms?page=${params.page || 1}&size=${params.size || 20}`
      + `&keyword=${encodeURIComponent(params.keyword || '')}&type=${params.type || ''}`),
  adminAddTerm: (t) => post('/api/admin/terms', t),
  adminUpdateTerm: (term, patch) => put(`/api/admin/terms/${encodeURIComponent(term)}`, patch),
  adminDelTerm: (term) => post(`/api/admin/terms/${encodeURIComponent(term)}`, {}, 'DELETE'),
  adminTermEdges: (params = {}) =>
    get(`/api/admin/term-edges?page=${params.page || 1}&size=${params.size || 20}`
      + `&rel=${params.rel || ''}&keyword=${encodeURIComponent(params.keyword || '')}`),
  adminTermEdgeEvidence: (id) => get(`/api/admin/term-edges/${id}/evidence`),
  adminDelTermEdge: (id) => post(`/api/admin/term-edges/${id}`, {}, 'DELETE'),

  // ---- 经验共识（Claim 共识层） ----
  adminClaims: (params = {}) =>
    get(`/api/admin/claims?page=${params.page || 1}&size=${params.size || 20}`
      + `&keyword=${encodeURIComponent(params.keyword || '')}&min_support=${params.minSupport || 0}`
      + `&topic=${encodeURIComponent(params.topic || '')}`),
  adminClaimTopics: () => get('/api/admin/claims/topics'),
  adminUpdateClaim: (id, patch) => put(`/api/admin/claims/${id}`, patch),
  adminClaimEvidence: (id) => get(`/api/admin/claims/${id}/evidence`),

  // ---- 知识探索（公开只读：知识图谱 / 经验共识） ----
  exploreOverview: () => get('/api/explore/overview'),
  exploreTerms: (params = {}) =>
    get(`/api/explore/terms?page=${params.page || 1}&size=${params.size || 60}`
      + `&keyword=${encodeURIComponent(params.keyword || '')}&type=${encodeURIComponent(params.type || '')}`),
  exploreEdges: (params = {}) =>
    get(`/api/explore/edges?page=${params.page || 1}&size=${params.size || 20}`
      + `&rel=${encodeURIComponent(params.rel || '')}`
      + `&keyword=${encodeURIComponent(params.keyword || '')}`
      + `&min_support=${params.minSupport ?? 2}`),
  exploreEdgeEvidence: (id) => get(`/api/explore/edges/${id}/evidence`),
  exploreGraph: (params = {}) =>
    get(`/api/explore/graph?rel=${encodeURIComponent(params.rel || '')}`
      + `&keyword=${encodeURIComponent(params.keyword || '')}&limit=${params.limit || 60}`),
  exploreClaims: (params = {}) =>
    get(`/api/explore/claims?page=${params.page || 1}&size=${params.size || 10}`
      + `&keyword=${encodeURIComponent(params.keyword || '')}`
      + `&min_support=${params.minSupport ?? 3}&topic=${encodeURIComponent(params.topic || '')}`),
  exploreClaimTopics: () => get('/api/explore/claims/topics'),
  exploreClaimEvidence: (id) => get(`/api/explore/claims/${id}/evidence`),

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

  /** SSE 流式问答：onEvent(event, data) 依次收到 meta/citations/thinking/answer/done
   *  （浏览器端 axios 走 XHR 无法增量读取响应体，流式接口保留 fetch） */
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

  // ---- 经验共识证据（答案内「共识」胶囊点击查看） ----
  claimEvidence: (id) => get(`/api/claims/${id}/evidence`),

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
  // 服务端返回 snake_case（created_at），前端会话对象用 createdAt，这里统一
  loadSessions: async () => {
    const list = await get(`/api/sessions${getToken() ? '' : `?device_id=${getDeviceId()}`}`)
    return (Array.isArray(list) ? list : []).map((s) => ({
      ...s,
      createdAt: s.createdAt ?? s.created_at ?? Date.now(),
    }))
  },
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

  // ---------------- 分享链接（会话快照，公开只读，无需登录查看） ----------------
  createShareLink: (sessions, title) =>
    post('/api/shares', {
      title: title || null,
      device_id: getToken() ? null : getDeviceId(),
      sessions: sessions.map((s) => ({
        title: s.title,
        created_at: s.createdAt || null,
        messages: (s.messages || [])
          .filter((m) => (m.role === 'user' && m.content) || (m.role === 'assistant' && m.content))
          .map((m) => ({
            role: m.role,
            content: m.content,
            query: m.query || null,
            citations: m.citations || [],
          })),
      })),
    }),
  getShare: (id) => get(`/api/shares/${encodeURIComponent(id)}`),
  deleteShare: (id) =>
    post(`/api/shares/${id}${getToken() ? '' : `?device_id=${getDeviceId()}`}`, {}, 'DELETE'),

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
