// Tiny helpers for the docs site: theme toggle, copy-to-clipboard, tabs, TOC active state.

(function () {
    'use strict';

    // ---- Theme persistence ----
    const THEME_KEY = 'perflens-docs-theme';
    function setTheme(t) {
        document.documentElement.setAttribute('data-theme', t);
        try { localStorage.setItem(THEME_KEY, t); } catch (e) {}
        const btn = document.getElementById('theme-toggle');
        if (btn) btn.setAttribute('aria-label', 'Toggle theme (current: ' + t + ')');
    }
    const saved = (function () {
        try { return localStorage.getItem(THEME_KEY); } catch (e) { return null; }
    })();
    if (saved === 'light' || saved === 'dark') {
        setTheme(saved);
    } else if (window.matchMedia && window.matchMedia('(prefers-color-scheme: light)').matches) {
        setTheme('light');
    }

    document.addEventListener('click', function (e) {
        const t = e.target.closest('#theme-toggle');
        if (!t) return;
        const cur = document.documentElement.getAttribute('data-theme') || 'dark';
        setTheme(cur === 'dark' ? 'light' : 'dark');
    });

    // ---- Copy code buttons ----
    function addCopyButtons() {
        document.querySelectorAll('pre').forEach(function (pre) {
            if (pre.querySelector('.copy-btn')) return;
            const btn = document.createElement('button');
            btn.className = 'copy-btn';
            btn.type = 'button';
            btn.textContent = 'Copy';
            btn.addEventListener('click', function () {
                const code = pre.querySelector('code') || pre;
                const text = code.innerText;
                navigator.clipboard.writeText(text).then(function () {
                    btn.textContent = 'Copied';
                    btn.classList.add('copied');
                    setTimeout(function () {
                        btn.textContent = 'Copy';
                        btn.classList.remove('copied');
                    }, 1400);
                }).catch(function () {
                    btn.textContent = 'Failed';
                });
            });
            pre.appendChild(btn);
        });
    }

    // ---- Tabs (used in install section) ----
    function wireTabs() {
        document.querySelectorAll('[data-tabs]').forEach(function (group) {
            const btns = group.querySelectorAll('.tab-btn');
            const panels = group.querySelectorAll('.tab-panel');
            btns.forEach(function (btn) {
                btn.addEventListener('click', function () {
                    btns.forEach(b => b.classList.remove('active'));
                    panels.forEach(p => p.classList.remove('active'));
                    btn.classList.add('active');
                    const target = group.querySelector(
                        '.tab-panel[data-tab="' + btn.dataset.tab + '"]'
                    );
                    if (target) target.classList.add('active');
                });
            });
        });
    }

    // ---- TOC active highlight via IntersectionObserver ----
    function wireToc() {
        const toc = document.querySelector('.toc');
        if (!toc) return;
        const links = Array.from(toc.querySelectorAll('a[href^="#"]'));
        if (!links.length) return;
        const targets = links
            .map(a => document.getElementById(a.getAttribute('href').slice(1)))
            .filter(Boolean);
        const byId = {};
        links.forEach(a => { byId[a.getAttribute('href').slice(1)] = a; });

        let visible = new Set();
        const io = new IntersectionObserver(function (entries) {
            entries.forEach(function (e) {
                if (e.isIntersecting) visible.add(e.target.id);
                else visible.delete(e.target.id);
            });
            links.forEach(l => l.classList.remove('active'));
            for (const t of targets) {
                if (visible.has(t.id)) { byId[t.id].classList.add('active'); break; }
            }
        }, { rootMargin: '-100px 0px -65% 0px', threshold: 0 });
        targets.forEach(t => io.observe(t));
    }

    document.addEventListener('DOMContentLoaded', function () {
        addCopyButtons();
        wireTabs();
        wireToc();
    });
})();
