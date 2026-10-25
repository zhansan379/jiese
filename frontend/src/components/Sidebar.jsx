import { useMemo } from 'react'
import { Avatar, Popconfirm } from 'antd'
import {
  BookOutlined, DeleteOutlined, LogoutOutlined, PlusOutlined, UserOutlined,
} from '@ant-design/icons'

const DAY_MS = 24 * 60 * 60 * 1000

const dayStart = (ts) => {
  const d = new Date(ts)
  d.setHours(0, 0, 0, 0)
  return d.getTime()
}

/** 会话按创建时间分组：今天 / 昨天 / 7天内 / 更早（参照 DeepSeek 抽屉的时间分组） */
function groupSessions(sessions) {
  const today = dayStart(Date.now())
  const groups = { 今天: [], 昨天: [], '7天内': [], 更早: [] }
  for (const s of sessions) {
    const days = Math.floor((today - dayStart(s.createdAt || Date.now())) / DAY_MS)
    if (days <= 0) groups.今天.push(s)
    else if (days === 1) groups.昨天.push(s)
    else if (days <= 7) groups['7天内'].push(s)
    else groups.更早.push(s)
  }
  return Object.entries(groups).filter(([, list]) => list.length > 0)
}

/**
 * 左侧导航栏：Logo / 新对话 / 书架 / 会话历史（时间分组）/ 账户区。
 * 桌面端为固定侧栏；移动端（≤768px）由 CSS 转为遮罩 + 左滑抽屉。
 */
export default function Sidebar({
  sessions, currentId, view, health, user, mobileOpen, onClose,
  onNewChat, onOpenSession, onDeleteSession, onOpenShelf, onOpenAuth, onLogout,
}) {
  const shelfActive = view.type === 'shelf' || view.type === 'reader'
  const groups = useMemo(() => groupSessions(sessions), [sessions])

  return (
    <>
      <div
        className={`sidebar-scrim${mobileOpen ? ' show' : ''}`}
        onClick={onClose}
        aria-hidden="true"
      />
      <aside className={`sidebar${mobileOpen ? ' open' : ''}`}>
        <div className="sidebar-logo">
          <img src="/logo.png" alt="戒色知识库" className="logo-img" />
        </div>

        <button className="new-chat-btn" onClick={onNewChat}>
          <PlusOutlined /> 开启新对话
        </button>
        <button className={`side-nav-item${shelfActive ? ' active' : ''}`} onClick={onOpenShelf}>
          <BookOutlined /> 书架阅读
        </button>

        <div className="session-groups">
          {groups.map(([label, list]) => (
            <div key={label} className="session-group">
              <div className="session-label">{label}</div>
              <div className="session-list">
                {list.map((s) => (
                  <div
                    key={s.id}
                    className={`session-item${s.id === currentId && view.type === 'chat' ? ' active' : ''}`}
                    onClick={() => onOpenSession(s.id)}
                  >
                    <span className="session-title" title={s.title}>{s.title}</span>
                    <Popconfirm
                      title="删除该会话？"
                      okText="删除"
                      cancelText="取消"
                      onConfirm={(e) => { e?.stopPropagation?.(); onDeleteSession(s.id) }}
                      onCancel={(e) => e?.stopPropagation?.()}
                    >
                      <DeleteOutlined
                        className="session-del"
                        onClick={(e) => e.stopPropagation()}
                      />
                    </Popconfirm>
                  </div>
                ))}
              </div>
            </div>
          ))}
        </div>

        <div
          className="sidebar-user"
          onClick={() => { if (!user) { onOpenAuth(); onClose(); } }}
          role="button"
          tabIndex={-1}
        >
          {user ? (
            <Avatar className="user-avatar logged" size={34}>
              {user.username.slice(0, 1).toUpperCase()}
            </Avatar>
          ) : (
            <Avatar className="user-avatar guest" size={34} icon={<UserOutlined />} />
          )}
          <div className="user-info">
            <div className="user-name">{user ? user.username : '游客 · 点击登录'}</div>
            <div className="user-status">
              {user ? '已登录 · 云端同步' : (health ? '知识库已就绪' : '连接中…')}
            </div>
          </div>
          {user && (
            <Popconfirm
              title="退出登录？"
              okText="退出"
              cancelText="取消"
              onConfirm={(e) => { e?.stopPropagation?.(); onLogout() }}
              onCancel={(e) => e?.stopPropagation?.()}
            >
              <LogoutOutlined className="user-logout" onClick={(e) => e.stopPropagation()} />
            </Popconfirm>
          )}
        </div>
      </aside>
    </>
  )
}
