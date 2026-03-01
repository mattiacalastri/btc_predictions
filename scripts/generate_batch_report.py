#!/usr/bin/env python3
"""
BTC Predictor Bot — Batch Report PDF Generator
Genera il report completo del batch "Confidence Fix Hardening" (sess.75/76)
6 cloni + risultati + analisi expectancy + commit log
"""

import os
import json
import subprocess
from datetime import datetime
from pathlib import Path

from reportlab.lib.pagesizes import A4
from reportlab.lib.units import mm, cm
from reportlab.lib.colors import HexColor, white, black
from reportlab.lib.styles import ParagraphStyle
from reportlab.lib.enums import TA_LEFT, TA_CENTER, TA_RIGHT
from reportlab.platypus import (
    BaseDocTemplate, PageTemplate, Frame, Paragraph, Spacer, Table,
    TableStyle, PageBreak, KeepTogether, HRFlowable
)
from reportlab.graphics.shapes import Drawing, Rect, String, Line
from reportlab.pdfbase import pdfmetrics

# ──────────────────────────────────────────────
# CONFIG
# ──────────────────────────────────────────────
REPO = Path(__file__).parent.parent
OUTPUT_PATH = REPO / "scripts" / "results" / "BATCH_REPORT_sess75_76.pdf"

DARK_BG     = HexColor("#0f1117")
CARD_BG     = HexColor("#1a1d27")
ACCENT      = HexColor("#00d4aa")
ACCENT2     = HexColor("#7b61ff")
RED         = HexColor("#ff4d6d")
YELLOW      = HexColor("#ffd166")
GREEN       = HexColor("#06d6a0")
MUTED       = HexColor("#6b7280")
LIGHT       = HexColor("#e2e8f0")
WHITE       = HexColor("#ffffff")
BODY        = HexColor("#c9d1d9")
ROW_ALT     = HexColor("#1e2233")
ROW_BASE    = HexColor("#161925")
BORDER      = HexColor("#2d3148")
HEADER_BG   = HexColor("#0d1021")

PAGE_W, PAGE_H = A4
MARGIN = 1.8 * cm

# ──────────────────────────────────────────────
# DATA
# ──────────────────────────────────────────────
CLONES = [
    {
        "id": "C3", "name": "Security", "model": "Sonnet 4.6",
        "cost": 1.07, "elapsed": 114, "status": "✅ done",
        "color": HexColor("#f97316"),
        "tasks_done": [
            "security_audit.py — 0 CRITICAL nel proprio territorio",
            ".env.example: CONF_THRESHOLD commentato a 0.56, variabili dead rimosse",
            "SECURITY.md: sezione 'Recent Changes' con 3 fix del batch",
            "CVE spot-check: Flask 3.0 / requests 2.32 / werkzeug 3.0 — nessun HIGH/CRITICAL attivo",
        ],
        "findings": [
            ("WARN", "BLOCKED_HOURS_UTC non è env var su Railway — hardcoded in n8n"),
            ("INFO", "mcp.json e opencode.json gitignored ✅"),
            ("INFO", "Nessun secret hardcoded nei .py del proprio territorio"),
        ],
    },
    {
        "id": "C4", "name": "Compliance", "model": "Sonnet 4.6",
        "cost": 1.12, "elapsed": 121, "status": "✅ done",
        "color": HexColor("#a78bfa"),
        "tasks_done": [
            "Disclaimer MiFID II + EU AI Act verificati su tutte le 12 pagine pages/",
            "AI disclosure aggiornato: 'parametri del sistema aggiornati periodicamente'",
            "privacy.html: mention soglie di confidenza calibrate su dati storici",
            "investors.html: aggiunta nota funding rate nel PnL netto",
            "README.md: disclaimer presente, no claim rendimento, link privacy OK",
        ],
        "findings": [
            ("OK", "Tutte le 12 pagine hanno disclaimer MiFID II + AI Act"),
            ("OK", "README: nessun claim di rendimento garantito"),
            ("INFO", "investors.html: funding rate disclosure aggiunto"),
        ],
    },
    {
        "id": "C5", "name": "R&D / ML", "model": "Sonnet 4.6",
        "cost": 1.44, "elapsed": 179, "status": "✅ done",
        "color": HexColor("#38bdf8"),
        "tasks_done": [
            "CRITICAL-1: StratifiedKFold(shuffle=True) → TimeSeriesSplit(n_splits=5)",
            "CRITICAL-1b: confidence NULL→None (non 0) in build_dataset.py",
            "MIN_SAMPLES=50 guard in train_xgboost.py — abort se dataset troppo piccolo",
            "hour_utc aggiunto a build_dataset.py row_to_csv_dict()",
            "Model versioning: archive/ con timestamp, max 10 versioni, model_metadata.json",
        ],
        "findings": [
            ("CRITICAL→FIXED", "TimeSeriesSplit: look-ahead bias eliminato (era 3-8% accuracy overestimate)"),
            ("CRITICAL→FIXED", "confidence=0 era fuori range [0.5,1.0] → ora None → dropna()"),
            ("OK", "MIN_SAMPLES guard previene training su dati insufficienti"),
        ],
    },
    {
        "id": "C6", "name": "Trading", "model": "Opus 4.6",
        "cost": 1.60, "elapsed": 217, "status": "✅ done",
        "color": HexColor("#fbbf24"),
        "tasks_done": [
            "Strategie G (THRESHOLD_056) e H (FULL_STACK_CORRECTED) aggiunte a backtest.py",
            "Expectancy Framework completo: E, Profit Factor, Kelly f*, max consec. losses, max drawdown",
            "What-if analysis: E_expected = +0.35x con tutti e 3 i fix (vs -0.01x attuale)",
            "analyze_errors.py: SSL fix + analisi technical_score + pattern combinati (value trap)",
            "performance.md: storico sess.75/76 + strategie G/H + nota TAKER_FEE 10x error",
        ],
        "findings": [
            ("P0", "Kelly negativo con WR=33% — sistema senza edge a soglia 0.62"),
            ("OK", "Con tutti i fix: E = +0.35x, Kelly = 17.5% (bot usa 2% conservativo)"),
            ("WARN", "Dead hours min_sample=4 non significativo — alzare a 15+ con più dati"),
        ],
    },
    {
        "id": "C1", "name": "Full Stack", "model": "Opus 4.6",
        "cost": 1.54, "elapsed": 182, "status": "✅ done",
        "color": HexColor("#00d4aa"),
        "tasks_done": [
            "Funding Rate nel PnL: _get_funding_fee() legge unrealizedFunding da Kraken",
            "onchain_commit_tx NULL fix: verify-after-write + cockpit alert se hash=NULL",
            "Auto-retrain pipeline: /auto-retrain endpoint + rate limit 6h + background thread",
            "datetime import alias fix (_dt.datetime invece di datetime.datetime)",
            "_model_lock threading.Lock() per XGBoost hot-reload thread-safe",
        ],
        "findings": [
            ("P0→FIXED", "Funding fee ora inclusa nel pnl_net — corretto per contratti perpetui"),
            ("P0→FIXED", "onchain_commit_tx NULL: verify-after-write su /commit-prediction e /resolve-prediction"),
            ("INFO", "circuit breaker: ora ignora le perdite pre-resume per evitare loop"),
        ],
    },
    {
        "id": "C2", "name": "Blockchain", "model": "Sonnet 4.6",
        "cost": 1.37, "elapsed": 302, "status": "✅ done",
        "color": HexColor("#fb923c"),
        "tasks_done": [
            "CRITICAL F-09: _nonce_lock (threading.Lock) + retry 3x su 'replacement underpriced'",
            "_with_retry(fn, retries=3, delays=(2,4,8)) per tutte le RPC Polygon",
            "TX_DELAY_SEC → _wait_for_onchain_confirmation() con poll Supabase ogni 3s (timeout 30s)",
            "[ONCHAIN_SUMMARY] Committed/Resolved/Errors/Skipped a fine main()",
            "Contract audit: collisione bet_id in ~24.038 anni, gasPrice 30 Gwei adeguato, front-running trascurabile",
        ],
        "findings": [
            ("CRITICAL→FIXED", "Nonce race condition F-09: threading.Lock garantisce serializzazione"),
            ("OK", "bet_id offset collision: ~24.038 anni a ritmo attuale (10 bet/giorno) — nessun rischio"),
            ("WARN", "F-08/F-09 in app.py Flask restano P1 per C1 — onchain_monitor.py ora protetto"),
        ],
    },
]

COMMITS = [
    ("71231c2", "fix: datetime import alias — use _dt instead of datetime"),
    ("1e960ec", "fix(P0): verify onchain commit/resolve hash persisted in Supabase"),
    ("02fe042", "fix: add _model_lock for thread-safe XGBoost hot-reload"),
    ("c2982f2", "fix: circuit breaker resume loop — ignore pre-resume losses"),
    ("aed60f7", "feat: daily auto-retrain pipeline via n8n at 03:00 UTC"),
    ("1a25beb", "feat(P0): include funding rate in PnL calculation"),
    ("2b236ad", "fix(P0): rewrite backfill script to match /ghost-evaluate gold standard"),
    ("855c7a4", "fix: audit critical fixes — nonce lock, receipt wait, temporal CV, gas limit"),
    ("1327e8b", "fix(threshold): lower confidence threshold 0.62→0.56, add hour filter + tech score penalty"),
    ("c628209", "fix(P0): correct TAKER_FEE from 0.005% to 0.05% (10x underestimate)"),
]

EXPECTANCY_DATA = [
    ("Scenario", "Win Rate", "Expectancy (RR 2:1)", "$/bet (size $130)"),
    ("Attuale (soglia 0.62, nessun fix)", "~33%", "−0.01x", "−$0.01"),
    ("+ Hour filter (blocca ore 1/5/7/10 UTC)", "~37%", "+0.11x", "+$0.14"),
    ("+ Soglia 0.56 (in prod da 1 Mar 2026)", "~43%", "+0.30x", "+$0.39"),
    ("+ Tech score penalty (late-consensus)", "~45%", "+0.35x", "+$0.46"),
]

# ──────────────────────────────────────────────
# PAGE TEMPLATE
# ──────────────────────────────────────────────
def _build_page(canvas, doc):
    canvas.saveState()

    # Full dark background
    canvas.setFillColor(DARK_BG)
    canvas.rect(0, 0, PAGE_W, PAGE_H, fill=1, stroke=0)

    # Top accent bar
    canvas.setFillColor(ACCENT)
    canvas.rect(0, PAGE_H - 4, PAGE_W, 4, fill=1, stroke=0)

    # Header strip
    canvas.setFillColor(HEADER_BG)
    canvas.rect(0, PAGE_H - 36, PAGE_W, 32, fill=1, stroke=0)

    # Header text left
    canvas.setFillColor(ACCENT)
    canvas.setFont("Helvetica-Bold", 9)
    canvas.drawString(MARGIN, PAGE_H - 23, "BTC PREDICTOR BOT")

    canvas.setFillColor(MUTED)
    canvas.setFont("Helvetica", 8)
    canvas.drawString(MARGIN + 120, PAGE_H - 23, "btcpredictor.io")

    # Header text right
    canvas.setFillColor(MUTED)
    canvas.setFont("Helvetica", 8)
    canvas.drawRightString(PAGE_W - MARGIN, PAGE_H - 23,
                           "Batch Report | sess.75/76 | 1–2 Mar 2026")

    # Bottom bar
    canvas.setFillColor(HEADER_BG)
    canvas.rect(0, 0, PAGE_W, 24, fill=1, stroke=0)

    canvas.setFillColor(MUTED)
    canvas.setFont("Helvetica", 7.5)
    canvas.drawString(MARGIN, 8.5, "Documento confidenziale — uso interno | Sistema sperimentale, capitale proprio")
    canvas.drawRightString(PAGE_W - MARGIN, 8.5, f"Pag. {doc.page}")

    canvas.restoreState()


def make_doc(path):
    doc = BaseDocTemplate(
        str(path),
        pagesize=A4,
        leftMargin=MARGIN,
        rightMargin=MARGIN,
        topMargin=MARGIN + 22,
        bottomMargin=MARGIN + 10,
    )
    frame = Frame(
        MARGIN, MARGIN + 10,
        PAGE_W - 2 * MARGIN,
        PAGE_H - 2 * MARGIN - 32,
        id='main',
    )
    doc.addPageTemplates([PageTemplate(id='main', frames=[frame], onPage=_build_page)])
    return doc


# ──────────────────────────────────────────────
# STYLES
# ──────────────────────────────────────────────
def S(name, **kw):
    defaults = dict(fontName='Helvetica', fontSize=10, leading=14,
                    textColor=BODY, spaceAfter=4)
    defaults.update(kw)
    return ParagraphStyle(name, **defaults)

TITLE   = S('t', fontName='Helvetica-Bold', fontSize=28, leading=34, textColor=WHITE, alignment=TA_CENTER, spaceAfter=4)
TITLE2  = S('t2', fontName='Helvetica-Bold', fontSize=16, leading=20, textColor=ACCENT, alignment=TA_CENTER, spaceAfter=2)
META    = S('meta', fontSize=9, textColor=MUTED, alignment=TA_CENTER, spaceAfter=2)
SEC     = S('sec', fontName='Helvetica-Bold', fontSize=13, textColor=ACCENT, spaceAfter=6, spaceBefore=14)
SEC2    = S('sec2', fontName='Helvetica-Bold', fontSize=11, textColor=LIGHT, spaceAfter=4, spaceBefore=8)
BODY_S  = S('body', fontSize=9.5, textColor=BODY, leading=14, spaceAfter=3)
BODY_B  = S('bodyb', fontName='Helvetica-Bold', fontSize=9.5, textColor=LIGHT, leading=14, spaceAfter=3)
MONO    = S('mono', fontName='Courier', fontSize=8.5, textColor=ACCENT, leading=12, spaceAfter=2)
QUOTE   = S('quote', fontSize=9, textColor=MUTED, leftIndent=16, spaceAfter=2, spaceBefore=2)
LABEL   = S('lbl', fontName='Helvetica-Bold', fontSize=8, textColor=MUTED, spaceAfter=1)
SMALL   = S('sm', fontSize=8, textColor=MUTED, leading=11)
WARN_S  = S('warn', fontSize=9, textColor=YELLOW, fontName='Helvetica-Bold', spaceAfter=2)
ERR_S   = S('err', fontSize=9, textColor=RED, fontName='Helvetica-Bold', spaceAfter=2)
OK_S    = S('ok', fontSize=9, textColor=GREEN, fontName='Helvetica-Bold', spaceAfter=2)


def hr():
    return HRFlowable(width="100%", thickness=0.5, color=BORDER, spaceAfter=6, spaceBefore=6)

def sp(h=6):
    return Spacer(1, h)


# ──────────────────────────────────────────────
# TABLE HELPERS
# ──────────────────────────────────────────────
def dark_table(data, col_widths, style_extra=None):
    n_rows = len(data)
    base_style = [
        ('BACKGROUND', (0, 0), (-1, 0), ACCENT),
        ('TEXTCOLOR', (0, 0), (-1, 0), DARK_BG),
        ('FONTNAME', (0, 0), (-1, 0), 'Helvetica-Bold'),
        ('FONTSIZE', (0, 0), (-1, 0), 8.5),
        ('ALIGN', (0, 0), (-1, 0), 'CENTER'),
        ('TOPPADDING', (0, 0), (-1, 0), 5),
        ('BOTTOMPADDING', (0, 0), (-1, 0), 5),
        ('GRID', (0, 0), (-1, -1), 0.4, BORDER),
        ('ROWBACKGROUNDS', (0, 1), (-1, -1), [ROW_BASE, ROW_ALT]),
        ('FONTNAME', (0, 1), (-1, -1), 'Helvetica'),
        ('FONTSIZE', (0, 1), (-1, -1), 8.5),
        ('TEXTCOLOR', (0, 1), (-1, -1), BODY),
        ('ALIGN', (0, 1), (-1, -1), 'LEFT'),
        ('VALIGN', (0, 0), (-1, -1), 'MIDDLE'),
        ('TOPPADDING', (0, 1), (-1, -1), 4),
        ('BOTTOMPADDING', (0, 1), (-1, -1), 4),
        ('LEFTPADDING', (0, 0), (-1, -1), 8),
        ('RIGHTPADDING', (0, 0), (-1, -1), 8),
    ]
    if style_extra:
        base_style.extend(style_extra)
    t = Table(data, colWidths=col_widths)
    t.setStyle(TableStyle(base_style))
    return t


def p(text, style=None):
    return Paragraph(text, style or BODY_S)


# ──────────────────────────────────────────────
# COVER PAGE
# ──────────────────────────────────────────────
def cover_page(story):
    story.append(sp(30))

    # Big badge
    d = Drawing(PAGE_W - 2*MARGIN, 80)
    d.add(Rect(0, 0, PAGE_W - 2*MARGIN, 80, fillColor=CARD_BG, strokeColor=ACCENT, strokeWidth=1.5, rx=8, ry=8))
    d.add(String((PAGE_W - 2*MARGIN)/2, 52, "BATCH REPORT", fontSize=11, fontName='Helvetica-Bold',
                 fillColor=ACCENT.clone(), textAnchor='middle'))
    d.add(String((PAGE_W - 2*MARGIN)/2, 30, "sess.75 / sess.76", fontSize=32, fontName='Helvetica-Bold',
                 fillColor=white, textAnchor='middle'))
    d.add(String((PAGE_W - 2*MARGIN)/2, 10, "Confidence Fix Hardening", fontSize=13, fontName='Helvetica',
                 fillColor=HexColor("#a5b4fc"), textAnchor='middle'))
    story.append(d)
    story.append(sp(20))

    story.append(Paragraph("BTC Predictor Bot", TITLE))
    story.append(sp(4))
    story.append(Paragraph("6 AI Clones · 10 Commits · $8.14 totale", TITLE2))
    story.append(sp(6))
    story.append(Paragraph("1–2 Marzo 2026  |  Sistema LIVE  |  btcpredictor.io", META))
    story.append(sp(30))

    # KPI cards
    kpis = [
        ("6 / 6", "CLONI COMPLETATI", GREEN),
        ("$8.14", "COSTO TOTALE", ACCENT),
        ("~5 min", "TEMPO MEDIO", ACCENT2),
        ("10", "COMMIT", YELLOW),
    ]
    w = (PAGE_W - 2*MARGIN) / 4
    d2 = Drawing(PAGE_W - 2*MARGIN, 70)
    for i, (val, lbl, col) in enumerate(kpis):
        x = i * w
        d2.add(Rect(x+2, 0, w-4, 68, fillColor=CARD_BG, strokeColor=BORDER, strokeWidth=0.5, rx=6, ry=6))
        d2.add(String(x + w/2, 42, val, fontSize=22, fontName='Helvetica-Bold',
                      fillColor=col, textAnchor='middle'))
        d2.add(String(x + w/2, 22, lbl, fontSize=7.5, fontName='Helvetica-Bold',
                      fillColor=MUTED, textAnchor='middle'))
    story.append(d2)
    story.append(sp(30))

    # Summary table
    story.append(p("RIEPILOGO ORCHESTRAZIONE", LABEL))
    rows = [["Clone", "Ruolo", "Modello", "Costo", "Durata", "Status"]]
    for c in CLONES:
        rows.append([
            c["id"], c["name"], c["model"],
            f"${c['cost']:.2f}", f"{c['elapsed']//60}m {c['elapsed']%60}s",
            c["status"],
        ])
    rows.append(["TOTALE", "", "", "$8.14", "~5 min", "6/6 ✅"])

    col_w = [(PAGE_W - 2*MARGIN) / 6] * 6
    extras = [
        ('FONTNAME', (0, -1), (-1, -1), 'Helvetica-Bold'),
        ('TEXTCOLOR', (0, -1), (-1, -1), ACCENT),
        ('BACKGROUND', (0, -1), (-1, -1), HEADER_BG),
    ]
    story.append(dark_table(rows, col_w, extras))
    story.append(sp(20))

    story.append(Paragraph(
        'Documento generato automaticamente dal sistema di orchestrazione multi-clone. '
        'Ogni sezione rappresenta il lavoro di un agente AI specializzato operante in parallelo. '
        'Sistema sperimentale — capitale proprio — nessuna consulenza finanziaria.',
        QUOTE
    ))
    story.append(PageBreak())


# ──────────────────────────────────────────────
# P0 FIXES EXECUTIVE SUMMARY
# ──────────────────────────────────────────────
def p0_summary(story):
    story.append(Paragraph("▸ P0 FIX — EXECUTIVE SUMMARY", SEC))
    story.append(p(
        "Il batch sess.75/76 ha implementato e verificato i 3 fix P0 identificati dall'analisi "
        "di confidence inversion. Di seguito l'impatto atteso sull'expectancy del sistema.",
        BODY_S
    ))
    story.append(sp(8))

    # Expectancy progression table
    story.append(p("PROGRESSIONE EXPECTANCY CON I FIX", LABEL))
    col_w = [
        (PAGE_W - 2*MARGIN) * 0.40,
        (PAGE_W - 2*MARGIN) * 0.18,
        (PAGE_W - 2*MARGIN) * 0.22,
        (PAGE_W - 2*MARGIN) * 0.20,
    ]
    extras = [
        ('TEXTCOLOR', (2, 2), (2, 2), YELLOW),   # +0.11
        ('TEXTCOLOR', (2, 3), (2, 3), GREEN),    # +0.30
        ('TEXTCOLOR', (2, 4), (2, 4), GREEN),    # +0.35
        ('FONTNAME', (2, 4), (2, 4), 'Helvetica-Bold'),
        ('BACKGROUND', (0, 4), (-1, 4), HexColor("#0d2b1f")),
    ]
    story.append(dark_table(EXPECTANCY_DATA, col_w, extras))
    story.append(sp(12))

    # Break-even formula
    story.append(Paragraph("FORMULA EXPECTANCY  (Risk:Reward = 2:1)", SEC2))
    story.append(Paragraph("E = WR × 2 − (1 − WR) × 1 = 3×WR − 1", MONO))
    story.append(Paragraph("Break-even WR = 33.3%  →  qualsiasi WR > 33.4% genera edge positivo", BODY_S))
    story.append(sp(6))

    # Kelly
    story.append(Paragraph("KELLY CRITERION con tutti i fix (WR=45%, RR 2:1)", SEC2))
    story.append(Paragraph("f* = (p×b − q) / b = (0.45×2 − 0.55) / 2 = 17.5%", MONO))
    story.append(p(
        "Il bot usa ~2% del capitale per bet → molto conservativo. Corretto per la fase di "
        "validazione. Kelly può essere aumentato progressivamente dopo 100+ bet live con WR ≥ 43%.",
        BODY_S
    ))
    story.append(sp(6))

    # Warning box
    d = Drawing(PAGE_W - 2*MARGIN, 44)
    d.add(Rect(0, 0, PAGE_W - 2*MARGIN, 44, fillColor=HexColor("#2d1a00"), strokeColor=YELLOW, strokeWidth=1, rx=4, ry=4))
    d.add(String(12, 28, "⚠  INCERTEZZA STATISTICA", fontSize=8.5, fontName='Helvetica-Bold',
                 fillColor=YELLOW, textAnchor='start'))
    d.add(String(12, 12, "Con N < 200 bet, IC 95% per WR=45% = [35%, 55%]. Queste stime sono DIREZIONALI. "
                 "Monitorare WR rolling a 50 bet.",
                 fontSize=8, fontName='Helvetica', fillColor=HexColor("#fde68a"), textAnchor='start'))
    story.append(d)
    story.append(PageBreak())


# ──────────────────────────────────────────────
# CLONE SECTIONS
# ──────────────────────────────────────────────
def clone_section(story, c):
    # Clone header card
    d = Drawing(PAGE_W - 2*MARGIN, 48)
    d.add(Rect(0, 0, PAGE_W - 2*MARGIN, 48, fillColor=CARD_BG, strokeColor=c["color"],
               strokeWidth=1.5, rx=6, ry=6))
    d.add(String(16, 30, f"{c['id']}  —  {c['name']}", fontSize=15, fontName='Helvetica-Bold',
                 fillColor=white, textAnchor='start'))
    d.add(String(16, 12, f"Modello: {c['model']}  |  Costo: ${c['cost']:.2f}  |  "
                 f"Durata: {c['elapsed']//60}m {c['elapsed']%60}s  |  {c['status']}",
                 fontSize=8.5, fontName='Helvetica', fillColor=MUTED, textAnchor='start'))
    story.append(d)
    story.append(sp(10))

    # Tasks done
    story.append(Paragraph("TASK COMPLETATI", LABEL))
    for task in c["tasks_done"]:
        story.append(Paragraph(f"<font color='#06d6a0'>✓</font>  {task}", BODY_S))
    story.append(sp(8))

    # Findings
    story.append(Paragraph("FINDINGS", LABEL))
    for sev, msg in c["findings"]:
        if "CRITICAL" in sev or "P0" in sev:
            if "FIXED" in sev or "→FIXED" in sev:
                style = OK_S
                icon = "✅"
            else:
                style = ERR_S
                icon = "🔴"
        elif "WARN" in sev or "P1" in sev:
            style = WARN_S
            icon = "⚠"
        elif "OK" in sev:
            style = OK_S
            icon = "✅"
        else:
            style = BODY_S
            icon = "ℹ"
        story.append(Paragraph(f"<b>[{sev}]</b>  {msg}", style))

    story.append(hr())
    story.append(sp(4))


# ──────────────────────────────────────────────
# COMMIT LOG
# ──────────────────────────────────────────────
def commit_log(story):
    story.append(Paragraph("▸ COMMIT LOG — Batch sess.75/76", SEC))
    story.append(p(
        "Tutti i commit sono stati generati dai cloni AI o dalla sessione principale. "
        "Nessun push forzato — CI verde su tutti i branch.",
        BODY_S
    ))
    story.append(sp(8))

    rows = [["Hash", "Messaggio"]]
    for h, msg in COMMITS:
        rows.append([h, msg])

    col_w = [(PAGE_W - 2*MARGIN) * 0.15, (PAGE_W - 2*MARGIN) * 0.85]
    extras = [
        ('FONTNAME', (0, 1), (0, -1), 'Courier'),
        ('TEXTCOLOR', (0, 1), (0, -1), ACCENT),
        ('FONTSIZE', (0, 1), (0, -1), 8),
    ]
    story.append(dark_table(rows, col_w, extras))
    story.append(sp(16))


# ──────────────────────────────────────────────
# OPEN ISSUES
# ──────────────────────────────────────────────
def open_issues(story):
    story.append(Paragraph("▸ ISSUE APERTE — Prossimi passi", SEC))

    issues = [
        ("P1", "C1", "hour_utc in /predict-xgb response",
         "Esporre hour_utc per C5 come feature. Non ancora implementato."),
        ("P1", "C2", "F-08/F-09 in app.py Flask (Railway)",
         "wait_for_receipt lato server Railway + threading.Lock endpoint. Delegato a C1 per prossimo batch."),
        ("P1", "C5", "Retrain con TimeSeriesSplit + confidence NULL",
         "Eseguire build_dataset.py + train_xgboost.py dopo 50+ bet reali con i fix live."),
        ("P1", "C6", "Dead hours min_sample=4 → 15+",
         "Alzare quando il dataset supera 100 bet. Ora statistically non significativo."),
        ("P2", "SEC", "BLOCKED_HOURS_UTC come env var Railway",
         "Ora hardcoded in n8n. Aggiungere come env var per configurabilità remota."),
        ("P2", "ALL", "Cockpit: cockpit_events non mostra agenti in real-time",
         "Push Supabase dall'orchestratore fallisce silenziosamente se la tabella non esiste."),
    ]

    rows = [["Priorità", "Owner", "Issue", "Descrizione"]]
    for prio, owner, issue, desc in issues:
        rows.append([prio, owner, issue, desc])

    col_w = [
        (PAGE_W - 2*MARGIN) * 0.08,
        (PAGE_W - 2*MARGIN) * 0.07,
        (PAGE_W - 2*MARGIN) * 0.30,
        (PAGE_W - 2*MARGIN) * 0.55,
    ]
    p1_rows = [i+1 for i,(pr,*_) in enumerate(issues) if pr == "P1"]
    extras = []
    for r in p1_rows:
        extras.append(('TEXTCOLOR', (0, r), (0, r), YELLOW))
    story.append(dark_table(rows, col_w, extras))
    story.append(sp(12))


# ──────────────────────────────────────────────
# SIGNATURE / FOOTER
# ──────────────────────────────────────────────
def signature(story):
    story.append(sp(16))
    story.append(hr())
    story.append(sp(8))

    d = Drawing(PAGE_W - 2*MARGIN, 60)
    d.add(Rect(0, 0, PAGE_W - 2*MARGIN, 60, fillColor=CARD_BG, strokeColor=BORDER, strokeWidth=0.5, rx=4, ry=4))
    d.add(String(16, 42, "Blockchain Audit Trail", fontSize=8.5, fontName='Helvetica-Bold',
                 fillColor=MUTED, textAnchor='start'))
    d.add(String(16, 26, "Contratto: BTCBotAudit.sol su Polygon PoS", fontSize=8, fontName='Helvetica',
                 fillColor=BODY, textAnchor='start'))
    d.add(String(16, 12, "0xe4661F7dB62644951Eb1F9Fd23DB90e647833a55  |  Verifica: polygonscan.com",
                 fontSize=7.5, fontName='Courier', fillColor=ACCENT, textAnchor='start'))

    ts = datetime.now().strftime("%d %b %Y, %H:%M UTC")
    d.add(String(PAGE_W - 2*MARGIN - 16, 42, "Generato:", fontSize=8.5, fontName='Helvetica-Bold',
                 fillColor=MUTED, textAnchor='end'))
    d.add(String(PAGE_W - 2*MARGIN - 16, 26, ts, fontSize=8, fontName='Helvetica',
                 fillColor=BODY, textAnchor='end'))
    d.add(String(PAGE_W - 2*MARGIN - 16, 12, "btcpredictor.io  |  sistema sperimentale",
                 fontSize=7.5, fontName='Helvetica', fillColor=MUTED, textAnchor='end'))
    story.append(d)


# ──────────────────────────────────────────────
# BUILD
# ──────────────────────────────────────────────
def build():
    doc = make_doc(OUTPUT_PATH)
    story = []

    cover_page(story)
    p0_summary(story)

    # Clone sections — grouped in pairs
    story.append(Paragraph("▸ RISULTATI PER CLONE", SEC))
    story.append(p(
        "Ogni clone ha operato in isolamento sul proprio territorio di file. "
        "Il protocollo AGENT_HANDOFF.md ha coordinato LOCK/UNLOCK per evitare conflitti.",
        BODY_S
    ))
    story.append(sp(8))

    for clone in CLONES:
        clone_section(story, clone)

    story.append(PageBreak())
    commit_log(story)
    open_issues(story)
    signature(story)

    doc.build(story)
    print(f"✅ PDF generato: {OUTPUT_PATH}")
    return str(OUTPUT_PATH)


if __name__ == "__main__":
    path = build()
    # Open on Mac
    subprocess.run(["open", path])
    print("📄 PDF aperto su Mac")
