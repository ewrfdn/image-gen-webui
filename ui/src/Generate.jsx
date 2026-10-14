import { useEffect, useMemo, useState } from 'react';
import { Alert, App, Button, Card, Col, Collapse, Empty, Flex, Form, Grid, Input, InputNumber, Row, Select, Slider, Spin, Tag, Typography, Upload } from 'antd';
import { Check, Download, ImagePlus, Layers3, Maximize2, Plus, Sparkles, WandSparkles } from 'lucide-react';
import ArtworkModal from './ArtworkModal.jsx';
import './generate.css';
import { icon, PageHeading, useStudio } from './App.jsx';
import { api, request, dateLabel, modelKey, send, sizeChoices, taskNames } from './lib.js';

const { Text, Title } = Typography;
export function ModelArt({ index = 0, large = false }) {
  const palettes = [['#39324d', '#9680bd', '#5f4a89'], ['#3b3749', '#bd93a2', '#756078'], ['#2c354a', '#778fc0', '#465578']];
  const [bg, mid, dark] = palettes[index % palettes.length];
  return <svg viewBox="0 0 160 120" aria-hidden="true" style={{ width: large ? '100%' : 66, height: large ? 'auto' : 56, display: 'block', borderRadius: 10, flexShrink: 0 }}>
    <rect width="160" height="120" fill={bg} /><circle cx="112" cy="36" r="20" fill="#e8dbff" />
    <path d="M0 96L65 30L122 120H0Z" fill={mid} /><path d="M160 45L78 120H160Z" fill={dark} />
    <path d="M0 105Q75 75 160 105V120H0Z" fill={dark} opacity=".42" />
    <path d="M0 114Q85 88 160 120" fill="none" stroke="#cab9ee" strokeWidth="1" opacity=".5" />
  </svg>;
}
function SectionLabel({ number, children, extra }) {
  return <Flex justify="space-between" align="center" style={{ marginBottom: 12 }}><Flex align="center" gap={8}><Text type="secondary" style={{ fontSize: 10 }}>{number}</Text><Text strong style={{ fontSize: 12 }}>{children}</Text></Flex>{extra}</Flex>;
}

export default function Generate() {
  const { catalog, tasks, refresh, identity, navigate, generationDraft, setGenerationDraft } = useStudio();
  const { message, modal } = App.useApp();
  const screens = Grid.useBreakpoint();
  const [selection, setSelection] = useState(generationDraft ? `${generationDraft.model_id}|${generationDraft.model_revision}` : ''), [prompt, setPrompt] = useState('');
  const [negativePrompt, setNegativePrompt] = useState(''), [count, setCount] = useState(1);
  const [lora, setLora] = useState([]), [scale, setScale] = useState(1), [files, setFiles] = useState([]);
  const [ratio, setRatio] = useState(''), [size, setSize] = useState(''), [busy, setBusy] = useState(false), [error, setError] = useState('');
  const [steps, setSteps] = useState(null), [guidance, setGuidance] = useState(null), [seed, setSeed] = useState(null), [loraScales, setLoraScales] = useState({});
  const [advancedOpen, setAdvancedOpen] = useState(false);
  const [previewTaskId, setPreviewTaskId] = useState(null);
  const [taskId, setTaskId] = useState(null);
  const model = catalog.models.find(m => modelKey(m) === selection) || catalog.models.find(m => m.status !== 'unavailable') || catalog.models[0];
  const selectedKey = model ? modelKey(model) : '';
  const g = model?.configuration.generation;
  const generationSignature = JSON.stringify(g);
  const choices = useMemo(() => {
    const options = sizeChoices(g);
    const [w, h] = size.split('x').map(Number);
    if (w && h && w % 16 === 0 && h % 16 === 0 && w % g?.dimension_multiple === 0 && h % g?.dimension_multiple === 0 && w >= g.width_min && w <= g.width_max && h >= g.height_min && h <= g.height_max && w * h <= g.max_pixels && !options.some(r => r.sizes.some(s => s.value === size))) options.push({ ratio: '原始尺寸', rw: w, rh: h, sizes: [{ value: size, label: `${w} × ${h}` }] });
    return options;
  }, [generationSignature, size]); // Catalogue polling must not reset a user's size selection.
  const compatibleLoras = (model?.loras || []).filter(l => (!l.model_id || l.model_id === model.model_id) && (!l.base_model_revision || l.base_model_revision === model.revision));
  const ref = model?.configuration.reference;
  useEffect(() => {
    setLora([]); setScale(1); setLoraScales({}); setSteps(g?.default_steps); setGuidance(g?.default_guidance_scale); setSeed(g?.default_seed ?? null); setCount(1); setError('');
    const options = sizeChoices(g); const preferred = options.find(r => r.isDefault) || options[0];
    setRatio(preferred?.ratio || ''); setSize(preferred?.sizes.find(s => s.isDefault)?.value || preferred?.sizes[0]?.value || '');
  }, [selectedKey]);
  useEffect(() => {
    if (!generationDraft || !catalog.models.length) return;
    const draft = generationDraft;
    const sourceModel = catalog.models.find(m => m.model_id === draft.model_id && m.revision === draft.model_revision);
    if (!sourceModel) { setError('原模型版本已不可用，请重新选择模型。'); setGenerationDraft(null); return; }
    if (selectedKey !== modelKey(sourceModel)) { setSelection(modelKey(sourceModel)); return; }
    const options = sizeChoices(sourceModel.configuration.generation);
    const requested = draft.parameters.size;
    const matching = options.find(r => r.sizes.some(s => s.value === requested));
    setPrompt(draft.prompt); setNegativePrompt(draft.negative_prompt || '');
    setRatio(matching?.ratio || '默认'); setSize(requested);
    setCount(draft.parameters.n || draft.files.length || 1);
    setSteps(draft.parameters.num_inference_steps); setGuidance(draft.parameters.guidance_scale);
    setSeed(draft.parameters.seed ?? null); setLora(draft.loras.map(l => l.lora_id));
    setLoraScales(Object.fromEntries(draft.loras.map(l => [l.lora_id, l.scale])));
    setFiles((draft.references || []).map((f, i) => ({ uid: f.id, name: `参考图片 ${i + 1}`, status: 'done', url: f.url, type: f.mime, size: f.size, remote: true })));
    setGenerationDraft(null); message.success('已复制生成参数，可修改后再生成');
  }, [generationDraft, catalog.models.length, selectedKey]);
  const currentRatio = choices.find(r => r.ratio === ratio && r.sizes.some(s => s.value === size)) || choices.find(r => r.sizes.some(s => s.value === size)) || choices.find(r => r.isDefault) || choices[0];
  const currentSize = currentRatio?.sizes.some(s => s.value === size) ? size : currentRatio?.sizes.find(s => s.isDefault)?.value || currentRatio?.sizes[0]?.value || '';
  const selectedLoras = compatibleLoras.filter(l => lora.includes(l.lora_id));
  const task = tasks.find(t => t.id === taskId) || (!taskId ? tasks[0] : null);
  const queueFull = catalog.queue_count >= catalog.max_count;
  const referenceError = files.length > (ref?.max_count || 0) || files.length < (ref?.min_count || 0) ? '参考文件数量不符合此模型要求。' : files.some(f => !ref?.mime_types.includes(f.type) || f.size > ref.max_file_bytes) ? '参考文件类型或大小不符合此模型要求。' : '';
  const blocked = !model || !currentSize || model.status === 'unavailable' || queueFull || !!referenceError;
  function changeModel(m) { setSelection(modelKey(m)); setLora([]); setScale(1); setError(''); }
  function changeRatio(key) {
    setRatio(key); const options = choices.find(r => r.ratio === key)?.sizes || [];
    setSize(options.find(s => s.value.startsWith('1024x') || s.value.endsWith('x1024'))?.value || options[0]?.value || '');
  }
  async function submit() {
    if (blocked || busy) return;
    if (!prompt.trim()) { setError('请输入提示词。'); return; }
    setBusy(true); setError('');
    try {
      const references = [];
      for (const item of files) { const data = new FormData(); data.append('file', item.remote ? new File([await (await request(item.url)).blob()], 'reference.png', { type: item.type }) : item.originFileObj || item); references.push((await api('/api/uploads', { method: 'POST', body: data })).uri); }
      const result = await send('/api/tasks', 'POST', { model: model.model_id, revision: model.revision, prompt: prompt.trim(), size: currentSize, image: references, n: Math.min(count, g?.max_count || 1), negative_prompt: negativePrompt, num_inference_steps: steps ?? g.default_steps, guidance_scale: guidance ?? g.default_guidance_scale, ...(seed != null ? { seed } : {}), lora: selectedLoras.map(l => ({ lora_id: l.lora_id, scale: loraScales[l.lora_id] ?? scale })) });
      setTaskId(result.id); setFiles([]); await refresh(); message.success('已加入生成队列');
    } catch (e) { setError(e.message); } finally { setBusy(false); }
  }
  const retry = () => modal.confirm({ title: '确认重新生成？', content: '原执行可能已完成，重新生成可能产生重复结果。', onOk: async () => { try { await send('/api/admin/tasks/' + task.id + '/retry', 'POST', {}); await refresh(); } catch (e) { message.error(e.message); } } });
  return <>
    <Row gutter={[26, 24]} align="top">
      <Col xs={24} lg={12} xl={10} xxl={8}>
        <Card styles={{ body: { padding: screens.md ? 20 : 16 } }} style={{ borderColor: '#2c2d3c', boxShadow: '0 8px 24px #00000012' }}>
          <Form className="ig-compact-form" id="ig-generate-form" onFinish={submit}>
            <div className="ig-prompt-section"><SectionLabel number="01" extra={<Text type="secondary" style={{ fontSize: 10 }}>{prompt.length} / 4000</Text>}>画面描述</SectionLabel>
              <Input.TextArea id="ig-prompt" aria-label="提示词" placeholder="描述画面、光线与风格…" value={prompt} onChange={e => setPrompt(e.target.value)} maxLength={4000} autoSize={{ minRows: 3, maxRows: 5 }} style={{ lineHeight: 1.75, padding: '10px 12px', background: '#14151e' }} />
              <Text className="ig-compact-label">负向提示词</Text><Input.TextArea id="ig-negative-prompt" aria-label="负向提示词" value={negativePrompt} onChange={e => { setNegativePrompt(e.target.value); if (e.target.value.trim() && (guidance ?? 1) <= 1 && model?.configuration.architecture === 'qwen_image_21') setGuidance(2); }} maxLength={4000} autoSize={{ minRows: 2, maxRows: 3 }} placeholder="不希望出现的内容，例如模糊、水印" style={{ lineHeight: 1.7, padding: '8px 12px' }} />
              {(ref?.supported || files.length > 0) && <div id="ig-reference-field" className="ig-compact-reference"><Flex justify="space-between" align="center" gap={8}><Text type="secondary" style={{ fontSize: 11 }}>参考图片 · 可选</Text><Text type="secondary" style={{ fontSize: 10 }}>{ref?.supported ? `最多 ${ref.max_count} 张 · ${Math.round(ref.max_file_bytes / 1024 / 1024)} MB/张` : '当前模型不支持参考图'}</Text></Flex><Upload id="ig-reference" accept={ref?.mime_types.join(',')} multiple beforeUpload={() => false} fileList={files} onChange={({ fileList }) => setFiles(fileList)} disabled={!ref?.supported} listType="picture"><Button size="small" icon={icon(ImagePlus, 14)}>添加参考图片</Button></Upload></div>}
            </div>
            <div className="ig-compact-section ig-resource-section"><SectionLabel number="02">模型与风格</SectionLabel>
              <div id="ig-model-cards" className="ig-compact-models" role="group" aria-label="选择模型">
                {catalog.models.map((m, index) => <Button key={modelKey(m)} data-model={modelKey(m)} aria-pressed={selectedKey === modelKey(m)} onClick={() => changeModel(m)} className={`ig-compact-model ${selectedKey === modelKey(m) ? 'is-selected' : ''}`}><span className={`ig-model-emblem ig-model-emblem-${index % 3}`}>{m.configuration.display_name.slice(0, 1).toUpperCase()}</span><span className="ig-model-name">{m.configuration.display_name}<small>{catalog.models.filter(n => n.model_id === m.model_id).length > 1 ? m.revision : m.configuration.type === 'video' ? '视频模型' : '图像模型'}</small></span><span className="ig-model-check">{selectedKey === modelKey(m) && <Check size={11} />}</span></Button>)}
                {!catalog.models.length && <Empty image={Empty.PRESENTED_IMAGE_SIMPLE} description="暂无模型，请联系管理员接入后端。" />}
              </div>
              <div className="ig-compact-lora"><Flex align="center" gap={6}><Layers3 size={13} /><Text>LoRA 风格</Text><Text type="secondary">可选</Text></Flex><Select id="ig-lora" aria-label="LoRA" mode="multiple" maxTagCount="responsive" maxCount={model?.configuration.lora.max_count || 1} value={selectedLoras.map(l => l.lora_id)} placeholder={compatibleLoras.length && model?.configuration.lora.supported ? '选择适合画面的风格' : '此模型暂无可选风格'} onChange={setLora} disabled={!compatibleLoras.length || !model?.configuration.lora.supported} options={compatibleLoras.map(l => ({ value: l.lora_id, label: l.display_name || l.lora_id }))} /></div>
            </div>
            <div className="ig-compact-section"><SectionLabel number="03">画面尺寸</SectionLabel>
              <div id="ig-ratios" className="ig-compact-ratios" role="group" aria-label="画面比例">{choices.map(r => <Button key={r.ratio} data-ratio={r.ratio} aria-pressed={currentRatio?.ratio === r.ratio} onClick={() => changeRatio(r.ratio)} className={currentRatio?.ratio === r.ratio ? 'is-selected' : ''}><span aria-hidden="true" style={{ width: Math.round(15 * r.rw / Math.max(r.rw, r.rh)), height: Math.round(15 * r.rh / Math.max(r.rw, r.rh)) }} />{r.ratio}</Button>)}</div>
              <div className="ig-size-count-row"><div><Text className="ig-compact-label">分辨率</Text><Select id="ig-resolution" aria-label="分辨率" value={currentSize || undefined} onChange={setSize} options={currentRatio?.sizes || []} /></div><div><Text className="ig-compact-label">张数</Text><Select id="ig-count" aria-label="生成张数" value={Math.min(count, g?.max_count || 1)} onChange={setCount} options={Array.from({ length: g?.max_count || 1 }, (_, i) => ({ value: i + 1, label: `${i + 1} 张` }))} /></div></div>
            </div>
            <div id="ig-advanced"><Collapse className="ig-compact-advanced" ghost activeKey={advancedOpen ? ['parameters'] : []} onChange={keys => setAdvancedOpen(keys.includes('parameters'))} items={[{ key: 'parameters', label: <Flex justify="space-between" align="center"><Text style={{ fontSize: 12 }}>高级参数</Text><Text type="secondary" style={{ fontSize: 10 }}>LoRA 强度 · Steps · Guidance · Seed</Text></Flex>, forceRender: true, children: <>
              {selectedLoras.map(l => <Flex key={l.lora_id} vertical gap={2} style={{ marginBottom: 8 }}><Flex justify="space-between"><Text type="secondary" style={{ fontSize: 11 }}>{l.display_name || l.lora_id} · 强度</Text><Text style={{ fontSize: 11 }}>{(loraScales[l.lora_id] ?? scale).toFixed(2)}</Text></Flex><Slider aria-label={`${l.lora_id} 强度`} min={0} max={2} step={.05} value={loraScales[l.lora_id] ?? scale} onChange={value => setLoraScales(previous => ({ ...previous, [l.lora_id]: value }))} /></Flex>)}
              <div className="ig-advanced-values"><div><Text className="ig-compact-label">Steps</Text><InputNumber id="ig-steps" aria-label="Steps" min={1} max={g?.steps_max || 100} precision={0} value={steps} onChange={setSteps} /></div><div><Text className="ig-compact-label">Guidance</Text><InputNumber id="ig-guidance" aria-label="Guidance" min={0} max={20} step={.1} value={guidance} onChange={setGuidance} /></div><div><Text className="ig-compact-label">Seed</Text><InputNumber id="ig-seed" aria-label="Seed" min={0} max={4294967295} precision={0} placeholder="随机" value={seed} onChange={setSeed} /></div></div>
            </> }]} /></div>
            {(error || referenceError || queueFull || model?.status === 'unavailable') && <Alert id="ig-submit-error" type="error" showIcon title={error || referenceError || (queueFull ? '等待队列已满，请稍后再试。' : '此模型暂不可用。')} style={{ marginTop: 12 }} />}
            <Button id="ig-submit" type="primary" htmlType="submit" block loading={busy} disabled={blocked} icon={icon(WandSparkles, 17)} style={{ height: 42, marginTop: 18, background: blocked ? undefined : '#7664ce', borderColor: blocked ? undefined : '#7664ce' }}>{queueFull ? '队列已满' : model?.configuration.type === 'video' ? '生成视频' : '生成图片'}</Button>
          </Form>
        </Card>
      </Col>
      <Col xs={24} lg={12} xl={14} xxl={16}>
        <Flex justify="space-between" align="center" style={{ marginBottom: 14 }}><Flex align="center" gap={8}>{icon(Maximize2, 14)}<Text strong style={{ fontSize: 12 }}>作品预览</Text></Flex><Flex align="center" gap={10}><Text type="secondary" style={{ fontSize: 10 }}>{task ? taskNames[task.status] : 'YOUR NEXT CREATION'}</Text><Tag id="ig-queue" variant="filled" style={{ margin: 0, background: '#242333', color: '#aaa0c8' }}>等待 {catalog.queue_count} / {catalog.max_count}</Tag></Flex></Flex>
        <Card id="ig-result" data-signature={task ? task.id + '|' + task.status : ''} styles={{ body: { padding: screens.md ? 26 : 20 } }} style={{ background: '#161720', minHeight: screens.xl ? 580 : screens.lg ? 460 : 340, borderColor: '#2c2d3c' }}>
          {task ? <Flex vertical align="center" gap={18}>
            {!task.files.length && <Flex vertical align="center" justify="center" gap={18} style={{ minHeight: 310 }}><Spin spinning={!['failed', 'unknown'].includes(task.status)} /><Text strong>{taskNames[task.status]}</Text><Text type="secondary" style={{ fontSize: 11 }}>任务 {task.id.slice(0, 8)}</Text></Flex>}
            {task.files.length > 0 && (task.files[0].mime.startsWith('video/') ? <Flex vertical gap={16} align="center" style={{ width: '100%' }}><video controls src={task.files[0].url} style={{ width: '100%', maxHeight: 570, borderRadius: 12 }} /><Button href={task.files[0].url} download={task.files[0].storage_key.split('/').pop()} icon={icon(Download)}>下载视频</Button></Flex> : <div className="ig-result-preview">
              <button type="button" id="ig-open-result" className="ig-result-cover" aria-label="查看生成结果" onClick={() => setPreviewTaskId(task.id)}><img src={task.files[0].url} alt="生成结果" /><span className="ig-result-zoom">{icon(Maximize2, 16)} 查看作品</span>{task.files.length > 1 && <span className="ig-image-count">{icon(Layers3, 13)} {task.files.length}</span>}</button>
              <Flex className="ig-result-actions" justify="space-between" align="center" gap={12}><Text type="secondary" style={{ fontSize: 11 }}>共 {task.files.length} 张 · {task.parameters.size}</Text><Button id="ig-result-download" icon={icon(Download, 15)} onClick={() => setPreviewTaskId(task.id)}>查看与下载</Button></Flex>
            </div>)}
            {task.error && <Alert type="error" title={task.error} showIcon style={{ width: '100%' }} />}
            {task.status === 'unknown' && identity.admin && <Button onClick={retry}>确认重新生成</Button>}
          </Flex> : <Flex vertical align="center" justify="center" gap={15} style={{ minHeight: screens.xl ? 526 : 350 }}>
            <Flex style={{ width: 235, maxWidth: '80%', padding: 18, background: '#262335', borderRadius: 24, transform: 'rotate(-5deg)', marginBottom: 10 }}><ModelArt index={0} large /></Flex>
            <Title level={4} style={{ fontSize: 17, margin: 0 }}>留白，是创作的开始。</Title><Text type="secondary" style={{ fontSize: 12, textAlign: 'center' }}>描述一个想法，你的下一幅作品将在这里出现。</Text><Flex gap={6} align="center" style={{ marginTop: 14, color: '#7c7596' }}>{icon(Sparkles, 13)}<Text style={{ fontSize: 10, color: '#7c7596', letterSpacing: 2 }}>FROM WORDS TO WORLDS</Text></Flex>
          </Flex>}
        </Card>
        <Flex justify="space-between" style={{ margin: '26px 0 14px' }}><Flex gap={8} align="center"><Text strong style={{ fontSize: 12 }}>最近创作</Text><Text type="secondary" style={{ fontSize: 10 }}>最近 5 次</Text></Flex><Button id="ig-view-all" type="link" size="small" onClick={() => navigate('gallery')}>查看全部作品 →</Button></Flex>
        <Flex id="ig-task-list" vertical gap={8}>{tasks.slice(0, 5).map(t => <Button key={t.id} data-task={t.id} onClick={() => setTaskId(t.id)} type="text" style={{ height: 'auto', padding: '12px 14px', background: task?.id === t.id ? '#28243a' : '#1a1b2599', textAlign: 'left', whiteSpace: 'normal' }}><Flex align="center" justify="space-between" gap={12} style={{ width: '100%' }}><Flex vertical gap={3} style={{ minWidth: 0, flex: 1 }}><Text ellipsis style={{ fontSize: 12 }}>{t.prompt || t.model_id}</Text><Text type="secondary" style={{ fontSize: 10 }}>{t.model_id} · {dateLabel(t.created_at)}</Text></Flex><Tag variant="filled" color={t.status === 'succeeded' ? 'success' : t.status === 'failed' ? 'error' : 'default'} style={{ margin: 0, fontSize: 10 }}>{taskNames[t.status]}</Tag></Flex></Button>)}{!tasks.length && <Text type="secondary" style={{ fontSize: 12, padding: 14 }}>还没有作品，开始你的第一次创作吧。</Text>}</Flex>
      </Col>
    </Row>
    <ArtworkModal task={tasks.find(t => t.id === previewTaskId) || null} onClose={() => setPreviewTaskId(null)} onDeleted={id => { if (taskId === id) setTaskId(null); }} />
  </>;
}
