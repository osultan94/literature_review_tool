const API = "";

function $(sel) { return document.querySelector(sel); }
function $$(sel) { return document.querySelectorAll(sel); }

function showSpinner(show) {
    $("#spinner").classList.toggle("hidden", !show);
}

function logRun(msg) {
    const el = $("#run-log");
    el.textContent += `[${new Date().toLocaleTimeString()}] ${msg}\n`;
    el.scrollTop = el.scrollHeight;
}

async function api(path, opts = {}) {
    const res = await fetch(API + path, opts);
    if (!res.ok) {
        const text = await res.text();
        throw new Error(`${res.status}: ${text}`);
    }
    return res.json();
}

// Tabs
$$('nav button').forEach(btn => {
    btn.addEventListener('click', () => {
        $$('nav button').forEach(b => b.classList.remove('active'));
        $$('.tab').forEach(t => t.classList.remove('active'));
        btn.classList.add('active');
        $(`#${btn.dataset.tab}`).classList.add('active');
        if (btn.dataset.tab === 'dashboard') loadDashboard();
        if (btn.dataset.tab === 'papers') loadPapers();
        if (btn.dataset.tab === 'criteria') loadCriteria();
        if (btn.dataset.tab === 'weights') loadWeights();
        if (btn.dataset.tab === 'seeds') loadSeeds();
    });
});

// Dashboard
async function loadDashboard() {
    const data = await api('/api/dashboard');
    const s = data.seeds, p = data.papers;
    const saturationClass = data.saturation.saturated ? 'exclude' : 'include';
    $('#dashboard-content').innerHTML = `
        <div class="stats">
            <div class="stat"><div class="value">${s.total}</div><div>Seeds</div></div>
            <div class="stat"><div class="value">${s.resolved}</div><div>Resolved</div></div>
            <div class="stat"><div class="value">${p.total}</div><div>Papers</div></div>
            <div class="stat include"><div class="value">${p.include}</div><div>Include</div></div>
            <div class="stat exclude"><div class="value">${p.exclude}</div><div>Exclude</div></div>
            <div class="stat uncertain"><div class="value">${p.uncertain}</div><div>Uncertain</div></div>
            <div class="stat"><div class="value">${p.unscreened}</div><div>Unscreened</div></div>
        </div>
        <div class="card">
            <p><strong>Active criteria version:</strong> ${data.criteria_version ?? 'none'}</p>
            <p><strong>Active weight version:</strong> ${data.weight_version ?? 'none'}</p>
            <p><strong>Saturation:</strong> <span class="${saturationClass}">${data.saturation.saturated ? 'SATURATED' : 'Not saturated'}</span></p>
            <table>
                <tr><th>Round</th><th>Stage</th><th>Results</th><th>New unique</th><th>Novelty</th></tr>
                ${data.saturation.rounds.map(r => `
                    <tr>
                        <td>${r.round_number}</td>
                        <td>${r.stage}</td>
                        <td>${r.results_count}</td>
                        <td>${r.new_unique_count}</td>
                        <td>${(r.novelty_ratio * 100).toFixed(1)}%</td>
                    </tr>
                `).join('')}
            </table>
        </div>
    `;
}

// Papers
async function loadPapers() {
    const q = $('#paper-search').value;
    const verdict = $('#paper-verdict').value;
    const sort = $('#paper-sort').value;
    const url = `/api/papers?q=${encodeURIComponent(q)}&verdict=${encodeURIComponent(verdict)}&sort=${sort}`;
    const papers = await api(url);
    const wrap = $('#papers-table-wrap');
    if (!papers.length) {
        wrap.innerHTML = '<p>No papers found.</p>';
        return;
    }
    wrap.innerHTML = `
        <table>
            <thead>
                <tr>
                    <th>ID</th>
                    <th>Title</th>
                    <th>Year</th>
                    <th>Citations</th>
                    <th>Venue</th>
                    <th>Verdict</th>
                    <th>Score</th>
                    <th>Origin</th>
                    <th>Actions</th>
                </tr>
            </thead>
            <tbody>
                ${papers.map(p => `
                    <tr>
                        <td>${p.id}</td>
                        <td>${escapeHtml(p.canonical_title)}<br><small>${p.doi || ''}</small></td>
                        <td>${p.pub_year || ''}</td>
                        <td>${p.citation_count || ''}</td>
                        <td>${escapeHtml(p.venue || '')}</td>
                        <td class="${p.human_override || p.llm_verdict || ''}">${p.human_override || p.llm_verdict || 'unscreened'}</td>
                        <td>${p.final_score != null ? p.final_score.toFixed(3) : ''}</td>
                        <td>${p.origin} ${p.discovery_round ? `(r${p.discovery_round})` : ''}</td>
                        <td class="paper-actions">
                            <button onclick="overridePaper(${p.id}, 'include')">Incl</button>
                            <button onclick="overridePaper(${p.id}, 'exclude')">Excl</button>
                            <button onclick="overridePaper(${p.id}, 'uncertain')">Unc</button>
                        </td>
                    </tr>
                `).join('')}
            </tbody>
        </table>
    `;
}

window.overridePaper = async function(id, verdict) {
    const note = prompt('Optional note for override:') || '';
    await api(`/api/papers/${id}/override`, {
        method: 'POST',
        headers: { 'Content-Type': 'application/x-www-form-urlencoded' },
        body: `override=${encodeURIComponent(verdict)}&note=${encodeURIComponent(note)}`
    });
    loadPapers();
};

$('#paper-search').addEventListener('input', debounce(loadPapers, 300));
$('#paper-verdict').addEventListener('change', loadPapers);
$('#paper-sort').addEventListener('change', loadPapers);
$('#paper-refresh').addEventListener('click', loadPapers);

// Criteria
async function loadCriteria() {
    const data = await api('/api/criteria');
    $('#criteria-versions').innerHTML = `
        <table>
            <tr><th>Version</th><th>Active</th><th>Created</th><th>Text preview</th><th>Action</th></tr>
            ${data.versions.map(v => `
                <tr>
                    <td>${v.version}</td>
                    <td>${v.active ? '✅' : ''}</td>
                    <td>${v.created_at}</td>
                    <td>${escapeHtml(v.criteria_text.substring(0, 120))}...</td>
                    <td>
                        ${!v.active ? `<button class="secondary" onclick="activateCriteria(${v.version})">Activate</button>` : ''}
                    </td>
                </tr>
            `).join('')}
        </table>
    `;
}

window.activateCriteria = async function(version) {
    await api(`/api/criteria/${version}/activate`, { method: 'POST' });
    loadCriteria();
};

$('#criteria-save').addEventListener('click', async () => {
    const text = $('#criteria-text').value;
    if (!text.trim()) return alert('Criteria text is required');
    await api('/api/criteria', {
        method: 'POST',
        headers: { 'Content-Type': 'application/x-www-form-urlencoded' },
        body: `text=${encodeURIComponent(text)}`
    });
    $('#criteria-text').value = '';
    loadCriteria();
});

// Weights
async function loadWeights() {
    const data = await api('/api/weights');
    $('#weights-versions').innerHTML = `
        <table>
            <tr><th>Version</th><th>Active</th><th>Weights</th><th>Action</th></tr>
            ${data.versions.map(v => `
                <tr>
                    <td>${v.version}</td>
                    <td>${v.active ? '✅' : ''}</td>
                    <td>${Object.entries(v.component_weights).map(([k, val]) => `${k}: ${val}`).join(', ')}</td>
                    <td>
                        ${!v.active ? `<button class="secondary" onclick="activateWeights(${v.version})">Activate</button>` : ''}
                    </td>
                </tr>
            `).join('')}
        </table>
    `;
}

window.activateWeights = async function(version) {
    await api(`/api/weights/${version}/activate`, { method: 'POST' });
    loadWeights();
};

$('#weights-form').addEventListener('submit', async (e) => {
    e.preventDefault();
    const fd = new FormData(e.target);
    await api('/api/weights', { method: 'POST', body: fd });
    loadWeights();
});

// Seeds
async function loadSeeds() {
    const seeds = await api('/api/seeds');
    const wrap = $('#seeds-table-wrap');
    wrap.innerHTML = `
        <table>
            <tr><th>ID</th><th>Raw text</th><th>Extracted title</th><th>Confidence</th><th>Status</th><th>Action</th></tr>
            ${seeds.map(s => `
                <tr>
                    <td>${s.id}</td>
                    <td>${escapeHtml(s.raw_text)}</td>
                    <td>${escapeHtml(s.extracted_title || '')}</td>
                    <td>${s.extraction_confidence != null ? s.extraction_confidence.toFixed(2) : ''}</td>
                    <td>${s.status}</td>
                    <td><button class="secondary" onclick="reextractSeed(${s.id})">Re-extract</button></td>
                </tr>
            `).join('')}
        </table>
    `;
}

window.reextractSeed = async function(id) {
    await api(`/api/seeds/${id}/reextract`, { method: 'POST' });
    loadSeeds();
};

$('#seed-upload-form').addEventListener('submit', async (e) => {
    e.preventDefault();
    const fd = new FormData(e.target);
    showSpinner(true);
    try {
        const res = await api('/api/seeds/upload', { method: 'POST', body: fd });
        logRun(`Uploaded seeds: ${res.inserted} inserted`);
        loadSeeds();
    } finally {
        showSpinner(false);
    }
});

// Run controls
$$('.run-btn').forEach(btn => {
    btn.addEventListener('click', async () => {
        const stage = btn.dataset.stage;
        showSpinner(true);
        logRun(`Starting ${stage}...`);
        try {
            const res = await api(`/api/run/${stage}`, { method: 'POST' });
            logRun(`${stage} finished: ${JSON.stringify(res)}`);
            if ($('#dashboard').classList.contains('active')) loadDashboard();
            if ($('#papers').classList.contains('active')) loadPapers();
        } catch (err) {
            logRun(`${stage} error: ${err.message}`);
        } finally {
            showSpinner(false);
        }
    });
});

$('#run-all-btn').addEventListener('click', async () => {
    const seeds = $('#run-all-seeds').value.trim();
    if (!seeds) return alert('Please enter at least one seed title');
    const snowballRounds = parseInt($('#run-all-snowball').value, 10) || 0;
    const btn = $('#run-all-btn');
    const originalText = btn.textContent;
    btn.disabled = true;
    btn.textContent = 'Running...';
    showSpinner(true);
    logRun('Starting full pipeline...');
    try {
        const res = await api('/api/run-all', {
            method: 'POST',
            headers: { 'Content-Type': 'application/x-www-form-urlencoded' },
            body: `seeds=${encodeURIComponent(seeds)}&snowball_rounds=${encodeURIComponent(snowballRounds)}`
        });
        logRun(`Full pipeline finished: ${JSON.stringify(res)}`);
        if ($('#dashboard').classList.contains('active')) loadDashboard();
        if ($('#papers').classList.contains('active')) loadPapers();
    } catch (err) {
        logRun(`Full pipeline error: ${err.message}`);
        alert(`Pipeline failed: ${err.message}`);
    } finally {
        showSpinner(false);
        btn.disabled = false;
        btn.textContent = originalText;
    }
});

// Helpers
function escapeHtml(text) {
    const div = document.createElement('div');
    div.textContent = text;
    return div.innerHTML;
}

function debounce(fn, ms) {
    let t;
    return (...args) => {
        clearTimeout(t);
        t = setTimeout(() => fn(...args), ms);
    };
}

// Initial load
loadDashboard();
