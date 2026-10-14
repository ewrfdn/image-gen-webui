import { useEffect, useState } from 'react';
import { Alert, App, Button, Card, Dropdown, Empty, Flex, Grid, Spin, Tag, Typography } from 'antd';
import { ArrowLeft, Download, Layers, MoreHorizontal, RefreshCw, Trash2, WandSparkles } from 'lucide-react';
import { icon, PageHeading, useStudio } from './App.jsx';
import { api, request, dateLabel } from './lib.js';
import './gallery.css';
import ArtworkModal from './ArtworkModal.jsx';

const { Text, Paragraph } = Typography;
const PAGE_SIZE = 24;
export default function Gallery() {
  const { navigate, setGenerationDraft, refresh } = useStudio();
  const { message, modal } = App.useApp();
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
  function openTask(task) { setSelected(task); }
  function recreate(task) { setGenerationDraft(task); setSelected(null); navigate('generate'); }
  function remove(task) {
    modal.confirm({ title: '删除整个作品？', content: `将永久删除此任务及其 ${task.files.length} 张生成图片。`, okText: '删除作品', cancelText: '取消', okButtonProps: { danger: true }, onOk: async () => {
      try { await request('/api/tasks/' + task.id, { method: 'DELETE' }); setSelected(null); reload(); await refresh(); message.success('作品及关联图片已删除'); }
      catch (e) { message.error(e.message); throw e; }
    } });
  }
  const taskMenu = task => ({ items: [{ key: 'recreate', label: '相同参数创建', icon: icon(WandSparkles, 15) }, { type: 'divider' }, { key: 'delete', label: '删除整个作品', danger: true, icon: icon(Trash2, 15) }], onClick: ({ key }) => key === 'delete' ? remove(task) : recreate(task) });
  return <>
    <PageHeading eyebrow="YOUR IMAGE LIBRARY" title="我的作品" description="按时间收藏你的每一次创作，点击图片查看提示词与生成参数。" extra={<Flex gap={8} wrap><Button icon={icon(ArrowLeft, 14)} onClick={() => navigate('generate')}>返回创作台</Button><Button aria-label="刷新作品" icon={icon(RefreshCw, 14)} onClick={reload} /></Flex>} />
    <Flex justify="space-between" style={{ marginBottom: 20 }}><Text type="secondary" style={{ fontSize: 12 }}>已生成的历史图片</Text><Tag id="ig-gallery-total" variant="filled">{total} 幅作品</Tag></Flex>
    {error && <Alert type="error" showIcon title={error} style={{ marginBottom: 20 }} />}
    <Flex id="ig-personal-gallery" role="list" aria-label="历史作品瀑布流" aria-busy={loading} style={{ display: 'block', columnCount: columns, columnGap: 18 }}>
      {records.map(task => <Card key={task.id} className="ig-artwork-card" data-artwork={task.id} role="listitem" styles={{ body: { padding: 0 } }} style={{ breakInside: 'avoid', marginBottom: 18, overflow: 'hidden', background: '#191b22', borderColor: '#ffffff0d' }}>
        <div className="ig-gallery-cover">
          <Button type="text" aria-label={`查看作品：${task.prompt.slice(0, 80)}`} onClick={() => openTask(task)} className="ig-gallery-open"><img src={task.files[0].url} alt={task.prompt.slice(0, 100)} loading="lazy" /></Button>
          {task.files.length > 1 && <span className="ig-image-count" aria-label={`${task.files.length} 张图片`}>{icon(Layers, 13)} {task.files.length}</span>}
          <Dropdown menu={taskMenu(task)} trigger={['click']} placement="bottomRight"><Button className="ig-task-menu" shape="circle" icon={icon(MoreHorizontal, 18)} aria-label="作品操作" /></Dropdown>
        </div>
        <div className="ig-gallery-caption"><Paragraph ellipsis={{ rows: 2 }} style={{ margin: '0 0 12px', fontSize: 13, lineHeight: 1.7 }}>{task.prompt}</Paragraph>
          <Flex justify="space-between" align="center" gap={8}><Flex vertical gap={4} style={{ minWidth: 0 }}><Text type="secondary" ellipsis style={{ fontSize: 11 }}>{task.model_id} · {task.parameters.size}</Text><Text type="secondary" style={{ fontSize: 10 }}>{dateLabel(task.created_at)}</Text></Flex><Button type="text" shape="circle" href={task.files[0].url} download={task.files[0].storage_key.split('/').pop()} icon={icon(Download, 16)} aria-label="下载作品" /></Flex>
        </div>
      </Card>)}
    </Flex>
    {!records.length && !loading && !error && <Empty description="还没有历史图片，去创作第一幅作品吧。"><Button type="primary" onClick={() => navigate('generate')}>开始创作</Button></Empty>}
    <Flex align="center" justify="center" gap={12} style={{ padding: 24 }}>{loading ? <Spin /> : !error && offset + PAGE_SIZE < total ? <Button id="ig-gallery-more" onClick={() => setOffset(o => o + PAGE_SIZE)}>加载更多作品</Button> : records.length > 0 && <Text type="secondary" style={{ fontSize: 11 }}>已展示全部 {total} 幅作品</Text>}{error && <Button onClick={() => setVersion(v => v + 1)}>重新加载</Button>}</Flex>
    <ArtworkModal task={selected} onClose={() => setSelected(null)} onDeleted={reload} />
  </>;
}
