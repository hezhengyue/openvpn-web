// 无头冒烟：起一个 MOCK 模式的后台，用 jsdom 真实执行前端脚本，逐页切换抓运行时错误
const {spawn, execSync} = require('child_process');
const {JSDOM, VirtualConsole} = require('jsdom');
const path = require('path');
const fs = require('fs');

// 源码在 ../src（目录重构后：仓库根只放 README 与各分类目录）
const SRC = path.resolve(__dirname, '..', 'src');
const TMP = path.join(require('os').tmpdir(), 'ovpn-smoke-' + Date.now());
fs.mkdirSync(path.join(TMP, 'data'), {recursive: true});
fs.writeFileSync(path.join(TMP, 'data', 'admin_password.txt'), 'SmokePass123!');

const env = {...process.env, OVPN_WEB_ROOT: TMP, OVPN_WEB_MOCK: '1', OVPN_WEB_PORT: '8899'};

// 造一点数据，让列表/分组/授权都有内容
const seed = `
import os,sys
sys.path.insert(0, r"${SRC}")
import core
core.init_db(); core.set_meta("admin_hash", core.hash_password("SmokePass123!"))
for i,n in enumerate(["alice","bob","carol","dave"]):
    core.add_user(n, real_name=["爱丽丝","鲍勃","卡罗","戴夫"][i], dept=["运维部","财务部","运维部","外包"][i],
                  note="", password="Passw0rd!23")
# 账号名本身就用中文（这一轮新增支持）
core.add_user("张三", real_name="张三", dept="运维部", password="Passw0rd!23")
core.add_asset_group("核心数据库","生产库",[("10.10.1.11","tcp","3306"),("10.10.1.12","tcp","3306"),("10.10.1.13","tcp","3306")])
core.add_asset_group("Web 前端","",[("10.10.2.0/24","tcp","80,443")])
core.add_user_group("运维组","7x24",["alice","carol"])
g=[x for x in core.list_asset_groups() if x["name"]=="核心数据库"][0]
core.add_grant("group","运维组",g["id"])
core.add_grant("user","bob",g["id"])
core.set_meta("public_addr","vpn.example.com"); core.set_meta("initialized",1)
core.set_meta("extra_routes",["10.10.0.0/16","10.20.0.0/16","10.30.0.0/16"])
core.write_all_config()
print("seeded")
`;
// 造数据的脚本写进临时目录，不在 tests/ 留生成物
const seedPath = path.join(TMP, 'seed.py');
fs.writeFileSync(seedPath, seed, 'utf8');
execSync(`python "${seedPath}"`, {env, stdio: 'inherit'});

const srv = spawn('python', ['app.py'], {cwd: SRC, env, stdio: ['ignore', 'pipe', 'pipe']});
let srvlog = '';
srv.stdout.on('data', d => srvlog += d);
srv.stderr.on('data', d => srvlog += d);

const base = 'http://127.0.0.1:8899';
const errors = [];
process.on('unhandledRejection', e => errors.push('未捕获异常: ' + (e && e.message || e)));
const sleep = ms => new Promise(r => setTimeout(r, ms));

async function waitUp() {
  for (let i = 0; i < 60; i++) {
    try { const r = await fetch(base + '/api/session'); if (r.ok) return; } catch (e) {}
    await sleep(250);
  }
  throw new Error('server not up\n' + srvlog);
}

(async () => {
  try {
    await waitUp();
    const vc = new VirtualConsole();
    vc.on('jsdomError', e => errors.push('jsdomError: ' + (e.message || e)));
    vc.on('error', (...a) => errors.push('console.error: ' + a.join(' ')));
    const html = await (await fetch(base + '/')).text();
    if (!html.includes('id="force"')) errors.push('缺少首登强制改密界面 #force');
    if (/value="admin"|默认\s*admin|新装默认账户/.test(html)) errors.push('登录页仍暴露默认账户或预填 admin');
    if (/value\.trim\(\)\|\|'admin'/.test(html)) errors.push('登录逻辑仍把空账户兜底成 admin');
    if (!/忘记管理凭据/.test(html)) errors.push('登录页缺少重置凭据入口说明');
    if (!/async function doForce\(/.test(html)) errors.push('缺少首登改密提交逻辑 doForce');
    if (/function userPwd\(/.test(html)) errors.push('前端仍保留独立的改密弹窗 userPwd');
    if (/显示密码列|togglePwCol/.test(html)) errors.push('账号页仍保留【显示密码列】开关');
    if (!/有效期/.test(html)) errors.push('账号页缺少有效期字段');
    const jar = {};
    const dom = new JSDOM(html, {url: base + '/', runScripts: 'dangerously', virtualConsole: vc,
      pretendToBeVisual: true, beforeParse(w) {
        w.fetch = async (url, opt = {}) => {
          const abs = new URL(url, base).toString();
          opt.headers = Object.assign({}, opt.headers);
          const ck = Object.entries(jar).map(([k, v]) => k + '=' + v).join('; ');
          if (ck) opt.headers['Cookie'] = ck;
          const r = await fetch(abs, opt);
          const sc = r.headers.getSetCookie ? r.headers.getSetCookie() : [];
          for (const line of sc) {
            const kv = line.split(';')[0], i = kv.indexOf('=');
            if (i > 0) jar[kv.slice(0, i)] = kv.slice(i + 1);
          }
          const txt = await r.text();
          return {ok: r.ok, status: r.status, text: async () => txt, json: async () => JSON.parse(txt)};
        };
        w.alert = () => {};
        // jsdom 没实现滚动布局，补个空实现（真浏览器里有）
        w.HTMLElement.prototype.scrollIntoView = function () {};
        w.Element.prototype.scrollIntoView = function () {};
      }});
    const w = dom.window;
    const wait = ms => new Promise(r => setTimeout(r, ms));
    if (w.document.querySelector('#lu').value) errors.push('登录页账户框被预填了内容（不应暴露默认账户）');
    if (/admin/.test(w.document.querySelector('#lu').placeholder)) errors.push('账户框 placeholder 仍提示默认 admin');
    w.document.querySelector('#lu').value = 'admin';
    w.document.querySelector('#lp').value = 'SmokePass123!';
    await w.doLogin();
    await wait(500);
    if (!w.document.querySelector('#app').style.display.includes('block')) errors.push('登录后未显示主界面');

    const waitPage = async (name) => {
      for (let i = 0; i < 40; i++) {
        const page = w.document.querySelector('#page');
        const txt = (page.textContent || '').trim();
        if (txt && !txt.startsWith('加载中')) return txt;
        await wait(200);
      }
      return null;
    };
    const pages = ['overview', 'wizard', 'online', 'users', 'acl', 'settings', 'update', 'logs', 'route', 'security'];
    for (const p of pages) {
      try { w.go(p); } catch (e) { errors.push('go(' + p + ') 抛错: ' + e.message); }
      const txt = await waitPage(p);
      if (txt === null) { errors.push('页面 ' + p + ' 一直卡在加载中'); continue; }
      if (txt.length < 5) errors.push('页面 ' + p + ' 渲染为空');
      if (/undefined|NaN/.test(txt)) errors.push('页面 ' + p + ' 出现 undefined/NaN: ' + txt.slice(0, 120));
    }
    // 资产与授权四个子页 + 各自交互
    try {
      w.go('acl'); await wait(400);
      for (const t of ['ag', 'ug', 'gr', 'p2p']) {
        w.aclTab(t); await wait(200);
        const b = w.document.querySelector('#aclbox');
        if (!b || (b.textContent || '').trim().length < 10) errors.push('ACL 子页 ' + t + ' 渲染为空');
      }
      w.aclTab('gr'); await wait(150);
      if (!w.document.querySelector('#g_subject')) errors.push('授权页缺少主体选择');
      w.document.querySelector('#g_type').value = 'group';
      w.syncSubj();
      if (!w.document.querySelector('#g_subject').innerHTML.includes('运维组')) errors.push('切换用户组后主体列表没更新');
      // 用户组成员改成手动输入（标签框），不再是一长串勾选框
      w.aclTab('ug'); await wait(150);
      w.ugNew(); await wait(150);
      if (w.document.querySelector('.mck')) errors.push('用户组成员仍是勾选列表');
      if (!w.document.querySelector('#tg_ugm')) errors.push('用户组成员缺少手动输入的标签框');
      w.closeModal();
      // 资产组条目可编辑
      w.aclTab('ag'); await wait(150);
      const agBtn = w.document.querySelector('#aclbox button[onclick*="agItems("]');
      if (agBtn) {
        agBtn.click(); await wait(150);
        if (!w.document.querySelector('#mask').textContent.includes('编辑'))
          errors.push('资产条目管理缺少编辑按钮');
        w.closeModal();
      }
    } catch (e) { errors.push('ACL 交互抛错: ' + e.message); }

    // 设置页：标签式路由、加密套件、自动算人数
    try {
      w.go('settings'); await wait(500);
      const chips = w.document.querySelectorAll('#tg_extra_routes .chip').length;
      if (chips !== 3) errors.push('附加推送路由标签渲染异常，chip=' + chips);
      const ccks = w.document.querySelectorAll('.cck').length;
      if (ccks < 3) errors.push('加密套件勾选组缺失');
      const cfg = w.readCfg();
      if (!Array.isArray(cfg.extra_routes) || cfg.extra_routes.length !== 3) errors.push('readCfg 没读到路由标签');
      if (cfg.tun_mtu !== 1400) errors.push('readCfg tun_mtu=' + cfg.tun_mtu);
      if (cfg.data_ciphers !== 'AES-256-GCM:CHACHA20-POLY1305') errors.push('readCfg 加密套件=' + cfg.data_ciphers);
      if (w.document.querySelector('#c_lan_nets')) errors.push('设置页仍然出现「内网网段」输入框');
      // 最低 TLS / 压缩 应藏在高级折叠区里（默认收起），而不是摆在常用表单上
      const adv = w.document.querySelector('details.adv');
      if (!adv) errors.push('设置页缺少高级选项折叠区');
      else {
        if (adv.open) errors.push('高级选项默认应收起');
        if (!adv.querySelector('#c_tls_min') || !adv.querySelector('#c_compress'))
          errors.push('最低 TLS / 压缩 没放进高级折叠区');
        if (!adv.querySelector('#c_pool_on') || !adv.querySelector('#c_dns') || !adv.querySelector('#c_ntp'))
          errors.push('动态地址池 / DNS / NTP 例外没放进高级折叠区');
        if (adv.textContent.includes('undefined')) errors.push('高级区出现 undefined');
      }
      if (cfg.compress !== 'off') errors.push('readCfg 压缩默认值=' + cfg.compress);
      if (cfg.tls_min !== '1.2') errors.push('readCfg tls_min=' + cfg.tls_min);
      // 最低 TLS 必须是下拉框，不是文本框
      const tls = w.document.querySelector('#c_tls_min');
      if (!tls) errors.push('缺少最低 TLS 控件');
      else if (tls.tagName !== 'SELECT') errors.push('最低 TLS 不是选择框，是 ' + tls.tagName);
      else if (![...tls.options].some(o => o.value === '1.3')) errors.push('最低 TLS 缺少 1.3 选项');
      // VPN 网段自动下发要在界面上说明
      if (!/自动下发/.test(w.document.querySelector('#page').textContent)) errors.push('设置页没说明 VPN 网段自动下发');
      // 已初始化时，安装向导不应出现在侧栏
      const nw = w.document.querySelector('#navWizard');
      if (!nw) errors.push('侧栏缺少向导节点');
      else if (nw.style.display !== 'none') errors.push('已初始化后侧栏仍显示安装向导');
    } catch (e) { errors.push('设置页交互抛错: ' + e.message); }

    // 版本与更新页：快照与全备份已合并为一张「备份与恢复」卡片
    try {
      w.go('update'); await wait(600);
      const t = w.document.querySelector('#page').textContent;
      if (w.document.querySelector('#upweb')) errors.push('版本页仍有「Web 自身更新」上传框');
      if (w.document.querySelector('#upbin')) errors.push('版本页仍有「上传二进制」入口（应只留自动编译）');
      if (!w.document.querySelector('#vstat') || !w.document.querySelector('#vver')) errors.push('顶栏缺少状态片');
      if (/Web 自身更新/.test(t)) errors.push('版本页仍显示 Web 自身更新文案');
      if (!/备份与恢复/.test(t)) errors.push('版本页缺少合并后的备份与恢复卡片');
      if (/快照与回滚|全备份与迁移/.test(t)) errors.push('版本页仍保留旧的两张备份卡片');
      if (!/ta\.key/.test(t)) errors.push('备份说明没写清备份内容');
      if (!w.document.querySelector('#fbf')) errors.push('备份卡片缺少上传备份包的输入框');
      if (!/openvpn\//.test(t) || !/web\//.test(t)) errors.push('备份说明没写清两块内容');
      if (!/立即备份/.test(t)) errors.push('备份卡片缺少手动备份入口');
      if (!/来源/.test(t)) errors.push('备份列表缺少来源标记');
      if (/安全线|分支 EOL|达标|偏低/.test(t)) errors.push('版本页仍显示毫无作用的安全线/分支 EOL');
      if (!/重新检测/.test(t)) errors.push('版本页编译环境缺少【重新检测】按钮');
      if (!/安装完成与服务启动时已自动检查/.test(t))
        errors.push('版本页没有说明编译环境在安装/启动已自动检查');
      if (/编译环境不完整/.test(t)) errors.push('版本页仍用「编译环境不完整」制造故障错觉');
      const cards = [...w.document.querySelectorAll('#page > .card')];
      const upIdx = cards.findIndex(c => c.textContent.includes('升级 OpenVPN'));
      const bkIdx = cards.findIndex(c => c.textContent.includes('备份与恢复'));
      if (upIdx < 0 || bkIdx < 0 || upIdx > bkIdx)
        errors.push('升级 OpenVPN 卡片应在备份与恢复卡片上方');
      if (w.document.querySelector('#page > .row'))
        errors.push('版本页卡片仍被两栏 .row 包裹，未与备份卡片对齐');
    } catch (e) { errors.push('版本页交互抛错: ' + e.message); }

    // 表格分页：在线会话页也要有分页条
    try {
      w.go('online'); await wait(400);
      if (!w.document.querySelector('.pager')) errors.push('在线用户表格缺少分页条');
    } catch (e) { errors.push('在线页交互抛错: ' + e.message); }

    // 导入面板：密码必填，不再有「空密码也重置」开关
    try {
      w.go('users'); await wait(400);
      w.impOpen(); await wait(150);
      const box = w.document.querySelector('#impbox');
      if (!box) errors.push('导入面板没打开');
      else if (box.querySelector('#imp_reset')) errors.push('导入面板仍保留「空密码也重置」开关');
      else if (!/密码列\s*必须逐行填写|密码列必填/.test(box.textContent)) errors.push('导入面板没说明密码列必填');
      else if (/留空则保留原密码/.test(box.textContent)) errors.push('导入面板仍说密码留空可保留');
    } catch (e) { errors.push('导入面板抛错: ' + e.message); }

    // 账号页：搜索、生成密码可见
    try {
      w.go('users'); await wait(400);
      const kw = w.document.querySelector('#ukw');
      kw.value = '运维'; kw.dispatchEvent(new w.Event('input'));
      await wait(120);
      const t = w.document.querySelector('#ubox').textContent;
      if (!/爱丽丝|卡罗/.test(t)) errors.push('按部门/姓名搜索无效');
      if (w.document.querySelector('#ubox .pager')) errors.push('认证账号表格仍分页（应全部显示）');
      const pt = w.document.querySelector('#page').textContent;
      if (!/导入 Excel（全量覆盖）/.test(pt) || !/导出 Excel（全部账号）/.test(pt))
        errors.push('认证账号导入导出按钮缺少全量说明');
      kw.value = ''; kw.dispatchEvent(new w.Event('input'));
      // 台账页只做台账：操作列不该再有「踢」，也不再有独立「改密」（统一进编辑）
      const ubtns = [...w.document.querySelectorAll('#ubox button')].map(b => b.textContent.trim());
      if (ubtns.some(t => t === '踢')) errors.push('账号管理页操作列仍有「踢」按钮');
      if (ubtns.some(t => t === '改密')) errors.push('账号管理页操作列仍有独立的「改密」按钮');
      if (typeof w.userPwd !== 'undefined') errors.push('前端仍保留独立的 userPwd 改密入口');
      // 账号列表不再提供显示密码列开关，密码只在编辑弹窗里查看
      if (/Passw0rd!23/.test(w.document.querySelector('#ubox').textContent))
        errors.push('账号列表里仍显示了明文密码');
      if (typeof w.togglePwCol !== 'undefined') errors.push('仍保留 togglePwCol 显示密码列入口');
      // 添加账号：留空密码一次生成并直接保存，不再要求复制后二次确认
      w.userAdd(); await wait(100);
      w.document.querySelector('#u_name').value = '新建测试';
      w.document.querySelector('#mok').click(); await wait(1200);
      if (w.document.querySelector('#mask').style.display === 'flex')
        errors.push('添加账号保存后弹窗未关闭');
      // 编辑弹窗：账号可改名、密码默认星号遮盖但可显示/生成/修改、有有效期
      w.userEdit('alice'); await wait(100);
      const uname = w.document.querySelector('#u_name');
      if (!uname) errors.push('编辑弹窗里没有「账号」输入框（应支持改名）');
      else if (uname.value !== 'alice') errors.push('编辑弹窗的账号没有回填: ' + uname.value);
      const inp = w.document.querySelector('#u_pw');
      if (!inp || inp.value !== 'Passw0rd!23') errors.push('编辑弹窗密码框未回填当前明文: ' + (inp && inp.value));
      else if (inp.type !== 'password') errors.push('编辑弹窗密码框默认未用星号遮盖');
      if (!w.document.querySelector('#u_exp')) errors.push('编辑弹窗缺少有效期输入框');
      const genBtn = [...w.document.querySelectorAll('#mbody button')].find(b => /生成随机密码/.test(b.textContent));
      if (!genBtn) errors.push('编辑弹窗缺少【生成随机密码】按钮');
      else {
        genBtn.click();
        const pw2 = w.document.querySelector('#u_pw');
        if (!pw2.value || pw2.value.length < 8) errors.push('点生成后密码框没填上明文');
        else if (pw2.type !== 'password') errors.push('生成后密码框不再是星号遮盖');
      }
      const showBtn = [...w.document.querySelectorAll('#mbody button')].find(b => b.textContent.trim() === '显示');
      if (!showBtn) errors.push('编辑弹窗缺少【显示】按钮');
      else {
        showBtn.click(); await wait(80);
        if (w.document.querySelector('#u_pw').type !== 'text') errors.push('点【显示】后密码没有明文可见');
        if (showBtn.textContent.trim() !== '隐藏') errors.push('点【显示】后按钮文字没变成【隐藏】');
        showBtn.click(); await wait(80);
        if (w.document.querySelector('#u_pw').type !== 'password') errors.push('点【隐藏】后密码没有恢复星号');
      }
      // 保存后不能再弹一次性密码页：密码留在台账里，列表/编辑随时可看
      w.document.querySelector('#u_pw').value = 'ResetPw12345!';
      w.document.querySelector('#mok').click();
      await wait(1500);
      const mask = w.document.querySelector('#mask');
      if (mask.style.display === 'flex') errors.push('编辑保存后仍弹出一次性密码页（已要求去掉）');
    } catch (e) { errors.push('账号页交互抛错: ' + e.message); }

    // 这一轮：最大客户端去掉、动态地址池改成手动设置、管理账户免密、审计带 IP
    try {
      w.go('settings'); await wait(500);
      const pg = w.document.querySelector('#page');
      if (w.document.querySelector('#c_max_clients')) errors.push('设置页仍有「最大客户端」');
      if (/最大客户端|max-clients/.test(pg.textContent)) errors.push('设置页仍有最大客户端文案');
      const pool = w.document.querySelector('#c_pool_on');
      if (!pool) errors.push('设置页缺少「动态地址池」开关');
      else {
        const start = w.document.querySelector('#c_pool_start');
        if (pool.checked) errors.push('动态地址池默认应关闭');
        if (!start.disabled) errors.push('地址池未启用时起止地址应禁用');
        pool.checked = true; w.poolToggle();
        if (start.disabled) errors.push('勾选后地址池起止地址仍禁用');
        const cfg = w.readCfg();
        if (cfg.pool_enabled !== true || !cfg.pool_start) errors.push('readCfg 没读到地址池字段');
        pool.checked = false; w.poolToggle();
      }
      const who = w.document.querySelector('#vwho');
      if (!who || !(who.textContent || '').trim()) errors.push('顶栏没有显示管理员账户');
    } catch (e) { errors.push('设置页地址池交互抛错: ' + e.message); }

    try {
      w.go('security'); await wait(500);
      const t = w.document.querySelector('#page').textContent;
      if (/操作者名称/.test(t)) errors.push('安全页仍有已废弃的「操作者名称」设置');
      if (!/访问白名单/.test(t)) errors.push('安全页缺少管理端访问白名单');
      if (!w.document.querySelector('#tg_web_allow')) errors.push('白名单没有标签式输入区');
      else if (w.document.querySelectorAll('#tgchip_web_allow .chip').length < 1)
        errors.push('白名单初始条目没渲染出来');
      if (w.document.querySelector('#adn')) errors.push('操作者名称输入框应已移除');
      if (!w.document.querySelector('#au')) errors.push('安全页缺少独立管理账户名输入框');
      if (w.document.querySelector('#aup') || w.document.querySelector('#rp'))
        errors.push('改账户名/重置仍要求输入当前密码（忘了密码时无法使用）');
      if (!w.document.querySelector('#np2')) errors.push('改密码缺少「重复新密码」输入框');
      if (!/重新生成账户与密码/.test(t)) errors.push('安全页缺少一键重置管理凭据入口');
      if (!/reset-admin\.sh/.test(t)) errors.push('安全页缺少 Linux 重置脚本提示');
    } catch (e) { errors.push('安全页交互抛错: ' + e.message); }

    try {
      w.go('logs'); await wait(500);
      const head = w.document.querySelector('#page table thead');
      const txt = head ? head.textContent : '';
      if (!/操作者 IP/.test(txt)) errors.push('审计日志缺少「操作者 IP」列: ' + txt);
      // 动作必须是中文，不能把 user.add 这种 key 直接甩给用户看
      const body = w.document.querySelector('#page table tbody').textContent;
      if (/user\.add|user\.update|settings\.save/.test(body))
        errors.push('审计日志动作仍显示英文 key: ' + body.slice(0, 120));
      if (!/新增账号|修改账号|系统/.test(body)) errors.push('审计日志没有中文化的动作/操作者');
    } catch (e) { errors.push('日志页交互抛错: ' + e.message); }

    // 在线页：搜索框 + 停用账号（不再有踢下线，踢了客户端会自动重连）
    try {
      w.go('online'); await wait(500);
      const okw = w.document.querySelector('#okw');
      if (!okw) errors.push('在线用户页缺少搜索框');
      const t = w.document.querySelector('#page').textContent;
      if (!/停用账号/.test(t)) errors.push('在线页缺少「停用账号」按钮');
      if (!/自动重连/.test(t)) errors.push('在线页没说明踢下线的自动重连问题');
      const btns = [...w.document.querySelectorAll('#obox button')].map(b => b.textContent.trim());
      if (btns.some(x => /踢下线/.test(x))) errors.push('在线页仍有「踢下线」按钮');
    } catch (e) { errors.push('在线页交互抛错: ' + e.message); }

    // 自助改密页：不该再有固定 IP 输入框
    try {
      const sp = await (await fetch(base + '/self')).text();      if (/id="ip"/.test(sp) || /id="ipbox"/.test(sp)) errors.push('自助页仍然有固定 IP 输入框');
      if (!/whoami/.test(sp)) errors.push('自助页缺少自动识别');
      if (!/连上 VPN/.test(sp)) errors.push('自助页没说明必须先连 VPN');
    } catch (e) { errors.push('自助页检查抛错: ' + e.message); }

    // CSRF 自愈：故意塞一个过期 token，应自动刷新会话后重试成功，而不是弹「请刷新页面」
    try {
      const stale = 'stale-' + Date.now();
      w.eval('S.csrf=' + JSON.stringify(stale));
      await w.logout(); await wait(300);
      if (!w.document.querySelector('#login').style.display.includes('flex')) errors.push('CSRF token 失效后没有自愈退出');
      if (w.eval('S.csrf') === stale) errors.push('CSRF token 没有刷新');
    } catch (e) { errors.push('CSRF 自愈测试抛错: ' + e.message); }

    console.log(errors.length ? '问题：\n - ' + errors.join('\n - ') : '前端冒烟全部通过');
    srv.kill();
    process.exit(errors.length ? 1 : 0);
  } catch (e) {
    console.error('冒烟失败:', e.message);
    console.error(srvlog.split('\n').slice(-20).join('\n'));
    srv.kill();
    process.exit(1);
  }
})();
