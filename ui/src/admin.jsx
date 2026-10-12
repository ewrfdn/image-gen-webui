import { useEffect, useState } from 'react';
import { Alert, App, Button, Card, Col, Collapse, Descriptions, Empty, Flex, Form, Image, Input, InputNumber, Modal, Pagination, Row, Select, Space, Spin, Switch, Table, Tag, Typography } from 'antd';
import { ArrowRight, Boxes, Check, Download, History, KeyRound, Layers3, Plus, RefreshCw, Search, Settings2, SlidersHorizontal } from 'lucide-react';
import { icon, PageHeading, useStudio } from './App.jsx';
import { api, dateLabel, modelKey, roles, send, statusNames, taskNames } from './lib.js';
import { ModelArt } from './Generate.jsx';

const { Text, Title, Paragraph } = Typography;
const cardStyle = { borderColor: '#2c2d3c' };
function useRemote(path) {
  const [data, setData] = useState(null), [loading, setLoading] = useState(true), [error, setError] = useState(''), [version, setVersion] = useState(0);
  useEffect(() => {
    const controller = new AbortController(); setLoading(true); setError('');
    api(path, { signal: controller.signal }).then(result => { if (!controller.signal.aborted) setData(result); }).catch(e => { if (!controller.signal.aborted) setError(e.message); }).finally(() => { if (!controller.signal.aborted) setLoading(false); });
    return () => controller.abort();
  }, [path, version]);
  return { data, loading, error, reload: () => setVersion(v => v + 1) };
}
function LoadError({ error }) { return error ? <Alert type="error" title={error} showIcon style={{ marginBottom: 20 }} /> : null; }
function Counter({ value, label, Icon }) { return <Card style={cardStyle}><Flex justify="space-between" align="center"><Flex vertical gap={3}><Text type="secondary" style={{ fontSize: 11 }}>{label}</Text><Text strong style={{ fontSize: 26 }}>{value}</Text></Flex><Flex align="center" justify="center" style={{ width: 44, height: 44, background: '#28243a', borderRadius: 12, color: '#a89be0' }}>{icon(Icon, 21)}</Flex></Flex></Card>; }

export function Backends() {
  const { refresh } = useStudio(), { message } = App.useApp();
  const { data, error, loading, reload } = useRemote('/api/admin/backends');
  const [editor, setEditor] = useState(null), [saving, setSaving] = useState(false), [form] = Form.useForm();
  const nodes = data?.nodes || [];
  function open(node) { setEditor(node || { enabled: true }); form.setFieldsValue({ name: node?.name || '', base_url: node?.base_url || '', inference_token: '', admin_token: '' }); }
  function close() { setEditor(null); form.resetFields(); }
  async function save(values) {
    setSaving(true);
    try { await send('/api/admin/backends' + (editor.id ? '/' + editor.id : ''), editor.id ? 'PUT' : 'POST', { ...values, enabled: editor.enabled }); close(); reload(); await refresh(); message.success('配置已保存，已尝试同步后端。'); }
    catch (e) { message.error(e.message); } finally { setSaving(false); }
  }
  async function action(node, type) {
    try { if (type === 'sync') await send('/api/admin/backends/sync', 'POST', {}); else await send('/api/admin/backends/' + node.id, 'PUT', { name: node.name, base_url: node.base_url, enabled: !node.enabled }); reload(); await refresh(); }
    catch (e) { message.error(e.message); }
  }
  return <>
    <PageHeading eyebrow="INFERENCE INFRASTRUCTURE" title="模型后端" description="管理推理节点，查看模型资源与运行状态。" extra={<Button id="ig-add-node" type="primary" icon={icon(Plus)} onClick={() => open(null)}>接入后端</Button>} />
    <Row gutter={[16, 16]} style={{ marginBottom: 28 }}><Col xs={24} sm={8}><Counter value={nodes.length} label="已接入节点" Icon={Boxes} /></Col><Col xs={24} sm={8}><Counter value={nodes.filter(n => n.connected).length} label="在线节点" Icon={Check} /></Col><Col xs={24} sm={8}><Counter value={nodes.reduce((n, node) => n + node.models.length, 0)} label="已同步模型" Icon={Layers3} /></Col></Row>
    <LoadError error={error} /><Spin spinning={loading}><Flex id="ig-nodes" vertical gap={16}>
      {nodes.map(node => <Card className="ig-node" key={node.id} style={cardStyle}><Flex justify="space-between" align="center" wrap gap={18}><Flex gap={14} align="center"><AvatarIcon Icon={Boxes} /><Flex vertical gap={5}><Flex gap={10} align="center"><Text strong>{node.name}</Text><Tag variant="filled" color={node.connected ? 'success' : 'default'}>{!node.enabled ? '已禁用' : node.quarantined ? '执行结果待确认' : node.connected ? '已连接' : '连接失败'}</Tag></Flex><Text type="secondary" style={{ fontSize: 11, overflowWrap: 'anywhere' }}>{node.base_url}</Text><Text type="secondary" style={{ fontSize: 11 }}>{node.models.length} 个模型 · {node.models.reduce((s, m) => s + m.loras.length, 0)} 个关联 LoRA</Text></Flex></Flex><Space wrap><Button data-action="sync" icon={icon(RefreshCw, 13)} onClick={() => action(node, 'sync')}>同步</Button><Button data-action="edit" onClick={() => open(node)}>编辑</Button><Button data-action="toggle" onClick={() => action(node, 'toggle')}>{node.enabled ? '禁用' : '启用'}</Button></Space></Flex>
        {node.error && <Alert type="warning" title={node.error} style={{ marginTop: 16 }} />}
        <Collapse ghost style={{ marginTop: 16 }} items={[{ key: 'models', label: '模型资源与配置', children: <Flex vertical gap={18}>{node.models.map(m => <Flex key={modelKey(m)} vertical gap={8}><Text strong>{m.configuration.display_name}</Text><Text type="secondary">LoRA：{m.loras.map(l => l.display_name || l.lora_id).join('、') || '无'}</Text><Typography><pre style={{ maxHeight: 280, overflow: 'auto', fontSize: 11 }}>{JSON.stringify(m.configuration, null, 2)}</pre></Typography></Flex>)}</Flex> }]} />
      </Card>)}{!nodes.length && !loading && <Empty description="尚未接入推理后端" />}
    </Flex></Spin>
    <Modal title={editor?.id ? '编辑模型后端' : '接入模型后端'} open={!!editor} onCancel={close} footer={null} destroyOnHidden>
      <Form id="ig-node-form" form={form} onFinish={save} layout="vertical" requiredMark={false} style={{ marginTop: 20 }}>
        <Form.Item label="名称" name="name" rules={[{ required: true }]}><Input id="ig-node-name" placeholder="例如：DGX A" /></Form.Item>
        <Form.Item label="后端地址" name="base_url" rules={[{ required: true }, { validator: (_, value) => { try { const url = new URL(value); if (['http:', 'https:'].includes(url.protocol) && url.hostname) return Promise.resolve(); } catch { /* invalid address */ } return Promise.reject(new Error('请输入有效的 HTTP 或 HTTPS 地址')); } }]}><Input id="ig-node-url" placeholder="http://dgx-a:8000" /></Form.Item>
        <Form.Item label="推理 Token" name="inference_token" rules={[{ required: !editor?.id }]}><Input.Password id="ig-node-inference" autoComplete="new-password" placeholder={editor?.id ? '留空保持原值' : '推理节点访问凭证'} /></Form.Item>
        <Form.Item label="管理 Token" name="admin_token" rules={[{ required: !editor?.id }]}><Input.Password id="ig-node-admin" autoComplete="new-password" placeholder={editor?.id ? '留空保持原值' : '推理节点管理凭证'} /></Form.Item>
        <Space><Button htmlType="submit" type="primary" loading={saving}>保存并接入</Button><Button onClick={close}>取消</Button></Space>
      </Form>
    </Modal>
  </>;
}
function AvatarIcon({ Icon }) { return <Flex align="center" justify="center" style={{ width: 44, height: 44, borderRadius: 12, background: '#28243a', color: '#a89be0', flexShrink: 0 }}>{icon(Icon, 22)}</Flex>; }

export function ModelDefaults({ loraOnly = false }) {
  const { catalog, refresh } = useStudio();
  return <><PageHeading eyebrow={loraOnly ? 'MODEL × LORA' : 'MODEL CONFIGURATION'} title={loraOnly ? 'LoRA 模型关联' : '模型默认参数'} description={loraOnly ? '每个 LoRA 归属于指定模型；生成时仅展示并使用当前模型的 LoRA。' : '统一配置生成默认值，保存后对新任务生效。'} extra={<Button icon={icon(RefreshCw, 14)} onClick={refresh}>刷新目录</Button>} />
    {loraOnly && <Alert type="info" showIcon title="关联来自推理节点的模型目录" description="LoRA 放置在对应模型的 loras 目录中，由节点自动同步。模型或版本不匹配的 LoRA 会被过滤，无法跨模型调用。" style={{ marginBottom: 24 }} />}
    <Row gutter={[20, 20]} id="ig-model-defaults">{catalog.models.map((model, index) => <Col xs={24} xl={12} key={modelKey(model)}><DefaultCard model={model} index={index} loraOnly={loraOnly} /></Col>)}</Row>
    {!catalog.models.length && <Empty description="暂无模型，接入推理后端后即可配置。" />}
  </>;
}
function DefaultCard({ model, index, loraOnly }) {
  const { refresh } = useStudio();
  const [saving, setSaving] = useState(false), [notice, setNotice] = useState(''), [error, setError] = useState('');
  const g = model.configuration.generation;
  async function save(values) {
    setSaving(true); setError(''); setNotice('');
    try { await send('/api/admin/model-defaults', 'PUT', { model_id: model.model_id, revision: model.revision, ...values, default_seed: values.default_seed ?? null }); setNotice('已保存，新任务立即生效。'); await refresh(); }
    catch (e) { setError(e.message); } finally { setSaving(false); }
  }
  return <Card className="ig-default-card" data-default-index={index} data-model-id={model.model_id} style={cardStyle}>
    <Flex align="center" gap={14} style={{ marginBottom: 22 }}><ModelArt index={index} /><Flex vertical gap={4}><Text strong>{model.configuration.display_name}</Text><Text type="secondary" style={{ fontSize: 11 }}>{model.revision} · {statusNames[model.status]}</Text></Flex></Flex>
    {!loraOnly && <Form layout="vertical" onFinish={save} initialValues={{ default_steps: g.default_steps, default_seed: g.default_seed, default_guidance_scale: g.default_guidance_scale, default_size: g.default_size }} requiredMark={false}>
      <Row gutter={16}><Col xs={24} sm={12}><Form.Item label="默认步数 / Steps" name="default_steps" rules={[{ required: true }]}><InputNumber aria-label="默认步数" name="default_steps" min={1} max={g.steps_max} precision={0} style={{ width: '100%' }} /></Form.Item></Col><Col xs={24} sm={12}><Form.Item label="随机种子 / Seed" name="default_seed"><InputNumber aria-label="随机种子" name="default_seed" min={0} max={4294967295} precision={0} placeholder="留空随机生成" style={{ width: '100%' }} /></Form.Item></Col><Col xs={24} sm={12}><Form.Item label="引导强度 / Guidance" name="default_guidance_scale" rules={[{ required: true }]}><InputNumber aria-label="引导强度" name="default_guidance_scale" min={0} max={20} step={.1} style={{ width: '100%' }} /></Form.Item></Col><Col xs={24} sm={12}><Form.Item label="默认分辨率" name="default_size" rules={[{ required: true }, { pattern: /^\d+x\d+$/, message: '格式为 1024x1024' }]}><Input aria-label="默认分辨率" name="default_size" /></Form.Item></Col></Row>
      <Text type="secondary" style={{ fontSize: 11 }}>宽 {g.width_min}–{g.width_max} / 高 {g.height_min}–{g.height_max}，{g.dimension_multiple} 的倍数，最多 {(g.max_pixels / 1000000).toFixed(2)} MP</Text>
      <Flex align="center" gap={12} wrap style={{ marginTop: 20 }}><Button htmlType="submit" type="primary" loading={saving} icon={icon(Settings2, 14)}>保存默认参数</Button>{notice && <Text type="success" role="status" style={{ fontSize: 11 }}>{notice}</Text>}</Flex>{error && <Alert type="error" title={error} style={{ marginTop: 16 }} />}
    </Form>}
    <Flex vertical gap={12} style={{ marginTop: loraOnly ? 0 : 24, paddingTop: loraOnly ? 0 : 20, borderTop: loraOnly ? 'none' : '1px solid #2c2d3c' }}><Flex justify="space-between"><Flex gap={7} align="center">{icon(Layers3, 14)}<Text strong style={{ fontSize: 12 }}>关联 LoRA</Text></Flex><Tag variant="filled">{model.loras.length} 个</Tag></Flex>
      {model.loras.map(l => <Flex key={l.lora_id + l.sha256} justify="space-between" align="center" gap={12} style={{ padding: '12px 14px', borderRadius: 10, background: '#20212d' }}><Flex vertical gap={3}><Text strong style={{ fontSize: 12 }}>{l.display_name || l.lora_id}</Text><Text type="secondary" style={{ fontSize: 10, overflowWrap: 'anywhere' }}>{model.model_id} · {model.revision}</Text></Flex><Tag color="success" variant="filled" style={{ fontSize: 10, margin: 0 }}>已关联</Tag></Flex>)}
      {!model.loras.length && <Text type="secondary" style={{ fontSize: 12 }}>{model.configuration.lora.supported ? '此模型尚未关联 LoRA' : '此模型不支持 LoRA'}</Text>}
    </Flex>
  </Card>;
}

export function HistoryPage() {
  const { historyFilter, setHistoryFilter } = useStudio();
  const [query, setQuery] = useState(historyFilter), [status, setStatus] = useState(''), [offset, setOffset] = useState(0), [selected, setSelected] = useState(null);
  const { data, error, loading, reload } = useRemote('/api/admin/history?limit=24&offset=' + offset + '&q=' + encodeURIComponent(query.q) + '&user_id=' + encodeURIComponent(query.user_id || '') + '&status=' + status);
  function filter(values) { const next = { q: values.q?.trim() || '', user_id: '' }; setHistoryFilter(next); setQuery(next); setStatus(values.status || ''); setOffset(0); reload(); }
  const records = data?.tasks || [];
  return <><PageHeading eyebrow="GENERATION LIBRARY" title="生成记录" description="每一次创作，都有迹可循。查看用户作品与生成参数。" extra={<Tag id="ig-history-count" variant="filled">{data ? `${data.total} 条记录` : '正在读取记录'}</Tag>} />
    <Card style={{ ...cardStyle, marginBottom: 24 }} styles={{ body: { padding: 18 } }}><Form id="ig-history-filter" initialValues={{ q: historyFilter.q, status: '' }} onFinish={filter}><Row gutter={[10, 10]}><Col xs={24} md={12}><Form.Item name="q" noStyle><Input id="ig-history-user" prefix={icon(Search, 15)} placeholder="搜索用户名或 API Key 名称" aria-label="搜索用户" /></Form.Item></Col><Col xs={12} md={6}><Form.Item name="status" noStyle><Select id="ig-history-status" aria-label="任务状态" style={{ width: '100%' }} options={[{ value: '', label: '全部状态' }, ...Object.entries(taskNames).map(([value, label]) => ({ value, label }))]} /></Form.Item></Col><Col xs={12} md={6}><Flex gap={8}><Button type="primary" htmlType="submit" icon={icon(SlidersHorizontal, 14)}>筛选记录</Button><Button aria-label="刷新记录" icon={icon(RefreshCw, 14)} onClick={reload} /></Flex></Col></Row></Form></Card>
    <LoadError error={error} /><Spin spinning={loading}><Row gutter={[18, 18]} id="ig-admin-history">{records.map(t => <Col xs={24} sm={12} xl={8} xxl={6} key={t.id}><Card className="ig-gallery-card" role="button" tabIndex={0} aria-label={`查看 ${t.username} 的作品`} onClick={() => setSelected(t)} onKeyDown={e => { if (e.target === e.currentTarget && ['Enter', ' '].includes(e.key)) { e.preventDefault(); setSelected(t); } }} hoverable styles={{ body: { padding: 18 } }} style={{ overflow: 'hidden', ...cardStyle }} cover={<Flex align="center" justify="center" style={{ aspectRatio: '4/3', background: '#232431', overflow: 'hidden', color: '#9287b1' }}>{t.files[0] ? t.files[0].mime.startsWith('video/') ? <video muted preload="metadata" src={t.files[0].url} style={{ width: '100%', height: '100%', objectFit: 'cover' }} /> : <img src={t.files[0].url} alt={t.prompt.slice(0, 100)} loading="lazy" style={{ width: '100%', height: '100%', objectFit: 'cover' }} /> : <Flex vertical gap={10} align="center">{icon(History, 26)}<Text type="secondary">{taskNames[t.status]}</Text></Flex>}</Flex>}><Flex justify="space-between" align="center" wrap gap={6}><Text strong style={{ fontSize: 12 }}>{t.username}</Text><Tag variant="filled" color={t.status === 'succeeded' ? 'success' : t.status === 'failed' ? 'error' : 'default'} style={{ fontSize: 10, margin: 0 }}>{taskNames[t.status]}</Tag></Flex><Paragraph ellipsis={{ rows: 2 }} style={{ fontSize: 12, margin: '12px 0 10px', minHeight: 38 }}>{t.prompt}</Paragraph><Flex vertical gap={3}><Text type="secondary" style={{ fontSize: 10 }}>{t.model_id} · {t.parameters.size}</Text><Text type="secondary" style={{ fontSize: 10 }}>{dateLabel(t.created_at)}</Text></Flex></Card></Col>)}</Row>{!records.length && !loading && <Empty description="没有匹配的生成记录。" />}</Spin>
    <Flex justify="flex-end" style={{ marginTop: 26 }}><Pagination current={offset / 24 + 1} pageSize={24} total={data?.total || 0} showSizeChanger={false} size="small" onChange={page => setOffset((page - 1) * 24)} /></Flex>
    <Modal title="作品详情" open={!!selected} onCancel={() => setSelected(null)} width={820} footer={<Button id="ig-history-close" onClick={() => setSelected(null)}>关闭</Button>} destroyOnHidden>{selected && <Flex id="ig-history-detail" vertical gap={18} style={{ marginTop: 18 }}>{selected.files.map(file => <Flex key={file.id} vertical align="center" gap={10}>{file.mime.startsWith('video/') ? <video controls src={file.url} style={{ width: '100%', maxHeight: 430, borderRadius: 12 }} /> : <Image src={file.url} alt="生成作品" style={{ maxHeight: 430, objectFit: 'contain', borderRadius: 12 }} />}<Button href={file.url} download={file.storage_key.split('/').pop()} icon={icon(Download)}>下载原图</Button></Flex>)}<Paragraph style={{ whiteSpace: 'pre-wrap', overflowWrap: 'anywhere', margin: 0 }}>{selected.prompt}</Paragraph><Descriptions size="small" column={{ xs: 1, sm: 2 }} items={[
      { key: 'user', label: '用户', children: selected.username }, { key: 'state', label: '状态', children: taskNames[selected.status] }, { key: 'model', label: '模型', children: selected.model_id }, { key: 'version', label: '版本', children: selected.model_revision }, { key: 'size', label: '分辨率', children: selected.parameters.size }, { key: 'steps', label: 'Steps', children: selected.parameters.num_inference_steps }, { key: 'seed', label: 'Seed', children: selected.parameters.seed ?? '随机' }, { key: 'guidance', label: 'Guidance', children: selected.parameters.guidance_scale }, { key: 'date', label: '创建时间', children: dateLabel(selected.created_at) }, { key: 'id', label: '任务 ID', children: <Text style={{ fontSize: 11, overflowWrap: 'anywhere' }} copyable>{selected.id}</Text> },
    ]} />{selected.error && <Alert type="error" showIcon title={selected.error} />}</Flex>}</Modal>
  </>;
}

export function Keys() {
  const { message } = App.useApp();
  const { data, error, loading, reload } = useRemote('/api/admin/keys');
  const [secret, setSecret] = useState(''), [creating, setCreating] = useState(false), [form] = Form.useForm();
  async function create(values) {
    setCreating(true);
    try { const result = await send('/api/admin/keys', 'POST', values); setSecret(result.key); form.resetFields(); reload(); }
    catch (e) { message.error(e.message); } finally { setCreating(false); }
  }
  async function change(key, action) {
    try { await send('/api/admin/keys/' + key.id, 'PATCH', action === 'revoke' ? { revoked: true } : { enabled: !key.enabled }); reload(); }
    catch (e) { message.error(e.message); }
  }
  const columns = [
    { title: '名称', dataIndex: 'name' }, { title: 'Key 前缀', dataIndex: 'prefix', render: v => <Text code>{v}…</Text> },
    { title: '状态', render: (_, key) => <Tag color={key.revoked ? 'default' : key.enabled ? 'success' : 'warning'} variant="filled">{key.revoked ? '已撤销' : key.enabled ? '启用' : '停用'}</Tag> },
    { title: '最后调用', dataIndex: 'last_used_at', render: v => v ? dateLabel(v) : '尚未调用' },
    { title: '操作', render: (_, key) => <Space><Button data-action="toggle" size="small" disabled={key.revoked} onClick={() => change(key, 'toggle')}>{key.enabled ? '停用' : '启用'}</Button><Button data-action="revoke" size="small" disabled={key.revoked} onClick={() => change(key, 'revoke')}>撤销</Button></Space> },
  ];
  return <><PageHeading eyebrow="DEVELOPER ACCESS" title="API Key" description="让创作连接你的应用、安全地调用生成接口。" />
    <Card style={{ ...cardStyle, marginBottom: 24 }}><Form id="ig-key-form" form={form} onFinish={create}><Flex wrap gap={12}><Form.Item name="name" rules={[{ required: true, message: '请输入 Key 名称' }]} style={{ marginBottom: 0, flex: 1, minWidth: 140 }}><Input id="ig-key-name" placeholder="例如：本地脚本" aria-label="Key 名称" /></Form.Item><Button htmlType="submit" type="primary" loading={creating} icon={icon(Plus, 14)}>创建 Key</Button></Flex></Form></Card>
    {secret && <Card id="ig-created-key" style={{ marginBottom: 24, background: '#28243a', borderColor: '#4d4269' }}><Flex vertical gap={12}><Text strong>仅此处展示一次，请保存到安全位置</Text><Input id="ig-key-value" value={secret} readOnly aria-label="新建 API Key" /><Button id="ig-dismiss-key" onClick={() => setSecret('')} style={{ alignSelf: 'flex-start' }}>已保存，关闭</Button></Flex></Card>}
    <LoadError error={error} /><Card styles={{ body: { padding: 0, overflow: 'hidden' } }} style={cardStyle}><Table id="ig-keys" dataSource={data?.keys || []} rowKey="id" columns={columns} loading={loading} pagination={false} scroll={{ x: 700 }} /></Card>
    <Card title="调用示例" style={{ ...cardStyle, marginTop: 24 }}><Typography><pre style={{ fontSize: 12 }}>{'POST /v1/images/generations\nAuthorization: Bearer YOUR_PLATFORM_KEY\n\n{"model":"qwen-image-2.1","prompt":"雨夜城市","n":1}'}</pre></Typography><Text type="secondary" style={{ fontSize: 12 }}>参考图使用 POST /v1/images/edits。LoRA 参数只接受所选模型关联的 LoRA。</Text></Card>
  </>;
}

export function SettingsPage() {
  const { refresh } = useStudio();
  const { data, error, loading } = useRemote('/api/admin/settings');
  const [saving, setSaving] = useState(false), [notice, setNotice] = useState(''), [saveError, setSaveError] = useState('');
  async function save(values) {
    setSaving(true); setNotice(''); setSaveError('');
    try { await send('/api/admin/settings', 'PUT', values); setNotice('参数已保存。'); await refresh(); }
    catch (e) { setSaveError(e.message); } finally { setSaving(false); }
  }
  return <><PageHeading eyebrow="PLATFORM PREFERENCES" title="系统参数" description="调整队列容量与节点目录更新频率。" /><LoadError error={error} /><Spin spinning={loading}>
    {data && <Card style={{ ...cardStyle, maxWidth: 600 }}><Form id="ig-settings-form" layout="vertical" onFinish={save} initialValues={data} requiredMark={false}>
      <Form.Item label="最大等待任务数 / maxCount" name="max_count" extra="图片与视频共同计数；已有任务会继续处理。" rules={[{ required: true }]}><InputNumber id="ig-max" min={1} max={100000} precision={0} style={{ width: '100%' }} /></Form.Item>
      <Form.Item label="目录刷新间隔 / 秒" name="refresh_seconds" rules={[{ required: true }]}><InputNumber id="ig-refresh" min={1} max={3600} precision={0} style={{ width: '100%' }} /></Form.Item>
      <Form.Item label="节点状态有效期 / 秒" name="stale_seconds" rules={[{ required: true }]}><InputNumber id="ig-stale" min={1} max={86400} precision={0} style={{ width: '100%' }} /></Form.Item>
      <Descriptions column={1} size="small" items={[{ key: 'timeout', label: '可用性查询超时', children: '1 秒' }, { key: 'release', label: '模型空闲释放', children: '1 小时 · 每分钟检查' }]} style={{ marginBottom: 24 }} />
      <Button type="primary" htmlType="submit" loading={saving}>保存参数</Button>{notice && <Alert id="ig-settings-message" type="success" title={notice} style={{ marginTop: 18 }} />}{saveError && <Alert type="error" title={saveError} style={{ marginTop: 18 }} />}
    </Form></Card>}
  </Spin></>;
}

export function UsersPage() {
  const { identity, refresh, navigate, setHistoryFilter } = useStudio(), { message } = App.useApp();
  const [offset, setOffset] = useState(0), [search, setSearch] = useState('');
  const { data, error, loading, reload } = useRemote('/api/admin/users?limit=20&offset=' + offset + '&q=' + encodeURIComponent(search));
  const [creating, setCreating] = useState(false), [editor, setEditor] = useState(null), [saving, setSaving] = useState(false), [editError, setEditError] = useState('');
  const [createForm] = Form.useForm(), [editForm] = Form.useForm();
  const roleOptions = Object.entries(roles).map(([value, label]) => ({ value, label }));
  async function create(values) {
    setCreating(true);
    try { await send('/api/admin/users', 'POST', values); createForm.resetFields(); setOffset(0); reload(); message.success('账号已创建。'); }
    catch (e) { message.error(e.message); } finally { setCreating(false); }
  }
  async function toggle(user) {
    try { await send('/api/admin/users/' + user.id, 'PATCH', { enabled: !user.enabled }); reload(); await refresh(); }
    catch (e) { message.error(e.message); }
  }
  function edit(user, reset) { setEditor({ ...user, reset }); setEditError(''); editForm.resetFields(); editForm.setFieldsValue({ username: user.username, role: user.role, password: '' }); }
  function close() { setEditor(null); editForm.resetFields(); setEditError(''); }
  async function save(values) {
    setSaving(true); setEditError('');
    try { await send('/api/admin/users/' + editor.id, 'PATCH', editor.reset ? { password: values.password } : { username: values.username, role: values.role }); close(); reload(); await refresh(); }
    catch (e) { setEditError(e.message); } finally { setSaving(false); }
  }
  function history(user) { setHistoryFilter({ q: user.username, user_id: user.id }); navigate('history'); }
  const columns = [
    { title: '用户名', dataIndex: 'username', render: name => <Text strong>{name}</Text> },
    { title: '角色', dataIndex: 'role', render: role => <Tag variant="filled" color={role === 'super_admin' ? 'gold' : role === 'admin' ? 'cyan' : 'default'}>{roles[role]}</Tag> },
    { title: '状态', dataIndex: 'enabled', render: enabled => <Tag variant="filled" color={enabled ? 'success' : 'default'}>{enabled ? '启用' : '禁用'}</Tag> },
    { title: '操作', render: (_, user) => <Space wrap><Button data-action="history" size="small" icon={icon(History, 12)} onClick={() => history(user)}>生成记录</Button><Button data-action="edit" size="small" onClick={() => edit(user, false)}>编辑</Button><Button data-action="toggle" size="small" onClick={() => toggle(user)}>{user.enabled ? '禁用' : '启用'}</Button><Button data-action="password" size="small" onClick={() => edit(user, true)}>重置密码</Button></Space> },
  ];
  if (identity.role !== 'super_admin') return null;
  return <><PageHeading eyebrow="PEOPLE & PERMISSIONS" title="用户管理" description="管理团队账号、访问权限和用户的创作记录。" />
    <Card title="创建账号" style={{ ...cardStyle, marginBottom: 22 }}><Form id="ig-user-form" form={createForm} layout="vertical" onFinish={create} initialValues={{ role: 'user' }} requiredMark={false}><Row gutter={16} align="bottom"><Col xs={24} md={8}><Form.Item label="用户名" name="username" rules={[{ required: true }, { min: 3 }]}><Input id="ig-user-name" maxLength={64} autoComplete="off" /></Form.Item></Col><Col xs={24} md={8}><Form.Item label="初始密码" name="password" rules={[{ required: true }, { min: 12, message: '密码至少需要 12 个字符' }]}><Input.Password id="ig-user-password" maxLength={256} autoComplete="new-password" /></Form.Item></Col><Col xs={24} md={8}><Form.Item label="角色" name="role"><Select id="ig-user-role" options={roleOptions} /></Form.Item></Col></Row><Button htmlType="submit" type="primary" loading={creating} icon={icon(Plus, 14)}>创建账号</Button></Form></Card>
    <Form id="ig-user-search-form" onFinish={values => { setSearch(values.q?.trim() || ''); setOffset(0); reload(); }} style={{ marginBottom: 18 }}><Flex gap={10}><Form.Item name="q" noStyle><Input id="ig-user-search" placeholder="搜索用户名" maxLength={64} prefix={icon(Search, 14)} style={{ maxWidth: 360 }} /></Form.Item><Button htmlType="submit">搜索</Button></Flex></Form>
    <LoadError error={error} /><Card styles={{ body: { padding: 0, overflow: 'hidden' } }} style={cardStyle}><Table id="ig-users" rowKey="id" columns={columns} dataSource={data?.users || []} loading={loading} pagination={false} scroll={{ x: 780 }} /></Card>
    <Flex justify="flex-end" style={{ marginTop: 22 }}><Pagination pageSize={20} current={offset / 20 + 1} total={data?.total || 0} showSizeChanger={false} showTotal={n => `共 ${n} 个账号`} onChange={page => setOffset((page - 1) * 20)} /></Flex>
    <Modal title={editor?.reset ? '重置密码' : '编辑账号'} open={!!editor} onCancel={close} footer={null} forceRender><Form id="ig-user-edit-form" form={editForm} onFinish={save} layout="vertical" requiredMark={false} style={{ marginTop: 22 }}>
      {editor?.reset ? <Form.Item label="新密码" name="password" rules={[{ required: true }, { min: 12, message: '密码至少需要 12 个字符' }]}><Input.Password id="ig-user-edit-password" autoComplete="new-password" maxLength={256} /></Form.Item> : <><Form.Item label="用户名" name="username" rules={[{ required: true }, { min: 3 }]}><Input id="ig-user-edit-name" maxLength={64} /></Form.Item><Form.Item label="角色" name="role"><Select id="ig-user-edit-role" options={roleOptions} /></Form.Item></>}
      {editError && <Alert type="error" title={editError} style={{ marginBottom: 16 }} />}<Space><Button htmlType="submit" type="primary" loading={saving}>保存</Button><Button onClick={close}>取消</Button></Space>
    </Form></Modal>
  </>;
}
