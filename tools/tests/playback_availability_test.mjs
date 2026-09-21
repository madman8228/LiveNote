import assert from 'node:assert/strict'
import fs from 'node:fs/promises'
import esbuild from 'esbuild'

const sourcePath = new URL('../../src/playback/PlaybackAvailability.ts', import.meta.url)
const source = await fs.readFile(sourcePath, 'utf8')
const transformed = await esbuild.transform(source, { loader: 'ts', format: 'esm', target: 'es2022', sourcefile: sourcePath.pathname })
const moduleUrl = `data:text/javascript;base64,${Buffer.from(transformed.code).toString('base64')}`
const { hasPlayableSession } = await import(moduleUrl)

assert.equal(hasPlayableSession({ serverOnly: true, serverChunkCount: 3, segments: [] }), true)
assert.equal(hasPlayableSession({ serverOnly: true, serverChunkCount: 0, segments: [] }), false)
assert.equal(hasPlayableSession({ serverOnly: false, segments: [{ chunks: [{ id: 'chunk-1' }] }] }), true)
assert.equal(hasPlayableSession({ serverOnly: false, segments: [{ chunks: [] }] }), false)

console.log('playback availability regression: PASS')
