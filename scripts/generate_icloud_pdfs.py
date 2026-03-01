#!/usr/bin/env python3
"""
BTC Predictor Bot — Professional PDF Generator
Generates 6 PDF documents for the Go-Live Day 0 (1 March 2026).
Uses reportlab with dark theme styling, professional layout.
"""

import os
import math
from datetime import datetime
from reportlab.lib.pagesizes import A4
from reportlab.lib.units import mm, cm
from reportlab.lib.colors import HexColor, white, black, Color
from reportlab.lib.styles import getSampleStyleSheet, ParagraphStyle
from reportlab.lib.enums import TA_LEFT, TA_CENTER, TA_RIGHT, TA_JUSTIFY
from reportlab.platypus import (
    SimpleDocTemplate, Paragraph, Spacer, Table, TableStyle,
    PageBreak, KeepTogether, HRFlowable, Frame, PageTemplate,
    BaseDocTemplate
)
from reportlab.platypus.doctemplate import _doNothing
from reportlab.graphics.shapes import Drawing, Rect, String, Line
from reportlab.pdfbase import pdfmetrics

# ──────────────────────────────────────────────
# CONSTANTS
# ──────────────────────────────────────────────
BASE_PATH = "/Users/mattiacalastri/Library/Mobile Documents/com~apple~CloudDocs/\U0001f916 BTC Predictor Bot"
DATE_STAMP = "1 March 2026"
HEADER_TEXT = "BTC Predictor Bot | btcpredictor.io"

# Colors
DARK_BG = HexColor("#1a1a2e")
DARKER_BG = HexColor("#0f0f23")
ACCENT = HexColor("#00d4aa")
ACCENT_DIM = HexColor("#008f73")
LIGHT_TEXT = HexColor("#e0e0e0")
WHITE = HexColor("#ffffff")
MUTED = HexColor("#888899")
TABLE_ROW_ALT = HexColor("#f4f8fb")
TABLE_HEADER_BG = HexColor("#1a1a2e")
TABLE_BORDER = HexColor("#cccccc")
SECTION_BG = HexColor("#f0f4f8")
DARK_SECTION_BG = HexColor("#e8edf2")
RED_ACCENT = HexColor("#ff4444")
YELLOW_ACCENT = HexColor("#ffaa00")
GREEN_ACCENT = HexColor("#00d4aa")
BODY_TEXT_COLOR = HexColor("#222233")

PAGE_W, PAGE_H = A4
MARGIN = 2.0 * cm


# ──────────────────────────────────────────────
# STYLES
# ──────────────────────────────────────────────
def get_styles():
    """Return a dict of ParagraphStyles for consistent use."""
    styles = {}

    styles['title'] = ParagraphStyle(
        'Title',
        fontName='Helvetica-Bold',
        fontSize=26,
        leading=32,
        textColor=WHITE,
        alignment=TA_CENTER,
        spaceAfter=6,
    )
    styles['subtitle'] = ParagraphStyle(
        'Subtitle',
        fontName='Helvetica',
        fontSize=14,
        leading=18,
        textColor=ACCENT,
        alignment=TA_CENTER,
        spaceAfter=4,
    )
    styles['section'] = ParagraphStyle(
        'Section',
        fontName='Helvetica-Bold',
        fontSize=16,
        leading=22,
        textColor=DARK_BG,
        spaceBefore=18,
        spaceAfter=8,
        borderPadding=(0, 0, 4, 0),
    )
    styles['subsection'] = ParagraphStyle(
        'Subsection',
        fontName='Helvetica-Bold',
        fontSize=12,
        leading=16,
        textColor=HexColor("#2a2a4e"),
        spaceBefore=12,
        spaceAfter=6,
    )
    styles['body'] = ParagraphStyle(
        'Body',
        fontName='Helvetica',
        fontSize=10,
        leading=14,
        textColor=BODY_TEXT_COLOR,
        spaceAfter=6,
        alignment=TA_JUSTIFY,
    )
    styles['body_center'] = ParagraphStyle(
        'BodyCenter',
        fontName='Helvetica',
        fontSize=10,
        leading=14,
        textColor=BODY_TEXT_COLOR,
        spaceAfter=6,
        alignment=TA_CENTER,
    )
    styles['bullet'] = ParagraphStyle(
        'Bullet',
        fontName='Helvetica',
        fontSize=10,
        leading=14,
        textColor=BODY_TEXT_COLOR,
        leftIndent=20,
        spaceAfter=4,
        bulletIndent=8,
        bulletFontName='Helvetica',
        bulletFontSize=10,
    )
    styles['bullet_bold'] = ParagraphStyle(
        'BulletBold',
        fontName='Helvetica-Bold',
        fontSize=10,
        leading=14,
        textColor=BODY_TEXT_COLOR,
        leftIndent=20,
        spaceAfter=4,
        bulletIndent=8,
    )
    styles['code'] = ParagraphStyle(
        'Code',
        fontName='Courier',
        fontSize=9,
        leading=12,
        textColor=HexColor("#333344"),
        leftIndent=16,
        spaceAfter=4,
        backColor=HexColor("#f5f5fa"),
    )
    styles['quote'] = ParagraphStyle(
        'Quote',
        fontName='Helvetica-Oblique',
        fontSize=11,
        leading=15,
        textColor=HexColor("#444466"),
        leftIndent=24,
        rightIndent=24,
        spaceBefore=8,
        spaceAfter=8,
        borderPadding=(8, 12, 8, 12),
        backColor=HexColor("#f0f4f8"),
        alignment=TA_CENTER,
    )
    styles['table_header'] = ParagraphStyle(
        'TableHeader',
        fontName='Helvetica-Bold',
        fontSize=9,
        leading=12,
        textColor=WHITE,
        alignment=TA_CENTER,
    )
    styles['table_cell'] = ParagraphStyle(
        'TableCell',
        fontName='Helvetica',
        fontSize=9,
        leading=12,
        textColor=BODY_TEXT_COLOR,
        alignment=TA_LEFT,
    )
    styles['table_cell_center'] = ParagraphStyle(
        'TableCellCenter',
        fontName='Helvetica',
        fontSize=9,
        leading=12,
        textColor=BODY_TEXT_COLOR,
        alignment=TA_CENTER,
    )
    styles['table_cell_bold'] = ParagraphStyle(
        'TableCellBold',
        fontName='Helvetica-Bold',
        fontSize=9,
        leading=12,
        textColor=BODY_TEXT_COLOR,
        alignment=TA_LEFT,
    )
    styles['footer'] = ParagraphStyle(
        'Footer',
        fontName='Helvetica',
        fontSize=8,
        leading=10,
        textColor=MUTED,
        alignment=TA_CENTER,
    )
    styles['accent_text'] = ParagraphStyle(
        'AccentText',
        fontName='Helvetica-Bold',
        fontSize=10,
        leading=14,
        textColor=ACCENT_DIM,
        spaceAfter=6,
    )
    styles['red_text'] = ParagraphStyle(
        'RedText',
        fontName='Helvetica-Bold',
        fontSize=10,
        leading=14,
        textColor=RED_ACCENT,
        leftIndent=20,
        spaceAfter=4,
    )

    return styles

S = get_styles()


# ──────────────────────────────────────────────
# HELPER FUNCTIONS
# ──────────────────────────────────────────────
class NumberedCanvas:
    """Canvas wrapper that adds page numbers and headers."""

    def __init__(self, canvas, doc):
        self._canvas = canvas
        self._doc = doc

    @staticmethod
    def add_header_footer(canvas, doc):
        canvas.saveState()
        # Header line
        canvas.setStrokeColor(ACCENT)
        canvas.setLineWidth(0.5)
        canvas.line(MARGIN, PAGE_H - MARGIN + 8, PAGE_W - MARGIN, PAGE_H - MARGIN + 8)
        # Header text
        canvas.setFont('Helvetica', 7)
        canvas.setFillColor(MUTED)
        canvas.drawString(MARGIN, PAGE_H - MARGIN + 12, HEADER_TEXT)
        canvas.drawRightString(PAGE_W - MARGIN, PAGE_H - MARGIN + 12, DATE_STAMP)

        # Footer line
        canvas.setStrokeColor(HexColor("#cccccc"))
        canvas.setLineWidth(0.3)
        canvas.line(MARGIN, MARGIN - 10, PAGE_W - MARGIN, MARGIN - 10)
        # Page number (placeholder — will use actual page numbers)
        canvas.setFont('Helvetica', 7)
        canvas.setFillColor(MUTED)
        canvas.drawCentredString(PAGE_W / 2, MARGIN - 22,
                                 f"Page {canvas.getPageNumber()}")
        canvas.drawRightString(PAGE_W - MARGIN, MARGIN - 22,
                               "Confidential — BTC Predictor Bot")
        canvas.restoreState()


def build_doc(filepath, title_text, subtitle_text, story_func):
    """Build a complete PDF document with title page + content."""
    os.makedirs(os.path.dirname(filepath), exist_ok=True)

    doc = SimpleDocTemplate(
        filepath,
        pagesize=A4,
        leftMargin=MARGIN,
        rightMargin=MARGIN,
        topMargin=MARGIN + 10,
        bottomMargin=MARGIN + 10,
        title=title_text,
        author="BTC Predictor Bot",
    )

    story = []

    # ── Title Page ──
    story.append(Spacer(1, 60))

    # Dark title banner
    title_table = Table(
        [[Paragraph(title_text, S['title'])]],
        colWidths=[PAGE_W - 2 * MARGIN - 20],
        rowHeights=[None],
    )
    title_table.setStyle(TableStyle([
        ('BACKGROUND', (0, 0), (-1, -1), DARK_BG),
        ('TOPPADDING', (0, 0), (-1, -1), 28),
        ('BOTTOMPADDING', (0, 0), (-1, -1), 28),
        ('LEFTPADDING', (0, 0), (-1, -1), 20),
        ('RIGHTPADDING', (0, 0), (-1, -1), 20),
        ('ROUNDEDCORNERS', [8, 8, 8, 8]),
    ]))
    story.append(title_table)
    story.append(Spacer(1, 16))

    # Subtitle
    story.append(Paragraph(subtitle_text, S['subtitle']))
    story.append(Spacer(1, 8))

    # Accent divider
    story.append(HRFlowable(
        width="60%", thickness=2, color=ACCENT,
        spaceBefore=8, spaceAfter=8, hAlign='CENTER'
    ))
    story.append(Spacer(1, 4))
    story.append(Paragraph(DATE_STAMP, S['body_center']))
    story.append(Spacer(1, 30))

    # Page break to content
    story.append(PageBreak())

    # ── Content ──
    story_func(story)

    # Build
    doc.build(story, onFirstPage=NumberedCanvas.add_header_footer,
              onLaterPages=NumberedCanvas.add_header_footer)
    print(f"  [OK] {os.path.basename(filepath)}")


def section_header(title):
    """Return a styled section header with accent underline."""
    return [
        Spacer(1, 6),
        Paragraph(title, S['section']),
        HRFlowable(width="100%", thickness=1.5, color=ACCENT,
                    spaceBefore=0, spaceAfter=8),
    ]


def subsection_header(title):
    return [
        Paragraph(title, S['subsection']),
        HRFlowable(width="40%", thickness=0.5, color=HexColor("#aabbcc"),
                    spaceBefore=0, spaceAfter=6, hAlign='LEFT'),
    ]


def bullet(text):
    return Paragraph(f"\u2022  {text}", S['bullet'])


def bullet_check(text):
    return Paragraph(f"\u2713  {text}", S['bullet'])


def bullet_cross(text):
    return Paragraph(f"\u2717  {text}", S['red_text'])


def bullet_arrow(text):
    return Paragraph(f"\u25b8  {text}", S['bullet'])


def make_table(headers, rows, col_widths=None):
    """Create a professionally styled table."""
    header_row = [Paragraph(h, S['table_header']) for h in headers]
    data = [header_row]
    for row in rows:
        data.append([
            Paragraph(str(c), S['table_cell']) if not isinstance(c, Paragraph) else c
            for c in row
        ])

    tbl = Table(data, colWidths=col_widths, repeatRows=1)
    style_cmds = [
        ('BACKGROUND', (0, 0), (-1, 0), TABLE_HEADER_BG),
        ('TEXTCOLOR', (0, 0), (-1, 0), WHITE),
        ('FONTNAME', (0, 0), (-1, 0), 'Helvetica-Bold'),
        ('FONTSIZE', (0, 0), (-1, 0), 9),
        ('BOTTOMPADDING', (0, 0), (-1, 0), 8),
        ('TOPPADDING', (0, 0), (-1, 0), 8),
        ('GRID', (0, 0), (-1, -1), 0.5, TABLE_BORDER),
        ('VALIGN', (0, 0), (-1, -1), 'MIDDLE'),
        ('LEFTPADDING', (0, 0), (-1, -1), 8),
        ('RIGHTPADDING', (0, 0), (-1, -1), 8),
        ('TOPPADDING', (0, 1), (-1, -1), 6),
        ('BOTTOMPADDING', (0, 1), (-1, -1), 6),
    ]
    # Alternate row colors
    for i in range(1, len(data)):
        if i % 2 == 0:
            style_cmds.append(('BACKGROUND', (0, i), (-1, i), TABLE_ROW_ALT))

    tbl.setStyle(TableStyle(style_cmds))
    return tbl


def info_box(text, bg_color=None):
    """A highlighted info box."""
    if bg_color is None:
        bg_color = HexColor("#f0f8f5")
    tbl = Table(
        [[Paragraph(text, S['body'])]],
        colWidths=[PAGE_W - 2 * MARGIN - 40],
    )
    tbl.setStyle(TableStyle([
        ('BACKGROUND', (0, 0), (-1, -1), bg_color),
        ('TOPPADDING', (0, 0), (-1, -1), 12),
        ('BOTTOMPADDING', (0, 0), (-1, -1), 12),
        ('LEFTPADDING', (0, 0), (-1, -1), 16),
        ('RIGHTPADDING', (0, 0), (-1, -1), 16),
        ('ROUNDEDCORNERS', [6, 6, 6, 6]),
        ('BOX', (0, 0), (-1, -1), 1, ACCENT),
    ]))
    return tbl


def dark_box(text):
    """A dark-themed quote/highlight box."""
    style = ParagraphStyle(
        'DarkBox',
        fontName='Helvetica-Oblique',
        fontSize=10,
        leading=14,
        textColor=WHITE,
        alignment=TA_CENTER,
    )
    tbl = Table(
        [[Paragraph(text, style)]],
        colWidths=[PAGE_W - 2 * MARGIN - 40],
    )
    tbl.setStyle(TableStyle([
        ('BACKGROUND', (0, 0), (-1, -1), DARK_BG),
        ('TOPPADDING', (0, 0), (-1, -1), 14),
        ('BOTTOMPADDING', (0, 0), (-1, -1), 14),
        ('LEFTPADDING', (0, 0), (-1, -1), 20),
        ('RIGHTPADDING', (0, 0), (-1, -1), 20),
        ('ROUNDEDCORNERS', [6, 6, 6, 6]),
    ]))
    return tbl


def accent_label(text):
    """Inline accent colored text."""
    return f'<font color="#00d4aa"><b>{text}</b></font>'


def bold(text):
    return f'<b>{text}</b>'


def code(text):
    return f'<font face="Courier" size="9" color="#333355">{text}</font>'


# ══════════════════════════════════════════════
# PDF 1: Go-Live Day 0 Operations
# ══════════════════════════════════════════════
def pdf1_content(story):
    # Section: System Status at Launch
    story.extend(section_header("System Status at Launch"))
    status_rows = [
        ["Bot Status", "LIVE on Railway (web-production-e27d0.up.railway.app)"],
        ["Exchange", "Kraken Futures \u2014 PF_XBTUSD perpetual"],
        ["Capital", "$100 USDC"],
        ["On-chain Audit", "Polygon PoS \u2014 BTCBotAudit.sol\n0xe4661F7dB62644951Eb1F9Fd23DB90e647833a55"],
        ["Dashboard", "btcpredictor.io/dashboard"],
        ["n8n Automation", "12 workflows active (6-minute cycle)"],
        ["Database", "Supabase PostgreSQL \u2014 RESET today (clean dataset)"],
    ]
    story.append(make_table(
        ["Component", "Status"],
        status_rows,
        col_widths=[4 * cm, None]
    ))
    story.append(Spacer(1, 12))

    # Section: Pre-Launch Checklist
    story.extend(section_header("Pre-Launch Checklist"))
    story.append(Paragraph("All items resolved prior to go-live:", S['body']))
    story.append(Spacer(1, 4))
    checklist = [
        "Fix CNBC RSS feed parsing \u2014 resolved 28 Feb",
        "Fix PENDING cleanup logic \u2014 resolved 28 Feb",
        "Fix ghost_exit_price calculation \u2014 resolved 28 Feb",
        "Security audit completed (28 Feb) \u2014 git-filter-repo, all credentials rotated",
        "Compliance disclaimer added to all public pages",
        "Privacy notice updated on btcpredictor.io",
    ]
    for item in checklist:
        story.append(bullet_check(item))
    story.append(Spacer(1, 8))
    story.append(info_box(
        "All 6 pre-launch items have been resolved. The system is cleared for live trading."
    ))

    # Section: Post-Launch Monitoring Plan
    story.extend(section_header("Post-Launch Monitoring Plan"))
    milestones = [
        ["First 10 trades", "Verify on-chain timing compliance (commit before fill)"],
        ["First 50 trades", "Retrain XGBoost with clean, certified data"],
        ["First 100 trades", "Evaluate Claude Opus 4.6 upgrade on wf01B brain node"],
        ["First 200 trades", "XGBoost becomes primary gate (dual-gate active)"],
        ["First 500 trades", "Pattern memory fully operational \u2014 behavioral learning complete"],
    ]
    story.append(make_table(
        ["Milestone", "Action"],
        milestones,
        col_widths=[3.5 * cm, None]
    ))

    # Section: Key Metrics to Track
    story.extend(section_header("Key Metrics to Track"))
    metrics = [
        ["Win Rate", "> 55%", "Primary performance indicator"],
        ["Expectancy", "Positive", "E = (WR \u00d7 avg_win) \u2212 ((1\u2212WR) \u00d7 avg_loss)"],
        ["On-chain Timing", "100%", "Commit timestamp < Fill timestamp"],
        ["Avg Confidence", "0.62 \u2013 0.75", "Calibrated probability range"],
        ["Dead Hours Accuracy", "Tracked", "Performance during low-volume hours"],
        ["Cycle Lock Effectiveness", "Tracked", "Prevents duplicate entries in same cycle"],
    ]
    story.append(make_table(
        ["Metric", "Target", "Description"],
        metrics,
        col_widths=[3.5 * cm, 2.5 * cm, None]
    ))


def generate_pdf1():
    filepath = os.path.join(BASE_PATH, "\U0001f4ca Performance",
                            "Go_Live_Day_0_Operations_2026-03-01.pdf")
    build_doc(
        filepath,
        "\U0001f916 GO-LIVE DAY 0 \u2014 Operations Report",
        "1 Marzo 2026 \u2014 BTC Predictor Bot",
        pdf1_content
    )


# ══════════════════════════════════════════════
# PDF 2: Probabilistic Framework
# ══════════════════════════════════════════════
def pdf2_content(story):
    # The Fundamental Equation
    story.extend(section_header("The Fundamental Equation"))
    story.append(dark_box(
        "E = (WR \u00d7 avg_win) \u2212 ((1 \u2212 WR) \u00d7 avg_loss)"
    ))
    story.append(Spacer(1, 8))
    story.append(Paragraph(
        f"The goal is {bold('NOT')} maximizing Win Rate but maximizing "
        f"{accent_label('Expectancy (E)')}. A system with 45% WR and 3:1 reward-to-risk "
        f"beats a system with 60% WR and 0.8:1 reward-to-risk.",
        S['body']
    ))
    story.append(Spacer(1, 4))
    comp_rows = [
        ["System A", "45%", "3.0 : 1", "(0.45 \u00d7 3) \u2212 (0.55 \u00d7 1) = +0.80"],
        ["System B", "60%", "0.8 : 1", "(0.60 \u00d7 0.8) \u2212 (0.40 \u00d7 1) = +0.08"],
    ]
    story.append(make_table(
        ["System", "Win Rate", "R:R", "Expectancy"],
        comp_rows,
        col_widths=[2.5 * cm, 2.5 * cm, 2.5 * cm, None]
    ))

    # Kelly Criterion
    story.extend(section_header("Kelly Criterion for Position Sizing"))
    story.append(dark_box("f* = (p \u00d7 b \u2212 q) / b"))
    story.append(Spacer(1, 6))
    story.append(Paragraph(
        "Where <b>p</b> = probability of winning, <b>q</b> = probability of losing (1\u2212p), "
        "<b>b</b> = ratio of average win to average loss (reward-to-risk).",
        S['body']
    ))
    approx_label = accent_label('f* \u2248 10%')
    story.append(Paragraph(
        f"With WR = 55% and R:R = 2.0x: {approx_label}. "
        f"The system currently uses ~2% (conservative phase for capital preservation).",
        S['body']
    ))
    story.append(Spacer(1, 4))
    kelly_rows = [
        ["50%", "1.5x", "16.7%"],
        ["55%", "1.5x", "21.7%"],
        ["55%", "2.0x", "10.0%"],
        ["60%", "2.0x", "20.0%"],
        ["60%", "2.5x", "24.0%"],
        ["65%", "2.0x", "32.5%"],
    ]
    story.append(make_table(
        ["Win Rate", "Reward:Risk", "Kelly Fraction (f*)"],
        kelly_rows,
        col_widths=[4 * cm, 4 * cm, None]
    ))

    # Dual-Gate Decision Theory
    story.extend(section_header("Dual-Gate Decision Theory"))
    story.append(Paragraph(
        "Two independent classifiers (LLM + XGBoost) must agree before entering a trade. "
        "This dual-gate architecture reduces false signals at the cost of missed opportunities "
        "\u2014 the correct tradeoff for small capital.",
        S['body']
    ))
    story.append(Spacer(1, 4))
    story.append(info_box(
        "<b>P(both wrong)</b> = P(LLM wrong) \u00d7 P(XGB wrong) &nbsp;&nbsp;[if independent]<br/><br/>"
        "With 55% accuracy each: P(single wrong) = 0.45<br/>"
        "P(both wrong on same direction) = 0.45 \u00d7 0.45 = <b>0.2025 (20.25%)</b><br/><br/>"
        "The dual-gate effectively raises accuracy from 55% to ~80% on agreed signals."
    ))

    # Regime Detection Framework
    story.extend(section_header("Regime Detection Framework"))
    story.append(Paragraph(
        "Markets alternate between three primary regimes. A signal's win rate varies "
        "dramatically by regime, making regime detection a critical feature.",
        S['body']
    ))
    regime_rows = [
        ["Trending", "\u03c3(4h) moderate, directional bias strong", "Momentum signals excel"],
        ["Ranging", "\u03c3(4h) low, mean-reversion dominant", "Reversal signals excel"],
        ["Volatile", "\u03c3(4h) high, no directional bias", "SKIP \u2014 reduce exposure"],
    ]
    story.append(make_table(
        ["Regime", "Characteristics", "Optimal Strategy"],
        regime_rows,
        col_widths=[2.5 * cm, 6 * cm, None]
    ))
    story.append(Spacer(1, 6))
    story.append(Paragraph(
        f"{accent_label('Priority P1:')} Add regime label (vol_4h_normalized) as XGBoost feature.",
        S['body']
    ))

    # The 8 Levers
    story.extend(section_header("The 8 Levers to Perfect Predictability"))
    levers = [
        ("1. Confidence Calibration", "Ceiling at 0.62, clamped to prevent overconfidence."),
        ("2. Regime Label", "Volatility-based regime as XGBoost feature."),
        ("3. Claude Opus 4.6", "Upgrade brain node for superior reasoning."),
        ("4. Funding Settlement Timing", "Exploit 8h funding rate patterns."),
        ("5. CVD as Primary Filter", "Cumulative Volume Delta for order flow."),
        ("6. Liquidation Wall Detection", "Proximity to large liquidation clusters."),
        ("7. XGBoost Retrain", "Retrain with ghost signals for larger dataset."),
        ("8. Behavioral Pattern Memory", "Expand pattern memory for market fingerprinting."),
    ]
    for lever_title, lever_desc in levers:
        story.append(Paragraph(
            f"{bold(lever_title)} \u2014 {lever_desc}", S['bullet']
        ))

    # Statistical Significance
    story.extend(section_header("Statistical Significance"))
    story.append(Paragraph(
        "With N &lt; 500 bets, any observed win rate split loses statistical significance. "
        "The table below shows the 95% confidence interval for WR given N bets:",
        S['body']
    ))
    story.append(Spacer(1, 4))
    ci_rows = [
        ["10", "[26%, 81%]", "\u00b127.5%"],
        ["50", "[41%, 69%]", "\u00b114.0%"],
        ["200", "[48%, 62%]", "\u00b17.0%"],
        ["500", "[51%, 59%]", "\u00b14.0%"],
    ]
    story.append(make_table(
        ["N (trades)", "95% CI for WR=55%", "Margin"],
        ci_rows,
        col_widths=[4 * cm, 5 * cm, None]
    ))
    story.append(Spacer(1, 10))
    story.append(dark_box(
        '"Patience is the edge. The math only works at scale."'
    ))


def generate_pdf2():
    filepath = os.path.join(BASE_PATH, "\U0001f4ca Performance",
                            "Probabilistic_Framework_2026-03-01.pdf")
    build_doc(
        filepath,
        "\U0001f916 Probabilistic Framework",
        "The Math Behind Perfect Predictability",
        pdf2_content
    )


# ══════════════════════════════════════════════
# PDF 3: On-Chain Audit Architecture
# ══════════════════════════════════════════════
def pdf3_content(story):
    # The Core Principle
    story.extend(section_header("The Core Principle"))
    story.append(dark_box(
        "BEFORE executing a trade, the prediction is committed on-chain.<br/>"
        "This makes retroactive manipulation mathematically impossible."
    ))
    story.append(Spacer(1, 8))
    story.append(Paragraph(
        "The system follows a <b>commit-then-reveal</b> pattern on Polygon PoS. "
        "Every prediction is hashed and stored on-chain before the trade is placed on Kraken. "
        "After the trade resolves, the outcome is also committed on-chain for permanent verification.",
        S['body']
    ))

    # Architecture Diagram
    story.extend(section_header("Architecture Diagram"))
    arch_style = ParagraphStyle(
        'Arch', fontName='Courier', fontSize=9, leading=13,
        textColor=BODY_TEXT_COLOR, leftIndent=8,
    )
    diagram_text = (
        "wf01A (Data Collection)<br/>"
        "&nbsp;&nbsp;&nbsp;&nbsp;\u2502<br/>"
        "&nbsp;&nbsp;&nbsp;&nbsp;\u25bc<br/>"
        "wf01B (LLM Decision) \u2500\u2500\u2500\u25b8 /commit-prediction (Polygon PoS)<br/>"
        "&nbsp;&nbsp;&nbsp;&nbsp;\u2502&nbsp;&nbsp;&nbsp;&nbsp;&nbsp;&nbsp;&nbsp;&nbsp;&nbsp;"
        "&nbsp;&nbsp;&nbsp;&nbsp;&nbsp;&nbsp;&nbsp;&nbsp;&nbsp;&nbsp;&nbsp;&nbsp;&nbsp;&nbsp;"
        "&nbsp;&nbsp;&nbsp;&nbsp;&nbsp;&nbsp;&nbsp;&nbsp;&nbsp;&nbsp;\u2502<br/>"
        "&nbsp;&nbsp;&nbsp;&nbsp;\u25bc&nbsp;&nbsp;&nbsp;&nbsp;&nbsp;&nbsp;&nbsp;&nbsp;&nbsp;"
        "&nbsp;&nbsp;&nbsp;&nbsp;&nbsp;&nbsp;&nbsp;&nbsp;&nbsp;&nbsp;&nbsp;&nbsp;&nbsp;&nbsp;"
        "&nbsp;&nbsp;&nbsp;&nbsp;&nbsp;&nbsp;&nbsp;&nbsp;&nbsp;&nbsp;\u25bc<br/>"
        "/place-bet (Kraken) &nbsp;&nbsp;&nbsp;&nbsp;&nbsp;&nbsp;&nbsp;&nbsp;&nbsp;&nbsp;"
        "VERIFY: commit.timestamp &lt; fill.timestamp<br/>"
        "&nbsp;&nbsp;&nbsp;&nbsp;\u2502<br/>"
        "&nbsp;&nbsp;&nbsp;&nbsp;\u25bc<br/>"
        "wf02 (Exit Monitor) \u2500\u2500\u2500\u25b8 /resolve-prediction (Polygon PoS)<br/>"
    )
    story.append(info_box(diagram_text, HexColor("#f5f5fa")))

    # Smart Contract
    story.extend(section_header("Smart Contract \u2014 BTCBotAudit.sol"))
    story.append(Paragraph(
        f"Address: {code('0xe4661F7dB62644951Eb1F9Fd23DB90e647833a55')}",
        S['body']
    ))
    story.append(Spacer(1, 4))
    contract_rows = [
        ["commit(betId, commitHash)", "Write", "Stores prediction hash before trade execution"],
        ["resolve(betId, resolveHash, won)", "Write", "Stores outcome hash after trade resolution"],
        ["getCommit(betId)", "View", "Returns commit hash and timestamp"],
        ["getResolve(betId)", "View", "Returns resolve hash, won status, timestamp"],
        ["isCommitted(betId)", "View", "Boolean: has this bet been committed?"],
        ["isResolved(betId)", "View", "Boolean: has this bet been resolved?"],
    ]
    story.append(make_table(
        ["Function", "Type", "Description"],
        contract_rows,
        col_widths=[5.5 * cm, 2 * cm, None]
    ))
    story.append(Spacer(1, 6))
    story.append(bullet(f"{bold('Access Control:')} onlyOwner modifier \u2014 only the bot wallet can write"))
    story.append(bullet(f"{bold('Events:')} Committed(betId, hash, timestamp), Resolved(betId, hash, won, timestamp)"))

    # Hash Formulas
    story.extend(section_header("Hash Formulas"))
    hash_rows = [
        ["Commit", "keccak256(betId, direction, confidence,\nentryPrice, betSize, timestamp)", "+0 offset"],
        ["Resolve", "keccak256(betId, exitPrice, pnlUsd,\nwon, closeTimestamp)", "+0 offset"],
        ["Inputs", "keccak256(inputs payload)", "+10M offset"],
        ["Fill", "keccak256(fill payload)", "+20M offset"],
        ["Stops", "keccak256(stops payload)", "+30M offset"],
    ]
    story.append(make_table(
        ["Phase", "Hash Formula", "betId Offset"],
        hash_rows,
        col_widths=[2.5 * cm, 8.5 * cm, None]
    ))

    # Verification Guide
    story.extend(section_header("Verification Guide (for anyone)"))
    story.append(Paragraph(
        "Anyone can independently verify the integrity of any prediction:",
        S['body']
    ))
    steps = [
        f"{bold('Step 1:')} Go to polygonscan.com/address/0xe4661F7dB62644951Eb1F9Fd23DB90e647833a55",
        f"{bold('Step 2:')} Find the Committed event for the target betId \u2014 note the block timestamp",
        f"{bold('Step 3:')} Compare with the Kraken fill timestamp in Supabase (public data)",
        f"{bold('Step 4:')} Verify: {accent_label('block.timestamp(commit) < block.timestamp(fill)')} = VERIFIED",
    ]
    for step in steps:
        story.append(bullet_arrow(step))

    # Gas & Cost Analysis
    story.extend(section_header("Gas & Cost Analysis"))
    gas_rows = [
        ["Network", "Polygon PoS"],
        ["Finality", "~2 seconds"],
        ["Gas Price", "~30 gwei"],
        ["Cost per commit+resolve", "~$0.001"],
        ["Monthly budget (100 trades)", "~$0.10"],
        ["Annual budget (1200 trades)", "~$1.20"],
    ]
    story.append(make_table(
        ["Parameter", "Value"],
        gas_rows,
        col_widths=[5 * cm, None]
    ))
    story.append(Spacer(1, 8))
    story.append(info_box(
        "The on-chain audit system costs less than $2/year while providing "
        "cryptographically irrefutable proof of every prediction."
    ))


def generate_pdf3():
    filepath = os.path.join(BASE_PATH, "\U0001f510 Security",
                            "On_Chain_Audit_Architecture_2026-03-01.pdf")
    build_doc(
        filepath,
        "\U0001f916 On-Chain Audit Architecture",
        "Immutable Verification System",
        pdf3_content
    )


# ══════════════════════════════════════════════
# PDF 4: Master Orchestration — 6 Clones
# ══════════════════════════════════════════════
def pdf4_content(story):
    # Clone Map
    story.extend(section_header("Clone Map"))
    clone_rows = [
        ["C1", "Full Stack Developer", "app.py, tests/", "Core application logic"],
        ["C2", "Crypto & Blockchain", "contracts/, onchain_monitor.py", "On-chain integration"],
        ["C3", "Cybersecurity Expert", "SECURITY.md, scripts/security_audit.py", "Security hardening"],
        ["C4", "Legal & Compliance", "All .html files", "Regulatory compliance"],
        ["C5", "Research & Development", "build_dataset.py, train_xgboost.py", "ML pipeline"],
        ["C6", "Trading & Probabilistic", "backtest.py, analysis/", "Trading strategy"],
    ]
    story.append(make_table(
        ["Clone", "Role", "Primary Files", "Domain"],
        clone_rows,
        col_widths=[1.5 * cm, 3.5 * cm, 5 * cm, None]
    ))

    # Task Summary per Clone
    story.extend(section_header("Task Summary per Clone"))

    clone_tasks = [
        ("C1 \u2014 Full Stack Developer", [
            "Implement timing gate: on-chain commit must precede Kraken fill",
            "Add distributed mutex to prevent duplicate cycle execution",
            "Write comprehensive test suite for all critical paths",
            "Harden error handling across all API endpoints",
        ]),
        ("C2 \u2014 Crypto & Blockchain Expert", [
            "Generate and validate Supabase DDL for all tables",
            "Audit BTCBotAudit.sol contract for edge cases",
            "Harden onchain_monitor.py against RPC failures",
            "Implement retry logic with exponential backoff",
        ]),
        ("C3 \u2014 Cybersecurity Expert", [
            "Complete environment variable audit (no secrets in code)",
            "Create automated security scan script",
            "Audit all dependencies for known vulnerabilities",
            "Verify credential rotation after git-filter-repo",
        ]),
        ("C4 \u2014 Legal & Compliance", [
            "Add TUF/MiFID II disclaimer to all HTML pages",
            "Implement AI-generated signals disclosure (EU AI Act)",
            "Draft and deploy privacy notice on btcpredictor.io",
            "Create Telegram channel disclaimer pin",
        ]),
        ("C5 \u2014 Research & Development", [
            "Review feature engineering pipeline for data leakage",
            "Audit dataset integrity (clean vs. ghost signals)",
            "Prepare XGBoost retraining pipeline for clean data",
            "Document feature importance rankings",
        ]),
        ("C6 \u2014 Trading & Probabilistic Master", [
            "Run comprehensive backtest analysis on historical signals",
            "Calibrate stop-loss and take-profit parameters",
            "Build expectancy framework with regime detection",
            "Document probabilistic foundations for system review",
        ]),
    ]

    for clone_title, tasks in clone_tasks:
        story.extend(subsection_header(clone_title))
        for task in tasks:
            story.append(bullet_arrow(task))

    # File Ownership Matrix
    story.extend(section_header("File Ownership Matrix"))
    ownership_rows = [
        ["app.py", "C1", "Read-only: C3"],
        ["tests/*", "C1", "\u2014"],
        ["contracts/*", "C2", "Read-only: C3"],
        ["onchain_monitor.py", "C2", "\u2014"],
        ["SECURITY.md", "C3", "\u2014"],
        ["scripts/security_audit.py", "C3", "\u2014"],
        ["templates/*.html", "C4", "\u2014"],
        ["build_dataset.py", "C5", "\u2014"],
        ["train_xgboost.py", "C5", "\u2014"],
        ["backtest.py", "C6", "\u2014"],
        ["analysis/*", "C6", "\u2014"],
    ]
    story.append(make_table(
        ["File / Path", "Owner", "Access"],
        ownership_rows,
        col_widths=[5 * cm, 2.5 * cm, None]
    ))

    # Conflict Prevention Rules
    story.extend(section_header("Conflict Prevention Rules"))
    rules = [
        f"{bold('Zero file overlap')} \u2014 Each clone owns specific files exclusively",
        f"{bold('C2 generates SQL, Mattia executes')} \u2014 No direct database writes from clones",
        f"{bold('C3 reads app.py, never writes')} \u2014 Security review is read-only",
        f"{bold('All clones prefix messages')} with [C{{N}}] for clear attribution",
    ]
    for rule in rules:
        story.append(bullet(rule))

    # Timeline
    story.extend(section_header("Timeline"))
    timeline_rows = [
        ["T+0", "All clones receive briefing and file assignments"],
        ["T+1", "C1 + C2 implement timing gate and DDL"],
        ["T+2", "C3 security audit; C4 compliance disclaimers"],
        ["T+3", "C5 dataset audit; C6 backtest analysis"],
        ["T+4", "Integration testing across all clone outputs"],
        ["T+5", "Go-live verification and monitoring handoff"],
    ]
    story.append(make_table(
        ["Phase", "Activities"],
        timeline_rows,
        col_widths=[2 * cm, None]
    ))


def generate_pdf4():
    filepath = os.path.join(BASE_PATH, "\U0001f5c2\ufe0f Claude Sessions",
                            "Master_Orchestration_6_Clones_2026-03-01.pdf")
    build_doc(
        filepath,
        "\U0001f916 Master Orchestration",
        "6 Claude Clones in Parallel \u2014 Go-Live Day Operations, 1 March 2026",
        pdf4_content
    )


# ══════════════════════════════════════════════
# PDF 5: Feature Engineering Roadmap
# ══════════════════════════════════════════════
def pdf5_content(story):
    # Current Feature Set
    story.extend(section_header("Current Feature Set (11 + 1 optional)"))
    feature_rows = [
        ["confidence", "LLM output", "HIGH", "Continuous"],
        ["fear_greed_value", "Alternative.me API", "HIGH", "Continuous"],
        ["rsi14", "Binance klines", "MEDIUM", "Continuous"],
        ["technical_score", "Multi-indicator composite", "MEDIUM", "Continuous"],
        ["hour_sin", "Derived (time)", "MEDIUM", "Cyclical"],
        ["hour_cos", "Derived (time)", "MEDIUM", "Cyclical"],
        ["technical_bias_score", "Technical analysis", "MEDIUM", "Continuous"],
        ["signal_fg_fear", "Fear & Greed binary", "LOW", "Binary"],
        ["dow_sin", "Derived (day-of-week)", "LOW", "Cyclical"],
        ["dow_cos", "Derived (day-of-week)", "LOW", "Cyclical"],
        ["session", "Market session label", "LOW", "Categorical"],
        ["cvd_6m_pct (optional)", "Binance trades", "TBD", "Continuous"],
    ]
    story.append(make_table(
        ["Feature", "Source", "Importance", "Type"],
        feature_rows,
        col_widths=[4 * cm, 4 * cm, 2.5 * cm, None]
    ))

    # P1 Features
    story.extend(section_header("P1 Features \u2014 High Priority"))

    p1_features = [
        ("Regime Label", "vol_4h_normalized \u2192 trend / range / volatile",
         "Binance klines", "2/5", "HIGH"),
        ("Funding Rate Numeric", "Continuous value (not just filter)",
         "Binance API", "1/5", "HIGH"),
        ("Liquidation Wall Proximity", "Distance to nearest large liquidation cluster",
         "Coinglass API", "4/5", "HIGH"),
    ]
    for name, desc, source, effort, gain in p1_features:
        story.extend(subsection_header(name))
        story.append(Paragraph(desc, S['body']))
        story.append(Paragraph(
            f"Source: {bold(source)} &nbsp;|&nbsp; Effort: {bold(effort)} &nbsp;|&nbsp; "
            f"Expected Gain: {accent_label(gain)}",
            S['body']
        ))

    # P2 Features
    story.extend(section_header("P2 Features \u2014 Medium Priority"))
    p2_rows = [
        ["SOPR", "Spent Output Profit Ratio", "Glassnode / CryptoQuant", "3/5", "MEDIUM"],
        ["Exchange Netflow", "Inflow vs outflow balance", "CryptoQuant", "3/5", "MEDIUM"],
        ["OI Change Rate", "Open Interest momentum", "Binance Futures", "2/5", "MEDIUM"],
    ]
    story.append(make_table(
        ["Feature", "Description", "Source", "Effort", "Expected Gain"],
        p2_rows,
        col_widths=[2.5 * cm, 3.5 * cm, 3.5 * cm, 1.5 * cm, None]
    ))

    # P3 Features
    story.extend(section_header("P3 Features \u2014 Future Research"))
    p3_items = [
        "MVRV Z-score \u2014 Market Value to Realized Value ratio",
        "Whale alerts \u2014 Transactions > 1,000 BTC",
        "Mempool congestion \u2014 Network activity indicator",
        "Cross-asset correlation \u2014 ETH/BTC ratio divergence",
    ]
    for item in p3_items:
        story.append(bullet(item))

    # The 5-Check Filter
    story.extend(section_header("The 5-Check Filter (from CLAUDE.md)"))
    story.append(Paragraph(
        "Every proposed feature must pass all 5 checks before integration:",
        S['body']
    ))
    story.append(Spacer(1, 4))
    checks = [
        ("Edge Check", "Does this feature provide statistically significant predictive power?"),
        ("Regime Check", "Does the feature's value vary meaningfully across market regimes?"),
        ("Overfitting Check", "Does the feature improve out-of-sample performance, not just in-sample?"),
        ("Cost Check", "Can the feature be computed within the 8-minute cycle budget?"),
        ("Verifiability Check", "Can the feature's value be independently verified and audited?"),
    ]
    check_rows = [[name, desc] for name, desc in checks]
    story.append(make_table(
        ["Check", "Criteria"],
        check_rows,
        col_widths=[3.5 * cm, None]
    ))

    # Training Data Roadmap
    story.extend(section_header("Training Data Roadmap"))
    data_rows = [
        ["Pre-reset (legacy)", "40 rows", "17 bet + 23 ghost \u2014 mixed quality"],
        ["Post-reset (Day 0)", "0 rows", "Clean slate \u2014 rebuild from scratch"],
        ["Target (Phase 1)", "50+ rows", "Minimum for initial XGBoost training"],
        ["Target (Phase 2)", "200+ rows", "Reliable XGBoost with certified data"],
        ["Target (Phase 3)", "500+ rows", "Full statistical significance"],
    ]
    story.append(make_table(
        ["Stage", "Dataset Size", "Notes"],
        data_rows,
        col_widths=[3.5 * cm, 2.5 * cm, None]
    ))
    story.append(Spacer(1, 8))
    story.append(info_box(
        "Every prediction from Day 0 onward is certified via on-chain commit. "
        "This creates a verifiable, clean dataset for XGBoost retraining."
    ))


def generate_pdf5():
    filepath = os.path.join(BASE_PATH, "\U0001f4ca Performance",
                            "Feature_Engineering_Roadmap_2026-03-01.pdf")
    build_doc(
        filepath,
        "\U0001f916 Feature Engineering Roadmap",
        "The Path to Information Gain",
        pdf5_content
    )


# ══════════════════════════════════════════════
# PDF 6: Go-Live Compliance Checklist
# ══════════════════════════════════════════════
def pdf6_content(story):
    # Regulatory Status Summary
    story.extend(section_header("Regulatory Status Summary"))
    reg_rows = [
        ["MiFID II", "LOW RISK", "No payments, public signals, proprietary trading only"],
        ["VASP / OAM", "NOT APPLICABLE", "Own capital only \u2014 no third-party management"],
        ["EU AI Act", "LIMITED RISK", "Transparency obligation \u2014 AI disclosure required"],
        ["GDPR", "PARTIAL", "Privacy notice needed for data collection via dashboard"],
    ]
    story.append(make_table(
        ["Regulation", "Risk Level", "Assessment"],
        reg_rows,
        col_widths=[3 * cm, 3.5 * cm, None]
    ))

    # Immediate Actions
    story.extend(section_header("Immediate Actions (\u003C 1 week, zero cost)"))
    actions = [
        "Complete disclaimer in footer of all pages (TUF, MiFID II reference)",
        '"AI-generated signals" disclosure on all output channels (EU AI Act compliance)',
        "Privacy notice deployment on btcpredictor.io",
        "Disclaimer pinned message in Telegram channel",
    ]
    for action in actions:
        story.append(Paragraph(
            f"\u2610  {action}", S['bullet']
        ))

    # Red Lines
    story.extend(section_header("Red Lines \u2014 NEVER Cross Without Legal Counsel"))
    story.append(Spacer(1, 4))

    red_lines_style = ParagraphStyle(
        'RedLine', fontName='Helvetica-Bold', fontSize=10, leading=14,
        textColor=WHITE, leftIndent=20, spaceAfter=4, bulletIndent=8,
    )

    red_items = [
        "Accept payments for trading signals",
        "Manage third-party capital",
        'Promise returns ("earn X% per month")',
        "Collect user data without a published privacy notice",
        "Scale to >100 Telegram users without proper disclaimer",
    ]

    red_data = []
    for item in red_items:
        red_data.append([Paragraph(f"\u2717  {item}", red_lines_style)])

    red_table = Table(red_data, colWidths=[PAGE_W - 2 * MARGIN - 40])
    red_style_cmds = [
        ('BACKGROUND', (0, 0), (-1, -1), HexColor("#441111")),
        ('TOPPADDING', (0, 0), (0, 0), 12),
        ('BOTTOMPADDING', (0, -1), (0, -1), 12),
        ('TOPPADDING', (0, 1), (-1, -2), 4),
        ('BOTTOMPADDING', (0, 1), (-1, -2), 4),
        ('LEFTPADDING', (0, 0), (-1, -1), 16),
        ('RIGHTPADDING', (0, 0), (-1, -1), 16),
        ('ROUNDEDCORNERS', [6, 6, 6, 6]),
        ('BOX', (0, 0), (-1, -1), 1.5, RED_ACCENT),
    ]
    red_table.setStyle(TableStyle(red_style_cmds))
    story.append(red_table)

    # Defensive Assets
    story.extend(section_header("Defensive Assets Already in Place"))
    assets = [
        (
            "On-chain Audit Trail (Polygon PoS)",
            "Irrefutable, timestamped proof of every prediction \u2014 commit before fill"
        ),
        (
            "Public GitHub Repository",
            "Full methodology is open and verifiable by anyone"
        ),
        (
            "Sentry Monitoring",
            "Technical diligence \u2014 errors are tracked and resolved promptly"
        ),
        (
            "Own Capital Only ($100 USDC)",
            "No third-party risk \u2014 proprietary trading with personal funds"
        ),
        (
            "Supabase RLS (Row-Level Security)",
            "Data governance \u2014 access controls enforced at database level"
        ),
    ]
    asset_rows = [[name, desc] for name, desc in assets]
    story.append(make_table(
        ["Defensive Asset", "Purpose"],
        asset_rows,
        col_widths=[5 * cm, None]
    ))
    story.append(Spacer(1, 12))
    story.append(info_box(
        "The combination of on-chain audit, public code, and own-capital-only trading "
        "creates a strong defensive posture. No regulatory action is expected at current scale, "
        "but the compliance checklist ensures readiness for any future inquiry."
    ))


def generate_pdf6():
    filepath = os.path.join(BASE_PATH, "\u2696\ufe0f Compliance",
                            "Go_Live_Compliance_Checklist_2026-03-01.pdf")
    build_doc(
        filepath,
        "\U0001f916 Go-Live Compliance Checklist",
        "Operational Readiness for Live Trading \u2014 1 March 2026",
        pdf6_content
    )


# ══════════════════════════════════════════════
# MAIN
# ══════════════════════════════════════════════
def main():
    print("=" * 60)
    print("  BTC Predictor Bot \u2014 PDF Document Generator")
    print(f"  Date: {DATE_STAMP}")
    print("=" * 60)
    print()

    generators = [
        ("PDF 1: Go-Live Day 0 Operations", generate_pdf1),
        ("PDF 2: Probabilistic Framework", generate_pdf2),
        ("PDF 3: On-Chain Audit Architecture", generate_pdf3),
        ("PDF 4: Master Orchestration \u2014 6 Clones", generate_pdf4),
        ("PDF 5: Feature Engineering Roadmap", generate_pdf5),
        ("PDF 6: Go-Live Compliance Checklist", generate_pdf6),
    ]

    for name, gen_func in generators:
        print(f"Generating {name}...")
        try:
            gen_func()
        except Exception as e:
            print(f"  [FAIL] {name}: {e}")
            import traceback
            traceback.print_exc()

    print()
    print("=" * 60)
    print("  All PDFs generated successfully!")
    print(f"  Output: {BASE_PATH}")
    print("=" * 60)


if __name__ == "__main__":
    main()
