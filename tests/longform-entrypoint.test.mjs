import test from 'node:test';
import assert from 'node:assert/strict';
import fs from 'node:fs';
import { spawnSync } from 'node:child_process';
import { tmpdir } from 'node:os';
import { dirname, join, relative, resolve, sep } from 'node:path';
import { fileURLToPath } from 'node:url';

test('maintained npm entry creates v3 and rejects legacy receipts', { timeout: 30000 }, () => {
  const repo = fileURLToPath(new URL('../', import.meta.url));
  const pkg = JSON.parse(fs.readFileSync(join(repo, 'package.json'), 'utf8'));
  assert.equal(pkg.scripts['acquire:longform'],
    'python -B research/full-channel-2026-08-16/scripts/acquire_longform_sections_v3.py');
  const npmCli = process.env.npm_execpath ?? join(dirname(process.execPath), 'node_modules/npm/bin/npm-cli.js');
  assert(fs.existsSync(npmCli));
  const parent = resolve(tmpdir());
  const root = fs.mkdtempSync(join(parent, 'neuro-adoption-test-'));
  try {
    const env = Object.fromEntries(Object.entries(process.env).filter(([key]) =>
      /^(path|pathext|systemroot|systemdrive|comspec|windir)$/i.test(key)));
    Object.assign(env, { TEMP: root, TMP: root, NPM_CONFIG_OFFLINE: 'true',
      NPM_CONFIG_CACHE: join(root, 'cache'), NPM_CONFIG_USERCONFIG: join(root, 'absent-user.npmrc'),
      NPM_CONFIG_GLOBALCONFIG: join(root, 'absent-global.npmrc'), NPM_CONFIG_UPDATE_NOTIFIER: 'false' });
    const run = (...args) => spawnSync(process.execPath,
      [npmCli, 'run', 'acquire:longform', '--', ...args],
      { cwd: repo, env, encoding: 'utf8', timeout: 10000, windowsHide: true });
    const help = run('init', '--help');
    assert.ifError(help.error);
    assert.equal(help.status, 0, help.stderr);
    assert.match(help.stdout, /acquire_longform_sections_v3\.py init/);
    const url = join(root, 'url.txt'), identity = join(root, 'identity.json'), output = join(root, 'new');
    fs.writeFileSync(url, 'https://www.youtube.com/watch?v=exact123\n');
    fs.writeFileSync(identity, JSON.stringify({ video_id: 'exact123', title: 'synthetic', uploader: 'synthetic',
      duration_seconds: '2', availability: 'public', is_live: false }));
    const init = run('init', '--url-file', url, '--identity-file', identity, '--output-dir', output);
    assert.ifError(init.error);
    assert.equal(init.status, 0, init.stderr);
    const contract = JSON.parse(fs.readFileSync(join(output, 'contract.json'), 'utf8'));
    assert.equal(contract.schema, 'longform-source-segments-v3');
    assert.equal(contract.pipeline_provenance.policy_name, 'video-only-v2');
    assert.equal(contract.settings.source_format_selector, 'bv[height<=360]');
    assert.deepEqual(Object.keys(contract.pipeline_provenance.implementation_sha256).sort(),
      ['acquire_longform_core_v3.py', 'acquire_longform_policy_v3.py', 'acquire_longform_sections_v3.py']);
    const legacy = join(root, 'synthetic-legacy');
    fs.mkdirSync(join(legacy, 'segments'), { recursive: true });
    const before = JSON.stringify({ ...contract, schema: 'longform-source-segments-v1' });
    fs.writeFileSync(join(legacy, 'contract.json'), before);
    const rejected = run('verify', '--contract-dir', legacy);
    assert.ifError(rejected.error);
    assert.notEqual(rejected.status, 0);
    assert.match(rejected.stderr, /contract schema mismatch/);
    assert.equal(fs.readFileSync(join(legacy, 'contract.json'), 'utf8'), before);
    assert(!fs.existsSync(join(legacy, 'whole-item-complete.json')));
  } finally {
    const bounded = relative(parent, resolve(root));
    assert(bounded.startsWith('neuro-adoption-test-') && !bounded.includes(sep));
    fs.rmSync(root, { recursive: true, force: true });
  }
});
