/* Contract Register — Vue 3 + Element Plus, no build step */
const { createApp, reactive, ref, computed, watch, onMounted, onBeforeUnmount, provide, inject, nextTick } = Vue;
const { ElMessage, ElMessageBox } = ElementPlus;

// ---------- shared state ----------
const store = reactive({
  lang: (() => { try { return localStorage.getItem('cms_lang') || 'zh'; } catch (e) { return 'zh'; } })(),
  route: { name: 'list', id: null, query: {} },
  user: null, needsSetup: false, authChecked: false, pendingCount: 0,
});
function t(k, vars) {
  let s = (I18N[store.lang] && I18N[store.lang][k]) || I18N.zh[k] || k;
  if (vars) for (const v in vars) s = s.replace('{' + v + '}', vars[v]);
  return s;
}
const TYPES = ['software', 'purchase', 'lease', 'service', 'other'];
const STATUSES = ['queued', 'reading', 'extracting', 'review', 'confirmed', 'failed'];
const LIFECYCLES = ['draft', 'active', 'completed', 'terminated'];
const WORKING = ['queued', 'reading', 'extracting'];
const ROLES = ['admin', 'editor', 'viewer'];
const ACTIONS = ['register', 'user_approve', 'user_reject', 'ai_question', 'upload', 'departments', 'dept_create', 'dept_update', 'dept_delete', 'role_create', 'role_update', 'role_delete', 'extracted', 'extract_failed', 'edit', 'confirm', 'unconfirm', 'reextract', 'reread', 'delete',
  'lifecycle_status', 'notes', 'login', 'login_failed', 'setup', 'user_create', 'user_update', 'password_change', 'settings', 'export', 'backup'];
// permission check: 'admin' = administrators; otherwise a permission key such as 'contract.upload'
const can = p => !!store.user && (p === 'admin' ? store.user.role === 'admin' : (store.user.permissions || []).includes(p));

function fmt(n, cur) {
  if (n === null || n === undefined || n === '' || isNaN(Number(n))) return '';
  const s = Number(n).toLocaleString('en-US', { minimumFractionDigits: 0, maximumFractionDigits: 2 });
  return cur ? cur + ' ' + s : s;
}
function num(v) {
  if (v === null || v === undefined || v === '') return null;
  const x = Number(String(v).replace(/[^0-9.\-]/g, ''));
  return isNaN(x) ? null : x;
}
function esc(s) { return String(s == null ? '' : s).replace(/&/g, '&amp;').replace(/</g, '&lt;').replace(/>/g, '&gt;'); }
function highlight(text, q) {
  let s = esc(text);
  (q || '').split(/\s+/).filter(Boolean).forEach(w => {
    const re = new RegExp(w.replace(/[.*+?^${}()|[\]\\]/g, '\\$&'), 'gi');
    s = s.replace(re, m => '<mark>' + m + '</mark>');
  });
  return s;
}
async function api(path, opts = {}) {
  const o = Object.assign({}, opts);
  if (o.json !== undefined) { o.body = JSON.stringify(o.json); o.headers = { 'Content-Type': 'application/json' }; delete o.json; }
  const r = await fetch(path, o);
  if (r.status === 401 && !path.startsWith('/api/auth/')) { store.user = null; throw new Error(t('loginTitle')); }
  if (!r.ok) {
    let msg = r.status + '';
    try { const j = await r.json(); msg = j.detail || JSON.stringify(j); } catch (e) {}
    throw new Error(msg);
  }
  return r.json();
}
function statusTag(s) {
  return { review: 'warning', confirmed: 'success', failed: 'danger', queued: 'info', reading: 'primary', extracting: 'primary' }[s] || 'info';
}
function go(name, id, query) {
  let h = name === 'list' ? '#/' : name === 'detail' ? '#/c/' + id : name === 'ai' ? '#/ai' + (id ? '/' + id : '') : '#/' + name;
  if (query) h += '?' + new URLSearchParams(query).toString();
  location.hash = h;
}
function parseHash() {
  const raw = location.hash.replace(/^#\/?/, '');
  const [h, qs] = raw.split('?');
  const query = Object.fromEntries(new URLSearchParams(qs || ''));
  const m = h.match(/^c\/(\d+)/);
  if (m) return { name: 'detail', id: Number(m[1]), query };
  const ma = h.match(/^ai(?:\/(\d+))?/);
  if (ma) return { name: 'ai', id: ma[1] ? Number(ma[1]) : null, query };
  for (const n of ['audit', 'users', 'settings']) if (h.startsWith(n)) return { name: n, id: null, query };
  return { name: 'list', id: null, query };
}
function pathLabel(path) {
  // "line_items[2].amount" -> "金额明细 #3 · 金额"
  const parts = path.split('.');
  const out = [];
  parts.forEach((p, i) => {
    const m = p.match(/^([a-z_]+)(?:\[(\d+)\])?$/i);
    if (!m) { out.push(p); return; }
    const key = m[1];
    let label = i === 0 ? t('f_' + key) : FIELD_LABELS[key] ? t(FIELD_LABELS[key]) : key;
    if (label === 'f_' + key) label = key;
    out.push(m[2] !== undefined ? label + ' #' + (Number(m[2]) + 1) : label);
  });
  return out.join(' · ');
}
const FIELD_LABELS = { contract_no: 'contractNo', title: 'title', title_zh: 'title', party_a: 'partyA', party_b: 'partyB',
  signing_date: 'signingDate', effective_date: 'effectiveDate', start_date: 'startDate', end_date: 'endDate', term_text: 'termText',
  governing_law: 'governingLaw', dispute_resolution: 'dispute', currency: 'currency', settlement_currency: 'settlement',
  total_amount: 'totalAmount', amount: 'amount', name: 'item', name_zh: 'itemZh', description: 'desc', category: 'category',
  quantity: 'qty', unit: 'unit', unit_price: 'unitPrice', page: 'page', trigger: 'trigger', trigger_zh: 'trigger',
  percentage: 'percent', due_rule: 'due', conditions: 'conditions', deadline_rule: 'deadlineRule', deadline_date: 'deadlineDate',
  deliverables: 'deliverables', period: 'wPeriod', start_rule: 'wStart', content: 'wContent', rule: 'pRule', cap: 'pCap',
  topic: 'topic', topic_zh: 'topic', section: 'section', label: 'label', label_zh: 'label', value: 'value', tax_note: 'taxNote',
  exchange_rate_rule: 'fxRule', total_amount_text: 'totalText', party_a_address: 'addrA', party_b_address: 'addrB' };
function showVal(v) { return v === null || v === undefined || v === '' ? '—' : (typeof v === 'object' ? JSON.stringify(v) : String(v)); }
function who(name) { return name || t('system'); }
const roleNames = reactive({});
function roleName(key) { return ['admin', 'editor', 'viewer'].includes(key) ? t('r_' + key) : (roleNames[key] || key); }

const mixin = { methods: { t, fmt, statusTag, can, who }, computed: { lang() { return store.lang; } } };

// ---------- small components ----------
const PageRef = {
  mixins: [mixin], props: ['p'],
  setup() { return { openPage: inject('openPage', () => {}) }; },
  template: `<button v-if="p" class="pg" type="button" @click.stop="openPage(p)" :title="t('page') + ' ' + p">p.{{ p }}</button>`,
};

const ChangeTable = {
  mixins: [mixin], props: { changes: Array },
  setup() { return { pathLabel, showVal }; },
  template: `
  <el-table :data="changes" size="small" border class="change-table">
    <el-table-column :label="t('field')" min-width="180"><template #default="{ row }">{{ pathLabel(row.path) }}</template></el-table-column>
    <el-table-column :label="t('oldValue')" min-width="160"><template #default="{ row }"><span class="old-val">{{ showVal(row.old) }}</span></template></el-table-column>
    <el-table-column :label="t('newValue')" min-width="160"><template #default="{ row }"><span class="new-val">{{ showVal(row.new) }}</span></template></el-table-column>
  </el-table>`,
};

const AuditDetail = {
  mixins: [mixin], components: { ChangeTable }, props: { row: Object },
  setup(props) {
    const open = ref(false);
    const summary = computed(() => {
      const r = props.row, d = r.detail;
      if (!d) return '';
      if (r.action === 'edit') return (d.changes || []).length + ' ' + t('changes');
      if (r.action === 'lifecycle_status') return t('l_' + (d.old || 'active')) + ' → ' + t('l_' + d.new);
      if (r.action === 'notes') return showVal(d.new);
      if (r.action === 'user_create') return d.username + ' (' + roleName(d.role) + (d.department ? ' · ' + d.department : '') + ')';
      if (r.action === 'register') return d.name + ' · ' + d.username + ' · ' + t('employeeNo') + ' ' + d.employee_no + (d.ip ? ' · IP ' + d.ip : '');
      if (r.action === 'user_approve') return d.username + ' → ' + roleName(d.role) + (d.department ? ' · ' + d.department : '');
      if (r.action === 'user_reject') return d.username + ' · ' + t('employeeNo') + ' ' + d.employee_no;
      if (r.action === 'user_update') return d.username + ': ' + Object.entries(d.changes || {}).map(([k, v]) =>
        k === 'role' ? t('role') + ' ' + roleName(v[0]) + '→' + roleName(v[1]) :
        k === 'department' ? t('department') + ' ' + (v[0] || '—') + '→' + (v[1] || '—') :
        k === 'position' ? t('position') + ' ' + (v[0] || '—') + '→' + (v[1] || '—') :
        k === 'employee_no' ? t('employeeNo') + ' ' + (v[0] || '—') + '→' + (v[1] || '—') :
        k === 'active' ? (v[1] ? t('enabled') : t('disabled')) : k === 'password' ? t('resetPw') : k).join(', ');
      if (r.action === 'extracted') return (d.model || '') + (d.items != null ? ' · ' + d.items + ' items' : '');
      if (r.action === 'extract_failed') return d.error;
      if (r.action === 'upload' || r.action === 'delete') return d.file === r.contract_label ? '' : d.file;
      if (r.action === 'export') return d.contracts + ' contracts';
      if (r.action === 'login') return d.ip ? 'IP ' + d.ip : '';
      if (r.action === 'login_failed') return (d.username || '') + (d.ip ? ' · IP ' + d.ip : '');
      if (r.action === 'settings') return Object.keys(d).join(', ');
      if (r.action === 'departments') return ((d.old || []).join('、') || t('companyWide')) + ' → ' + ((d.new || []).join('、') || t('companyWide'));
      if (r.action === 'dept_create' || r.action === 'dept_delete' || r.action === 'role_create' || r.action === 'role_delete') return d.name;
      if (r.action === 'dept_update') return (d.name || []).filter((x, i, a) => a.indexOf(x) === i).join(' → ') + (d.see_all && d.see_all[0] !== d.see_all[1] ? ' · ' + t('seeAll') + ': ' + (d.see_all[1] ? '✓' : '✗') : '');
      if (r.action === 'role_update') return d.name + (d.added && d.added.length ? ' +' + d.added.map(p => t('p_' + p)).join('、') : '') + (d.removed && d.removed.length ? ' −' + d.removed.map(p => t('p_' + p)).join('、') : '');
      return '';
    });
    return { open, summary };
  },
  template: `
  <div>
    <span class="muted">{{ summary }}</span>
    <template v-if="row.action === 'edit' && row.detail && (row.detail.changes || []).length">
      <el-button link type="primary" size="small" @click="open = !open">{{ t('viewChanges') }}</el-button>
      <div v-if="open" class="mt-s">
        <div v-if="row.detail.reopened" class="risk small">{{ t('reopenedNote') }}</div>
        <ChangeTable :changes="row.detail.changes" />
      </div>
    </template>
  </div>`,
};

const DataTable = {
  mixins: [mixin], components: { PageRef },
  props: { rows: Array, cols: Array, editing: Boolean, emptyText: String, highlightIndex: { type: Number, default: -1 } },
  emits: ['add', 'remove'],
  computed: {
    shownCols() {
      if (this.editing) return this.cols.filter(c => !c.viewOnly);
      return this.cols.filter(c => !c.editOnly && (!c.optional || (this.rows || []).some(r => r[c.key] !== null && r[c.key] !== undefined && r[c.key] !== '')));
    },
  },
  methods: { rowClass({ rowIndex }) { return rowIndex === this.highlightIndex ? 'hit-row' : ''; } },
  template: `
  <div>
    <el-table :data="rows" border size="small" :empty-text="emptyText || '—'" style="width:100%" :row-class-name="rowClass">
      <el-table-column v-for="c in shownCols" :key="c.key + (c.type||'')" :label="c.label" :width="c.width" :min-width="c.min"
        :align="c.type === 'num' ? 'right' : 'left'">
        <template #default="{ row }">
          <template v-if="editing && c.type !== 'page'">
            <el-input v-if="c.type === 'long'" v-model="row[c.key]" type="textarea" :autosize="{minRows:1,maxRows:5}" />
            <el-input v-else v-model="row[c.key]" />
          </template>
          <template v-else-if="editing && c.type === 'page'"><el-input v-model.number="row[c.key]" /></template>
          <PageRef v-else-if="c.type === 'page'" :p="row[c.key]" />
          <span v-else-if="c.type === 'num'" class="num">{{ fmt(row[c.key]) }}</span>
          <div v-else-if="c.type === 'item'"><div class="t-title">{{ row.name }}</div>
            <div class="small" v-if="row.name_zh">{{ row.name_zh }}</div>
            <div class="muted small" v-if="row.category && row.category !== row.name">{{ row.category }}</div></div>
          <span v-else class="cell-text">{{ row[c.key] }}</span>
        </template>
      </el-table-column>
      <el-table-column v-if="editing" width="56" align="center">
        <template #default="{ $index }">
          <el-button link type="danger" @click="$emit('remove', $index)"><el-icon><Delete /></el-icon></el-button>
        </template>
      </el-table-column>
    </el-table>
    <el-button v-if="editing" class="add-row" size="small" @click="$emit('add')"><el-icon><Plus /></el-icon>{{ t('addRow') }}</el-button>
  </div>`,
};

// ---------- login / setup ----------
const AuthView = {
  mixins: [mixin],
  setup() {
    // mode: login | register | done (registration submitted); first run uses the setup form
    const mode = ref('login');
    const f = reactive({ username: '', password: '', password2: '', display_name: '', employee_no: '' });
    const busy = ref(false);
    const rules = computed(() => ({
      user: /^[A-Za-z0-9]{3,32}$/.test(f.username),
      len: f.password.length >= 10,
      letter: /[A-Za-z]/.test(f.password),
      digit: /\d/.test(f.password),
      same: !!f.password && f.password === f.password2,
      emp: /^[A-Za-z0-9-]{1,32}$/.test(f.employee_no.trim()),
    }));
    const isForm = computed(() => store.needsSetup || mode.value === 'register');
    const formOk = computed(() => {
      const r = rules.value;
      return r.user && r.len && r.letter && r.digit && r.same && r.emp && !!f.display_name.trim();
    });
    function reset() { Object.assign(f, { username: '', password: '', password2: '', display_name: '', employee_no: '' }); }
    function switchTo(m) { mode.value = m; reset(); }
    async function submit() {
      if (isForm.value && !formOk.value) { ElMessage.error(t('fixForm')); return; }
      busy.value = true;
      try {
        if (store.needsSetup) {
          const r = await api('/api/auth/setup', { method: 'POST', json: { username: f.username, password: f.password, display_name: f.display_name, employee_no: f.employee_no.trim() } });
          store.user = r.user; store.needsSetup = false;
        } else if (mode.value === 'register') {
          await api('/api/auth/register', { method: 'POST', json: { username: f.username, password: f.password, password2: f.password2, display_name: f.display_name, employee_no: f.employee_no.trim() } });
          mode.value = 'done';
        } else {
          const r = await api('/api/auth/login', { method: 'POST', json: { username: f.username, password: f.password } });
          store.user = r.user;
        }
      } catch (e) { ElMessage.error(e.message); }
      busy.value = false;
    }
    function toggleLang() { store.lang = store.lang === 'zh' ? 'en' : 'zh'; try { localStorage.setItem('cms_lang', store.lang); } catch (e) {} }
    return { mode, f, busy, rules, isForm, formOk, submit, switchTo, store, toggleLang };
  },
  template: `
  <div class="auth-page">
    <div class="auth-card">
      <div class="brand auth-brand"><div class="brand-mark" aria-hidden="true"></div>
        <div><div class="brand-name dark">{{ t('appName') }}</div><div class="brand-sub">SIPG Bayport</div></div></div>

      <template v-if="mode === 'done' && !store.needsSetup">
        <el-result icon="success" :title="t('regDoneTitle')" :sub-title="t('regDoneText')">
          <template #extra><el-button type="primary" @click="switchTo('login')">{{ t('backToLogin') }}</el-button></template>
        </el-result>
      </template>

      <template v-else>
        <h1>{{ store.needsSetup ? t('setupTitle') : mode === 'register' ? t('registerTitle') : t('loginTitle') }}</h1>
        <p v-if="store.needsSetup" class="note">{{ t('setupText') }}</p>
        <p v-else-if="mode === 'register'" class="note">{{ t('registerText') }}</p>
        <el-form label-position="top" @submit.prevent="submit">
          <el-form-item :label="t('username')">
            <el-input v-model="f.username" autocomplete="username" autofocus maxlength="32" />
            <div v-if="isForm" class="rule" :class="{ ok: rules.user, bad: f.username && !rules.user }">{{ rules.user ? '✓' : '•' }} {{ t('ruleUsername') }}</div>
          </el-form-item>
          <template v-if="isForm">
            <el-form-item :label="t('displayName')"><el-input v-model="f.display_name" maxlength="40" /></el-form-item>
            <el-form-item :label="t('employeeNo')">
              <el-input v-model="f.employee_no" maxlength="32" :placeholder="t('employeeNoPh')" />
              <div class="rule" :class="{ ok: rules.emp, bad: f.employee_no && !rules.emp }">{{ rules.emp ? '✓' : '•' }} {{ t('ruleEmployeeNo') }}</div>
            </el-form-item>
          </template>
          <el-form-item :label="t('password')">
            <el-input v-model="f.password" type="password" show-password :autocomplete="isForm ? 'new-password' : 'current-password'" />
            <div v-if="isForm" class="rules">
              <span class="rule" :class="{ ok: rules.len }">{{ rules.len ? '✓' : '•' }} {{ t('ruleLen') }}</span>
              <span class="rule" :class="{ ok: rules.letter }">{{ rules.letter ? '✓' : '•' }} {{ t('ruleLetter') }}</span>
              <span class="rule" :class="{ ok: rules.digit }">{{ rules.digit ? '✓' : '•' }} {{ t('ruleDigit') }}</span>
            </div>
          </el-form-item>
          <el-form-item v-if="isForm" :label="t('passwordAgain')">
            <el-input v-model="f.password2" type="password" show-password autocomplete="new-password" />
            <div v-if="f.password2" class="rule" :class="{ ok: rules.same, bad: !rules.same }">{{ rules.same ? '✓ ' + t('pwSame') : '✗ ' + t('pwMismatch') }}</div>
          </el-form-item>
          <el-button type="primary" size="large" class="w100" :loading="busy" native-type="submit" :disabled="isForm && !formOk">
            {{ store.needsSetup ? t('setupBtn') : mode === 'register' ? t('submitRegister') : t('login') }}</el-button>
        </el-form>
        <div class="auth-foot">
          <template v-if="!store.needsSetup">
            <span v-if="mode === 'login'" class="muted">{{ t('noAccount') }} <a href="javascript:void 0" @click="switchTo('register')">{{ t('registerBtn') }}</a></span>
            <span v-else class="muted">{{ t('haveAccount') }} <a href="javascript:void 0" @click="switchTo('login')">{{ t('backToLogin') }}</a></span>
          </template>
          <button class="lang-link" type="button" @click="toggleLang">{{ t('lang') }}</button>
        </div>
      </template>
    </div>
  </div>`,
};

// ---------- search results grouped by contract ----------
const SearchResults = {
  mixins: [mixin], props: { rows: Array, q: String },
  setup(props) {
    function titleOf(r) { return (store.lang === 'zh' ? (r.title_zh || r.title) : (r.title || r.title_zh)) || r.file_name; }
    function hl(s) { return highlight(s, props.q); }
    function open(r, query) { go('detail', r.id, Object.assign({ q: props.q }, query || {})); }
    function empty(h) { return !h || (!h.items.length && !h.terms.length && !h.page_total); }
    return { titleOf, hl, open, empty };
  },
  template: `
  <div class="results">
    <div class="muted mb-s">{{ t('resultsFor', { n: rows.length }) }}</div>
    <div v-for="r in rows" :key="r.id" class="result">
      <div class="result-head" @click="open(r)">
        <div class="result-title">
          <span class="mono-ish" v-if="r.contract_no">{{ r.contract_no }}</span>
          <b v-html="hl(titleOf(r))"></b>
        </div>
        <div class="meta-row">
          <span v-html="hl(r.party_b || '')"></span>
          <el-tag v-if="r.contract_type" type="info" effect="plain" size="small">{{ t('t_' + r.contract_type) }}</el-tag>
          <el-tag :type="statusTag(r.status)" size="small">{{ t('s_' + r.status) }}</el-tag>
          <span class="num" v-if="r.total_amount != null">{{ fmt(r.total_amount, r.currency) }}</span>
        </div>
      </div>
      <div class="hit-group" v-if="r.hits.items.length">
        <div class="hit-label">{{ t('hitItems') }} ({{ r.hits.items.length }})</div>
        <div v-for="it in r.hits.items" :key="'i' + it.index" class="hit" @click="open(r, { tab: 'amounts', item: it.index, p: it.page || '' })">
          <div class="hit-main"><span v-html="hl(it.name)"></span><span class="muted small" v-if="it.name_zh"> · <span v-html="hl(it.name_zh)"></span></span>
            <div class="muted small" v-if="it.description" v-html="hl(it.description)"></div></div>
          <span class="num hit-amt">{{ fmt(it.amount, it.currency || r.currency) }}</span>
          <span class="pg" v-if="it.page">p.{{ it.page }}</span>
        </div>
      </div>
      <div class="hit-group" v-if="r.hits.terms.length">
        <div class="hit-label">{{ t('hitTerms') }} ({{ r.hits.terms.length }})</div>
        <div v-for="(x, i) in r.hits.terms.slice(0, 5)" :key="'t' + i" class="hit" @click="open(r, { tab: x.tab, p: x.page || '' })">
          <div class="hit-main"><b v-html="hl(x.kind.startsWith('summary') || x.kind.startsWith('scope') ? t('f_' + x.kind) : x.title)"></b>
            <div class="muted small" v-html="hl(x.text)"></div></div>
          <span class="pg" v-if="x.page">p.{{ x.page }}</span>
        </div>
      </div>
      <div class="hit-group" v-if="r.hits.page_total">
        <div class="hit-label">{{ t('hitPages') }} · {{ t('pagesMatched', { n: r.hits.page_total }) }}</div>
        <div v-for="p in r.hits.pages" :key="'p' + p.page" class="hit" @click="open(r, { tab: 'source', p: p.page })">
          <div class="hit-main small" v-html="hl(p.text)"></div>
          <span class="pg">p.{{ p.page }}</span>
        </div>
      </div>
      <div class="hit-group muted small" v-if="empty(r.hits)">{{ t('nameHit') }}</div>
    </div>
  </div>`,
};

// ---------- list view ----------
const ListView = {
  mixins: [mixin], components: { SearchResults },
  setup() {
    const rows = ref([]); const stats = ref({ by_status: {}, parties: [], years: [], count: 0 });
    const loading = ref(true); const showUpload = ref(false); const uploader = ref(null);
    const up = reactive({ total: 0, done: 0, created: 0 });
    const f = reactive({ q: store.route.query.q || '', type: '', party: '', year: '', status: '', lifecycle: '', dept: '' });
    const depts = ref([]);
    api('/api/departments').then(r => { depts.value = r; }).catch(() => {});
    let timer = null, debounce = null, seq = 0;
    async function load() {
      const my = ++seq;
      const p = new URLSearchParams(f).toString();
      try {
        const [r, s] = await Promise.all([api('/api/contracts?' + p), api('/api/stats')]);
        if (my !== seq) return;
        rows.value = r; stats.value = s;
      } catch (e) { ElMessage.error(e.message); }
      loading.value = false;
      clearTimeout(timer);
      if (rows.value.some(r => WORKING.includes(r.status))) timer = setTimeout(load, 3000);
    }
    watch(() => [f.type, f.party, f.year, f.status, f.lifecycle, f.dept], load);
    watch(() => f.q, () => { clearTimeout(debounce); debounce = setTimeout(load, 300); });
    onMounted(load);
    onBeforeUnmount(() => clearTimeout(timer));
    const filtered = computed(() => !!(f.q || f.type || f.party || f.year || f.status || f.lifecycle || f.dept));
    const searching = computed(() => !!f.q.trim());
    const working = computed(() => WORKING.reduce((a, s) => a + (stats.value.by_status[s] || 0), 0));

    // upload dialog: count files, close automatically when every file has finished
    function onChange(file) { if (file.status === 'ready') up.total++; }
    function finishOne() {
      up.done++;
      if (up.done >= up.total) {
        if (up.created) ElMessage.success(t('uploaded_ok', { n: up.created }));
        setTimeout(() => { showUpload.value = false; uploader.value && uploader.value.clearFiles(); Object.assign(up, { total: 0, done: 0, created: 0 }); }, 400);
        load();
      }
    }
    function onUploaded(res) {
      (res.duplicates || []).forEach(d => ElMessage.warning(t('duplicate', { f: d.file })));
      (res.rejected || []).forEach(x => ElMessage.error(t('rejected', { f: x })));
      up.created += (res.created || []).length;
      finishOne();
    }
    function onUploadError(err) { ElMessage.error(String(err && err.message || err)); finishOne(); }
    function onDialogClosed() { uploader.value && uploader.value.clearFiles(); Object.assign(up, { total: 0, done: 0, created: 0 }); }

    function exportXlsx() { location.href = '/api/export.xlsx?ids=' + rows.value.map(r => r.id).join(','); }
    function titleOf(r) { return (store.lang === 'zh' ? (r.title_zh || r.title) : (r.title || r.title_zh)) || r.file_name; }
    return { depts, rows, stats, f, loading, filtered, searching, working, showUpload, uploader, up, onChange, onUploaded, onUploadError,
      onDialogClosed, exportXlsx, titleOf, TYPES, STATUSES, LIFECYCLES, WORKING, go };
  },
  template: `
  <div>
    <div class="ph">
      <div><h1>{{ t('contracts') }}</h1><div class="sub">{{ t('contractsSub') }}</div></div>
      <div class="actions">
        <el-button v-if="can('contract.export')" @click="exportXlsx" :disabled="!rows.length"><el-icon><Download /></el-icon>{{ t('export') }}</el-button>
        <el-button v-if="can('contract.upload')" type="primary" @click="showUpload = true"><el-icon><Upload /></el-icon>{{ t('upload') }}</el-button>
      </div>
    </div>

    <div class="ledger" v-if="stats.count">
      <div><div class="k">{{ t('total') }}</div><div class="v">{{ stats.count }}</div></div>
      <div><div class="k">{{ t('toReview') }}</div><div class="v amber">{{ stats.by_status.review || 0 }}</div></div>
      <div><div class="k">{{ t('s_confirmed') }}</div><div class="v">{{ stats.by_status.confirmed || 0 }}</div></div>
      <div v-if="working"><div class="k">{{ t('processing') }}</div><div class="v">{{ working }}</div></div>
    </div>

    <div class="filters">
      <el-input v-model="f.q" :placeholder="t('search')" clearable class="grow" size="large"><template #prefix><el-icon><Search /></el-icon></template></el-input>
      <el-select v-model="f.type" :placeholder="t('type')" clearable style="width:140px">
        <el-option v-for="x in TYPES" :key="x" :value="x" :label="t('t_' + x)" /></el-select>
      <el-select v-model="f.party" :placeholder="t('party')" clearable filterable style="width:220px">
        <el-option v-for="x in stats.parties" :key="x" :value="x" :label="x" /></el-select>
      <el-select v-model="f.year" :placeholder="t('year')" clearable style="width:110px">
        <el-option v-for="x in stats.years" :key="x" :value="x" :label="x" /></el-select>
      <el-select v-model="f.status" :placeholder="t('status')" clearable style="width:130px">
        <el-option v-for="x in STATUSES" :key="x" :value="x" :label="t('s_' + x)" /></el-select>
      <el-select v-if="depts.length" v-model="f.dept" :placeholder="t('department')" clearable style="width:150px">
        <el-option value="none" :label="t('companyWide')" />
        <el-option v-for="d in depts" :key="d.id" :value="String(d.id)" :label="d.name" /></el-select>
      <el-select v-model="f.lifecycle" :placeholder="t('lifecycle')" clearable style="width:130px">
        <el-option v-for="x in LIFECYCLES" :key="x" :value="x" :label="t('l_' + x)" /></el-select>
    </div>

    <SearchResults v-if="searching && rows.length" :rows="rows" :q="f.q" />
    <div v-else class="panel" v-loading="loading">
      <el-table v-if="rows.length" :data="rows" @row-click="r => go('detail', r.id)" row-class-name="click" style="width:100%">
        <el-table-column :label="t('contractNo')" width="160">
          <template #default="{ row }"><span class="mono-ish">{{ row.contract_no || '—' }}</span></template></el-table-column>
        <el-table-column :label="t('title')" min-width="300">
          <template #default="{ row }">
            <div class="t-title">{{ titleOf(row) }}</div>
            <div class="muted small" v-if="WORKING.includes(row.status)">{{ row.progress }}</div>
          </template></el-table-column>
        <el-table-column :label="t('type')" width="110">
          <template #default="{ row }"><el-tag v-if="row.contract_type" type="info" effect="plain" size="small">{{ t('t_' + row.contract_type) }}</el-tag></template></el-table-column>
        <el-table-column :label="t('partyB')" prop="party_b" min-width="200" show-overflow-tooltip />
        <el-table-column v-if="depts.length" :label="t('department')" min-width="120">
          <template #default="{ row }">
            <template v-if="(row.departments || []).length"><el-tag v-for="d in row.departments" :key="d.id" size="small" effect="plain" class="mr-xs">{{ d.name }}</el-tag></template>
            <span v-else class="muted small">{{ t('companyWide') }}</span>
          </template></el-table-column>
        <el-table-column :label="t('totalAmount')" width="170" align="right">
          <template #default="{ row }"><span class="num">{{ fmt(row.total_amount, row.currency) }}</span></template></el-table-column>
        <el-table-column :label="t('signingDate')" width="110">
          <template #default="{ row }">{{ row.signing_date || row.effective_date || '—' }}</template></el-table-column>
        <el-table-column :label="t('status')" width="120">
          <template #default="{ row }"><el-tag :type="statusTag(row.status)" size="small">{{ t('s_' + row.status) }}</el-tag></template></el-table-column>
        <el-table-column :label="t('reviewedBy')" width="100">
          <template #default="{ row }"><span class="muted">{{ row.confirmed_by || '' }}</span></template></el-table-column>
      </el-table>
      <el-empty v-else-if="!loading && filtered" :description="t('noMatch')" />
      <div v-else-if="!loading" class="empty-state">
        <h2>{{ t('emptyTitle') }}</h2><p>{{ t('emptyText') }}</p>
        <el-button v-if="can('contract.upload')" type="primary" size="large" @click="showUpload = true"><el-icon><Upload /></el-icon>{{ t('upload') }}</el-button>
      </div>
    </div>

    <el-dialog v-model="showUpload" :title="t('uploadTitle')" width="560px" @closed="onDialogClosed">
      <el-upload ref="uploader" drag multiple action="/api/contracts" name="files" accept=".pdf,.docx,.txt"
        :on-change="onChange" :on-success="onUploaded" :on-error="onUploadError">
        <el-icon class="el-icon--upload"><UploadFilled /></el-icon>
        <div class="el-upload__text">{{ t('uploadHint') }}</div>
        <template #tip><div class="el-upload__tip">{{ up.total && up.done < up.total ? t('uploadingN', { n: up.total - up.done }) : t('uploadTip') }}</div></template>
      </el-upload>
    </el-dialog>
  </div>`,
};

// ---------- detail view ----------
const DetailView = {
  mixins: [mixin], components: { PageRef, DataTable, AuditDetail },
  props: ['id'],
  setup(props) {
    const query = store.route.query || {};
    const c = ref(null); const draft = ref(null); const editing = ref(false);
    const tab = ref(['overview', 'amounts', 'payments', 'terms', 'source', 'history'].includes(query.tab) ? query.tab : 'overview');
    const showPdf = ref((() => { try { return localStorage.getItem('cms_pdf') !== '0'; } catch (e) { return true; } })());
    const pdfPage = ref(Number(query.p) || 1); const pdfKey = ref(0);
    const hitItem = ref(query.item !== undefined && query.item !== '' ? Number(query.item) : -1);
    const pages = ref([]); const srcMode = ref('masked'); const find = ref(query.tab === 'source' ? (query.q || '') : ''); const saving = ref(false);
    const historyRows = ref([]);
    const deptDlg = ref(false); const deptSel = ref([]); const allDepts = ref([]);
    async function openDept() {
      allDepts.value = await api('/api/departments');
      deptSel.value = (c.value.departments || []).map(d => d.id); deptDlg.value = true;
    }
    async function askAI() {
      try { const cv = await api('/api/ai/conversations', { method: 'POST', json: { contract_ids: [Number(props.id)] } }); go('ai', cv.id); }
      catch (e) { ElMessage.error(e.message); }
    }
    async function saveDept() {
      try {
        c.value.departments = await api('/api/contracts/' + props.id + '/departments', { method: 'PUT', json: { department_ids: deptSel.value } });
        deptDlg.value = false; ElMessage.success(t('saved'));
      } catch (e) { ElMessage.error(e.message); }
    }
    let timer = null;
    const pdfRef = ref(null);
    watch(showPdf, v => { try { localStorage.setItem('cms_pdf', v ? '1' : '0'); } catch (e) {} });
    function openPage(p) {
      showPdf.value = true; pdfPage.value = p; pdfKey.value++;
      if (window.innerWidth < 1100) nextTick(() => pdfRef.value && pdfRef.value.scrollIntoView({ behavior: 'smooth' }));
    }
    provide('openPage', openPage);
    async function load() {
      try { c.value = await api('/api/contracts/' + props.id); } catch (e) { ElMessage.error(e.message); return; }
      clearTimeout(timer);
      if (WORKING.includes(c.value.status)) timer = setTimeout(load, 3000);
    }
    async function loadPages() { pages.value = await api('/api/contracts/' + props.id + '/pages'); }
    async function loadHistory() { historyRows.value = await api('/api/contracts/' + props.id + '/history'); }
    watch(tab, v => { if (v === 'source' && !pages.value.length) loadPages(); if (v === 'history') loadHistory(); }, { immediate: true });
    onMounted(async () => {
      await load();
      if (hitItem.value >= 0) setTimeout(() => { const el = document.querySelector('.hit-row'); el && el.scrollIntoView({ block: 'center', behavior: 'smooth' }); }, 300);
    });
    onBeforeUnmount(() => clearTimeout(timer));

    const d = computed(() => (editing.value ? draft.value : c.value && c.value.data) || null);
    const isPdf = computed(() => c.value && /\.pdf$/i.test(c.value.file_name));
    const cur = computed(() => d.value && d.value.amount && d.value.amount.currency || '');
    const title = computed(() => {
      if (!c.value) return '';
      const b = (d.value && d.value.basic) || {};
      return (store.lang === 'zh' ? (b.title_zh || b.title) : (b.title || b.title_zh)) || c.value.file_name;
    });
    const subTitle = computed(() => {
      const b = (d.value && d.value.basic) || {};
      return store.lang === 'zh' ? (b.title_zh ? b.title : '') : (b.title ? b.title_zh : '');
    });
    const recon = computed(() => {
      if (!d.value) return null;
      const items = d.value.line_items || [];
      const sum = items.reduce((a, it) => a + (num(it.amount) || 0), 0);
      const total = num(d.value.amount && d.value.amount.total_amount);
      if (!items.length || total === null) return null;
      const diff = Math.round((total - sum) * 100) / 100;
      return { sum, total, diff, ok: Math.abs(diff) < 0.01 };
    });

    function ensure(x) {
      x.basic = x.basic || {}; x.amount = x.amount || {}; x.warranty = x.warranty || {};
      ['line_items', 'payment_terms', 'milestones', 'penalties', 'key_clauses', 'type_specific', 'attachments', 'risk_notes_zh'].forEach(k => { x[k] = x[k] || []; });
      return x;
    }
    function startEdit() { draft.value = ensure(JSON.parse(JSON.stringify(c.value.data || {}))); editing.value = true; }
    function cancelEdit() { editing.value = false; draft.value = null; }
    async function save() {
      saving.value = true;
      try {
        c.value = await api('/api/contracts/' + props.id, { method: 'PUT', json: { data: draft.value } });
        editing.value = false; draft.value = null; ElMessage.success(t('saved'));
        if (tab.value === 'history') loadHistory();
      } catch (e) { ElMessage.error(e.message); }
      saving.value = false;
    }
    async function setMeta(k, v) {
      try { c.value = await api('/api/contracts/' + props.id, { method: 'PUT', json: { [k]: v } }); ElMessage.success(t('saved')); }
      catch (e) { ElMessage.error(e.message); }
    }
    async function confirmReview() {
      if (editing.value) await save();
      try { c.value = await api('/api/contracts/' + props.id + '/confirm', { method: 'POST' }); ElMessage.success(t('confirmed')); }
      catch (e) { ElMessage.error(e.message); }
    }
    async function unconfirm() { c.value = await api('/api/contracts/' + props.id + '/unconfirm', { method: 'POST' }); }
    async function onMore(cmd) {
      if (cmd === 'download') { location.href = '/api/contracts/' + props.id + '/file?download=1'; return; }
      if (cmd === 'delete') {
        try { await ElMessageBox.confirm(t('delConfirm'), t('del'), { type: 'warning', confirmButtonText: t('del'), cancelButtonText: t('no') }); } catch (e) { return; }
        await api('/api/contracts/' + props.id, { method: 'DELETE' }); ElMessage.success(t('deleted')); go('list'); return;
      }
      if (c.value.data) {
        try { await ElMessageBox.confirm(t('reextractConfirm'), t(cmd === 'read' ? 'reread' : 'reextract'), { type: 'warning', confirmButtonText: t('yes'), cancelButtonText: t('no') }); } catch (e) { return; }
      }
      await api('/api/contracts/' + props.id + '/reprocess?stage=' + cmd, { method: 'POST' });
      ElMessage.success(t('queued')); editing.value = false; pages.value = []; load();
    }
    function addRow(key, tmpl) { draft.value[key].push(Object.assign({}, tmpl)); }
    function removeRow(key, i) { draft.value[key].splice(i, 1); }

    const cols = computed(() => ({
      items: [
        { key: 'name', label: t('item'), min: 190, type: 'item', viewOnly: true },
        { key: 'category', label: t('category'), min: 140, type: 'long', editOnly: true },
        { key: 'name', label: t('item'), min: 170, type: 'long', editOnly: true },
        { key: 'name_zh', label: t('itemZh'), min: 120, editOnly: true },
        { key: 'description', label: t('desc'), min: 240, type: 'long' },
        { key: 'quantity', label: t('qty'), width: 80, type: 'num', optional: true },
        { key: 'unit', label: t('unit'), width: 70, optional: true },
        { key: 'unit_price', label: t('unitPrice'), width: 110, type: 'num', optional: true },
        { key: 'amount', label: t('amount'), width: 120, type: 'num' },
        { key: 'page', label: t('page'), width: 66, type: 'page' },
      ],
      pay: [
        { key: 'seq', label: t('seq'), width: 60 },
        { key: store.lang === 'zh' ? 'trigger_zh' : 'trigger', label: t('trigger'), min: 180, type: 'long' },
        { key: 'percentage', label: t('percent'), width: 80, type: 'num' },
        { key: 'amount', label: t('amount'), width: 120, type: 'num' },
        { key: 'due_rule', label: t('due'), min: 160, type: 'long' },
        { key: 'conditions', label: t('conditions'), min: 120, type: 'long', optional: true },
        { key: 'page', label: t('page'), width: 66, type: 'page' },
      ],
      ms: [
        { key: store.lang === 'zh' ? 'name_zh' : 'name', label: t('mName'), min: 160, type: 'long' },
        { key: 'deadline_rule', label: t('deadlineRule'), min: 220, type: 'long' },
        { key: 'deadline_date', label: t('deadlineDate'), width: 110, optional: true },
        { key: 'deliverables', label: t('deliverables'), min: 140, type: 'long' },
        { key: 'page', label: t('page'), width: 66, type: 'page' },
      ],
      pen: [
        { key: store.lang === 'zh' ? 'type_zh' : 'type', label: t('pType'), min: 120 },
        { key: 'rule', label: t('pRule'), min: 240, type: 'long' },
        { key: 'cap', label: t('pCap'), min: 120, type: 'long' },
        { key: 'page', label: t('page'), width: 66, type: 'page' },
      ],
      clauses: [
        { key: store.lang === 'zh' ? 'topic_zh' : 'topic', label: t('topic'), width: 130 },
        { key: 'section', label: t('section'), width: 70, optional: true },
        { key: store.lang === 'zh' ? 'summary_zh' : 'summary_en', label: t('clauseSummary'), min: 280, type: 'long' },
        { key: 'page', label: t('page'), width: 66, type: 'page' },
      ],
      ts: [
        { key: store.lang === 'zh' ? 'label_zh' : 'label', label: t('label'), width: 170 },
        { key: 'value', label: t('value'), min: 260, type: 'long' },
        { key: 'page', label: t('page'), width: 66, type: 'page' },
      ],
    }));
    const basicFields = computed(() => [
      ['contract_no', 'contractNo'], ['title', 'title'], ['title_zh', 'title'], ['party_a', 'partyA'], ['party_b', 'partyB'],
      ['signing_date', 'signingDate'], ['effective_date', 'effectiveDate'], ['effective_date_rule', 'effRule'],
      ['start_date', 'startDate'], ['end_date', 'endDate'], ['term_text', 'termText'],
      ['governing_law', 'governingLaw'], ['dispute_resolution', 'dispute'], ['party_a_address', 'addrA'], ['party_b_address', 'addrB'],
    ]);
    const amountFields = [['currency', 'currency'], ['settlement_currency', 'settlement'], ['total_amount', 'totalAmount'],
      ['total_amount_text', 'totalText'], ['tax_note', 'taxNote'], ['exchange_rate_rule', 'fxRule']];

    function hl(text) {
      let s = esc(text);
      s = s.replace(/\[REDACTED\]/g, '<span class="redacted">[REDACTED]</span>');
      const q = find.value.trim();
      if (q) {
        const re = new RegExp(q.replace(/[.*+?^${}()|[\]\\]/g, '\\$&'), 'gi');
        s = s.replace(re, m => '<mark>' + m + '</mark>');
      }
      return s;
    }
    const shownPages = computed(() => {
      const q = find.value.trim().toLowerCase();
      return pages.value.filter(p => !q || (srcMode.value === 'masked' ? p.redacted_text : p.text).toLowerCase().includes(q));
    });

    return { c, d, draft, editing, tab, showPdf, pdfPage, pdfKey, pdfRef, pages, srcMode, find, saving, isPdf, cur, title, subTitle, recon,
      startEdit, cancelEdit, save, setMeta, confirmReview, unconfirm, onMore, addRow, removeRow, cols, basicFields, amountFields,
      hl, shownPages, openPage, go, TYPES, LIFECYCLES, WORKING, store, hitItem, historyRows, deptDlg, deptSel, allDepts, openDept, saveDept, askAI };
  },
  template: `
  <div v-if="c">
    <div class="ph detail-head">
      <div class="head-left">
        <el-button link @click="go('list', null, store.route.query.q ? { q: store.route.query.q } : null)"><el-icon><ArrowLeft /></el-icon>{{ t('back') }}</el-button>
        <h1>{{ title }}</h1>
        <div class="sub" v-if="subTitle">{{ subTitle }}</div>
        <div class="meta-row">
          <span v-if="d && d.basic && d.basic.contract_no" class="mono-ish">{{ d.basic.contract_no }}</span>
          <el-tag v-if="c.contract_type" type="info" effect="plain" size="small">{{ t('t_' + c.contract_type) }}</el-tag>
          <el-tag :type="statusTag(c.status)" size="small">{{ t('s_' + c.status) }}</el-tag>
          <span v-if="c.page_count">{{ c.page_count }} {{ t('pages') }}<template v-if="c.ocr_pages"> · {{ c.ocr_pages }} {{ t('ocrPages') }}</template></span>
        </div>
        <div class="meta-row people">
          <span class="dept-line"><el-icon><OfficeBuilding /></el-icon>
            <template v-if="(c.departments || []).length"><el-tag v-for="d in c.departments" :key="d.id" size="small" effect="plain">{{ d.name }}</el-tag></template>
            <el-tag v-else size="small" type="success" effect="plain">{{ t('companyWide') }}</el-tag>
            <el-button v-if="can('contract.assign_dept')" link type="primary" size="small" @click="openDept">{{ t('changeDept') }}</el-button>
          </span>
          <span v-if="c.uploaded_by">{{ t('uploadedBy') }}：{{ c.uploaded_by }} <span class="muted">{{ (c.uploaded_at || '').slice(0, 16) }}</span></span>
          <span v-if="c.updated_by">{{ t('updatedBy') }}：{{ c.updated_by }}</span>
          <span v-if="c.confirmed_by" class="ok">{{ t('reviewedBy') }}：{{ c.confirmed_by }} <span class="muted">{{ (c.confirmed_at || '').slice(0, 16) }}</span></span>
        </div>
      </div>
      <div class="actions">
        <el-button v-if="can('ai.use') && c.data" @click="askAI"><el-icon><ChatDotRound /></el-icon>{{ t('askAI') }}</el-button>
        <el-button @click="showPdf = !showPdf" :type="showPdf ? '' : 'primary'" plain>
          <el-icon><Document /></el-icon>{{ showPdf ? t('hidePdf') : t('showPdf') }}</el-button>
        <el-select :model-value="c.lifecycle_status || 'active'" @change="v => setMeta('lifecycle_status', v)" style="width:120px" :disabled="!can('contract.edit')">
          <el-option v-for="x in LIFECYCLES" :key="x" :value="x" :label="t('l_' + x)" />
        </el-select>
        <template v-if="c.data">
          <template v-if="can('contract.edit')">
            <template v-if="editing">
              <el-button @click="cancelEdit">{{ t('cancel') }}</el-button>
              <el-button type="primary" :loading="saving" @click="save">{{ t('save') }}</el-button>
            </template>
            <el-button v-else @click="startEdit" :disabled="WORKING.includes(c.status)"><el-icon><Edit /></el-icon>{{ t('edit') }}</el-button>
          </template>
          <el-button v-if="c.status === 'review' && can('contract.review')" type="success" @click="confirmReview"><el-icon><Check /></el-icon>{{ t('confirm') }}</el-button>
          <el-button v-if="c.status === 'confirmed' && can('contract.review')" @click="unconfirm">{{ t('unconfirm') }}</el-button>
        </template>
        <el-dropdown trigger="click" @command="onMore">
          <el-button><el-icon><MoreFilled /></el-icon></el-button>
          <template #dropdown><el-dropdown-menu>
            <el-dropdown-item v-if="can('contract.reprocess')" command="extract" :disabled="WORKING.includes(c.status)">{{ t('reextract') }}</el-dropdown-item>
            <el-dropdown-item v-if="can('contract.reprocess')" command="read" :disabled="WORKING.includes(c.status)">{{ t('reread') }}</el-dropdown-item>
            <el-dropdown-item v-if="can('contract.download')" command="download">{{ t('download') }}</el-dropdown-item>
            <el-dropdown-item v-if="can('contract.delete')" command="delete" divided :disabled="WORKING.includes(c.status)"><span class="danger">{{ t('del') }}</span></el-dropdown-item>
          </el-dropdown-menu></template>
        </el-dropdown>
      </div>
    </div>

    <el-alert v-if="WORKING.includes(c.status)" type="info" :closable="false" show-icon class="mb"
      :title="t('processingNow') + '：' + t('s_' + c.status) + (c.progress ? ' — ' + c.progress : '')" />
    <el-alert v-if="c.status === 'failed'" type="error" :closable="false" show-icon class="mb" :title="t('failedTitle')" :description="c.error" />
    <el-alert v-if="c.status === 'review' && !editing && (can('contract.review') || can('contract.edit'))" type="warning" :closable="false" show-icon class="mb" :title="t('reviewTip')" />

    <div class="detail" :class="{ nopdf: !showPdf }">
      <div class="detail-main">
        <el-tabs v-model="tab">
          <el-tab-pane :label="t('tabOverview')" name="overview">
            <template v-if="d">
              <div class="card">
                <h2>{{ t('summary') }}</h2>
                <template v-if="editing">
                  <el-input v-model="draft.summary_zh" type="textarea" :autosize="{minRows:3}" class="mb" />
                  <el-input v-model="draft.summary_en" type="textarea" :autosize="{minRows:3}" />
                </template>
                <p v-else class="prose">{{ store.lang === 'zh' ? (d.summary_zh || d.summary_en) : (d.summary_en || d.summary_zh) }}</p>
                <h2 class="mt">{{ t('scope') }}</h2>
                <template v-if="editing">
                  <el-input v-model="draft.scope_zh" type="textarea" :autosize="{minRows:2}" class="mb" />
                  <el-input v-model="draft.scope_en" type="textarea" :autosize="{minRows:2}" />
                </template>
                <p v-else class="prose">{{ store.lang === 'zh' ? (d.scope_zh || d.scope_en) : (d.scope_en || d.scope_zh) }}</p>
              </div>
              <div class="card" v-if="(d.risk_notes_zh || []).length && !editing">
                <h2>{{ t('risks') }}</h2>
                <div class="risk" v-for="(r, i) in d.risk_notes_zh" :key="i">{{ r }}</div>
              </div>
              <div class="card">
                <h2>{{ t('basicInfo') }}</h2>
                <div class="grid">
                  <div class="f" v-if="editing">
                    <div class="k">{{ t('contractType') }}</div>
                    <el-select v-model="draft.contract_type" style="width:100%"><el-option v-for="x in TYPES" :key="x" :value="x" :label="t('t_' + x)" /></el-select>
                  </div>
                  <div class="f" v-for="[k, l] in basicFields" :key="k" v-show="editing || (d.basic && d.basic[k])">
                    <div class="k">{{ t(l) }}<template v-if="k === 'title_zh'"> (中文)</template></div>
                    <el-input v-if="editing" v-model="draft.basic[k]" />
                    <div v-else class="v">{{ d.basic && d.basic[k] }}</div>
                  </div>
                </div>
              </div>
              <div class="card" v-if="editing || (d.type_specific || []).length">
                <h2>{{ t('typeSpecific') }}</h2>
                <DataTable :rows="d.type_specific" :cols="cols.ts" :editing="editing"
                  @add="addRow('type_specific', {})" @remove="i => removeRow('type_specific', i)" />
              </div>
              <div class="card">
                <h2>{{ t('notes') }}</h2>
                <el-input :model-value="c.notes" @change="v => setMeta('notes', v)" @update:model-value="v => c.notes = v"
                  type="textarea" :autosize="{minRows:2}" :placeholder="t('notesPh')" :disabled="!can('contract.edit')" />
              </div>
            </template>
          </el-tab-pane>

          <el-tab-pane :label="t('tabAmounts')" name="amounts">
            <template v-if="d">
              <div v-if="recon" class="recon" :class="recon.ok ? 'match' : 'mismatch'">
                <div><div class="k">{{ t('itemsSum') }} ({{ (d.line_items || []).length }})</div><div class="v">{{ fmt(recon.sum, cur) }}</div></div>
                <div><div class="k">{{ t('contractTotal') }}</div><div class="v">{{ fmt(recon.total, cur) }}</div></div>
                <div class="diff"><div class="k">{{ recon.ok ? t('matched') : t('unmatched') }}</div><div class="v">{{ recon.ok ? '✓' : fmt(recon.diff, cur) }}</div></div>
              </div>
              <div class="card">
                <h2>{{ t('totalAmount') }} <PageRef :p="d.amount && d.amount.page" /></h2>
                <div class="grid">
                  <div class="f" v-for="[k, l] in amountFields" :key="k" v-show="editing || (d.amount && d.amount[k] != null && d.amount[k] !== '')">
                    <div class="k">{{ t(l) }}</div>
                    <el-input v-if="editing" v-model="draft.amount[k]" />
                    <div v-else class="v" :class="{ 'big-num': k === 'total_amount' }">{{ k === 'total_amount' ? fmt(d.amount[k]) : d.amount[k] }}</div>
                  </div>
                </div>
              </div>
              <div class="card">
                <h2>{{ t('lineItems') }}</h2>
                <DataTable :rows="d.line_items" :cols="cols.items" :editing="editing" :highlight-index="hitItem"
                  @add="addRow('line_items', { currency: cur })" @remove="i => removeRow('line_items', i)" />
              </div>
            </template>
          </el-tab-pane>

          <el-tab-pane :label="t('tabPayments')" name="payments">
            <template v-if="d">
              <div class="card"><h2>{{ t('payments') }} <span class="muted" v-if="cur">{{ cur }}</span></h2>
                <DataTable :rows="d.payment_terms" :cols="cols.pay" :editing="editing"
                  @add="addRow('payment_terms', { seq: (draft.payment_terms.length + 1) })" @remove="i => removeRow('payment_terms', i)" /></div>
              <div class="card"><h2>{{ t('milestones') }}</h2>
                <DataTable :rows="d.milestones" :cols="cols.ms" :editing="editing"
                  @add="addRow('milestones', {})" @remove="i => removeRow('milestones', i)" /></div>
            </template>
          </el-tab-pane>

          <el-tab-pane :label="t('tabTerms')" name="terms">
            <template v-if="d">
              <div class="card"><h2>{{ t('warranty') }} <PageRef :p="d.warranty && d.warranty.page" /></h2>
                <div class="grid">
                  <div class="f" v-for="[k, l] in [['period','wPeriod'],['start_rule','wStart'],['content','wContent']]" :key="k">
                    <div class="k">{{ t(l) }}</div>
                    <el-input v-if="editing" v-model="draft.warranty[k]" />
                    <div v-else class="v">{{ (d.warranty && d.warranty[k]) || '—' }}</div>
                  </div>
                </div></div>
              <div class="card"><h2>{{ t('penalties') }}</h2>
                <DataTable :rows="d.penalties" :cols="cols.pen" :editing="editing"
                  @add="addRow('penalties', {})" @remove="i => removeRow('penalties', i)" /></div>
              <div class="card"><h2>{{ t('keyClauses') }}</h2>
                <DataTable :rows="d.key_clauses" :cols="cols.clauses" :editing="editing"
                  @add="addRow('key_clauses', {})" @remove="i => removeRow('key_clauses', i)" /></div>
              <div class="card" v-if="(d.attachments || []).length"><h2>{{ t('attachments') }}</h2>
                <div v-for="(a, i) in d.attachments" :key="i" class="att"><b>{{ a.name }}</b> <PageRef :p="a.page" /><div class="muted">{{ a.summary_zh }}</div></div></div>
            </template>
          </el-tab-pane>

          <el-tab-pane :label="t('tabSource')" name="source">
            <el-alert type="success" :closable="false" show-icon class="mb" :title="t('maskedNote', { n: c.redaction_count || 0 })" />
            <div class="filters">
              <el-radio-group v-model="srcMode">
                <el-radio-button value="masked">{{ t('masked') }}</el-radio-button>
                <el-radio-button value="original">{{ t('original') }}</el-radio-button>
              </el-radio-group>
              <el-input v-model="find" :placeholder="t('findInText')" clearable style="max-width:280px"><template #prefix><el-icon><Search /></el-icon></template></el-input>
            </div>
            <div v-for="p in shownPages" :key="p.page_no">
              <div class="page-head"><PageRef :p="p.page_no" /><el-tag v-if="p.is_ocr" size="small" type="info">{{ t('ocr') }}</el-tag></div>
              <div class="page-text" v-html="hl(srcMode === 'masked' ? p.redacted_text : p.text)"></div>
            </div>
          </el-tab-pane>

          <el-tab-pane :label="t('tabHistory')" name="history">
            <el-empty v-if="!historyRows.length" :description="t('noHistory')" />
            <el-timeline v-else class="history">
              <el-timeline-item v-for="h in historyRows" :key="h.id" :timestamp="h.ts" placement="top"
                :type="h.action === 'confirm' ? 'success' : h.action === 'edit' ? 'warning' : h.action === 'extract_failed' ? 'danger' : 'primary'">
                <div class="hist-line"><b>{{ who(h.username) }}</b> {{ t('a_' + h.action) }}</div>
                <AuditDetail :row="h" />
              </el-timeline-item>
            </el-timeline>
          </el-tab-pane>
        </el-tabs>
      </div>

      <div class="pdf" v-show="showPdf" ref="pdfRef">
        <div class="pdf-bar"><span>{{ t('pdfTitle') }} · p.{{ pdfPage }}</span>
          <el-button link size="small" @click="showPdf = false"><el-icon><Close /></el-icon></el-button></div>
        <iframe v-if="isPdf" :key="pdfKey" :src="'/api/contracts/' + c.id + '/file#page=' + pdfPage + '&view=FitH'" title="PDF"></iframe>
        <el-empty v-else :description="t('noPreview')" />
      </div>
    </div>
    <el-dialog v-model="deptDlg" :title="t('changeDept')" width="460px">
      <el-select v-model="deptSel" multiple style="width:100%" :placeholder="t('companyWide')">
        <el-option v-for="d in allDepts" :key="d.id" :value="d.id" :label="d.name" />
      </el-select>
      <p class="note mt-s">{{ t('deptHelp') }}</p>
      <template #footer><el-button @click="deptDlg = false">{{ t('cancel') }}</el-button><el-button type="primary" @click="saveDept">{{ t('save') }}</el-button></template>
    </el-dialog>
  </div>`,
};

// ---------- audit view ----------
const AuditView = {
  mixins: [mixin], components: { AuditDetail },
  setup() {
    const rows = ref([]); const total = ref(0); const loading = ref(true); const page = ref(1); const size = 50;
    const f = reactive({ user_q: '', action: '', range: null });
    async function load() {
      loading.value = true;
      const p = { user_q: f.user_q, action: f.action, limit: size, offset: (page.value - 1) * size };
      if (f.range && f.range.length === 2) { p.date_from = f.range[0]; p.date_to = f.range[1]; }
      try { const r = await api('/api/audit?' + new URLSearchParams(p).toString()); rows.value = r.rows; total.value = r.total; }
      catch (e) { ElMessage.error(e.message); }
      loading.value = false;
    }
    let debounce = null;
    watch(() => [f.action, f.range], () => { page.value = 1; load(); });
    watch(() => f.user_q, () => { clearTimeout(debounce); debounce = setTimeout(() => { page.value = 1; load(); }, 300); });
    watch(page, load);
    onMounted(load);
    return { rows, total, loading, page, size, f, ACTIONS, go };
  },
  template: `
  <div>
    <div class="ph"><div><h1>{{ t('auditTitle') }}</h1><div class="sub">{{ t('auditSub') }}</div></div></div>
    <div class="filters">
      <el-input v-model="f.user_q" :placeholder="t('user')" clearable style="width:180px"><template #prefix><el-icon><User /></el-icon></template></el-input>
      <el-select v-model="f.action" :placeholder="t('allActions')" clearable style="width:180px">
        <el-option v-for="a in ACTIONS" :key="a" :value="a" :label="t('a_' + a)" /></el-select>
      <el-date-picker v-model="f.range" type="daterange" value-format="YYYY-MM-DD" :start-placeholder="t('dateRange')" :end-placeholder="t('dateRange')" style="width:280px" />
    </div>
    <div class="panel" v-loading="loading">
      <el-table :data="rows" style="width:100%" :empty-text="t('noHistory')">
        <el-table-column :label="t('time')" width="190"><template #default="{ row }"><span class="num">{{ row.ts }}</span></template></el-table-column>
        <el-table-column :label="t('user')" width="120"><template #default="{ row }">{{ who(row.username) }}</template></el-table-column>
        <el-table-column :label="t('action')" width="150"><template #default="{ row }">
          <el-tag size="small" :type="row.action === 'confirm' ? 'success' : row.action === 'edit' ? 'warning' : ['delete','extract_failed','login_failed'].includes(row.action) ? 'danger' : 'info'" effect="plain">{{ t('a_' + row.action) }}</el-tag></template></el-table-column>
        <el-table-column :label="t('target')" min-width="200"><template #default="{ row }">
          <a v-if="row.contract_id && row.action !== 'delete'" href="javascript:void 0" @click="go('detail', row.contract_id, { tab: 'history' })">{{ row.contract_label || ('#' + row.contract_id) }}</a>
          <span v-else class="muted">{{ row.contract_label }}</span></template></el-table-column>
        <el-table-column :label="t('details')" min-width="320"><template #default="{ row }"><AuditDetail :row="row" /></template></el-table-column>
      </el-table>
    </div>
    <el-pagination v-if="total > size" class="pager" layout="prev, pager, next, total" :total="total" :page-size="size" v-model:current-page="page" />
  </div>`,
};

// ---------- users / roles / departments (admin) ----------
const UsersView = {
  mixins: [mixin],
  setup() {
    const tab = ref(['users', 'pending', 'roles', 'depts'].includes(store.route.query.tab) ? store.route.query.tab : 'users');
    const users = ref([]); const roles = ref([]); const perms = ref([]); const depts = ref([]); const loading = ref(true);
    const pending = ref([]);
    async function loadAll() {
      loading.value = true;
      try {
        const [u, r, d, p] = await Promise.all([api('/api/users'), api('/api/roles'), api('/api/departments'), api('/api/users/pending')]);
        users.value = u; roles.value = r.roles; perms.value = r.permissions; depts.value = d; pending.value = p;
        store.pendingCount = p.length;
        r.roles.forEach(x => { roleNames[x.key] = x.name; });
      } catch (e) { ElMessage.error(e.message); }
      loading.value = false;
    }
    onMounted(loadAll);
    const positions = computed(() => [...new Set(users.value.map(u => u.position).filter(Boolean))]);
    function suggestPositions(q, cb) { cb(positions.value.filter(p => !q || p.includes(q)).map(value => ({ value }))); }

    // ---- users ----
    const dlg = ref(false); const busy = ref(false);
    const blank = { id: null, username: '', display_name: '', employee_no: '', role: 'viewer', department_id: null, position: '', active: true,
      password: '', ai_daily_questions: null, ai_daily_tokens: null };
    const form = reactive(Object.assign({}, blank));
    function openNew() { Object.assign(form, blank); dlg.value = true; }
    function openEdit(u) {
      Object.assign(form, blank, { id: u.id, username: u.username, display_name: u.display_name, employee_no: u.employee_no || '', role: u.role,
        department_id: u.department_id, position: u.position || '', active: !!u.active,
        ai_daily_questions: u.ai_daily_questions, ai_daily_tokens: u.ai_daily_tokens });
      dlg.value = true;
    }
    async function submit() {
      busy.value = true;
      const body = { display_name: form.display_name, employee_no: form.employee_no.trim(), role: form.role, department_id: form.department_id || null,
        position: form.position || null, ai_daily_questions: form.ai_daily_questions, ai_daily_tokens: form.ai_daily_tokens };
      try {
        if (form.id) {
          body.active = form.active;
          if (form.password) body.password = form.password;
          await api('/api/users/' + form.id, { method: 'PUT', json: body });
        } else {
          await api('/api/users', { method: 'POST', json: Object.assign(body, { username: form.username, password: form.password }) });
        }
        dlg.value = false; ElMessage.success(t('saved')); loadAll();
      } catch (e) { ElMessage.error(e.message); }
      busy.value = false;
    }

    // ---- registration requests ----
    const apDlg = ref(false); const ap = reactive({ id: null, username: '', display_name: '', employee_no: '', role: 'viewer', department_id: null, position: '' });
    function openApprove(u) { Object.assign(ap, { id: u.id, username: u.username, display_name: u.display_name, employee_no: u.employee_no, role: 'viewer', department_id: null, position: '' }); apDlg.value = true; }
    async function approve() {
      try {
        await api('/api/users/' + ap.id + '/approve', { method: 'POST', json: { role: ap.role, department_id: ap.department_id || null, position: ap.position || null } });
        apDlg.value = false; ElMessage.success(t('approved')); loadAll();
      } catch (e) { ElMessage.error(e.message); }
    }
    async function reject(u) {
      try { await ElMessageBox.confirm(t('rejectConfirm', { n: u.display_name + ' (' + u.username + ')' }), t('reject'), { type: 'warning', confirmButtonText: t('reject'), cancelButtonText: t('no') }); } catch (e) { return; }
      try { await api('/api/users/' + u.id + '/reject', { method: 'POST' }); ElMessage.success(t('rejected')); loadAll(); } catch (e) { ElMessage.error(e.message); }
    }

    // ---- roles: permission matrix, every tick saves immediately ----
    const roleDlg = ref(false); const roleForm = reactive({ key: null, name: '', copyFrom: 'viewer' });
    function newRole() { Object.assign(roleForm, { key: null, name: '', copyFrom: 'viewer' }); roleDlg.value = true; }
    function renameRole(r) { Object.assign(roleForm, { key: r.key, name: r.name }); roleDlg.value = true; }
    async function saveRole() {
      try {
        if (roleForm.key) await api('/api/roles/' + roleForm.key, { method: 'PUT', json: { name: roleForm.name } });
        else {
          const src = roles.value.find(r => r.key === roleForm.copyFrom);
          await api('/api/roles', { method: 'POST', json: { name: roleForm.name, permissions: src ? src.permissions : [] } });
        }
        roleDlg.value = false; ElMessage.success(t('saved')); loadAll();
      } catch (e) { ElMessage.error(e.message); }
    }
    async function togglePerm(r, p, on) {
      const next = on ? r.permissions.concat([p]) : r.permissions.filter(x => x !== p);
      try { const res = await api('/api/roles/' + r.key, { method: 'PUT', json: { permissions: next } }); r.permissions = res.permissions; ElMessage.success(t('saved')); }
      catch (e) { ElMessage.error(e.message); }
    }
    async function roleCmd(r, cmd) {
      if (cmd === 'rename') return renameRole(r);
      try { await ElMessageBox.confirm(t('deleteRoleConfirm', { n: r.name }), t('remove'), { type: 'warning', confirmButtonText: t('remove'), cancelButtonText: t('no') }); } catch (e) { return; }
      try { await api('/api/roles/' + r.key, { method: 'DELETE' }); ElMessage.success(t('deleted')); loadAll(); } catch (e) { ElMessage.error(e.message); }
    }

    // ---- departments ----
    const deptDlg = ref(false); const deptForm = reactive({ id: null, name: '', see_all: false });
    function newDept() { Object.assign(deptForm, { id: null, name: '', see_all: false }); deptDlg.value = true; }
    function editDept(d) { Object.assign(deptForm, { id: d.id, name: d.name, see_all: !!d.see_all }); deptDlg.value = true; }
    async function saveDept() {
      try {
        if (deptForm.id) await api('/api/departments/' + deptForm.id, { method: 'PUT', json: { name: deptForm.name, see_all: deptForm.see_all } });
        else await api('/api/departments', { method: 'POST', json: { name: deptForm.name, see_all: deptForm.see_all } });
        deptDlg.value = false; ElMessage.success(t('saved')); loadAll();
      } catch (e) { ElMessage.error(e.message); }
    }
    async function toggleSeeAll(d, v) {
      try { await api('/api/departments/' + d.id, { method: 'PUT', json: { see_all: v } }); d.see_all = v ? 1 : 0; ElMessage.success(t('saved')); }
      catch (e) { ElMessage.error(e.message); }
    }
    async function deleteDept(d) {
      try { await ElMessageBox.confirm(t('deleteDeptConfirm', { n: d.name }), t('remove'), { type: 'warning', confirmButtonText: t('remove'), cancelButtonText: t('no') }); } catch (e) { return; }
      try { await api('/api/departments/' + d.id, { method: 'DELETE' }); ElMessage.success(t('deleted')); loadAll(); } catch (e) { ElMessageBox.alert(e.message, t('remove'), { type: 'warning' }); }
    }
    return { tab, users, pending, apDlg, ap, openApprove, approve, reject, roles, perms, depts, loading, dlg, busy, form, openNew, openEdit, submit, suggestPositions,
      roleDlg, roleForm, newRole, saveRole, togglePerm, roleCmd, deptDlg, deptForm, newDept, editDept, saveDept, toggleSeeAll, deleteDept, store, roleName };
  },
  template: `
  <div class="users">
    <div class="ph"><div><h1>{{ t('navUsers') }}</h1><div class="sub">{{ t('usersSub') }}</div></div>
      <div class="actions">
        <el-button v-if="tab === 'users'" type="primary" @click="openNew"><el-icon><Plus /></el-icon>{{ t('addUser') }}</el-button>
        <el-button v-if="tab === 'roles'" type="primary" @click="newRole"><el-icon><Plus /></el-icon>{{ t('addRole') }}</el-button>
        <el-button v-if="tab === 'depts'" type="primary" @click="newDept"><el-icon><Plus /></el-icon>{{ t('addDept') }}</el-button>
      </div></div>
    <el-tabs v-model="tab" v-loading="loading">
      <el-tab-pane :label="t('tabUsers')" name="users">
        <div class="panel">
          <el-table :data="users" style="width:100%">
            <el-table-column :label="t('username')" prop="username" width="130" />
            <el-table-column :label="t('displayName')" prop="display_name" min-width="110" />
            <el-table-column :label="t('employeeNo')" min-width="100"><template #default="{ row }"><span class="mono-ish">{{ row.employee_no || '—' }}</span></template></el-table-column>
            <el-table-column :label="t('department')" min-width="120"><template #default="{ row }">{{ row.department_name || '—' }}</template></el-table-column>
            <el-table-column :label="t('position')" min-width="110"><template #default="{ row }">{{ row.position || '—' }}</template></el-table-column>
            <el-table-column :label="t('role')" width="120"><template #default="{ row }">
              <el-tag size="small" :type="row.role === 'admin' ? 'danger' : row.role === 'editor' ? 'warning' : 'info'">{{ roleName(row.role) }}</el-tag></template></el-table-column>
            <el-table-column :label="t('active')" width="80"><template #default="{ row }">
              <span :class="row.active ? 'ok' : 'muted'">{{ row.active ? t('enabled') : t('disabled') }}</span></template></el-table-column>
            <el-table-column :label="t('lastLogin')" width="170"><template #default="{ row }"><span class="num muted">{{ row.last_login || '—' }}</span></template></el-table-column>
            <el-table-column width="80" align="right"><template #default="{ row }">
              <el-button link type="primary" @click="openEdit(row)">{{ t('edit') }}</el-button></template></el-table-column>
          </el-table>
        </div>
      </el-tab-pane>

      <el-tab-pane name="pending">
        <template #label><span>{{ t('tabPending') }}</span><el-badge v-if="pending.length" :value="pending.length" class="tab-badge" /></template>
        <p class="note">{{ t('pendingHelp') }}</p>
        <div class="panel">
          <el-table :data="pending" style="width:100%" :empty-text="t('noPending')">
            <el-table-column :label="t('username')" prop="username" width="140" />
            <el-table-column :label="t('displayName')" prop="display_name" min-width="110" />
            <el-table-column :label="t('employeeNo')" min-width="110"><template #default="{ row }"><span class="mono-ish">{{ row.employee_no }}</span></template></el-table-column>
            <el-table-column :label="t('appliedAt')" width="170"><template #default="{ row }"><span class="num muted">{{ row.created_at }}</span></template></el-table-column>
            <el-table-column width="170" align="right"><template #default="{ row }">
              <el-button size="small" type="success" @click="openApprove(row)">{{ t('approve') }}</el-button>
              <el-button size="small" type="danger" plain @click="reject(row)">{{ t('reject') }}</el-button></template></el-table-column>
          </el-table>
        </div>
      </el-tab-pane>

      <el-tab-pane :label="t('tabRoles')" name="roles">
        <p class="note">{{ t('rolesHelp') }}</p>
        <div class="panel">
          <el-table :data="perms" style="width:100%" class="perm-matrix">
            <el-table-column :label="t('permission')" min-width="220" fixed>
              <template #default="{ row }"><div>{{ t('p_' + row) }}</div><div class="muted small">{{ t('pd_' + row) }}</div></template></el-table-column>
            <el-table-column v-for="r in roles" :key="r.key" min-width="120" align="center">
              <template #header>
                <div class="role-head">
                  <span>{{ roleName(r.key) }}</span>
                  <el-dropdown v-if="r.key !== 'admin'" trigger="click" @command="cmd => roleCmd(r, cmd)">
                    <el-icon class="role-more"><MoreFilled /></el-icon>
                    <template #dropdown><el-dropdown-menu>
                      <el-dropdown-item command="rename">{{ t('rename') }}</el-dropdown-item>
                      <el-dropdown-item v-if="!r.builtin" command="delete" divided><span class="danger">{{ t('remove') }}</span></el-dropdown-item>
                    </el-dropdown-menu></template>
                  </el-dropdown>
                </div>
                <div class="muted small">{{ t('nUsers', { n: r.users }) }}</div>
              </template>
              <template #default="{ row }">
                <el-checkbox :model-value="r.permissions.includes(row)" :disabled="r.key === 'admin'" @change="v => togglePerm(r, row, v)" />
              </template>
            </el-table-column>
          </el-table>
        </div>
        <p class="note mt-s">{{ t('adminOnlyHelp') }}</p>
      </el-tab-pane>

      <el-tab-pane :label="t('tabDepts')" name="depts">
        <p class="note">{{ t('deptsHelp') }}</p>
        <div class="panel">
          <el-table :data="depts" style="width:100%" :empty-text="t('noDepts')">
            <el-table-column :label="t('department')" prop="name" min-width="160" />
            <el-table-column :label="t('seeAll')" width="160"><template #default="{ row }">
              <el-switch :model-value="!!row.see_all" @change="v => toggleSeeAll(row, v)" /></template></el-table-column>
            <el-table-column :label="t('members')" width="90" align="right"><template #default="{ row }">{{ row.users }}</template></el-table-column>
            <el-table-column :label="t('contracts')" width="90" align="right"><template #default="{ row }">{{ row.contracts }}</template></el-table-column>
            <el-table-column width="140" align="right"><template #default="{ row }">
              <el-button link type="primary" @click="editDept(row)">{{ t('edit') }}</el-button>
              <el-button link type="danger" @click="deleteDept(row)">{{ t('remove') }}</el-button></template></el-table-column>
          </el-table>
        </div>
      </el-tab-pane>
    </el-tabs>

    <el-dialog v-model="dlg" :title="form.id ? t('editUser') : t('addUser')" width="520px">
      <el-form label-width="110px" label-position="left" @submit.prevent="submit">
        <el-form-item :label="t('username')"><el-input v-model="form.username" :disabled="!!form.id" maxlength="32" :placeholder="t('ruleUsername')" /></el-form-item>
        <el-form-item :label="t('displayName')"><el-input v-model="form.display_name" /></el-form-item>
        <el-form-item :label="t('employeeNo')"><el-input v-model="form.employee_no" maxlength="32" :placeholder="t('employeeNoPh')" /></el-form-item>
        <el-form-item :label="t('department')">
          <el-select v-model="form.department_id" clearable :placeholder="t('noDept')" style="width:100%">
            <el-option v-for="d in depts" :key="d.id" :value="d.id" :label="d.name" /></el-select></el-form-item>
        <el-form-item :label="t('position')">
          <el-autocomplete v-model="form.position" :fetch-suggestions="suggestPositions" clearable style="width:100%" :placeholder="t('positionPh')" /></el-form-item>
        <el-form-item :label="t('role')">
          <el-select v-model="form.role" style="width:100%" :disabled="form.id === store.user.id">
            <el-option v-for="r in roles" :key="r.key" :value="r.key" :label="roleName(r.key)" /></el-select></el-form-item>
        <el-form-item v-if="form.id" :label="t('active')"><el-switch v-model="form.active" :disabled="form.id === store.user.id" /></el-form-item>
        <el-form-item :label="form.id ? t('resetPw') : t('password')">
          <el-input v-model="form.password" type="password" show-password :placeholder="form.id ? t('resetPwPh') : t('pwRule')" /></el-form-item>
        <el-form-item :label="t('aiQuota')">
          <div class="quota-row">
            <el-input-number v-model="form.ai_daily_questions" :min="0" :max="10000" :controls="false" :placeholder="t('useDefault')" />
            <span class="muted small">{{ t('questionsPerDay') }}</span>
            <el-input-number v-model="form.ai_daily_tokens" :min="0" :max="100000000" :step="10000" :controls="false" :placeholder="t('useDefault')" />
            <span class="muted small">{{ t('tokensPerDay') }}</span>
          </div></el-form-item>
      </el-form>
      <template #footer><el-button @click="dlg = false">{{ t('cancel') }}</el-button><el-button type="primary" :loading="busy" @click="submit">{{ t('save') }}</el-button></template>
    </el-dialog>

    <el-dialog v-model="apDlg" :title="t('approveTitle')" width="480px">
      <p class="note">{{ ap.display_name }} · {{ ap.username }} · {{ t('employeeNo') }} {{ ap.employee_no }}</p>
      <el-form label-width="96px" label-position="left">
        <el-form-item :label="t('role')">
          <el-select v-model="ap.role" style="width:100%"><el-option v-for="r in roles" :key="r.key" :value="r.key" :label="roleName(r.key)" /></el-select></el-form-item>
        <el-form-item :label="t('department')">
          <el-select v-model="ap.department_id" clearable :placeholder="t('noDept')" style="width:100%">
            <el-option v-for="d in depts" :key="d.id" :value="d.id" :label="d.name" /></el-select></el-form-item>
        <el-form-item :label="t('position')"><el-autocomplete v-model="ap.position" :fetch-suggestions="suggestPositions" clearable style="width:100%" /></el-form-item>
      </el-form>
      <template #footer><el-button @click="apDlg = false">{{ t('cancel') }}</el-button><el-button type="success" @click="approve">{{ t('approveBtn') }}</el-button></template>
    </el-dialog>

    <el-dialog v-model="roleDlg" :title="roleForm.key ? t('rename') : t('addRole')" width="420px">
      <el-form label-width="96px" label-position="left" @submit.prevent="saveRole">
        <el-form-item :label="t('roleName')"><el-input v-model="roleForm.name" :placeholder="t('roleNamePh')" /></el-form-item>
        <el-form-item v-if="!roleForm.key" :label="t('copyFrom')">
          <el-select v-model="roleForm.copyFrom" style="width:100%">
            <el-option v-for="r in roles" :key="r.key" :value="r.key" :label="roleName(r.key)" /></el-select></el-form-item>
      </el-form>
      <template #footer><el-button @click="roleDlg = false">{{ t('cancel') }}</el-button><el-button type="primary" @click="saveRole">{{ t('save') }}</el-button></template>
    </el-dialog>

    <el-dialog v-model="deptDlg" :title="deptForm.id ? t('editDept') : t('addDept')" width="420px">
      <el-form label-width="96px" label-position="left" @submit.prevent="saveDept">
        <el-form-item :label="t('department')"><el-input v-model="deptForm.name" :placeholder="t('deptNamePh')" /></el-form-item>
        <el-form-item :label="t('seeAll')"><el-switch v-model="deptForm.see_all" /><span class="muted small ml-s">{{ t('seeAllHelp') }}</span></el-form-item>
      </el-form>
      <template #footer><el-button @click="deptDlg = false">{{ t('cancel') }}</el-button><el-button type="primary" @click="saveDept">{{ t('save') }}</el-button></template>
    </el-dialog>
  </div>`,
};

// ---------- AI assistant ----------
function renderAnswer(text, cites) {
  const labels = {};
  (cites || []).forEach(c => { labels[c.id] = c.label; });
  let html = window.DOMPurify.sanitize(window.marked.parse(text || '', { breaks: true, gfm: true }),
    { FORBID_TAGS: ['style', 'img', 'iframe', 'form', 'input'], FORBID_ATTR: ['style', 'onerror', 'onclick'] });
  html = html.replace(/\[#(\d+)(?:\s*p\.?\s*(\d+))?\]/g, (m, id, p) =>
    '<a class="cite" href="javascript:void 0" data-cid="' + id + '" data-page="' + (p || '') + '">' +
    esc(labels[id] || ('#' + id)) + (p ? ' p.' + p : '') + '</a>');
  return html;
}
function stepText(s) {
  if (!s) return '';
  if (s.kind === 'search') return t('st_search', { q: s.text || t('all'), n: s.count == null ? '?' : s.count });
  if (s.kind === 'contract') return t('st_contract', { id: s.contract_id });
  if (s.kind === 'text') return t('st_text', { q: s.text || '', n: s.count == null ? '?' : s.count });
  if (s.kind === 'pages') return t('st_pages', { id: s.contract_id, p: (s.pages || []).join(', ') });
  if (s.kind === 'items') return t('st_items', { q: s.text || t('all'), n: s.count == null ? '?' : s.count });
  return s.kind;
}
function dayGroup(ts) {
  const d = new Date((ts || '').replace(' ', 'T')); const now = new Date();
  const days = Math.floor((new Date(now.toDateString()) - new Date(d.toDateString())) / 86400000);
  return days <= 0 ? 'g_today' : days === 1 ? 'g_yesterday' : days < 7 ? 'g_week' : days < 30 ? 'g_month' : 'g_older';
}

const AIView = {
  mixins: [mixin],
  props: ['id'],
  setup(props) {
    const convs = ref([]); const search = ref(''); const conv = ref(null); const messages = ref([]);
    const input = ref(''); const busy = ref(false); const usage = ref(null); const scopePick = ref([]); const scopeOptions = ref([]);
    const scroller = ref(null); let controller = null; let debounce = null;
    async function loadConvs() {
      try { convs.value = await api('/api/ai/conversations?q=' + encodeURIComponent(search.value)); } catch (e) { ElMessage.error(e.message); }
    }
    async function loadUsage() { try { usage.value = await api('/api/ai/usage'); } catch (e) {} }
    async function openConv(id) {
      if (!id) { conv.value = null; messages.value = []; return; }
      try { const c = await api('/api/ai/conversations/' + id); conv.value = c; messages.value = c.messages; scrollDown(); }
      catch (e) { ElMessage.error(e.message); go('ai'); }
    }
    watch(search, () => { clearTimeout(debounce); debounce = setTimeout(loadConvs, 300); });
    let justCreated = null;  // conversation created on this page: keep the streaming messages, don't reload
    watch(() => props.id, id => { if (id && id === justCreated) return; openConv(id); });
    onMounted(() => { loadConvs(); loadUsage(); openConv(props.id); });
    onBeforeUnmount(() => { if (controller) controller.abort(); });
    const groups = computed(() => {
      const out = {};
      convs.value.forEach(c => { const g = dayGroup(c.updated_at); (out[g] = out[g] || []).push(c); });
      return ['g_today', 'g_yesterday', 'g_week', 'g_month', 'g_older'].filter(g => out[g]).map(g => ({ key: g, items: out[g] }));
    });
    function scrollDown() { nextTick(() => { const el = scroller.value; if (el) el.scrollTop = el.scrollHeight; }); }
    async function searchContracts(q) {
      try { scopeOptions.value = (await api('/api/contracts?q=' + encodeURIComponent(q || ''))).slice(0, 30); } catch (e) {}
    }
    function newChat() { go('ai'); conv.value = null; messages.value = []; scopePick.value = []; input.value = ''; }

    async function send(text) {
      const q = (text || input.value).trim();
      if (!q || busy.value) return;
      let c = conv.value;
      if (!c) {
        try { c = await api('/api/ai/conversations', { method: 'POST', json: { contract_ids: scopePick.value } }); }
        catch (e) { ElMessage.error(e.message); return; }
        conv.value = Object.assign(c, { messages: [] }); justCreated = c.id;
        history.replaceState(null, '', '#/ai/' + c.id); store.route = parseHash();
      }
      input.value = '';
      messages.value.push({ role: 'user', content: q });
      const ans = reactive({ role: 'assistant', content: '', steps: [], citations: [], status: 'streaming' });
      messages.value.push(ans); scrollDown();
      busy.value = true; controller = new AbortController();
      try {
        const r = await fetch('/api/ai/conversations/' + c.id + '/messages', {
          method: 'POST', headers: { 'Content-Type': 'application/json' }, body: JSON.stringify({ content: q }), signal: controller.signal });
        if (!r.ok) {
          let msg = r.status + ''; try { msg = (await r.json()).detail || msg; } catch (e) {}
          ans.status = 'error'; ans.content = msg; messages.value.splice(messages.value.length - 2, 1);
          throw new Error(msg);
        }
        const reader = r.body.getReader(); const dec = new TextDecoder(); let buf = '';
        for (;;) {
          const { value, done } = await reader.read();
          if (done) break;
          buf += dec.decode(value, { stream: true });
          let i;
          while ((i = buf.indexOf('\n\n')) >= 0) {
            const chunk = buf.slice(0, i); buf = buf.slice(i + 2);
            const line = chunk.split('\n').find(l => l.startsWith('data: '));
            if (!line) continue;
            const ev = JSON.parse(line.slice(6));
            if (ev.type === 'delta') { ans.content += ev.text; scrollDown(); }
            else if (ev.type === 'step') ans.steps.push(ev.step);
            else if (ev.type === 'error') { ans.error = ev.message; }
            else if (ev.type === 'done') { ans.citations = ev.citations; ans.status = ev.status; ans.id = ev.message_id; }
          }
        }
        if (ans.status === 'streaming') ans.status = 'done';
      } catch (e) {
        if (e.name === 'AbortError') ans.status = 'stopped';
        else ElMessage.error(e.message);
      } finally {
        busy.value = false; controller = null; scrollDown(); loadUsage();
        await loadConvs();
        const fresh = conv.value && convs.value.find(x => x.id === conv.value.id);
        if (fresh) conv.value.title = fresh.title;
      }
    }
    function stop() { if (controller) controller.abort(); }
    function onKey(e) { if (e.key === 'Enter' && !e.shiftKey && !e.isComposing) { e.preventDefault(); send(); } }
    function onAnswerClick(e) {
      const a = e.target.closest('.cite'); if (!a) return;
      go('detail', a.dataset.cid, a.dataset.page ? { p: a.dataset.page } : null);
    }
    async function copy(m) { try { await navigator.clipboard.writeText(m.content); ElMessage.success(t('copied')); } catch (e) {} }
    async function rename(c) {
      let v; try { v = (await ElMessageBox.prompt(t('renameChat'), t('rename'), { inputValue: c.title, confirmButtonText: t('save'), cancelButtonText: t('cancel') })).value; } catch (e) { return; }
      await api('/api/ai/conversations/' + c.id, { method: 'PUT', json: { title: v } }); loadConvs();
      if (conv.value && conv.value.id === c.id) conv.value.title = v;
    }
    async function remove(c) {
      try { await ElMessageBox.confirm(t('deleteChatConfirm'), t('remove'), { type: 'warning', confirmButtonText: t('remove'), cancelButtonText: t('no') }); } catch (e) { return; }
      await api('/api/ai/conversations/' + c.id, { method: 'DELETE' }); loadConvs();
      if (conv.value && conv.value.id === c.id) newChat();
    }
    function onConvCmd(c, cmd) { if (cmd === 'rename') rename(c); if (cmd === 'delete') remove(c); }
    const examples = computed(() => [t('ex1'), t('ex2'), t('ex3'), t('ex4')]);
    return { convs, search, conv, messages, input, busy, usage, scopePick, scopeOptions, scroller, groups, newChat, send, stop,
      onKey, onAnswerClick, copy, onConvCmd, searchContracts, examples, renderAnswer, stepText, go, store };
  },
  template: `
  <div class="ai">
    <aside class="ai-side">
      <el-button type="primary" class="w100" @click="newChat"><el-icon><Plus /></el-icon>{{ t('newChat') }}</el-button>
      <el-input v-model="search" :placeholder="t('searchChats')" clearable class="mt-s"><template #prefix><el-icon><Search /></el-icon></template></el-input>
      <div class="conv-list">
        <template v-for="g in groups" :key="g.key">
          <div class="conv-group">{{ t(g.key) }}</div>
          <div v-for="c in g.items" :key="c.id" class="conv" :class="{ on: conv && conv.id === c.id }" @click="go('ai', c.id)">
            <span class="conv-title">{{ c.title }}</span>
            <el-icon v-if="c.scope" class="conv-scope" :title="c.scope.map(s => s.label).join(', ')"><Paperclip /></el-icon>
            <el-dropdown trigger="click" @command="cmd => onConvCmd(c, cmd)" @click.stop>
              <el-icon class="conv-more" @click.stop><MoreFilled /></el-icon>
              <template #dropdown><el-dropdown-menu>
                <el-dropdown-item command="rename">{{ t('rename') }}</el-dropdown-item>
                <el-dropdown-item command="delete" divided><span class="danger">{{ t('remove') }}</span></el-dropdown-item>
              </el-dropdown-menu></template>
            </el-dropdown>
          </div>
        </template>
        <div v-if="!convs.length" class="muted small conv-empty">{{ search ? t('noMatch') : t('noChats') }}</div>
      </div>
      <div v-if="usage" class="usage-line">{{ t('usageToday', { q: usage.used.questions, ql: usage.limits.questions }) }}
        <el-progress :percentage="Math.min(100, Math.round(100 * usage.used.tokens / Math.max(1, usage.limits.tokens)))" :show-text="false" :stroke-width="4" /></div>
    </aside>

    <section class="ai-main">
      <div class="ai-head">
        <div class="ai-title">{{ conv ? conv.title : t('aiTitle') }}</div>
        <div class="ai-scope">
          <template v-if="conv && conv.scope"><el-icon><Paperclip /></el-icon>
            <el-tag v-for="s in conv.scope" :key="s.id" size="small" effect="plain" class="clickable" @click="go('detail', s.id)">{{ s.label }}</el-tag></template>
          <span v-else-if="conv" class="muted small">{{ t('scopeAll') }}</span>
        </div>
      </div>

      <div class="ai-scroll" ref="scroller" @click="onAnswerClick">
        <div v-if="!messages.length" class="ai-welcome">
          <h2>{{ t('aiWelcome') }}</h2>
          <p class="note">{{ t('aiWelcomeText') }}</p>
          <div v-if="!conv" class="scope-pick">
            <el-select v-model="scopePick" multiple filterable remote :remote-method="searchContracts" @focus="searchContracts('')"
              :placeholder="t('scopePickPh')" style="width:100%">
              <el-option v-for="c in scopeOptions" :key="c.id" :value="c.id" :label="(c.contract_no ? c.contract_no + ' · ' : '') + (c.title_zh || c.title || c.file_name)" />
            </el-select>
          </div>
          <div class="examples">
            <button v-for="(e, i) in examples" :key="i" type="button" class="example" @click="send(e)">{{ e }}</button>
          </div>
        </div>

        <div v-for="(m, i) in messages" :key="m.id || ('tmp' + i)" class="msg" :class="m.role">
          <div v-if="m.role === 'user'" class="bubble">{{ m.content }}</div>
          <div v-else class="answer">
            <div v-if="m.steps && m.steps.length" class="steps">
              <details :open="m.status === 'streaming'">
                <summary>{{ m.status === 'streaming' && !m.content ? t('thinking') : t('consulted', { n: m.steps.length }) }}</summary>
                <ul><li v-for="(s, j) in m.steps" :key="j">{{ stepText(s) }}</li></ul>
              </details>
            </div>
            <div v-else-if="m.status === 'streaming' && !m.content" class="muted small thinking">{{ t('thinking') }}</div>
            <div class="md" v-html="renderAnswer(m.content, m.citations)"></div>
            <span v-if="m.status === 'streaming' && m.content" class="caret"></span>
            <div v-if="m.status === 'stopped'" class="muted small">— {{ t('stopped') }}</div>
            <div v-if="m.status === 'error' || m.error" class="err small">{{ m.error || m.content }}</div>
            <div v-if="m.status !== 'streaming' && m.content" class="msg-tools">
              <el-button link size="small" @click.stop="copy(m)"><el-icon><DocumentCopy /></el-icon>{{ t('copy') }}</el-button>
            </div>
          </div>
        </div>
      </div>

      <div class="composer">
        <el-input v-model="input" type="textarea" :autosize="{ minRows: 1, maxRows: 6 }" :placeholder="t('askPh')" @keydown="onKey" :disabled="busy" />
        <el-button v-if="!busy" type="primary" :disabled="!input.trim()" @click="send()"><el-icon><Promotion /></el-icon></el-button>
        <el-button v-else type="danger" plain @click="stop"><el-icon><VideoPause /></el-icon>{{ t('stop') }}</el-button>
      </div>
      <div class="composer-note muted small">{{ t('aiDisclaimer') }}</div>
    </section>
  </div>`,
};

// ---------- settings view ----------
const SettingsView = {
  mixins: [mixin],
  setup() {
    const cfg = reactive({ provider: '', base_url: '', model: '', api_key: '', max_tokens: 8192,
      chat_model: '', ai_daily_questions: 100, ai_daily_tokens: 500000, ai_global_daily_tokens: 5000000, ai_per_minute: 6 });
    const aiUsage = ref(null);
    async function loadAiUsage() { try { aiUsage.value = await api('/api/ai/usage/all'); } catch (e) {} }
    const providers = ref({}); const keySet = ref(false); const keyMask = ref(''); const testing = ref(false); const saving = ref(false);
    async function load() {
      const r = await api('/api/settings');
      providers.value = r.providers;
      Object.assign(cfg, { provider: r.config.provider, base_url: r.config.base_url, model: r.config.model, max_tokens: r.config.max_tokens, api_key: '',
        chat_model: r.config.chat_model || '', ai_daily_questions: r.config.ai_daily_questions, ai_daily_tokens: r.config.ai_daily_tokens,
        ai_global_daily_tokens: r.config.ai_global_daily_tokens, ai_per_minute: r.config.ai_per_minute });
      loadAiUsage();
      keySet.value = r.config.api_key_set; keyMask.value = r.config.api_key;
    }
    onMounted(load);
    function onProvider(p) {
      const x = providers.value[p];
      if (x && x.base_url) { cfg.base_url = x.base_url; cfg.model = x.model; }
    }
    function payload() { return Object.assign({}, cfg, { api_key: cfg.api_key || '__keep__', max_tokens: Number(cfg.max_tokens) || 8192 }); }
    async function save() {
      saving.value = true;
      try { await api('/api/settings', { method: 'PUT', json: payload() }); ElMessage.success(t('saved')); await load(); }
      catch (e) { ElMessage.error(e.message); }
      saving.value = false;
    }
    async function test() {
      testing.value = true;
      const r = await api('/api/settings/test', { method: 'POST', json: payload() });
      testing.value = false;
      if (r.ok) ElMessage.success(t('testOk')); else ElMessageBox.alert(r.error, t('testFail'), { type: 'error' });
    }
    return { cfg, providers, keySet, keyMask, testing, saving, onProvider, save, test, aiUsage, fmt };
  },
  template: `
  <div class="settings">
    <div class="ph"><div><h1>{{ t('settingsTitle') }}</h1></div></div>
    <div class="card">
      <h2>{{ t('aiModel') }}</h2>
      <el-form label-width="140px" label-position="left">
        <el-form-item :label="t('provider')">
          <el-select v-model="cfg.provider" @change="onProvider" style="width:100%">
            <el-option v-for="(p, k) in providers" :key="k" :value="k" :label="p.label" /></el-select></el-form-item>
        <el-form-item :label="t('baseUrl')"><el-input v-model="cfg.base_url" /></el-form-item>
        <el-form-item :label="t('model')"><el-input v-model="cfg.model" placeholder="deepseek-chat / qwen-plus / qwen-max" /></el-form-item>
        <el-form-item :label="t('apiKey')">
          <el-input v-model="cfg.api_key" type="password" show-password :placeholder="keySet ? keyMask + '  (' + t('apiKeySet') + ')' : 'sk-...'" /></el-form-item>
        <el-form-item :label="t('maxTokens')"><el-input-number v-model="cfg.max_tokens" :min="1024" :max="32768" :step="1024" /></el-form-item>
        <el-form-item>
          <el-button type="primary" :loading="saving" @click="save">{{ t('save') }}</el-button>
          <el-button :loading="testing" @click="test">{{ t('test') }}</el-button>
        </el-form-item>
      </el-form>
    </div>
    <div class="card">
      <h2>{{ t('aiAssistant') }}</h2>
      <el-form label-width="190px" label-position="left">
        <el-form-item :label="t('chatModel')"><el-input v-model="cfg.chat_model" :placeholder="t('chatModelPh')" /></el-form-item>
        <el-form-item :label="t('qDaily')"><el-input-number v-model="cfg.ai_daily_questions" :min="0" :max="10000" /></el-form-item>
        <el-form-item :label="t('tDaily')"><el-input-number v-model="cfg.ai_daily_tokens" :min="0" :max="100000000" :step="50000" /></el-form-item>
        <el-form-item :label="t('tGlobal')"><el-input-number v-model="cfg.ai_global_daily_tokens" :min="0" :max="1000000000" :step="500000" /></el-form-item>
        <el-form-item :label="t('qMinute')"><el-input-number v-model="cfg.ai_per_minute" :min="1" :max="100" /></el-form-item>
        <el-form-item><el-button type="primary" :loading="saving" @click="save">{{ t('save') }}</el-button>
          <span class="muted small ml-s">{{ t('quotaHelp') }}</span></el-form-item>
      </el-form>
      <template v-if="aiUsage">
        <h2 class="mt">{{ t('aiUsageTitle') }} <span class="muted">{{ t('globalToday', { u: fmt(aiUsage.global_today), l: fmt(aiUsage.global_limit) }) }}</span></h2>
        <el-table :data="aiUsage.users" size="small" border>
          <el-table-column :label="t('user')" min-width="120"><template #default="{ row }">{{ row.display_name || row.username }}</template></el-table-column>
          <el-table-column :label="t('qToday')" width="120" align="right"><template #default="{ row }">{{ row.q_today }} / {{ row.limits.questions }}</template></el-table-column>
          <el-table-column :label="t('tToday')" width="170" align="right"><template #default="{ row }">{{ fmt(row.t_today) }} / {{ fmt(row.limits.tokens) }}</template></el-table-column>
          <el-table-column :label="t('qMonth')" width="110" align="right"><template #default="{ row }">{{ row.q_month }}</template></el-table-column>
          <el-table-column :label="t('tMonth')" width="130" align="right"><template #default="{ row }">{{ fmt(row.t_month) }}</template></el-table-column>
        </el-table>
      </template>
    </div>
    <div class="card"><h2>{{ t('privacyTitle') }}</h2><p class="note">{{ t('privacyText') }}</p></div>
    <div class="card"><h2>{{ t('backupTitle') }}</h2><p class="note">{{ t('backupText') }}</p>
      <el-button tag="a" href="/api/backup"><el-icon><Download /></el-icon>{{ t('backupBtn') }}</el-button></div>
  </div>`,
};

// ---------- root ----------
const Root = {
  mixins: [mixin],
  components: { AuthView, ListView, DetailView, AuditView, UsersView, SettingsView, AIView },
  setup() {
    const epLocale = computed(() => store.lang === 'zh' ? ElementPlusLocaleZhCn : ElementPlusLocaleEn);
    function sync() { store.route = parseHash(); window.scrollTo(0, 0); }
    window.addEventListener('hashchange', sync); sync();
    async function checkAuth() {
      try { const s = await api('/api/auth/state'); store.user = s.user; store.needsSetup = s.needs_setup; }
      catch (e) { store.user = null; }
      store.authChecked = true;
    }
    onMounted(checkAuth);
    async function pollPending() {
      if (store.user && store.user.role === 'admin') {
        try { store.pendingCount = (await api('/api/users/pending/count')).count; } catch (e) {}
      }
    }
    watch(() => store.user && store.user.id, pollPending);
    const pendingTimer = setInterval(pollPending, 60000);
    onBeforeUnmount(() => clearInterval(pendingTimer));
    function toggleLang() {
      store.lang = store.lang === 'zh' ? 'en' : 'zh';
      try { localStorage.setItem('cms_lang', store.lang); } catch (e) {}
      document.documentElement.lang = store.lang === 'zh' ? 'zh-CN' : 'en';
    }
    async function logout() { await api('/api/auth/logout', { method: 'POST' }); store.user = null; go('list'); }
    const pwDlg = ref(false); const pw = reactive({ old_password: '', new_password: '', again: '' });
    async function changePw() {
      if (pw.new_password !== pw.again) { ElMessage.error(t('pwMismatch')); return; }
      try { await api('/api/auth/password', { method: 'POST', json: pw }); pwDlg.value = false; ElMessage.success(t('pwChanged')); Object.assign(pw, { old_password: '', new_password: '', again: '' }); }
      catch (e) { ElMessage.error(e.message); }
    }
    function onUserCmd(cmd) { if (cmd === 'logout') logout(); if (cmd === 'pw') pwDlg.value = true; if (cmd === 'lang') toggleLang(); }
    const active = computed(() => store.route.name === 'detail' ? 'list' : store.route.name);
    const allowed = computed(() => {
      const n = store.route.name;
      if ((n === 'users' || n === 'settings') && !can('admin')) return false;
      if (n === 'audit' && !can('audit.view')) return false;
      if (n === 'ai' && !can('ai.use')) return false;
      return true;
    });
    return { store, epLocale, toggleLang, active, go, onUserCmd, pwDlg, pw, changePw, allowed };
  },
  template: `
  <el-config-provider :locale="epLocale">
    <template v-if="store.authChecked">
      <AuthView v-if="!store.user" />
      <div v-else class="shell">
        <aside class="nav">
          <div class="brand"><div class="brand-mark" aria-hidden="true"></div>
            <div><div class="brand-name">{{ t('appName') }}</div><div class="brand-sub">SIPG Bayport</div></div></div>
          <el-menu :default-active="active" @select="k => go(k)" background-color="transparent" text-color="#C9D3DD" active-text-color="#ffffff" class="nav-menu">
            <el-menu-item index="list"><el-icon><Tickets /></el-icon><span>{{ t('navContracts') }}</span></el-menu-item>
            <el-menu-item v-if="can('ai.use')" index="ai"><el-icon><ChatDotRound /></el-icon><span>{{ t('navAI') }}</span></el-menu-item>
            <el-menu-item v-if="can('audit.view')" index="audit"><el-icon><Notebook /></el-icon><span>{{ t('navAudit') }}</span></el-menu-item>
            <el-menu-item v-if="can('admin')" index="users"><el-icon><User /></el-icon><span>{{ t('navUsers') }}</span>
              <el-badge v-if="store.pendingCount" :value="store.pendingCount" class="nav-badge" /></el-menu-item>
            <el-menu-item v-if="can('admin')" index="settings"><el-icon><Setting /></el-icon><span>{{ t('navSettings') }}</span></el-menu-item>
          </el-menu>
          <div class="nav-foot">
            <el-dropdown trigger="click" @command="onUserCmd" placement="top-start" class="w100">
              <button class="me" type="button">
                <span class="avatar">{{ (store.user.display_name || store.user.username).slice(0, 1).toUpperCase() }}</span>
                <span class="me-text"><span class="me-name">{{ store.user.display_name || store.user.username }}</span>
                  <span class="me-role">{{ t('r_' + store.user.role) }}</span></span>
                <el-icon class="me-more"><ArrowUp /></el-icon>
              </button>
              <template #dropdown><el-dropdown-menu>
                <el-dropdown-item command="pw">{{ t('changePw') }}</el-dropdown-item>
                <el-dropdown-item command="lang">{{ t('lang') }}</el-dropdown-item>
                <el-dropdown-item command="logout" divided>{{ t('logout') }}</el-dropdown-item>
              </el-dropdown-menu></template>
            </el-dropdown>
            <div class="foot-actions">
              <button type="button" @click="toggleLang"><el-icon><Switch /></el-icon>{{ t('lang') }}</button>
              <button type="button" @click="onUserCmd('logout')"><el-icon><SwitchButton /></el-icon>{{ t('logout') }}</button>
            </div>
          </div>
        </aside>
        <main class="main" :class="{ 'main-ai': store.route.name === 'ai' }">
          <template v-if="allowed">
            <ListView v-if="store.route.name === 'list'" :key="'l' + (store.route.query.q || '')" />
            <DetailView v-else-if="store.route.name === 'detail'" :id="store.route.id" :key="'d' + store.route.id + JSON.stringify(store.route.query)" />
            <AuditView v-else-if="store.route.name === 'audit'" />
            <AIView v-else-if="store.route.name === 'ai'" :id="store.route.id" />
            <UsersView v-else-if="store.route.name === 'users'" />
            <SettingsView v-else-if="store.route.name === 'settings'" />
          </template>
          <el-result v-else icon="warning" :title="'403'" />
        </main>
        <el-dialog v-model="pwDlg" :title="t('changePw')" width="420px">
          <el-form label-position="top">
            <el-form-item :label="t('oldPw')"><el-input v-model="pw.old_password" type="password" show-password /></el-form-item>
            <el-form-item :label="t('newPw')"><el-input v-model="pw.new_password" type="password" show-password /></el-form-item>
            <el-form-item :label="t('passwordAgain')"><el-input v-model="pw.again" type="password" show-password @keyup.enter="changePw" /></el-form-item>
          </el-form>
          <template #footer><el-button @click="pwDlg = false">{{ t('cancel') }}</el-button><el-button type="primary" @click="changePw">{{ t('save') }}</el-button></template>
        </el-dialog>
      </div>
    </template>
  </el-config-provider>`,
};

const app = createApp(Root);
app.use(ElementPlus);
for (const [name, comp] of Object.entries(ElementPlusIconsVue)) app.component(name, comp);
app.mount('#app');
