/* Load app.js for real and render both views against API-shaped payloads.
 *
 * Top-level function and var declarations in a vm script become properties of
 * the context, so the render functions are callable from here exactly as the
 * browser would call them -- shadowing, typos and all.
 */
'use strict';
var fs = require('fs'), path = require('path'), vm = require('vm');
var dom = require('./dom.js');

var root = path.join(__dirname, '..', '..');
var ctx = {
  document: dom.document,
  location: { hash: '#/' },
  localStorage: { getItem: function () { return null; }, setItem: function () {} },
  fetch: function () { return { then: function () { return this; },
                                catch: function () { return this; } }; },
  console: console, Date: Date, Math: Math, JSON: JSON,
  setTimeout: function () {}, clearTimeout: function () {},
  setInterval: function () {}, clearInterval: function () {},
  encodeURIComponent: encodeURIComponent, decodeURIComponent: decodeURIComponent,
  matchMedia: function () { return { matches: false, addEventListener: function () {} }; },
  innerWidth: 1600, addEventListener: function () {}
};
ctx.window = ctx;
vm.createContext(ctx);
vm.runInContext(fs.readFileSync(path.join(root, 'server/static/app.js'), 'utf8'), ctx,
                { filename: 'app.js' });

module.exports = { ctx: ctx, textOf: dom.textOf };
