(which) => {
  const doc = Bokeh.documents[0];
  const all = [...doc._all_models.values()];
  const q = (m) => String((m.constructor && m.constructor.__qualified__) || m.type || '');
  const find = (id) => (Bokeh.index.find_one_by_id ? Bokeh.index.find_one_by_id(id) : null);
  const out = [];
  for (const tool of all.filter(m => q(m).endsWith('FlagTool') && m.flag)) {
    const fig = all.find(m => m.toolbar && m.toolbar.tools && m.toolbar.tools.includes(tool));
    if (!fig) continue;
    const view = find(fig.id); if (!view) continue;
    const e = view.el.getBoundingClientRect(), b = view.frame.bbox;
    if (!(e.width > 50 && b.width > 50)) continue;
    out.push({panel: tool.panel, x: e.left + b.left, y: e.top + b.top, w: b.width, h: b.height,
              id: tool.id, fig: fig.id});
  }
  return out;
}
