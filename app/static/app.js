const form = document.querySelector('#upload-form');
const statusBox = document.querySelector('#status');
const resultPanel = document.querySelector('#result');
const fileInput = document.querySelector('#file');
const dropzone = document.querySelector('#dropzone');
const processButton = document.querySelector('#process-button');
const profileSelect = document.querySelector('#profile');
const schemaDetails = document.querySelector('#custom-schema');
let activeResult = null;
let editableExtraction = null;
let editableTables = [];

const presets = {
  invoice: {
    document_type: 'invoice_receipt',
    fields: {
      merchant_name: { type: 'string', required: true },
      merchant_address: { type: 'string', required: false },
      phone: { type: 'string', required: false },
      receipt_number: { type: 'string', required: false },
      date: { type: 'string', required: false },
      time: { type: 'string', required: false },
      cashier: { type: 'string', required: false },
      table_number: { type: 'string', required: false },
      party_size: { type: 'string', required: false },
      currency: { type: 'string', required: true },
      currency_symbol: { type: 'string', required: false },
      currency_as_printed: { type: 'string', required: false },
      subtotal: { type: 'number', required: false },
      discount: { type: 'number', required: false },
      total: { type: 'number', required: true },
      payment_method: { type: 'string', required: false },
      amount_paid: { type: 'number', required: false },
      change: { type: 'number', required: false },
      reference_number: { type: 'string', required: false },
      promotional_text: { type: 'string', required: false },
      other_text: { type: 'string', required: false },
      line_items: { type: 'table', required: true, columns: ['description', 'quantity', 'unit_price', 'amount'] },
      taxes: { type: 'table', required: false, columns: ['tax_name', 'rate', 'amount'] },
    },
  },
  marksheet: {
    document_type: 'marksheet',
    fields: {
      student_name: { type: 'string', required: false },
      roll_number: { type: 'string', required: false },
      registration_number: { type: 'string', required: false },
      date_of_birth: { type: 'string', required: false },
      father_name: { type: 'string', required: false },
      mother_name: { type: 'string', required: false },
      school_name: { type: 'string', required: false },
      board: { type: 'string', required: false },
      examination: { type: 'string', required: false },
      exam_year: { type: 'string', required: false },
      result: { type: 'string', required: false },
      date_of_issue: { type: 'string', required: false },
      subjects_and_marks: { type: 'table', columns: ['subject_code', 'subject', 'theory_marks', 'practical_marks', 'total_marks', 'total_in_words', 'grade'] },
    },
  },
};

function requestHeaders(extra = {}) {
  const key = document.querySelector('#api-key').value.trim();
  return { ...(key ? { 'X-API-Key': key } : {}), ...extra };
}

async function request(url, options = {}) {
  let response;
  try {
    response = await fetch(url, { ...options, headers: requestHeaders(options.headers || {}) });
  } catch (error) {
    if (error instanceof TypeError) {
      throw new Error(`Can't reach the local document service at ${window.location.origin}. Check that the server is running, then refresh this page.`);
    }
    throw error;
  }
  const type = response.headers.get('content-type') || '';
  const payload = type.includes('json') ? await response.json() : await response.text();
  if (!response.ok) throw new Error(payload?.detail || payload || 'Request failed');
  return payload;
}

async function checkServiceHealth() {
  const pill = document.querySelector('#service-status');
  const label = document.querySelector('#service-status-text');
  try {
    const response = await fetch('/health', { cache: 'no-store' });
    if (!response.ok) throw new Error(`HTTP ${response.status}`);
    const health = await response.json();
    pill.classList.remove('offline');
    const modelStatus = health.ollama;
    if (modelStatus?.reachable && modelStatus.text_ready && modelStatus.vision_ready) {
      label.textContent = 'Service and local models ready';
    } else if (!modelStatus?.reachable) {
      label.textContent = 'Service online · Ollama offline';
    } else {
      const missing = [!modelStatus.text_ready && 'text', !modelStatus.vision_ready && 'vision'].filter(Boolean).join(' and ');
      label.textContent = `Service online · missing ${missing} model`;
    }
    pill.title = `Upload limit: ${health.max_upload_mb} MB${health.api_key_enabled ? ' · API key required' : ''}`;
    if (modelStatus) pill.title += ` · Ollama ${modelStatus.reachable ? 'online' : 'offline'} · text: ${modelStatus.text_ready ? 'ready' : 'missing'} · vision: ${modelStatus.vision_ready ? 'ready' : 'missing'}`;
    const subtitle = document.querySelector('#file-subtitle');
    if (!fileInput.files?.length) subtitle.textContent = `PDF, images, DOCX, XLSX, PPTX, CSV, TXT and more · up to ${health.max_upload_mb} MB`;
  } catch {
    pill.classList.add('offline');
    label.textContent = 'Local service offline';
    pill.title = 'The website cannot reach its API. Start the server and refresh this page.';
  }
}

function setStatus(message, kind = '') {
  statusBox.textContent = message;
  statusBox.className = `status-line${kind ? ` ${kind}` : ''}`;
}

function fieldValue(field) {
  const value = field && typeof field === 'object' && !Array.isArray(field) && Object.hasOwn(field, 'value') ? field.value : field;
  if (value === null || value === undefined || value === '') return '—';
  return typeof value === 'object' ? JSON.stringify(value) : String(value);
}

function addText(parent, tag, text, className = '') {
  const node = document.createElement(tag);
  node.textContent = text;
  if (className) node.className = className;
  parent.append(node);
  return node;
}

function renderFields(fields, validation) {
  const grid = document.querySelector('#result-fields');
  grid.replaceChildren();
  for (const [name, field] of Object.entries(fields || {})) {
    if (field && typeof field === 'object' && Array.isArray(field.rows)) continue;
    const card = document.createElement('article');
    const fieldValidation = validation?.grounded_fields?.[name];
    const reviewStatus = fieldValidation?.review_status || (fieldValidation?.grounded === false ? 'needs_review' : 'grounded');
    card.className = `field-card${reviewStatus !== 'grounded' ? ' ungrounded' : ''}`;
    addText(card, 'div', name.replaceAll('_', ' '), 'field-name');
    addText(card, 'span', reviewStatus.replaceAll('_', ' '), `field-status ${reviewStatus}`);
    addText(card, 'div', fieldValue(field), 'field-value');
    const evidence = field && typeof field === 'object' ? field.evidence || field.quote : '';
    if (evidence) addText(card, 'div', `Evidence: ${evidence}`, 'field-evidence');
    if (fieldValidation?.review_reason) addText(card, 'div', fieldValidation.review_reason, 'field-evidence missing-evidence');
    grid.append(card);
  }
  if (!grid.children.length) addText(grid, 'p', 'No scalar fields were returned. Open the JSON below for the full response.', 'muted');
}

function renderTables(tables, validation) {
  const host = document.querySelector('#result-tables');
  host.replaceChildren();
  for (const [index, table] of (tables || []).entries()) {
    if (!table || typeof table !== 'object') continue;
    const headers = Array.isArray(table.headers) ? table.headers : [];
    const rows = Array.isArray(table.rows) ? table.rows : [];
    const tableValidation = validation?.grounded_tables?.[index];
    const wrap = document.createElement('section');
    wrap.className = 'table-wrap';
    addText(wrap, 'h3', table.name || `Table ${index + 1}`, 'table-title');
    const tableNode = document.createElement('table');
    tableNode.className = 'result-table';
    const head = document.createElement('thead');
    const headRow = document.createElement('tr');
    for (const header of headers) addText(headRow, 'th', String(header));
    addText(headRow, 'th', 'Review status');
    head.append(headRow);
    tableNode.append(head);
    const body = document.createElement('tbody');
    for (const [rowIndex, row] of rows.entries()) {
      const tr = document.createElement('tr');
      const rowValidation = tableValidation?.rows?.[rowIndex];
      const rowStatus = rowValidation?.review_status || (rowValidation?.grounded === false ? 'needs_review' : 'grounded');
      tr.className = rowStatus === 'grounded' ? 'grounded-row' : 'ungrounded';
      tr.title = [rowValidation?.review_reason, rowValidation?.evidence].filter(Boolean).join(' · ');
      for (let i = 0; i < Math.max(headers.length, Array.isArray(row) ? row.length : 0); i++) {
        addText(tr, 'td', fieldValue(Array.isArray(row) ? row[i] : ''));
      }
      const statusCell = addText(tr, 'td', rowStatus.replaceAll('_', ' '), `field-status ${rowStatus}`);
      if (rowValidation?.review_reason) statusCell.title = rowValidation.review_reason;
      body.append(tr);
    }
    if (!rows.length) {
      const tr = document.createElement('tr');
      const empty = document.createElement('td');
      empty.colSpan = Math.max(1, headers.length + 1);
      empty.textContent = 'No rows were extracted. Review this table and add any missing rows.';
      empty.className = 'empty-table-message';
      tr.append(empty);
      body.append(tr);
    }
    tableNode.append(body);
    wrap.append(tableNode);
    host.append(wrap);
  }
}

function renderCorrectionEditor() {
  const fieldsHost = document.querySelector('#scalar-editor');
  fieldsHost.replaceChildren();
  for (const [name, field] of Object.entries(editableExtraction.fields || {})) {
    if (field && typeof field === 'object' && Array.isArray(field.rows)) continue;
    const card = document.createElement('label');
    card.className = 'edit-field';
    addText(card, 'span', name.replaceAll('_', ' '), 'field-name');
    const input = document.createElement('input');
    input.type = 'text';
    input.value = fieldValue(field === null ? '' : field).replace(/^—$/, '');
    input.dataset.fieldName = name;
    card.append(input);
    const evidence = field && typeof field === 'object' ? field.evidence || field.quote : '';
    if (evidence) addText(card, 'small', `OCR source: ${evidence}`, 'edit-evidence');
    else addText(card, 'small', 'No OCR quote found. Check the document before entering a value.', 'edit-evidence missing-evidence');
    fieldsHost.append(card);
  }

  const tablesHost = document.querySelector('#table-editor');
  tablesHost.replaceChildren();
  editableTables.forEach((table, tableIndex) => {
    const section = document.createElement('section');
    section.className = 'edit-table';
    const heading = document.createElement('div');
    heading.className = 'edit-table-heading';
    addText(heading, 'h4', table.name || `Table ${tableIndex + 1}`);
    const addRow = document.createElement('button');
    addRow.type = 'button';
    addRow.className = 'secondary-button';
    addRow.textContent = 'Add row';
    addRow.addEventListener('click', () => {
      table.rows.push((table.headers || []).map(() => ''));
      renderCorrectionEditor();
    });
    heading.append(addRow);
    section.append(heading);
    const columns = document.createElement('div');
    columns.className = 'edit-row edit-header-row';
    columns.style.setProperty('--edit-columns', String(Math.max(1, (table.headers || []).length)));
    for (const header of table.headers || []) addText(columns, 'span', String(header));
    addText(columns, 'span', '');
    section.append(columns);
    (table.rows || []).forEach((row, rowIndex) => {
      const rowNode = document.createElement('div');
      rowNode.className = 'edit-row';
      rowNode.style.setProperty('--edit-columns', String(Math.max(1, (table.headers || []).length)));
      for (let columnIndex = 0; columnIndex < (table.headers || []).length; columnIndex++) {
        const input = document.createElement('input');
        input.type = 'text';
        input.value = row[columnIndex] ?? '';
        input.setAttribute('aria-label', `${table.headers[columnIndex]} row ${rowIndex + 1}`);
        input.addEventListener('input', () => { table.rows[rowIndex][columnIndex] = input.value; });
        rowNode.append(input);
      }
      const remove = document.createElement('button');
      remove.type = 'button';
      remove.className = 'remove-row';
      remove.textContent = 'Remove';
      remove.addEventListener('click', () => {
        table.rows.splice(rowIndex, 1);
        renderCorrectionEditor();
      });
      rowNode.append(remove);
      section.append(rowNode);
      const evidence = Array.isArray(table.row_evidence) ? table.row_evidence[rowIndex] : '';
      addText(section, 'p', evidence ? `OCR row source: ${evidence}` : 'No OCR row quote was captured. Compare this row with the OCR text above.', 'edit-row-evidence');
    });
    tablesHost.append(section);
  });
}

function openCorrectionEditor() {
  if (!activeResult?.jobId) return;
  editableExtraction = JSON.parse(JSON.stringify(activeResult.data.extraction || { fields: {}, tables: [] }));
  editableExtraction.fields ||= {};
  editableTables = Array.isArray(editableExtraction.tables) ? editableExtraction.tables : [];
  const schemaFields = activeResult.data.requested_schema?.fields || {};
  for (const [name, spec] of Object.entries(schemaFields)) {
    if (spec?.type === 'table') {
      if (!editableTables.some(table => table.name === name)) {
        editableTables.push({ name, headers: spec.columns || [], rows: [], row_evidence: [], page: 0 });
      }
      continue;
    }
    if (!Object.hasOwn(editableExtraction.fields, name)) {
      editableExtraction.fields[name] = { value: '', evidence: '', page: null, bbox: [] };
    }
  }
  renderCorrectionEditor();
  document.querySelector('#review-comment').value = '';
  document.querySelector('#correction-editor').hidden = false;
  document.querySelector('#correction-editor').scrollIntoView({ behavior: 'smooth', block: 'start' });
}

async function saveCorrections() {
  if (!activeResult?.jobId || !editableExtraction) return;
  const saveButton = document.querySelector('#save-corrections');
  saveButton.disabled = true;
  try {
    const schemaFields = activeResult.data.requested_schema?.fields || {};
    for (const input of document.querySelectorAll('[data-field-name]')) {
      const name = input.dataset.fieldName;
      const field = editableExtraction.fields[name];
      const spec = schemaFields[name];
      if (!input.value.trim() && spec && typeof spec === 'object' && spec.required === false) {
        delete editableExtraction.fields[name];
        continue;
      }
      if (field && typeof field === 'object' && !Array.isArray(field) && Object.hasOwn(field, 'value')) field.value = input.value;
      else editableExtraction.fields[name] = input.value;
    }
    editableExtraction.tables = editableTables;
    for (const table of editableTables) {
      table.row_evidence = (table.rows || []).map(row => row.filter(cell => String(cell ?? '').trim()).join(' | '));
    }
    const jobId = activeResult.jobId;
    const sourceFilename = activeResult.sourceFilename;
    const response = await request(`/reviews/${jobId}`, {
      method: 'POST',
      headers: { 'Content-Type': 'application/json' },
      body: JSON.stringify({
        decision: 'corrected',
        comment: document.querySelector('#review-comment').value.trim(),
        corrected_extraction: editableExtraction,
      }),
    });
    renderResult(response.result, sourceFilename, jobId);
    setStatus('Corrections saved to this result and recorded for review.');
    await loadSavedResults();
  } catch (error) {
    setStatus(error.message, 'error');
  } finally {
    saveButton.disabled = false;
  }
}

function renderResult(data, sourceFilename = '', jobId = null) {
  const validation = data?.validation || {};
  const extraction = data?.extraction || {};
  activeResult = { data, sourceFilename: sourceFilename || data.filename || '', jobId: jobId || data.job_id || null };
  resultPanel.hidden = false;
  document.querySelector('#result-title').textContent = extraction.document_type || sourceFilename || data.filename || 'Document result';
  const meta = [sourceFilename || data.filename, data.model_mode || data.mode, data.model].filter(Boolean);
  document.querySelector('#result-subtitle').textContent = meta.join(' · ');
  const badge = document.querySelector('#result-status');
  const needsReview = Boolean(validation.review_required);
  const decision = data.human_review?.submission?.decision;
  badge.textContent = decision === 'corrected' ? 'Corrected by reviewer' : decision === 'approved' ? 'Approved by reviewer' : needsReview ? 'Review recommended' : 'Validation passed';
  badge.className = `result-badge${needsReview && decision !== 'corrected' && decision !== 'approved' ? ' review' : ''}`;
  document.querySelector('#edit-result').hidden = !activeResult.jobId;
  document.querySelector('#correction-editor').hidden = true;
  renderFields(extraction.fields, validation);
  renderTables(extraction.tables, validation);

  const segments = Array.isArray(data.segments) ? data.segments : [];
  const ocrReadout = document.querySelector('#ocr-readout');
  const ocrText = segments.map(segment => `Page ${segment.index || '?'}${segment.label ? ` · ${segment.label}` : ''}\n${segment.text || ''}`).join('\n\n');
  ocrReadout.hidden = !ocrText.trim();
  document.querySelector('#ocr-output').textContent = ocrText || 'No OCR text was captured.';

  const reasons = Array.isArray(validation.review_reasons) ? validation.review_reasons : [];
  const reasonPanel = document.querySelector('#review-reasons');
  const reasonList = document.querySelector('#review-list');
  reasonList.replaceChildren();
  reasonPanel.hidden = reasons.length === 0;
  for (const reason of reasons) addText(reasonList, 'li', String(reason));

  const raw = document.querySelector('#output');
  raw.textContent = JSON.stringify(data, null, 2);
  const saved = data.saved_result;
  const savedBox = document.querySelector('#saved-location');
  savedBox.replaceChildren();
  const pathText = document.createElement('code');
  pathText.textContent = saved?.path ? `Saved to ${saved.path}` : 'Saved result path is not available';
  savedBox.append(pathText);
  if (saved?.filename) {
    const download = document.createElement('button');
    download.type = 'button';
    download.className = 'download-button';
    download.textContent = 'Download JSON';
    download.addEventListener('click', () => downloadResult(saved.filename));
    savedBox.append(download);
  }
  resultPanel.scrollIntoView({ behavior: 'smooth', block: 'start' });
}

async function showJob(id) {
  for (let attempt = 0; attempt < 900; attempt++) {
    const job = await request(`/jobs/${id}`);
    setStatus(`${job.status} · ${job.filename}`);
    if (job.status === 'completed') {
      const data = await request(`/results/${id}`);
      renderResult(data, job.filename, id);
      setStatus(data.validation?.review_required ? 'Finished. Check the review warnings before using these values.' : 'Finished. Result saved locally.');
      await loadSavedResults();
      return;
    }
    if (job.status === 'failed') throw new Error(job.error || 'Processing failed');
    await new Promise(resolve => setTimeout(resolve, 1000));
  }
  throw new Error('This document is still processing. Refresh Recent saved results in a moment.');
}

async function downloadResult(filename) {
  try {
    const response = await fetch(`/saved-results/${encodeURIComponent(filename)}`, { headers: requestHeaders() });
    if (!response.ok) {
      const error = await response.json().catch(() => ({}));
      throw new Error(error.detail || 'Could not download saved result');
    }
    const blob = await response.blob();
    const url = URL.createObjectURL(blob);
    const link = document.createElement('a');
    link.href = url;
    link.download = filename;
    link.click();
    URL.revokeObjectURL(url);
  } catch (error) {
    setStatus(error.message, 'error');
  }
}

async function openSavedResult(filename) {
  try {
    const record = await request(`/saved-results/${encodeURIComponent(filename)}`);
    const result = record?.result && typeof record.result === 'object' ? record.result : record;
    renderResult(result, record?.source_filename || result?.filename, record?.job_id || null);
    setStatus('Opened saved result.');
  } catch (error) {
    setStatus(error.message, 'error');
  }
}

async function loadSavedResults() {
  const host = document.querySelector('#saved-results');
  host.replaceChildren();
  try {
    const data = await request('/saved-results');
    if (!data.items?.length) {
      addText(host, 'p', 'No saved results yet. Upload a document to create your first result.', 'history-empty');
      return;
    }
    for (const item of data.items) {
      const row = document.createElement('article');
      row.className = 'history-item';
      const info = document.createElement('div');
      info.className = 'history-name';
      addText(info, 'strong', item.source_filename || item.file);
      const timestamp = item.saved_at ? new Date(item.saved_at).toLocaleString() : 'Saved locally';
      addText(info, 'span', `${timestamp} · ${item.mode || 'auto'} · ${(item.size_bytes / 1024).toFixed(1)} KB`);
      const view = document.createElement('button');
      view.type = 'button';
      view.className = 'history-action';
      view.textContent = 'View';
      view.addEventListener('click', () => openSavedResult(item.file));
      const download = document.createElement('button');
      download.type = 'button';
      download.className = 'history-action';
      download.textContent = 'Download JSON';
      download.addEventListener('click', () => downloadResult(item.file));
      row.append(info, view, download);
      host.append(row);
    }
  } catch (error) {
    addText(host, 'p', `Could not load saved results: ${error.message}`, 'history-empty');
  }
}

profileSelect.addEventListener('change', () => {
  schemaDetails.hidden = profileSelect.value !== 'custom';
});

fileInput.addEventListener('change', () => {
  const file = fileInput.files?.[0];
  document.querySelector('#file-title').textContent = file?.name || 'Drop a file here or browse';
  document.querySelector('#file-subtitle').textContent = file ? `${(file.size / 1024 / 1024).toFixed(2)} MB · Ready to process` : 'PDF, images, DOCX, XLSX, PPTX, CSV, TXT and more · up to 30 MB';
});

for (const eventName of ['dragenter', 'dragover']) {
  dropzone.addEventListener(eventName, event => { event.preventDefault(); dropzone.classList.add('dragover'); });
}
for (const eventName of ['dragleave', 'drop']) {
  dropzone.addEventListener(eventName, event => { event.preventDefault(); dropzone.classList.remove('dragover'); });
}
dropzone.addEventListener('drop', event => {
  const file = event.dataTransfer?.files?.[0];
  if (!file) return;
  const transfer = new DataTransfer();
  transfer.items.add(file);
  fileInput.files = transfer.files;
  fileInput.dispatchEvent(new Event('change'));
});

form.addEventListener('submit', async event => {
  event.preventDefault();
  const file = fileInput.files?.[0];
  if (!file) return;
  let schema = presets[profileSelect.value] || null;
  if (profileSelect.value === 'custom') {
    try {
      schema = JSON.parse(document.querySelector('#schema-json').value);
      if (!schema || typeof schema !== 'object' || Array.isArray(schema)) throw new Error('Schema must be a JSON object.');
    } catch (error) {
      setStatus(`Custom schema error: ${error.message}`, 'error');
      return;
    }
  }
  processButton.disabled = true;
  processButton.querySelector('span').textContent = 'Processing…';
  setStatus('Uploading document…');
  resultPanel.hidden = true;
  const body = new FormData();
  body.append('file', file);
  if (schema) body.append('schema_json', JSON.stringify(schema));
  try {
    const mode = document.querySelector('#mode').value;
    const job = await request(`/documents?mode=${encodeURIComponent(mode)}`, { method: 'POST', body });
    await showJob(job.id);
  } catch (error) {
    setStatus(error.message, 'error');
  } finally {
    processButton.disabled = false;
    processButton.querySelector('span').textContent = 'Run extraction';
  }
});

document.querySelector('#load-results').addEventListener('click', loadSavedResults);
document.querySelector('#edit-result').addEventListener('click', openCorrectionEditor);
document.querySelector('#cancel-edit').addEventListener('click', () => { document.querySelector('#correction-editor').hidden = true; });
document.querySelector('#save-corrections').addEventListener('click', saveCorrections);
loadSavedResults();
checkServiceHealth();
window.setInterval(checkServiceHealth, 15000);
