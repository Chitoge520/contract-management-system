/* Contract Register — Vue 3 + Element Plus, no build step */
const { createApp, reactive, ref, computed, watch, onMounted, onBeforeUnmount, provide, inject, nextTick } = Vue;
const { ElMessage, ElMessageBox } = ElementPlus;

// ---------- shared state ----------
const store = reactive({
  lang: (() => { try { return localStorage.getItem('cms_lang') || 'zh'; } catch (e) { return 'zh'; } })(),
  route: { name: 'list', id: null },
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
async function api(path, opts = {}) {
  const o = Object.assign({}, opts);
  if (o.json !== undefined) { o.body = JSON.stringify(o.json); o.headers = { 'Content-Type': 'application/json' }; delete o.json; }
  const r = await fetch(path, o);
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
function go(name, id) { location.hash = name === 'list' ? '#/' : name === 'detail' ? '#/c/' + id : '#/' + name; }
function parseHash() {
  const h = location.hash.replace(/^#\/?/, '');
  const m = h.match(/^c\/(\d+)/);
  if (m) return { name: 'detail', id: Number(m[1]) };
  if (h.startsWith('items')) return { name: 'items', id: null };
  if (h.startsWith('settings')) return { name: 'settings', id: null };
  return { name: 'list', id: null };
}
const mixin = { methods: { t, fmt, statusTag }, computed: { lang() { return store.lang; } } };

// ---------- page reference chip ----------
const PageRef = {
  mixins: [mixin], props: ['p'],
  setup() { return { openPage: inject('openPage', () => {}) }; },
  template: `<button v-if="p" class="pg" type="button" @click.stop="openPage(p)" :title="t('page') + ' ' + p">p.{{ p }}</button>`,
};

// ---------- generic table (view / edit) ----------
const DataTable = {
  mixins: [mixin], components: { PageRef },
  props: { rows: Array, cols: Array, editing: Boolean, currency: String, emptyText: String },
  emits: ['add', 'remove'],
  computed: {
    shownCols() {
      if (this.editing) return this.cols.filter(c => !c.viewOnly);
      return this.cols.filter(c => !c.editOnly && (!c.optional || (this.rows || []).some(r => r[c.key] !== null && r[c.key] !== undefined && r[c.key] !== '')));
    },
  },
  template: `
  <div>
    <el-table :data="rows" border size="small" :empty-text="emptyText || '—'" style="width:100%">
      <el-table-column v-for="c in shownCols" :key="c.key" :label="c.label" :width="c.width" :min-width="c.min"
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

// ---------- list view ----------
const ListView = {
  mixins: [mixin],
  setup() {
    const rows = ref([]); const stats = ref({ by_status: {}, totals: [], parties: [], years: [], count: 0 });
    const loading = ref(true); const showUpload = ref(false);
    const f = reactive({ q: '', type: '', party: '', year: '', status: '', lifecycle: '' });
    let timer = null, debounce = null;
    async function load() {
      const p = new URLSearchParams(f).toString();
      try {
        const [r, s] = await Promise.all([api('/api/contracts?' + p), api('/api/stats')]);
        rows.value = r; stats.value = s;
      } catch (e) { ElMessage.error(e.message); }
      loading.value = false;
      clearTimeout(timer);
      if (r_working()) timer = setTimeout(load, 3000);
    }
    function r_working() { return rows.value.some(r => WORKING.includes(r.status)); }
    watch(() => [f.type, f.party, f.year, f.status, f.lifecycle], load);
    watch(() => f.q, () => { clearTimeout(debounce); debounce = setTimeout(load, 300); });
    onMounted(load);
    onBeforeUnmount(() => clearTimeout(timer));
    const filtered = computed(() => !!(f.q || f.type || f.party || f.year || f.status || f.lifecycle));
    const working = computed(() => STATUSES.filter(s => WORKING.includes(s)).reduce((a, s) => a + (stats.value.by_status[s] || 0), 0));
    function onUploaded(res) {
      (res.duplicates || []).forEach(d => ElMessage.warning(t('duplicate', { f: d.file })));
      (res.rejected || []).forEach(x => ElMessage.error(t('rejected', { f: x })));
      if ((res.created || []).length) ElMessage.success(t('uploaded_ok', { n: res.created.length }));
      load();
    }
    function onUploadError(err) { ElMessage.error(String(err && err.message || err)); }
    function exportXlsx() { location.href = '/api/export.xlsx?ids=' + rows.value.map(r => r.id).join(','); }
    function titleOf(r) { return (store.lang === 'zh' ? (r.title_zh || r.title) : (r.title || r.title_zh)) || r.file_name; }
    return { rows, stats, f, loading, filtered, working, showUpload, onUploaded, onUploadError, exportXlsx, titleOf, TYPES, STATUSES, LIFECYCLES, WORKING, go };
  },
  template: `
  <div>
    <div class="ph">
      <div><h1>{{ t('contracts') }}</h1><div class="sub">{{ t('contractsSub') }}</div></div>
      <div class="actions">
        <el-button @click="exportXlsx" :disabled="!rows.length"><el-icon><Download /></el-icon>{{ t('export') }}</el-button>
        <el-button type="primary" @click="showUpload = true"><el-icon><Upload /></el-icon>{{ t('upload') }}</el-button>
      </div>
    </div>

    <div class="ledger" v-if="stats.count">
      <div><div class="k">{{ t('total') }}</div><div class="v">{{ stats.count }}</div></div>
      <div><div class="k">{{ t('toReview') }}</div><div class="v amber">{{ stats.by_status.review || 0 }}</div></div>
      <div v-if="working"><div class="k">{{ t('processing') }}</div><div class="v">{{ working }}</div></div>
      <div v-for="x in stats.totals" :key="x.currency"><div class="k">{{ t('sum') }} · {{ x.currency }}</div><div class="v">{{ fmt(x.total) }}</div></div>
    </div>

    <div class="filters">
      <el-input v-model="f.q" :placeholder="t('search')" clearable class="grow"><template #prefix><el-icon><Search /></el-icon></template></el-input>
      <el-select v-model="f.type" :placeholder="t('type')" clearable style="width:140px">
        <el-option v-for="x in TYPES" :key="x" :value="x" :label="t('t_' + x)" /></el-select>
      <el-select v-model="f.party" :placeholder="t('party')" clearable filterable style="width:220px">
        <el-option v-for="x in stats.parties" :key="x" :value="x" :label="x" /></el-select>
      <el-select v-model="f.year" :placeholder="t('year')" clearable style="width:110px">
        <el-option v-for="x in stats.years" :key="x" :value="x" :label="x" /></el-select>
      <el-select v-model="f.status" :placeholder="t('status')" clearable style="width:130px">
        <el-option v-for="x in STATUSES" :key="x" :value="x" :label="t('s_' + x)" /></el-select>
      <el-select v-model="f.lifecycle" :placeholder="t('lifecycle')" clearable style="width:130px">
        <el-option v-for="x in LIFECYCLES" :key="x" :value="x" :label="t('l_' + x)" /></el-select>
    </div>

    <div class="panel" v-loading="loading">
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
        <el-table-column :label="t('totalAmount')" width="170" align="right">
          <template #default="{ row }"><span class="num">{{ fmt(row.total_amount, row.currency) }}</span></template></el-table-column>
        <el-table-column :label="t('signingDate')" width="110">
          <template #default="{ row }">{{ row.signing_date || row.effective_date || '—' }}</template></el-table-column>
        <el-table-column :label="t('status')" width="120">
          <template #default="{ row }"><el-tag :type="statusTag(row.status)" size="small">{{ t('s_' + row.status) }}</el-tag></template></el-table-column>
        <el-table-column :label="t('lifecycle')" width="100">
          <template #default="{ row }"><span class="muted">{{ t('l_' + (row.lifecycle_status || 'active')) }}</span></template></el-table-column>
      </el-table>
      <el-empty v-else-if="!loading && filtered" :description="t('noMatch')" />
      <div v-else-if="!loading" class="empty-state">
        <h2>{{ t('emptyTitle') }}</h2><p>{{ t('emptyText') }}</p>
        <el-button type="primary" size="large" @click="showUpload = true"><el-icon><Upload /></el-icon>{{ t('upload') }}</el-button>
      </div>
    </div>

    <el-dialog v-model="showUpload" :title="t('uploadTitle')" width="560px">
      <el-upload drag multiple action="/api/contracts" name="files" accept=".pdf,.docx,.txt"
        :on-success="onUploaded" :on-error="onUploadError" :show-file-list="true">
        <el-icon class="el-icon--upload"><UploadFilled /></el-icon>
        <div class="el-upload__text">{{ t('uploadHint') }}</div>
        <template #tip><div class="el-upload__tip">{{ t('uploadTip') }}</div></template>
      </el-upload>
    </el-dialog>
  </div>`,
};

// ---------- detail view ----------
const DetailView = {
  mixins: [mixin], components: { PageRef, DataTable },
  props: ['id'],
  setup(props) {
    const c = ref(null); const draft = ref(null); const editing = ref(false); const tab = ref('overview');
    const showPdf = ref(true); const pdfPage = ref(1); const pdfKey = ref(0);
    const pages = ref([]); const srcMode = ref('masked'); const find = ref(''); const saving = ref(false);
    let timer = null;
    const pdfRef = ref(null);
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
    watch(tab, v => { if (v === 'source' && !pages.value.length) loadPages(); });
    onMounted(load);
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
      } catch (e) { ElMessage.error(e.message); }
      saving.value = false;
    }
    async function setMeta(k, v) {
      try { c.value = await api('/api/contracts/' + props.id, { method: 'PUT', json: { [k]: v } }); ElMessage.success(t('saved')); }
      catch (e) { ElMessage.error(e.message); }
    }
    async function confirmReview() {
      if (editing.value) await save();
      c.value = await api('/api/contracts/' + props.id + '/confirm', { method: 'POST' }); ElMessage.success(t('confirmed'));
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
      let s = (text || '').replace(/&/g, '&amp;').replace(/</g, '&lt;');
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
      hl, shownPages, openPage, go, TYPES, LIFECYCLES, WORKING, store };
  },
  template: `
  <div v-if="c">
    <div class="ph detail-head">
      <div class="head-left">
        <el-button link @click="go('list')"><el-icon><ArrowLeft /></el-icon>{{ t('back') }}</el-button>
        <h1>{{ title }}</h1>
        <div class="sub" v-if="subTitle">{{ subTitle }}</div>
        <div class="meta-row">
          <span v-if="d && d.basic && d.basic.contract_no" class="mono-ish">{{ d.basic.contract_no }}</span>
          <el-tag v-if="c.contract_type" type="info" effect="plain" size="small">{{ t('t_' + c.contract_type) }}</el-tag>
          <el-tag :type="statusTag(c.status)" size="small">{{ t('s_' + c.status) }}</el-tag>
          <span v-if="c.page_count">{{ c.page_count }} {{ t('pages') }}<template v-if="c.ocr_pages"> · {{ c.ocr_pages }} {{ t('ocrPages') }}</template></span>
          <span class="muted">{{ c.file_name }}</span>
        </div>
      </div>
      <div class="actions">
        <el-select :model-value="c.lifecycle_status || 'active'" @change="v => setMeta('lifecycle_status', v)" style="width:120px">
          <el-option v-for="x in LIFECYCLES" :key="x" :value="x" :label="t('l_' + x)" />
        </el-select>
        <template v-if="c.data">
          <template v-if="editing">
            <el-button @click="cancelEdit">{{ t('cancel') }}</el-button>
            <el-button type="primary" :loading="saving" @click="save">{{ t('save') }}</el-button>
          </template>
          <el-button v-else @click="startEdit"><el-icon><Edit /></el-icon>{{ t('edit') }}</el-button>
          <el-button v-if="c.status === 'review'" type="success" @click="confirmReview"><el-icon><Check /></el-icon>{{ t('confirm') }}</el-button>
          <el-button v-if="c.status === 'confirmed'" @click="unconfirm">{{ t('unconfirm') }}</el-button>
        </template>
        <el-dropdown trigger="click" @command="onMore">
          <el-button><el-icon><MoreFilled /></el-icon></el-button>
          <template #dropdown><el-dropdown-menu>
            <el-dropdown-item command="extract" :disabled="WORKING.includes(c.status)">{{ t('reextract') }}</el-dropdown-item>
            <el-dropdown-item command="read" :disabled="WORKING.includes(c.status)">{{ t('reread') }}</el-dropdown-item>
            <el-dropdown-item command="download">{{ t('download') }}</el-dropdown-item>
            <el-dropdown-item command="delete" divided><span class="danger">{{ t('del') }}</span></el-dropdown-item>
          </el-dropdown-menu></template>
        </el-dropdown>
      </div>
    </div>

    <el-alert v-if="WORKING.includes(c.status)" type="info" :closable="false" show-icon class="mb"
      :title="t('processingNow') + '：' + t('s_' + c.status) + (c.progress ? ' — ' + c.progress : '')" />
    <el-alert v-if="c.status === 'failed'" type="error" :closable="false" show-icon class="mb" :title="t('failedTitle')" :description="c.error" />
    <el-alert v-if="c.status === 'review' && !editing" type="warning" :closable="false" show-icon class="mb" :title="t('reviewTip')" />

    <div class="detail" :class="{ nopdf: !showPdf }">
      <div class="detail-main">
        <el-tabs v-model="tab">
          <!-- overview -->
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
                  type="textarea" :autosize="{minRows:2}" :placeholder="t('notesPh')" />
              </div>
            </template>
          </el-tab-pane>

          <!-- amounts -->
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
                <DataTable :rows="d.line_items" :cols="cols.items" :editing="editing"
                  @add="addRow('line_items', { currency: cur })" @remove="i => removeRow('line_items', i)" />
              </div>
            </template>
          </el-tab-pane>

          <!-- payments -->
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

          <!-- terms -->
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

          <!-- source -->
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
        </el-tabs>
      </div>

      <div class="pdf" v-show="showPdf" ref="pdfRef">
        <div class="pdf-bar"><span>{{ t('pdfTitle') }}</span>
          <el-button link size="small" @click="showPdf = false">{{ t('hidePdf') }}</el-button></div>
        <iframe v-if="isPdf" :key="pdfKey" :src="'/api/contracts/' + c.id + '/file#page=' + pdfPage + '&view=FitH'" title="PDF"></iframe>
        <el-empty v-else :description="t('noPreview')" />
      </div>
    </div>
    <el-button v-if="!showPdf" class="pdf-fab" type="primary" @click="showPdf = true"><el-icon><Document /></el-icon>{{ t('showPdf') }}</el-button>
  </div>`,
};

// ---------- line items view ----------
const ItemsView = {
  mixins: [mixin], components: { PageRef },
  setup() {
    const rows = ref([]); const parties = ref([]); const loading = ref(true);
    const f = reactive({ q: '', type: '', party: '', confirmed_only: 0 });
    let debounce = null;
    async function load() {
      loading.value = true;
      try { rows.value = await api('/api/items?' + new URLSearchParams(f).toString()); } catch (e) { ElMessage.error(e.message); }
      loading.value = false;
    }
    watch(() => [f.type, f.party, f.confirmed_only], load);
    watch(() => f.q, () => { clearTimeout(debounce); debounce = setTimeout(load, 300); });
    onMounted(async () => { load(); parties.value = (await api('/api/stats')).parties; });
    const totals = computed(() => {
      const m = {};
      rows.value.forEach(r => { if (r.amount != null) { const k = r.currency || '?'; m[k] = (m[k] || 0) + r.amount; } });
      return Object.entries(m);
    });
    function summary() { return ['', t('sumBy'), '', '', totals.value.map(([k, v]) => fmt(v, k)).join(' / '), '']; }
    function exportXlsx() {
      const ids = [...new Set(rows.value.map(r => r.contract_id))];
      location.href = '/api/export.xlsx?ids=' + ids.join(',');
    }
    function cTitle(r) { return (store.lang === 'zh' ? (r.title_zh || r.title) : (r.title || r.title_zh)) || ''; }
    return { rows, f, parties, loading, totals, summary, exportXlsx, cTitle, TYPES, go, store };
  },
  template: `
  <div>
    <div class="ph">
      <div><h1>{{ t('itemsTitle') }}</h1><div class="sub">{{ t('itemsSub') }}</div></div>
      <div class="actions"><el-button @click="exportXlsx" :disabled="!rows.length"><el-icon><Download /></el-icon>{{ t('export') }}</el-button></div>
    </div>
    <div class="filters">
      <el-input v-model="f.q" :placeholder="t('itemsSearch')" clearable class="grow"><template #prefix><el-icon><Search /></el-icon></template></el-input>
      <el-select v-model="f.type" :placeholder="t('type')" clearable style="width:140px"><el-option v-for="x in TYPES" :key="x" :value="x" :label="t('t_' + x)" /></el-select>
      <el-select v-model="f.party" :placeholder="t('party')" clearable filterable style="width:220px"><el-option v-for="x in parties" :key="x" :value="x" :label="x" /></el-select>
      <el-checkbox v-model="f.confirmed_only" :true-value="1" :false-value="0" border>{{ t('onlyConfirmed') }}</el-checkbox>
    </div>
    <div class="ledger" v-if="totals.length">
      <div><div class="k">{{ t('lineItems') }}</div><div class="v">{{ rows.length }}</div></div>
      <div v-for="[k, v] in totals" :key="k"><div class="k">{{ t('sumBy') }} · {{ k }}</div><div class="v">{{ fmt(v) }}</div></div>
    </div>
    <div class="panel" v-loading="loading">
      <el-table :data="rows" style="width:100%" :empty-text="t('noMatch')">
        <el-table-column :label="t('contract')" min-width="220">
          <template #default="{ row }"><a href="javascript:void 0" @click="go('detail', row.contract_id)">{{ row.contract_no || cTitle(row) }}</a>
            <div class="muted small">{{ row.party_b }}</div></template></el-table-column>
        <el-table-column :label="t('category')" prop="category" min-width="160" show-overflow-tooltip />
        <el-table-column :label="t('item')" min-width="220">
          <template #default="{ row }"><div>{{ row.name }}</div><div class="muted small" v-if="row.name_zh">{{ row.name_zh }}</div></template></el-table-column>
        <el-table-column :label="t('desc')" prop="description" min-width="280" show-overflow-tooltip />
        <el-table-column :label="t('amount')" width="170" align="right">
          <template #default="{ row }"><span class="num">{{ fmt(row.amount, row.currency) }}</span></template></el-table-column>
        <el-table-column :label="t('page')" width="70">
          <template #default="{ row }"><a href="javascript:void 0" class="pg" @click="go('detail', row.contract_id)" v-if="row.page">p.{{ row.page }}</a></template></el-table-column>
      </el-table>
    </div>
  </div>`,
};

// ---------- settings view ----------
const SettingsView = {
  mixins: [mixin],
  setup() {
    const cfg = reactive({ provider: '', base_url: '', model: '', api_key: '', max_tokens: 8192 });
    const providers = ref({}); const keySet = ref(false); const keyMask = ref(''); const testing = ref(false); const saving = ref(false);
    async function load() {
      const r = await api('/api/settings');
      providers.value = r.providers;
      Object.assign(cfg, { provider: r.config.provider, base_url: r.config.base_url, model: r.config.model, max_tokens: r.config.max_tokens, api_key: '' });
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
    return { cfg, providers, keySet, keyMask, testing, saving, onProvider, save, test };
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
    <div class="card"><h2>{{ t('privacyTitle') }}</h2><p class="note">{{ t('privacyText') }}</p></div>
    <div class="card"><h2>{{ t('backupTitle') }}</h2><p class="note">{{ t('backupText') }}</p>
      <el-button tag="a" href="/api/backup"><el-icon><Download /></el-icon>{{ t('backupBtn') }}</el-button></div>
  </div>`,
};

// ---------- root ----------
const Root = {
  mixins: [mixin],
  components: { ListView, DetailView, ItemsView, SettingsView },
  setup() {
    const epLocale = computed(() => store.lang === 'zh' ? ElementPlusLocaleZhCn : ElementPlusLocaleEn);
    function sync() { store.route = parseHash(); window.scrollTo(0, 0); }
    window.addEventListener('hashchange', sync); sync();
    function toggleLang() {
      store.lang = store.lang === 'zh' ? 'en' : 'zh';
      try { localStorage.setItem('cms_lang', store.lang); } catch (e) {}
      document.documentElement.lang = store.lang === 'zh' ? 'zh-CN' : 'en';
    }
    const active = computed(() => store.route.name === 'detail' ? 'list' : store.route.name);
    return { store, epLocale, toggleLang, active, go };
  },
  template: `
  <el-config-provider :locale="epLocale">
    <div class="shell">
      <aside class="nav">
        <div class="brand"><div class="brand-mark" aria-hidden="true"></div>
          <div><div class="brand-name">{{ t('appName') }}</div><div class="brand-sub">SIPG Bayport</div></div></div>
        <el-menu :default-active="active" @select="k => go(k)" background-color="transparent" text-color="#C9D3DD" active-text-color="#ffffff" class="nav-menu">
          <el-menu-item index="list"><el-icon><Tickets /></el-icon><span>{{ t('navContracts') }}</span></el-menu-item>
          <el-menu-item index="items"><el-icon><List /></el-icon><span>{{ t('navItems') }}</span></el-menu-item>
          <el-menu-item index="settings"><el-icon><Setting /></el-icon><span>{{ t('navSettings') }}</span></el-menu-item>
        </el-menu>
        <div class="nav-foot"><button class="lang" type="button" @click="toggleLang">{{ t('lang') }}</button></div>
      </aside>
      <main class="main">
        <ListView v-if="store.route.name === 'list'" :key="'l' + store.lang" />
        <DetailView v-else-if="store.route.name === 'detail'" :id="store.route.id" :key="'d' + store.route.id" />
        <ItemsView v-else-if="store.route.name === 'items'" />
        <SettingsView v-else-if="store.route.name === 'settings'" />
      </main>
    </div>
  </el-config-provider>`,
};

const app = createApp(Root);
app.use(ElementPlus);
for (const [name, comp] of Object.entries(ElementPlusIconsVue)) app.component(name, comp);
app.mount('#app');
