"""Build a shareable, privacy-reduced HTML report with inline SVG charts."""
from __future__ import annotations

import argparse
import html
import json
from pathlib import Path
from statistics import median


def pct(value: object) -> str:
    return "Not scored" if value is None else f"{float(value) * 100:.1f}%"


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("scored_summary", type=Path)
    args = parser.parse_args()
    scored_path = args.scored_summary.resolve()
    data = json.loads(scored_path.read_text(encoding="utf-8"))
    run_data = json.loads((scored_path.parent / "run_summary.json").read_text(encoding="utf-8"))
    run_mode = str(run_data.get("mode", "unknown"))
    configuration = data.get("model_configuration") or {}
    model_key = "vision_model" if run_mode == "vision" else "text_model"
    requested_model = html.escape(str(configuration.get(model_key, "local model")))
    output_budget = configuration.get("output_token_budget")
    model_details = f"{requested_model}, {html.escape(run_mode)} mode"
    if output_budget:
        model_details += f", output cap {html.escape(str(output_budget))} tokens"
    if run_mode == "text":
        model_details += "; vision model was installed but not used in this run"
    docs = data["documents"]
    count = len(docs)
    success = sum(item["model_status"] == "success" for item in docs)
    processed = sum(item["run_status"] == "processed" for item in docs)
    refs = [item for item in docs if item["key_facts_expected"]]
    rows = [item for item in docs if item["expected_rows"]]
    finite_latencies = [item["runtime_total_ms"] / 1000 for item in docs if item.get("runtime_total_ms")]
    categories = sorted({item["category"] for item in docs})

    def bars(items: list[tuple[str, float | None]], width: int = 760, row_height: int = 38) -> str:
        height = max(60, len(items) * row_height + 26)
        markup = [f'<svg viewBox="0 0 {width} {height}" role="img" aria-label="Horizontal score chart">']
        for index, (label, value) in enumerate(items):
            y = 18 + index * row_height
            markup.append(f'<text x="0" y="{y+15}" class="chart-label">{html.escape(label)}</text>')
            markup.append(f'<rect x="220" y="{y}" width="480" height="20" rx="6" fill="#e9eff1"/>')
            if value is not None:
                bar_width = 480 * max(0, min(1, value))
                markup.append(f'<rect x="220" y="{y}" width="{bar_width:.1f}" height="20" rx="6" fill="#168879"/>')
                label_text = f"{value*100:.1f}%"
            else:
                label_text = "not scored"
            markup.append(f'<text x="716" y="{y+15}" class="chart-value">{html.escape(label_text)}</text>')
        markup.append("</svg>")
        return "".join(markup)

    category_bars = []
    for category in categories:
        entries = [item for item in docs if item["category"] == category]
        category_bars.append((f"{category} ({len(entries)})", sum(item["model_status"] == "success" for item in entries) / len(entries)))
    score_bars = [
        ("Key fact recall", data["key_fact_recall"]),
        ("Table row precision", data["table_row_precision"]),
        ("Table row recall", data["table_row_recall"]),
        ("Table row F1", data["table_row_f1"]),
    ]
    latency_height = max(60, len(docs) * 38 + 26)
    latency_chart = [f'<svg viewBox="0 0 760 {latency_height}" role="img" aria-label="End-to-end latency in seconds">']
    for index, item in enumerate(docs):
        y = 18 + index * 38
        latency = item.get("runtime_total_ms")
        latency_chart.append(f'<text x="0" y="{y+15}" class="chart-label">{html.escape(item["doc_id"])}</text>')
        latency_chart.append(f'<rect x="220" y="{y}" width="480" height="20" rx="6" fill="#e9eff1"/>')
        if latency is not None:
            seconds = latency / 1000
            bar_width = 480 * max(0, min(1, seconds / 300))
            latency_chart.append(f'<rect x="220" y="{y}" width="{bar_width:.1f}" height="20" rx="6" fill="#168879"/>')
            label_text = f"{seconds:.1f}s"
        else:
            label_text = "n/a"
        latency_chart.append(f'<text x="716" y="{y+15}" class="chart-value">{html.escape(label_text)}</text>')
    latency_chart.append("</svg>")
    table_rows = []
    for item in docs:
        table_rows.append(
            "<tr>" + "".join([
                f"<td>{html.escape(item['doc_id'])}</td>",
                f"<td>{html.escape(item['category'])}</td>",
                f"<td>{html.escape(item['run_status'])}</td>",
                f"<td>{html.escape(item['model_status'])}</td>",
                f"<td>{html.escape(item['reference_source'])}</td>",
                f"<td>{item['key_facts_correct']}/{item['key_facts_expected']}</td>" if item["key_facts_expected"] else "<td>Not scored</td>",
                f"<td>{item['row_true_positive']}/{item['expected_rows']}</td>" if item["expected_rows"] else "<td>Not scored</td>",
                f"<td>{item['runtime_total_ms']/1000:.1f}s</td>" if item.get("runtime_total_ms") else "<td>—</td>",
                f"<td>{'Yes' if item['review_required'] else 'No'}</td>",
            ]) + "</tr>"
        )
    report = f'''<!doctype html>
<html lang="en"><head><meta charset="utf-8"><meta name="viewport" content="width=device-width,initial-scale=1">
<title>Open Document Intelligence - Evaluation Report</title>
<style>
:root{{color-scheme:light;--ink:#18333b;--muted:#637b82;--line:#dce7e8;--accent:#168879;--paper:#f4f8f8}}
*{{box-sizing:border-box}}body{{margin:0;background:var(--paper);color:var(--ink);font:15px/1.55 'Segoe UI',Arial,sans-serif}}
main{{max-width:1100px;margin:auto;padding:38px 28px 64px}}header{{padding:34px;background:#103b40;color:#fff;border-radius:18px}}
header p{{margin:7px 0;color:#c8e2df}}h1{{font-size:34px;line-height:1.15;margin:0}}h2{{margin:30px 0 12px;font-size:21px}}
.grid{{display:grid;grid-template-columns:repeat(4,1fr);gap:12px;margin-top:16px}}.card,.panel{{background:#fff;border:1px solid var(--line);border-radius:13px;padding:18px}}
.metric{{font-size:29px;font-weight:800;color:#126b62}}.label,.muted{{font-size:12px;color:var(--muted)}}.panel{{margin-top:14px;overflow:hidden}}
svg{{width:100%;height:auto;display:block;overflow:visible}}.chart-label,.chart-value{{font:12px 'Segoe UI',Arial,sans-serif;fill:#39565e}}.chart-value{{font-weight:700}}
.scroll{{overflow:auto}}table{{border-collapse:collapse;width:100%;font-size:12px}}th,td{{text-align:left;padding:9px;border-bottom:1px solid #e8eeee;white-space:nowrap}}th{{color:#60777e;background:#f7fafa}}
.callout{{background:#fff8e8;border:1px solid #efdaa8;border-radius:12px;padding:15px;margin-top:14px}}.callout strong{{color:#815d19}}
.footer{{margin-top:24px;color:var(--muted);font-size:12px}}@media(max-width:760px){{.grid{{grid-template-columns:repeat(2,1fr)}}main{{padding:20px 14px}}header{{padding:23px}}}}
@media print{{body{{background:#fff}}main{{max-width:none;padding:12mm}}.panel,.card,header{{break-inside:avoid;box-shadow:none}}}}
</style></head><body><main>
<header><h1>Open Document Intelligence</h1><p>Sample Document Evaluation</p><p>Local OCR + Ollama {model_details} · 31 October 2026</p></header>
<section class="grid">
<div class="card"><div class="metric">{processed}/{count}</div><div class="label">documents processed</div></div>
<div class="card"><div class="metric">{success}/{count}</div><div class="label">model extraction succeeded</div></div>
<div class="card"><div class="metric">{pct(data['key_fact_recall'])}</div><div class="label">key fact recall on {len(refs)} reference-scored documents</div></div>
<div class="card"><div class="metric">{pct(data['table_row_f1'])}</div><div class="label">table row F1 on {len(rows)} row-referenced documents</div></div>
<div class="card"><div class="metric">{pct(data.get('review_required_rate'))}</div><div class="label">documents routed to human review</div></div>
</section>
<div class="callout"><strong>How to read these results</strong><br>This report measures one local model run on the supplied sample files. Reference facts are partial checklists, not exhaustive annotations; key fact recall is not all-field accuracy. Only documents with known row references contribute to row scores. Personal values and raw document text are omitted from this shareable report. Do not interpret results as a guarantee on unseen documents.</div>
<h2>Reference-based scores</h2><section class="panel">{bars(score_bars)}</section>
<h2>Model success by document type</h2><section class="panel">{bars(category_bars)}</section>
<h2>Processing time by sample ID</h2><section class="panel"><p class="muted">Bar length is scaled to five minutes; values are labeled in seconds.</p>{''.join(latency_chart)}</section>
<h2>Per-document results</h2><section class="panel scroll"><table><thead><tr><th>Sample</th><th>Type</th><th>Pipeline</th><th>Model</th><th>Reference</th><th>Key facts</th><th>Rows recovered</th><th>Time</th><th>Review</th></tr></thead><tbody>{''.join(table_rows)}</tbody></table></section>
<h2>Metrics and run settings</h2><section class="panel"><ul><li>Key fact recall: {data['key_fact_correct']} of {data['key_fact_expected']} reference facts were found in structured output.</li><li>Table row precision: {pct(data['table_row_precision'])}; recall: {pct(data['table_row_recall'])}; F1: {pct(data['table_row_f1'])}.</li><li>Mean end-to-end latency: {data.get('mean_latency_seconds', 0):.1f}s; median: {data.get('median_latency_seconds', 0):.1f}s. Mean model inference time: {data.get('mean_inference_seconds', 0):.1f}s.</li><li>Median generation speed: {data.get('median_generation_tokens_per_second', 0):.1f} tokens/s. Runtime-reported peak VRAM use: {data.get('reported_max_vram_bytes', 0)} bytes.</li><li>Model: {model_details}.</li><li>All {data.get('ocr_documents_with_text',0)} files produced OCR/native text. {data.get('review_required_documents',0)} of {count} results were routed for human review.</li></ul><p class="muted">Reference set: structured-details PDF, two user-approved visual marksheet references, and direct visual checks for four sample receipts. Some other document types have partial key-fact references. Reference facts are not exhaustive ground truth.</p></section>
<p class="footer">Generated from local evaluation JSON. Full results remain in the local project and may contain personal data. Review and mask sensitive source documents before sharing them.</p>
</main></body></html>'''
    output = scored_path.parent / "evaluation_report.html"
    output.write_text(report, encoding="utf-8")
    print(f"Created shareable report: {output}")


if __name__ == "__main__":
    main()
