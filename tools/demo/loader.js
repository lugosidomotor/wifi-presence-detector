/* Starts the inlined UI modules without a server: each one becomes a Blob URL, and the import specifiers that
   point at its dependencies are rewritten to those URLs (dependencies first). tools/build_demo.py fills MODULES. */
(() => {
"use strict";
const MODULES = /*MODULES*/;
const urls = {};
const pattern = /(\bfrom\s*|\bimport\s*\(\s*|\bimport\s*)(["'])([^"']+)\2/g;
for (const module of MODULES) {
  const code = module.code.replace(pattern, (match, head, quote, specifier) => {
    const target = MODULES.find(m => m.specifiers.includes(specifier));
    return target && urls[target.name] ? `${head}${quote}${urls[target.name]}${quote}` : match;
  });
  urls[module.name] = URL.createObjectURL(new Blob([code], {type: "text/javascript"}));
}
import(urls[MODULES[MODULES.length - 1].name]).catch(error => {
  console.error(error);
  document.getElementById("subtitle").textContent = "A demó nem indult el ebben a böngészőben.";
});
})();
