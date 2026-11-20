// 真浏览器截图：起 MOCK 后台 + 无头 Edge，登录后逐页截图，用来肉眼核对版式
// 用法：node shots.js [输出目录]
const {spawn, execSync} = require('child_process');
const path = require('path');
const fs = require('fs');
const os = require('os');

const SRC = path.resolve(__dirname, '..', 'src');
const OUT = process.argv[2] || path.resolve(__dirname, '..', '.workbuddy', 'shots');
const EDGE = process.env.EDGE || 'C:\\Program Files (x86)\\Microsoft\\Edge\\Application\\msedge.exe';
const PORT = 8899;
// SHOT_BASE / SHOT_PW 指向已装好的机器时，不再本地起 MOCK 服务，直接对着线上截图
const REMOTE = process.env.SHOT_BASE || '';
const BASE = REMOTE || 'http://127.0.0.1:' + PORT;
const PW = process.env.SHOT_PW || 'SmokePass123!';

fs.mkdirSync(OUT, {recursive: true});

let TMP = '', env = process.env;
if (!REMOTE) {
  TMP = path.join(os.tmpdir(), 'ovpn-shot-' + Date.now());
  fs.mkdirSync(path.join(TMP, 'data'), {recursive: true});
  fs.writeFileSync(path.join(TMP, 'data', 'admin_password.txt'), PW);
  env = {...process.env, OVPN_WEB_ROOT: TMP, OVPN_WEB_MOCK: '1', OVPN_WEB_PORT: String(PORT)};
}
const seed = `
import sys
sys.path.insert(0, r"${SRC}")
import core
core.init_db(); core.set_meta("admin_hash", core.hash_password("${PW}"))
for i, n in enumerate(["alice","bob","carol","dave","erin","frank"]):
    core.add_user(n, real_name=["张爱丽","李博","王卡萝","赵大卫","钱一霖","孙方"][i],
                  dept=["运维部","财务部","运维部","外包","研发部","研发部"][i],
                  note="", password="Passw0rd!23")
core.add_asset_group("核心数据库","生产库 3 台",[("10.10.1.11","tcp","3306"),("10.10.1.12","tcp","3306"),("10.10.1.13","tcp","3306")])
core.add_asset_group("Web 前端","",[("10.10.2.0/24","tcp","80,443")])
core.add_asset_group("跳板机","",[("10.10.9.5","tcp","22")])
core.add_user_group("运维组","7x24 值班",["alice","carol","erin"])
core.add_user_group("财务组","",["bob","dave"])
for g in core.list_asset_groups():
    core.add_grant("group","运维组",g["id"])
core.add_grant("user","bob",[x for x in core.list_asset_groups() if x["name"]=="核心数据库"][0]["id"])
core.set_meta("public_addr","vpn.example.com"); core.set_meta("initialized",1)
core.set_meta("extra_routes",["10.10.0.0/16","10.20.0.0/16","10.30.0.0/16"])
core.write_all_config()
print("seeded")
`;
if (!REMOTE) {
  const seedPath = path.join(TMP, 'seed.py');
  fs.writeFileSync(seedPath, seed, 'utf8');
  execSync(`python "${seedPath}"`, {env, stdio: 'inherit'});
}

const sleep = ms => new Promise(r => setTimeout(r, ms));
const srv = REMOTE ? null : spawn('python', ['app.py'], {cwd: SRC, env, stdio: ['ignore', 'pipe', 'pipe']});
let srvlog = '';
if (srv) { srv.stdout.on('data', d => srvlog += d); srv.stderr.on('data', d => srvlog += d); }

async function waitUp() {
  for (let i = 0; i < 80; i++) {
    try { const r = await fetch(BASE + '/api/session'); if (r.ok) return; } catch (e) {}
    await sleep(250);
  }
  throw new Error('server not up\n' + srvlog);
}

let seq = 0, ws, pending = new Map();
function cdp(method, params, sessionId) {
  const id = ++seq;
  ws.send(JSON.stringify(sessionId ? {id, method, params, sessionId} : {id, method, params}));
  return new Promise((res, rej) => pending.set(id, {res, rej}));
}

(async () => {
  let edge;
  try {
    await waitUp();
    const prof = path.join(os.tmpdir(), 'edge-shot-prof-' + Date.now());
    edge = spawn(EDGE, ['--headless=new', '--disable-gpu', '--hide-scrollbars', '--no-first-run',
      '--remote-debugging-port=9222', '--user-data-dir=' + prof, 'about:blank'], {stdio: 'ignore'});

    let ver;
    for (let i = 0; i < 60; i++) {
      try { ver = await (await fetch('http://127.0.0.1:9222/json/version')).json(); break; } catch (e) { await sleep(300); }
    }
    if (!ver) throw new Error('Edge 调试端口没起来');
    ws = new WebSocket(ver.webSocketDebuggerUrl);
    await new Promise((res, rej) => { ws.onopen = res; ws.onerror = rej; });
    ws.onmessage = ev => {
      const m = JSON.parse(ev.data);
      if (m.id && pending.has(m.id)) {
        const p = pending.get(m.id); pending.delete(m.id);
        m.error ? p.rej(new Error(m.error.message)) : p.res(m.result);
      }
    };

    const {targetId} = await cdp('Target.createTarget', {url: BASE + '/'});
    const {sessionId} = await cdp('Target.attachToTarget', {targetId, flatten: true});
    await cdp('Page.enable', {}, sessionId);
    await cdp('Emulation.setDeviceMetricsOverride',
      {width: 1600, height: 1000, deviceScaleFactor: 1, mobile: false}, sessionId);
    await sleep(1200);

    const shot = async (name) => {
      const r = await cdp('Page.captureScreenshot', {format: 'png'}, sessionId);
      fs.writeFileSync(path.join(OUT, name + '.png'), Buffer.from(r.data, 'base64'));
      console.log('  ' + name + '.png');
    };
    const evalJs = async (expr) => {
      const r = await cdp('Runtime.evaluate', {expression: expr, awaitPromise: true, returnByValue: true}, sessionId);
      if (r.exceptionDetails) throw new Error(JSON.stringify(r.exceptionDetails).slice(0, 300));
      return r.result && r.result.value;
    };

    await shot('00-login');
    await evalJs(`(async()=>{document.querySelector('#lp').value='${PW}';await doLogin();return 1})()`);
    await sleep(1500);
    for (const p of ['overview', 'online', 'users', 'acl', 'settings', 'update', 'logs', 'route', 'security']) {
      await evalJs(`go('${p}')`);
      await sleep(p === 'acl' || p === 'settings' ? 1200 : 800);
      await shot('p-' + p);
    }
    // 资产与授权四个子页
    for (const [i, t] of ['ag', 'ug', 'gr', 'p2p'].entries()) {
      await evalJs(`aclTab('${t}')`);
      await sleep(700);
      await shot('acl-' + (i + 2) + '-' + t);
    }
    // 一个弹窗（账号编辑，内含明文密码框）看看表单排版
    await evalJs(`go('users');a=0`);
    await sleep(600);
    await evalJs(`userEdit('alice')`);
    await sleep(500);
    await shot('modal-pwd');
    console.log('截图输出：' + OUT);
  } catch (e) {
    console.error('截图失败:', e.message);
    process.exitCode = 1;
  } finally {
    try { srv.kill(); } catch (e) {}
    try { if (edge) edge.kill(); } catch (e) {}
    setTimeout(() => process.exit(process.exitCode || 0), 400);
  }
})();
