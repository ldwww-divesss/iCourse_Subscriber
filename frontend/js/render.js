/**
 * Markdown + KaTeX rendering pipeline.
 * Sets window.ICS.render global.
 *
 * Depends on CDN globals: marked, DOMPurify, renderMathInElement
 *
 * Pipeline: stash formulas → marked → restore formulas → DOMPurify → KaTeX
 * We stash formula delimiters before marked.parse() because characters like
 * * and _ inside $...$ LaTeX (e.g. D^*, P_n, \sum_{i=1}) would otherwise
 * be treated as markdown emphasis and break the formula structure.
 */

window.ICS = window.ICS || {};

var _FORMULA_PLACEHOLDER_PREFIX = "";
var _FORMULA_PLACEHOLDER_SUFFIX = "";

/** Replace $...$ and $$...$$ with placeholders so marked won't touch them. */
function _stashFormulas(mdText) {
  var formulas = [];
  function stash(replacement) {
    var key = _FORMULA_PLACEHOLDER_PREFIX + formulas.length + _FORMULA_PLACEHOLDER_SUFFIX;
    formulas.push(replacement);
    return key;
  }

  var text = mdText;

  // 1. Stash existing \(...\) and \[...\] (already-LaTeX, protect from double-processing)
  text = text.replace(/(\\\([\s\S]*?\\\))|(\\\[[\s\S]*?\\\])/g, function (m) {
    return stash(m);
  });

  // 2. Stash $$...$$ (must run before $ → to avoid consuming individual $ chars of $$)
  text = text.replace(/\$\$([\s\S]*?)\$\$/g, function (_, f) {
    return stash("\\[" + f + "\\]");
  });

  // 3. Stash $...$ (inline math — non-greedy to pair nearest closing $)
  text = text.replace(/\$([\s\S]+?)\$/g, function (_, f) {
    return stash("\\(" + f + "\\)");
  });

  return { text: text, formulas: formulas };
}

/** Restore stashed formulas in the HTML output after marked.parse(). */
function _restoreFormulas(html, formulas) {
  for (var i = 0; i < formulas.length; i++) {
    html = html.split(_FORMULA_PLACEHOLDER_PREFIX + i + _FORMULA_PLACEHOLDER_SUFFIX).join(formulas[i]);
  }
  return html;
}

function _renderMarkdown(mdText) {
  if (!mdText) return "";
  var stashed = _stashFormulas(mdText);
  var rawHtml = marked.parse(stashed.text, { breaks: true });
  var restored = _restoreFormulas(rawHtml, stashed.formulas);
  return DOMPurify.sanitize(restored);
}

function _activateKaTeX(element) {
  if (typeof renderMathInElement !== "function") return;
  renderMathInElement(element, {
    delimiters: [
      { left: "$$", right: "$$", display: true },
      { left: "\\[", right: "\\]", display: true },
      { left: "\\(", right: "\\)", display: false },
      // NOTE: $...$ intentionally omitted from KaTeX — converted to \(...\) in _stashFormulas
    ],
    throwOnError: false,
  });
}

function _plainSnippet(mdText, maxLen) {
  maxLen = maxLen || 100;
  if (!mdText) return "";
  var text = mdText
    .replace(/\$\$.+?\$\$/gs, "...")
    .replace(/\\\[.+?\\\]/gs, "...")
    .replace(/\$[^$]+?\$/g, "...")
    .replace(/\\\(.+?\\\)/g, "...")
    .replace(/#{1,6}\s+/g, "")
    .replace(/\*{1,3}(.+?)\*{1,3}/g, "$1")
    .replace(/`{1,3}[^`]*`{1,3}/g, "")
    .replace(/\[([^\]]+)\]\([^)]+\)/g, "$1")
    .replace(/[|:\-]+/g, " ")
    .replace(/\n+/g, " ")
    .replace(/\s+/g, " ")
    .trim();
  return text.length > maxLen ? text.slice(0, maxLen) + "..." : text;
}

window.ICS.render = {
  renderMarkdown: _renderMarkdown,
  activateKaTeX: _activateKaTeX,
  plainSnippet: _plainSnippet,
};
