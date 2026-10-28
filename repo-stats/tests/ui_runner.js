// Playwright driver for test_ui.py. Usage: node ui_runner.js config.json
// Writes a JSON results file; all assertions live in test_ui.py.
const { chromium } = require('playwright');
const fs = require('fs');

const cfg = JSON.parse(fs.readFileSync(process.argv[2], 'utf8'));
const results = {};
const BAD_TEXT = /\b(null|undefined|NaN|Infinity)\b|\[object |\bNaN\b/g;

async function newPage(browser, ds, o = {}) {
  const ctx = await browser.newContext({
    viewport: { width: o.width || 1280, height: o.height || 900 },
    colorScheme: o.colorScheme || 'light',
  });
  // Block everything that is not the local file; record attempts.
  const attempts = [];
  await ctx.route('**/*', (route) => {
    const u = route.request().url();
    if (u.startsWith('file:') || u.startsWith('data:') || u.startsWith('blob:')) return route.continue();
    attempts.push(u);
    return route.abort();
  });
  if (o.theme) await ctx.addInitScript((t) => { try { if (!sessionStorage.getItem('__seeded')) { localStorage.setItem('repo-stats-theme', t); sessionStorage.setItem('__seeded', '1'); } } catch (e) {} }, o.theme);
  const page = await ctx.newPage();
  const rec = { errors: [], pageErrors: [], dialogs: [], attempts };
  page.on('console', (m) => { if (m.type() === 'error') rec.errors.push(m.text()); });
  page.on('pageerror', (e) => rec.pageErrors.push(String(e && e.message || e)));
  page.on('dialog', async (d) => { rec.dialogs.push(d.message()); await d.dismiss(); });
  await page.goto('file://' + ds.html);
  await page.waitForSelector('#overview, .kpis', { timeout: 15000 }).catch(() => {});
  return { ctx, page, rec };
}

const textIssues = (page) => page.evaluate((src) => {
  const re = new RegExp(src, 'g');
  const t = document.body.innerText, out = [];
  let m;
  while ((m = re.exec(t)) && out.length < 8) out.push(t.slice(Math.max(0, m.index - 40), m.index + 40).replace(/\n/g, ' | '));
  return out;
}, BAD_TEXT.source);

const hScroll = (page) => page.evaluate(() => document.documentElement.scrollWidth - window.innerWidth);

async function openAllTables(page) {
  await page.evaluate(() => {
    document.querySelectorAll('section.card .card-head button[title^="Show the values"]').forEach((b) => { if (b.getAttribute('aria-pressed') !== 'true') b.click(); });
  });
}

const cardTable = (page, title) => page.evaluate((title) => {
  const card = [...document.querySelectorAll('section.card')].find((c) => c.querySelector('h3') && c.querySelector('h3').textContent === title);
  if (!card) return null;
  const tables = [...card.querySelectorAll('table')];
  return tables.map((t) => ({
    visible: !!(t.offsetParent),
    head: [...t.querySelectorAll('thead th')].map((x) => x.textContent),
    rows: [...t.querySelectorAll('tbody tr')].map((tr) => [...tr.children].map((td) => td.textContent)),
  }));
}, title);

const snapshot = (page) => page.evaluate(() => {
  const kpis = {};
  document.querySelectorAll('.kpi').forEach((k) => { kpis[k.querySelector('.label').textContent] = { value: k.querySelector('.value').textContent, hint: k.querySelector('.hint').textContent }; });
  const tbl = (title) => {
    const card = [...document.querySelectorAll('section.card')].find((c) => c.querySelector('h3') && c.querySelector('h3').textContent === title);
    if (!card) return null;
    const t = card.querySelector('table');
    return t ? [...t.querySelectorAll('tbody tr')].map((tr) => [...tr.children].map((td) => td.textContent)) : null;
  };
  return {
    kpis,
    size: tbl('Size statistics'),
    periodStats: tbl('Merged PRs per period: statistics'),
    byType: tbl('Statistics by type'),
    duration: tbl('Duration statistics'),
    cycles: tbl('Review cycle statistics'),
    cyclesBySize: tbl('Review cycles by PR size'),
    mostCycles: tbl('Ten PRs with the most review cycles'),
    empties: [...document.querySelectorAll('.empty')].map((e) => e.textContent),
    preset: (document.querySelector('#preset') || {}).value,
    title: document.title,
  };
});

async function applyStep(page, st) {
  if (st.op === 'preset') {
    await page.selectOption('#preset', st.value);
  } else if (st.op === 'custom') {
    await page.selectOption('#preset', 'custom');
    for (const k of ['from', 'to']) {
      await page.evaluate(([k, v]) => { const i = document.querySelector(`input[aria-label=${k}]`); i.value = v; i.dispatchEvent(new Event('change', { bubbles: true })); }, [k, st[k]]);
    }
  } else if (st.op === 'type' && st.value === '__all__') {
    for (let i = 0; i < 20; i++) {
      const ok = await page.evaluate(() => { const b = document.querySelector('.chips button[aria-pressed="true"]'); if (b) b.click(); return !!b; });
      if (!ok) break;
    }
  } else if (st.op === 'type') {
    await page.click(`.chips button:has-text("${st.value}")`);
  } else if (st.op === 'bots') {
    await page.evaluate((v) => { const c = document.querySelector('.filters input[type=checkbox]'); if (c.checked !== v) c.click(); }, st.value);
  } else if (st.op === 'period') {
    await page.click(`.seg[aria-label=Period] button:has-text("${st.value[0].toUpperCase() + st.value.slice(1)}")`);
  } else if (st.op === 'table') {
    await page.evaluate((title) => {
      const card = [...document.querySelectorAll('section.card')].find((c) => c.querySelector('h3').textContent === title);
      card.querySelector('.card-head button[title^="Show the values"]').click();
    }, st.value);
  }
}

(async () => {
  const browser = await chromium.launch();
  const ds = Object.fromEntries(cfg.datasets.map((d) => [d.name, d]));

  // ---- S1: loads clean at every theme and width
  if (cfg.sections.includes('load')) {
    results.load = [];
    for (const d of cfg.datasets) {
      for (const theme of ['dark', 'light']) {
        for (const width of [1280, 820, 390]) {
          const { ctx, page, rec } = await newPage(browser, d, { theme, width });
          results.load.push({
            ds: d.name, theme, width, errors: rec.errors, pageErrors: rec.pageErrors, dialogs: rec.dialogs, attempts: rec.attempts,
            hscroll: await hScroll(page), themeAttr: await page.evaluate(() => document.documentElement.dataset.theme),
            text: await textIssues(page),
            sections: await page.evaluate(() => ['overview', 'loc', 'throughput', 'size', 'types', 'risk', 'time', 'reviews', 'dora', 'method'].filter((id) => !document.getElementById(id))),
            cards: await page.evaluate(() => document.querySelectorAll('section.card').length),
            svgs: await page.evaluate(() => document.querySelectorAll('svg').length),
            overflowing: await page.evaluate(() => [...document.querySelectorAll('body *')].filter((e) => { const r = e.getBoundingClientRect(); return r.width > 0 && r.right > window.innerWidth + 1 && !e.closest('.tablewrap') && !e.closest('svg') && !e.closest('#tip'); }).slice(0, 5).map((e) => e.tagName + '.' + e.className)),
          });
          await ctx.close();
        }
      }
    }
  }

  // ---- S2: theme default, switching, persistence
  if (cfg.sections.includes('theme')) {
    const d = ds[cfg.main];
    const out = { steps: [] };
    const { ctx, page, rec } = await newPage(browser, d, { colorScheme: 'light' });
    const probe = async (label) => out.steps.push({
      label, theme: await page.evaluate(() => document.documentElement.dataset.theme),
      surface: await page.evaluate(() => getComputedStyle(document.documentElement).getPropertyValue('--surface').trim()),
      bodyBg: await page.evaluate(() => getComputedStyle(document.body).backgroundColor),
      barFill: await page.evaluate(() => { const r = document.querySelector('svg rect.bar'); return r && r.getAttribute('fill'); }),
      lineStroke: await page.evaluate(() => { const r = document.querySelector('svg path[stroke]'); return r && r.getAttribute('stroke'); }),
      fills: await page.evaluate(() => [...new Set([...document.querySelectorAll('svg [fill], svg [stroke]')].flatMap((e) => [e.getAttribute('fill'), e.getAttribute('stroke')]).filter((v) => v && v[0] === '#'))]),
      btn: await page.evaluate(() => { const b = [...document.querySelectorAll('.filters button')].find((x) => /theme/i.test(x.textContent)); return b && [b.textContent, b.getAttribute('aria-pressed')]; }),
      stored: await page.evaluate(() => { try { return localStorage.getItem('repo-stats-theme'); } catch (e) { return 'ERR'; } }),
      chipDot: await page.evaluate(() => { const e = document.querySelector('.chips .dot'); return e && e.style.background; }),
    });
    await probe('initial (OS light, nothing stored)');
    await page.click('.filters button:has-text("theme")');
    await probe('after click 1');
    await page.reload(); await page.waitForSelector('.kpis');
    await probe('after reload');
    await page.click('.filters button:has-text("theme")');
    await probe('after click 2');
    await page.reload(); await page.waitForSelector('.kpis');
    await probe('after reload 2');
    out.errors = rec.errors.concat(rec.pageErrors);
    results.theme = out;
    await ctx.close();
    // OS dark, nothing stored
    const o2 = await newPage(browser, d, { colorScheme: 'dark' });
    results.themeOsDark = await o2.page.evaluate(() => document.documentElement.dataset.theme);
    await o2.ctx.close();
  }

  // ---- S3: every control on every dataset
  if (cfg.sections.includes('controls')) {
    results.controls = [];
    for (const d of cfg.datasets) {
      const { ctx, page, rec } = await newPage(browser, d, { theme: 'dark' });
      const issues = [];
      const check = async (label) => { const t = await textIssues(page); if (t.length) issues.push({ label, t }); };
      await check('initial');
      for (const label of ['By language', 'Test vs non-test', 'Total']) { await page.click(`button:has-text("${label}")`).catch(() => {}); await check('loc ' + label); }
      await page.click('button:has-text("By language")').catch(() => {});
      await page.click('button:has-text("Code only")').catch(() => {});
      await check('loc lang + code only');
      await page.click('button:has-text("Test vs non-test")').catch(() => {});
      await check('loc test + code only');
      await page.click('button:has-text("Total")').catch(() => {});
      await check('loc total + code only');
      const opts = await page.$$eval('select[aria-label="Duration metric"] option', (o) => o.map((x) => x.value)).catch(() => []);
      for (const v of opts) { await page.selectOption('select[aria-label="Duration metric"]', v); await check('metric ' + v); }
      for (const p of ['Day', 'Month', 'Week']) { await page.click(`.seg[aria-label=Period] button:has-text("${p}")`); await check('period ' + p); }
      await openAllTables(page);
      await check('all tables open');
      results.controls.push({ ds: d.name, issues, errors: rec.errors, pageErrors: rec.pageErrors, metricOptions: opts });
      await ctx.close();
    }
  }

  // ---- S4: filter scenarios
  if (cfg.sections.includes('filters')) {
    results.filters = [];
    for (const dn of cfg.filterDatasets) {
      for (const sc of cfg.scenarios) {
        if (sc.only && !sc.only.includes(dn)) continue;
        const { ctx, page, rec } = await newPage(browser, ds[dn], { theme: 'dark' });
        const item = { ds: dn, name: sc.name, steps: sc.steps };
        try {
          for (const st of sc.steps) await applyStep(page, st);
          item.snap = await snapshot(page);
          // day-granularity chart table sum
          await applyStep(page, { op: 'period', value: 'day' });
          await applyStep(page, { op: 'table', value: 'PRs per day' }).catch(() => {});
          const t = await cardTable(page, 'PRs per day');
          item.dayTable = t && t.length ? t[0] : null;
          item.text = await textIssues(page);
        } catch (e) { item.exception = String(e); }
        item.errors = rec.errors.concat(rec.pageErrors);
        results.filters.push(item);
        await ctx.close();
      }
    }
  }

  // ---- S5: raw stats tables for correctness comparison
  if (cfg.sections.includes('stats')) {
    results.stats = {};
    for (const d of cfg.datasets) {
      if (!cfg.statsDatasets.includes(d.name)) continue;
      const { ctx, page } = await newPage(browser, d, { theme: 'dark' });
      const snap = await snapshot(page);
      await openAllTables(page);
      results.stats[d.name] = {
        snap,
        dora: await cardTable(page, 'Deployment frequency statistics'),
        lead: await cardTable(page, 'Lead time for changes'),
        riskStats: await cardTable(page, 'Risk score statistics'),
        top: await cardTable(page, 'Ten highest-risk PRs'),
        factors: await cardTable(page, 'What drives the score'),
        sizeBuckets: await cardTable(page, 'PRs by size bucket'),
        byRisk: await cardTable(page, 'PRs by risk level'),
        byTypeChart: await cardTable(page, 'PRs by type'),
        tCdf: await cardTable(page, 'Cumulative frequency of lines changed'),
        tHist: await cardTable(page, 'Distribution of lines changed'),
      };
      await ctx.close();
    }
  }

  // ---- S6: tooltips
  if (cfg.sections.includes('tooltips')) {
    results.tooltips = [];
    for (const width of [1280, 390]) {
      const { ctx, page, rec } = await newPage(browser, ds[cfg.main], { theme: 'dark', width });
      const targets = [
        ['PRs per week', 'rect[fill=transparent]', 'bar'],
        ['Lines of code', 'rect[fill=transparent]', 'line'],
        ['Distribution of lines changed', 'rect.bar', 'hist'],
        ['Cumulative frequency of lines changed', 'rect[fill=transparent]', 'cdf'],
        ['PRs by size bucket', 'rect[fill=transparent]', 'bucketbar'],
        ['Median and p90 over time', 'rect[fill=transparent]', 'durline'],
        ['Deployments per week', 'rect[fill=transparent]', 'deploybar'],
      ];
      for (const [title, sel, kind] of targets) {
        for (const where of ['middle', 'right-edge']) {
          const r = { width, title, kind, where };
          try {
            const handle = await page.evaluateHandle(([title, sel, where]) => {
              const card = [...document.querySelectorAll('section.card')].find((c) => c.querySelector('h3').textContent === title);
              if (!card) return null;
              const els = [...card.querySelectorAll(sel)];
              if (!els.length) return null;
              return where === 'middle' ? els[Math.floor(els.length / 2)] : els[els.length - 1];
            }, [title, sel, where]);
            const el = handle.asElement();
            if (!el) { r.missing = true; results.tooltips.push(r); continue; }
            await el.evaluate((e) => e.scrollIntoView({ block: 'center' }));
            await page.waitForTimeout(50);
            const box = await el.boundingBox();
            const x = where === 'middle' ? box.x + box.width / 2 : Math.min(box.x + box.width - 2, page.viewportSize().width - 2);
            const y = box.y + Math.min(box.height - 2, Math.max(2, box.height / 2));
            await page.mouse.move(x - 6, y); await page.mouse.move(x, y);
            await page.waitForTimeout(30);
            r.tip = await page.evaluate(() => { const t = document.getElementById('tip'); const b = t.getBoundingClientRect(); return { display: getComputedStyle(t).display, text: t.innerText, left: b.left, top: b.top, right: b.right, bottom: b.bottom, vw: innerWidth, vh: innerHeight }; });
            await page.mouse.move(2, 2);
            await page.waitForTimeout(30);
            r.after = await page.evaluate(() => getComputedStyle(document.getElementById('tip')).display);
          } catch (e) { r.exception = String(e).slice(0, 200); }
          results.tooltips.push(r);
        }
      }
      results.tooltipErrors = (results.tooltipErrors || []).concat(rec.errors, rec.pageErrors);
      await ctx.close();
    }
  }

  // ---- S7: table toggles
  if (cfg.sections.includes('tables')) {
    const { ctx, page, rec } = await newPage(browser, ds[cfg.main], { theme: 'dark' });
    const titles = await page.$$eval('section.card', (cs) => cs.filter((c) => c.querySelector('button[title^="Show the values"]')).map((c) => c.querySelector('h3').textContent));
    results.tables = { titles, items: [] };
    for (const title of titles) {
      const it = { title };
      const probe = () => page.evaluate((title) => {
        const c = [...document.querySelectorAll('section.card')].find((x) => x.querySelector('h3').textContent === title);
        const b = c.querySelector('button[title^="Show the values"]');
        const chart = c.querySelector('.chart'), tw = c.querySelector('.tablewrap');
        return { pressed: b.getAttribute('aria-pressed'), chartHidden: chart.hidden, tableHidden: tw.hidden, rows: tw.querySelectorAll('tbody tr').length, text: tw.innerText.slice(0, 4000) };
      }, title);
      it.before = await probe();
      await applyStep(page, { op: 'table', value: title });
      it.on = await probe();
      await applyStep(page, { op: 'table', value: title });
      it.off = await probe();
      results.tables.items.push(it);
    }
    results.tables.errors = rec.errors.concat(rec.pageErrors);
    await ctx.close();
  }

  // ---- S9: banners and sources
  if (cfg.sections.includes('banners')) {
    results.banners = {};
    for (const d of cfg.datasets) {
      const { ctx, page } = await newPage(browser, d, { theme: 'dark' });
      results.banners[d.name] = await page.evaluate(() => ({
        banner: document.querySelector('#banner .banner').innerText,
        isOk: document.querySelector('#banner .banner').classList.contains('ok'),
        li: [...document.querySelectorAll('#banner li')].map((l) => l.textContent),
        needsApi: [...document.querySelectorAll('section.card')].filter((c) => c.querySelector('h3').textContent === 'Duration statistics').flatMap((c) => [...c.querySelectorAll('tbody tr')].map((tr) => [tr.children[0].textContent, tr.lastElementChild.textContent])),
        badges: document.querySelectorAll('.badge').length,
      }));
      await ctx.close();
    }
  }

  // ---- S10: keyboard and ARIA
  if (cfg.sections.includes('a11y')) {
    const { ctx, page } = await newPage(browser, ds[cfg.main], { theme: 'dark' });
    const seq = [];
    await page.evaluate(() => document.activeElement && document.activeElement.blur());
    for (let i = 0; i < 40; i++) {
      await page.keyboard.press('Tab');
      seq.push(await page.evaluate(() => { const a = document.activeElement; return a ? { tag: a.tagName, id: a.id, aria: a.getAttribute('aria-label'), text: (a.textContent || '').trim().slice(0, 24), pressed: a.getAttribute('aria-pressed'), inFilters: !!a.closest('#filters') } : null; }));
    }
    results.a11y = {
      seq,
      buttonsWithoutPressed: await page.evaluate(() => [...document.querySelectorAll('button')].filter((b) => !b.hasAttribute('aria-pressed')).map((b) => b.textContent.trim())),
      svgs: await page.evaluate(() => [...document.querySelectorAll('svg')].map((s) => ({ role: s.getAttribute('role'), label: s.getAttribute('aria-label') })).filter((s) => s.role !== 'img' || !s.label)),
      svgCount: await page.evaluate(() => document.querySelectorAll('svg').length),
      selectsUnlabelled: await page.evaluate(() => [...document.querySelectorAll('select, input')].filter((e) => !e.getAttribute('aria-label') && !e.closest('label')).length),
      focusableRects: await page.evaluate(() => document.querySelectorAll('svg rect[tabindex]').length),
      lang: await page.evaluate(() => document.documentElement.lang),
      h1: await page.evaluate(() => document.querySelectorAll('h1').length),
    };
    // focus ring visible on a button
    await page.focus('.filters button');
    results.a11y.outline = await page.evaluate(() => getComputedStyle(document.activeElement).outlineStyle);
    // Keyboard activation of a toggle
    await page.focus('.seg[aria-label=Period] button:has-text("Day")');
    await page.keyboard.press('Enter');
    results.a11y.dayPressedAfterEnter = await page.evaluate(() => [...document.querySelectorAll('.seg[aria-label=Period] button')].find((b) => b.textContent === 'Day').getAttribute('aria-pressed'));
    await ctx.close();
  }

  // ---- S11: screenshots
  if (cfg.sections.includes('shots')) {
    results.shots = [];
    for (const d of cfg.datasets) {
      if (!cfg.shotDatasets.includes(d.name)) continue;
      for (const theme of ['dark', 'light']) {
        for (const [label, width, height] of [['desktop', 1280, 900], ['mobile', 390, 844]]) {
          const { ctx, page } = await newPage(browser, d, { theme, width, height });
          const file = `${cfg.shotsDir}/${d.name}-${theme}-${label}.png`;
          await page.screenshot({ path: file, fullPage: true });
          results.shots.push(file);
          await ctx.close();
        }
      }
    }
  }

  // ---- S12: resize re-render, no errors
  if (cfg.sections.includes('resize')) {
    const { ctx, page, rec } = await newPage(browser, ds[cfg.main], { theme: 'dark', width: 1280 });
    const w0 = await page.evaluate(() => document.querySelector('svg').getBoundingClientRect().width);
    await page.setViewportSize({ width: 600, height: 900 });
    await page.waitForTimeout(600);
    const w1 = await page.evaluate(() => document.querySelector('svg').getBoundingClientRect().width);
    results.resize = { w0, w1, hscroll: await hScroll(page), errors: rec.errors.concat(rec.pageErrors) };
    await ctx.close();
  }

  // ---- S13: XSS probes
  if (cfg.sections.includes('xss')) {
    results.xss = {};
    for (const d of cfg.datasets) {
      if (!cfg.xssDatasets.includes(d.name)) continue;
      const { ctx, page, rec } = await newPage(browser, d, { theme: 'dark' });
      await openAllTables(page);
      await applyStep(page, { op: 'period', value: 'day' });
      results.xss[d.name] = {
        dialogs: rec.dialogs, errors: rec.errors.concat(rec.pageErrors),
        injected: await page.evaluate(() => document.querySelectorAll('img[src="x"], img[onerror], b.pwn, script:not(:first-of-type)').length),
        scripts: await page.evaluate(() => document.querySelectorAll('script').length),
        bodyHasLiteral: await page.evaluate(() => document.body.innerText.includes('<img src=x onerror=alert(1)>')),
        titleTag: await page.evaluate(() => document.title),
        h1: await page.evaluate(() => document.getElementById('title').textContent),
        pwned: await page.evaluate(() => !!window.__pwned),
      };
      await ctx.close();
    }
  }

  fs.writeFileSync(cfg.results, JSON.stringify(results));
  await browser.close();
})().catch((e) => { console.error('RUNNER FAILURE', e); process.exit(2); });
