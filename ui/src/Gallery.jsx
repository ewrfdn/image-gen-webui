import { useEffect, useState } from 'react';
import { Alert, Button, Card, Descriptions, Empty, Flex, Grid, Image, Modal, Spin, Tag, Typography } from 'antd';
import { ArrowLeft, Download, RefreshCw } from 'lucide-react';
import { icon, PageHeading, useStudio } from './App.jsx';
import { api, dateLabel } from './lib.js';

const { Text, Paragraph } = Typography;
const PAGE_SIZE = 24;
export default function Gallery() {
  const { navigate } = useStudio();
  const screens = Grid.useBreakpoint();
  const [records, setRecords] = useState([]), [total, setTotal] = useState(0);
  const [offset, setOffset] = useState(0), [version, setVersion] = useState(0);
  const [loading, setLoading] = useState(true), [error, setError] = useState(''), [selected, setSelected] = useState(null);
  const columns = screens.xxl ? 4 : screens.xl ? 3 : screens.sm ? 2 : 1;
  useEffect(() => {
    const controller = new AbortController(); setLoading(true); setError('');
    api(`/api/history?limit=${PAGE_SIZE}&offset=${offset}`, { signal: controller.signal }).then(result => {
      if (controller.signal.aborted) return;
      setTotal(result.total);
      setRecords(previous => offset === 0 ? result.tasks : [...new Map([...previous, ...result.tasks].map(t => [t.id, t])).values()]);
    }).catch(e => { if (!controller.signal.aborted) setError(e.message); }).finally(() => { if (!controller.signal.aborted) setLoading(false); });
    return () => controller.abort();
  }, [offset, version]);
  function reload() { setOffset(0); setVersion(v => v + 1); }
  return <>
    <PageHeading eyebrow="YOUR IMAGE LIBRARY" title="我的作品" description="按时间收藏你的每一次创作，点击图片查看提示词与生成参数。" extra={<Flex gap={8} wrap><Button icon={icon(ArrowLeft, 14)} onClick={() => navigate('generate')}>返回创作台</Button><Button aria-label="刷新作品" icon={icon(RefreshCw, 14)} onClick={reload} /></Flex>} />
    <Flex justify="space-between" style={{ marginBottom: 20 }}><Text type="secondary" style={{ fontSize: 12 }}>已生成的历史图片</Text><Tag id="ig-gallery-total" variant="filled">{total} 幅作品</Tag></Flex>
    {error && <Alert type="error" showIcon title={error} style={{ marginBottom: 20 }} />}
    <Flex id="ig-personal-gallery" role="list" aria-label="历史作品瀑布流" aria-busy={loading} style={{ display: 'block', columnCount: columns, columnGap: 18 }}>
      {records.map(task => <Card key={task.id} className="ig-artwork-card" data-artwork={task.id} role="listitem" styles={{ body: { padding: 16 } }} style={{ breakInside: 'avoid', marginBottom: 18, overflow: 'hidden', background: '#1a1b25', borderColor: '#2c2d3c' }}>
        <Button type="text" aria-label={`查看作品：${task.prompt.slice(0, 80)}`} onClick={() => setSelected(task)} style={{ display: 'block', height: 'auto', padding: 0, width: '100%', border: 0, borderRadius: 8, overflow: 'hidden', marginBottom: 14 }}>
          <img src={task.files[0].url} alt={task.prompt.slice(0, 100)} loading="lazy" style={{ display: 'block', width: '100%', height: 'auto' }} />
        </Button>
        <Paragraph ellipsis={{ rows: 2 }} style={{ margin: '0 0 12px', fontSize: 12 }}>{task.prompt}</Paragraph>
        <Flex justify="space-between" align="center" gap={8}><Flex vertical gap={3} style={{ minWidth: 0 }}><Text type="secondary" ellipsis style={{ fontSize: 10 }}>{task.model_id} · {task.parameters.size}</Text><Text type="secondary" style={{ fontSize: 10 }}>{dateLabel(task.created_at)}</Text></Flex><Button type="text" size="small" href={task.files[0].url} download={task.files[0].storage_key.split('/').pop()} icon={icon(Download, 14)} aria-label="下载作品" /></Flex>
      </Card>)}
    </Flex>
    {!records.length && !loading && !error && <Empty description="还没有历史图片，去创作第一幅作品吧。"><Button type="primary" onClick={() => navigate('generate')}>开始创作</Button></Empty>}
    <Flex align="center" justify="center" gap={12} style={{ padding: 24 }}>{loading ? <Spin /> : !error && offset + PAGE_SIZE < total ? <Button id="ig-gallery-more" onClick={() => setOffset(o => o + PAGE_SIZE)}>加载更多作品</Button> : records.length > 0 && <Text type="secondary" style={{ fontSize: 11 }}>已展示全部 {total} 幅作品</Text>}{error && <Button onClick={() => setVersion(v => v + 1)}>重新加载</Button>}</Flex>
    <Modal title="作品详情" open={!!selected} onCancel={() => setSelected(null)} width={820} footer={<Button onClick={() => setSelected(null)}>关闭</Button>} destroyOnHidden>
      {selected && <Flex id="ig-artwork-detail" vertical gap={18} style={{ marginTop: 20 }}>{selected.files.map(file => <Flex key={file.id} vertical align="center" gap={12}><Image src={file.url} alt={selected.prompt} style={{ maxHeight: 500, objectFit: 'contain', borderRadius: 10 }} /><Button href={file.url} download={file.storage_key.split('/').pop()} icon={icon(Download)}>下载原图</Button></Flex>)}<Paragraph style={{ margin: 0, whiteSpace: 'pre-wrap', overflowWrap: 'anywhere' }}>{selected.prompt}</Paragraph><Descriptions column={{ xs: 1, sm: 2 }} size="small" items={[
        { key: 'model', label: '模型', children: selected.model_id }, { key: 'size', label: '分辨率', children: selected.parameters.size }, { key: 'steps', label: 'Steps', children: selected.parameters.num_inference_steps }, { key: 'seed', label: 'Seed', children: selected.parameters.seed ?? '随机' }, { key: 'date', label: '生成时间', children: dateLabel(selected.created_at) },
      ]} /></Flex>}
    </Modal>
  </>;
}
