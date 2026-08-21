/* ==========================================================================
   MediRemind - front end.

   The whole app is one file on purpose: it has to run from a plain static
   server with no build step, and it has to keep working on an old phone with a
   slow connection, which is what the target user actually owns.

   The important piece is the alarm engine at the bottom. An alarm that is easy
   to ignore is useless for the person it is meant to protect, so a due dose
   takes over the whole screen, rings, speaks the medicine name out loud, and
   stays there until somebody answers it.
   ========================================================================== */

'use strict';

const $ = (id) => document.getElementById(id);
const POLL_MS = 30000;

const state = {
  patients: [],
  patient: null,
  schedule: null,
  medicines: [],
  draft: null,          // parsed-but-unconfirmed prescription
  alarmQueue: [],
  ringing: null,
  reportDays: 7,
};

const prefs = {
  read() {
    try { return JSON.parse(localStorage.getItem('mediremind.prefs') || '{}'); }
    catch { return {}; }
  },
  write(patch) {
    const next = Object.assign(this.read(), patch);
    localStorage.setItem('mediremind.prefs', JSON.stringify(next));
    return next;
  },
};

/* ------------------------------------------------------------------ api -- */

async function api(path, options = {}) {
  const response = await fetch(path, {
    headers: options.body instanceof FormData ? {} : { 'Content-Type': 'application/json' },
    ...options,
  });
  const text = await response.text();
  let payload = null;
  try { payload = text ? JSON.parse(text) : null; } catch { payload = { error: text }; }
  if (!response.ok) {
    const message = (payload && (payload.error || payload.detail)) || `Something went wrong (${response.status})`;
    throw new Error(typeof message === 'string' ? message : 'Something went wrong');
  }
  return payload;
}

let bannerTimer = null;
function banner(message, kind = 'ok', ms = 4000) {
  const el = $('banner');
  el.textContent = message;
  el.className = 'banner' + (kind === 'error' ? ' banner--error' : '');
  el.hidden = false;
  clearTimeout(bannerTimer);
  bannerTimer = setTimeout(() => { el.hidden = true; }, ms);
}

/* --------------------------------------------------------------- format -- */

const FOOD_TEXT = {
  before_food: 'before food',
  after_food: 'after food',
  with_food: 'with food',
  empty_stomach: 'on an empty stomach',
  any: '',
};

const SLOT_TEXT = {
  morning: 'Morning', afternoon: 'Afternoon', evening: 'Evening',
  night: 'Night', bedtime: 'Bedtime',
};

function qty(value) {
  const number = Number(value);
  if (number === 0.5) return 'half a';
  if (number === 0.25) return 'quarter of a';
  return Number.isInteger(number) ? String(number) : String(number);
}

function doseSentence(dose) {
  const unit = dose.unit === 'tablet' && Number(dose.quantity) !== 1 ? 'tablets' : dose.unit;
  const strength = dose.strength ? ` (${dose.strength})` : '';
  return `${qty(dose.quantity)} ${unit}${strength}`;
}

function escapeHtml(value) {
  return String(value ?? '').replace(/[&<>"']/g, (ch) => (
    { '&': '&amp;', '<': '&lt;', '>': '&gt;', '"': '&quot;', "'": '&#39;' }[ch]
  ));
}

function relativeTime(iso) {
  const minutes = Math.round((new Date(iso) - new Date()) / 60000);
  if (minutes <= 0) return 'now';
  if (minutes < 60) return `in ${minutes} minute${minutes === 1 ? '' : 's'}`;
  const hours = Math.floor(minutes / 60);
  const rest = minutes % 60;
  return `in ${hours} hour${hours === 1 ? '' : 's'}${rest ? ` ${rest} min` : ''}`;
}

function todayISO() {
  const now = new Date();
  return new Date(now.getTime() - now.getTimezoneOffset() * 60000).toISOString().slice(0, 10);
}

/* --------------------------------------------------------------- router -- */

const VIEWS = ['today', 'upload', 'medicines', 'report', 'settings'];

function go(view) {
  VIEWS.forEach((name) => { $(`view-${name}`).hidden = name !== view; });
  document.querySelectorAll('.tab').forEach((tab) => {
    tab.classList.toggle('is-active', tab.dataset.go === view);
  });
  window.scrollTo({ top: 0, behavior: 'instant' in window ? 'instant' : 'auto' });
  if (view === 'today') refreshToday();
  if (view === 'medicines') renderMedicines();
  if (view === 'report') renderReport();
  if (view === 'settings') fillPatientForm();
}

document.addEventListener('click', (event) => {
  const target = event.target.closest('[data-go]');
  if (target) go(target.dataset.go);
});

/* ------------------------------------------------------------- patients -- */

async function loadPatients(preferId) {
  state.patients = await api('/api/patients');
  const select = $('patient-select');
  select.innerHTML = state.patients
    .map((p) => `<option value="${p.id}">${escapeHtml(p.name)}</option>`).join('');

  const saved = preferId || prefs.read().patientId;
  const found = state.patients.find((p) => String(p.id) === String(saved));
  state.patient = found || state.patients[0] || null;

  if (state.patient) {
    select.value = state.patient.id;
    prefs.write({ patientId: state.patient.id });
  }
  select.hidden = state.patients.length < 2;
  $('no-patient').hidden = !!state.patient;
  $('today-body').hidden = !state.patient;
}

$('patient-select').addEventListener('change', async (event) => {
  state.patient = state.patients.find((p) => String(p.id) === event.target.value) || null;
  prefs.write({ patientId: state.patient ? state.patient.id : null });
  await refreshToday();
});

/* ---------------------------------------------------------------- today -- */

async function refreshToday() {
  if (!state.patient) {
    $('no-patient').hidden = false;
    $('today-body').hidden = true;
    return;
  }
  $('no-patient').hidden = true;
  $('today-body').hidden = false;

  const [schedule, refills] = await Promise.all([
    api(`/api/patients/${state.patient.id}/schedule`),
    api(`/api/patients/${state.patient.id}/refills`).catch(() => []),
  ]);
  state.schedule = schedule;
  renderToday(schedule);
  renderRefills(refills);
  await renderPrn();
}

function renderToday(schedule) {
  $('today-date').textContent = new Date(schedule.date + 'T00:00')
    .toLocaleDateString(undefined, { weekday: 'long', day: 'numeric', month: 'long' });

  const next = schedule.next_dose;
  $('next-time').textContent = next ? next.time_label : '—';
  $('next-name').textContent = next ? next.medicine_name : 'Nothing more today';
  $('next-meta').textContent = next
    ? `${doseSentence(next)}${next.food_label ? ' · ' + next.food_label : ''}`
    : 'All done. Well done! 🎉';
  $('next-countdown').textContent = next ? relativeTime(next.scheduled_at) : '';

  const counts = schedule.counts;
  $('daystats').innerHTML = `
    <div class="daystat daystat--taken"><span class="daystat__n">${counts.taken}</span><span class="daystat__l">Taken</span></div>
    <div class="daystat"><span class="daystat__n">${counts.pending}</span><span class="daystat__l">To come</span></div>
    <div class="daystat daystat--missed"><span class="daystat__n">${counts.missed}</span><span class="daystat__l">Missed</span></div>`;

  const list = $('dose-list');
  if (!schedule.doses.length) {
    list.innerHTML = `<li class="card">No medicines are scheduled for today.
      Add a prescription to set the alarms.</li>`;
    return;
  }

  list.innerHTML = schedule.doses.map((dose) => {
    const isDue = schedule.due_now.some((d) => d.dose_id === dose.dose_id);
    const modifier = isDue && dose.status === 'pending' ? 'due' : dose.status;
    const statusLine = {
      taken: '<span class="dose__status dose__status--taken">✓ Taken</span>',
      missed: '<span class="dose__status dose__status--missed">✗ Missed</span>',
      skipped: '<span class="dose__status">Skipped</span>',
      snoozed: '<span class="dose__status">⏱ Snoozed</span>',
    }[dose.status] || '';

    const actions = (dose.status === 'taken' || dose.status === 'skipped')
      ? `<button class="btn btn--ghost" data-dose="${dose.dose_id}" data-action="undo">Undo</button>`
      : `<button class="btn btn--green" data-dose="${dose.dose_id}" data-action="taken">✓ Taken</button>
         <button class="btn btn--ghost" data-dose="${dose.dose_id}" data-action="skip">Skip</button>`;

    return `<li class="dose dose--${modifier}">
      <div>
        <div class="dose__time">${escapeHtml(dose.time_label)}</div>
        <span class="dose__slot">${escapeHtml(SLOT_TEXT[dose.slot] || '')}</span>
      </div>
      <div>
        <p class="dose__name">${escapeHtml(dose.medicine_name)}</p>
        <p class="dose__what">${escapeHtml(doseSentence(dose))}</p>
        ${dose.food_label ? `<p class="dose__food">Take it ${escapeHtml(dose.food_label)}</p>` : ''}
        ${statusLine}
        <div class="dose__actions">${actions}</div>
      </div>
    </li>`;
  }).join('');
}

$('dose-list').addEventListener('click', async (event) => {
  const button = event.target.closest('[data-dose]');
  if (!button) return;
  button.disabled = true;
  try {
    await doseAction(button.dataset.dose, button.dataset.action);
    await refreshToday();
  } catch (error) {
    banner(error.message, 'error');
    button.disabled = false;
  }
});

async function doseAction(doseId, action, minutes = 10) {
  return api('/api/doses/action', {
    method: 'POST',
    body: JSON.stringify({ patient_id: state.patient.id, dose_id: doseId, action, minutes }),
  });
}

function renderRefills(refills) {
  const box = $('refill-box');
  if (!refills.length) { box.hidden = true; return; }
  box.hidden = false;
  box.innerHTML = '<strong>💊 Time to buy a refill</strong><ul>' + refills.map((r) => (
    `<li>${escapeHtml(r.name)} — ${r.finished ? 'course finished' : `${r.days_left} day(s) left`}</li>`
  )).join('') + '</ul>';
}

async function renderPrn() {
  const medicines = await api(`/api/patients/${state.patient.id}/medicines`);
  state.medicines = medicines;
  const asNeeded = medicines.filter((m) => m.prn);
  $('prn-box').hidden = asNeeded.length === 0;
  $('prn-list').innerHTML = asNeeded.map((m) => (
    `<li><strong>${escapeHtml(m.name)}</strong> ${escapeHtml(m.strength || '')}<br>
     <span class="hint">Only if needed — no alarm will ring for this one.</span></li>`
  )).join('');
}

/* ------------------------------------------------------- prescriptions -- */

function setStep(step) {
  document.querySelectorAll('.steps__item').forEach((item) => {
    item.classList.toggle('is-active', Number(item.dataset.step) === step);
  });
  $('step-upload').hidden = step !== 1;
  $('step-review').hidden = step !== 2;
  $('step-done').hidden = step !== 3;
}

$('file-input').addEventListener('change', async (event) => {
  const file = event.target.files[0];
  if (!file) return;
  if (!state.patient) { banner('Add a patient first, in Settings.', 'error'); return; }

  const preview = $('preview');
  preview.src = URL.createObjectURL(file);
  preview.hidden = false;
  $('dropzone').classList.add('is-busy');
  banner('Reading the prescription…');

  const form = new FormData();
  form.append('file', file);
  try {
    const parsed = await api('/api/prescriptions/parse', { method: 'POST', body: form });
    openReview(parsed);
  } catch (error) {
    banner(error.message, 'error', 7000);
  } finally {
    $('dropzone').classList.remove('is-busy');
    event.target.value = '';
  }
});

$('parse-text').addEventListener('click', async () => {
  const text = $('rx-text').value.trim();
  if (!text) { banner('Please type the prescription first.', 'error'); return; }
  if (!state.patient) { banner('Add a patient first, in Settings.', 'error'); return; }
  try {
    openReview(await api('/api/prescriptions/parse-text', {
      method: 'POST', body: JSON.stringify({ text }),
    }));
  } catch (error) {
    banner(error.message, 'error');
  }
});

function openReview(parsed) {
  state.draft = parsed;
  $('start-date').value = todayISO();
  renderReview();
  setStep(2);
  window.scrollTo({ top: 0 });
}

function renderReview() {
  const warnings = state.draft.warnings || [];
  $('review-warnings').innerHTML = warnings.length
    ? `<div class="notice notice--amber">${warnings.map(escapeHtml).join('<br>')}</div>` : '';

  $('review-list').innerHTML = state.draft.medicines.map((med, index) => {
    const unsure = med.confidence < 0.6 || (med.warnings || []).length > 0;
    const slots = ['morning', 'afternoon', 'evening', 'night', 'bedtime'].map((slot) => `
      <label class="slotbox"><span>${SLOT_TEXT[slot]}</span>
        <input type="number" min="0" max="10" step="0.5" data-index="${index}" data-slot="${slot}"
               value="${med.slot_qty && med.slot_qty[slot] ? med.slot_qty[slot] : 0}">
      </label>`).join('');

    return `<article class="review ${unsure ? 'review--unsure' : ''}">
      <div class="review__head">
        <div>
          <p class="review__name">${escapeHtml(med.name)} ${escapeHtml(med.strength || '')}</p>
          <p class="review__sub">${escapeHtml(med.frequency_label || '')}
            ${med.food && med.food !== 'any' ? ' · ' + FOOD_TEXT[med.food] : ''}
            ${med.duration_days ? ' · for ' + med.duration_days + ' days' : ' · ongoing'}</p>
        </div>
        ${unsure ? '<span class="review__flag">⚠ please check</span>' : ''}
      </div>
      ${(med.warnings || []).map((w) => `<p class="review__warn">⚠ ${escapeHtml(w)}</p>`).join('')}
      ${(med.notes || []).map((n) => `<p class="review__note">${escapeHtml(n)}</p>`).join('')}
      <button class="review__toggle" data-toggle="${index}">✎ Change this medicine</button>
      <div class="review__edit" id="edit-${index}" hidden>
        <label class="field"><span>Medicine name</span>
          <input data-index="${index}" data-field="name" value="${escapeHtml(med.name)}"></label>
        <div class="row">
          <label class="field"><span>Strength</span>
            <input data-index="${index}" data-field="strength" value="${escapeHtml(med.strength || '')}"></label>
          <label class="field"><span>For how many days</span>
            <input type="number" min="1" max="365" data-index="${index}" data-field="duration_days"
                   value="${med.duration_days || ''}" placeholder="ongoing"></label>
        </div>
        <label class="field"><span>When to take it</span>
          <select data-index="${index}" data-field="food">
            <option value="any"${med.food === 'any' ? ' selected' : ''}>Any time</option>
            <option value="before_food"${med.food === 'before_food' ? ' selected' : ''}>Before food</option>
            <option value="after_food"${med.food === 'after_food' ? ' selected' : ''}>After food</option>
            <option value="with_food"${med.food === 'with_food' ? ' selected' : ''}>With food</option>
            <option value="empty_stomach"${med.food === 'empty_stomach' ? ' selected' : ''}>Empty stomach</option>
          </select></label>
        <p class="hint">How many ${escapeHtml(med.dose_unit || 'tablet')}(s) at each time? Put 0 for none.</p>
        <div class="review__slots">${slots}</div>
        <label class="check"><input type="checkbox" data-index="${index}" data-field="prn"
          ${med.prn ? 'checked' : ''}> <span>Only when needed (no alarm)</span></label>
        <button class="review__remove" data-remove="${index}">🗑 Remove this medicine</button>
      </div>
    </article>`;
  }).join('');
}

$('review-list').addEventListener('click', (event) => {
  const toggle = event.target.closest('[data-toggle]');
  if (toggle) {
    const box = $(`edit-${toggle.dataset.toggle}`);
    box.hidden = !box.hidden;
    return;
  }
  const remove = event.target.closest('[data-remove]');
  if (remove) {
    state.draft.medicines.splice(Number(remove.dataset.remove), 1);
    renderReview();
  }
});

$('review-list').addEventListener('input', (event) => {
  const input = event.target;
  const index = Number(input.dataset.index);
  const med = state.draft.medicines[index];
  if (!med) return;

  if (input.dataset.slot) {
    const value = Number(input.value) || 0;
    med.slot_qty = med.slot_qty || {};
    if (value > 0) med.slot_qty[input.dataset.slot] = value;
    else delete med.slot_qty[input.dataset.slot];
    med.slots = Object.keys(med.slot_qty);
    med.interval_hours = null;            // an explicit grid replaces "every N hours"
    return;
  }

  const field = input.dataset.field;
  if (!field) return;
  if (field === 'prn') med.prn = input.checked;
  else if (field === 'duration_days') med.duration_days = input.value ? Number(input.value) : null;
  else med[field] = input.value;
});

$('add-row').addEventListener('click', () => {
  state.draft = state.draft || { medicines: [], warnings: [] };
  state.draft.medicines.push({
    name: '', form: 'tablet', strength: '', dose_qty: 1, dose_unit: 'tablet',
    slots: ['morning'], slot_qty: { morning: 1 }, food: 'any', duration_days: null,
    prn: false, frequency_label: 'Once a day', confidence: 1, warnings: [], notes: [],
  });
  renderReview();
});

$('cancel-review').addEventListener('click', () => {
  state.draft = null;
  $('preview').hidden = true;
  $('rx-text').value = '';
  setStep(1);
});

$('confirm-btn').addEventListener('click', async () => {
  const medicines = (state.draft.medicines || []).filter((m) => (m.name || '').trim());
  if (!medicines.length) { banner('Please add at least one medicine.', 'error'); return; }

  const button = $('confirm-btn');
  button.disabled = true;
  try {
    const result = await api('/api/prescriptions/confirm', {
      method: 'POST',
      body: JSON.stringify({
        patient_id: state.patient.id,
        medicines,
        raw_text: state.draft.raw_text || '',
        source: state.draft.source || 'text',
        image_path: state.draft.image_path || '',
        start_date: $('start-date').value || todayISO(),
      }),
    });
    $('done-title').textContent = `${result.medicine_ids.length} medicine(s) saved`;
    $('done-list').innerHTML = result.alarms_today.length
      ? result.alarms_today.map((d) => (
          `<li>⏰ <strong>${escapeHtml(d.time_label)}</strong> — ${escapeHtml(d.medicine_name)},
            ${escapeHtml(doseSentence(d))}</li>`)).join('')
      : '<li>No alarms today — the first dose starts on the date you chose.</li>';
    setStep(3);
    state.draft = null;
    $('rx-text').value = '';
    $('preview').hidden = true;
    await refreshToday();
    await requestNotificationPermission();
  } catch (error) {
    banner(error.message, 'error', 7000);
  } finally {
    button.disabled = false;
  }
});

/* ------------------------------------------------------------ medicines -- */

async function renderMedicines() {
  if (!state.patient) { $('medicine-list').innerHTML = '<p class="card">Add a patient first.</p>'; return; }
  const medicines = await api(`/api/patients/${state.patient.id}/medicines`, {});
  const stopped = await api(`/api/patients/${state.patient.id}/medicines?include_stopped=true`);
  state.medicines = stopped;

  $('medicine-list').innerHTML = stopped.length ? stopped.map((med) => {
    const times = med.prn ? 'Only when needed'
      : med.interval_hours ? `Every ${med.interval_hours} hours`
      : (med.slots || []).map((s) => `${SLOT_TEXT[s]} ×${qty(med.slot_qty[s] || med.dose_qty)}`).join(' · ');
    const course = med.course_end_date
      ? `Course ends ${new Date(med.course_end_date + 'T00:00').toLocaleDateString()} (${Math.max(med.days_left, 0)} day(s) left)`
      : 'Ongoing medicine';
    return `<article class="medicine ${med.active ? '' : 'medicine--stopped'}">
      <p class="medicine__name">${escapeHtml(med.name)} ${escapeHtml(med.strength || '')}</p>
      <p class="medicine__meta">${escapeHtml(med.form)}${med.food && med.food !== 'any' ? ' · ' + FOOD_TEXT[med.food] : ''}</p>
      <p class="medicine__times">${escapeHtml(times)}</p>
      <p class="medicine__course">${escapeHtml(course)}${med.day_interval > 1 ? ` · every ${med.day_interval} days` : ''}</p>
      ${med.active
        ? `<div class="medicine__actions"><button class="btn btn--ghost" data-stop="${med.id}">Stop this medicine</button></div>`
        : '<p class="medicine__course"><strong>Stopped</strong></p>'}
    </article>`;
  }).join('') : '<p class="card">No medicines yet. Add a prescription to get started.</p>';
  void medicines;
}

$('medicine-list').addEventListener('click', async (event) => {
  const button = event.target.closest('[data-stop]');
  if (!button) return;
  const medicine = state.medicines.find((m) => String(m.id) === button.dataset.stop);
  if (!confirm(`Stop the alarms for ${medicine ? medicine.name : 'this medicine'}?`)) return;
  await api(`/api/medicines/${button.dataset.stop}`, { method: 'DELETE' });
  banner('Medicine stopped. No more alarms for it.');
  await renderMedicines();
});

/* --------------------------------------------------------------- report -- */

document.querySelectorAll('#report-range .segmented__btn').forEach((button) => {
  button.addEventListener('click', () => {
    document.querySelectorAll('#report-range .segmented__btn')
      .forEach((b) => b.classList.toggle('is-active', b === button));
    state.reportDays = Number(button.dataset.days);
    renderReport();
  });
});

async function renderReport() {
  if (!state.patient) { $('report-body').innerHTML = '<p class="card">Add a patient first.</p>'; return; }
  const report = await api(`/api/patients/${state.patient.id}/adherence?days=${state.reportDays}`);
  const percent = report.adherence_percent;
  const tone = percent === null ? '' : percent >= 80 ? '' : percent >= 50 ? ' score__n--mid' : ' score__n--low';

  const bars = report.days.map((day) => {
    const total = day.scheduled || 1;
    const seg = (key) => day[key] ? `<div class="bar__seg bar__seg--${key}" style="width:${(day[key] / total) * 100}%"></div>` : '';
    const label = new Date(day.date + 'T00:00').toLocaleDateString(undefined, { weekday: 'short', day: 'numeric' });
    return `<div class="bar">
      <span class="bar__day">${escapeHtml(label)}</span>
      <div class="bar__track">${seg('taken')}${seg('missed')}${seg('skipped')}${seg('pending')}</div>
      <span class="bar__n">${day.taken}/${day.scheduled}</span>
    </div>`;
  }).join('');

  $('report-body').innerHTML = `
    <div class="card score">
      <div class="score__n${tone}">${percent === null ? '—' : percent + '%'}</div>
      <p class="score__l">of doses taken on time over the last ${report.days.length} day(s)</p>
      <p class="score__l">✓ ${report.taken} taken · ✗ ${report.missed} missed · ${report.skipped} skipped</p>
    </div>
    <div class="card">
      <h3 class="subhead">Day by day</h3>
      <div class="bars">${bars}</div>
      <div class="legend">
        <span class="l-taken">Taken</span><span class="l-missed">Missed</span>
        <span class="l-skipped">Skipped</span><span class="l-pending">Still to come</span>
      </div>
    </div>
    ${report.caregiver.phone ? `<div class="card">
      <h3 class="subhead">Share with family</h3>
      <p>${escapeHtml(report.caregiver.name || 'Caregiver')} — ${escapeHtml(report.caregiver.phone)}</p>
      <a class="btn btn--big btn--green" href="sms:${encodeURIComponent(report.caregiver.phone)}?body=${encodeURIComponent(
        `${report.patient.name}'s medicines: ${report.taken} taken, ${report.missed} missed in the last ${report.days.length} days.`)}">
        Send this summary by SMS</a>
    </div>` : ''}`;
}

/* ------------------------------------------------------------- settings -- */

function fillPatientForm() {
  const patient = state.patient;
  if (!patient) return;
  $('p-name').value = patient.name || '';
  $('p-age').value = patient.age || '';
  $('p-phone').value = patient.phone || '';
  $('p-cname').value = patient.caregiver_name || '';
  $('p-cphone').value = patient.caregiver_phone || '';
  const routine = patient.routine || {};
  ['wake', 'breakfast', 'lunch', 'evening', 'dinner', 'bed'].forEach((key) => {
    $(`r-${key}`).value = routine[key] || '';
  });
}

$('patient-form').addEventListener('submit', async (event) => {
  event.preventDefault();
  if (!state.patient) return;
  const routine = {};
  ['wake', 'breakfast', 'lunch', 'evening', 'dinner', 'bed'].forEach((key) => {
    if ($(`r-${key}`).value) routine[key] = $(`r-${key}`).value;
  });
  try {
    await api(`/api/patients/${state.patient.id}`, {
      method: 'PUT',
      body: JSON.stringify({
        name: $('p-name').value.trim(),
        age: $('p-age').value ? Number($('p-age').value) : null,
        phone: $('p-phone').value,
        caregiver_name: $('p-cname').value,
        caregiver_phone: $('p-cphone').value,
        routine,
      }),
    });
    banner('Saved. All alarms now follow these times.');
    await loadPatients(state.patient.id);
    await refreshToday();
  } catch (error) {
    banner(error.message, 'error');
  }
});

$('add-patient').addEventListener('click', async () => {
  const name = $('new-patient-name').value.trim();
  if (!name) { banner('Please type a name.', 'error'); return; }
  const created = await api('/api/patients', { method: 'POST', body: JSON.stringify({ name }) });
  $('new-patient-name').value = '';
  await loadPatients(created.id);
  fillPatientForm();
  await refreshToday();
  banner(`${name} added.`);
});

const SCALES = ['normal', 'large', 'huge'];
$('text-size').addEventListener('click', () => {
  const current = document.body.dataset.scale;
  const next = SCALES[(SCALES.indexOf(current) + 1) % SCALES.length];
  document.body.dataset.scale = next;
  prefs.write({ scale: next });
  banner(`Text size: ${next}`);
});

['sound', 'voice', 'notify'].forEach((key) => {
  $(`opt-${key}`).addEventListener('change', (event) => {
    prefs.write({ [key]: event.target.checked });
    if (key === 'notify' && event.target.checked) requestNotificationPermission();
  });
});

$('test-alarm').addEventListener('click', () => {
  showAlarm([{
    dose_id: 'test', medicine_name: 'Test medicine', quantity: 1, unit: 'tablet',
    strength: '500 mg', time_label: new Date().toLocaleTimeString([], { hour: 'numeric', minute: '2-digit' }),
    food_label: 'after food', scheduled_at: new Date().toISOString(),
  }], true);
});

/* ==========================================================================
   The alarm engine
   ========================================================================== */

let audioContext = null;
let chimeTimer = null;

function unlockAudio() {
  if (!audioContext) {
    const Ctor = window.AudioContext || window.webkitAudioContext;
    if (Ctor) audioContext = new Ctor();
  }
  if (audioContext && audioContext.state === 'suspended') audioContext.resume();
}
['click', 'touchstart', 'keydown'].forEach((type) => {
  document.addEventListener(type, unlockAudio, { once: true, passive: true });
});

function chime() {
  if (!audioContext || prefs.read().sound === false) return;
  const now = audioContext.currentTime;
  // Two rising notes, repeated - distinct from a phone ringtone so it is not
  // mistaken for a call, and low enough in pitch to stay audible with age-related
  // high-frequency hearing loss.
  [0, 0.36, 0.72].forEach((offset, index) => {
    const osc = audioContext.createOscillator();
    const gain = audioContext.createGain();
    osc.type = 'sine';
    osc.frequency.setValueAtTime(index % 2 === 0 ? 660 : 880, now + offset);
    gain.gain.setValueAtTime(0.0001, now + offset);
    gain.gain.exponentialRampToValueAtTime(0.5, now + offset + 0.04);
    gain.gain.exponentialRampToValueAtTime(0.0001, now + offset + 0.32);
    osc.connect(gain).connect(audioContext.destination);
    osc.start(now + offset);
    osc.stop(now + offset + 0.35);
  });
}

function speak(dose) {
  if (prefs.read().voice === false || !('speechSynthesis' in window)) return;
  const unit = dose.unit === 'tablet' && Number(dose.quantity) !== 1 ? 'tablets' : dose.unit;
  const sentence = `It is time for your medicine. Please take ${qty(dose.quantity)} ${unit} of `
    + `${dose.medicine_name}${dose.food_label ? ', ' + dose.food_label : ''}.`;
  const utterance = new SpeechSynthesisUtterance(sentence);
  utterance.rate = 0.85;                     // slower than default: much easier to follow
  utterance.lang = (state.patient && state.patient.language) || 'en-IN';
  window.speechSynthesis.cancel();
  window.speechSynthesis.speak(utterance);
}

function notify(dose) {
  if (prefs.read().notify === false) return;
  if (!('Notification' in window) || Notification.permission !== 'granted') return;
  try {
    new Notification('💊 Time for your medicine', {
      body: `${dose.medicine_name} — ${doseSentence(dose)}${dose.food_label ? ', ' + dose.food_label : ''}`,
      tag: dose.dose_id,
      requireInteraction: true,
    });
  } catch { /* some browsers only allow this from a service worker */ }
}

async function requestNotificationPermission() {
  if (!('Notification' in window)) return;
  if (Notification.permission === 'default') {
    try { await Notification.requestPermission(); } catch { /* user dismissed */ }
  }
  $('notify-state').textContent = 'Phone notifications: ' + (
    !('Notification' in window) ? 'not supported on this phone' : Notification.permission);
}

function showAlarm(doses, isTest = false) {
  if (!doses.length) return;
  state.alarmQueue = doses.slice(1);
  state.ringing = doses[0];
  state.ringingIsTest = isTest;
  const dose = state.ringing;

  $('alarm-clock').textContent = `Scheduled for ${dose.time_label}`;
  $('alarm-title').textContent = dose.medicine_name;
  $('alarm-dose').textContent = `Take ${doseSentence(dose)}`;
  $('alarm-food').textContent = dose.food_label ? `Take it ${dose.food_label}` : '';
  $('alarm-queue').hidden = state.alarmQueue.length === 0;
  $('alarm-queue').textContent = state.alarmQueue.length
    ? `${state.alarmQueue.length} more medicine(s) are due right now` : '';
  $('alarm').hidden = false;

  unlockAudio();
  chime();
  speak(dose);
  notify(dose);
  if (navigator.vibrate) navigator.vibrate([500, 250, 500, 250, 500]);
  clearInterval(chimeTimer);
  chimeTimer = setInterval(chime, 4000);
}

function stopSound() {
  clearInterval(chimeTimer);
  chimeTimer = null;
  if ('speechSynthesis' in window) window.speechSynthesis.cancel();
  if (navigator.vibrate) navigator.vibrate(0);
}

function closeAlarm() {
  stopSound();
  $('alarm').hidden = true;
  state.ringing = null;
  if (state.alarmQueue.length) {
    setTimeout(() => showAlarm(state.alarmQueue, state.ringingIsTest), 500);
  }
}

async function answerAlarm(action, minutes) {
  const dose = state.ringing;
  if (!dose) return;
  if (state.ringingIsTest) { closeAlarm(); return; }
  try {
    await doseAction(dose.dose_id, action, minutes);
    const words = { taken: 'Well done — marked as taken.', skip: 'Dose skipped.', snooze: `We will remind you in ${minutes} minutes.` };
    banner(words[action] || 'Saved.');
  } catch (error) {
    banner(error.message, 'error');
  }
  closeAlarm();
  refreshToday();
}

$('alarm-taken').addEventListener('click', () => answerAlarm('taken'));
$('alarm-skip').addEventListener('click', () => answerAlarm('skip'));
$('alarm-snooze').addEventListener('click', () => answerAlarm('snooze', 10));
$('alarm-quiet').addEventListener('click', stopSound);

async function pollAlarms() {
  if (!state.patient || state.ringing) return;
  try {
    const due = await api(`/api/patients/${state.patient.id}/due`);
    if (due.ringing.length) showAlarm(due.ringing);
    else if (state.schedule) {
      // Keep the "next medicine" card honest even when nothing is ringing.
      const next = due.next_dose;
      if (next) {
        $('next-time').textContent = next.time_label;
        $('next-name').textContent = next.medicine_name;
        $('next-countdown').textContent = relativeTime(next.scheduled_at);
      }
    }
  } catch { /* offline: try again on the next tick */ }
}

/* ----------------------------------------------------------------- boot -- */

async function boot() {
  const saved = prefs.read();
  document.body.dataset.scale = saved.scale || 'large';
  $('opt-sound').checked = saved.sound !== false;
  $('opt-voice').checked = saved.voice !== false;
  $('opt-notify').checked = saved.notify !== false;

  try {
    const health = await api('/api/health');
    if (!health.ocr_available) {
      $('ocr-note').hidden = false;
      $('ocr-note').textContent =
        'Photo reading is not available on this server, so please type the prescription below.';
      $('typebox').open = true;
    }
  } catch { /* the UI still works offline for everything already loaded */ }

  await loadPatients();
  await refreshToday();
  fillPatientForm();
  await requestNotificationPermission();

  setInterval(pollAlarms, POLL_MS);
  pollAlarms();
  // A dose can come due while the phone is in a pocket; check the moment it wakes.
  document.addEventListener('visibilitychange', () => {
    if (!document.hidden) { pollAlarms(); refreshToday(); }
  });

  if ('serviceWorker' in navigator) {
    navigator.serviceWorker.register('/app/sw.js').catch(() => { /* https only */ });
  }
}

boot().catch((error) => banner(error.message, 'error', 8000));
