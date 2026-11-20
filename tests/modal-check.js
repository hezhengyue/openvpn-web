// 真浏览器验证：编辑弹窗里点【生成随机密码】→ 密码明文在输入框 → 保存 → 不再弹二次密码页
// 用法：
//   node modal-check.js                      # 本地 MOCK（账号 alice）
//   SHOT_BASE=http://ip:8800 SHOT_PW=xxx MODAL_USER=会话 node modal-check.js
// 背景：早期保存后会再弹一个大字密码窗，用户要求去掉；改密已并入编辑，密码存台账明文。
const {spawn, execSync} = require('child_process');
const path = require('path');
const fs = require('fs');
const os = require('os');

const SRC = path.resolve(__dirname, '..', 'src');
const OUT = path.resolve(__dirname, '..', '.workbuddy', 'shots-modal');
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
  TMP = path.join(os.tmpdir(), 'ovpn-modal-' + Date.now());
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
let srvlog = '';
if (srv) { srv.stdout.on('data', d => srvlog += d); srv.stderr.on('data', d => srvlog += d); }

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
    const prof = path.join(os.tmpdir(), 'edge-modal-prof-' + Date.now());
    edge = spawn(EDGE, ['--headless=new', '--disable-gpu', '--hide-scrollbars', '--no-first-run',
      '--remote-debugging-port=9223', '--user-data-dir=' + prof, 'about:blank'], {stdio: 'ignore'});

    let ver;
    for (let i = 0; i < 60; i++) {
      try { ver = await (await fetch('http://127.0.0.1:9223/json/version')).json(); break; } catch (e) { await sleep(300); }
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

    await evalJs(`(async()=>{document.querySelector('#lp').value=${JSON.stringify(PW)};await doLogin();return 1})()`);
    await sleep(1500);
    await evalJs(`go('users')`);
    await sleep(900);

    // 打开编辑弹窗 → 点「生成随机密码」→ 读回生成的密码
    await evalJs(`userEdit(${JSON.stringify(USER)})`);
    await sleep(400);
    const pw = await evalJs(`(function(){
      const b=[...document.querySelectorAll('#mbody button')].find(x=>/生成随机密码/.test(x.textContent));
      if(!b) return '__NO_BTN__';
      b.click();
      return document.querySelector('#u_pw').value;
    })()`);
    if (pw === '__NO_BTN__') fails.push('编辑弹窗里找不到【生成随机密码】按钮');
    else if (!pw || pw.length < 8) fails.push('点生成后输入框没填上密码: ' + pw);
    else {
      // 输入框必须是明文可见，不是 password 点
      const typ = await evalJs(`document.querySelector('#u_pw').type`);
      if (typ === 'password') fails.push('生成后密码框仍是 password 类型，看不到值');
    }
    await shot('1-生成后');

    // 点确定保存 → 等 → 不应再弹密码页
    await evalJs(`document.querySelector('#mok').click();1`);
    await sleep(2000);
    const st = await evalJs(`(function(){
      const m=document.querySelector('#mask');
      return {disp:m.style.display, txt:m.textContent};
    })()`);
    if (st.disp === 'flex') fails.push('保存后仍弹出一次性密码弹窗（已要求去掉）');
    else if (st.txt.includes(pw)) fails.push('保存后页面仍残留刚生成的密码');
    await shot('2-保存后');

    console.log(fails.length ? '失败：\n - ' + fails.join('\n - ') : '通过：生成 → 保存 → 无二次弹窗，密码为 ' + pw);
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
