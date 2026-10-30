const els = {
  start: document.querySelector('#start-demo'), restart: document.querySelector('#restart-demo'),
  workspace: document.querySelector('#workspace'), check: document.querySelector('#check-file'),
  held: document.querySelector('#held-decision'), complete: document.querySelector('#complete-decision'),
  reason: document.querySelector('#approval-reason'), approve: document.querySelector('#approve-file'),
  late: document.querySelector('#try-late'), proof: document.querySelector('#proof-result'),
  roster: document.querySelector('#roster-list'), activity: document.querySelector('#activity-list'),
  activeCount: document.querySelector('#active-count'), missingCount: document.querySelector('#missing-count'),
  missingHelp: document.querySelector('#missing-help'), syncStatus: document.querySelector('#sync-status'),
  syncHelp: document.querySelector('#sync-help'), completeCopy: document.querySelector('#complete-copy'),
  toast: document.querySelector('#toast')
};

let session = null;
let currentRun = null;
let missingIds = new Set();
let applied = false;
let toastTimer = null;

async function request(path, { method = 'GET', token, body, contentType = 'application/json' } = {}) {
  const headers = {};
  if (token) headers.Authorization = `Bearer ${token}`;
  if (body !== undefined) headers['Content-Type'] = contentType;
  const response = await fetch(path, { method, headers, body: body === undefined ? undefined : (typeof body === 'string' ? body : JSON.stringify(body)), cache: 'no-store' });
  const data = await response.json().catch(() => ({}));
  if (!response.ok) throw new Error(data.error || data.detail || `Request failed (${response.status})`);
  return data;
}

function toast(message) {
  els.toast.textContent = message;
  els.toast.hidden = false;
  clearTimeout(toastTimer);
  toastTimer = setTimeout(() => { els.toast.hidden = true; }, 5500);
}

function activity(message) {
  const item = document.createElement('li');
  const text = document.createElement('span');
  const time = document.createElement('time');
  text.textContent = message;
  time.textContent = new Date().toLocaleTimeString([], { hour: '2-digit', minute: '2-digit' });
  item.append(text, time);
  els.activity.prepend(item);
}

function csvFor(records) {
  const fields = ['employee_id', 'name', 'email', 'phone', 'manager', 'office_code', 'shift', 'source_version'];
  const cell = value => `"${String(value ?? '').replaceAll('"', '""')}"`;
  return [fields.join(','), ...records.map(record => fields.map(field => cell(record[field])).join(','))].join('\r\n') + '\r\n';
}

async function renderRoster() {
  const people = await request('/v1/employees', { token: session.operator_token });
  els.roster.replaceChildren();
  const active = people.filter(person => person.active).length;
  els.activeCount.textContent = String(active);
  for (const person of people) {
    const row = document.createElement('div'); row.className = 'roster-row';
    const avatar = document.createElement('span'); avatar.className = 'avatar'; avatar.textContent = person.name.slice(0, 1).toUpperCase();
    const details = document.createElement('span'); details.className = 'roster-person';
    const name = document.createElement('strong'); name.textContent = person.name;
    const info = document.createElement('small'); info.textContent = `${person.employee_id} · ${person.shift} shift`;
    details.append(name, info);
    const chip = document.createElement('span'); chip.className = 'status-chip';
    if (!person.active) { chip.classList.add('inactive'); chip.textContent = 'Inactive'; }
    else if (missingIds.has(person.employee_id)) { chip.classList.add('missing'); chip.textContent = 'Missing from file'; }
    else { chip.textContent = 'Active'; }
    row.append(avatar, details, chip);
    els.roster.append(row);
  }
}

async function startDemo() {
  els.start.disabled = true;
  els.restart.disabled = true;
  try {
    session = await request('/demo/session', { method: 'POST' });
    currentRun = null; missingIds = new Set(); applied = false;
    els.workspace.hidden = false; els.held.hidden = true; els.complete.hidden = true; els.proof.hidden = true;
    els.reason.value = ''; els.approve.disabled = true; els.check.disabled = false; els.late.disabled = true;
    els.missingCount.textContent = '—'; els.missingHelp.textContent = 'Check the sample export';
    els.syncStatus.textContent = 'Not checked'; els.syncHelp.textContent = 'No changes have been made';
    els.activity.replaceChildren();
    activity('A fresh sample roster was opened. All ten employees are active.');
    await renderRoster();
    els.workspace.scrollIntoView({ behavior: 'smooth', block: 'start' });
  } catch (error) {
    toast(error.message === 'NOT_FOUND' ? 'Start the local demo with docker compose up --build.' : `Could not open the sample workspace: ${error.message}`);
  } finally {
    els.start.disabled = false; els.restart.disabled = false;
  }
}

async function checkFile() {
  els.check.disabled = true;
  try {
    const rows = session.records.slice(0, 6);
    currentRun = await request('/v1/snapshots?version=10', {
      method: 'POST', token: session.hr_token, contentType: 'text/csv', body: csvFor(rows)
    });
    missingIds = new Set(session.records.slice(6).map(record => record.employee_id));
    els.missingCount.textContent = String(currentRun.missing);
    els.missingHelp.textContent = 'Of 10 active employees';
    els.syncStatus.textContent = currentRun.status === 'HELD' ? 'On hold' : 'Ready';
    els.syncHelp.textContent = currentRun.status === 'HELD' ? 'A person needs to review this' : 'The file can be applied';
    els.held.hidden = currentRun.status !== 'HELD';
    activity('The export was checked: four employees are missing, so the sync was put on hold.');
    await renderRoster();
    els.held.scrollIntoView({ behavior: 'smooth', block: 'nearest' });
  } catch (error) {
    els.check.disabled = false;
    toast(`Could not check the file: ${error.message}`);
  }
}

async function approveFile() {
  if (!currentRun || els.reason.value.trim().length < 8) return;
  els.approve.disabled = true;
  try {
    const result = await request(`/v1/runs/${currentRun.run_id}/apply`, {
      method: 'POST', token: session.operator_token,
      body: { approve_missing: true, reason: els.reason.value.trim() }
    });
    applied = true;
    els.held.hidden = true; els.complete.hidden = false;
    els.completeCopy.textContent = `${result.applied} records changed. The approval reason and operator ID were recorded.`;
    els.syncStatus.textContent = 'Applied'; els.syncHelp.textContent = 'Decision recorded';
    els.late.disabled = false;
    activity('The operator approved the file. Four missing employees were deactivated.');
    await renderRoster();
  } catch (error) {
    els.approve.disabled = false;
    toast(`Could not apply the file: ${error.message}`);
  }
}

async function tryLateUpdate() {
  if (!applied) return;
  els.late.disabled = true;
  try {
    const original = session.records[0];
    const newer = { ...original, type: 'UPDATE', source_version: 11, shift: '19:00' };
    const older = { ...original, type: 'UPDATE', source_version: 10, shift: '20:00' };
    const first = await request('/v1/events', { method: 'POST', token: session.hr_token, body: newer });
    const second = await request('/v1/events', { method: 'POST', token: session.hr_token, body: older });
    const final = await request(`/v1/employees/${original.employee_id}`, { token: session.operator_token });
    els.proof.hidden = false;
    els.proof.textContent = `Version 11: ${first.result.toLowerCase()}. Version 10: ${second.result.toLowerCase()}. Asha’s shift is still ${final.shift}.`;
    activity('A late version 10 update was ignored; Asha kept the newer 19:00 shift.');
    await renderRoster();
  } catch (error) {
    els.late.disabled = false;
    toast(`Could not run the ordering check: ${error.message}`);
  }
}

els.start.addEventListener('click', startDemo);
els.restart.addEventListener('click', startDemo);
els.check.addEventListener('click', checkFile);
els.reason.addEventListener('input', () => { els.approve.disabled = els.reason.value.trim().length < 8; });
els.approve.addEventListener('click', approveFile);
els.late.addEventListener('click', tryLateUpdate);
