// Lists every API call JLC's own web apps make, with its service prefix resolved, from their
// webpack bundles. Driven by catalog.py (`jlc-agent endpoints --refresh`).
//
//   node catalog.cjs <app>=<folder of bundle .js files> ...   > endpoints.json
//
// A call site reads like r.http.post("".concat(o.m,"/v1/newOrder/myOrderBatchList")): `o` is
// whatever the calling module named its import of a small "base" module whose exports m, n, e...
// are service prefixes ("/overseas-pcb-order", "/overseas-core-platform", ...). The letters differ
// between bundles, and the main site carries two base modules with clashing letters, so a letter
// is only resolved through the import it came from: each chunk is evaluated with a stub
// webpackJsonp that captures its module table without running any module.
'use strict';
const fs = require('fs');
const path = require('path');
const vm = require('vm');

// The chunk runs in a fresh context whose every object is made inside it, with code generation from
// strings off. The global is backed by a null-prototype object: an ordinary {} would carry this
// realm's Object, and `this.constructor.constructor('return process')()` would reach Node (the test
// suite tries exactly that). This is hardening, not a sandbox (Node's vm is not a security boundary);
// the files are the ones jlcpcb.com serves to every browser, and JLCONE runs them with more privilege.
function modulesOf(file) {
  const context = vm.createContext(Object.create(null), { codeGeneration: { strings: false, wasm: false } });
  vm.runInContext('var __pushed = []; var window = {webpackJsonp: {push: function (x) { __pushed.push(x); }}};' +
                  'var self = window; var webpackJsonp = window.webpackJsonp;', context);
  try {
    vm.runInContext(fs.readFileSync(file, 'utf8'), context, { timeout: 10000 });
  } catch (error) {
    return [];  // the webpack runtime and anything else that is not a chunk
  }
  const out = [];
  for (const [, table] of context.__pushed) {
    if (Array.isArray(table)) table.forEach((fn, id) => typeof fn === 'function' && out.push([String(id), fn]));
    else if (table) for (const id of Object.keys(table)) out.push([id, table[id]]);
  }
  return out;
}

function params(source) {
  const match = source.match(/^function\s*\w*\s*\(([^)]*)\)/) || source.match(/^\(?([^)=]*)\)?\s*=>/);
  return match ? match[1].split(',').map((p) => p.trim()) : [];
}

function scan(app, folder) {
  const modules = new Map();
  for (const name of fs.readdirSync(folder).filter((n) => n.endsWith('.js'))) {
    for (const [id, fn] of modulesOf(path.join(folder, name))) modules.set(id, fn.toString());
  }
  // base modules: declare the prefixes and export them under letters
  const bases = new Map();
  for (const [id, source] of modules) {
    if (!source.includes('"/overseas-pcb-order"')) continue;
    const [, exportsName, requireName] = params(source);
    if (!exportsName || !requireName) continue;
    const values = Object.fromEntries([...source.matchAll(/(\w+)="(\/(?:overseas|diy)[\w-]*)"/g)].map((m) => [m[1], m[2]]));
    const exported = new RegExp(`${requireName}\\.d\\(${exportsName},"(\\w+)",\\(function\\(\\)\\{return (\\w+)\\}\\)\\)`, 'g');
    const map = {};
    for (const m of source.matchAll(exported)) if (values[m[2]]) map[m[1]] = values[m[2]];
    if (Object.keys(map).length) bases.set(id, map);
  }
  const found = new Map();
  const exported = new Map();  // "<module id> <export name>" -> endpoint key, for finding the callers
  const importsOf = new Map();
  for (const [id, source] of modules) {
    const [, exportsName, requireName] = params(source);
    if (!requireName) continue;
    const imports = new Map();
    for (const m of source.matchAll(new RegExp(`(\\w+)=${requireName}\\((\\d+)\\)`, 'g'))) imports.set(m[1], m[2]);
    importsOf.set(id, imports);
    const exportsByVar = new Map();
    if (exportsName) {
      const exp = new RegExp(`${requireName}\\.d\\(${exportsName},"(\\w+)",\\(function\\(\\)\\{return (\\w+)\\}\\)\\)`, 'g');
      for (const m of source.matchAll(exp)) exportsByVar.set(m[2], m[1]);
    }
    const sites = /(?:(\w+)=\w+\.\w+\.)?(?:\.?(get|post|put|delete|patch)\(\s*)?"(\/api)?"\.concat\((\w+)\.(\w+),\s*"(\/[^"?]+)/g;
    for (const m of source.matchAll(sites)) {
      const [, variable, method, apiPrefix, alias, letter, rest] = m;
      const base = bases.get(imports.get(alias));
      if (!base || !base[letter]) continue;
      const full = base[letter] + rest;
      const key = `${(method || '').toUpperCase()} ${full}`;
      const entry = found.get(key) || { method: method ? method.toUpperCase() : null, path: full, apps: [],
                                        url_only: !!apiPrefix && !method, usage: [] };
      if (!entry.apps.includes(app)) entry.apps.push(app);
      found.set(key, entry);
      if (variable && exportsByVar.has(variable)) exported.set(`${id} ${exportsByVar.get(variable)}`, key);
    }
  }
  // callers: Object(x.Fc)(r,{shipHandle:!0}) where x imports the module exporting Fc; the text just
  // before the call usually builds the argument (r={expressNo:...,batchNum:...})
  for (const [id, source] of modules) {
    const imports = importsOf.get(id);
    if (!imports) continue;
    for (const m of source.matchAll(/Object\((\w+)\.(\w+)\)\(/g)) {
      const key = exported.get(`${imports.get(m[1])} ${m[2]}`);
      const entry = key && found.get(key);
      if (!entry || entry.usage.length >= 3) continue;
      const snippet = source.slice(Math.max(0, m.index - 260), m.index + 200).replace(/\s+/g, ' ');
      if (!entry.usage.includes(snippet)) entry.usage.push(snippet);
    }
  }
  return [...found.values()];
}

const merged = new Map();
for (const arg of process.argv.slice(2)) {
  const [app, folder] = arg.split('=');
  for (const entry of scan(app, folder)) {
    const key = `${entry.method} ${entry.path}`;
    const known = merged.get(key);
    if (known) {
      entry.apps.forEach((a) => known.apps.includes(a) || known.apps.push(a));
      entry.usage.forEach((u) => known.usage.length < 3 && !known.usage.includes(u) && known.usage.push(u));
    } else merged.set(key, entry);
  }
}
const list = [...merged.values()].sort((a, b) => a.path.localeCompare(b.path) || String(a.method).localeCompare(String(b.method)));
process.stdout.write(JSON.stringify(list, null, 1));
