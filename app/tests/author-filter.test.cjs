const assert = require('node:assert/strict');
const { readFileSync } = require('node:fs');
const { resolve } = require('node:path');
const vm = require('node:vm');
const test = require('node:test');
const ts = require('typescript');
const source = ts.transpileModule(readFileSync(resolve(__dirname, '../src/authorSelection.ts'), 'utf8'), {
  compilerOptions: {module: ts.ModuleKind.CommonJS, target: ts.ScriptTarget.ES2022}
}).outputText;
const mod = {exports: {}};
vm.runInThisContext(`(function(exports){${source}\n})`)(mod.exports);
const {authorKey, authorParams, matchingAuthors, demoAuthorOptions} = mod.exports;

test('account selection is case insensitive but isolated by platform and real handle', () => {
  const base = {platform:'x', handle:'OpenAI', author:'OpenAI'};
  assert.equal(authorKey(base), authorKey({...base, handle:'openai', author:'Changed display name'}));
  assert.notEqual(authorKey(base), authorKey({...base, platform:'facebook'}));
  assert.notEqual(authorKey(base), authorKey({...base, handle:'different'}));
  assert.equal(authorKey({...base, platform:'rss', handle:'', author:'研究员: 小明'}), 'rss:name:研究员: 小明');
});
test('picker keeps combined scope without narrowing to the selected author or first page', () => {
  const params = new URLSearchParams({author:'x:handle:openai', limit:'30', offset:'30', sort:'latest', topic:'技术', q:'推理', saved:'true'});
  const result = new URLSearchParams(authorParams(params));
  assert.deepEqual([...result.keys()], ['topic','q','saved']);
  assert.equal(result.get('q'), '推理');
  assert.equal(params.get('author'), 'x:handle:openai');
});
test('name and @handle lookup and per-account counts', () => {
  const data = demoAuthorOptions([
    {platform:'x', handle:'OpenAI', author:'OpenAI'},
    {platform:'x', handle:'openai', author:'OpenAI'},
    {platform:'rss', handle:'', author:'研究员: 小明'},
    {platform:'facebook', handle:'OpenAI', author:'OpenAI'}
  ]);
  assert.equal(data.length,3);
  assert.equal(data[0].count,2);
  assert.equal(matchingAuthors(data,'@OPENAI').length,2);
  assert.equal(matchingAuthors(data,'小明')[0].key,'rss:name:研究员: 小明');
  assert.equal(matchingAuthors(data,'nonexistent').length,0);
});
