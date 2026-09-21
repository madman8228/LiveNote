import assert from 'node:assert/strict'
import fs from 'node:fs/promises'
import { pathToFileURL } from 'node:url'
import esbuild from 'esbuild'

const sourcePath = new URL('../../src/playback/PlaybackProgress.ts', import.meta.url)
const source = await fs.readFile(sourcePath, 'utf8')
const transformed = await esbuild.transform(source, { loader: 'ts', format: 'esm', target: 'es2022', sourcefile: sourcePath.pathname })
const moduleUrl = `data:text/javascript;base64,${Buffer.from(transformed.code).toString('base64')}`
const { resolvePlaybackDurationMs, playbackProgressPercent } = await import(moduleUrl)

const recordedDurationMs = 5_465_168

assert.equal(resolvePlaybackDurationMs(Number.NaN, recordedDurationMs), recordedDurationMs)
assert.equal(resolvePlaybackDurationMs(Number.POSITIVE_INFINITY, recordedDurationMs), recordedDurationMs)
assert.equal(resolvePlaybackDurationMs(12.5, recordedDurationMs), 12_500)
assert.ok(playbackProgressPercent(33, recordedDurationMs) > 0)
assert.ok(playbackProgressPercent(33, recordedDurationMs) < 1)
assert.equal(playbackProgressPercent(recordedDurationMs / 1000 / 2, recordedDurationMs), 50)
assert.equal(playbackProgressPercent(recordedDurationMs / 1000 * 2, recordedDurationMs), 100)

console.log('playback progress fallback regression: PASS')
