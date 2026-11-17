"""Builds the NASA Omni Inventory POC Design document (.docx) in the style of the NASA Semantic Routing POC design.

Run:  /tmp/docxenv/bin/python docs/poc-design/build_poc_doc.py
Out:  docs/poc-design/NASA-Omni-Inventory-POC-Design.docx
Architecture figures are placeholders to be drawn in Miro; dashboard figures are real screenshots (img/).
"""
from pathlib import Path

from docx import Document
from docx.enum.section import WD_ORIENT
from docx.enum.table import WD_TABLE_ALIGNMENT
from docx.enum.text import WD_ALIGN_PARAGRAPH, WD_BREAK
from docx.oxml import OxmlElement
from docx.oxml.ns import qn
from docx.shared import Inches, Pt, RGBColor

HERE = Path(__file__).resolve().parent
IMG = HERE / "img"
OUT = HERE / "NASA-Omni-Inventory-POC-Design.docx"

RED = RGBColor(0xEE, 0x00, 0x00)
NAVY = RGBColor(0x0B, 0x33, 0x5E)
BLUE = RGBColor(0x1B, 0x5E, 0x9B)
GREY = RGBColor(0x6A, 0x6E, 0x73)
GREEN = RGBColor(0x5E, 0x8E, 0x3E)
ORANGE = RGBColor(0xE0, 0x7A, 0x10)
HEAD_FONT = "Red Hat Display"
BODY_FONT = "Red Hat Text"

doc = Document()
sec = doc.sections[0]
sec.page_width, sec.page_height = Inches(8.5), Inches(11)
for side in ("left_margin", "right_margin"):
    setattr(sec, side, Inches(0.9))
sec.top_margin = sec.bottom_margin = Inches(0.8)

# ---------------------------------------------------------------- base styles
st = doc.styles["Normal"]
st.font.name = BODY_FONT
st.font.size = Pt(10.5)
st.element.rPr.rFonts.set(qn("w:eastAsia"), BODY_FONT)
st.paragraph_format.space_after = Pt(6)
st.paragraph_format.line_spacing = 1.12
for lvl, size, color in ((1, 20, NAVY), (2, 14, BLUE), (3, 12, BLUE)):
    h = doc.styles[f"Heading {lvl}"]
    h.font.name = HEAD_FONT
    h.font.size = Pt(size)
    h.font.bold = True
    h.font.color.rgb = color
    h.element.rPr.rFonts.set(qn("w:eastAsia"), HEAD_FONT)
    h.paragraph_format.space_before = Pt(16 if lvl == 1 else 10)
    h.paragraph_format.space_after = Pt(6)
    h.paragraph_format.keep_with_next = True
for name in ("List Bullet", "List Number"):
    doc.styles[name].font.name = BODY_FONT
    doc.styles[name].font.size = Pt(10.5)
    doc.styles[name].paragraph_format.space_after = Pt(2)


def shade(cell, hex_fill):
    tcPr = cell._tc.get_or_add_tcPr()
    shd = OxmlElement("w:shd")
    shd.set(qn("w:val"), "clear")
    shd.set(qn("w:color"), "auto")
    shd.set(qn("w:fill"), hex_fill)
    tcPr.append(shd)


def borders(table, color="000000", size=8, style="single"):
    tbl = table._tbl
    tblPr = tbl.tblPr
    b = OxmlElement("w:tblBorders")
    for edge in ("top", "left", "bottom", "right", "insideH", "insideV"):
        el = OxmlElement(f"w:{edge}")
        el.set(qn("w:val"), style)
        el.set(qn("w:sz"), str(size))
        el.set(qn("w:space"), "0")
        el.set(qn("w:color"), color)
        b.append(el)
    tblPr.append(b)


def set_widths(t, widths):
    """Fixed layout + explicit grid so Word, Pages and Google Docs all honour the column widths."""
    t.autofit = False
    tblPr = t._tbl.tblPr
    layout = OxmlElement("w:tblLayout")
    layout.set(qn("w:type"), "fixed")
    tblPr.append(layout)
    grid = t._tbl.tblGrid
    for i, gc in enumerate(grid.findall(qn("w:gridCol"))):
        if i < len(widths):
            gc.set(qn("w:w"), str(int(widths[i] * 1440)))
    for row in t.rows:
        for i, w in enumerate(widths):
            row.cells[i].width = Inches(w)


def numbered(items):
    """Explicit 1..n numbering with a hanging indent (Word's List Number continues across lists)."""
    for n, it in enumerate(items, 1):
        p = doc.add_paragraph()
        p.paragraph_format.left_indent = Inches(0.3)
        p.paragraph_format.first_line_indent = Inches(-0.3)
        p.paragraph_format.space_after = Pt(2)
        _runs(p, f"{n}.\t" + it)
        p.paragraph_format.tab_stops.add_tab_stop(Inches(0.3))


def cell_text(cell, text, bold=False, color=None, size=9.5, italic=False, font=BODY_FONT):
    cell.text = ""
    p = cell.paragraphs[0]
    p.paragraph_format.space_after = Pt(1)
    _runs(p, text, bold=bold, color=color, size=size, italic=italic, font=font)


def _runs(p, text, bold=False, color=None, size=None, italic=False, font=None):
    """Supports **bold** spans inside text."""
    parts = text.split("**")
    for i, part in enumerate(parts):
        if not part:
            continue
        r = p.add_run(part)
        r.bold = bold or (i % 2 == 1)
        r.italic = italic
        if color is not None:
            r.font.color.rgb = color
        if size:
            r.font.size = Pt(size)
        if font:
            r.font.name = font
    return p


def para(text, italic=False, size=None, color=None, align=None, after=None, bold=False):
    p = doc.add_paragraph()
    _runs(p, text, italic=italic, size=size, color=color, bold=bold)
    if align:
        p.alignment = align
    if after is not None:
        p.paragraph_format.space_after = Pt(after)
    return p


def bullets(items, style="List Bullet"):
    for it in items:
        p = doc.add_paragraph(style=style)
        _runs(p, it)


def table(header, rows, widths=None, font_size=9.5):
    t = doc.add_table(rows=1, cols=len(header))
    t.alignment = WD_TABLE_ALIGNMENT.CENTER
    borders(t)
    for i, h in enumerate(header):
        c = t.rows[0].cells[i]
        shade(c, "0B335E")
        cell_text(c, h, bold=True, color=RGBColor(0xFF, 0xFF, 0xFF), size=font_size)
        for hp in c.paragraphs:
            hp.paragraph_format.keep_with_next = True  # never strand the header row at a page bottom
    trPr = t.rows[0]._tr.get_or_add_trPr()
    for tag in ("w:tblHeader", "w:cantSplit"):
        el = OxmlElement(tag)
        trPr.append(el)
    for row in rows:
        cells = t.add_row().cells
        for i, v in enumerate(row):
            cell_text(cells[i], v, size=font_size)
    if not widths:
        widths = [6.7 / len(header)] * len(header)
    set_widths(t, widths)
    doc.add_paragraph().paragraph_format.space_after = Pt(2)
    return t


def callout(label, text, color, fill):
    t = doc.add_table(rows=1, cols=1)
    t.alignment = WD_TABLE_ALIGNMENT.CENTER
    hexc = "%02X%02X%02X" % (color[0], color[1], color[2])
    borders(t, color=hexc, size=16)
    set_widths(t, [6.7])
    c = t.rows[0].cells[0]
    shade(c, fill)
    c.text = ""
    p = c.paragraphs[0]
    p.paragraph_format.space_before = Pt(3)
    p.paragraph_format.space_after = Pt(3)
    r = p.add_run(label + "  ")
    r.bold = True
    r.font.color.rgb = color
    r.font.name = HEAD_FONT
    r.font.size = Pt(10)
    _runs(p, text, size=10)
    doc.add_paragraph().paragraph_format.space_after = Pt(2)


def figure_placeholder(num, title, shows, caption):
    t = doc.add_table(rows=1, cols=1)
    t.alignment = WD_TABLE_ALIGNMENT.CENTER
    borders(t, color="8A8D91", size=12, style="dashed")
    set_widths(t, [6.7])
    c = t.rows[0].cells[0]
    shade(c, "F2F3F5")
    c.text = ""
    p = c.paragraphs[0]
    p.alignment = WD_ALIGN_PARAGRAPH.CENTER
    p.paragraph_format.space_before = Pt(10)
    r = p.add_run(f"FIGURE {num} PLACEHOLDER: {title}")
    r.bold = True
    r.font.color.rgb = NAVY
    r.font.name = HEAD_FONT
    r.font.size = Pt(11)
    p2 = c.add_paragraph()
    p2.alignment = WD_ALIGN_PARAGRAPH.CENTER
    r2 = p2.add_run("[ Diagram to be drawn in Miro and inserted here ]")
    r2.italic = True
    r2.font.color.rgb = GREY
    r2.font.size = Pt(9.5)
    p3 = c.add_paragraph()
    r3 = p3.add_run("The diagram should show:")
    r3.bold = True
    r3.font.size = Pt(9.5)
    for s in shows:
        q = c.add_paragraph()
        q.paragraph_format.left_indent = Inches(0.25)
        q.paragraph_format.space_after = Pt(1)
        _runs(q, "•  " + s, size=9.5)
    c.add_paragraph().paragraph_format.space_after = Pt(6)
    cap = doc.add_paragraph()
    cap.alignment = WD_ALIGN_PARAGRAPH.CENTER
    _runs(cap, f"Figure {num}. {caption}", italic=True, size=9, color=GREY)


def screenshot(num, path, caption, width=6.6):
    doc.add_picture(str(path), width=Inches(width))
    doc.paragraphs[-1].alignment = WD_ALIGN_PARAGRAPH.CENTER
    cap = doc.add_paragraph()
    cap.alignment = WD_ALIGN_PARAGRAPH.CENTER
    _runs(cap, f"Figure {num}. {caption}", italic=True, size=9, color=GREY)


def code(text):
    t = doc.add_table(rows=1, cols=1)
    borders(t, color="D9DCE0", size=6)
    set_widths(t, [6.7])
    c = t.rows[0].cells[0]
    shade(c, "F1F3F5")
    c.text = ""
    first = True
    for line in text.strip("\n").split("\n"):
        p = c.paragraphs[0] if first else c.add_paragraph()
        first = False
        p.paragraph_format.space_after = Pt(0)
        p.paragraph_format.line_spacing = 1.0
        r = p.add_run(line)
        r.font.name = "Red Hat Mono"
        r.font.size = Pt(8.5)
    doc.add_paragraph().paragraph_format.space_after = Pt(2)


def page_break():
    doc.add_paragraph().add_run().add_break(WD_BREAK.PAGE)


def add_page_number(paragraph):
    run = paragraph.add_run("Page ")
    run.font.size = Pt(9)
    run.font.color.rgb = GREY
    for kind, txt in (("begin", None), (None, "PAGE"), ("end", None)):
        r = paragraph.add_run()
        r.font.size = Pt(9)
        r.font.color.rgb = GREY
        if kind:
            f = OxmlElement("w:fldChar")
            f.set(qn("w:fldCharType"), kind)
            r._r.append(f)
        else:
            it = OxmlElement("w:instrText")
            it.set(qn("xml:space"), "preserve")
            it.text = txt
            r._r.append(it)


# header / footer
hp = sec.header.paragraphs[0]
hp.alignment = WD_ALIGN_PARAGRAPH.RIGHT
hr = hp.add_run("NASA Omni Inventory POC  |  Draft")
hr.font.size = Pt(9)
hr.font.color.rgb = GREY
fp = sec.footer.paragraphs[0]
fp.alignment = WD_ALIGN_PARAGRAPH.RIGHT
add_page_number(fp)

# ================================================================= TITLE PAGE
for _ in range(3):
    doc.add_paragraph()
t1 = para("NASA Omni Inventory Tracking", size=32, color=RED, align=WD_ALIGN_PARAGRAPH.CENTER, after=4)
t1.runs[0].font.name = HEAD_FONT
t1.runs[0].bold = True
t2 = para("Proof of Concept Design", size=24, color=RED, align=WD_ALIGN_PARAGRAPH.CENTER, after=6)
t2.runs[0].font.name = HEAD_FONT
t2.runs[0].bold = True
t3 = para("vLLM-Omni video + audio perception, an on-device inventory database, and mission-control chat on a Dell GB10",
          italic=True, size=13, color=RED, align=WD_ALIGN_PARAGRAPH.CENTER, after=10)
rule = para("_" * 72, color=RED, align=WD_ALIGN_PARAGRAPH.CENTER, after=14)
callout("ENGAGEMENT", "POC starts November 2026. Onsite 1 the week of November 17, 2026; iteration on its feedback; onsite 2 (date TBD); POC completion targeted for January 2027.", NAVY, "EEF3F9")
callout("FOCUS", "Validate simultaneous video and audio understanding with vLLM-Omni to track ISS objects and people "
        "into an on-device Postgres inventory, with a mission-control dashboard and chatbot.", GREEN, "EAF2E3")
callout("PLATFORM", "Dell GB10 (DGX Spark class) provided by Dell to Red Hat for POC work; ETA mid-November 2026. "
        "Red Hat OpenShift AI / Open Data Hub, open-source and US-origin models.", BLUE, "EAF1FA")
callout("STATUS", "Draft for design-partner review. The POC will not be production-ready.", ORANGE, "FDF3E1")
doc.add_paragraph()
para("Prepared for NASA", size=13, bold=True, align=WD_ALIGN_PARAGRAPH.CENTER, after=8)
para("Prepared by Red Hat Field CTO / Chief Architect Team", align=WD_ALIGN_PARAGRAPH.CENTER, after=2)
para("October 20, 2026  |  Version 0.1", size=10, align=WD_ALIGN_PARAGRAPH.CENTER)
page_break()

# ================================================================= DOCUMENT CONTROL
doc.add_heading("Document Control", 1)
table(["Field", "Value"], [
    ["Document", "NASA Omni Inventory Tracking Proof of Concept Design"],
    ["Version", "0.1"],
    ["Status", "Draft for joint review"],
    ["Engagement", "POC starting November 2026; onsite 1 week of November 17, 2026; onsite 2 TBD; completion targeted for January 2027"],
    ["Primary scope", "vLLM-Omni multimodal perception (video + audio), closed-world object and person tracking, "
                      "on-device Postgres inventory, mission-control dashboard and chatbot"],
    ["Target hardware", "Dell GB10 (DGX Spark class): Blackwell GPU, 128 GB unified memory, single node"],
    ["Development environment", "Red Hat OpenShift on Azure (ARO) with NVIDIA T4 GPUs, used to build and evaluate the POC "
                                "before the GB10 arrives"],
    ["Model policy", "Open-source, US-origin models: Google Gemma 4, OpenAI Whisper, Google DiffusionGemma, Google OWLv2, "
                     "Meta DINOv2"],
    ["Phase 2 (out of scope here)", "Agentic operations: Red Hat AI enterprise Claw agent (OpenClaw Enterprise) and "
                                    "NVIDIA OpenShell as candidate agent runtimes"],
    ["Primary outcome", "Prove the architecture on the GB10, agree on accuracy and privacy targets with NASA, and define "
                        "the path to an operational pilot"],
], widths=[2.0, 4.7])

# ================================================================= EXECUTIVE SUMMARY
doc.add_heading("Executive Summary", 1)
para("On the International Space Station, items are constantly moved, borrowed, stowed and misplaced. Crew time spent "
     "searching is expensive, and ground teams cannot see where equipment is unless it was logged. NASA asked Red Hat "
     "for an experimental proof of concept that watches **live video and listens to live audio at the same time**, "
     "identifies and tracks the objects and people it sees, and keeps an inventory that mission control can browse, "
     "correct and question in natural language.")
para("The design puts **vLLM-Omni** at the centre. vLLM-Omni extends vLLM, the open-source inference engine behind "
     "Red Hat AI, to omni-modal models that take images, video and audio together. Each window of camera frames and "
     "speech becomes one structured observation: the objects in view, the people and what they are doing, events such as "
     "an item being moved or used, and where on the station the camera is. A closed-world reconciler matches each "
     "observation against the ISS catalog and the existing inventory in an **on-device Postgres database**, so the same "
     "laptop seen twice becomes one record with a history, not two. Anything the system cannot identify goes to a review "
     "queue with its image, description and timestamp for NASA to classify.")
para("Red Hat has already built and evaluated a working prototype on OpenShift. Over seven automated browser-based review "
     "rounds and roughly 3,800 automated tests, it ran NASA's public **\"Step Inside the International Space Station\"** "
     "tour video end to end. Using NASA's own on-screen captions as ground truth, the prototype placed observation "
     "windows in the correct module 65–75% of the time across four runs and **never assigned a wrong module**. The "
     "November POC moves that prototype onto the Dell GB10 that Dell has provided to Red Hat, the class of hardware "
     "NASA expects to fly, and validates it with NASA operators and data.")
doc.add_heading("POC Outcomes", 2)
bullets([
    "Run the complete pipeline on a single Dell GB10: video and audio in, inventory records out, dashboard and chatbot "
    "served from the same box.",
    "Demonstrate vLLM-Omni serving a US-origin omni model on the GB10, and measure its throughput against the sampling "
    "rate the inventory needs.",
    "Show closed-world tracking that minimises duplicate entries and routes every uncertain case to a human with evidence.",
    "Put the mission-control dashboard and the grounded chatbot in front of NASA operators and capture their feedback.",
    "Agree crew-privacy rules (no biometrics, no-capture zones) and verify them with evidence, not assertions.",
    "Define the operational pilot: hardware sizing, data integration, and the phase-2 agentic architecture.",
])

doc.add_heading("Contents", 1)
for line in [
    "1. Context and Problem Statement", "2. Goals, Scope, and Non-Goals", "3. POC Success Criteria",
    "4. Research Summary: vLLM-Omni and the Model Stack", "5. Key Architecture",
    "6. Deployment on the Dell GB10", "7. Perception Pipeline and Data Flow",
    "8. On-Device Inventory Database and Object Tracking", "9. Mission Control Dashboard and ChatBot",
    "10. Evaluation Plan", "11. Phase 2: Agentic Operations (Outlook)", "12. Engagement Plan",
    "13. Telemetry, Privacy, and Data Handling", "14. Risks, Assumptions, and Open Questions",
    "15. Product Evolution and Deliverables", "Appendix A. Observation Schema (Illustrative)",
    "Appendix B. Inventory API Surface (Illustrative)", "Appendix C. Source Basis",
]:
    p = para(line, color=BLUE, after=1)

# ================================================================= 1 CONTEXT
doc.add_heading("1. Context and Problem Statement", 1)
para("The ISS is a closed world. Almost every object aboard is known to a catalog, lives in a known stowage location, "
     "and has an expected count. The difficulty is not recognising a laptop; it is knowing **which** laptop it is, where "
     "it is now, who last handled it, and noticing when something appears that nobody has catalogued. A model that "
     "describes each camera frame on its own produces a new \"laptop\" every time it looks. The inventory then fills "
     "with duplicates and becomes useless.")
para("Three properties shape this POC:")
bullets([
    "**Multimodal at once.** Video shows what is there; audio carries the crew's own words (\"moving the drill to Node "
    "1\"). Both arrive together and must be understood together.",
    "**Closed-world tracking.** New observations are matched against the catalog and the existing inventory before any "
    "record is created, and uncertain matches go to a decision model or a human.",
    "**Crew privacy by design.** People are tracked by appearance only (clothing, never identity), and crew quarters "
    "and hygiene areas never store images or descriptions.",
])
callout("POC PRINCIPLE", "The omni model describes what it sees and hears. The inventory database is the source of truth. "
        "Every inventory change is traceable to evidence (a frame, a transcript, an operator), and nothing is merged or "
        "reclassified without either high confidence or a human decision.", NAVY, "EEF3F9")

# ================================================================= 2 GOALS
doc.add_heading("2. Goals, Scope, and Non-Goals", 1)
doc.add_heading("2.1 Goals", 2)
bullets([
    "Stand up the full stack on the Dell GB10 using Red Hat OpenShift AI (or its upstream, Open Data Hub) components.",
    "Serve the omni perception model through vLLM-Omni on the GB10 and characterise latency and throughput.",
    "Track objects and people from video and audio into an on-device Postgres inventory with low duplication.",
    "Give mission control a dashboard to browse, filter, correct and add items, and a chatbot to ask about them.",
    "Present unknown objects with image, description, location and timestamp so NASA can classify them.",
    "Evaluate with repeatable, automated measurements on NASA-provided or NASA-approved footage.",
])
doc.add_heading("2.2 In Scope", 2)
bullets([
    "One Dell GB10 running the complete stack, with the ARO development cluster as a fallback environment.",
    "Video plus audio ingest from recorded footage and, where available, a live camera feed.",
    "vLLM-Omni serving of the perception model; vLLM serving of the chat model, ASR and decision model.",
    "Postgres (with pgvector) inventory, S3-compatible object storage for evidence, and the inventory API.",
    "The mission-control dashboard and chatbot, including review, merge and undo workflows.",
    "Evaluation harnesses: synthetic gates, real-video scoring against ground truth, and automated UI review.",
    "Installation notes, configuration, test artifacts, findings and a product-gap log.",
])
doc.add_heading("2.3 Non-Goals", 2)
bullets([
    "Production availability, flight certification, or integration with operational ISS inventory systems.",
    "Biometric identification of crew members (face or voice recognition).",
    "Autonomous agents that change inventory without human approval (phase 2 explores agentic operation under policy).",
    "Training new foundation models during the engagement.",
    "Real-time guarantees: the POC samples frames at the rate the hardware sustains, not every frame.",
])

# ================================================================= 3 SUCCESS
doc.add_heading("3. POC Success Criteria", 1)
para("The criteria below are proposed for joint review. Targets are POC targets, not product commitments. The "
     "\"Prototype evidence\" column shows what the ARO prototype measured, for orientation.")
table(["Area", "Measure", "Proposed POC Target", "Prototype evidence"], [
    ["Integration", "End-to-end pipeline on the GB10",
     "Video + audio in; inventory, dashboard and chat served from one box", "Running end to end on ARO (T4 GPUs)"],
    ["Location accuracy", "Window module vs ground truth",
     "≥ 0.70, with no wrong-module assignments", "0.65–0.75 over 4 runs; 0 wrong modules"],
    ["Duplicate control", "New entities created on a revisit of the same scene",
     "≤ 5% (automated eval gate)", "0.0 on the synthetic revisit gate"],
    ["Unknown objects", "Review items with image, description, location, timestamp",
     "100% of unknowns", "100%; real loose items (orange sphere, tools, bags)"],
    ["Crew privacy", "Media or text stored from no-capture zones; crops overlapping people",
     "0 and 0", "0 face crops across the last 3 review rounds"],
    ["Chat grounding", "Answers that cite inventory records; standard question set",
     "100% cited; standard set answered correctly", "7 of 7 standard questions answered from the data"],
    ["Throughput", "Processing time per observation window on the GB10",
     "≤ the sampling stride agreed with NASA", "~25–60 s per window on T4 (fp32); to be measured on GB10"],
    ["Traceability", "Inventory changes with evidence and audit",
     "100%; merges reversible", "Audit rows, evidence frames, undoable merges"],
], widths=[1.15, 1.85, 1.9, 1.8], font_size=9)

# ================================================================= 4 RESEARCH
doc.add_heading("4. Research Summary: vLLM-Omni and the Model Stack", 1)
para("Before building, the team produced seven research reports (see Appendix C). This section summarises the findings "
     "that shape the architecture.")
doc.add_heading("4.1 vLLM-Omni: the crown jewel", 2)
para("vLLM-Omni is the omni-modal extension of vLLM. Where vLLM serves a single autoregressive model, vLLM-Omni serves a "
     "**graph of stages**: for example an audio and vision understanding stage (the \"thinker\"), a speech-generation "
     "stage (the \"talker\") and a waveform stage. Each stage is its own engine, configured in a deploy YAML, and stages run "
     "pipelined with streaming between them. For inventory tracking we need text out, not speech, so the POC uses "
     "**thinker-only** pipelines: one stage that consumes frames and audio and produces structured text.")
table(["Serving path", "What it is", "Fit for this POC"], [
    ["Windowed chat completions", "OpenAI-compatible /v1/chat/completions with N frames plus an audio clip per request",
     "**Used by the prototype.** Stateless, easy to validate and retry; one structured observation per window"],
    ["/v1/video/chat/stream", "Purpose-built streaming video: frame and audio chunks, near-duplicate frame filtering, "
                              "incremental prefill", "**Primary candidate on the GB10:** lower latency per window by "
                                                      "prefilling frames as they arrive"],
    ["/v1/realtime?duplex=0", "OpenAI-Realtime turn-based sessions; image input added by PR #8896 (merged 2026-10-14)",
     "Useful for interactive voice + camera sessions with crew; not needed for passive tracking"],
    ["/v1/realtime?duplex=1", "Native full-duplex conversation; camera frames ride on the audio stream",
     "Conversational agents (phase 2); only some models consume frames on this path today"],
], widths=[1.7, 2.6, 2.4], font_size=9)
bullets([
    "**Arm64 matters.** vLLM-Omni publishes multi-architecture images (including nightly aarch64 builds), which is what "
    "the GB10's Arm CPU needs.",
    "**Quantization is per stage.** Only the thinker language model is quantized; the audio and vision encoders stay at "
    "full precision. FP8 or NVFP4 on Blackwell keeps the omni model small and fast.",
    "**Structured output is validated in our code.** The prototype validates each reply against a JSON schema, repairs or "
    "salvages truncated replies, and never writes an unvalidated observation to the database.",
])
callout("POC GOAL", "The prototype proved the vLLM-Omni path with Qwen2.5-Omni, and proved the US-origin model (Gemma 4 12B) "
        "through vLLM's multimodal API. vLLM-Omni does not yet ship a pipeline for Gemma 4 or NVIDIA's Nemotron 3 Nano Omni. "
        "Delivering and measuring that pipeline on the GB10 is a headline POC objective and a natural upstream contribution.",
        BLUE, "EAF1FA")
doc.add_heading("4.2 Omni model selection (US-origin)", 2)
para("NASA requires US-origin models. The team compared the original open model, Qwen2.5-Omni-7B (Alibaba), with Google "
     "Gemma 4 12B on the same eight windows of the NASA tour video, using the same perception client:")
table(["On the same 8 NASA windows", "Qwen2.5-Omni-7B", "Gemma 4 12B"], [
    ["Valid structured replies", "8 / 8", "8 / 8"],
    ["Whole-frame \"junk\" boxes", "35 of 73 objects", "0 of 50 objects"],
    ["Object naming", "Generic (\"space station interior\")", "Specific ISS hardware (hatch, EXPRESS rack, stowage bags)"],
    ["People", "Generic", "Appearance descriptions (\"astronaut in a blue shirt floats in Kibo\")"],
    ["Latency per window (T4 test GPUs)", "22 s", "~25–60 s (fp32 on T4; see 4.4)"],
], widths=[2.3, 2.0, 2.4], font_size=9)
bullets([
    "**Gemma 4 12B** (Apache-2.0) takes images and speech audio natively and returns object boxes in its own 0–1000 "
    "grid format, which the pipeline now converts. It is the POC default.",
    "**NVIDIA Nemotron 3 Nano Omni** is the alternative for the GB10: built from NVIDIA components, it takes video plus "
    "up to an hour of audio, ships NVFP4 weights, and has a DGX Spark recipe. A bake-off on the GB10 is proposed.",
    "**NVIDIA Cosmos Reason** models are post-trained from Qwen-VL. If NASA's US-origin rule covers weight lineage, they "
    "do not qualify; NASA to confirm.",
])
doc.add_heading("4.3 Supporting models", 2)
table(["Role", "Model", "Why"], [
    ["Speech-to-text (transcript of record)", "OpenAI Whisper large-v3-turbo (MIT)",
     "Accurate, fast, US-origin; the omni model handles ambiguity, Whisper provides the record"],
    ["Chatbot / inventory agent", "Google Gemma 4 12B (QAT W4A16), tool calling",
     "Same family as perception; one model can serve both on the GB10"],
    ["Decision model (\"same object or new?\")", "Google DiffusionGemma-26B-A4B",
     "Calibrated probabilities for ambiguous matches; used only in the uncertain band"],
    ["Object boxes", "Google OWLv2 (open-vocabulary detector)",
     "Precise boxes for catalog items and for unknowns, queried by their own label"],
    ["Appearance embeddings", "Meta DINOv2", "Re-identify the same physical object across time (pgvector search)"],
], widths=[2.0, 2.2, 2.5], font_size=9)
doc.add_heading("4.4 Hardware findings", 2)
bullets([
    "The T4 development GPUs have no bf16, and Gemma 4's image path overflows in fp16, so the prototype runs it in fp32: "
    "correct but slow. The GB10's Blackwell GPU runs bf16 and FP8 natively, removing this limitation.",
    "The GB10's 128 GB of unified memory holds every model at once; its ~273 GB/s memory bandwidth makes decode speed the "
    "constraint. Quantization (FP8/NVFP4), short structured replies and frame sampling are the levers.",
    "Real time is not required for inventory. The prototype analyses a six-frame window every 30 seconds of video; the "
    "POC will set the stride from measured GB10 throughput.",
])

# ================================================================= 5 ARCHITECTURE
doc.add_heading("5. Key Architecture", 1)
para("Figure 1 shows the target POC architecture. Camera video and audio enter the perception service, which samples "
     "frames and audio into observation windows. vLLM-Omni turns each window into a structured observation. The "
     "reconciler matches it against the catalog and the inventory, asking the decision model when a match is uncertain, "
     "and writes the result through the inventory API into Postgres and the evidence store. Mission control works "
     "through the dashboard and the chatbot.")
screenshot(1, IMG / "fig1-architecture.png",
           "Target POC architecture: vLLM-Omni perception, on-device inventory, dashboard and chatbot on one Dell GB10.",
           width=6.7)
doc.add_heading("5.1 Component Responsibilities", 2)
table(["Component", "POC Responsibility", "Boundary"], [
    ["Perception service", "Sample frames and audio into windows; get boxes and embeddings; call vLLM-Omni; reconcile; "
                           "write observations", "Never writes to Postgres directly; all writes go through the API"],
    ["vLLM-Omni (omni model)", "Describe each window as a structured observation: objects, people, events, location hint",
     "Proposes; does not decide identity or merge records"],
    ["Whisper (ASR)", "Transcript of record for speech", "Speech claims stay unconfirmed until video agrees"],
    ["Decision model", "Choose \"same object\" or \"new\" for ambiguous matches, with probabilities",
     "Only consulted in the uncertain band; falls back safely to review"],
    ["Inventory API", "Single writer to the inventory: items, sightings, events, reviews, merges, audit",
     "Enforces privacy rules and data contracts"],
    ["Postgres + pgvector", "Source of truth for inventory, history, embeddings and audit", "On-device; no external sync in POC"],
    ["Object store", "Evidence frames, crops, raw model replies, operator photos", "Nothing stored from private areas"],
    ["Dashboard", "Browse, filter, review, classify, edit, merge/undo, add items", "Operator name required for changes"],
    ["Chatbot / agent", "Answer questions from inventory data with citations; propose (not apply) updates",
     "Read tools plus proposals; no autonomous writes in phase 1"],
], widths=[1.6, 3.0, 2.1], font_size=9)

# ================================================================= 6 DEPLOYMENT
doc.add_heading("6. Deployment on the Dell GB10", 1)
para("Dell has provided Red Hat a GB10 (DGX Spark class) for POC work, with an expected arrival in mid-November 2026. "
     "The GB10 is a single node: a Blackwell GPU and a 20-core Arm CPU sharing 128 GB of unified memory. All models "
     "therefore share one GPU, so each model is given an explicit memory fraction.")
table(["Process", "Memory fraction", "≈ GB", "Notes"], [
    ["Gemma 4 12B FP8 (omni + chat)", "0.24", "31", "One server for perception and chat"],
    ["Decision model (DiffusionGemma INT8)", "0.30", "38", "Single GPU; no tensor parallelism needed"],
    ["Whisper large-v3-turbo", "0.05", "6", ""],
    ["Perception (OWLv2 + DINOv2)", "n/a", "≈ 3", "PyTorch, outside vLLM"],
    ["OS, Postgres, object store, API, dashboard", "n/a", "≈ 20", ""],
    ["Total", "0.59 (vLLM)", "≈ 98", "≈ 30 GB headroom"],
], widths=[2.6, 1.2, 0.7, 2.2], font_size=9)
bullets([
    "**Single-node OpenShift or MicroShift** on the GB10, with the NVIDIA GPU operator sharing the GPU across model pods.",
    "**Offline by design:** model weights are pre-loaded to a local volume, as they would be on orbit.",
    "**Fallback:** the ARO development cluster runs the identical stack (on T4 GPUs) if the GB10 is late or needs debugging.",
])
callout("IMPLEMENTATION NOTE", "A GB10 deployment profile (manifests, memory budget and a verification checklist) has been "
        "written but cannot be tested until the hardware arrives. The first POC milestone is running that checklist on the "
        "GB10: GPU and CUDA versions, model loading, text/image/audio smoke tests, and the evaluation suites.",
        ORANGE, "FDF3E1")
screenshot(2, IMG / "fig2-gb10-deployment.png",
           "Single-node GB10 deployment: GPU sharing, deployed components and persistent volumes.", width=6.7)

# ================================================================= 7 PERCEPTION FLOW
doc.add_heading("7. Perception Pipeline and Data Flow", 1)
doc.add_heading("7.1 Per-window flow", 2)
numbered([
    "Ingest decodes video and audio together and samples keyframes (for example one per second) and audio chunks.",
    "A window of frames (six in the prototype) plus its audio is assembled every stride (30 s in the prototype).",
    "Whisper transcribes the speech; silent windows are detected and skipped.",
    "vLLM-Omni receives the frames and audio and returns one structured observation (Appendix A), validated against a JSON "
    "schema. Truncated replies are salvaged rather than discarded.",
    "OWLv2 finds precise boxes for catalog classes, and for each unknown object using its own label as the query. "
    "DINOv2 embeds each crop. Any crop overlapping a person is dropped before it is stored or embedded.",
    "The location resolver maps signage and speech to ISS modules and zones; private zones force a no-capture window.",
    "The reconciler matches each object and person to the inventory (section 8) and posts the window to the inventory API.",
    "The API writes items, sightings, events, reviews and audit rows in one transaction; evidence goes to object storage.",
])
screenshot(3, IMG / "fig3-perception.png", "Perception pipeline for one observation window.", width=6.7)
doc.add_heading("7.2 Failure behaviour", 2)
table(["Condition", "Pipeline behaviour", "Evidence"], [
    ["Omni reply invalid or cut off", "Salvage the complete part; retry once otherwise; never write invalid data",
     "Per-window usage and validity log"],
    ["Decision model unavailable", "Do not guess: attach to the best candidate and open a review, or create a new item "
                                   "flagged for review", "Decision log with reason"],
    ["ASR failure", "Continue with video; speech-only claims are not recorded", "Window status"],
    ["Private zone or sleep cue", "Window becomes no-capture: no frames, crops, text or reviews stored", "Privacy flag on window"],
    ["Crop overlaps a person", "Crop dropped; sighting kept as a location fact; review shows whole frame",
     "person_overlap flag on sighting"],
    ["Station structure (hatch, handrail)", "Not raised as an unknown; operators' \"Not inventory\" decisions are remembered",
     "Structure memory and counters"],
], widths=[1.9, 3.0, 1.8], font_size=9)

# ================================================================= 8 DATABASE
doc.add_heading("8. On-Device Inventory Database and Object Tracking", 1)
para("Postgres 16 with the pgvector extension runs on the GB10 and is the single source of truth. The perception "
     "pipeline proposes; the database records what is known, with evidence and history, and the dashboard and chatbot read "
     "from it.")
doc.add_heading("8.1 Data model", 2)
table(["Table", "Holds"], [
    ["location", "ISS location hierarchy: segment → module → zone (e.g. USOS → LAB1 → LAB1S2), with a no_capture flag "
                 "for crew quarters and the hygiene area"],
    ["catalog_item", "What can exist aboard: class, synonyms, category, home location, expected count, fungible or not"],
    ["entity", "Each tracked physical item or person track: status, last location and time, confidence, snapshot, "
               "merge links, location provenance (who set it, when)"],
    ["entity_exemplar", "DINOv2 appearance embeddings (halfvec, HNSW index) used to recognise the same item again"],
    ["sighting", "Every time an entity was seen: when, where, box and box source, evidence frame, privacy flags"],
    ["event", "Moved, used, handed over, added, removed, stowed, consumed: with actor description and evidence"],
    ["observation_window", "Each analysed window: time span, scene summary, transcript, private flag"],
    ["review_item", "Work for operators: unknown objects, possible duplicates, low-confidence or conflicting labels"],
    ["stock", "Counts for fungible items (food packs, wipes) instead of individual records"],
    ["structure_memory", "Operator \"Not inventory\" decisions so station structure is not raised again"],
    ["audit_log / decision_log", "Every change and every decision-model call, for traceability and undo"],
], widths=[1.6, 5.1], font_size=9)
screenshot(4, IMG / "fig4-data-model.png", "Inventory data model.", width=6.7)
doc.add_heading("8.2 How tracking keeps duplicates down", 2)
numbered([
    "**Map to the catalog.** The observed label is mapped to a catalog class using names and synonyms. Station structure "
    "and items worn by crew are filtered out; truly unknown loose items become unknowns.",
    "**Consolidate within the window.** The same object listed in several frames becomes one instance.",
    "**Retrieve candidates.** pgvector finds existing items of the same class with similar appearance, plus recently seen "
    "items in the same module.",
    "**Apply hard gates.** Different class, or an impossible move (seen in another module seconds ago), rules a candidate "
    "out. Colour clashes in the description rule out look-alikes.",
    "**Decide in three bands.** Similarity ≥ 0.85: attach automatically. 0.50–0.85: ask the decision model, with "
    "descriptions and timing. Below 0.50: create a new item.",
    "**Flag, don't hide.** If a class exceeds its expected count, a possible-duplicate review is opened for an operator.",
    "**Handle revisits.** Returning to a module after a long gap reattaches to the existing items rather than creating new ones.",
])
screenshot(5, IMG / "fig5-dedup-flow.png", "Tracking and de-duplication decision flow.", width=6.7)
doc.add_heading("8.3 People, locations and privacy", 2)
bullets([
    "**People are appearance-only tracks** (\"blue shirt, grey shorts\"), never identities. No face crops or embeddings are "
    "stored for people. Tracks are approximate by design and may over-count when crew change clothes.",
    "**Locations use ISS conventions** (module and zone codes in the style of SSP 50621). Signage and speech set the module; "
    "for a moving handheld camera the last named module carries forward briefly. Fixed cameras give the most reliable "
    "location, and NASA's camera inventory is an input to the POC.",
    "**No-capture zones** (crew quarters, hygiene) and windows showing sleeping crew store no images and no text; only a "
    "label and a place can be recorded.",
    "**Every change is reversible.** Merges keep full undo detail; operator edits record who set a location and when, "
    "separately from when the camera last saw the item.",
])

# ================================================================= 9 DASHBOARD
doc.add_heading("9. Mission Control Dashboard and ChatBot", 1)
para("The dashboard follows the inventory views in NASA's initial sketches: items last seen with confidence, location, "
     "the person who last handled them and the saved frame; filters, search and timestamps; and management actions for "
     "items that changed off-screen or have not yet been seen. It is a React single-page application served from the GB10.")
doc.add_heading("9.1 Pages", 2)
table(["Page", "What operators do there"], [
    ["Inventory", "Search and filter (kind, location, status, minimum confidence, time range, needs review); sort by last "
                  "seen; see last handled (\"used by person in blue shirt · 09:54 UTC\")"],
    ["Item detail", "Frames with boxes (solid = detector, dashed = approximate), sighting history, events, edit location "
                    "or description, merge and unmerge"],
    ["Review queue", "Unknown objects with image, description, location and time: classify, create a catalog item, mark "
                     "as station structure, or dismiss. Possible duplicates side by side with ranked reasons: merge "
                     "(with undo) or keep both"],
    ["Add item", "Add an item not yet seen by a camera: image required, label, location, catalog class"],
    ["Overview", "Counts, person tracks by module, recent events in plain English, and a live activity feed"],
    ["Chat", "Ask the inventory in natural language (section 9.3)"],
], widths=[1.4, 5.3], font_size=9)
screenshot(6, IMG / "dash-inventory.png", "Inventory view (prototype, NASA public tour footage).")
screenshot(7, IMG / "dash-review-unknown.png", "Review queue: unknown objects with frame, box, description, location "
                                             "and timestamp, ready for NASA to classify.")
screenshot(8, IMG / "dash-item-detail.png", "Item detail: evidence frame with detector box and close-up, history and actions.")
doc.add_heading("9.2 Design principles", 2)
bullets([
    "**Times are always UTC**, absolute with relative beside them; locations always show name and code.",
    "**Every action names an operator** and is audited; destructive actions confirm first and can be undone.",
    "**Show evidence, not just conclusions:** boxes say whether they came from the detector or are approximate.",
    "**Accessibility:** zero automated accessibility violations (axe) across all pages at 1024 and 1440 px widths.",
    "**Crew-privacy wording** in the interface explains why private areas show no image.",
])
doc.add_heading("9.3 Mission-control ChatBot", 2)
para("The chatbot is a tool-calling agent (Gemma 4) over the inventory API. It answers only from inventory records and "
     "cites them; when a question cannot be answered from the data it says what it checked rather than guessing.")
table(["Example question", "How it is answered"], [
    ["Where is the laptop right now?", "Lists matching items with last location and time, and states the total"],
    ["Where is the multimeter usually kept, and how many should there be?", "Catalog: home location and expected count"],
    ["Who used the drill?", "Event history with the actor's appearance and time"],
    ["What changed in the last hour?", "Time window computed in code; paged change list with stable \"show more\""],
    ["Who was in the Cupola?", "Person tracks with sightings there, with the non-biometric caveat"],
    ["How many people have been seen today, and where?", "Exact counts by module from the database, never estimated"],
    ["What is in the crew quarters?", "Privacy-safe answer: nothing is recorded there by design"],
], widths=[2.9, 3.8], font_size=9)
bullets([
    "**Grounding:** every factual claim must be supported by a tool result; unsupported wording is replaced by an answer "
    "built only from the data, and counts must match a database total.",
    "**Live status:** the interface shows which tools are being used while the answer is prepared; answers stream in.",
    "**Proposals, not writes:** in phase 1 the agent can propose an update; an operator confirms it.",
    "**MCP server:** the same inventory tools are exposed over the Model Context Protocol, so phase-2 agent runtimes can "
    "use them unchanged.",
])
screenshot(9, IMG / "dash-chat.png", "Mission-control chatbot answering a people-count question with citations.")

# ================================================================= 10 EVAL
doc.add_heading("10. Evaluation Plan", 1)
table(["Evaluation", "What it measures", "How"], [
    ["Automated test suites", "Correctness of every component and contract", "~3,800 tests (unit, integration, "
                                                                            "end-to-end) run on every change"],
    ["Synthetic eval gates", "Duplicate rate, pairwise F1, location accuracy, unknown recall, revisit duplication, "
                             "person-track recall, privacy", "Scripted scenes with known truth; gates must pass"],
    ["Real-video eval", "Window location accuracy and module coverage", "NASA on-screen captions as ground truth; "
                                                                        "per-module confusion matrix"],
    ["Omni model A/B", "Validity, box quality, naming, latency", "Same windows through each candidate model"],
    ["Chat question set", "Grounded, correct answers and latency", "Standard questions in one conversation"],
    ["Automated UI review", "Design fit, usability, accessibility, privacy", "Playwright browser reviewer with screenshots; "
                                                                             "findings become tests and fixes"],
    ["NASA operator review", "Fitness for mission-control use", "Hands-on sessions during onsites 1 and 2"],
], widths=[1.6, 2.6, 2.5], font_size=9)
doc.add_heading("10.1 Data for the POC", 2)
bullets([
    "Public NASA footage (the prototype uses \"Step Inside the International Space Station\", 2022) for repeatable runs.",
    "NASA-approved recorded footage from fixed and handheld cameras, with ground truth agreed with NASA SMEs.",
    "The NASA catalog and location model, replacing the prototype's illustrative seed data.",
    "A live camera feed on the GB10 where NASA can provide one.",
])
doc.add_heading("10.2 Decision rule", 2)
para("The POC succeeds when the GB10 runs the full stack, the agreed targets in section 3 are met on NASA-approved "
     "footage, privacy checks pass with evidence, and NASA operators judge the dashboard and chatbot useful. Where a "
     "target is missed, the POC records why and what would close the gap (hardware, data, or model).")

# ================================================================= 11 PHASE 2
doc.add_heading("11. Phase 2: Agentic Operations (Outlook)", 1)
para("Phase 1 keeps agents read-only with human-confirmed proposals. NASA's architecture sketches also describe an "
     "inventory agent and an agentic memory layer that compresses context while prioritising inventory state. Phase 2 "
     "will design that side; it is outlined here so that phase-1 interfaces are ready for it.")
table(["Candidate", "What it provides", "Why it fits"], [
    ["Red Hat AI enterprise Claw agent (OpenClaw Enterprise)", "An open-source control plane for persistent agents across "
     "users and teams, from the OpenClaw Foundation, co-developed by Red Hat, NVIDIA and others",
     "Enterprise operation of long-running inventory agents: identity, governance and lifecycle"],
    ["NVIDIA OpenShell", "An open-source (Apache-2.0) runtime that runs agents in isolated sandboxes, with declarative "
                         "policies limiting files, credentials and network access", "Strong containment for agents that "
                                                                                    "act on station systems"],
    ["Inventory MCP server (built in phase 1)", "The inventory tools over the Model Context Protocol",
     "Lets either runtime use the same audited tools without new integration"],
], widths=[2.0, 2.6, 2.1], font_size=9)
bullets([
    "Agentic memory (\"dreaming\"): periodic compression of observations that keeps inventory state first; a prototype "
    "job exists and will be reviewed in phase 2.",
    "Policy: which actions an agent may take on its own, which need a human, and how that is enforced and audited.",
    "Crew interaction: voice and camera sessions through vLLM-Omni's realtime paths.",
])
screenshot(10, IMG / "fig10-phase2-agentic.png", "Phase-2 agentic architecture (outlook, to be designed with NASA).", width=6.7)

# ================================================================= 12 ENGAGEMENT
doc.add_heading("12. Engagement Plan", 1)
para("The POC starts in November 2026 and targets completion in January 2027. It includes two onsite visits. The Dell "
     "GB10 is expected mid-November, and onsite 1 is targeted for the week of November 17; the plan front-loads GB10 "
     "bring-up so that onsite time is spent on review and evaluation, not installation. Between the visits, the team "
     "iterates on the feedback and data from onsite 1, so that onsite 2 (date TBD) reviews measured improvements on "
     "NASA footage.")
table(["When", "Primary Activities", "Exit Criteria", "Key Participants"], [
    ["Early November (pre-work)", "Confirm data, catalog and location model with NASA; agree privacy rules; prepare "
                                 "offline model bundle; finalise the GB10 profile", "Data and privacy inputs agreed; "
                                                                                   "install bundle ready",
     "NASA leads; Red Hat POC team"],
    ["GB10 arrival (mid-November)", "Bring up OpenShift/MicroShift, GPU operator and storage; deploy the stack; run the "
                                     "GB10 checklist and evaluation suites", "Full stack running on the GB10; first "
                                                                             "throughput numbers", "Red Hat POC team"],
    ["Onsite 1 (week of Nov 17)", "Architecture walkthrough and live demo on the GB10; vLLM-Omni deep dive; inventory "
                                  "data model and tracking with NASA SMEs; privacy review; first hands-on dashboard and "
                                  "chatbot session", "Shared understanding; agreed evaluation footage and targets; "
                                                     "feedback and gap log", "NASA platform, operations and privacy SMEs; "
                                                                             "mission-control users; Red Hat"],
    ["Iteration (late Nov to onsite 2)", "Run the stack on NASA-approved footage; address onsite-1 findings; tune "
                                         "sampling, location and duplicate control against NASA ground truth; omni "
                                         "model bake-off on the GB10 (Gemma 4 vs Nemotron Omni)",
     "Findings closed or explained; updated evaluation results", "Red Hat POC team; NASA SMEs for data and questions"],
    ["Onsite 2 (TBD)", "Mission-control operators use the improved dashboard and chatbot; evaluation results against "
                       "the agreed targets; phase-2 agentic planning", "Targets met or gaps explained; agreed next "
                                                                       "steps", "NASA mission-control users and "
                                                                                "stakeholders; Red Hat"],
    ["POC completion (January 2027)", "Final evaluation; written POC report; operational pilot recommendation",
     "POC report and pilot recommendation", "Red Hat with NASA review"],
], widths=[1.35, 2.35, 1.6, 1.4], font_size=9)
callout("FALLBACK", "If the GB10 arrives late or needs debugging, onsite 1 uses the identical stack on the ARO "
        "development cluster, and GB10 results are reviewed at onsite 2.", ORANGE, "FDF3E1")

# ================================================================= 13 TELEMETRY & PRIVACY
doc.add_heading("13. Telemetry, Privacy, and Data Handling", 1)
doc.add_heading("13.1 Minimum records", 2)
table(["Record", "Purpose", "POC handling"], [
    ["Observation window", "What was seen and heard, when, by which camera", "Private windows keep no text or media"],
    ["Model usage", "Tokens, latency, finish reason, truncation per window", "Throughput and quality analysis"],
    ["Decision log", "Each decision-model call: candidates, probabilities, choice", "No descriptive text for private windows"],
    ["Sighting / event", "Where and when items and people were seen and what happened", "Evidence frame only in "
                                                                                        "capturable areas"],
    ["Audit log", "Every operator and pipeline change, with undo detail", "Retained for the POC duration"],
], widths=[1.6, 2.8, 2.3], font_size=9)
doc.add_heading("13.2 Privacy and retention", 2)
bullets([
    "No biometric identification: no face recognition, no voice identification, no face crops or person embeddings.",
    "No-capture zones and sleep-cue windows store no images, crops, transcripts, descriptions or reviews.",
    "Any object crop that overlaps a person is discarded before storage, enforced in both the pipeline and the API.",
    "Whole frames that include crew are stored as evidence: NASA to confirm this policy.",
    "All data stays on the GB10 or the approved NASA environment; no transfer without explicit authorisation.",
    "Secrets are generated on the cluster and never stored in source code.",
])

# ================================================================= 14 RISKS
doc.add_heading("14. Risks, Assumptions, and Open Questions", 1)
doc.add_heading("14.1 Assumptions and Prerequisites", 2)
bullets([
    "The Dell GB10 arrives by mid-November and can run single-node OpenShift or MicroShift with the NVIDIA GPU operator.",
    "NASA provides approved footage, the catalog, the location model and SMEs for both onsites and the iteration phase.",
    "Open-source, US-origin models are acceptable (Gemma 4 and DiffusionGemma under Google's terms; Whisper MIT).",
    "The POC uses non-operational, public or otherwise approved data.",
])
doc.add_heading("14.2 Risks and Mitigations", 2)
table(["Risk", "Impact", "Mitigation"], [
    ["GB10 arrives late", "Less time on target hardware before onsite 1", "Identical stack on ARO for onsite 1; GB10 results at onsite 2"],
    ["Decode speed on the GB10 (≈273 GB/s)", "Fewer windows per minute", "FP8/NVFP4, short structured replies, "
                                                                          "streaming prefill, tuned sampling stride"],
    ["vLLM-Omni pipeline for Gemma 4 / Nemotron Omni not ready", "Omni model served through plain vLLM instead",
     "Prototype already works this way; pipeline delivered as a POC objective"],
    ["Location from handheld video is ambiguous", "Items placed only at segment level", "Use fixed-camera positions "
                                                                                         "and signage; NASA camera inventory"],
    ["Person tracks over-count", "Misleading people counts", "Clearly labelled as approximate; no identity use"],
    ["Model labels disagree with descriptions", "Wrong catalog class", "Conflict detection opens a low-confidence review"],
    ["Privacy expectations differ", "Rework of data handling", "Agree rules in pre-work; verify with evidence"],
], widths=[2.1, 2.0, 2.6], font_size=9)
doc.add_heading("14.3 Open Questions to Resolve at Onsite 1", 2)
bullets([
    "When is onsite 2? It should leave time after the iteration phase and before January 2027 completion.",
    "Which cameras (fixed and handheld) and positions will feed the system, and is a live feed available for the POC?",
    "Does NASA's US-origin requirement cover weight lineage (e.g. Cosmos models derived from Qwen-VL)?",
    "Are whole frames that include crew acceptable as stored evidence?",
    "Should windows showing sleeping crew always be treated as private?",
    "Should the chatbot report presence (not content) in private areas, or decline as it does today?",
    "What sampling rate is acceptable for inventory on the GB10?",
    "Which inventory system of record should the POC eventually reconcile with?",
    "Who owns the catalog and location model after the POC?",
])

# ================================================================= 15 DELIVERABLES
doc.add_heading("15. Product Evolution and Deliverables", 1)
doc.add_heading("15.1 Product Questions the POC Must Answer", 2)
bullets([
    "Is vLLM-Omni on the GB10 fast enough for station inventory, and with which model and quantization?",
    "What does a supported omni pipeline for US-origin models need (vLLM-Omni pipeline, packaging, lifecycle)?",
    "Which parts belong in Red Hat AI (serving, evaluation, model lifecycle) versus the application?",
    "What does the agentic phase need from OpenClaw Enterprise and OpenShell?",
])
doc.add_heading("15.2 POC Deliverables", 2)
bullets([
    "Running stack on the Dell GB10: vLLM-Omni perception, ASR, decision model, inventory, dashboard and chatbot.",
    "GB10 measurements: throughput per window, memory use, and an omni model bake-off.",
    "Evaluation report: test suites, eval gates, real-video accuracy, chat question set, UI review findings.",
    "Privacy verification evidence (no-capture zones, no face crops, private-window redaction).",
    "Installation and operator runbook; configuration; data model and API documentation.",
    "Phase-2 agentic architecture proposal and a prioritised next-step backlog.",
])
page_break()

# ================================================================= APPENDICES
doc.add_heading("Appendix A. Observation Schema (Illustrative)", 1)
para("Each observation window produces one JSON object. The reconciler validates it against this schema before anything "
     "reaches the database. Field values below are illustrative.")
code('''{
  "scene_summary": "A crew member in a blue shirt floats in the Kibo module near a stowage rack.",
  "location_hint": "Japanese Experiment Module Kibo",
  "objects": [
    {"label": "orange sphere", "description": "small orange spherical object near the hatch",
     "bbox": [0.52, 0.42, 0.56, 0.48], "frame_index": 3, "confidence": 0.82, "held_by": null},
    {"label": "laptop", "description": "silver laptop on a rack",
     "bbox": [0.10, 0.55, 0.24, 0.70], "frame_index": 1, "confidence": 0.90, "held_by": "P1"}
  ],
  "people": [
    {"ref": "P1", "appearance": "blue shirt, dark pants", "activity": "using a laptop", "is_speaking": false}
  ],
  "events": [
    {"type": "used", "object_label": "laptop", "actor_ref": "P1", "frame_index": 1,
     "evidence": "P1 types on the laptop", "confidence": 0.80}
  ],
  "transcript": "",
  "audio_events": []
}''')
doc.add_heading("Appendix B. Inventory API Surface (Illustrative)", 1)
table(["Endpoint", "Purpose"], [
    ["GET /items, GET /items/{id}", "List with filters; item detail with sightings, events and history"],
    ["POST /items (image required), PATCH /items/{id}", "Add an unseen item; edit an item changed off-screen"],
    ["POST /items/{id}/merge, POST /items/{id}/unmerge", "Merge duplicates with full undo"],
    ["GET /review, POST /review/{id}/resolve", "Review queue; classify, new catalog item, merge, not inventory, dismiss"],
    ["GET /catalog, GET /locations, GET /events, GET /search, GET /stats", "Reference data, history, search and counts"],
    ["POST /ingest/candidates, POST /ingest/window", "Perception: candidate retrieval and transactional window ingest"],
    ["GET /media/{key}", "Evidence images (never from private areas)"],
], widths=[3.0, 3.7], font_size=9)
doc.add_heading("Appendix C. Source Basis", 1)
bullets([
    "Red Hat research reports produced for this POC: vLLM-Omni realtime serving; tracking, de-duplication and evaluation; "
    "audio fusion; ISS domain (catalog, location model, privacy zones); agent memory and platform; decision models; "
    "US-origin model selection.",
    "vLLM-Omni project documentation and pull request #8896 (image input on the turn-based realtime API).",
    "NASA public-domain video \"Step Inside the International Space Station\" (2022) for prototype evaluation.",
    "Public documentation for Gemma 4, DiffusionGemma, Whisper, OWLv2, DINOv2, NVIDIA Nemotron 3 Nano Omni, OpenClaw "
    "Enterprise and NVIDIA OpenShell.",
])
callout("SOURCE AND ASSUMPTION NOTE", "Prototype figures come from Red Hat's development cluster and NASA public footage, "
        "not NASA operational data. ISS catalog entries, location codes and privacy zones in the prototype are "
        "illustrative until NASA provides the authoritative versions.", ORANGE, "FDF3E1")

doc.save(OUT)
print("wrote", OUT)
