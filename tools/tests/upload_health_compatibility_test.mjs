import assert from 'node:assert/strict'
import fs from 'node:fs/promises'
import esbuild from 'esbuild'

const sourcePath = new URL('../../src/upload/ApiClient.ts', import.meta.url)
const source = (await fs.readFile(sourcePath, 'utf8')).replaceAll('import.meta.env', '{}')
const transformed = await esbuild.transform(source, { loader: 'ts', format: 'esm', target: 'es2022', sourcefile: sourcePath.pathname })
const moduleUrl = `data:text/javascript;base64,${Buffer.from(transformed.code).toString('base64')}`
const { REQUIRED_SERVER_STORAGE_SCHEMA, isCompatibleServerHealth } = await import(moduleUrl)

assert.equal(REQUIRED_SERVER_STORAGE_SCHEMA, 9)
assert.equal(isCompatibleServerHealth({ storageSchema: 9, capabilities: { ffmpeg: true, ffprobe: true } }), true)
assert.equal(isCompatibleServerHealth({ storageSchema: 10, capabilities: { ffmpeg: true, ffprobe: true } }), true)
assert.equal(isCompatibleServerHealth({ storageSchema: 8, capabilities: { ffmpeg: true, ffprobe: true } }), false)
assert.equal(isCompatibleServerHealth({ storageSchema: 6, capabilities: { ffmpeg: true, ffprobe: true } }), false)
assert.equal(isCompatibleServerHealth({ storageSchema: 9 }), false)

console.log('upload health compatibility regression: PASS')
