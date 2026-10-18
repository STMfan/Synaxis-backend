'use strict';
const $ = id => document.getElementById(id);
let csrf = '', next = null, editId = null;
function status(text, error = false) { $('status').textContent = text; $('status').classList.toggle('error', error); }
async function api(path, method = 'GET', body) {
  const headers = {}; if (body !== undefined) headers['Content-Type'] = 'application/json';
  if (csrf) headers['X-CSRF-Token'] = csrf;
  const response = await fetch('/admin/api/' + path, {method, headers, credentials: 'same-origin', body: body === undefined ? undefined : JSON.stringify(body)});
  if (!response.ok) { let data = {}; try { data = await response.json(); } catch {} throw new Error(data.detail || data.code || ('HTTP ' + response.status)); }
  return response.status === 204 ? null : response.json();
}
function action(fn) { return async event => { event?.preventDefault(); try { await fn(); } catch (error) { status(error.message, true); } }; }
async function users(append = false) {
  const page = await api('users?q=' + encodeURIComponent($('search').value) + '&after=' + (append ? next || 0 : 0));
  if (!append) $('users').replaceChildren();
  for (const user of page.items) {
    const row = document.createElement('tr');
    for (const value of [user.id, user.username, user.active ? '启用' : '停用']) { const cell = document.createElement('td'); cell.textContent = value; row.append(cell); }
    const cell = document.createElement('td'), edit = document.createElement('button'), remove = document.createElement('button');
    edit.textContent = '编辑'; edit.addEventListener('click', () => { editId = user.id; $('edit-name').value = user.username; $('edit-password').value = ''; $('edit-active').checked = Boolean(user.active); $('edit-dialog').showModal(); });
    remove.textContent = '删除'; remove.className = 'danger'; remove.addEventListener('click', action(async () => { if (!confirm('删除 ' + user.username + ' 的登录账号？历史与计费记录会保留。')) return; await api('users/' + user.id, 'DELETE'); await users(); status('用户已删除。'); }));
    cell.append(edit, remove); row.append(cell); $('users').append(row);
  }
  next = page.next_after; $('more').hidden = next === null;
}
async function showPanel() {
  $('login').hidden = true; $('panel').hidden = false; $('logout').hidden = false;
  await users(); const value = await api('network'); $('network').value = JSON.stringify(value.saved, null, 2); $('effective').textContent = JSON.stringify(value.effective, null, 2);
  status(value.restart_required ? '配置文件与当前运行配置不同，重启后生效。' : '已登录。');
}
$('login-form').addEventListener('submit', action(async () => { const result = await api('login', 'POST', {password: $('admin-password').value}); $('admin-password').value = ''; csrf = result.csrf_token; await showPanel(); }));
$('logout').addEventListener('click', action(async () => { await api('logout', 'POST'); location.reload(); }));
$('search-form').addEventListener('submit', action(() => users()));
$('more').addEventListener('click', action(() => users(true)));
$('create-form').addEventListener('submit', action(async () => { await api('users', 'POST', {username: $('new-name').value, password: $('new-password').value}); $('create-form').reset(); await users(); status('用户已创建。'); }));
$('edit-form').addEventListener('submit', action(async () => { const body = {username: $('edit-name').value, active: $('edit-active').checked}; if ($('edit-password').value) body.password = $('edit-password').value; await api('users/' + editId, 'PATCH', body); $('edit-password').value = ''; $('edit-dialog').close(); await users(); status('用户已更新，旧登录凭据已撤销。'); }));
$('close-edit').addEventListener('click', () => $('edit-dialog').close());
$('save-network').addEventListener('click', action(async () => { await api('network', 'PUT', JSON.parse($('network').value)); status('配置已保存，重启服务后生效。'); }));
$('restart').addEventListener('click', action(async () => { if (!confirm('确认重启服务？')) return; await api('restart', 'POST'); status('重启已请求。请稍后刷新并重新登录；地址更改时使用新地址。'); $('panel').hidden = true; }));
api('session').then(async value => { csrf = value.csrf_token; await showPanel(); }).catch(() => status('请登录；首次使用请先在终端设置管理员密码。'));
