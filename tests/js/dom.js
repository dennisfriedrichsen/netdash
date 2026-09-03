/* A DOM small enough to read and large enough to render netdash.
 *
 * The point is not fidelity. It is that `node --check` proves only that
 * app.js parses, and the bug that shipped -- a `var` inside renderDetail
 * shadowing a top-level function of the same name across the whole scope --
 * parses perfectly. The only thing that catches that class is calling the
 * render functions for real, so this provides just enough document for them
 * to run and throw where a browser would.
 */
'use strict';

function mkEl(tag, ns) {
  var e = {
    tagName: tag, namespaceURI: ns || null, children: [], attrs: {},
    style: { setProperty: function (k, v) { this[k] = v; } },
    classList: { _s: [], add: function () {
      [].push.apply(this._s, arguments); }, contains: function (c) {
      return this._s.indexOf(c) >= 0; } },
    appendChild: function (c) {
      if (c === null || c === undefined) {
        throw new TypeError('appendChild(' + c + ') on <' + tag + '>');
      }
      this.children.push(c); return c;
    },
    replaceChildren: function () { this.children = [].slice.call(arguments); },
    setAttribute: function (k, v) { this.attrs[k] = v; },
    removeAttribute: function (k) { delete this.attrs[k]; },
    getAttribute: function (k) { return k in this.attrs ? this.attrs[k] : null; },
    addEventListener: function () {},
    remove: function () {}
  };
  return e;
}

/* Every string that ends up on screen, so a test can assert on what a user
   would actually read rather than on the shape of the tree. */
function textOf(node) {
  if (node === null || node === undefined) return '';
  if (typeof node === 'string') return node;
  var out = node.textContent ? String(node.textContent) : '';
  if (node.title) out += ' ' + node.title;
  (node.children || []).forEach(function (c) { out += ' ' + textOf(c); });
  return out;
}

var document = {
  createElement: function (t) { return mkEl(t); },
  createElementNS: function (ns, t) { return mkEl(t, ns); },
  createTextNode: function (t) { var e = mkEl('#text'); e.textContent = t; return e; },
  createComment: function (t) { var e = mkEl('#comment'); e.textContent = t; return e; },
  createDocumentFragment: function () { return mkEl('#fragment'); },
  getElementById: function (id) { return mkEl('div#' + id); },
  documentElement: mkEl('html'),
  addEventListener: function () {}
};

module.exports = { document: document, mkEl: mkEl, textOf: textOf };
