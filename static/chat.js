/**
 * chat.js — Workout Trainer Agent frontend logic
 *
 * Handles:
 *   - Login / Register (tab switching, form submission)
 *   - JWT storage in sessionStorage
 *   - Chat message rendering (basic markdown)
 *   - SSE streaming from POST /chat
 *   - Textarea auto-resize
 *   - Logout
 */

const API = '';   // empty = same origin; set to 'http://localhost:8000' for dev if needed

// ── state ─────────────────────────────────────────────────────────────────────
let authToken    = sessionStorage.getItem('wt_token')    || null;
let authUsername = sessionStorage.getItem('wt_username') || null;
let activeTab    = 'login';
let isStreaming  = false;

// ── boot ──────────────────────────────────────────────────────────────────────
(function init() {
  if (authToken && authUsername) {
    showChat();
  } else {
    showAuth();
  }

  // allow Enter to submit the auth form
  document.getElementById('auth-password').addEventListener('keydown', e => {
    if (e.key === 'Enter') { e.preventDefault(); handleAuth(); }
  });
  document.getElementById('auth-username').addEventListener('keydown', e => {
    if (e.key === 'Enter') { e.preventDefault(); document.getElementById('auth-password').focus(); }
  });
})();

// ── screen switching ──────────────────────────────────────────────────────────
function showAuth() {
  document.getElementById('auth-screen').style.display = 'flex';
  document.getElementById('chat-screen').style.display = 'none';
  document.getElementById('auth-username').focus();
}

function showChat() {
  document.getElementById('auth-screen').style.display = 'none';
  document.getElementById('chat-screen').style.display = 'flex';
  document.getElementById('username-label').textContent = authUsername;
  document.getElementById('msg-input').focus();
}

// ── tab switching ─────────────────────────────────────────────────────────────
function switchTab(tab) {
  activeTab = tab;
  document.getElementById('tab-login').classList.toggle('active', tab === 'login');
  document.getElementById('tab-register').classList.toggle('active', tab === 'register');
  document.getElementById('auth-submit-btn').textContent = tab === 'login' ? 'Login' : 'Create account';
  clearAuthError();
}

function clearAuthError() {
  const el = document.getElementById('auth-error');
  el.style.display = 'none';
  el.textContent = '';
}

function showAuthError(msg) {
  const el = document.getElementById('auth-error');
  el.textContent = msg;
  el.style.display = 'block';
}

// ── auth ──────────────────────────────────────────────────────────────────────
async function handleAuth() {
  const username = document.getElementById('auth-username').value.trim();
  const password = document.getElementById('auth-password').value;
  const btn      = document.getElementById('auth-submit-btn');

  clearAuthError();

  if (!username || !password) {
    showAuthError('Please enter a username and password.');
    return;
  }

  btn.disabled = true;
  btn.textContent = 'Please wait…';

  const endpoint = activeTab === 'login' ? '/auth/login' : '/auth/register';

  try {
    const res  = await fetch(API + endpoint, {
      method:  'POST',
      headers: { 'Content-Type': 'application/json' },
      body:    JSON.stringify({ username, password }),
    });
    const data = await res.json();

    if (!res.ok) {
      showAuthError(data.detail || 'Something went wrong.');
      return;
    }

    authToken    = data.token;
    authUsername = data.username;
    sessionStorage.setItem('wt_token',    authToken);
    sessionStorage.setItem('wt_username', authUsername);
    showChat();

  } catch (err) {
    showAuthError('Could not reach the server. Is it running?');
  } finally {
    btn.disabled = false;
    btn.textContent = activeTab === 'login' ? 'Login' : 'Create account';
  }
}

// ── logout ────────────────────────────────────────────────────────────────────
function logout() {
  authToken    = null;
  authUsername = null;
  sessionStorage.removeItem('wt_token');
  sessionStorage.removeItem('wt_username');
  // clear messages
  const msgs = document.getElementById('messages');
  msgs.innerHTML = '';
  msgs.appendChild(buildEmptyState());
  showAuth();
}

// ── message rendering ─────────────────────────────────────────────────────────

/** Minimal Markdown → HTML converter (safe subset). */
function parseMarkdown(text) {
  // Escape HTML first
  let html = text
    .replace(/&/g, '&amp;')
    .replace(/</g, '&lt;')
    .replace(/>/g, '&gt;');

  // Fenced code blocks
  html = html.replace(/```[\w]*\n([\s\S]*?)```/g, (_, code) =>
    `<pre><code>${code.trimEnd()}</code></pre>`
  );

  // Inline code
  html = html.replace(/`([^`]+)`/g, '<code>$1</code>');

  // Bold and italic
  html = html.replace(/\*\*\*(.+?)\*\*\*/g, '<strong><em>$1</em></strong>');
  html = html.replace(/\*\*(.+?)\*\*/g,     '<strong>$1</strong>');
  html = html.replace(/\*(.+?)\*/g,          '<em>$1</em>');

  // Headings (### ## #)
  html = html.replace(/^### (.+)$/gm, '<h3>$1</h3>');
  html = html.replace(/^## (.+)$/gm,  '<h2>$1</h2>');
  html = html.replace(/^# (.+)$/gm,   '<h1>$1</h1>');

  // Horizontal rule
  html = html.replace(/^---+$/gm, '<hr>');

  // Unordered lists  (- item or * item)
  html = html.replace(/^[\-\*] (.+)$/gm, '<li>$1</li>');
  html = html.replace(/(<li>[\s\S]*?<\/li>)/g, '<ul>$1</ul>');
  // collapse consecutive <ul> wraps
  html = html.replace(/<\/ul>\s*<ul>/g, '');

  // Ordered lists
  html = html.replace(/^\d+\. (.+)$/gm, '<li>$1</li>');

  // Paragraphs — wrap bare lines not already in a block tag
  const blockRe = /^<(h[123]|ul|ol|li|pre|hr|p)/;
  html = html
    .split('\n')
    .map(line => {
      if (blockRe.test(line) || line.trim() === '') return line;
      return `<p>${line}</p>`;
    })
    .join('\n');

  // Collapse repeated blank lines
  html = html.replace(/(<\/p>\s*){2,}/g, '</p>');

  return html;
}

function buildEmptyState() {
  const div = document.createElement('div');
  div.className = 'empty-state';
  div.id        = 'empty-state';
  div.innerHTML = `
    <div class="empty-icon">🏋️</div>
    <h2>Ready when you are</h2>
    <p>Tell me about your fitness goals and I'll build a personalised 7-day workout plan.</p>
    <div class="suggestions">
      <button class="suggestion-chip" onclick="sendSuggestion(this)">Start my first plan</button>
      <button class="suggestion-chip" onclick="sendSuggestion(this)">Show today's workout</button>
      <button class="suggestion-chip" onclick="sendSuggestion(this)">I hit a new PR today</button>
      <button class="suggestion-chip" onclick="sendSuggestion(this)">Modify my plan</button>
    </div>`;
  return div;
}

function appendMessage(role, text) {
  // remove empty state on first message
  const empty = document.getElementById('empty-state');
  if (empty) empty.remove();

  const msgs   = document.getElementById('messages');
  const row    = document.createElement('div');
  row.className = `msg-row ${role}`;

  const avatar = document.createElement('div');
  avatar.className = `avatar ${role === 'user' ? 'user' : 'agent'}`;
  avatar.textContent = role === 'user' ? '👤' : '🤖';

  const bubble = document.createElement('div');
  bubble.className = `bubble ${role === 'user' ? 'user' : 'agent'}`;

  if (role === 'agent') {
    bubble.innerHTML = parseMarkdown(text);
  } else {
    bubble.textContent = text;
  }

  row.appendChild(avatar);
  row.appendChild(bubble);
  msgs.appendChild(row);
  scrollToBottom();
  return bubble;
}

function appendTypingIndicator() {
  const empty = document.getElementById('empty-state');
  if (empty) empty.remove();

  const msgs = document.getElementById('messages');
  const row  = document.createElement('div');
  row.className = 'msg-row agent';
  row.id = 'typing-row';

  const avatar = document.createElement('div');
  avatar.className = 'avatar agent';
  avatar.textContent = '🤖';

  const bubble = document.createElement('div');
  bubble.className = 'bubble agent';
  bubble.innerHTML = `<div class="typing-dots"><span></span><span></span><span></span></div>`;

  row.appendChild(avatar);
  row.appendChild(bubble);
  msgs.appendChild(row);
  scrollToBottom();
  return bubble;
}

function removeTypingIndicator() {
  const row = document.getElementById('typing-row');
  if (row) row.remove();
}

function scrollToBottom() {
  const msgs = document.getElementById('messages');
  msgs.scrollTop = msgs.scrollHeight;
}

// ── send message ──────────────────────────────────────────────────────────────
async function sendMessage(text) {
  const input   = document.getElementById('msg-input');
  const sendBtn = document.getElementById('send-btn');
  const message = (text || input.value).trim();

  if (!message || isStreaming) return;

  // clear input and reset height
  input.value = '';
  input.style.height = 'auto';
  isStreaming = true;
  sendBtn.disabled = true;

  appendMessage('user', message);
  const agentBubble = appendTypingIndicator();
  let   agentText   = '';
  let   firstChunk  = true;

  try {
    const res = await fetch(API + '/chat', {
      method:  'POST',
      headers: {
        'Content-Type':  'application/json',
        'Authorization': `Bearer ${authToken}`,
      },
      body: JSON.stringify({ message }),
    });

    if (res.status === 401) {
      logout();
      return;
    }

    if (!res.ok) {
      removeTypingIndicator();
      appendMessage('agent', '⚠️ Server error. Please try again.');
      return;
    }

    const reader  = res.body.getReader();
    const decoder = new TextDecoder();
    let   buffer  = '';

    while (true) {
      const { done, value } = await reader.read();
      if (done) break;

      buffer += decoder.decode(value, { stream: true });

      // SSE lines are separated by \n\n
      const lines = buffer.split('\n\n');
      buffer = lines.pop();   // keep incomplete chunk

      for (const line of lines) {
        if (!line.startsWith('data: ')) continue;
        try {
          const payload = JSON.parse(line.slice(6));

          if (payload.error) {
            removeTypingIndicator();
            appendMessage('agent', `⚠️ ${payload.error}`);
            return;
          }

          if (payload.text) {
            if (firstChunk) {
              // replace typing indicator with real bubble
              removeTypingIndicator();
              agentText   = payload.text;
              firstChunk  = false;
              // create the real agent bubble that we'll update in place
              const empty = document.getElementById('empty-state');
              if (empty) empty.remove();

              const msgs   = document.getElementById('messages');
              const row    = document.createElement('div');
              row.className = 'msg-row agent';

              const avatar = document.createElement('div');
              avatar.className = 'avatar agent';
              avatar.textContent = '🤖';

              const bubble = document.createElement('div');
              bubble.className = 'bubble agent';
              bubble.innerHTML = parseMarkdown(agentText);

              row.appendChild(avatar);
              row.appendChild(bubble);
              msgs.appendChild(row);

              // keep a reference so we can update it
              agentBubble._live = bubble;

            } else {
              agentText += payload.text;
              if (agentBubble._live) {
                agentBubble._live.innerHTML = parseMarkdown(agentText);
              }
            }
            scrollToBottom();
          }

          if (payload.done) break;

        } catch (_) {
          // malformed JSON chunk, skip
        }
      }
    }

  } catch (err) {
    removeTypingIndicator();
    appendMessage('agent', '⚠️ Connection lost. Please check the server.');
    console.error(err);
  } finally {
    isStreaming     = false;
    sendBtn.disabled = false;
    input.focus();
  }
}

// ── suggestion chips ──────────────────────────────────────────────────────────
function sendSuggestion(btn) {
  sendMessage(btn.textContent);
}

// ── keyboard handling ─────────────────────────────────────────────────────────
function handleKey(e) {
  if (e.key === 'Enter' && !e.shiftKey) {
    e.preventDefault();
    sendMessage();
  }
}

// ── textarea auto-resize ──────────────────────────────────────────────────────
function autoResize(el) {
  el.style.height = 'auto';
  el.style.height = Math.min(el.scrollHeight, 140) + 'px';
}