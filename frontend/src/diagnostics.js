// Diagnostics from homeo/file/check (1-based lines and columns, end exclusive) as CodeMirror
// lint diagnostics (document offsets).

export function diagnosticsToCodeMirror(doc, diagnostics) {
  const pos = (line, col) => {
    const l = doc.line(Math.min(Math.max(line, 1), doc.lines));
    return Math.min(l.from + Math.max(col - 1, 0), l.to);
  };
  return diagnostics.map((d) => {
    const from = pos(d.line, d.col);
    let to = pos(d.end_line, d.end_col);
    if (to <= from) to = Math.min(from + 1, doc.length);
    return {
      from,
      to,
      severity: d.severity === "warning" ? "warning" : "error",
      message: d.hint ? `${d.message}\nhint: ${d.hint}` : d.message,
    };
  });
}
