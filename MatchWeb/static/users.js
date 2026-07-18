const userRows = document.getElementById('user-rows');
const totalUsers = document.getElementById('total-users');
const userSummary = document.getElementById('user-summary');
const userSearch = document.getElementById('user-search');
const usersEmpty = document.getElementById('users-empty');
const usersError = document.getElementById('users-error');
const userDialog = document.getElementById('user-dialog');
const userForm = document.getElementById('user-form');
const dialogTitle = document.getElementById('dialog-title');
const dialogMode = document.getElementById('dialog-mode');
const dialogUsername = document.getElementById('dialog-username');
const dialogPassword = document.getElementById('dialog-password');
const dialogSubmit = document.getElementById('dialog-submit');
const formError = document.getElementById('form-error');
const deleteDialog = document.getElementById('delete-dialog');
const deleteForm = document.getElementById('delete-form');
const deleteUsername = document.getElementById('delete-username');
const deleteError = document.getElementById('delete-error');
const toast = document.getElementById('toast');
let users = [];

function initials(username) {
  return username.slice(0, 2).toUpperCase();
}

function makeButton(label, className, onClick) {
  const button = document.createElement('button');
  button.type = 'button';
  button.className = className;
  button.textContent = label;
  button.addEventListener('click', onClick);
  return button;
}

function renderUsers() {
  const query = userSearch.value.trim().toLocaleLowerCase('zh-CN');
  const visible = users.filter(({ username }) => username.toLocaleLowerCase('zh-CN').includes(query));
  userRows.replaceChildren();
  for (const user of visible) {
    const row = document.createElement('tr');
    const identityCell = document.createElement('td');
    const identity = document.createElement('div');
    identity.className = 'user-identity';
    const avatar = document.createElement('span');
    avatar.className = `user-avatar${user.is_admin ? ' admin' : ''}`;
    avatar.textContent = initials(user.username);
    const name = document.createElement('span');
    const strong = document.createElement('strong');
    strong.textContent = user.username;
    const small = document.createElement('small');
    small.textContent = user.is_admin ? '系统管理员账号' : '普通访问账号';
    name.append(strong, small);
    identity.append(avatar, name);
    identityCell.append(identity);

    const roleCell = document.createElement('td');
    const role = document.createElement('span');
    role.className = `role-badge${user.is_admin ? ' admin' : ''}`;
    role.textContent = user.is_admin ? '管理员' : '普通用户';
    roleCell.append(role);

    const statusCell = document.createElement('td');
    const status = document.createElement('span');
    status.className = 'active-state';
    status.textContent = '正常';
    statusCell.append(status);

    const actionsCell = document.createElement('td');
    actionsCell.className = 'row-actions';
    actionsCell.append(makeButton('重置密码', 'text-button', () => openReset(user.username)));
    if (!user.is_admin) actionsCell.append(makeButton('删除', 'text-button danger-text', () => openDelete(user.username)));
    row.append(identityCell, roleCell, statusCell, actionsCell);
    userRows.append(row);
  }
  usersEmpty.hidden = visible.length !== 0;
  userSummary.textContent = `共 ${users.length} 个账号${query ? `，找到 ${visible.length} 个结果` : ''}`;
}

async function api(url, options = {}) {
  const response = await fetch(url, { cache: 'no-store', ...options });
  if (response.status === 401) {
    location.href = '/login';
    throw new Error('登录已失效');
  }
  if (response.status === 403) {
    location.href = '/';
    throw new Error('没有访问权限');
  }
  const payload = await response.json();
  if (!response.ok) throw new Error(payload.error || '操作失败');
  return payload;
}

async function loadUsers() {
  usersError.hidden = true;
  try {
    const payload = await api('/api/users');
    users = payload.users;
    totalUsers.textContent = payload.total;
    renderUsers();
  } catch (error) {
    userRows.replaceChildren();
    usersEmpty.hidden = true;
    usersError.hidden = false;
    usersError.textContent = error.message || '无法读取用户';
    userSummary.textContent = '读取失败';
  }
}

function openCreate() {
  dialogMode.value = 'create';
  dialogTitle.textContent = '新增用户';
  dialogSubmit.textContent = '创建用户';
  dialogUsername.disabled = false;
  userForm.reset();
  formError.hidden = true;
  userDialog.showModal();
  dialogUsername.focus();
}

function openReset(username) {
  dialogMode.value = 'reset';
  dialogTitle.textContent = '重置密码';
  dialogSubmit.textContent = '保存新密码';
  userForm.reset();
  dialogUsername.value = username;
  dialogUsername.disabled = true;
  formError.hidden = true;
  userDialog.showModal();
  dialogPassword.focus();
}

function openDelete(username) {
  deleteUsername.textContent = username;
  deleteError.hidden = true;
  deleteDialog.showModal();
}

function showToast(message) {
  toast.textContent = message;
  toast.hidden = false;
  clearTimeout(showToast.timer);
  showToast.timer = setTimeout(() => { toast.hidden = true; }, 2600);
}

userForm.addEventListener('submit', async (event) => {
  event.preventDefault();
  formError.hidden = true;
  dialogSubmit.disabled = true;
  const mode = dialogMode.value;
  const username = dialogUsername.value;
  try {
    if (mode === 'create') {
      await api('/api/users', {
        method: 'POST', headers: { 'Content-Type': 'application/json' },
        body: JSON.stringify({ username, password: dialogPassword.value }),
      });
      showToast('用户已创建');
    } else {
      await api(`/api/users/${encodeURIComponent(username)}`, {
        method: 'PUT', headers: { 'Content-Type': 'application/json' },
        body: JSON.stringify({ password: dialogPassword.value }),
      });
      showToast('密码已重置');
    }
    userDialog.close();
    await loadUsers();
  } catch (error) {
    formError.textContent = error.message || '保存失败';
    formError.hidden = false;
  } finally {
    dialogSubmit.disabled = false;
  }
});

deleteForm.addEventListener('submit', async (event) => {
  event.preventDefault();
  deleteError.hidden = true;
  try {
    await api(`/api/users/${encodeURIComponent(deleteUsername.textContent)}`, { method: 'DELETE' });
    deleteDialog.close();
    showToast('用户已删除');
    await loadUsers();
  } catch (error) {
    deleteError.textContent = error.message || '删除失败';
    deleteError.hidden = false;
  }
});

document.getElementById('add-user-button').addEventListener('click', openCreate);
document.getElementById('dialog-close').addEventListener('click', () => userDialog.close());
document.getElementById('dialog-cancel').addEventListener('click', () => userDialog.close());
document.getElementById('delete-cancel').addEventListener('click', () => deleteDialog.close());
userSearch.addEventListener('input', renderUsers);
loadUsers();
