import assert from 'node:assert/strict'
import fs from 'node:fs/promises'
import { pathToFileURL } from 'node:url'
import esbuild from 'esbuild'

const sourcePath = new URL('../../src/diagnostics/PlaybackDiagnostics.ts', import.meta.url)
const source = await fs.readFile(sourcePath, 'utf8')
const transformed = await esbuild.transform(source, { loader: 'ts', format: 'esm', target: 'es2022', sourcefile: sourcePath.pathname })
const moduleUrl = `data:text/javascript;base64,${Buffer.from(transformed.code).toString('base64')}`
const diagnosticsModule = await import(moduleUrl)
const { PlaybackDiagnostics, diagnosticReportBytes, PLAYBACK_DIAGNOSTICS_LIMITS } = diagnosticsModule

class FakeRanges {
  constructor(ranges) { this.ranges = ranges; this.length = ranges.length }
  start(index) { return this.ranges[index][0] }
  end(index) { return this.ranges[index][1] }
}

class FakeAudio extends EventTarget {
  currentTime = 0
  duration = 30
  paused = true
  seeking = false
  ended = false
  playbackRate = 1
  readyState = 4
  networkState = 1
  error = null
  buffered = new FakeRanges([[0, 30]])
  seekable = new FakeRanges([[0, 30]])
}

let now = 10_000
const diagnostics = new PlaybackDiagnostics({ sessionId: 'session-test', segmentCount: 1, chunkCount: 3, durationMs: 30_000, mimeType: 'audio/webm', localUploadComplete: true }, 'test-build', { now: () => now, wallClock: () => 1_000_000 })
const audio = new FakeAudio()
diagnostics.attachMediaElement(audio)
audio.dispatchEvent(new Event('play'))
audio.currentTime = 4
now += 600
audio.dispatchEvent(new Event('timeupdate'))
audio.currentTime = 1
now += 600
audio.dispatchEvent(new Event('timeupdate'))
const initialReport = diagnostics.freeze('test playback jump')

const floodDiagnostics = new PlaybackDiagnostics({ sessionId: 'session-test', segmentCount: 1, chunkCount: 3, durationMs: 30_000, mimeType: 'audio/webm', localUploadComplete: true }, 'test-build', { now: () => now, wallClock: () => 1_000_000 })
for (let index = 0; index < PLAYBACK_DIAGNOSTICS_LIMITS.maxEvents + 20; index += 1) floodDiagnostics.record('synthetic', { index })
const report = floodDiagnostics.freeze('test event limit')

assert.equal(report.kind, 'playback-diagnostic')
assert.equal(report.sessionId, 'session-test')
assert.ok(report.events.length <= PLAYBACK_DIAGNOSTICS_LIMITS.maxEvents)
assert.ok(report.droppedEvents > 0)
assert.ok(initialReport.events.some((event) => event.type === 'audio-mount'))
assert.ok(initialReport.events.some((event) => event.type === 'media-sample'))
assert.ok(diagnosticReportBytes(report) <= PLAYBACK_DIAGNOSTICS_LIMITS.maxReportBytes)

console.log('playback runtime diagnostics: PASS')
