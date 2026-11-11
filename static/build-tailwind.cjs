/** Compile Tailwind v4 from a preinstalled or local tailwindcss npm dependency.
 *  Run with: node static/build-tailwind.cjs
 *  All source files are local. The generated file is committed to this distribution.
 */
const fs = require('fs');
const path = require('path');
const {compile} = require('tailwindcss');
const root = __dirname;
const input = fs.readFileSync(path.join(root, 'index.html'),'utf8') + '\n' + fs.readFileSync(path.join(root,'app.js'),'utf8');
// Collect string tokens including utilities in dynamically selected classes.
const candidates = new Set((input.match(/[a-zA-Z0-9_!:\[\].\/%-]+/g) || []).filter(x => x.length<110));
(async () => {
  const source = require.resolve('tailwindcss/index.css');
  const c = await compile(fs.readFileSync(source,'utf8'),{
    base:path.dirname(source),
    loadStylesheet:async(id,base)=>{
      const file=require.resolve(id,{paths:[base]});
      return {content:fs.readFileSync(file,'utf8'), base:path.dirname(file)};
    }
  });
  const css=c.build([...candidates]);
  const target=path.join(root,'tailwind.css');
  fs.writeFileSync(target,css);
  console.log(`Tailwind locally compiled: ${path.basename(target)} ${css.length} bytes, ${candidates.size} candidates`);
})().catch(err=>{console.error(err);process.exit(1)});
