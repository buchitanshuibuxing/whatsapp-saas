// ====== State ======
var API = '/api/tenant';
var state = {
  token: localStorage.getItem('wa_token') || '',
  tenant: null,
  activeView: 'chat',
  chats: [],
  currentChat: null,
  messages: [],
  chatTranslate: true,
  translateConfig: null,
  customers: [],
  detailCustomerId: null,
  pollingTimer: null,
  translateTimer: null
};

// ====== Init ======
function init() {
  if (state.token) {
    document.getElementById('loginOverlay').style.display = 'none';
    document.getElementById('app').style.display = 'flex';
    loadMe();
  } else {
    document.getElementById('loginOverlay').style.display = 'flex';
    document.getElementById('app').style.display = 'none';
  }
  // Bind search events
  var chatSearch = document.getElementById('chatSearch');
  if (chatSearch) chatSearch.addEventListener('input', function(e) { filterChats(e.target.value); });
  var crmSearch = document.getElementById('crmSearch');
  if (crmSearch) crmSearch.addEventListener('input', debounce(loadCustomers, 300));
  var crmTag = document.getElementById('crmFilterTag');
  if (crmTag) crmTag.addEventListener('change', loadCustomers);
  var tempSlider = document.getElementById('llmTemperature');
  if (tempSlider) tempSlider.addEventListener('input', function() { document.getElementById('llmTempVal').textContent = this.value; });
}

// ====== Login ======
function showLogin() {
  document.getElementById('loginOverlay').style.display = 'flex';
  document.getElementById('app').style.display = 'none';
  document.getElementById('loginUser').value = '';
  document.getElementById('loginPass').value = '';
  document.getElementById('loginError').style.display = 'none';
  state.token = '';
  localStorage.removeItem('wa_token');
}

function hideLogin() {
  document.getElementById('loginOverlay').style.display = 'none';
  document.getElementById('app').style.display = 'flex';
}

function doLogin() {
  var user = document.getElementById('loginUser').value.trim();
  var pass = document.getElementById('loginPass').value.trim();
  var err = document.getElementById('loginError');
  if (!user || !pass) { err.textContent = '请输入用户名和密码'; err.style.display = 'block'; return; }
  err.style.display = 'none';
  fetch(API + '/login', {
    method: 'POST',
    headers: { 'Content-Type': 'application/json' },
    body: JSON.stringify({ username: user, password: pass })
  }).then(function(r) { return r.json(); })
  .then(function(data) {
    if (data.error) { err.textContent = data.error; err.style.display = 'block'; return; }
    state.token = data.token || data.access_token;
    localStorage.setItem('wa_token', state.token);
    hideLogin();
    loadMe();
  }).catch(function(e) { err.textContent = '网络错误'; err.style.display = 'block'; });
}

function doLogout() {
  state.token = '';
  localStorage.removeItem('wa_token');
  if (state.pollingTimer) { clearInterval(state.pollingTimer); state.pollingTimer = null; }
  state.chats = [];
  state.messages = [];
  state.currentChat = null;
  showLogin();
}

// ====== API ======
function api(path, opts) {
  opts = opts || {};
  var headers = opts.headers || {};
  if (state.token) headers['Authorization'] = 'Bearer ' + state.token;
  headers['Content-Type'] = headers['Content-Type'] || 'application/json';
  return fetch(API + path, {
    method: opts.method || 'GET',
    headers: headers,
    body: opts.body
  }).then(function(r) { return r.json().catch(function() { return {}; }); });
}

function apiRaw(path, opts) {
  opts = opts || {};
  var headers = opts.headers || {};
  if (state.token) headers['Authorization'] = 'Bearer ' + state.token;
  return fetch(API + path, {
    method: opts.method || 'GET',
    headers: headers,
    body: opts.body
  });
}

// ====== Load Me ======
function loadMe() {
  api('/me').then(function(data) {
    if (data.error) { doLogout(); return; }
    state.tenant = data;
    document.getElementById('sidebarAvatar').textContent = (data.username || 'U')[0].toUpperCase();
    document.getElementById('setUsername').textContent = data.username || '--';
    document.getElementById('setCompany').textContent = data.company_name || '--';
    document.getElementById('setWaPhone').textContent = data.whatsapp_number || '未绑定';
    document.getElementById('setApiKey').textContent = data.api_key || '--';
    loadChats();
    pollWASession();
    if (state.pollingTimer) clearInterval(state.pollingTimer);
    state.pollingTimer = setInterval(function() {
      pollWASession();
      if (state.currentChat) refreshMessages(state.currentChat);
    }, 5000);
    loadTranslateConfig();
  });
}

// ====== View Switching ======
function switchView(view) {
  state.activeView = view;
  // Sidebar nav
  ['Chat','Crm','Translate','Settings'].forEach(function(k) {
    var el = document.getElementById('nav' + k);
    if (el) el.classList.toggle('active', k.toLowerCase() === view);
  });
  // Right panel views
  ['chatView','crmView','translateView','settingsView'].forEach(function(id) {
    var el = document.getElementById(id);
    if (el) {
      if (view === 'chat' && id === 'chatView') { el.style.display = 'flex'; el.style.flexDirection = 'column'; el.style.padding = '0'; }
      else if (view === 'crm' && id === 'crmView') { el.classList.add('active'); }
      else if (view === 'translate' && id === 'translateView') { el.classList.add('active'); }
      else if (view === 'settings' && id === 'settingsView') { el.classList.add('active'); }
      else { el.classList.remove('active'); el.style.display = (id === 'chatView') ? 'none' : ''; }
    }
  });
  if (view === 'crm') loadCustomers();
  if (view === 'translate') loadTranslateConfig();
}

// ====== Toast ======
function toast(msg, isErr) {
  var t = document.getElementById('toast');
  t.textContent = msg;
  t.className = 'toast ' + (isErr ? 'error' : '') + ' show';
  setTimeout(function() { t.className = 'toast'; }, 2500);
}

// ====== WA Polling ======
function pollWASession() {
  api('/self/session').then(function(data) {
    var dot = document.getElementById('waStatusBadge');
    var mini = document.getElementById('waStatusMini');
    var qrCard = document.getElementById('qrCard');
    var settingWaStatus = document.getElementById('settingWaStatus');
    if (!dot || !mini) return;
    var status = (data.status || '').toUpperCase();
    var dotEl = dot.querySelector('.dot');
    dotEl.className = 'dot';
    if (status === 'CONNECTED' || status === 'WORKING') {
      dotEl.classList.add('connected'); mini.textContent = '已连接';
      if (qrCard) qrCard.style.display = 'none';
    } else if (status === 'SCAN_QR_CODE' || status === 'QR') {
      dotEl.classList.add('qr'); mini.textContent = '待扫码';
      if (qrCard) qrCard.style.display = 'block'; loadQR();
    } else if (status === 'STARTING') {
      dotEl.classList.add('qr'); mini.textContent = '启动中';
      if (qrCard) qrCard.style.display = 'none';
    } else {
      dotEl.classList.add('offline'); mini.textContent = data.status || '离线';
      if (qrCard) qrCard.style.display = 'none';
    }
    if (settingWaStatus) settingWaStatus.textContent = data.status || '离线';
    if (data.whatsapp_number) {
      document.getElementById('setWaPhone').textContent = data.whatsapp_number;
    }
  }).catch(function() {});
}

function loadQR() {
  apiRaw('/qr').then(function(r) {
    if (!r.ok) { document.getElementById('qrContainer').innerHTML = '<span style="color:#e74c3c">获取二维码失败: ' + r.status + '</span>'; return; }
    return r.blob();
  }).then(function(blob) {
    if (!blob) return;
    var url = URL.createObjectURL(blob);
    document.getElementById('qrContainer').innerHTML = '<img src="' + url + '" alt="QR Code" style="max-width:260px;border-radius:8px">';
  }).catch(function() {
    document.getElementById('qrContainer').innerHTML = '<span style="color:#e74c3c">加载失败</span>';
  });
}

function waSessionAction(action) {
  api('/self/session/' + action, { method: 'POST' }).then(function(data) {
    if (data.error) { toast(data.error, true); return; }
    toast('操作成功: ' + action);
    setTimeout(pollWASession, 1500);
  }).catch(function() { toast('操作失败', true); });
}

// ====== Chat List ======
function loadChats() {
  api('/chats?limit=200').then(function(data) {
    if (data.error) return;
    state.chats = data || [];
    renderChatList();
  }).catch(function() {});
}

function renderChatList(filter) {
  var list = document.getElementById('chatList');
  var chats = state.chats;
  if (filter) {
    filter = filter.toLowerCase();
    chats = chats.filter(function(c) {
      return (c.name || '').toLowerCase().indexOf(filter) >= 0 ||
             (c.id || '').toLowerCase().indexOf(filter) >= 0;
    });
  }
  if (chats.length === 0) {
    list.innerHTML = '<div class="empty-state">暂无消息<br><br>在搜索框输入手机号开始新对话</div>';
    return;
  }
  list.innerHTML = chats.map(function(c) {
    var name = c.name || (c.id || '').replace('@c.us','').replace('@s.whatsapp.net','');
    var preview = (c.last_message || '').substring(0, 40);
    var time = formatTime(c.last_message_at);
    var initial = (name || '?')[0].toUpperCase();
    var isActive = state.currentChat === c.id;
    return '<div class="chat-item' + (isActive ? ' active' : '') + '" onclick="selectChat(\'' + escAttr(c.id) + '\')">' +
      '<div class="chat-avatar" style="background:' + colorFor(c.id) + ';color:#fff">' + initial + '</div>' +
      '<div class="chat-info">' +
        '<div class="chat-name">' + esc(name) + '</div>' +
        '<div class="chat-preview">' + esc(preview || '无消息') + '</div>' +
      '</div>' +
      '<div class="chat-time">' + time + '</div>' +
    '</div>';
  }).join('');
}

function filterChats(query) {
  renderChatList(query);
}

function newChat() {
  var phone = document.getElementById('chatSearch').value.trim();
  if (!phone) { toast('请输入手机号', true); return; }
  var chatId = phone.replace(/[^0-9]/g, '') + '@c.us';
  selectChat(chatId);
  document.getElementById('chatSearch').value = '';
}

// ====== Chat ======
function selectChat(chatId) {
  state.currentChat = chatId;
  state.messages = [];
  document.getElementById('welcomeScreen').style.display = 'none';
  document.getElementById('chatWorkspace').style.display = 'flex';
  document.getElementById('chatContactName').textContent = chatId.replace('@c.us','').replace('@s.whatsapp.net','');
  document.getElementById('chatContactAvatar').textContent = chatId[0] || '?';
  document.getElementById('chatContactAvatar').style.background = colorFor(chatId);
  document.getElementById('chatMessages').innerHTML = '<div class="empty-state">加载中...</div>';
  document.getElementById('sendBtn').disabled = false;
  document.getElementById('chatInput').disabled = false;
  document.getElementById('chatInput').focus();
  renderChatList();
  refreshMessages(chatId);
}

function refreshMessages(chatId) {
  chatId = chatId || state.currentChat;
  if (!chatId) return;
  fetch('/api/tenant/chats/' + encodeURIComponent(chatId) + '/messages?limit=100', {
    headers: { 'Authorization': 'Bearer ' + state.token }
  }).then(function(r) { return r.json(); })
  .then(function(data) {
    if (Array.isArray(data)) {
      state.messages = data;
      renderMessages();
      translateIncoming();
    }
  }).catch(function() {});
}

function renderMessages() {
  var area = document.getElementById('chatMessages');
  if (state.messages.length === 0) {
    area.innerHTML = '<div class="empty-state">暂无消息，发送第一条消息吧</div>';
    return;
  }
  area.innerHTML = state.messages.map(function(m) {
    var isOut = m.direction === 'out';
    var time = formatTime(m.created_at);
    var html = '<div class="msg-wrapper ' + (isOut ? 'out' : 'in') + '">' +
      '<div class="msg-bubble">' +
        '<div class="msg-original">' + esc(m.content || '') + '</div>';
    if (m._translated) {
      html += '<div class="msg-translated"><span class="lang-tag">' + (m._targetLang || 'zh').toUpperCase() + '</span>' + esc(m._translated) + '</div>';
    }
    html += '</div><div class="msg-time">' + time + '</div></div>';
    return html;
  }).join('');
  // Scroll to bottom
  area.scrollTop = area.scrollHeight;
}

function translateIncoming() {
  if (!state.chatTranslate) return;
  // Race condition fix: if config not loaded yet, retry in 500ms
  if (!state.translateConfig) { setTimeout(translateIncoming, 500); return; }
  if (state.translateConfig.receive_enabled === false) return;
  // Collect untranslated incoming messages
  var toTranslate = [];
  state.messages.forEach(function(m, i) {
    if (m.direction === 'in' && !m._translated && m.content) {
      toTranslate.push({ index: i, text: m.content });
    }
  });
  if (toTranslate.length === 0) return;
  var texts = toTranslate.map(function(t) { return t.text; });
  api('/translate/batch', {
    method: 'PUT',
    body: JSON.stringify({ texts: texts, direction: 'in' })
  }).then(function(data) {
    if (data.error) return;
    var results = data.translations || data.results || [];
    toTranslate.forEach(function(t, i) {
      if (results[i] && results[i].translated) {
        state.messages[t.index]._translated = results[i].translated;
        state.messages[t.index]._targetLang = results[i].target_lang || state.translateConfig.receive_target_lang || 'zh';
      }
    });
    renderMessages();
  }).catch(function() {});
}

// ====== Send Message ======
function sendMsg() {
  var input = document.getElementById('chatInput');
  var text = input.value.trim();
  if (!text || !state.currentChat) return;
  input.value = '';
  input.style.height = 'auto';
  document.getElementById('translatePreview').style.display = 'none';
  document.getElementById('sendBtn').disabled = true;

  function doSend(finalText) {
    api('/send', {
      method: 'POST',
      body: JSON.stringify({ chatId: state.currentChat, text: finalText })
    }).then(function(data) {
      if (data.error) { toast(data.error, true); document.getElementById('sendBtn').disabled = false; return; }
      // Add to local messages
      state.messages.push({
        direction: 'out',
        content: text,
        _translated: (finalText !== text) ? finalText : null,
        _targetLang: state.translateConfig ? state.translateConfig.send_target_lang : null,
        created_at: new Date().toISOString()
      });
      renderMessages();
      document.getElementById('sendBtn').disabled = false;
      document.getElementById('chatInput').focus();
      // Refresh chat list
      setTimeout(loadChats, 1000);
    }).catch(function() {
      toast('发送失败', true);
      document.getElementById('sendBtn').disabled = false;
    });
  }

  // If send translate is enabled, translate first
  if (state.chatTranslate && state.translateConfig && state.translateConfig.send_enabled !== false) {
    api('/translate', {
      method: 'POST',
      body: JSON.stringify({ text: text, direction: 'out' })
    }).then(function(data) {
      if (data.translated) {
        doSend(data.translated);
      } else {
        doSend(text);
      }
    }).catch(function() {
      doSend(text); // Fallback: send original
    });
  } else {
    doSend(text);
  }
}

function debounceTranslatePreview() {
  if (state.translateTimer) clearTimeout(state.translateTimer);
  state.translateTimer = setTimeout(updateTranslatePreview, 500);
}

function updateTranslatePreview() {
  var input = document.getElementById('chatInput');
  var text = input.value.trim();
  var preview = document.getElementById('translatePreview');
  if (!text || !state.chatTranslate || !state.translateConfig || state.translateConfig.send_enabled === false) {
    preview.style.display = 'none';
    return;
  }
  api('/translate', {
    method: 'POST',
    body: JSON.stringify({ text: text, direction: 'out' })
  }).then(function(data) {
    if (data.translated && data.translated !== text) {
      var targetLabel = (data.target_lang || state.translateConfig.send_target_lang || 'en').toUpperCase();
      document.getElementById('translatePreviewText').innerHTML = esc(text) + ' <span class="arrow">→</span> <span class="lang-tag preview-tag">' + targetLabel + '</span> ' + esc(data.translated);
      preview.style.display = 'block';
    } else {
      preview.style.display = 'none';
    }
  }).catch(function() { preview.style.display = 'none'; });
}

function toggleChatTranslate() {
  state.chatTranslate = !state.chatTranslate;
  var btn = document.getElementById('chatTranslateToggle');
  if (state.chatTranslate) {
    btn.textContent = '🌐 翻译中';
    btn.classList.add('on');
    translateIncoming();
  } else {
    btn.textContent = '🌐 翻译关';
    btn.classList.remove('on');
    // Remove translations from display
    state.messages.forEach(function(m) { m._translated = null; });
    renderMessages();
  }
}

// ====== Input ======
function handleInputKey(e) {
  if (e.key === 'Enter' && !e.shiftKey) {
    e.preventDefault();
    sendMsg();
  }
}

function autoResize(el) {
  el.style.height = 'auto';
  el.style.height = Math.min(el.scrollHeight, 120) + 'px';
}

// ====== Translate Config ======
function loadTranslateConfig() {
  api('/translate/config').then(function(data) {
    if (data.error) return;
    state.translateConfig = data;
    // Receive
    var recEn = document.getElementById('receiveEnabled');
    if (recEn) recEn.checked = data.receive_enabled !== false;
    var recTl = document.getElementById('receiveTargetLang');
    if (recTl) recTl.value = data.receive_target_lang || 'zh';
    var recEng = document.getElementById('receiveEngine');
    if (recEng) recEng.value = data.receive_engine || 'google';
    var recBk = document.getElementById('recBaiduApiKey');
    if (recBk) recBk.value = data.baidu_key || '';
    var recDl = document.getElementById('recDeeplApiKey');
    if (recDl) recDl.value = data.deepl_key || '';
    // Send
    var sendEn = document.getElementById('sendEnabled');
    if (sendEn) sendEn.checked = data.send_enabled !== false;
    var sendTl = document.getElementById('sendTargetLang');
    if (sendTl) sendTl.value = data.send_target_lang || 'zh';
    var sendEng = document.getElementById('sendEngine');
    if (sendEng) sendEng.value = data.send_engine || 'google';
    var sendBk = document.getElementById('sendBaiduApiKey');
    if (sendBk) sendBk.value = data.baidu_key || '';
    var sendDl = document.getElementById('sendDeeplApiKey');
    if (sendDl) sendDl.value = data.deepl_key || '';
    // AI
    var llmProv = document.getElementById('llmProvider');
    if (llmProv) llmProv.value = data.llm_provider || 'deepseek';
    var llmModel = document.getElementById('llmModel');
    if (llmModel) llmModel.value = data.llm_model || 'deepseek-chat';
    var llmKey = document.getElementById('llmApiKey');
    if (llmKey) llmKey.value = data.llm_api_key || '';
    var llmEp = document.getElementById('llmApiEndpoint');
    if (llmEp) llmEp.value = data.llm_api_endpoint || '';
    var llmSp = document.getElementById('llmSystemPrompt');
    if (llmSp) llmSp.value = data.llm_system_prompt || 'You are a professional translator. Translate the following text accurately.';
    var llmTemp = document.getElementById('llmTemperature');
    if (llmTemp) { llmTemp.value = data.llm_temperature || 0.3; document.getElementById('llmTempVal').textContent = data.llm_temperature || 0.3; }
    var llmMt = document.getElementById('llmMaxTokens');
    if (llmMt) llmMt.value = data.llm_max_tokens || 1024;
    showReceiveFields();
    showSendFields();
    showReceiveEngineFields();
    showSendEngineFields();
    showLlmEndpoint();
  }).catch(function() {});
}

function showReceiveFields() {
  var en = document.getElementById('receiveEnabled').checked;
  var el = document.getElementById('receiveFields');
  if (el) { el.style.opacity = en ? '1' : '0.4'; el.style.pointerEvents = en ? '' : 'none'; }
}
function showSendFields() {
  var en = document.getElementById('sendEnabled').checked;
  var el = document.getElementById('sendFields');
  if (el) { el.style.opacity = en ? '1' : '0.4'; el.style.pointerEvents = en ? '' : 'none'; }
}
function showReceiveEngineFields() {
  var eng = document.getElementById('receiveEngine').value;
  var baidu = document.getElementById('receiveBaiduKey');
  var deepl = document.getElementById('receiveDeepLKey');
  if (baidu) baidu.style.display = eng === 'baidu' ? '' : 'none';
  if (deepl) deepl.style.display = eng === 'deepl' ? '' : 'none';
}
function showSendEngineFields() {
  var eng = document.getElementById('sendEngine').value;
  var baidu = document.getElementById('sendBaiduKey');
  var deepl = document.getElementById('sendDeepLKey');
  if (baidu) baidu.style.display = eng === 'baidu' ? '' : 'none';
  if (deepl) deepl.style.display = eng === 'deepl' ? '' : 'none';
}
function showLlmEndpoint() {
  var prov = document.getElementById('llmProvider').value;
  var row = document.getElementById('llmEndpointRow');
  var ep = document.getElementById('llmApiEndpoint');
  if (row) row.style.display = prov === 'custom' ? '' : 'none';
  if (ep) {
    if (prov === 'deepseek') ep.value = 'https://api.deepseek.com/v1';
    else if (prov === 'openai') ep.value = 'https://api.openai.com/v1';
    else if (prov === 'anthropic') ep.value = 'https://api.anthropic.com/v1';
  }
}

function saveTranslateConfig() {
  var body = {
    receive_enabled: document.getElementById('receiveEnabled').checked,
    receive_target_lang: document.getElementById('receiveTargetLang').value,
    receive_engine: document.getElementById('receiveEngine').value,
    send_enabled: document.getElementById('sendEnabled').checked,
    send_target_lang: document.getElementById('sendTargetLang').value,
    send_engine: document.getElementById('sendEngine').value,
    baidu_key: document.getElementById('recBaiduApiKey').value || document.getElementById('sendBaiduApiKey').value,
    deepl_key: document.getElementById('recDeeplApiKey').value || document.getElementById('sendDeeplApiKey').value,
    llm_provider: document.getElementById('llmProvider').value,
    llm_model: document.getElementById('llmModel').value,
    llm_api_key: document.getElementById('llmApiKey').value,
    llm_api_endpoint: document.getElementById('llmApiEndpoint').value,
    llm_system_prompt: document.getElementById('llmSystemPrompt').value,
    llm_temperature: parseFloat(document.getElementById('llmTemperature').value) || 0.3,
    llm_max_tokens: parseInt(document.getElementById('llmMaxTokens').value) || 1024
  };
  api('/translate/config', { method: 'PUT', body: JSON.stringify(body) }).then(function(data) {
    if (data.error) { toast(data.error, true); return; }
    state.translateConfig = body;
    toast('翻译设置已保存');
  }).catch(function() { toast('保存失败', true); });
}

function testTranslation() {
  var text = prompt('输入测试文本:');
  if (!text) return;
  api('/translate', { method: 'POST', body: JSON.stringify({ text: text, direction: 'out' }) }).then(function(data) {
    if (data.error) { toast(data.error, true); return; }
    toast('翻译结果: ' + (data.translated || text));
  }).catch(function() { toast('翻译测试失败', true); });
}

// ====== CRM ======
function loadCustomers() {
  var search = document.getElementById('crmSearch') ? document.getElementById('crmSearch').value : '';
  var tag = document.getElementById('crmFilterTag') ? document.getElementById('crmFilterTag').value : '';
  var params = [];
  if (search) params.push('search=' + encodeURIComponent(search));
  if (tag) params.push('tag=' + encodeURIComponent(tag));
  var qs = params.length ? '?' + params.join('&') : '';
  api('/customers' + qs).then(function(data) {
    state.customers = data.customers || [];
    document.getElementById('crmTotal').textContent = state.customers.length;
    document.getElementById('crmActive').textContent = state.customers.filter(function(c) { return c.status === 'active'; }).length;
    document.getElementById('crmNew').textContent = state.customers.filter(function(c) { return c.status === 'new'; }).length;
    renderCustomerTable();
  }).catch(function() {});
}

function renderCustomerTable() {
  var tbody = document.getElementById('crmTableBody');
  if (!state.customers.length) {
    tbody.innerHTML = '<tr><td colspan="6" style="text-align:center;padding:40px;color:#5a6a7a">暂无客户数据</td></tr>';
    return;
  }
  // Build tag filter
  var allTags = {};
  state.customers.forEach(function(c) {
    var tags = Array.isArray(c.tags) ? c.tags : (typeof c.tags === 'string' ? parseTags(c.tags) : []);
    tags.forEach(function(t) { t = (t||'').trim(); if (t) allTags[t] = true; });
  });
  var tagSel = document.getElementById('crmFilterTag');
  var curVal = tagSel.value;
  tagSel.innerHTML = '<option value="">全部标签</option>' + Object.keys(allTags).sort().map(function(t) { return '<option value="' + t + '">' + t + '</option>'; }).join('');
  tagSel.value = curVal;

  tbody.innerHTML = state.customers.map(function(c) {
    var tags = Array.isArray(c.tags) ? c.tags : (typeof c.tags === 'string' ? parseTags(c.tags) : []);
    var tagHtml = tags.map(function(t) { return '<span class="tag tag-active">' + esc(t) + '</span>'; }).join(' ');
    var statusCls = c.status === 'inactive' ? 'tag-inactive' : 'tag-active';
    var statusText = c.status === 'inactive' ? '非活跃' : (c.status === 'active' ? '活跃' : (c.status === 'new' ? '新客户' : (c.status || '--')));
    var jidShort = (c.whatsapp_jid || '').replace('@c.us','').replace('@s.whatsapp.net','');
    return '<tr>' +
      '<td><a href="#" onclick="openCustomerDetail(' + c.id + ');return false">' + esc(c.display_name || jidShort) + '</a></td>' +
      '<td>' + esc(jidShort) + '</td>' +
      '<td>' + esc(c.country || '--') + '</td>' +
      '<td>' + (tagHtml || '--') + '</td>' +
      '<td><span class="tag ' + statusCls + '">' + statusText + '</span></td>' +
      '<td>' +
        '<button onclick="openCustomerModal(' + c.id + ')" style="background:transparent;border:1px solid #374248;color:#8696a0;padding:4px 8px;border-radius:4px;cursor:pointer;font-size:11px;margin-right:4px">编辑</button>' +
        '<button onclick="deleteCustomer(' + c.id + ')" style="background:transparent;border:1px solid #e74c3c;color:#e74c3c;padding:4px 8px;border-radius:4px;cursor:pointer;font-size:11px">删除</button>' +
      '</td></tr>';
  }).join('');
}

function parseTags(val) {
  try { return JSON.parse(val); } catch(e) { return []; }
}

function openCustomerModal(id) {
  var modal = document.getElementById('customerModal');
  document.getElementById('customerEditId').value = id || '';
  if (id) {
    document.getElementById('customerModalTitle').textContent = '编辑客户';
    var c = state.customers.find(function(x) { return x.id == id; });
    if (c) {
      var tags = Array.isArray(c.tags) ? c.tags.join(',') : (typeof c.tags === 'string' ? (function(){try{return JSON.parse(c.tags).join(',')}catch(e){return c.tags}})() : '');
      document.getElementById('custDisplayName').value = c.display_name || '';
      document.getElementById('custJid').value = c.whatsapp_jid || '';
      document.getElementById('custPhone').value = c.phone || '';
      document.getElementById('custEmail').value = c.email || '';
      document.getElementById('custCountry').value = c.country || '';
      document.getElementById('custTags').value = tags;
      document.getElementById('custStatus').value = c.status || 'new';
      document.getElementById('custNotes').value = c.notes || '';
    }
  } else {
    document.getElementById('customerModalTitle').textContent = '添加客户';
    ['custDisplayName','custJid','custPhone','custEmail','custCountry','custTags','custNotes'].forEach(function(f) {
      document.getElementById(f).value = '';
    });
    document.getElementById('custStatus').value = 'new';
  }
  modal.classList.add('show');
}

function closeCustomerModal() {
  document.getElementById('customerModal').classList.remove('show');
}

function saveCustomer() {
  var id = document.getElementById('customerEditId').value;
  var jid = document.getElementById('custJid').value.trim();
  if (!jid) { toast('请输入 WhatsApp JID', true); return; }
  var tagsRaw = document.getElementById('custTags').value.trim();
  var tags = tagsRaw ? tagsRaw.split(',').map(function(t) { return t.trim(); }).filter(Boolean) : [];
  var body = {
    whatsapp_jid: jid,
    display_name: document.getElementById('custDisplayName').value.trim(),
    phone: document.getElementById('custPhone').value.trim(),
    email: document.getElementById('custEmail').value.trim(),
    country: document.getElementById('custCountry').value.trim(),
    tags: tags,
    status: document.getElementById('custStatus').value,
    notes: document.getElementById('custNotes').value.trim()
  };
  var method = id ? 'PUT' : 'POST';
  var path = id ? '/customers/' + id : '/customers';
  api(path, { method: method, body: JSON.stringify(body) }).then(function(data) {
    if (data.error) { toast(data.error, true); return; }
    toast(id ? '客户已更新' : '客户已创建');
    closeCustomerModal();
    loadCustomers();
  }).catch(function() { toast('保存失败', true); });
}

function deleteCustomer(id) {
  if (!confirm('确定删除此客户？')) return;
  api('/customers/' + id, { method: 'DELETE' }).then(function(data) {
    if (data.error) { toast(data.error, true); return; }
    toast('客户已删除');
    loadCustomers();
  }).catch(function() { toast('删除失败', true); });
}

function openCustomerDetail(id) {
  state.detailCustomerId = id;
  document.getElementById('detailModal').classList.add('show');
  api('/customers/' + id).then(function(data) {
    if (data.error) { toast(data.error, true); return; }
    var c = data.customer || data;
    var tags = Array.isArray(c.tags) ? c.tags.join(', ') : (typeof c.tags === 'string' ? (function(){try{return JSON.parse(c.tags).join(', ')}catch(e){return c.tags}})() : '');
    var jidShort = (c.whatsapp_jid || '').replace('@c.us','').replace('@s.whatsapp.net','');
    document.getElementById('detailName').textContent = c.display_name || jidShort;
    document.getElementById('detailContent').innerHTML =
      '<div style="display:grid;grid-template-columns:1fr 1fr;gap:8px;font-size:13px">' +
      '<div><span style="color:#8696a0">WhatsApp：</span>' + esc(jidShort) + '</div>' +
      '<div><span style="color:#8696a0">手机号：</span>' + esc(c.phone || '--') + '</div>' +
      '<div><span style="color:#8696a0">邮箱：</span>' + esc(c.email || '--') + '</div>' +
      '<div><span style="color:#8696a0">国家：</span>' + esc(c.country || '--') + '</div>' +
      '<div><span style="color:#8696a0">标签：</span>' + (tags || '--') + '</div>' +
      '<div><span style="color:#8696a0">状态：</span>' + (c.status === 'inactive' ? '非活跃' : (c.status === 'active' ? '活跃' : (c.status === 'new' ? '新客户' : (c.status || '--')))) + '</div>' +
      '<div><span style="color:#8696a0">备注：</span>' + esc(c.notes || '--') + '</div>' +
      '<div><span style="color:#8696a0">待处理提醒：</span>' + (c.pending_reminders || 0) + ' 条</div>' +
      '</div>';
    loadReminders(id);
    loadTimeline(id);
  });
}

function closeDetailModal() {
  document.getElementById('detailModal').classList.remove('show');
  state.detailCustomerId = null;
}

function loadReminders(customerId) {
  api('/customers/' + customerId + '/reminders').then(function(data) {
    var reminders = data.reminders || [];
    var div = document.getElementById('detailReminders');
    if (reminders.length === 0) { div.innerHTML = '<div style="color:#5a6a7a;font-size:12px">暂无提醒</div>'; return; }
    div.innerHTML = reminders.map(function(r) {
      var checkbox = r.completed ? '☑' : '<a href="#" onclick="completeReminder(' + r.id + ');return false" style="color:#00a884">☐</a>';
      return '<div style="padding:4px 0;font-size:13px;display:flex;justify-content:space-between"><span>' + checkbox + ' ' + esc(r.content) + '</span><span style="font-size:11px;color:#5a6a7a">' + (r.created_at || '') + '</span></div>';
    }).join('');
  });
}

function addReminder() {
  var text = document.getElementById('reminderText').value.trim();
  if (!text || !state.detailCustomerId) return;
  api('/customers/' + state.detailCustomerId + '/reminders', { method: 'POST', body: JSON.stringify({ content: text }) }).then(function(data) {
    if (data.error) { toast(data.error, true); return; }
    document.getElementById('reminderText').value = '';
    loadReminders(state.detailCustomerId);
  });
}

function completeReminder(reminderId) {
  if (!state.detailCustomerId) return;
  api('/customers/' + state.detailCustomerId + '/reminders/' + reminderId + '/complete', { method: 'PUT' }).then(function() {
    loadReminders(state.detailCustomerId);
  });
}

function loadTimeline(customerId) {
  api('/customers/' + customerId + '/timeline').then(function(data) {
    var events = data.events || [];
    var div = document.getElementById('detailTimeline');
    if (events.length === 0) { div.innerHTML = '<div style="color:#5a6a7a;font-size:12px">暂无交互记录</div>'; return; }
    div.innerHTML = events.map(function(e) {
      return '<div style="padding:6px 0;border-bottom:1px solid #1f2c33;font-size:13px"><span style="color:#5a6a7a;font-size:11px">' + (e.created_at || '') + '</span> ' + esc(e.event || '') + '</div>';
    }).join('');
  });
}

// ====== Helpers ======
function esc(s) {
  return (s || '').replace(/&/g,'&amp;').replace(/</g,'&lt;').replace(/>/g,'&gt;').replace(/"/g,'&quot;').replace(/'/g,'&#39;');
}
function escAttr(s) {
  return (s || '').replace(/&/g,'&amp;').replace(/"/g,'&quot;').replace(/'/g,'&#39;').replace(/</g,'&lt;').replace(/>/g,'&gt;');
}
function formatTime(ts) {
  if (!ts) return '';
  var d = new Date(ts);
  if (isNaN(d.getTime())) return ts;
  var now = new Date();
  var hh = String(d.getHours()).padStart(2,'0');
  var mm = String(d.getMinutes()).padStart(2,'0');
  if (d.toDateString() === now.toDateString()) return hh + ':' + mm;
  var yesterday = new Date(now); yesterday.setDate(now.getDate()-1);
  if (d.toDateString() === yesterday.toDateString()) return '昨天';
  return (d.getMonth()+1) + '/' + d.getDate();
}
function colorFor(s) {
  var colors = ['#00a884','#6c5ce7','#e17055','#0984e3','#d63031','#00b894','#fdcb6e','#2d3436','#e84393','#636e72'];
  var h = 0;
  for (var i = 0; i < (s||'').length; i++) h = ((h << 5) - h + s.charCodeAt(i)) | 0;
  return colors[Math.abs(h) % colors.length];
}
function debounce(fn, ms) {
  var t;
  return function() { var args=arguments; clearTimeout(t); t = setTimeout(function() { fn.apply(null, args); }, ms); };
}

// ====== Startup ======
document.addEventListener('DOMContentLoaded', function() {
  init();
  // Enter key for login
  var loginPass = document.getElementById('loginPass');
  if (loginPass) loginPass.addEventListener('keydown', function(e) {
    if (e.key === 'Enter') doLogin();
  });
});
