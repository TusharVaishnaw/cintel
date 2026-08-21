// build_ppt.js <slide_data.json> <out.pptx>
// Data-driven renderer. Every add* function skips itself/its rows when the
// underlying field is missing — never prints a placeholder for null data.
const fs = require("fs");
const pptxgen = require("pptxgenjs");

const IN = process.argv[2], OUT = process.argv[3];
const d = JSON.parse(fs.readFileSync(IN, "utf8"));

const FONT = "Calibri";
const NAVY = "1F2D50";
const NAVY_DK = "13192E";
const ACCENT = "2E6F9E";
const SLATE = "5B6472";
const LIGHT_BG = "F4F6F9";
const CARD_BG = "FFFFFF";
const WHITE = "FFFFFF";
const GOOD = "3C8A5B";
const WARN = "C6862B";
const BAD = "B5473A";

const pres = new pptxgen();
pres.layout = "LAYOUT_WIDE"; // 13.3 x 7.5
const PW = 13.33, PH = 7.5;

function newSlide(bg) {
  const sl = pres.addSlide();
  sl.background = { color: bg || WHITE };
  return sl;
}

function footer(sl, label) {
  sl.addText(label || d.company_name, {
    x: 0.4, y: PH - 0.35, w: 8, h: 0.3, fontFace: FONT, fontSize: 9, color: SLATE,
  });
  sl.addText(`Company Intelligence`, {
    x: PW - 3.4, y: PH - 0.35, w: 3.0, h: 0.3, fontFace: FONT, fontSize: 9, color: SLATE, align: "right",
  });
}

function slideTitle(sl, text) {
  sl.addText(text, {
    x: 0.5, y: 0.35, w: PW - 1.0, h: 0.65, fontFace: FONT, fontSize: 26, bold: true, color: NAVY,
  });
}

function card(sl, x, y, w, h, opts = {}) {
  sl.addShape("roundRect", {
    x, y, w, h, rectRadius: 0.06,
    fill: { color: opts.fill || CARD_BG },
    line: { color: opts.line || "E3E7EE", width: 1 },
    shadow: { type: "outer", color: "888888", opacity: 0.18, blur: 6, offset: 2, angle: 90 },
  });
}

// ---------- 1. Title slide ----------
function addTitleSlide() {
  const sl = newSlide(NAVY_DK);
  sl.addText(d.company_name, {
    x: 0.8, y: 2.7, w: PW - 1.6, h: 1.3, fontFace: FONT, fontSize: 44, bold: true, color: WHITE,
  });
  sl.addText("Company Intelligence", {
    x: 0.8, y: 3.9, w: PW - 1.6, h: 0.6, fontFace: FONT, fontSize: 20, color: "AFC6E3",
  });
  if (d.ticker) {
    sl.addText(d.ticker, {
      x: 0.8, y: 4.45, w: 4, h: 0.4, fontFace: FONT, fontSize: 13, color: "7E96BF",
    });
  }
  sl.addText("Information Resource Centre", {
    x: 0.8, y: PH - 0.7, w: 6, h: 0.4, fontFace: FONT, fontSize: 11, color: "7E96BF",
  });
}

// ---------- Section separator ----------
function addSeparator(title) {
  const sl = newSlide(NAVY);
  sl.addText(title, {
    x: 0.8, y: PH / 2 - 0.5, w: PW - 1.6, h: 1.0, fontFace: FONT, fontSize: 32, bold: true, color: WHITE,
  });
}

// ---------- 2. Company overview ----------
function addOverview() {
  const sl = newSlide();
  slideTitle(sl, "Company Overview");
  const ov = d.overview;
  const stats = [
    ["Founded", ov.founded], ["Headquarters", ov.headquarters], ["Employees", ov.employees],
    ["Revenue", ov.revenue], ["Market Cap", ov.market_cap],
  ].filter(([, v]) => v);

  const colW = (PW - 1.0 - 0.3 * (stats.length - 1)) / Math.max(stats.length, 1);
  stats.forEach(([label, val], i) => {
    const x = 0.5 + i * (colW + 0.3);
    card(sl, x, 1.25, colW, 1.1);
    sl.addText(label.toUpperCase(), { x: x + 0.12, y: 1.35, w: colW - 0.24, h: 0.3, fontFace: FONT, fontSize: 9, color: SLATE, bold: true });
    sl.addText(String(val), { x: x + 0.12, y: 1.62, w: colW - 0.24, h: 0.65, fontFace: FONT, fontSize: 12, color: NAVY, bold: true, valign: "top", fit: "shrink" });
  });

  let y = 2.65;
  const lists = [
    ["Key Competitors", ov.key_competitors], ["Key Acquisitions", ov.key_acquisitions],
  ].filter(([, arr]) => arr && arr.length);
  const twoCol = [["Brands", ov.brands], ["Services", ov.services]].filter(([, v]) => v);
  const twoColH = lists.length ? 1.55 : (PH - y - 0.55);
  const halfW = (PW - 1.0 - 0.3) / 2;
  twoCol.forEach(([label, val], i) => {
    const x = 0.5 + i * (halfW + 0.3);
    card(sl, x, y, halfW, twoColH);
    sl.addText(label, { x: x + 0.15, y: y + 0.1, w: halfW - 0.3, h: 0.3, fontFace: FONT, fontSize: 12, bold: true, color: ACCENT });
    sl.addText(val, { x: x + 0.15, y: y + 0.42, w: halfW - 0.3, h: twoColH - 0.55, fontFace: FONT, fontSize: 10, color: NAVY_DK, valign: "top", fit: "shrink" });
  });
  y += twoColH + 0.25;

  if (lists.length) {
    const w2 = (PW - 1.0 - 0.3) / lists.length;
    lists.forEach(([label, arr], i) => {
      const x = 0.5 + i * (w2 + 0.3);
      const h = PH - y - 0.55;
      card(sl, x, y, w2, h);
      sl.addText(label, { x: x + 0.15, y: y + 0.1, w: w2 - 0.3, h: 0.3, fontFace: FONT, fontSize: 12, bold: true, color: ACCENT });
      sl.addText(arr.map(v => ({ text: v, options: { bullet: true, breakLine: true } })), {
        x: x + 0.15, y: y + 0.42, w: w2 - 0.3, h: h - 0.55, fontFace: FONT, fontSize: 10, color: NAVY_DK, valign: "top",
      });
    });
  }
  footer(sl);
}

// ---------- 3. Mission / Vision ----------
function addMissionVision() {
  const mv = d.mission_vision;
  const items = [["Mission", mv.mission], ["Vision", mv.vision], ["Values", mv.values]].filter(([, v]) => v);
  if (!items.length) return;
  const sl = newSlide();
  slideTitle(sl, "Mission / Vision");
  const h = (PH - 1.4 - 0.3 * (items.length - 1)) / items.length;
  items.forEach(([label, val], i) => {
    const y = 1.25 + i * (h + 0.3);
    card(sl, 0.5, y, PW - 1.0, h);
    sl.addText(label, { x: 0.75, y: y + 0.12, w: 2.2, h: h - 0.24, fontFace: FONT, fontSize: 15, bold: true, color: ACCENT, valign: "middle" });
    sl.addText(val, { x: 3.0, y: y + 0.12, w: PW - 3.5, h: h - 0.24, fontFace: FONT, fontSize: 11.5, color: NAVY_DK, valign: "middle", fit: "shrink" });
  });
  footer(sl);
}

// ---------- 4. Geo presence ----------
function addGeo() {
  const g = d.geo;
  const rows = [["Countries", g.countries], ["Regions", g.regions], ["Offices / Facilities", g.offices],
    ["Delivery Centers", g.delivery_centers], ["Revenue by Geography", g.geographic_revenue]].filter(([, v]) => v);
  if (!rows.length) return;
  const sl = newSlide();
  slideTitle(sl, "Geographic Presence");
  let y = 1.3;
  rows.forEach(([label, val]) => {
    const h = 0.55 + Math.ceil(val.length / 90) * 0.22;
    card(sl, 0.5, y, PW - 1.0, h);
    sl.addText(label, { x: 0.7, y: y + 0.08, w: 2.6, h: h - 0.16, fontFace: FONT, fontSize: 12, bold: true, color: ACCENT, valign: "top" });
    sl.addText(val, { x: 3.3, y: y + 0.08, w: PW - 4.0, h: h - 0.16, fontFace: FONT, fontSize: 10.5, color: NAVY_DK, valign: "top" });
    y += h + 0.18;
  });
  footer(sl);
}

// ---------- 5. Business segments ----------
function addSegments() {
  const segs = d.segments;
  if (!segs.length) return;
  const sl = newSlide();
  slideTitle(sl, "Business Segments");

  const n = segs.length;
  const colW = (PW - 1.0 - 0.25 * (n - 1)) / n;
  segs.forEach((seg, i) => {
    const x = 0.5 + i * (colW + 0.25);
    const h = 3.0;
    card(sl, x, 1.25, colW, h);
    sl.addText(seg.name || "Segment", { x: x + 0.12, y: 1.35, w: colW - 0.24, h: 0.45, fontFace: FONT, fontSize: 12.5, bold: true, color: NAVY, fit: "shrink" });
    let iy = 1.85;
    [["Revenue", seg.revenue], ["Growth", seg.growth], ["Op. Margin", seg.op_margin]].forEach(([lbl, val]) => {
      if (!val) return;
      sl.addText(`${lbl}: `, { x: x + 0.12, y: iy, w: colW - 0.24, h: 0.28, fontFace: FONT, fontSize: 9.5, color: SLATE, bold: true });
      sl.addText(val, { x: x + 0.12, y: iy + 0.18, w: colW - 0.24, h: 0.28, fontFace: FONT, fontSize: 11.5, color: NAVY_DK, bold: true });
      iy += 0.5;
    });
    if (seg.description) {
      sl.addText(seg.description, { x: x + 0.12, y: iy + 0.05, w: colW - 0.24, h: h - (iy + 0.05 - 1.25) - 0.1, fontFace: FONT, fontSize: 8.5, color: SLATE, valign: "top", fit: "shrink" });
    }
  });

  const withPct = segs.filter(s => s.revenue_pct != null);
  if (withPct.length) {
    sl.addText("Revenue Mix", { x: 0.5, y: 4.5, w: 4, h: 0.35, fontFace: FONT, fontSize: 13, bold: true, color: NAVY });
    sl.addChart(pres.ChartType.pie, [{
      name: "Revenue %", labels: withPct.map(s => s.name), values: withPct.map(s => s.revenue_pct),
    }], {
      x: 0.5, y: 4.85, w: 6.0, h: 2.35, showTitle: false, showLegend: true, legendPos: "r",
      showValue: true, dataLabelFormatCode: '0"%"', dataLabelColor: WHITE,
      chartColors: [ACCENT, "6FA8C9", NAVY, "9DB4C9", SLATE],
    });
  }
  footer(sl);
}

// ---------- 6/7. Sustainability ----------
function addSustainabilityStrategy() {
  const s = d.sustainability;
  if (!s.strategy && !s.goals) return;
  const sl = newSlide();
  slideTitle(sl, "Sustainability Strategy and Goals");
  const items = [["Strategy", s.strategy], ["Goals", s.goals]].filter(([, v]) => v);
  const h = (PH - 1.4 - 0.3 * (items.length - 1)) / Math.max(items.length, 1);
  items.forEach(([label, val], i) => {
    const y = 1.25 + i * (h + 0.3);
    card(sl, 0.5, y, PW - 1.0, h);
    sl.addText(label, { x: 0.75, y: y + 0.15, w: 2.5, h: 0.4, fontFace: FONT, fontSize: 14, bold: true, color: ACCENT });
    sl.addText(val, { x: 0.75, y: y + 0.55, w: PW - 1.5, h: h - 0.7, fontFace: FONT, fontSize: 10.5, color: NAVY_DK, valign: "top", fit: "shrink" });
  });
  footer(sl);
}
function addSustainabilityInitiatives() {
  const s = d.sustainability;
  if (!s.key_initiatives) return;
  const sl = newSlide();
  slideTitle(sl, "Sustainability – Key Initiatives");
  card(sl, 0.5, 1.25, PW - 1.0, PH - 1.85);
  sl.addText(s.key_initiatives, { x: 0.8, y: 1.4, w: PW - 1.6, h: PH - 2.1, fontFace: FONT, fontSize: 13, color: NAVY_DK, valign: "middle", fit: "shrink" });
  footer(sl);
}

// ---------- 8. Org structure ----------
function addOrgStructure() {
  if (!d.org_structure) return;
  const sl = newSlide();
  slideTitle(sl, "Organization Structure");
  card(sl, 0.5, 1.25, PW - 1.0, PH - 1.85);
  sl.addText(d.org_structure, { x: 0.8, y: 1.4, w: PW - 1.6, h: PH - 2.1, fontFace: FONT, fontSize: 13, color: NAVY_DK, valign: "middle", fit: "shrink" });
  footer(sl);
}

// ---------- Initials avatar (no photo download in this sandbox — see note) ----------
function initials(name) {
  return (name || "?").split(/\s+/).filter(Boolean).map(w => w[0]).slice(0, 2).join("").toUpperCase();
}
function addPeopleSlide(title, people) {
  if (!people.length) return;
  const sl = newSlide();
  slideTitle(sl, title);
  const n = Math.min(people.length, 3); // 3 per row, wrap to a second slide if more
  const rows = [];
  for (let i = 0; i < people.length; i += 3) rows.push(people.slice(i, i + 3));
  const rowH = (PH - 1.5) / rows.length;
  rows.forEach((row, ri) => {
    const colW = (PW - 1.0 - 0.3 * (row.length - 1)) / row.length;
    row.forEach((p, ci) => {
      const x = 0.5 + ci * (colW + 0.3);
      const y = 1.25 + ri * rowH;
      const h = rowH - 0.25;
      card(sl, x, y, colW, h);
      if (p.photo_path && fs.existsSync(p.photo_path)) {
        // clip to a circle: PowerPoint image objects support rounding via the "rounding" shape option in pptxgenjs
        sl.addImage({ path: p.photo_path, x: x + 0.18, y: y + 0.18, w: 0.65, h: 0.65, rounding: true });
      } else {
        sl.addShape("ellipse", { x: x + 0.18, y: y + 0.18, w: 0.65, h: 0.65, fill: { color: ACCENT }, line: { type: "none" } });
        sl.addText(initials(p.name), { x: x + 0.18, y: y + 0.18, w: 0.65, h: 0.65, align: "center", valign: "middle", fontFace: FONT, fontSize: 16, bold: true, color: WHITE });
      }
      sl.addText(p.name || "", { x: x + 0.95, y: y + 0.15, w: colW - 1.1, h: 0.35, fontFace: FONT, fontSize: 11.5, bold: true, color: NAVY, fit: "shrink" });
      sl.addText(p.designation || "", { x: x + 0.95, y: y + 0.48, w: colW - 1.1, h: 0.45, fontFace: FONT, fontSize: 9, color: SLATE, valign: "top", fit: "shrink" });
      if (p.brief) {
        sl.addText(p.brief, { x: x + 0.18, y: y + 0.95, w: colW - 0.36, h: h - 1.15, fontFace: FONT, fontSize: 8.5, color: NAVY_DK, valign: "top", fit: "shrink" });
      }
      if (p.linkedin_url) {
        sl.addText("LinkedIn ↗", { x: x + 0.18, y: y + h - 0.32, w: colW - 0.36, h: 0.28, fontFace: FONT, fontSize: 8.5, color: ACCENT, hyperlink: { url: p.linkedin_url } });
      }
    });
  });
  footer(sl);
}

// ---------- 10. SWOT overview ----------
function addSwotOverview() {
  const sw = d.swot;
  const quads = [["Strengths", sw.strengths, GOOD], ["Weaknesses", sw.weaknesses, BAD],
    ["Opportunities", sw.opportunities, ACCENT], ["Threats", sw.threats, WARN]];
  if (!quads.some(([, arr]) => arr.length)) return;
  const sl = newSlide();
  slideTitle(sl, "SWOT Analysis");
  const colW = (PW - 1.0 - 0.3 * 3) / 4;
  quads.forEach(([label, arr, color], i) => {
    const x = 0.5 + i * (colW + 0.3);
    card(sl, x, 1.25, colW, PH - 1.85, { fill: LIGHT_BG });
    sl.addShape("rect", { x, y: 1.25, w: colW, h: 0.5, fill: { color }, line: { type: "none" } });
    sl.addText(label.toUpperCase(), { x, y: 1.25, w: colW, h: 0.5, align: "center", valign: "middle", fontFace: FONT, fontSize: 12, bold: true, color: WHITE });
    const pts = arr.slice(0, 5).map(r => ({ text: r.point, options: { bullet: true, breakLine: true, fontSize: 9.5 } }));
    if (pts.length) {
      sl.addText(pts, { x: x + 0.15, y: 1.9, w: colW - 0.3, h: PH - 2.5, fontFace: FONT, color: NAVY_DK, valign: "top" });
    }
  });
  footer(sl);
}

// ---------- 11-14. SWOT detail ----------
function addSwotDetail(title, arr, color) {
  if (!arr.length) return;
  const sl = newSlide();
  slideTitle(sl, `SWOT – ${title}`);
  let y = 1.25;
  const rowH = Math.min(1.35, (PH - 1.85) / arr.length);
  arr.slice(0, 5).forEach(item => {
    card(sl, 0.5, y, PW - 1.0, rowH - 0.15);
    sl.addShape("ellipse", { x: 0.7, y: y + 0.16, w: 0.14, h: 0.14, fill: { color }, line: { type: "none" } });
    sl.addText(item.point, { x: 0.98, y: y + 0.08, w: PW - 1.73, h: 0.35, fontFace: FONT, fontSize: 12, bold: true, color: NAVY, fit: "shrink" });
    const body = item.detail || item.evidence;
    if (body) {
      sl.addText(body, { x: 0.98, y: y + 0.42, w: PW - 1.73, h: rowH - 0.55, fontFace: FONT, fontSize: 9.5, color: SLATE, valign: "top", fit: "shrink" });
    }
    y += rowH;
  });
  footer(sl);
}

// ---------- 15. Financials annual ----------
function addFinancialsAnnual() {
  const f = d.financials_annual;
  const hasCurrent = [f.revenue, f.operating_income, f.net_income].some(m => m.current != null);
  if (!hasCurrent) return;
  const sl = newSlide();
  slideTitle(sl, "Financials – Annual");

  const yLabels = ["2 yrs prior", "Prior year", f.financial_year || "Current year"];
  const series = [
    { name: "Revenue ($)", labels: yLabels, values: [f.revenue.two_prior, f.revenue.previous, f.revenue.current] },
    { name: "Operating Income ($)", labels: yLabels, values: [f.operating_income.two_prior, f.operating_income.previous, f.operating_income.current] },
    { name: "Net Income ($)", labels: yLabels, values: [f.net_income.two_prior, f.net_income.previous, f.net_income.current] },
  ].filter(s => s.values.some(v => v != null)).map(s => ({ ...s, values: s.values.map(v => v == null ? 0 : v / 1e9) }));

  if (series.length) {
    sl.addChart(pres.ChartType.bar, series, {
      x: 0.5, y: 1.25, w: 8.0, h: 5.6, barDir: "col", barGrouping: "clustered",
      showTitle: true, title: "Revenue / Operating Income / Net Income ($B)", titleFontSize: 12,
      showLegend: true, legendPos: "b", showValue: false,
      chartColors: [NAVY, ACCENT, "9DB4C9"],
      catAxisLabelColor: SLATE, valAxisLabelColor: SLATE,
      valGridLine: { color: "E3E7EE", size: 1 }, catGridLine: { style: "none" },
    });
  }

  const hi = f.highlights;
  const rows = [["EBITDA", hi.ebitda], ["Gross Profit", hi.gross_profit], ["EPS", hi.eps],
    ["Revenue Growth", hi.revenue_growth], ["Operating Margin", hi.operating_margin],
    ["Net Margin", hi.net_margin], ["Free Cash Flow", hi.free_cash_flow], ["ROE", hi.roe]]
    .filter(([, v]) => v != null && v !== "");
  if (rows.length) {
    sl.addText("Highlights", { x: 8.85, y: 1.25, w: 3.9, h: 0.35, fontFace: FONT, fontSize: 13, bold: true, color: NAVY });
    let y = 1.65;
    rows.forEach(([label, val]) => {
      sl.addText(label, { x: 8.85, y, w: 2.3, h: 0.35, fontFace: FONT, fontSize: 10, color: SLATE });
      sl.addText(String(val), { x: 11.1, y, w: 1.6, h: 0.35, fontFace: FONT, fontSize: 10, bold: true, color: NAVY_DK, align: "right" });
      y += 0.4;
    });
  }
  footer(sl);
}

// ---------- 16. Financials quarterly ----------
function addFinancialsQuarterly() {
  const q = d.financials_quarterly;
  const rows = [["Quarter", q.quarter], ["Revenue", q.revenue], ["Operating Income", q.operating_income],
    ["Net Income", q.net_income], ["EBITDA", q.ebitda], ["EPS", q.eps], ["Cash Flow", q.cash_flow],
    ["Revenue Growth (YoY)", q.revenue_growth], ["Net Income Growth (YoY)", q.net_income_growth]]
    .filter(([, v]) => v != null && v !== "");
  if (!rows.length) return;
  const sl = newSlide();
  slideTitle(sl, "Financials – Current Quarter");
  const n = rows.length, perCol = Math.ceil(n / 2);
  [rows.slice(0, perCol), rows.slice(perCol)].forEach((col, ci) => {
    const x = 0.5 + ci * ((PW - 1.0) / 2 + 0.1);
    let y = 1.35;
    col.forEach(([label, val]) => {
      card(sl, x, y, (PW - 1.2) / 2, 0.85);
      sl.addText(label.toUpperCase(), { x: x + 0.15, y: y + 0.1, w: (PW - 1.2) / 2 - 0.3, h: 0.3, fontFace: FONT, fontSize: 9, bold: true, color: SLATE });
      sl.addText(String(val), { x: x + 0.15, y: y + 0.38, w: (PW - 1.2) / 2 - 0.3, h: 0.4, fontFace: FONT, fontSize: 14, bold: true, color: NAVY });
      y += 1.0;
    });
  });
  footer(sl);
}

// ---------- Generic table-of-cards slide (acquisitions / awards / challenges) ----------
function addCardListSlide(title, items, fields) {
  // fields: [{key, label, primary?}]
  if (!items.length) return;
  const sl = newSlide();
  slideTitle(sl, title);
  const cols = items.length <= 2 ? 1 : 2;
  const rows = Math.ceil(items.length / cols);
  const colW = (PW - 1.0 - 0.25 * (cols - 1)) / cols;
  const rowH = Math.min(1.6, (PH - 1.85 - 0.2 * (rows - 1)) / rows);
  items.slice(0, cols * Math.min(rows, 6)).forEach((item, i) => {
    const c = i % cols, r = Math.floor(i / cols);
    const x = 0.5 + c * (colW + 0.25), y = 1.25 + r * (rowH + 0.2);
    card(sl, x, y, colW, rowH);
    let tx = x + 0.15, ty = y + 0.1;
    const primary = fields.find(f => f.primary);
    if (primary && item[primary.key]) {
      sl.addText(item[primary.key], { x: tx, y: ty, w: colW - 0.3, h: 0.32, fontFace: FONT, fontSize: 12, bold: true, color: NAVY, fit: "shrink" });
      ty += 0.36;
    }
    const rest = fields.filter(f => !f.primary && item[f.key]);
    const bodyText = rest.map(f => `${f.label}: ${item[f.key]}`).join("  |  ");
    if (bodyText) {
      sl.addText(bodyText, { x: tx, y: ty, w: colW - 0.3, h: rowH - (ty - y) - 0.1, fontFace: FONT, fontSize: 9, color: SLATE, valign: "top", fit: "shrink" });
    }
  });
  footer(sl);
}

// ---------- 18/23. Table slides (competitors / deals) ----------
function addTableSlide(title, headers, rows, colWs) {
  if (!rows.length) return;
  const sl = newSlide();
  slideTitle(sl, title);
  const tableRows = [
    headers.map(h => ({ text: h, options: { bold: true, color: WHITE, fill: { color: NAVY }, fontSize: 10.5 } })),
    ...rows.map(r => r.map(c => ({ text: c || "—", options: { fontSize: 9.5, color: NAVY_DK } }))),
  ];
  sl.addTable(tableRows, {
    x: 0.5, y: 1.25, w: PW - 1.0, colW: colWs, fontFace: FONT,
    border: { type: "solid", color: "E3E7EE", pt: 0.5 },
    autoPage: false, valign: "middle", margin: [4, 6, 4, 6],
  });
  footer(sl);
}

// ---------- 21. News bulletin ----------
function addNews() {
  const groups = d.news;
  const cats = Object.entries(groups).filter(([, arr]) => arr.length);
  if (!cats.length) return;
  const sl = newSlide();
  slideTitle(sl, "Latest News");
  const colW = (PW - 1.0 - 0.25 * (cats.length - 1)) / cats.length;
  cats.forEach(([cat, items], ci) => {
    const x = 0.5 + ci * (colW + 0.25);
    sl.addShape("roundRect", { x, y: 1.2, w: colW, h: 0.45, rectRadius: 0.06, fill: { color: NAVY }, line: { type: "none" } });
    sl.addText(cat, { x, y: 1.2, w: colW, h: 0.45, align: "center", valign: "middle", fontFace: FONT, fontSize: 11, bold: true, color: WHITE });
    let y = 1.8;
    const itemH = (PH - 2.35) / Math.max(items.length, 1);
    items.forEach(n => {
      const h = Math.min(itemH, 1.5);
      card(sl, x, y, colW, h - 0.12);
      if (n.date) sl.addText(n.date.slice(0, 10), { x: x + 0.12, y: y + 0.06, w: colW - 0.24, h: 0.22, fontFace: FONT, fontSize: 8, color: SLATE });
      sl.addText(n.title || "", { x: x + 0.12, y: y + 0.26, w: colW - 0.24, h: 0.4, fontFace: FONT, fontSize: 9.5, bold: true, color: NAVY, valign: "top", fit: "shrink" });
      if (n.summary) sl.addText(n.summary, { x: x + 0.12, y: y + 0.62, w: colW - 0.24, h: h - 0.85, fontFace: FONT, fontSize: 8, color: SLATE, valign: "top", fit: "shrink" });
      if (n.url) sl.addText("Read more ↗", { x: x + 0.12, y: y + h - 0.28, w: colW - 0.24, h: 0.2, fontFace: FONT, fontSize: 7.5, color: ACCENT, hyperlink: { url: n.url } });
      y += h;
    });
  });
  footer(sl);
}

// ---------- 22. IT spending ----------
function addITSpending() {
  if (!d.it_spending) return;
  const sl = newSlide();
  slideTitle(sl, "IT Spending");
  card(sl, 0.5, 1.25, PW - 1.0, PH - 1.85);
  sl.addText(d.it_spending, { x: 0.8, y: 1.4, w: PW - 1.6, h: PH - 2.1, fontFace: FONT, fontSize: 13, color: NAVY_DK, valign: "middle", fit: "shrink" });
  footer(sl);
}

// ---------- 24. Tech initiatives ----------
function addTechInitiatives() {
  const items = d.tech_initiatives;
  if (!items.length) return;
  const sl = newSlide();
  slideTitle(sl, "Technology Initiatives");
  const cols = 2, rows = Math.ceil(items.length / cols);
  const colW = (PW - 1.0 - 0.25) / cols;
  const rowH = Math.min(1.5, (PH - 1.85 - 0.2 * (rows - 1)) / rows);
  items.slice(0, cols * Math.min(rows, 6)).forEach((item, i) => {
    const c = i % cols, r = Math.floor(i / cols);
    const x = 0.5 + c * (colW + 0.25), y = 1.25 + r * (rowH + 0.2);
    card(sl, x, y, colW, rowH);
    sl.addText(item.date || "", { x: x + 0.15, y: y + 0.08, w: colW - 0.3, h: 0.25, fontFace: FONT, fontSize: 8.5, color: ACCENT, bold: true });
    sl.addText(item.title || "", { x: x + 0.15, y: y + 0.32, w: colW - 0.3, h: 0.35, fontFace: FONT, fontSize: 11.5, bold: true, color: NAVY, fit: "shrink" });
    if (item.details) sl.addText(item.details, { x: x + 0.15, y: y + 0.68, w: colW - 0.3, h: rowH - 0.8, fontFace: FONT, fontSize: 9, color: SLATE, valign: "top", fit: "shrink" });
  });
  footer(sl);
}

// ---------- 25. Technologies in use ----------
function addTechnologiesInUse() {
  const items = d.technologies_in_use;
  if (!items.length) return;
  const perSlide = 12;
  for (let p = 0; p < items.length; p += perSlide) {
    const chunk = items.slice(p, p + perSlide);
    const sl = newSlide();
    slideTitle(sl, items.length > perSlide ? `Technologies in Use (${p / perSlide + 1})` : "Technologies in Use");
    const cols = 3, rows = Math.ceil(chunk.length / cols);
    const colW = (PW - 1.0 - 0.2 * (cols - 1)) / cols;
    const rowH = Math.min(1.1, (PH - 1.85 - 0.15 * (rows - 1)) / rows);
    chunk.forEach((t, i) => {
      const c = i % cols, r = Math.floor(i / cols);
      const x = 0.5 + c * (colW + 0.2), y = 1.25 + r * (rowH + 0.15);
      card(sl, x, y, colW, rowH);
      sl.addText(t.technology || "", { x: x + 0.12, y: y + 0.08, w: colW - 0.24, h: 0.3, fontFace: FONT, fontSize: 11, bold: true, color: NAVY, fit: "shrink" });
      if (t.category) sl.addText(t.category, { x: x + 0.12, y: y + 0.36, w: colW - 0.24, h: 0.22, fontFace: FONT, fontSize: 8, color: ACCENT });
      if (t.brief) sl.addText(t.brief, { x: x + 0.12, y: y + 0.58, w: colW - 0.24, h: rowH - 0.68, fontFace: FONT, fontSize: 8, color: SLATE, valign: "top", fit: "shrink" });
    });
    footer(sl);
  }
}

// ---------- 26. Industry indicators ----------
function addIndustryIndicators() {
  const ind = d.industry_indicators;
  const rows = [["Demand Trend", ind.demand_trend], ["Technology Adoption", ind.technology_adoption],
    ["Spending Trend", ind.spending_trend], ["Regulatory Environment", ind.regulatory_environment],
    ["Competitive Intensity", ind.competitive_intensity], ["Outlook", ind.outlook]].filter(([, v]) => v);
  if (!ind.growth_rating && !rows.length) return;
  const sl = newSlide();
  slideTitle(sl, "Industry Indicators");

  if (ind.growth_rating) {
    const levels = ["Low", "Medium", "High"];
    const active = levels.findIndex(l => ind.growth_rating.toLowerCase().includes(l.toLowerCase()));
    sl.addText("Growth Rating", { x: 0.5, y: 1.3, w: 3, h: 0.35, fontFace: FONT, fontSize: 12, bold: true, color: NAVY });
    levels.forEach((lvl, i) => {
      const x = 0.5 + i * 2.0;
      const isActive = i === active || (active === -1 && lvl.toLowerCase() === "medium" && ind.growth_rating);
      sl.addShape("roundRect", { x, y: 1.75, w: 1.8, h: 0.6, rectRadius: 0.08, fill: { color: isActive ? ACCENT : LIGHT_BG }, line: { color: "E3E7EE", width: 1 } });
      sl.addText(lvl, { x, y: 1.75, w: 1.8, h: 0.6, align: "center", valign: "middle", fontFace: FONT, fontSize: 11, bold: true, color: isActive ? WHITE : SLATE });
    });
  }

  let y = 2.75;
  rows.forEach(([label, val]) => {
    card(sl, 0.5, y, PW - 1.0, 0.55);
    sl.addText(label, { x: 0.7, y, w: 3.5, h: 0.55, valign: "middle", fontFace: FONT, fontSize: 11, bold: true, color: SLATE });
    sl.addText(val, { x: 4.3, y, w: PW - 5.0, h: 0.55, valign: "middle", fontFace: FONT, fontSize: 11, color: NAVY_DK, fit: "shrink" });
    y += 0.65;
  });
  footer(sl);
}

// ---------- 27. Industry forecast + competitive landscape ----------
function addIndustryForecast() {
  const f = d.industry_forecast;
  const items = [["Industry", f.industry], ["Competitive Landscape", f.competitive_landscape],
    ["Forecast", f.forecast], ["Growth Drivers", f.growth_drivers]].filter(([, v]) => v);
  if (!items.length) return;
  const sl = newSlide();
  slideTitle(sl, "Industry Forecast & Competitive Landscape");
  let y = 1.25;
  const totalLen = items.reduce((a, [, v]) => a + v.length, 0);
  items.forEach(([label, val]) => {
    const h = Math.max(0.9, ((PH - 1.85) * (val.length / totalLen)));
    card(sl, 0.5, y, PW - 1.0, h - 0.15);
    sl.addText(label, { x: 0.75, y: y + 0.1, w: PW - 1.5, h: 0.3, fontFace: FONT, fontSize: 12, bold: true, color: ACCENT });
    sl.addText(val, { x: 0.75, y: y + 0.42, w: PW - 1.5, h: h - 0.6, fontFace: FONT, fontSize: 9.5, color: NAVY_DK, valign: "top", fit: "shrink" });
    y += h;
  });
  footer(sl);
}

// ---------- 28. Thank you ----------
function addThankYou() {
  const sl = newSlide(NAVY_DK);
  sl.addText("Thank You", { x: 0.8, y: 3.0, w: PW - 1.6, h: 1.0, fontFace: FONT, fontSize: 40, bold: true, color: WHITE });
  sl.addText(`${d.company_name} — Company Intelligence`, { x: 0.8, y: 3.9, w: PW - 1.6, h: 0.5, fontFace: FONT, fontSize: 14, color: "AFC6E3" });
}

// ---------------- Build ----------------
addTitleSlide();                                                    // 1
addOverview();                                                       // 2
addMissionVision();                                                  // 3
addGeo();                                                             // 4
addSegments();                                                        // 5
addSustainabilityStrategy();                                          // 6
addSustainabilityInitiatives();                                       // 7
addOrgStructure();                                                    // 8
addPeopleSlide("Leadership Team", d.leadership);                      // 9
addPeopleSlide("Organization Structure – Technology Team", d.technology_team); // optional
addSeparator("SWOT Analysis");
addSwotOverview();                                                    // 10
addSwotDetail("Strengths", d.swot.strengths, GOOD);                   // 11
addSwotDetail("Weaknesses", d.swot.weaknesses, BAD);                  // 12
addSwotDetail("Opportunities", d.swot.opportunities, ACCENT);         // 13
addSwotDetail("Threats", d.swot.threats, WARN);                       // 14
addSeparator("Financials");
addFinancialsAnnual();                                                // 15
addFinancialsQuarterly();                                             // 16
addCardListSlide("Key Acquisitions", d.acquisitions,
  [{ key: "company_name", label: "Company", primary: true }, { key: "year", label: "Year" }, { key: "value", label: "Value" }, { key: "brief", label: "Brief" }]); // 17
addTableSlide("Key Competitors", ["Company", "Revenue", "Employees", "Market Cap", "ICT Budget"],
  d.competitors.map(c => [c.name, c.revenue, c.employees, c.market_cap, c.ict_budget]),
  [3.2, 2.4, 2.4, 2.4, 2.13]);                                        // 18
addCardListSlide("Awards and Accolades", d.awards,
  [{ key: "award", label: "Award", primary: true }, { key: "date", label: "Date" }, { key: "brief", label: "Brief" }]); // 19
addCardListSlide("Business Challenges", d.challenges,
  [{ key: "challenge", label: "Challenge", primary: true }, { key: "impact", label: "Impact" }, { key: "brief", label: "Brief" }]); // 20
addSeparator("Market & Technology");
addNews();                                                            // 21
addITSpending();                                                      // 22
addTableSlide("Deals", ["Vendor", "Start Date", "End Date", "Contract Details"],
  d.deals.map(dl => [dl.vendor, dl.start_date, dl.end_date, dl.contract_details]),
  [2.8, 1.6, 1.6, 6.33]);                                             // 23
addTechInitiatives();                                                 // 24
addTechnologiesInUse();                                               // 25
addSeparator("Industry Outlook");
addIndustryIndicators();                                              // 26
addIndustryForecast();                                                // 27
addThankYou();                                                        // 28

pres.writeFile({ fileName: OUT }).then(() => console.log("wrote " + OUT));
