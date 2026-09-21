import assert from 'node:assert/strict'
import { execFileSync } from 'node:child_process'
import { mkdtemp, readdir, readFile, rm } from 'node:fs/promises'
import { tmpdir } from 'node:os'
import { join, relative } from 'node:path'
import { fileURLToPath } from 'node:url'

const root = fileURLToPath(new URL('../..', import.meta.url))
const outputDir = await mkdtemp(join(tmpdir(), 'livenote-build-id-'))
const viteBin = join(root, 'node_modules', 'vite', 'bin', 'vite.js')

async function javascriptFiles(directory) {
  const entries = await readdir(directory, { withFileTypes: true })
  const files = []
  for (const entry of entries) {
    const path = join(directory, entry.name)
    if (entry.isDirectory()) files.push(...await javascriptFiles(path))
    else if (entry.name.endsWith('.js')) files.push(path)
  }
  return files
}

try {
  execFileSync(process.execPath, [viteBin, 'build', '--config', 'vite.control.config.ts', '--outDir', outputDir], { cwd: root, stdio: 'pipe' })
  const files = await javascriptFiles(outputDir)
  const bundle = (await Promise.all(files.map((path) => readFile(path, 'utf8')))).join('\n')
  assert.equal(bundle.includes('__LIVENOTE_BUILD_ID__'), false, `未替换的构建标识残留于 ${relative(root, outputDir)}`)
  console.log('build id config regression: PASS')
} finally {
  await rm(outputDir, { recursive: true, force: true })
}
