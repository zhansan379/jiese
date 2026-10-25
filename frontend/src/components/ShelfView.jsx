import { useEffect, useState } from 'react'
import { Button, Card, Col, Row, Spin, Typography } from 'antd'
import { ReadOutlined } from '@ant-design/icons'
import { motion } from 'framer-motion'
import { api } from '../api.js'

const { Title, Text } = Typography

/** 封面：取自各 EPUB 内嵌的封面图 */
const COVER_SRC = {
  jwly: '/covers/jwly.jpg',
  jzl: '/covers/jzl.jpg',
}

/** 书架内容区（渲染在 main-scroll 内） */
export default function ShelfView({ onOpenBook }) {
  const [books, setBooks] = useState(null)

  useEffect(() => {
    api.books().then(setBooks).catch(() => setBooks([]))
  }, [])

  if (!books) return <div className="page-center"><Spin size="large" /></div>

  return (
    <div className="shelf-content">
      <Row gutter={[24, 24]}>
        {books.map((b, i) => (
          <Col key={b.id} xs={24} sm={12} xl={8}>
            <motion.div
              initial={{ opacity: 0, y: 16 }}
              animate={{ opacity: 1, y: 0 }}
              transition={{ delay: i * 0.08, duration: 0.35 }}
            >
              <Card hoverable className="book-card" onClick={() => onOpenBook(b.id)}>
                <div className="book-cover">
                  <img src={COVER_SRC[b.id] || '/logo-seal.png'} alt={b.title} />
                </div>
                <div className="book-info">
                  <Title level={5} className="book-title">{b.title}</Title>
                  <Text type="secondary">{b.author} 编著</Text>
                  <div className="book-stats">
                    <Text type="secondary">{b.doc_count} 页</Text>
                    <Text type="secondary">{Math.round(b.char_count / 10000)} 万字</Text>
                  </div>
                  <Button type="primary" icon={<ReadOutlined />} block>
                    开始阅读
                  </Button>
                </div>
              </Card>
            </motion.div>
          </Col>
        ))}
      </Row>
    </div>
  )
}
