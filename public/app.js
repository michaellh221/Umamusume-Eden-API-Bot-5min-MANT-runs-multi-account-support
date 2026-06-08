// public/app.js
// =============
// Single-page application logic for the Sweepy bot UI.
//
// Architecture overview
// ---------------------
// The entire app lives inside one IIFE so state is private.
// Key sections (search for "// ──" to jump):
//   State & DOM refs        – shared mutable state and cached element handles
//   Dev / loop controls     – LOOP: ON toggle and delay settings
//   Mode switching          – SETUP ↔ DIAGNOSTICS tab bar
//   Diagnostics rendering   – career card, action log, fan metrics
//   API helpers             – apiJson(), master-data, delay settings
//   Login / auth            – login form, 2FA, session restore
//   Career modal            – delete / resume career overlay
//   Selection               – trainee, deck, parents, friend picking
//   Selection rendering     – renderParents(), renderFollowParents(), tooltips
//   Race editor             – per-year race slot picker
//   Skill editor            – skill priority list, blacklist, auto-buy
//   Preset config           – stat priority, targets, distance, save/load
//   Friends panel           – friend support card list and filtering
//   Career runner           – startCareer(), polling, action history
//   Fan stats               – STATS tab rendering
//   Dashboard init          – showDashboardView(), renderDashboard()
//   App entry point         – restoreSession(), login handler
//
// Extending / forking notes
// --------------------------
// - All REST calls go through apiJson(url, options) which adds error handling.
// - The UI polls /api/career/runner every ~2 s while a career is running.
// - Fan stats are polled via updateDiagMetrics() when the DIAGNOSTICS pane is open.
// - Selection state lives in the `selection` object; synced to the server via
//   syncSelectionToServer() after every change so refreshes restore the picks.

(() => {
// ── State & DOM refs ────────────────────────────────────────────────────────
const state = {
    needs2fa: false,
    isLoading: false,
    account: null,
    isDeletingCareer: false,
    isFetchingFriends: false,
    isStartingCareer: false,
    presets: [],
    selectedPreset: "",
    runnerTimer: 0,
    isSavingPreset: false,
    raceData: [],
    selectedRaces: [],
    scenarioType: "Mant",
    burnClocks: false,
    displayedClocksUsed: 0,
    devEnabled: true,
    careersLimit: 0,      // 0 = infinite; stop loop after N completions this session
    sessionCareersStart: 0, // careers_count at session start (for delta tracking)
    consecutiveRunnerFails: 0
};
const els = {
    loadingScreen: document.getElementById('loading-screen'),
    navbar: document.querySelector('.navbar'),
    themeToggle: document.getElementById('theme-toggle'),
    brandMark: document.querySelector('.title span'),
    loginBtn: document.getElementById('login-btn'),
    logoutBtn: document.getElementById('logout-btn'),
    turnDelayMin: document.getElementById('turn-delay-min'),
    turnDelayMax: document.getElementById('turn-delay-max'),
    temptFateBtn: document.getElementById('tempt-fate-btn'),
    burnClocksBtn: document.getElementById('burn-clocks-btn'),
    devBtn: document.getElementById('dev-career-btn'),
    loginView: document.getElementById('login-view'),
    dashboardView: document.getElementById('dashboard-view'),
    errorMsg: document.getElementById('error-msg'),
    standardFields: document.getElementById('standard-fields'),
    faFields: document.getElementById('2fa-fields'),
    umaGrid: document.getElementById('uma-grid'),
    cardGrid: document.getElementById('card-grid'),
    cardGridWrapper: document.getElementById('card-grid-wrapper'),
    cardsToggle: document.getElementById('cards-toggle'),
    cardsChevron: document.getElementById('cards-chevron'),
    parentGrid: document.getElementById('parent-grid'),
    friendGrid: document.getElementById('friend-grid'),
    deckList: document.getElementById('deck-list'),
    umaCount: document.getElementById('uma-count'),
    cardCount: document.getElementById('card-count'),
    parentCount: document.getElementById('parent-count'),
    followParentGrid: document.getElementById('follow-parent-grid'),
    followParentCount: document.getElementById('follow-parent-count'),
    followParentLoadBtn: document.getElementById('follow-parent-load-btn'),
    followParentStatus: document.getElementById('follow-parent-status'),
    friendCount: document.getElementById('friend-count'),
    friendStatus: document.getElementById('friend-status'),
    friendRefreshBtn: document.getElementById('friend-refresh-btn'),
    presetSelect: document.getElementById('preset-select'),
    startCareerBtn: document.getElementById('start-career-btn'),
    startStatus: document.getElementById('start-status'),
    accountStrip: document.getElementById('account-strip'),
    careerModal: document.getElementById('career-modal'),
    careerModalCopy: document.getElementById('career-modal-copy'),
    careerCancelBtn: document.getElementById('career-cancel-btn'),
    careerDeleteBtn: document.getElementById('career-delete-btn'),
    raceToggle: document.getElementById('race-toggle'),
    raceChevron: document.getElementById('race-chevron'),
    raceBody: document.getElementById('race-body'),
    saveRacesBtn: document.getElementById('save-races-btn'),
    raceOptionsContent: document.getElementById('race-options-content'),
    racePopupOverlay: document.getElementById('race-slot-popup-overlay'),
    racePopupTitle: document.getElementById('race-slot-popup-title'),
    racePopupBody: document.getElementById('race-slot-popup-body'),
    racePopupClose: document.getElementById('race-slot-popup-close'),
    masterDataPath: document.getElementById('master-data-path'),
    masterDataSaveBtn: document.getElementById('master-data-save-btn'),
    masterDataStatus: document.getElementById('master-data-status'),
    presetSection: document.getElementById('preset-section'),
    presetAddBtn: document.getElementById('preset-add-btn'),
    presetDelBtn: document.getElementById('preset-del-btn'),
    presetRunningStyle: document.getElementById('preset-running-style'),
    presetTargetDistance: document.getElementById('preset-target-distance'),
    presetSkillThreshold: document.getElementById('preset-skill-threshold'),
    presetAutoBuyOverride: document.getElementById('preset-auto-buy-override'),
    presetEditSkillsBtn: document.getElementById('preset-edit-skills-btn'),
    skillModal: document.getElementById('skill-modal'),
    skillSearch: document.getElementById('skill-search'),
    skillList: document.getElementById('skill-list'),
    skillTiersContainer: document.getElementById('skill-tiers-container'),
    skillBlacklistContainer: document.getElementById('skill-blacklist-container'),
    skillMandatoryContainer: document.getElementById('skill-mandatory-container'),
    skillAddTierBtn: document.getElementById('skill-add-tier-btn'),
    skillModalClose: document.getElementById('skill-modal-close')
};
        const delaySettingsStorageKey = 'uma_turn_delay_settings';
        const burnClocksStorageKey = 'uma_burn_clocks';
        // ── Dev / loop controls ────────────────────────────────────────────────
        function syncDevControls() {
            if (!els.devBtn) return;
            els.devBtn.classList.toggle('is-active', state.devEnabled);
            els.devBtn.innerText = state.devEnabled ? 'LOOP: ON' : 'LOOP: OFF';
            els.devBtn.style.cursor = 'pointer';
            els.devBtn.title = 'Toggle auto-loop after each career';
        }

        function setDevEnabled(enabled, opts) {
            state.devEnabled = !!enabled;
            syncDevControls();

        // ── Stop-after-N careers control ──────────────────────────────────────
        const careersLimitBtn = document.getElementById('careers-limit-btn');
        const CAREERS_LIMIT_OPTIONS = [0, 1, 2, 3, 5, 10]; // 0 = infinite

        function syncCareersLimitBtn() {
            if (!careersLimitBtn) return;
            const n = state.careersLimit;
            careersLimitBtn.innerText = n > 0 ? `STOP: ${n}` : 'STOP: ∞';
            careersLimitBtn.classList.toggle('is-active', n > 0);
        }

        try {
            const saved = localStorage.getItem('uma_careers_limit');
            if (saved !== null) state.careersLimit = parseInt(saved, 10) || 0;
        } catch(e) {}
        syncCareersLimitBtn();

        if (careersLimitBtn) careersLimitBtn.addEventListener('click', () => {
            const idx = CAREERS_LIMIT_OPTIONS.indexOf(state.careersLimit);
            state.careersLimit = CAREERS_LIMIT_OPTIONS[(idx + 1) % CAREERS_LIMIT_OPTIONS.length];
            try { localStorage.setItem('uma_careers_limit', String(state.careersLimit)); } catch(e) {}
            syncCareersLimitBtn();
        });

        function checkCareersLimit(careersCount) {
            if (!state.devEnabled || state.careersLimit <= 0) return;
            const done = careersCount - state.sessionCareersStart;
            if (done >= state.careersLimit) {
                setDevEnabled(false, { persist: true });
                console.log(`[loop] career limit ${state.careersLimit} reached — loop disabled`);
            }
        }
            if (opts && opts.persist) {
                try { localStorage.setItem('uma_loop_enabled', state.devEnabled ? '1' : '0'); } catch(e) {}
            }
        }

        // Restore persisted loop preference
        try {
            const saved = localStorage.getItem('uma_loop_enabled');
            if (saved !== null) state.devEnabled = saved === '1';
        } catch(e) {}

        if (els.devBtn) els.devBtn.addEventListener('click', () => {
            setDevEnabled(!state.devEnabled, { persist: true });
        });

        syncDevControls();

        function setLoadingScreen(visible) {
            if (!els.loadingScreen) return;
            els.loadingScreen.classList.toggle('hidden', !visible);
        }
        function hideNavbar() {
            document.body.classList.add('pre-login');
            if (els.brandMark) els.brandMark.classList.remove('is-entrance');
        }
        function showNavbar() {
            document.body.classList.remove('pre-login');
        }
        function playBrandIntro() {
            if (!els.brandMark) return;
            els.brandMark.classList.remove('is-entrance');
            void els.brandMark.offsetWidth;
            els.brandMark.classList.add('is-entrance');
            window.setTimeout(() => els.brandMark.classList.remove('is-entrance'), 950);
        }
        hideNavbar();
        function syncDashboardHeight() {
            const navbar = document.querySelector('.navbar');
            const navbarHeight = navbar ? navbar.getBoundingClientRect().height : 0;
            const availableHeight = Math.max(360, Math.floor(window.innerHeight - navbarHeight));
            document.documentElement.style.setProperty('--dashboard-height', `${availableHeight}px`);
            syncDashboardCollapseState(false);
        }
        window.addEventListener('resize', syncDashboardHeight);
        window.addEventListener('orientationchange', syncDashboardHeight);
        syncDashboardHeight();
        const panelToggleSyncers = [];
        const dashboardMotion = window.matchMedia('(prefers-reduced-motion: reduce)');
        let dashboardLayoutAnimation = 0;
        const dashboardAnimationMs = 420;
        function isCompactDashboard() {
            return window.matchMedia('(max-width: 850px)').matches;
        }
        function getPanelLayoutTarget(setupCollapsed, contentCollapsed) {
            const compact = isCompactDashboard();
            const gutter = document.querySelector('.split-gutter-controls');
            const dashboardRect = els.dashboardView.getBoundingClientRect();
            const gutterRect = gutter.getBoundingClientRect();
            const gutterSize = compact ? gutterRect.height : gutterRect.width;
            const available = Math.max(0, (compact ? dashboardRect.height : dashboardRect.width) - gutterSize);
            if (compact) {
                const setupSize = setupCollapsed ? 0 : contentCollapsed ? available : available * 0.34;
                const contentSize = contentCollapsed ? 0 : setupCollapsed ? available : Math.max(340, available - setupSize);
                return { compact, gutterSize, setupSize, contentSize };
            }
            const setupSize = setupCollapsed ? 0 : contentCollapsed ? available : Math.min(available * 0.62, available - 340);
            const contentSize = contentCollapsed ? 0 : setupCollapsed ? available : Math.max(340, available - setupSize);
            return { compact, gutterSize, setupSize, contentSize };
        }
        function setDashboardTemplate(layout, setupSize, contentSize) {
            const safeSetup = Math.max(0, setupSize);
            const safeContent = Math.max(0, contentSize);
            if (layout.compact) {
                els.dashboardView.style.gridTemplateColumns = '';
                els.dashboardView.style.gridTemplateRows = `${safeSetup}px ${layout.gutterSize}px ${safeContent}px`;
            } else {
                els.dashboardView.style.gridTemplateRows = '';
                els.dashboardView.style.gridTemplateColumns = `${safeSetup}px ${layout.gutterSize}px ${safeContent}px`;
            }
        }
        function easeDashboardLayout(t) {
            return t < 0.5 ? 4 * t * t * t : 1 - Math.pow(-2 * t + 2, 3) / 2;
        }
        function syncDashboardCollapseState(animate = false) {
            const setupPanel = document.getElementById('setup-panel');
            const contentPanel = document.getElementById('content-panel');
            if (!setupPanel || !contentPanel || !els.dashboardView) return;
            if (setupPanel.classList.contains('collapsed') && contentPanel.classList.contains('collapsed')) {
                contentPanel.classList.remove('collapsed');
            }
            const setupCollapsed = setupPanel.classList.contains('collapsed');
            const contentCollapsed = contentPanel.classList.contains('collapsed');
            els.dashboardView.classList.toggle('setup-collapsed', setupCollapsed);
            els.dashboardView.classList.toggle('content-collapsed', contentCollapsed);
            if (!els.dashboardView.classList.contains('active')) return;
            const layout = getPanelLayoutTarget(setupCollapsed, contentCollapsed);
            if (dashboardLayoutAnimation) {
                cancelAnimationFrame(dashboardLayoutAnimation);
                dashboardLayoutAnimation = 0;
            }
            els.dashboardView.style.transition = 'none';
            if (!animate || dashboardMotion.matches) {
                setDashboardTemplate(layout, layout.setupSize, layout.contentSize);
                return;
            }
            const compact = layout.compact;
            const setupRect = setupPanel.getBoundingClientRect();
            const contentRect = contentPanel.getBoundingClientRect();
            const startSetup = compact ? setupRect.height : setupRect.width;
            const startContent = compact ? contentRect.height : contentRect.width;
            const targetSetup = layout.setupSize;
            const targetContent = layout.contentSize;
            if (Math.abs(startSetup - targetSetup) < 0.5 && Math.abs(startContent - targetContent) < 0.5) {
                setDashboardTemplate(layout, targetSetup, targetContent);
                return;
            }
            const startedAt = performance.now();
            const step = now => {
                const t = Math.min(1, (now - startedAt) / dashboardAnimationMs);
                const eased = easeDashboardLayout(t);
                setDashboardTemplate(
                    layout,
                    startSetup + (targetSetup - startSetup) * eased,
                    startContent + (targetContent - startContent) * eased
                );
                if (t < 1) {
                    dashboardLayoutAnimation = requestAnimationFrame(step);
                } else {
                    setDashboardTemplate(layout, targetSetup, targetContent);
                    dashboardLayoutAnimation = 0;
                }
            };
            setDashboardTemplate(layout, startSetup, startContent);
            dashboardLayoutAnimation = requestAnimationFrame(step);
        }
        function syncPanelToggleButtons() {
            panelToggleSyncers.forEach(sync => sync());
        }
        function makePanelToggle(panelId, btnId, collapseIcon, expandIcon) {
            const panel = document.getElementById(panelId);
            const btn = document.getElementById(btnId);
            const label = (btn.dataset.panelLabel || 'panel').toLowerCase();
            const renderChevrons = icon => `
                <span class="panel-collapse-btn-chevron-stack" aria-hidden="true">
                    <span>${icon}</span>
                    <span>${icon}</span>
                    <span>${icon}</span>
                </span>
            `;
            const syncButton = () => {
                const isCollapsed = panel.classList.contains('collapsed');
                const icon = isCollapsed ? expandIcon : collapseIcon;
                btn.classList.toggle('is-collapsed', isCollapsed);
                btn.innerHTML = renderChevrons(icon);
                btn.setAttribute('title', `${isCollapsed ? 'Expand' : 'Collapse'} ${label}`);
                btn.setAttribute('aria-label', `${isCollapsed ? 'Expand' : 'Collapse'} ${label}`);
                btn.setAttribute('aria-expanded', String(!isCollapsed));
            };
            panelToggleSyncers.push(syncButton);
            btn.addEventListener('click', () => {
                panel.classList.toggle('collapsed');
                syncDashboardCollapseState(true);
                syncPanelToggleButtons();
            });
            syncDashboardCollapseState(false);
            syncButton();
        }
        makePanelToggle('setup-panel',   'setup-collapse-btn',   '&lt;', '&gt;');
        makePanelToggle('content-panel', 'content-collapse-btn', '&gt;', '&lt;');
        function makeSectionToggle(toggleId, chevronId, bodyId, startExpanded) {
            const toggle  = document.getElementById(toggleId);
            const chevron = document.getElementById(chevronId);
            const body    = document.getElementById(bodyId);
            if (!toggle || !body) return;
            const setInitial = () => {
                const expanded = body.classList.contains('expanded');
                body.style.height = expanded ? 'auto' : '0px';
                chevron.classList.toggle('expanded', expanded);
            };
            const expand = () => {
                body.classList.add('expanded');
                chevron.classList.add('expanded');
                body.style.height = '0px';
                body.offsetHeight;
                body.style.height = `${body.scrollHeight}px`;
            };
            const collapse = () => {
                body.style.height = `${body.scrollHeight}px`;
                body.offsetHeight;
                body.classList.remove('expanded');
                chevron.classList.remove('expanded');
                body.style.height = '0px';
            };
            body.addEventListener('transitionend', event => {
                if (event.propertyName === 'height' && body.classList.contains('expanded')) body.style.height = 'auto';
            });
            toggle.addEventListener('click', () => {
                if (body.classList.contains('expanded')) collapse();
                else expand();
            });
            setInitial();
        }
        makeSectionToggle('decks-toggle',    'decks-chevron',    'decks-body',    true);
        makeSectionToggle('friends-toggle',  'friends-chevron',  'friends-body',  true);
        makeSectionToggle('trainees-toggle', 'trainees-chevron', 'trainees-body', true);
        makeSectionToggle('parents-toggle',  'parents-chevron',  'parents-body',  true);
        makeSectionToggle('cards-toggle',    'cards-chevron',    'card-grid-wrapper', true);
        makeSectionToggle('follow-parents-toggle', 'follow-parents-chevron', 'follow-parents-body', false);

        // ── Tab switching ──
        const TAB_STORAGE_KEY = 'uma_active_tab';
        function switchTab(tabId) {
            document.querySelectorAll('.panel-tab').forEach(btn => {
                btn.classList.toggle('is-active', btn.dataset.tab === tabId);
            });
            document.querySelectorAll('.tab-pane').forEach(pane => {
                pane.classList.toggle('is-active', pane.id === tabId);
            });
            try { localStorage.setItem(TAB_STORAGE_KEY, tabId); } catch(e) {}
        }
        document.querySelectorAll('.panel-tab').forEach(btn => {
            btn.addEventListener('click', () => {
                switchTab(btn.dataset.tab);
                if (btn.dataset.tab === 'tab-stats') fetchAndRenderFanStats();
            });
        });
        const savedTab = (() => { try { return localStorage.getItem(TAB_STORAGE_KEY); } catch(e) { return null; } })();
        if (savedTab && document.getElementById(savedTab)) switchTab(savedTab);

        // ══════════════════════════════════════════════════════════════
        // MODE SWITCHING (SETUP / DIAGNOSTICS)
        // ══════════════════════════════════════════════════════════════
        const MODE_KEY = 'uma_active_mode';
        let _sessionStartTime = Date.now();
        let _diagMetricsTimer = 0;

        // ── Mode switching (SETUP ↔ DIAGNOSTICS) ──────────────────────────────
        function switchMode(mode) {
            document.querySelectorAll('.mode-btn').forEach(btn => {
                btn.classList.toggle('is-active', btn.dataset.mode === mode);
            });
            document.querySelectorAll('.mode-pane').forEach(pane => {
                pane.classList.toggle('is-active', pane.id === 'mode-' + mode);
            });
            try { localStorage.setItem(MODE_KEY, mode); } catch(e) {}
            if (mode === 'diagnostics') {
                refreshDiagnostics();
                startDiagMetricsTimer();
            } else {
                stopDiagMetricsTimer();
            }
        }

        function startDiagMetricsTimer() {
            if (_diagMetricsTimer) return;
            _diagMetricsTimer = setInterval(updateDiagMetrics, 5000);
        }
        function stopDiagMetricsTimer() {
            if (_diagMetricsTimer) { clearInterval(_diagMetricsTimer); _diagMetricsTimer = 0; }
        }

        document.querySelectorAll('.mode-btn').forEach(btn => {
            btn.addEventListener('click', () => switchMode(btn.dataset.mode));
        });
        // Restore last mode
        try {
            const savedMode = localStorage.getItem(MODE_KEY);
            if (savedMode) switchMode(savedMode);
        } catch(e) {}

        // ── Diagnostics rendering ──────────────────────────────────────
        const MOOD_LABELS = { 1: 'BAD', 2: 'NORMAL', 3: 'GOOD', 4: 'GREAT', 5: 'SUPER' };

        function fmtFans(n) {
            n = Number(n || 0);
            if (n >= 1000000) return (n / 1000000).toFixed(2) + 'M';
            if (n >= 1000) return (n / 1000).toFixed(1) + 'K';
            return n.toLocaleString();
        }
        function fmtRuntime(ms) {
            const s = Math.floor(ms / 1000);
            if (s < 60) return s + 's';
            const m = Math.floor(s / 60), rs = s % 60;
            if (m < 60) return m + 'm ' + rs + 's';
            return Math.floor(m / 60) + 'h ' + (m % 60) + 'm';
        }

        function renderDiagCareer(runner, account, selection) {
            const card = document.getElementById('diag-career-card');
            const body = document.getElementById('diag-career-body');
            const badge = document.getElementById('diag-running-badge');
            if (!card || !body) return;

            const isRunning = runner && runner.running;
            const career = account && account.career;
            if (badge) badge.style.display = isRunning ? '' : 'none';

            if (!career || !career.active) {
                body.innerHTML = '<div class="diag-career-empty">No active career</div>';
                return;
            }

            // ── Core stats ──────────────────────────────────────────────────
            const hist = (runner && runner.action_history) || [];
            const lastRow = hist.length ? hist[hist.length - 1] : null;
            const stats = (lastRow && lastRow.stats) || {};
            const turn = (runner && runner.turn) || 0;
            const fans = career.fans || 0;
            const hp = stats.hp != null ? stats.hp : (career.vital || 0);
            const maxHp = stats.max_hp != null ? stats.max_hp : (career.max_vital || 100);
            const hpPct = maxHp > 0 ? Math.min(100, Math.round(hp / maxHp * 100)) : 0;
            const hpColor = hpPct > 60 ? '#22c55e' : hpPct > 30 ? '#f59e0b' : '#ef4444';
            const mood = stats.motivation != null ? stats.motivation : 3;
            const moodLabel = MOOD_LABELS[mood] || 'GOOD';
            const cardId = career.card_id || '';
            const charaName = career.name || 'Unknown';

            // ── Setup portraits (deck / friend / parents) ────────────────────
            const sel = selection || {};
            const deckImgs = (sel.deck && sel.deck.cards || [])
                .map(c => `<img class="diag-setup-thumb" src="/api/images/${escapeAttr(String(c.id||'10001'))}.png" onerror="this.style.display='none'" title="${escapeAttr(c.name||'')}">`)
                .join('');
            const friendImg = sel.friend
                ? `<img class="diag-setup-thumb" src="/api/images/${escapeAttr(String(sel.friend.support_card_id||'10001'))}.png" onerror="this.style.display='none'" title="${escapeAttr(sel.friend.support_name||'Friend')}">`
                : '';
            const parentImgs = [
                ...(sel.veterans || []).map(v => `<img class="diag-setup-thumb diag-setup-parent" src="/api/images/${escapeAttr(String(v.card_id||'100101'))}.png" onerror="this.style.display='none'" title="${escapeAttr(v.name||'Parent')}">`),
                sel.guestParent ? `<img class="diag-setup-thumb diag-setup-parent diag-setup-guest" src="/api/images/${escapeAttr(String(sel.guestParent.card_id||'100101'))}.png" onerror="this.style.display='none'" title="${escapeAttr(sel.guestParent.name||'Guest')}">` : ''
            ].join('');
            const setupHtml = (deckImgs || friendImg || parentImgs) ? `
                <div class="diag-setup-row">
                    <div class="diag-setup-group">${deckImgs}</div>
                    ${friendImg ? `<div class="diag-setup-sep"></div><div class="diag-setup-group">${friendImg}</div>` : ''}
                    ${parentImgs ? `<div class="diag-setup-sep"></div><div class="diag-setup-group">${parentImgs}</div>` : ''}
                </div>` : '';

            // ── Last action debrief ──────────────────────────────────────────
            const debrief = lastRow || {};
            const debriefAction = debrief.action || '—';
            const debriefFacility = debrief.facility || '';
            const debriefOutcome = debrief.outcome;   // 'ok','failed','win','top3','lost'
            const debriefItems = debrief.items || [];
            const debriefFailRate = debrief.failure_rate;
            const debriefRank = debrief.rank;
            const debriefDelta = debrief.stat_delta;

            // Outcome badge
            const outcomeColor = { ok:'#22c55e', failed:'#ef4444', win:'#22c55e', top3:'#f59e0b', lost:'#ef4444' };
            const outcomeLabel = { ok:'✓ OK', failed:'✗ FAILED', win:'🥇 WIN', top3:'🏅 TOP 3', lost:'✗ LOST' };
            const outcomeBadge = debriefOutcome
                ? `<span style="color:${outcomeColor[debriefOutcome]||'#aaa'};font-weight:700;font-size:0.75rem;">${outcomeLabel[debriefOutcome]||debriefOutcome}</span>`
                : '';

            // Build debrief lines
            const debriefLines = [];
            if (debriefAction === 'train' || debriefAction === 'command') {
                if (debriefFailRate != null) debriefLines.push(`Failure risk: ${debriefFailRate}%`);
                if (debriefDelta != null)    debriefLines.push(`Stat gain: +${debriefDelta}`);
            }
            if (debriefAction === 'race' && debriefRank != null && debriefRank < 99) {
                debriefLines.push(`Finished rank: ${debriefRank}`);
            }
            if (debriefItems.length) debriefLines.push(`Items: ${debriefItems.join(', ')}`);

            const traceTable = (debrief.turn != null) ? `
                <div class="diag-debrief-box">
                    <div class="diag-debrief-header">
                        <span class="diag-debrief-title">${escapeHtml((debriefAction + (debriefFacility ? ' · ' + debriefFacility : '')).toUpperCase())}</span>
                        ${outcomeBadge}
                    </div>
                    ${debriefLines.map(l => `<div class="diag-debrief-line">${escapeHtml(l)}</div>`).join('')}
                </div>` : '';
            const traceReason = '';

            body.innerHTML = `
                <div class="diag-chara-row">
                    <img class="diag-chara-portrait" src="/api/images/${escapeAttr(String(cardId))}" alt="" onerror="this.style.display='none'">
                    <div class="diag-chara-info">
                        <div class="diag-chara-name">${escapeHtml(charaName)}</div>
                        <div class="diag-chara-sub">Turn ${turn} / 78 · ${fmtFans(fans)} fans</div>
                        <span class="diag-mood-badge diag-mood-${mood}">${moodLabel}</span>
                    </div>
                </div>
                <div class="diag-hp-row">
                    <div class="diag-hp-bar-track">
                        <div class="diag-hp-bar-fill" style="width:${hpPct}%;background:${hpColor};"></div>
                    </div>
                    <span class="diag-hp-label">HP ${hp}/${maxHp}</span>
                </div>
                <div class="diag-stat-grid">
                    ${['speed','stamina','power','guts','wit','skill_point'].map((k, i) => {
                        const labels = ['SPD','STA','PWR','GUT','WIT','SP'];
                        return `<div class="diag-stat-box">
                            <span class="diag-stat-label">${labels[i]}</span>
                            <span class="diag-stat-value">${stats[k] != null ? stats[k] : '—'}</span>
                        </div>`;
                    }).join('')}
                </div>
                ${setupHtml}
                ${traceReason}
                ${traceTable}
            `;

            // Footer labels
            const footerLabel = document.getElementById('diag-footer-label');
            if (footerLabel) footerLabel.textContent = `TURN ${turn} / RACE ${hist.filter(r => r.action === 'race').length}`;
            const turnBadge = document.getElementById('diag-turn-badge');
            if (turnBadge) turnBadge.textContent = turn + ' TURNS';
        }

        function renderDiagLog(runner) {
            const logEl = document.getElementById('diag-action-log');
            if (!logEl) return;
            const hist = (runner && runner.action_history) || [];
            if (!hist.length) { logEl.innerHTML = '<div style="padding:1rem;opacity:0.35;font-size:0.75rem;">No actions yet.</div>'; return; }
            const rows = [...hist].reverse().slice(0, 80); // newest first, max 80
            const body = rows.map(row => {
                const norm = normalizeHistoryAction(row);
                const s = row.stats || {};
                const hp = s.hp != null ? `${s.hp}/${s.max_hp ?? 100}` : '—';
                const statCells = ['speed','stamina','power','guts','wit','skill_point'].map(k =>
                    `<td class="diag-log-stat">${s[k] != null ? s[k] : '—'}</td>`
                ).join('');
                return `<tr>
                    <td>${escapeHtml(String(row.turn))}</td>
                    <td><span class="action-pill action-pill-${escapeAttr(norm.action)}">${escapeHtml(norm.action.toUpperCase())}</span></td>
                    <td>${escapeHtml(row.facility || '')}</td>
                    ${statCells}
                    <td class="diag-log-stat diag-log-hp">${escapeHtml(hp)}</td>
                </tr>`;
            }).join('');
            logEl.innerHTML = `<table>
                <thead><tr>
                    <th>TRN</th><th>ACTION</th><th>FACILITY</th>
                    <th>SPD</th><th>STA</th><th>PWR</th><th>GUT</th><th>WIT</th><th>SP</th><th>HP</th>
                </tr></thead>
                <tbody>${body}</tbody>
            </table>`;
            logEl.scrollTop = 0;
        }

        let _circleStatsFetched = false;
        async function fetchCircleStats(refresh = false) {
            try {
                const data = await apiJson(`/api/stats/circle${refresh ? '?refresh=true' : ''}`);
                const clubEl = document.getElementById('diag-club-display');
                if (!clubEl) return;
                if (!data.success || !data.circle) {
                    clubEl.textContent = '—';
                    return;
                }
                const c = data.circle;
                const members = c.member_num ?? c.member_count;
                clubEl.innerHTML = [
                    c.name    ? `<div class="diag-metric-row"><span class="diag-metric-label">NAME</span><span class="diag-metric-value">${c.name}</span></div>` : '',
                    c.rank != null ? `<div class="diag-metric-row"><span class="diag-metric-label">RANK</span><span class="diag-metric-value">#${c.rank}</span></div>` : '',
                    c.score != null ? `<div class="diag-metric-row"><span class="diag-metric-label">SCORE</span><span class="diag-metric-value">${fmtFans(c.score)}</span></div>` : '',
                    members != null ? `<div class="diag-metric-row"><span class="diag-metric-label">MEMBERS</span><span class="diag-metric-value">${members}</span></div>` : '',
                    c.comment ? `<div class="diag-metric-row"><span class="diag-metric-label">INFO</span><span class="diag-metric-value" style="font-size:0.8em;opacity:0.8">${c.comment}</span></div>` : '',
                ].filter(Boolean).join('') || '<span style="opacity:0.5">No data</span>';
            } catch(e) {}
        }
        async function updateDiagMetrics() {
            try {
                const data = await apiJson('/api/stats/fans');
                const el = id => document.getElementById(id);
                const elapsed = Date.now() - _sessionStartTime;
                if (el('diag-total-fans')) el('diag-total-fans').textContent = fmtFans(Number(data.total_gained || 0));
                if (el('diag-current-fans')) el('diag-current-fans').textContent = data.current_fans != null ? fmtFans(Number(data.current_fans)) : '—';
                if (el('diag-runtime')) el('diag-runtime').textContent = fmtRuntime(elapsed);
                if (el('diag-careers-done')) el('diag-careers-done').textContent = data.careers_count || 0;

                // Club stats: fetch once on first open (backend caches for 5 min)
                if (!_circleStatsFetched) {
                    _circleStatsFetched = true;
                    fetchCircleStats();
                }
            } catch(e) {}
        }

        async function refreshDiagnostics() {
            try {
                const data = await apiJson('/api/career/runner');
                const runner = (data.success && data.runner) ? data.runner : null;
                // Use the fresh account from the runner endpoint so the career card
                // always reflects the current run, even mid-loop between careers.
                const account = data.account || state.account;
                if (data.account) { state.account = data.account; renderAccountStrip(data.account); }
                renderDiagCareer(runner, account, data.selection);
                renderDiagLog(runner);
            } catch(e) {}
            updateDiagMetrics();
        }

        // Wire diagnostics footer buttons
        const diagResumeBtn = document.getElementById('diag-resume-btn');
        const diagStopBtn = document.getElementById('diag-stop-btn');
        const diagSyncBtn = document.getElementById('diag-sync-btn');

        if (diagResumeBtn) diagResumeBtn.addEventListener('click', () => {
            // Switch to setup and click run
            switchMode('setup');
            const runBtn = document.getElementById('start-career-btn');
            if (runBtn && !runBtn.disabled) runBtn.click();
        });
        if (diagStopBtn) diagStopBtn.addEventListener('click', async () => {
            try {
                await apiJson('/api/career/runner/stop', { method: 'POST', headers: {'Content-Type':'application/json'}, body: '{}' });
                setTimeout(refreshDiagnostics, 500);
            } catch(e) {}
        });
        const diagGiveUpBtn = document.getElementById('diag-give-up-btn');
        if (diagGiveUpBtn) diagGiveUpBtn.addEventListener('click', async () => {
            if (!confirm('Give up and permanently abandon the current career? This cannot be undone.')) return;
            diagGiveUpBtn.disabled = true;
            diagGiveUpBtn.textContent = 'GIVING UP…';
            try {
                const data = await apiJson('/api/career/give-up', { method: 'POST', headers: {'Content-Type':'application/json'}, body: '{}' });
                if (!data.success) throw new Error(data.detail || 'Give up failed');
                if (data.account) {
                    renderAccountStrip(data.account);
                    state.account = data.account;
                    if (dashData) dashData.account = data.account;
                }
                // Stop runner polling and reset setup-tab UI
                if (state.runnerTimer) { bgClearTimer(state.runnerTimer); state.runnerTimer = 0; }
                state.runner = null;
                syncStartButton();
                renderTeamPanel();
                refreshDiagnostics();
            } catch(e) {
                alert(e.message || 'Give up failed');
            } finally {
                diagGiveUpBtn.disabled = false;
                diagGiveUpBtn.textContent = 'GIVE UP';
            }
        });
        if (diagSyncBtn) diagSyncBtn.addEventListener('click', async () => {
            try {
                const data = await apiJson('/api/status');
                if (data.account) { renderAccountStrip(data.account); state.account = data.account; }
                refreshDiagnostics();
            } catch(e) { refreshDiagnostics(); }
        });

        // Club info is fetched automatically via updateDiagMetrics() → /api/stats/fans circle_info

        // ══════════════════════════════════════════════════════════════

        const applyTheme = theme => {
            const nextTheme = theme === 'blue' ? 'blue' : 'pink';
            document.documentElement.dataset.theme = nextTheme;
            document.documentElement.classList.toggle('theme-blue', nextTheme === 'blue');
            document.body.classList.toggle('theme-blue', nextTheme === 'blue');
            return nextTheme;
        };
        applyTheme(localStorage.getItem('theme'));
        const savedUsername = localStorage.getItem('saved_username');
        const savedPassword = localStorage.getItem('saved_password');
        if (savedUsername) document.getElementById('username').value = savedUsername;
        if (savedPassword) document.getElementById('password').value = savedPassword;
        let themeToggleClicks = 0;
        els.themeToggle.addEventListener('click', () => {
            const nextTheme = document.body.classList.contains('theme-blue') ? 'pink' : 'blue';
            applyTheme(nextTheme);
            localStorage.setItem('theme', nextTheme);
            themeToggleClicks++;
            if (themeToggleClicks >= 11 && els.devBtn) {
                els.devBtn.style.display = 'inline-block';
            }
        });
        window.iwillnotabusethis = function() {
            if (els.devBtn) els.devBtn.style.display = 'inline-block';
            setDevEnabled(true, { persist: true });
        };
        const sleep = ms => new Promise(resolve => window.setTimeout(resolve, ms));
        const nextFrame = () => new Promise(resolve => requestAnimationFrame(resolve));
        // ── API helpers ──────────────────────────────────────────────────────────
        async function waitForDomPaint(frames = 2) {
            for (let i = 0; i < frames; i++) await nextFrame();
        }
        async function apiJson(url, options = {}) {
            const res = await fetch(url, options);
            return res.json();
        }
        function setMasterDataStatus(message, stateName = '') {
            if (!els.masterDataStatus) return;
            els.masterDataStatus.textContent = message || '';
            els.masterDataStatus.className = `master-data-status ${stateName}`.trim();
        }
        function applyMasterDataStatus(data) {
            if (!data) return;
            if (els.masterDataPath && data.master_mdb_path) {
                els.masterDataPath.value = data.master_mdb_path;
            }
            if (els.masterDataPath) {
                els.masterDataPath.classList.toggle('needs-action', !data.exists);
            }
            if (data.exists) {
                if (data.generation_error) {
                    setMasterDataStatus(data.generation_error, 'needs-action');
                } else if (data.generated) {
                    setMasterDataStatus('master.mdb found; data generated', 'ok');
                } else {
                    setMasterDataStatus('master.mdb found', 'ok');
                }
            } else {
                setMasterDataStatus(data.access_error || 'master.mdb not found; update the path', 'needs-action');
            }
        }
        async function loadMasterDataStatus() {
            if (!els.masterDataPath) return;
            try {
                applyMasterDataStatus(await apiJson('/api/master-data/status'));
            } catch (e) {
                setMasterDataStatus('Unable to read master data status', 'needs-action');
            }
        }
        async function saveMasterDataPath() {
            if (!els.masterDataPath) return null;
            const master_mdb_path = els.masterDataPath.value.trim();
            if (!master_mdb_path) {
                setMasterDataStatus('Enter the full path to master.mdb', 'needs-action');
                els.masterDataPath.classList.add('needs-action');
                return null;
            }
            if (els.masterDataSaveBtn) els.masterDataSaveBtn.disabled = true;
            setMasterDataStatus('Saving path and generating data...', 'working');
            const data = await apiJson('/api/master-data/path', {
                method: 'POST',
                headers: { 'Content-Type': 'application/json' },
                body: JSON.stringify({ master_mdb_path })
            });
            applyMasterDataStatus(data);
            if (data.exists && !data.generation_error) {
                await loadRaceData();
            }
            if (els.masterDataSaveBtn) els.masterDataSaveBtn.disabled = false;
            return data;
        }
        function bindMasterDataControls() {
            if (!els.masterDataPath) return;
            if (els.masterDataSaveBtn) {
                els.masterDataSaveBtn.addEventListener('click', async () => {
                    try {
                        await saveMasterDataPath();
                    } catch (e) {
                        setMasterDataStatus(e.message || 'Unable to save master.mdb path', 'needs-action');
                        if (els.masterDataPath) els.masterDataPath.classList.add('needs-action');
                    } finally {
                        if (els.masterDataSaveBtn) els.masterDataSaveBtn.disabled = false;
                    }
                });
            }
            els.masterDataPath.addEventListener('input', () => {
                els.masterDataPath.classList.remove('needs-action');
            });
            loadMasterDataStatus();
        }
        function writeLocalSetting(key, value) {
            try {
                localStorage.setItem(key, JSON.stringify(value));
            } catch (e) {}
        }
        function readLocalSetting(value, fallback = null) {
            if (!value) return fallback;
            try {
                return JSON.parse(value);
            } catch (e) {
                return fallback;
            }
        }
        function escapeHtml(value) {
            return String(value ?? '').replace(/[&<>"']/g, char => ({
                '&': '&amp;',
                '<': '&lt;',
                '>': '&gt;',
                '"': '&quot;',
                "'": '&#39;'
            }[char]));
        }
        function escapeAttr(value) {
            return escapeHtml(value);
        }
        function normalizeDelayBounds(min, max, disabled = false, restoreMin = null, restoreMax = null) {
            const fallbackMin = Number.isFinite(Number(restoreMin)) ? Number(restoreMin) : 1.6;
            const fallbackMax = Number.isFinite(Number(restoreMax)) ? Number(restoreMax) : 3.7;
            if (disabled) return { min: 0, max: 0, restoreMin: fallbackMin, restoreMax: fallbackMax, disabled: true };
            const left = Math.max(0, Number.isFinite(Number(min)) ? Number(min) : fallbackMin);
            let right = Math.max(0, Number.isFinite(Number(max)) ? Number(max) : fallbackMax);
            if (left > right) right = left;
            return { min: left, max: right, restoreMin: left, restoreMax: right, disabled: false };
        }
        function setDelayControls(settings) {
            if (!els.turnDelayMin || !els.turnDelayMax || !els.temptFateBtn) return;
            const disabled = Boolean(settings.disabled);
            const restoreMin = Number.isFinite(Number(settings.restoreMin)) ? Number(settings.restoreMin) : Number(settings.restore_min);
            const restoreMax = Number.isFinite(Number(settings.restoreMax)) ? Number(settings.restoreMax) : Number(settings.restore_max);
            els.turnDelayMin.value = String(settings.min);
            els.turnDelayMax.value = String(settings.max);
            els.turnDelayMin.dataset.restoreValue = String(Number.isFinite(restoreMin) ? restoreMin : settings.min);
            els.turnDelayMax.dataset.restoreValue = String(Number.isFinite(restoreMax) ? restoreMax : settings.max);
            els.turnDelayMin.disabled = disabled;
            els.turnDelayMax.disabled = disabled;
            els.temptFateBtn.classList.toggle('is-active', disabled);
            els.temptFateBtn.innerText = disabled ? 'FATE TEMPTED' : 'TEMPT FATE';
        }
        async function saveDelaySettings(settings) {
            setDelayControls(settings);
            const data = await apiJson('/api/settings/turn-delay', {
                method: 'POST',
                headers: { 'Content-Type': 'application/json' },
                body: JSON.stringify(settings)
            });
            const normalized = normalizeDelayBounds(data.min, data.max, data.disabled, data.restore_min, data.restore_max);
            setDelayControls(normalized);
            writeLocalSetting(delaySettingsStorageKey, normalized);
        }
        async function loadDelaySettings() {
            if (!els.turnDelayMin || !els.turnDelayMax || !els.temptFateBtn) return;
            try {
                const data = await apiJson('/api/settings/turn-delay');
                setDelayControls(normalizeDelayBounds(data.min, data.max, data.disabled, data.restore_min, data.restore_max));
            } catch (e) {
                setDelayControls({ min: 1.6, max: 3.7, restoreMin: 1.6, restoreMax: 3.7, disabled: false });
            }
        }
        function bindDelayControls() {
            if (!els.turnDelayMin || !els.turnDelayMax || !els.temptFateBtn) return;
            const sync = () => {
                saveDelaySettings(normalizeDelayBounds(els.turnDelayMin.value, els.turnDelayMax.value, false));
            };
            els.turnDelayMin.addEventListener('input', sync);
            els.turnDelayMax.addEventListener('input', sync);
            els.temptFateBtn.addEventListener('click', () => {
                const active = els.temptFateBtn.classList.contains('is-active');
                const restoreMin = Number(els.turnDelayMin.dataset.restoreValue || 1.6);
                const restoreMax = Number(els.turnDelayMax.dataset.restoreValue || 3.7);
                saveDelaySettings(active
                    ? normalizeDelayBounds(restoreMin, restoreMax, false)
                    : normalizeDelayBounds(0, 0, true, restoreMin, restoreMax)
                );
            });
            loadDelaySettings();
        }
        window.addEventListener('storage', event => {
            if (event.key !== delaySettingsStorageKey || !event.newValue) return;
            const settings = readLocalSetting(event.newValue);
            if (settings) setDelayControls(normalizeDelayBounds(settings.min, settings.max, settings.disabled, settings.restoreMin, settings.restoreMax));
        });
        window.addEventListener('storage', event => {
            if (event.key !== burnClocksStorageKey || !event.newValue) return;
            setBurnClocks(readLocalSetting(event.newValue, false));
        });
        // ── Login / auth ─────────────────────────────────────────────────────────
        function resetLoginState() {
            state.isLoading = false;
            els.loginBtn.innerText = state.needs2fa ? 'VALIDATE' : 'LOGIN';
        }
        function showLoginError(message) {
            setLoadingScreen(false);
            els.errorMsg.innerText = String(message || 'FAIL').toUpperCase();
            els.errorMsg.style.display = 'block';
            resetLoginState();
        }
        function showTwoFactorPrompt() {
            setLoadingScreen(false);
            state.needs2fa = true;
            state.isLoading = false;
            els.standardFields.style.display = 'none';
            els.faFields.style.display = 'block';
            els.loginBtn.innerText = 'VALIDATE';
            els.errorMsg.innerText = '2FA REQUIRED';
            els.errorMsg.style.display = 'block';
        }
        function readLoginPayload() {
            return {
                username: document.getElementById('username').value,
                password: document.getElementById('password').value,
                code: document.getElementById('code').value
            };
        }
        function resetSelection() {
            selection.deck = null;
            selection.friend = null;
            selection.trainee = null;
            selection.veterans = [];
            selection.guestParent = null;
        }
        function hideBrokenImage(img) {
            img.onerror = null;
            img.style.display = 'none';
        }
        const loginForm = document.getElementById('login-form');
        loginForm.addEventListener('submit', async event => {
            event.preventDefault();
            if (state.isLoading) return;
            state.isLoading = true;
            setLoadingScreen(true);
            els.loginBtn.innerText = 'WORKING...';
            els.errorMsg.style.display = 'none';
            const payload = readLoginPayload();
            try {
                const data = await apiJson('/api/login', {
                    method: 'POST',
                    headers: { 'Content-Type': 'application/json' },
                    body: JSON.stringify(payload)
                });
                if (data.needs_2fa) {
                    showTwoFactorPrompt();
                } else if (data.success) {
                    localStorage.setItem('saved_username', payload.username);
                    localStorage.setItem('saved_password', payload.password);
                    _circleStatsFetched = false; // reset so club info re-fetches on next login
                    await renderDashboard(data, { animateIntro: true, waitForIntro: true });
                    state.isLoading = false;
                } else {
                    showLoginError(data.detail || 'FAIL');
                }
            } catch (e) {
                showLoginError('NETWORK ERROR');
            }
        });

        els.logoutBtn.addEventListener('click', async () => {
            setLoadingScreen(false);
            try {
                await apiJson('/api/logout', { method: 'POST' });
            } catch (e) {}
            document.body.classList.remove('dashboard-mode');
            hideNavbar();
            els.loginView.style.display = 'flex';
            els.dashboardView.style.display = 'none';
            els.dashboardView.classList.remove('active');
            els.logoutBtn.style.display = 'none';
            els.standardFields.style.display = 'block';
            els.faFields.style.display = 'none';
            els.loginBtn.innerText = 'LOGIN';
            els.accountStrip.style.display = 'none';
            els.accountStrip.innerHTML = '';
            state.account = null;
            state.needs2fa = false;
            dashData = null;
            resetSelection();
            syncDashboardHeight();
            loginForm.reset();
        });

        const formatNumber = value => Number(value || 0).toLocaleString();
        // ── Career modal (delete / resume) ───────────────────────────────────────
        function closeCareerModal() {
            els.careerModal.style.display = 'none';
            els.careerModalCopy.innerText = 'This will force-delete the ongoing career.';
            els.careerDeleteBtn.innerText = 'DELETE';
            state.isDeletingCareer = false;
        }
        function openCareerModal() {
            const career = state.account && state.account.career;
            if (!career || !career.active) return;
            els.careerModalCopy.innerText = 'This will force-delete the ongoing career.';
            els.careerModal.style.display = 'flex';
        }
        async function deleteCareer() {
            const career = state.account && state.account.career;
            if (!career || !career.active || state.isDeletingCareer) return;
            state.isDeletingCareer = true;
            els.careerDeleteBtn.innerText = 'DELETING';
            els.careerModalCopy.innerText = 'Deleting ongoing career...';
            try {
                const data = await apiJson('/api/career/delete', {
                    method: 'POST',
                    headers: { 'Content-Type': 'application/json' },
                    body: JSON.stringify({ current_turn: career.turn || 0 })
                });
                if (!data.success) throw new Error(data.detail || 'Delete failed');
                renderAccountStrip(data.account);
                closeCareerModal();
            } catch (e) {
                els.careerModalCopy.innerText = e.message || 'Delete failed';
                els.careerDeleteBtn.innerText = 'RETRY';
                state.isDeletingCareer = false;
            }
        }
        els.careerCancelBtn.addEventListener('click', closeCareerModal);
        els.careerDeleteBtn.addEventListener('click', deleteCareer);
        els.careerModal.addEventListener('click', event => {
            if (event.target === els.careerModal) closeCareerModal();
        });
        // ── Burn clocks controls ─────────────────────────────────────────────────
        function syncBurnClocksControls() {
            if (!els.burnClocksBtn) return;
            const clocks = state.account ? Number(state.account.clocks || 0) : 0;
            const disabled = clocks <= 11;

            if (disabled) {
                state.burnClocks = false;
                els.burnClocksBtn.disabled = true;
                els.burnClocksBtn.classList.remove('is-active');
                els.burnClocksBtn.innerText = `BURN CLOCKS: LOW (${clocks})`;
            } else {
                els.burnClocksBtn.disabled = false;
                els.burnClocksBtn.classList.toggle('is-active', state.burnClocks);
                els.burnClocksBtn.innerText = `BURN CLOCKS: ${state.burnClocks ? 'ON' : 'OFF'}`;
            }
        }
        function setBurnClocks(value, options = {}) {
            state.burnClocks = Boolean(value);
            syncBurnClocksControls();
            if (options.persist) writeLocalSetting(burnClocksStorageKey, state.burnClocks);
        }
        function loadStoredBurnClocks() {
            if (state.runner && state.runner.running) return;
            const stored = readLocalSetting(localStorage.getItem(burnClocksStorageKey));
            if (stored !== null) setBurnClocks(stored);
        }

        // ── Account strip ────────────────────────────────────────────────────────
        function renderAccountStrip(account) {
            state.account = account || null;
            if (!account) {
                els.accountStrip.style.display = 'none';
                els.accountStrip.innerHTML = '';
                return;
            }
            const tp = account.tp || {};
            const career = account.career;
            const careerHtml = career && career.active ? `
                <div id="career-pill" class="account-pill pill-career account-pill-clickable">
                    <span class="label">CAREER</span>
                    <strong>ONGOING</strong>
                </div>
            ` : `<div class="account-pill" style="opacity: 0.25;">
                    <span class="label">CAREER</span>
                    <strong>NONE</strong>
                </div>`;
            const carrots = account.carrots || {};
            els.accountStrip.innerHTML = `
                <div class="account-pill pill-tp">
                    <span class="label">TP</span>
                    <strong>${tp.current || 0}/${tp.max || 0}</strong>
                </div>
                <div class="account-pill pill-carrots">
                    <span class="label">CARROTS</span>
                    <strong>${formatNumber(carrots.total)}</strong>
                </div>
                <div class="account-pill pill-gold">
                    <span class="label">GOLD</span>
                    <strong>${formatNumber(account.gold)}</strong>
                </div>
                <div class="account-pill pill-clk">
                    <span class="label">CLOCKS</span>
                    <strong>${formatNumber(account.clocks)}</strong>
                </div>
                ${careerHtml}
            `;
            els.accountStrip.style.display = 'flex';
            const careerPill = document.getElementById('career-pill');
            if (careerPill) careerPill.addEventListener('click', openCareerModal);
            loadStoredBurnClocks();
            syncBurnClocksControls();
        }

        els.burnClocksBtn.addEventListener('click', async () => {
            setBurnClocks(!state.burnClocks, { persist: true });
            if (state.runner && state.runner.running) {
                try {
                    const data = await apiJson('/api/career/runner/burn_clocks', {
                        method: 'POST',
                        headers: { 'Content-Type': 'application/json' },
                        body: JSON.stringify({ burn_clocks: state.burnClocks })
                    });
                    if (!data.success) throw new Error(data.detail || 'Failed to update burn_clocks');
                    if (data.runner) applyRunnerSnapshot(data.runner);
                } catch (e) {
                    console.error("Failed to update burn_clocks mid-run", e);
                    if (state.runner && state.runner.burn_clocks !== undefined) {
                        setBurnClocks(state.runner.burn_clocks, { persist: true });
                    }
                }
            }
        });

        const rankMap = {
            1: 'G', 2: 'G+', 3: 'F', 4: 'F+', 5: 'E', 6: 'E+',
            7: 'D', 8: 'D+', 9: 'C', 10: 'C+', 11: 'B', 12: 'B+',
            13: 'A', 14: 'A+', 15: 'S', 16: 'S+', 17: 'SS', 18: 'SS+',
            19: 'UG', 20: 'UF', 21: 'UE', 22: 'UD'
        };
        let dashData = null;
        const selection = { deck: null, friend: null, trainee: null, veterans: [], rentalParent: null, guestParent: null };

        // ── Selection sync ───────────────────────────────────────────────────────
        async function syncSelectionToServer() {
            try {
                const payload = {
                    deck: selection.deck,
                    friend: selection.friend,
                    trainee: selection.trainee,
                    veterans: selection.veterans,
                    guestParent: selection.guestParent || null
                };
                await apiJson('/api/selection', {
                    method: 'POST',
                    headers: { 'Content-Type': 'application/json' },
                    body: JSON.stringify({ selection: payload })
                });
            } catch (e) {}
        }

        function deselect(action, idx) {
            if (action === 'deck') {
                document.querySelectorAll('.deck-container.selected').forEach(el => el.classList.remove('selected'));
                selection.deck = null;
            } else if (action === 'friend') {
                document.querySelectorAll('#friend-grid .grid-card.selected').forEach(el => el.classList.remove('selected'));
                selection.friend = null;
            } else if (action === 'trainee') {
                document.querySelectorAll('#uma-grid .grid-card.selected').forEach(el => el.classList.remove('selected'));
                selection.trainee = null;
            } else if (action === 'vet') {
                // Slot 2 may show guestParent when veterans[1] is empty
                if (idx === 1 && !selection.veterans[1] && selection.guestParent) {
                    const gp = selection.guestParent;
                    const gpEl = document.querySelector(
                        `#parent-grid .grid-card[data-idx="${gp._gridIdx}"], #follow-parent-grid .grid-card[data-follow-idx="${gp._gridIdx}"]`
                    );
                    if (gpEl) gpEl.classList.remove('selected');
                    selection.guestParent = null;
                } else {
                    const vet = selection.veterans[idx];
                    if (vet != null) {
                        const card = document.querySelectorAll('#parent-grid .grid-card')[vet._gridIdx];
                        if (card) card.classList.remove('selected');
                    }
                    selection.veterans.splice(idx, 1);
                }
                updateVetSelectability();
            }
            renderTeamPanel();
            syncSelectionToServer();
        }
        // ── Start validation ─────────────────────────────────────────────────────
        function getStartMissingReason() {
            const activeCareer = state.account && state.account.career && state.account.career.active;
            if (!state.selectedPreset) return 'Select a preset';
            if (activeCareer) return '';
            if (!selection.deck) return 'Select a deck';
            if (!selection.friend) return 'Select a friend support';
            if (!selection.trainee) return 'Select a trainee';
            if (selection.veterans.length < 2 && !selection.guestParent) return 'Select two veteran parents';
            if (selection.veterans.length < 1 && selection.guestParent) return 'Select one veteran parent';
            const parentError = getParentSelectionError();
            if (parentError) return parentError;
            const tp = state.account && state.account.tp ? Number(state.account.tp.current || 0) : 0;
            if (state.account && tp < 30 && !state.devEnabled) return `Not enough TP: ${tp}/30`;
            return '';
        }
        function getParentLineageCards(parent) {
            if (!parent || !parent.tree) return [];
            return ['self', 'p1', 'p2', 'gp1', 'gp2', 'gp3', 'gp4']
                .map(key => Number(parent.tree[key] && parent.tree[key].card_id))
                .filter(Boolean);
        }
        function getParentSelectionError() {
            if (!selection.trainee) return '';
            const traineeId = Number(selection.trainee.id);
            const allParents = [...selection.veterans];
            if (selection.guestParent) allParents.push(selection.guestParent);
            const lineages = allParents.map(getParentLineageCards);
            if (lineages.some(cards => cards[0] === traineeId)) return 'Direct parent is trainee';
            return '';
        }
        function syncStartButton() {
            const reason = getStartMissingReason();
            els.startCareerBtn.disabled = Boolean(reason) || state.isStartingCareer;
            if (state.isStartingCareer) {
                els.startCareerBtn.innerText = 'RUNNING...';
                els.startStatus.innerText = 'Starting runner...';
                els.startStatus.classList.remove('error');
            } else {
                const activeCareer = state.account && state.account.career && state.account.career.active;
                els.startCareerBtn.innerText = activeCareer ? 'RESUME CAREER' : 'RUN CAREER';
                els.startStatus.innerText = reason;
                els.startStatus.classList.toggle('error', false);
            }
        }
        // ── Team panel (selected trainee / deck / parents) ───────────────────────
        function renderTeamPanel() {
            document.getElementById('dashboard-view').classList.add('active');
            function setSlot(id, role, content, action, idx, emptyText = 'select') {
                const el = document.getElementById(id);
                el.className = content ? 'team-item filled' : 'team-item';
                el.onclick = content ? () => deselect(action, idx) : null;
                const clear = content ? '<span class="team-item-clear">clear</span>' : '';
                const empty = `<div class="team-item-empty">${emptyText}</div>`;
                el.innerHTML = `
                    <div class="team-item-head">
                        <span class="team-item-role">${role}</span>
                        ${clear}
                    </div>
                    ${content || empty}
                `;
            }
            if (selection.deck) {
                const thumbs = selection.deck.cards.map(c =>
                    `<img class="team-item-thumb" src="/api/images/${c.id || '10001'}.png" onerror="hideBrokenImage(this)">`
                ).join('');
                setSlot('team-slot-deck', 'Deck', `
                    <div class="team-item-body">
                        <div class="team-item-thumbs">${thumbs}</div>
                        <div class="team-item-text">
                            <span class="team-item-name">${selection.deck.name}</span>
                            <span class="team-item-sub">Slot ${selection.deck.id}</span>
                        </div>
                    </div>
                `, 'deck', null, 'select deck');
            } else {
                setSlot('team-slot-deck', 'Deck', null, 'deck', null, 'select deck');
            }
            if (selection.friend) {
                setSlot('team-slot-friend', 'Friend', `
                    <div class="team-item-body">
                        <img class="team-item-portrait" src="/api/images/${selection.friend.support_card_id || '10001'}.png" onerror="hideBrokenImage(this)">
                        <div class="team-item-text">
                            <span class="team-item-name">${selection.friend.support_name || 'Unknown'}</span>
                            <span class="team-item-sub">${selection.friend.type || '?'} | LB${selection.friend.limit_break_count ?? '?'}</span>
                        </div>
                    </div>
                `, 'friend', null, 'select friend');
            } else {
                setSlot('team-slot-friend', 'Friend', null, 'friend', null, 'select friend');
            }
            if (selection.trainee) {
                setSlot('team-slot-trainee', 'Trainee', `
                    <div class="team-item-body">
                        <img class="team-item-portrait" src="/api/images/${selection.trainee.id || '100101'}.png" onerror="hideBrokenImage(this)">
                        <div class="team-item-text">
                            <span class="team-item-name">${selection.trainee.name || 'Unknown'}</span>
                        </div>
                    </div>
                `, 'trainee', null, 'select trainee');
            } else {
                setSlot('team-slot-trainee', 'Trainee', null, 'trainee', null, 'select trainee');
            }
            const slotDefs = [
                { id: 'team-slot-vet1', parent: selection.veterans[0], label: 'Parent 1' },
                { id: 'team-slot-vet2', parent: selection.veterans[1] || selection.guestParent, label: selection.veterans[1] ? 'Parent 2' : 'Parent 2 (Follow)' },
            ];
            slotDefs.forEach(({ id, parent, label }, i) => {
                if (parent) {
                    const isFollow = parent.from_follow;
                    setSlot(id, label, `
                        <div class="team-item-body">
                            <img class="team-item-portrait" src="/api/images/${parent.card_id || '100101'}.png" onerror="hideBrokenImage(this)">
                            <div class="team-item-text">
                                <span class="team-item-name">${parent.name || 'Unknown'}</span>
                                <span class="team-item-sub">${isFollow ? ('FOLLOW · ' + (parent.owner_name || '')) : (rankMap[parent.rank] || '??')}</span>
                            </div>
                        </div>
                    `, 'vet', i, 'select parent');
                } else {
                    setSlot(id, label, null, 'vet', i, 'select parent');
                }
            });
            syncStartButton();
        }
                function updateVetSelectability() {
                    const full = selection.veterans.length >= 2;
                    document.querySelectorAll('#parent-grid .grid-card, #follow-parent-grid .grid-card').forEach(card => {
                        const idx = parseInt(card.getAttribute('data-idx') || card.getAttribute('data-follow-idx') || '-1');
                        const p = dashData.parents[idx];
                        const isRental = p && (p.is_guest || p.from_follow);
                        if (card.classList.contains('selected') || isRental) {
                            card.classList.remove('vet-full');
                        } else {
                            card.classList.toggle('vet-full', full);
                        }
                    });
                    syncStartButton();
                }
        function clampValue(value, min, max) {
            return Math.min(Math.max(value, min), max);
        }
        let activeSparkCard = null;
        let activeSparkTooltip = null;
        function positionSparkTooltip(card, tooltip = card.querySelector('.sparks-tooltip')) {
            if (!card || !tooltip) return;
            const rect = card.getBoundingClientRect();
            const tooltipRect = tooltip.getBoundingClientRect();
            const tooltipWidth = Math.min(tooltipRect.width || 620, window.innerWidth - 16);
            const tooltipHeight = tooltipRect.height || 320;
            const x = clampValue(rect.left + rect.width / 2, tooltipWidth / 2 + 8, window.innerWidth - tooltipWidth / 2 - 8);
            const y = Math.max(8, rect.top - tooltipHeight - 10);
            tooltip.style.setProperty('--tooltip-left', `${x}px`);
            tooltip.style.setProperty('--tooltip-top', `${y}px`);
        }
        function bindSparkTooltips() {
            document.querySelectorAll('body > .sparks-tooltip').forEach(tooltip => tooltip.remove());
            document.querySelectorAll('#parent-grid .grid-card').forEach(card => {
                const tooltip = card.querySelector('.sparks-tooltip');
                if (!tooltip) return;
                card.classList.add('has-sparks');
                const show = () => {
                    if (tooltip.parentElement !== document.body) document.body.appendChild(tooltip);
                    activeSparkCard = card;
                    activeSparkTooltip = tooltip;
                    positionSparkTooltip(card, tooltip);
                    tooltip.classList.add('is-visible');
                };
                const hide = () => {
                    if (activeSparkCard === card) {
                        activeSparkCard = null;
                        activeSparkTooltip = null;
                    }
                    tooltip.classList.remove('is-visible');
                };
                tooltip.addEventListener('click', event => event.stopPropagation());
                tooltip.addEventListener('mousedown', event => event.stopPropagation());
                card.addEventListener('mouseenter', show);
                card.addEventListener('mouseleave', hide);
                card.addEventListener('focusin', show);
                card.addEventListener('focusout', hide);
            });
        }
        document.addEventListener('scroll', () => {
            if (activeSparkCard && activeSparkTooltip) positionSparkTooltip(activeSparkCard, activeSparkTooltip);
        }, true);
        window.addEventListener('resize', () => {
            if (activeSparkCard && activeSparkTooltip) positionSparkTooltip(activeSparkCard, activeSparkTooltip);
        });
        // ── Friends helpers ──────────────────────────────────────────────────────
        function friendKey(friend) {
            return `${friend.viewer_id}:${friend.support_card_id}`;
        }
        function normalizedCardName(value) {
            return String(value || '').toLowerCase().replace(/\([^)]*\)/g, '').replace(/[^a-z0-9]+/g, '');
        }
        function friendAllowed(friend) {
            if (!friend) return false;
            const friendId = String(friend.support_card_id || '');
            const friendName = normalizedCardName(friend.support_name);
            if (selection.deck) {
                const deckIds = new Set(selection.deck.cards.map(card => String(card.id || '')));
                if (deckIds.has(friendId)) return false;
                const deckNames = new Set(selection.deck.cards.map(card => normalizedCardName(card.name)));
                if (friendName && deckNames.has(friendName)) return false;
            }
            if (selection.trainee && friendName && normalizedCardName(selection.trainee.name) === friendName) return false;
            return true;
        }
        function getVisibleFriends() {
            const friends = (dashData && dashData.friends) || [];
            return friends.filter(friendAllowed);
        }
        function clearInvalidFriendSelection() {
            if (selection.friend && !friendAllowed(selection.friend)) {
                selection.friend = null;
            }
        }
        function syncFriendSelection() {
            const visibleFriends = (dashData && dashData.visibleFriends) || [];
            document.querySelectorAll('#friend-grid .grid-card').forEach((el, i) => {
                const friend = visibleFriends[i];
                el.classList.toggle('selected', Boolean(selection.friend && friend && friendKey(selection.friend) === friendKey(friend)));
            });
        }
        function findDeckIndexForCareer(activeCareer) {
            const decks = (dashData && dashData.validDecks) || [];
            if (!activeCareer || !decks.length) return -1;
            if (activeCareer.deck_id) {
                const deckIdx = decks.findIndex(d => Number(d.id) === Number(activeCareer.deck_id));
                if (deckIdx >= 0) return deckIdx;
            }
            const supportIds = (activeCareer.support_card_ids || []).map(id => String(id)).filter(Boolean);
            if (!supportIds.length) return -1;
            const careerSet = new Set(supportIds);
            return decks.findIndex(deck => {
                const deckIds = (deck.cards || []).map(card => String(card.id || '')).filter(Boolean);
                return deckIds.length === careerSet.size && deckIds.every(id => careerSet.has(id));
            });
        }
        function selectCareerDeck(activeCareer) {
            const deckIdx = findDeckIndexForCareer(activeCareer);
            if (deckIdx >= 0) {
                selection.deck = dashData.validDecks[deckIdx];
                const deckEls = document.querySelectorAll('.deck-container');
                if (deckEls[deckIdx]) deckEls[deckIdx].classList.add('selected');
                return;
            }
            const supportCards = (activeCareer && activeCareer.support_cards) || [];
            if (supportCards.length) {
                selection.deck = {
                    id: activeCareer.deck_id || 'active',
                    name: activeCareer.deck_id ? `Deck ${activeCareer.deck_id}` : 'Active career deck',
                    cards: supportCards
                };
            }
        }
        function selectCareerFriend(activeCareer) {
            if (!activeCareer || !activeCareer.friend_viewer_id || !activeCareer.friend_card_id) return;
            state.pendingFriendSelection = {
                viewer_id: String(activeCareer.friend_viewer_id),
                support_card_id: String(activeCareer.friend_card_id)
            };
            if (activeCareer.friend) {
                selection.friend = {
                    ...activeCareer.friend,
                    viewer_id: String(activeCareer.friend_viewer_id),
                    support_card_id: String(activeCareer.friend_card_id)
                };
            }
        }
        // ── Race editor ──────────────────────────────────────────────────────────
        async function loadRaceData() {
            try {
                const raceRes = await fetch('/assets/data/uma_race_data.json');
                const data = await raceRes.json();
                state.raceData = Array.isArray(data.races) ? data.races : [];
                syncSelectedPresetRaces();
                renderRaces();
            } catch (e) {}
        }

        function getCurrentPreset() {
            return (state.presets || []).find(p => p.name === state.selectedPreset);
        }

        function normalizePresetName(value) {
            return String(value || '').trim().replace(/[^a-zA-Z0-9._ -]+/g, '').replace(/\s+/g, ' ').trim();
        }

        function presetNameExists(name) {
            const normalized = normalizePresetName(name).toLowerCase();
            return Boolean(normalized && (state.presets || []).some(p => p.name.toLowerCase() === normalized));
        }

        function syncSelectedPresetRaces() {
            const current = getCurrentPreset();
            state.selectedRaces = (current?.extra_race_list || [])
                .map(id => parseInt(id, 10))
                .filter(id => Number.isFinite(id));
        }

        function getYearSlots(yearIdx) {
            const months = ['Jan', 'Feb', 'Mar', 'Apr', 'May', 'Jun', 'Jul', 'Aug', 'Sep', 'Oct', 'Nov', 'Dec'];
            const periods = ['Early', 'Late'];
            const yearLabels = ['Junior Year', 'Classic Year', 'Senior Year'];
            const slots = [];
            for (const month of months) {
                for (const period of periods) {
                    const label = period + ' ' + month;
                    const datePrefix = yearLabels[yearIdx] + ' ' + label;
                    const races = state.raceData.filter(r => r.date.includes(datePrefix));
                    slots.push({ period: label, races: races, yearIdx: yearIdx });
                }
            }
            return slots;
        }

        function raceKeys(race) {
            const keys = [race.id, ...(race.legacy_ids || [])];
            return keys.map(id => parseInt(id)).filter(id => Number.isFinite(id));
        }

        function raceSelected(race) {
            return raceKeys(race).some(id => state.selectedRaces.includes(id));
        }

        function renderRaces() {
            if (!els.raceOptionsContent) return;
            els.raceOptionsContent.innerHTML = '';

            const yearLabels = ['Junior Year', 'Classic Year', 'Senior Year'];
            yearLabels.forEach((label, yi) => {
                const block = document.createElement('div');
                block.className = 'race-year-block';
                block.innerHTML = `<div class="race-year-title">${label}</div>`;

                const grid = document.createElement('div');
                grid.className = 'race-time-grid';

                const slots = getYearSlots(yi);
                slots.forEach((slot, si) => {
                    const cell = document.createElement('div');
                    cell.className = 'race-time-cell';

                    const slotIds = slot.races.flatMap(r => raceKeys(r));
                    const selectedInSlot = state.selectedRaces.filter(id => slotIds.includes(id));
                    const mainRaceId = selectedInSlot[0];
                    const selected = slot.races.find(r => raceKeys(r).includes(mainRaceId));

                    let html = `<div class="race-time-label">${slot.period}</div>`;
                    if (selected) {
                        html += `
                            <div class="race-cell-selected-img">
                                <img src="/races/${encodeURIComponent(selected.name)}.png" onerror="this.style.display='none'; this.nextElementSibling.style.display='flex'">
                                <div class="race-image-fallback" style="display:none">${selected.type}</div>
                                <span class="race-cell-selected-grade badge-${selected.type.toLowerCase().replace('-', '')}">${selected.type}</span>
                            </div>
                            <div class="race-cell-selected-name">${escapeHtml(selected.name)}</div>
                        `;
                    } else {
                        html += `<div class="race-time-plus">+</div>`;
                    }

                    cell.innerHTML = html;
                    cell.onclick = () => openSlotPopup(slot, yi);
                    grid.appendChild(cell);
                });

                block.appendChild(grid);
                els.raceOptionsContent.appendChild(block);
            });
        }

        function openSlotPopup(slot, yearIdx) {
            const yearLabels = ['Junior Year', 'Classic Year', 'Senior Year'];
            els.racePopupTitle.textContent = `${yearLabels[yearIdx]} - ${slot.period}`;
            els.racePopupBody.innerHTML = '';

            if (slot.races.length === 0) {
                els.racePopupBody.innerHTML = '<div class="race-slot-popup-empty">No races available</div>';
            } else {
                const list = document.createElement('div');
                list.className = 'race-slot-popup-list';

                const slotIds = slot.races.flatMap(r => raceKeys(r));

                slot.races.forEach(race => {
                    const myIds = raceKeys(race);
                    const selectedInSlot = state.selectedRaces.filter(id => slotIds.includes(id));
                    const selIndex = selectedInSlot.findIndex(id => myIds.includes(id));
                    const isSelected = selIndex !== -1;

                    let badgeHtml = '<div class="race-slot-popup-check">✓</div>';
                    if (isSelected && state.scenarioType === "Mant" && selectedInSlot.length > 0) {
                        if (selIndex === 0) {
                            badgeHtml = '<div class="race-slot-popup-check main-race" style="font-size: 0.7rem; font-weight: bold; width: auto; padding: 0 8px; border-radius: 12px; background: rgba(255,255,255,0.2);">MAIN</div>';
                        } else {
                            badgeHtml = `<div class="race-slot-popup-check overwrite-race" style="font-size: 0.7rem; font-weight: bold; width: auto; padding: 0 8px; border-radius: 12px; background: rgba(255,255,255,0.1);">RIVAL OVERWRITE ${selIndex}</div>`;
                        }
                    }

                    const item = document.createElement('div');
                    item.className = `race-slot-popup-item ${isSelected ? 'on' : ''}`;
                    item.innerHTML = `
                        <div class="race-slot-popup-img">
                            <img src="/races/${encodeURIComponent(race.name)}.png" onerror="this.src='/broom.png'">
                        </div>
                        <div class="race-slot-popup-info">
                            <div class="race-slot-popup-name-row">
                                <span class="race-slot-popup-grade badge-${race.type.toLowerCase().replace('-', '')}">${race.type}</span>
                                <span class="race-slot-popup-name">${escapeHtml(race.name)}</span>
                            </div>
                            <div class="race-slot-popup-meta">
                                <span class="race-slot-popup-terrain ${race.terrain.toLowerCase()}">${race.terrain}</span>
                                <span class="race-slot-popup-distance">${race.distance}</span>
                            </div>
                        </div>
                        ${badgeHtml}
                    `;
                    item.onclick = async () => {
                        const isMant = state.scenarioType === "Mant";

                        if (isSelected) {
                            state.selectedRaces = state.selectedRaces.filter(id => !myIds.includes(id));
                        } else {
                            if (!isMant) {
                                state.selectedRaces = state.selectedRaces.filter(id => !slotIds.includes(id));
                            }
                            state.selectedRaces.push(parseInt(race.id));
                        }

                        openSlotPopup(slot, yearIdx);
                        renderRaces();
                        await autoSaveRaces();
                    };
                    list.appendChild(item);
                });
                els.racePopupBody.appendChild(list);
            }
            els.racePopupOverlay.style.display = 'flex';
        }

        async function autoSaveRaces() {
            try {
                const current = getCurrentPreset();
                if (current) current.extra_race_list = [...state.selectedRaces];
                await apiJson('/api/presets/save_races', {
                    method: 'POST',
                    headers: { 'Content-Type': 'application/json' },
                    body: JSON.stringify({
                        preset_name: state.selectedPreset,
                        races: state.selectedRaces
                    })
                });
            } catch (e) {}
        }

        function getTurnFromDate(dateStr) {
            const match = dateStr.match(/(\d+)年(\d+)月(前|後)半/);
            if (!match) return 0;
            const year = parseInt(match[1]);
            const month = parseInt(match[2]);
            const half = match[3] === '前' ? 0 : 1;
            return (year - 1) * 24 + (month - 1) * 2 + half + 1;
        }

        function bindRaceHandlers() {
            els.racePopupClose?.addEventListener('click', () => {
                els.racePopupOverlay.style.display = 'none';
            });
            els.racePopupOverlay?.addEventListener('click', (e) => {
                if (e.target === els.racePopupOverlay) els.racePopupOverlay.style.display = 'none';
            });

            makeSectionToggle('race-toggle', 'race-chevron', 'race-body', false);
        }

        let skillDataCache = null;
        let activeEditTier = null;
        let activeSkillFilter = null;
        let activeColorFilter = null;

        const SKILL_FILTERS = [
            { id: 101, label: 'Front' },
            { id: 102, label: 'Pace' },
            { id: 103, label: 'Late' },
            { id: 104, label: 'End' },
            { id: 201, label: 'Short' },
            { id: 202, label: 'Mile' },
            { id: 203, label: 'Medium' },
            { id: 204, label: 'Long' },
            { id: 502, label: 'Dirt' },
            { id: 'turf', label: 'Turf' }
        ];

        const COLOR_FILTERS = [
            { id: 'green', label: 'Green', color: '#4ade80', iconPrefixes: ['1001', '1002', '1003', '1004', '1005', '1006'] },
            { id: 'blue', label: 'Blue', color: '#60a5fa', iconPrefixes: ['2002'] },
            { id: 'yellow', label: 'Yellow', color: '#fbbf24', iconPrefixes: ['2001', '2004', '2005', '2006', '2009'] },
            { id: 'red', label: 'Red', color: '#f87171', iconPrefixes: ['3001', '3002', '3004', '3005', '3007'] }
        ];

        // ── Skill editor ─────────────────────────────────────────────────────────
        async function loadSkillData() {
            if (skillDataCache) return skillDataCache;
            try {
                const res = await apiJson('/api/skills');
                if (res.success && res.skills) {
                    const uniqueMap = new Map();
                    Object.entries(res.skills).forEach(([id, s]) => {
                        if (!uniqueMap.has(s.name)) {
                            uniqueMap.set(s.name, { id, ...s, tags: new Set(s.tags || []) });
                        } else {
                            const existing = uniqueMap.get(s.name);
                            if (s.rarity > existing.rarity) existing.rarity = s.rarity;
                            (s.tags || []).forEach(t => existing.tags.add(t));
                        }
                    });
                    skillDataCache = Array.from(uniqueMap.values()).map(s => ({ ...s, tags: Array.from(s.tags) }));
                    skillDataCache.sort((a, b) => a.name.localeCompare(b.name));
                    return skillDataCache;
                }
            } catch (e) {}
            return [];
        }

        function renderSkillFilters() {
            const container = document.getElementById('skill-filters');
            if (!container) return;
            
            let html = '<div style="display: flex; flex-wrap: wrap; gap: 4px; margin-bottom: 4px;">';
            for (const filter of SKILL_FILTERS) {
                const isActive = activeSkillFilter === filter.id;
                const bg = isActive ? 'rgba(var(--accent-primary-rgb), 0.2)' : 'rgba(255,255,255,0.05)';
                const border = isActive ? 'var(--accent-primary)' : 'transparent';
                const color = isActive ? 'var(--text-main)' : '#a1a1aa';
                html += `<div class="skill-filter-chip affinity-filter" data-id="${filter.id}" style="padding: 0.35rem 0.75rem; border-radius: 1rem; font-size: 0.75rem; cursor: pointer; background: ${bg}; border: 1px solid ${border}; color: ${color}; font-weight: bold; transition: all 0.1s;">${filter.label}</div>`;
            }
            html += '</div><div style="display: flex; flex-wrap: wrap; gap: 4px;">';
            
            for (const filter of COLOR_FILTERS) {
                const isActive = activeColorFilter === filter.id;
                const bg = isActive ? `${filter.color}33` : 'rgba(255,255,255,0.05)';
                const border = isActive ? filter.color : 'transparent';
                const color = isActive ? 'var(--text-main)' : filter.color;
                html += `<div class="skill-filter-chip color-filter" data-color="${filter.id}" style="padding: 0.35rem 0.75rem; border-radius: 1rem; font-size: 0.75rem; cursor: pointer; background: ${bg}; border: 1px solid ${border}; color: ${color}; font-weight: bold; transition: all 0.1s;">${filter.label}</div>`;
            }
            html += '</div>';
            
            container.innerHTML = html;
            
            container.querySelectorAll('.affinity-filter').forEach(el => {
                el.addEventListener('click', () => {
                    let tagId = el.getAttribute('data-id');
                    if (tagId !== 'turf') tagId = Number(tagId);
                    
                    if (activeSkillFilter === tagId) activeSkillFilter = null;
                    else activeSkillFilter = tagId;
                    
                    renderSkillFilters();
                    renderSkillList();
                });
            });

            container.querySelectorAll('.color-filter').forEach(el => {
                el.addEventListener('click', () => {
                    const colorId = el.getAttribute('data-color');
                    
                    if (activeColorFilter === colorId) activeColorFilter = null;
                    else activeColorFilter = colorId;
                    
                    renderSkillFilters();
                    renderSkillList();
                });
            });
        }

        function renderSkillList() {
            const query = (els.skillSearch?.value || '').toLowerCase();
            const skills = skillDataCache || [];
            const preset = getCurrentPreset() || {};
            const STYLE_TAG = {1: 101, 2: 102, 3: 103, 4: 104};
            const DIST_TAG = {1: 201, 2: 202, 3: 203, 4: 204};
            const autoTags = new Set([
                STYLE_TAG[preset.running_style],
                DIST_TAG[preset.target_distance],
            ].filter(Boolean));

            let count = 0;
            let html = '';
            for (const s of skills) {
                if (query && !s.name.toLowerCase().includes(query)) continue;

                if (activeSkillFilter !== null) {
                    const skillTags = s.tags || [];
                    if (activeSkillFilter === 'turf') {
                        if (skillTags.includes(502)) continue;
                    } else {
                        if (!skillTags.includes(activeSkillFilter)) continue;
                    }
                }

                if (activeColorFilter !== null) {
                    const iconId = String(s.icon_id || '');
                    const colorFilter = COLOR_FILTERS.find(filter => filter.id === activeColorFilter);
                    const skillColor = colorFilter && colorFilter.iconPrefixes.some(prefix => iconId.startsWith(prefix)) ? activeColorFilter : 'none';

                    if (skillColor !== activeColorFilter) continue;
                }

                const skillTags = s.tags || [];
                const mandatoryList = preset.mandatory_skill_list || [];
                const isMandatory = mandatoryList.includes(s.name);
                const isStyleAuto = !isMandatory && autoTags.size > 0 && skillTags.some(t => [101, 102, 103, 104].includes(t) && autoTags.has(t));
                const isDistAuto = !isMandatory && autoTags.size > 0 && skillTags.some(t => [201, 202, 203, 204].includes(t) && autoTags.has(t));
                const isAuto = isStyleAuto || isDistAuto;
                const autoBadge = isMandatory
                    ? `<span style="font-size:0.65rem;padding:0.1rem 0.4rem;border-radius:3px;background:#f59e0b22;color:#f59e0b;border:1px solid #f59e0b55;white-space:nowrap;">MANDATORY</span>`
                    : isAuto
                    ? `<span style="font-size:0.65rem;padding:0.1rem 0.4rem;border-radius:3px;background:${isStyleAuto ? '#7c3aed33' : '#0369a133'};color:${isStyleAuto ? '#a78bfa' : '#38bdf8'};border:1px solid ${isStyleAuto ? '#7c3aed66' : '#0369a166'};white-space:nowrap;">${isStyleAuto ? 'AUTO·STYLE' : 'AUTO·DIST'}</span>`
                    : '';

                count++;

                html += `<div class="skill-list-item" data-name="${escapeAttr(s.name)}" style="padding: 0.5rem; background: rgba(255,255,255,0.03); border-radius: 4px; cursor: pointer; display: flex; align-items: center; gap: 8px; transition: background 0.1s;">
                    <span style="flex: 1; overflow: hidden; text-overflow: ellipsis; white-space: nowrap; color: var(--text-main); font-size: 0.85rem;">${escapeHtml(s.name)}</span>
                    ${autoBadge}
                </div>`;
            }
            
            if (els.skillList) {
                if (count === 0) {
                    els.skillList.innerHTML = `<div style="padding: 1rem; color: #a1a1aa; font-size: 0.85rem;">No skills found.</div>`;
                } else {
                    els.skillList.innerHTML = html;
                    els.skillList.querySelectorAll('.skill-list-item').forEach(el => {
                        el.addEventListener('click', () => {
                            const name = el.getAttribute('data-name');
                            addSkillToFocusedArea(name);
                        });
                        el.addEventListener('mouseenter', () => el.style.background = 'rgba(255,255,255,0.1)');
                        el.addEventListener('mouseleave', () => el.style.background = 'rgba(255,255,255,0.03)');
                    });
                }
            }
        }

        // activeEditTier: number = tier idx, null = blacklist, 'mandatory' = mandatory list
        function renderSkillEditorRightSide() {
            const current = getCurrentPreset();
            if (!current) {
                if (els.skillTiersContainer) els.skillTiersContainer.innerHTML = '';
                if (els.skillBlacklistContainer) els.skillBlacklistContainer.innerHTML = '';
                if (els.skillMandatoryContainer) els.skillMandatoryContainer.innerHTML = '';
                return;
            }

            let tiersHtml = '';
            const storedTiers = current.learn_skill_list || [];
            const tiers = storedTiers.length > 0 ? storedTiers : [[]];
            tiers.forEach((tier, i) => {
                const isActive = activeEditTier === i;
                const itemsHtml = tier.map(s =>
                    `<div class="skill-tag">
                        ${escapeHtml(s)} <span class="skill-tag-del" data-tier="${i}" data-skill="${escapeAttr(s)}">&times;</span>
                    </div>`
                ).join('');

                tiersHtml += `
                <div class="skill-tier-dropzone ${isActive ? 'is-active' : ''}" data-tier="${i}">
                    <div class="skill-tier-header">
                        <span class="skill-tier-label">TIER ${i+1}</span>
                        <button class="btn btn-sm btn-danger-soft skill-editor-action tier-del-btn" data-tier="${i}">DEL</button>
                    </div>
                    <div class="skill-tag-list">
                        ${itemsHtml}
                    </div>
                </div>`;
            });
            if (els.skillTiersContainer) els.skillTiersContainer.innerHTML = tiersHtml;

            if (els.skillBlacklistContainer) {
                const isBlActive = activeEditTier === null;
                els.skillBlacklistContainer.classList.toggle('is-active', isBlActive);

                const blacklist = current.learn_skill_blacklist || [];
                els.skillBlacklistContainer.innerHTML = blacklist.map(s =>
                    `<div class="skill-tag blacklist">
                        ${escapeHtml(s)} <span class="skill-tag-del" data-blacklist="true" data-skill="${escapeAttr(s)}">&times;</span>
                    </div>`
                ).join('');
            }

            if (els.skillMandatoryContainer) {
                const isMandActive = activeEditTier === 'mandatory';
                els.skillMandatoryContainer.classList.toggle('is-active', isMandActive);
                const mandList = current.mandatory_skill_list || [];
                els.skillMandatoryContainer.innerHTML = mandList.map(s =>
                    `<div class="skill-tag" style="background:#f59e0b22;border-color:#f59e0b55;color:#f59e0b;">
                        ${escapeHtml(s)} <span class="skill-tag-del" data-mandatory="true" data-skill="${escapeAttr(s)}">&times;</span>
                    </div>`
                ).join('');
            }

            els.skillTiersContainer?.querySelectorAll('.skill-tier-dropzone').forEach(el => {
                el.addEventListener('click', (e) => {
                    if (e.target.classList.contains('tier-del-btn') || e.target.classList.contains('skill-tag-del')) return;
                    activeEditTier = parseInt(el.getAttribute('data-tier'));
                    renderSkillEditorRightSide();
                });
            });
            if (els.skillBlacklistContainer) {
                els.skillBlacklistContainer.onclick = (e) => {
                    if (e.target.classList.contains('skill-tag-del')) return;
                    activeEditTier = null;
                    renderSkillEditorRightSide();
                };
            }
            if (els.skillMandatoryContainer) {
                els.skillMandatoryContainer.onclick = (e) => {
                    if (e.target.classList.contains('skill-tag-del')) return;
                    activeEditTier = 'mandatory';
                    renderSkillEditorRightSide();
                };
            }

            els.skillTiersContainer?.querySelectorAll('.tier-del-btn').forEach(btn => {
                btn.addEventListener('click', async () => {
                    const idx = parseInt(btn.getAttribute('data-tier'));
                    current.learn_skill_list = current.learn_skill_list || [];
                    current.learn_skill_list.splice(idx, 1);
                    if (activeEditTier === idx) activeEditTier = null;
                    else if (typeof activeEditTier === 'number' && activeEditTier > idx) activeEditTier--;
                    await savePresetConfig();
                    renderSkillEditorRightSide();
                });
            });

            document.querySelectorAll('.skill-tag-del').forEach(btn => {
                btn.addEventListener('click', async () => {
                    const skillName = btn.getAttribute('data-skill');
                    if (btn.hasAttribute('data-mandatory')) {
                        current.mandatory_skill_list = (current.mandatory_skill_list || []).filter(s => s !== skillName);
                    } else if (btn.hasAttribute('data-blacklist')) {
                        current.learn_skill_blacklist = current.learn_skill_blacklist.filter(s => s !== skillName);
                    } else {
                        const tierIdx = parseInt(btn.getAttribute('data-tier'));
                        current.learn_skill_list[tierIdx] = current.learn_skill_list[tierIdx].filter(s => s !== skillName);
                    }
                    await savePresetConfig();
                    renderSkillEditorRightSide();
                });
            });
        }

        async function addSkillToFocusedArea(name) {
            const current = getCurrentPreset();
            if (!current) return;

            if (activeEditTier === 'mandatory') {
                if (!current.mandatory_skill_list) current.mandatory_skill_list = [];
                if (!current.mandatory_skill_list.includes(name)) {
                    current.mandatory_skill_list.push(name);
                }
            } else if (activeEditTier === null) {
                if (!current.learn_skill_blacklist) current.learn_skill_blacklist = [];
                if (!current.learn_skill_blacklist.includes(name)) {
                    current.learn_skill_blacklist.push(name);
                }
            } else {
                if (!current.learn_skill_list) current.learn_skill_list = [];
                if (!current.learn_skill_list[activeEditTier]) current.learn_skill_list[activeEditTier] = [];
                if (!current.learn_skill_list[activeEditTier].includes(name)) {
                    current.learn_skill_list[activeEditTier].push(name);
                }
            }
            await savePresetConfig();
            renderSkillEditorRightSide();
        }

        function initSkillEditor() {
            if (!state.selectedPreset) return;
            activeEditTier = 0;

            els.skillModal.style.display = 'flex';
            if (els.skillSearch) els.skillSearch.value = '';
            activeSkillFilter = null;
            activeColorFilter = null;

            loadSkillData().then(() => {
                renderSkillFilters();
                renderSkillList();
            });
            renderSkillEditorRightSide();
        }

        const STAT_NAMES = ["Speed", "Stamina", "Power", "Guts", "Wit"];
        const STAT_COLORS = ["#4e8ef7", "#e05555", "#e07a30", "#4caf7d", "#d4b84e"];

        // ── Stat priority & targets ──────────────────────────────────────────────
        function renderStatPriority(priority, idealTargets, minTargets) {
            const list = document.getElementById('stat-priority-list');
            if (!list) return;
            const order = (Array.isArray(priority) && priority.length === 5) ? priority : [0, 1, 2, 3, 4];
            const ideals = (Array.isArray(idealTargets) && idealTargets.length === 5) ? idealTargets : [0, 0, 0, 0, 0];
            const mins = (Array.isArray(minTargets) && minTargets.length === 5) ? minTargets : [0, 0, 0, 0, 0];
            list.innerHTML = '';
            order.forEach((statIdx, rank) => {
                const item = document.createElement('div');
                item.draggable = true;
                item.dataset.statIdx = statIdx;
                item.style.cssText = `display:flex;align-items:center;gap:0.5rem;padding:0.4rem 0.7rem;border-radius:6px;cursor:grab;user-select:none;background:color-mix(in srgb,${STAT_COLORS[statIdx]} 15%,var(--bg-surface,#1e1e2e));border:1px solid color-mix(in srgb,${STAT_COLORS[statIdx]} 35%,transparent);font-size:0.82rem;font-weight:600;letter-spacing:0.04em;`;
                const inputStyle = `width:4.5rem;padding:0.15rem 0.35rem;border-radius:4px;border:1px solid color-mix(in srgb,${STAT_COLORS[statIdx]} 40%,transparent);background:var(--bg-input,#12121c);color:var(--text-primary,#e0e0e0);font-size:0.78rem;font-weight:500;text-align:center;cursor:text;`;
                item.innerHTML = `<span style="color:${STAT_COLORS[statIdx]};font-size:1rem;line-height:1;">⠿</span><span class="sp-rank" style="color:${STAT_COLORS[statIdx]};min-width:1.4rem;">#${rank+1}</span><span style="flex:1;">${STAT_NAMES[statIdx]}</span><label style="display:flex;align-items:center;gap:0.25rem;font-size:0.72rem;font-weight:400;color:var(--text-secondary,#aaa);">Ideal<input class="sp-ideal" type="number" min="0" max="2000" step="1" value="${ideals[statIdx] || 0}" style="${inputStyle}" draggable="false"></label><label style="display:flex;align-items:center;gap:0.25rem;font-size:0.72rem;font-weight:400;color:var(--text-secondary,#aaa);">Min<input class="sp-min" type="number" min="0" max="2000" step="1" value="${mins[statIdx] || 0}" style="${inputStyle}" draggable="false"></label>`;

                // Prevent drag when clicking inputs
                item.querySelectorAll('input').forEach(inp => {
                    inp.addEventListener('mousedown', e => e.stopPropagation());
                    inp.addEventListener('change', () => savePresetConfig());
                });

                item.addEventListener('dragstart', e => {
                    e.dataTransfer.effectAllowed = 'move';
                    e.dataTransfer.setData('text/plain', String(statIdx));
                    item.style.opacity = '0.4';
                    list._dragging = item;
                });
                item.addEventListener('dragend', () => {
                    item.style.opacity = '';
                    list._dragging = null;
                    [...list.children].forEach((el, i) => {
                        el.querySelector('.sp-rank').textContent = `#${i+1}`;
                    });
                    savePresetConfig();
                });
                item.addEventListener('dragover', e => {
                    e.preventDefault();
                    const dragging = list._dragging;
                    if (!dragging || dragging === item) return;
                    const rect = item.getBoundingClientRect();
                    if (e.clientY < rect.top + rect.height / 2) {
                        list.insertBefore(dragging, item);
                    } else {
                        list.insertBefore(dragging, item.nextSibling);
                    }
                });
                list.appendChild(item);
            });
        }

        function getStatPriorityFromDOM() {
            const list = document.getElementById('stat-priority-list');
            if (!list) return [0, 1, 2, 3, 4];
            return [...list.querySelectorAll('[data-stat-idx]')].map(el => parseInt(el.dataset.statIdx));
        }

        function getStatTargetsFromDOM() {
            const list = document.getElementById('stat-priority-list');
            const ideals = [0, 0, 0, 0, 0];
            const mins = [0, 0, 0, 0, 0];
            if (!list) return { ideals, mins };
            list.querySelectorAll('[data-stat-idx]').forEach(el => {
                const idx = parseInt(el.dataset.statIdx);
                if (idx >= 0 && idx < 5) {
                    ideals[idx] = parseInt(el.querySelector('.sp-ideal')?.value) || 0;
                    mins[idx] = parseInt(el.querySelector('.sp-min')?.value) || 0;
                }
            });
            return { ideals, mins };
        }

        // ── Preset config save / load ────────────────────────────────────────────
        async function savePresetConfig() {
            if (!state.selectedPreset || !state.presets) return;
            const current = getCurrentPreset();
            if (!current) return;

            current.learn_skill_threshold = parseInt(els.presetSkillThreshold.value) || 888;
            current.running_style = parseInt(els.presetRunningStyle?.value) || 1;
            current.target_distance = parseInt(els.presetTargetDistance?.value) || 0;
            current.auto_buy_override_threshold = els.presetAutoBuyOverride ? els.presetAutoBuyOverride.checked : true;
            if (!Array.isArray(current.mandatory_skill_list)) current.mandatory_skill_list = [];
            current.stat_priority = getStatPriorityFromDOM();
            const { ideals, mins } = getStatTargetsFromDOM();
            current.stat_ideal_targets = ideals;
            current.stat_min_targets = mins;

            try {
                await apiJson('/api/presets', {
                    method: 'POST',
                    headers: { 'Content-Type': 'application/json' },
                    body: JSON.stringify({ preset: current })
                });
            } catch (e) {}
        }

        function populatePresetUI() {
            if (!state.selectedPreset || !state.presets) return;
            const current = getCurrentPreset();
            if (!current) return;

            els.presetSkillThreshold.value = current.learn_skill_threshold || 888;
            if (els.presetRunningStyle) els.presetRunningStyle.value = current.running_style || 1;
            if (els.presetTargetDistance) els.presetTargetDistance.value = current.target_distance || 0;
            if (els.presetAutoBuyOverride) els.presetAutoBuyOverride.checked = current.auto_buy_override_threshold !== false;
            renderStatPriority(current.stat_priority || [0, 1, 2, 3, 4], current.stat_ideal_targets || [0,0,0,0,0], current.stat_min_targets || [0,0,0,0,0]);
        }

        function bindPresetHandlers() {
            if (els.presetSelect) {
                els.presetSelect.addEventListener('change', async (e) => {
                    state.selectedPreset = e.target.value;
                    localStorage.setItem('uma_selected_preset', state.selectedPreset);
                    syncSelectedPresetRaces();
                    populatePresetUI();
                    renderRaces();
                });
            }

            const saveHandler = () => savePresetConfig();
            els.presetSkillThreshold?.addEventListener('change', saveHandler);
            els.presetRunningStyle?.addEventListener('change', saveHandler);
            els.presetTargetDistance?.addEventListener('change', saveHandler);
            els.presetAutoBuyOverride?.addEventListener('change', saveHandler);

            els.presetEditSkillsBtn?.addEventListener('click', () => {
                if (!state.selectedPreset) return;
                activeEditTier = 0;

                els.skillModal.style.display = 'flex';
                if (els.skillSearch) els.skillSearch.value = '';
                activeSkillFilter = null;

                loadSkillData().then(() => {
                    renderSkillFilters();
                    renderSkillList();
                });
                renderSkillEditorRightSide();
            });
            els.skillModalClose?.addEventListener('click', () => { els.skillModal.style.display = 'none'; });

            els.skillSearch?.addEventListener('input', renderSkillList);

            els.skillAddTierBtn?.addEventListener('click', async () => {
                const current = getCurrentPreset();
                if (!current) return;
                if (!current.learn_skill_list) current.learn_skill_list = [];
                current.learn_skill_list.push([]);
                activeEditTier = current.learn_skill_list.length - 1;
                await savePresetConfig();
                renderSkillEditorRightSide();
            });

            document.getElementById('skill-select-all-btn')?.addEventListener('click', async () => {
                const current = getCurrentPreset();
                if (!current) return;
                const visibleNodes = els.skillList?.querySelectorAll('.skill-list-item') || [];
                let changed = false;

                visibleNodes.forEach(node => {
                    const name = node.getAttribute('data-name');
                    if (activeEditTier === null) {
                        if (!current.learn_skill_blacklist) current.learn_skill_blacklist = [];
                        if (!current.learn_skill_blacklist.includes(name)) {
                            current.learn_skill_blacklist.push(name);
                            changed = true;
                        }
                    } else {
                        if (!current.learn_skill_list) current.learn_skill_list = [];
                        if (!current.learn_skill_list[activeEditTier]) current.learn_skill_list[activeEditTier] = [];
                        if (!current.learn_skill_list[activeEditTier].includes(name)) {
                            current.learn_skill_list[activeEditTier].push(name);
                            changed = true;
                        }
                    }
                });
                if (changed) {
                    await savePresetConfig();
                    renderSkillEditorRightSide();
                }
            });

            document.getElementById('skill-deselect-all-btn')?.addEventListener('click', async () => {
                const current = getCurrentPreset();
                if (!current) return;
                const visibleNodes = els.skillList?.querySelectorAll('.skill-list-item') || [];
                let changed = false;

                const namesToRemove = Array.from(visibleNodes).map(node => node.getAttribute('data-name'));

                if (activeEditTier === null) {
                    if (current.learn_skill_blacklist) {
                        const originalLen = current.learn_skill_blacklist.length;
                        current.learn_skill_blacklist = current.learn_skill_blacklist.filter(s => !namesToRemove.includes(s));
                        if (current.learn_skill_blacklist.length !== originalLen) changed = true;
                    }
                } else {
                    if (current.learn_skill_list && current.learn_skill_list[activeEditTier]) {
                        const originalLen = current.learn_skill_list[activeEditTier].length;
                        current.learn_skill_list[activeEditTier] = current.learn_skill_list[activeEditTier].filter(s => !namesToRemove.includes(s));
                        if (current.learn_skill_list[activeEditTier].length !== originalLen) changed = true;
                    }
                }

                if (changed) {
                    await savePresetConfig();
                    renderSkillEditorRightSide();
                }
            });

            document.getElementById('skill-blacklist-all-btn')?.addEventListener('click', async () => {
                const current = getCurrentPreset();
                if (!current) return;
                const visibleNodes = els.skillList?.querySelectorAll('.skill-list-item') || [];
                let changed = false;

                if (!current.learn_skill_blacklist) current.learn_skill_blacklist = [];
                visibleNodes.forEach(node => {
                    const name = node.getAttribute('data-name');
                    if (!current.learn_skill_blacklist.includes(name)) {
                        current.learn_skill_blacklist.push(name);
                        changed = true;
                    }
                });

                if (changed) {
                    await savePresetConfig();
                    renderSkillEditorRightSide();
                }
            });
            document.getElementById('skill-clear-blacklist-btn')?.addEventListener('click', async () => {
                const current = getCurrentPreset();
                if (!current) return;
                if (current.learn_skill_blacklist && current.learn_skill_blacklist.length > 0) {
                    current.learn_skill_blacklist = [];
                    await savePresetConfig();
                    renderSkillEditorRightSide();
                }
            });

            document.getElementById('skill-clear-mandatory-btn')?.addEventListener('click', async () => {
                const current = getCurrentPreset();
                if (!current) return;
                if (current.mandatory_skill_list && current.mandatory_skill_list.length > 0) {
                    current.mandatory_skill_list = [];
                    await savePresetConfig();
                    renderSkillEditorRightSide();
                }
            });

            els.presetAddBtn?.addEventListener('click', async () => {
                const newName = prompt("Enter new preset name:");
                if (!newName || !newName.trim()) return;
                const normalizedName = normalizePresetName(newName);
                if (!normalizedName) {
                    alert("Preset name cannot be empty.");
                    return;
                }
                if (presetNameExists(normalizedName)) {
                    alert("A preset with that name already exists.");
                    return;
                }

                const newPreset = {
                    name: normalizedName,
                    running_style: 1,
                    learn_skill_list: [],
                    learn_skill_blacklist: [],
                    extra_race_list: [],
                    learn_skill_threshold: 888
                };

                try {
                    const res = await apiJson('/api/presets', {
                        method: 'POST',
                        headers: { 'Content-Type': 'application/json' },
                        body: JSON.stringify({ preset: newPreset })
                    });
                    if (!res.success || !res.preset?.name) {
                        alert(res.detail || "Failed to save new preset.");
                        return;
                    }
                    state.selectedPreset = res.preset.name;
                    localStorage.setItem('uma_selected_preset', state.selectedPreset);
                    await loadPresets();
                    if (els.presetSelect) els.presetSelect.value = state.selectedPreset;
                    syncSelectedPresetRaces();
                    populatePresetUI();
                    renderRaces();
                } catch (e) { alert("Failed to save new preset."); }
            });

            els.presetDelBtn?.addEventListener('click', async () => {
                if (!state.selectedPreset) return;
                const deletedName = state.selectedPreset;
                if (!confirm(`Are you sure you want to delete preset '${deletedName}'?`)) return;

                try {
                    const res = await apiJson('/api/presets/delete', {
                        method: 'POST',
                        headers: { 'Content-Type': 'application/json' },
                        body: JSON.stringify({ name: deletedName })
                    });
                    if (!res.success) {
                        alert(res.detail || "Failed to delete preset.");
                        return;
                    }
                    await loadPresets();
                } catch (e) { alert("Failed to delete preset."); }
            });
        }

        // ── Preset list ──────────────────────────────────────────────────────────
        async function loadPresets() {
            try {
                const res = await apiJson('/api/presets');
                if (res.success && res.presets && res.presets.length > 0) {
                    state.presets = res.presets;
                    if (els.presetSelect) {
                        els.presetSelect.innerHTML = state.presets.map(p => `<option value="${escapeAttr(p.name)}">${escapeHtml(p.name)}</option>`).join('');
                    }
                    const saved = localStorage.getItem('uma_selected_preset');
                    if (saved && state.presets.some(p => p.name === saved)) {
                        state.selectedPreset = saved;
                    } else {
                        state.selectedPreset = state.presets[0].name;
                    }
                    localStorage.setItem('uma_selected_preset', state.selectedPreset);
                    if (els.presetSelect) els.presetSelect.value = state.selectedPreset;
                    populatePresetUI();
                } else {
                    state.presets = [];
                    state.selectedPreset = "";
                    localStorage.removeItem('uma_selected_preset');
                    if (els.presetSelect) els.presetSelect.innerHTML = "";
                    populatePresetUI();
                }
            } catch(e) {
                state.presets = [];
                state.selectedPreset = "";
                localStorage.removeItem('uma_selected_preset');
                populatePresetUI();
            }
            syncStartButton();
            await loadRaceData();
        }

        // ── Friends panel ────────────────────────────────────────────────────────
        function renderFriends() {
            const friends = (dashData && dashData.friends) || [];
            clearInvalidFriendSelection();
            const visibleFriends = getVisibleFriends();
            console.log("Friend test:", visibleFriends[0]);
            if (dashData) dashData.visibleFriends = visibleFriends;

            if (state.pendingFriendSelection) {
                const f = visibleFriends.find(v =>
                    String(v.viewer_id) === state.pendingFriendSelection.viewer_id &&
                    String(v.support_card_id) === state.pendingFriendSelection.support_card_id
                );
                if (f) {
                    selection.friend = f;
                    state.pendingFriendSelection = null;
                }
            }

            els.friendCount.innerText = `(${visibleFriends.length}/${friends.length})`;
            els.friendGrid.innerHTML = visibleFriends.map(friend => {
                const imgId = friend.support_card_id || '10001';
                const lb = friend.limit_break_count ?? '?';
                return `<div class="grid-card friend-card">
                    <img src="/api/images/${imgId}.png" onerror="hideBrokenImage(this)">
                    <div class="grid-card-overlay">
                        <span class="grid-card-name">${friend.support_name || 'Unknown'}</span>
                        <span class="grid-card-kicker">${friend.type || '?'} | LB${lb}</span>
                    </div>
                </div>`;
            }).filter(Boolean).join('');
            attachFriendHandlers();
            syncFriendSelection();
            renderTeamPanel();
        }
        function appendSeenFriendIds(ids) {
            if (!dashData) return;
            const seen = new Set(dashData.friendExcludeIds || []);
            (ids || []).forEach(id => {
                if (id) seen.add(id);
            });
            dashData.friendExcludeIds = Array.from(seen);
        }
        async function loadFriends(refresh = false) {
            if (!dashData || state.isFetchingFriends) return;
            const isCareerActive = dashData.account && dashData.account.career && dashData.account.career.active;
            if (isCareerActive) {
                els.friendRefreshBtn.disabled = true;
                els.friendStatus.classList.remove('error');
                els.friendStatus.innerText = 'Active career, endpoint blocked';
                return;
            }
            state.isFetchingFriends = true;
            els.friendRefreshBtn.disabled = true;
            els.friendStatus.classList.remove('error');
            els.friendStatus.innerText = refresh ? 'Refreshing friends...' : 'Loading friends...';
            const excludeIds = refresh ? (dashData.friendExcludeIds || []) : [];
            try {
                const data = await apiJson('/api/career/friends', {
                    method: 'POST',
                    headers: { 'Content-Type': 'application/json' },
                    body: JSON.stringify({ exclude_viewer_ids: excludeIds })
                });
                if (!data.success) throw new Error(data.detail || 'Friend load failed');
                dashData.friends = data.friends || [];
                appendSeenFriendIds(data.exclude_viewer_ids || []);
                renderFriends();
                if (data.source === 'Active Career (Skip)') {
                    els.friendStatus.innerText = 'Active career, endpoint blocked';
                    return;
                }
                const source = data.source === 'initial' ? 'initial' : 'refresh';
                const visibleCount = ((dashData && dashData.visibleFriends) || []).length;
                els.friendStatus.innerText = `${source} list: ${visibleCount}/${dashData.friends.length} cards`;
            } catch (e) {
                els.friendStatus.innerText = e.message || 'Friend load failed';
                els.friendStatus.classList.add('error');
            } finally {
                state.isFetchingFriends = false;
                const stillActive = dashData.account && dashData.account.career && dashData.account.career.active;
                els.friendRefreshBtn.disabled = !!stillActive;
            }
        }
        function attachFriendHandlers() {
            const visibleFriends = (dashData && dashData.visibleFriends) || [];
            document.querySelectorAll('#friend-grid .grid-card').forEach((el, i) => {
                el.classList.add('selectable');
                el.addEventListener('click', () => {
                    const friend = visibleFriends[i];
                    const already = selection.friend && friendKey(selection.friend) === friendKey(friend);
                    document.querySelectorAll('#friend-grid .grid-card').forEach(c => c.classList.remove('selected'));
                    selection.friend = already ? null : friend;
                    selection.rentalParent = already ? null : friend;
                    console.log("Rental selected:", selection.rentalParent);
                    console.log("Parent data:", selection.rentalParent?.parent_data);
                    console.log("BODY RENTAL",
                        selection.friend?.parent_data?.viewer_id,
                        selection.friend?.parent_data?.trained_chara_id
                    );
                    if (!already) el.classList.add('selected');
                    renderTeamPanel();
                });
            });
        }
        // ── Career runner ────────────────────────────────────────────────────────
        async function startCareer() {
            const reason = getStartMissingReason();
            if (reason || state.isStartingCareer) {
                syncStartButton();
                return;
            }
            state.isStartingCareer = true;
            syncStartButton();
            let finalMessage = '';
            let finalIsError = false;
            const activeCareer = state.account && state.account.career && state.account.career.active;
            const body = activeCareer ? {
                preset_name: state.selectedPreset,
                max_steps: 2500,
                burn_clocks: state.burnClocks,
                dev_mode: state.devEnabled
            } : {
                card_id: Number(selection.trainee.id),
                support_card_ids: selection.deck.cards.map(card => Number(card.id)),
                friend_viewer_id: Number(selection.friend.viewer_id),
                friend_card_id: Number(selection.friend.support_card_id),
                parent_id_1: Number(selection.veterans[0]?.instance_id || 0),
                parent_id_2: Number(selection.veterans[1]?.instance_id || 0),
                // Only populate rental fields when the user explicitly picked a GUEST parent.
                // Never fall back to the friend's parent_data — that's their support card's
                // linked uma, not a guest legacy parent, and sending it causes result_code 205.
                rental_viewer_id: Number(selection.guestParent?.guest_viewer_id || selection.guestParent?.owner_viewer_id || 0),
                rental_trained_chara_id: Number(selection.guestParent?.instance_id || 0),
                deck_id: Number(selection.deck.id),
                scenario_id: 4,
                use_tp: 30,
                difficulty_id: 0,
                difficulty: 0,
                is_boost: 0,
                boost_story_event_id: 0,
                preset_name: state.selectedPreset,
                max_steps: 2500,
                burn_clocks: state.burnClocks,
                dev_mode: state.devEnabled
            };
            try {
                console.log("START BODY", body);
                const data = await apiJson('/api/career/run', {
                    method: 'POST',
                    headers: { 'Content-Type': 'application/json' },
                    body: JSON.stringify(body)
                });
                if (!data.success) throw new Error(data.detail || 'Start failed');
                state.displayedClocksUsed = Number(data.runner && data.runner.clocks_used || 0);
                renderAccountStrip(data.account);
                if (data.account && data.account.career && data.account.career.active) {
                    renderFriends();
                }
                startRunnerPolling();
                finalMessage = 'Career runner started';
            } catch (e) {
                finalMessage = e.message || 'Start failed';
                finalIsError = true;
                if (state.devEnabled) {
                    setDevEnabled(false, { persist: true });
                }
            } finally {
                state.isStartingCareer = false;
                syncStartButton();
                if (finalMessage) {
                    els.startStatus.innerText = finalMessage;
                    els.startStatus.classList.toggle('error', finalIsError);
                }
            }
        }
        function applyRunnerSettings(runner) {
            if (runner.running && runner.burn_clocks !== undefined && state.burnClocks !== runner.burn_clocks) {
                setBurnClocks(runner.burn_clocks, { persist: true });
            }
        }
        function applyRunnerClockUsage(runner) {
            const clocksUsed = Number(runner.clocks_used || 0);
            if (state.account && clocksUsed > state.displayedClocksUsed) {
                const delta = clocksUsed - state.displayedClocksUsed;
                state.account = {
                    ...state.account,
                    clocks: Math.max(0, Number(state.account.clocks || 0) - delta)
                };
                state.displayedClocksUsed = clocksUsed;
                renderAccountStrip(state.account);
            } else if (clocksUsed < state.displayedClocksUsed) {
                state.displayedClocksUsed = clocksUsed;
            }
        }
        function applyRunnerSnapshot(runner) {
            state.runner = runner;
            applyRunnerSettings(runner);
            applyRunnerClockUsage(runner);
        }
        async function refreshRunnerStatus() {
            try {
                const data = await apiJson('/api/career/runner');
                if (!data.success || !data.runner) return;
                const runner = data.runner;
                applyRunnerSnapshot(runner);

                const rows = (runner.action_history && runner.action_history.length) ? runner.action_history : deriveActionHistory(runner.log || []);
                if (rows.length) renderActionHistory(rows);
                // also update diagnostics panel if visible
                const diagPane = document.getElementById('mode-diagnostics');
                if (diagPane && diagPane.classList.contains('is-active')) {
                    renderDiagCareer(runner, state.account, null);
                    renderDiagLog(runner);
                }
                if (runner.running) {
                    els.startStatus.classList.toggle('error', false);
                    if (!rows.length) els.startStatus.innerText = '';
                    return;
                }
                if (state.runnerTimer && !state.devEnabled) {
                    bgClearTimer(state.runnerTimer);
                    state.runnerTimer = 0;
                }
                if (runner.last_error) {
                    els.startStatus.classList.toggle('error', true);
                    if (!rows.length) els.startStatus.innerText = runner.last_error;
                    if (state.devEnabled) {
                        state.consecutiveRunnerFails++;
                        if (state.consecutiveRunnerFails >= 3) {
                            if (!rows.length) els.startStatus.innerText = runner.last_error + " (Auto-retry disabled due to loop)";
                            setDevEnabled(false, { persist: true });
                        }
                    }
                } else if (state.devEnabled && runner.finished && !runner.last_error) {
                    state.consecutiveRunnerFails = 0;
                    els.startStatus.classList.toggle('error', false);
                    if (!rows.length) els.startStatus.innerText = `Career finished! Restarting...`;
                    if (state.account && state.account.career) state.account.career.active = false;
                    renderAccountStrip(state.account);
                    // Check if session career limit has been reached
                    try {
                        const statsData = await apiJson('/api/stats/fans');
                        checkCareersLimit(statsData.careers_count || 0);
                    } catch(e) {}
                } else if (runner.steps) {
                    els.startStatus.classList.toggle('error', false);
                    if (!rows.length) els.startStatus.innerText = `Runner stopped after ${runner.steps} steps`;
                    if (state.devEnabled) {
                        state.consecutiveRunnerFails++;
                        if (state.consecutiveRunnerFails >= 3) {
                            if (!rows.length) els.startStatus.innerText = `Runner stopped after ${runner.steps} steps (Auto-retry disabled due to loop)`;
                            setDevEnabled(false, { persist: true });
                        }
                    }
                }
            } catch (e) {}
        }
        function renderActionHistory(rows) {
            // Action log table lives in the DIAGNOSTICS tab; setup tab shows text only.
            if (!els.startStatus || !rows.length) return;
            const last = rows[rows.length - 1];
            const norm = normalizeHistoryAction(last);
            els.startStatus.innerText = `T${last.turn} · ${norm.action.toUpperCase()}${last.facility ? ' · ' + last.facility : ''}`;
        }
        function deriveActionHistory(log) {
            return log.filter(item => ['command', 'race', 'race_progress', 'finish', 'api_delay', 'turn_delay', 'complex_delay'].includes(item.action)).map(item => {
                const detail = String(item.detail || '');
                let action = item.action;
                let facility = '';
                if (action === 'command') {
                    if (detail.startsWith('training ')) {
                        action = 'train';
                        facility = detail.replace('training ', '');
                    } else if (detail.startsWith('rest ')) {
                        action = 'rest';
                        facility = detail.replace('rest ', '');
                        if (['301', '302', '303', '304', '305', '390'].includes(facility)) action = 'recreation';
                    } else if (detail.startsWith('challenge ')) {
                        action = 'rest';
                        facility = detail.replace('challenge ', '');
                    } else if (detail.startsWith('recreation ')) {
                        action = 'recreation';
                        facility = detail.replace('recreation ', '');
                    } else if (detail.startsWith('command 8:')) {
                        action = 'medic';
                    }
                } else if (action === 'race_progress') {
                    action = 'race';
                }
                return { turn: item.turn, action, facility, detail };
            });
        }
        function normalizeHistoryAction(row) {
            const facility = String(row.facility ?? '');
            if (row.action === 'rest' && ['301', '302', '303', '304', '305', '390'].includes(facility)) {
                return { ...row, action: 'recreation' };
            }
            return row;
        }
        const timerWorkerBlob = new Blob([`
            let activeTimers = {};
            self.onmessage = function(e) {
                const { action, id, ms } = e.data;
                if (action === 'setInterval') {
                    activeTimers[id] = setInterval(() => postMessage({ id }), ms);
                } else if (action === 'setTimeout') {
                    activeTimers[id] = setTimeout(() => {
                        postMessage({ id });
                        delete activeTimers[id];
                    }, ms);
                } else if (action === 'clear') {
                    clearInterval(activeTimers[id]);
                    clearTimeout(activeTimers[id]);
                    delete activeTimers[id];
                }
            };
        `], {type: 'application/javascript'});
        const timerWorker = new Worker(URL.createObjectURL(timerWorkerBlob));
        let nextTimerId = 1;
        const timerCallbacks = {};
        timerWorker.onmessage = function(e) {
            if (timerCallbacks[e.data.id]) timerCallbacks[e.data.id]();
        };
        function bgSetInterval(cb, ms) {
            const id = nextTimerId++;
            timerCallbacks[id] = cb;
            timerWorker.postMessage({ action: 'setInterval', id, ms });
            return id;
        }
        function bgSetTimeout(cb, ms) {
            const id = nextTimerId++;
            timerCallbacks[id] = () => { delete timerCallbacks[id]; cb(); };
            timerWorker.postMessage({ action: 'setTimeout', id, ms });
            return id;
        }
        function bgClearTimer(id) {
            delete timerCallbacks[id];
            timerWorker.postMessage({ action: 'clear', id });
        }
        function startRunnerPolling() {
            if (state.runnerTimer) bgClearTimer(state.runnerTimer);
            refreshRunnerStatus();
            state.runnerTimer = bgSetInterval(refreshRunnerStatus, 1500);
        }
        els.friendRefreshBtn.addEventListener('click', event => {
            event.stopPropagation();
            loadFriends(true);
        });
        document.getElementById('follow-parent-load-btn')?.addEventListener('click', loadFollowParents);
        els.startCareerBtn.addEventListener('click', startCareer);

        // ── Selection handlers ───────────────────────────────────────────────────
        function selectDeck(index, element) {
            const alreadySelected = element.classList.contains('selected');
            document.querySelectorAll('.deck-container.selected').forEach(card => card.classList.remove('selected'));
            selection.deck = null;
            if (!alreadySelected) {
                element.classList.add('selected');
                selection.deck = dashData.validDecks[index];
            }
            renderFriends();
            renderTeamPanel();
            syncSelectionToServer();
        }
        function selectTrainee(index, element) {
            const alreadySelected = element.classList.contains('selected');
            document.querySelectorAll('#uma-grid .grid-card.selected').forEach(card => card.classList.remove('selected'));
            selection.trainee = null;
            if (!alreadySelected) {
                element.classList.add('selected');
                selection.trainee = dashData.umas[index];
            }
            renderFriends();
            updateVetSelectability();
            renderTeamPanel();
            syncSelectionToServer();
        }
        function selectParent(index, element) {
            const parent = dashData.parents[index];
            const isRental = parent && (parent.is_guest || parent.from_follow);
            if (element.classList.contains('selected')) {
                element.classList.remove('selected');
                if (isRental) {
                    selection.guestParent = null;
                } else {
                    selection.veterans = selection.veterans.filter(p => p._gridIdx !== index);
                }
            } else if (isRental) {
                // Only one rental slot — deselect previous if any
                if (selection.guestParent !== null) {
                    const prevEl = document.querySelector(`#parent-grid .grid-card[data-idx="${selection.guestParent._gridIdx}"], #follow-parent-grid .grid-card[data-follow-idx="${selection.guestParent._gridIdx}"]`);
                    if (prevEl) prevEl.classList.remove('selected');
                }
                selection.guestParent = { ...parent, _gridIdx: index };
                element.classList.add('selected');
            } else if (!element.classList.contains('vet-full')) {
                selection.veterans.push({ ...parent, _gridIdx: index });
                element.classList.add('selected');
            }
            updateVetSelectability();
            renderTeamPanel();
            syncSelectionToServer();
        }
        function attachSelectionHandlers() {
            document.querySelectorAll('.deck-container').forEach((element, index) => {
                element.addEventListener('click', () => selectDeck(index, element));
            });
            document.querySelectorAll('#uma-grid .grid-card').forEach((element, index) => {
                element.classList.add('selectable');
                element.addEventListener('click', () => selectTrainee(index, element));
            });
            document.querySelectorAll('#parent-grid .grid-card').forEach((element, index) => {
                element.classList.add('selectable');
                element.addEventListener('click', () => selectParent(index, element));
            });
        }
        function isValidDeck(deck) {
            return deck.cards.every(card => {
                const id = card.id || '';
                const name = card.name || '';
                return !id.includes('{') && !id.includes('-') && !name.includes('Unknown');
            });
        }
        // ── Grid rendering ───────────────────────────────────────────────────────
        function renderCounts(data) {
            els.umaCount.innerText = `(${data.umas.length})`;
            els.cardCount.innerText = `(${data.supports.length})`;
            els.parentCount.innerText = `(${data.parents.length})`;
        }
        function renderDecks(decks) {
            els.deckList.innerHTML = decks.map(deck => {
                const cards = deck.cards.map(card => {
                    const imgId = card.id || '10001';
                    return `<div class="grid-card deck-card">
                        <img src="/api/images/${imgId}.png" onerror="hideBrokenImage(this)">
                        <div class="grid-card-overlay">
                            <span class="grid-card-kicker">${card.type || '?'} | ${card.rarity || '?'}</span>
                            <span class="grid-card-name">${card.name || 'Unknown'}</span>
                        </div>
                    </div>`;
                }).join('');
                return `<div class="deck-container">
                    <div class="deck-header">
                        <span>${deck.name.toUpperCase()}</span>
                        <span style="font-size:0.85rem; opacity:0.8">SLOT ${deck.id}</span>
                    </div>
                    <div class="deck-cards">${cards}</div>
                </div>`;
            }).join('');
        }
        function renderFactors(factors) {
            const star = String.fromCharCode(9733);
            return factors.map(factor => `
                <div class="factor-badge f-${factor.category}">
                    ${factor.name} <span class="stars">${star.repeat(factor.stars)}</span>
                </div>
            `).join('');
        }
        function renderWins(wins) {
            if (!wins || !wins.total) return '<span class="spark-win-chip">Wins --</span>';
            return `
                <span class="spark-win-chip">G1 ${wins.g1 || 0}</span>
                <span class="spark-win-chip">G2 ${wins.g2 || 0}</span>
                <span class="spark-win-chip">G3 ${wins.g3 || 0}</span>
            `;
        }
        // Builds the lineage tooltip content (factors + wins per family member).
        function renderParentSparks(parent, fallbackImgId) {
            const tree = parent.tree || {};
            return ['self', 'p1', 'p2'].map(key => {
                const node = tree[key];
                if (!node || !node.factors || node.factors.length === 0) return '';
                const nodeImg = node.card_id || fallbackImgId;
                const nodeClass = key === 'self' ? 'spark-node spark-node-self' : 'spark-node';
                return `<div class="${nodeClass}" style="--node-bg: url('/api/images/${nodeImg}.png')">
                    <div class="spark-node-header">
                        <img class="spark-node-portrait" src="/api/images/${nodeImg}.png" onerror="hideBrokenImage(this)">
                        <div class="spark-node-meta">
                            <div class="spark-node-title">${node.name || `Card ${node.card_id || '?'}`}</div>
                            <div class="spark-win-row">${renderWins(node.wins)}</div>
                        </div>
                    </div>
                    <div class="spark-factor-list">
                        ${renderFactors(node.factors)}
                    </div>
                </div>`;
            }).join('');
        }
        function renderParents(parents) {
            els.parentGrid.innerHTML = parents.map((parent, i) => {
                const imgId = parent.card_id || '100101';
                const guestBadge = parent.is_guest ? `<div class="guest-badge">GUEST</div>` : '';
                return `<div class="grid-card" data-idx="${i}">
                    <div class="rank-badge">${rankMap[parent.rank] || '??'}</div>
                    ${guestBadge}
                    <img src="/api/images/${imgId}.png" onerror="hideBrokenImage(this)">
                    <div class="sparks-tooltip" style="--spark-bg: url('/api/images/${imgId}.png')">
                        <div class="sparks-tooltip-title"></div>
                        <div class="sparks-tooltip-scroll">
                            <div class="sparks-lineage-grid">
                                ${renderParentSparks(parent, imgId)}
                            </div>
                        </div>
                    </div>
                    <div class="grid-card-overlay">
                        <span class="grid-card-kicker">${parent.is_guest ? 'GUEST' : 'ID: ' + (parent.instance_id || '?')}</span>
                        <span class="grid-card-name">${parent.name || 'Unknown'}</span>
                    </div>
                </div>`;
            }).join('');
        }
        function renderFollowParents(parents) {
            if (!els.followParentGrid) return;
            if (!parents || !parents.length) {
                els.followParentGrid.innerHTML = '<div style="padding:1rem;color:#a1a1aa;font-size:0.85rem;">No parents found from follows.</div>';
                return;
            }

        // Merge into dashData.parents so selectParent() works correctly
        const startIdx = dashData.parents.length;
        parents.forEach((p, i) => {
            p._gridIdx = startIdx + i;
            dashData.parents.push(p);
        });

        els.followParentGrid.innerHTML = parents.map((parent, i) => {
            const imgId = parent.card_id || '100101';
            const gridIdx = startIdx + i;
            return `<div class="grid-card selectable" data-follow-idx="${gridIdx}">
                <div class="rank-badge">${rankMap[parent.rank] || '??'}</div>
                <img src="/api/images/${imgId}.png" onerror="hideBrokenImage(this)">
                <div class="sparks-tooltip" style="--spark-bg: url('/api/images/${imgId}.png')">
                    <div class="sparks-tooltip-title"></div>
                    <div class="sparks-tooltip-scroll">
                        <div class="sparks-lineage-grid">
                            ${renderParentSparks(parent, imgId)}
                        </div>
                    </div>
                </div>
                <div class="grid-card-overlay">
                    <span class="grid-card-kicker" style="font-size:0.65rem;opacity:0.75">${escapeHtml(parent.owner_name)}</span>
                    <span class="grid-card-name">${parent.name || 'Unknown'}</span>
                </div>
            </div>`;
        }).join('');

        els.followParentGrid.querySelectorAll('.grid-card').forEach(el => {
            el.classList.add('selectable');
            el.addEventListener('click', () => {
                const gridIdx = parseInt(el.getAttribute('data-follow-idx'));
                selectParent(gridIdx, el);
            });
        });

        if (els.followParentCount) {
            els.followParentCount.innerText = `(${parents.length})`;
        }
        updateVetSelectability();
    }

    async function loadFollowParents() {
        if (!els.followParentLoadBtn) return;
        els.followParentLoadBtn.disabled = true;
        if (els.followParentStatus) els.followParentStatus.innerText = 'Loading...';
        try {
            const data = await apiJson('/api/follow/parents');
            if (!data.success) throw new Error(data.detail || 'Failed');
            renderFollowParents(data.parents);
            bindSparkTooltips(); // wire hover tooltips for the newly rendered cards
            if (els.followParentStatus) els.followParentStatus.innerText = `${data.parents.length} parents loaded`;
        } catch (e) {
            if (els.followParentStatus) {
                els.followParentStatus.innerText = e.message || 'Load failed';
            }
        } finally {
            els.followParentLoadBtn.disabled = false;
        }
    }
        function renderTrainees(umas) {
            els.umaGrid.innerHTML = umas.map(uma => {
                const imgId = uma.id || '100101';
                return `<div class="grid-card">
                    <img src="/api/images/${imgId}.png" onerror="hideBrokenImage(this)">
                    <div class="grid-card-overlay"><span class="grid-card-name">${uma.name || 'Unknown'}</span></div>
                </div>`;
            }).join('');
        }
        // ── Deck Builder ──
        const deckBuilder = { cards: [] }; // array of {id, name, rarity, type}

        function renderDeckBuilder() {
            const slots = document.getElementById('deck-builder-slots');
            const count = document.getElementById('deck-builder-count');
            const saveBtn = document.getElementById('deck-builder-save-btn');
            if (!slots) return;
            const n = deckBuilder.cards.length;
            count.textContent = `(${n}/5 cards)`;
            saveBtn.disabled = n !== 5;

            // 5 fixed slots
            slots.innerHTML = Array.from({length: 5}, (_, i) => {
                const card = deckBuilder.cards[i];
                if (card) {
                    return `<div class="deck-builder-slot filled" data-slot="${i}" title="${escapeHtml(card.name)}">
                        <img src="/api/images/${card.id}.png" onerror="hideBrokenImage(this)" style="width:100%;height:100%;object-fit:cover;border-radius:6px;">
                        <button class="deck-slot-remove" data-slot="${i}" title="Remove">✕</button>
                    </div>`;
                }
                return `<div class="deck-builder-slot empty" data-slot="${i}"><span style="font-size:1.4rem;opacity:0.25;">+</span></div>`;
            }).join('');

            // remove buttons
            slots.querySelectorAll('.deck-slot-remove').forEach(btn => {
                btn.addEventListener('click', e => {
                    e.stopPropagation();
                    const idx = parseInt(btn.dataset.slot);
                    const removed = deckBuilder.cards.splice(idx, 1)[0];
                    // un-highlight in card grid
                    if (removed) {
                        document.querySelectorAll(`#card-grid .grid-card.in-builder[data-card-id="${removed.id}"]`)
                            .forEach(el => el.classList.remove('in-builder'));
                    }
                    renderDeckBuilder();
                });
            });
        }

        function deckBuilderToggleCard(card) {
            const idx = deckBuilder.cards.findIndex(c => c.id === card.id);
            if (idx >= 0) {
                deckBuilder.cards.splice(idx, 1);
            } else {
                if (deckBuilder.cards.length >= 5) return; // full
                deckBuilder.cards.push(card);
            }
            renderDeckBuilder();
        }

        function renderSupports(supports) {
            els.cardGrid.innerHTML = supports.map(card => {
                const imgId = card.id || '10001';
                return `<div class="grid-card support-card" data-card-id="${card.id}" data-card='${JSON.stringify({id:card.id,name:card.name,rarity:card.rarity,type:card.type})}'>
                    <img src="/api/images/${imgId}.png" onerror="hideBrokenImage(this)">
                    <div class="grid-card-overlay">
                        <span class="grid-card-kicker">${(card.rarity || '?') + ' | ' + (card.type || '?')}</span>
                        <span class="grid-card-name">${card.name || 'Unknown'}</span>
                    </div>
                    <div class="builder-check" style="display:none;">✓</div>
                </div>`;
            }).join('');

            els.cardGrid.querySelectorAll('.grid-card.support-card').forEach(el => {
                el.addEventListener('click', () => {
                    const card = JSON.parse(el.dataset.card);
                    const inBuilder = deckBuilder.cards.findIndex(c => c.id === card.id) >= 0;
                    if (!inBuilder && deckBuilder.cards.length >= 5) return;
                    deckBuilderToggleCard(card);
                    el.classList.toggle('in-builder', deckBuilder.cards.findIndex(c => c.id === card.id) >= 0);
                });
            });
        }

        // Deck builder clear button
        document.getElementById('deck-builder-clear-btn')?.addEventListener('click', () => {
            deckBuilder.cards = [];
            document.querySelectorAll('#card-grid .grid-card.in-builder').forEach(el => el.classList.remove('in-builder'));
            renderDeckBuilder();
            const status = document.getElementById('deck-builder-status');
            if (status) status.textContent = '';
        });

        // Deck builder save button
        document.getElementById('deck-builder-save-btn')?.addEventListener('click', async () => {
            if (deckBuilder.cards.length !== 5) return;
            const saveBtn = document.getElementById('deck-builder-save-btn');
            const status = document.getElementById('deck-builder-status');
            saveBtn.disabled = true;
            if (status) status.textContent = 'Saving...';
            try {
                const res = await apiJson('/api/deck/save', {
                    method: 'POST',
                    headers: {'Content-Type': 'application/json'},
                    body: JSON.stringify({deck_id: 1, card_ids: deckBuilder.cards.map(c => Number(c.id))})
                });
                if (!res.success) throw new Error(res.detail || 'Save failed');
                if (status) status.textContent = 'Saved to Slot 1!';
                // Refresh deck list if dashData updated
                if (dashData && active_dashboard_data_decks) {
                    dashData.validDecks = active_dashboard_data_decks.filter(isValidDeck);
                    renderDecks(dashData.validDecks);
                } else {
                    // just re-render with current data
                    const d1 = deckBuilder.cards.map(c => ({id: c.id, name: c.name, rarity: c.rarity, type: c.type}));
                    const existing = (dashData?.validDecks || []).filter(d => d.id !== 1);
                    dashData.validDecks = [{id: 1, name: 'Deck 1', cards: d1}, ...existing];
                    renderDecks(dashData.validDecks);
                }
            } catch (e) {
                if (status) status.textContent = e.message || 'Save failed';
            } finally {
                saveBtn.disabled = deckBuilder.cards.length !== 5;
            }
        });

        renderDeckBuilder(); // initial empty state

        // ── Dashboard init ───────────────────────────────────────────────────────
        function showDashboardView(data) {
            _sessionStartTime = Date.now();
            // Snapshot careers_count at login so stop-after-N counts from this session
            apiJson('/api/stats/fans').then(d => { state.sessionCareersStart = d.careers_count || 0; }).catch(() => {});
            document.body.classList.add('dashboard-mode');
            els.loginView.style.display = 'none';
            els.dashboardView.style.display = '';
            els.dashboardView.classList.add('active');
            els.logoutBtn.style.display = 'block';
            showNavbar();
            renderAccountStrip(data.account);
            syncDashboardHeight();
            // preload fan stats if the stats tab is active or was last used
            try {
                const ct = localStorage.getItem('uma_active_tab');
                if (!ct || ct === 'tab-stats') fetchAndRenderFanStats();
            } catch(e) {}
        }

        function autoLoadCareerSelection() {
            const activeCareer = state.account && state.account.career && state.account.career.active ? state.account.career : null;
            if (!activeCareer) return;

            resetSelection();
            document.querySelectorAll('.deck-container.selected, #uma-grid .grid-card.selected, #parent-grid .grid-card.selected, #friend-grid .grid-card.selected')
                .forEach(el => el.classList.remove('selected'));

            selectCareerDeck(activeCareer);

            if (activeCareer.card_id && dashData.umas) {
                const umaIdx = dashData.umas.findIndex(u => String(u.id) === String(activeCareer.card_id));
                if (umaIdx >= 0) {
                    selection.trainee = dashData.umas[umaIdx];
                    const umaEls = document.querySelectorAll('#uma-grid .grid-card');
                    if (umaEls[umaIdx]) umaEls[umaIdx].classList.add('selected');
                }
            }

            if (dashData.parents) {
                const p1 = activeCareer.parent_id_1;
                const p2 = activeCareer.parent_id_2;

                if (p1 || p2) {
                    dashData.parents.forEach((p, idx) => {
                        const pId = Number(p.instance_id);
                        if ((p1 && pId === Number(p1)) || (p2 && pId === Number(p2))) {
                            const isRental = p.from_follow || p.is_guest;
                            if (isRental) {
                                // Game echoes the rental parent ID in succession_trained_chara_id_2
                                // — put it in guestParent, NOT veterans
                                if (!selection.guestParent) {
                                    selection.guestParent = { ...p, _gridIdx: idx };
                                    const followEl = document.querySelector(`#follow-parent-grid .grid-card[data-follow-idx="${idx}"]`);
                                    if (followEl) followEl.classList.add('selected');
                                }
                            } else {
                                if (selection.veterans.length < 2 && !selection.veterans.find(v => Number(v.instance_id) === pId)) {
                                    p._gridIdx = idx;
                                    selection.veterans.push(p);
                                    const parentEls = document.querySelectorAll('#parent-grid .grid-card');
                                    if (parentEls[idx]) parentEls[idx].classList.add('selected');
                                }
                            }
                        }
                    });
                    updateVetSelectability();
                }
            }

            selectCareerFriend(activeCareer);
            renderTeamPanel();
        }

        function applyServerSelection(serverSelection) {
            if (!serverSelection) return;
            if (serverSelection.deck && dashData.validDecks) {
                const deckIdx = dashData.validDecks.findIndex(d => Number(d.id) === Number(serverSelection.deck.id));
                if (deckIdx >= 0) {
                    selection.deck = dashData.validDecks[deckIdx];
                    const deckEls = document.querySelectorAll('.deck-container');
                    if (deckEls[deckIdx]) deckEls[deckIdx].classList.add('selected');
                }
            }
            if (serverSelection.trainee && dashData.umas) {
                const umaIdx = dashData.umas.findIndex(u => String(u.id) === String(serverSelection.trainee.id));
                if (umaIdx >= 0) {
                    selection.trainee = dashData.umas[umaIdx];
                    const umaEls = document.querySelectorAll('#uma-grid .grid-card');
                    if (umaEls[umaIdx]) umaEls[umaIdx].classList.add('selected');
                }
            }
            if (serverSelection.veterans && dashData.parents) {
                serverSelection.veterans.forEach(v => {
                    const pIdx = dashData.parents.findIndex(p => Number(p.instance_id) === Number(v.instance_id));
                    if (pIdx >= 0 && selection.veterans.length < 2) {
                        const parent = dashData.parents[pIdx];
                        parent._gridIdx = pIdx;
                        selection.veterans.push(parent);
                        const parentEls = document.querySelectorAll('#parent-grid .grid-card');
                        if (parentEls[pIdx]) parentEls[pIdx].classList.add('selected');
                    }
                });
                updateVetSelectability();
            }
            // Restore follow/guest parent — it won't be in dashData.parents yet (needs load-follows),
            // but we restore it into selection so the career start request is still correct.
            if (serverSelection.guestParent) {
                selection.guestParent = serverSelection.guestParent;
                // If follow parents happen to already be loaded, highlight the card
                if (dashData.parents) {
                    const gpIdx = dashData.parents.findIndex(p =>
                        Number(p.instance_id) === Number(serverSelection.guestParent.instance_id)
                    );
                    if (gpIdx >= 0) {
                        const el = document.querySelector(
                            `#parent-grid .grid-card[data-idx="${gpIdx}"], #follow-parent-grid .grid-card[data-follow-idx="${gpIdx}"]`
                        );
                        if (el) el.classList.add('selected');
                    }
                }
            }
            if (serverSelection.friend) {
                state.pendingFriendSelection = {
                    viewer_id: String(serverSelection.friend.viewer_id),
                    support_card_id: String(serverSelection.friend.support_card_id)
                };
            }
        }

        async function renderDashboard(data, options = {}) {
            dashData = data;
            dashData.validDecks = data.decks.filter(isValidDeck);
            dashData.friends = data.friends || [];
            dashData.friendExcludeIds = data.friendExcludeIds || [];
            showDashboardView(data);
            renderCounts(data);
            renderDecks(dashData.validDecks);
            renderParents(data.parents);
            renderTrainees(dashData.umas);
            renderSupports(data.supports);
            resetSelection();
            if (data.selection) applyServerSelection(data.selection);
            autoLoadCareerSelection();

            await loadPresets();
            if (!dashData.friends.length) {
                loadFriends(false);
            } else {
                renderFriends();
            }
            bindSparkTooltips();
            attachSelectionHandlers();
            bindRaceHandlers();
            bindPresetHandlers();
            renderTeamPanel();

            startRunnerPolling();
            await waitForDomPaint(2);
            setLoadingScreen(false);
            await waitForDomPaint(2);
            if (options.animateIntro !== false) {
                playBrandIntro();
                if (options.waitForIntro) await sleep(780);
            }
        }

        // ── App entry point ──────────────────────────────────────────────────────
        async function restoreSession() {
            try {
                const data = await apiJson('/api/session?t=' + Date.now());
                if (data && data.success) await renderDashboard(data, { animateIntro: true, waitForIntro: false });
                else {
                    hideNavbar();
                    setLoadingScreen(false);
                }
            } catch (e) {
                hideNavbar();
                setLoadingScreen(false);
            }
        }
        bindDelayControls();
        bindMasterDataControls();
        setLoadingScreen(true);
        restoreSession();
})();
        // ── Fan Stats ────────────────────────────────────────────────────────
        let _fanStatsCache = null;

        function formatFanNumber(n) {
            n = Number(n || 0);
            if (n >= 1000000) return (n / 1000000).toFixed(2) + 'M';
            if (n >= 1000) return (n / 1000).toFixed(1) + 'K';
            return n.toLocaleString();
        }

        async function fetchAndRenderFanStats() {
            const panel = document.getElementById('fan-stats-panel');
            if (!panel) return;
            try {
                const data = await apiJson('/api/stats/fans');
                _fanStatsCache = data;
                renderFanStats(data);
            } catch(e) {
                panel.innerHTML = `<div class="account-pill" style="opacity:0.5">Not logged in</div>`;
            }
        }

        function renderFanStats(data) {
            const panel = document.getElementById('fan-stats-panel');
            if (!panel || !data) return;

            const circle = data.circle_info;
            const circleHtml = circle ? `
                <div class="master-data-panel" style="margin-bottom:0.75rem;">
                    <div class="dashboard-section-title" style="font-size:0.75rem;margin-bottom:0.4rem;">CLUB</div>
                    <div style="display:flex;flex-wrap:wrap;gap:0.5rem;">
                        ${circle.name ? `<div class="account-pill"><span class="label">NAME</span><strong>${circle.name}</strong></div>` : ''}
                        ${circle.member_count != null ? `<div class="account-pill"><span class="label">MEMBERS</span><strong>${circle.member_count}</strong></div>` : ''}
                        ${circle.rank != null ? `<div class="account-pill"><span class="label">RANK</span><strong>${circle.rank}</strong></div>` : ''}
                        ${circle.score != null ? `<div class="account-pill"><span class="label">SCORE</span><strong>${formatFanNumber(circle.score)}</strong></div>` : ''}
                    </div>
                </div>` : '';

            const currentFansHtml = data.current_fans != null ? `
                <div class="account-pill pill-career" style="flex:1;min-width:120px;">
                    <span class="label">IN CAREER</span>
                    <strong>${formatFanNumber(data.current_fans)}</strong>
                </div>` : '';

            const statsHtml = `
                <div style="display:flex;flex-wrap:wrap;gap:0.5rem;margin-bottom:0.75rem;">
                    ${currentFansHtml}
                    <div class="account-pill pill-tp" style="flex:1;min-width:120px;">
                        <span class="label">SESSION</span>
                        <strong>+${formatFanNumber(data.session_gained)}</strong>
                    </div>
                    <div class="account-pill pill-gold" style="flex:1;min-width:120px;">
                        <span class="label">TODAY</span>
                        <strong>+${formatFanNumber(data.today_gained)}</strong>
                    </div>
                    <div class="account-pill pill-carrots" style="flex:1;min-width:120px;">
                        <span class="label">ALL-TIME</span>
                        <strong>+${formatFanNumber(data.total_gained)}</strong>
                    </div>
                    <div class="account-pill" style="flex:1;min-width:120px;opacity:0.7;">
                        <span class="label">CAREERS</span>
                        <strong>${data.careers_count || 0}</strong>
                    </div>
                </div>`;

            const careers = data.recent_careers || [];
            const rowsHtml = careers.length === 0
                ? '<div style="opacity:0.45;font-size:0.8rem;padding:0.5rem 0;">No careers recorded yet.</div>'
                : `<table style="width:100%;border-collapse:collapse;font-size:0.78rem;">
                    <thead>
                        <tr style="opacity:0.55;text-transform:uppercase;font-size:0.7rem;">
                            <th style="text-align:left;padding:0.2rem 0.4rem;">TIME</th>
                            <th style="text-align:left;padding:0.2rem 0.4rem;">CHARA</th>
                            <th style="text-align:right;padding:0.2rem 0.4rem;">FANS END</th>
                            <th style="text-align:right;padding:0.2rem 0.4rem;">GAINED</th>
                            <th style="text-align:right;padding:0.2rem 0.4rem;">TURN</th>
                        </tr>
                    </thead>
                    <tbody>
                        ${careers.map(c => `
                        <tr style="border-top:1px solid rgba(128,128,128,0.15);">
                            <td style="padding:0.25rem 0.4rem;opacity:0.6;">${(c.timestamp || '').slice(11,16)}</td>
                            <td style="padding:0.25rem 0.4rem;">${c.chara_name || c.card_id || '-'}</td>
                            <td style="padding:0.25rem 0.4rem;text-align:right;">${formatFanNumber(c.final_fans)}</td>
                            <td style="padding:0.25rem 0.4rem;text-align:right;color:var(--accent-color);">+${formatFanNumber(c.fans_gained)}</td>
                            <td style="padding:0.25rem 0.4rem;text-align:right;opacity:0.6;">${c.final_turn || '-'}</td>
                        </tr>`).join('')}
                    </tbody>
                </table>`;

            const clearBtn = `<div style="margin-top:0.75rem;display:flex;gap:0.5rem;align-items:center;">
                <button class="btn btn-sm" id="fan-stats-refresh-btn" type="button">↻ REFRESH</button>
                <button class="btn btn-sm btn-danger" id="fan-stats-clear-btn" type="button">CLEAR HISTORY</button>
            </div>`;

            panel.innerHTML = circleHtml + statsHtml +
                `<div class="master-data-panel">
                    <div class="dashboard-section-title" style="font-size:0.75rem;margin-bottom:0.4rem;">RECENT CAREERS (last 30)</div>
                    ${rowsHtml}
                </div>` + clearBtn;

            document.getElementById('fan-stats-refresh-btn').addEventListener('click', fetchAndRenderFanStats);
            document.getElementById('fan-stats-clear-btn').addEventListener('click', async () => {
                if (!confirm('Clear all fan stats history?')) return;
                await apiJson('/api/stats/fans', { method: 'DELETE' });
                fetchAndRenderFanStats();
            });
        }
        // ───────────────────────────────────────────�