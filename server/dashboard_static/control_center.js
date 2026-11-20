(() => {
  'use strict';
  const $ = (id) => document.getElementById(id);
  const esc = (value) =>
    String(value ?? '').replace(
      /[&<>"']/g,
      (char) => ({ '&': '&amp;', '<': '&lt;', '>': '&gt;', '"': '&quot;', "'": '&#39;' })[char],
    );
  const json = (value) => JSON.stringify(value, null, 2);
  const prettyTime = (value) =>
    typeof value === 'number'
      ? new Date(value * 1000).toLocaleString('zh-CN', { hour12: false })
      : '暂无记录';
  const config = JSON.parse($('control-config').textContent);
  const hostControls = config.host_controls === true;
  const serviceLabels = {
    ready: '已就绪',
    stopped: '已停止',
    starting: '启动中',
    stopping: '停止中',
    blocked: '依赖阻塞',
    failed: '失败',
  };
  const jobLabels = {
    pending: '等待中',
    running: '处理中',
    done: '已完成',
    failed: '失败',
    cancelled: '已取消',
  };
  const documents = {
    persona: '人格定义',
    mcp: '外部 MCP 服务',
    screen: '实时屏幕设置',
    skills: '技能动作开关',
  };
  let settingsSnapshot = null,
    settingsBusy = false;
  const settingDrafts = new Map();
  let documentSnapshot = null,
    documentOriginal = '',
    documentBusy = false;
  let runtimeSnapshot = null,
    runtimeBusy = false,
    stateBusy = false,
    lifeOperation = null;
  let schedulerCursor = '0',
    schedulerNext = null,
    schedulersBusy = false;
  let jobsOffset = 0,
    jobsTotal = 0,
    jobsBusy = false;
  const jobsById = new Map();
  let servicesBusy = false;

  document.querySelectorAll('[data-port]').forEach((anchor) => {
    const port = config.ports?.[anchor.dataset.port];
    if (!Number.isInteger(port) || port < 1 || port > 65535) return;
    const url = new URL(window.location.href);
    const destination = (anchor.dataset.path || '/').split('#');
    url.port = String(port);
    url.pathname = destination[0];
    url.search = '';
    url.hash = destination[1] || '';
    anchor.href = url.toString();
  });

  function notify(message, kind = 'success') {
    const notice = $('control-notice');
    notice.textContent = message;
    notice.className = `notice ${kind}`;
    notice.hidden = false;
    if (kind !== 'success') notice.scrollIntoView({ behavior: 'smooth', block: 'nearest' });
  }
  function inlineError(id, error) {
    const element = $(id);
    element.hidden = !error;
    element.textContent = error || '';
  }
  async function request(path, options = {}) {
    const response = await fetch(path, {
      signal: AbortSignal.timeout(45000),
      ...options,
      headers: { ...(options.body ? { 'Content-Type': 'application/json' } : {}), ...options.headers },
    });
    let data;
    try {
      data = await response.json();
    } catch {
      throw new Error('服务未返回可读取的结果，请检查服务后刷新。');
    }
    if (!response.ok || data.success === false) {
      const message =
        response.status === 401
          ? '管理会话已失效，请重新打开本机控制页面后重试。'
          : response.status === 409
            ? `${data.error || data.message || '配置或状态版本已变化'}。未保存内容仍保留，请先重新读取并核对。`
            : data.error || data.message || `请求未成功（${response.status}）`;
      const error = new Error(message);
      error.status = response.status;
      throw error;
    }
    return data.result !== undefined ? data.result : data;
  }
  const send = (path, body, method = 'POST') => request(path, { method, body: JSON.stringify(body) });
  function errorMessage(error) {
    return error?.name === 'TimeoutError'
      ? '请求等待超时，结果可能尚未返回。请先重新读取状态，避免重复提交。'
      : error.message || '操作未完成，请稍后重试。';
  }
  function objectText(text, label) {
    let value;
    try {
      value = JSON.parse(text);
    } catch {
      throw new Error(`${label}必须是有效 JSON，请检查括号、引号和逗号。`);
    }
    if (!value || Array.isArray(value) || typeof value !== 'object')
      throw new Error(`${label}必须是 JSON 对象。`);
    return value;
  }
  function documentDirty() {
    return documentSnapshot !== null && $('document-content').value !== documentOriginal;
  }
  function stateDirty() {
    try {
      return Object.keys(objectText($('state-changes').value, '状态变更')).length > 0;
    } catch {
      return true;
    }
  }
  let extensionOriginal = '';
  function extensionDirty() {
    return !$('state-extensions').hidden && $('extension-values').value !== extensionOriginal;
  }
  function hasUnsaved() {
    return settingDrafts.size > 0 || documentDirty() || stateDirty() || extensionDirty();
  }
  window.addEventListener('beforeunload', (event) => {
    if (hasUnsaved()) {
      event.preventDefault();
      event.returnValue = '';
    }
  });

  function selectSection(name) {
    $('control-notice').hidden = true;
    if (!['settings', 'documents', 'runtime', 'services'].includes(name)) name = 'settings';
    document.querySelectorAll('[data-section]').forEach((button) => {
      const active = button.dataset.section === name;
      button.setAttribute('aria-selected', String(active));
      button.tabIndex = active ? 0 : -1;
      $(`section-${button.dataset.section}`).hidden = !active;
    });
    history.replaceState(null, '', `#${name}`);
    if (name === 'documents' && !documentSnapshot) loadDocument();
    if (name === 'runtime' && !runtimeSnapshot) {
      loadRuntime();
      loadJobs();
      loadSchedulers();
    }
    if (name === 'services' && hostControls) loadHost();
  }
  document.querySelector('.tabs').addEventListener('click', (event) => {
    const button = event.target.closest('[data-section]');
    if (button) selectSection(button.dataset.section);
  });
  document.querySelector('.tabs').addEventListener('keydown', (event) => {
    if (!['ArrowLeft', 'ArrowRight', 'Home', 'End'].includes(event.key)) return;
    const tabs = [...document.querySelectorAll('[data-section]')];
    const index = tabs.indexOf(document.activeElement);
    if (index < 0) return;
    const next =
      event.key === 'Home'
        ? 0
        : event.key === 'End'
          ? tabs.length - 1
          : (index + (event.key === 'ArrowRight' ? 1 : -1) + tabs.length) % tabs.length;
    event.preventDefault();
    selectSection(tabs[next].dataset.section);
    tabs[next].focus();
  });

  function allFields() {
    return (settingsSnapshot?.groups || []).flatMap((group) => group.fields || []);
  }
  function settingValue(field) {
    const draft = settingDrafts.get(field.key);
    if (draft?.kind === 'unset') return field.default;
    if (draft) return draft.value;
    return field.value === null || field.value === undefined ? field.default : field.value;
  }
  function fieldInput(field) {
    const id = `setting-${field.key}`,
      value = settingValue(field),
      draft = settingDrafts.get(field.key);
    const disabled = settingsBusy || field.editable === false ? ' disabled' : '';
    const common = `id="${esc(id)}" data-setting="${esc(field.key)}"${disabled}`;
    if (field.secret || field.type === 'secret') {
      const secretValue = draft?.kind === 'set' ? draft.value : '';
      return `<input ${common} type="password" autocomplete="new-password" value="${esc(secretValue)}" placeholder="${field.configured ? '已配置 · 输入新值才会更新' : '未配置 · 输入凭据'}">`;
    }
    if (field.type === 'boolean')
      return `<select ${common}>${value == null ? '<option value="">未设置</option>' : ''}<option value="true"${value === true || value === 'true' ? ' selected' : ''}>开启 / true</option><option value="false"${value === false || value === 'false' ? ' selected' : ''}>关闭 / false</option></select>`;
    if (Array.isArray(field.choices) && field.choices.length) {
      const choices = field.choices.map((choice) =>
        typeof choice === 'object' ? choice : { value: choice, label: choice },
      );
      if (!choices.some((choice) => String(choice.value) === String(value ?? '')))
        choices.unshift({ value: value ?? '', label: value == null ? '未设置' : value });
      return `<select ${common}>${choices.map((choice) => `<option value="${esc(choice.value)}"${String(choice.value) === String(value ?? '') ? ' selected' : ''}>${esc(choice.label ?? choice.value)}</option>`).join('')}</select>`;
    }
    const numeric = field.type === 'number' || field.type === 'integer';
    return `<input ${common} type="${numeric ? 'number' : 'text'}" value="${esc(value ?? '')}"${numeric ? ` step="${field.type === 'integer' ? '1' : 'any'}"${field.minimum != null ? ` min="${esc(field.minimum)}"` : ''}${field.maximum != null ? ` max="${esc(field.maximum)}"` : ''}` : ' autocomplete="off"'} placeholder="${value == null ? '未设置，由程序默认值决定' : ''}">`;
  }
  function renderSettings() {
    if (!settingsSnapshot) return;
    const search = $('settings-search').value.trim().toLowerCase(),
      selected = $('settings-group').value;
    let visible = 0;
    const groups = settingsSnapshot.groups
      .map((group) => {
        if (selected && String(group.id) !== selected) return '';
        const fields = (group.fields || []).filter(
          (field) =>
            !search ||
            `${field.key} ${field.label || ''} ${field.description || ''}`.toLowerCase().includes(search),
        );
        if (!fields.length) return '';
        visible += fields.length;
        return `<section class="panel settings-group"><h2>${esc(group.title)} <span class="muted">${fields.length} 项</span></h2>${fields
          .map((field) => {
            const draft = settingDrafts.get(field.key),
              secret = field.secret || field.type === 'secret';
            const runtime = field.runtime || {};
            const processValue = secret
              ? runtime.configured
                ? '已配置'
                : '未配置'
              : (runtime.value ?? '未设置');
            const runtimeText = `当前管理进程环境：${processValue}`;
            return `<div class="setting-row${draft ? ' changed' : ''}" data-setting-row="${esc(field.key)}"><div><label for="setting-${esc(field.key)}">${esc(field.label || field.key)}</label>${field.label && field.label !== field.key ? `<div class="setting-key">${esc(field.key)}</div>` : ''}<p class="setting-description">${esc(field.description || '此配置由对应服务读取。')}</p></div><div>${fieldInput(field)}<div class="setting-meta"><span class="pill${draft ? ' warning' : ''}">${draft ? (draft.kind === 'unset' ? '将恢复默认' : '待保存') : field.configured ? (secret ? '凭据已配置' : '已保存覆盖') : '使用默认值'}</span>${field.editable === false ? '<span>部署参数 · 只读</span>' : field.restart_required ? '<span>保存后需重启</span>' : ''}<button type="button" class="small-button" data-setting-unset="${esc(field.key)}"${settingsBusy || field.editable === false ? ' disabled' : ''}>恢复默认</button></div><div class="setting-meta"><span class="default-value">${secret ? '默认凭据不显示' : `默认：${esc(field.default == null ? '由程序决定' : typeof field.default === 'object' ? JSON.stringify(field.default) : field.default)}`}</span></div><div class="setting-meta">${esc(runtimeText)}</div></div></div>`;
          })
          .join('')}</section>`;
      })
      .join('');
    $('settings-fields').innerHTML =
      groups || '<div class="panel empty">没有匹配的配置。请更换搜索词或分组。</div>';
    $('settings-count').textContent = `(${allFields().length})`;
    $('settings-source').textContent =
      `${settingsSnapshot.source?.note || '保存的配置与运行值分别展示。'} 当前显示 ${visible} / ${allFields().length} 项${settingsSnapshot.source?.env_file ? ` · ${settingsSnapshot.source.env_file}` : ''}`;
    updateSettingsDirty();
  }
  function updateSettingsDirty() {
    $('settings-dirty').textContent = settingDrafts.size
      ? `${settingDrafts.size} 项未保存更改`
      : '没有未保存的更改';
    $('settings-save').disabled = settingsBusy || !settingDrafts.size;
    $('settings-discard').disabled = settingsBusy || !settingDrafts.size;
    $('settings-reload').disabled = settingsBusy;
  }
  async function loadSettings(confirmDiscard = false) {
    if (settingsBusy) return;
    if (confirmDiscard && settingDrafts.size && !confirm('重新读取会丢弃本页尚未保存的配置更改，继续吗？'))
      return;
    settingsBusy = true;
    renderSettings();
    updateSettingsDirty();
    try {
      const selected = $('settings-group').value;
      settingsSnapshot = await request('/api/control/settings');
      settingDrafts.clear();
      if (confirmDiscard) notify('已重新读取配置。');
      $('settings-group').innerHTML =
        '<option value="">全部分组</option>' +
        settingsSnapshot.groups
          .map(
            (group) =>
              `<option value="${esc(group.id)}">${esc(group.title)} (${group.fields.length})</option>`,
          )
          .join('');
      if ([...$('settings-group').options].some((option) => option.value === selected))
        $('settings-group').value = selected;
    } catch (error) {
      notify(errorMessage(error), 'error');
      if (!settingsSnapshot)
        $('settings-fields').innerHTML =
          `<div class="panel empty">配置不可读取：${esc(errorMessage(error))}。可点击“重新读取”重试。</div>`;
    } finally {
      settingsBusy = false;
      renderSettings();
      updateSettingsDirty();
    }
  }
  $('settings-search').addEventListener('input', renderSettings);
  $('settings-group').addEventListener('change', renderSettings);
  $('settings-reload').addEventListener('click', () => loadSettings(true));
  $('settings-discard').addEventListener('click', () => {
    if (confirm('撤销所有尚未保存的配置更改？')) {
      settingDrafts.clear();
      renderSettings();
    }
  });
  $('settings-fields').addEventListener('input', (event) => {
    const key = event.target.dataset.setting;
    if (!key || settingsBusy) return;
    const field = allFields().find((item) => item.key === key);
    if (!field) return;
    let value = event.target.value;
    if (field.type === 'boolean' && value !== '') value = value === 'true';
    else if ((field.type === 'integer' || field.type === 'number') && value !== '') value = Number(value);
    const baseline = field.value == null ? field.default : field.value;
    if (
      (field.secret && value === '') ||
      (!(field.secret || field.type === 'secret') && String(value ?? '') === String(baseline ?? ''))
    )
      settingDrafts.delete(key);
    else settingDrafts.set(key, { kind: 'set', value });
    const row = event.target.closest('[data-setting-row]');
    row.classList.toggle('changed', settingDrafts.has(key));
    const pill = row.querySelector('.pill');
    pill.textContent = settingDrafts.has(key) ? '待保存' : field.configured ? '已保存覆盖' : '使用默认值';
    pill.classList.toggle('warning', settingDrafts.has(key));
    updateSettingsDirty();
  });
  $('settings-fields').addEventListener('click', (event) => {
    const button = event.target.closest('[data-setting-unset]');
    if (!button || settingsBusy) return;
    const field = allFields().find((item) => item.key === button.dataset.settingUnset);
    if (!field) return;
    if (field.configured) settingDrafts.set(field.key, { kind: 'unset' });
    else settingDrafts.delete(field.key);
    renderSettings();
  });
  $('settings-form').addEventListener('submit', async (event) => {
    event.preventDefault();
    if (settingsBusy || !settingDrafts.size || !settingsSnapshot) return;
    const changes = {},
      unset = [];
    try {
      settingDrafts.forEach((draft, key) => {
        const field = allFields().find((item) => item.key === key);
        if (draft.kind === 'unset') {
          unset.push(key);
          return;
        }
        if (
          (field.type === 'integer' || field.type === 'number') &&
          (draft.value === '' ||
            !Number.isFinite(draft.value) ||
            (field.type === 'integer' && !Number.isInteger(draft.value)))
        )
          throw new Error(
            `${field.label || key} 需要有效${field.type === 'integer' ? '整数' : '数值'}；移除配置请使用“恢复默认”。`,
          );
        changes[key] = draft.value;
      });
      settingsBusy = true;
      renderSettings();
      settingsSnapshot = await send(
        '/api/control/settings',
        { revision: settingsSnapshot.revision, changes, unset },
        'PUT',
      );
      settingDrafts.clear();
      notify('配置已保存。请按字段提示重启对应服务；Docker 配置通常需要重新创建服务后生效。');
    } catch (error) {
      notify(errorMessage(error), 'error');
    } finally {
      settingsBusy = false;
      renderSettings();
    }
  });

  function updateDocumentDirty() {
    const dirty = documentDirty();
    $('document-dirty').textContent = dirty ? '此配置有未保存的更改' : '没有未保存的更改';
    $('document-save').disabled =
      documentBusy || !documentSnapshot || !dirty || documentSnapshot.editable === false;
    $('document-content').disabled = documentBusy || !documentSnapshot || documentSnapshot.editable === false;
    $('document-kind').disabled = documentBusy;
    $('document-reload').disabled = documentBusy;
    $('document-format').disabled = documentBusy || !documentSnapshot || documentSnapshot.editable === false;
  }
  async function loadDocument(confirmDiscard = false, selected = $('document-kind').value) {
    if (documentBusy) return;
    if (confirmDiscard && documentDirty() && !confirm('重新读取会丢弃此配置未保存的更改，继续吗？')) {
      $('document-kind').value = documentSnapshot.id;
      return;
    }
    documentBusy = true;
    updateDocumentDirty();
    try {
      const result = await request(`/api/control/documents/${encodeURIComponent(selected)}`);
      documentSnapshot = result;
      documentOriginal = json(result.content);
      $('document-content').value = documentOriginal;
      $('document-kind').value = result.id;
      $('document-note').textContent =
        `${result.note || ''}${result.path ? ` · ${result.path}` : ''}${result.restart_required ? ' · 保存后需重启对应服务。' : ''}`;
      $('document-revision').textContent = `版本 ${String(result.revision).slice(0, 12)}`;
      $('document-secrets').hidden = !(result.secret_paths || []).length;
    } catch (error) {
      notify(errorMessage(error), 'error');
      if (documentSnapshot) $('document-kind').value = documentSnapshot.id;
      else $('document-note').textContent = `此配置暂不可读取：${errorMessage(error)}`;
    } finally {
      documentBusy = false;
      updateDocumentDirty();
    }
  }
  $('document-kind').addEventListener('change', () => loadDocument(true));
  $('document-reload').addEventListener('click', () => loadDocument(true));
  $('document-content').addEventListener('input', updateDocumentDirty);
  $('document-format').addEventListener('click', () => {
    try {
      $('document-content').value = json(objectText($('document-content').value, '配置'));
      updateDocumentDirty();
    } catch (error) {
      notify(errorMessage(error), 'error');
    }
  });
  $('document-save').addEventListener('click', async () => {
    if (documentBusy || !documentSnapshot) return;
    try {
      const content = objectText($('document-content').value, '配置');
      documentBusy = true;
      updateDocumentDirty();
      const result = await send(
        `/api/control/documents/${encodeURIComponent(documentSnapshot.id)}`,
        { revision: documentSnapshot.revision, content },
        'PUT',
      );
      documentSnapshot = result;
      documentOriginal = json(result.content);
      $('document-content').value = documentOriginal;
      $('document-note').textContent = result.note || '';
      $('document-revision').textContent = `版本 ${String(result.revision).slice(0, 12)}`;
      $('document-secrets').hidden = !(result.secret_paths || []).length;
      notify(`${documents[result.id] || '配置'}已保存。${result.note || ''}`);
    } catch (error) {
      notify(errorMessage(error), 'error');
    } finally {
      documentBusy = false;
      updateDocumentDirty();
    }
  });

  const stateFields = [
    ['current_time', '当前时间', 'string', '状态中的时间描述'],
    ['current_location', '当前位置', 'string', '当前所在位置'],
    ['active_quest', '当前任务', 'string', '正在进行的任务'],
    ['important_thing', '重要事项', 'string', '需要记住的当前事项'],
    ['relationship_delta', '关系分值增量', 'integer', '输入正负整数；关系等级由系统派生'],
    ['add_item', '添加物品', 'string', '单个物品名称'],
    ['remove_item', '移除物品', 'string', '单个物品名称'],
    ['npc_name', '角色名称', 'string', '当前角色名称'],
    ['npc_attire', '角色着装', 'string', '当前着装描述'],
    ['npc_visual_status', '角色外观状态', 'string', '当前外观状态'],
    ['npc_activity', '角色活动', 'string', '当前正在进行的活动'],
    ['add_memory_highlight', '添加记忆高亮', 'string', '单条高亮内容'],
    ['remove_memory_highlight', '移除记忆高亮', 'string', '要移除的单条内容'],
  ];
  function readStateChanges() {
    return objectText($('state-changes').value, '状态变更');
  }
  const npcKeys = {
    npc_name: 'name',
    npc_attire: 'attire',
    npc_visual_status: 'visual_module_status',
    npc_activity: 'current_activity',
  };
  function renderStateFields() {
    let changes;
    try {
      changes = readStateChanges();
    } catch {
      return;
    }
    const available =
      runtimeSnapshot?.state?.available === true && !stateBusy && !runtimeBusy && !extensionDirty();
    const current = runtimeSnapshot?.state?.data?.data || {};
    $('state-fields').innerHTML = stateFields
      .map(([key, label, type, help]) => {
        const enabled = Object.prototype.hasOwnProperty.call(changes, key);
        const currentValue = npcKeys[key] ? current.npc_state?.[npcKeys[key]] : current[key];
        const value = enabled ? changes[key] : type === 'integer' ? 0 : (currentValue ?? '');
        return `<div class="state-edit-row"><label for="state-enable-${key}"><input id="state-enable-${key}" type="checkbox" data-state-enable="${key}"${enabled ? ' checked' : ''}${!available ? ' disabled' : ''}>${label}</label><input aria-label="${label}变更值" data-state-value="${key}" type="${type === 'integer' ? 'number' : 'text'}"${type === 'integer' ? ' step="1" min="-10000" max="10000"' : ' maxlength="4000"'} value="${esc(typeof value === 'object' ? JSON.stringify(value) : value)}"${!available || !enabled ? ' disabled' : ''}><small>${help}</small></div>`;
      })
      .join('');
  }
  function updateStateDirty() {
    const dirty = stateDirty();
    $('state-dirty').textContent = dirty ? '有待提交的状态变更' : '没有待提交的变更';
    $('state-save').disabled =
      stateBusy || runtimeBusy || extensionDirty() || !dirty || runtimeSnapshot?.state?.available !== true;
    $('state-changes').disabled =
      stateBusy || runtimeBusy || extensionDirty() || runtimeSnapshot?.state?.available !== true;
    $('state-discard').disabled = stateBusy || runtimeBusy || !dirty;
    const extensionsLocked = stateBusy || runtimeBusy || dirty || runtimeSnapshot?.state?.available !== true;
    $('extension-values').disabled = extensionsLocked;
    $('extension-save').disabled = extensionsLocked || !extensionDirty();
    $('extension-discard').disabled = extensionsLocked || !extensionDirty();
    $('extension-dirty').textContent = extensionDirty()
      ? '有未保存的扩展字段更改'
      : dirty
        ? '请先保存或撤销上方的状态变更'
        : '';
    $('runtime-scope').disabled = stateBusy || runtimeBusy;
    $('runtime-reload').disabled = runtimeBusy || stateBusy;
  }
  function resetExtensions() {
    const extensions = runtimeSnapshot?.state?.data?.extensions || {};
    $('state-extensions').hidden = !Object.keys(extensions).length;
    extensionOriginal = json(extensions);
    $('extension-values').value = extensionOriginal;
  }
  function renderRuntime() {
    if (!runtimeSnapshot) return;
    const runtime = runtimeSnapshot,
      state = runtime.state,
      life = runtime.life,
      autonomy = runtime.autonomy;
    inlineError('state-error', state?.available ? '' : state?.error || '记忆状态暂不可用，请检查记忆服务。');
    $('state-current').textContent = state?.available ? json(state.data.data) : '当前状态不可用';
    $('state-version').textContent = state?.available ? `版本 ${state.data.version}` : '版本未知';
    inlineError('life-error', life?.available ? '' : life?.error || '生命状态暂不可用。');
    $('life-current').textContent = life?.available ? json(life.data) : '当前生命状态不可用';
    $('life-state').textContent = life?.data?.phase_label || '未知';
    $('life-save').disabled = !life?.available || runtime.scope !== 'owner';
    inlineError('autonomy-error', autonomy?.available ? '' : autonomy?.error || '自主状态存储暂不可用。');
    $('autonomy-current').textContent = autonomy?.available ? json(autonomy.data) : '当前自主状态不可用';
    $('autonomy-state').textContent =
      { pause: '已暂停', resume: '允许活动', stop: '已停止', auto: '自动' }[autonomy?.data?.mode] || '未知';
    document.querySelectorAll('[data-autonomy]').forEach((button) => {
      button.disabled = !autonomy?.available;
    });
    $('runtime-updated').textContent =
      (runtime.scope_status?.available === false ? '作用域列表暂不完整 · ' : '') +
      `读取于 ${new Date().toLocaleTimeString('zh-CN', { hour12: false })}`;
    renderStateFields();
    updateStateDirty();
  }
  async function loadRuntime(confirmDiscard = false, scope = $('runtime-scope').value) {
    if (runtimeBusy || stateBusy) return;
    if (
      confirmDiscard &&
      (stateDirty() || extensionDirty()) &&
      !confirm('刷新或切换作用域会丢弃待提交的状态变更，继续吗？')
    ) {
      $('runtime-scope').value = runtimeSnapshot?.scope || 'owner';
      return;
    }
    runtimeBusy = true;
    renderStateFields();
    updateStateDirty();
    try {
      runtimeSnapshot = await request(`/api/control/runtime?scope=${encodeURIComponent(scope)}`);
      const scopes = [...new Set(['owner', ...(runtimeSnapshot.scopes || []), runtimeSnapshot.scope])];
      $('runtime-scope').innerHTML = scopes
        .map(
          (item) =>
            `<option value="${esc(item)}">${esc(item === 'owner' ? 'owner · 管理员' : item)}</option>`,
        )
        .join('');
      $('runtime-scope').value = runtimeSnapshot.scope;
      $('state-changes').value = '{}';
      resetExtensions();
      renderRuntime();
    } catch (error) {
      notify(errorMessage(error), 'error');
      inlineError('state-error', `${errorMessage(error)} 当前显示可能已过期，未保存内容仍保留。`);
      if (runtimeSnapshot) $('runtime-scope').value = runtimeSnapshot.scope;
    } finally {
      runtimeBusy = false;
      renderStateFields();
      updateStateDirty();
    }
  }
  $('runtime-reload').addEventListener('click', () => loadRuntime(true));
  $('runtime-scope').addEventListener('change', () => loadRuntime(true));
  $('state-fields').addEventListener('change', (event) => {
    const key = event.target.dataset.stateEnable;
    if (!key) return;
    try {
      const changes = readStateChanges();
      if (event.target.checked) {
        const input = event.target.closest('.state-edit-row').querySelector('[data-state-value]');
        changes[key] = key === 'relationship_delta' ? Number(input.value) : input.value;
      } else delete changes[key];
      $('state-changes').value = json(changes);
      renderStateFields();
      updateStateDirty();
    } catch (error) {
      notify(errorMessage(error), 'error');
    }
  });
  $('state-fields').addEventListener('input', (event) => {
    const key = event.target.dataset.stateValue;
    if (!key) return;
    try {
      const changes = readStateChanges();
      changes[key] =
        key === 'relationship_delta' && event.target.value !== ''
          ? Number(event.target.value)
          : event.target.value;
      $('state-changes').value = json(changes);
      updateStateDirty();
    } catch (error) {
      notify(errorMessage(error), 'error');
    }
  });
  $('state-changes').addEventListener('input', () => {
    renderStateFields();
    updateStateDirty();
  });
  $('state-discard').addEventListener('click', () => {
    $('state-changes').value = '{}';
    renderStateFields();
    updateStateDirty();
  });
  $('state-save').addEventListener('click', async () => {
    if (stateBusy || !runtimeSnapshot?.state?.available) return;
    try {
      const changes = readStateChanges();
      for (const [key, value] of Object.entries(changes)) {
        if (!stateFields.some((field) => field[0] === key))
          throw new Error(`状态字段 ${key} 不支持直接修改，请使用对应的状态操作。`);
        if (
          key === 'relationship_delta'
            ? !Number.isInteger(value) || Math.abs(value) > 10000
            : typeof value !== 'string' || value.length > 4000
        )
          throw new Error(`${key} 的类型或长度不正确。`);
      }
      if (!Object.keys(changes).length) return;
      stateBusy = true;
      renderStateFields();
      updateStateDirty();
      const saved = await send(
        '/api/control/state',
        { scope: runtimeSnapshot.scope, expected_version: runtimeSnapshot.state.data.version, changes },
        'PUT',
      );
      runtimeSnapshot.state.data = saved;
      $('state-changes').value = '{}';
      resetExtensions();
      renderRuntime();
      notify('当前作用域状态已保存。');
    } catch (error) {
      notify(errorMessage(error), 'error');
    } finally {
      stateBusy = false;
      renderStateFields();
      updateStateDirty();
    }
  });
  function updateExtensionControls() {
    renderStateFields();
    updateStateDirty();
  }
  $('extension-values').addEventListener('input', updateExtensionControls);
  $('extension-discard').addEventListener('click', () => {
    $('extension-values').value = extensionOriginal;
    updateExtensionControls();
  });
  $('extension-save').addEventListener('click', async () => {
    if (stateBusy || runtimeBusy || !extensionDirty() || stateDirty()) return;
    try {
      const extensions = objectText($('extension-values').value, '扩展字段');
      const originalKeys = Object.keys(runtimeSnapshot.state.data.extensions).sort();
      if (json(Object.keys(extensions).sort()) !== json(originalKeys)) {
        throw new Error('请保留已存在的扩展字段名称。');
      }
      stateBusy = true;
      updateExtensionControls();
      runtimeSnapshot.state.data = await send(
        '/api/control/state/extensions',
        {
          scope: runtimeSnapshot.scope,
          expected_version: runtimeSnapshot.state.data.version,
          extensions,
        },
        'PUT',
      );
      resetExtensions();
      renderRuntime();
      notify('扩展字段已保存。');
    } catch (error) {
      notify(errorMessage(error), 'error');
    } finally {
      stateBusy = false;
      updateExtensionControls();
    }
  });
  $('life-action').addEventListener('change', () => {
    $('life-duration').disabled = $('life-action').value === 'auto';
    lifeOperation = null;
  });
  $('life-save').addEventListener('click', async () => {
    const button = $('life-save');
    if (button.disabled) return;
    const action = $('life-action').value,
      duration = action === 'auto' ? 0 : Number($('life-duration').value),
      reason = $('life-reason').value.trim();
    if (
      reason.length < 2 ||
      reason.length > 300 ||
      (action !== 'auto' && (!Number.isInteger(duration) || duration < 5))
    ) {
      notify('请填写 2–300 字的原因，以及至少 5 分钟的整数时长。', 'error');
      return;
    }
    const payload = { scope: 'owner', action, duration_minutes: duration, reason };
    const key = JSON.stringify(payload);
    if (!lifeOperation || lifeOperation.key !== key) {
      const bytes = crypto.getRandomValues(new Uint8Array(16));
      lifeOperation = { key, id: Array.from(bytes, (byte) => byte.toString(16).padStart(2, '0')).join('') };
    }
    button.disabled = true;
    try {
      const result = await send('/api/control/life', { ...payload, operation_id: lifeOperation.id });
      lifeOperation = null;
      if (runtimeSnapshot?.scope === 'owner') {
        runtimeSnapshot.life = { available: true, data: result.life };
        renderRuntime();
      }
      notify('节律调节已应用。');
    } catch (error) {
      notify(errorMessage(error), 'error');
    } finally {
      button.disabled = !runtimeSnapshot?.life?.available || runtimeSnapshot.scope !== 'owner';
    }
  });
  document.querySelectorAll('[data-autonomy]').forEach((button) =>
    button.addEventListener('click', async () => {
      const mode = button.dataset.autonomy;
      if (mode === 'stop' && !confirm('停止 Bines 的自主活动？可在此页手动恢复。')) return;
      document.querySelectorAll('[data-autonomy]').forEach((item) => {
        item.disabled = true;
      });
      try {
        const result = await send('/api/control/autonomy', {
          mode,
          reason: $('autonomy-reason').value.trim(),
        });
        if (runtimeSnapshot)
          runtimeSnapshot.autonomy = {
            available: true,
            data: { backend: runtimeSnapshot.autonomy?.data?.backend, configured: true, ...result },
          };
        renderRuntime();
        notify('自主活动控制已保存。');
      } catch (error) {
        notify(errorMessage(error), 'error');
      } finally {
        document.querySelectorAll('[data-autonomy]').forEach((item) => {
          item.disabled = !runtimeSnapshot?.autonomy?.available;
        });
      }
    }),
  );
  async function loadSchedulers(cursor = '0') {
    if (schedulersBusy) return;
    schedulersBusy = true;
    for (const id of ['schedulers-refresh', 'schedulers-first', 'schedulers-next']) $(id).disabled = true;
    try {
      const data = await request(`/api/control/schedulers?cursor=${encodeURIComponent(cursor)}`);
      schedulerCursor = cursor;
      schedulerNext = data.next_cursor;
      const cards = (data.items || []).map((item) => {
        const card = document.createElement('details');
        card.className = 'task';
        const summary = document.createElement('summary');
        summary.textContent = [item.channel === 'qq' ? 'QQ' : '桌面', item.id, item.activity_mode]
          .filter(Boolean)
          .join(' · ');
        const body = document.createElement('pre');
        body.className = 'readout';
        body.textContent = json(item);
        card.append(summary, body);
        return card;
      });
      $('schedulers-list').replaceChildren(...cards);
      if (!cards.length) $('schedulers-list').textContent = '本页没有已记录的调度状态。';
      inlineError('schedulers-error', '');
    } catch (error) {
      inlineError('schedulers-error', errorMessage(error));
    } finally {
      schedulersBusy = false;
      $('schedulers-refresh').disabled = false;
      $('schedulers-first').disabled = schedulerCursor === '0';
      $('schedulers-next').disabled = !schedulerNext;
    }
  }
  $('schedulers-refresh').addEventListener('click', () => loadSchedulers(schedulerCursor));
  $('schedulers-first').addEventListener('click', () => loadSchedulers('0'));
  $('schedulers-next').addEventListener('click', () => {
    if (schedulerNext) loadSchedulers(schedulerNext);
  });

  async function loadJobs() {
    if (jobsBusy) return;
    jobsBusy = true;
    $('jobs-reload').disabled = true;
    $('jobs-status').disabled = true;
    $('jobs-prev').disabled = true;
    $('jobs-next').disabled = true;
    try {
      const result = await request(
        `/api/control/jobs?status=${encodeURIComponent($('jobs-status').value)}&offset=${jobsOffset}&limit=30`,
      );
      jobsTotal = result.total;
      jobsById.clear();
      (result.items || []).forEach((job) => jobsById.set(String(job.id), job));
      $('jobs-list').className = 'task-list';
      $('jobs-list').innerHTML =
        (result.items || [])
          .map(
            (job) =>
              `<article class="task"><div class="task-head"><div><strong>${esc(job.kind)} · ${esc(job.scope || '未标注作用域')}</strong><br><small>任务 ${esc(job.id)} · 尝试 ${esc(job.attempts ?? 0)} 次 · ${esc(prettyTime(job.updated_at))}</small></div><span class="pill${job.status === 'failed' ? ' warning' : ''}">${esc(jobLabels[job.status] || job.status)}</span></div>${job.error_type || job.terminal_reason || job.error ? `<p>${esc(job.error_type || '')} ${esc(job.terminal_reason || job.error || '')}</p>` : ''}${job.retryable ? `<div class="task-actions"><label class="sr-only" for="retry-reason-${esc(job.id)}">重试依据</label><select id="retry-reason-${esc(job.id)}"><option value="connectivity_rechecked">已检查连接</option><option value="configuration_corrected">已修正配置</option><option value="request_corrected">已修正请求</option></select><button type="button" data-job-retry="${esc(job.id)}">重试此任务</button></div>` : job.status === 'failed' ? '<p class="muted">此任务当前不支持人工重试，请先处理其失败原因。</p>' : ''}</article>`,
          )
          .join('') || '<div class="empty">当前筛选下没有任务。</div>';
      $('jobs-page').textContent = jobsTotal
        ? `${jobsOffset + 1}–${Math.min(jobsOffset + 30, jobsTotal)} / ${jobsTotal}`
        : '0 条';
      inlineError('jobs-error', '');
    } catch (error) {
      inlineError('jobs-error', errorMessage(error));
      $('jobs-list').innerHTML = '<div class="empty">任务列表暂不可用。</div>';
    } finally {
      jobsBusy = false;
      $('jobs-reload').disabled = false;
      $('jobs-status').disabled = false;
      $('jobs-prev').disabled = jobsOffset === 0;
      $('jobs-next').disabled = jobsOffset + 30 >= jobsTotal;
    }
  }
  $('jobs-reload').addEventListener('click', loadJobs);
  $('jobs-status').addEventListener('change', () => {
    jobsOffset = 0;
    loadJobs();
  });
  $('jobs-prev').addEventListener('click', () => {
    jobsOffset = Math.max(0, jobsOffset - 30);
    loadJobs();
  });
  $('jobs-next').addEventListener('click', () => {
    if (jobsOffset + 30 < jobsTotal) {
      jobsOffset += 30;
      loadJobs();
    }
  });
  $('jobs-list').addEventListener('click', async (event) => {
    const button = event.target.closest('[data-job-retry]');
    if (!button) return;
    const job = jobsById.get(button.dataset.jobRetry);
    if (!job) return;
    if (!confirm(`确认已处理失败原因，并重试任务 ${job.id}（${job.kind}）？`)) return;
    button.disabled = true;
    try {
      await send(`/api/control/jobs/${encodeURIComponent(job.id)}/retry`, {
        scope: job.scope,
        kind: job.kind,
        expected_attempt: job.attempts,
        expected_updated_at: job.updated_at,
        expected_error_type: job.error_type,
        reason: $(`retry-reason-${job.id}`).value,
      });
      notify('任务已重新排队。');
      await loadJobs();
    } catch (error) {
      notify(errorMessage(error), 'error');
      button.disabled = false;
    }
  });
  $('index-rebuild').addEventListener('click', async () => {
    if (!confirm('从当前记忆数据库重建向量索引？这可能需要一些时间，原记忆会保留。')) return;
    const button = $('index-rebuild');
    button.disabled = true;
    try {
      await send('/api/control/index/rebuild', {});
      notify('索引重建请求已提交。请在状态总览查看进度。');
    } catch (error) {
      notify(errorMessage(error), 'error');
    } finally {
      button.disabled = false;
    }
  });

  $('host-unavailable').hidden = hostControls;
  $('host-controls').hidden = !hostControls;
  async function loadServices() {
    if (!hostControls || servicesBusy) return;
    servicesBusy = true;
    $('services-reload').disabled = true;
    try {
      const result = await request('/api/services');
      $('services-list').className = 'table-wrap';
      $('services-list').innerHTML =
        `<table class="services-table"><thead><tr><th>服务</th><th>状态</th><th>说明</th><th>操作</th></tr></thead><tbody>${(result.services || []).map((service) => `<tr><td><strong>${esc(service.name)}</strong>${service.pid ? `<br><span class="muted">PID ${esc(service.pid)}</span>` : ''}</td><td>${esc(serviceLabels[service.state] || service.state || '未知')}</td><td class="service-message">${esc(service.message || '')}${service.depends_on?.length ? `<br>依赖：${esc(service.depends_on.join('、'))}` : ''}</td><td><div class="button-row"><button type="button" class="small-button" data-service="${esc(service.name)}" data-action="start"${service.running ? ' disabled' : ''}>启动</button><button type="button" class="small-button danger" data-service="${esc(service.name)}" data-action="stop"${!service.running ? ' disabled' : ''}>停止</button><button type="button" class="small-button" data-service="${esc(service.name)}" data-action="logs">日志</button></div></td></tr>`).join('')}</tbody></table>`;
      inlineError(
        'services-error',
        result.startup?.active ? '已有服务启动任务正在执行，稍后刷新查看结果。' : '',
      );
    } catch (error) {
      inlineError('services-error', errorMessage(error));
      $('services-list').innerHTML = '<div class="empty">服务状态暂不可用。</div>';
    } finally {
      servicesBusy = false;
      $('services-reload').disabled = false;
    }
  }
  async function loadHost() {
    if (!hostControls) return;
    await Promise.allSettled([
      loadServices(),
      request('/api/presence')
        .then((data) => {
          $('presence-online').checked = data.user_online === true;
          $('presence-current').textContent = data.user_online ? '当前：用户在线' : '当前：用户离线';
          inlineError('presence-error', '');
        })
        .catch((error) => inlineError('presence-error', errorMessage(error))),
      request('/api/game_mode')
        .then((data) => {
          $('game-enabled').checked = data.enabled === true;
          $('game-interval').value = data.interval;
          inlineError('game-error', '');
        })
        .catch((error) => inlineError('game-error', errorMessage(error))),
    ]);
  }
  $('services-reload').addEventListener('click', loadServices);
  $('presence-save').addEventListener('click', async () => {
    const button = $('presence-save');
    button.disabled = true;
    try {
      const result = await send('/api/presence', { online: $('presence-online').checked });
      $('presence-current').textContent = result.user_online ? '当前：用户在线' : '当前：用户离线';
      inlineError('presence-error', '');
      notify('用户在线状态已保存。');
    } catch (error) {
      inlineError('presence-error', errorMessage(error));
    } finally {
      button.disabled = false;
    }
  });
  $('game-save').addEventListener('click', async () => {
    const interval = Number($('game-interval').value);
    if (!Number.isFinite(interval) || interval <= 0) {
      inlineError('game-error', '请输入大于 0 的间隔秒数。');
      return;
    }
    const button = $('game-save');
    button.disabled = true;
    try {
      await send('/api/game_mode', { enabled: $('game-enabled').checked, interval });
      inlineError('game-error', '');
      notify('游戏模式指令已发送。');
    } catch (error) {
      inlineError('game-error', errorMessage(error));
    } finally {
      button.disabled = false;
    }
  });
  $('services-list').addEventListener('click', async (event) => {
    const button = event.target.closest('[data-service]');
    if (!button) return;
    const name = button.dataset.service,
      action = button.dataset.action;
    if (action === 'stop' && !confirm(`停止 ${name}？依赖它的托管服务也可能停止。`)) return;
    button.disabled = true;
    try {
      if (action === 'logs') {
        const result = await request(`/api/services/${encodeURIComponent(name)}/logs`);
        $('service-log-title').textContent = `${name} · 日志`;
        $('service-log-content').textContent = Array.isArray(result.logs)
          ? result.logs.join('\n')
          : json(result);
        $('service-log').showModal();
      } else {
        const result = await send(`/api/services/${encodeURIComponent(name)}/${action}`, {});
        notify(result.message || '操作已提交。');
        await loadServices();
      }
    } catch (error) {
      notify(errorMessage(error), 'error');
    } finally {
      button.disabled = false;
    }
  });
  $('service-log-close').addEventListener('click', () => $('service-log').close());
  [
    ['services-start-all', 'start-all'],
    ['services-stop-all', 'stop-all'],
  ].forEach(([id, action]) =>
    $(id).addEventListener('click', async () => {
      if (
        action === 'stop-all' &&
        !confirm('停止此管理器托管的全部服务？外部启动的 Docker 容器不受此按钮控制。')
      )
        return;
      const button = $(id);
      button.disabled = true;
      try {
        const result = await send(`/api/services/${action}`, {});
        notify(result.message || '服务操作已提交。');
        await loadServices();
      } catch (error) {
        notify(errorMessage(error), 'error');
      } finally {
        button.disabled = false;
      }
    }),
  );
  $('screen-apply').addEventListener('click', async () => {
    if (documentSnapshot?.id === 'screen' && documentDirty()) {
      notify('实时屏幕配置还有未保存的更改，请先保存或撤销，再应用。', 'warning');
      return;
    }
    if (!confirm('把已保存的实时屏幕设置应用到此宿主机？这可能启动或停止屏幕分析。')) return;
    const button = $('screen-apply');
    button.disabled = true;
    try {
      const result = await send('/api/control/screen/apply', {});
      notify(result.message || '屏幕设置已应用。');
      await loadServices();
    } catch (error) {
      notify(errorMessage(error), 'error');
    } finally {
      button.disabled = false;
    }
  });
  selectSection(location.hash.slice(1));
  loadSettings();
})();
