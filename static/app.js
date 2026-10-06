/* FetchGram frontend */
const $ = (id) => document.getElementById(id);
const state = {
  mode: null,          // 'profile' | 'posts'
  username: null,
  cursor: null,
  hasMore: false,
  posts: [],           // all loaded post cards
  selected: new Set(), // shortcodes
  filter: 'all',
};

function fmt(n) {
  if (n == null) return '';
  if (n >= 1e9) return (n / 1e9).toFixed(1) + 'B';
  if (n >= 1e6) return (n / 1e6).toFixed(1) + 'M';
  if (n >= 1e3) return (n / 1e3).toFixed(1) + 'K';
  return String(n);
}
function show(el) { el.classList.remove('hidden'); }
function hide(el) { el.classList.add('hidden'); }
function toast(msg) {
  const t = $('toast');
  t.textContent = msg; show(t);
  clearTimeout(t._h); t._h = setTimeout(() => hide(t), 3200);
}
function setStep(n) {
  document.querySelectorAll('.step').forEach(s =>
    s.classList.toggle('active', +s.dataset.step <= n));
}
function showError(msg) {
  const e = $('errorBox');
  e.textContent = msg; show(e);
}
function clearError() { hide($('errorBox')); }
function setLoading(on, text) {
  if (text) $('loadingText').textContent = text;
  $('loadingBox').classList.toggle('hidden', !on);
  $('searchBtn').disabled = on;
}
async function api(path, opts) {
  const r = await fetch(path, opts);
  const data = await r.json().catch(() => ({}));
  if (!r.ok) throw new Error(data.detail || `Request failed (${r.status})`);
  return data;
}
const thumbUrl = (u) => u ? `/api/thumb?url=${encodeURIComponent(u)}` : '';

/* ---------- search ---------- */
$('searchForm').addEventListener('submit', async (e) => {
  e.preventDefault();
  const q = $('searchInput').value.trim();
  if (!q) return;
  clearError();
  resetResults();
  setLoading(true, 'Fetching…');
  setStep(1);
  try {
    const data = await api(`/api/resolve?input=${encodeURIComponent(q)}`);
    if (data.type === 'profile') {
      state.mode = 'profile';
      state.username = data.profile.username;
      renderProfile(data.profile);
      await loadGridPage(true);
      setStep(2);
    } else {
      state.mode = 'posts';
      state.posts = data.posts || [];
      hide($('profileCard'));
      renderGrid();
      show($('toolbar'));
      setStep(2);
      if (!state.posts.length) showError('No posts found in that input.');
    }
  } catch (err) {
    showError(err.message);
    setStep(1);
  } finally {
    setLoading(false);
  }
});

function resetResults() {
  state.posts = []; state.selected.clear(); state.cursor = null;
  state.hasMore = false; state.filter = 'all';
  document.querySelectorAll('.tab').forEach(t =>
    t.classList.toggle('active', t.dataset.filter === 'all'));
  $('grid').innerHTML = '';
  hide($('toolbar')); hide($('profileCard')); hide($('moreWrap'));
  updateDlBar();
}

function renderProfile(p) {
  $('profAvatar').src = thumbUrl(p.avatar);
  $('profName').textContent = p.full_name || p.username;
  $('profVerified').classList.toggle('hidden', !p.is_verified);
  $('profUser').textContent = '@' + p.username;
  $('profBio').textContent = p.bio || '';
  $('statPosts').textContent = fmt(p.post_count);
  $('statFollowers').textContent = fmt(p.followers);
  $('statFollowing').textContent = fmt(p.following);
  show($('profileCard'));
}

/* ---------- grid ---------- */
async function loadGridPage(first) {
  setLoading(true, first ? 'Loading posts…' : 'Loading more…');
  try {
    const url = `/api/profile/${encodeURIComponent(state.username)}?limit=12` +
      (state.cursor ? `&cursor=${encodeURIComponent(state.cursor)}` : '');
    const data = await api(url);
    if (first) state.posts = [];
    // merge without duplicates
    const seen = new Set(state.posts.map(p => p.shortcode));
    for (const p of data.posts) if (!seen.has(p.shortcode)) state.posts.push(p);
    state.cursor = data.next_cursor;
    state.hasMore = data.has_more;
    $('moreWrap').classList.toggle('hidden', !state.hasMore);
    renderGrid();
    show($('toolbar'));
  } catch (err) {
    showError(err.message);
  } finally {
    setLoading(false);
  }
}
$('loadMoreBtn').addEventListener('click', () => loadGridPage(false));

const KIND_LABEL = { photo: 'Photo', video: 'Video', reel: 'Reel', carousel: 'Carousel' };

function visiblePosts() {
  if (state.filter === 'all') return state.posts;
  return state.posts.filter(p => p.kind === state.filter);
}

function renderGrid() {
  const grid = $('grid');
  grid.innerHTML = '';
  for (const p of visiblePosts()) {
    if (p.error) {
      const d = document.createElement('div');
      d.className = 'gitem err'; d.textContent = '⚠ ' + p.error;
      grid.appendChild(d);
      continue;
    }
    const d = document.createElement('div');
    d.className = 'gitem' + (state.selected.has(p.shortcode) ? ' selected' : '');
    d.title = (p.caption || '').slice(0, 120);
    d.innerHTML = `
      <span class="check">✓</span>
      <span class="badge">${KIND_LABEL[p.kind] || p.kind}</span>
      ${p.thumbnail ? `<img loading="lazy" src="${thumbUrl(p.thumbnail)}" alt="">` : ''}
      <div class="gmeta"><span>❤ ${fmt(p.likes)}</span><span>💬 ${fmt(p.comments)}</span></div>`;
    d.addEventListener('click', () => {
      if (state.selected.has(p.shortcode)) state.selected.delete(p.shortcode);
      else state.selected.add(p.shortcode);
      d.classList.toggle('selected');
      updateDlBar();
      if (state.selected.size) setStep(3); else setStep(2);
    });
    grid.appendChild(d);
  }
  updateDlBar();
}

document.querySelectorAll('.tab').forEach(t =>
  t.addEventListener('click', () => {
    document.querySelectorAll('.tab').forEach(x => x.classList.remove('active'));
    t.classList.add('active');
    state.filter = t.dataset.filter;
    renderGrid();
  }));

$('selectAllBtn').addEventListener('click', () => {
  visiblePosts().forEach(p => { if (!p.error) state.selected.add(p.shortcode); });
  renderGrid(); setStep(3);
});
$('clearBtn').addEventListener('click', () => {
  state.selected.clear(); renderGrid(); setStep(2);
});

/* ---------- download bar ---------- */
function updateDlBar() {
  const n = state.selected.size;
  $('selCount').textContent = n;
  $('dlbar').classList.toggle('hidden', n === 0);
}

$('downloadBtn').addEventListener('click', async () => {
  const items = [...state.selected];
  if (!items.length) return;
  const btn = $('downloadBtn');
  btn.disabled = true; btn.textContent = '⏳ Preparing…';
  try {
    const r = await fetch('/api/download', {
      method: 'POST',
      headers: { 'Content-Type': 'application/json' },
      body: JSON.stringify({ items }),
    });
    if (!r.ok) {
      const d = await r.json().catch(() => ({}));
      throw new Error(d.detail || `Download failed (${r.status})`);
    }
    const blob = await r.blob();
    let name = 'fetchgram_download';
    const cd = r.headers.get('Content-Disposition') || '';
    const m = cd.match(/filename="?([^";]+)"?/);
    if (m) name = m[1];
    const a = document.createElement('a');
    a.href = URL.createObjectURL(blob);
    a.download = name;
    document.body.appendChild(a); a.click(); a.remove();
    setTimeout(() => URL.revokeObjectURL(a.href), 5000);
    toast(`Downloaded ${items.length} item${items.length > 1 ? 's' : ''} 🎉`);
  } catch (err) {
    showError(err.message);
  } finally {
    btn.disabled = false; btn.textContent = '⬇ Download';
  }
});
