#!/usr/bin/env node
/* SPDX-License-Identifier: GPL-3.0-only WITH GPL-3.0-linking-exception */
/* Headless harness for the FM1 browser simulator (build/web/simulator.wasm).
 *
 * Boots the same reactor module the web workbench loads, drives panel controls,
 * renders the LCD and writes it out as a PNG — so firmware-side UI and
 * sequencing changes can be checked without Windows, a device, or a browser.
 *
 * Usage:
 *   node tools/sim-headless.mjs --check
 *   node tools/sim-headless.mjs --frames 12 --png lcd.png --json
 *   node tools/sim-headless.mjs --button 12 --frames 30 --png playing.png
 *   node tools/sim-headless.mjs --control 0:12:1 --control 3:0:512
 */

import fs from 'node:fs';
import path from 'node:path';
import zlib from 'node:zlib';

const WIDTH = 240, HEIGHT = 240;
const BUTTONS = ['OCT_DOWN', 'OCT_UP', 'FX', 'SCL', 'ENV', 'LFO', 'EDIT', 'GLO',
  'HOME', 'SAVE', 'ARP', 'SEQ', 'PLAY', 'REC'];

function parseArgs(argv) {
  const opts = { wasm: 'build/web/simulator.wasm', frames: 8, png: null, json: false,
    check: false, buttons: [], controls: [], midi: null };
  for (let i = 0; i < argv.length; i++) {
    const arg = argv[i];
    const next = () => {
      if (i + 1 >= argv.length) { console.error(`Missing value for ${arg}`); process.exit(2); }
      return argv[++i];
    };
    if (arg === '--wasm') opts.wasm = next();
    else if (arg === '--frames') opts.frames = Number(next());
    else if (arg === '--png') opts.png = next();
    else if (arg === '--json') opts.json = true;
    else if (arg === '--check') opts.check = true;
    else if (arg === '--button') opts.buttons.push(Number(next()));
    else if (arg === '--midi') opts.midi = next();
    else if (arg === '--control') {
      const [kind, index, value] = next().split(':').map(Number);
      if ([kind, index, value].some(v => !Number.isFinite(v))) {
        console.error('--control expects kind:index:value'); process.exit(2);
      }
      opts.controls.push([kind, index, value]);
    } else if (arg === '--help' || arg === '-h') {
      console.log(fs.readFileSync(new URL(import.meta.url), 'utf8').split('*/')[0].replace('/*', ''));
      process.exit(0);
    } else { console.error(`Unknown argument: ${arg}`); process.exit(2); }
  }
  if (!Number.isInteger(opts.frames) || opts.frames < 1) { console.error('--frames must be >= 1'); process.exit(2); }
  return opts;
}

function boot(wasmBytes) {
  const instance = new WebAssembly.Instance(new WebAssembly.Module(wasmBytes), {});
  const c = instance.exports;
  c._initialize();
  c.sim_boot(1);                       // 1 = factory state, 0 = restore saved flash
  return c;
}

function render(c, frames) { for (let i = 0; i < frames; i++) c.sim_render(); }

function pixels(c) {
  return new Uint16Array(c.memory.buffer, c.sim_pixels(), WIDTH * HEIGHT);
}

function ascii(c, pointer, length) {
  return Buffer.from(new Uint8Array(c.memory.buffer, pointer, length)).toString('ascii').replace(/\0.*$/s, '').trim();
}

function stats(c) {
  const px = pixels(c);
  let lit = 0, checksum = 0xcbf29ce484222325n;
  for (let i = 0; i < px.length; i++) {
    const v = px[i];
    if (v) lit++;
    checksum = ((checksum ^ BigInt(v)) * 0x100000001b3n) & 0xffffffffffffffffn;
  }
  return { lit, total: px.length, checksum: checksum.toString(16).padStart(16, '0') };
}

function png(c, file) {
  const px = pixels(c);
  const stride = WIDTH * 3 + 1;
  const raw = Buffer.alloc(stride * HEIGHT);
  for (let y = 0; y < HEIGHT; y++) {
    raw[y * stride] = 0;
    for (let x = 0; x < WIDTH; x++) {
      const v = px[y * WIDTH + x];
      const r = (v >> 11) & 31, g = (v >> 5) & 63, b = v & 31;
      const at = y * stride + 1 + x * 3;
      raw[at] = (r << 3) | (r >> 2);
      raw[at + 1] = (g << 2) | (g >> 4);
      raw[at + 2] = (b << 3) | (b >> 2);
    }
  }
  const chunk = (type, data) => {
    const length = Buffer.alloc(4); length.writeUInt32BE(data.length);
    const body = Buffer.concat([Buffer.from(type), data]);
    let crc = ~0;
    for (const byte of body) { crc ^= byte; for (let k = 0; k < 8; k++) crc = crc & 1 ? 0xEDB88320 ^ (crc >>> 1) : crc >>> 1; }
    const sum = Buffer.alloc(4); sum.writeUInt32BE((~crc) >>> 0);
    return Buffer.concat([length, body, sum]);
  };
  const header = Buffer.alloc(13);
  header.writeUInt32BE(WIDTH, 0); header.writeUInt32BE(HEIGHT, 4); header[8] = 8; header[9] = 2;
  fs.mkdirSync(path.dirname(path.resolve(file)), { recursive: true });
  fs.writeFileSync(file, Buffer.concat([
    Buffer.from([137, 80, 78, 71, 13, 10, 26, 10]),
    chunk('IHDR', header), chunk('IDAT', zlib.deflateSync(raw)), chunk('IEND', Buffer.alloc(0))]));
}

function sendMidi(c, bytes) {
  const data = new Uint8Array(c.memory.buffer, c.sim_file(), bytes.length);
  data.set(bytes);
  return c.sim_midi(bytes.length);
}

function describe(c, extra) {
  const state = new Uint8Array(c.memory.buffer, c.sim_state(), 225);
  return Object.assign({
    project: ascii(c, c.sim_project_name(), 12),
    preset_position: c.sim_preset_position(),
    dirty: !!c.sim_project_dirty(),
    lcd_context: c.sim_lcd_context(),
    lcd_mask: c.sim_lcd_mask(),
    lcd_targets: c.sim_lcd_count(),
    step_first: c.sim_step_first(),
    block_frames: c.sim_block_frames(),
    frame_revision: c.sim_frame_revision(),
    edit_revision: c.sim_edit_revision(),
    project_revision: c.sim_project_revision(),
    state_checksum: [...state].reduce((h, v) => ((h ^ v) * 16777619) >>> 0, 2166136261).toString(16),
  }, extra);
}

function run(opts) {
  const bytes = fs.readFileSync(opts.wasm);
  const c = boot(bytes);
  for (const [kind, index, value] of opts.controls) c.sim_control(kind, index, value);
  for (const button of opts.buttons) {
    if (!Number.isInteger(button) || button < 0 || button > 13) {
      console.error(`--button expects 0..13 (${BUTTONS.join(', ')})`); process.exit(2);
    }
    c.sim_control(0, button, 1);
    render(c, opts.frames);
    c.sim_control(0, button, 0);
  }
  if (opts.midi) {
    const bytes = opts.midi.split(/[\s,]+/).filter(Boolean).map(h => parseInt(h, 16));
    render(c, opts.frames);
    sendMidi(c, bytes);
  }
  render(c, opts.frames);
  const picture = stats(c);
  if (opts.png) png(c, opts.png);
  const result = describe(c, { wasm: opts.wasm, frames: opts.frames, lit_pixels: picture.lit,
    total_pixels: picture.total, lcd_checksum: picture.checksum });
  if (opts.json || opts.check) console.log(JSON.stringify(result, null, 2));
  if (opts.check) {
    const again = describe(boot(bytes), {});
    const replay = stats(boot(bytes));
    const problems = [];
    if (picture.lit * 100 < picture.total) problems.push('LCD framebuffer is blank');
    if (!result.project) problems.push('project name is empty');
    if (result.frame_revision === 0) problems.push('no frame was rendered');
    if (again.state_checksum !== result.state_checksum) problems.push('boot state is not deterministic');
    if (replay.checksum !== picture.checksum) problems.push('LCD output is not deterministic');
    if (problems.length) { console.error('CHECK FAILED: ' + problems.join('; ')); return 1; }
    console.error(`CHECK PASSED: ${result.project || 'unnamed'}, ${result.lit_pixels} lit pixels, ` +
      `frame ${result.frame_revision}, lcd ${result.lcd_checksum}`);
  }
  return 0;
}

process.exit(run(parseArgs(process.argv.slice(2))));
