import { useState } from 'react'
import { Button, Form, Input } from 'antd'
import { api } from '../api.js'

/**
 * 登录/注册全屏页（DeepSeek 风格）：居中窄列、大圆角输入框、胶囊主按钮。
 * 字段仅需用户名+密码；注册含确认密码与邮箱（选填，预留字段）。
 * 底部提供游客入口，关闭后继续本地模式。
 */
export default function AuthModal({ open, onClose, onAuthed }) {
  const [mode, setMode] = useState('login')
  const [busy, setBusy] = useState(false)
  const [form] = Form.useForm()

  if (!open) return null

  const submit = async (values) => {
    setBusy(true)
    try {
      const res = mode === 'login'
        ? await api.login(values.username, values.password)
        : await api.register(values.username, values.password, values.email)
      form.resetFields()
      await onAuthed(res)
    } catch (e) {
      form.setFields([{ name: 'password', errors: [e.message] }])
    } finally {
      setBusy(false)
    }
  }

  const switchMode = (next) => {
    form.resetFields()
    setMode(next)
  }

  return (
    <div className="auth-page">
      <div className="auth-card">
        <img className="auth-logo" src="/logo-seal.png" alt="" />
        <div className="auth-app-name">戒色知识库</div>
        <div className="auth-desc">
          {mode === 'login' ? '登录后对话云端同步，跨设备可见' : '只需一个账号，即可同步全部对话记录'}
        </div>

        <Form
          form={form}
          layout="vertical"
          onFinish={submit}
          requiredMark={false}
          className="auth-form"
        >
          <Form.Item name="username" noStyle
            rules={[{ required: true, message: '请输入用户名' }]}>
            <Input className="auth-input" size="large" placeholder="请输入用户名" autoComplete="username" />
          </Form.Item>

          <Form.Item name="password" noStyle
            rules={[{ required: true, message: '请输入密码' }]}>
            <Input.Password className="auth-input" size="large" placeholder="请输入密码"
              autoComplete={mode === 'login' ? 'current-password' : 'new-password'} />
          </Form.Item>

          {mode === 'register' && (
            <>
              <Form.Item name="confirm" noStyle dependencies={['password']}
                rules={[
                  { required: true, message: '请再次输入密码' },
                  ({ getFieldValue }) => ({
                    validator: (_, v) =>
                      v === getFieldValue('password')
                        ? Promise.resolve()
                        : Promise.reject(new Error('两次密码不一致')),
                  }),
                ]}>
                <Input.Password className="auth-input" size="large" placeholder="请再次输入密码"
                  autoComplete="new-password" />
              </Form.Item>

              <Form.Item name="email" noStyle
                rules={[{ type: 'email', message: '邮箱格式不正确' }]}>
                <Input className="auth-input" size="large" placeholder="邮箱（选填，预留）" autoComplete="email" />
              </Form.Item>
            </>
          )}

          <Button type="primary" htmlType="submit" size="large" block loading={busy} className="auth-submit">
            {mode === 'login' ? '登录' : '注册'}
          </Button>
        </Form>

        <div className="auth-switch">
          {mode === 'login'
            ? <>还没有账号？<a onClick={() => switchMode('register')}>注册</a></>
            : <>已有账号？<a onClick={() => switchMode('login')}>返回登录</a></>}
        </div>

        <div className="auth-agreement">
          继续，即表示同意 <a>用户协议</a> 与 <a>隐私政策</a>
        </div>

        <div className="auth-divider"><span>或</span></div>
        <button type="button" className="auth-guest" onClick={onClose}>
          游客访问，暂不登录
        </button>
      </div>

      <div className="auth-foot">戒色知识库 · AI 问答仅供学习参考</div>
    </div>
  )
}
