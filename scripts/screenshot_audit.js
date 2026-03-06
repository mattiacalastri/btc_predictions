/**
 * Screenshot audit — cattura desktop + mobile di tutte le pagine del predictor
 * Include test burger menu (click + screenshot aperto)
 */
const puppeteer = require('puppeteer');
const path = require('path');
const fs = require('fs');

const BASE = 'https://web-production-e27d0.up.railway.app';
const OUT = path.join(__dirname, '..', 'screenshots');

const PAGES = [
  { name: 'home', path: '/' },
  { name: 'dashboard', path: '/dashboard' },
  { name: 'manifesto', path: '/manifesto' },
  { name: 'council', path: '/council' },
  { name: 'investors', path: '/investors' },
  { name: 'xgboost', path: '/xgboost-spiegato' },
  { name: 'audit', path: '/audit' },
  { name: 'cockpit', path: '/cockpit' },
  { name: 'support', path: '/support' },
];

const VIEWPORTS = [
  { name: 'desktop', width: 1440, height: 900 },
  { name: 'mobile', width: 375, height: 812 },
];

(async () => {
  fs.mkdirSync(OUT, { recursive: true });

  const browser = await puppeteer.launch({
    headless: 'new',
    args: ['--no-sandbox', '--disable-setuid-sandbox'],
  });

  for (const vp of VIEWPORTS) {
    const page = await browser.newPage();
    await page.setViewport({ width: vp.width, height: vp.height });

    for (const pg of PAGES) {
      const url = BASE + pg.path;
      console.log(`[${vp.name}] ${pg.name} → ${url}`);

      try {
        await page.goto(url, { waitUntil: 'networkidle2', timeout: 30000 });
        await new Promise(r => setTimeout(r, 1500)); // let animations settle

        // Screenshot pagina
        const file = path.join(OUT, `${pg.name}_${vp.name}.png`);
        await page.screenshot({ path: file, fullPage: false });
        console.log(`  ✓ saved ${path.basename(file)}`);

        // Mobile: test burger menu
        if (vp.name === 'mobile') {
          // Test gnav-burger
          const burger = await page.$('#gnavBurger');
          if (burger) {
            await burger.click();
            await new Promise(r => setTimeout(r, 500));
            const menuFile = path.join(OUT, `${pg.name}_mobile_menu_open.png`);
            await page.screenshot({ path: menuFile, fullPage: false });
            console.log(`  ✓ burger menu screenshot → ${path.basename(menuFile)}`);

            // Check if menu actually opened
            const isOpen = await page.evaluate(() => {
              const links = document.getElementById('gnavLinks');
              if (!links) return 'NO_ELEMENT';
              const style = window.getComputedStyle(links);
              return {
                hasOpenClass: links.classList.contains('gnav-open'),
                visibility: style.visibility,
                transform: style.transform,
                pointerEvents: style.pointerEvents,
              };
            });
            console.log(`  → menu state:`, JSON.stringify(isOpen));

            // Close menu
            await burger.click();
            await new Promise(r => setTimeout(r, 300));
          } else {
            console.log(`  ✗ NO #gnavBurger found!`);
          }

          // Test tron-menu (solo su dashboard)
          if (pg.name === 'dashboard') {
            const tronBtn = await page.$('#tronMenuBtn');
            if (tronBtn) {
              await tronBtn.click();
              await new Promise(r => setTimeout(r, 500));
              const tronFile = path.join(OUT, `dashboard_mobile_tronmenu_open.png`);
              await page.screenshot({ path: tronFile, fullPage: false });
              console.log(`  ✓ tron menu screenshot → ${path.basename(tronFile)}`);

              const tronState = await page.evaluate(() => {
                const drawer = document.getElementById('tronMenuDrawer');
                if (!drawer) return 'NO_ELEMENT';
                return {
                  hasOpenClass: drawer.classList.contains('open'),
                  display: window.getComputedStyle(drawer).display,
                  transform: window.getComputedStyle(drawer).transform,
                };
              });
              console.log(`  → tron menu state:`, JSON.stringify(tronState));
            }
          }
        }
      } catch (err) {
        console.error(`  ✗ ERROR: ${err.message}`);
      }
    }
    await page.close();
  }

  // Performance audit on home page
  console.log('\n── PERFORMANCE AUDIT ──');
  const perfPage = await browser.newPage();
  await perfPage.setViewport({ width: 1440, height: 900 });

  // Collect performance metrics
  await perfPage.goto(BASE + '/', { waitUntil: 'networkidle2', timeout: 30000 });

  const metrics = await perfPage.evaluate(() => {
    const perf = window.performance;
    const nav = perf.getEntriesByType('navigation')[0];
    const paint = perf.getEntriesByType('paint');
    const resources = perf.getEntriesByType('resource');

    const totalSize = resources.reduce((sum, r) => sum + (r.transferSize || 0), 0);
    const scripts = resources.filter(r => r.initiatorType === 'script');
    const styles = resources.filter(r => r.initiatorType === 'css' || r.name.endsWith('.css'));
    const images = resources.filter(r => r.initiatorType === 'img' || /\.(png|jpg|jpeg|gif|svg|webp)/.test(r.name));

    return {
      domContentLoaded: Math.round(nav.domContentLoadedEventEnd - nav.startTime),
      loadComplete: Math.round(nav.loadEventEnd - nav.startTime),
      firstPaint: paint.find(p => p.name === 'first-paint')?.startTime?.toFixed(0) || 'N/A',
      firstContentfulPaint: paint.find(p => p.name === 'first-contentful-paint')?.startTime?.toFixed(0) || 'N/A',
      totalTransferKB: Math.round(totalSize / 1024),
      totalResources: resources.length,
      scripts: scripts.length,
      stylesheets: styles.length,
      images: images.length,
      largestResources: resources
        .sort((a, b) => (b.transferSize || 0) - (a.transferSize || 0))
        .slice(0, 5)
        .map(r => ({ name: r.name.split('/').pop(), sizeKB: Math.round((r.transferSize || 0) / 1024) })),
    };
  });

  console.log('DOM Content Loaded:', metrics.domContentLoaded, 'ms');
  console.log('Full Load:', metrics.loadComplete, 'ms');
  console.log('First Paint:', metrics.firstPaint, 'ms');
  console.log('FCP:', metrics.firstContentfulPaint, 'ms');
  console.log('Total Transfer:', metrics.totalTransferKB, 'KB');
  console.log('Resources:', metrics.totalResources, `(${metrics.scripts} JS, ${metrics.stylesheets} CSS, ${metrics.images} img)`);
  console.log('Largest resources:', JSON.stringify(metrics.largestResources, null, 2));

  // Check for console errors
  const errors = [];
  perfPage.on('console', msg => { if (msg.type() === 'error') errors.push(msg.text()); });
  await perfPage.reload({ waitUntil: 'networkidle2' });
  await new Promise(r => setTimeout(r, 2000));
  if (errors.length) {
    console.log('\nCONSOLE ERRORS:');
    errors.forEach(e => console.log(' -', e));
  } else {
    console.log('\nNo console errors detected.');
  }

  await perfPage.close();
  await browser.close();
  console.log('\n✓ Done. Screenshots in:', OUT);
})();
