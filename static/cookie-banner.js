/**
 * BTCPREDICTOR.IO — GDPR Cookie Banner v2.0
 * Matrix/Tron style — self-injecting (CSS + HTML + logic)
 * Single source of truth: include this script, get everything.
 *
 * Storage:
 *   btcp_ga_consent  = 'granted' | 'denied' | 'custom'  (backward compat)
 *   btcp_consent     = JSON {analytics, clarity, sentry}  (granular)
 */
(function () {
  'use strict';

  // ── Already consented? Skip banner ──
  var existing = localStorage.getItem('btcp_ga_consent');
  if (existing) {
    // Load vendors based on stored preference
    if (existing === 'granted') {
      _bootVendors({ analytics: true, clarity: true, sentry: true });
    } else if (existing === 'custom') {
      try {
        var detail = JSON.parse(localStorage.getItem('btcp_consent') || '{}');
        _bootVendors(detail);
      } catch (e) { /* corrupted — show banner again */ _injectBanner(); }
    }
    return; // 'denied' → do nothing
  }

  // ── No consent yet → show banner after 1.2s ──
  if (document.readyState === 'loading') {
    document.addEventListener('DOMContentLoaded', function () { setTimeout(_injectBanner, 1200); });
  } else {
    setTimeout(_injectBanner, 1200);
  }

  // ═══════════════════════════════════════════════════════
  //  CSS INJECTION
  // ═══════════════════════════════════════════════════════
  function _injectCSS() {
    if (document.getElementById('cb-styles')) return;
    var style = document.createElement('style');
    style.id = 'cb-styles';
    style.textContent = [
      /* ── Banner container ── */
      '#cbOverlay{position:fixed;bottom:0;left:0;right:0;z-index:99999;font-family:"Space Mono",monospace;pointer-events:none}',
      '#cbOverlay *{box-sizing:border-box}',
      '#cbBanner{pointer-events:all;position:relative;background:rgba(5,10,18,0.98);border-top:1px solid rgba(0,255,136,0.25);box-shadow:0 -6px 40px rgba(0,255,136,0.08),inset 0 1px 0 rgba(0,255,136,0.1);backdrop-filter:blur(16px);-webkit-backdrop-filter:blur(16px);padding:20px 24px 18px;animation:cbSlideUp .5s cubic-bezier(.16,1,.3,1)}',

      /* scanline overlay */
      '#cbBanner::before{content:"";position:absolute;top:0;left:0;right:0;bottom:0;background:repeating-linear-gradient(0deg,transparent,transparent 2px,rgba(0,255,136,0.015) 2px,rgba(0,255,136,0.015) 4px);pointer-events:none;z-index:0}',

      /* glow line top */
      '#cbBanner::after{content:"";position:absolute;top:-1px;left:10%;right:10%;height:1px;background:linear-gradient(90deg,transparent,rgba(0,255,136,0.6),transparent);z-index:1}',

      '@keyframes cbSlideUp{from{transform:translateY(100%);opacity:0}to{transform:translateY(0);opacity:1}}',

      /* ── Main row ── */
      '.cb-main{position:relative;z-index:2;display:flex;align-items:flex-start;gap:16px;flex-wrap:wrap}',
      '.cb-shield{font-size:22px;line-height:1;flex-shrink:0;margin-top:2px;filter:drop-shadow(0 0 6px rgba(0,255,136,0.4))}',

      /* ── Text content ── */
      '.cb-body{flex:1;min-width:260px}',
      '.cb-title{font-size:11px;font-weight:700;letter-spacing:2.5px;text-transform:uppercase;color:#00ff88;margin-bottom:8px;text-shadow:0 0 12px rgba(0,255,136,0.3)}',
      '.cb-desc{font-size:12.5px;line-height:1.65;color:rgba(200,210,228,0.85);letter-spacing:0.2px}',
      '.cb-desc strong{color:#e0e6f0;font-weight:600}',
      '.cb-links{margin-top:8px;font-size:10.5px;letter-spacing:0.5px}',
      '.cb-links a{color:#00ff88;text-decoration:none;transition:text-shadow .2s}',
      '.cb-links a:hover{text-shadow:0 0 8px rgba(0,255,136,0.5);text-decoration:underline}',
      '.cb-links .cb-sep{color:rgba(200,210,228,0.25);margin:0 6px}',

      /* ── Buttons row ── */
      '.cb-actions{display:flex;gap:8px;flex-shrink:0;align-items:center;flex-wrap:wrap;margin-top:2px}',

      /* shared btn */
      '.cb-btn{font-family:"Space Mono",monospace;font-size:10px;font-weight:700;letter-spacing:1.5px;text-transform:uppercase;cursor:pointer;padding:10px 20px;border:none;transition:all .25s ease}',

      /* accept */
      '.cb-btn-accept{background:#00ff88;color:#050a12;box-shadow:0 0 16px rgba(0,255,136,0.25),inset 0 1px 0 rgba(255,255,255,0.15)}',
      '.cb-btn-accept:hover{background:#33ffaa;box-shadow:0 0 24px rgba(0,255,136,0.4)}',

      /* customize */
      '.cb-btn-custom{background:transparent;color:#00ff88;border:1px solid rgba(0,255,136,0.35);box-shadow:0 0 8px rgba(0,255,136,0.06)}',
      '.cb-btn-custom:hover{border-color:#00ff88;box-shadow:0 0 16px rgba(0,255,136,0.15);background:rgba(0,255,136,0.05)}',

      /* decline */
      '.cb-btn-decline{background:transparent;color:rgba(200,210,228,0.5);border:1px solid rgba(200,210,228,0.15)}',
      '.cb-btn-decline:hover{color:rgba(200,210,228,0.8);border-color:rgba(200,210,228,0.3)}',

      /* ── Customize panel ── */
      '.cb-customize{position:relative;z-index:2;max-height:0;overflow:hidden;transition:max-height .4s ease,padding .4s ease,opacity .3s ease;opacity:0;padding:0 0}',
      '.cb-customize.cb-open{max-height:500px;opacity:1;padding:16px 0 4px}',

      '.cb-customize-inner{border-top:1px solid rgba(0,255,136,0.12);padding-top:14px}',

      /* category row */
      '.cb-cat{display:flex;align-items:center;justify-content:space-between;padding:10px 12px;margin-bottom:6px;background:rgba(0,255,136,0.02);border:1px solid rgba(0,255,136,0.06);border-radius:2px;transition:border-color .2s}',
      '.cb-cat:hover{border-color:rgba(0,255,136,0.15)}',
      '.cb-cat-info{flex:1;min-width:0}',
      '.cb-cat-name{display:block;font-size:11.5px;font-weight:600;color:#e0e6f0;letter-spacing:0.5px}',
      '.cb-cat-desc{display:block;font-size:10px;color:rgba(200,210,228,0.5);margin-top:2px;letter-spacing:0.3px}',
      '.cb-cat-tag{display:inline-block;font-size:8px;letter-spacing:1px;text-transform:uppercase;padding:2px 6px;margin-left:8px;border-radius:2px}',
      '.cb-tag-required{background:rgba(0,255,136,0.1);color:#00ff88}',
      '.cb-tag-optional{background:rgba(200,210,228,0.06);color:rgba(200,210,228,0.4)}',

      /* toggle switch */
      '.cb-toggle{position:relative;width:38px;height:20px;flex-shrink:0;margin-left:12px}',
      '.cb-toggle input{opacity:0;width:0;height:0;position:absolute}',
      '.cb-slider{position:absolute;top:0;left:0;right:0;bottom:0;background:rgba(200,210,228,0.1);border:1px solid rgba(200,210,228,0.15);border-radius:10px;cursor:pointer;transition:all .25s}',
      '.cb-slider::before{content:"";position:absolute;width:14px;height:14px;left:2px;bottom:2px;background:rgba(200,210,228,0.5);border-radius:50%;transition:all .25s}',
      '.cb-toggle input:checked+.cb-slider{background:rgba(0,255,136,0.15);border-color:rgba(0,255,136,0.4)}',
      '.cb-toggle input:checked+.cb-slider::before{transform:translateX(18px);background:#00ff88;box-shadow:0 0 8px rgba(0,255,136,0.4)}',
      '.cb-toggle input:disabled+.cb-slider{opacity:0.6;cursor:not-allowed}',
      '.cb-toggle input:disabled:checked+.cb-slider::before{background:#00ff88}',

      /* save button row */
      '.cb-save-row{text-align:right;margin-top:10px}',
      '.cb-btn-save{background:rgba(0,255,136,0.12);color:#00ff88;border:1px solid rgba(0,255,136,0.3);box-shadow:0 0 10px rgba(0,255,136,0.08)}',
      '.cb-btn-save:hover{background:rgba(0,255,136,0.2);box-shadow:0 0 16px rgba(0,255,136,0.15)}',

      /* hidden */
      '.cb-hidden{display:none!important}',

      /* ── Responsive ── */
      '@media(max-width:640px){',
      '  #cbBanner{padding:16px 14px 14px}',
      '  .cb-main{flex-direction:column;gap:12px}',
      '  .cb-desc{font-size:12px}',
      '  .cb-actions{width:100%;justify-content:stretch}',
      '  .cb-btn{flex:1;padding:10px 10px;font-size:9px;text-align:center}',
      '  .cb-cat{flex-wrap:wrap;gap:8px}',
      '  .cb-toggle{margin-left:0}',
      '}'
    ].join('\n');
    document.head.appendChild(style);
  }

  // ═══════════════════════════════════════════════════════
  //  HTML INJECTION
  // ═══════════════════════════════════════════════════════
  function _injectBanner() {
    _injectCSS();

    // Remove any old cookie banner if it exists
    var old = document.getElementById('cookieBanner');
    if (old) old.remove();

    var overlay = document.createElement('div');
    overlay.id = 'cbOverlay';
    overlay.innerHTML = [
      '<div id="cbBanner">',
      '  <div class="cb-main">',
      '    <div class="cb-shield">&#x1f6e1;</div>',
      '    <div class="cb-body">',
      '      <div class="cb-title">Cookie &amp; Privacy</div>',
      '      <div class="cb-desc">',
      '        Questo sito utilizza cookie tecnici necessari al funzionamento e, previo tuo consenso, strumenti analitici di terze parti per migliorare il servizio:',
      '        <strong>Google Analytics</strong> (analisi traffico), <strong>Microsoft Clarity</strong> (session replay),',
      '        <strong>Sentry</strong> (error monitoring).<br>',
      '        Nessun dato di trading o finanziario viene condiviso con terze parti. Nessun cookie di profilazione pubblicitaria.',
      '      </div>',
      '      <div class="cb-links">',
      '        <a href="/legal">Privacy &amp; Cookie Policy</a>',
      '        <span class="cb-sep">&middot;</span>',
      '        <a href="/legal">Disclaimer</a>',
      '        <span class="cb-sep">&middot;</span>',
      '        <a href="https://policies.google.com/privacy" target="_blank" rel="noopener">Google</a>',
      '        <span class="cb-sep">&middot;</span>',
      '        <a href="https://privacy.microsoft.com/privacystatement" target="_blank" rel="noopener">Microsoft</a>',
      '        <span class="cb-sep">&middot;</span>',
      '        <a href="https://sentry.io/privacy/" target="_blank" rel="noopener">Sentry</a>',
      '      </div>',
      '    </div>',
      '    <div class="cb-actions">',
      '      <button class="cb-btn cb-btn-accept" id="cbAcceptAll">ACCETTA TUTTO</button>',
      '      <button class="cb-btn cb-btn-custom" id="cbCustomizeBtn">PERSONALIZZA</button>',
      '      <button class="cb-btn cb-btn-decline" id="cbDeclineAll">RIFIUTA</button>',
      '    </div>',
      '  </div>',

      '  <div class="cb-customize" id="cbCustomizePanel">',
      '    <div class="cb-customize-inner">',

      '      <div class="cb-cat">',
      '        <div class="cb-cat-info">',
      '          <span class="cb-cat-name">Necessari <span class="cb-cat-tag cb-tag-required">SEMPRE ATTIVI</span></span>',
      '          <span class="cb-cat-desc">Cookie tecnici essenziali per il funzionamento del sito. Non possono essere disattivati.</span>',
      '        </div>',
      '        <label class="cb-toggle"><input type="checkbox" checked disabled><span class="cb-slider"></span></label>',
      '      </div>',

      '      <div class="cb-cat">',
      '        <div class="cb-cat-info">',
      '          <span class="cb-cat-name">Analytics <span class="cb-cat-tag cb-tag-optional">OPZIONALE</span></span>',
      '          <span class="cb-cat-desc">Google Analytics &mdash; analisi anonima del traffico e comportamento di navigazione.</span>',
      '        </div>',
      '        <label class="cb-toggle"><input type="checkbox" id="cbToggleGA" checked><span class="cb-slider"></span></label>',
      '      </div>',

      '      <div class="cb-cat">',
      '        <div class="cb-cat-info">',
      '          <span class="cb-cat-name">Session Replay <span class="cb-cat-tag cb-tag-optional">OPZIONALE</span></span>',
      '          <span class="cb-cat-desc">Microsoft Clarity &mdash; heatmap e replay per migliorare la user experience.</span>',
      '        </div>',
      '        <label class="cb-toggle"><input type="checkbox" id="cbToggleClarity" checked><span class="cb-slider"></span></label>',
      '      </div>',

      '      <div class="cb-cat">',
      '        <div class="cb-cat-info">',
      '          <span class="cb-cat-name">Error Monitoring <span class="cb-cat-tag cb-tag-optional">OPZIONALE</span></span>',
      '          <span class="cb-cat-desc">Sentry &mdash; rilevamento errori per garantire stabilit&agrave; del servizio.</span>',
      '        </div>',
      '        <label class="cb-toggle"><input type="checkbox" id="cbToggleSentry" checked><span class="cb-slider"></span></label>',
      '      </div>',

      '      <div class="cb-save-row">',
      '        <button class="cb-btn cb-btn-save" id="cbSaveCustom">SALVA PREFERENZE</button>',
      '      </div>',

      '    </div>',
      '  </div>',
      '</div>'
    ].join('\n');

    document.body.appendChild(overlay);

    // ── Bind events ──
    document.getElementById('cbAcceptAll').addEventListener('click', function () {
      _saveConsent('granted', { analytics: true, clarity: true, sentry: true });
    });

    document.getElementById('cbDeclineAll').addEventListener('click', function () {
      _saveConsent('denied', { analytics: false, clarity: false, sentry: false });
    });

    document.getElementById('cbCustomizeBtn').addEventListener('click', function () {
      var panel = document.getElementById('cbCustomizePanel');
      panel.classList.toggle('cb-open');
      this.textContent = panel.classList.contains('cb-open') ? 'CHIUDI' : 'PERSONALIZZA';
    });

    document.getElementById('cbSaveCustom').addEventListener('click', function () {
      var prefs = {
        analytics: document.getElementById('cbToggleGA').checked,
        clarity: document.getElementById('cbToggleClarity').checked,
        sentry: document.getElementById('cbToggleSentry').checked
      };
      var allOn = prefs.analytics && prefs.clarity && prefs.sentry;
      var allOff = !prefs.analytics && !prefs.clarity && !prefs.sentry;
      var mode = allOn ? 'granted' : allOff ? 'denied' : 'custom';
      _saveConsent(mode, prefs);
    });
  }

  // ═══════════════════════════════════════════════════════
  //  CONSENT STORAGE & DISMISS
  // ═══════════════════════════════════════════════════════
  function _saveConsent(mode, prefs) {
    localStorage.setItem('btcp_ga_consent', mode);
    localStorage.setItem('btcp_consent', JSON.stringify(prefs));

    // Dismiss banner
    var overlay = document.getElementById('cbOverlay');
    if (overlay) {
      overlay.style.transition = 'opacity .3s ease, transform .3s ease';
      overlay.style.opacity = '0';
      overlay.style.transform = 'translateY(20px)';
      setTimeout(function () { overlay.remove(); }, 350);
    }

    // Boot allowed vendors
    _bootVendors(prefs);
  }

  // ═══════════════════════════════════════════════════════
  //  VENDOR LOADING (consent-gated)
  // ═══════════════════════════════════════════════════════
  function _bootVendors(prefs) {
    if (!prefs) return;

    // Google Analytics
    if (prefs.analytics && typeof gtag === 'function') {
      gtag('consent', 'update', { 'analytics_storage': 'granted' });
      if (!document.querySelector('script[src*="googletagmanager.com/gtag"]')) {
        var s = document.createElement('script');
        s.async = true;
        s.src = 'https://www.googletagmanager.com/gtag/js?id=G-K2E2B4FVQ5';
        s.onload = function () {
          gtag('js', new Date());
          gtag('config', 'G-K2E2B4FVQ5');
        };
        document.head.appendChild(s);
      }
    }

    // Microsoft Clarity + Sentry (via existing loader if available)
    if ((prefs.clarity || prefs.sentry) && typeof window._loadAnalyticsVendors === 'function') {
      window._loadAnalyticsVendors();
    }
  }

})();
