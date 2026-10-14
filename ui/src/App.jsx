import { createContext, lazy, Suspense, useContext, useEffect, useState, useCallback, useRef } from 'react';
import { Alert, App as AntApp, Avatar, Button, Card, Divider, Flex, Form, Grid, Input, Layout, Spin, Tag, Typography } from 'antd';
import { ArrowLeft, ArrowUpRight, Boxes, ChevronRight, History, KeyRound, Layers3, LogOut, Settings2, ShieldCheck, Sparkles, UserRound, Users } from 'lucide-react';
import { api, roles, send } from './lib.js';
import Generate from './Generate.jsx';
const Gallery = lazy(() => import('./Gallery.jsx'));
const adminPage = name => lazy(() => import('./admin.jsx').then(module => ({ default: module[name] })));
const Backends = adminPage('Backends'), ModelDefaults = adminPage('ModelDefaults'), HistoryPage = adminPage('HistoryPage'), Keys = adminPage('Keys'), UsersPage = adminPage('UsersPage'), SettingsPage = adminPage('SettingsPage');

const { Text, Title } = Typography;
export const StudioContext = createContext(null);
export const useStudio = () => useContext(StudioContext);
const adminSurface = location.pathname.replace(/\/$/, '') === '/admin';
const navigation = [
  ['generate', '创作工作台', Sparkles], ['gallery', '我的作品', History], ['backends', '模型后端', Boxes],
  ['models', '模型参数', Settings2], ['loras', 'LoRA 关联', Layers3],
  ['history', '生成记录', History], ['keys', 'API Key', KeyRound],
  ['users', '用户管理', Users], ['settings', '系统参数', Settings2], ['account', '我的账号', UserRound],
];
const adminPages = ['backends', 'models', 'loras', 'history', 'keys', 'users', 'settings'];
const userPages = ['generate', 'gallery', 'account'];
export const icon = (Icon, size = 16) => <Icon size={size} strokeWidth={1.7} />;
export function PageHeading({ eyebrow, title, description, extra }) {
  return <Flex justify="space-between" align="center" gap={16} wrap style={{ marginBottom: 28 }}>
    <Flex vertical gap={6}><Text type="secondary" style={{ fontSize: 10, letterSpacing: 2.4 }}>{eyebrow}</Text>
      <Title level={2} style={{ margin: 0, fontSize: 28, letterSpacing: -.6 }}>{title}</Title>
      <Text type="secondary">{description}</Text>
    </Flex>{extra}
  </Flex>;
}

export default function App() {
  const [identity, setIdentity] = useState(null);
  const [checking, setChecking] = useState(true);
  const [notice, setNotice] = useState('');
  const [sessionKey, setSessionKey] = useState(0);
  const logout = useCallback(() => { setIdentity(null); setSessionKey(k => k + 1); }, []);
  const acceptIdentity = useCallback(user => {
    if (adminSurface && !user.admin) { location.replace('/'); return; }
    if (!adminSurface && new URLSearchParams(location.search).get('next') === '/admin' && user.admin) { location.replace('/admin'); return; }
    setIdentity(user);
  }, []);
  useEffect(() => {
    let active = true;
    api('/api/session').then(user => { if (active) acceptIdentity(user); }).catch(() => {}).finally(() => { if (active) setChecking(false); });
    window.addEventListener('frame:unauthorized', logout);
    return () => { active = false; window.removeEventListener('frame:unauthorized', logout); };
  }, [acceptIdentity, logout]);
  if (checking) return <Flex align="center" justify="center" style={{ minHeight: '100vh', background: '#111219' }}><Spin size="large" /></Flex>;
  if (!identity) return <Login notice={notice} onLogin={acceptIdentity} />;
  return <Workspace key={sessionKey} identity={identity} setIdentity={acceptIdentity} onLogout={logout} passwordChanged={() => { setNotice('密码已更新，请重新登录。'); logout(); }} />;
}

function Login({ onLogin, notice }) {
  const screens = Grid.useBreakpoint();
  const [busy, setBusy] = useState(false), [error, setError] = useState('');
  const [form] = Form.useForm();
  async function submit(values) {
    setBusy(true); setError('');
    try { await send('/api/auth/login', 'POST', values); form.resetFields(); onLogin(await api('/api/session')); }
    catch (e) { setError(e.message); } finally { setBusy(false); }
  }
  return <Flex align="center" justify="center" style={{ minHeight: '100vh', background: '#111219', padding: 20 }}>
    <Card id="frame-login" styles={{ body: { padding: screens.md ? 40 : 26 } }} style={{ width: 440, maxWidth: '100%', border: 'none', boxShadow: '0 30px 90px #00000030' }}>
      <Flex vertical gap={8} style={{ marginBottom: 30 }}><Flex align="center" gap={10}>{icon(Sparkles, 24)}<Text strong style={{ letterSpacing: 4, fontSize: 20 }}>FRAME</Text></Flex>
        <Title level={3} style={{ margin: '18px 0 0' }}>欢迎回到创作空间</Title><Text type="secondary">使用账号和密码登录。账号由超级管理员创建。</Text></Flex>
      <Form id="frame-login-form" form={form} layout="vertical" onFinish={submit} requiredMark={false}>
        <Form.Item label="用户名" name="username" rules={[{ required: true, message: '请输入用户名' }]}><Input id="frame-username" autoComplete="username" placeholder="你的用户名" /></Form.Item>
        <Form.Item label="密码" name="password" rules={[{ required: true, message: '请输入密码' }]}><Input.Password id="frame-password" autoComplete="current-password" placeholder="输入密码" /></Form.Item>
        {(error || notice) && <Alert id="frame-login-error" type={error ? 'error' : 'success'} showIcon title={error || notice} style={{ marginBottom: 18 }} />}
        <Button type="primary" htmlType="submit" loading={busy} block icon={icon(ArrowUpRight)}>进入平台</Button>
      </Form>
      <Divider /><Text type="secondary" style={{ fontSize: 11 }}>A SPACE FOR YOUR NEXT IDEA.</Text>
    </Card>
  </Flex>;
}

function Workspace({ identity, setIdentity, onLogout, passwordChanged }) {
  const screens = Grid.useBreakpoint();
  const { message } = AntApp.useApp();
  const [catalog, setCatalog] = useState({ models: [], queue_count: 0, max_count: 100 });
  const [tasks, setTasks] = useState([]), [error, setError] = useState('');
  const [page, setPage] = useState((adminSurface ? adminPages : userPages).includes(location.hash.slice(1)) ? location.hash.slice(1) : adminSurface ? 'backends' : 'generate');
  const [generationDraft, setGenerationDraft] = useState(null);
  const [historyFilter, setHistoryFilter] = useState({ q: '', user_id: '' });
  const active = useRef(true), polling = useRef(false);
  const refresh = useCallback(async () => {
    if (polling.current) return;
    polling.current = true;
    try {
      const [models, records, session] = await Promise.all([api('/api/models'), api('/api/tasks?mine=true&limit=5'), api('/api/session')]);
      if (!active.current) return;
      setCatalog(models); setTasks(records.tasks); setIdentity(session); setError('');
    } catch (e) { if (active.current) setError(e.message); } finally { polling.current = false; }
  }, [setIdentity]);
  useEffect(() => {
    active.current = true; refresh(); const timer = setInterval(refresh, 5000);
    return () => { active.current = false; clearInterval(timer); };
  }, [refresh]);
  const visiblePages = navigation.filter(([name]) => adminSurface ? adminPages.includes(name) && (name !== 'users' || identity.role === 'super_admin') : userPages.includes(name));
  const currentPage = visiblePages.some(([name]) => name === page) ? page : adminSurface ? 'backends' : 'generate';
  useEffect(() => { document.title = adminSurface ? 'FRAME 管理后台' : 'FRAME 创作工作台'; }, []);
  const navigate = name => { setPage(name); history.replaceState(null, '', '#' + name); };
  async function logout() {
    // Unmount the workspace before the server round-trip, clearing all sensitive form state.
    onLogout();
    try { await send('/api/auth/logout', 'POST', {}); } catch (e) { message.error(e.message); }
  }
  const context = { generationDraft, setGenerationDraft, identity, catalog, tasks, refresh, navigate, historyFilter, setHistoryFilter, setIdentity, passwordChanged };
  return <StudioContext.Provider value={context}>
    <Layout id="dgx-studio" style={{ minHeight: '100vh', maxWidth: 1920, margin: 'auto', background: '#111219' }}>
      {screens.lg && <Layout.Sider width={216} style={{ background: '#13141d', padding: '34px 18px', position: 'sticky', top: 0, height: '100vh', overflowY: 'auto' }}><Flex vertical style={{ minHeight: '100%' }}>
        <Flex align="center" gap={12} style={{ padding: '0 16px 44px', color: '#c3baff' }}>{icon(Sparkles, 25)}<Text strong style={{ color: '#f1eefb', letterSpacing: 4, fontSize: 21 }}>FRAME</Text></Flex>
        <Text style={{ color: '#727185', paddingLeft: 16, fontSize: 10, letterSpacing: 2 }}>{adminSurface ? 'MANAGEMENT' : 'CREATIVE SPACE'}</Text>
        <Flex vertical gap={7} style={{ marginTop: 18 }}>{visiblePages.map(([name, label, Icon]) => <Button key={name} data-page={name} aria-pressed={currentPage === name} onClick={() => navigate(name)} icon={icon(Icon)} type="text" style={{ height: 46, textAlign: 'left', justifyContent: 'flex-start', color: currentPage === name ? '#d8d0ff' : '#9392a7', background: currentPage === name ? '#ffffff12' : 'transparent', fontWeight: currentPage === name ? 600 : 400 }}>{label}</Button>)}</Flex>
        <Flex vertical gap={14} style={{ marginTop: 'auto', padding: '60px 16px 0' }}><Divider style={{ borderColor: '#ffffff13', margin: 0 }} /><Text style={{ color: '#737186', fontSize: 10, letterSpacing: 1.8 }}>MAKE SOMETHING<br />WORTH IMAGINING.</Text><Flex align="center" gap={8} style={{ color: '#737186' }}>{icon(ShieldCheck, 13)}<Text style={{ color: '#737186', fontSize: 10 }}>你的专属创作空间</Text></Flex></Flex>
      </Flex></Layout.Sider>}
      <Layout style={{ minWidth: 0, background: 'transparent' }}>
        <Flex align="center" justify="space-between" wrap gap={14} style={{ padding: screens.md ? '14px 26px' : '12px 16px', background: '#1a1b25', borderBottom: '1px solid #282a39' }}>
          <Text strong style={{ fontSize: 15 }}>{visiblePages.find(([name]) => name === currentPage)?.[1]}</Text>
          <Flex align="center" gap={10} wrap><Avatar size={30} style={{ background: '#29263d', color: '#b9adff' }}>{identity.username.slice(0, 1).toUpperCase()}</Avatar><Text id="frame-identity" style={{ fontSize: 12 }}>{identity.username} · {roles[identity.role]}</Text>
            {adminSurface ? <Button id="frame-studio-link" href="/" size="small" icon={icon(ArrowLeft, 13)}>返回推理平台</Button> : identity.admin && <Button id="frame-admin-link" href="/admin" size="small" icon={icon(ArrowUpRight, 13)}>管理后台</Button>}
            <Button id="frame-logout" type="text" size="small" icon={icon(LogOut, 14)} onClick={logout}>退出</Button>
          </Flex>
        </Flex>
        {!screens.lg && <Flex wrap gap={4} style={{ padding: '12px 14px', background: '#13141d' }}>{visiblePages.map(([name, label, Icon]) => <Button key={name} data-page={name} aria-pressed={currentPage === name} onClick={() => navigate(name)} icon={icon(Icon, 14)} type="text" size="small" style={{ color: currentPage === name ? '#d8d0ff' : '#9392a7', background: currentPage === name ? '#ffffff12' : 'transparent' }}>{label}</Button>)}</Flex>}
        <Layout.Content style={{ padding: screens.xl ? '24px 30px' : screens.md ? '22px 24px' : '20px 16px', minWidth: 0 }}>
          {error && <Alert type="error" showIcon title={error} style={{ marginBottom: 20 }} />}
          <Flex vertical data-screen={currentPage}><Suspense fallback={<Spin style={{ padding: 50 }} />}>
            {currentPage === 'generate' && <Generate />}
            {currentPage === 'gallery' && <Gallery />}
            {currentPage === 'backends' && <Backends />}
            {currentPage === 'models' && <ModelDefaults />}
            {currentPage === 'loras' && <ModelDefaults loraOnly />}
            {currentPage === 'history' && <HistoryPage />}
            {currentPage === 'keys' && <Keys />}
            {currentPage === 'users' && <UsersPage />}
            {currentPage === 'settings' && <SettingsPage />}
            {currentPage === 'account' && <Account />}
          </Suspense></Flex>
        </Layout.Content>
        <Flex justify="space-between" gap={12} wrap style={{ borderTop: '1px solid #282a39', padding: '16px 26px' }}><Text id="ig-cluster-foot" type="secondary" style={{ fontSize: 11 }}>模型与 LoRA 自动同步</Text><Text type="secondary" style={{ fontSize: 10, letterSpacing: 1.6 }}>FRAME · CREATE WITH INTENTION</Text></Flex>
      </Layout>
    </Layout>
  </StudioContext.Provider>;
}

function Account() {
  const { passwordChanged } = useStudio();
  const [busy, setBusy] = useState(false), [error, setError] = useState('');
  async function submit(values) {
    setBusy(true); setError('');
    try { await send('/api/auth/password', 'POST', values); passwordChanged(); }
    catch (e) { setError(e.message); } finally { setBusy(false); }
  }
  return <><PageHeading eyebrow="YOUR ACCOUNT" title="我的账号" description="更新密码后，所有已登录会话将退出。" />
    <Card style={{ maxWidth: 540 }}><Form id="ig-password-form" layout="vertical" onFinish={submit} requiredMark={false}>
      <Form.Item label="当前密码" name="current_password" rules={[{ required: true }]}><Input.Password id="ig-current-password" autoComplete="current-password" /></Form.Item>
      <Form.Item label="新密码" name="password" rules={[{ required: true }, { min: 12, message: '密码至少需要 12 个字符' }]}><Input.Password id="ig-new-password" autoComplete="new-password" maxLength={256} /></Form.Item>
      {error && <Alert type="error" title={error} style={{ marginBottom: 16 }} />}
      <Button htmlType="submit" type="primary" loading={busy}>更新密码</Button>
    </Form></Card></>;
}
