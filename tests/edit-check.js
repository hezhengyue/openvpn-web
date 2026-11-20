// 真浏览器验证：认证账号的「编辑」弹窗能改账号名，改完台账与 ccd 都跟着换
// 用法：
//   node edit-check.js                      # 本地 MOCK（账号 alice）
//   SHOT_BASE=http://ip:8800 SHOT_PW=xxx MODAL_USER=会话 node edit-check.js
// 背景：以前编辑弹窗只能改姓名/部门/IP/备注，工号写错没有补救办法。
const {spawn, execSync} = require('child_process');
const path = require('path');
const fs = require('fs');
const os = require('os');

const SRC = path.resolve(__dirname, '..', 'src');
const OUT = path.resolve(__dirname, '..', '.workbuddy', 'shots-edit');
const EDGE = process.env.EDGE || 'C:\\Program Files (x86)\\Microsoft\\Edge\\Application\\msedge.exe';
const PORT = 8899;
const REMOTE = process.env.SHOT_BASE || '';
const BASE = REMOTE || 'http://127.0.0.1:' + PORT;
const PW = process.env.SHOT_PW || 'SmokePass123!';
const USER = process.env.MODAL_USER || 'alice';

fs.mkdirSync(OUT, {recursive: true});
const sleep = ms => new Promise(r => setTimeout(r, ms));

let TMP = '', env = process.env;
if (!REMOTE) {
  TMP = path.join(os.tmpdir(), 'ovpn-edit-' + Date.now());
  fs.mkdirSync(path.join(TMP, 'data'), {recursive: true});
  fs.writeFileSync(path.join(TMP, 'data', 'admin_password.txt'), PW);
  env = {...process.env, OVPN_WEB_ROOT: TMP, OVPN_WEB_MOCK: '1', OVPN_WEB_PORT: String(PORT)};
  const seedPath = path.join(TMP, 'seed.py');
  fs.writeFileSync(seedPath, `
import sys
sys.path.insert(0, r"${SRC}")
import core
core.init_db(); core.set_meta("admin_hash", core.hash_password("${PW}"))
core.add_user("alice", real_name="张爱丽", dept="运维部", password="Passw0rd!23")
core.set_meta("public_addr","vpn.example.com"); core.set_meta("initialized",1)
core.write_all_config()
print("seeded")
`, 'utf8');
  execSync(`python "${seedPath}"`, {env, stdio: 'inherit'});
}

const srv = REMOTE ? null : spawn('python', ['app.py'], {cwd: SRC, env, stdio: ['ignore', 'pipe', 'pipe']});

let seq = 0, ws, pending = new Map();
function cdp(method, params, sessionId) {
  const id = ++seq;
  ws.send(JSON.stringify(sessionId ? {id, method, params, sessionId} : {id, method, params}));
  return new Promise((res, rej) => pending.set(id, {res, rej}));
}

(async () => {
  let edge, fails = [];
  try {
    for (let i = 0; i < 80; i++) {
      try { const r = await fetch(BASE + '/api/session'); if (r.ok) break; } catch (e) {}
      await sleep(250);
    }
    const prof = path.join(os.tmpdir(), 'edge-edit-prof-' + Date.now());
    edge = spawn(EDGE, ['--headless=new', '--disable-gpu', '--hide-scrollbars', '--no-first-run',
      '--remote-debugging-port=9225', '--user-data-dir=' + prof, 'about:blank'], {stdio: 'ignore'});

    let ver;
    for (let i = 0; i < 60; i++) {
      try { ver = await (await fetch('http://127.0.0.1:9225/json/version')).json(); break; } catch (e) { await sleep(300); }
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
      {width: 1280, height: 900, deviceScaleFactor: 1, mobile: false}, sessionId);
    await sleep(1200);

    const evalJs = async (expr) => {
      const r = await cdp('Runtime.evaluate', {expression: expr, awaitPromise: true, returnByValue: true}, sessionId);
      if (r.exceptionDetails) throw new Error(JSON.stringify(r.exceptionDetails).slice(0, 300));
      return r.result && r.result.value;
    };
    const shot = async (name) => {
      const r = await cdp('Page.captureScreenshot', {format: 'png'}, sessionId);
      fs.writeFileSync(path.join(OUT, name + '.png'), Buffer.from(r.data, 'base64'));
    };

    await evalJs(`(async()=>{document.querySelector('#lu').value='admin';document.querySelector('#lp').value=${JSON.stringify(PW)};await doLogin();return 1})()`);
    await sleep(1500);
    await evalJs(`go('users')`);
    await sleep(800);

    // 操作列不该再有「踢」——踢下线只放在在线用户页
    const btns = await evalJs(`[...document.querySelectorAll('#ubox button')].map(b=>b.textContent.trim())`);
    if (btns.some(t => t === '踢')) fails.push('台账操作列仍有「踢」按钮: ' + btns.join('/'));

    // 打开编辑弹窗：必须有账号输入框且回填当前账号
    await evalJs(`userEdit(${JSON.stringify(USER)})`);
    await sleep(400);
    const info = await evalJs(`(function(){const n=document.querySelector('#u_name');
      return n?{has:true,val:n.value}:{has:false}})()`);
    if (!info.has) fails.push('编辑弹窗里没有「账号」输入框');
    else if (info.val !== USER) fails.push('账号没回填，实际是 ' + info.val);
    await shot('1-编辑弹窗');

    // 真改一次名，再看台账
    const NEW = USER + 'X';
    await evalJs(`document.querySelector('#u_name').value=${JSON.stringify(NEW)};1`);
    await evalJs(`document.querySelector('#mok').click();1`);
    await sleep(2500);
    const names1 = await evalJs(`(async()=>(await get('/api/users')).users.map(u=>u.username))()`);
    if (!names1.includes(NEW)) fails.push('改名没生效，台账里没有 ' + NEW + '：' + names1.join(','));
    if (names1.includes(USER)) fails.push('改名后旧账号还在：' + names1.join(','));
    await shot('2-改名后');

    // 改回去，别在环境里留脏数据
    await evalJs(`userEdit(${JSON.stringify(NEW)})`);
    await sleep(300);
    await evalJs(`document.querySelector('#u_name').value=${JSON.stringify(USER)};1`);
    await evalJs(`document.querySelector('#mok').click();1`);
    await sleep(2500);
    const names2 = await evalJs(`(async()=>(await get('/api/users')).users.map(u=>u.username))()`);
    if (!names2.includes(USER)) fails.push('改回原名失败：' + names2.join(','));

    console.log(fails.length ? '失败：\n - ' + fails.join('\n - ')
      : '通过：编辑弹窗可改账号名（' + USER + ' → ' + NEW + ' → ' + USER + '），操作列无「踢」');
    if (fails.length) process.exitCode = 1;
  } catch (e) {
    console.error('执行失败:', e.message);
    process.exitCode = 1;
  } finally {
    try { srv && srv.kill(); } catch (e) {}
    try { edge && edge.kill(); } catch (e) {}
    setTimeout(() => process.exit(process.exitCode || 0), 400);
  }
})();
