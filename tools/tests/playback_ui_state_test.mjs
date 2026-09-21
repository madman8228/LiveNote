import assert from 'node:assert/strict'
import fs from 'node:fs/promises'
import esbuild from 'esbuild'

const sourcePath = new URL('../../src/playback/PlaybackUiState.ts', import.meta.url)
const source = await fs.readFile(sourcePath, 'utf8')
const transformed = await esbuild.transform(source, { loader: 'ts', format: 'esm', target: 'es2022', sourcefile: sourcePath.pathname })
const moduleUrl = `data:text/javascript;base64,${Buffer.from(transformed.code).toString('base64')}`
const { shouldShowPlaybackControls } = await import(moduleUrl)

assert.equal(shouldShowPlaybackControls(true, 'blob:preparing'), false)
assert.equal(shouldShowPlaybackControls(false, 'blob:ready'), true)
assert.equal(shouldShowPlaybackControls(false, null), false)

console.log('playback UI state regression: PASS')
